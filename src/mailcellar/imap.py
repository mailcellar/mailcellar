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
only if it lives nowhere else.

A message's ID is 16 hex digits, as Gmail's are: a hash of its Message-ID,
INTERNALDATE and size, which a move keeps, so the same message in two
folders is one message with two labels. Where that can't tell messages
apart (no Message-ID, or one header ID at two new places with different
content), the content's hash is the ID.

A scan reads (the UID map as the backup last saved it, folders, UIDs,
flags, the headers of mail the map doesn't know, content hashes where
needed) and decides with pure functions over frozen values (IDs, labels,
where to fetch each message, what is gone). The new map is written only
by save_state, through the backup's own meta.sqlite connection
(meta.sqlite's imap_map, backup/sidecar.py).
"""

import base64
import binascii
import hashlib
import imaplib
import logging
import re
import ssl
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from mailcellar import gmailapi, progress
from mailcellar.mailbox import IMAP_BACKUP_ONLY

logger = logging.getLogger(__name__)

FETCH_CHUNK = 200  # UIDs per FETCH
ATTEMPTS = 3  # tries per folder after a dropped connection
TIMEOUT = 120  # seconds a socket may stall
SPECIAL = {"\\Sent": "SENT", "\\Drafts": "DRAFT", "\\Junk": "SPAM", "\\Trash": "TRASH"}
BY_NAME = {"sent": "SENT", "drafts": "DRAFT", "junk": "SPAM", "spam": "SPAM", "trash": "TRASH"}
SKIP = {"\\Noselect", "\\NonExistent", "\\All", "\\Flagged"}  # not mail, or copies of it
SYSTEM = {"INBOX", "SENT", "DRAFT", "SPAM", "TRASH", "UNREAD", "STARRED"}
HIDDEN = frozenset({"SPAM", "TRASH"})  # left out of listings unless include_spam_trash
TOKEN = "imap:1"  # a history point: the UID map is the state

LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\) (?P<delim>"(?:[^"\\]|\\.)*"|NIL) (?P<name>.+)$')

Location = tuple[str, int]  # (folder, uid)


# ── values ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Folder:
    name: str  # as the server names it
    label: str  # as the backup names it


@dataclass(frozen=True)
class Fetched:
    """One message's part of a UID FETCH answer."""

    uid: int
    flags: frozenset[str] | None = None
    internal: int | None = None  # ms since epoch
    size: int | None = None
    literal: bytes | None = None


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
    labels: frozenset[str]
    where: Location  # where to fetch it from
    internal: int | None = None  # None: not read this run (the map knew it)
    size: int | None = None


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


@dataclass(frozen=True)
class Scan:
    messages: Mapping[str, Message]
    labels: frozenset[str]
    maps: Mapping[str, tuple[int, Mapping[int, str]]]  # folder → (UIDVALIDITY, UID → ID)
    skipped: frozenset[str] = field(default_factory=frozenset)  # folders that couldn't be read


# ── pure: parsing ────────────────────────────────────────────────────────────


def decode_utf7(name: str) -> str:
    """A folder name in IMAP's modified UTF-7 (RFC 3501 5.1.3); as given if malformed."""
    parts = re.split(r"(&[^-]*-)", name)
    try:
        return "".join(_decode_shift(p) if p.startswith("&") else p for p in parts)
    except binascii.Error, UnicodeDecodeError:
        return name


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


def folders_from(entries) -> tuple[Folder, ...]:
    """
    The folders to back up, with their labels. A name stands for a role
    (Trash, Junk…) only when the server marks no folder for it: iCloud marks
    Sent and Trash but not its Junk, while a user's own folder called Junk
    stays theirs on a server that marks its real one. Pure.
    """
    by_name = frozenset(BY_NAME.values()) - marked_roles(entries)
    return tuple(
        Folder(name, folder_label(name, delim, flags, by_name=by_name))
        for name, delim, flags in entries
        if not flags & SKIP
    )


def marked_roles(entries) -> frozenset[str]:
    """The roles (SENT, TRASH…) the server marks a folder for, special-use. Pure."""
    return frozenset(SPECIAL[flag] for _n, _d, flags in entries for flag in flags & set(SPECIAL))


def search_uids(data) -> frozenset[int]:
    """SEARCH's answer as UIDs. Pure."""
    return frozenset(int(u) for u in b" ".join(d for d in data if d).split())


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
    decoded = decode_utf7(name)
    return decoded.replace(delim, "/") if delim and delim != "/" else decoded


def flag_labels(flags) -> frozenset[str]:
    return frozenset(
        ({"UNREAD"} if "\\Seen" not in flags else set())
        | ({"STARRED"} if "\\Flagged" in flags else set())
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
    return Fetched(
        uid=int(uid[1]),
        flags=frozenset(flags[1].decode().split()) if flags else None,
        internal=internal_ms(date[1].decode()) if date else None,
        size=int(size[1]) if size else None,
        literal=literal,
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
    at more than one new place, which may be copies or different messages. Pure.
    """
    places: dict[str, list[Location]] = {}
    without = []
    for read in reads:
        for uid, f in read.headers.items():
            key = header_key(f)
            if key is None:
                without.append((read.folder.name, uid))
            else:
                places.setdefault(key, []).append((read.folder.name, uid))
    shared = [loc for locs in places.values() if len(locs) > 1 for loc in locs]
    return frozenset(without + shared)


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


def merge(reads, ids: Mapping[Location, str]) -> Scan:
    """
    Locations merged into messages: the labels of every folder a message is in
    (Trash and Junk only if it is in nothing else) and its flags; fetched from
    a folder it lives in. Pure.
    """
    placed: dict[str, list[tuple[FolderRead, int]]] = {}
    for read in reads:
        for uid in read.flags:
            loc = (read.folder.name, uid)
            if loc in ids:
                placed.setdefault(ids[loc], []).append((read, uid))
    messages = {}
    for gid, places in placed.items():
        folder_labels = {read.folder.label for read, _uid in places}
        kept = folder_labels - HIDDEN or folder_labels
        flags = frozenset().union(*(read.flags[uid] for read, uid in places))
        home = [(r, u) for r, u in places if r.folder.label in kept]
        read, uid = (home or places)[0]
        header = read.headers.get(uid)
        messages[gid] = Message(
            gid,
            frozenset(kept) | flag_labels(flags),
            (read.folder.name, uid),
            header.internal if header else None,
            header.size if header else None,
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
    labels = frozenset({read.folder.label for read in reads} | {"UNREAD", "STARRED"})
    return Scan(messages, labels, maps)


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
        "threadId": "",
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
        caps = self._retrying(
            "reading capabilities",
            lambda conn: capabilities_from(self._ok(*conn.capability(), "CAPABILITY")),
        )
        return ServerInfo(caps, folders_from(entries), marked_roles(entries))

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

    # ── reading ──────────────────────────────────────────────────────────────
    def _read_entries(self):
        def run(conn):
            typ, data = conn.list()
            return list_entries(self._ok(typ, data, f"{self.account}: LIST"))

        return self._retrying("listing folders", run)

    def _read_folders(self) -> tuple[Folder, ...]:
        return folders_from(self._read_entries())

    def _list_uids(self, conn, exists: int) -> list[int]:
        """
        The examined folder's UIDs. A server with MESSAGELIMIT (RFC 9738;
        Yahoo's is 1,000) answers a SEARCH over more messages with the
        highest UIDs only, and says so in an OK that imaplib doesn't return;
        so search again below the lowest UID found until EXAMINE's count is
        reached or a pass finds nothing.
        """
        found: set[int] = set()
        criteria: tuple[str, ...] = ("ALL",)
        while True:
            typ, data = conn.uid("SEARCH", None, *criteria)
            got = search_uids(self._ok(typ, data, "SEARCH"))
            found |= got
            if not got or len(found) >= exists or min(got) <= 1:
                return sorted(found)
            criteria = ("UID", f"1:{min(got) - 1}")

    def _read_folder(self, folder: Folder) -> FolderRead | None:
        """One folder's UIDs, flags and new headers; None if it can't be examined."""

        def run(conn):
            typ, data = conn.select(quote(folder.name), readonly=True)
            exists = int(self._ok(typ, data, f"EXAMINE {folder.name}")[0])
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
                typ, data = conn.uid(
                    "FETCH",
                    ",".join(map(str, chunk)),
                    "(INTERNALDATE RFC822.SIZE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])",
                )
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
        folders = self._read_folders()
        reads, skipped = [], []
        with progress.Progress(f"{self.account}: reading folders", len(folders)) as p:
            for folder in folders:
                read = self._read_folder(folder)
                if read is None:
                    skipped.append(folder.name)
                else:
                    reads.append(read)
                p.advance()
        digests = self._read_digests(needs_content(reads))
        decided = merge(reads, assign_ids(reads, digests))
        scan = Scan(decided.messages, decided.labels, decided.maps, frozenset(skipped))
        self._cache = (before, scan)  # the map is written by save_state, through backup's con
        return self._cache

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
        return {gid: details(messages.get(gid), dates.get(gid)) for gid in gmail_ids}

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
    def _refuse(self, *_args, **_kwargs):
        raise gmailapi.GmailError(IMAP_BACKUP_ONLY.format(account=self.account))

    label_ids = add_label = remove_label = trash = untrash = _refuse
    find_message_ids = import_messages = _refuse


def details(message: Message | None, dates: Fetched | None) -> dict | None:
    """message_details' answer for one message, dates read now where given. Pure."""
    if message is None:
        return None
    return {
        "id": message.id,
        "threadId": "",
        "labelIds": sorted(message.labels),
        "internalDate": str(dates.internal if dates else message.internal or 0),
        "sizeEstimate": (dates.size if dates else message.size) or 0,
    }
