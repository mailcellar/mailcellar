"""The offload record (meta.sqlite schema 6, renamed in 7): offloaded mail stays in the backup."""

import sqlite3

import pytest

from mailcellar.backup import commands, sidecar
from mailcellar.report import health
from mailcellar.report.db import open_sidecars
from test_backup import FakeGmail, raw_response

A = "a@x.com"
OFFLOADED = ("g0", "g1", "g2")


def yes(prompt):
    return True


@pytest.fixture
def setup(tmp_path):
    """20 backed-up messages; g0–g2 offloaded (recorded, then trashed in Gmail)."""
    folder = tmp_path / "data" / A
    gmail = FakeGmail({f"g{i}": raw_response(f"g{i}") for i in range(20)})
    commands.backup(gmail, A, folder)
    con = sidecar.open_meta(folder)
    sidecar.record_offloaded(con, OFFLOADED, "2026-10-24-120000", "old bulk", 1_000)
    con.commit()
    con.close()
    gmail.trashed.update(OFFLOADED)
    return gmail, folder


def offloaded_rows(folder):
    con = sqlite3.connect(folder / "meta.sqlite")
    return dict(con.execute("SELECT gmail_id, gone_ms IS NOT NULL FROM offloaded"))


def delete(gmail, *gids):
    """Permanent deletion in Gmail (Trash emptied)."""
    for gid in gids:
        del gmail.messages[gid]
        gmail.trashed.discard(gid)


def last_run(folder, *cols):
    con = sqlite3.connect(folder / "meta.sqlite")
    return con.execute(f"SELECT {', '.join(cols)} FROM runs ORDER BY run_id DESC").fetchone()


def test_v5_sidecar_gains_offloaded_repeatably(tmp_path):
    folder = tmp_path / "data" / A
    commands.backup(FakeGmail({"g1": raw_response("g1")}), A, folder)
    con = sqlite3.connect(folder / "meta.sqlite")
    con.execute("DROP TABLE offloaded")
    con.execute("ALTER TABLE runs DROP COLUMN offloaded")
    con.execute("UPDATE meta SET value = '5' WHERE name = 'schema_version'")
    con.commit()
    con.close()
    for _ in range(2):
        sidecar.open_meta(folder).close()
    con = sqlite3.connect(folder / "meta.sqlite")
    assert con.execute("SELECT value FROM meta WHERE name = 'schema_version'").fetchone() == ("9",)
    assert con.execute("SELECT command, offloaded FROM runs").fetchall() == [("backup", None)]
    assert con.execute("SELECT count(*) FROM offloaded").fetchone() == (0,)


def test_v6_purge_record_becomes_the_offload_record_repeatably(setup):
    gmail, folder = setup
    commands.count(gmail, A, folder)  # a run row with offloaded = 3
    con = sqlite3.connect(folder / "meta.sqlite")  # back to v6's names
    con.execute("ALTER TABLE offloaded RENAME TO purged")
    con.execute("ALTER TABLE runs RENAME COLUMN offloaded TO purged")
    con.execute("UPDATE meta SET value = '6' WHERE name = 'schema_version'")
    con.commit()
    con.close()
    for _ in range(2):
        sidecar.open_meta(folder).close()
    con = sqlite3.connect(folder / "meta.sqlite")
    tables = {n for (n,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "purged" not in tables
    assert con.execute("SELECT value FROM meta WHERE name = 'schema_version'").fetchone() == ("9",)
    assert offloaded_rows(folder) == dict.fromkeys(OFFLOADED, 0)
    assert (
        con.execute("SELECT batch, rule FROM offloaded").fetchall()
        == [("2026-10-24-120000", "old bulk")] * 3
    )
    assert last_run(folder, "command", "offloaded") == ("count", 3)


def test_v5_sidecar_with_both_run_columns_keeps_the_count(tmp_path):
    """A v5 sidecar migrated by v6's step gains runs.offloaded; a stray purged column merges."""
    folder = tmp_path / "data" / A
    commands.backup(FakeGmail({"g1": raw_response("g1")}), A, folder)
    con = sqlite3.connect(folder / "meta.sqlite")
    con.execute("ALTER TABLE runs ADD COLUMN purged INTEGER")
    con.execute("UPDATE runs SET purged = 4")
    con.execute("UPDATE meta SET value = '6' WHERE name = 'schema_version'")
    con.commit()
    con.close()
    sidecar.open_meta(folder).close()
    con = sqlite3.connect(folder / "meta.sqlite")
    cols = {row[1] for row in con.execute("PRAGMA table_info(runs)")}
    assert "purged" not in cols
    assert con.execute("SELECT offloaded FROM runs").fetchall() == [(4,)]


def test_count_reports_offloaded_apart(setup):
    gmail, folder = setup
    c = commands.count(gmail, A, folder)
    assert (c["only_in_backup"], c["gone_from_server"]) == (0, 0)  # trashed, but offloaded
    assert (c["offloaded"], c["offloaded_in_gmail"]) == (3, 3)

    delete(gmail, "g0", "g5")  # Trash emptied for g0; g5 lost unexpectedly
    c = commands.count(gmail, A, folder)
    assert (c["only_in_backup"], c["gone_from_server"]) == (1, 1)
    assert (c["offloaded"], c["offloaded_in_gmail"]) == (3, 2)
    assert offloaded_rows(folder) == {"g0": 1, "g1": 0, "g2": 0}
    assert last_run(folder, "command", "offloaded", "deleted_in_gmail") == ("count", 3, 1)


def test_sync_keeps_offloaded_and_leaves_them_out_of_the_guard(setup):
    gmail, folder = setup
    delete(gmail, *OFFLOADED, "g5")  # 4 of 20 gone: over 10%, but only 1 of 17 unexpectedly
    result = commands.prune(gmail, A, folder, confirm=yes)
    assert result["pruned"] == 1
    con = sqlite3.connect(folder / "msg-db.sqlite")
    uids = {u for (u,) in con.execute("SELECT uid FROM uids")}
    assert set(OFFLOADED) <= uids and "g5" not in uids
    assert all(next(folder.rglob(f"{g}.eml"), None) for g in OFFLOADED)
    assert offloaded_rows(folder) == dict.fromkeys(OFFLOADED, 1)
    assert last_run(folder, "command", "status", "pruned", "offloaded") == ("sync", "ok", 1, 3)

    delete(gmail, "g6", "g7")  # 2 more of 17 unexpectedly: the guard still applies
    with pytest.raises(commands.PruneAborted):
        commands.prune(gmail, A, folder, confirm=yes, max_fraction=0.1)


def test_history_backup_does_not_count_offloaded_as_deleted(setup):
    gmail, folder = setup
    commands.backup(gmail, A, folder)  # history: sees the offloaded mail trashed
    delete(gmail, "g0", "g5")
    stats = commands.backup(gmail, A, folder)
    assert stats["mode"] == "history"
    assert last_run(folder, "deleted_in_gmail") == (1,)
    assert offloaded_rows(folder) == {"g0": 1, "g1": 0, "g2": 0}


def test_health_shows_offloaded(setup):
    gmail, folder = setup
    delete(gmail, "g0")
    commands.count(gmail, A, folder)
    con, _accounts = open_sidecars([folder])
    assert con.execute("SELECT account, count(*) FROM all_offloaded").fetchone() == (A, 3)
    tables = {t.name: t for t in health.build(con, [folder], health.Options())}
    con.close()
    (state,) = tables["state"].rows
    assert state[5:7] == (3, 2)  # offloaded, still in Trash
    count_run = tables["runs"].rows[0]
    assert (count_run[2], count_run[11]) == ("count", 3)
