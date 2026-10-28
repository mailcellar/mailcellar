import csv
import sqlite3
from datetime import datetime

import httplib2
import pytest
from googleapiclient.errors import HttpError

from fake_mailbox import ACCOUNT, MS, FakeMailbox, raw_response
from mailcellar import gmailapi
from mailcellar.backup import chain, commands, gyb
from mailcellar.backup.gyb import backup_account, iter_backup
from mailcellar.dedup import cli as dedup
from test_index import make_eml


def rows(folder):
    con = sqlite3.connect(folder / "msg-db.sqlite")
    try:
        return {
            "settings": sorted(con.execute("SELECT name, value FROM settings")),
            "messages": sorted(
                con.execute("SELECT message_filename, message_internaldate FROM messages")
            ),
            "uids": sorted(u for (u,) in con.execute("SELECT uid FROM uids")),
            "labels": sorted(
                con.execute("SELECT u.uid, l.label FROM labels l JOIN uids u USING (message_num)")
            ),
        }
    finally:
        con.close()


# ── layout ───────────────────────────────────────────────────────────────────


def test_backups_live_under_the_data_root(tmp_path, monkeypatch):
    from mailcellar.paths import backup_folder, find_backups

    monkeypatch.chdir(tmp_path)
    assert backup_folder(ACCOUNT) == tmp_path / "data" / ACCOUNT  # ./data, as conftest sets
    assert backup_folder(ACCOUNT, tmp_path / "x") == tmp_path / "x" / ACCOUNT

    commands.backup(FakeMailbox({"g1": raw_response("g1")}), ACCOUNT, backup_folder(ACCOUNT))
    (tmp_path / "data" / "_pruned" / ACCOUNT / "2026-10-22-121708").mkdir(parents=True)
    assert find_backups() == [tmp_path / "data" / ACCOUNT]

    # dedup's and offload's default files, and the reports' backups, move with it.
    from mailcellar.dedup.paths import default_db
    from mailcellar.offload.cli import offload_dir
    from mailcellar.paths import DATA_ENV
    from mailcellar.report.cli import resolve_folders

    root = tmp_path / "mail"
    monkeypatch.setenv(DATA_ENV, str(root))
    chain.anchor_path(ACCOUNT).unlink()  # a second, separate backup of the account
    commands.backup(FakeMailbox({"g1": raw_response("g1")}), ACCOUNT, root / ACCOUNT)
    assert resolve_folders([]) == [root / ACCOUNT]
    assert default_db() == root / "dedup" / "dedup.sqlite"
    assert offload_dir() == root / "offload"


def test_data_root_from_environment_then_settings_then_default(tmp_path, monkeypatch):
    from mailcellar.paths import DATA_ENV, data_root

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(DATA_ENV)
    assert data_root() == tmp_path / "home" / "mailcellar"  # the default (~ is the test's)
    (tmp_path / "config").mkdir()
    settings = tmp_path / "config" / "settings.toml"
    settings.write_text('data_dir = "~/mail-archive"\n')
    assert data_root() == tmp_path / "home" / "mail-archive"
    settings.write_text('data_dir = "backups"\n')  # relative: from the working directory
    assert data_root() == tmp_path / "backups"
    monkeypatch.setenv(DATA_ENV, str(tmp_path / "elsewhere"))  # the environment wins
    assert data_root() == tmp_path / "elsewhere"
    monkeypatch.setenv(DATA_ENV, "  ")  # blank: as if unset
    assert data_root() == tmp_path / "backups"


def test_the_old_default_is_kept_where_it_holds_mailcellars_data(tmp_path, monkeypatch):
    from mailcellar.paths import DATA_ENV, data_root, data_setting

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(DATA_ENV)
    home = tmp_path / "home" / "mailcellar"
    (tmp_path / "data").mkdir()  # a data/ folder that isn't mailcellar's: ignored
    assert data_root() == home
    (tmp_path / "data" / "offload").mkdir()  # mailcellar wrote here: kept
    assert data_root() == tmp_path / "data"
    assert data_setting()[1].startswith("./data (the old default")
    (tmp_path / "data" / "offload").rmdir()
    (tmp_path / "data" / ACCOUNT).mkdir()
    (tmp_path / "data" / ACCOUNT / "msg-db.sqlite").write_bytes(b"")  # a backup: kept
    assert data_root() == tmp_path / "data"
    home.mkdir(parents=True)  # once ~/mailcellar exists, it wins
    assert data_root() == home
    assert data_setting() == ("~/mailcellar", "default")


@pytest.mark.parametrize(
    ("text", "error"),
    [('data_dr = "x"\n', "unknown settings"), ("data_dir = 3\n", "must be a path")],
)
def test_data_root_settings_errors(tmp_path, monkeypatch, text, error):
    from mailcellar.paths import DATA_ENV, data_root

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(DATA_ENV)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.toml").write_text(text)
    with pytest.raises(ValueError, match=error):
        data_root()


# ── gyb ────────────────────────────────────────────────────────────────


def test_relative_path_uses_utc_without_padding():
    assert gyb.relative_path("abc", MS) == "2026/10/22/abc.eml"
    assert gyb.relative_path("abc", 1_000_000_000_000) == "2001/9/9/abc.eml"


def test_internaldate_matches_gyb_format():
    assert gyb.sqlite_internaldate(MS) == str(datetime.fromtimestamp(MS / 1000))
    # microseconds appear only when non-zero, as in gyb's own databases
    assert gyb.sqlite_internaldate(MS + 514).endswith(".514000")
    assert "." not in gyb.sqlite_internaldate(MS)


def test_new_backup_matches_gyb_schema(tmp_path):
    con = gyb.open_backup(tmp_path / "b", ACCOUNT)
    schema = sorted(
        s for (s,) in con.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL")
    )
    assert schema == sorted(
        [
            "CREATE TABLE settings (name TEXT PRIMARY KEY, value TEXT)",
            "CREATE TABLE messages(message_num INTEGER PRIMARY KEY,\n"
            "                         message_filename TEXT,\n"
            "                         message_internaldate TIMESTAMP)",
            "CREATE TABLE labels (message_num INTEGER, label TEXT)",
            "CREATE TABLE uids (message_num INTEGER, uid TEXT PRIMARY KEY)",
            "CREATE UNIQUE INDEX labelidx ON labels (message_num, label)",
        ]
    )
    assert rows(tmp_path / "b")["settings"] == [("db_version", "6"), ("email_address", ACCOUNT)]
    con.close()


def test_open_backup_rejects_other_account_or_version(tmp_path):
    gyb.open_backup(tmp_path, ACCOUNT).close()
    with pytest.raises(gyb.FormatError, match="belongs to"):
        gyb.open_backup(tmp_path, "b@example.com")
    con = sqlite3.connect(tmp_path / "msg-db.sqlite")
    con.execute("UPDATE settings SET value = '5' WHERE name = 'db_version'")
    con.commit()
    con.close()
    with pytest.raises(gyb.FormatError, match="db_version"):
        gyb.open_backup(tmp_path, ACCOUNT)


def test_interrupted_write_leaves_no_eml(tmp_path, monkeypatch):
    con = gyb.open_backup(tmp_path, ACCOUNT)

    def boom(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(gyb.os, "replace", boom)
    with pytest.raises(KeyboardInterrupt):
        gyb.write_message(con, tmp_path, raw_response("g1"), ["INBOX"])
    assert list(tmp_path.rglob("*.eml")) == []
    assert list(tmp_path.rglob(".tmp-*")) == []
    con.close()


# ── backup ───────────────────────────────────────────────────────────────────


def test_backup_writes_gyb_layout(tmp_path):
    body = make_eml(msgid="<g1@x>")
    gmail = FakeMailbox({"g1": raw_response("g1", label_ids=["INBOX", "Label_1"], body=body)})
    folder = tmp_path / "data" / ACCOUNT
    stats = commands.backup(gmail, ACCOUNT, folder)

    assert stats["downloaded"] == 1
    assert (folder / "2026/10/22/g1.eml").read_bytes() == body
    r = rows(folder)
    assert r["messages"] == [("2026/10/22/g1.eml", gyb.sqlite_internaldate(MS))]
    assert r["labels"] == [("g1", "INBOX"), ("g1", "work")]


def test_backup_skips_chats(tmp_path):
    gmail = FakeMailbox({"c1": raw_response("c1", label_ids=["CHAT"]), "g1": raw_response("g1")})
    stats = commands.backup(gmail, ACCOUNT, tmp_path)
    assert (stats["downloaded"], stats["chats"]) == (1, 1)
    assert rows(tmp_path)["uids"] == ["g1"]


def test_a_full_listing_never_downloads_chats(tmp_path):
    gmail = FakeMailbox({"c1": raw_response("c1", label_ids=["CHAT"]), "g1": raw_response("g1")})
    commands.backup(gmail, ACCOUNT, tmp_path)
    gmail.fetched.clear()
    stats = commands.backup(
        gmail, ACCOUNT, tmp_path, full=True
    )  # every full run, not only the first
    assert gmail.fetched == [] and (stats["downloaded"], stats["chats"]) == (0, 1)
    gmail.messages["g2"] = raw_response("g2")
    stats = commands.backup(gmail, ACCOUNT, tmp_path, refresh_labels=True)
    assert gmail.fetched == ["g2"] and stats["chats"] == 1


def test_backup_is_incremental_and_refetches_missing_files(tmp_path):
    gmail = FakeMailbox({"g1": raw_response("g1"), "g2": raw_response("g2")})
    commands.backup(gmail, ACCOUNT, tmp_path)

    gmail.messages["g3"] = raw_response("g3")
    (tmp_path / "2026/10/22/g2.eml").unlink()
    gmail.fetched.clear()
    stats = commands.backup(gmail, ACCOUNT, tmp_path)

    assert sorted(gmail.fetched) == ["g2", "g3"]
    assert stats["downloaded"] == 2
    assert (tmp_path / "2026/10/22/g2.eml").is_file()


def test_backup_never_deletes_messages_gone_from_gmail(tmp_path):
    gmail = FakeMailbox({"g1": raw_response("g1")})
    commands.backup(gmail, ACCOUNT, tmp_path)
    del gmail.messages["g1"]
    commands.backup(gmail, ACCOUNT, tmp_path)
    assert (tmp_path / "2026/10/22/g1.eml").is_file()
    assert rows(tmp_path)["uids"] == ["g1"]


def test_backup_reports_failures_and_retries_next_run(tmp_path):
    gmail = FakeMailbox({"g1": raw_response("g1"), "g2": raw_response("g2")}, fail={"g2"})
    stats = commands.backup(gmail, ACCOUNT, tmp_path)
    assert stats["failed"] == ["g2: HttpError 500"]

    gmail.fail.clear()
    gmail.fetched.clear()
    commands.backup(gmail, ACCOUNT, tmp_path)
    assert gmail.fetched == ["g2"]


def test_refresh_labels(tmp_path):
    gmail = FakeMailbox({"g1": raw_response("g1", label_ids=["INBOX"])})
    commands.backup(gmail, ACCOUNT, tmp_path)
    gmail.messages["g1"]["labelIds"] = ["Label_1"]

    # a full listing doesn't look at existing messages' labels …
    commands.backup(gmail, ACCOUNT, tmp_path, full=True)
    assert rows(tmp_path)["labels"] == [("g1", "INBOX")]
    # … unless asked to
    stats = commands.backup(gmail, ACCOUNT, tmp_path, refresh_labels=True)
    assert stats["labels_changed"] == 1
    assert rows(tmp_path)["labels"] == [("g1", "work")]


def test_many_labels_are_read_by_listing_the_mailbox(tmp_path, monkeypatch):
    messages = {g: raw_response(g, label_ids=["INBOX"]) for g in ("g1", "g2", "g3")}
    gmail = FakeMailbox(messages)
    commands.backup(gmail, ACCOUNT, tmp_path)
    monkeypatch.setattr(gmailapi, "prefer_listing", lambda wanted, mailbox: True)
    gmail.detail_lookups = []

    gmail.messages["g1"]["labelIds"] = ["Label_1"]  # a history sync …
    del gmail.messages["g2"]
    stats = commands.backup(gmail, ACCOUNT, tmp_path)
    assert stats["labels_changed"] == 1 and stats["deleted_backed_up"] == 1
    assert ("g1", "work") in rows(tmp_path)["labels"]

    gmail.messages["g3"]["labelIds"] = ["Label_1"]  # … and --refresh-labels
    stats = commands.backup(gmail, ACCOUNT, tmp_path, refresh_labels=True)
    assert ("g3", "work") in rows(tmp_path)["labels"]
    # one listing each: the history sync, and the refresh (a full run, without history)
    assert gmail.listings == 2 and gmail.detail_lookups == []  # never one by one


def test_output_is_readable_by_index(tmp_path):
    folder = tmp_path / "data" / ACCOUNT
    gmail = FakeMailbox({"g1": raw_response("g1", label_ids=["INBOX", "Label_1"])})
    commands.backup(gmail, ACCOUNT, folder)

    assert backup_account(folder) == ACCOUNT
    [bm] = list(iter_backup(folder))
    assert (bm.gmail_id, bm.labels) == ("g1", ["INBOX", "work"])

    db = tmp_path / "dedup.sqlite"
    dedup.main(["index", "--db", str(db), str(folder)])
    con = sqlite3.connect(db)
    assert con.execute("SELECT gmail_id, message_id FROM messages").fetchall() == [("g1", "g1@x")]


# ── count ────────────────────────────────────────────────────────────────────


def test_count(tmp_path):
    with pytest.raises(gyb.FormatError, match="no msg-db.sqlite"):  # no backup yet
        commands.count(FakeMailbox({}), ACCOUNT, tmp_path)
    assert not (tmp_path / "msg-db.sqlite").exists()

    gmail = FakeMailbox({g: raw_response(g) for g in ("g1", "g2", "g4")})
    commands.backup(gmail, ACCOUNT, tmp_path)
    (tmp_path / "2026/10/22/g2.eml").unlink()  # indexed, file lost
    gmail.messages["g3"] = raw_response("g3")  # new in Gmail
    gmail.messages["c1"] = raw_response("c1", label_ids=["CHAT"])
    del gmail.messages["g1"]  # permanently deleted in Gmail
    gmail.trashed.add("g4")  # in Trash: only in the backup, but not gone

    c = commands.count(gmail, ACCOUNT, tmp_path)
    assert c == {
        "in_gmail": 3,
        "backed_up": 2,
        "missing": ["g2", "g3"],
        "chats": 1,
        "only_in_backup": 2,
        "restored_gone": 0,
        "gone_from_server": 1,
        "offloaded": 0,
        "offloaded_in_gmail": 0,
        "forgotten": 0,
        "restored": 0,
        "file_missing": 1,
    }


# ── sync ─────────────────────────────────────────────────────────────────────


def synced(tmp_path, n=20, gone=("g0",), trashed=("g1",)):
    """A backup of n messages, then some deleted for good and some trashed in Gmail."""
    folder = tmp_path / "data" / ACCOUNT
    gmail = FakeMailbox(
        {f"g{i}": raw_response(f"g{i}", label_ids=["INBOX", "Label_1"]) for i in range(n)}
    )
    commands.backup(gmail, ACCOUNT, folder)
    for gid in gone:
        del gmail.messages[gid]
    gmail.trashed.update(trashed)
    return gmail, folder


def yes(prompt):
    return True


def test_sync_moves_permanently_deleted_messages_out(tmp_path):
    gmail, folder = synced(tmp_path)
    result = commands.prune(gmail, ACCOUNT, folder, confirm=yes)

    assert result["pruned"] == 1
    dest = result["dest"]
    assert dest.parent == tmp_path / "data" / "_pruned" / ACCOUNT
    assert not (folder / "2026/10/22/g0.eml").exists()
    assert (dest / "2026/10/22/g0.eml").is_file()
    r = rows(folder)
    assert "g0" not in r["uids"] and "g1" in r["uids"]  # trashed mail is kept
    assert not any(uid == "g0" for uid, _ in r["labels"])
    assert len(r["messages"]) == 19

    with (dest / "pruned.csv").open() as fh:
        manifest = list(csv.DictReader(fh))
    assert manifest == [
        {
            "gmail_id": "g0",
            "path": "2026/10/22/g0.eml",
            "internal_date": gyb.sqlite_internaldate(MS),
            "labels": "INBOX;work",
            "file": "moved",
        }
    ]


def test_sync_drops_index_entries_whose_file_is_missing(tmp_path):
    gmail, folder = synced(tmp_path)
    (folder / "2026/10/22/g0.eml").unlink()
    result = commands.prune(gmail, ACCOUNT, folder, confirm=yes)
    assert result["pruned"] == 1
    assert "g0" not in rows(folder)["uids"]
    assert not (result["dest"] / "2026/10/22/g0.eml").exists()


def test_sync_declined_changes_nothing(tmp_path):
    gmail, folder = synced(tmp_path)
    before = rows(folder)
    assert commands.prune(gmail, ACCOUNT, folder, confirm=lambda p: False) == {
        "pruned": 0,
        "dest": None,
    }
    assert rows(folder) == before
    assert not (tmp_path / "data" / "_pruned").exists()


def test_sync_stops_above_threshold_unless_forced(tmp_path):
    gmail, folder = synced(tmp_path, n=10, gone=("g0", "g1"), trashed=())
    with pytest.raises(commands.PruneAborted, match="more than 10%"):
        commands.prune(gmail, ACCOUNT, folder, confirm=yes)
    assert len(rows(folder)["uids"]) == 10
    assert commands.prune(gmail, ACCOUNT, folder, confirm=yes, force=True)["pruned"] == 2


def test_sync_refuses_when_gmail_lists_nothing(tmp_path):
    gmail, folder = synced(tmp_path)
    gmail.messages.clear()
    with pytest.raises(commands.PruneAborted, match="no messages at all"):
        commands.prune(gmail, ACCOUNT, folder, confirm=yes, force=True)
    assert len(rows(folder)["uids"]) == 20


def test_pruned_backup_is_still_valid_for_index(tmp_path):
    gmail, folder = synced(tmp_path)
    db = tmp_path / "dedup.sqlite"
    dedup.main(["index", "--db", str(db), str(folder)])
    commands.prune(gmail, ACCOUNT, folder, confirm=yes)
    dedup.main(["index", "--db", str(db), str(folder)])
    con = sqlite3.connect(db)
    assert con.execute("SELECT count(*) FROM messages WHERE gmail_id = 'g0'").fetchone() == (0,)
    assert con.execute("SELECT count(*) FROM messages").fetchone() == (19,)


# ── backup --prune (CLI) ──────────────────────────────────────────────────────


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """Run the CLI against a FakeMailbox instead of the real API."""
    state = {}

    def fake_connect(args):
        return ACCOUNT, state["folder"], state["gmail"]

    monkeypatch.setattr(commands, "connect", fake_connect)

    def run(*argv):
        return commands.main(list(argv))

    return state, run


def test_cli_prune_with_search_is_refused(cli):
    _state, run = cli
    with pytest.raises(SystemExit, match="can't be combined with --search"):
        run("backup", "a", "--prune", "--search", "x")


def test_cli_prune_skipped_after_fetch_failures(tmp_path, cli):
    state, run = cli
    gmail, folder = synced(tmp_path)
    gmail.messages["new"] = raw_response("new")
    gmail.fail = {"new"}
    state.update(gmail=gmail, folder=folder)
    assert run("backup", "a", "--prune", "--yes") == 1
    assert "g0" in rows(folder)["uids"]


def test_cli_prune_yes_and_aborted(tmp_path, cli):
    state, run = cli
    gmail, folder = synced(tmp_path / "ok")
    state.update(gmail=gmail, folder=folder)
    assert run("backup", "a", "--prune", "--yes") == 0
    assert "g0" not in rows(folder)["uids"]

    # above the guard: aborted, exit 1, nothing pruned
    chain.anchor_path(ACCOUNT).unlink()  # a second, separate backup of the account
    gmail, folder = synced(tmp_path / "guard", n=10, gone=("g0", "g1"), trashed=())
    state.update(gmail=gmail, folder=folder)
    assert run("backup", "a", "--prune", "--yes") == 1
    assert len(rows(folder)["uids"]) == 10


def test_cli_sync_is_gone(tmp_path, cli, capsys):
    state, run = cli
    gmail, folder = synced(tmp_path)
    state.update(gmail=gmail, folder=folder)
    with pytest.raises(SystemExit) as exc:
        run("backup", "a", "--sync", "--yes")
    assert exc.value.code == 2 and "unrecognized arguments: --sync" in capsys.readouterr().err
    assert "g0" in rows(folder)["uids"]  # nothing pruned


def test_cli_help_renders_and_lists_prune_not_sync(capsys):
    with pytest.raises(SystemExit) as exc:
        commands.main(["backup", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--prune" in out and "--sync" not in out


# ── gmailapi retries ─────────────────────────────────────────────────────────


def http_error(status, content=b"{}"):
    return HttpError(httplib2.Response({"status": status}), content)


class Req:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeService:
    """Just enough of the googleapiclient Gmail service for GmailAccount."""

    def __init__(self, get_results):
        self.get_results = get_results  # gmail_id → list of results/exceptions
        self.batches = 0

    def users(self):
        return self

    def messages(self):
        return self

    def labels(self):
        return self

    def getProfile(self, userId):
        return Req(lambda: {"emailAddress": ACCOUNT})

    def list(self, userId, **kwargs):
        return Req(lambda: {"labels": [{"id": "INBOX", "type": "system"}]})

    def get(self, userId, id, **kwargs):
        def run():
            result = self.get_results[id].pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        return Req(run)

    def new_batch_http_request(self, callback):
        service = self

        class Batch:
            def __init__(self):
                self.items = []
                service.batches += 1

            def add(self, req, request_id):
                self.items.append((request_id, req))

            def execute(self):
                for rid, req in self.items:
                    try:
                        callback(rid, req.execute(), None)
                    except HttpError as exc:
                        callback(rid, None, exc)

        return Batch()


def account(service):
    return gmailapi.GmailAccount(ACCOUNT, None, service=service, sleep=lambda s: None)


def test_get_raw_retries_rate_limits_and_skips_deleted():
    service = FakeService(
        {
            "g1": [http_error(429), http_error(503), raw_response("g1")],
            "g2": [http_error(404)],
            "g3": [
                http_error(403, b'{"error": {"errors": [{"reason": "userRateLimitExceeded"}]}}'),
                raw_response("g3"),
            ],
        }
    )
    got = []
    failed = account(service).get_raw(["g1", "g2", "g3"], lambda r: got.append(r["id"]))
    assert sorted(got) == ["g1", "g3"]
    assert failed == []


FAILED_PRECONDITION = (
    b'{"error": {"code": 400, "message": "Precondition check failed.", '
    b'"errors": [{"reason": "failedPrecondition"}]}}'
)


def test_a_failed_precondition_is_retried_and_other_400s_are_not():
    service = FakeService({"g1": [http_error(400, FAILED_PRECONDITION), raw_response("g1")]})
    got = []
    assert account(service).get_raw(["g1"], lambda r: got.append(r["id"])) == []
    assert got == ["g1"]
    assert gmailapi.describe(http_error(400, FAILED_PRECONDITION)).startswith("400 (precondition")
    assert not gmailapi.is_retryable(http_error(400, b'{"error": {"reason": "badRequest"}}'))


def test_message_labels_collects_failed_lookups_when_asked():
    def gmail():
        service = FakeService(
            {"g1": [{"id": "g1", "labelIds": ["INBOX"]}], "g2": [http_error(400)]}
        )
        g = account(service)
        g._label_names = {"INBOX": "INBOX"}
        return g

    failures = []
    assert gmail().message_labels(["g1", "g2"], failures) == {"g1": {"INBOX"}}
    assert len(failures) == 1 and failures[0].startswith("g2: ")
    with pytest.raises(gmailapi.GmailError, match="1 lookups failed"):
        gmail().message_labels(["g1", "g2"])


def test_get_raw_gives_up_after_max_attempts():
    service = FakeService({"g1": [http_error(500)] * gmailapi.BATCH_ATTEMPTS})
    failed = account(service).get_raw(["g1"], lambda r: None)
    assert len(failed) == 1 and failed[0].startswith("g1: ")


def test_permanent_errors_are_not_retried():
    service = FakeService({"g1": [http_error(400), raw_response("g1")]})
    failed = account(service).get_raw(["g1"], lambda r: None)
    assert len(failed) == 1
    assert service.get_results["g1"]  # second result never requested


def stalls(n, result):
    """A request that times out n times, then returns result."""
    calls = []

    def run():
        calls.append(1)
        if len(calls) <= n:
            raise TimeoutError("[Errno 60] Operation timed out")
        return result

    return Req(run), calls


def test_socket_timeouts_are_retried():
    gmail = account(FakeService({}))
    req, calls = stalls(2, {"ok": True})
    assert gmail._execute(req) == {"ok": True}
    assert len(calls) == 3

    req, calls = stalls(gmailapi.MAX_ATTEMPTS, {"ok": True})
    with pytest.raises(TimeoutError):
        gmail._execute(req)
    assert len(calls) == gmailapi.MAX_ATTEMPTS


def test_timed_out_batch_is_resent():
    service = FakeService({"g1": [raw_response("g1")]})
    gmail = account(service)
    make_batch = service.new_batch_http_request

    def stall():
        raise TimeoutError("[Errno 60] Operation timed out")

    def stalling_batch(callback):
        batch = make_batch(callback)
        if service.batches == 1:  # the first batch request stalls; nothing ran
            batch.execute = stall
        return batch

    service.new_batch_http_request = stalling_batch
    got = []
    assert gmail.get_raw(["g1"], lambda r: got.append(r["id"])) == []
    assert got == ["g1"] and service.batches == 2


CONCURRENT_429 = b'{"error": {"message": "Too many concurrent requests for user."}}'


class ConcurrencyLimitedService(FakeService):
    """Answers 429 to the calls of a batch beyond the first `limit`, as Gmail does."""

    def __init__(self, limit):
        super().__init__({})
        self.limit = limit
        self.sizes = []

    def get(self, userId, id, **kwargs):
        return Req(lambda: {"id": id, "labelIds": ["INBOX"]})

    def new_batch_http_request(self, callback):
        service = self

        class Batch:
            def __init__(self):
                self.items = []

            def add(self, req, request_id):
                self.items.append((request_id, req))

            def execute(self):
                service.sizes.append(len(self.items))
                for i, (rid, req) in enumerate(self.items):
                    if i < service.limit:
                        callback(rid, req.execute(), None)
                    else:
                        callback(rid, None, http_error(429, CONCURRENT_429))

        return Batch()


def test_batches_shrink_to_gmails_concurrency_limit():
    service = ConcurrencyLimitedService(limit=8)
    sleeps = []
    gmail = gmailapi.GmailAccount(ACCOUNT, None, service=service, sleep=sleeps.append)
    ids = [f"g{i}" for i in range(2_000)]
    details = gmail.message_details(ids)  # raises GmailError if any lookup gave up
    assert len(details) == 2_000 and all(v["labelIds"] == ["INBOX"] for v in details.values())
    assert service.sizes[:4] == [50, 25, 12, 6]
    assert max(service.sizes[4:]) <= 9  # probes one above the limit, then halves
    assert len(sleeps) < len(service.sizes) / 4  # no backoff while halving helps

    service.limit = 50  # Gmail recovers: the next call starts small and grows by one
    before = len(service.sizes)
    gmail.message_details(ids[:100])
    grown = service.sizes[before:-1]  # the last batch takes what is left
    assert grown[0] <= 9 and grown == list(range(grown[0], grown[0] + len(grown)))


def test_quota_exhausted_for_a_while_backs_off_then_recovers():
    service = ConcurrencyLimitedService(limit=0)  # every call refused
    sleeps = []
    gmail = gmailapi.GmailAccount(ACCOUNT, None, service=service, sleep=sleeps.append)

    def recover(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 5:
            service.limit = 50

    gmail._sleep = recover
    assert len(gmail.message_details([f"g{i}" for i in range(20)])) == 20
    waits = [round(s) for s in sleeps]  # 1, 2, 4, 8, 16 s, plus jitter
    assert waits == sorted(waits) and sleeps[-1] >= 16


def test_a_refused_id_gets_about_five_minutes_of_retries():
    service = ConcurrencyLimitedService(limit=0)
    sleeps = []
    gmail = gmailapi.GmailAccount(ACCOUNT, None, service=service, sleep=sleeps.append)
    failures = []
    gmail.message_details(["g1"], failures)
    assert len(failures) == 1 and len(service.sizes) == gmailapi.BATCH_ATTEMPTS
    assert sum(sleeps) >= 127 + 3 * 64  # 1 + 2 + … + 64, then 64 s three more times


def test_wrong_mailbox_is_rejected():
    service = FakeService({})
    service.getProfile = lambda userId: Req(lambda: {"emailAddress": "other@example.com"})
    with pytest.raises(gmailapi.GmailError, match="signed in as other@example.com"):
        account(service)


def test_retries_are_logged_and_long_waits_at_info(caplog):
    import logging

    caplog.set_level(logging.DEBUG, logger="mailcellar")
    results = [http_error(429)] * 5 + [raw_response("g1")]

    def run():
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    assert account(FakeService({}))._execute(Req(run))["id"] == "g1"
    levels = [(r.levelname, r.getMessage()) for r in caplog.records]
    assert all("429 (rate limit)" in m for _l, m in levels)
    assert [lvl for lvl, _m in levels] == ["DEBUG"] * 4 + ["INFO"]  # 1, 2, 4, 8 s; then 16 s


def test_prune_decision_moves_what_is_gone_but_held_mail():
    d = commands.prune_decision(
        ["g1", "g2", "g3"],
        100,
        {"g2": "audit", "x9": "audit"},
        force=False,
        max_fraction=0.1,
        where="Gmail",
    )
    # holds on mail not gone don't count
    assert d == commands.PruneDecision(("g1", "g3"), (("g2", "audit"),))
    nothing = commands.prune_decision([], 100, {}, force=False, max_fraction=0.1, where="Gmail")
    assert nothing == commands.PruneDecision()


def test_prune_decision_refuses_too_much_unless_forced_and_held_mail_doesnt_count():
    def decide(held, force=False):
        return commands.prune_decision(
            gone, 100, held, force=force, max_fraction=0.1, where="the server"
        )

    gone = [f"g{i}" for i in range(11)]
    refused = decide({})
    assert refused.move == () and refused.refusal == (
        "11 of 100 backed-up messages are gone from the server, more than 10%; "
        "re-run with --force if that is right"
    )
    assert decide({}, force=True).move == tuple(gone)
    held_one = decide({"g0": "audit"})
    assert held_one.refusal == "" and len(held_one.move) == 10  # 10 of 100 is not more than 10%
