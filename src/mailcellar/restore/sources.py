"""
sources: mail to restore from somewhere other than the backup's own index.

  pruned   what `backup --prune` moved into <data root>/_pruned/<email>/<batch>/,
           with the labels and dates in its pruned.csv
  mbox     a Google Takeout export, or a file written by `mailcellar export`,
           with labels from each message's X-Gmail-Labels header
  folder   a gyb backup folder from elsewhere, read as it is (read-only)

Each gives Candidates for restore's plan(): read once here for what
narrowing and deciding need (sender, date, content hash), and again by
plan() and apply(), which check the copy hasn't changed in between. None
has a Gmail ID that means anything in the account restored into, so every
message takes the way back that restoring into another account takes:
found in Gmail by Message-ID and content, or imported.
"""

import csv
import email.utils
from datetime import datetime
from pathlib import Path

from mailcellar.backup import chain, gyb, sidecar
from mailcellar.message import PARSER_VERSION, parse_message
from mailcellar.paths import pruned_folder
from mailcellar.restore import mbox
from mailcellar.restore.apply import Candidate


class SourceError(Exception):
    pass


# ── decisions ────────────────────────────────────────────────────────────────


def local_ms(internal_date: str) -> int:
    """gyb's message_internaldate (local time) as ms UTC; 0 if unreadable."""
    try:
        return int(datetime.fromisoformat(internal_date).timestamp() * 1000)
    except ValueError:
        return 0


def date_header_ms(raw: bytes) -> int:
    """The Date header as ms UTC; 0 if missing or unreadable."""
    head = raw.split(b"\n\n", 1)[0]
    for line in head.splitlines():
        if line.lower().startswith(b"date:"):
            try:
                when = email.utils.parsedate_to_datetime(line[5:].decode("ascii", "replace"))
                return int(when.timestamp() * 1000)
            except TypeError, ValueError, OverflowError:
                return 0
    return 0


def candidate(source_id: str, raw: bytes, path: Path, labels, internal_date: int, **more):
    """A Candidate for a message read from a source, its fields parsed from `raw`."""
    fields = parse_message(raw)
    return Candidate(
        source_id,
        more.pop("batch", ""),
        "",
        path,
        tuple(labels),
        internal_date or date_header_ms(raw),
        fields["content_hash"],
        PARSER_VERSION,
        offloaded=False,
        sender=fields["sender_addr"].lower(),
        **more,
    )


def unreadable(
    source_id: str, path: Path | None, labels=(), internal_date=0, batch=""
) -> Candidate:
    """A Candidate whose copy is missing or can't be read: plan() skips it, saying why."""
    return Candidate(source_id, batch, "", path, tuple(labels), internal_date, "", "")


# ── reading ──────────────────────────────────────────────────────────────────


def pruned_batches(folder: Path) -> list[str]:
    """The batches `backup --prune` moved out of a backup, oldest first."""
    root = pruned_folder(folder)
    return sorted(d.name for d in root.glob("*") if (d / gyb.PRUNE_MANIFEST).is_file())


def from_pruned(folder: Path, batch: str) -> list[Candidate]:
    """The messages of one pruned batch of the backup in `folder`."""
    dest = pruned_folder(folder) / batch
    manifest = dest / gyb.PRUNE_MANIFEST
    if not manifest.is_file():
        have = ", ".join(pruned_batches(folder)) or "none"
        raise SourceError(f"{dest} has no {gyb.PRUNE_MANIFEST}; pruned batches: {have}")
    with manifest.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    recorded = chain.removed(folder)  # each file's SHA-256 as the prune recorded it
    out = []
    for row in rows:
        labels = [n for n in row["labels"].split(";") if n]
        ms = local_ms(row["internal_date"])
        path = dest / row["path"]
        try:
            raw = path.read_bytes()
        except OSError:
            out.append(unreadable(row["gmail_id"], None, labels, ms, batch))
            continue
        out.append(
            candidate(
                row["gmail_id"],
                raw,
                path,
                labels,
                ms,
                batch=batch,
                sha256=recorded.get(row["gmail_id"], ""),
            )
        )
    return out


def from_folder(folder: Path) -> list[Candidate]:
    """Every message of a gyb backup folder, read-only (old IDs of restored mail apart)."""
    if not (folder / "msg-db.sqlite").is_file():
        raise SourceError(f"{folder} is not a gyb backup: it has no msg-db.sqlite")
    superseded = sidecar.superseded_ids(folder)
    recorded = chain.held(folder)  # a mailcellar backup's own chain, if it has one
    marks = sidecar.read_mark_labels(folder)  # an IMAP backup's review marks: not put back
    out = []
    for bm in gyb.iter_backup(folder):
        labels = [label for label in bm.labels if label not in marks]
        if bm.gmail_id in superseded:
            continue
        ms = local_ms(bm.internal_date)
        try:
            raw = bm.path.read_bytes()
        except OSError:
            out.append(unreadable(bm.gmail_id, None, labels, ms))
            continue
        out.append(
            candidate(bm.gmail_id, raw, bm.path, labels, ms, sha256=recorded.get(bm.gmail_id, ""))
        )
    return out


def from_mbox(path: Path) -> list[Candidate]:
    """Every message of an mbox file; each is named mbox-<n>, counting from 1."""
    if not path.is_file():
        raise SourceError(f"{path} is not a file")
    with path.open("rb") as fh:
        spans = mbox.entries(fh)
    if not spans:
        raise SourceError(f"{path} has no messages: no line starts with 'From '")
    out = []
    with path.open("rb") as fh:
        for n, span in enumerate(spans, 1):
            fh.seek(span[0])
            entry = fh.read(span[1] - span[0])
            raw = mbox.message(entry)
            out.append(candidate(f"mbox-{n}", raw, path, mbox.labels(entry), 0, span=span))
    return out
