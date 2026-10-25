"""
restore: put backed-up messages back into Gmail, from the backup.

Each message takes the first way back that applies:

  untrash    an offloaded message still in Gmail's Trash (within 30 days of
             the offload): move it out; it keeps its ID, labels and thread
  in gmail   an offloaded message already out of Trash: just record it
  present    a message that was never offloaded and is still in Gmail (or
             its Trash): nothing to do (whole-backup restores)
  found      gone, but Gmail has a message with the same Message-ID and
             content: record that one (and untrash it if it is in Trash)
  import     gone: upload the backed-up .eml with users.messages.import,
             with its labels, and index the new ID in the backup

Restoring into another account (`target`) skips the first three: Gmail IDs
mean nothing there, so every message is looked up by Message-ID.

What restore puts back loses the offload label and gains a restored/<date>
label, so a restore can be found and undone. A message whose Message-ID
Gmail already has with other content is skipped: Gmail would match the
import to that message instead of storing this one.

The steps, each returning new frozen values:
  candidates()  the backup's messages to restore (Candidate)
  plan()        reads the backup copies and Gmail, then decide() — a pure
                function of what was read — picks each one's way back
                (Decision)
  dry_run()     what apply() would do, without doing it (Result)
  apply()       does it, recording each restore as it succeeds (Result)

Nothing is changed without apply(); nothing is deleted.

Candidates can also come from elsewhere (sources.py): a pruned batch, an
mbox file or another gyb folder. They take the other-account way back, and
apply() with no backup folder records and indexes nothing.
"""

import base64
import csv
import sqlite3
import time
from dataclasses import dataclass, replace
from email.parser import BytesHeaderParser
from pathlib import Path

from mailcellar.backup import gyb, sidecar
from mailcellar.dedup.apply import DEDUP_LABEL
from mailcellar.dedup.rules import is_user_label
from mailcellar.message import PARSER_VERSION, parse_message
from mailcellar.offload.apply import OFFLOAD_LABEL, upgrade_log

# System labels messages.import accepts (tested against Gmail); CHAT fails the whole import.
IMPORT_SYSTEM_LABELS = {"INBOX", "UNREAD", "STARRED", "IMPORTANT", "SENT", "DRAFT"}
DROP_LABELS = {"TRASH", "SPAM", "CHAT", "CHATS", OFFLOAD_LABEL, DEDUP_LABEL}
# Messages read, uploaded and indexed at a time: bounds memory and the work
# a crash can leave unindexed (the next backup downloads that).
IMPORT_CHUNK = 200
# Above this many messages, plan() reads Gmail's state from two ID listings
# (500 IDs a call) rather than one lookup per message: a whole backup of
# 56,000 took over 20 minutes of quota waits looked up one by one.
LISTING_THRESHOLD = 1000
PROGRESS_EVERY = 5000  # messages between progress lines in plan()
# Measured against Gmail: about 6 imports a second in batches of 10.
IMPORTS_PER_SECOND = 6
LOG_COLUMNS = [
    "time",
    "mode",
    "account",
    "target",
    "source_id",
    "new_id",
    "outcome",
    "detail",
    "batch",
    "rule",
    "path",
]

# The ways back (Decision.way) and what a dry run says about each.
UNTRASH, IN_GMAIL, PRESENT, FOUND, IMPORT, SKIPPED = (
    "untrash",
    "in gmail",
    "present",
    "found",
    "import",
    "skipped",
)
DRY_RUN = {
    UNTRASH: "would untrash",
    IN_GMAIL: "would record (already in Gmail)",
    FOUND: "would record (found in Gmail)",
    IMPORT: "would import",
}
ALREADY = "already in Gmail"  # the outcome of PRESENT, dry run or not
DONE = ("untrashed", "recorded", "imported")  # outcomes that restored a message


@dataclass(frozen=True)
class Candidate:
    """One backed-up message to restore, with what the backup knows about it."""

    source_id: str
    batch: str  # the offload batch; "" if never offloaded
    rule: str
    path: Path | None  # the .eml; None if the index has no file for it
    labels: tuple[str, ...]  # gyb-style names, from msg-db.sqlite
    internal_date: int  # ms UTC, from meta.sqlite
    content_hash: str  # "" for a message with no text or attachments
    parser_version: str  # "" if the sidecar has no row
    offloaded: bool = True
    sender: str = ""  # the From address, lower case; "" if the sidecar has no row
    span: tuple[int, int] | None = None  # in an mbox file (path): where the message is


def read_raw(c: Candidate) -> bytes:
    """The message's bytes: its .eml file, or its entry in an mbox file."""
    if c.span is None:
        return c.path.read_bytes()
    from mailcellar.restore import mbox

    with c.path.open("rb") as fh:
        fh.seek(c.span[0])
        return mbox.message(fh.read(c.span[1] - c.span[0]))


@dataclass(frozen=True)
class Narrowing:
    """
    Further conditions on the candidates, all of which must hold; the values
    of labels and senders are alternatives (any one will do), as in offload
    rules. Dates are ms UTC: a message is before_ms when older than it,
    after_ms when as old as it or newer.
    """

    before_ms: int | None = None
    after_ms: int | None = None
    labels: tuple[str, ...] = ()  # user labels, by name, case ignored
    senders: tuple[str, ...] = ()  # an address, or "@domain"; case ignored

    def __bool__(self) -> bool:
        return bool(
            self.before_ms is not None or self.after_ms is not None or self.labels or self.senders
        )


def matches(c: Candidate, n: Narrowing) -> bool:
    """Whether a candidate meets every condition of a narrowing. Pure."""
    if n.before_ms is not None and not c.internal_date < n.before_ms:
        return False
    if n.after_ms is not None and not c.internal_date >= n.after_ms:
        return False
    if n.labels:
        wanted = {name.lower() for name in n.labels}
        if not any(label.lower() in wanted for label in c.labels if is_user_label(label)):
            return False
    if n.senders:
        senders = [s.lower() for s in n.senders]
        if not any(c.sender == s or (s.startswith("@") and c.sender.endswith(s)) for s in senders):
            return False
    return True


@dataclass(frozen=True)
class Copy:
    """What reading the backup copy found: its size and Message-ID header."""

    size: int
    message_id: str  # as written; "" if none


@dataclass(frozen=True)
class Decision:
    """A candidate's way back, and why."""

    candidate: Candidate
    way: str  # UNTRASH, IN_GMAIL, PRESENT, FOUND, IMPORT or SKIPPED
    detail: str = ""
    new_id: str = ""  # the Gmail ID it will have: its own, or the copy found
    size: int = 0

    @property
    def source_id(self) -> str:
        return self.candidate.source_id


@dataclass(frozen=True)
class Result:
    """What restoring a candidate came to."""

    decision: Decision
    outcome: str  # one of DONE, "failed", "skipped", ALREADY, or a DRY_RUN text
    detail: str = ""
    new_id: str = ""

    @property
    def source_id(self) -> str:
        return self.decision.source_id


def _open_current(folder: Path) -> sqlite3.Connection:
    path = sidecar.meta_path(folder)
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    version = con.execute("SELECT value FROM meta WHERE name = 'schema_version'").fetchone()
    if version is None or version[0] != sidecar.SCHEMA_VERSION:
        con.close()
        raise sidecar.MetaError(
            f"{path} has schema {version[0] if version else '?'}; run mailcellar meta to migrate it"
        )
    return con


def candidates(
    folder: Path,
    batches=(),
    rules=(),
    ids=(),
    everything: bool = False,
    target: str = "",
    narrow: Narrowing | None = None,
    include_restored: bool = False,
) -> list[Candidate]:
    """
    Messages of the backup in `folder` not yet restored into `target` ('' =
    the backup's own account), oldest first: the offloaded ones, narrowed
    to the given batches, rules and Gmail IDs (any of each), or with
    `everything` the whole backup (old IDs superseded by a restore apart).
    `narrow` then keeps only those that meet its dates, labels and senders.
    include_restored (for an export, which is not a restore) keeps messages
    already restored, but always leaves out the old IDs of messages restored
    under new ones, so none is listed twice.
    """
    con = _open_current(folder)
    try:
        offload = {
            gid: (batch, rule)
            for gid, batch, rule in con.execute("SELECT gmail_id, batch, rule FROM offloaded")
        }
        stored = {
            gid: (internal_date, content_hash, version, sender)
            for gid, internal_date, content_hash, version, sender in con.execute(
                """SELECT gmail_id, internal_date, content_hash, parser_version, sender_addr
                   FROM messages"""
            )
        }
        done = set() if include_restored else sidecar.restored_ids(con, target)
    finally:
        con.close()
    superseded = sidecar.superseded_ids(folder) if everything or include_restored else set()
    index = {bm.gmail_id: bm for bm in gyb.iter_backup(folder)}
    pool = set(index) | set(offload) if everything else set(offload)

    def candidate(gid: str) -> Candidate:
        batch, rule = offload.get(gid, ("", ""))
        bm = index.get(gid)
        internal_date, content_hash, version, sender = stored.get(gid, (None, "", "", ""))
        return Candidate(
            gid,
            batch,
            rule,
            bm.path if bm else None,
            tuple(bm.labels) if bm else (),
            internal_date or 0,
            content_hash or "",
            version or "",
            offloaded=gid in offload,
            sender=(sender or "").lower(),
        )

    chosen = (
        candidate(gid)
        for gid in pool - done - superseded
        if (not ids or gid in ids)
        and (not batches or offload.get(gid, ("", ""))[0] in batches)
        and (not rules or offload.get(gid, ("", ""))[1] in rules)
    )
    if narrow:
        chosen = (c for c in chosen if matches(c, narrow))
    return sorted(chosen, key=lambda c: (c.internal_date, c.source_id))


def check_backup(c: Candidate) -> tuple[str, Copy | None]:
    """(why the backup copy can't be used, or ""; what reading it found)."""
    if c.path is None:
        return "no .eml in the backup", None
    if not c.parser_version:
        return "not in the sidecar; run mailcellar meta", None
    if c.parser_version != PARSER_VERSION:
        return "sidecar row is stale; run mailcellar meta", None
    try:
        raw = read_raw(c)
        fields = parse_message(raw)
    except Exception as exc:
        return f"backup copy doesn't parse: {exc}", None
    if fields["content_hash"] != c.content_hash:
        return "backup copy differs from the sidecar", None
    return "", Copy(len(raw), message_id_header(raw))


def message_id_header(raw: bytes) -> str:
    """The Message-ID header as written, for an rfc822msgid: search; "" if none."""
    value = BytesHeaderParser().parsebytes(raw).get("Message-ID", "")
    return " ".join(str(value).split())


def import_labels(
    names, system: set[str], prefix: str = "", strip: bool = False
) -> tuple[list[str], list[str]]:
    """
    (labels to restore, labels dropped) from a message's backed-up label
    names; `system` holds the account's system label IDs (gyb stores those
    by ID, user labels by name). `prefix` nests user labels under it;
    `strip` restores none.
    """

    def dropped(name: str) -> bool:
        unsupported = (
            name in system and name not in IMPORT_SYSTEM_LABELS and not name.startswith("CATEGORY_")
        )
        return strip or name in DROP_LABELS or unsupported

    keep = [f"{prefix}/{n}" if prefix and n not in system else n for n in names if not dropped(n)]
    return keep, [n for n in names if dropped(n)]


def restored_label(now: float | None = None) -> str:
    return time.strftime("restored/%Y-%m-%d", time.localtime(now))


def decide(
    c: Candidate,
    problem: str,
    copy: Copy | None,
    *,
    labels: set[str] | None,
    same_account: bool,
    hits: list[str],
    hashes: dict[str, str],
) -> Decision:
    """
    A candidate's way back, from what was read: the backup check (`problem`,
    `copy`); in its own account, Gmail's labels for its ID (None if Gmail no
    longer has it); and the other messages with its Message-ID (`hits`)
    with their content hashes. Pure: no I/O.
    """
    if problem or copy is None:
        return Decision(c, SKIPPED, problem)
    decision = Decision(c, SKIPPED, size=copy.size)
    if same_account and labels is not None:
        if not c.offloaded:
            return replace(
                decision, way=PRESENT, detail="in Gmail's Trash" if "TRASH" in labels else ""
            )
        if "TRASH" in labels:
            return replace(decision, way=UNTRASH, new_id=c.source_id)
        return replace(decision, way=IN_GMAIL, new_id=c.source_id, detail="already out of Trash")

    same = [g for g in hits if c.content_hash and hashes.get(g) == c.content_hash]
    if same:
        return replace(decision, way=FOUND, new_id=same[0], detail=f"Gmail has it as {same[0]}")
    if hits and not c.content_hash:
        return replace(
            decision,
            detail=f"Gmail has its Message-ID ({', '.join(hits)}); with no text or "
            "attachments the content can't be compared",
        )
    if hits:
        return replace(
            decision,
            detail=f"Gmail has its Message-ID with other content ({', '.join(hits)}); "
            "an import would only match that message",
        )
    no_id = "no Message-ID: a crash during the import could leave a duplicate"
    return replace(decision, way=IMPORT, detail="" if copy.message_id else no_id)


def _quiet(message: str) -> None:
    pass


def plan(
    gmail, cands: list[Candidate], same_account: bool = True, progress=_quiet
) -> list[Decision]:
    """
    Read what deciding needs — each backup copy, Gmail's state, matching
    Message-IDs — and decide() each candidate's way back. Reads only.
    `gmail` is the account restored into. progress(message) is told each
    stage, and every PROGRESS_EVERY messages of the long ones.
    """
    checks = []
    for i, c in enumerate(cands, 1):
        if i % PROGRESS_EVERY == 0:
            progress(f"Checking backup copies: {i:,} of {len(cands):,}")
        checks.append(check_backup(c))
    usable = [c for c, (problem, _copy) in zip(cands, checks, strict=True) if not problem]
    progress(f"Checked {len(cands):,} backup copies; {len(cands) - len(usable):,} unusable")

    state: dict[str, set[str] | None] = {}
    if same_account:
        how = "listing its message IDs" if len(usable) > LISTING_THRESHOLD else "one by one"
        progress(f"Reading Gmail's state for {len(usable):,} messages, {how}")
        state = gmail_state(gmail, [c.source_id for c in usable])
    copies = {c.source_id: copy for c, (_p, copy) in zip(cands, checks, strict=True)}
    gone = [c for c in usable if not same_account or state.get(c.source_id) is None]

    # Gone: does Gmail have the Message-ID under another ID?
    if gone:
        progress(f"Searching Gmail for the Message-IDs of {len(gone):,} messages it doesn't have")
    with_id = {c.source_id: copies[c.source_id].message_id for c in gone}
    searched = gmail.find_message_ids({k: m for k, m in with_id.items() if m})
    # In its own account a message's own ID isn't another copy; elsewhere any ID is.
    hits = {
        c.source_id: [
            g for g in searched.get(c.source_id, []) if not (same_account and g == c.source_id)
        ]
        for c in gone
    }
    others = sorted({g for found in hits.values() for g in found})
    hashes: dict[str, str] = {}
    if others:
        progress(f"Comparing content with {len(others):,} messages that share a Message-ID")
        gmail.get_raw(
            others,
            lambda r: hashes.__setitem__(r["id"], parse_message(_decode(r["raw"]))["content_hash"]),
        )
    return [
        decide(
            c,
            problem,
            copy,
            labels=state.get(c.source_id),
            same_account=same_account,
            hits=hits.get(c.source_id, []),
            hashes=hashes,
        )
        for c, (problem, copy) in zip(cands, checks, strict=True)
    ]


def gmail_state(gmail, ids: list[str]) -> dict[str, set[str] | None]:
    """
    Per Gmail ID: a set holding "TRASH" if it is in Trash (empty if it is
    elsewhere in the mailbox), None if Gmail no longer has it. Large
    selections are answered from listings; only TRASH matters to decide().
    """
    if len(ids) <= LISTING_THRESHOLD:
        return gmail.message_labels(ids) if ids else {}
    present = set(gmail.list_message_ids(include_spam_trash=True))
    trash = set(gmail.list_message_ids("in:trash", include_spam_trash=True))
    return {gid: ({"TRASH"} if gid in trash else set()) if gid in present else None for gid in ids}


def _decode(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw)


def unchanged(d: Decision) -> Result | None:
    """The result of a decision that needs no action, or None if it needs one."""
    if d.way == SKIPPED:
        return Result(d, "skipped", d.detail)
    if d.way == PRESENT:
        return Result(d, ALREADY, d.detail)
    return None


def dry_run(decisions: list[Decision]) -> list[Result]:
    """What apply() would do. Pure."""
    return [unchanged(d) or Result(d, DRY_RUN[d.way], d.detail, d.new_id) for d in decisions]


def apply(
    gmail,
    folder: Path | None,
    decisions: list[Decision],
    run: str,
    label: str,
    *,
    target: str = "",
    target_folder: Path | None = None,
    prefix: str = "",
    strip: bool = False,
    progress=_quiet,
) -> list[Result]:
    """
    Carry out the decisions into `gmail`: the backup's own account, or
    `target` (whose backup is `target_folder`). Each restore is recorded in
    the backup's meta.sqlite as it succeeds, so an interrupted run carries
    on where it stopped. Returns a Result per decision, in order.

    folder None: the candidates come from a source other than a backup's
    index (sources.py). Nothing is recorded or indexed; a run again finds
    what was restored by its Message-ID, and the next backup downloads it.
    """
    by_way: dict[str, list[Decision]] = {}
    for d in decisions:
        by_way.setdefault(d.way, []).append(d)
    results = {d.source_id: r for d in decisions if (r := unchanged(d)) is not None}
    mcon = sidecar.open_meta(folder) if folder else None
    try:

        def record(d: Decision, new_id: str, how: str, outcome: str, detail: str) -> Result:
            if mcon is not None:
                sidecar.record_restored(
                    mcon, d.source_id, new_id, how, run, int(time.time() * 1000), target
                )
                mcon.commit()
            return Result(d, outcome, detail, new_id)

        # Untrash: the offloaded message itself, or the copy Gmail has in Trash.
        found = by_way.get(FOUND, [])
        found_trashed = set()
        if found:
            live = gmail.message_labels([d.new_id for d in found])
            found_trashed = {g for g, labels in live.items() if labels and "TRASH" in labels}
        to_untrash = by_way.get(UNTRASH, []) + [d for d in found if d.new_id in found_trashed]
        failed = {}
        if to_untrash:
            progress(f"Moving {len(to_untrash):,} messages out of Trash")
            failed = {f.split(":", 1)[0]: f for f in gmail.untrash([d.new_id for d in to_untrash])}
        how = {UNTRASH: "untrashed", FOUND: "found", IN_GMAIL: "in gmail"}
        for d in by_way.get(UNTRASH, []) + found + by_way.get(IN_GMAIL, []):
            if d.new_id in failed:
                results[d.source_id] = Result(d, "failed", failed[d.new_id])
            else:
                outcome = "untrashed" if d.way == UNTRASH else "recorded"
                results[d.source_id] = record(d, d.new_id, how[d.way], outcome, d.detail)

        imports = by_way.get(IMPORT, [])
        if imports:
            for r in _import(
                gmail, mcon, folder, target_folder, imports, record, prefix, strip, progress
            ):
                results[r.source_id] = r

        done = sorted({r.new_id for r in results.values() if r.outcome in DONE})
        if done:
            if not target:
                gmail.remove_label(done, OFFLOAD_LABEL)
            gmail.label_ids([label])  # creates restored/ first
            gmail.add_label(done, label)
    finally:
        if mcon is not None:
            mcon.close()
    return [results[d.source_id] for d in decisions]


def _import(
    gmail,
    mcon,
    folder: Path | None,
    target_folder: Path | None,
    imports,
    record,
    prefix,
    strip,
    progress,
) -> list[Result]:
    """
    Upload the gone messages, IMPORT_CHUNK at a time, and index each new ID
    in the backup of the account restored into: `target_folder`, or the
    backup's own `folder`; neither, for another source.
    """
    system = {label["id"] for label in gmail.label_catalogue() if label["type"] == "system"}
    index_folder = target_folder or folder
    tcon = sidecar.open_meta(target_folder) if target_folder else mcon
    results: list[Result] = []
    try:
        for start in range(0, len(imports), IMPORT_CHUNK):
            progress(f"Importing: {start:,} of {len(imports):,} done")
            chunk = imports[start : start + IMPORT_CHUNK]
            results += _import_chunk(
                gmail, tcon, index_folder, chunk, record, system, prefix, strip
            )
    finally:
        if target_folder:
            tcon.close()
    return results


def _import_chunk(gmail, tcon, folder, chunk, record, system, prefix, strip) -> list[Result]:
    results: dict[str, Result] = {}
    # source ID → (decision, raw bytes, parsed fields, labels note, label IDs)
    ready: dict[str, tuple[Decision, bytes, dict, str, list[str]]] = {}
    for d in chunk:
        raw = read_raw(d.candidate)
        fields = parse_message(raw)
        if fields["content_hash"] != d.candidate.content_hash:  # changed since plan()
            results[d.source_id] = Result(d, "skipped", "backup copy differs from the sidecar")
            continue
        keep, dropped = import_labels(d.candidate.labels, system, prefix, strip)
        note = f"labels not restored: {', '.join(dropped)}" if dropped else ""
        ready[d.source_id] = (d, raw, fields, note, gmail.label_ids(keep))
    if not ready:
        return list(results.values())

    def on_imported(source_id: str, new_id: str) -> None:
        d, _raw, _fields, note, _labels = ready[source_id]
        results[source_id] = record(d, new_id, "imported", "imported", note)

    messages = {sid: (raw, label_ids) for sid, (_d, raw, _f, _n, label_ids) in ready.items()}
    for failure in gmail.import_messages(messages, on_imported):
        sid = failure.split(":", 1)[0]
        results[sid] = Result(ready[sid][0], "failed", failure)

    imported = [r for r in results.values() if r.outcome == "imported"]
    if imported and folder is not None:
        _index_new_ids(gmail, tcon, folder, imported, {sid: v[2] for sid, v in ready.items()})
    return list(results.values())


def _index_new_ids(gmail, tcon, folder, imported: list[Result], fields: dict) -> None:
    """
    Index the new IDs in the backup, sharing the old IDs' files where they are
    on one volume, so the next backup doesn't download them again.
    """
    details = gmail.message_details([r.new_id for r in imported])
    con = gyb.open_backup(folder, gmail.account)
    try:
        for r in imported:
            d = details.get(r.new_id)
            if d is None:
                continue
            ms = int(d["internalDate"])
            names = gmail.label_names(d.get("labelIds", []))
            gyb.add_copy(con, folder, r.new_id, ms, names, r.decision.candidate.path)
            sidecar.upsert_message(
                tcon,
                r.new_id,
                thread_id=d.get("threadId", ""),
                internal_date=ms,
                size_estimate=d.get("sizeEstimate"),
                fields=fields[r.source_id],
                label_ids=d.get("labelIds", []),
            )
        con.commit()
        tcon.commit()
    finally:
        con.close()


def write_log(path: Path, results: list[Result], mode: str, account: str, target: str) -> None:
    upgrade_log(path, LOG_COLUMNS)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.is_file()
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    with path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=LOG_COLUMNS)
        if new:
            w.writeheader()
        for r in results:
            c = r.decision.candidate
            w.writerow(
                {
                    "time": now,
                    "mode": mode,
                    "account": account,
                    "target": target,
                    "source_id": c.source_id,
                    "new_id": r.new_id,
                    "outcome": r.outcome,
                    "detail": r.detail,
                    "batch": c.batch,
                    "rule": c.rule,
                    "path": str(c.path or ""),
                }
            )
