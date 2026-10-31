"""
export: copy a selection of the backup out of mailcellar, as an mbox file for
a mail client, a maildir for the tools that read one (notmuch, mu, aerc,
mutt), or a zip of the .eml files with a CSV index, for an accountant or a
lawyer. The selection is restore's (--batch, --rule, --id, --offloaded
or --all, narrowed by --before, --after, --has-label and --sender).

Read-only: nothing changes in Gmail or the backup, and no Gmail access is
needed. The file or maildir is written under another name and renamed when
complete, so an interrupted export never leaves one that looks finished. It
refuses an existing file or folder, and a place inside the backup itself.

mbox: one `From ` line per message (the message's date, UTC), then the
message as stored, preceded by an X-Gmail-Labels header (comma-separated,
a label holding a comma or a quote in double quotes, system labels by
Takeout's English names, as Google Takeout writes it, and user labels
first), with lines that start with `From ` escaped the mboxrd way.
maildir: every message in cur/, with LF line endings, X-Gmail-Labels as in
mbox and X-Keywords as OfflineIMAP writes it (Gmail's IMAP names for system
labels, comma-separated, unquoted); flags D, F and S from DRAFT, STARRED and
UNREAD; index.csv at the top.
zip: the .eml files under their paths in the backup (<year>/<month>/<day>/),
and index.csv with the date, sender, subject, labels, ID and file.
--attachments: the attached files instead of the messages, to a zip or a
folder (attachments.py).

The decisions (export_format, inside, takeout_names, imap_names, csv_quoted,
mbox_entry, maildir_name, maildir_entry, index_row, safe_cell) are pure;
reading the backup and writing the export are a thin layer around them.
"""

import argparse
import csv
import email.header
import io
import logging
import os
import re
import shutil
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
# Gmail's system labels by the names its IMAP extension gives them, which
# OfflineIMAP writes in X-Keywords (its sample ignorelabels lists these). The
# others (UNREAD, the categories, chats, superstars) are no IMAP label.
IMAP_NAMES = {
    "INBOX": "\\Inbox",
    "STARRED": "\\Starred",
    "SENT": "\\Sent",
    "DRAFT": "\\Draft",
    "SPAM": "\\Spam",
    "TRASH": "\\Trash",
    "IMPORTANT": "\\Important",
}


# ── decisions ────────────────────────────────────────────────────────────────


def export_format(dest: str) -> str:
    """
    'mbox', 'zip' or 'maildir', from --to as typed: a name ending / is a
    maildir (a Path would drop the slash). Raises ValueError.
    """
    if dest.endswith(("/", os.sep)):
        return "maildir"
    suffix = Path(dest).suffix.lower()
    if suffix == ".mbox":
        return "mbox"
    if suffix == ".zip":
        return "zip"
    raise ValueError(
        f"{Path(dest).name}: name the file *.mbox (for a mail client) or *.zip "
        "(the .eml files and an index), or end the name with / for a maildir"
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


def is_category(label: str) -> bool:
    """One of Gmail's categories, CATEGORY_UPDATES and so on. Pure."""
    return label.startswith("CATEGORY_") and label[9:].isalpha()


def is_system(label: str) -> bool:
    """A Gmail system label (a Takeout name, a category or a superstar). Pure."""
    return label in TAKEOUT_NAMES or label in SUPERSTARS or is_category(label)


def takeout_name(label: str) -> str:
    """A system label's Takeout name; a superstar keeps its ID. Pure."""
    if is_category(label):
        return "Category " + label[9:].capitalize()
    return TAKEOUT_NAMES.get(label, label)


def takeout_names(labels: tuple[str, ...]) -> tuple[str, ...]:
    """
    The labels as Takeout names them, user labels first: a reader that keeps
    only one label, as the folder (Bichon does), then keeps yours rather than
    Important or a category. Pure.
    """
    user = [label for label in labels if not is_system(label)]
    system = [takeout_name(label) for label in labels if is_system(label)]
    return tuple(user + system)


def imap_names(labels: tuple[str, ...]) -> tuple[str, ...]:
    """
    The labels as OfflineIMAP's X-Keywords holds them: system labels by
    Gmail's IMAP names (IMAP_NAMES), user labels as they are, sorted; labels
    IMAP doesn't have left out. Pure.
    """
    kept = (label for label in labels if label in IMAP_NAMES or not is_system(label))
    return tuple(sorted(IMAP_NAMES.get(label, label) for label in kept))


def csv_quoted(label: str) -> str:
    """
    A label as one CSV field: in double quotes, its own doubled, if it holds
    a comma or a quote, so restore's CSV reading gives it back whole. Pure.
    """
    return '"' + label.replace('"', '""') + '"' if "," in label or '"' in label else label


def labels_header(
    labels: tuple[str, ...], newline: bytes, name: str = "X-Gmail-Labels", quoted: bool = True
) -> bytes:
    """
    A labels header (X-Gmail-Labels unless `name` says otherwise) with the
    labels comma-separated, as Takeout writes it, a label holding a comma or
    a quote CSV-quoted unless `quoted` is false (OfflineIMAP's X-Keywords
    knows no quoting); empty for no labels. Control characters become spaces
    and non-ASCII names are encoded (RFC 2047), so the header is always one
    valid header.
    """
    if not labels:
        return b""
    clean = (re.sub(r"[\x00-\x1f\x7f]", " ", label) for label in labels)
    value = ",".join(csv_quoted(label) if quoted else label for label in clean)
    if not value.isascii():
        value = email.header.Header(value, "utf-8").encode().replace("\n", newline.decode())
    return name.encode("ascii") + b": " + value.encode("ascii") + newline


def mbox_entry(raw: bytes, internal_date_ms: int, labels: tuple[str, ...]) -> bytes:
    """One message as it goes into an mbox file."""
    newline = newline_of(raw)
    body = labels_header(takeout_names(labels), newline) + escape_from(raw)
    if not body.endswith(b"\n"):
        body += newline
    return from_line(internal_date_ms) + body + b"\n"


def maildir_flags(labels: tuple[str, ...]) -> str:
    """The maildir flags, in ASCII order: D (draft), F (starred), S (seen). Pure."""
    on = {"D": "DRAFT" in labels, "F": "STARRED" in labels, "S": "UNREAD" not in labels}
    return "".join(flag for flag, set_ in on.items() if set_)


def maildir_name(c: Candidate) -> str:
    """A message's file name in cur/: its date (seconds), its ID, the flags. Pure."""
    safe_id = re.sub(r"[^0-9A-Za-z_-]", "_", c.source_id)
    return f"{c.internal_date // 1000}.{safe_id}.mailcellar:2,{maildir_flags(c.labels)}"


def maildir_entry(raw: bytes, labels: tuple[str, ...]) -> bytes:
    """
    One message as it goes into a maildir: LF line endings, as maildir
    readers expect, with X-Gmail-Labels and X-Keywords in front. Pure.
    """
    return (
        labels_header(takeout_names(labels), b"\n")
        + labels_header(imap_names(labels), b"\n", "X-Keywords", quoted=False)
        + raw.replace(b"\r\n", b"\n")
    )


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
            zf.writestr(zip_entry(name, c.internal_date), raw)
            sender, subject = details.get(c.source_id, ("", ""))
            rows.append(index_row(c, sender, subject, name))
            written, size = written + 1, size + len(raw)
        zf.writestr(INDEX_NAME, index_csv(rows))
    return written, size, skipped


def write_maildir(
    cands: list[Candidate], root: Path, details: dict[str, tuple[str, str]]
) -> tuple[int, int, list[str]]:
    """
    Write a maildir into the empty folder `root`: tmp/, new/ and cur/, every
    message in cur/, and index.csv: (messages, bytes, IDs whose file is missing).
    """
    for sub in ("tmp", "new", "cur"):
        (root / sub).mkdir()
    written = size = 0
    skipped = []
    rows = []
    with Progress("Exporting", len(cands)) as exporting:
        for c in cands:
            exporting.advance()
            raw = read_eml(c)
            if raw is None:
                skipped.append(c.source_id)
                continue
            name = f"cur/{maildir_name(c)}"
            entry = maildir_entry(raw, c.labels)
            with (root / name).open("xb") as fh:
                fh.write(entry)
            sender, subject = details.get(c.source_id, ("", ""))
            rows.append(index_row(c, sender, subject, name))
            written, size = written + 1, size + len(entry)
    (root / INDEX_NAME).write_text(index_csv(rows), encoding="utf-8", newline="")
    return written, size, skipped


def zip_entry(name: str, internal_date_ms: int) -> zipfile.ZipInfo:
    """A zip entry dated as the message (zip dates start in 1980), deflated, readable by all."""
    sent = max(time.gmtime(internal_date_ms / 1000)[:6], (1980, 1, 1, 0, 0, 0))
    info = zipfile.ZipInfo(name, date_time=sent)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    return info


def index_csv(rows: list[dict[str, str]], columns: list[str] = INDEX_COLUMNS) -> str:
    """index.csv's text."""
    text = io.StringIO(newline="")
    writer = csv.DictWriter(text, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)
    return text.getvalue()


def discard(part: Path, fmt: str) -> None:
    """Remove a half-written export: the .part file, or the .part maildir this run made."""
    if fmt == "maildir":
        shutil.rmtree(part, ignore_errors=True)
    else:
        part.unlink(missing_ok=True)


def warn_missing(skipped: list[str]) -> None:
    """Say which messages were left out for want of their .eml file, if any."""
    if skipped:
        logger.warning(
            f"{len(skipped):,} messages have no .eml file in the backup and were left out, "
            f"e.g. {skipped[0]}. Run mailcellar verify to see what is missing."
        )


# ── the command ──────────────────────────────────────────────────────────────


def main(argv=None, prog: str | None = None) -> int:
    p = argparse.ArgumentParser(
        prog=prog or "mailcellar export",
        description="Copy a selection of the backup out: an mbox file for a mail client, a "
        "maildir, or a zip of the .eml files with a CSV index; or, with --attachments, the "
        "attached files themselves. Read-only: nothing changes in Gmail or the backup, and no "
        "Gmail access is needed.",
    )
    p.add_argument("account", help="The backup: address, config folder name or unique part")
    add_selection_arguments(p, all_help="Every message in the backup")
    p.add_argument(
        "--to",
        required=True,
        metavar="FILE",
        help="Where to write it: a name ending .mbox or .zip, or ending / for a maildir "
        "(a plain folder with --attachments); it must not exist yet",
    )
    p.add_argument(
        "--attachments",
        action="store_true",
        help="The attached files, not the messages, with index.csv naming the message each "
        "came from: to a .zip, or a folder (--to ending /)",
    )
    p.add_argument(
        "--inline",
        action="store_true",
        help="With --attachments: inline parts too (images in the text, logos), left out otherwise",
    )
    p.add_argument(
        "--type",
        action="append",
        default=[],
        metavar="TYPE",
        help="With --attachments: only parts of this type, by MIME type (application/pdf), "
        "subtype (pdf) or extension; can be repeated",
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
        fmt = export_format(args.to)
    except ValueError as exc:
        p.error(str(exc))
    if args.attachments and fmt == "mbox":
        p.error("--attachments writes a zip or a folder: name it *.zip, or end it with /")
    if (args.inline or args.type) and not args.attachments:
        p.error("--inline and --type go with --attachments")

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

    if args.attachments or fmt == "mbox":
        details = {}
    else:
        details = read_details(folder, [c.source_id for c in cands])
    part = dest.with_name(dest.name + ".part")
    if fmt == "maildir":
        if part.exists():
            sys.exit(f"error: {part} exists, left by an interrupted export; remove it first")
        part.mkdir()
    try:
        if args.attachments:
            from mailcellar.restore import attachments

            types = attachments.type_names(args.type)
            # a folder, not a maildir: the files at its top
            done = attachments.write(cands, part, fmt == "maildir", folder, args.inline, types)
            written, size, skipped = done.files, done.size, list(done.skipped)
        elif fmt == "maildir":
            written, size, skipped = write_maildir(cands, part, details)
        else:
            with part.open("wb") as out:
                if fmt == "mbox":
                    written, size, skipped = write_mbox(cands, out)
                else:
                    written, size, skipped = write_zip(cands, out, folder, details)
        if not written:
            discard(part, fmt)
            if not args.attachments:
                sys.exit(f"error: none of the {len(cands):,} messages has its file in the backup")
            if done.opened and len(skipped) == done.opened:
                sys.exit(
                    f"error: none of the {done.opened:,} messages with a matching attachment "
                    "has its file in the backup"
                )
            warn_missing(skipped)
            sys.exit(
                f"Nothing to export: none of the {len(cands):,} messages has a matching attachment."
            )
        os.replace(part, dest)
    except BaseException:
        discard(part, fmt)  # never leave a half-written export behind
        raise
    if args.attachments:
        print(
            f"Exported {done.attachments:,} attachments from {done.messages:,} messages "
            f"({human_size(size)}) to {dest}"
        )
        if done.files < done.attachments:
            print(
                f"{done.attachments - done.files:,} had the same content as one before them "
                "and were written once; index.csv lists every message."
            )
    else:
        print(f"Exported {written:,} messages ({human_size(size)}) to {dest}")
    warn_missing(skipped)
    return 1 if skipped else 0
