"""
gyb: Got-Your-Back's on-disk format (db_version 6), written and read here,
so folders made by gyb and by mailcellar are interchangeable.

The contract, from gyb 1.97 (initializeDB, backup_message, refresh_message):

  <folder>/msg-db.sqlite
      settings(name, value)    email_address, db_version = '6'
      messages(message_num, message_filename, message_internaldate)
      uids(message_num, uid)   uid = Gmail message ID
      labels(message_num, label)
  <folder>/<year>/<month>/<day>/<gmail_id>.eml
      date from internalDate in UTC, no zero padding; file = decoded `raw`

message_internaldate is local time, as str(datetime.fromtimestamp(ms/1000)),
so it carries microseconds only when they are non-zero. Chat messages
(CHAT/CHATS labels) are not backed up.

Beyond what gyb does, each leaving a valid gyb folder:
  add_copy()  indexes a restored message under its new Gmail ID, sharing
              the old ID's .eml file
  prune()     moves a message's file out of the backup and deletes its
              rows (backup --prune, offload forget); gyb itself never
              removes anything
  unprune()   puts back what prune() moved out (offload forget --undo)
"""

import base64
import csv
import logging
import os
import shutil
import sqlite3
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DB_NAME = "msg-db.sqlite"
DB_VERSION = "6"
CHAT_LABELS = {"CHAT", "CHATS"}
CHAT_LABEL = "CHAT"  # the label ID Gmail lists chats under

_SCHEMA = [
    "CREATE TABLE settings (name TEXT PRIMARY KEY, value TEXT);",
    """CREATE TABLE messages(message_num INTEGER PRIMARY KEY,
                         message_filename TEXT,
                         message_internaldate TIMESTAMP);""",
    "CREATE TABLE labels (message_num INTEGER, label TEXT);",
    "CREATE TABLE uids (message_num INTEGER, uid TEXT PRIMARY KEY);",
    "CREATE UNIQUE INDEX labelidx ON labels (message_num, label);",
]


class FormatError(Exception):
    pass


def open_backup(folder: Path, account: str) -> sqlite3.Connection:
    """Open the backup's msg-db.sqlite, creating folder and database if new."""
    folder.mkdir(parents=True, exist_ok=True)
    db_path = folder / DB_NAME
    new = not db_path.exists()
    con = sqlite3.connect(db_path)
    try:
        if new:
            for statement in _SCHEMA:
                con.execute(statement)
            con.executemany(
                "INSERT INTO settings (name, value) VALUES (?, ?)",
                [("email_address", account), ("db_version", DB_VERSION)],
            )
            con.commit()
            return con

        settings = dict(con.execute("SELECT name, value FROM settings"))
        if settings.get("db_version") != DB_VERSION:
            raise FormatError(
                f"{db_path}: db_version {settings.get('db_version')!r}, expected {DB_VERSION}"
            )
        if settings.get("email_address", "").lower() != account.lower():
            raise FormatError(
                f"{db_path} belongs to {settings.get('email_address')}, not {account}"
            )
        return con
    except BaseException:
        con.close()  # a failed open leaves no connection behind
        raise


def backed_up_ids(con: sqlite3.Connection, folder: Path) -> set[str]:
    """Gmail IDs gyb would treat as backed up: indexed and the file exists."""
    return {
        uid
        for uid, fname in con.execute(
            "SELECT uid, message_filename FROM uids NATURAL JOIN messages"
        )
        if (folder / fname).is_file()
    }


def message_files(folder: Path, gmail_ids) -> dict[str, Path]:
    """
    The backed-up .eml of each ID the index has, read-only, where the file is
    there; an empty answer for a folder with no backup.
    """
    path = folder / DB_NAME
    if not path.is_file():
        return {}
    wanted = set(gmail_ids)
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT uid, message_filename FROM uids NATURAL JOIN messages")
        found = {uid: inside(folder, name) for uid, name in rows if uid in wanted}
    finally:
        con.close()
    return {uid: p for uid, p in found.items() if p is not None and p.is_file()}


def labels_of(folder: Path, gmail_ids) -> dict[str, list[str]]:
    """Each ID's labels in the backup's index, read-only; empty for no backup."""
    path = folder / DB_NAME
    if not path.is_file():
        return {}
    wanted = set(gmail_ids)
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        out: dict[str, list[str]] = {}
        for uid, label in con.execute(
            "SELECT uid, label FROM uids NATURAL JOIN labels ORDER BY uid, label"
        ):
            if uid in wanted:
                out.setdefault(uid, []).append(label)
        return out
    finally:
        con.close()


def is_chat(label_ids) -> bool:
    return bool(CHAT_LABELS & set(label_ids))


def inside(folder: Path, rel: str) -> Path | None:
    """
    folder / rel, the file an index entry names, or None if that leads out of
    the folder (an absolute path, or one through ..): a gyb folder from
    elsewhere, read by restore --from-folder, could otherwise name any file
    on the machine.
    """
    path = folder / rel
    return path if path.resolve().is_relative_to(folder.resolve()) else None


def relative_path(gmail_id: str, internal_date_ms: int) -> str:
    t = time.gmtime(internal_date_ms / 1000)
    return f"{t.tm_year}/{t.tm_mon}/{t.tm_mday}/{gmail_id}.eml"


def sqlite_internaldate(internal_date_ms: int) -> str:
    try:
        dt = datetime.fromtimestamp(internal_date_ms / 1000)
    except OSError, OverflowError, ValueError:
        dt = datetime.fromtimestamp(86400)  # gyb's fallback
    return str(dt)


def _write_atomic(path: Path, data: bytes) -> None:
    """Write via a temp file and rename, so a crash never leaves a partial .eml."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Not *.eml, so a leftover after a hard crash is never mistaken for mail.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_message(con: sqlite3.Connection, folder: Path, response: dict, labels: list[str]) -> str:
    """
    Store one messages.get(format=raw) response with its gyb-style label
    names. Returns the relative path. The caller commits.
    """
    gmail_id = response["id"]
    ms = int(response["internalDate"])
    rel = relative_path(gmail_id, ms)
    _write_atomic(folder / rel, base64.urlsafe_b64decode(response["raw"]))

    cur = con.execute(
        "INSERT INTO messages (message_filename, message_internaldate) VALUES (?, ?)",
        (rel, sqlite_internaldate(ms)),
    )
    message_num = cur.lastrowid
    con.execute("REPLACE INTO uids (message_num, uid) VALUES (?, ?)", (message_num, gmail_id))
    con.executemany(
        "INSERT OR IGNORE INTO labels (message_num, label) VALUES (?, ?)",
        ((message_num, label) for label in labels),
    )
    return rel


def add_copy(
    con: sqlite3.Connection,
    folder: Path,
    gmail_id: str,
    internal_date_ms: int,
    labels: list[str],
    source: Path,
) -> str:
    """
    Index a message already in the backup under another ID (a restore gave
    it a new one): its file is hard-linked (copied across volumes) to the
    new ID's path. Returns the relative path. The caller commits.
    """
    rel = relative_path(gmail_id, internal_date_ms)
    target = folder / rel
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source, target)
        except OSError:
            shutil.copy2(source, target)
    if con.execute("SELECT 1 FROM uids WHERE uid = ?", (gmail_id,)).fetchone() is None:
        cur = con.execute(
            "INSERT INTO messages (message_filename, message_internaldate) VALUES (?, ?)",
            (rel, sqlite_internaldate(internal_date_ms)),
        )
        con.execute("REPLACE INTO uids (message_num, uid) VALUES (?, ?)", (cur.lastrowid, gmail_id))
        con.executemany(
            "INSERT OR IGNORE INTO labels (message_num, label) VALUES (?, ?)",
            ((cur.lastrowid, label) for label in labels),
        )
    return rel


PRUNE_MANIFEST = "pruned.csv"


def earlier_copy(dest: Path, rel: str) -> Path | None:
    """
    The file for `rel` in another run's folder beside `dest`, if there is
    one: an earlier prune or forget moved it out and was stopped (a crash,
    a power cut) before its index rows were committed. The oldest, if more.
    """
    if not dest.parent.is_dir():
        return None
    for run in sorted(p for p in dest.parent.iterdir() if p.is_dir() and p != dest):
        if (run / rel).is_file():
            return run / rel
    return None


def prune(
    con: sqlite3.Connection,
    folder: Path,
    gmail_ids,
    dest: Path,
    manifest_name: str = PRUNE_MANIFEST,
) -> list[dict]:
    """
    Remove messages from the backup: move each .eml to the same relative
    path under `dest`, then delete its messages / uids / labels rows. Index
    entries whose file is already missing just lose their rows. Commits once,
    at the end, then writes dest/<manifest_name>: stopped part-way (Ctrl-C, a
    crash), the index is as it was, so it still agrees with meta.sqlite, and
    the files already moved are picked up by the next run, which links each
    into its own folder (earlier_copy) so its manifest lists it as moved and
    `restore` finds it. Returns the manifest rows.
    """
    manifest = []
    for gmail_id in gmail_ids:
        row = con.execute(
            """SELECT message_num, message_filename, message_internaldate
               FROM uids NATURAL JOIN messages WHERE uid = ?""",
            (gmail_id,),
        ).fetchone()
        if row is None:
            continue
        num, rel, internal_date = row
        labels = sorted(
            label
            for (label,) in con.execute("SELECT label FROM labels WHERE message_num = ?", (num,))
        )

        src, target = inside(folder, rel), inside(dest, rel)
        moved = src is not None and target is not None and src.is_file()
        if moved:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(src, target)
        elif target is not None and (earlier := earlier_copy(dest, rel)) is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(earlier, target)
            except OSError:
                shutil.copy2(earlier, target)
            moved = True
        con.execute("DELETE FROM labels WHERE message_num = ?", (num,))
        con.execute("DELETE FROM uids WHERE message_num = ?", (num,))
        con.execute("DELETE FROM messages WHERE message_num = ?", (num,))
        manifest.append(
            {
                "gmail_id": gmail_id,
                "path": rel,
                "internal_date": internal_date,
                "labels": ";".join(labels),
                "file": "moved" if moved else "was missing",
            }
        )
    con.commit()

    if manifest:
        dest.mkdir(parents=True, exist_ok=True)
        with (dest / manifest_name).open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(manifest[0]))
            w.writeheader()
            w.writerows(manifest)
    return manifest


def unprune(con: sqlite3.Connection, folder: Path, src: Path, manifest_name: str) -> list[str]:
    """
    Put back what prune() moved into `src`: each file to its path in the
    backup, and its messages / uids / labels rows from the manifest. A
    message the backup has again meanwhile is left as it is. Commits.
    Returns the Gmail IDs put back.
    """
    with (src / manifest_name).open(newline="", encoding="utf-8") as fh:
        manifest = list(csv.DictReader(fh))
    back = []
    for row in manifest:
        gmail_id, rel = row["gmail_id"], row["path"]
        if con.execute("SELECT 1 FROM uids WHERE uid = ?", (gmail_id,)).fetchone():
            continue
        moved = src / rel
        if moved.is_file():
            target = folder / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(moved, target)
        cur = con.execute(
            "INSERT INTO messages (message_filename, message_internaldate) VALUES (?, ?)",
            (rel, row["internal_date"]),
        )
        con.execute("REPLACE INTO uids (message_num, uid) VALUES (?, ?)", (cur.lastrowid, gmail_id))
        con.executemany(
            "INSERT OR IGNORE INTO labels (message_num, label) VALUES (?, ?)",
            ((cur.lastrowid, label) for label in filter(None, row["labels"].split(";"))),
        )
        back.append(gmail_id)
    con.commit()
    return back


def replace_labels(con: sqlite3.Connection, gmail_id: str, labels: list[str]) -> bool:
    """
    Make the stored labels of `gmail_id` equal `labels` (gyb's refresh).
    Returns True if anything changed. The caller commits.
    """
    row = con.execute("SELECT message_num FROM uids WHERE uid = ?", (gmail_id,)).fetchone()
    if row is None:
        return False
    (num,) = row
    current = {
        label for (label,) in con.execute("SELECT label FROM labels WHERE message_num = ?", (num,))
    }
    wanted = set(labels)
    if current == wanted:
        return False
    con.executemany(
        "DELETE FROM labels WHERE message_num = ? AND label = ?",
        ((num, label) for label in current - wanted),
    )
    con.executemany(
        "INSERT INTO labels (message_num, label) VALUES (?, ?)",
        ((num, label) for label in wanted - current),
    )
    return True


# ── backup folders ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BackupMessage:
    gmail_id: str
    path: Path
    internal_date: str
    labels: list[str] = field(default_factory=list)


def backup_account(backup_dir: Path) -> str:
    """The account email recorded in the backup's msg-db.sqlite."""
    con = sqlite3.connect(f"file:{backup_dir / 'msg-db.sqlite'}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT value FROM settings WHERE name = 'email_address'").fetchone()
    finally:
        con.close()
    if not row:
        raise ValueError(f"{backup_dir}: msg-db.sqlite has no email_address setting")
    return row[0].lower()


def iter_backup(backup_dir: Path):
    """
    Yield a BackupMessage for every message in the backup's index whose
    .eml file exists. Opens msg-db.sqlite read-only; gyb owns that file.
    """
    con = sqlite3.connect(f"file:{backup_dir / 'msg-db.sqlite'}?mode=ro", uri=True)
    try:
        labels = defaultdict(list)
        for num, label in con.execute("SELECT message_num, label FROM labels"):
            labels[num].append(label)
        rows = con.execute(
            """SELECT m.message_num, u.uid, m.message_filename, m.message_internaldate
               FROM messages m JOIN uids u USING (message_num)
               ORDER BY m.message_num"""
        ).fetchall()
    finally:
        con.close()

    for num, uid, fname, internal_date in rows:
        path = inside(backup_dir, fname)
        if path is None:
            logger.warning(f"{backup_dir}: {uid}'s file {fname!r} is outside the folder; skipped")
        elif path.exists():
            yield BackupMessage(uid, path, internal_date or "", sorted(labels[num]))
