"""Workspace accounts read through a service account with domain-wide delegation (#103)."""

import json
from datetime import UTC, datetime

import pytest

from mailcellar import cli, gmailapi
from mailcellar.backup import schedule
from mailcellar.setup import cli as setup

ADDRESS = "alex@example.org"
KEY = {
    "type": "service_account",
    "client_email": "backup@project.iam.gserviceaccount.com",
    "client_id": "1234567890",
    "private_key": "-----BEGIN PRIVATE KEY-----\nnot a real key\n-----END PRIVATE KEY-----\n",
    "token_uri": "https://oauth2.googleapis.com/token",
}


class FakeCredentials:
    refused = None  # set to a message to make Google refuse the delegation
    offline = False  # set to make the token request fail to reach Google

    def __init__(self, info, scopes):
        self.info, self.scopes, self.subject = info, scopes, None

    @classmethod
    def from_service_account_info(cls, info, scopes):
        if "damaged" in info["private_key"]:
            raise ValueError("Unable to load PEM file")  # as google-auth says
        return cls(info, scopes)

    def with_subject(self, subject):
        self.subject = subject
        return self

    def refresh(self, request):
        from google.auth.exceptions import RefreshError, TransportError

        if FakeCredentials.offline:
            raise TransportError("Failed to resolve oauth2.googleapis.com")
        if FakeCredentials.refused:
            raise RefreshError(FakeCredentials.refused)


@pytest.fixture
def fake_google(monkeypatch):
    from google.oauth2 import service_account

    FakeCredentials.refused, FakeCredentials.offline = None, False
    monkeypatch.setattr(service_account, "Credentials", FakeCredentials)
    return FakeCredentials


@pytest.fixture
def configured(tmp_path):
    """A config root with the account listed and the domain's key installed, owner-only."""
    root = tmp_path / "config"
    (root / "alex").mkdir(parents=True)
    (root / "accounts.toml").write_text(f'alex = "{ADDRESS}"\n')
    key = gmailapi.service_account_path(root, "example.org")
    key.parent.mkdir()
    key.write_text(json.dumps(KEY))
    key.chmod(0o600)
    (root / "alex" / gmailapi.SERVICE_ACCOUNT_MARKER).write_text(f"{key.name}\n")  # set up with it
    return root, root / "alex", key


# ── the key ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("not json", "not a JSON file"),
        ('{"installed": {}}', "not a service-account key"),
        (json.dumps({**KEY, "private_key": None}), "no private_key"),
    ],
)
def test_only_a_service_account_key_is_accepted(text, error):
    with pytest.raises(gmailapi.GmailError, match=error):
        gmailapi.check_service_account(text)


def test_the_key_is_found_by_the_accounts_domain(configured):
    _root, folder, key = configured
    assert gmailapi.service_account_key(folder, ADDRESS) == key
    assert gmailapi.service_account_key(folder, "alex@example.com") is None  # another domain
    assert gmailapi.service_account_key(folder, "Alex@EXAMPLE.org") == key


def test_only_an_account_set_up_with_the_key_reads_through_it(configured, fake_google):
    root, _folder, _key = configured
    sam = root / "sam"  # same domain, set up with its own sign-in
    sam.mkdir()
    gmailapi.token_file(sam, "readonly").write_text("{}")
    assert gmailapi.service_account_key(sam, "sam@example.org") is None
    with pytest.raises(gmailapi.GmailError, match="has no OAuth client"):
        gmailapi.load_credentials(sam, "sam@example.org", "readonly", renew=True)  # own sign-in
    # Undo: without its marker, alex signs in for itself again.
    (root / "alex" / gmailapi.SERVICE_ACCOUNT_MARKER).unlink()
    assert gmailapi.service_account_key(root / "alex", ADDRESS) is None


# ── credentials ──────────────────────────────────────────────────────────────


def test_reads_go_through_the_key_as_the_account_and_read_only(configured, fake_google):
    _root, folder, _key = configured
    creds = gmailapi.load_credentials(folder, ADDRESS, "readonly", interactive=False)
    assert creds.subject == ADDRESS
    assert creds.scopes == [gmailapi.SCOPES["readonly"]]  # never more
    assert [p.name for p in folder.iterdir()] == [gmailapi.SERVICE_ACCOUNT_MARKER]  # no token


def test_anything_that_changes_gmail_needs_the_users_own_sign_in(configured, fake_google, capsys):
    _root, folder, _key = configured
    with pytest.raises(gmailapi.GmailError, match="needs the account's own sign-in"):
        gmailapi.load_credentials(folder, ADDRESS, "modify")
    assert "sign in" not in capsys.readouterr().out  # not asked to sign in, then refused


def test_a_key_others_can_read_is_refused(configured, fake_google):
    _root, folder, key = configured
    key.chmod(0o644)
    with pytest.raises(gmailapi.GmailError, match="chmod 600"):
        gmailapi.load_credentials(folder, ADDRESS, "readonly")


def test_a_refused_delegation_says_what_to_grant(configured, fake_google):
    _root, folder, _key = configured
    fake_google.refused = "unauthorized_client"
    with pytest.raises(gmailapi.GmailError) as exc:
        gmailapi.load_credentials(folder, ADDRESS, "readonly")
    message = str(exc.value)
    assert "Manage Domain Wide Delegation" in message and KEY["client_id"] in message
    assert gmailapi.SCOPES["readonly"] in message and "private_key" not in message


def test_an_account_with_only_a_key_is_configured(configured):
    root, folder, _key = configured
    assert gmailapi.find_config_folder(root, ADDRESS) == folder  # no OAuth client needed


# ── setup, config and schedule status ────────────────────────────────────────


def setup_run(*args):
    return setup.main(list(args))


def test_setup_installs_the_key_once_for_the_domain(tmp_path, capsys, fake_google):
    root = tmp_path / "config"
    source = tmp_path / "Downloads" / "project-key.json"
    source.parent.mkdir()
    source.write_text(json.dumps(KEY))
    source.chmod(0o644)  # as downloaded
    args = (ADDRESS, "--service-account", str(source), "--no-auth", "--config", str(root))
    assert setup_run(*args) == 0
    key = gmailapi.service_account_path(root, "example.org")
    assert json.loads(key.read_text()) == KEY and key.stat().st_mode & 0o777 == 0o600
    assert key.parent.stat().st_mode & 0o777 == 0o700
    assert (root / "accounts.toml").read_text() == f'alex = "{ADDRESS}"\n'
    out = capsys.readouterr().out
    assert "read through the service account" in out and "--client FILE adds" in out
    marker = root / "alex" / gmailapi.SERVICE_ACCOUNT_MARKER
    assert marker.read_text() == "example.org.json\n"  # this account reads through the key

    # An account that already has an OAuth client keeps it, and isn't told to add one.
    (root / "alex" / "client_secrets.json").write_text("{}")
    assert setup_run(*args) == 0
    out = capsys.readouterr().out
    assert "already listed" in out and "with the account's OAuth client" in out
    assert "--client FILE adds" not in out

    # A second address in the domain shares the key, and gets its own marker; one set up
    # without it doesn't. Another key is refused without --force.
    other = ("sam@example.org", "--service-account", str(source), "--no-auth")
    assert setup_run(*other, "--config", str(root)) == 0
    assert (root / "sam" / gmailapi.SERVICE_ACCOUNT_MARKER).is_file()
    (root / "kim").mkdir()
    assert gmailapi.service_account_key(root / "kim", "kim@example.org") is None
    source.write_text(json.dumps({**KEY, "client_id": "999"}))
    with pytest.raises(SystemExit, match="different service-account key"):
        setup_run(*other, "--config", str(root))
    assert setup_run(*other, "--force", "--config", str(root)) == 0
    assert json.loads(key.read_text())["client_id"] == "999"


def test_setup_refuses_a_file_that_isnt_a_key(tmp_path, fake_google):
    source = tmp_path / "client.json"
    source.write_text('{"installed": {}}')
    with pytest.raises(SystemExit, match="not a service-account key"):
        setup_run(ADDRESS, "--service-account", str(source), "--config", str(tmp_path / "c"))


def test_config_and_schedule_status_show_the_service_account(configured, monkeypatch, capsys):
    root, _folder, _key = configured
    monkeypatch.setenv("MAILCELLAR_CONFIG", str(root))
    assert cli.main(["config"]) == 0
    assert "readonly (service account)" in capsys.readouterr().out
    note, attention = schedule.token_note(root, ADDRESS, datetime.now(UTC))
    assert "service account" in note and not attention


# ── review findings: a damaged key, the domain, failures reported ──────────


def test_a_damaged_private_key_is_refused_before_it_is_installed(tmp_path, fake_google):
    source = tmp_path / "key.json"
    source.write_text(json.dumps({**KEY, "private_key": "damaged in copying"}))
    root = tmp_path / "config"
    with pytest.raises(SystemExit, match="private key can't be read"):
        setup_run(ADDRESS, "--service-account", str(source), "--config", str(root))
    assert not (root / gmailapi.SERVICE_ACCOUNTS_DIR).exists()  # nothing installed


@pytest.mark.parametrize("domain", ["../../somewhere", "x/y.org", "example..org", "localhost"])
def test_a_domain_that_isnt_a_host_name_has_no_key(tmp_path, domain, fake_google):
    with pytest.raises(gmailapi.GmailError, match="not a domain name"):
        gmailapi.service_account_path(tmp_path, domain)
    assert gmailapi.service_account_key(tmp_path / "alex", f"a@{domain}") is None
    source = tmp_path / "key.json"
    source.write_text(json.dumps(KEY))
    with pytest.raises(SystemExit, match="not a domain name"):
        setup_run(f"a@{domain}", "--service-account", str(source), "--config", str(tmp_path / "c"))


def test_a_network_failure_is_a_gmail_error_not_a_traceback(configured, fake_google):
    _root, folder, _key = configured
    fake_google.offline = True
    with pytest.raises(gmailapi.GmailError, match="couldn't reach Google") as exc:
        gmailapi.load_credentials(folder, ADDRESS, "readonly")
    assert not isinstance(exc.value, gmailapi.AccessRefused)  # not the admin's to fix


class FakeAccount:
    """Stands in for GmailAccount: its identity check, and the mail backup reads."""

    def __init__(self, account, creds):
        from test_backup import FakeGmail, raw_response

        assert creds.subject == account  # the key acting as the account
        self.inner = FakeGmail({"s1": raw_response("s1")})

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_setup_checks_the_mailbox_and_says_when_it_cant(
    configured, fake_google, monkeypatch, capsys, caplog
):
    root, _folder, key = configured
    monkeypatch.setattr(gmailapi, "GmailAccount", FakeAccount)
    args = (ADDRESS, "--service-account", str(key), "--config", str(root))
    assert setup_run(*args) == 0
    assert "OK." in capsys.readouterr().out
    fake_google.refused = "unauthorized_client"
    assert setup_run(*args) == 1
    assert "the check failed" in capsys.readouterr().out + caplog.text


def test_a_backup_reads_through_the_key(configured, fake_google, monkeypatch):
    from mailcellar.backup import gyb
    from mailcellar.paths import backup_folder

    monkeypatch.setattr(gmailapi, "GmailAccount", FakeAccount)
    assert cli.main(["backup", "alex", "--unattended"]) == 0
    assert [m.gmail_id for m in gyb.iter_backup(backup_folder(ADDRESS))] == ["s1"]


def test_an_unattended_backup_whose_key_is_refused_is_recorded(
    configured, fake_google, monkeypatch
):
    import sqlite3

    from mailcellar.backup import sidecar
    from mailcellar.paths import backup_folder

    monkeypatch.setattr(gmailapi, "GmailAccount", FakeAccount)
    assert cli.main(["backup", "alex", "--unattended"]) == 0  # a backup to record against
    fake_google.refused = "unauthorized_client"  # then the delegation is removed
    with pytest.raises(SystemExit, match="refused the service account"):
        cli.main(["backup", "alex", "--unattended"])
    con = sqlite3.connect(sidecar.meta_path(backup_folder(ADDRESS)))
    last = con.execute(
        "SELECT status, reason FROM runs WHERE command = 'backup' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    con.close()
    assert last == ("error", "access-refused")  # health and schedule status show it


def test_auth_renew_says_it_doesnt_apply_to_the_service_account(
    configured, fake_google, monkeypatch, capsys
):
    root, folder, _key = configured
    monkeypatch.setattr(gmailapi, "GmailAccount", FakeAccount)
    cli.main(["auth", "alex", "--scope", "readonly"])
    out = capsys.readouterr().out
    assert "OK, through the service account" in out and "--renew" not in out
    cli.main(["auth", "alex", "--scope", "readonly", "--renew"])
    out = capsys.readouterr().out
    assert "--renew doesn't apply" in out
    assert str(folder / gmailapi.SERVICE_ACCOUNT_MARKER) in out


def test_setup_checks_the_client_before_it_changes_the_key(tmp_path, fake_google, capsys):
    root = tmp_path / "config"
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    first.write_text(json.dumps(KEY))
    second.write_text(json.dumps({**KEY, "client_id": "999"}))
    base = (ADDRESS, "--no-auth", "--config", str(root))
    assert setup_run(*base, "--service-account", str(first)) == 0
    assert "key: added" in capsys.readouterr().out
    bad = tmp_path / "client.json"
    bad.write_text('{"web": {}}')  # a Web application client: refused
    with pytest.raises(SystemExit, match="Web application client"):
        setup_run(*base, "--service-account", str(second), "--client", str(bad), "--force")
    key = gmailapi.service_account_path(root, "example.org")
    assert json.loads(key.read_text())["client_id"] == KEY["client_id"]  # unchanged
    assert setup_run(*base, "--service-account", str(first)) == 0
    assert "key: kept" in capsys.readouterr().out
    assert setup_run(*base, "--service-account", str(second), "--force") == 0
    out = capsys.readouterr().out
    assert "key: replaced (every account in the domain set up with it" in out


def test_setup_creates_a_new_config_root_owner_only(tmp_path, fake_google):
    key = tmp_path / "key.json"
    key.write_text(json.dumps(KEY))
    client = tmp_path / "client.json"
    client.write_text(
        json.dumps(
            {"installed": {k: "x" for k in ("client_id", "client_secret", "auth_uri", "token_uri")}}
        )
    )
    for n, extra in enumerate((("--service-account", str(key)), ("--client", str(client)))):
        root = tmp_path / f"fresh{n}" / "mailcellar"
        assert setup_run(ADDRESS, *extra, "--no-auth", "--config", str(root)) == 0
        assert root.stat().st_mode & 0o777 == 0o700, extra[0]
