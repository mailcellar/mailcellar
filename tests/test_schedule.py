"""schedule: running backups regularly and unattended."""

import json
import plistlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from mailcellar import cli, gmailapi
from mailcellar.backup import commands, schedule, sidecar
from mailcellar.backup.schedule import Job, Schedule
from test_backup import FakeGmail, raw_response

A, B = "a@x.com", "b@y.com"
NOW = datetime.now(UTC)
EXE = "/opt/mailcellar/bin/mailcellar"
WEEKLY = Schedule("weekly", 3, 30, 0)
DAILY = Schedule("daily", 6, 15)


def job(schedule_=WEEKLY, accounts=(), log="/data/logs/backup.log"):
    return Job(
        schedule_,
        (EXE, "backup", "--unattended", *accounts),
        (("MAILCELLAR_CONFIG", "/config"), ("MAILCELLAR_DATA", "/data")),
        Path(log),
    )


# ── the decisions ────────────────────────────────────────────────────────────


def test_times_of_day():
    assert schedule.parse_time("03:30") == (3, 30)
    assert schedule.parse_time("3:05") == (3, 5)
    for bad in ("24:00", "03:60", "noon", "3", "-1:00", "03:30:00"):
        with pytest.raises(ValueError, match="use HH:MM"):
            schedule.parse_time(bad)


def test_schedules_are_described_in_words():
    assert schedule.describe(WEEKLY) == "weekly on Sun at 03:30"
    assert schedule.describe(Schedule("weekly", 21, 5, 3)) == "weekly on Wed at 21:05"
    assert schedule.describe(DAILY) == "daily at 06:15"


@pytest.mark.parametrize(
    ("days_ago", "schedule_", "expected"),
    [
        (None, WEEKLY, True),  # never ran
        (1, DAILY, False),
        (2, DAILY, False),  # one missed run is made up when the Mac wakes
        (3, DAILY, True),
        (13, WEEKLY, False),
        (15, WEEKLY, True),
        (27, None, False),  # no schedule: Gmail's four-week history window
        (57, None, True),
    ],
)
def test_overdue(days_ago, schedule_, expected):
    last = None if days_ago is None else 1_000 * DAY_MS_FOR_TESTS - days_ago * DAY_MS_FOR_TESTS
    assert schedule.overdue(last, 1_000 * DAY_MS_FOR_TESTS, schedule_) is expected


DAY_MS_FOR_TESTS = 86_400_000


def test_launchd_plist_says_what_to_run_when_and_where():
    plist = plistlib.loads(schedule.launchd_plist(job(accounts=(A,))))
    assert plist["Label"] == schedule.LABEL
    assert plist["ProgramArguments"] == [EXE, "backup", "--unattended", A]
    assert plist["StartCalendarInterval"] == {"Hour": 3, "Minute": 30, "Weekday": 0}
    assert plist["EnvironmentVariables"] == {
        "MAILCELLAR_DATA": "/data",
        "MAILCELLAR_CONFIG": "/config",
    }
    assert plist["StandardOutPath"] == plist["StandardErrorPath"] == "/data/logs/backup.log"
    assert plist["RunAtLoad"] is False  # installing doesn't start a backup
    daily = plistlib.loads(schedule.launchd_plist(job(DAILY)))
    assert daily["StartCalendarInterval"] == {"Hour": 6, "Minute": 15}  # no Weekday


@pytest.mark.parametrize("which", [WEEKLY, DAILY, Schedule("weekly", 0, 0, 6)])
def test_a_plist_reads_back_as_the_same_job(which):
    original = job(which, accounts=(A, B))
    assert schedule.job_from_plist(schedule.launchd_plist(original)) == original
    assert schedule.accounts_of(original) == (A, B)
    assert schedule.accounts_of(job()) == ()


def test_cron_lines():
    line = schedule.cron_line(job())
    assert line == (
        f"30 3 * * 0 MAILCELLAR_CONFIG=/config MAILCELLAR_DATA=/data {EXE} backup --unattended "
        ">> /data/logs/backup.log 2>&1"
    )
    assert schedule.cron_line(job(DAILY)).startswith("15 6 * * * ")
    assert schedule.cron_line(job(Schedule("weekly", 1, 2, 5))).startswith("2 1 * * 5 ")
    spaced = Job(
        WEEKLY,
        ("/My Apps/mailcellar", "backup"),
        (("MAILCELLAR_DATA", "/my data"),),
        Path("/my data/log"),
    )
    assert "'/My Apps/mailcellar'" in schedule.cron_line(spaced)
    assert "MAILCELLAR_DATA='/my data'" in schedule.cron_line(spaced)
    assert ">> '/my data/log'" in schedule.cron_line(spaced)
    # cron ends the command at an unescaped %, so a path holding one is escaped.
    percent = Job(
        WEEKLY, ("mailcellar", "backup"), (("MAILCELLAR_DATA", "/mail%20x"),), Path("/l%")
    )
    line = schedule.cron_line(percent)
    assert "MAILCELLAR_DATA=/mail\\%20x" in line and ">> /l\\% 2>&1" in line
    assert "%" not in line.replace("\\%", "")


# ── installing, with the system faked ────────────────────────────────────────


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A config with two accounts and tokens, backups of both, and a fake launchctl."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = tmp_path / "config"
    for name, _account in (("aa", A), ("bb", B)):
        (config / name).mkdir(parents=True)
        (config / name / "client_secrets.json").write_text("{}")
        write_token(config / name)
    (config / "accounts.toml").write_text(f'aa = "{A}"\nbb = "{B}"\n')
    for account in (A, B):
        commands.backup(FakeGmail({"g1": raw_response("g1")}), account, tmp_path / "data" / account)
    calls = []
    state = {"bootstrap": 0, "print": 0}

    def fake_launchctl(*args):
        calls.append(args)
        return {"bootstrap": state["bootstrap"], "print": state["print"]}.get(args[0], 0), "out"

    monkeypatch.setattr(schedule, "launchctl", fake_launchctl)
    monkeypatch.setattr(schedule, "uid", lambda: 501)
    monkeypatch.setattr(schedule, "executable", lambda: [EXE])
    monkeypatch.setattr(schedule.sys, "platform", "darwin")
    return SimpleNamespace(path=tmp_path / "home", calls=calls, state=state, config=config)


def write_token(folder, days_ago=None, scope="readonly"):
    data = {"token": "t"}
    if days_ago is not None:
        data["authorised_at"] = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    gmailapi.token_file(folder, scope).write_text(json.dumps(data))


def plist_file(home):
    return home.path / "Library" / "LaunchAgents" / f"{schedule.LABEL}.plist"


def run(*args):
    return cli.main(["schedule", *args])


def test_install_writes_the_agent_and_loads_it(home, capsys):
    assert run("install") == 0
    agent = schedule.job_from_plist(plist_file(home).read_bytes())
    assert agent.schedule == WEEKLY  # weekly, Sunday 03:30, by default
    assert agent.arguments == (EXE, "backup", "--unattended")  # no accounts: later ones included
    env = dict(agent.environment)
    assert env["MAILCELLAR_DATA"] == str((Path.cwd() / "data").resolve())
    assert env["MAILCELLAR_CONFIG"] == str((Path.cwd() / "config").resolve())
    assert agent.log == (Path.cwd() / "data").resolve() / "logs" / "backup.log"
    assert agent.log.parent.is_dir()
    assert home.calls == [
        ("bootout", f"gui/501/{schedule.LABEL}"),
        ("bootstrap", "gui/501", str(plist_file(home))),
    ]
    out = capsys.readouterr().out
    assert "Scheduled weekly on Sun at 03:30" in out and "when it wakes" in out


def test_install_records_the_accounts_chosen_by_address(home):
    assert run("install", "aa", "--daily", "--at", "06:15") == 0
    agent = schedule.job_from_plist(plist_file(home).read_bytes())
    assert agent.schedule == DAILY
    assert agent.arguments == (EXE, "backup", "--unattended", A)  # "aa" resolved


def test_install_weekday_and_reinstall_replace_the_agent(home):
    run("install", "--weekday", "wed")
    assert schedule.job_from_plist(plist_file(home).read_bytes()).schedule.weekday == 3
    home.calls.clear()
    run("install", "--daily")
    assert schedule.job_from_plist(plist_file(home).read_bytes()).schedule.frequency == "daily"
    assert home.calls[0][0] == "bootout"  # the old one is unloaded first


def test_install_refuses_an_account_with_no_token(home):
    gmailapi.token_file(home.config / "bb", "readonly").unlink()
    with pytest.raises(
        SystemExit, match=rf"no token for {B}.*mailcellar auth {B} --scope readonly"
    ):
        run("install")
    assert not plist_file(home).exists() and home.calls == []


def test_a_modify_token_is_enough(home):
    gmailapi.token_file(home.config / "bb", "readonly").unlink()
    write_token(home.config / "bb", scope="modify")
    assert run("install") == 0


def test_print_shows_the_agent_and_installs_nothing(home, capsys):
    gmailapi.token_file(home.config / "bb", "readonly").unlink()  # --print doesn't mind
    assert run("install", "--print") == 0
    assert schedule.LABEL in capsys.readouterr().out
    assert not plist_file(home).exists() and home.calls == []


def test_elsewhere_install_prints_a_cron_line_and_changes_nothing(home, monkeypatch, capsys):
    monkeypatch.setattr(schedule.sys, "platform", "linux")
    assert run("install", "--daily", "--at", "02:10") == 0
    out = capsys.readouterr().out
    assert "daily at 02:10" in out and "crontab -e" in out
    assert "10 2 * * * MAILCELLAR_CONFIG=" in out and f"{EXE} backup --unattended >> " in out
    assert "never edits a crontab" in out
    assert not plist_file(home).exists() and home.calls == []


def test_a_failing_launchctl_is_an_error(home):
    home.state["bootstrap"] = 5
    with pytest.raises(SystemExit, match="launchctl bootstrap failed"):
        run("install")


def test_bad_arguments_are_errors(home):
    with pytest.raises(SystemExit, match="use HH:MM"):
        run("install", "--at", "25:00")
    with pytest.raises(SystemExit, match="error:"):
        run("install", "nobody")
    assert home.calls == []


def test_remove_unloads_and_deletes_the_agent(home, monkeypatch, capsys):
    run("install")
    home.calls.clear()
    assert run("remove") == 0
    assert not plist_file(home).exists()
    assert home.calls == [("bootout", f"gui/501/{schedule.LABEL}")]
    assert "Removed" in capsys.readouterr().out
    assert run("remove") == 0
    assert "no schedule is installed" in capsys.readouterr().out
    monkeypatch.setattr(schedule.sys, "platform", "linux")
    run("remove")
    assert "only on macOS" in capsys.readouterr().out


# ── status ───────────────────────────────────────────────────────────────────


def add_run(home, account, status, ago_hours=1, reason=""):
    folder = Path.cwd() / "data" / account
    con = sidecar.open_meta(folder)
    now = int((datetime.now(UTC) - timedelta(hours=ago_hours)).timestamp() * 1000)
    sidecar.record_run(con, "backup", now, now, status, reason=reason)
    con.close()


def test_status_of_a_healthy_schedule(home, capsys):
    run("install")
    assert run("status") == 0
    out = capsys.readouterr().out
    assert "Scheduled weekly on Sun at 03:30" in out and "not loaded" not in out
    assert str(plist_file(home)) in out
    for account in (A, B):
        line = next(line for line in out.splitlines() if line.strip().startswith(account))
        assert "last backup" in line and "ok" in line and "overdue" not in line
        assert "readonly token, age unknown" in line


def test_status_reports_the_last_run_even_a_failed_one(home, capsys):
    run("install")
    add_run(home, A, "error", ago_hours=0, reason="needs-consent")
    assert run("status") == 1
    line = next(x for x in capsys.readouterr().out.splitlines() if x.strip().startswith(A))
    assert ", error" in line  # the unattended run that could not sign in


def test_status_flags_an_overdue_backup(home, capsys):
    run("install")
    add_run(home, B, "ok", ago_hours=24 * 20)
    con = sqlite3.connect(Path.cwd() / "data" / B / "meta.sqlite")
    con.execute("DELETE FROM runs WHERE started_ms > (SELECT min(started_ms) + 1000 FROM runs)")
    con.commit()
    con.close()
    # B's newest run is now the 20-day-old one; the weekly schedule allows 14 days
    assert run("status") == 1
    assert "(overdue)" in capsys.readouterr().out


def test_status_flags_an_agent_that_is_not_loaded(home, capsys):
    run("install")
    home.state["print"] = 113
    assert run("status") == 1
    assert "(not loaded)" in capsys.readouterr().out


def test_status_shows_how_old_each_token_is(home, capsys):
    run("install")
    write_token(home.config / "aa", days_ago=2)
    write_token(home.config / "bb", days_ago=6)  # past the warning threshold of 5 days
    assert run("status") == 1
    out = capsys.readouterr().out
    assert "readonly token, 2 days since consent" in out
    assert "readonly token, 6 days since consent" in out


def test_status_without_a_schedule_still_reports_the_backups(home, monkeypatch, capsys):
    monkeypatch.setattr(schedule.sys, "platform", "linux")
    assert run("status") == 0
    out = capsys.readouterr().out
    assert "No schedule installed by mailcellar (it installs one only on macOS)" in out
    assert A in out and B in out
    (Path.cwd() / "data" / B / "meta.sqlite").unlink()
    assert run("status") == 1
    assert "no backup run recorded" in capsys.readouterr().out


def test_status_flags_a_missing_token(home, capsys):
    run("install")
    gmailapi.token_file(home.config / "aa", "readonly").unlink()
    assert run("status") == 1
    assert f"no token: run mailcellar auth {A} --scope readonly" in capsys.readouterr().out


def test_status_uses_the_accounts_the_schedule_names(home, capsys):
    run("install", "aa")
    run("status")
    out = capsys.readouterr().out
    assert A in out and B not in out


# ── an unattended run that cannot sign in ────────────────────────────────────


class Flow:
    called = False


@pytest.fixture
def no_browser(monkeypatch):
    from google_auth_oauthlib import flow as gflow

    def from_secrets(cls, path, scopes):
        raise AssertionError("an unattended run must not start the consent flow")

    monkeypatch.setattr(
        gflow.InstalledAppFlow, "from_client_secrets_file", classmethod(from_secrets)
    )


def test_unattended_without_a_token_raises_with_the_command(tmp_path, no_browser, capsys):
    with pytest.raises(gmailapi.NeedsConsent) as raised:
        gmailapi.load_credentials(tmp_path, A, "readonly", interactive=False)
    assert "has no token yet" in str(raised.value)
    assert f"mailcellar auth {A} --scope readonly" in str(raised.value)
    assert isinstance(raised.value, gmailapi.GmailError)
    assert capsys.readouterr().out == ""  # and says nothing else


def test_unattended_with_a_rejected_token_raises_without_printing(
    tmp_path, no_browser, monkeypatch, capsys
):
    from google.auth.exceptions import RefreshError
    from google.oauth2 import credentials as gcreds

    gmailapi.token_file(tmp_path, "readonly").write_text("{}")

    class Stale:
        expired, valid, refresh_token = True, False, "r"

        def refresh(self, request):
            raise RefreshError("invalid_grant")

    monkeypatch.setattr(
        gcreds.Credentials, "from_authorized_user_file", classmethod(lambda cls, p, s: Stale())
    )
    with pytest.raises(gmailapi.NeedsConsent, match=r"was rejected \(invalid_grant\)"):
        gmailapi.load_credentials(tmp_path, A, "readonly", interactive=False)
    assert capsys.readouterr().out == ""


def test_unattended_with_a_working_token_just_runs(tmp_path, no_browser, monkeypatch):
    from google.oauth2 import credentials as gcreds

    class Fine:
        expired, valid = False, True

        def has_scopes(self, scopes):
            return True

        def to_json(self):
            return "{}"

    gmailapi.token_file(tmp_path, "readonly").write_text("{}")
    monkeypatch.setattr(
        gcreds.Credentials, "from_authorized_user_file", classmethod(lambda cls, p, s: Fine())
    )
    assert isinstance(gmailapi.load_credentials(tmp_path, A, "readonly", interactive=False), Fine)


@pytest.fixture
def needs_consent(home, monkeypatch):
    seen = []

    def load(folder, account, scope, renew=False, interactive=True):
        seen.append(interactive)
        raise gmailapi.NeedsConsent(f"{account}: needs signing in")

    monkeypatch.setattr(gmailapi, "load_credentials", load)
    return seen


def backup_main(*args):
    return commands.main(["backup", "--config", "config", *args], prog="mailcellar")


def runs_of(account):
    con = sqlite3.connect(Path.cwd() / "data" / account / "meta.sqlite")
    try:
        return con.execute("SELECT command, status, reason FROM runs ORDER BY run_id").fetchall()
    finally:
        con.close()


def test_an_unattended_backup_that_cannot_sign_in_fails_and_is_recorded(needs_consent):
    before = runs_of(A)
    with pytest.raises(SystemExit, match=f"error: {A}: needs signing in"):
        backup_main("--unattended", A)
    assert needs_consent == [False]  # asked not to open a browser
    assert runs_of(A) == [*before, ("backup", "error", "needs-consent")]


def test_nothing_is_recorded_for_an_account_with_no_backup_yet(needs_consent, tmp_path):
    new = "c@z.com"
    (tmp_path / "config" / "cc").mkdir()
    (tmp_path / "config" / "accounts.toml").write_text(f'cc = "{new}"\n')
    with pytest.raises(SystemExit):
        backup_main("--unattended", new)
    assert not (tmp_path / "data" / new).exists()


def test_one_account_failing_does_not_stop_the_others(home, monkeypatch, capsys):
    def load(folder, account, scope, renew=False, interactive=True):
        if account == A:
            raise gmailapi.NeedsConsent(f"{account}: needs signing in")
        return object()

    monkeypatch.setattr(gmailapi, "load_credentials", load)
    monkeypatch.setattr(gmailapi, "GmailAccount", lambda account, creds: FakeGmail({}))
    assert backup_main("--unattended") == 1
    captured = capsys.readouterr()
    assert f"error: {A}: {A}: needs signing in" in captured.err  # the account, then the message
    assert f"── {B} ──" in captured.out
    assert runs_of(A)[-1] == ("backup", "error", "needs-consent")


def test_report_health_shows_the_run_that_could_not_sign_in(needs_consent, capsys):
    with pytest.raises(SystemExit):
        backup_main("--unattended", A)
    capsys.readouterr()
    assert cli.main(["report", "health"]) == 0
    row = next(
        line
        for line in capsys.readouterr().out.splitlines()
        if "needs-consent" in line and line.split()[:1] == [A]
    )
    assert "error" in row


def test_a_normal_backup_stays_interactive(needs_consent):
    with pytest.raises(SystemExit):
        backup_main(A)
    assert needs_consent == [True]
    assert runs_of(A)[-1][1] == "error"  # recorded either way: the run did fail


def test_schedule_is_a_command_and_documented_in_the_help(home, capsys):
    assert "schedule" in cli.COMMANDS and "mailcellar schedule" in cli.__doc__
    with pytest.raises(SystemExit):
        cli.main(["schedule", "--help"])
    out = capsys.readouterr().out
    assert out.startswith("usage: mailcellar schedule ") and "install" in out and "remove" in out
