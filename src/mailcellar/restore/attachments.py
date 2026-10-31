"""
export --attachments: the attached files themselves, not the .eml files, to
a zip or a folder, with index.csv naming the message each came from: for
"every PDF the accountant sent since 2020".

The selection is export's. meta.sqlite's attachments table says which
messages have a part worth opening; a message whose row is from an older
parser (or missing) is opened anyway, and every part written comes from
parsing the .eml itself, so a stale row can only cost a read, never a wrong
file. Parts with Content-Disposition: attachment are taken; inline ones
(logos, signature images) only with --inline. --type narrows by MIME type,
subtype or extension. Identical content is written once, under the first
name it had; the index has a row for every message it came with.

Names: `<YYYY-MM-DD> <sender address> <filename>`, made safe on Windows,
macOS and Linux, at most NAME_BYTES bytes with the extension kept; a part
with no filename gets `attachment-<idx>.<ext>`; a clash (ignoring case and
Unicode normalisation, as macOS and Windows do) gets " (2)".

The decisions (wanted, to_open, read_parts, attachment_name, place,
index_row) are pure; reading meta.sqlite and writing are a thin layer.
"""

import mimetypes
import re
import sqlite3
import unicodedata
import zipfile
from collections.abc import Callable, Iterable, Mapping, Set
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from mailcellar.backup import sidecar
from mailcellar.message import (
    PARSER_VERSION,
    attachment_parts,
    decode_header_value,
    extract_address,
    parse_bytes,
)
from mailcellar.progress import Progress
from mailcellar.restore.apply import Candidate
from mailcellar.restore.export import INDEX_NAME, index_csv, read_eml, safe_cell, zip_entry

INDEX_COLUMNS = ["date", "sender", "subject", "id", "filename", "type", "size", "file"]
NAME_BYTES = 150  # well inside every filesystem's 255, with room for " (2)"
UNSAFE = re.compile(r'[/\\:*?"<>|\x00-\x1f\x7f]')  # not allowed in a name somewhere
# Python's own table, not the system's: the same extension on every machine.
TYPES = mimetypes.MimeTypes()


@dataclass(frozen=True)
class Part:
    """One attachment of one message, with what the index says about it."""

    source_id: str
    internal_date: int  # ms UTC
    sender: str  # the From header, decoded
    sender_addr: str
    subject: str
    idx: int  # among the message's non-body parts, as meta.sqlite numbers them
    filename: str  # decoded; "" if the part has none
    mime_type: str
    size: int
    sha256: str


@dataclass(frozen=True)
class Written:
    """What an attachment export wrote."""

    opened: int  # messages read
    messages: int  # messages with an attachment written
    attachments: int  # index rows
    files: int  # files written: fewer than attachments when content repeats
    size: int  # bytes of those files
    skipped: tuple[str, ...]  # IDs whose .eml is missing


# ── decisions ────────────────────────────────────────────────────────────────


def type_names(values: Iterable[str]) -> frozenset[str]:
    """--type values as wanted() compares them: lower case, no leading dot, none empty. Pure."""
    return frozenset(v.strip().lower().lstrip(".") for v in values) - {""}


def wanted(details: Mapping, inline: bool, types: Set[str]) -> bool:
    """
    Whether a part (attachment_parts' details or a meta.sqlite row) is
    exported: not inline unless asked, and of a --type if any is given,
    by MIME type, subtype or the filename's extension. Pure.
    """
    if details["inline"] and not inline:
        return False
    if not types:
        return True
    mime_type = details["mime_type"].lower()
    extension = Path(details["filename"]).suffix.lower().lstrip(".")
    return bool({mime_type, mime_type.partition("/")[2], extension} & types)


def to_open(
    cands: list[Candidate], rows: Mapping[str, list[dict]], inline: bool, types: Set[str]
) -> list[Candidate]:
    """
    The messages to read: those with a wanted part by meta.sqlite, and those
    whose rows an older parser wrote (or none did), which may be wrong. Pure.
    """
    return [
        c
        for c in cands
        if c.parser_version != PARSER_VERSION
        or any(wanted(row, inline, types) for row in rows.get(c.source_id, ()))
    ]


def read_parts(c: Candidate, raw: bytes, inline: bool, types: Set[str]) -> list[tuple[Part, bytes]]:
    """The message's wanted parts, with their content, from its raw bytes. Pure."""
    msg = parse_bytes(raw)
    from_raw = str(msg.get("From", ""))
    sender, sender_addr = decode_header_value(from_raw), extract_address(from_raw)
    subject = decode_header_value(msg.get("Subject", ""))
    return [
        (
            Part(
                c.source_id,
                c.internal_date,
                sender,
                sender_addr,
                subject,
                details["idx"],
                details["filename"],
                details["mime_type"],
                details["size"],
                details["sha256"],
            ),
            payload,
        )
        for details, payload in attachment_parts(msg)
        if wanted(details, inline, types)
    ]


def safe_name(text: str) -> str:
    """
    A file name every filesystem takes: characters one forbids become _,
    runs of space become one, no leading or trailing dot or space, NFC. Pure.
    """
    text = UNSAFE.sub("_", unicodedata.normalize("NFC", text))
    return " ".join(text.split()).strip(" .")


def extension(name: str) -> str:
    """
    The name's extension, "" if it has none: a suffix with a space in it is
    the sender's domain and the rest of the name, not an extension. Pure.
    """
    suffix = Path(name).suffix
    return "" if " " in suffix or len(suffix.encode()) > 16 else suffix


def shorten(name: str, limit: int = NAME_BYTES) -> str:
    """The name cut to `limit` bytes (UTF-8), its extension kept. Pure."""
    if len(name.encode()) <= limit:
        return name
    ext = extension(name)
    stem = name[: len(name) - len(ext)]
    room = limit - len(ext.encode())
    stem = stem.encode()[:room].decode(errors="ignore").rstrip(" .")
    return stem + ext


def attachment_name(p: Part) -> str:
    """`<YYYY-MM-DD> <sender address> <filename>`, made safe and short. Pure."""
    date = datetime.fromtimestamp(p.internal_date / 1000, UTC).strftime("%Y-%m-%d")
    filename = p.filename or f"attachment-{p.idx}{TYPES.guess_extension(p.mime_type) or '.bin'}"
    return shorten(safe_name(" ".join(x for x in (date, p.sender_addr, filename) if x)))


def numbered(name: str, n: int) -> str:
    """`report (2).pdf` for `report.pdf`, `README (2)` for `README`. Pure."""
    ext = extension(name)
    return f"{name[: len(name) - len(ext)]} ({n}){ext}"


def same_name(name: str) -> str:
    """The form two names clash in on macOS or Windows: case and normalisation ignored. Pure."""
    return unicodedata.normalize("NFC", name).casefold()


def place(name: str, taken: Set[str]) -> str:
    """The name, or the first numbered one, that clashes with none `taken` (same_name). Pure."""
    n, candidate = 1, name
    while same_name(candidate) in taken:
        n += 1
        candidate = numbered(name, n)
    return candidate


def index_row(p: Part, file: str) -> dict[str, str]:
    """One line of index.csv. Pure."""
    return {
        "date": datetime.fromtimestamp(p.internal_date / 1000, UTC).isoformat(timespec="seconds"),
        "sender": safe_cell(p.sender),
        "subject": safe_cell(p.subject),
        "id": p.source_id,
        "filename": safe_cell(p.filename),
        "type": p.mime_type,
        "size": str(p.size),
        "file": file,
    }


# ── reading and writing ──────────────────────────────────────────────────────


def read_rows(folder: Path, ids: Iterable[str]) -> dict[str, list[dict]]:
    """{ID: its meta.sqlite attachments rows}, opened read-only."""
    wanted_ids = set(ids)
    rows: dict[str, list[dict]] = {}
    con = sqlite3.connect(f"file:{sidecar.meta_path(folder)}?mode=ro", uri=True)
    try:
        for gid, filename, mime_type, inline in con.execute(
            "SELECT gmail_id, filename, mime_type, inline FROM attachments"
        ):
            if gid in wanted_ids:
                row = {"filename": filename, "mime_type": mime_type, "inline": bool(inline)}
                rows.setdefault(gid, []).append(row)
    finally:
        con.close()
    return rows


def export_parts(
    cands: list[Candidate],
    rows: Mapping[str, list[dict]],
    inline: bool,
    types: Set[str],
    write: Callable[[str, bytes, int | None], None],
) -> Written:
    """
    Write each wanted part once with `write(name, content, internal_date)`,
    then index.csv (with no date).
    """
    opened = to_open(cands, rows, inline, types)
    by_hash: dict[str, str] = {}
    taken = {same_name(INDEX_NAME)}
    index, skipped = [], []
    messages = size = 0
    with Progress("Exporting", len(opened)) as exporting:
        for c in opened:
            exporting.advance()
            raw = read_eml(c)
            if raw is None:
                skipped.append(c.source_id)
                continue
            parts = read_parts(c, raw, inline, types)
            messages += bool(parts)
            for part, payload in parts:
                file = by_hash.get(part.sha256)
                if file is None:
                    file = place(attachment_name(part), taken)
                    taken.add(same_name(file))
                    by_hash[part.sha256] = file
                    write(file, payload, part.internal_date)
                    size += len(payload)
                index.append(index_row(part, file))
    write(INDEX_NAME, index_csv(index, INDEX_COLUMNS).encode(), None)
    return Written(len(opened), messages, len(index), len(by_hash), size, tuple(skipped))


def write(
    cands: list[Candidate],
    part: Path,
    to_folder: bool,
    folder: Path,
    inline: bool,
    types: Set[str],
) -> Written:
    """Export the attachments into `part`: the empty folder made for it, or a zip file."""
    rows = read_rows(folder, (c.source_id for c in cands))
    if to_folder:

        def to_file(name: str, content: bytes, _internal_date: int | None) -> None:
            with (part / name).open("xb") as fh:
                fh.write(content)

        return export_parts(cands, rows, inline, types, to_file)
    with part.open("wb") as out, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:

        def to_zip(name: str, content: bytes, internal_date: int | None) -> None:
            zf.writestr(name if internal_date is None else zip_entry(name, internal_date), content)

        return export_parts(cands, rows, inline, types, to_zip)
