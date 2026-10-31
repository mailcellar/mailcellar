"""
sidecar: the per-account database <data root>/<email>/meta.sqlite, next to
gyb's msg-db.sqlite. It holds what gyb's format has no room for, so each
message is parsed once:

  messages        fields from message.parse_message(), plus the Gmail
                  thread ID, UTC internal date and size estimate
  message_labels  Gmail label IDs (msg-db.sqlite stores names, which change
                  when a label is renamed)
  attachments     every non-body part: file name, type, size, hash
  recipients      To / Cc / Bcc addresses and names
  labels          the account's label ID → name catalogue
  runs            one row per backup, count and prune run: mode, counts,
                  outcome (for the health report)
  offloaded       messages removed from Gmail on purpose and kept here;
                  forget --deleted never takes them
  restored        messages put back into Gmail by restore (into this
                  account or another), and the ID each has there now
  meta            schema_version, and history_id: where the next
                  history-based backup starts

backup writes it on download and label refresh, and `meta` fills
it for older mail; offload apply and restore record what they did. dedup
index and the reports read it instead of parsing .eml files. gyb ignores
the file; msg-db.sqlite is never touched here.
"""

import json
import logging
import os
import shutil
import sqlite3
from collections.abc import Mapping
from pathlib import Path

from mailcellar.message import PARSER_VERSION

logger = logging.getLogger(__name__)

META_NAME = "meta.sqlite"
SCHEMA_VERSION = "13"


def _add_run_column(name: str):
    """A migration step adding an INTEGER column to runs, unless it exists."""

    def step(con: sqlite3.Connection) -> None:
        if name not in {row[1] for row in con.execute("PRAGMA table_info(runs)")}:
            con.execute(f"ALTER TABLE runs ADD COLUMN {name} INTEGER")

    return step


def _rename_purged(con: sqlite3.Connection) -> None:
    """
    v7: the purge record became the offload record. _SCHEMA has already
    created an empty offloaded table, so v6's purged rows are copied into
    it and purged is dropped; runs.purged is renamed. Safe to repeat.
    """
    tables = {
        name for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if "purged" in tables:
        cols = "gmail_id, batch, rule, trashed_ms, gone_ms"
        con.execute(f"INSERT OR IGNORE INTO offloaded ({cols}) SELECT {cols} FROM purged")
        con.execute("DROP TABLE purged")
    run_cols = {row[1] for row in con.execute("PRAGMA table_info(runs)")}
    if "purged" in run_cols:
        if "offloaded" in run_cols:  # added by v6's step on a sidecar that came from v5
            con.execute("UPDATE runs SET offloaded = coalesce(offloaded, purged)")
            con.execute("ALTER TABLE runs DROP COLUMN purged")
        else:
            con.execute("ALTER TABLE runs RENAME COLUMN purged TO offloaded")


def _add_offloaded_restored(con: sqlite3.Connection) -> None:
    """v8: offloaded.restored_ms (the restored table is created by _SCHEMA)."""
    if "restored_ms" not in {row[1] for row in con.execute("PRAGMA table_info(offloaded)")}:
        con.execute("ALTER TABLE offloaded ADD COLUMN restored_ms INTEGER")


def _add_restored_target(con: sqlite3.Connection) -> None:
    """v9: restored rows name the account restored into ('' = the backup's own)."""
    if "target" in {row[1] for row in con.execute("PRAGMA table_info(restored)")}:
        return
    con.execute("ALTER TABLE restored RENAME TO restored_v8")
    con.executescript(_SCHEMA)  # the new restored table
    con.execute(
        """INSERT INTO restored (source_id, target, new_id, how, restored_ms, run)
           SELECT source_id, '', new_id, how, restored_ms, run FROM restored_v8"""
    )
    con.execute("DROP TABLE restored_v8")


def _add_columns(*names: str):
    """A migration step adding TEXT columns to messages, skipping existing ones."""

    def step(con: sqlite3.Connection) -> None:
        have = {row[1] for row in con.execute("PRAGMA table_info(messages)")}
        for name in names:
            if name not in have:
                con.execute(f"ALTER TABLE messages ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")

    return step


# Steps that upgrade schema version N to N+1, applied in order by open_meta.
# New columns start empty; their rows are stale by parser_version, so
# `mailcellar meta` re-parses them.
MIGRATIONS = {
    "1": [_add_columns("content_hash", "header_key")],
    "2": [_add_columns("sender_name", "reply_to")],
    # v4 also adds the attachments / recipients tables (created by _SCHEMA).
    "3": [_add_columns("list_id", "list_unsubscribe", "precedence", "in_reply_to")],
    "4": [],  # v5 adds the runs table (created by _SCHEMA); runs before it are not known
    # v6 adds the purge record and runs.purged, named offloaded since v7
    # (_SCHEMA creates the table; this adds the column under its v7 name).
    "5": [_add_run_column("offloaded")],
    "6": [_rename_purged],
    "7": [_add_offloaded_restored],
    "8": [_add_restored_target],
    "9": [],  # v10 adds the forgotten table (created by _SCHEMA)
    "10": [],  # v11 adds the holds table (created by _SCHEMA)
    "11": [],  # v12 adds the imap_map table (created by _SCHEMA)
    "12": [],  # v13 adds the imap_review table (created by _SCHEMA)
}

FIELDS = (
    "message_id",
    "sender",
    "sender_addr",
    "subject",
    "body_hash",
    "forwarded_from",
    "content_hash",
    "header_key",
    "sender_name",
    "reply_to",
    "list_id",
    "list_unsubscribe",
    "precedence",
    "in_reply_to",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS messages (
    gmail_id       TEXT PRIMARY KEY,
    thread_id      TEXT NOT NULL,         -- '' if unknown (gone from Gmail)
    internal_date  INTEGER NOT NULL,      -- ms since epoch, UTC
    size_estimate  INTEGER,
    message_id     TEXT NOT NULL,
    sender         TEXT NOT NULL,
    sender_addr    TEXT NOT NULL,
    subject        TEXT NOT NULL,
    body_hash      TEXT NOT NULL,
    forwarded_from TEXT NOT NULL,
    content_hash   TEXT NOT NULL DEFAULT '',  -- visible text + attachments
    header_key     TEXT NOT NULL DEFAULT '',  -- From, Date, Subject
    sender_name    TEXT NOT NULL DEFAULT '',  -- decoded From display name
    reply_to       TEXT NOT NULL DEFAULT '',  -- Reply-To address
    list_id        TEXT NOT NULL DEFAULT '',  -- List-Id identifier
    list_unsubscribe TEXT NOT NULL DEFAULT '',  -- 'mailto' | 'https' | 'both' | ''
    precedence     TEXT NOT NULL DEFAULT '',  -- e.g. 'bulk', 'list'
    in_reply_to    TEXT NOT NULL DEFAULT '',  -- normalised In-Reply-To Message-ID
    parser_version TEXT NOT NULL
);

-- Child rows of messages, replaced whenever a message is (re)parsed.
CREATE TABLE IF NOT EXISTS attachments (
    gmail_id  TEXT NOT NULL,
    idx       INTEGER NOT NULL,           -- order among the message's non-body parts
    filename  TEXT NOT NULL,              -- decoded; '' if none
    mime_type TEXT NOT NULL,
    size      INTEGER NOT NULL,           -- decoded bytes
    sha256    TEXT NOT NULL,
    inline    INTEGER NOT NULL,           -- 1 unless Content-Disposition: attachment
    PRIMARY KEY (gmail_id, idx)
);
CREATE INDEX IF NOT EXISTS attachments_sha256 ON attachments (sha256);

CREATE TABLE IF NOT EXISTS recipients (
    gmail_id TEXT NOT NULL,
    kind     TEXT NOT NULL,               -- 'to' | 'cc' | 'bcc'
    address  TEXT NOT NULL,
    name     TEXT NOT NULL,
    PRIMARY KEY (gmail_id, kind, address)
);
CREATE INDEX IF NOT EXISTS recipients_address ON recipients (address);

CREATE TABLE IF NOT EXISTS message_labels (
    gmail_id TEXT NOT NULL,
    label_id TEXT NOT NULL,
    PRIMARY KEY (gmail_id, label_id)
);

CREATE TABLE IF NOT EXISTS labels (
    label_id TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    type     TEXT NOT NULL                -- 'system' | 'user'
);

-- One row per mailcellar backup run; counts are NULL where they don't apply.
CREATE TABLE IF NOT EXISTS runs (
    run_id           INTEGER PRIMARY KEY,
    started_ms       INTEGER NOT NULL,    -- ms since epoch, UTC
    finished_ms      INTEGER NOT NULL,
    command          TEXT NOT NULL,       -- 'backup' | 'count' | 'verify' (backup's check) |
                                          -- 'sync' (= --prune, before 0.19.0)
    mode             TEXT NOT NULL DEFAULT '',  -- backup: 'history' | 'full'
    reason           TEXT NOT NULL DEFAULT '',  -- why full: 'no-history' | 'expired' |
                                                -- 'full' | 'search' | 'refresh-labels';
                                                -- 'needs-consent': an unattended run
                                                -- that could not sign in;
                                                -- 'access-refused': its service
                                                -- account was refused
    status           TEXT NOT NULL,       -- 'ok' | 'failures' | 'missing' | 'error' |
                                          -- 'aborted' | 'declined'
    listed           INTEGER,             -- backup (full): messages listed
    downloaded       INTEGER,
    failed           INTEGER,             -- backup: downloads that failed
    labels_changed   INTEGER,
    skipped          INTEGER,             -- backup: in Spam/Trash/Chat
    deleted_in_gmail INTEGER,             -- backup: backed-up messages deleted in Gmail
    pruned           INTEGER,             -- prune: messages moved out of the backup
    in_gmail         INTEGER,             -- count
    missing          INTEGER,             -- count: in Gmail, not backed up
    only_in_backup   INTEGER,             -- count: excluding offloaded messages
    messages         INTEGER,             -- messages in the backup afterwards
    offloaded        INTEGER              -- count, prune: offloaded messages in the backup
);

-- Messages removed from Gmail on purpose (offload apply), kept in the backup.
CREATE TABLE IF NOT EXISTS offloaded (
    gmail_id   TEXT PRIMARY KEY,
    batch      TEXT NOT NULL,             -- the offload run that removed it
    rule       TEXT NOT NULL,             -- the rule that selected it
    trashed_ms INTEGER NOT NULL,          -- moved to Gmail's Trash, ms since epoch, UTC
    gone_ms    INTEGER,                   -- first seen permanently deleted from Gmail
    restored_ms INTEGER                   -- put back into Gmail (see restored); NULL if not
);

-- Offloaded messages deleted from the backup on purpose (forget):
-- what is left of them, with no sender, subject or content.
CREATE TABLE IF NOT EXISTS forgotten (
    gmail_id      TEXT PRIMARY KEY,
    batch         TEXT NOT NULL,          -- the offload batch it came from
    rule          TEXT NOT NULL,
    internal_date INTEGER,                -- ms since epoch, UTC; NULL if unknown
    size_estimate INTEGER,
    run           TEXT NOT NULL,          -- the forget run: <data root>/_forgotten/<email>/<run>/
    forgotten_ms  INTEGER NOT NULL,       -- moved out of the backup, ms since epoch, UTC
    emptied_ms    INTEGER                 -- its file deleted for good; NULL while held
);

-- Legal holds (mailcellar hold): what nothing may remove from Gmail or the backup.
-- A hold follows its criteria, so mail that matches later is held too.
CREATE TABLE IF NOT EXISTS holds (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,            -- unique among holds not released
    reason      TEXT NOT NULL,
    criteria    TEXT NOT NULL,            -- JSON: parties, after_ms, before_ms, label, ids
    created_ms  INTEGER NOT NULL,         -- ms since epoch, UTC
    until_ms    INTEGER,                  -- ends by itself then; NULL: until released
    released_ms INTEGER                   -- NULL while in force
);

-- Backed-up messages put back into Gmail by mailcellar restore.
CREATE TABLE IF NOT EXISTS restored (
    source_id   TEXT NOT NULL,            -- the message's Gmail ID in this backup
    target      TEXT NOT NULL DEFAULT '', -- the account restored into; '' = this one
    new_id      TEXT NOT NULL,            -- its ID there now; = source_id if untrashed
    how         TEXT NOT NULL,            -- 'untrashed' | 'in gmail' | 'found' | 'imported'
                                          -- | 'moved into'
    restored_ms INTEGER NOT NULL,         -- ms since epoch, UTC
    run         TEXT NOT NULL,            -- the restore run
    PRIMARY KEY (source_id, target)
);

-- An IMAP account's backup only: where each backed-up message is on the
-- server, so an unchanged folder is listed without fetching anything.
-- A folder's rows are replaced whenever its UIDVALIDITY changes.
CREATE TABLE IF NOT EXISTS imap_map (
    folder      TEXT NOT NULL,            -- the server's folder name, as LIST gives it
    uidvalidity INTEGER NOT NULL,
    uid         INTEGER NOT NULL,
    msg_id      TEXT NOT NULL,            -- the message's ID in this backup
    PRIMARY KEY (folder, uidvalidity, uid)
);
CREATE INDEX IF NOT EXISTS imap_map_msg_id ON imap_map (msg_id);

-- An IMAP account reviewed by folder only: the folders a message was in
-- when offload or dedup moved it into a review folder (mailcellar/review/
-- <label>), so its labels stay the same while it's reviewed, and restore
-- can put it back.
CREATE TABLE IF NOT EXISTS imap_review (
    msg_id    TEXT NOT NULL,
    label     TEXT NOT NULL,                -- the review label (offload-candidate)
    origins   TEXT NOT NULL,                -- JSON [[folder name, label], ...]
    marked_at INTEGER NOT NULL,             -- ms since epoch
    PRIMARY KEY (msg_id, label)
);
"""

RUN_COUNTS = (
    "listed",
    "downloaded",
    "failed",
    "labels_changed",
    "skipped",
    "deleted_in_gmail",
    "pruned",
    "in_gmail",
    "missing",
    "only_in_backup",
    "messages",
    "offloaded",
)


class MetaError(Exception):
    pass


def meta_path(folder: Path) -> Path:
    return folder / META_NAME


def copy_before_migrating(path: Path) -> Path | None:
    """
    Copy an existing meta.sqlite that is about to be migrated to
    <name>.pre-v<SCHEMA_VERSION> next to it, before anything touches it, so
    the version before survives a migration that goes wrong (and an older
    mailcellar can still read it). A copy already there is kept: it is the
    first, true pre-migration state. Returns the copy, or None.
    """
    if not path.is_file() or path.stat().st_size == 0:
        return None
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        try:
            row = src.execute("SELECT value FROM meta WHERE name = 'schema_version'").fetchone()
        except sqlite3.Error:
            return None  # not a sidecar mailcellar knows how to migrate
        if row is None or row[0] == SCHEMA_VERSION or row[0] not in MIGRATIONS:
            return None
        dest = path.with_name(f"{path.name}.pre-v{SCHEMA_VERSION}")
        if dest.exists():
            return None
        part = dest.with_name(f".{dest.name}.part")
        out = sqlite3.connect(part)
        try:
            src.backup(out)
        finally:
            out.close()
        shutil.copymode(path, part)
        os.replace(part, dest)
    finally:
        src.close()
    logger.info(f"Copied {path.name} (schema {row[0]}) to {dest} before migrating it")
    return dest


def open_meta(folder: Path) -> sqlite3.Connection:
    """
    Open (creating if needed) the backup folder's meta.sqlite for writing,
    migrating an older schema after copying it aside (copy_before_migrating).
    """
    folder.mkdir(parents=True, exist_ok=True)
    copy_before_migrating(meta_path(folder))
    con = sqlite3.connect(meta_path(folder))
    try:
        con.executescript(_SCHEMA)
        row = con.execute("SELECT value FROM meta WHERE name = 'schema_version'").fetchone()
        if row is None:
            con.execute("INSERT INTO meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
            con.commit()
            return con
        version = row[0]
        while version != SCHEMA_VERSION and version in MIGRATIONS:
            for step in MIGRATIONS[version]:
                if callable(step):
                    step(con)
                else:
                    con.execute(step)
            version = str(int(version) + 1)
            con.execute("UPDATE meta SET value = ? WHERE name = 'schema_version'", (version,))
            con.commit()
        if version != SCHEMA_VERSION:
            raise MetaError(f"{meta_path(folder)} has schema {version}, expected {SCHEMA_VERSION}")
        return con
    except BaseException:
        con.close()  # a failed open leaves no connection behind
        raise


def current_rows(folder: Path) -> dict[str, tuple[dict, int]]:
    """
    gmail_id → (parsed fields, internal_date in ms UTC), for messages parsed
    with the current parser. {} when the folder has no meta.sqlite or an
    older schema. Opens it read-only.
    """
    path = meta_path(folder)
    if not path.is_file():
        return {}
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        # An older schema (not yet migrated by mailcellar backup) can't hold
        # current fields; the caller then parses the .eml files instead.
        row = con.execute("SELECT value FROM meta WHERE name = 'schema_version'").fetchone()
        if row is None or row[0] != SCHEMA_VERSION:
            return {}
        cols = ", ".join(FIELDS)
        return {
            row[0]: (dict(zip(FIELDS, row[2:], strict=True)), row[1])
            for row in con.execute(
                f"SELECT gmail_id, internal_date, {cols} FROM messages WHERE parser_version = ?",
                (PARSER_VERSION,),
            )
        }
    finally:
        con.close()


def current_fields(folder: Path) -> dict[str, dict]:
    """gmail_id → parsed fields, for messages parsed with the current parser."""
    return {gid: fields for gid, (fields, _date) in current_rows(folder).items()}


def upsert_message(
    con: sqlite3.Connection,
    gmail_id: str,
    *,
    thread_id: str,
    internal_date: int,
    size_estimate: int | None,
    fields: dict,
    label_ids=None,
) -> None:
    """Store one message. label_ids=None leaves its stored labels alone. No commit."""
    con.execute(
        f"""INSERT OR REPLACE INTO messages
            (gmail_id, thread_id, internal_date, size_estimate, {", ".join(FIELDS)},
             parser_version)
            VALUES (?, ?, ?, ?, {", ".join("?" * len(FIELDS))}, ?)""",
        (
            gmail_id,
            thread_id or "",
            internal_date,
            size_estimate,
            *(fields[f] for f in FIELDS),
            PARSER_VERSION,
        ),
    )
    _replace_children(con, gmail_id, fields)
    if label_ids is not None:
        set_message_labels(con, gmail_id, label_ids)


def update_fields(con: sqlite3.Connection, gmail_id: str, fields: dict) -> None:
    """Replace only the parsed fields (after a parser change). No commit."""
    con.execute(
        f"""UPDATE messages SET {", ".join(f"{f} = ?" for f in FIELDS)},
            parser_version = ? WHERE gmail_id = ?""",
        (*(fields[f] for f in FIELDS), PARSER_VERSION, gmail_id),
    )
    _replace_children(con, gmail_id, fields)


def _replace_children(con: sqlite3.Connection, gmail_id: str, fields: dict) -> None:
    """Store a parse's attachments / recipients lists, when it has them."""
    if "attachments" in fields:
        con.execute("DELETE FROM attachments WHERE gmail_id = ?", (gmail_id,))
        con.executemany(
            "INSERT INTO attachments VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    gmail_id,
                    a["idx"],
                    a["filename"],
                    a["mime_type"],
                    a["size"],
                    a["sha256"],
                    int(a["inline"]),
                )
                for a in fields["attachments"]
            ),
        )
    if "recipients" in fields:
        con.execute("DELETE FROM recipients WHERE gmail_id = ?", (gmail_id,))
        con.executemany(
            "INSERT INTO recipients VALUES (?, ?, ?, ?)",
            ((gmail_id, r["kind"], r["address"], r["name"]) for r in fields["recipients"]),
        )


def update_gmail_details(
    con: sqlite3.Connection,
    gmail_id: str,
    *,
    thread_id: str,
    internal_date: int,
    size_estimate: int | None,
) -> None:
    """Fill in what only the Gmail API knows. No commit."""
    con.execute(
        """UPDATE messages SET thread_id = ?, internal_date = ?, size_estimate = ?
                   WHERE gmail_id = ?""",
        (thread_id or "", internal_date, size_estimate, gmail_id),
    )


def set_message_labels(con: sqlite3.Connection, gmail_id: str, label_ids) -> bool:
    """Make the stored label IDs equal label_ids. Returns True if they changed."""
    current = {
        label
        for (label,) in con.execute(
            "SELECT label_id FROM message_labels WHERE gmail_id = ?", (gmail_id,)
        )
    }
    wanted = set(label_ids)
    if current == wanted:
        return False
    con.executemany(
        "DELETE FROM message_labels WHERE gmail_id = ? AND label_id = ?",
        ((gmail_id, label) for label in current - wanted),
    )
    con.executemany(
        "INSERT INTO message_labels VALUES (?, ?)",
        ((gmail_id, label) for label in wanted - current),
    )
    return True


def replace_label_catalogue(con: sqlite3.Connection, labels: list[dict]) -> None:
    """Store labels.list output ({id, name, type} each). No commit."""
    con.execute("DELETE FROM labels")
    con.executemany(
        "INSERT INTO labels VALUES (?, ?, ?)",
        ((label["id"], label["name"], label["type"]) for label in labels),
    )


def delete_messages(con: sqlite3.Connection, gmail_ids) -> None:
    """Delete messages' rows: moved out of the backup by forget. No commit."""
    ids = list(gmail_ids)
    for table in ("message_labels", "attachments", "recipients"):
        con.executemany(f"DELETE FROM {table} WHERE gmail_id = ?", ((g,) for g in ids))
    con.executemany("DELETE FROM messages WHERE gmail_id = ?", ((g,) for g in ids))


def record_run(
    con: sqlite3.Connection,
    command: str,
    started_ms: int,
    finished_ms: int,
    status: str,
    mode: str = "",
    reason: str = "",
    **counts: int | None,
) -> None:
    """Add a row to runs (keys of counts from RUN_COUNTS) and commit."""
    unknown = set(counts) - set(RUN_COUNTS)
    if unknown:
        raise ValueError(f"unknown run counts: {sorted(unknown)}")
    con.execute(
        f"""INSERT INTO runs (started_ms, finished_ms, command, mode, reason, status,
                              {", ".join(RUN_COUNTS)})
            VALUES (?, ?, ?, ?, ?, ?, {", ".join("?" * len(RUN_COUNTS))})""",
        (
            started_ms,
            finished_ms,
            command,
            mode,
            reason,
            status,
            *(counts.get(c) for c in RUN_COUNTS),
        ),
    )
    con.commit()


def imap_folder_map(con: sqlite3.Connection, folder: str, uidvalidity: int) -> dict[int, str]:
    """An IMAP folder's UID → message ID, for the UIDVALIDITY given."""
    rows = con.execute(
        "SELECT uid, msg_id FROM imap_map WHERE folder = ? AND uidvalidity = ?",
        (folder, uidvalidity),
    )
    return dict(rows)


def imap_ids_by_folder(con: sqlite3.Connection) -> dict[str, frozenset[str]]:
    """Each mapped IMAP folder's message IDs."""
    out: dict[str, set[str]] = {}
    for folder, msg_id in con.execute("SELECT folder, msg_id FROM imap_map"):
        out.setdefault(folder, set()).add(msg_id)
    return {folder: frozenset(ids) for folder, ids in out.items()}


def replace_imap_folder(
    con: sqlite3.Connection, folder: str, uidvalidity: int, uids: dict[int, str]
) -> None:
    """Make `uids` (UID → message ID) the folder's whole map. No commit."""
    con.execute("DELETE FROM imap_map WHERE folder = ?", (folder,))
    con.executemany(
        "INSERT INTO imap_map VALUES (?, ?, ?, ?)",
        ((folder, uidvalidity, uid, msg_id) for uid, msg_id in uids.items()),
    )


def drop_imap_folders(con: sqlite3.Connection, keep) -> None:
    """Forget the map of every folder not in `keep` (gone from the server). No commit."""
    keep = set(keep)
    for (folder,) in con.execute("SELECT DISTINCT folder FROM imap_map").fetchall():
        if folder not in keep:
            con.execute("DELETE FROM imap_map WHERE folder = ?", (folder,))


def record_imap_review(con: sqlite3.Connection, label: str, origins: Mapping, now_ms: int) -> None:
    """Where each message (ID → ((folder, label), ...)) was before review. No commit."""
    con.executemany(
        "INSERT OR REPLACE INTO imap_review VALUES (?, ?, ?, ?)",
        (
            (msg_id, label, json.dumps([list(o) for o in where]), now_ms)
            for msg_id, where in origins.items()
        ),
    )


def read_message_ids(folder: Path) -> dict[str, str]:
    """Every backed-up message's Message-ID header (ID → it), read-only; empty if none."""
    path = meta_path(folder)
    if not path.is_file():
        return {}
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return dict(con.execute("SELECT gmail_id, message_id FROM messages"))
    finally:
        con.close()


def keep_imap_reviews(con: sqlite3.Connection, keep, before_ms: int) -> None:
    """
    Drop the review records not in `keep`, (ID, label) pairs, that were written
    before `before_ms`: one written since (a review that began while the scan
    ran) isn't the scan's to judge. No commit.
    """
    keep = set(keep)
    rows = con.execute(
        "SELECT msg_id, label FROM imap_review WHERE marked_at < ?", (before_ms,)
    ).fetchall()
    con.executemany(
        "DELETE FROM imap_review WHERE msg_id = ? AND label = ?",
        [row for row in rows if tuple(row) not in keep],
    )


def read_imap_reviews(folder: Path) -> dict[str, dict[str, tuple[tuple[str, str], ...]]]:
    """
    Each reviewed message's folders before review, per review label (ID →
    label → ((folder, label), ...)), read-only; empty for an older sidecar.
    """
    path = meta_path(folder)
    if not path.is_file():
        return {}
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = {n for (n,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "imap_review" not in tables:
            return {}
        out: dict[str, dict[str, tuple[tuple[str, str], ...]]] = {}
        for msg_id, label, origins in con.execute(
            "SELECT msg_id, label, origins FROM imap_review ORDER BY marked_at, label"
        ):
            out.setdefault(msg_id, {})[label] = tuple(tuple(o) for o in json.loads(origins))
        return out
    finally:
        con.close()


def provider(folder: Path) -> str:
    """
    'imap' for a backup of an IMAP account, else 'gmail'. Opens meta.sqlite
    read-only; raises MetaError if it can't be read, rather than guess.
    """
    path = meta_path(folder)
    if not path.is_file():
        return "gmail"
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return get_state(con, "provider") or "gmail"
        finally:
            con.close()
    except sqlite3.Error as exc:
        raise MetaError(f"{path} can't be read: {exc}") from None


def read_imap_map(folder: Path) -> dict[str, tuple[int, dict[int, str]]]:
    """An IMAP backup's whole UID map, {folder: (UIDVALIDITY, UID → ID)}, read-only."""
    path = meta_path(folder)
    if not path.is_file():
        return {}
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = {n for (n,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "imap_map" not in tables:  # an older sidecar: nothing mapped yet
            return {}
        out: dict[str, tuple[int, dict[int, str]]] = {}
        for name, uidvalidity, uid, msg_id in con.execute("SELECT * FROM imap_map"):
            out.setdefault(name, (uidvalidity, {}))[1][uid] = msg_id
        return out
    finally:
        con.close()


def mark_labels(con: sqlite3.Connection) -> frozenset[str]:
    """The labels an IMAP backup has seen as mailcellar's marks (keywords, review folders)."""
    return frozenset(json.loads(get_state(con, "imap_marks") or "[]"))


def add_mark_labels(con: sqlite3.Connection, labels) -> None:
    """Remember more mark labels, keeping those seen before. No commit."""
    seen = mark_labels(con) | set(labels)
    set_state(con, "imap_marks", json.dumps(sorted(seen)))


def read_mark_labels(folder: Path) -> frozenset[str]:
    """mark_labels of the backup in `folder`, read-only; none without a sidecar."""
    path = meta_path(folder)
    if not path.is_file():
        return frozenset()
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return mark_labels(con)
    except sqlite3.Error:
        return frozenset()
    finally:
        con.close()


def get_state(con: sqlite3.Connection, name: str) -> str | None:
    """A value from the meta table, e.g. 'history_id'; None if unset."""
    row = con.execute("SELECT value FROM meta WHERE name = ?", (name,)).fetchone()
    return row[0] if row else None


def set_state(con: sqlite3.Connection, name: str, value: str) -> None:
    """Set a value in the meta table. No commit."""
    con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (name, value))


def record_offloaded(
    con: sqlite3.Connection, gmail_ids, batch: str, rule: str, trashed_ms: int
) -> None:
    """Record messages moved to Gmail's Trash by an offload. No commit."""
    con.executemany(
        """INSERT OR REPLACE INTO offloaded (gmail_id, batch, rule, trashed_ms)
           VALUES (?, ?, ?, ?)""",
        ((gid, batch, rule, trashed_ms) for gid in gmail_ids),
    )
    con.executemany(
        "DELETE FROM restored WHERE source_id = ? AND target = ''", ((g,) for g in gmail_ids)
    )


def offloaded_ids(con: sqlite3.Connection) -> set[str]:
    """Gmail IDs of the messages offloaded now: not restored since."""
    return {
        gid for (gid,) in con.execute("SELECT gmail_id FROM offloaded WHERE restored_ms IS NULL")
    }


def kept_ids(con: sqlite3.Connection) -> set[str]:
    """
    Gmail IDs forget --deleted never takes and count doesn't call "only in backup":
    every message ever offloaded, restored or not. A message restored under
    a new ID keeps its old ID in the backup.
    """
    return {gid for (gid,) in con.execute("SELECT gmail_id FROM offloaded")}


def record_restored(
    con: sqlite3.Connection,
    source_id: str,
    new_id: str,
    how: str,
    run: str,
    restored_ms: int,
    target: str = "",
) -> None:
    """
    Record one message as back in Gmail as new_id: in this backup's own
    account, or in `target`. Only the former ends an offload. No commit.
    """
    con.execute(
        """INSERT OR REPLACE INTO restored (source_id, target, new_id, how, restored_ms, run)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (source_id, target, new_id, how, restored_ms, run),
    )
    if not target:
        con.execute(
            "UPDATE offloaded SET restored_ms = ? WHERE gmail_id = ?", (restored_ms, source_id)
        )


def restored_as(con: sqlite3.Connection, target: str = "", hows=()) -> dict[str, str]:
    """
    Source ID → the ID it was restored under, in `target` ('' = this backup's
    own account); only those recorded one of these ways, if `hows` names any.
    """
    rows = con.execute("SELECT source_id, new_id, how FROM restored WHERE target = ?", (target,))
    return {source: new for source, new, how in rows if not hows or how in hows}


def restored_ids(con: sqlite3.Connection, target: str = "") -> set[str]:
    """Source IDs already restored into `target` ('' = this backup's own account)."""
    return {g for (g,) in con.execute("SELECT source_id FROM restored WHERE target = ?", (target,))}


def superseded_ids(folder: Path) -> set[str]:
    """
    Gmail IDs of restored messages whose copy in the backup now has another
    ID, so the old one is left out of dedup and the reports. Read-only;
    empty without a current meta.sqlite.
    """
    path = meta_path(folder)
    if not path.is_file():
        return set()
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT value FROM meta WHERE name = 'schema_version'").fetchone()
        if row is None or row[0] != SCHEMA_VERSION:
            return set()
        return superseded(con)
    finally:
        con.close()


def superseded(con: sqlite3.Connection) -> set[str]:
    """superseded_ids(), from a meta.sqlite already open."""
    return {
        gid
        for (gid,) in con.execute(
            "SELECT source_id FROM restored WHERE target = '' AND new_id != source_id"
        )
    }


def forgotten_ids(con: sqlite3.Connection) -> set[str]:
    """Gmail IDs deleted from the backup by forget, held or emptied."""
    return {gid for (gid,) in con.execute("SELECT gmail_id FROM forgotten")}


def mark_gone(con: sqlite3.Connection, gmail_ids, gone_ms: int) -> None:
    """Note offloaded messages Gmail no longer has (Trash emptied). No commit."""
    con.executemany(
        "UPDATE offloaded SET gone_ms = ? WHERE gmail_id = ? AND gone_ms IS NULL",
        ((gone_ms, gid) for gid in gmail_ids),
    )
