"""
export: copy a selection of the backup out of mailcellar, as an mbox file for
a mail client, or a zip of the .eml files with a CSV index, for an accountant
or a lawyer. The selection is restore's (--batch, --rule, --id, --offloaded
or --all, narrowed by --before, --after, --has-label and --sender).

Read-only: nothing changes in Gmail or the backup, and no Gmail access is
needed. The file is written under another name and renamed when complete, so
an interrupted export never leaves a file that looks finished. It refuses an
existing file, and a place inside the backup itself.

mbox: one `From ` line per message (the message's date, UTC), then the
message as stored, preceded by an X-Gmail-Labels header (comma-separated,
system labels by Takeout's English names, as Google Takeout writes it, and
user labels first), with lines that start with `From ` escaped the mboxrd way.
zip: the .eml files under their paths in the backup (<year>/<month>/<day>/),
and index.csv with the date, sender, subject, labels, ID and file.

The decisions (export_format, inside, takeout_names, mbox_entry, index_row,
safe_cell) are pure; reading the backup and writing the file are a thin layer around them.
"""

import argparse
import csv
import email.header
import io
import logging
import os
import re
import sqlite3
import sys
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from mailcellar.backup import sidecar
from mailcellar.dedup.apply import limit_count
from mailcellar.progress import Progress
from mailcellar.report.cli import resolve_folders
from mailcellar.report.db import AnalyticsError
from mailcellar.report.output import human_size
from mailcellar.restore.apply import Candidate, candidates
from mailcellar.restore.cli import add_selection_arguments, selection

logger = logging.getLogger(__name__)

INDEX_COLUMNS = ["date", "sender", "subject", "labels", "id", "file"]  # id: Gmail's or IMAP's
INDEX_NAME = "index.csv"
# Takeout's English names for Gmail's system labels, by the label IDs the
# backup stores; the categories are "Category Updates" and so on. Readers of
# Takeout's mbox know these names, not the IDs (restore --from-mbox reads both).
TAKEOUT_NAMES = {
    "INBOX": "Inbox",
    "SENT": "Sent",
    "STARRED": "Starred",
    "IMPORTANT": "Important",
    "UNREAD": "Unread",
    "DRAFT": "Drafts",
    "SPAM": "Spam",
    "TRASH": "Trash",
    "CHAT": "Chat",
}
# Gmail's other system labels, its "superstars": kept by ID (Takeout's names
# for them aren't known), but after the user's labels, with the system ones.
SUPERSTARS = {
    "YELLOW_STAR",
    "ORANGE_STAR",
    "RED_STAR",
    "PURPLE_STAR",
    "BLUE_STAR",
    "GREEN_STAR",
    "RED_BANG",
    "ORANGE_GUILLEMET",
    "YELLOW_BANG",
    "GREEN_CHECK",
    "BLUE_INFO",
    "PURPLE_QUESTION",
}


# ── decisions ────────────────────────────────────────────────────────────────


def export_format(dest: Path) -> str:
    """'mbox' or 'zip', from the file name. Raises ValueError."""
    suffix = dest.suffix.lower()
    if suffix == ".mbox":
        return "mbox"
    if suffix == ".zip":
        return "zip"
    raise ValueError(
        f"{dest.name}: name the file *.mbox (for a mail client) or *.zip "
        "(the .eml files and an index)"
    )


def inside(path: Path, folder: Path) -> bool:
    """Whether `path` is somewhere within `folder`."""
    return folder.resolve() in path.resolve().parents


def from_line(internal_date_ms: int) -> bytes:
    """The mbox separator line, with the message's date in UTC."""
    return (
        b"From MAILER-DAEMON " + time.asctime(time.gmtime(internal_date_ms / 1000)).encode() + b"\n"
    )


_FROM = re.compile(rb"^(>*From )", re.M)


def escape_from(raw: bytes) -> bytes:
    """mboxrd: a line that starts with From (after any >) gets one more >, so it can be undone."""
    return _FROM.sub(rb">\1", raw)


def newline_of(raw: bytes) -> bytes:
    """The line ending a message uses (Gmail's raw messages use CRLF)."""
    return b"\r\n" if b"\r\n" in raw[:4096] else b"\n"


def takeout_names(labels: tuple[str, ...]) -> tuple[str, ...]:
    """
    The labels as Takeout names them, user labels first: a reader that keeps
    only one label, as the folder (Bichon does), then keeps yours rather than
    Important or a category. Pure.
    """
    user, system = [], []
    for label in labels:
        if label in TAKEOUT_NAMES:
            system.append(TAKEOUT_NAMES[label])
        elif label.startswith("CATEGORY_") and label[9:].isalpha():
            system.append("Category " + label[9:].capitalize())
        elif label in SUPERSTARS:
            system.append(label)
        else:
            user.append(label)
    return tuple(user + system)


def labels_header(labels: tuple[str, ...], newline: bytes) -> bytes:
    """
    X-Gmail-Labels with the labels comma-separated, as Takeout writes it; empty
    for no labels. Control characters become spaces and non-ASCII names are
    encoded (RFC 2047), so the header is always one valid header.
    """
    if not labels:
        return b""
    value = ",".join(re.sub(r"[\x00-\x1f\x7f]", " ", label) for label in labels)
    if not value.isascii():
        value = email.header.Header(value, "utf-8").encode().replace("\n", newline.decode())
    return b"X-Gmail-Labels: " + value.encode("ascii") + newline


def mbox_entry(raw: bytes, internal_date_ms: int, labels: tuple[str, ...]) -> bytes:
    """One message as it goes into an mbox file."""
    newline = newline_of(raw)
    body = labels_header(takeout_names(labels), newline) + escape_from(raw)
    if not body.endswith(b"\n"):
        body += newline
    return from_line(internal_date_ms) + body + b"\n"


def safe_cell(text: str) -> str:
    """
    A CSV cell that a spreadsheet won't run as a formula: mail is written by
    others, and a subject can begin with = + - or @.
    """
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def index_row(c: Candidate, sender: str, subject: str, file: str) -> dict[str, str]:
    """One line of index.csv."""
    date = datetime.fromtimestamp(c.internal_date / 1000, UTC).isoformat(timespec="seconds")
    return {
        "date": date,
        "sender": safe_cell(sender),
        "subject": safe_cell(subject),
        "labels": safe_cell(";".join(takeout_names(c.labels))),
        "id": c.source_id,
        "file": file,
    }


def arcname(c: Candidate, folder: Path) -> str:
    """Where a message goes in a zip: its path in the backup."""
    try:
        return c.path.relative_to(folder).as_posix()
    except ValueError:
        return c.path.name


# ── reading and writing ──────────────────────────────────────────────────────


def read_details(folder: Path, ids: list[str]) -> dict[str, tuple[str, str]]:
    """{Gmail ID: (sender, subject)} from meta.sqlite, opened read-only."""
    wanted = set(ids)
    con = sqlite3.connect(f"file:{sidecar.meta_path(folder)}?mode=ro", uri=True)
    try:
        return {
            gid: (sender, subject)
            for gid, sender, subject in con.execute(
                "SELECT gmail_id, sender, subject FROM messages"
            )
            if gid in wanted
        }
    finally:
        con.close()


def read_eml(c: Candidate) -> bytes | None:
    """The message's .eml, or None if the backup has no file for it."""
    if c.path is None:
        return None
    try:
        return c.path.read_bytes()
    except OSError:
        return None


def write_mbox(cands: list[Candidate], out) -> tuple[int, int, list[str]]:
    """Write the messages to the open binary file: (messages, bytes, IDs whose file is missing)."""
    written = size = 0
    skipped = []
    with Progress("Exporting", len(cands)) as exporting:
        for c in cands:
            exporting.advance()
            raw = read_eml(c)
            if raw is None:
                skipped.append(c.source_id)
                continue
            entry = mbox_entry(raw, c.internal_date, c.labels)
            out.write(entry)
            written, size = written + 1, size + len(entry)
    return written, size, skipped


def write_zip(
    cands: list[Candidate], out, folder: Path, details: dict[str, tuple[str, str]]
) -> tuple[int, int, list[str]]:
    """Write the .eml files and index.csv to a zip: (messages, bytes, IDs whose file is missing)."""
    written = size = 0
    skipped = []
    rows = []
    with (
        zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf,
        Progress("Exporting", len(cands)) as exporting,
    ):
        for c in cands:
            exporting.advance()
            raw = read_eml(c)
            if raw is None:
                skipped.append(c.source_id)
                continue
            name = arcname(c, folder)
            sent = max(time.gmtime(c.internal_date / 1000)[:6], (1980, 1, 1, 0, 0, 0))
            info = zipfile.ZipInfo(name, date_time=sent)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, raw)
            sender, subject = details.get(c.source_id, ("", ""))
            rows.append(index_row(c, sender, subject, name))
            written, size = written + 1, size + len(raw)
        text = io.StringIO(newline="")
        writer = csv.DictWriter(text, fieldnames=INDEX_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
        zf.writestr(INDEX_NAME, text.getvalue())
    return written, size, skipped


# ── the command ──────────────────────────────────────────────────────────────


def main(argv=None, prog: str | None = None) -> int:
    p = argparse.ArgumentParser(
        prog=prog or "mailcellar export",
        description="Copy a selection of the backup out: an mbox file for a mail client, or a "
        "zip of the .eml files with a CSV index. Read-only: nothing changes in Gmail or the "
        "backup, and no Gmail access is needed.",
    )
    p.add_argument("account", help="The backup: address, config folder name or unique part")
    add_selection_arguments(p, all_help="Every message in the backup")
    p.add_argument(
        "--to",
        required=True,
        metavar="FILE",
        help="Where to write it: a name ending .mbox or .zip (it must not exist yet)",
    )
    p.add_argument(
        "--hold",
        metavar="NAME",
        help="The mail a legal hold keeps (see mailcellar hold), e.g. for counsel; the other "
        "options narrow it",
    )
    p.add_argument(
        "--limit", type=limit_count, default=None, help="Only the first N (oldest first)"
    )
    args = p.parse_args(argv)
    narrowing = selection(p, args, required=not args.hold)
    dest = Path(args.to).expanduser()
    try:
        fmt = export_format(dest)
    except ValueError as exc:
        p.error(str(exc))

    try:
        folders = resolve_folders([args.account])
    except AnalyticsError as exc:
        sys.exit(f"error: {exc}")
    (folder,) = folders
    if not sidecar.meta_path(folder).is_file():
        sys.exit(f"error: {folder} has no meta.sqlite; run mailcellar meta")
    if inside(dest, folder):
        sys.exit(f"error: {dest} is inside the backup; write the export somewhere else")
    if dest.exists():
        sys.exit(f"error: {dest} already exists; choose another name, or remove it first")
    if not dest.parent.is_dir():
        sys.exit(f"error: the folder {dest.parent} does not exist")

    ids = set(args.id)
    if args.hold:
        from mailcellar.hold import holds

        mine = [h for h in holds.read_holds(folder) if h.name == args.hold]
        if not mine:
            sys.exit(f"error: no hold {args.hold!r} in {folder.name}")
        covered = {
            f.gmail_id
            for f in holds.read_facts(folder)
            if f.known and holds.covers(mine[-1].criteria, f)
        }
        if not covered:
            sys.exit(f"Nothing to export: hold {args.hold!r} keeps no messages in the backup.")
        ids = (ids & covered) if ids else covered
    try:
        cands = candidates(
            folder,
            set(args.batch),
            set(args.rule),
            ids,
            everything=args.all,
            narrow=narrowing,
            include_restored=True,
            offloaded_only=args.offloaded,
        )
    except sidecar.MetaError as exc:
        sys.exit(f"error: {exc}")
    if args.limit is not None:
        cands = cands[: args.limit]
    if not cands:
        sys.exit("Nothing to export: no matching messages.")

    part = dest.with_name(dest.name + ".part")
    try:
        with part.open("wb") as out:
            if fmt == "mbox":
                written, size, skipped = write_mbox(cands, out)
            else:
                details = read_details(folder, [c.source_id for c in cands])
                written, size, skipped = write_zip(cands, out, folder, details)
        if not written:
            part.unlink()
            sys.exit(f"error: none of the {len(cands):,} messages has its file in the backup")
        os.replace(part, dest)
    except BaseException:
        part.unlink(missing_ok=True)  # never leave a half-written file behind
        raise
    print(f"Exported {written:,} messages ({human_size(size)}) to {dest}")
    if skipped:
        logger.warning(
            f"{len(skipped):,} messages have no .eml file in the backup and were left out, "
            f"e.g. {skipped[0]}. Run mailcellar verify to see what is missing."
        )
        return 1
    return 0
