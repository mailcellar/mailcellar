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
    s.writable = True  # setup SELECTs INBOX to see whether it keeps keywords: checked below
    s.add_folder("Trash", special=["\\Trash"])
    s.add("INBOX", msgid="<a@x>", flags={"\\Seen"})
    monkeypatch.setattr(imap.ImapAccount, "_connect_ssl", lambda self: s)
    monkeypatch.setattr(setup.getpass, "getpass", lambda prompt: "secret")
    monkeypatch.setenv("MAILCELLAR_CONFIG", str(tmp_path / "config"))
    yield s
    assert_changed_nothing(s)  # setup never changes the server, whatever the test


def run(tmp_path, *args):
    return setup.main(
        [ADDRESS, "--imap", "imap.example.org", *args, "--config", str(tmp_path / "config")]
    )


def assert_changed_nothing(server):
    changes = {"STORE", "MOVE", "COPY", "CREATE", "APPEND", "EXPUNGE", "DELETE"}
    sent = {c[0] for c in server.commands} | {c[1] for c in server.commands if c[0] == "UID"}
    assert not sent & changes


def test_setup_signs_in_then_writes_the_account(server, tmp_path, capsys):
    assert run(tmp_path, "--name", "home") == 0
    folder = tmp_path / "config" / "home"
    assert (folder / gmailapi.IMAP_SETTINGS).read_text() == 'host = "imap.example.org"\n'
    password = folder / gmailapi.IMAP_PASSWORD
    assert password.read_text() == "secret\n" and password.stat().st_mode & 0o777 == 0o600
    assert f'home = "{ADDRESS}"' in (tmp_path / "config" / "accounts.toml").read_text()
    out = capsys.readouterr().out
    assert "folders backed up: 1 (INBOX)" in out and "read, not backed up: TRASH" in out
    assert "offload and dedup: possible" in out  # MOVE named only after LOGIN, as Dovecot does
    assert "review marks: a keyword (the server keeps them)" in out
    assert ("SELECT", '"INBOX"') in server.commands  # the check: a SELECT, changing nothing
    assert "restore: possible\n" in out
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
    assert "refused on this server" in setup.server_report(info)[-2]
    assert setup.server_report(info)[-1].endswith(
        "(uploads only: without MOVE, mail in its Trash stays there)"
    )
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


def gmail_account(tmp_path):
    """The account set up for the Gmail API: a token in its config folder."""
    folder = tmp_path / "config" / "alex"
    folder.mkdir(parents=True)
    (tmp_path / "config" / "accounts.toml").write_text(f'alex = "{ADDRESS}"\n')
    (folder / "token-readonly.json").write_text("{}")
    return folder


def test_a_gmail_account_isnt_moved_to_another_imap_server(server, tmp_path):
    folder = gmail_account(tmp_path)
    with pytest.raises(SystemExit, match="set up as a Gmail account.* isn't Gmail's"):
        run(tmp_path, "--force")
    assert not (folder / gmailapi.IMAP_SETTINGS).exists()
    with pytest.raises(SystemExit, match="without --no-auth"):
        run(tmp_path, "--force", "--no-auth")  # which server it is can't be known
    assert not (folder / gmailapi.IMAP_SETTINGS).exists()


def test_a_gmail_account_moves_to_imap_only_as_its_own_mailbox(server, tmp_path):
    folder = gmail_account(tmp_path)
    server.signed_in = (*server.signed_in, "X-GM-EXT-1")
    with pytest.raises(SystemExit, match="log in as alex@example.org, not other@example.org"):
        run(tmp_path, "--username", "other@example.org")
    assert not (folder / gmailapi.IMAP_SETTINGS).exists()
    assert not [c for c in server.commands if c[0] == "LOGIN"]  # refused before signing in


def test_a_gmail_account_moves_to_gmails_imap_server(server, tmp_path):
    folder = gmail_account(tmp_path)
    server.signed_in = (*server.signed_in, "X-GM-EXT-1")  # the same IDs as the Gmail API's
    assert run(tmp_path) == 0
    assert (folder / gmailapi.IMAP_SETTINGS).exists()


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


# ── review marks (whether the server keeps keywords) ────────────────────────


def test_setup_says_when_inbox_keeps_no_keywords(server, tmp_path, capsys):
    server.keywordless.add("INBOX")
    assert run(tmp_path) == 0  # said, never chosen: the account is set up as asked
    out = capsys.readouterr().out
    assert "review marks: the server keeps no keywords in INBOX, so marking would fail" in out
    assert f"mailcellar setup {ADDRESS} --review folder" in out
    assert "review" not in (tmp_path / "config" / "alex" / gmailapi.IMAP_SETTINGS).read_text()


def test_setup_checks_no_keywords_where_they_arent_used(server, tmp_path, capsys):
    assert run(tmp_path, "--review", "folder") == 0
    assert "review marks: a move into mailcellar/review/<label>" in capsys.readouterr().out
    assert ("SELECT", '"INBOX"') not in server.commands
    server.signed_in = ("IMAP4rev1",)  # no MOVE: offload is refused, so marks don't matter
    assert run(tmp_path, "--review", "keyword", "--force") == 0
    out = capsys.readouterr().out
    assert "review marks" not in out and ("SELECT", '"INBOX"') not in server.commands


def test_review_report_when_the_check_couldnt_say():
    line = setup.review_report("keyword", None, ADDRESS)
    assert line.endswith("a keyword (not known whether the server keeps them)")


# ── Microsoft: OAuth only ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "address, host",
    [
        ("alex@outlook.com", "outlook.office365.com"),
        ("alex@hotmail.com", "imap.example.org"),  # whatever server was typed
        ("alex@example.org", "Outlook.Office365.com"),  # Microsoft 365, own domain
    ],
)
def test_microsoft_is_refused_before_the_password_is_asked(
    server, tmp_path, monkeypatch, address, host
):
    monkeypatch.setattr(setup.getpass, "getpass", lambda prompt: pytest.fail("asked"))
    with pytest.raises(SystemExit, match="accepts only OAuth sign-in over IMAP"):
        setup.main([address, "--imap", host, "--config", str(tmp_path / "config")])
    assert not (tmp_path / "config").exists() and server.commands == []


def test_other_servers_are_not_microsoft():
    assert setup.microsoft_refusal("alex@live.example", "imap.example.org") == ""
    assert setup.microsoft_refusal("alex@example.org", "outlook.example.org") == ""


# ── setup ACCOUNT --review MODE: imap.toml alone ─────────────────────────────


def review(tmp_path, account, mode):
    return setup.main([account, "--review", mode, "--config", str(tmp_path / "config")])


def test_review_mode_changes_without_the_password(server, tmp_path, monkeypatch, capsys):
    assert run(tmp_path, "--no-auth", "--port", "9993", "--name", "home") == 0
    toml = tmp_path / "config" / "home" / gmailapi.IMAP_SETTINGS
    password = (tmp_path / "config" / "home" / gmailapi.IMAP_PASSWORD).read_text()
    monkeypatch.setattr(setup.getpass, "getpass", lambda prompt: pytest.fail("asked"))
    assert review(tmp_path, "home", "folder") == 0
    assert toml.read_text() == 'host = "imap.example.org"\nport = 9993\nreview = "folder"\n'
    assert "review keyword → folder" in capsys.readouterr().out
    assert review(tmp_path, ADDRESS.upper(), "keyword") == 0  # by address, case ignored
    assert toml.read_text() == 'host = "imap.example.org"\nport = 9993\n'
    assert review(tmp_path, "home", "keyword") == 0
    assert "already keyword; nothing changed" in capsys.readouterr().out
    assert (tmp_path / "config" / "home" / gmailapi.IMAP_PASSWORD).read_text() == password
    assert toml.stat().st_mode & 0o777 == 0o600
    assert server.commands == []  # no sign-in


@pytest.mark.parametrize(
    "args, problem",
    [
        (["nobody", "--review", "folder"], "'nobody' is not a configured account"),
        (["gmail", "--review", "folder"], "is a Gmail account"),
        (["--review", "folder"], "needs the account"),
        (["home", "--review", "folder", "--name", "x"], "takes no --name"),
        (["home", "--review", "folder", "--port", "1"], "--port: only with --imap"),
    ],
)
def test_review_mode_change_refusals(server, tmp_path, args, problem):
    assert run(tmp_path, "--no-auth", "--name", "home") == 0
    config = tmp_path / "config"
    (config / "gmail").mkdir()
    with (config / "accounts.toml").open("a") as fh:
        fh.write('gmail = "someone@example.com"\n')
    before = (config / "home" / gmailapi.IMAP_SETTINGS).read_text()
    with pytest.raises(SystemExit, match=problem):
        setup.main([*args, "--config", str(config)])
    assert (config / "home" / gmailapi.IMAP_SETTINGS).read_text() == before


@pytest.mark.parametrize(
    "text, review, edited",
    [
        ('host = "h"\n', "folder", 'host = "h"\nreview = "folder"\n'),
        ('host = "h"', "folder", 'host = "h"\nreview = "folder"\n'),
        ('# mine\nreview = "folder"  # why\nport = 1\n', "keyword", "# mine\nport = 1\n"),
        ('host = "h"\n# review = "folder"\n', "keyword", 'host = "h"\n# review = "folder"\n'),
        ('  review="keyword"\nhost = "h"\n', "folder", 'review = "folder"\nhost = "h"\n'),
    ],
)
def test_with_review_changes_only_the_review_line(text, review, edited):
    assert setup.with_review(text, review) == edited


def test_review_mode_change_keeps_the_files_comments(server, tmp_path):
    assert run(tmp_path, "--no-auth", "--name", "home") == 0
    toml = tmp_path / "config" / "home" / gmailapi.IMAP_SETTINGS
    toml.write_text('# app password made in October\nhost = "imap.example.org"\n')
    assert review(tmp_path, "home", "folder") == 0
    assert toml.read_text() == (
        '# app password made in October\nhost = "imap.example.org"\nreview = "folder"\n'
    )


def test_a_review_line_it_cant_edit_is_left_alone(server, tmp_path):
    assert run(tmp_path, "--no-auth", "--name", "home") == 0
    toml = tmp_path / "config" / "home" / gmailapi.IMAP_SETTINGS
    toml.write_text('host = "imap.example.org"\n"review" = "keyword"\n')  # a quoted key
    with pytest.raises(SystemExit, match="couldn't change its review line; edit it by hand"):
        review(tmp_path, "home", "folder")
    assert '"review" = "keyword"' in toml.read_text()
