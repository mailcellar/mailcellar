"""
imap: an IMAP account as a read-only Mailbox (#108, step 2 #111).

    with closing(ImapAccount(account, settings, password, folder=backup_folder)) as box:
        ids = box.list_message_ids()

Nothing on the server changes: folders are opened with EXAMINE and bodies
fetched with BODY.PEEK[], so not even \\Seen is set; every write method
raises. Folders and flags become labels, as Gmail's labels are: INBOX and
the special-use Sent, Drafts, Junk and Trash as INBOX, SENT, DRAFT, SPAM
and TRASH (known by name for a role the server marks no folder for),
other folders by name ('/' for nesting), a missing \\Seen as
UNREAD and \\Flagged as STARRED. A message counts as in Trash or Junk
only if it lives nowhere else. A \\All folder repeats the others and is
skipped, but on Gmail (X-GM-EXT-1) All Mail is the only place archived
mail with no label lives: it is read, gives no label, and counts only for
mail in no other folder; it is also where archived mail goes back to.

A message's ID is 16 hex digits, as Gmail's are: a hash of its Message-ID,
INTERNALDATE and size, which a move keeps, so the same message in two
folders is one message with two labels. Where that can't tell messages
apart (no Message-ID, or one header ID at two new places with different
content), the content's hash is the ID; on Gmail, copies sharing an
X-GM-MSGID are one message without it.

A scan reads (the UID map as the backup last saved it, folders, UIDs,
flags, the headers of mail the map doesn't know, content hashes where
needed) and decides with pure functions over frozen values (IDs, labels,
where to fetch each message, what is gone). The new map is written only
by save_state, through the backup's own meta.sqlite connection
(meta.sqlite's imap_map, backup/sidecar.py).
"""

import ast
import base64
import binascii
import hashlib
import imaplib
import logging
import re
import ssl
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from types import MappingProxyType

from mailcellar import gmailapi, progress
from mailcellar.message import normalise_message_id

logger = logging.getLogger(__name__)

FETCH_CHUNK = 200  # UIDs per FETCH
ATTEMPTS = 3  # tries per folder after a dropped connection
TIMEOUT = 120  # seconds a socket may stall
SPECIAL = {"\\Sent": "SENT", "\\Drafts": "DRAFT", "\\Junk": "SPAM", "\\Trash": "TRASH"}
BY_NAME = {"sent": "SENT", "drafts": "DRAFT", "junk": "SPAM", "spam": "SPAM", "trash": "TRASH"}
SKIP = {"\\Noselect", "\\NonExistent", "\\All", "\\Flagged"}  # not mail, or copies of it
GMAIL_EXT = "X-GM-EXT-1"  # Gmail: X-GM-MSGID, and an All Mail (\\All) that isn't only copies
SYSTEM = {"INBOX", "SENT", "DRAFT", "SPAM", "TRASH", "UNREAD", "STARRED"}
HIDDEN = frozenset({"SPAM", "TRASH"})  # left out of listings unless include_spam_trash
TOKEN = "imap:1"  # a history point: the UID map is the state
GMAIL_ONLY = frozenset({"IMPORTANT", "CHAT", "CHATS"})  # Gmail labels with no folder to be
# Review marks (step 3): mailcellar's own labels as keywords, or as folders under REVIEW_ROOT,
# reserved so a user's own folders under mailcellar/ keep their names.
KEYWORD_PREFIX = "$mailcellar-"
REVIEW_ROOT = "mailcellar/review/"
KEYWORD_SAFE = re.compile(r"^[a-z0-9-]+(/[a-z0-9-]+)*$")  # labels a keyword can carry

LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\) (?P<delim>"(?:[^"\\]|\\.)*"|NIL) (?P<name>.+)$')

Location = tuple[str, int]  # (folder, uid)


# ── values ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Folder:
    name: str  # as the server names it
    label: str  # as the backup names it
    review: bool = False  # one of mailcellar's review folders (mailcellar/review/<label>)
    archive: bool = False  # the server marks it \\Archive (RFC 6154): where archived mail goes
    store: bool = False  # Gmail's All Mail: every message outside Trash and Spam; no label


@dataclass(frozen=True)
class Fetched:
    """One message's part of a UID FETCH answer."""

    uid: int
    flags: frozenset[str] | None = None
    internal: int | None = None  # ms since epoch
    size: int | None = None
    literal: bytes | None = None
    gm_msgid: int | None = None  # Gmail's X-GM-MSGID: one message's, in every folder


@dataclass(frozen=True)
class FolderRead:
    """What one folder showed: its UIDs' flags, and headers of the UIDs the map doesn't know."""

    folder: Folder
    uidvalidity: int
    flags: Mapping[int, frozenset[str]]
    known: Mapping[int, str]  # UID → ID, from the map
    headers: Mapping[int, Fetched]  # new UIDs only


@dataclass(frozen=True)
class Message:
    """A message as the backup should see it."""

    id: str
    labels: frozenset[str]  # folders, flags and review marks, as the backup records them
    where: Location  # where to fetch it from
    internal: int | None = None  # None: not read this run (the map knew it)
    size: int | None = None
    # mailcellar's review marks, from its keywords and review folders only: a user's own
    # folder named like a mark gives the label, never the mark, so the trash step's
    # check reads this, not labels.
    marks: frozenset[str] = frozenset()
    # Every copy, folder and UID: what a write acts on. All Mail's only for mail in no other folder.
    places: tuple[Location, ...] = ()
    keywords: frozenset[str] = frozenset()  # the marks it carries as keywords (on any copy)


@dataclass(frozen=True)
class ServerInfo:
    """What setup reports about a server."""

    capabilities: frozenset[str]  # as CAPABILITY answers once signed in
    folders: tuple[Folder, ...]
    marked: frozenset[str]  # the roles the server marks a folder for (TRASH…)

    @property
    def can_offload(self) -> bool:
        """
        MOVE and a Trash the server marks \\Trash, not one taken from its name:
        what offloading will need (step 3).
        """
        return "MOVE" in self.capabilities and "TRASH" in self.marked

    @property
    def can_move(self) -> bool:
        """MOVE: what restore needs to take mail out of Trash. Uploads need only APPEND."""
        return "MOVE" in self.capabilities


@dataclass(frozen=True)
class Scan:
    messages: Mapping[str, Message]
    labels: frozenset[str]
    maps: Mapping[str, tuple[int, Mapping[int, str]]]  # folder → (UIDVALIDITY, UID → ID)
    skipped: frozenset[str] = field(default_factory=frozenset)  # folders that couldn't be read
    folders: tuple[Folder, ...] = ()  # the folders read
    read_at: int = 0  # ms since epoch: when the read began


# ── pure: parsing ────────────────────────────────────────────────────────────


def decode_utf7(name: str) -> str:
    """A folder name in IMAP's modified UTF-7 (RFC 3501 5.1.3); as given if malformed."""
    parts = re.split(r"(&[^-]*-)", name)
    try:
        return "".join(_decode_shift(p) if p.startswith("&") else p for p in parts)
    except binascii.Error, UnicodeDecodeError:
        return name


def encode_utf7(name: str) -> str:
    """A folder name in IMAP's modified UTF-7 (RFC 3501 5.1.3), as CREATE wants it. Pure."""
    out, run = [], []

    def flush():
        if run:
            b64 = base64.b64encode("".join(run).encode("utf-16-be")).decode().rstrip("=")
            out.append("&" + b64.replace("/", ",") + "-")
            run.clear()

    for ch in name:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            run.append(ch)
    flush()
    return "".join(out)


def _decode_shift(part: str) -> str:
    if part == "&-":
        return "&"
    chunk = part[1:-1].replace(",", "/")
    return base64.b64decode(chunk + "=" * (-len(chunk) % 4), validate=True).decode("utf-16-be")


def unquote(value: bytes) -> str:
    text = value.decode("utf-8", "replace")
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        return re.sub(r"\\(.)", r"\1", text[1:-1])
    return text


def quote(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def list_entries(lines) -> tuple[tuple[str, str | None, frozenset[str]], ...]:
    """LIST's answer as (name, delimiter, flags). Pure."""
    out = []
    for line in lines:
        literal = None
        if isinstance(line, tuple):  # a name sent as a literal: taken as it is
            line, literal = line[0].rsplit(b" ", 1)[0] + b" X", line[1]
        m = LIST_RE.match(line or b"")
        if m is None:
            continue
        name = literal.decode("utf-8", "replace") if literal is not None else unquote(m["name"])
        delim = None if m["delim"] == b"NIL" else unquote(m["delim"])
        out.append((name, delim, frozenset(m["flags"].decode().split())))
    return tuple(out)


def folders_from(entries, gmail: bool = False) -> tuple[Folder, ...]:
    """
    The folders to back up, with their labels. A name stands for a role
    (Trash, Junk…) only when the server marks no folder for it: a server may
    mark Sent and Trash but not its Junk, while a user's own folder called
    Junk stays theirs on a server that marks its real one. On Gmail
    (`gmail`: X-GM-EXT-1) All Mail (\\All) is kept too, as the store and
    the archive: archived mail with no label is in no other folder. Pure.
    """
    by_name = frozenset(BY_NAME.values()) - marked_roles(entries)
    skip = SKIP - {"\\All"} if gmail else SKIP
    return tuple(
        Folder(
            name,
            folder_label(name, delim, flags, by_name=by_name),
            review=review_label(plain_label(name, delim)) is not None,
            archive=bool(flags & {"\\Archive", "\\All"}),
            store="\\All" in flags,
        )
        for name, delim, flags in entries
        if not flags & skip
    )


def marked_roles(entries) -> frozenset[str]:
    """
    The roles (SENT, TRASH…) the server marks a folder for, special-use;
    only folders that can be opened count. Pure.
    """
    return frozenset(
        SPECIAL[flag]
        for _n, _d, flags in entries
        if not flags & SKIP
        for flag in flags & set(SPECIAL)
    )


def search_uids(data) -> frozenset[int]:
    """SEARCH's answer as UIDs. Pure."""
    return frozenset(int(u) for u in b" ".join(d for d in data if d).split())


def exists_from(data) -> int | None:
    """EXAMINE's message count (its EXISTS), or None if it gave none. Pure."""
    first = data[0] if data else None
    return int(first) if isinstance(first, bytes) and first.strip().isdigit() else None


def short_by(found: frozenset[int], exists: int | None) -> int:
    """How many messages a finished listing is short of EXAMINE's count (0: none known). Pure."""
    return max(exists - len(found), 0) if exists is not None else 0


def stalled(before: frozenset[int], got: frozenset[int]) -> bool:
    """
    A SEARCH pass below the lowest UID found that found UIDs, none of them
    new: the server ignored the range, so what it lists may be cut short.
    Pure.
    """
    return bool(got) and not got - before and min(got) > 1


def next_search(before: frozenset[int], got: frozenset[int], exists: int | None):
    """
    After a SEARCH pass that found `got`, with `before` found by the passes
    ahead of it: the criteria for the next pass, or None when the listing is
    done. A server with MESSAGELIMIT (RFC 9738) answers with the highest UIDs
    only, so the next pass searches below the lowest; done when a pass finds
    nothing, UID 1 is reached, or EXAMINE's count is. A pass that finds only
    old UIDs is stalled(), refused before this is asked. Pure.
    """
    if not got or min(got) <= 1:
        return None
    if exists is not None and len(before | got) >= exists:
        return None
    return ("UID", f"1:{min(got) - 1}")


def capabilities_from(data) -> frozenset[str]:
    """CAPABILITY's answer as upper-case names. Pure."""
    return frozenset(b" ".join(d for d in data if d).decode("ascii", "replace").upper().split())


def folder_label(
    name: str, delim: str | None, flags, by_name: frozenset[str] = frozenset(BY_NAME.values())
) -> str:
    """
    The label a folder's mail gets: Gmail's system names where they apply,
    from its special-use flag, or from its name for the roles in `by_name`. Pure.
    """
    if name.upper() == "INBOX":
        return "INBOX"
    special = [label for flag, label in SPECIAL.items() if flag in flags]
    if special:
        return special[0]
    if BY_NAME.get(name.lower()) in by_name:
        return BY_NAME[name.lower()]
    label = plain_label(name, delim)
    return review_label(label) or label


def plain_label(name: str, delim: str | None) -> str:
    """A folder's name as a label: decoded, its hierarchy delimiter as '/'. Pure."""
    decoded = decode_utf7(name)
    return decoded.replace(delim, "/") if delim and delim != "/" else decoded


def review_label(label: str) -> str | None:
    """The label a review folder (mailcellar/review/<label>) stands for, or None. Pure."""
    rest = label[len(REVIEW_ROOT) :] if label.startswith(REVIEW_ROOT) else ""
    return rest if KEYWORD_SAFE.match(rest) else None


def keyword_for(label: str) -> str:
    """
    The keyword that carries one of mailcellar's labels (`offload-candidate`,
    `restored/2026-10-27`): $mailcellar- and the label, '/' as '_'. Not '.',
    which RFC 3501 allows but iCloud refuses ("BAD Parse Error"); labels have
    no '_', so the keyword still reads back as its label. GmailError for a
    label a keyword can't carry, naming the form it can. Pure.
    """
    if not KEYWORD_SAFE.match(label):
        raise gmailapi.GmailError(
            f"{label!r} can't be marked on an IMAP server: use lowercase letters, digits, "
            "'-' and '/' (as offload-candidate or restored/2026-10-27)"
        )
    return KEYWORD_PREFIX + label.replace("/", "_")


def label_from_keyword(keyword: str) -> str | None:
    """
    The label one of mailcellar's keywords carries, or None for any other flag:
    only keywords in the form keyword_for writes count (any case, as IMAP
    keywords are), so every mark read can be cleared again. '/' written as
    '.', as earlier versions wrote it, still counts: only restored/<date>
    has one, and nothing takes that off. Pure.
    """
    lowered = keyword.lower()
    if not lowered.startswith(KEYWORD_PREFIX) or "/" in lowered:
        return None
    label = lowered[len(KEYWORD_PREFIX) :].replace("_", "/").replace(".", "/")
    return label if KEYWORD_SAFE.match(label) else None


def keyword_marks(flags) -> frozenset[str]:
    """The review marks a message's mailcellar keywords carry. Pure."""
    return frozenset({label_from_keyword(f) for f in flags} - {None})


def flag_labels(flags) -> frozenset[str]:
    """Labels from a message's flags: UNREAD, STARRED, and mailcellar's own keywords. Pure."""
    return frozenset(
        ({"UNREAD"} if "\\Seen" not in flags else set())
        | ({"STARRED"} if "\\Flagged" in flags else set())
        | keyword_marks(flags)
    )


def internal_ms(text: str) -> int:
    """INTERNALDATE ("17-Jul-1996 02:44:25 -0700") as ms since epoch. Pure."""
    return int(datetime.strptime(text.strip(), "%d-%b-%Y %H:%M:%S %z").timestamp() * 1000)


def message_id_of(header: bytes) -> str:
    """The Message-ID in a fetched header, whitespace folded; "" if there is none."""
    m = re.search(rb"^message-id:[ \t]*(.*(?:\r?\n[ \t].*)*)", header, re.I | re.M)
    return " ".join(m[1].decode("utf-8", "replace").split()) if m else ""


def header_id(message_id: str, internal: int, size: int) -> str:
    return hashlib.sha256(f"{message_id}\n{internal}\n{size}".encode()).hexdigest()[:16]


def content_id(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:16]


ANGLE_ID = re.compile(r"<[^<>\s]+>")


def thread_of(raw: bytes, gid: str) -> str:
    """
    A message's thread, which IMAP doesn't give: 16 hex digits of a hash of
    the conversation's first Message-ID (References' first, else
    In-Reply-To's, else its own), so replies share it; its own ID when it
    names none. Never "": the backup and offload read '' as gone. Pure.
    """
    head = raw.split(b"\r\n\r\n", 1)[0].split(b"\n\n", 1)[0]
    for name in (b"references", b"in-reply-to", b"message-id"):
        m = re.search(rb"^" + name + rb":[ \t]*(.*(?:\r?\n[ \t].*)*)", head, re.I | re.M)
        ids = ANGLE_ID.findall(m[1].decode("utf-8", "replace")) if m else []
        if ids:
            return hashlib.sha256(f"thread\n{ids[0]}".encode()).hexdigest()[:16]
    return gid


def fetch_messages(data) -> tuple[tuple[bytes, bytes | None], ...]:
    """
    A UID FETCH answer split into (text, literal) per message. imaplib gives a
    message with a literal as (prefix, bytes) followed by the rest of its line,
    and one without as a line. Pure.
    """
    items = [item for item in data if item is not None]
    out = []
    i = 0
    while i < len(items):
        item = items[i]
        if isinstance(item, tuple):
            rest = items[i + 1] if i + 1 < len(items) and isinstance(items[i + 1], bytes) else b""
            out.append((item[0] + rest, item[1]))
            i += 2 if rest else 1
        else:
            out.append((item, None))
            i += 1
    return tuple(out)


def parse_fetched(text: bytes, literal: bytes | None) -> Fetched | None:
    uid = re.search(rb"UID (\d+)", text)
    if uid is None:  # an unsolicited update without a UID
        return None
    flags = re.search(rb"FLAGS \(([^)]*)\)", text)
    date = re.search(rb'INTERNALDATE "([^"]+)"', text)
    size = re.search(rb"RFC822\.SIZE (\d+)", text)
    gm_msgid = re.search(rb"X-GM-MSGID (\d+)", text)
    return Fetched(
        uid=int(uid[1]),
        flags=frozenset(flags[1].decode().split()) if flags else None,
        internal=internal_ms(date[1].decode()) if date else None,
        size=int(size[1]) if size else None,
        literal=literal,
        gm_msgid=int(gm_msgid[1]) if gm_msgid else None,
    )


def parse_fetch(data) -> dict[int, Fetched]:
    """A UID FETCH answer as {uid: Fetched}. Pure."""
    fetched = (parse_fetched(text, literal) for text, literal in fetch_messages(data))
    return {f.uid: f for f in fetched if f is not None}


def chunks(items, size: int = FETCH_CHUNK):
    items = list(items)
    return [items[start : start + size] for start in range(0, len(items), size)]


# ── pure: deciding ───────────────────────────────────────────────────────────


def header_key(f: Fetched) -> str | None:
    """A new message's ID from its headers; None when it has no Message-ID."""
    mid = message_id_of(f.literal or b"")
    return header_id(mid, f.internal, f.size) if mid else None


def needs_content(reads) -> frozenset[Location]:
    """
    New messages whose headers can't name them: no Message-ID, or a header ID
    at more than one new place, which may be copies or different messages;
    on Gmail, places sharing one X-GM-MSGID are copies of one. Pure.
    """
    places: dict[str, list[tuple[Location, int | None]]] = {}
    without = []
    for read in reads:
        for uid, f in read.headers.items():
            key = header_key(f)
            if key is None:
                without.append((read.folder.name, uid))
            else:
                places.setdefault(key, []).append(((read.folder.name, uid), f.gm_msgid))
    shared = [
        loc
        for found in places.values()
        if len(found) > 1 and not copies_of_one(gm for _loc, gm in found)
        for loc, _gm in found
    ]
    return frozenset(without + shared)


def copies_of_one(gm_msgids) -> bool:
    """Whether places are copies of one Gmail message: one X-GM-MSGID, given at each. Pure."""
    found = set(gm_msgids)
    return len(found) == 1 and None not in found


def assign_ids(reads, digests: Mapping[Location, str]) -> dict[Location, str]:
    """
    Each location's message ID: the map's for known UIDs; the header ID for new
    ones, unless the content was hashed and copies under one header ID differ,
    when each gets its content's ID. Pure.
    """
    out: dict[Location, str] = {}
    by_key: dict[str, list[Location]] = {}
    for read in reads:
        for uid in read.flags:
            loc = (read.folder.name, uid)
            if uid in read.known:
                out[loc] = read.known[uid]
            elif uid in read.headers:
                key = header_key(read.headers[uid])
                if key is None:
                    if loc in digests:
                        out[loc] = digests[loc]
                else:
                    by_key.setdefault(key, []).append(loc)
    for key, locs in by_key.items():
        same = len({digests.get(loc) for loc in locs}) == 1
        for loc in locs:
            if same or loc in digests:
                out[loc] = key if same else digests[loc]
    return out


def merge(reads, ids: Mapping[Location, str], origins: Mapping | None = None) -> Scan:
    """
    Locations merged into messages: the labels of every folder a message is in
    (Trash and Junk only if it is in nothing else) and its flags; fetched from
    a folder it lives in. A message in a review folder keeps the labels of the
    folders it was in before (`origins`: ID → ((folder, label), ...)), as a
    Gmail message keeps its labels when one is added. Gmail's All Mail (a
    store folder) gives no label, and its copy counts only for a message in
    no other folder. Pure.
    """
    stores = {read.folder.label for read in reads if read.folder.store}
    placed: dict[str, list[tuple[FolderRead, int]]] = {}
    for read in reads:
        for uid in read.flags:
            loc = (read.folder.name, uid)
            if loc in ids:
                placed.setdefault(ids[loc], []).append((read, uid))
    messages = {}
    for gid, places in placed.items():
        places = [(r, u) for r, u in places if not r.folder.store] or places
        folder_labels = {read.folder.label for read, _uid in places}
        recorded = (origins or {}).get(gid, {})
        for read, _uid in places:  # the review it's in now: older ones are done with
            if read.folder.review:
                folder_labels |= {label for _n, label in recorded.get(read.folder.label, ())}
        folder_labels -= stores
        kept = folder_labels - HIDDEN or folder_labels
        flags = frozenset().union(*(read.flags[uid] for read, uid in places))
        home = [(r, u) for r, u in places if r.folder.label in kept]
        read, uid = (home or places)[0]
        header = read.headers.get(uid)
        keywords = keyword_marks(flags)
        marks = keywords | {r.folder.label for r, _u in places if r.folder.review}
        messages[gid] = Message(
            gid,
            frozenset(kept) | flag_labels(flags),
            (read.folder.name, uid),
            header.internal if header else None,
            header.size if header else None,
            marks,
            tuple((r.folder.name, u) for r, u in places),
            keywords,
        )
    maps = {
        read.folder.name: (
            read.uidvalidity,
            {
                uid: ids[(read.folder.name, uid)]
                for uid in read.flags
                if (read.folder.name, uid) in ids
            },
        )
        for read in reads
    }
    labels = frozenset(
        {read.folder.label for read in reads} - stores | {"UNREAD", "STARRED"}
    ).union(
        *(m.labels for m in messages.values())  # mailcellar's keywords among them
    )
    return Scan(messages, labels, maps, folders=tuple(read.folder for read in reads))


def changes(before: Mapping[str, frozenset[str]], scan: Scan) -> tuple[set[str], set[str]]:
    """
    (touched, deleted) since the map was `before`: every message now (backup
    keeps only real label changes), and those in no folder any more. Mail in a
    folder that couldn't be read this run isn't taken for gone. Pure.
    """
    still = set(scan.messages).union(*(before.get(f, ()) for f in scan.skipped))
    deleted = set().union(*before.values()) - still
    return set(scan.messages) | deleted, deleted


def response(message: Message, raw: bytes, internal: int | None) -> dict:
    """A Gmail-shaped response, as gyb.write_message and the sidecar take it. Pure."""
    return {
        "id": message.id,
        "threadId": thread_of(raw, message.id),
        "internalDate": str(internal if internal is not None else message.internal or 0),
        "labelIds": sorted(message.labels),
        "sizeEstimate": len(raw),
        "raw": base64.urlsafe_b64encode(raw).decode(),
    }


def by_folder(messages, ids) -> dict[str, list[tuple[int, str]]]:
    """The messages' locations grouped by folder: {folder: [(uid, id)]}. Pure."""
    out: dict[str, list[tuple[int, str]]] = {}
    for gid in ids:
        if gid in messages:
            name, uid = messages[gid].where
            out.setdefault(name, []).append((uid, gid))
    return out


# ── the account: reading, then writing the map ───────────────────────────────


def drop(conn) -> None:
    """Close a connection's socket, whatever state it is in; None is nothing to close."""
    if conn is not None:
        try:
            conn.shutdown()
        except OSError:
            pass


def log_out(conn) -> None:
    """LOGOUT, then the socket closed, whatever LOGOUT answers. Never raises."""
    try:
        conn.logout()  # imaplib closes the socket only if LOGOUT gets its answer
    except OSError, imaplib.IMAP4.error:
        drop(conn)


# ── writing: decided here, carried out by ImapAccount ─────────────────────────


BAD_ANSWER = re.compile(r"^(\S+) command error: BAD (\[.*\])$")  # as imaplib words a tagged BAD


def answered(exc: BaseException) -> bool:
    """
    Whether imaplib raised for the server's tagged BAD, so the session is in
    step; not an abort, nor imaplib refusing a command itself (a command in
    the wrong state is mailcellar's mistake, to be seen). _write takes such a
    BAD as a refusal. Pure.
    """
    return (
        isinstance(exc, imaplib.IMAP4.error)
        and not isinstance(exc, imaplib.IMAP4.abort)
        and BAD_ANSWER.search(str(exc)) is not None
    )


def server_said(exc: BaseException) -> str:
    """
    A BAD as _ok words a NO, "<command>: BAD <text>…", in the server's words
    as it sent them; else str(exc). Pure.
    """
    found = BAD_ANSWER.search(str(exc))
    try:
        parts = ast.literal_eval(found[2]) if found else None
    except ValueError, SyntaxError:
        parts = None
    if not isinstance(parts, list):
        return str(exc)
    words = [p.decode("utf-8", "replace") if isinstance(p, bytes) else str(p) for p in parts]
    return f"{found[1]}: " + " ".join(["BAD", *(w for w in words if w)])


def write_refusal(info: ServerInfo, scope: str = "modify") -> str:
    """
    Why mailcellar won't change this server, or "": it needs MOVE and a Trash
    the server marks \\Trash, so nothing is ever expunged. "append" (restore)
    is never refused: an upload needs only APPEND, which every IMAP4rev1
    server has, and restore asks can_move() before taking mail out of Trash.
    Pure.
    """
    if scope == "append":
        return ""
    missing = [
        what
        for what, ok in (
            ("MOVE (RFC 6851)", "MOVE" in info.capabilities),
            ("a special-use \\Trash folder", "TRASH" in info.marked),
        )
        if not ok
    ]
    if missing:
        return (
            f"the server has no {' and no '.join(missing)}, so mailcellar won't move mail "
            "on it (without them a move can delete for good); it is backed up only"
        )
    return ""


def trash_folder(scan: Scan) -> Folder | None:
    """The folder the server marks \\Trash, if the scan read it. Pure."""
    return next((f for f in scan.folders if f.label == "TRASH"), None)


def by_folder_uids(locations) -> dict[str, list[int]]:
    """Locations grouped by folder, UIDs sorted. Pure."""
    out: dict[str, list[int]] = {}
    for name, uid in locations:
        out.setdefault(name, []).append(uid)
    return {name: sorted(uids) for name, uids in out.items()}


@dataclass(frozen=True)
class TrashPlan:
    """What trash(ids) does: decided from a scan by trash_plan, before anything moves."""

    trash: str | None  # the Trash folder's name
    moves: Mapping[str, tuple[int, ...]]  # folder → the UIDs to move from it
    owners: Mapping[str, frozenset[str]]  # folder → the IDs with a copy there
    failures: tuple[str, ...]  # "<id>: <why>", for IDs nothing is done for
    confirm: tuple[str, ...]  # the IDs to find in Trash, and only there, afterwards


def trash_plan(scan: Scan, ids) -> TrashPlan:
    """
    Every copy outside Trash, by folder, for the IDs on the server. Nothing
    moves while a folder couldn't be read: a copy there would stay behind.
    Pure.
    """
    trash = trash_folder(scan)
    if trash is None or scan.skipped:
        why = (
            "the Trash folder couldn't be read"
            if trash is None
            else f"folders couldn't be read ({', '.join(sorted(scan.skipped))})"
        )
        empty = MappingProxyType({})
        return TrashPlan(None, empty, empty, tuple(f"{gid}: {why}" for gid in ids), ())
    present = [gid for gid in ids if gid in scan.messages]
    copies = [
        (gid, loc) for gid in present for loc in scan.messages[gid].places if loc[0] != trash.name
    ]
    owners: dict[str, set[str]] = {}
    for gid, (name, _uid) in copies:
        owners.setdefault(name, set()).add(gid)
    return TrashPlan(
        trash.name,
        MappingProxyType(
            {name: tuple(uids) for name, uids in by_folder_uids(loc for _g, loc in copies).items()}
        ),
        MappingProxyType({name: frozenset(gids) for name, gids in owners.items()}),
        tuple(f"{gid}: not on the server" for gid in ids if gid not in scan.messages),
        tuple(present),
    )


def trash_result(plan: TrashPlan, refused: Mapping[str, str], after: Scan | None) -> list[str]:
    """
    trash()'s failures: the plan's own, the IDs with a copy in a folder whose
    MOVE was refused (`refused`: folder → why), and those a re-read (`after`)
    doesn't show in Trash and only there. Pure.
    """
    stuck = {
        gid: f"{gid}: moving it out of {name} failed: {why}"
        for name, why in sorted(refused.items())
        for gid in sorted(plan.owners.get(name, ()))
    }
    unconfirmed = [
        f"{gid}: not in Trash after the move"
        for gid in plan.confirm
        if gid not in stuck
        and (
            after is None or gid not in after.messages or "TRASH" not in after.messages[gid].labels
        )
    ]
    return [*plan.failures, *stuck.values(), *unconfirmed]


def review_folder_name(label: str, delim: str | None) -> str:
    """The review folder for a label: mailcellar/review/<label> in the server's own form. Pure."""
    parts = [*REVIEW_ROOT.strip("/").split("/"), *label.split("/")]
    return (delim or "/").join(parts)


class KeywordsRefused(gmailapi.GmailError):
    """The server keeps no custom keywords: keyword review can't work on it."""


@dataclass(frozen=True)
class Marking:
    """
    What add_label does, decided before anything changes: by keyword, the
    copies to mark; by folder, the copies to move into `folder` and where each
    message was, to record first.
    """

    folder: str | None  # the review folder (folder mode); None: by keyword
    create: bool  # the review folder doesn't exist yet
    copies: Mapping[str, tuple[int, ...]]  # folder → UIDs to mark, or to move into review
    owners: Mapping[str, frozenset[str]]  # folder → the IDs with a copy there
    origins: Mapping[str, tuple[tuple[str, str], ...]]  # ID → (folder, label) it leaves
    failures: tuple[str, ...]  # "<id>: <why>", for IDs nothing is done for


def by_folder_with_owners(pairs) -> tuple[Mapping, Mapping]:
    """(ID, location) pairs as folder → UIDs and folder → IDs, read-only. Pure."""
    owners: dict[str, set[str]] = {}
    for gid, (name, _uid) in pairs:
        owners.setdefault(name, set()).add(gid)
    uids = by_folder_uids(loc for _g, loc in pairs)
    return (
        MappingProxyType({name: tuple(u) for name, u in uids.items()}),
        MappingProxyType({name: frozenset(g) for name, g in owners.items()}),
    )


def keyword_marking(scan: Scan, ids) -> Marking:
    """Every copy of the messages on the server, wherever it is. Pure."""
    pairs = [(gid, loc) for gid in ids if gid in scan.messages for loc in scan.messages[gid].places]
    copies, owners = by_folder_with_owners(pairs)
    missing = tuple(f"{gid}: not on the server" for gid in ids if gid not in scan.messages)
    return Marking(None, False, copies, owners, MappingProxyType({}), missing)


def keyword_unmarking(scan: Scan, ids, label: str) -> Marking:
    """
    keyword_marking of the messages that carry `label`'s keyword: a server
    that keeps no keywords, or mail marked by folder, is never asked to take
    one off. Pure.
    """
    carrying = [g for g in ids if g in scan.messages and label in scan.messages[g].keywords]
    return keyword_marking(scan, carrying)  # one not on the server is put_back's failure


def folder_marking(scan: Scan, ids, label: str, entries) -> Marking:
    """
    The review folder for `label` in the server's own hierarchy (to create if
    missing), and every copy of each message to move into it but those in
    Trash or Junk, which stay. A message under another review is left to
    that one, and nothing moves while a folder couldn't be read (a copy
    there would stay behind); both are failures. Pure.
    """
    delim = next((d for _n, d, _f in entries if d), None)
    review = review_folder_name(label, delim)
    create = review not in {name for name, _d, _f in entries}
    empty = MappingProxyType({})
    if scan.skipped:
        why = f"folders couldn't be read ({', '.join(sorted(scan.skipped))})"
        return Marking(review, create, empty, empty, empty, tuple(f"{g}: {why}" for g in ids))
    label_of = {f.name: f.label for f in scan.folders}
    reviews = {f.name for f in scan.folders if f.review}
    pairs, origins, failures = [], {}, []
    for gid in ids:
        if gid not in scan.messages:
            failures.append(f"{gid}: not on the server")
            continue
        places = scan.messages[gid].places
        other = sorted({n for n, _u in places if n in reviews and n != review})
        if other:
            failures.append(f"{gid}: under review in {other[0]} already; finish that first")
            continue
        leaving = [
            loc
            for loc in places
            if loc[0] != review and label_of[loc[0]] not in HIDDEN and loc[0] not in reviews
        ]
        if not leaving and review not in {n for n, _u in places}:
            failures.append(f"{gid}: only in Junk or Trash")
            continue
        if leaving:
            origins[gid] = tuple(sorted({(n, label_of[n]) for n, _u in leaving}))
            pairs += [(gid, loc) for loc in leaving]
    copies, owners = by_folder_with_owners(pairs)
    return Marking(review, create, copies, owners, MappingProxyType(origins), tuple(failures))


def marking_failures(marking: Marking, refused: Mapping[str, str], what: str) -> list[str]:
    """
    The failures of `what` done to a marking's copies (marking, unmarking):
    the plan's own, and the IDs in a folder that refused. Pure.
    """
    stuck = {
        gid: f"{gid}: {what} in {name} failed: {why}"
        for name, why in sorted(refused.items())
        for gid in sorted(marking.owners.get(name, ()))
    }
    return [*marking.failures, *stuck.values()]


def home_folders(labels, folders: tuple[Folder, ...]) -> tuple[str, ...]:
    """
    The folders a message with these labels belongs in, by the server's names:
    its folder labels that are folders now (not Trash, Junk or a review
    folder), INBOX first; with none, the archive folder (archive_folder), as
    an upload files mail with no folder label. Pure.
    """
    by_label = {f.label: f.name for f in folders if f.label not in HIDDEN and not f.review}
    found = sorted({by_label[label] for label in labels if label in by_label})
    if "INBOX" in found:
        found = ["INBOX", *[n for n in found if n != "INBOX"]]
    return tuple(found) or (archive_folder(folders)[0],)


@dataclass(frozen=True)
class PutBack:
    """
    What untrash or remove_label does: copies to move out of Trash (or a review
    folder) into each message's first home folder, and the further home folders
    to copy it into once it's there. Decided by put_back before anything moves.
    """

    moves: Mapping[str, Mapping[str, tuple[int, ...]]]  # from → to → UIDs
    owners: Mapping[tuple[str, str], frozenset[str]]  # (from, to) → the IDs that MOVE takes
    homes: Mapping[str, tuple[str, ...]]  # ID → its home folders
    failures: tuple[str, ...]
    create: tuple[str, ...] = ()  # homes the server doesn't have yet (a new Archive)


def put_back(scan: Scan, ids, origins: Mapping, out_of) -> PutBack:
    """
    Every copy of each message in a folder `out_of(folder)` says to leave
    (Trash, a review folder), to go to its homes: `origins` (ID → (folder,
    label)) where recorded, else the labels it has. Each copy goes to a home
    of its own, in order (trash took one from each folder), extra copies to
    the first; the homes left over are filled by COPY afterwards, where the
    message isn't already. Pure.
    """
    moves: dict[str, dict[str, list[int]]] = {}
    owners: dict[str, set[str]] = {}
    homes, failures = {}, []
    for gid in ids:
        if gid not in scan.messages:
            failures.append(f"{gid}: not on the server")
            continue
        message = scan.messages[gid]
        recorded = [label for _n, label in origins.get(gid, ())]
        homes[gid] = home_folders(recorded or message.labels, scan.folders)
        leaving = sorted(loc for loc in message.places if out_of(loc[0]))
        for k, (name, uid) in enumerate(leaving):
            dst = homes[gid][k] if k < len(homes[gid]) else homes[gid][0]
            moves.setdefault(name, {}).setdefault(dst, []).append(uid)
            owners.setdefault((name, dst), set()).add(gid)
    return PutBack(
        MappingProxyType(
            {
                src: MappingProxyType({dst: tuple(sorted(u)) for dst, u in by_dst.items()})
                for src, by_dst in moves.items()
            }
        ),
        MappingProxyType({step: frozenset(g) for step, g in owners.items()}),
        MappingProxyType(homes),
        tuple(failures),
        tuple(sorted({h for hs in homes.values() for h in hs} - {f.name for f in scan.folders})),
    )


def stuck_ids(plan: PutBack, moves: Mapping[tuple[str, str], str]) -> frozenset[str]:
    """The IDs a refused MOVE (`moves`: (from, to) → why) left where they were. Pure."""
    return frozenset().union(*(plan.owners.get(step, ()) for step in moves))


def put_back_failures(
    plan: PutBack, moves: Mapping[tuple[str, str], str], copies: Mapping[str, tuple[str, str]]
) -> dict[str, str]:
    """
    ID → failure: the IDs a refused MOVE (`moves`: (from, to) → why) left
    where they were (stuck_ids, each with its MOVE's reason), then those whose
    COPY into a further home was refused (`copies`: ID → (to, why)), only a
    copy missing. Pure.
    """
    copied = {gid: f"{gid}: copying it to {to} failed: {why}" for gid, (to, why) in copies.items()}
    stuck = {
        gid: f"{gid}: moving it out of {src} to {to} failed: {why}"
        for (src, to), why in sorted(moves.items())
        for gid in sorted(plan.owners.get((src, to), ()))
    }
    return copied | stuck


def further_homes(plan: PutBack, stuck: frozenset[str]) -> Mapping[str, tuple[str, ...]]:
    """
    ID → the homes after its first, which COPY fills once MOVE has put it
    there: for each message with more than one, unless it is `stuck`
    (stuck_ids), still where it was. Pure.
    """
    return MappingProxyType(
        {gid: homes[1:] for gid, homes in plan.homes.items() if len(homes) > 1 and gid not in stuck}
    )


def further_copies(
    more: Mapping[str, tuple[str, ...]], scan: Scan
) -> tuple[tuple[str, str, int, str], ...]:
    """
    The COPYs that fill further homes (`more`, from further_homes), as
    (ID, folder, UID, destination), by the scan read after the MOVEs: from
    the copy it fetches from, into each home it isn't in yet (no second
    copy), in `more`'s order. A message the scan doesn't show has none. Pure.
    """
    out = []
    for gid, extra in more.items():
        if gid not in scan.messages:
            continue
        name, uid = scan.messages[gid].where
        there = {n for n, _u in scan.messages[gid].places}
        out += [(gid, name, uid, dst) for dst in extra if dst not in there]
    return tuple(out)


def untrash_result(plan: PutBack, stuck: Mapping[str, str], after: Scan) -> list[str]:
    """
    untrash()'s failures: the plan's own, those of putting back (`stuck`, from
    put_back_failures), and those a re-read still shows in Trash only. Pure.
    """
    still = [
        f"{gid}: still in Trash"
        for gid in plan.homes
        if gid not in stuck and gid in after.messages and "TRASH" in after.messages[gid].labels
    ]
    return [*plan.failures, *stuck.values(), *still]


def into_moves(scan: Scan, ids, into: str) -> PutBack:
    """
    restore --into: one copy of each message moved into the folder `into`
    (created first if the server lacks it), from Trash only when it has no
    other copy; one already there moves nowhere. Its other copies stay where
    they are; a message with one copy has that copy moved, so the caller
    passes only mail a restore put where it is. Pure.
    """
    trash = {f.name for f in scan.folders if f.label == "TRASH"}
    moves: dict[str, list[int]] = {}
    owners: dict[str, set[str]] = {}
    homes, failures = {}, []
    for gid in ids:
        if gid not in scan.messages:
            failures.append(f"{gid}: not on the server")
            continue
        homes[gid] = (into,)
        places = scan.messages[gid].places
        if any(name == into for name, _uid in places):
            continue
        name, uid = min(places, key=lambda p: (p[0] in trash, p))
        moves.setdefault(name, []).append(uid)
        owners.setdefault(name, set()).add(gid)
    return PutBack(
        MappingProxyType(
            {src: MappingProxyType({into: tuple(sorted(u))}) for src, u in moves.items()}
        ),
        MappingProxyType({(src, into): frozenset(g) for src, g in owners.items()}),
        MappingProxyType(homes),
        tuple(failures),
        (into,) if homes and into not in {f.name for f in scan.folders} else (),
    )


def into_result(plan: PutBack, stuck: Mapping[str, str], after: Scan, into: str) -> list[str]:
    """
    move_into()'s failures: the plan's own, the refused moves' (`stuck`), and
    any message a re-read doesn't show in `into`. Pure.
    """

    def there(gid: str) -> bool:
        message = after.messages.get(gid)
        return message is not None and any(name == into for name, _u in message.places)

    missing = [
        f"{gid}: not in {into} after the move"
        for gid in plan.homes
        if gid not in stuck and not there(gid)
    ]
    return [*plan.failures, *stuck.values(), *missing]


def elsewhere_too(scan: Scan, ids, into: str) -> int:
    """How many of the messages have a copy outside `into`, Trash and Junk aside. Pure."""
    hidden = {f.name for f in scan.folders if f.label in HIDDEN}
    return sum(
        1
        for gid in ids
        if gid in scan.messages
        and any(name not in hidden and name != into for name, _u in scan.messages[gid].places)
    )


@dataclass(frozen=True)
class Append:
    """How one restored message goes onto the server: decided by append_plan."""

    folders: tuple[str, ...]  # first the one it's appended to, then those it's copied to
    create: tuple[str, ...]  # folders to create first
    flags: str  # the APPEND flag list, keywords included: "(\\Seen $mailcellar-…)"


def append_plan(labels, folders: tuple[Folder, ...], delim: str | None) -> Append:
    """
    Labels to folders and flags: INBOX and the special-use roles to their
    folders (Sent, Drafts by name when the server has none), other labels to
    the folder of that label (created, in the server's hierarchy and its
    modified UTF-7, if missing); restored/… to mailcellar's keyword, UNREAD
    and STARRED to \\Seen and \\Flagged; Gmail-only labels dropped; a
    message with no folder label (Gmail's archived mail) to Archive. Pure.
    """
    kept = [f for f in folders if not f.review]
    exact = {f.label: f.name for f in kept}
    folded = {f.label.casefold(): f.name for f in reversed(kept)}  # the first, on a tie
    placed = [place_label(label, exact, folded, delim) for label in sorted(set(labels))]
    homes = [(p.name, p.new) for p in placed if p.kind == "folder"]
    if not homes:  # archived in Gmail, or its folder gone: in no folder
        homes = [archive_folder(folders)]
    keywords = [p.name for p in placed if p.kind == "keyword"]
    flags = (["\\Seen"] if "UNREAD" not in labels else []) + (
        ["\\Flagged"] if "STARRED" in labels else []
    )
    return Append(
        tuple(sorted({name for name, _n in homes}, key=lambda n: (n != "INBOX", n))),
        tuple(sorted({name for name, new in homes if new})),
        f"({' '.join(flags + keywords)})",
    )


@dataclass(frozen=True)
class Placement:
    """Where one label goes on an IMAP server: decided by place_label."""

    kind: str  # "folder", "keyword" or "drop" (flags, Trash and Spam, Gmail-only labels)
    name: str = ""  # the folder's name, or the keyword
    new: bool = False  # a folder the server doesn't have yet


DROPPED = Placement("drop")


def place_label(label: str, exact, folded, delim: str | None) -> Placement:
    """
    Where one label goes. A folder is matched exactly (`exact`: label → name),
    else without regard to case (`folded`), as Gmail's labels are; a new one
    nests at '/' only, a separator already in the label made '-', and its
    name is in modified UTF-7. Pure.
    """
    if label in {"UNREAD", "STARRED"} | HIDDEN or label in GMAIL_ONLY:
        return DROPPED
    if label.startswith("CATEGORY_"):
        return DROPPED
    if label.startswith("restored/") and KEYWORD_SAFE.match(label):
        return Placement("keyword", keyword_for(label))
    if label in exact:
        return Placement("folder", exact[label])
    if label.casefold() in folded:
        return Placement("folder", folded[label.casefold()])
    default = {"SENT": "Sent", "DRAFT": "Drafts"}.get(label)
    if default is not None:
        known = folded.get(default.casefold())
        return Placement("folder", known or default, known is None)
    parts = [p.replace(delim, "-") if delim and delim != "/" else p for p in label.split("/")]
    return Placement("folder", encode_utf7((delim or "/").join(parts)), True)


def archive_folder(folders: tuple[Folder, ...]) -> tuple[str, bool]:
    """
    Where archived mail goes, as (name, new): the folder the server marks
    \\Archive, else one named Archive, else a new Archive. Pure.
    """
    marked = [f.name for f in folders if f.archive]
    if marked:
        return marked[0], False
    named = [f.name for f in folders if f.label.casefold() == "archive" and not f.review]
    return (named[0], False) if named else ("Archive", True)


def append_outcome(key: str, folders, refused: Mapping[str, str]) -> tuple[str | None, list]:
    """
    (failure, missed) for one message appended to `folders`, in order, given
    those that refused (`refused`: folder → why): failing the first means it
    isn't on the server at all; any after is only a copy missing. Pure.
    """
    if folders and folders[0] in refused:
        return f"{key}: {refused[folders[0]]}", []
    return None, [f"{name} ({refused[name]})" for name in folders if name in refused]


def raw_message_id(raw: bytes) -> str:
    """A whole message's Message-ID, from its header; "" if it has none. Pure."""
    return message_id_of(raw.split(b"\r\n\r\n", 1)[0].split(b"\n\n", 1)[0])


@dataclass(frozen=True)
class Appended:
    """One message restore appended: where its first copy went, and what was sent."""

    key: str
    folder: str
    uid: int | None  # from the APPEND's answer (UIDPLUS), else found by Message-ID
    raw: bytes
    ms: int


def group_by_folder(appended) -> dict[str, list[Appended]]:
    """Appended messages by the folder of their first copy, in order. Pure."""
    out: dict[str, list[Appended]] = {}
    for a in appended:
        out.setdefault(a.folder, []).append(a)
    return out


def appended_key(raw: bytes, ms: int) -> tuple[str, int]:
    """What an appended message is known by: its Message-ID, case folded, and its second. Pure."""
    return raw_message_id(raw).lower(), ms // 1000


def fetched_key(f: Fetched) -> tuple[str, int | None]:
    """appended_key of a message as fetched back (Message-ID header, INTERNALDATE). Pure."""
    return message_id_of(
        f.literal or b""
    ).lower(), None if f.internal is None else f.internal // 1000


def since(fetched: Mapping[int, Fetched], uid_next: int) -> dict[int, Fetched]:
    """The messages at or above `uid_next` (a UID n:* answer also holds the highest below). Pure."""
    return {uid: f for uid, f in fetched.items() if uid >= uid_next}


def already_stored(
    fetched: Mapping[int, Fetched], key: tuple[str, int], earlier: int
) -> int | None:
    """
    The UID of an APPEND a dropped connection hid, or None: among the messages
    appended since the batch began (`fetched`), the (earlier + 1)-th known by
    `key`, where `earlier` appends of the same message came before. Without a
    Message-ID nothing tells it apart, so None. Pure.
    """
    if not key[0]:
        return None
    matching = sorted(uid for uid, f in fetched.items() if fetched_key(f) == key)
    return matching[earlier] if len(matching) > earlier else None


def match_appended(items, fetched: Mapping[int, Fetched]) -> dict[str, int]:
    """
    Each appended message's UID ({key: UID}): the APPEND's own (UIDPLUS); else
    the first unclaimed message appended since the batch began (`fetched`) known
    by the same Message-ID and second, in append order, so two messages sharing a
    Message-ID get one UID each. Pure.
    """
    out = {a.key: a.uid for a in items if a.uid is not None}
    claimed = set(out.values())
    for a in items:
        if a.uid is not None:
            continue
        key = appended_key(a.raw, a.ms)
        found = next(
            (u for u in sorted(fetched) if u not in claimed and fetched_key(fetched[u]) == key),
            None,
        )
        if found is not None:
            out[a.key] = found
            claimed.add(found)
    return out


def read_back_ids(items, uids: Mapping[str, int], heads, bodies) -> tuple[dict[str, str], int]:
    """
    Each appended message's ID from what the server stored (server_id), by its
    matched UID's header (`heads`) or, without a Message-ID, its content
    (`bodies`); the predicted ID (appended_id) where nothing was read back, and
    how many those were. Pure.
    """
    ids, predicted = {}, 0
    for a in items:
        by_header = bool(raw_message_id(a.raw))
        uid = uids.get(a.key)
        got = None if uid is None else (heads if by_header else bodies).get(uid)
        found = server_id(got, by_header)
        ids[a.key] = found or appended_id(a.raw, a.ms)
        predicted += found is None
    return ids, predicted


def server_id(fetched: Fetched | None, by_header: bool) -> str | None:
    """
    The ID the next scan gives a message, from what the server stored: its
    Message-ID, date and size (`by_header`), as header_key, or its content's
    hash; None when the answer lacks them. A server may change what it stores
    (one adds a Content-Length header), so this, not appended_id, is the ID. Pure.
    """
    if fetched is None:
        return None
    if by_header:
        if fetched.internal is None or fetched.size is None:
            return None
        return header_key(fetched)
    return content_id(fetched.literal) if fetched.literal else None


def appended_id(raw: bytes, internal_ms: int) -> str:
    """
    The ID the next scan gives a message appended with this date: its header
    ID, or its content's without a Message-ID (as assign_ids does, collisions
    aside). Pure.
    """
    message_id = raw_message_id(raw)
    if not message_id:
        return content_id(raw)
    return header_id(message_id, internal_ms - internal_ms % 1000, len(raw))


def append_uid(data) -> int | None:
    """The UID an APPEND's answer names (UIDPLUS's APPENDUID), else None. Pure."""
    text = b" ".join(d for d in data if isinstance(d, bytes))
    found = re.search(rb"\[APPENDUID \d+ (\d+)\]", text)
    return int(found[1]) if found else None


def date_of(raw: bytes) -> int | None:
    """The Date header as ms since epoch, or None. Pure."""
    from email.utils import parsedate_to_datetime

    m = re.search(rb"^date:[ \t]*(.*)$", raw.split(b"\n\n", 1)[0], re.I | re.M)
    if m is None:
        return None
    try:
        return int(
            parsedate_to_datetime(m[1].decode("ascii", "replace").strip()).timestamp() * 1000
        )
    except TypeError, ValueError:
        return None


def under_review(scan: Scan) -> frozenset[tuple[str, str]] | None:
    """
    (ID, label) for every message in a review folder now, whose record must
    stay; None when a folder couldn't be read, so nothing is dropped on a
    guess. Pure.
    """
    if scan.skipped:
        return None
    reviews = {f.name: f.label for f in scan.folders if f.review}
    return frozenset(
        (gid, reviews[name])
        for gid, m in scan.messages.items()
        for name, _u in m.places
        if name in reviews
    )


class ImapAccount:
    """The read half of mailbox.Mailbox, over IMAP; see the module docstring."""

    provider = "imap"

    def __init__(
        self,
        account: str,
        settings: gmailapi.ImapSettings,
        password: str,
        folder: Path | None = None,
        connect=None,
        sleep=time.sleep,
    ):
        self.account = account
        self.settings = settings
        self._password = password
        self.folder = folder  # the backup folder, for the UID map; None: kept in memory
        self._connect = connect or self._connect_ssl
        self._sleep = sleep
        self._conn = None
        self._prior_map: dict[str, tuple[int, dict[int, str]]] | None = None
        self._cache: tuple[Mapping[str, frozenset[str]], Scan] | None = None
        self._entries = None  # LIST's answer, once read

    # ── connection ───────────────────────────────────────────────────────────
    def _connect_ssl(self):
        context = ssl.create_default_context()
        if self.settings.cafile is not None:
            context.load_verify_locations(self.settings.cafile)
        return imaplib.IMAP4_SSL(
            self.settings.host, self.settings.port, ssl_context=context, timeout=TIMEOUT
        )

    def _session(self):
        if self._conn is None:
            try:
                conn = self._connect()
            except (OSError, imaplib.IMAP4.error) as exc:
                raise gmailapi.GmailError(
                    f"{self.account}: can't reach {self.settings.host}: {exc}"
                ) from None
            self._conn = conn  # before LOGIN: a timeout or drop in it is _retrying's to close
            try:
                conn.login(self.settings.username, self._password)
            except imaplib.IMAP4.abort:  # a dropped connection, not a refusal: retried
                raise
            except imaplib.IMAP4.error as exc:  # AccessRefused isn't retried, nor dropped there
                drop(conn)
                self._conn = None
                said = exc.args[0] if exc.args else exc
                said = said.decode("utf-8", "replace") if isinstance(said, bytes) else said
                raise gmailapi.AccessRefused(
                    f"{self.account}: {self.settings.host} refused the login: {said}"
                ) from None
        return self._conn

    def _retrying(self, what: str, fn):
        """
        fn(conn), reconnecting after a dropped connection, ATTEMPTS times. Any
        other failure but the server's own NO also drops the connection: the
        command may have stopped halfway through a reply, so close() would
        send LOGOUT into the middle of it.
        """
        for attempt in range(ATTEMPTS):
            try:
                return fn(self._session())
            except gmailapi.GmailError:  # the server answered: the session is in step
                raise
            except BaseException as exc:
                drop(self._conn)
                self._conn = None
                if not isinstance(exc, (OSError, imaplib.IMAP4.abort)):  # timeouts are OSErrors
                    raise
                if attempt == ATTEMPTS - 1:
                    raise gmailapi.GmailError(f"{self.account}: {what} failed: {exc}") from None
                delay = 2**attempt
                level = logging.INFO if delay >= gmailapi.LONG_WAIT else logging.DEBUG
                logger.log(level, "%s: %s: %s; retrying in %d s", self.account, what, exc, delay)
                self._sleep(delay)

    def server_info(self) -> ServerInfo:
        """
        The server's capabilities and the folders a backup would read. Asks
        CAPABILITY after signing in: imaplib's conn.capabilities is the
        greeting's list, and servers name MOVE only once signed in.
        """
        entries = self._read_entries()
        capabilities = self._capabilities()
        folders = folders_from(entries, GMAIL_EXT in capabilities)
        return ServerInfo(capabilities, folders, marked_roles(entries))

    def _capabilities(self) -> frozenset[str]:
        return self._retrying(
            "reading capabilities",
            lambda conn: capabilities_from(self._ok(*conn.capability(), "CAPABILITY")),
        )

    def can_move(self) -> bool:
        """Whether the server has MOVE, which untrash() needs."""
        return "MOVE" in self._capabilities()

    def review_problem(self) -> str:
        """
        Why offload and dedup couldn't mark mail for review here, or "": what
        open_mailbox refuses for "modify", or, marking by keyword, an INBOX
        that keeps none (a SELECT that changes nothing). For a dry run, which
        opens the account read-only.
        """
        refusal = write_refusal(self.server_info())
        if refusal:
            return refusal
        if self.settings.review != "folder" and self.keywords_kept_in("INBOX") is False:
            return (
                "the server keeps no keywords in INBOX (PERMANENTFLAGS has no \\*), so "
                f"--label would fail; mailcellar setup {self.account} --review folder marks "
                "mail by moving it into a review folder instead"
            )
        return ""

    def unread(self) -> tuple[str, ...]:
        """The folders this run's scan skipped: refused, or listed short."""
        return tuple(sorted(self._cache[1].skipped)) if self._cache is not None else ()

    def close(self) -> None:
        """
        Log out, if signed in: the server sees the session end, not a dropped
        socket. Never raises: a backup that is done stays done.
        """
        conn, self._conn = self._conn, None
        if conn is not None:
            log_out(conn)

    def sign_in(self) -> None:
        """Connect and log in now; AccessRefused if the server refuses."""
        self._retrying("signing in", lambda conn: None)

    @staticmethod
    def _ok(typ, data, what: str):
        if typ != "OK":
            raise gmailapi.GmailError(f"{what}: {typ} {data!r}")
        return data

    def _fetch(self, folder: str, uids, items: str, what: str) -> dict[int, Fetched]:
        def run(conn):
            self._ok(*conn.select(quote(folder), readonly=True), f"EXAMINE {folder}")
            got: dict[int, Fetched] = {}
            for chunk in chunks(uids):
                typ, data = conn.uid("FETCH", ",".join(map(str, chunk)), items)
                got |= parse_fetch(self._ok(typ, data, what))
            return got

        return self._retrying(f"{what} in {folder}", run)

    # ── the UID map ──────────────────────────────────────────────────────────
    def _prior(self) -> dict[str, tuple[int, dict[int, str]]]:
        """The map as the backup last saved it, read once, read-only."""
        if self._prior_map is None:
            if self.folder is None:
                self._prior_map = {}
            else:
                from mailcellar.backup import sidecar

                self._prior_map = sidecar.read_imap_map(self.folder)
        return self._prior_map

    def _reviews(self) -> dict[str, tuple[tuple[str, str], ...]]:
        """Reviewed messages' folders before review, as recorded in the backup, read-only."""
        if self.folder is None:
            return {}
        from mailcellar.backup import sidecar

        return sidecar.read_imap_reviews(self.folder)

    def _known(self, name: str, uidvalidity: int) -> dict[int, str]:
        v, uids = self._prior().get(name, (None, {}))
        return dict(uids) if v == uidvalidity else {}

    def save_state(self, con) -> None:
        """The scan's UID map into the backup's meta.sqlite (con). No commit."""
        if self._cache is None:
            return
        from mailcellar.backup import sidecar

        _before, scan = self._cache
        for name, (uidvalidity, uids) in scan.maps.items():
            sidecar.replace_imap_folder(con, name, uidvalidity, uids)
        sidecar.drop_imap_folders(con, set(scan.maps) | set(scan.skipped))
        # The marks it saw, by label: restore leaves them off what it puts back, since a
        # backup records a keyword or a review folder as a label like any folder's.
        sidecar.add_mark_labels(con, frozenset().union(*(m.marks for m in scan.messages.values())))
        keep = under_review(scan)
        if keep is not None:  # finished reviews (kept, trashed or put back), not newer ones
            sidecar.keep_imap_reviews(con, keep, before_ms=scan.read_at)

    # ── reading ──────────────────────────────────────────────────────────────
    def _read_entries(self):
        """
        LIST's folders, read once until a write may have changed them
        (_changed): server_info and the scan share it.
        """

        def run(conn):
            typ, data = conn.list()
            return list_entries(self._ok(typ, data, f"{self.account}: LIST"))

        if self._entries is None:
            self._entries = self._retrying("listing folders", run)
        return self._entries

    def _read_folders(self, gmail: bool = False) -> tuple[Folder, ...]:
        return folders_from(self._read_entries(), gmail)

    def _list_uids(self, conn, exists: int | None) -> list[int]:
        """
        The examined folder's UIDs, in as many SEARCH passes as next_search
        asks for: a server with MESSAGELIMIT says it cut a SEARCH short in an
        OK that imaplib doesn't return. A listing that stalls, or ends short of
        EXAMINE's count, is refused (GmailError): the folder is skipped, and its
        mail not taken as gone.
        """
        found: frozenset[int] = frozenset()
        criteria: tuple[str, ...] | None = ("ALL",)
        while criteria is not None:
            typ, data = conn.uid("SEARCH", None, *criteria)
            got = search_uids(self._ok(typ, data, "SEARCH"))
            if stalled(found, got):
                break  # criteria still set: refused below
            criteria = next_search(found, got, exists)
            found |= got
        if criteria is not None or short_by(found, exists):
            shown = f" of {exists:,}" if exists is not None else ""
            raise gmailapi.GmailError(
                f"listed {len(found):,}{shown} messages: the server cut SEARCH short, "
                "and searching below what it gave didn't list the rest"
            )
        return sorted(found)

    def _read_folder(self, folder: Folder, gmail: bool = False) -> FolderRead | None:
        """
        One folder's UIDs, flags and new headers (on Gmail with X-GM-MSGID); None
        if it can't be examined.
        """
        items = "(INTERNALDATE RFC822.SIZE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]"
        items += " X-GM-MSGID)" if gmail else ")"

        def run(conn):
            typ, data = conn.select(quote(folder.name), readonly=True)
            exists = exists_from(self._ok(typ, data, f"EXAMINE {folder.name}"))
            _code, (uidvalidity, *_rest) = conn.response("UIDVALIDITY")
            uidvalidity = int(uidvalidity)
            uids = self._list_uids(conn, exists)
            known = self._known(folder.name, uidvalidity)
            flags: dict[int, frozenset[str]] = {}
            for chunk in chunks(uids):
                typ, data = conn.uid("FETCH", ",".join(map(str, chunk)), "(FLAGS)")
                fetched = parse_fetch(self._ok(typ, data, "FETCH FLAGS"))
                flags |= {uid: f.flags or frozenset() for uid, f in fetched.items()}
            headers: dict[int, Fetched] = {}
            for chunk in chunks(u for u in uids if u not in known and u in flags):
                typ, data = conn.uid("FETCH", ",".join(map(str, chunk)), items)
                headers |= parse_fetch(self._ok(typ, data, "FETCH headers"))
            # A UID expunged between SEARCH and FETCH has no flags or headers: left out.
            seen = {u: f for u, f in flags.items() if u in known or u in headers}
            return FolderRead(folder, uidvalidity, seen, known, headers)

        try:
            return self._retrying(f"reading {folder.name}", run)
        except gmailapi.GmailError as exc:
            if isinstance(exc, gmailapi.AccessRefused):
                raise
            logger.warning(f"{self.account}: skipped {folder.name}: {exc}")
            return None

    def _read_digests(self, locations) -> dict[Location, str]:
        """Content hashes for these locations; the bytes aren't kept."""
        out: dict[Location, str] = {}
        grouped: dict[str, list[int]] = {}
        for name, uid in sorted(locations):
            grouped.setdefault(name, []).append(uid)
        for name, uids in grouped.items():
            got = self._fetch(name, uids, "(BODY.PEEK[])", "FETCH body")
            out |= {(name, u): content_id(f.literal) for u, f in got.items() if f.literal}
        return out

    def _scan(self) -> tuple[Mapping[str, frozenset[str]], Scan]:
        """(the map before this run, every message now): read, then decide."""
        if self._cache is not None:
            return self._cache
        before = {name: frozenset(uids.values()) for name, (_v, uids) in self._prior().items()}
        self._cache = (before, self._read_scan())  # the map is written by save_state
        return self._cache

    def _read_scan(self) -> Scan:
        """Every folder read, and the messages decided."""
        read_at = int(time.time() * 1000)
        gmail = GMAIL_EXT in self._capabilities()
        folders = self._read_folders(gmail)
        reads, skipped = [], []
        # Inside restore's uploads, their line stays instead: see progress.part.
        with progress.part(f"{self.account}: reading folders", len(folders)) as p:
            for folder in folders:
                read = self._read_folder(folder, gmail)
                if read is None:
                    skipped.append(folder.name)
                else:
                    reads.append(read)
                p.advance()
        digests = self._read_digests(needs_content(reads))
        decided = merge(reads, assign_ids(reads, digests), self._reviews())
        return replace(decided, skipped=frozenset(skipped), read_at=read_at)

    def _messages(self) -> Mapping[str, Message]:
        return self._scan()[1].messages

    # ── Mailbox: listing ─────────────────────────────────────────────────────
    def list_message_ids(self, query=None, include_spam_trash=False, label_id=None):
        if query is not None:
            raise gmailapi.GmailError(
                f"{self.account} is an IMAP account: Gmail's search ({query!r}) doesn't apply"
            )
        return [
            gid
            for gid, m in self._messages().items()
            if (include_spam_trash or not m.labels & HIDDEN)
            and (label_id is None or label_id in m.labels)
        ]

    def labels_by_listing(self):
        return {gid: sorted(m.labels) for gid, m in self._messages().items()}

    # ── Mailbox: changes ─────────────────────────────────────────────────────
    def current_history_id(self):
        return TOKEN

    def history_changes(self, start_history_id):
        """Since the last run: every message now, and those gone (see changes())."""
        if start_history_id != TOKEN:
            raise gmailapi.HistoryExpired(
                f"{self.account}: {start_history_id!r} isn't an IMAP history point"
            )
        before, scan = self._scan()
        touched, deleted = changes(before, scan)
        return touched, deleted, TOKEN

    # ── Mailbox: content ─────────────────────────────────────────────────────
    def get_raw(self, gmail_ids, on_message):
        messages = self._messages()
        failed: list[str] = []
        for _gone in set(gmail_ids) - set(messages):
            progress.advance()  # gone since listing: skipped, as GmailAccount does
        for name, places in by_folder(messages, gmail_ids).items():
            try:
                got = self._fetch(
                    name, [u for u, _ in places], "(INTERNALDATE BODY.PEEK[])", "FETCH"
                )
            except gmailapi.GmailError as exc:
                failed += [f"{gid}: {exc}" for _uid, gid in places]
                continue
            for uid, gid in places:
                f = got.get(uid)
                if f is not None and f.literal is None:
                    failed.append(f"{gid}: {name} returned no body for UID {uid}")
                elif f is not None:
                    on_message(response(messages[gid], f.literal, f.internal))
                # f None: expunged since the scan, so gone, as GmailAccount skips it
                progress.advance()
        return failed

    def message_details(self, gmail_ids, failures=None):
        messages = self._messages()
        unread = [g for g in gmail_ids if g in messages and messages[g].internal is None]
        dates: dict[str, Fetched] = {}
        try:
            for name, places in by_folder(messages, unread).items():
                got = self._fetch(
                    name, [u for u, _ in places], "(INTERNALDATE RFC822.SIZE)", "FETCH dates"
                )
                dates |= {gid: got[uid] for uid, gid in places if uid in got}
        except gmailapi.GmailError as exc:
            if failures is None:
                raise
            failures += [f"{gid}: {exc}" for gid in unread]
            return {gid: None for gid in gmail_ids if gid not in messages}
        heads = self._backed_up_heads(gmail_ids)
        return {
            gid: details(messages.get(gid), dates.get(gid), heads.get(gid)) for gid in gmail_ids
        }

    def _backed_up_heads(self, gmail_ids) -> dict[str, bytes]:
        """
        The headers of each message's backed-up .eml, where the backup has it:
        what meta's thread backfill needs, which the server's details can't give
        without fetching every message again.
        """
        if self.folder is None:
            return {}
        from mailcellar.backup import gyb

        out = {}
        for gid, path in gyb.message_files(self.folder, gmail_ids).items():
            with path.open("rb") as fh:
                out[gid] = fh.read(64 * 1024)  # headers, and the start of the body
        return out

    # ── Mailbox: labels ──────────────────────────────────────────────────────
    def message_labels(self, gmail_ids, failures=None):
        messages = self._messages()
        return {gid: set(messages[gid].labels) if gid in messages else None for gid in gmail_ids}

    def label_catalogue(self):
        return [
            {"id": name, "name": name, "type": "system" if name in SYSTEM else "user"}
            for name in sorted(self._scan()[1].labels)
        ]

    def label_names(self, label_ids):
        return list(label_ids)  # a label's ID is its name

    # ── Mailbox: writes, which an IMAP account refuses (step 3) ──────────────
    # ── writing (step 3): only where open_mailbox allowed it ─────────────────
    def _write(self, folder: str, uids: list[int], what: str, fn, selected=None) -> None:
        """
        fn(conn, chunk) for each chunk of `uids`, with `folder` SELECTed for
        writing (the only place mailcellar SELECTs rather than EXAMINEs);
        selected(conn, folder), if given, first, to check what SELECT answered.
        With no `uids` it only SELECTs: keywords_kept_in reads PERMANENTFLAGS
        so, as EXAMINE reports none, changing nothing.
        """

        def run(conn):
            try:
                self._ok(*conn.select(quote(folder), readonly=False), f"SELECT {folder}")
                if selected is not None:
                    selected(conn, folder)
                for chunk in chunks(uids):
                    fn(conn, ",".join(map(str, chunk)))
            except imaplib.IMAP4.error as exc:
                # A change the server answered BAD (a keyword it won't keep) is refused, as
                # a NO is, and the session is in step. Reads keep raising: there a BAD is
                # most likely a command of mailcellar's gone wrong, to be seen.
                if not answered(exc):
                    raise
                raise gmailapi.GmailError(server_said(exc)) from None

        self._retrying(f"{what} in {folder}", run)

    def add_label(self, gmail_ids, label_name, review=True):
        """
        Mark messages for review: mailcellar's keyword for the label on every copy,
        or, with review = "folder", every copy moved into mailcellar/review/<label>,
        the folders it leaves recorded first in the backup's meta.sqlite. Returns
        "<id>: <why>" for those not marked, as trash() does.
        """
        if not review:  # restore's mark: a keyword in either mode; failing to set it is a note
            try:
                return self._keyword(gmail_ids, label_name)
            except gmailapi.AccessRefused:
                raise
            except KeywordsRefused:  # never can be: said once, not a note on every message
                logger.info(
                    f"{self.account}: the server keeps no keywords: not marked {label_name}"
                )
                return []
            except gmailapi.GmailError as exc:
                return [f"{gid}: {exc}" for gid in gmail_ids]
        if self.settings.review == "folder":
            return self._move_to_review(gmail_ids, label_name)
        return self._keyword(gmail_ids, label_name)

    def _keyword(self, gmail_ids, label_name) -> list[str]:
        """
        mailcellar's keyword for the label, on every copy of each message. A
        folder keeping no keywords fails its own mail (servers set PERMANENTFLAGS
        per folder); KeywordsRefused, the account's setup, only when INBOX and
        every folder touched keep none.
        """
        keyword = keyword_for(label_name)
        marking = keyword_marking(self._scan()[1], gmail_ids)
        keywordless: list[str] = []

        def keeps_keywords(conn, folder):
            try:
                self._require_keywords(conn, folder)
            except KeywordsRefused:
                keywordless.append(folder)
                raise

        def store(conn, uids):
            self._ok(*conn.uid("STORE", uids, "+FLAGS.SILENT", f"({keyword})"), "STORE")

        failures = self._carry_out(marking, f"marking {label_name}", store, keeps_keywords)
        if keywordless and set(keywordless) == set(marking.copies):
            # Known to keep none (not just unknown, the check failing): the account's setup.
            if "INBOX" in keywordless or self.keywords_kept_in("INBOX") is False:
                raise KeywordsRefused(
                    f"{self.account}: the server keeps no keywords of mailcellar's "
                    f"({', '.join(sorted({'INBOX', *keywordless}))}: PERMANENTFLAGS has no \\*); "
                    "set the account up with --review folder"
                )
        return failures

    @staticmethod
    def _require_keywords(conn, folder: str) -> None:
        """A check of what SELECT answered: raises KeywordsRefused if `folder` keeps none."""
        _code, (permanent, *_rest) = conn.response("PERMANENTFLAGS")
        if b"\\*" not in (permanent or b""):
            raise KeywordsRefused(f"{folder} keeps no keywords (PERMANENTFLAGS has no \\*)")

    def keywords_kept_in(self, folder: str) -> bool | None:
        """
        Whether `folder` keeps keywords, by a SELECT that changes nothing; None
        when the check itself fails (the folder refused, the connection gave
        out): not known, so nothing is concluded from it.
        """
        try:
            self._write(
                folder, [], "checking keywords", lambda conn, uids: None, self._require_keywords
            )
        except KeywordsRefused:
            return False
        except gmailapi.AccessRefused:
            raise
        except gmailapi.GmailError as exc:
            logger.debug("%s: couldn't check %s for keywords: %s", self.account, folder, exc)
            return None
        return True

    def _carry_out(self, marking: Marking, what: str, fn, selected=None) -> list[str]:
        """fn on every folder's copies, a refused folder failing its own; then re-read."""
        refused: dict[str, str] = {}
        try:
            for folder, uids in marking.copies.items():
                try:
                    self._write(folder, list(uids), what, fn, selected)
                except gmailapi.AccessRefused:
                    raise  # the account, not this folder
                except gmailapi.GmailError as exc:  # a folder keeping no keywords, too
                    refused[folder] = str(exc)
        finally:
            self._changed()
        return marking_failures(marking, refused, what)

    def _create(self, conn, name: str) -> None:
        """CREATE the folder; one that exists by now (another client, a run before) is fine."""
        typ, data = conn.create(quote(name))
        if typ != "OK" and b"ALREADYEXISTS" not in b" ".join(d for d in data if d).upper():
            self._ok(typ, data, f"CREATE {name}")

    def _move_to_review(self, gmail_ids, label_name) -> list[str]:
        """Folder review (add_label): its failures as "<id>: <why>"."""
        keyword_for(label_name)  # the same rule: a label a review folder can carry
        if self.folder is None:
            raise gmailapi.GmailError(
                f"{self.account}: reviewing by folder records where mail came from in the "
                "backup, and this account has none open"
            )
        marking = folder_marking(self._scan()[1], gmail_ids, label_name, self._read_entries())
        if not marking.copies:
            return list(marking.failures)
        review = marking.folder
        if marking.create:
            self._retrying(f"creating {review}", lambda conn: self._create(conn, review))
        from mailcellar.backup import sidecar

        con = sidecar.open_meta(self.folder)  # recorded before moving: never lost in between
        try:
            sidecar.record_imap_review(con, label_name, marking.origins, int(time.time() * 1000))
            con.commit()
        finally:
            con.close()
        return self._carry_out(marking, f"moving to {review}", self._mover(review))

    def trash(self, gmail_ids):
        """
        Move every copy of the messages to the server's Trash with MOVE, folder by
        folder, then read Trash and those folders again to confirm each is in Trash
        and nowhere else. Returns failures as "<id>: <why>"; a folder whose MOVE is
        refused fails its messages and the rest carry on. Nothing is expunged.
        """
        plan = trash_plan(self._scan()[1], gmail_ids)
        refused: dict[str, str] = {}
        try:
            for folder, uids in plan.moves.items():
                try:
                    self._write(folder, list(uids), "moving to Trash", self._mover(plan.trash))
                except gmailapi.GmailError as exc:
                    refused[folder] = str(exc)
        finally:
            self._changed()  # whatever got through
        # A full re-read: IDs are decided over the whole mailbox (two messages sharing
        # header IDs are told apart by content), so a partial one could rename one. With
        # the backup's UID map it lists folders and reads only the moved mail's headers.
        after = self._read_scan() if plan.confirm else None
        return trash_result(plan, refused, after)

    def _changed(self) -> None:
        """After a write: the scan, and the folder list, are read again when next needed."""
        self._cache = None
        self._entries = None

    def _mover(self, trash: str | None):
        def move(conn, chunk):
            self._ok(*conn.uid("MOVE", chunk, quote(trash)), "MOVE")

        return move

    # ── restore (step 3) ─────────────────────────────────────────────────────
    def _origins(self, gmail_ids) -> dict[str, tuple[tuple[str, str], ...]]:
        """Where each message lived, by the backup: a review's record, else its labels."""
        if self.folder is None:
            return {}
        from mailcellar.backup import gyb

        reviews = self._reviews()
        labelled = gyb.labels_of(self.folder, gmail_ids)
        return {
            gid: list(reviews[gid].values())[-1]  # the newest review's (read in order)
            if gid in reviews
            else tuple(("", label) for label in labelled.get(gid, []))
            for gid in gmail_ids
            if gid in reviews or gid in labelled
        }

    def _put_back(self, plan: PutBack, what: str) -> dict[str, str]:
        """Move each copy home, then copy into its further homes; ID → failure."""
        refused: dict[tuple[str, str], str] = {}
        copies: dict[str, tuple[str, str]] = {}
        for name in plan.create:
            self._retrying(f"creating {name}", lambda c, n=name: self._create(c, n))
        try:
            for src, by_dst in plan.moves.items():
                for dst, uids in by_dst.items():
                    try:
                        self._write(src, list(uids), what, self._mover(dst))
                    except gmailapi.GmailError as exc:
                        refused[(src, dst)] = str(exc)
        finally:
            self._changed()
        more = further_homes(plan, stuck_ids(plan, refused))
        if more:
            for gid, name, uid, dst in further_copies(more, self._scan()[1]):

                def copy(conn, chunk, dst=dst):
                    self._ok(*conn.uid("COPY", chunk, quote(dst)), "COPY")

                try:
                    self._write(name, [uid], f"copying to {dst}", copy)
                except gmailapi.GmailError as exc:
                    copies.setdefault(gid, (dst, str(exc)))
            self._changed()
        return put_back_failures(plan, refused, copies)

    def untrash(self, gmail_ids):
        """
        Move each message out of Trash into the folders it lived in (a review's
        record, else its labels in the backup; the archive if none), the first by MOVE
        and the others by COPY; failures as "<id>: <why>".
        """
        scan = self._scan()[1]
        trash = trash_folder(scan)
        plan = put_back(
            scan, gmail_ids, self._origins(gmail_ids), lambda name: trash and name == trash.name
        )
        failed = self._put_back(plan, "moving out of Trash")
        return untrash_result(plan, failed, self._scan()[1])

    def move_into(self, gmail_ids, label):
        """
        restore --into: one copy of each message moved into the folder for
        `label` (as an upload names it: append_plan), created if missing, from
        Trash only when it has no other copy; failures as "<id>: <why>". Copies
        in other folders stay, and are counted in the log.
        """
        scan = self._scan()[1]
        delim = next((d for _n, d, _f in self._read_entries() if d), None)
        kept = [f for f in scan.folders if not f.review]
        exact = {f.label: f.name for f in kept}
        folded = {f.label.casefold(): f.name for f in reversed(kept)}
        placed = place_label(label, exact, folded, delim)
        if placed.kind != "folder":
            return [f"{gid}: {label} is no folder name" for gid in gmail_ids]
        plan = into_moves(scan, gmail_ids, placed.name)
        failed = self._put_back(plan, f"moving into {placed.name}")
        after = self._scan()[1]
        moved = [gid for gid in plan.homes if gid not in failed]
        if extra := elsewhere_too(after, moved, placed.name):
            logger.info(
                f"{self.account}: {extra:,} messages in {placed.name} also have copies in "
                "other folders, left where they are"
            )
        return into_result(plan, failed, after, placed.name)

    def remove_label(self, gmail_ids, label_name):
        """
        Take a review mark off: any copy in the label's review folder moved back
        to the folders it came from, then the keyword from the copies that carry
        it; failures as "<id>: <why>", as add_label's.
        """
        scan = self._scan()[1]
        review = {f.name for f in scan.folders if f.review and f.label == label_name}
        plan = put_back(scan, gmail_ids, self._origins(gmail_ids), lambda n: n in review)
        failed = self._put_back(plan, f"taking {label_name} off")
        # MOVE keeps flags: what carried the keyword before still does, so with none
        # to take off, the server isn't read again.
        if not keyword_unmarking(scan, gmail_ids, label_name).copies:
            return [*plan.failures, *failed.values()]
        keyword = keyword_for(label_name)
        marking = keyword_unmarking(self._scan()[1], gmail_ids, label_name)

        def unstore(conn, uids):
            self._ok(*conn.uid("STORE", uids, "-FLAGS.SILENT", f"({keyword})"), "STORE")

        unmarked = self._carry_out(marking, f"taking {label_name} off", unstore)
        return [*plan.failures, *failed.values(), *unmarked]

    HEAD_ITEMS = "(INTERNALDATE RFC822.SIZE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])"

    def _uid_next(self, folder: str) -> int | None:
        """The UID `folder`'s next message will get (EXAMINE's UIDNEXT); None if not said."""

        def run(conn):
            self._ok(*conn.select(quote(folder), readonly=True), f"EXAMINE {folder}")
            _code, (value, *_rest) = conn.response("UIDNEXT")
            return int(value) if value else None

        try:
            return self._retrying(f"reading {folder}'s next UID", run)
        except gmailapi.AccessRefused:
            raise
        except gmailapi.GmailError:
            return None

    def _since(self, conn, folder: str, uid_next: int, items: str) -> dict[int, Fetched]:
        """What `folder` (EXAMINEd now) holds at or above `uid_next`, with `items`."""
        self._ok(*conn.select(quote(folder), readonly=True), f"EXAMINE {folder}")
        typ, data = conn.uid("FETCH", f"{uid_next}:*", items)
        return since(parse_fetch(self._ok(typ, data, "FETCH appended")), uid_next)

    def _append(
        self, name: str, flags: str, ms: int, raw: bytes, uid_next: int | None, earlier: int
    ) -> int | None:
        """
        APPEND `raw` to `name`; the UID the server gave it, where it says (UIDPLUS).
        A retry after a dropped connection first looks at what was appended since
        the batch began (`uid_next`): if the dropped attempt stored it, that copy is
        the message (already_stored, `earlier` appends of it having come before).
        """
        when = imaplib.Time2Internaldate(ms // 1000)
        tried = []

        def run(conn):
            if tried and uid_next is not None:
                stored = already_stored(
                    self._since(conn, name, uid_next, self.HEAD_ITEMS),
                    appended_key(raw, ms),
                    earlier,
                )
                if stored is not None:
                    return stored
            tried.append(True)
            return append_uid(
                self._ok(*conn.append(quote(name), flags, when, raw), f"APPEND {name}")
            )

        return self._retrying(f"appending to {name}", run)

    def _read_back(self, appended: list[Appended], uid_next) -> tuple[dict[str, str], int]:
        """
        Each appended message's ID ({key: ID}) from what the server stored, one
        range of FETCHes per folder from the UIDNEXT taken before the batch
        (`uid_next`: folder → UID): matched by the APPEND's UID or by Message-ID
        and date (match_appended). Predicted where nothing can be read back; how
        many those were (read_back_ids).
        """
        ids: dict[str, str] = {}
        predicted = 0
        for folder, items in group_by_folder(appended).items():

            def run(conn, f=folder, i=items):
                start = uid_next.get(f)
                own = sorted(a.uid for a in i if a.uid is not None)
                start = start if start is not None else (own[0] if own else None)
                heads = {} if start is None else self._since(conn, f, start, self.HEAD_ITEMS)
                uids = match_appended(i, heads)
                bare = sorted(uids[a.key] for a in i if a.key in uids and not raw_message_id(a.raw))
                bodies = {}
                for chunk in chunks(bare):
                    typ, data = conn.uid("FETCH", ",".join(map(str, chunk)), "(BODY.PEEK[])")
                    bodies |= parse_fetch(self._ok(typ, data, "FETCH appended"))
                return read_back_ids(i, uids, heads, bodies)

            try:
                found, missed = self._retrying(f"reading back what was appended in {folder}", run)
            except gmailapi.AccessRefused:
                raise
            except gmailapi.GmailError as exc:
                logger.debug("%s: %s: kept the predicted IDs: %s", self.account, folder, exc)
                found, missed = read_back_ids(items, {}, {}, {})
            ids |= found
            predicted += missed
        return ids, predicted

    def label_ids(self, names):
        """An IMAP account's labels are their own IDs; folders are made when mail goes in."""
        return list(names)

    def find_message_ids(self, message_ids):
        """
        {key: Message-ID} → {key: IDs of the messages on the server with it}, Trash
        included: the backup's record of each Message-ID where it has one, the
        server's headers for the rest (mail newer than the last backup).
        """
        scan = self._scan()[1]
        found: dict[str, list[str]] = {}
        known: dict[str, str] = {}
        if self.folder is not None:
            from mailcellar.backup import sidecar

            known = sidecar.read_message_ids(self.folder)
        for gid in scan.messages:
            if known.get(gid):
                found.setdefault(normalise_message_id(known[gid]), []).append(gid)
        unknown = [gid for gid in scan.messages if gid not in known]  # newer than the backup
        for name, places in by_folder(scan.messages, unknown).items():
            got = self._fetch(
                name,
                [u for u, _ in places],
                "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])",
                "FETCH Message-IDs",
            )
            for uid, gid in places:
                if uid in got and got[uid].literal is not None:
                    found.setdefault(
                        normalise_message_id(message_id_of(got[uid].literal)), []
                    ).append(gid)
        # The backup keeps Message-IDs bare and lower-cased: every side compared that way.
        return {
            key: sorted(set(found.get(normalise_message_id(mid), [])))
            for key, mid in message_ids.items()
        }

    def import_messages(self, messages, on_imported, dates=None):
        """
        APPEND each message ({key: (raw, label names)}) to its first folder with its
        flags and the date the backup says it was received (`dates`, else its Date
        header's), so it comes back under the same ID where the server stores it
        unchanged, and to its further folders likewise (the copies come back as one
        message with all those labels); folders are made first. on_imported(key,
        new ID) for each message in its first folder, once all are appended and read
        back (_read_back), the IDs being what the server stored, matched from the
        folder's UIDNEXT before the batch; a further folder it doesn't reach is logged.
        Stopped in between, a restore run again finds what was appended by its
        Message-ID. Failures: "<key>: <why>".
        """
        scan = self._scan()[1]
        delim = next((d for _n, d, _f in self._read_entries() if d), None)
        have = {f.name for f in scan.folders}
        failures = []
        appended: list[Appended] = []
        uid_next: dict[str, int | None] = {}  # folder → its UIDNEXT before this batch
        sent: dict[str, list[tuple[str, int]]] = {}  # folder → appended_key of what went there
        for key, (raw, labels) in messages.items():
            plan = append_plan(labels, scan.folders, delim)
            ms = (dates or {}).get(key) or date_of(raw) or int(time.time() * 1000)
            refused: dict[str, str] = {}
            first_uid = None
            for folder in plan.folders:
                try:
                    if folder in plan.create and folder not in have:
                        self._retrying(f"creating {folder}", lambda c, f=folder: self._create(c, f))
                        have.add(folder)
                    if folder not in uid_next:
                        uid_next[folder] = self._uid_next(folder)
                    key_sent = appended_key(raw, ms)
                    earlier = sent.setdefault(folder, []).count(key_sent)
                    uid = self._append(folder, plan.flags, ms, raw, uid_next[folder], earlier)
                    sent[folder].append(key_sent)
                    if folder == plan.folders[0]:
                        first_uid = uid
                except gmailapi.GmailError as exc:
                    refused[folder] = str(exc)
                    if folder == plan.folders[0]:
                        break  # not on the server: no copies to add
            failure, missed = append_outcome(key, plan.folders, refused)
            progress.advance()  # restore's "Importing" line: one more message tried
            if failure:
                failures.append(failure)
                continue
            if missed:  # restored all the same: only these copies are missing
                logger.warning(f"{self.account}: {key} not appended to {', '.join(missed)}")
            appended.append(Appended(key, plan.folders[0], first_uid, raw, ms))
        ids, predicted = self._read_back(appended, uid_next)
        if predicted:
            logger.warning(
                f"{self.account}: {predicted:,} appended messages couldn't be read back; "
                "their recorded IDs are predicted, and may differ from the server's"
            )
        for a in appended:  # in order, once their IDs are known
            on_imported(a.key, ids[a.key])
        self._changed()
        return failures


def details(message: Message | None, dates: Fetched | None, head: bytes | None) -> dict | None:
    """
    message_details' answer for one message, dates read now where given; its
    thread from `head`, the backed-up message's headers, else its own ID. Pure.
    """
    if message is None:
        return None
    return {
        "id": message.id,
        "threadId": thread_of(head, message.id) if head else message.id,
        "labelIds": sorted(message.labels),
        "internalDate": str(dates.internal if dates else message.internal or 0),
        "sizeEstimate": (dates.size if dates else message.size) or 0,
    }
