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

    def __init__(self, info, scopes):
        self.info, self.scopes, self.subject = info, scopes, None

    @classmethod
    def from_service_account_info(cls, info, scopes):
        return cls(info, scopes)

    def with_subject(self, subject):
        self.subject = subject
        return self

    def refresh(self, request):
        from google.auth.exceptions import RefreshError

        if FakeCredentials.refused:
            raise RefreshError(FakeCredentials.refused)


@pytest.fixture
def fake_google(monkeypatch):
    from google.oauth2 import service_account

    FakeCredentials.refused = None
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


# ── credentials ──────────────────────────────────────────────────────────────


def test_reads_go_through_the_key_as_the_account_and_read_only(configured, fake_google):
    _root, folder, _key = configured
    creds = gmailapi.load_credentials(folder, ADDRESS, "readonly", interactive=False)
    assert creds.subject == ADDRESS
    assert creds.scopes == [gmailapi.SCOPES["readonly"]]  # never more
    assert list(folder.iterdir()) == []  # no token stored, nothing to renew


def test_anything_that_changes_gmail_needs_the_users_own_sign_in(configured, fake_google):
    _root, folder, _key = configured
    with pytest.raises(gmailapi.GmailError, match="needs the account's own sign-in"):
        gmailapi.load_credentials(folder, ADDRESS, "modify")


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


def test_setup_installs_the_key_once_for_the_domain(tmp_path, capsys):
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
    assert "read through the service account" in capsys.readouterr().out

    # A second address in the domain shares it; another key is refused without --force.
    other = ("sam@example.org", "--service-account", str(source), "--no-auth")
    assert setup_run(*other, "--config", str(root)) == 0
    source.write_text(json.dumps({**KEY, "client_id": "999"}))
    with pytest.raises(SystemExit, match="different service-account key"):
        setup_run(*other, "--config", str(root))
    assert setup_run(*other, "--force", "--config", str(root)) == 0
    assert json.loads(key.read_text())["client_id"] == "999"


def test_setup_refuses_a_file_that_isnt_a_key(tmp_path):
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
