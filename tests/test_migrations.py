"""
Every schema migration must be safe to repeat (CLAUDE.md). A migration's
steps can run and the version bump not be recorded: a crash in between, or
a step that commits on its own (SQLite's ALTER TABLE does, outside a
transaction). The next open then runs the steps again on a database that
already has them. These tests make that happen for every version in each
MIGRATIONS: open a current database with rows in it, set its recorded
version back, open it again, and check nothing failed or changed.
"""

import sqlite3

import pytest

from mailcellar.backup import commands, sidecar
from mailcellar.dedup import cli as dedup
from mailcellar.dedup import db as dedup_db
from mailcellar.search import db as search_db
from mailcellar.search import index
from test_backup import ACCOUNT, FakeGmail, raw_response

# Rebuilt on every match (dedup's v5 step drops them), so not data to keep.
REBUILT = {"match_groups", "match_members"}


def backed_up(tmp_path):
    folder = tmp_path / "data" / ACCOUNT
    gmail = FakeGmail({gid: raw_response(gid) for gid in ("g1", "g2")})
    commands.backup(gmail, ACCOUNT, folder)
    return folder


def snapshot(path, skip=frozenset()):
    """Each table's columns and rows, apart from the recorded version."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = [
            name
            for (name,) in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
            if name not in skip and not name.startswith(("sqlite_", "text"))
        ]
        out = {}
        for table in tables:
            columns = [row[1] for row in con.execute(f"PRAGMA table_info({table})")]
            rows = sorted(map(repr, con.execute(f"SELECT * FROM {table}")))
            if table == "meta":
                rows = [r for r in rows if "schema_version" not in r]
            out[table] = (columns, rows)
        return out
    finally:
        con.close()


def set_version(path, version):
    con = sqlite3.connect(path)
    con.execute("UPDATE meta SET value = ? WHERE name = 'schema_version'", (version,))
    con.commit()
    con.close()


def recorded_version(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return con.execute("SELECT value FROM meta WHERE name = 'schema_version'").fetchone()[0]
    finally:
        con.close()


@pytest.mark.parametrize("version", sorted(sidecar.MIGRATIONS, key=int))
def test_meta_migrations_repeat_safely(tmp_path, version):
    folder = backed_up(tmp_path)
    con = sidecar.open_meta(folder)
    con.execute("INSERT INTO offloaded VALUES ('g1', 'b1', 'old', 1, 2, NULL)")
    con.execute("INSERT INTO restored VALUES ('g2', '', 'n2', 'imported', 3, 'r1')")
    con.execute("INSERT INTO forgotten VALUES ('g9', 'b1', 'old', 4, 5, 'run', 6, NULL)")
    con.commit()
    con.close()
    path = sidecar.meta_path(folder)
    before = snapshot(path)

    set_version(path, version)
    sidecar.open_meta(folder).close()

    assert recorded_version(path) == sidecar.SCHEMA_VERSION
    assert snapshot(path) == before


@pytest.mark.parametrize("version", sorted(dedup_db.MIGRATIONS, key=int))
def test_dedup_migrations_repeat_safely(tmp_path, version):
    folder = backed_up(tmp_path)
    path = tmp_path / "dedup.sqlite"
    dedup.main(["index", "--db", str(path), str(folder)])
    before = snapshot(path, skip=REBUILT)
    assert before["messages"][1]  # there is something to lose

    set_version(path, version)
    dedup_db.open_db(path).close()

    assert recorded_version(path) == dedup_db.SCHEMA_VERSION
    assert snapshot(path, skip=REBUILT) == before


def test_search_migrations_repeat_safely(tmp_path):
    folder = backed_up(tmp_path)
    path = tmp_path / "search.sqlite"
    con = search_db.open_db(path)
    index.update(con, folder)
    con.close()
    before = snapshot(path)
    assert before["docs"][1]

    # none yet: this pins the harness, so the first migration gets the same check
    for version in sorted(search_db.MIGRATIONS, key=int):
        set_version(path, version)
        search_db.open_db(path).close()
        assert snapshot(path) == before
    search_db.open_db(path).close()

    assert recorded_version(path) == search_db.SCHEMA_VERSION
    assert snapshot(path) == before
