"""
index: bring search.sqlite in line with the backups.

Each message's sender, recipients, subject and readable body text go into the
full-text index; attachments do not. Built incrementally, like dedup index: a
message is read again only when it is new, its file or size changed, or
TEXT_VERSION changed. The text is extracted here, not by parse_message(), so
meta.sqlite stays the size it is and PARSER_VERSION is not involved.
"""

import email.parser
import email.policy
import html
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from mailcellar.backup import sidecar
from mailcellar.backup.gyb import backup_account, iter_backup
from mailcellar.message import decode_header_value, recipients, strip_mbox_from_lines
from mailcellar.progress import Progress

logger = logging.getLogger(__name__)

# Bump when document() output changes; the next search re-reads every message.
TEXT_VERSION = "1"
MAX_BODY = 200_000  # characters of body text indexed per message
COMMIT_EVERY = 1000


# ── decisions ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Document:
    """What the index holds for one message."""

    sender: str
    recipients: str
    subject: str
    body: str


_HIDDEN_RE = re.compile(r"<(style|script|head|title)\b.*?</\1\s*>|<!--.*?-->", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]*>")


def html_text(markup: str) -> str:
    """The text a reader sees in HTML: no tags, styles, scripts or comments; entities decoded."""
    return " ".join(html.unescape(_TAG_RE.sub(" ", _HIDDEN_RE.sub(" ", markup))).split())


def _decoded(part) -> str:
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("latin-1", errors="replace")


def readable_body(msg) -> str:
    """
    The body as a reader sees it: the text/plain parts, or, for a message
    with only HTML, its HTML parts as text. Attachments are left out.
    Whitespace collapsed, at most MAX_BODY characters.
    """
    plain, markup = [], []
    for part in msg.walk():
        if part.is_multipart() or part.get_content_maintype() != "text":
            continue
        if part.get_content_disposition() == "attachment":
            continue
        subtype = part.get_content_subtype()
        if subtype == "plain":
            plain.append(_decoded(part))
        elif subtype == "html":
            markup.append(_decoded(part))
    text = " ".join(plain) if plain else " ".join(html_text(m) for m in markup)
    return " ".join(text.split())[:MAX_BODY]


def document(raw: bytes) -> Document:
    """The indexed fields of one message's raw bytes."""
    msg = email.parser.BytesParser(policy=email.policy.compat32).parsebytes(
        strip_mbox_from_lines(raw)
    )
    people = " ".join(f"{r['name']} {r['address']}".strip() for r in recipients(msg))
    return Document(
        sender=decode_header_value(str(msg.get("From", ""))),
        recipients=people,
        subject=decode_header_value(msg.get("Subject", "")),
        body=readable_body(msg),
    )


@dataclass(frozen=True)
class Changes:
    """What an update has to do: Gmail IDs to (re)index, and to remove."""

    index: tuple[str, ...]
    remove: tuple[str, ...]


def changes(known: dict[str, tuple], current: dict[str, tuple[str, int]]) -> Changes:
    """
    known: {Gmail ID: (path, size, text_version)} in the index; current:
    {Gmail ID: (path, size)} in the backup. Pure.
    """
    index = tuple(
        sorted(gid for gid, (p, s) in current.items() if known.get(gid) != (p, s, TEXT_VERSION))
    )
    return Changes(index, tuple(sorted(set(known) - set(current))))


def local_date_ms(internal_date: str) -> int:
    """gyb's message_internaldate (local time, 'YYYY-MM-DD HH:MM:SS') as ms UTC; 0 if unreadable."""
    try:
        return int(datetime.fromisoformat(internal_date).timestamp() * 1000)
    except ValueError:
        return 0


# ── reading and writing ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class Stats:
    account: str
    total: int
    indexed: int
    removed: int
    errors: int


def meta_dates(folder: Path) -> dict[str, int]:
    """{Gmail ID: internal date, ms UTC} from meta.sqlite, read-only; {} without one."""
    path = sidecar.meta_path(folder)
    if not path.is_file():
        return {}
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {
            gid: ms for gid, ms in con.execute("SELECT gmail_id, internal_date FROM messages") if ms
        }
    except sqlite3.Error:
        return {}
    finally:
        con.close()


def update(con: sqlite3.Connection, folder: Path) -> Stats:
    """Bring the index in line with one backup folder. Reads the backup, never writes it."""
    account = backup_account(folder)
    superseded = sidecar.superseded_ids(folder)
    backup = {bm.gmail_id: bm for bm in iter_backup(folder) if bm.gmail_id not in superseded}
    current = {
        gid: (bm.path.relative_to(folder).as_posix(), bm.path.stat().st_size)
        for gid, bm in backup.items()
    }
    known = {
        gid: (path, size, version)
        for gid, path, size, version in con.execute(
            "SELECT gmail_id, path, size, text_version FROM docs WHERE account = ?", (account,)
        )
    }
    todo = changes(known, current)
    if todo.index:
        what = "Building the search index" if not known else "Updating the search index"
        logger.info(f"{what} for {account}: {len(todo.index):,} messages to read")
    dates = meta_dates(folder) if todo.index else {}

    indexed = errors = 0
    with Progress(f"{account}: indexing", len(todo.index)) as indexing:
        for n, gid in enumerate(todo.index, 1):
            indexing.advance()
            bm = backup[gid]
            try:
                doc = document(bm.path.read_bytes())
            except Exception as exc:
                logger.warning(f"Cannot read {bm.path}: {exc}")
                errors += 1
                continue
            _remove(con, account, gid)
            path, size = current[gid]
            cur = con.execute(
                """INSERT INTO docs (account, gmail_id, path, size, internal_date, sender,
                                     subject, text_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    account,
                    gid,
                    path,
                    size,
                    dates.get(gid) or local_date_ms(bm.internal_date),
                    doc.sender,
                    doc.subject,
                    TEXT_VERSION,
                ),
            )
            con.execute(
                "INSERT INTO text (rowid, sender, recipients, subject, body) "
                "VALUES (?, ?, ?, ?, ?)",
                (cur.lastrowid, doc.sender, doc.recipients, doc.subject, doc.body),
            )
            indexed += 1
            if n % COMMIT_EVERY == 0:
                con.commit()
    for gid in todo.remove:
        _remove(con, account, gid)
    con.commit()
    return Stats(account, len(current), indexed, len(todo.remove), errors)


def _remove(con: sqlite3.Connection, account: str, gmail_id: str) -> None:
    row = con.execute(
        "SELECT doc FROM docs WHERE account = ? AND gmail_id = ?", (account, gmail_id)
    ).fetchone()
    if row:
        con.execute("DELETE FROM text WHERE rowid = ?", row)
        con.execute("DELETE FROM docs WHERE doc = ?", row)
