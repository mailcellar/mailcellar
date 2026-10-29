"""The runs table and the health report."""

import sqlite3
from contextlib import closing

import pytest

from fake_mailbox import FakeMailbox, raw_response
from mailcellar.backup import commands, sidecar
from mailcellar.report import cli, health
from mailcellar.report.db import open_sidecars

A, B = "a@x.com", "b@y.com"
ACCOUNT_DIR = f"data/{A}"
DAY = 86_400_000
NOW = 1_792_800_000_000  # 2026-10-24 00:00 UTC


def runs(folder, *cols):
    con = sqlite3.connect(folder / "meta.sqlite")
    return con.execute(f"SELECT {', '.join(cols)} FROM runs ORDER BY run_id").fetchall()


# ── recording runs ───────────────────────────────────────────────────────────


def test_v4_sidecar_gains_runs_and_offloaded_tables_repeatably(tmp_path):
    folder = tmp_path / ACCOUNT_DIR
    commands.backup(FakeMailbox({"g1": raw_response("g1")}), A, folder)
    con = sqlite3.connect(folder / "meta.sqlite")
    con.execute("DROP TABLE runs")
    con.execute("DROP TABLE offloaded")
    con.execute("UPDATE meta SET value = '4' WHERE name = 'schema_version'")
    con.commit()
    con.close()
    for _ in range(2):
        sidecar.open_meta(folder).close()
    con = sqlite3.connect(folder / "meta.sqlite")
    assert con.execute("SELECT value FROM meta WHERE name = 'schema_version'").fetchone() == (
        sidecar.SCHEMA_VERSION,
    )
    assert con.execute("SELECT count(*) FROM runs").fetchone() == (0,)
    assert con.execute("SELECT count(*) FROM offloaded").fetchone() == (0,)
    assert "offloaded" in {row[1] for row in con.execute("PRAGMA table_info(runs)")}


def test_backup_runs_record_mode_reason_and_counts(tmp_path):
    folder = tmp_path / ACCOUNT_DIR
    gmail = FakeMailbox({"g1": raw_response("g1"), "c1": raw_response("c1", label_ids=["CHAT"])})
    commands.backup(gmail, A, folder)  # first: no start point
    gmail.messages["g2"] = raw_response("g2")
    commands.backup(gmail, A, folder)  # history
    with closing(sidecar.open_meta(folder)) as mcon:
        gmail.expired.add(sidecar.get_state(mcon, "history_id"))
    commands.backup(gmail, A, folder)  # expired → full
    commands.backup(gmail, A, folder, full=True)
    commands.backup(gmail, A, folder, query="x")
    commands.backup(gmail, A, folder, refresh_labels=True)
    assert runs(folder, "command", "mode", "reason", "status") == [
        ("backup", "full", "no-history", "ok"),
        ("backup", "history", "", "ok"),
        ("backup", "full", "expired", "ok"),
        ("backup", "full", "full", "ok"),
        ("backup", "full", "search", "ok"),
        ("backup", "full", "refresh-labels", "ok"),
    ]
    first, second = runs(folder, "listed", "downloaded", "skipped", "failed", "messages")[:2]
    assert first == (2, 1, 1, 0, 1)  # the chat is skipped
    assert second == (None, 1, 0, 0, 2)  # history runs list nothing
    started, finished = runs(folder, "started_ms", "finished_ms")[0]
    assert 0 <= finished - started < 60_000


def test_backup_failures_and_errors_are_recorded(tmp_path):
    folder = tmp_path / ACCOUNT_DIR
    gmail = FakeMailbox({"g1": raw_response("g1"), "g2": raw_response("g2")}, fail={"g2"})
    commands.backup(gmail, A, folder)

    def broken(*a, **k):
        raise RuntimeError("network down")

    gmail.list_message_ids = broken  # no start point stored after the failure: lists again
    with pytest.raises(RuntimeError):
        commands.backup(gmail, A, folder)
    assert runs(folder, "status", "failed", "downloaded") == [("failures", 1, 1), ("error", 0, 0)]


def test_count_runs(tmp_path):
    folder = tmp_path / ACCOUNT_DIR
    gmail = FakeMailbox({f"g{i}": raw_response(f"g{i}") for i in range(10)})
    commands.backup(gmail, A, folder)
    del gmail.messages["g0"]
    gmail.messages["g10"] = raw_response("g10")
    commands.count(gmail, A, folder)
    cols = ("command", "status", "in_gmail", "missing", "only_in_backup", "pruned", "messages")
    assert runs(folder, *cols)[1:] == [("count", "missing", 10, 1, 1, None, 10)]


# ── the report ───────────────────────────────────────────────────────────────


@pytest.fixture
def report(tmp_path, monkeypatch):
    """A: runs at known times, a lost .eml and a pruned batch. B: no start point."""
    folder_a, folder_b = tmp_path / "data" / A, tmp_path / "data" / B
    gmail = FakeMailbox({"g1": raw_response("g1"), "g2": raw_response("g2")})
    commands.backup(gmail, A, folder_a)
    commands.backup(FakeMailbox({"h1": raw_response("h1")}), B, folder_b, query="x")
    con = sidecar.open_meta(folder_a)
    con.execute("DELETE FROM runs")
    for start, mode, reason, status, downloaded in [
        (NOW - 80 * DAY, "full", "no-history", "ok", 2),
        (NOW - 70 * DAY, "history", "", "ok", 3),
        (NOW - 30 * DAY, "full", "expired", "ok", 1),  # 40 days after the last
        (NOW - 29 * DAY, "full", "search", "ok", 0),  # leaves no start point
        (NOW - 20 * DAY, "history", "", "failures", 4),  # nor does a failed run
    ]:
        sidecar.record_run(
            con, "backup", start, start + 30_000, status, mode, reason, downloaded=downloaded
        )
    con.close()
    next(folder_a.rglob("g2.eml")).unlink()
    batch = tmp_path / "data" / "_pruned" / A / "2026-09-01-120000"
    batch.mkdir(parents=True)
    (batch / "pruned.csv").write_text("gmail_id,path\nx1,a\nx2,b\n")
    monkeypatch.chdir(tmp_path)

    def run(**opts):
        folders = cli.resolve_folders([])
        con, _accounts = open_sidecars(folders)
        with closing(con):
            built = health.build(con, folders, health.Options(now_ms=NOW, **opts))
        return {t.name: t for t in built}

    return run


def test_state(report):
    rows = {r[0]: r for r in report()["state"].rows}
    a, b = rows[A], rows[B]
    assert a[1:9] == (2, 1, 0, 0, 0, 0, "2026-10-22", "yes")
    assert a[9] == "2026-10-04 00:00"  # last backup: the failed one
    assert a[10] == "2026-09-24 00:00"  # last full listing
    assert a[11] == pytest.approx(30 - 30_000 / DAY)  # since the expired-then-full run
    assert a[12] == "full listing: start point too old"
    assert b[8] == "no" and b[12] == "full listing: no start point"
    assert report(window_days=31)["state"].rows[0][12] == "history"


def test_runs_gaps_and_months(report):
    t = report()
    assert [r[5] for r in t["runs"].rows if r[0] == A] == [
        "failures",
        "ok",
        "ok",
        "ok",
        "ok",
    ]
    assert [(r[1][:10], r[2][:10]) for r in t["gaps"].rows] == [
        ("2026-08-15", "2026-09-24"),
        ("2026-09-24", "now"),
    ]
    months = {(r[0], r[1]): r[2:] for r in t["by-month"].rows}
    assert months[("2026-08", A)] == (2, 1, 0, 0, 5, 0, 0, 0)
    assert months[("2026-10", A)] == (1, 0, 0, 0, 4, 0, 0, 1)


def test_pruned_batches_and_cli(report, capsys, tmp_path):
    assert report()["pruned"].rows == [(A, "2026-09-01-120000", 2)]
    cli.main(["health", "--csv", "reports"])
    assert capsys.readouterr().out.startswith(f"Backup health report — {A}, {B}")
    assert len(list((tmp_path / "reports").glob("health-*.csv"))) == 9
    headers = {
        h
        for path in (tmp_path / "reports").glob("health-*.csv")
        for h in path.read_text(encoding="utf-8").splitlines()[0].split(",")
    }
    assert {"Deleted in mailbox", "Not yet gone from mailbox"} <= headers  # Gmail or IMAP
    assert not any("Gmail" in h for h in headers)


def test_forget_runs_held_and_due(report, tmp_path):
    from datetime import datetime

    from mailcellar.offload.forget import RUN_FORMAT

    def held_run(days_ago, n):
        name = datetime.fromtimestamp((NOW - days_ago * DAY) / 1000).strftime(RUN_FORMAT)
        run = tmp_path / "data" / "_forgotten" / A / name
        run.mkdir(parents=True)
        lines = "".join(f"x{i},2026/1/1/x{i}.eml\n" for i in range(n))
        (run / "forgotten.csv").write_text("gmail_id,path\n" + lines)
        return name

    old, recent = held_run(40, 3), held_run(10, 1)
    (tmp_path / "data" / "_forgotten" / A / "notes").mkdir()  # no manifest: not a run
    rows = report()["forgotten"].rows
    assert [(r[0], r[1], r[2], r[4]) for r in rows] == [(A, old, 3, "yes"), (A, recent, 1, "")]
    assert rows[1][3] == datetime.fromtimestamp((NOW + 20 * DAY) / 1000).strftime("%Y-%m-%d")


# ── tokens ───────────────────────────────────────────────────────────────────


def test_tokens_table_shows_when_consent_was_given(report):
    from datetime import UTC, datetime

    consent = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)  # 3.5 days before NOW
    rows = [(A, "readonly", consent), (A, "modify", None)]
    folders = cli.resolve_folders([])
    con, _accounts = open_sidecars(folders)
    with closing(con):
        built = health.build(con, folders, health.Options(now_ms=NOW), rows)
    table = {t.name: t for t in built}["tokens"]
    assert table.rows[0][:3] == (A, "readonly", "2026-10-20")
    assert table.rows[0][3] == pytest.approx(3.5)
    assert table.rows[1] == (A, "modify", "", None)  # from before it was recorded
    assert "Testing status" in table.notes[0] and "--renew" in table.notes[0]


def test_there_is_no_tokens_table_without_tokens(report):
    assert "tokens" not in report()


def test_token_rows_lists_each_configured_token(tmp_path, monkeypatch):
    import json
    from datetime import UTC, datetime

    from mailcellar import gmailapi

    monkeypatch.chdir(tmp_path)
    assert cli.token_rows([A]) == []  # no accounts.toml: nothing to report
    config = tmp_path / "config"
    (config / "aa").mkdir(parents=True)
    (config / "accounts.toml").write_text(f'aa = "{A}"\nbb = "{B}"\n')
    consent = datetime(2026, 10, 20, tzinfo=UTC)
    gmailapi.token_file(config / "aa", "modify").write_text(
        json.dumps({"authorised_at": consent.isoformat()})
    )
    assert cli.token_rows([A, B, "c@z.com"]) == [(A, "modify", consent)]  # only what exists
