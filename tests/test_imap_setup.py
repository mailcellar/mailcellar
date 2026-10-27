"""setup ADDRESS --imap HOST: an IMAP account from its server and password."""

import tomllib

import pytest

from fake_imap import FakeImap
from mailcellar import cli, gmailapi, imap
from mailcellar.setup import cli as setup

ADDRESS = "alex@example.org"


@pytest.fixture
def server(tmp_path, monkeypatch):
    s = FakeImap()
    s.add_folder("Trash", special=["\\Trash"])
    s.add("INBOX", msgid="<a@x>", flags={"\\Seen"})
    monkeypatch.setattr(imap.ImapAccount, "_connect_ssl", lambda self: s)
    monkeypatch.setattr(setup.getpass, "getpass", lambda prompt: "secret")
    monkeypatch.setenv("MAILCELLAR_CONFIG", str(tmp_path / "config"))
    return s


def run(tmp_path, *args):
    return setup.main(
        [ADDRESS, "--imap", "imap.example.org", *args, "--config", str(tmp_path / "config")]
    )


def test_setup_signs_in_then_writes_the_account(server, tmp_path, capsys):
    assert run(tmp_path, "--name", "home") == 0
    folder = tmp_path / "config" / "home"
    assert (folder / gmailapi.IMAP_SETTINGS).read_text() == 'host = "imap.example.org"\n'
    password = folder / gmailapi.IMAP_PASSWORD
    assert password.read_text() == "secret\n" and password.stat().st_mode & 0o777 == 0o600
    assert f'home = "{ADDRESS}"' in (tmp_path / "config" / "accounts.toml").read_text()
    out = capsys.readouterr().out
    assert "folders backed up: 1 (INBOX)" in out and "read, not backed up: TRASH" in out
    assert "offload, later: possible" in out  # MOVE named only after LOGIN, as Dovecot does
    assert "secret" not in out
    assert server.commands[-1] == ("LOGOUT",)  # setup's session ended, not left open
    assert cli.main(["backup", "home"]) == 0  # what setup wrote is what backup reads


def test_a_refused_login_writes_nothing(server, tmp_path):
    server.password = "other"
    with pytest.raises(
        SystemExit, match=r"refused the login: \[AUTHENTICATIONFAILED\].*nothing was written"
    ):
        run(tmp_path)
    assert not (tmp_path / "config").exists()


def test_no_auth_writes_without_connecting(server, tmp_path):
    assert run(tmp_path, "--no-auth", "--port", "9993", "--username", "alex") == 0
    toml = (tmp_path / "config" / "alex" / gmailapi.IMAP_SETTINGS).read_text()
    assert toml == 'host = "imap.example.org"\nport = 9993\nusername = "alex"\n'
    assert server.commands == []


def test_another_server_needs_force(server, tmp_path):
    assert run(tmp_path, "--no-auth") == 0
    with pytest.raises(SystemExit, match="holds a different imap.toml; --force"):
        setup.main(
            [
                ADDRESS,
                "--imap",
                "other.example.org",
                "--no-auth",
                "--config",
                str(tmp_path / "config"),
            ]
        )
    assert (
        setup.main(
            [
                ADDRESS,
                "--imap",
                "other.example.org",
                "--no-auth",
                "--force",
                "--config",
                str(tmp_path / "config"),
            ]
        )
        == 0
    )


@pytest.mark.parametrize(
    "args, problem",
    [
        (["--port", "0"], "is not a port number"),
        (["--client", "x.json"], "neither --client nor --service-account"),
        (["--cafile", "/no/such/ca.pem"], "is not a file"),
    ],
)
def test_bad_options_are_refused_before_asking(server, tmp_path, monkeypatch, args, problem):
    monkeypatch.setattr(setup.getpass, "getpass", lambda prompt: pytest.fail("asked"))
    with pytest.raises(SystemExit, match=problem):
        run(tmp_path, *args)


def test_server_report_says_when_offload_wont_be_possible():
    folders = (imap.Folder("INBOX", "INBOX"), imap.Folder("Trash", "TRASH"))
    info = imap.ServerInfo(frozenset({"IMAP4REV1"}), folders, frozenset({"TRASH"}))
    assert "not on this server" in setup.server_report(info)[-1]
    # A Trash known only by its name isn't one offloading may move mail into.
    named = imap.ServerInfo(frozenset({"MOVE"}), folders, frozenset({"SENT"}))
    assert not named.can_offload
    assert imap.ServerInfo(frozenset({"MOVE"}), folders, frozenset({"TRASH"})).can_offload
    by_name = imap.ServerInfo(
        frozenset(), (imap.Folder("INBOX", "INBOX"), imap.Folder("Spam", "SPAM")), frozenset()
    )
    report = "\n".join(setup.server_report(by_name))
    assert "known by name (the server flags none): 'Spam' as SPAM; rename" in report
    drafts = imap.ServerInfo(frozenset(), (imap.Folder("Drafts", "DRAFT"),), frozenset())
    report = "\n".join(setup.server_report(drafts))
    assert "'Drafts' as DRAFT" in report and "rename" not in report  # only relabelled
    settings = gmailapi.ImapSettings("imap.example.org", 993, ADDRESS)
    assert setup.imap_toml(settings, ADDRESS) == 'host = "imap.example.org"\n'
    folder_mode = gmailapi.ImapSettings("imap.example.org", 993, ADDRESS, review="folder")
    assert setup.imap_toml(folder_mode, ADDRESS).endswith('review = "folder"\n')


@pytest.mark.parametrize("value", ['a"b\\c', "tab\there", "del\x7f", "émoji 😀", "line\nbreak"])
def test_imap_toml_is_toml_whatever_the_values(tmp_path, value):
    settings = gmailapi.ImapSettings("imap.example.org", 993, value, tmp_path / value)
    data = tomllib.loads(setup.imap_toml(settings, ADDRESS))
    assert data["username"] == value and data["cafile"] == str(tmp_path / value)


def test_a_relative_cafile_is_kept_as_an_absolute_path(server, tmp_path, monkeypatch):
    (tmp_path / "ca.pem").write_text("a CA")
    monkeypatch.chdir(tmp_path)
    assert run(tmp_path, "--no-auth", "--cafile", "ca.pem") == 0
    toml = tomllib.loads((tmp_path / "config" / "alex" / gmailapi.IMAP_SETTINGS).read_text())
    assert toml["cafile"] == str((tmp_path / "ca.pem").resolve())


def test_no_terminal_to_ask_the_password_is_an_error(server, tmp_path, monkeypatch):
    def eof(prompt):
        raise EOFError

    monkeypatch.setattr(setup.getpass, "getpass", eof)
    with pytest.raises(SystemExit, match="nothing to read"):
        run(tmp_path)


def test_a_gmail_account_isnt_turned_into_an_imap_one(server, tmp_path):
    folder = tmp_path / "config" / "alex"
    folder.mkdir(parents=True)
    (tmp_path / "config" / "accounts.toml").write_text(f'alex = "{ADDRESS}"\n')
    (folder / "token-readonly.json").write_text("{}")
    with pytest.raises(SystemExit, match="set up as a Gmail account"):
        run(tmp_path, "--force")
    assert not (folder / gmailapi.IMAP_SETTINGS).exists()


def test_the_review_mode_is_set_and_kept_when_setting_up_again(server, tmp_path):
    assert run(tmp_path, "--no-auth", "--review", "folder") == 0
    toml = tmp_path / "config" / "alex" / gmailapi.IMAP_SETTINGS
    assert 'review = "folder"' in toml.read_text()
    assert run(tmp_path, "--no-auth") == 0  # a new password, say: no --force needed
    assert 'review = "folder"' in toml.read_text()
    assert run(tmp_path, "--no-auth", "--review", "keyword", "--force") == 0
    assert "review" not in toml.read_text()  # the default isn't written


def test_setting_up_again_keeps_every_setting_not_given(server, tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("a CA")
    assert (
        run(tmp_path, "--no-auth", "--port", "9993", "--username", "alex", "--cafile", str(ca)) == 0
    )
    toml = tmp_path / "config" / "alex" / gmailapi.IMAP_SETTINGS
    before = toml.read_text()
    assert run(tmp_path, "--no-auth") == 0  # a new password: nothing else changes
    assert toml.read_text() == before


@pytest.mark.parametrize("text", ["host = \n", 'host = "imap.example.org"\nreview = "Folder"\n'])
def test_an_unusable_imap_toml_keeps_nothing(server, tmp_path, text):
    folder = tmp_path / "config" / "alex"
    folder.mkdir(parents=True)
    (folder / gmailapi.IMAP_SETTINGS).write_text(text)
    assert setup.kept_settings(folder, ADDRESS) == {}
    assert setup.kept_settings(tmp_path / "nowhere", ADDRESS) == {}


def test_imap_options_need_imap(tmp_path):
    with pytest.raises(SystemExit, match="--port, --username: only with --imap"):
        setup.main([ADDRESS, "--port", "9993", "--username", "alex", "--no-auth"])
