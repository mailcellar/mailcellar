import json

import pytest

from mailcellar import gmailapi
from mailcellar.dedup import cli as dedup

ACCOUNTS = {"work": "alex@example.org", "personal": "alex@example.com"}


def write_config(root, accounts):
    root.mkdir(exist_ok=True)
    (root / "accounts.toml").write_text(
        "".join(f'{name} = "{account}"\n' for name, account in accounts.items())
    )
    for name in accounts:
        (root / name).mkdir(exist_ok=True)
        (root / name / "client_secrets.json").write_text("{}")


@pytest.fixture
def config(tmp_path):
    """config/ with accounts.toml and one folder per account."""
    root = tmp_path / "config"
    write_config(root, ACCOUNTS)
    return root


def test_config_accounts(config, tmp_path):
    assert gmailapi.config_accounts(config) == {
        "alex@example.org": config / "work",
        "alex@example.com": config / "personal",
    }
    missing = tmp_path / "missing"
    with pytest.raises(gmailapi.GmailError, match="accounts.toml not found"):
        gmailapi.config_accounts(missing)
    bad = tmp_path / "bad"
    write_config(bad, {"oops": "not-an-address"})
    with pytest.raises(gmailapi.GmailError, match="not an email address"):
        gmailapi.config_accounts(bad)


@pytest.mark.parametrize(
    "name, expected",
    [
        ("alex@example.org", "alex@example.org"),
        ("ALEX@EXAMPLE.ORG", "alex@example.org"),
        ("personal", "alex@example.com"),  # folder name
        ("work", "alex@example.org"),  # folder name, not a substring
        ("example.org", "alex@example.org"),  # address part
    ],
)
def test_resolve_account(config, name, expected):
    assert gmailapi.resolve_account(config, name) == (
        expected,
        config / {v: k for k, v in ACCOUNTS.items()}[expected],
    )


def test_find_config_folder(config):
    assert gmailapi.find_config_folder(config, "alex@example.org") == config / "work"
    with pytest.raises(gmailapi.GmailError, match="not listed"):
        gmailapi.find_config_folder(config, "nobody@example.com")


@pytest.fixture
def fake_auth(monkeypatch):
    calls = []

    def load_credentials(folder, account, scope):
        calls.append((folder.name, account, scope))
        if account == "bad@example.com":
            raise gmailapi.GmailError("scope not granted")
        return object()

    monkeypatch.setattr(gmailapi, "load_credentials", load_credentials)
    monkeypatch.setattr(gmailapi, "GmailAccount", lambda account, creds: None)
    return calls


def test_auth_defaults_to_every_account(config, fake_auth, capsys):
    dedup.main(["auth", "--scope", "readonly", "--config", str(config)])
    assert sorted(fake_auth) == [
        ("personal", "alex@example.com", "readonly"),
        ("work", "alex@example.org", "readonly"),
    ]
    assert capsys.readouterr().out.count("gmail.readonly OK") == 2


def test_auth_named_account_by_substring(config, fake_auth):
    dedup.main(["auth", "personal", "--scope", "modify", "--config", str(config)])
    assert fake_auth == [("personal", "alex@example.com", "modify")]


def test_auth_rejects_ambiguous_account(config, fake_auth):
    with pytest.raises(SystemExit):
        dedup.main(["auth", "alex", "--scope", "modify", "--config", str(config)])
    assert fake_auth == []


def test_auth_reports_failure_and_continues(config, fake_auth, capsys):
    write_config(config, {**ACCOUNTS, "other": "bad@example.com"})
    with pytest.raises(SystemExit) as exc:
        dedup.main(["auth", "--scope", "readonly", "--config", str(config)])
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "bad@example.com: FAILED — scope not granted" in out
    assert out.count("OK") == 2


def test_rejected_refresh_token_falls_back_to_consent(tmp_path, monkeypatch):
    """A refresh token Google rejects (e.g. 7-day Testing expiry) re-runs consent."""
    from google.auth.exceptions import RefreshError
    from google.oauth2 import credentials as gcreds
    from google_auth_oauthlib import flow as gflow

    gmailapi.token_file(tmp_path, "modify").write_text("{}")
    scope = gmailapi.SCOPES["modify"]

    class Stale:
        expired, valid, refresh_token = True, False, "r"

        def refresh(self, request):
            raise RefreshError("invalid_grant")

    class Fresh:
        valid = True

        def has_scopes(self, scopes):
            return scopes == [scope]

        def to_json(self):
            return json.dumps({"token": "new"})

    class Flow:
        def run_local_server(self, **kwargs):
            assert kwargs["login_hint"] == "a@example.com"
            return Fresh()

    monkeypatch.setattr(
        gcreds.Credentials,
        "from_authorized_user_file",
        classmethod(lambda cls, path, scopes: Stale()),
    )
    monkeypatch.setattr(
        gflow.InstalledAppFlow,
        "from_client_secrets_file",
        classmethod(lambda cls, path, scopes: Flow()),
    )

    creds = gmailapi.load_credentials(tmp_path, "a@example.com", "modify")
    assert isinstance(creds, Fresh)
    assert json.loads(gmailapi.token_file(tmp_path, "modify").read_text()) == {"token": "new"}


def test_readonly_request_reuses_modify_token(tmp_path, monkeypatch):
    from google.oauth2 import credentials as gcreds

    gmailapi.token_file(tmp_path, "modify").write_text("{}")
    requested = []

    class Valid:
        expired, valid = False, True

        def has_scopes(self, scopes):
            return True

        def to_json(self):
            return "{}"

    def from_file(cls, path, scopes):
        requested.append((path.rsplit("/", 1)[-1], scopes))
        return Valid()

    monkeypatch.setattr(gcreds.Credentials, "from_authorized_user_file", classmethod(from_file))
    gmailapi.load_credentials(tmp_path, "a@example.com", "readonly")
    assert requested == [("dedup-token-modify.json", [gmailapi.SCOPES["modify"]])]
    assert not gmailapi.token_file(tmp_path, "readonly").exists()


def test_ungranted_scope_is_an_error(tmp_path, monkeypatch):
    from google_auth_oauthlib import flow as gflow

    class Partial:
        valid = True

        def has_scopes(self, scopes):
            return False

    class Flow:
        def run_local_server(self, **kwargs):
            return Partial()

    monkeypatch.setattr(
        gflow.InstalledAppFlow,
        "from_client_secrets_file",
        classmethod(lambda cls, path, scopes: Flow()),
    )
    with pytest.raises(gmailapi.GmailError, match="did not grant gmail.modify"):
        gmailapi.load_credentials(tmp_path, "a@example.com", "modify")
    assert not gmailapi.token_file(tmp_path, "modify").exists()
