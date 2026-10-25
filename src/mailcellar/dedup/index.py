"""
index: catalogue the backups into dedup.sqlite.
"""

import logging
import sqlite3
from pathlib import Path

from mailcellar.backup import sidecar
from mailcellar.backup.gyb import backup_account, iter_backup
from mailcellar.message import parse_eml

logger = logging.getLogger(__name__)


def index_backup(con: sqlite3.Connection, backup_dir: Path, reparse: bool = False) -> dict:
    """
    Bring dedup.sqlite in line with one backup folder. Returns counts.
    A message is re-parsed when it is new, its path (relative to the backup
    folder) or size changed, or `reparse` is set, so moving the whole folder
    costs nothing. Fields come from the folder's meta.sqlite when it has
    them for the current parser; only the rest are parsed from .eml files.
    Labels are replaced wholesale each run, since they change in place.
    """
    account = backup_account(backup_dir)
    from_meta = sidecar.current_fields(backup_dir)
    # A restored message's old ID shares its file with the new one; index it once.
    superseded = sidecar.superseded_ids(backup_dir)
    known = {
        gid: (path, size)
        for gid, path, size in con.execute(
            "SELECT gmail_id, path, size FROM messages WHERE account = ?", (account,)
        )
    }
    if reparse:
        known = {gid: None for gid in known}

    stats = {
        "account": account,
        "total": 0,
        "parsed": 0,
        "from_meta": 0,
        "unchanged": 0,
        "removed": 0,
        "errors": 0,
    }
    seen = set()
    label_rows = []

    logger.info(f"Indexing {account} …")
    for i, bm in enumerate(iter_backup(backup_dir), 1):
        if i % 5000 == 0:
            logger.info(f"  …{i:,} messages")
        if bm.gmail_id in superseded:
            continue
        stats["total"] += 1
        seen.add(bm.gmail_id)
        label_rows.extend((account, bm.gmail_id, label) for label in bm.labels)

        size = bm.path.stat().st_size
        rel = bm.path.relative_to(backup_dir).as_posix()
        if known.get(bm.gmail_id) == (rel, size):
            stats["unchanged"] += 1
            continue

        fields = from_meta.get(bm.gmail_id)
        if fields is not None:
            stats["from_meta"] += 1
        else:
            try:
                fields = parse_eml(bm.path)
            except Exception as exc:
                logger.warning(f"Cannot parse {bm.path}: {exc}")
                stats["errors"] += 1
                continue
            stats["parsed"] += 1

        con.execute(
            """INSERT OR REPLACE INTO messages
               (account, gmail_id, path, size, internal_date, message_id,
                sender, sender_addr, subject, body_hash, forwarded_from,
                content_hash, header_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                account,
                bm.gmail_id,
                rel,
                size,
                bm.internal_date,
                fields["message_id"],
                fields["sender"],
                fields["sender_addr"],
                fields["subject"],
                fields["body_hash"],
                fields["forwarded_from"],
                fields["content_hash"],
                fields["header_key"],
            ),
        )
        if (stats["parsed"] + stats["from_meta"]) % 1000 == 0:
            con.commit()

    gone = set(known) - seen
    con.executemany(
        "DELETE FROM messages WHERE account = ? AND gmail_id = ?", ((account, gid) for gid in gone)
    )
    stats["removed"] = len(gone)

    con.execute("INSERT OR REPLACE INTO backups VALUES (?, ?)", (account, str(backup_dir)))
    con.execute("DELETE FROM labels WHERE account = ?", (account,))
    con.executemany("INSERT INTO labels VALUES (?, ?, ?)", label_rows)
    con.commit()
    return stats
