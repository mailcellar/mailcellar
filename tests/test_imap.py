"""IMAP accounts (#108, step 2 #111): their settings, and what may be done with them."""

from datetime import UTC, datetime

import pytest

from mailcellar import cli, gmailapi
from mailcellar.backup import schedule

ADDRESS = "alex@example.org"


@pytest.fixture
def imap_account(tmp_path, monkeypatch):
    """A config root with one IMAP account, `home`."""
    root = tmp_path / "config"
    (root / "home").mkdir(parents=True)
    (root / "accounts.toml").write_text(f'home = "{ADDRESS}"\n')
    (root / "home" / gmailapi.IMAP_SETTINGS).write_text('host = "imap.example.org"\n')
    monkeypatch.setenv("MAILCELLAR_CONFIG", str(root))
    return root, root / "home"


# ── settings ─────────────────────────────────────────────────────────────────


def test_a_gmail_account_has_no_imap_settings(tmp_path):
    assert gmailapi.imap_settings(tmp_path, ADDRESS) is None


def test_imap_settings_default_to_port_993_and_the_address(imap_account):
    _root, folder = imap_account
    assert gmailapi.imap_settings(folder, ADDRESS) == gmailapi.ImapSettings(
        "imap.example.org", 993, ADDRESS
    )
    ca = folder / "ca.pem"
    ca.write_text("a CA")
    (folder / gmailapi.IMAP_SETTINGS).write_text(
        f'host = "Mail.Example.org"\nport = 9993\nusername = "alex"\ncafile = "{ca}"\n'
    )
    assert gmailapi.imap_settings(folder, ADDRESS) == gmailapi.ImapSettings(
        "mail.example.org", 9993, "alex", ca
    )


@pytest.mark.parametrize(
    "text, problem",
    [
        ("host = 3\n", "is not a host name"),
        ('host = "imap.example.org/x"\n', "is not a host name"),
        ('host = "imap.example.org"\nport = 0\n', "is not a port number"),
        ('host = "imap.example.org"\nport = true\n', "is not a port number"),
        ('host = "imap.example.org"\nusername = ""\n', "is not a user name"),
        ("host = \n", "imap.toml"),
        ('host = "imap.example.org"\ncafile = "/no/such/ca.pem"\n', "is not a file"),
        ('host = "imap.example.org"\nreview = "label"\n', "is not one of keyword, folder"),
    ],
)
def test_unusable_imap_settings_say_what_is_wrong(imap_account, text, problem):
    _root, folder = imap_account
    (folder / gmailapi.IMAP_SETTINGS).write_text(text)
    with pytest.raises(gmailapi.GmailError, match=problem):
        gmailapi.imap_settings(folder, ADDRESS)


def test_the_review_mode_defaults_to_keyword(imap_account):
    _root, folder = imap_account
    assert gmailapi.imap_settings(folder, ADDRESS).review == "keyword"
    (folder / gmailapi.IMAP_SETTINGS).write_text('host = "imap.example.org"\nreview = "folder"\n')
    assert gmailapi.imap_settings(folder, ADDRESS).review == "folder"


def test_an_imap_account_needs_no_oauth_client(imap_account):
    root, folder = imap_account
    assert gmailapi.find_config_folder(root, ADDRESS) == folder


# ── what may be done with it ─────────────────────────────────────────────────


def test_config_and_schedule_status_show_the_imap_server(imap_account, capsys):
    root, folder = imap_account
    assert cli.main(["config"]) == 0
    assert "imap (imap.example.org)" in capsys.readouterr().out
    (folder / gmailapi.IMAP_SETTINGS).write_text("port = 1\n")
    assert cli.main(["config"]) == 0
    assert "imap (can't be used:" in capsys.readouterr().out
    note, attention = schedule.token_note(root, ADDRESS, datetime.now(UTC))
    assert "IMAP password" in note and not attention


def test_auth_has_nothing_to_do_for_an_imap_account(imap_account, capsys, monkeypatch):
    monkeypatch.setattr(gmailapi, "load_credentials", lambda *a, **k: pytest.fail("signed in"))
    cli.main(["auth", "--scope", "readonly"])
    assert "an IMAP account, signing in with its password" in capsys.readouterr().out
