"""
FakeMailbox: one in-memory account that provides mailbox.Mailbox, for the
tests of backup, dedup, offload and restore.

Messages are Gmail API responses ({id, threadId, internalDate, labelIds,
sizeEstimate, raw}), and Trash and Spam are labels on them, as in Gmail:
plain listings leave them out. `trashed` is a view of the TRASH label, so
tests can add to it and test against it. Writes, searches and imports are
recorded in `calls`, label lookups in `lookups`, and both, in order, in
`events`.
"""

import base64
from collections.abc import MutableSet
from email.parser import BytesHeaderParser

from mailcellar import gmailapi
from test_index import make_eml

ACCOUNT = "a@example.com"
# 2026-10-22 23:30:00.000 UTC: the UTC day differs from most local days
MS = 1792711800000
HIDDEN = {"TRASH", "SPAM"}  # left out of listings unless include_spam_trash


def raw_response(gid, ms=MS, label_ids=("INBOX",), body=None):
    body = body if body is not None else make_eml(msgid=f"<{gid}@x>")
    return {
        "id": gid,
        "threadId": f"t-{gid}",
        "internalDate": str(ms),
        "labelIds": list(label_ids),
        "sizeEstimate": len(body),
        "raw": base64.urlsafe_b64encode(body).decode(),
    }


def message_id(raw_b64: str) -> str:
    """A message's Message-ID header, whitespace folded; "" when it has none."""
    raw = base64.urlsafe_b64decode(raw_b64)
    return " ".join(str(BytesHeaderParser().parsebytes(raw).get("Message-ID", "")).split())


class Trashed(MutableSet):
    """The IDs of the messages carrying TRASH; adding or removing one moves it."""

    def __init__(self, box):
        self.box = box

    def __contains__(self, gid):
        return gid in self.box.messages and "TRASH" in self.box.messages[gid]["labelIds"]

    def __iter__(self):
        return iter([g for g in self.box.messages if g in self])

    def __len__(self):
        return sum(1 for _ in self)

    def add(self, gid):
        if gid not in self:
            self.box.messages[gid]["labelIds"].append("TRASH")

    def discard(self, gid):
        if gid in self:
            self.box.messages[gid]["labelIds"].remove("TRASH")

    def update(self, gids):
        for gid in gids:
            self.add(gid)


class FakeMailbox:
    """In-memory mailbox.Mailbox; tests set the failure sets and read the records."""

    LABELS = {"INBOX": "INBOX", "Label_1": "work", "CHAT": "CHAT"}

    def __init__(
        self,
        messages: dict[str, dict] | None = None,
        account: str = ACCOUNT,
        labels: dict[str, str] | None = None,
        fail=(),
        fail_trash=(),
        fail_lookup=(),
    ):
        self.account = account
        self.messages = messages if messages is not None else {}  # gmail_id → response
        self.labels = dict(labels if labels is not None else type(self).LABELS)  # id → name
        self.fail = set(fail)  # get_raw fails for these
        self.fail_details: set[str] = set()
        self.fail_trash = set(fail_trash)
        self.fail_lookup = set(fail_lookup)
        self.calls: list[tuple] = []
        self.lookups: list[tuple] = []
        self.events: list[tuple] = []
        self.fetched: list[str] = []
        self.detail_lookups: list[str] = []
        self.queries: list[str | None] = []
        self.list_calls = 0  # whole-mailbox listings
        self.listings = 0  # labels_by_listing calls
        self.history_calls = 0
        self.imported = 0
        self.crash_after_import = False
        # History by snapshot: each history ID handed out records the mailbox
        # state, and history_changes() reports the difference from it, so
        # tests can keep editing self.messages directly.
        self.snapshots: dict[str, dict] = {}
        self.expired: set[str] = set()

    @property
    def trashed(self) -> Trashed:
        return Trashed(self)

    @classmethod
    def with_labels(cls, labels_by_id: dict[str, set[str]], **kwargs) -> FakeMailbox:
        """A mailbox from {gmail_id: label names}; TRASH puts a message in Trash."""
        box = cls(**kwargs)
        for gid, names in labels_by_id.items():
            box.messages[gid] = raw_response(gid, label_ids=[])
            box.set_labels(gid, names)
        return box

    # ── test helpers (not recorded) ──────────────────────────────────────────
    def labels_of(self, gid: str) -> set[str]:
        """A message's label names now, TRASH included."""
        return set(self.label_names(self.messages[gid]["labelIds"]))

    def set_labels(self, gid: str, names) -> None:
        """Give a message exactly these label names, as if changed in Gmail."""
        self.messages[gid]["labelIds"] = self._ids_for(sorted(set(names)))

    def _ids_for(self, names) -> list[str]:
        out = []
        for name in names:
            if name in HIDDEN:
                out.append(name)
                continue
            found = [i for i, n in self.labels.items() if n == name]
            if not found:
                found = [f"Label_{len(self.labels) + 1}"]
                self.labels[found[0]] = name
            out.append(found[0])
        return out

    def _record(self, record: list, entry: tuple) -> None:
        record.append(entry)
        self.events.append(entry)

    def _snapshot(self) -> str:
        hid = str(100 + len(self.snapshots))
        self.snapshots[hid] = {g: tuple(m["labelIds"]) for g, m in self.messages.items()}
        return hid

    # ── listing ──────────────────────────────────────────────────────────────
    def list_message_ids(self, query=None, include_spam_trash=False, label_id=None):
        self.queries.append(query)
        if query == "in:trash":
            return list(self.trashed)
        if label_id is None:
            self.list_calls += 1
        return [
            g
            for g, m in self.messages.items()
            if (include_spam_trash or not HIDDEN & set(m["labelIds"]))
            and (label_id is None or label_id in m["labelIds"])
        ]

    def labels_by_listing(self):
        self.listings += 1
        return {g: list(m["labelIds"]) for g, m in self.messages.items()}

    # ── changes ──────────────────────────────────────────────────────────────
    def current_history_id(self):
        return self._snapshot()

    def history_changes(self, start_history_id):
        self.history_calls += 1
        if start_history_id not in self.snapshots or start_history_id in self.expired:
            raise gmailapi.HistoryExpired(f"no history from {start_history_id}")
        before = self.snapshots[start_history_id]
        now = {g: tuple(m["labelIds"]) for g, m in self.messages.items()}
        deleted = set(before) - set(now)
        touched = deleted | {g for g in now if before.get(g) != now[g]}
        return touched, deleted, self._snapshot()

    # ── content ──────────────────────────────────────────────────────────────
    def get_raw(self, gmail_ids, on_message):
        """Like GmailAccount: messages gone since listing are skipped, not failures."""
        failed = []
        for gid in gmail_ids:
            self.fetched.append(gid)
            if gid in self.fail:
                failed.append(f"{gid}: HttpError 500")
            elif gid in self.messages:
                on_message(dict(self.messages[gid]))
        return failed

    def message_details(self, gmail_ids, failures=None):
        self.detail_lookups += list(gmail_ids)
        out, errors = {}, []
        for gid in gmail_ids:
            if gid in self.fail_details:
                errors.append(f"{gid}: HttpError 500")
            elif gid in self.messages:
                out[gid] = {k: v for k, v in self.messages[gid].items() if k != "raw"}
            else:
                out[gid] = None
        if errors and failures is None:
            raise gmailapi.GmailError(f"{len(errors)} lookups failed, e.g. {errors[0]}")
        if failures is not None:
            failures.extend(errors)
        return out

    # ── labels ───────────────────────────────────────────────────────────────
    def message_labels(self, gmail_ids, failures=None):
        self._record(self.lookups, ("get", sorted(gmail_ids)))
        failed = [
            f"{gid}: HttpError 400 failedPrecondition"
            for gid in gmail_ids
            if gid in self.fail_lookup
        ]
        if failed and failures is None:
            raise AssertionError("lookup failures must be collected, not raised")
        if failures is not None:
            failures.extend(failed)
        return {
            gid: self.labels_of(gid) if gid in self.messages else None
            for gid in gmail_ids
            if gid not in self.fail_lookup
        }

    def label_catalogue(self):
        return [
            {"id": i, "name": n, "type": "system" if i == n else "user"}
            for i, n in self.labels.items()
        ]

    def label_names(self, label_ids):
        """gyb-style names; TRASH and SPAM are Gmail's, whatever the catalogue lists."""
        return [
            i if i in HIDDEN else self.labels[i]
            for i in label_ids
            if i in HIDDEN or i in self.labels
        ]

    def label_ids(self, names):
        return self._ids_for(names)

    def add_label(self, gmail_ids, label_name):
        self._record(self.calls, ("label", sorted(gmail_ids), label_name))
        (label_id,) = self._ids_for([label_name])
        for gid in gmail_ids:
            if label_id not in self.messages[gid]["labelIds"]:
                self.messages[gid]["labelIds"].append(label_id)

    def remove_label(self, gmail_ids, label_name):
        self._record(self.calls, ("unlabel", sorted(gmail_ids), label_name))
        for gid in gmail_ids:
            self.messages[gid]["labelIds"] = [
                i for i in self.messages[gid]["labelIds"] if self.labels.get(i) != label_name
            ]

    # ── moves ────────────────────────────────────────────────────────────────
    def _move(self, gmail_ids, into_trash: bool) -> list[str]:
        """As GmailAccount._move: apply what can be, then report by reading back."""
        errors = {}
        for gid in gmail_ids:
            if gid in self.fail_trash:
                errors[gid] = "HttpError 500"
            elif gid in self.messages:
                (self.trashed.add if into_trash else self.trashed.discard)(gid)
        live = {gid: self.labels_of(gid) if gid in self.messages else None for gid in gmail_ids}
        return gmailapi.not_moved(gmail_ids, live, [], errors, into_trash)

    def trash(self, gmail_ids):
        self._record(self.calls, ("trash", sorted(gmail_ids)))
        return self._move(gmail_ids, into_trash=True)

    def untrash(self, gmail_ids):
        self._record(self.calls, ("untrash", sorted(gmail_ids)))
        return self._move(gmail_ids, into_trash=False)

    # ── upload ───────────────────────────────────────────────────────────────
    def _with_message_id(self, wanted: str) -> list[str]:
        if not wanted:  # nothing to match on: Gmail stores a new message
            return []
        return [g for g, m in self.messages.items() if message_id(m["raw"]) == wanted]

    def find_message_ids(self, message_ids):
        self._record(self.calls, ("search", len(message_ids)))
        return {k: self._with_message_id(m) for k, m in message_ids.items()}

    def import_messages(self, messages, on_imported):
        """Matches an import to the message holding its Message-ID, as Gmail does."""
        for key, (raw, label_ids) in messages.items():
            existing = self._with_message_id(message_id(base64.urlsafe_b64encode(raw).decode()))
            if existing:
                new_id = existing[0]
                self.trashed.discard(new_id)
            else:
                self.imported += 1
                new_id = f"n{self.imported}"
                self.messages[new_id] = raw_response(new_id, label_ids=label_ids, body=raw)
            self._record(self.calls, ("import", key, new_id, sorted(label_ids)))
            if self.crash_after_import:
                raise gmailapi.GmailError("connection lost")
            on_imported(key, new_id)
        return []
