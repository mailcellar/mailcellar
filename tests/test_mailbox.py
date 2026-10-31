"""mailbox: the Mailbox protocol, open_mailbox and opener."""

import base64
import inspect
from argparse import Namespace
from pathlib import Path

import pytest

from fake_mailbox import ACCOUNT, FakeMailbox, message_id, raw_response
from mailcellar import gmailapi, mailbox
from mailcellar.backup import commands
from mailcellar.paths import backup_folder
from test_backup import FakeService, Req, http_error
from test_index import make_eml


def protocol_methods():
    return {
        name: member
        for name, member in vars(mailbox.Mailbox).items()
        if inspect.isfunction(member) and not name.startswith("_")
    }


def parameters(function):
    return [(p.name, p.kind, p.default) for p in inspect.signature(function).parameters.values()]


def test_gmail_account_has_every_mailbox_method_with_the_same_parameters():
    assert len(protocol_methods()) == 21
    for name, method in protocol_methods().items():
        real = getattr(gmailapi.GmailAccount, name, None)
        assert real is not None, f"GmailAccount has no {name}"
        assert parameters(real) == parameters(method), name
    gmail = gmailapi.GmailAccount(ACCOUNT, None, service=FakeService({}), sleep=lambda s: None)
    assert gmail.account == ACCOUNT


def test_a_subclass_missing_a_method_fails_when_it_is_called():
    class Partial(mailbox.Mailbox):
        account = ACCOUNT

    with pytest.raises(NotImplementedError):
        Partial().trash(["g1"])


def test_open_mailbox_signs_in_with_the_scope_and_opens_the_account(monkeypatch, tmp_path):
    calls = []

    def load(folder, account, scope, interactive=True):
        calls.append((folder, account, scope, interactive))
        return "creds"

    monkeypatch.setattr(gmailapi, "load_credentials", load)
    monkeypatch.setattr(gmailapi, "GmailAccount", lambda account, creds: (account, creds))
    got = mailbox.open_mailbox("alex@example.com", tmp_path, "modify", interactive=False)
    assert got == ("alex@example.com", "creds")
    assert calls == [(tmp_path, "alex@example.com", "modify", False)]


def test_opener_finds_each_accounts_config_folder(monkeypatch, tmp_path):
    opened = []
    monkeypatch.setattr(gmailapi, "find_config_folder", lambda root, account: root / account)
    monkeypatch.setattr(
        mailbox,
        "open_mailbox",
        lambda account, config, scope, interactive=True, folder=None: opened.append(
            (account, config, scope, interactive, folder)
        ),
    )
    mailbox.opener(tmp_path)("alex@example.com", "modify")
    backup = backup_folder("alex@example.com")  # for an IMAP account's UID map
    assert opened == [("alex@example.com", tmp_path / "alex@example.com", "modify", True, backup)]
    mailbox.opener(tmp_path, {"alex@example.com": tmp_path / "elsewhere"})("alex@example.com", "x")
    assert opened[-1][-1] == tmp_path / "elsewhere"  # the command's own folder for it


@pytest.mark.parametrize("unattended", [False, True])
def test_backup_signs_in_interactively_unless_unattended(monkeypatch, unattended):
    opened = []
    monkeypatch.setattr(gmailapi, "resolve_account", lambda root, name: (ACCOUNT, Path("cfg")))
    monkeypatch.setattr(
        mailbox,
        "open_mailbox",
        lambda account, config_folder, scope, interactive=True, folder=None: opened.append(
            (interactive, folder)
        ),
    )
    args = Namespace(config="cfg", account=ACCOUNT, folder="data", unattended=unattended)
    commands.connect(args)
    assert opened == [(not unattended, Path("data"))]  # the backup folder, for an IMAP map


# ── contract: FakeMailbox and GmailAccount agree ─────────────────────────────
#
# The same scenarios run against FakeMailbox and against GmailAccount over
# GmailService, a stand-in for Gmail's service object. That checks the fake
# against GmailAccount's own logic: batching, label names, the read-back
# after a move, Message-ID searches, history. It can't check Gmail itself:
# GmailService answers as Gmail was observed to (Trash and Spam as labels
# left out of plain listings, 404 for a message that is gone, an import
# matched by Message-ID, which takes the message out of Trash: tested
# against Gmail on 2026-10-25), and the fake makes the same assumptions.


class GmailService(FakeService):
    """
    Enough of Gmail's service object for GmailAccount, holding a mailbox:
    labels, messages (list, get, batchModify, import) and history; batches
    are FakeService's.
    """

    def __init__(self, labels: dict[str, str]):
        super().__init__({})
        self.labels_ = [
            {"id": i, "name": n, "type": "system" if i == n else "user"} for i, n in labels.items()
        ]
        self.messages_: dict[str, dict] = {}
        self.history_: list[tuple[int, str, str]] = []  # (historyId, kind, gmail_id)
        self.imported = 0

    def add(self, response: dict) -> None:
        self.messages_[response["id"]] = dict(response, labelIds=list(response["labelIds"]))

    def _event(self, kind, gid):
        self.history_.append((len(self.history_) + 1, kind, gid))

    def users(self):
        return self

    def getProfile(self, userId):
        return Req(lambda: {"emailAddress": ACCOUNT, "historyId": str(len(self.history_))})

    def labels(self):
        service = self

        class Labels:
            def list(self, userId):
                return Req(lambda: {"labels": [dict(label) for label in service.labels_]})

            def create(self, userId, body):
                def create():
                    label = {"id": f"Label_{len(service.labels_) + 1}", "type": "user", **body}
                    service.labels_.append(label)
                    return label

                return Req(create)

        return Labels()

    def history(self):
        service = self

        class History:
            def list(self, userId, startHistoryId, **kwargs):
                start = int(startHistoryId)
                kinds = {}
                for hid, kind, gid in service.history_:
                    if hid > start:
                        kinds.setdefault(kind, []).append({"message": {"id": gid}})
                return Req(lambda: {"history": [kinds], "historyId": str(len(service.history_))})

        return History()

    def messages(self):
        service = self

        class Messages:
            def list(self, userId, q=None, includeSpamTrash=False, labelIds=(), **kwargs):
                def run():
                    out = []
                    for gid, m in service.messages_.items():
                        if not includeSpamTrash and {"TRASH", "SPAM"} & set(m["labelIds"]):
                            continue
                        if labelIds and not set(labelIds) <= set(m["labelIds"]):
                            continue
                        if q and q.startswith("rfc822msgid:"):
                            if message_id(m["raw"]) != q.removeprefix("rfc822msgid:"):
                                continue
                        out.append({"id": gid})
                    return {"messages": out} if out else {}

                return Req(run)

            def get(self, userId, id, format="full", fields=None):
                def run():
                    if id not in service.messages_:
                        raise http_error(404)
                    m = dict(
                        service.messages_[id], labelIds=list(service.messages_[id]["labelIds"])
                    )
                    if format != "raw":
                        m.pop("raw")
                    return m

                return Req(run)

            def batchModify(self, userId, body):
                def run():
                    for gid in body["ids"]:
                        labels = service.messages_[gid]["labelIds"]
                        for label in body.get("addLabelIds", []):
                            if label not in labels:
                                labels.append(label)
                                service._event("labelsAdded", gid)
                        for label in body.get("removeLabelIds", []):
                            if label in labels:
                                labels.remove(label)
                                service._event("labelsRemoved", gid)
                    return {}

                return Req(run)

            def import_(self, userId, body, media_body=None, **params):
                def run():
                    raw = base64.urlsafe_b64decode(body["raw"])
                    wanted = message_id(body["raw"])
                    for gid, m in service.messages_.items():
                        if wanted and message_id(m["raw"]) == wanted:
                            if "TRASH" in m["labelIds"]:
                                m["labelIds"].remove("TRASH")
                            return {"id": gid}
                    service.imported += 1
                    gid = f"n{service.imported}"
                    service.add(raw_response(gid, label_ids=body["labelIds"], body=raw))
                    service._event("messagesAdded", gid)
                    return {"id": gid}

                return Req(run)

        return Messages()


CONTRACT_LABELS = {"INBOX": "INBOX", "TRASH": "TRASH", "Label_1": "work"}


def mailboxes():
    """The same mailbox twice: as FakeMailbox, and as GmailAccount over GmailService."""
    spec = {
        "g1": (["INBOX", "Label_1"], False),
        "g2": (["Label_1"], False),
        "g3": (["INBOX"], True),  # in Trash
    }
    fake = FakeMailbox(labels=CONTRACT_LABELS)
    service = GmailService(CONTRACT_LABELS)
    for gid, (label_ids, trashed) in spec.items():
        response = raw_response(gid, label_ids=label_ids)
        fake.messages[gid] = dict(response, labelIds=list(label_ids))
        if trashed:
            fake.trashed.add(gid)
        service.add(dict(response, labelIds=label_ids + (["TRASH"] if trashed else [])))
    real = gmailapi.GmailAccount(ACCOUNT, None, service=service, sleep=lambda s: None)
    return {"fake": fake, "real": real}


def both(scenario):
    """scenario(mailbox) on each; their results must be equal."""
    results = {name: scenario(box) for name, box in mailboxes().items()}
    assert results["fake"] == results["real"]
    return results["real"]


def test_contract_listing():
    both(lambda m: sorted(m.list_message_ids()))
    both(lambda m: sorted(m.list_message_ids(include_spam_trash=True)))
    both(lambda m: sorted(m.list_message_ids(label_id="Label_1")))
    both(lambda m: {g: sorted(ids) for g, ids in m.labels_by_listing().items()})


def test_contract_labels():
    both(lambda m: m.message_labels(["g1", "g3", "gone"]))
    both(lambda m: (m.label_names(["INBOX", "Label_1", "TRASH"]), m.label_catalogue()))

    def label_and_unlabel(m):
        m.add_label(["g1", "g2"], "keep")
        added = m.message_labels(["g1", "g2"])
        m.remove_label(["g1"], "keep")
        return added, m.message_labels(["g1", "g2"]), m.label_ids(["keep", "INBOX"])

    both(label_and_unlabel)


def test_contract_trash_and_untrash():
    def move(m):
        failed = m.trash(["g1", "gone"])
        listed = sorted(m.list_message_ids())
        labels = m.message_labels(["g1"])
        return failed, listed, labels, m.untrash(["g1", "g3"]), sorted(m.list_message_ids())

    both(move)


def test_contract_content():
    def content(m):
        got = []
        failed = m.get_raw(["g1", "g3", "gone"], got.append)
        return failed, sorted(got, key=lambda r: r["id"]), m.message_details(["g1", "g3", "gone"])

    both(content)


def test_contract_upload():
    def upload(m):
        again = raw_response("x", body=make_eml(msgid="<g2@x>"))  # g2's Message-ID
        new = raw_response("x", body=make_eml(msgid="<new@x>"))
        imported = {}
        failed = m.import_messages(
            {
                "k1": (base64.urlsafe_b64decode(again["raw"]), ["INBOX"]),
                "k2": (base64.urlsafe_b64decode(new["raw"]), ["Label_1"]),
                # no Message-ID: nothing to match, so each is a message of its own
                "k3": (make_eml(msgid=None, body="one"), ["INBOX"]),
                "k4": (make_eml(msgid=None, body="two"), ["INBOX"]),
            },
            lambda key, gid: imported.__setitem__(key, gid),
        )
        found = m.find_message_ids({"a": "<g2@x>", "b": "<new@x>", "c": "<none@x>"})
        return failed, imported, found, m.message_labels([imported["k2"]])

    both(upload)


def test_contract_history():
    def changes(m):
        start = m.current_history_id()
        m.add_label(["g2"], "keep")
        touched, deleted, _latest = m.history_changes(start)
        return touched, deleted

    both(changes)
