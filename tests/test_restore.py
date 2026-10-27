"""mailcellar restore: backed-up mail back into Gmail."""

import base64
import csv
import email.utils
import sqlite3
from dataclasses import replace
from email.parser import BytesHeaderParser
from pathlib import Path

import pytest

from mailcellar import cli as mailcellar
from mailcellar import gmailapi
from mailcellar.backup import commands, gyb, sidecar
from mailcellar.dedup.db import open_db
from mailcellar.dedup.index import index_backup
from mailcellar.offload.apply import OFFLOAD_LABEL
from mailcellar.report.db import open_sidecars
from mailcellar.restore import apply as restore
from test_backup import ACCOUNT, MS, FakeGmail, FakeService, Req, http_error, raw_response
from test_index import make_eml

A, B = "a@x.com", "b@y.com"
LABELS = {
    "INBOX": "INBOX",
    "TRASH": "TRASH",
    "CHAT": "CHAT",
    "YELLOW_STAR": "YELLOW_STAR",
    "Label_1": "work",
    "Label_2": OFFLOAD_LABEL,
}
BATCH = "2026-01-15-093000"


def mid(raw_b64: str) -> str:
    raw = base64.urlsafe_b64decode(raw_b64)
    return " ".join(str(BytesHeaderParser().parsebytes(raw).get("Message-ID", "")).split())


class Gmail(FakeGmail):
    """FakeGmail plus the writes restore needs; matches imports by Message-ID like Gmail."""

    def __init__(self, messages, account=A):
        super().__init__(messages)
        self.account = account
        self.labels = dict(LABELS)
        self.calls = []
        self.imported = 0
        self.crash_after_import = False

    def label_names(self, label_ids):
        return [self.labels[i] for i in label_ids if i in self.labels]

    def label_catalogue(self):
        return [
            {"id": i, "name": n, "type": "system" if i == n else "user"}
            for i, n in self.labels.items()
        ]

    def message_labels(self, ids):
        return {
            gid: set(self.label_names(self.messages[gid]["labelIds"]))
            | ({"TRASH"} if gid in self.trashed else set())
            if gid in self.messages
            else None
            for gid in ids
        }

    def find_message_id(self, message_id):
        return [g for g, m in self.messages.items() if mid(m["raw"]) == message_id]

    def list_message_ids(self, query=None, include_spam_trash=False, label_id=None):
        self.listings = getattr(self, "listings", []) + [query]
        if query == "in:trash":
            return [g for g in self.messages if g in self.trashed]
        return super().list_message_ids(query, include_spam_trash, label_id)

    def find_message_ids(self, message_ids):
        self.calls.append(("search", len(message_ids)))
        return {k: self.find_message_id(m) for k, m in message_ids.items()}

    def untrash(self, ids):
        self.calls.append(("untrash", sorted(ids)))
        self.trashed -= set(ids)
        return []

    def label_ids(self, names):
        out = []
        for name in names:
            found = [i for i, n in self.labels.items() if n == name]
            if not found:
                self.labels[f"Label_{len(self.labels) + 1}"] = name
                found = [f"Label_{len(self.labels)}"]
            out.append(found[0])
        return out

    def add_label(self, ids, name):
        self.calls.append(("label", sorted(ids), name))
        (label_id,) = self.label_ids([name])
        for gid in ids:
            self.messages[gid]["labelIds"].append(label_id)

    def remove_label(self, ids, name):
        self.calls.append(("unlabel", sorted(ids), name))
        for gid in ids:
            self.messages[gid]["labelIds"] = [
                i for i in self.messages[gid]["labelIds"] if self.labels.get(i) != name
            ]

    def import_messages(self, messages, on_imported):
        failed = []
        for key, (raw, label_ids) in messages.items():
            b64 = base64.urlsafe_b64encode(raw).decode()
            existing = self.find_message_id(mid(b64))
            if existing:  # Gmail returns the message holding that Message-ID
                new_id = existing[0]
                self.trashed.discard(new_id)
            else:
                self.imported += 1
                new_id = f"n{self.imported}"
                self.messages[new_id] = raw_response(new_id, label_ids=label_ids, body=raw)
            self.calls.append(("import", key, new_id, sorted(label_ids)))
            if self.crash_after_import:
                raise gmailapi.GmailError("connection lost")
            on_imported(key, new_id)
        return failed


def message(gid, labels=("Label_1",), body=None):
    return raw_response(gid, label_ids=list(labels), body=body or make_eml(msgid=f"<{gid}@x>"))


@pytest.fixture
def env(tmp_path, monkeypatch):
    """
    Six backed-up messages, five of them offloaded:
      t1 still in Trash; g1, g2 permanently deleted; o1 moved out of Trash
      by hand; c1 deleted, while another message (x1) has its Message-ID
      with other content. k1 is ordinary mail.
    """
    monkeypatch.chdir(tmp_path)
    msgs = {g: message(g) for g in ("t1", "g1", "o1", "c1", "k1")}
    msgs["g2"] = message("g2", labels=("Label_1", "INBOX", "YELLOW_STAR", "Label_2"))
    gmail = Gmail(msgs)
    folder = tmp_path / "data" / A
    commands.backup(gmail, A, folder)
    con = sidecar.open_meta(folder)
    sidecar.record_offloaded(con, ["t1", "g1", "g2", "o1", "c1"], BATCH, "old work", 1_000)
    con.commit()
    con.close()
    gmail.trashed.add("t1")
    for gid in ("g1", "g2", "c1"):
        del gmail.messages[gid]
    gmail.messages["x1"] = message("x1", body=make_eml(msgid="<c1@x>", body="something else"))

    gmails = {A: gmail, B: Gmail({}, account=B)}
    monkeypatch.setattr(
        gmailapi,
        "resolve_account",
        lambda root, name: (B, Path("cfg-b")) if name in ("b", B) else (A, Path("cfg")),
    )
    monkeypatch.setattr(gmailapi, "load_credentials", lambda f, account, scope: scope)
    scopes = []

    def account(name, scope):
        scopes.append(scope)
        return gmails[name]

    monkeypatch.setattr(gmailapi, "GmailAccount", account)
    gmail.folder, gmail.scopes, gmail.other = folder, scopes, gmails[B]
    return gmail


def run(*args):
    return mailcellar.main(["restore", A, "--config", "cfg", "--log", "log.csv", *args])


def log():
    with open("log.csv", newline="", encoding="utf-8") as fh:
        return {r["source_id"]: (r["outcome"], r["detail"]) for r in csv.DictReader(fh)}


def restored_rows(folder):
    con = sqlite3.connect(folder / "meta.sqlite")
    return {s: (n, how) for s, n, how in con.execute("SELECT source_id, new_id, how FROM restored")}


def test_dry_run_says_what_it_would_do_and_changes_nothing(env, capsys):
    assert run("--batch", BATCH) == 1  # c1 is skipped
    got = log()
    assert got["t1"][0] == "would untrash"
    assert got["o1"][0] == "would record (already in Gmail)"
    assert got["g1"][0] == got["g2"][0] == "would import"
    assert got["c1"][0] == "skipped" and "other content (x1)" in got["c1"][1]
    assert "k1" not in got
    assert [c[0] for c in env.calls] == ["search"] and env.scopes == ["readonly"]
    assert restored_rows(env.folder) == {}
    captured = capsys.readouterr()
    assert "Nothing in Gmail or the backup was changed" in captured.out
    # Progress lines go to stderr, unless --quiet.
    assert captured.err.startswith("Checked 5 backup copies")
    assert "Searching Gmail for the Message-IDs" in captured.err
    run("--batch", BATCH, "--quiet")
    assert capsys.readouterr().err == ""


def test_restore_untrashes_imports_and_records(env):
    assert run("--batch", BATCH, "--yes", "--label", "restored/today") == 1  # c1 still skipped
    assert env.scopes == ["modify"]
    assert ("untrash", ["t1"]) in env.calls
    assert "t1" not in env.trashed
    rows = restored_rows(env.folder)
    assert rows["t1"] == ("t1", "untrashed")
    assert rows["o1"] == ("o1", "in gmail")
    assert {rows["g1"][1], rows["g2"][1]} == {"imported"}
    assert "c1" not in rows

    # Labels: g2's superstar and offload labels are dropped.
    imports = {c[1]: c[3] for c in env.calls if c[0] == "import"}
    assert sorted(env.label_names(imports["g2"])) == ["INBOX", "work"]
    assert "labels not restored" in log()["g2"][1]
    done = sorted({rows[g][0] for g in ("t1", "o1", "g1", "g2")})
    assert ("unlabel", done, OFFLOAD_LABEL) in env.calls
    assert ("label", done, "restored/today") in env.calls

    # Offloaded no more; the old IDs stay in the backup, kept from --prune.
    con = sidecar.open_meta(env.folder)
    assert sidecar.offloaded_ids(con) == {"c1"}
    assert sidecar.kept_ids(con) == {"t1", "g1", "g2", "o1", "c1"}
    con.close()


def test_a_label_gmail_refuses_costs_that_label_not_the_restore(env, monkeypatch, capsys):
    real = env.label_ids

    def label_ids(names):
        if "work" in names:
            raise gmailapi.GmailError(f"{A}: Gmail refused the label 'work': HTTP 409")
        return real(names)

    monkeypatch.setattr(env, "label_ids", label_ids)
    run("--id", "g1", "--yes")
    outcome, detail = log()["g1"]
    assert outcome == "imported" and "work (Gmail refused it)" in detail
    assert "Gmail refused the label 'work'" in capsys.readouterr().err


def test_imported_message_is_indexed_in_the_backup_sharing_the_file(env):
    run("--id", "g1", "--yes")
    new_id = restored_rows(env.folder)["g1"][0]
    paths = {bm.gmail_id: bm.path for bm in gyb.iter_backup(env.folder)}
    assert paths["g1"].samefile(paths[new_id])
    assert sidecar.current_fields(env.folder)[new_id]["message_id"] == "g1@x"
    from mailcellar.backup import chain

    last = chain.parse_line(chain.read_lines(env.folder)[-1])
    assert last.op == "restore" and [g for g, _h in last.added] == [new_id]
    assert chain.check(env.folder, A).failures == ()

    # The next backup doesn't download the restored message; count sees nothing missing.
    before = len(env.fetched)
    commands.backup(env, A, env.folder)
    assert env.fetched[before:] == ["x1"]  # only the fixture's unrelated message
    c = commands.count(env, A, env.folder)
    assert c["missing"] == [] and c["restored"] == 1
    assert c["only_in_backup"] == 0  # g1's old ID is kept, not "only in backup"

    # Reports and dedup see the message once.
    con, _accounts = open_sidecars([env.folder])
    ids = {g for (g,) in con.execute("SELECT gmail_id FROM all_messages")}
    assert new_id in ids and "g1" not in ids
    dcon = open_db(Path("dedup.sqlite"))
    index_backup(dcon, env.folder)
    indexed = {g for (g,) in dcon.execute("SELECT gmail_id FROM messages")}
    assert new_id in indexed and "g1" not in indexed
    con.close()
    dcon.close()


def test_untrashed_message_counts_as_restored_and_is_done(env):
    run("--id", "t1", "--yes")
    c = commands.count(env, A, env.folder)
    assert c["offloaded"] == 4 and c["restored"] == 1
    assert c["offloaded_in_gmail"] == 1  # o1 (out of Trash by hand, still offloaded)
    with pytest.raises(SystemExit, match="Nothing to do"):
        run("--id", "t1", "--yes")


def test_crash_after_import_is_found_next_time_not_uploaded_twice(env):
    env.crash_after_import = True
    with pytest.raises(SystemExit, match="connection lost"):
        run("--id", "g1", "--yes")
    assert restored_rows(env.folder) == {}
    env.crash_after_import = False
    run("--id", "g1", "--yes")
    assert env.imported == 1
    assert restored_rows(env.folder)["g1"] == ("n1", "found")


def test_an_interrupted_restore_leaves_its_log(env, monkeypatch):
    real = env.import_messages

    def import_then_interrupt(messages, on_imported):
        def first_then_stop(key, new_id):
            on_imported(key, new_id)
            raise KeyboardInterrupt

        return real(messages, first_then_stop)

    monkeypatch.setattr(env, "import_messages", import_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        run("--offloaded", "--yes")
    got = log()
    # Written as it happened: the untrash and the first upload, and what was
    # decided before anything ran; not the upload that never happened.
    assert got["t1"][0] == "untrashed" and got["c1"][0] == "skipped"
    assert [g for g in ("g1", "g2") if got.get(g, ("",))[0] == "imported"] == ["g1"]
    assert "g2" not in got


# ── dates ────────────────────────────────────────────────────────────────────

MS_2010 = 1_271_584_485_514  # 2010-04-18 09:54:45 UTC


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # no Date: added after the last header, with the message's line endings
        (b"From: a\r\nSubject: s\r\n\r\nbody\r\n",
         b"From: a\r\nSubject: s\r\nDate: Sun, 18 Apr 2010 09:54:45 GMT\r\n\r\nbody\r\n"),
        (b"From: a\n\nbody\n", b"From: a\nDate: Sun, 18 Apr 2010 09:54:45 GMT\n\nbody\n"),
        # unreadable (a Spanish month), folded: replaced where it was
        (b"From: a\r\nDate: 18 Abr 10\r\n 09:54:44\r\nTo: b\r\n\r\nbody",
         b"From: a\r\nDate: Sun, 18 Apr 2010 09:54:45 GMT\r\nTo: b\r\n\r\nbody"),
    ],
)  # fmt: skip
def test_a_message_without_a_usable_date_gets_the_backups(raw, expected):
    assert restore.dated(raw, MS_2010) == expected


@pytest.mark.parametrize(
    ("raw", "ms"),
    [
        (b"Date: Sun, 18 Apr 2010 10:54:44 +0100\r\n\r\nbody", MS_2010),  # readable: kept
        (b"From: a\r\n\r\nbody", 0),  # nothing recorded (an mbox entry)
        (b"From: a\r\n\r\nX-Note: Date: not a header\r\n", 0),
    ],
)
def test_a_message_with_a_usable_date_or_no_recorded_one_is_left_alone(raw, ms):
    assert restore.dated(raw, ms) is None


def test_an_undated_message_is_restored_on_the_backups_date(env):
    run("--id", "g1", "--yes")
    new_id = restored_rows(env.folder)["g1"][0]
    uploaded = base64.urlsafe_b64decode(env.messages[new_id]["raw"])
    (backed_up,) = [
        bm.path.read_bytes() for bm in gyb.iter_backup(env.folder) if bm.gmail_id == "g1"
    ]
    assert b"Date:" not in backed_up  # the backup's file is left as it was
    date = BytesHeaderParser().parsebytes(uploaded)["Date"]
    assert date == email.utils.formatdate(MS / 1000, usegmt=True)  # the date the backup recorded
    assert "Date header added from the backup's date" in log()["g1"][1]


def test_damaged_backup_copy_is_not_uploaded(env):
    (path,) = [bm.path for bm in gyb.iter_backup(env.folder) if bm.gmail_id == "g1"]
    path.write_bytes(path.read_bytes().replace(b"Hello there", b"Hello thera"))
    assert run("--id", "g1", "--yes") == 1
    assert log()["g1"] == ("skipped", restore.CHANGED_SINCE_RECORDED)
    assert env.imported == 0


def test_a_header_or_link_changed_in_the_backup_copy_is_not_uploaded(env):
    # The content hash ignores headers and links; the hash chain doesn't.
    (path,) = [bm.path for bm in gyb.iter_backup(env.folder) if bm.gmail_id == "g1"]
    path.write_bytes(path.read_bytes().replace(b"Subject: ", b"X-Added: 1\nSubject: ", 1))
    assert restore.check_backup(the_candidate(env, "g1"))[0] == restore.CHANGED_SINCE_RECORDED
    assert run("--id", "g1", "--yes") == 1
    assert log()["g1"] == ("skipped", restore.CHANGED_SINCE_RECORDED)
    assert env.imported == 0


def test_without_a_chain_the_content_hash_is_the_check(env):
    from mailcellar.backup import chain

    chain.chain_path(env.folder).unlink()  # a backup from before the chain
    (path,) = [bm.path for bm in gyb.iter_backup(env.folder) if bm.gmail_id == "g1"]
    path.write_bytes(path.read_bytes().replace(b"Subject: ", b"X-Added: 1\nSubject: ", 1))
    assert restore.check_backup(the_candidate(env, "g1"))[0] == ""  # headers aren't in it
    path.write_bytes(path.read_bytes().replace(b"Hello there", b"Hello thera"))
    assert (
        restore.check_backup(the_candidate(env, "g1"))[0] == "backup copy differs from the sidecar"
    )


def test_limit_takes_the_oldest_first(env, monkeypatch):
    con = sidecar.open_meta(env.folder)
    con.execute("UPDATE messages SET internal_date = 5 WHERE gmail_id = 'g2'")
    con.commit()
    con.close()
    run("--offloaded", "--limit", "1")
    assert list(log()) == ["g2"]


def test_needs_a_selection():
    with pytest.raises(SystemExit):
        run()


def test_offloading_again_clears_the_restore():
    con = sqlite3.connect(":memory:")
    con.executescript(sidecar._SCHEMA)
    sidecar.record_offloaded(con, ["m1"], "b1", "r", 1)
    sidecar.record_restored(con, "m1", "m1", "untrashed", "run", 2)
    assert sidecar.offloaded_ids(con) == set()
    sidecar.record_offloaded(con, ["m1"], "b2", "r", 3)
    assert sidecar.offloaded_ids(con) == {"m1"}
    assert con.execute("SELECT count(*) FROM restored").fetchone() == (0,)


def test_v7_and_v8_sidecars_gain_the_restore_record_repeatably(tmp_path):
    """v8 adds offloaded.restored_ms; v9 rebuilds v8's restored table with a target."""
    folder = tmp_path / "data" / A
    commands.backup(FakeGmail({"g1": raw_response("g1")}), A, folder)
    con = sqlite3.connect(folder / "meta.sqlite")
    con.executescript(
        """ALTER TABLE offloaded DROP COLUMN restored_ms;
           DROP TABLE restored;
           CREATE TABLE restored (source_id TEXT PRIMARY KEY, new_id TEXT NOT NULL,
               how TEXT NOT NULL, restored_ms INTEGER NOT NULL, run TEXT NOT NULL);
           INSERT INTO restored VALUES ('g9', 'n9', 'imported', 5, 'r');
           UPDATE meta SET value = '7' WHERE name = 'schema_version';"""
    )
    con.close()
    for _ in range(2):
        sidecar.open_meta(folder).close()
        con = sqlite3.connect(folder / "meta.sqlite")
        assert "restored_ms" in {r[1] for r in con.execute("PRAGMA table_info(offloaded)")}
        assert con.execute("SELECT source_id, target, new_id FROM restored").fetchall() == [
            ("g9", "", "n9")
        ]
        assert con.execute("SELECT value FROM meta WHERE name = 'schema_version'").fetchone() == (
            sidecar.SCHEMA_VERSION,
        )
        con.close()


def test_import_labels_keeps_what_import_accepts():
    system = {"INBOX", "TRASH", "CHAT", "YELLOW_STAR", "CATEGORY_SOCIAL", "UNREAD"}
    keep, dropped = restore.import_labels(
        ["INBOX", "UNREAD", "CATEGORY_SOCIAL", "TRASH", "CHAT", "YELLOW_STAR", "TODO", "a/b"],
        system,
    )
    assert keep == ["INBOX", "UNREAD", "CATEGORY_SOCIAL", "TODO", "a/b"]
    assert dropped == ["TRASH", "CHAT", "YELLOW_STAR"]


# ── gmailapi.import_messages ─────────────────────────────────────────────────


class ImportService(FakeService):
    def __init__(self):
        super().__init__({})
        self.imports = []

    def import_(self, userId, body, media_body=None, **params):
        n = len(self.imports)
        self.imports.append((body, media_body is not None, params))
        return Req(lambda: {"id": f"new{n}"})


def test_import_messages_batches_small_and_uploads_large_alone():
    service = ImportService()
    gmail = gmailapi.GmailAccount(ACCOUNT, None, service=service, sleep=lambda s: None)
    big = b"x" * (gmailapi.IMPORT_INLINE_LIMIT + 1)
    got = {}
    failed = gmail.import_messages(
        {"s1": (b"small", ["INBOX"]), "s2": (big, [])}, lambda k, n: got.__setitem__(k, n)
    )
    assert failed == [] and set(got) == {"s1", "s2"}
    (small_body, small_media, params), (big_body, big_media, _) = service.imports
    assert base64.urlsafe_b64decode(small_body["raw"]) == b"small" and not small_media
    assert "raw" not in big_body and big_media
    assert params["internalDateSource"] == "dateHeader" and params["neverMarkSpam"] is True
    assert service.batches == 1


WITH_ID = b"Message-ID: <a@example.com>\r\nSubject: one\r\n\r\nbody\r\n"
NO_ID = b"Subject: two\r\n\r\nbody\r\n"


class FlakyImportService(FakeService):
    """Each import's first sends fail with the errors given for its subject."""

    def __init__(self, errors):
        super().__init__({})
        self.errors = errors  # subject → exceptions, raised by the first sends in turn
        self.sent = []  # subjects, one per import that reached Gmail

    def import_(self, userId, body, media_body=None, **params):
        raw = media_body._fd.getvalue() if media_body else base64.urlsafe_b64decode(body["raw"])
        subject = BytesHeaderParser().parsebytes(raw)["Subject"]

        def run():
            self.sent.append(subject)
            if self.errors.get(subject):
                raise self.errors[subject].pop(0)
            return {"id": f"new-{subject}"}

        return Req(run)


def flaky_import(errors, messages):
    service = FlakyImportService(errors)
    gmail = gmailapi.GmailAccount(ACCOUNT, None, service=service, sleep=lambda s: None)
    got = {}
    failed = gmail.import_messages(messages, lambda k, n: got.__setitem__(k, n))
    return service, got, failed


def test_import_without_message_id_is_not_resent_after_a_server_error():
    # Gmail may have stored it before failing; with no Message-ID to match a
    # second import against, sending it again would store a second copy.
    service, got, failed = flaky_import(
        {"one": [http_error(500)], "two": [http_error(500)]},
        {"k1": (WITH_ID, []), "k2": (NO_ID, [])},
    )
    assert got == {"k1": "new-one"}
    assert service.sent.count("one") == 2 and service.sent.count("two") == 1
    assert failed == [f"k2: HTTP 500; {gmailapi.MAYBE_IMPORTED}"]


def test_import_without_message_id_is_resent_after_a_rate_limit():
    service, got, failed = flaky_import({"two": [http_error(429)]}, {"k2": (NO_ID, [])})
    assert got == {"k2": "new-two"} and failed == [] and service.sent == ["two", "two"]


def test_large_import_without_message_id_is_not_resent_after_a_timeout():
    big = NO_ID + b"x" * gmailapi.IMPORT_INLINE_LIMIT
    service, got, failed = flaky_import({"two": [TimeoutError("timed out")]}, {"k2": (big, [])})
    assert got == {} and service.sent == ["two"]
    assert failed == [f"k2: a timeout; {gmailapi.MAYBE_IMPORTED}"]


def test_failed_import_batch_resends_only_messages_with_a_message_id():
    service = FlakyImportService({})
    make_batch = service.new_batch_http_request

    def stalling_batch(callback):
        batch = make_batch(callback)
        if service.batches == 1:  # the first batch request stalls

            def stall():
                for _rid, req in batch.items:  # after Gmail ran the imports
                    req.execute()
                raise TimeoutError("timed out")

            batch.execute = stall
        return batch

    service.new_batch_http_request = stalling_batch
    gmail = gmailapi.GmailAccount(ACCOUNT, None, service=service, sleep=lambda s: None)
    got = {}
    failed = gmail.import_messages(
        {"k1": (WITH_ID, []), "k2": (NO_ID, [])}, lambda k, n: got.__setitem__(k, n)
    )
    assert got == {"k1": "new-one"} and service.batches == 2
    assert service.sent == ["one", "two", "one"]
    assert failed == [f"k2: a timeout; {gmailapi.MAYBE_IMPORTED}"]


def test_has_message_id():
    assert gmailapi.has_message_id(WITH_ID)
    assert not gmailapi.has_message_id(NO_ID)
    assert not gmailapi.has_message_id(b"Message-ID: \r\n\r\nbody")


# ── whole backups, other accounts, labels ────────────────────────────────────


def test_all_leaves_mail_still_in_gmail_alone(env, capsys, monkeypatch):
    # Large selections read Gmail's state from two ID listings, not one lookup each.
    lookups = env.message_labels
    monkeypatch.setattr(restore, "LISTING_THRESHOLD", 0)
    monkeypatch.setattr(env, "message_labels", lambda ids: pytest.fail("looked up one by one"))
    run("--all")
    assert "in:trash" in env.listings
    got = log()
    assert got["k1"][0] == "already in Gmail" and "x1" not in got  # x1 was never backed up
    assert got["t1"][0] == "would untrash" and got["o1"][0] == "would record (already in Gmail)"
    assert got["g1"][0] == "would import"
    assert "Would upload 2 messages" in capsys.readouterr().out
    monkeypatch.setattr(env, "message_labels", lookups)  # apply() looks up what it found
    run("--all", "--yes")
    assert [c for c in env.calls if c[0] == "import"] and "k1" not in restored_rows(env.folder)
    labelled = {g for c in env.calls if c[0] == "label" for g in c[1]}
    assert "k1" not in labelled


def test_restore_into_another_account(env):
    other = env.other
    other.messages["b1"] = message("b1", body=make_eml(msgid="<g1@x>"))  # B already has g1
    assert run("--offloaded", "--to", "b", "--yes", "--label-prefix", "old") == 0
    imports = {c[1]: c for c in other.calls if c[0] == "import"}
    assert set(imports) == {"t1", "g2", "o1", "c1"}  # IDs mean nothing in B; g1 was found
    assert sorted(other.label_names(imports["g2"][3])) == ["INBOX", "old/work"]
    assert env.calls == []  # A's Gmail untouched: no untrash, no label changes

    # Recorded against B; A's offload record is unchanged.
    con = sidecar.open_meta(env.folder)
    assert sidecar.restored_ids(con, B) == {"t1", "g1", "g2", "o1", "c1"}
    found = con.execute("SELECT new_id, how FROM restored WHERE source_id = 'g1'").fetchone()
    assert found == ("b1", "found")
    assert sidecar.restored_ids(con) == set()
    assert sidecar.offloaded_ids(con) == {"t1", "g1", "g2", "o1", "c1"}
    con.close()

    # B's backup has the new IDs; its next backup downloads only b1, B's own mail.
    b_folder = env.folder.parent / B
    assert {bm.gmail_id for bm in gyb.iter_backup(b_folder)} == set(other.messages) - {"b1"}
    before = len(other.fetched)  # restore read b1 to compare its content
    commands.backup(other, B, b_folder)
    assert other.fetched[before:] == ["b1"]
    with pytest.raises(SystemExit, match="Nothing to do"):
        run("--offloaded", "--to", "b")


def test_strip_labels_restores_only_the_restored_label(env):
    run("--id", "g2", "--yes", "--strip-labels", "--label", "restored/x")
    (imp,) = [c for c in env.calls if c[0] == "import"]
    assert imp[3] == []
    new_id = restored_rows(env.folder)["g2"][0]
    assert env.label_names(env.messages[new_id]["labelIds"]) == ["restored/x"]


class SearchService(FakeService):
    def __init__(self):
        super().__init__({})
        self.queries = []

    def list(self, userId, **kwargs):
        if "q" not in kwargs:
            return super().list(userId, **kwargs)
        self.queries.append(kwargs["q"])
        found = {"rfc822msgid:<a@x>": [{"id": "m1"}]}.get(kwargs["q"], [])
        return Req(lambda: {"messages": found} if found else {})


def test_find_message_ids_searches_in_batches():
    service = SearchService()
    gmail = gmailapi.GmailAccount(ACCOUNT, None, service=service, sleep=lambda s: None)
    got = gmail.find_message_ids({"s1": "<a@x>", "s2": "<b@x>"})
    assert got == {"s1": ["m1"], "s2": []}
    assert service.batches == 1 and len(service.queries) == 2


def test_message_without_text_or_attachments_is_restorable(env):
    from mailcellar.backup import chain

    con = sidecar.open_meta(env.folder)
    con.execute("UPDATE messages SET content_hash = '' WHERE gmail_id = 'g1'")
    con.commit()
    con.close()
    (path,) = [bm.path for bm in gyb.iter_backup(env.folder) if bm.gmail_id == "g1"]
    path.write_bytes(make_eml(msgid="<g1@x>", body=""))
    chain.chain_path(env.folder).unlink()  # the file was rewritten: as if from before the chain
    run("--id", "g1")
    assert log()["g1"][0] == "would import"


# ── decide(): the way back, from what was read ───────────────────────────────


def cand(offloaded=True, content_hash="h1"):
    return restore.Candidate(
        "s1", "b", "r", Path("x.eml"), ("work",), 1, content_hash, "7", offloaded=offloaded
    )


COPY = restore.Copy(10, "<m@x>")  # a readable backup copy with a Message-ID


def way(
    c=None,
    problem="",
    copy=COPY,
    labels=None,
    same_account=True,
    hits=(),
    hashes=None,
):
    d = restore.decide(
        c or cand(),
        problem,
        copy,
        labels=labels,
        same_account=same_account,
        hits=list(hits),
        hashes=hashes or {},
    )
    return d.way, d.new_id, d.detail


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (
            {"problem": "no .eml in the backup", "copy": None},
            ("skipped", "", "no .eml in the backup"),
        ),
        ({"labels": {"TRASH"}}, ("untrash", "s1", "")),
        ({"labels": {"INBOX"}}, ("in gmail", "s1", "already out of Trash")),
        ({"labels": {"TRASH"}, "c": cand(offloaded=False)}, ("present", "", "in Gmail's Trash")),
        ({"hits": ["n1"], "hashes": {"n1": "h1"}}, ("found", "n1", "Gmail has it as n1")),
        (
            {"hits": ["n1"], "hashes": {"n1": "other"}},
            (
                "skipped",
                "",
                "Gmail has its Message-ID "
                "with other content (n1); an import would only match that message",
            ),
        ),
        ({}, ("import", "", "")),
        (
            {"copy": restore.Copy(10, "")},
            ("import", "", "no Message-ID: a crash during the import could leave a duplicate"),
        ),
        # into another account, its own ID's state doesn't count
        ({"labels": {"TRASH"}, "same_account": False}, ("import", "", "")),
    ],
)
def test_decide(kwargs, expected):
    assert way(**kwargs) == expected


def test_empty_content_never_counts_as_the_same():
    w, _id, detail = way(cand(content_hash=""), hits=["n1"], hashes={"n1": ""})
    assert w == "skipped" and "can't be compared" in detail


def test_values_are_frozen():
    with pytest.raises(AttributeError):
        cand().source_id = "other"


# ── the backup copy can't be used ────────────────────────────────────────────


def the_candidate(env, gid="t1"):
    (c,) = restore.candidates(env.folder, ids=(gid,))
    return c


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"path": None}, "no .eml in the backup"),
        ({"parser_version": ""}, "not in the sidecar; run mailcellar meta"),
        ({"parser_version": "0"}, "sidecar row is stale; run mailcellar meta"),
        ({"content_hash": "not-the-hash"}, "backup copy differs from the sidecar"),
    ],
)
def test_an_unusable_backup_copy_is_named(env, change, problem):
    assert restore.check_backup(replace(the_candidate(env), **change)) == (problem, None)


def test_a_backup_copy_that_cannot_be_read_is_named(env):
    c = replace(the_candidate(env), path=env.folder)  # a folder, not a file
    problem, copy = restore.check_backup(c)
    assert problem.startswith("backup copy doesn't parse: ") and copy is None


def test_a_usable_backup_copy_gives_its_size_and_message_id(env):
    problem, copy = restore.check_backup(the_candidate(env))
    assert problem == "" and copy.message_id == "<t1@x>"
    assert copy.size == the_candidate(env).path.stat().st_size


def test_unusable_copies_are_skipped_in_a_run_and_nothing_is_changed(env):
    next(env.folder.rglob("g1.eml")).unlink()
    con = sidecar.open_meta(env.folder)
    con.execute("UPDATE messages SET parser_version = '0' WHERE gmail_id = 'g2'")
    con.commit()
    con.close()
    assert run("--id", "g1", "--id", "g2", "--yes") == 1
    assert log()["g1"] == ("skipped", "no .eml in the backup")
    assert log()["g2"] == ("skipped", "sidecar row is stale; run mailcellar meta")
    assert restored_rows(env.folder) == {} and env.imported == 0


def test_an_old_sidecar_schema_asks_for_meta(env):
    sql = sqlite3.connect(env.folder / "meta.sqlite")
    sql.execute("UPDATE meta SET value = '8' WHERE name = 'schema_version'")
    sql.commit()
    sql.close()
    with pytest.raises(sidecar.MetaError, match="run mailcellar meta to migrate it"):
        restore.candidates(env.folder)


# ── narrowing the selection ──────────────────────────────────────────────────


def ids_of(cands):
    return [c.source_id for c in cands]


def test_selection_narrows_by_batch_rule_and_id(env):
    everyone = ["t1", "g1", "g2", "o1", "c1"]
    assert sorted(ids_of(restore.candidates(env.folder))) == sorted(everyone)  # all offloaded
    assert sorted(ids_of(restore.candidates(env.folder, batches=(BATCH,)))) == sorted(everyone)
    assert restore.candidates(env.folder, batches=("2000-01-01-000000",)) == []
    assert sorted(ids_of(restore.candidates(env.folder, rules=("old work",)))) == sorted(everyone)
    assert restore.candidates(env.folder, rules=("another rule",)) == []
    assert ids_of(restore.candidates(env.folder, ids=("g1", "nope"))) == ["g1"]
    # each narrowing applies together with the others
    assert restore.candidates(env.folder, batches=(BATCH,), rules=("another rule",)) == []
    assert ids_of(restore.candidates(env.folder, batches=(BATCH,), ids=("g1",))) == ["g1"]
    assert restore.candidates(env.folder, rules=("old work",), ids=("k1",)) == []  # not offloaded


def test_selecting_everything_adds_mail_that_was_never_offloaded(env):
    assert "k1" in ids_of(restore.candidates(env.folder, everything=True))
    assert ids_of(restore.candidates(env.folder, everything=True, ids=("k1",))) == ["k1"]
    k1 = restore.candidates(env.folder, everything=True, ids=("k1",))[0]
    assert (k1.offloaded, k1.batch, k1.rule) == (False, "", "")


def test_an_id_is_looked_for_in_the_whole_backup_unless_offloaded_only(env):
    assert sorted(ids_of(restore.candidates(env.folder, ids=("k1", "g1")))) == ["g1", "k1"]
    assert ids_of(restore.candidates(env.folder, ids=("k1", "g1"), offloaded_only=True)) == ["g1"]
    assert restore.candidates(env.folder, ids=("x1",)) == []  # never backed up


def test_restoring_an_id_never_offloaded_leaves_it_alone_while_gmail_has_it(env):
    run("--id", "k1")
    assert log()["k1"][0] == "already in Gmail"
    with pytest.raises(SystemExit, match="Nothing to do"):
        run("--id", "k1", "--offloaded")


def test_the_oldest_messages_come_first(env):
    got = restore.candidates(env.folder, everything=True)
    assert [(c.internal_date, c.source_id) for c in got] == sorted(
        (c.internal_date, c.source_id) for c in got
    )


def test_a_rule_and_a_limit_narrow_a_run(env):
    assert run("--rule", "old work", "--limit", "2") == 1  # c1, the oldest by ID, is skipped
    assert sorted(log()) == ["c1", "g1"]  # a dry run of just the two oldest
    with pytest.raises(SystemExit, match="Nothing to do"):
        run("--rule", "another rule")


# ── a restore that goes wrong part of the way ────────────────────────────────


def test_an_untrash_that_fails_is_reported_and_not_recorded(env):
    real = env.untrash
    env.untrash = lambda ids: [f"{i}: HttpError 500" for i in ids] if "t1" in ids else real(ids)
    assert run("--id", "t1", "--id", "o1", "--yes") == 1
    assert log()["t1"] == ("failed", "t1: HttpError 500")
    assert log()["o1"][0] == "recorded"  # a failure doesn't stop the others
    assert "t1" in env.trashed
    assert set(restored_rows(env.folder)) == {"o1"}
    done = [c for c in env.calls if c[0] == "label"][-1]
    assert done[1] == ["o1"]  # only what was restored is relabelled
    con = sidecar.open_meta(env.folder)
    assert "t1" in sidecar.offloaded_ids(con)  # still offloaded: it can be tried again
    con.close()
    env.untrash = real
    assert run("--id", "t1", "--yes") == 0
    assert "t1" not in env.trashed and restored_rows(env.folder)["t1"] == ("t1", "untrashed")


def test_an_import_that_fails_is_reported_and_not_indexed(env):
    real = env.import_messages

    def fail_g1(messages, on_imported):
        messages.pop("g1")
        return [*real(messages, on_imported), "g1: HttpError 500"]

    env.import_messages = fail_g1
    assert run("--id", "g1", "--id", "g2", "--yes") == 1
    assert log()["g1"] == ("failed", "g1: HttpError 500")
    assert log()["g2"][0] == "imported"
    assert set(restored_rows(env.folder)) == {"g2"}
    indexed = {bm.gmail_id for bm in gyb.iter_backup(env.folder)}
    assert "n1" in indexed and env.imported == 1  # g2's copy only
    con = sidecar.open_meta(env.folder)
    assert "g1" in sidecar.offloaded_ids(con)  # still offloaded
    con.close()


def test_when_every_import_fails_nothing_is_indexed(env):
    before = {bm.gmail_id for bm in gyb.iter_backup(env.folder)}
    env.import_messages = lambda messages, on_imported: [f"{k}: HttpError 500" for k in messages]
    assert run("--id", "g1", "--id", "g2", "--yes") == 1
    assert {log()[g][0] for g in ("g1", "g2")} == {"failed"}
    assert restored_rows(env.folder) == {}
    assert {bm.gmail_id for bm in gyb.iter_backup(env.folder)} == before


def test_a_backup_copy_changed_after_the_dry_run_is_not_uploaded(env):
    cands = restore.candidates(env.folder, ids=("g1",))
    decisions = restore.plan(env, cands)
    assert [d.way for d in decisions] == ["import"]
    eml = cands[0].path
    eml.write_bytes(eml.read_bytes().replace(b"Hello there", b"Changed since"))
    results = restore.apply(env, env.folder, decisions, "run", "restored/today")
    assert [(r.outcome, r.detail) for r in results] == [("skipped", restore.CHANGED_SINCE_RECORDED)]
    assert env.imported == 0 and restored_rows(env.folder) == {}


def test_a_new_id_gmail_cannot_describe_is_recorded_but_not_indexed(env):
    env.message_details = lambda ids, failures=None: {i: None for i in ids}
    assert run("--id", "g1", "--yes") == 0
    new_id = restored_rows(env.folder)["g1"][0]
    assert new_id == "n1"
    assert new_id not in {bm.gmail_id for bm in gyb.iter_backup(env.folder)}
    assert new_id not in sidecar.current_fields(env.folder)  # the next backup fetches it


def test_progress_is_reported_while_checking_many_copies(env, monkeypatch, capsys):
    from mailcellar import progress

    monkeypatch.setattr(progress, "DELAY", 0)
    monkeypatch.setattr(progress, "LOG_EVERY", 0)
    messages = []
    restore.plan(env, restore.candidates(env.folder), progress=messages.append)
    err = capsys.readouterr().err  # not a terminal: progress is logged
    assert "Checking backup copies: 2 of 5 (40%)" in err
    assert "Checking backup copies: 5 of 5 in 0:00" in err
    assert messages[0] == "Checked 5 backup copies; 0 unusable"


# ── narrowing by date, label and sender ──────────────────────────────────────

DAY = 86_400_000
T0 = 1_700_006_400_000  # 2023-11-15 00:00 UTC


def narrowed(**kwargs):
    return restore.Narrowing(**kwargs)


def mine(**changes):
    return replace(cand(), **changes)


def test_an_empty_narrowing_keeps_everything_and_is_false():
    assert not narrowed() and restore.matches(mine(), narrowed())
    assert narrowed(before_ms=0) and narrowed(labels=("x",)) and narrowed(senders=("a@b.c",))


def test_dates_are_before_exclusive_and_after_inclusive():
    on = mine(internal_date=T0)
    assert restore.matches(on, narrowed(after_ms=T0))  # from this moment on
    assert not restore.matches(on, narrowed(before_ms=T0))  # older than it, not equal
    assert restore.matches(on, narrowed(before_ms=T0 + 1, after_ms=T0))
    assert not restore.matches(on, narrowed(after_ms=T0 + 1))
    assert not restore.matches(on, narrowed(before_ms=T0, after_ms=T0 - DAY))


def test_labels_match_user_labels_by_name_ignoring_case_and_any_one_will_do():
    c = mine(labels=("Work", "INBOX", "CATEGORY_PROMOTIONS", "projects/2026"))
    assert restore.matches(c, narrowed(labels=("work",)))
    assert restore.matches(c, narrowed(labels=("WORK",)))
    assert restore.matches(c, narrowed(labels=("nope", "Projects/2026")))
    assert not restore.matches(c, narrowed(labels=("nope",)))
    assert not restore.matches(c, narrowed(labels=("inbox",)))  # a system label isn't a user label
    assert not restore.matches(c, narrowed(labels=("category_promotions",)))
    assert not restore.matches(mine(labels=()), narrowed(labels=("work",)))


def test_senders_match_an_address_or_every_address_at_a_domain():
    c = mine(sender="bob@example.org")
    assert restore.matches(c, narrowed(senders=("bob@example.org",)))
    assert restore.matches(c, narrowed(senders=("BOB@Example.ORG",)))
    assert restore.matches(c, narrowed(senders=("@example.org",)))
    assert restore.matches(c, narrowed(senders=("nobody@example.com", "@example.org")))
    assert not restore.matches(c, narrowed(senders=("ob@example.org",)))  # not a suffix match
    assert not restore.matches(c, narrowed(senders=("@mail.example.org",)))
    assert not restore.matches(
        mine(sender="bob@mail.example.org"), narrowed(senders=("@example.org",))
    )
    assert not restore.matches(
        mine(sender=""), narrowed(senders=("@example.org",))
    )  # no sidecar row


def test_every_condition_must_hold():
    c = mine(internal_date=T0, labels=("work",), sender="bob@example.org")
    both = narrowed(after_ms=T0, labels=("work",), senders=("@example.org",))
    assert restore.matches(c, both)
    for other in (
        replace(both, before_ms=T0),
        replace(both, labels=("home",)),
        replace(both, senders=("@example.com",)),
    ):
        assert not restore.matches(c, other)


@pytest.fixture
def varied(env):
    """The fixture's messages, given different dates and senders in the sidecar."""
    rows = {
        "t1": (T0, "alice@example.com"),
        "g1": (T0 + 10 * DAY, "bob@example.org"),
        "g2": (T0 + 20 * DAY, "Bob@Example.org"),
        "o1": (T0 + 30 * DAY, "carol@mail.example.org"),
        "c1": (T0 + 40 * DAY, "alice@example.com"),
        "k1": (T0 + 50 * DAY, "alice@example.com"),
    }
    con = sqlite3.connect(env.folder / "meta.sqlite")
    for gid, (date, sender) in rows.items():
        con.execute(
            "UPDATE messages SET internal_date = ?, sender_addr = ? WHERE gmail_id = ?",
            (date, sender, gid),
        )
    con.commit()
    con.close()
    return env


def pick(env, **narrow):
    everything = narrow.pop("everything", True)
    got = restore.candidates(env.folder, everything=everything, narrow=restore.Narrowing(**narrow))
    return ids_of(got)


def test_candidates_narrow_by_sender(varied):
    assert sorted(pick(varied, senders=("alice@example.com",))) == ["c1", "k1", "t1"]
    assert sorted(pick(varied, senders=("@example.org",))) == ["g1", "g2"]  # not carol's subdomain
    assert pick(varied, senders=("@mail.example.org",)) == ["o1"]
    assert sorted(pick(varied, senders=("carol@mail.example.org", "bob@example.org"))) == [
        "g1",
        "g2",
        "o1",
    ]


def test_candidates_narrow_by_date_oldest_first(varied):
    assert pick(varied, before_ms=T0 + 15 * DAY) == ["t1", "g1"]
    assert pick(varied, after_ms=T0 + 15 * DAY) == ["g2", "o1", "c1", "k1"]
    assert pick(varied, after_ms=T0 + 10 * DAY, before_ms=T0 + 30 * DAY) == ["g1", "g2"]
    assert pick(varied, after_ms=T0 + 60 * DAY) == []


def test_candidates_narrow_by_label(varied):
    assert pick(varied, labels=("WORK",)) == ["t1", "g1", "g2", "o1", "c1", "k1"]
    assert pick(varied, labels=(OFFLOAD_LABEL,)) == ["g2"]  # a user label, by name
    assert pick(varied, labels=("inbox",)) == []  # g2 has INBOX, a system label


def test_narrowing_combines_with_every_selection(varied):
    alice = ("alice@example.com",)
    assert sorted(pick(varied, senders=alice)) == ["c1", "k1", "t1"]  # --all
    got = restore.candidates(
        varied.folder, batches=(BATCH,), narrow=restore.Narrowing(senders=alice)
    )
    assert sorted(ids_of(got)) == ["c1", "t1"]  # k1 was never offloaded
    got = restore.candidates(
        varied.folder, ids=("t1", "g1"), everything=True, narrow=restore.Narrowing(senders=alice)
    )
    assert ids_of(got) == ["t1"]
    got = restore.candidates(
        varied.folder,
        rules=("old work",),
        narrow=restore.Narrowing(senders=alice, after_ms=T0 + DAY),
    )
    assert ids_of(got) == ["c1"]


def cli_ids():
    return set(log())


def fresh(*args):
    """run(), but with an empty restore log first, so cli_ids() is this run's messages."""
    Path("log.csv").unlink(missing_ok=True)
    return run(*args)


def test_the_command_narrows_an_offload_by_sender(varied):
    assert fresh("--offloaded", "--sender", "bob@example.org") == 0
    assert cli_ids() == {"g1", "g2"}


def test_the_command_narrows_everything_by_date(varied):
    assert fresh("--all", "--after", "2023-12-05", "--before", "2023-12-25") == 0
    assert cli_ids() == {"g2", "o1"}  # 12-05 is included, 12-25 is not
    assert fresh("--all", "--before", "2023-11-25") == 0
    assert "t1" in cli_ids() and "g1" not in cli_ids()


def test_has_label_is_not_the_label_restored_mail_gets(varied):
    assert fresh("--offloaded", "--has-label", OFFLOAD_LABEL, "--label", "restored/x") == 0
    assert cli_ids() == {"g2"}
    with pytest.raises(SystemExit, match="Nothing to do"):
        fresh("--offloaded", "--has-label", "nope")


def test_the_command_takes_repeated_senders_and_labels(varied):
    assert fresh("--all", "--sender", "bob@example.org", "--sender", "@mail.example.org") == 0
    assert cli_ids() == {"g1", "g2", "o1"}
    assert fresh("--all", "--has-label", "nope", "--has-label", OFFLOAD_LABEL) == 0
    assert cli_ids() == {"g2"}


def test_the_limit_applies_after_narrowing_and_takes_the_oldest(varied):
    assert fresh("--all", "--sender", "alice@example.com", "--limit", "2") == 1  # c1 is skipped
    assert cli_ids() == {"t1", "c1"}


def test_nothing_matching_is_nothing_to_do(varied):
    with pytest.raises(SystemExit, match="Nothing to do"):
        run("--all", "--sender", "nobody@example.net")


def test_narrowing_alone_is_not_a_selection(varied, capsys):
    with pytest.raises(SystemExit) as stopped:
        run("--before", "2023-12-05")
    assert stopped.value.code == 2
    assert "say which messages" in capsys.readouterr().err


def test_all_can_be_narrowed_but_not_combined_with_other_selections(varied, capsys):
    with pytest.raises(SystemExit):
        run("--all", "--batch", BATCH)
    assert "--before, --after, --has-label or --sender" in capsys.readouterr().err


def test_bad_dates_name_the_option(varied, capsys):
    for option in ("--before", "--after"):
        with pytest.raises(SystemExit) as stopped:
            run("--all", option, "soon")
        assert stopped.value.code == 2
        assert f"{option} 'soon': give a date" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        run("--all", "--after", "2024-01-01", "--before", "2023-01-01")
    assert "--after must be earlier than --before" in capsys.readouterr().err


def test_help_names_the_narrowing_options(capsys):
    with pytest.raises(SystemExit):
        run("--help")
    out = capsys.readouterr().out
    for option in ("--before", "--after", "--has-label", "--sender"):
        assert option in out


def test_in_another_account_a_message_with_the_same_id_is_another_copy(env):
    env.other.messages["g1"] = message("g1")  # B happens to hold g1 under the same ID
    assert run("--id", "g1", "--to", "b", "--yes") == 0
    assert not any(c[0] == "import" for c in env.other.calls)
    con = sidecar.open_meta(env.folder)
    found = con.execute("SELECT new_id, how FROM restored WHERE source_id = 'g1'").fetchone()
    con.close()
    assert found == ("g1", "found")


def test_restore_into_another_account_refuses_a_backup_it_had_that_is_missing(env):
    other = env.other
    b_folder = env.folder.parent / B
    commands.backup(other, B, b_folder)  # B has been backed up: its chain has an anchor
    b_folder.rename(env.folder.parent / "unmounted")  # its volume goes away
    with pytest.raises(SystemExit, match="isn't mounted"):
        run("--offloaded", "--to", "b", "--yes")
    assert not [c for c in other.calls if c[0] == "import"]  # nothing restored into Gmail
    assert not b_folder.exists()  # and no backup started in its place
