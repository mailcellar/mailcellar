"""
restore: put backed-up messages back into Gmail, from the backup.

Each message takes the first way back that applies:

  untrash    an offloaded message still in Gmail's Trash (within 30 days of
             the offload): move it out; it keeps its ID, labels and thread
  in gmail   an offloaded message already out of Trash: just record it
  present    a message that was never offloaded and is still in Gmail (or
             its Trash), or one restored before whose copy the mailbox still
             has: nothing to do
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
  candidates()  the backup's messages to restore (Candidate): read_snapshot()
                reads what the backup knows, select() — pure — chooses
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
import email.utils
import hashlib
import logging
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from email.parser import BytesHeaderParser
from pathlib import Path
from types import MappingProxyType

from mailcellar.backup import chain, gyb, sidecar
from mailcellar.dedup.apply import DEDUP_LABEL
from mailcellar.dedup.rules import is_user_label
from mailcellar.mailbox import mailbox_name
from mailcellar.message import PARSER_VERSION, parse_message
from mailcellar.offload.apply import OFFLOAD_LABEL, upgrade_log
from mailcellar.progress import Progress

logger = logging.getLogger(__name__)

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
    IN_GMAIL: "would record (already in the mailbox)",
    FOUND: "would record (found in the mailbox)",
    IMPORT: "would import",
}
ALREADY = "already in the mailbox"  # the outcome of PRESENT, dry run or not; Gmail or IMAP
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
    sha256: str = ""  # the .eml's SHA-256 as a hash chain recorded it; "" if none did


def dated(raw: bytes, internal_date_ms: int) -> bytes | None:
    """
    The message with a Date header from the date the backup recorded, when
    its own is missing or can't be read; None when it needs none, or there
    is no recorded date (an mbox entry). Gmail dates an import by its Date
    header and, without a usable one, by the moment of the import. Pure.
    """
    from mailcellar.restore.mbox import header_bytes, header_end

    if internal_date_ms <= 0:
        return None
    current = header_bytes(raw, b"date")
    if current is not None:
        try:
            if email.utils.parsedate_to_datetime(current.decode("latin-1")) is not None:
                return None
        except TypeError, ValueError, IndexError:
            pass
    end = header_end(raw)
    eol = b"\r\n" if raw[end : end + 2] == b"\r\n" else b"\n"
    line = b"Date: " + email.utils.formatdate(internal_date_ms / 1000, usegmt=True).encode()
    if current is None:
        if end == 0:  # no headers at all
            return line + eol + raw
        return raw[:end] + eol + line + raw[end:]
    out, replacing = [], False
    for old in raw[:end].splitlines(keepends=True):
        if replacing and old[:1] in (b" ", b"\t"):
            continue  # a continuation of the unreadable Date
        replacing = False
        if old.split(b":", 1)[0].strip().lower() == b"date" and line:
            ending = old[len(old.rstrip(b"\r\n")) :]
            out.append(line + ending)
            line, replacing = b"", True
        else:
            out.append(old)
    return b"".join(out) + raw[end:]


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


@dataclass(frozen=True)
class Snapshot:
    """What a backup knows that candidates are chosen from: read by read_snapshot()."""

    offload: Mapping[str, tuple[str, str]]  # offloaded ID → (batch, rule)
    # ID → (internal date, content hash, parser version, sender), from meta.sqlite
    stored: Mapping[str, tuple[int | None, str | None, str | None, str | None]]
    index: Mapping[str, gyb.BackupMessage]  # what gyb's index has, with its .eml
    recorded: Mapping[str, str]  # ID → the .eml's SHA-256, as the hash chain recorded it
    restored: frozenset[str]  # source IDs restored into the target already
    forgotten: frozenset[str]  # deleted from the backup by offload forget
    superseded: frozenset[str]  # old IDs of messages restored under new ones
    marks: frozenset[str]  # an IMAP backup's review marks: never put back


@dataclass(frozen=True)
class Selection:
    """Which candidates to take from a Snapshot: see candidates()."""

    batches: frozenset[str] = frozenset()
    rules: frozenset[str] = frozenset()
    ids: frozenset[str] = frozenset()
    everything: bool = False
    narrow: Narrowing | None = None
    include_restored: bool = False
    offloaded_only: bool = False


def read_snapshot(folder: Path, target: str = "") -> Snapshot:
    """What the backup in `folder` knows, with what it restored into `target`. Read-only."""
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
        restored = frozenset(sidecar.restored_ids(con, target))
        forgotten = frozenset(sidecar.forgotten_ids(con))
        superseded = frozenset(sidecar.superseded(con))
        marks = sidecar.mark_labels(con)
    finally:
        con.close()
    return Snapshot(
        MappingProxyType(offload),
        MappingProxyType(stored),
        MappingProxyType({bm.gmail_id: bm for bm in gyb.iter_backup(folder)}),
        MappingProxyType(chain.held(folder)),
        restored,
        forgotten,
        superseded,
        marks,
    )


def select(snap: Snapshot, sel: Selection) -> list[Candidate]:
    """The candidates `sel` takes from `snap`, oldest first (see candidates()). Pure."""
    whole = sel.everything or (bool(sel.ids) and not sel.offloaded_only)
    pool = set(snap.index) | set(snap.offload) if whole else set(snap.offload)
    left_out = snap.forgotten | (frozenset() if sel.include_restored else snap.restored)
    if whole or sel.include_restored:
        left_out |= snap.superseded

    def candidate(gid: str) -> Candidate:
        batch, rule = snap.offload.get(gid, ("", ""))
        bm = snap.index.get(gid)
        internal_date, content_hash, version, sender = snap.stored.get(gid, (None, "", "", ""))
        return Candidate(
            gid,
            batch,
            rule,
            bm.path if bm else None,
            tuple(label for label in bm.labels if label not in snap.marks) if bm else (),
            internal_date or 0,
            content_hash or "",
            version or "",
            offloaded=gid in snap.offload,
            sender=(sender or "").lower(),
            sha256=snap.recorded.get(gid, ""),
        )

    chosen = (
        candidate(gid)
        for gid in pool - left_out
        if (not sel.ids or gid in sel.ids)
        and (not sel.batches or snap.offload.get(gid, ("", ""))[0] in sel.batches)
        and (not sel.rules or snap.offload.get(gid, ("", ""))[1] in sel.rules)
    )
    if sel.narrow:
        chosen = (c for c in chosen if matches(c, sel.narrow))
    return sorted(chosen, key=lambda c: (c.internal_date, c.source_id))


def candidates(
    folder: Path,
    batches=(),
    rules=(),
    ids=(),
    everything: bool = False,
    target: str = "",
    narrow: Narrowing | None = None,
    include_restored: bool = False,
    offloaded_only: bool = False,
) -> list[Candidate]:
    """
    Messages of the backup in `folder` not yet restored into `target` ('' =
    the backup's own account), oldest first: the offloaded ones, narrowed
    to the given batches, rules and Gmail IDs (any of each), or with
    `everything` the whole backup (old IDs superseded by a restore apart).
    Gmail IDs are looked for in the whole backup too, unless `offloaded_only`.
    `narrow` then keeps only those that meet its dates, labels and senders.
    include_restored keeps messages already restored, for an export (which is
    not a restore) and for a restore, whose plan() counts one as done only
    while the mailbox still has it; the old IDs of messages restored under
    new ones are always left out, so none is listed twice.
    Messages deleted from the backup by offload forget are never candidates
    (see forgotten_in). read_snapshot() reads; select() chooses.
    """
    selection = Selection(
        batches=frozenset(batches),
        rules=frozenset(rules),
        ids=frozenset(ids),
        everything=everything,
        narrow=narrow,
        include_restored=include_restored,
        offloaded_only=offloaded_only,
    )
    return select(read_snapshot(folder, target), selection)


def restored_into(folder: Path, target: str = "") -> dict[str, str]:
    """
    Source ID → the ID it went back under, for what the backup in `folder`
    has restored into `target` ('' = its own account). Read-only.
    """
    con = _open_current(folder)
    try:
        return sidecar.restored_as(con, target)
    finally:
        con.close()


def forgotten_in(folder: Path, batches=(), rules=(), ids=()) -> int:
    """How many forgotten messages a selection by batch, rule or ID would have taken."""
    con = _open_current(folder)
    try:
        rows = con.execute("SELECT gmail_id, batch, rule FROM forgotten").fetchall()
    finally:
        con.close()
    return sum(
        (not ids or gid in ids)
        and (not batches or batch in batches)
        and (not rules or rule in rules)
        for gid, batch, rule in rows
    )


CHANGED_SINCE_RECORDED = "backup copy differs from what its hash chain recorded"


def matches_chain(raw: bytes, c: Candidate) -> bool:
    """
    Whether the copy's bytes are those a hash chain recorded, when one did.
    The content hash ignores headers and links, so this is the check that a
    file wasn't altered or damaged since it was backed up. Pure.
    """
    return not c.sha256 or hashlib.sha256(raw).hexdigest() == c.sha256


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
    if not matches_chain(raw, c):
        return CHANGED_SINCE_RECORDED, None
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
    provider: str,
    restored_as: str = "",
) -> Decision:
    """
    A candidate's way back, from what was read: the backup check (`problem`,
    `copy`); the ID an earlier restore put it back under, if the mailbox
    still has that copy (`restored_as`); in its own account, the mailbox's
    labels for its ID (None if it no longer has it); and the other messages
    with its Message-ID (`hits`) with their content hashes. Details name the
    mailbox by `provider` (mailbox_name). Pure: no I/O.
    """
    where, start = mailbox_name(provider), mailbox_name(provider, start=True)
    if problem or copy is None:
        return Decision(c, SKIPPED, problem)
    decision = Decision(c, SKIPPED, size=copy.size)
    if restored_as:  # put back before and still there; gone since, it takes a way back again
        as_ = "" if restored_as == c.source_id else f", as {restored_as}"
        return replace(decision, way=PRESENT, detail=f"restored before{as_}")
    if same_account and labels is not None:
        if not c.offloaded:
            return replace(
                decision, way=PRESENT, detail=f"in {where}'s Trash" if "TRASH" in labels else ""
            )
        if "TRASH" in labels:
            return replace(decision, way=UNTRASH, new_id=c.source_id)
        return replace(decision, way=IN_GMAIL, new_id=c.source_id, detail="already out of Trash")

    same = [g for g in hits if c.content_hash and hashes.get(g) == c.content_hash]
    if same:
        return replace(decision, way=FOUND, new_id=same[0], detail=f"{start} has it as {same[0]}")
    if hits and not c.content_hash:
        return replace(
            decision,
            detail=f"{start} has its Message-ID ({', '.join(hits)}); with no text or "
            "attachments the content can't be compared",
        )
    if hits:
        return replace(
            decision,
            detail=f"{start} has its Message-ID with other content ({', '.join(hits)}); "
            "an import would only match that message",
        )
    no_id = "no Message-ID: a crash during the import could leave a duplicate"
    return replace(decision, way=IMPORT, detail="" if copy.message_id else no_id)


def _quiet(message: str) -> None:
    pass


def plan(
    gmail,
    cands: list[Candidate],
    same_account: bool = True,
    progress=_quiet,
    restored: Mapping[str, str] | None = None,
) -> list[Decision]:
    """
    Read what deciding needs — each backup copy, Gmail's state, matching
    Message-IDs — and decide() each candidate's way back. Reads only.
    `gmail` is the account restored into; `restored` maps the source IDs an
    earlier restore put into it to the IDs they went back under, which count
    as done only while the mailbox still has them. progress(message) is told
    each stage; the long ones also show a Progress.
    """
    checks = []
    with Progress("Checking backup copies", len(cands)) as checking:
        for c in cands:
            checks.append(check_backup(c))
            checking.advance()
    usable = [c for c, (problem, _copy) in zip(cands, checks, strict=True) if not problem]
    progress(f"Checked {len(cands):,} backup copies; {len(cands) - len(usable):,} unusable")

    where = mailbox_name(gmail.provider)
    again = {c.source_id: restored[c.source_id] for c in usable if c.source_id in (restored or {})}
    read = [c.source_id for c in usable] if same_account else []
    read = sorted(set(read) | set(again.values()))
    state: dict[str, set[str] | None] = {}
    if read:
        how = "listing its message IDs" if len(read) > LISTING_THRESHOLD else "one by one"
        progress(f"Reading {where}'s state for {len(read):,} messages, {how}")
        state = gmail_state(gmail, read)
    still = {sid: new for sid, new in again.items() if state.get(new) is not None}
    copies = {c.source_id: copy for c, (_p, copy) in zip(cands, checks, strict=True)}
    gone = [
        c
        for c in usable
        if c.source_id not in still and (not same_account or state.get(c.source_id) is None)
    ]

    # Gone: does the mailbox have the Message-ID under another ID?
    if gone:
        progress(f"Searching {where} for the Message-IDs of {len(gone):,} messages it doesn't have")
    with_id = {c.source_id: copies[c.source_id].message_id for c in gone}
    wanted = {k: m for k, m in with_id.items() if m}
    with Progress("Searching for Message-IDs", len(wanted)):
        searched = gmail.find_message_ids(wanted)
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
        with Progress("Reading messages that share a Message-ID", len(others)):
            gmail.get_raw(
                others,
                lambda r: hashes.__setitem__(
                    r["id"], parse_message(_decode(r["raw"]))["content_hash"]
                ),
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
            provider=gmail.provider,
            restored_as=still.get(c.source_id, ""),
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
        with Progress(f"Reading {mailbox_name(gmail.provider)}'s state", len(ids)):
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
    on_result=None,
) -> list[Result]:
    """
    Carry out the decisions into `gmail`: the backup's own account, or
    `target` (whose backup is `target_folder`). Each restore is recorded in
    the backup's meta.sqlite as it succeeds, so an interrupted run carries
    on where it stopped. Returns a Result per decision, in order;
    on_result(result) is called with each as soon as it is final, so a log
    kept that way survives an interruption; and again for one a mark then
    failed on (a note added), so its last call is the one that counts.

    folder None: the candidates come from a source other than a backup's
    index (sources.py). Nothing is recorded or indexed; a run again finds
    what was restored by its Message-ID, and the next backup downloads it.
    """
    by_way: dict[str, list[Decision]] = {}
    for d in decisions:
        by_way.setdefault(d.way, []).append(d)
    results: dict[str, Result] = {}

    def emit(r: Result) -> Result:
        if r.source_id not in results and on_result is not None:
            on_result(r)
        results[r.source_id] = r
        return r

    for d in decisions:
        if (r := unchanged(d)) is not None:
            emit(r)
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
            with Progress("Moving out of Trash", len(to_untrash)):
                untrashed = gmail.untrash([d.new_id for d in to_untrash])
            failed = {f.split(":", 1)[0]: f for f in untrashed}
        how = {UNTRASH: "untrashed", FOUND: "found", IN_GMAIL: "in gmail"}
        for d in by_way.get(UNTRASH, []) + found + by_way.get(IN_GMAIL, []):
            if d.new_id in failed:
                emit(Result(d, "failed", failed[d.new_id]))
            else:
                outcome = "untrashed" if d.way == UNTRASH else "recorded"
                emit(record(d, d.new_id, how[d.way], outcome, d.detail))

        imports = by_way.get(IMPORT, [])
        if imports:
            _import(
                gmail, mcon, folder, target_folder, imports, record, emit, prefix, strip, progress
            )

        done = sorted({r.new_id for r in results.values() if r.outcome in DONE})
        if done:
            kept = unmarked(gmail.remove_label(done, OFFLOAD_LABEL)) if not target else {}
            gmail.label_ids([label])  # creates restored/ first
            unlabelled = unmarked(gmail.add_label(done, label, review=False))
            noted = noted_results(results, mark_notes(kept, unlabelled, label))
            results |= noted
            for r in noted.values():
                if on_result is not None:  # logged again, noted: its last row counts
                    on_result(r)
            for count, what in (
                (len(kept), f"kept {OFFLOAD_LABEL}"),
                (len(unlabelled), f"couldn't be labelled {label}"),
            ):
                if count:
                    logger.warning(
                        f"{count:,} restored messages {what}; they are restored all the same "
                        "(the log says why)"
                    )
    finally:
        if mcon is not None:
            mcon.close()
    return [results[d.source_id] for d in decisions]


def unmarked(failures) -> dict[str, str]:
    """
    add_label's or remove_label's failures ("<id>: <why>") as ID → why, an
    ID's several reasons joined in order. Pure.
    """
    reasons: dict[str, list[str]] = {}
    for failure in failures:
        gid, why = failure.split(": ", 1)
        reasons.setdefault(gid, []).append(why)
    return {gid: "; ".join(whys) for gid, whys in reasons.items()}


def mark_notes(
    kept: Mapping[str, str], unlabelled: Mapping[str, str], label: str
) -> dict[str, str]:
    """
    ID → the note on a restored message whose marks went wrong: the offload
    mark left on (`kept`), restore's label not added (`unlabelled`). Pure.
    """
    wrong = ((f"{OFFLOAD_LABEL} not taken off", kept), (f"not labelled {label}", unlabelled))
    return {
        i: "; ".join(f"{what}: {by_id[i]}" for what, by_id in wrong if i in by_id)
        for i in sorted(kept.keys() | unlabelled.keys())
    }


def noted_results(results: Mapping[str, Result], notes: Mapping[str, str]) -> dict[str, Result]:
    """
    Source ID → the result with its new ID's note added, for the results this
    run restored (outcome in DONE) only: a skipped or already-restored one
    sharing the new ID wasn't acted on. Pure.
    """
    return {
        source_id: with_note(r, notes[r.new_id])
        for source_id, r in results.items()
        if r.outcome in DONE and r.new_id in notes
    }


def with_note(r: Result, note: str) -> Result:
    """The result with `note` added to its detail. Pure."""
    return replace(r, detail=f"{r.detail}; {note}" if r.detail else note)


def _import(
    gmail,
    mcon,
    folder: Path | None,
    target_folder: Path | None,
    imports,
    record,
    emit,
    prefix,
    strip,
    progress,
) -> None:
    """
    Upload the gone messages, IMPORT_CHUNK at a time, and index each new ID
    in the backup of the account restored into: `target_folder`, or the
    backup's own `folder`; neither, for another source. Each result goes
    to emit() as soon as it is known.
    """
    system = {label["id"] for label in gmail.label_catalogue() if label["type"] == "system"}
    index_folder = target_folder or folder
    tcon = sidecar.open_meta(target_folder) if target_folder else mcon
    if tcon is not None and gmail.provider != "gmail":  # its IDs aren't Gmail's, as backup marks
        sidecar.set_state(tcon, "provider", gmail.provider)
        tcon.commit()
    try:
        progress(f"Importing {len(imports):,} messages")
        # Advanced per message from inside the import; IMAP's scans there keep this line.
        with Progress("Importing", len(imports), steps_inside=True):
            for start in range(0, len(imports), IMPORT_CHUNK):
                chunk = imports[start : start + IMPORT_CHUNK]
                _import_chunk(gmail, tcon, index_folder, chunk, record, emit, system, prefix, strip)
    finally:
        if target_folder:
            tcon.close()


def _label_ids(gmail, names: list[str]) -> tuple[list[str], list[str]]:
    """
    (label IDs, names Gmail refused): the labels are created as needed, and
    one Gmail won't create costs the message that label, not the restore.
    """
    from mailcellar import gmailapi

    try:
        return gmail.label_ids(names), []
    except gmailapi.GmailError:
        ids, refused = [], []
        for name in names:
            try:
                ids += gmail.label_ids([name])
            except gmailapi.GmailError as exc:
                logger.warning(str(exc))
                refused.append(name)
        return ids, refused


def _import_chunk(gmail, tcon, folder, chunk, record, emit, system, prefix, strip) -> None:
    results: dict[str, Result] = {}
    # source ID → (decision, raw bytes, parsed fields, labels note, label IDs)
    ready: dict[str, tuple[Decision, bytes, dict, str, list[str]]] = {}
    for d in chunk:
        raw = read_raw(d.candidate)
        fields = parse_message(raw)
        if not matches_chain(raw, d.candidate):  # changed since plan()
            results[d.source_id] = emit(Result(d, "skipped", CHANGED_SINCE_RECORDED))
            continue
        if fields["content_hash"] != d.candidate.content_hash:  # changed since plan()
            results[d.source_id] = emit(
                Result(d, "skipped", "backup copy differs from the sidecar")
            )
            continue
        keep, dropped = import_labels(d.candidate.labels, system, prefix, strip)
        label_ids, refused = _label_ids(gmail, keep)
        dropped += [
            f"{name} ({mailbox_name(gmail.provider, start=True)} refused it)" for name in refused
        ]
        notes = [f"labels not restored: {', '.join(dropped)}"] if dropped else []
        # Gmail dates an import by its Date header, so one without gets the backup's
        # date; an IMAP server takes the date apart (INTERNALDATE), and adding a
        # header there would change the bytes, and so the ID it comes back under.
        redated = dated(raw, d.candidate.internal_date) if gmail.provider == "gmail" else None
        if redated is not None:
            raw = redated  # the backup's file is left as it is
            notes.append("Date header added from the backup's date")
        ready[d.source_id] = (d, raw, fields, "; ".join(notes), label_ids)
    if not ready:
        return

    def on_imported(source_id: str, new_id: str) -> None:
        d, _raw, _fields, note, _labels = ready[source_id]
        results[source_id] = emit(record(d, new_id, "imported", "imported", note))

    messages = {sid: (raw, label_ids) for sid, (_d, raw, _f, _n, label_ids) in ready.items()}
    dates = {sid: v[0].candidate.internal_date for sid, v in ready.items()}
    for failure in gmail.import_messages(messages, on_imported, dates):
        sid = failure.split(":", 1)[0]
        results[sid] = emit(Result(ready[sid][0], "failed", failure))

    imported = [r for r in results.values() if r.outcome == "imported"]
    if imported and folder is not None:
        _index_new_ids(gmail, tcon, folder, imported, {sid: v[2] for sid, v in ready.items()})


def _index_new_ids(gmail, tcon, folder, imported: list[Result], fields: dict) -> None:
    """
    Index the new IDs in the backup, sharing the old IDs' files where they are
    on one volume, so the next backup doesn't download them again.
    """
    # Its own Progress, innermost, so these lookups don't count as imports.
    with Progress("Reading the imported messages", len(imported)):
        details = gmail.message_details([r.new_id for r in imported])
    con = gyb.open_backup(folder, gmail.account)
    added = []
    try:
        for r in imported:
            d = details.get(r.new_id)
            if d is None:
                continue
            ms = int(d["internalDate"])
            names = gmail.label_names(d.get("labelIds", []))
            rel = gyb.add_copy(con, folder, r.new_id, ms, names, r.decision.candidate.path)
            added.append((r.new_id, chain.file_hash(folder / rel) or ""))
            sidecar.upsert_message(
                tcon,
                r.new_id,
                thread_id=d.get("threadId", ""),
                internal_date=ms,
                size_estimate=d.get("sizeEstimate"),
                fields=fields[r.source_id],
                label_ids=d.get("labelIds", []),
            )
        chain.append(folder, gmail.account, "restore", added)
        con.commit()
        tcon.commit()
    finally:
        con.close()


class RestoreLog:
    """
    The restore log, appended to a row at a time and flushed after each, so
    a run that is interrupted or fails still leaves what it did. Use with
    `with`; add() takes each Result as apply() reports it.
    """

    def __init__(self, path: Path, mode: str, account: str, target: str):
        self.path, self.mode, self.account, self.target = path, mode, account, target

    def __enter__(self) -> RestoreLog:
        upgrade_log(self.path, LOG_COLUMNS)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.is_file()
        self._fh = self.path.open("a", newline="", encoding="utf-8")
        self._w = csv.DictWriter(self._fh, fieldnames=LOG_COLUMNS)
        if new:
            self._w.writeheader()
        return self

    def add(self, r: Result) -> None:
        c = r.decision.candidate
        self._w.writerow(
            {
                "time": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
                "mode": self.mode,
                "account": self.account,
                "target": self.target,
                "source_id": c.source_id,
                "new_id": r.new_id,
                "outcome": r.outcome,
                "detail": r.detail,
                "batch": c.batch,
                "rule": c.rule,
                "path": str(c.path or ""),
            }
        )
        self._fh.flush()

    def __exit__(self, *exc) -> None:
        self._fh.close()
