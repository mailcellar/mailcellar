"""The mailcellar command, and where config and data are found."""

import pytest

from mailcellar import cli, gmailapi
from mailcellar.backup import commands
from mailcellar.paths import CONFIG_ENV, config_root, config_setting
from test_backup import FakeGmail, raw_response

A, B = "a@x.com", "b@y.com"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """Two configured accounts with backups; a's config has a token and a client."""
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "config"
    config.mkdir()
    (config / "accounts.toml").write_text(f'aa = "{A}"\nbb = "{B}"\n')
    (config / "aa").mkdir()
    (config / "aa" / "client_secrets.json").write_text('{"secret": "CLIENT-SECRET"}')
    gmailapi.token_file(config / "aa", "modify").write_text('{"token": "SECRET-TOKEN"}')
    gmails = {}
    for account in (A, B):
        gmails[account] = FakeGmail({f"{account[0]}1": raw_response(f"{account[0]}1")})
        commands.backup(gmails[account], account, tmp_path / "data" / account)

    def connect(args):
        account, _folder = gmailapi.resolve_account(config, args.account)
        if account in getattr(connect, "broken", ()):
            raise gmailapi.GmailError(f"{account}: token revoked")
        return account, tmp_path / "data" / account, gmails[account]

    monkeypatch.setattr(commands, "connect", connect)
    return tmp_path, connect


def test_help_and_unknown_commands(capsys):
    assert cli.main(["--help"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: mailcellar <command>")
    assert all(f"mailcellar {c}" in out for c in cli.COMMANDS)
    assert cli.main([]) == 2  # help, but a non-zero exit for scripts
    assert cli.main(["bakup"]) == 2
    assert "Did you mean 'backup'?" in capsys.readouterr().err


def test_count_runs_every_account_by_default(setup, capsys):
    assert cli.main(["count"]) == 0
    out = capsys.readouterr().out
    assert f"── {A} ──" in out and f"── {B} ──" in out
    assert cli.main(["count", "bb"]) == 0  # by config name
    out = capsys.readouterr().out
    assert B in out and A not in out and "──" not in out


def test_one_failing_account_does_not_stop_the_others(setup, capsys):
    _tmp, connect = setup
    connect.broken = {A}
    assert cli.main(["count"]) == 1
    captured = capsys.readouterr()
    assert f"error: {A}: {A}: token revoked" in captured.err
    assert f"── {B} ──" in captured.out and "In backup" in captured.out
    with pytest.raises(SystemExit, match="token revoked"):  # alone, it fails outright
        cli.main(["count", A])


def test_folder_needs_one_account(setup):
    with pytest.raises(SystemExit, match="exactly one account"):
        cli.main(["backup", "--folder", "x"])


def test_config_shows_paths_and_accounts_but_no_secrets(setup, capsys):
    tmp_path, _connect = setup
    commands.backup(FakeGmail({"c1": raw_response("c1")}), "c@z.com", tmp_path / "data" / "c@z.com")
    assert cli.main(["config"]) == 0
    out = capsys.readouterr().out
    assert "SECRET-TOKEN" not in out and "CLIENT-SECRET" not in out
    rows = {}
    for line in out.splitlines():
        words = line.split()
        if words and "@" in words[0]:
            rows[words[0]] = line
    assert "aa" in rows[A] and "modify" in rows[A] and " yes " in rows[A]
    assert "bb" in rows[B] and "none" in rows[B] and " no " in rows[B]
    assert "not configured" in rows["c@z.com"]
    assert "$MAILCELLAR_CONFIG" in out  # the test fixture sets it


def test_config_shows_how_long_ago_each_token_was_authorised(setup, capsys):
    from datetime import UTC, datetime, timedelta

    tmp_path, _connect = setup
    token = tmp_path / "config" / "aa" / "dedup-token-readonly.json"
    ago = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    token.write_text(f'{{"token": "SECRET-TOKEN", "authorised_at": "{ago}"}}')
    assert cli.main(["config"]) == 0
    out = capsys.readouterr().out
    row = next(line for line in out.splitlines() if line.split()[:1] == [A])
    assert "readonly (3 days)" in row  # recorded: its age
    assert "modify (age unknown)" in row  # from before consent was recorded
    assert "SECRET-TOKEN" not in out
    assert "since consent was given" in out and "--renew" in out


def test_config_without_accounts_file(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["config"]) == 0
    out = capsys.readouterr().out
    assert "accounts.toml not found" in out and "missing" in out


def test_dispatch_to_the_other_tools(setup, capsys):
    with pytest.raises(SystemExit) as done:
        cli.main(["dedup", "--help"])
    assert done.value.code == 0 and "usage: mailcellar dedup" in capsys.readouterr().out
    assert cli.main(["report", "health"]) == 0
    assert capsys.readouterr().out.startswith(f"Backup health report — {A}, {B}")
    assert cli.main(["report", "senders", "aa", "--top", "3"]) == 0
    assert "Top" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="usage: mailcellar report senders"):
        cli.main(["report", "senders"])


def test_config_root_precedence(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(CONFIG_ENV)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    user = tmp_path / "xdg" / "mailcellar"
    assert config_setting() == (user, "default")  # nothing exists yet: the new place
    (tmp_path / "config").mkdir()
    assert config_setting()[0] == tmp_path / "config"  # the old place, while it is the only one
    user.mkdir(parents=True)
    assert config_root() == user  # once moved, the new place wins
    monkeypatch.setenv(CONFIG_ENV, str(tmp_path / "elsewhere"))
    assert config_setting() == (tmp_path / "elsewhere", f"${CONFIG_ENV}")


def test_verbose_and_quiet_set_the_log_level_anywhere_on_the_line(setup, capsys):
    import logging

    root = logging.getLogger("mailcellar")
    assert cli.main(["count", "-q"]) == 0
    assert root.level == logging.WARNING
    assert cli.main(["-v", "count", "aa"]) == 0
    assert root.level == logging.DEBUG
    assert cli.main(["count", "aa"]) == 0
    assert root.level == logging.INFO
    logging.getLogger("mailcellar.x").warning("disk nearly full")
    assert capsys.readouterr().err.endswith("warning: disk nearly full\n")


def test_backup_status_lines_are_log_output(setup, capsys):
    assert cli.main(["backup", A]) == 0
    captured = capsys.readouterr()
    assert "Using backup folder" in captured.err
    assert "Using backup folder" not in captured.out
    assert cli.main(["-q", "backup", A]) == 0
    captured = capsys.readouterr()
    assert "Using backup folder" not in captured.err + captured.out


def test_backup_listing_summary_is_log_output(setup, capsys):
    assert cli.main(["backup", A, "--full"]) == 0
    captured = capsys.readouterr()
    assert "Gmail has 1 messages" in captured.err
    assert "Gmail has 1 messages" not in captured.out
    assert cli.main(["-q", "backup", A, "--full"]) == 0
    captured = capsys.readouterr()
    assert "Gmail has" not in captured.err + captured.out


def test_fatal_errors_use_the_log_prefix(setup):
    with pytest.raises(SystemExit) as fatal:
        cli.main(["backup", "--folder", "x"])
    assert str(fatal.value).startswith("error: ")
