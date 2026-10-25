"""
search.sqlite: schema, migrations and opening.

The full-text index is an SQLite FTS5 table without stored content: it holds
only what a search needs to find a message, and `docs` holds what a result
shows. The text itself stays in the .eml files.
"""

import sqlite3
import sys
from pathlib import Path

from mailcellar.paths import data_root

SCHEMA_VERSION = "1"

# Steps that upgrade schema version N to N+1, applied in order by open_db.
# A step is an SQL statement or a function taking the connection.
MIGRATIONS: dict = {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT);

-- One row per indexed message; doc is the rowid of its row in `text`.
CREATE TABLE IF NOT EXISTS docs (
    doc           INTEGER PRIMARY KEY AUTOINCREMENT,  -- never reused
    account       TEXT NOT NULL,
    gmail_id      TEXT NOT NULL,
    path          TEXT NOT NULL,     -- relative to the account's backup folder
    size          INTEGER NOT NULL,
    internal_date INTEGER NOT NULL,  -- ms UTC
    sender        TEXT NOT NULL,     -- decoded From, for display
    subject       TEXT NOT NULL,
    text_version  TEXT NOT NULL,     -- index.TEXT_VERSION it was indexed with
    UNIQUE (account, gmail_id)
);
CREATE INDEX IF NOT EXISTS docs_date ON docs (internal_date);

CREATE VIRTUAL TABLE IF NOT EXISTS text USING fts5(
    sender, recipients, subject, body,
    content = '', contentless_delete = 1,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""


def default_db() -> Path:
    return data_root() / "search" / "search.sqlite"


def open_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    try:
        try:
            con.executescript(SCHEMA)
        except sqlite3.OperationalError as exc:
            sys.exit(
                f"error: cannot create the search index ({exc}); it needs SQLite 3.43 or "
                f"newer with FTS5, and this Python has {sqlite3.sqlite_version}"
            )
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
                "Delete it; the next search builds it again."
            )
        return con
    except BaseException:
        con.close()  # a failed open leaves no connection behind
        raise
