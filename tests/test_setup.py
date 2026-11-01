import json
import os

import pytest

from mailcellar import cli
from mailcellar.setup import cli as setup

CLIENT = {
    "installed": {
        "client_id": "id.apps.googleusercontent.com",
        "client_secret": "not-a-real-secret",
        "auth_uri": "https://accounts.example.com/auth",
        "token_uri": "https://accounts.example.com/token",
    }
}


def client_json(data=CLIENT) -> str:
    return json.dumps(data)


@pytest.fixture(autouse=True)
def in_tmp(tmp_path, monkeypatch):
    """conftest points the config folder at ./config; make that the test's own."""
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def download(tmp_path):
    path = tmp_path / "client_secret_1.apps.googleusercontent.com.json"
    path.write_text(client_json())
    return path


@pytest.fixture
def auth_calls(monkeypatch):
    """Stand in for `mailcellar auth`: record its arguments, and succeed."""
    calls = []
    monkeypatch.setattr(
        "mailcellar.setup.auth.main", lambda argv, prog=None: calls.append(argv) or None
    )
    return calls


def test_check_client_accepts_a_desktop_client():
    setup.check_client(client_json())


@pytest.mark.parametrize(
    "text, message",
    [
        ("not json", "not a JSON file"),
        ("[]", "not an OAuth client file"),
        ('{"web": {"client_id": "x"}}', "Web application client"),
        ('{"other": {}}', "no .installed. section"),
        ('{"installed": {"client_id": "x"}}', "incomplete: no client_secret, auth_uri, token_uri"),
    ],
)
def test_check_client_refuses_other_files(text, message):
    with pytest.raises(setup.SetupError, match=message):
        setup.check_client(text)


def test_pick_name():
    entries = {"alex": "alex@example.com"}
    assert setup.pick_name({}, "Alex.Lee@example.org", None) == "alex-lee"
    assert setup.pick_name(entries, "alex@example.org", None) == "alex-example"  # taken: domain
    assert setup.pick_name({**entries, "alex-example": "x@y.z"}, "alex@example.org", None) == (
        "alex-2"
    )
    assert setup.pick_name(entries, "ALEX@example.com", None) == "alex"  # already configured
    assert setup.pick_name(entries, "alex@example.org", "work") == "work"
    with pytest.raises(setup.SetupError, match="already used for alex@example.com"):
        setup.pick_name(entries, "alex@example.org", "alex")
    with pytest.raises(setup.SetupError, match="already configured as 'alex'"):
        setup.pick_name(entries, "alex@example.com", "other")
    with pytest.raises(setup.SetupError, match="letters, digits"):
        setup.pick_name({}, "alex@example.org", "a.b")


def test_accounts_entries_and_add_account():
    assert setup.accounts_entries("") == {}
    assert setup.accounts_entries('work = "alex@example.org"\n') == {"work": "alex@example.org"}
    with pytest.raises(setup.SetupError, match="not valid TOML"):
        setup.accounts_entries("work = ")
    with pytest.raises(setup.SetupError, match="not an address"):
        setup.accounts_entries("work = 3")
    # the file is only appended to: comments and spacing survive, a missing newline is added
    assert setup.add_account("# mine\nwork = 'a@example.org'", "home", "b@example.com") == (
        "# mine\nwork = 'a@example.org'\nhome = \"b@example.com\"\n"
    )
    assert setup.add_account("", "home", "b@example.com") == 'home = "b@example.com"\n'


def test_latest_download(tmp_path):
    assert setup.latest_download(tmp_path / "missing") is None
    assert setup.latest_download(tmp_path) is None
    old, new = tmp_path / "client_secret_old.json", tmp_path / "client_secret_new.json"
    old.write_text("{}")
    new.write_text("{}")
    os.utime(old, (1, 1))
    (tmp_path / "other.json").write_text("{}")
    assert setup.latest_download(tmp_path) == new


def test_parse_path(tmp_path):
    assert setup.parse_path(f"'{tmp_path}/my file.json'") == tmp_path / "my file.json"
    assert setup.parse_path(f"{tmp_path}/my\\ file.json") == tmp_path / "my file.json"
    assert setup.parse_path("  ~/x.json ").name == "x.json"
    assert not str(setup.parse_path("~/x.json")).startswith("~")


def test_setup_adds_an_account_and_signs_in(tmp_path, download, auth_calls, capsys):
    assert cli.main(["setup", "alex@example.org", "--client", str(download)]) == 0
    config = tmp_path / "config"
    assert (config / "accounts.toml").read_text() == 'alex = "alex@example.org"\n'
    client = config / "alex" / "client_secrets.json"
    assert json.loads(client.read_text()) == CLIENT
    assert client.stat().st_mode & 0o777 == 0o600
    assert auth_calls == [["alex", "--scope", "readonly", "--config", str(config)]]
    out = capsys.readouterr().out
    assert "mailcellar backup alex" in out
    assert "not-a-real-secret" not in out


def test_setup_without_sign_in_and_with_a_name(tmp_path, download, auth_calls, capsys):
    assert cli.main(["setup", "alex@example.org", "--name", "work", "--client", str(download),
                     "--no-auth"]) == 0  # fmt: skip
    assert auth_calls == []
    assert (tmp_path / "config" / "work" / "client_secrets.json").is_file()
    assert "mailcellar auth work --scope readonly" in capsys.readouterr().out


def test_setup_asks_for_what_is_missing(tmp_path, download, auth_calls, monkeypatch, capsys):
    answers = iter(["alex@example.org", str(download)])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    assert cli.main(["setup"]) == 0
    out = capsys.readouterr().out  # the steps are shown, as there is no --client
    assert all(url in out.split() for _what, url in setup.CONSOLE)
    assert (tmp_path / "config" / "alex" / "client_secrets.json").is_file()


def test_setup_offers_the_latest_download(tmp_path, download, auth_calls, monkeypatch):
    downloads = tmp_path / "home" / "Downloads"
    downloads.mkdir(parents=True)
    (downloads / "client_secret_x.json").write_text(client_json())
    prompts = []
    monkeypatch.setattr("builtins.input", lambda p: prompts.append(p) or "")  # accept defaults
    assert cli.main(["setup", "alex@example.org"]) == 0
    assert str(downloads / "client_secret_x.json") in prompts[0]
    assert (tmp_path / "config" / "alex" / "client_secrets.json").is_file()


def test_setup_reuses_another_accounts_client(tmp_path, download, auth_calls, monkeypatch):
    cli.main(["setup", "alex@example.org", "--client", str(download), "--no-auth"])
    monkeypatch.setattr("builtins.input", lambda p: "")  # Enter at the path prompt
    assert cli.main(["setup", "alex@example.com", "--no-auth"]) == 0
    config = tmp_path / "config"
    assert (config / "accounts.toml").read_text() == (
        'alex = "alex@example.org"\nalex-example = "alex@example.com"\n'
    )
    assert json.loads((config / "alex-example" / "client_secrets.json").read_text()) == CLIENT


def test_setup_twice_changes_nothing(tmp_path, download, auth_calls):
    args = ["setup", "alex@example.org", "--client", str(download), "--no-auth"]
    cli.main(args)
    before = (tmp_path / "config" / "accounts.toml").read_text()
    assert cli.main(args) == 0
    assert (tmp_path / "config" / "accounts.toml").read_text() == before


def test_setup_keeps_a_different_client_unless_forced(tmp_path, download, auth_calls):
    args = ["setup", "alex@example.org", "--client", str(download), "--no-auth"]
    cli.main(args)
    other = tmp_path / "client_secret_2.json"
    other.write_text(client_json({"installed": {**CLIENT["installed"], "client_id": "other"}}))
    with pytest.raises(SystemExit, match="holds a different client"):
        cli.main(["setup", "alex@example.org", "--client", str(other), "--no-auth"])
    assert cli.main(["setup", "alex@example.org", "--client", str(other), "--no-auth",
                     "--force"]) == 0  # fmt: skip
    client = tmp_path / "config" / "alex" / "client_secrets.json"
    assert json.loads(client.read_text())["installed"]["client_id"] == "other"


def test_setup_refuses_a_wrong_client_before_writing(tmp_path, auth_calls):
    web = tmp_path / "web.json"
    web.write_text(json.dumps({"web": {"client_id": "x"}}))
    with pytest.raises(SystemExit, match="Web application client"):
        cli.main(["setup", "alex@example.org", "--client", str(web)])
    assert not (tmp_path / "config").exists()
    with pytest.raises(SystemExit, match="no such file"):
        cli.main(["setup", "alex@example.org", "--client", str(tmp_path / "nope.json")])
    with pytest.raises(SystemExit, match="not an email address"):
        cli.main(["setup", "alex", "--client", str(web)])
    assert auth_calls == []


def test_setup_keeps_existing_accounts_file(tmp_path, download, auth_calls):
    config = tmp_path / "config"
    config.mkdir()
    (config / "accounts.toml").write_text('# mine\nwork = "alex@example.org"')
    cli.main(["setup", "alex@example.com", "--client", str(download), "--no-auth"])
    assert (config / "accounts.toml").read_text() == (
        '# mine\nwork = "alex@example.org"\nalex = "alex@example.com"\n'
    )


def test_setup_reports_a_failed_sign_in(tmp_path, download, monkeypatch, capsys):
    def failing(argv, prog=None):
        raise SystemExit(1)

    monkeypatch.setattr("mailcellar.setup.auth.main", failing)
    assert cli.main(["setup", "alex@example.org", "--client", str(download)]) == 1
    out = capsys.readouterr().out
    assert "The account is set up; to finish: mailcellar auth alex --scope readonly" in out
    assert (tmp_path / "config" / "alex" / "client_secrets.json").is_file()


def test_setup_without_a_terminal_asks_for_arguments(monkeypatch):
    def eof(_prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    with pytest.raises(SystemExit, match="pass it on the command line"):
        cli.main(["setup"])


# ── moving an account backed up over Gmail's IMAP to the Gmail API ──────────


@pytest.fixture
def over_imap(tmp_path):
    """alex, backed up over IMAP: imap.toml and its password in its config folder."""
    folder = tmp_path / "config" / "alex"
    folder.mkdir(parents=True)
    (tmp_path / "config" / "accounts.toml").write_text('alex = "alex@example.org"\n')
    (folder / "imap.toml").write_text('host = "imap.gmail.com"\n')
    (folder / "imap-password").write_text("secret\n")
    return folder


@pytest.fixture
def signs_in(monkeypatch):
    """Stand in for the Gmail sign-in: record who signed in, and succeed."""
    from mailcellar import gmailapi

    who = []
    monkeypatch.setattr(gmailapi, "load_credentials", lambda folder, account, scope, **kw: scope)
    monkeypatch.setattr(gmailapi, "GmailAccount", lambda account, creds: who.append(account))
    return who


def imap_backup(provider_ids):
    """A backup of alex made over IMAP, holding Gmail's IDs or ones made from headers."""
    from mailcellar.backup import sidecar
    from mailcellar.paths import backup_folder

    con = sidecar.open_meta(backup_folder("alex@example.org"))
    sidecar.set_state(con, "provider", "imap")
    if provider_ids == "gmail":
        sidecar.set_state(con, "id_kind", "gmail")
    con.commit()
    con.close()


def test_setup_moves_an_imap_account_to_the_gmail_api(over_imap, download, signs_in, auth_calls):
    imap_backup("gmail")
    assert cli.main(["setup", "alex@example.org", "--client", str(download)]) == 0
    assert signs_in == ["alex@example.org"] and auth_calls == []  # auth skips IMAP accounts
    assert (over_imap / "client_secrets.json").is_file()
    assert not (over_imap / "imap.toml").exists() and not (over_imap / "imap-password").exists()


def test_an_imap_backup_of_header_ids_isnt_moved_to_the_api(over_imap, download, signs_in):
    imap_backup("imap")
    with pytest.raises(SystemExit, match="IDs made from each message's headers"):
        cli.main(["setup", "alex@example.org", "--client", str(download)])
    assert not (over_imap / "client_secrets.json").exists() and signs_in == []
    assert (over_imap / "imap.toml").is_file()


def test_a_move_to_the_api_needs_the_sign_in(over_imap, download, signs_in):
    with pytest.raises(SystemExit, match="leave out --no-auth"):
        cli.main(["setup", "alex@example.org", "--client", str(download), "--no-auth"])
    assert (over_imap / "imap.toml").is_file() and signs_in == []


def test_a_failed_sign_in_leaves_the_account_on_imap(over_imap, download, monkeypatch, capsys):
    from mailcellar import gmailapi

    def refused(*args, **kwargs):
        raise gmailapi.GmailError("signed in as someone@example.com, expected alex@example.org")

    monkeypatch.setattr(gmailapi, "load_credentials", refused)
    assert cli.main(["setup", "alex@example.org", "--client", str(download)]) == 1
    assert "still backed up over IMAP" in capsys.readouterr().out
    assert (over_imap / "imap.toml").is_file() and (over_imap / "imap-password").is_file()
