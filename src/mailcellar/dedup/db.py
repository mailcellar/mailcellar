"""
dedup.sqlite: schema, migrations and opening.
"""

import sqlite3
import sys
from collections import Counter
from pathlib import Path

SCHEMA_VERSION = "5"


def _migrate_relative_paths(con: sqlite3.Connection) -> None:
    """
    v2 stored each message's absolute .eml path. v3 stores gyb's relative
    message_filename (<year>/<month>/<day>/<id>.eml) and records each
    account's backup folder once, in backups.
    """
    folders: dict[str, Counter] = {}
    rows = con.execute("SELECT account, gmail_id, path FROM messages").fetchall()
    for account, gmail_id, path in rows:
        p = Path(path)
        if not p.is_absolute():
            continue
        folders.setdefault(account, Counter())[str(Path(*p.parts[:-4]))] += 1
        con.execute(
            "UPDATE messages SET path = ? WHERE account = ? AND gmail_id = ?",
            (Path(*p.parts[-4:]).as_posix(), account, gmail_id),
        )
    for account, counts in folders.items():
        con.execute(
            "INSERT OR REPLACE INTO backups VALUES (?, ?)", (account, counts.most_common(1)[0][0])
        )


def _add_columns(con: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    """Add each column that the table doesn't have yet (so the step can be repeated)."""
    have = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    for name, decl in columns.items():
        if name not in have:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _recreate_match_tables(con: sqlite3.Connection) -> None:
    con.execute("DROP TABLE IF EXISTS match_members")
    con.execute("DROP TABLE IF EXISTS match_groups")
    con.executescript(SCHEMA)


# Steps that upgrade schema version N to N+1, applied in order by open_db.
# A step is an SQL statement or a function taking the connection.
MIGRATIONS = {
    "1": [
        lambda con: _add_columns(con, "messages", {"forwarded_from": "TEXT NOT NULL DEFAULT ''"})
    ],
    "2": [_migrate_relative_paths],
    # Filled by the re-parse index does after the parser version changes.
    "3": [
        lambda con: _add_columns(
            con,
            "messages",
            {"content_hash": "TEXT NOT NULL DEFAULT ''", "header_key": "TEXT NOT NULL DEFAULT ''"},
        )
    ],
    # match_groups changed shape; both match tables are rebuilt on every
    # match, so drop them and let SCHEMA recreate them.
    "4": [_recreate_match_tables],
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS messages (
    account       TEXT NOT NULL,
    gmail_id      TEXT NOT NULL,
    path          TEXT NOT NULL,     -- relative to the account's backup folder
    size          INTEGER NOT NULL,
    internal_date TEXT,
    message_id    TEXT NOT NULL,     -- normalised; '' if missing
    sender        TEXT NOT NULL,     -- decoded From, for display
    sender_addr   TEXT NOT NULL,
    subject       TEXT NOT NULL,
    body_hash     TEXT NOT NULL,     -- '' if no text body
    forwarded_from TEXT NOT NULL DEFAULT '',  -- original recipients from
                                     -- X-Forwarded-For, space-separated
    content_hash  TEXT NOT NULL DEFAULT '',  -- visible text + attachments; '' if empty
    header_key    TEXT NOT NULL DEFAULT '',  -- From, Date, Subject; '' if incomplete
    PRIMARY KEY (account, gmail_id)
);
CREATE INDEX IF NOT EXISTS messages_message_id ON messages (message_id);

-- Each account's backup folder, as last given to index.
CREATE TABLE IF NOT EXISTS backups (
    account TEXT PRIMARY KEY,
    folder  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS labels (
    account  TEXT NOT NULL,
    gmail_id TEXT NOT NULL,
    label    TEXT NOT NULL,
    PRIMARY KEY (account, gmail_id, label)
);

-- Written by match; rebuilt on every run.
CREATE TABLE IF NOT EXISTS match_groups (
    group_id INTEGER PRIMARY KEY,
    scope    TEXT NOT NULL,          -- 'cross' (spans accounts) | 'within'
    method   TEXT NOT NULL,          -- 'message-id' | 'header-key'
    key      TEXT NOT NULL           -- the shared Message-ID or header key
);

CREATE TABLE IF NOT EXISTS match_members (
    group_id INTEGER NOT NULL REFERENCES match_groups (group_id),
    account  TEXT NOT NULL,
    gmail_id TEXT NOT NULL,
    PRIMARY KEY (account, gmail_id)
);
"""


# ── database ─────────────────────────────────────────────────────────────────


def open_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    try:
        con.executescript(SCHEMA)
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
            sys.exit(
                f"error: {db_path} has schema {version}, expected {SCHEMA_VERSION}. "
                "Delete it and rerun index."
            )
        return con
    except BaseException:
        con.close()  # a failed open leaves no connection behind
        raise


def remove_messages(con: sqlite3.Connection, account: str, gmail_ids) -> int:
    """
    Take messages out of the catalogue: their rows, labels and match
    memberships (forget). Returns how many were catalogued. No commit.
    """
    removed = 0
    for gid in gmail_ids:
        key = (account, gid)
        removed += con.execute(
            "DELETE FROM messages WHERE account = ? AND gmail_id = ?", key
        ).rowcount
        con.execute("DELETE FROM labels WHERE account = ? AND gmail_id = ?", key)
        con.execute("DELETE FROM match_members WHERE account = ? AND gmail_id = ?", key)
    return removed
