import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mailcellar import gmailapi
from mailcellar.paths import write_private
from mailcellar.setup import auth

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


class Calls(list):
    renewals: list

    def __init__(self):
        super().__init__()
        self.renewals = []
        self.exact = []


@pytest.fixture
def fake_auth(monkeypatch):
    calls = Calls()

    def load_credentials(folder, account, scope, renew=False, exact=False):
        calls.append((folder.name, account, scope))
        calls.renewals.append(renew)
        calls.exact.append(exact)
        if account == "bad@example.com":
            raise gmailapi.GmailError("scope not granted")
        return object()

    monkeypatch.setattr(gmailapi, "load_credentials", load_credentials)
    monkeypatch.setattr(gmailapi, "GmailAccount", lambda account, creds: None)
    return calls


def test_auth_defaults_to_every_account(config, fake_auth, capsys):
    auth.main(["--scope", "readonly", "--config", str(config)])
    assert sorted(fake_auth) == [
        ("personal", "alex@example.com", "readonly"),
        ("work", "alex@example.org", "readonly"),
    ]
    assert capsys.readouterr().out.count("gmail.readonly OK") == 2


def test_auth_named_account_by_substring(config, fake_auth):
    auth.main(["personal", "--scope", "readonly", "--config", str(config)])
    assert fake_auth == [("personal", "alex@example.com", "readonly")]


def test_auth_rejects_ambiguous_account(config, fake_auth):
    with pytest.raises(SystemExit):
        auth.main(["alex", "--scope", "modify", "--config", str(config)])
    assert fake_auth == []


def test_auth_reports_failure_and_continues(config, fake_auth, capsys):
    write_config(config, {**ACCOUNTS, "other": "bad@example.com"})
    with pytest.raises(SystemExit) as exc:
        auth.main(["--scope", "readonly", "--config", str(config)])
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "bad@example.com: FAILED — scope not granted" in out
    assert out.count("OK") == 2


def test_rejected_refresh_token_falls_back_to_consent(tmp_path, monkeypatch, capsys):
    (tmp_path / "client_secrets.json").write_text("{}")  # a sign-in needs the client
    """A refresh token Google rejects (e.g. 7-day Testing expiry) re-runs consent."""
    from google.auth.exceptions import RefreshError
    from google.oauth2 import credentials as gcreds
    from google_auth_oauthlib import flow as gflow

    gmailapi.token_file(tmp_path, "readonly").write_text("{}")
    scope = gmailapi.SCOPES["readonly"]

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

    creds = gmailapi.load_credentials(tmp_path, "a@example.com", "readonly")
    assert isinstance(creds, Fresh)
    stored = json.loads(gmailapi.token_file(tmp_path, "readonly").read_text())
    assert stored.pop("authorised_at").startswith(datetime.now(UTC).strftime("%Y-%m-%d"))
    assert stored == {"token": "new"}
    out = capsys.readouterr().out
    assert "was rejected (invalid_grant); authorising again" in out
    assert "mailcellar auth a@example.com --scope readonly --renew" in out


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
    assert requested == [("token-modify.json", [gmailapi.SCOPES["modify"]])]
    assert not gmailapi.token_file(tmp_path, "readonly").exists()
    assert gmailapi.token_file(tmp_path, "modify").exists()  # a read never deletes it


def test_ungranted_scope_is_an_error(tmp_path, monkeypatch):
    (tmp_path / "client_secrets.json").write_text("{}")  # a sign-in needs the client
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


# ── token age, renewing, and the auth command ────────────────────────────────

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def test_aging_notice_names_the_command_and_admits_it_cannot_see_the_status():
    ago = lambda days: NOW - timedelta(days=days)  # noqa: E731
    notice = gmailapi.aging_notice("a@example.com", "readonly", ago(6), NOW, 5)
    assert "authorised 6 days ago" in notice and "Testing status" in notice
    assert "can't see the status" in notice
    assert "mailcellar auth a@example.com --scope readonly --renew" in notice
    assert gmailapi.aging_notice("a@example.com", "readonly", ago(5), NOW, 5) != ""  # at the limit
    assert gmailapi.aging_notice("a@example.com", "readonly", ago(4.9), NOW, 5) == ""
    assert gmailapi.aging_notice("a@example.com", "readonly", ago(60), NOW, 0) == ""  # turned off
    assert gmailapi.aging_notice("a@example.com", "readonly", None, NOW, 5) == ""  # unknown age


def test_token_authorised_at_reads_the_recorded_time_or_none(tmp_path):
    path = gmailapi.token_file(tmp_path, "modify")
    assert gmailapi.token_authorised_at(tmp_path, "modify") is None  # no token
    path.write_text('{"token": "t"}')
    assert gmailapi.token_authorised_at(tmp_path, "modify") is None  # from before it was recorded
    path.write_text("not json")
    assert gmailapi.token_authorised_at(tmp_path, "modify") is None
    path.write_text(json.dumps({"authorised_at": NOW.isoformat()}))
    assert gmailapi.token_authorised_at(tmp_path, "modify") == NOW
    assert gmailapi.token_age_days(NOW - timedelta(hours=36), NOW) == pytest.approx(1.5)


class Token:
    """Credentials that are valid, serialise to a token, and count calls."""

    expired, valid, refresh_token = False, True, "r"

    def has_scopes(self, scopes):
        return True

    def to_json(self):
        return json.dumps({"token": "t"})


@pytest.fixture
def consent(monkeypatch):
    """Cached tokens are valid; a new consent flow gives a new Token and is counted."""
    from google.oauth2 import credentials as gcreds
    from google_auth_oauthlib import flow as gflow

    flows = []

    class Flow:
        def run_local_server(self, **kwargs):
            flows.append(kwargs["login_hint"])
            return Token()

    monkeypatch.setattr(
        gcreds.Credentials,
        "from_authorized_user_file",
        classmethod(lambda cls, path, scopes: Token()),
    )
    monkeypatch.setattr(
        gflow.InstalledAppFlow,
        "from_client_secrets_file",
        classmethod(lambda cls, path, scopes: Flow()),
    )
    return flows


@pytest.fixture
def account_folder(config):
    return config / "work"


def write_token(folder, scope="readonly", days_ago=None):
    data = {"token": "t"}
    if days_ago is not None:
        data["authorised_at"] = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    gmailapi.token_file(folder, scope).write_text(json.dumps(data))


@pytest.fixture
def open_umask():
    """A umask that would leave new files readable and writable by everyone."""
    old = os.umask(0)
    yield
    os.umask(old)


def test_a_token_is_never_readable_by_others_while_it_is_written(
    account_folder, consent, monkeypatch, open_umask
):
    write_token(account_folder, days_ago=2)
    token = gmailapi.token_file(account_folder, "readonly")
    token.chmod(0o644)
    seen = []
    real_replace = os.replace

    def replace(src, dst):
        seen.append((Path(src).stat().st_mode & 0o777, json.loads(Path(src).read_text())))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    gmailapi.load_credentials(account_folder, "alex@example.org", "readonly")
    (mode, written), *more = seen
    assert mode == 0o600 and written["token"] == "t" and not more
    assert token.stat().st_mode & 0o777 == 0o600
    assert [p.name for p in account_folder.iterdir() if p.name.endswith(".part")] == []


def test_a_private_file_left_half_written_keeps_the_old_one(tmp_path, monkeypatch, open_umask):
    target = tmp_path / "token-readonly.json"
    target.write_text("old")

    def fail(src, dst):
        assert Path(src).stat().st_mode & 0o777 == 0o600  # private before anything is written
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        write_private(target, b"new")
    assert target.read_text() == "old"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["token-readonly.json"]


def test_private_files_written_at_once_never_mix(tmp_path, monkeypatch):
    target = tmp_path / "token-readonly.json"
    real_replace, nested = os.replace, []

    def replace(src, dst):
        if not nested:  # another process writes the same token while this one is mid-write
            nested.append(src)
            write_private(target, b"B")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    write_private(target, b"A")  # its own temporary file is still there to rename
    assert target.read_bytes() == b"A"
    assert [p.name for p in tmp_path.iterdir()] == ["token-readonly.json"]


def test_a_modify_token_is_held_for_the_run_and_never_stored(account_folder, consent, capsys):
    first = gmailapi.load_credentials(account_folder, "alex@example.org", "modify")
    again = gmailapi.load_credentials(account_folder, "alex@example.org", "modify")
    assert again is first and consent == ["alex@example.org"]  # signed in once
    assert "held for this run only, never stored" in capsys.readouterr().out
    assert sorted(p.name for p in account_folder.iterdir()) == ["client_secrets.json"]


def test_a_stored_modify_token_is_used_once_more_then_deleted(
    account_folder, consent, caplog, capsys
):
    write_token(account_folder, "modify")
    gmailapi.legacy_token_file(account_folder, "modify").write_text("{}")
    with caplog.at_level(logging.INFO, logger="mailcellar"):
        gmailapi.load_credentials(account_folder, "alex@example.org", "modify")
    assert consent == []  # the stored token served this run
    assert sorted(p.name for p in account_folder.iterdir()) == ["client_secrets.json"]
    assert "deleted" in caplog.text and "keep_modify_token = true" in caplog.text
    assert "mailcellar auth alex@example.org --scope readonly" in caplog.text  # no read token

    caplog.clear()
    write_token(account_folder, "readonly")
    write_token(account_folder, "modify")
    with caplog.at_level(logging.INFO, logger="mailcellar"):
        gmailapi.load_credentials(account_folder, "alex@example.org", "modify")
    assert "deleted" in caplog.text and "--scope readonly" not in caplog.text


def test_auth_readonly_gets_its_own_token_beside_a_stored_modify_one(
    config, account_folder, consent, capsys, monkeypatch
):
    monkeypatch.setattr(gmailapi, "GmailAccount", lambda account, creds: None)
    (config / "settings.toml").write_text("keep_modify_token = true\n")
    write_token(account_folder, "modify")
    auth.main(["work", "--scope", "readonly", "--config", str(config)])
    assert consent == ["alex@example.org"]  # signed in for a read-only token
    assert gmailapi.token_file(account_folder, "readonly").is_file()
    assert gmailapi.token_file(account_folder, "modify").is_file()  # kept, as configured
    out = capsys.readouterr().out
    assert "gmail.readonly OK, authorised" in out and "via gmail.modify" not in out

    # From now on a read uses the read-only token, not the modify one.
    assert gmailapi.effective_scope(account_folder, "readonly") == "readonly"
    auth.main(["work", "--scope", "readonly", "--config", str(config)])
    assert consent == ["alex@example.org"]  # no second sign-in


def test_auth_asks_for_exactly_the_scope_named(config, fake_auth):
    auth.main(["work", "--scope", "readonly", "--config", str(config)])
    assert fake_auth.exact == [True]


def test_keep_modify_token_stores_it_as_before(config, account_folder, consent):
    (config / "settings.toml").write_text("keep_modify_token = true\n")
    gmailapi.load_credentials(account_folder, "alex@example.org", "modify")
    gmailapi.load_credentials(account_folder, "alex@example.org", "modify")
    assert consent == ["alex@example.org"]  # the second call read the stored token
    assert gmailapi.token_file(account_folder, "modify").stat().st_mode & 0o777 == 0o600

    (config / "settings.toml").write_text('keep_modify_token = "yes"\n')
    with pytest.raises(gmailapi.GmailError, match="keep_modify_token must be true or false"):
        gmailapi.load_credentials(account_folder, "alex@example.org", "modify")


def test_auth_for_modify_says_it_is_not_stored_unless_kept(config, fake_auth, capsys):
    auth.main(["work", "--scope", "modify", "--config", str(config)])
    assert fake_auth == []
    assert "tokens aren't stored" in capsys.readouterr().out
    (config / "settings.toml").write_text("keep_modify_token = true\n")
    auth.main(["work", "--scope", "modify", "--config", str(config)])
    assert fake_auth == [("work", "alex@example.org", "modify")]


def test_a_new_consent_records_when_it_was_given(account_folder, consent):
    gmailapi.load_credentials(account_folder, "alex@example.org", "readonly")
    assert consent == ["alex@example.org"]
    age = gmailapi.token_authorised_at(account_folder, "readonly")
    assert abs(gmailapi.token_age_days(age, datetime.now(UTC))) < 0.01


def test_using_a_cached_token_keeps_the_original_consent_time(account_folder, consent):
    write_token(account_folder, days_ago=2)
    before = gmailapi.token_authorised_at(account_folder, "readonly")
    gmailapi.load_credentials(account_folder, "alex@example.org", "readonly")
    assert consent == []  # no new consent
    assert gmailapi.token_authorised_at(account_folder, "readonly") == before
    mode = gmailapi.token_file(account_folder, "readonly").stat().st_mode & 0o777
    assert mode == 0o600


def test_a_token_from_before_the_time_was_recorded_stays_unknown(account_folder, consent, caplog):
    write_token(account_folder)  # no authorised_at
    gmailapi.load_credentials(account_folder, "alex@example.org", "readonly")
    assert gmailapi.token_authorised_at(account_folder, "readonly") is None
    assert "authorised" not in caplog.text  # nothing to warn about without an age


def test_an_ageing_token_is_reported_with_the_command_to_renew_it(account_folder, consent, caplog):
    write_token(account_folder, days_ago=6)
    with caplog.at_level(logging.WARNING, logger="mailcellar"):
        gmailapi.load_credentials(account_folder, "alex@example.org", "readonly")
    assert "authorised 6 days ago" in caplog.text
    assert "mailcellar auth alex@example.org --scope readonly --renew" in caplog.text


def test_a_recent_token_is_not_reported(account_folder, consent, caplog):
    write_token(account_folder, days_ago=1)
    with caplog.at_level(logging.WARNING, logger="mailcellar"):
        gmailapi.load_credentials(account_folder, "alex@example.org", "readonly")
    assert caplog.text == ""


def test_the_warning_can_be_turned_off_or_moved_in_settings(account_folder, consent, caplog):
    write_token(account_folder, days_ago=6)
    settings = account_folder.parent / "settings.toml"
    settings.write_text("token_warn_days = 0\n")
    with caplog.at_level(logging.WARNING, logger="mailcellar"):
        gmailapi.load_credentials(account_folder, "alex@example.org", "readonly")
    assert caplog.text == ""
    settings.write_text("token_warn_days = 6\n")
    with caplog.at_level(logging.WARNING, logger="mailcellar"):
        gmailapi.load_credentials(account_folder, "alex@example.org", "readonly")
    assert "authorised 6 days ago" in caplog.text
    settings.write_text("token_warn_days = 7\n")
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="mailcellar"):
        gmailapi.load_credentials(account_folder, "alex@example.org", "readonly")
    assert caplog.text == ""


def test_a_bad_warning_setting_is_reported_and_the_default_used(account_folder, consent, caplog):
    write_token(account_folder, days_ago=6)
    (account_folder.parent / "settings.toml").write_text('token_warn_days = "soon"\n')
    with caplog.at_level(logging.WARNING, logger="mailcellar"):
        gmailapi.load_credentials(account_folder, "alex@example.org", "readonly")
    assert "token_warn_days must be a number of days" in caplog.text
    assert "authorised 6 days ago" in caplog.text  # the default of 5 days applies


def test_renew_asks_for_consent_although_the_token_works(account_folder, consent):
    write_token(account_folder, days_ago=6)
    gmailapi.load_credentials(account_folder, "alex@example.org", "readonly", renew=True)
    assert consent == ["alex@example.org"]
    age = gmailapi.token_authorised_at(account_folder, "readonly")
    assert abs(gmailapi.token_age_days(age, datetime.now(UTC))) < 0.01  # the 7 days start again


def test_the_setting_is_validated(tmp_path, monkeypatch):
    from mailcellar import paths

    assert paths.token_warn_days(tmp_path) == paths.TOKEN_WARN_DAYS  # no settings file
    settings = tmp_path / "settings.toml"
    for text, expected in (("", 5), ("token_warn_days = 0", 0), ("token_warn_days = 3", 3)):
        settings.write_text(text + "\n")
        assert paths.token_warn_days(tmp_path) == expected
    for bad in ("-1", "true", '"x"', "2.5"):
        settings.write_text(f"token_warn_days = {bad}\n")
        with pytest.raises(ValueError, match="token_warn_days must be"):
            paths.token_warn_days(tmp_path)
    # the data-root reader accepts the new key beside data_dir, and still rejects others
    monkeypatch.setenv(paths.CONFIG_ENV, str(tmp_path))
    monkeypatch.delenv(paths.DATA_ENV, raising=False)
    settings.write_text('data_dir = "x"\ntoken_warn_days = 3\n')
    assert paths.data_setting() == ("x", str(settings))
    settings.write_text("other = 1\n")
    with pytest.raises(ValueError, match="unknown settings"):
        paths.data_setting()


def test_auth_prints_when_consent_was_given(config, monkeypatch, capsys):
    write_token(config / "work", days_ago=3)
    write_token(config / "personal")
    monkeypatch.setattr(gmailapi, "load_credentials", lambda *a, **k: object())
    monkeypatch.setattr(gmailapi, "GmailAccount", lambda account, creds: None)
    auth.main(["--scope", "readonly", "--config", str(config)])
    out = capsys.readouterr().out
    work = next(line for line in out.splitlines() if "alex@example.org" in line)
    assert "authorised" in work and "(3 days ago)" in work
    personal = next(line for line in out.splitlines() if "alex@example.com" in line)
    assert "authorised before mailcellar recorded when" in personal


def test_auth_passes_renew_on(config, fake_auth):
    auth.main(["work", "--scope", "readonly", "--renew", "--config", str(config)])
    assert fake_auth.renewals == [True]
    auth.main(["work", "--scope", "readonly", "--config", str(config)])
    assert fake_auth.renewals == [True, False]


def test_mailcellar_auth_is_the_command_and_dedup_auth_is_refused(config, fake_auth, capsys):
    from mailcellar import cli

    assert cli.main(["auth", "work", "--scope", "readonly", "--config", str(config)]) == 0
    assert "gmail.readonly OK" in capsys.readouterr().out
    assert fake_auth == [("work", "alex@example.org", "readonly")]

    with pytest.raises(SystemExit) as caught:
        cli.main(["dedup", "auth", "personal", "--scope", "readonly", "--config", str(config)])
    assert caught.value.code == "error: 'mailcellar dedup auth' was removed; use 'mailcellar auth'"
    assert len(fake_auth) == 1  # nothing signed in


def test_mailcellar_auth_help_names_itself(capsys):
    from mailcellar import cli

    with pytest.raises(SystemExit):
        cli.main(["auth", "--help"])
    out = capsys.readouterr().out
    assert out.startswith("usage: mailcellar auth ") and "--renew" in out
    assert "mailcellar auth" in cli.__doc__


def test_token_file_names(tmp_path):
    new = tmp_path / "token-modify.json"
    old = tmp_path / "dedup-token-modify.json"
    assert gmailapi.token_file(tmp_path, "modify") == new  # none yet: the new name
    old.write_text("{}")
    assert gmailapi.token_file(tmp_path, "modify") == old  # only the old one: found
    new.write_text("{}")
    assert gmailapi.token_file(tmp_path, "modify") == new  # both: the new one wins


def test_old_token_is_moved_to_the_new_name(tmp_path, monkeypatch):
    """A token from 0.9 keeps working, with its consent time, and is renamed on use."""
    from google.oauth2 import credentials as gcreds

    old = tmp_path / "dedup-token-readonly.json"
    old.write_text("{}")
    requested = []

    class Valid:
        expired, valid = False, True

        def has_scopes(self, scopes):
            return True

        def to_json(self):
            return json.dumps({"token": "t"})

    def from_file(cls, path, scopes):
        requested.append(path.rsplit("/", 1)[-1])
        return Valid()

    monkeypatch.setattr(gcreds.Credentials, "from_authorized_user_file", classmethod(from_file))
    gmailapi.load_credentials(tmp_path, "a@example.com", "readonly")
    assert requested == ["dedup-token-readonly.json"]  # no new consent asked for
    assert not old.exists()
    assert json.loads((tmp_path / "token-readonly.json").read_text()) == {"token": "t"}
    assert (tmp_path / "token-readonly.json").stat().st_mode & 0o777 == 0o600
