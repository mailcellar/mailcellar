"""IMAP step 3 (#113): writing to an IMAP account, where the server can do it safely."""

import csv
import imaplib
import sqlite3

import pytest

from fake_imap import FakeImap
from fake_mailbox import FakeMailbox, raw_response
from mailcellar import cli, gmailapi, imap, mailbox
from mailcellar.backup import commands, gyb, sidecar
from mailcellar.dedup.apply import DEDUP_LABEL, apply_plan
from mailcellar.offload import cli as offload_cli
from mailcellar.paths import backup_folder, data_root
from mailcellar.restore import apply as restore_apply
from test_imap_scan import account, ids_by_msgid
from test_index import make_eml

ADDRESS = "alex@example.org"


@pytest.fixture
def server():
    s = FakeImap()
    s.writable = True
    s.capabilities = ("IMAP4REV1",)
    s.signed_in = ("IMAP4rev1", "SPECIAL-USE", "MOVE", "UIDPLUS")
    s.add_folder("Trash", special=["\\Trash"])
    s.add_folder("Projects")
    return s


@pytest.fixture
def config(tmp_path, monkeypatch, server):
    """A configured IMAP account whose server is `server`."""
    folder = tmp_path / "config" / "home"
    folder.mkdir(parents=True)
    (folder / gmailapi.IMAP_SETTINGS).write_text('host = "imap.example.org"\n')
    password = folder / gmailapi.IMAP_PASSWORD
    password.write_text("secret\n")
    password.chmod(0o600)
    monkeypatch.setattr(imap.ImapAccount, "_connect_ssl", lambda self: server)
    return folder


def row(tmp_path, gid, labels=""):
    path = tmp_path / f"{gid}.eml"
    path.write_text("x")
    return {
        "action": "remove",
        "account": ADDRESS,
        "gmail_id": gid,
        "keeper_account": "",
        "keeper_gmail_id": "",
        "labels": labels,
        "path": str(path),
        "subject": "s",
    }


# ── opening for writing ──────────────────────────────────────────────────────


def test_a_server_with_move_and_a_marked_trash_opens_for_writing(config):
    box = mailbox.open_mailbox(ADDRESS, config, "modify")
    box.close()


@pytest.mark.parametrize(
    "change, problem",
    [
        (lambda s: setattr(s, "signed_in", ("IMAP4rev1", "SPECIAL-USE")), "no MOVE"),
        (lambda s: s.folders["Trash"]["flags"].remove("\\Trash"), "special-use \\\\Trash"),
    ],
)
def test_a_server_that_cant_move_safely_is_backed_up_only(config, server, change, problem):
    change(server)
    with pytest.raises(gmailapi.GmailError, match=problem):
        mailbox.open_mailbox(ADDRESS, config, "modify")
    assert server.commands[-1] == ("LOGOUT",)  # closed, not left open
    assert not [c for c in server.commands if c[0] == "SELECT"]


def by_folder(server, settings_review="folder", folder=None):
    """An ImapAccount on `server` reviewing by folder, its backup in `folder`."""
    settings = gmailapi.ImapSettings("imap.example.org", 993, ADDRESS, review=settings_review)
    return imap.ImapAccount(
        ADDRESS, settings, "secret", folder=folder, connect=lambda: server, sleep=lambda s: None
    )


def test_review_by_folder_moves_every_copy_and_records_where_it_was(server, tmp_path):
    server.add("INBOX", msgid="<a@x>", flags={"\\Seen"})
    server.add("Projects", msgid="<a@x>", flags={"\\Seen"})
    box = by_folder(server, folder=tmp_path)
    (gid,) = box.list_message_ids()
    box.add_label([gid], "offload-candidate")
    review = server.folders["mailcellar/review/offload-candidate"]["uids"]
    assert len(review) == 2 and not server.folders["INBOX"]["uids"]  # created, both moved
    assert sidecar.read_imap_reviews(tmp_path) == {
        gid: {"offload-candidate": (("INBOX", "INBOX"), ("Projects", "Projects"))}
    }
    message = by_folder(server, folder=tmp_path)._scan()[1].messages[gid]
    assert message.labels == {"INBOX", "Projects", "offload-candidate"}  # as on Gmail
    assert message.marks == {"offload-candidate"}


def test_review_by_folder_needs_the_backup_to_record_in(server):
    server.add("INBOX", msgid="<a@x>", flags={"\\Seen"})
    box = by_folder(server)
    with pytest.raises(gmailapi.GmailError, match="records where mail came from"):
        box.add_label(box.list_message_ids(), "offload-candidate")
    assert not [c for c in server.commands if c[0] in ("CREATE", "SELECT")]


def test_folder_review_label_then_trash_takes_only_what_is_still_in_review(server, tmp_path):
    server.add("INBOX", msgid="<a@x>", flags={"\\Seen"})
    server.add("Projects", msgid="<b@x>", flags={"\\Seen"})
    box = by_folder(server, folder=tmp_path)
    ids = ids_by_msgid(box, server)
    rows = [row(tmp_path, ids["<a@x>"]), row(tmp_path, ids["<b@x>"], labels="Projects")]
    assert {e.outcome for e in apply_plan(rows, "label", lambda a, s: box, lambda p: True)} == {
        "labelled"
    }
    review = "mailcellar/review/dedup-redundant"
    kept = next(u for u, m in server.folders[review]["uids"].items() if b"<b@x>" in m["raw"])
    server.move(review, kept, "Projects")  # the user keeps b@x: moves it back out
    box = by_folder(server, folder=tmp_path)
    log = apply_plan(rows, "trash", lambda a, s: box, lambda p: True)
    assert {e.row["gmail_id"]: e.outcome for e in log} == {
        ids["<a@x>"]: "trashed",
        ids["<b@x>"]: "skipped",
    }
    assert [b"<a@x>" in m["raw"] for m in server.folders["Trash"]["uids"].values()] == [True]


def test_folder_review_on_a_server_without_keywords_takes_the_mark_off_by_folder(server, tmp_path):
    server.add("INBOX", msgid="<a@x>", flags={"\\Seen"})
    server.permanentflags = b"(\\Seen \\Flagged)"  # it keeps no keywords
    box = by_folder(server, folder=tmp_path)
    (gid,) = box.list_message_ids()
    box.add_label([gid], "offload-candidate")
    by_folder(server, folder=tmp_path).remove_label([gid], "offload-candidate")
    assert len(server.folders["INBOX"]["uids"]) == 1  # moved back where it came from
    assert not [c for c in server.commands if c[:2] == ("UID", "STORE")]  # no keyword to take off


def test_a_bad_answer_is_a_failure_and_keeps_the_session(server):
    uid = server.add("INBOX", msgid="<a@x>", flags={"\\Seen", "$mailcellar-offload-candidate"})
    server.permanentflags = b"(\\Seen \\Flagged)"  # a STORE of the keyword is answered BAD
    box = account(server)
    (gid,) = box.list_message_ids()
    (failure,) = box.remove_label([gid], "offload-candidate")  # a refusal, not a crash
    assert failure.startswith(f"{gid}: taking offload-candidate off in INBOX failed")
    assert failure.endswith("in INBOX failed: UID: BAD arguments invalid")  # as a NO reads
    assert "$mailcellar-offload-candidate" in server.folders["INBOX"]["uids"][uid]["flags"]
    assert sum(c[0] == "LOGIN" for c in server.commands) == 1  # the session kept


def test_a_refused_move_out_of_review_fails_only_the_mail_it_was_taking(
    server, tmp_path, monkeypatch
):
    server.add("INBOX", msgid="<a@x>", flags={"\\Seen"})
    server.add("Projects", msgid="<b@x>", flags={"\\Seen"})
    box = by_folder(server, folder=tmp_path)
    ids = ids_by_msgid(box, server)
    box.add_label(list(ids.values()), "offload-candidate")
    refusing(server, monkeypatch, "MOVE", "Projects")
    box = by_folder(server, folder=tmp_path)
    (failure,) = box.remove_label(list(ids.values()), "offload-candidate")
    review = "mailcellar/review/offload-candidate"
    assert failure.startswith(f"{ids['<b@x>']}: moving it out of {review} to Projects failed")
    assert len(server.folders["INBOX"]["uids"]) == 1  # a@x went home all the same


def refusing(server, monkeypatch, command, folder):
    """The server answers NO to `command` (MOVE, COPY) into `folder`."""
    write = server._write

    def refuse(cmd, uids, wanted, *rest):
        if cmd == command and rest == (f'"{folder}"',):
            return "NO", [b"[CANNOT] not now"]
        return write(cmd, uids, wanted, *rest)

    monkeypatch.setattr(server, "_write", refuse)


def one_copy_two_homes(server, tmp_path):
    """A message reviewed by folder from INBOX and Projects, one copy left in review."""
    server.add("INBOX", msgid="<a@x>", flags={"\\Seen"})
    server.add("Projects", msgid="<a@x>", flags={"\\Seen"})
    box = by_folder(server, folder=tmp_path)
    (gid,) = box.list_message_ids()
    box.add_label([gid], "offload-candidate")
    review = server.folders["mailcellar/review/offload-candidate"]["uids"]
    review.pop(max(review))  # the user deleted the second copy
    return gid


def test_a_refused_copy_into_a_further_home_is_its_failure(server, tmp_path, monkeypatch):
    gid = one_copy_two_homes(server, tmp_path)
    refusing(server, monkeypatch, "COPY", "Projects")
    (failure,) = by_folder(server, folder=tmp_path).remove_label([gid], "offload-candidate")
    assert failure.startswith(f"{gid}: copying it to Projects failed")
    assert len(server.folders["INBOX"]["uids"]) == 1  # home, only a copy short


def test_a_message_whose_move_was_refused_is_copied_nowhere(server, tmp_path, monkeypatch):
    gid = one_copy_two_homes(server, tmp_path)
    refusing(server, monkeypatch, "MOVE", "INBOX")
    (failure,) = by_folder(server, folder=tmp_path).remove_label([gid], "offload-candidate")
    assert failure.startswith(f"{gid}: moving it out of mailcellar/review/")
    assert not server.folders["Projects"]["uids"]  # not copied out of review
    assert len(server.folders["mailcellar/review/offload-candidate"]["uids"]) == 1


@pytest.mark.parametrize("uidplus", [True, False])
def test_an_appended_messages_id_is_read_back_from_the_server(home, tmp_path, uidplus):
    home.append_adds = b"Content-Length: 1234\r\n"  # as one provider adds to appended mail
    if not uidplus:
        home.signed_in = tuple(c for c in home.signed_in if c != "UIDPLUS")
    offload_two(home, tmp_path)
    home.folders["Trash"]["uids"].clear()  # emptied: restore appends from the backup
    assert cli.main(["backup", "home"]) == 0
    assert cli.main(["restore", "home", "--offloaded", "--yes"]) == 0
    con = sqlite3.connect(backup_folder(ADDRESS) / "meta.sqlite")
    try:
        recorded = {n for (n,) in con.execute("SELECT new_id FROM restored")}
    finally:
        con.close()
    on_server = set(by_folder(home)._scan()[1].messages)
    assert len(recorded) == 2
    assert recorded <= on_server  # the server's IDs: by the APPEND's UID, else by Message-ID


def imap_box(server):
    """An ImapAccount on `server` reviewing by keyword, with no backup folder."""
    settings = gmailapi.ImapSettings("imap.example.org", 993, ADDRESS)
    return imap.ImapAccount(
        ADDRESS, settings, "secret", connect=lambda: server, sleep=lambda s: None
    )


def test_a_message_without_a_message_id_is_read_back_by_its_content(server):
    server.append_adds = b"Content-Length: 1\r\n"
    raw = b"Subject: no id\r\n\r\nbody\r\n"
    got = {}
    assert imap_box(server).import_messages({"k": (raw, ["INBOX"])}, got.__setitem__) == []
    (stored,) = [m["raw"] for m in server.folders["INBOX"]["uids"].values()]
    assert got == {"k": imap.content_id(stored)}  # what the server stored, not what was sent


def test_each_append_advances_the_run_it_belongs_to(server):
    from mailcellar import progress

    messages = {k: (make_eml(msgid=f"<{k}@x>").replace(b"\n", b"\r\n"), ["INBOX"]) for k in "abc"}
    with progress.Progress("Importing", 3, steps_inside=True) as p:
        assert imap_box(server).import_messages(messages, lambda k, i: None) == []
        assert p.done == 3  # one a message; the folder scans before and after don't count


def test_a_failed_read_back_keeps_the_prediction_and_says_so(server, monkeypatch, caplog):
    server.append_adds = b"Content-Length: 1\r\n"
    uid = server.uid

    def no_fetch(command, *args):
        if command.upper() == "FETCH":
            return "NO", [b"[UNAVAILABLE] try later"]
        return uid(command, *args)

    monkeypatch.setattr(server, "uid", no_fetch)
    raw = make_eml(msgid="<r@x>").replace(b"\n", b"\r\n")
    got = {}
    assert (
        imap_box(server).import_messages(
            {"k": (raw, ["INBOX"])}, got.__setitem__, {"k": 1_420_106_400_000}
        )
        == []
    )
    assert got == {"k": imap.appended_id(raw, 1_420_106_400_000)}
    assert "1 appended messages couldn't be read back" in caplog.text


def test_two_messages_sharing_a_message_id_are_read_back_apart(server):
    server.signed_in = tuple(c for c in server.signed_in if c != "UIDPLUS")  # no APPENDUID
    server.append_adds = b"Content-Length: 1\r\n"
    one = make_eml(msgid="<same@x>", body="first").replace(b"\n", b"\r\n")
    two = make_eml(msgid="<same@x>", body="the second, longer").replace(b"\n", b"\r\n")
    got = {}
    messages = {"a": (one, ["INBOX"]), "b": (two, ["INBOX"])}
    assert imap_box(server).import_messages(messages, got.__setitem__) == []
    on_server = set(imap_box(server)._scan()[1].messages)
    assert len(set(got.values())) == 2 and set(got.values()) <= on_server


def test_an_append_whose_answer_was_lost_is_not_stored_twice(server):
    server.drop_after_append = 1  # stored, then the connection drops before the OK
    raw = make_eml(msgid="<d@x>").replace(b"\n", b"\r\n")
    got = {}
    assert imap_box(server).import_messages({"k": (raw, ["INBOX"])}, got.__setitem__) == []
    assert len(server.folders["INBOX"]["uids"]) == 1  # the retry found it, and didn't append again
    assert set(got.values()) <= set(imap_box(server)._scan()[1].messages)


def test_an_append_uid_and_the_id_it_reads_back_are_decided_purely():
    assert imap.append_uid([b"[APPENDUID 7 42] Append completed"]) == 42
    assert imap.append_uid([b"Append completed"]) is None
    head = b"Message-ID: <a@x>\r\n\r\n"
    got = imap.Fetched(uid=42, internal=1_420_106_400_000, size=99, literal=head)
    assert imap.server_id(got, True) == imap.header_id("<a@x>", 1_420_106_400_000, 99)
    assert imap.server_id(None, True) is None  # nothing read back
    assert imap.server_id(imap.Fetched(uid=42, literal=b"x"), False) == imap.content_id(b"x")


def test_appended_mail_sharing_a_message_id_gets_a_uid_each():
    raw = b"Message-ID: <same@x>\r\n\r\nbody"
    ms = 1_420_106_400_000
    items = [imap.Appended("a", "INBOX", None, raw, ms), imap.Appended("b", "INBOX", None, raw, ms)]
    head = b"Message-ID: <same@x>\r\n\r\n"
    fetched = {u: imap.Fetched(uid=u, internal=ms, size=40, literal=head) for u in (7, 8)} | {
        6: imap.Fetched(uid=6, internal=ms, size=40, literal=b"Message-ID: <other@x>\r\n\r\n")
    }
    assert imap.match_appended(items, fetched) == {"a": 7, "b": 8}  # in order, one each
    assert imap.already_stored(fetched, imap.appended_key(raw, ms), earlier=1) == 8
    assert imap.already_stored(fetched, imap.appended_key(raw, ms), earlier=2) is None
    assert imap.since({3: fetched[6], 9: fetched[7]}, 5) == {9: fetched[7]}  # n:* holds the highest


def test_putting_back_fails_each_move_and_copy_on_its_own():
    plan = imap.PutBack(
        {"Trash": {"INBOX": (1,), "Projects": (2,)}},
        {("Trash", "INBOX"): frozenset({"a"}), ("Trash", "Projects"): frozenset({"b"})},
        {"a": ("INBOX", "Archive"), "b": ("Projects",)},
        (),
    )
    assert imap.put_back_failures(plan, {("Trash", "Projects"): "no"}, {}) == {
        "b": "b: moving it out of Trash to Projects failed: no"  # not a, moved to INBOX
    }
    assert imap.put_back_failures(plan, {}, {"a": ("Archive", "full")}) == {
        "a": "a: copying it to Archive failed: full"  # home, only a copy short
    }


def test_further_homes_are_copied_into_only_where_missing():
    plan = imap.PutBack(
        {},
        {},
        {"a": ("INBOX", "Archive", "Projects"), "b": ("INBOX", "Projects"), "c": ("INBOX",)},
        (),
    )
    more = imap.further_homes(plan, frozenset({"b"}))  # b stuck in Trash: copy it nowhere
    assert more == {"a": ("Archive", "Projects")}  # c has one home: MOVE filled it

    def message(gid, *places):
        return imap.Message(gid, frozenset(), places[0], places=places)

    scan = imap.Scan(
        {"a": message("a", ("INBOX", 4), ("Projects", 9)), "z": message("z", ("INBOX", 5))},
        frozenset(),
        {},
    )
    # from the copy it fetches from; not into Projects, which has one already; z, not in
    # `more`, gets none
    assert imap.further_copies(more, scan) == (("a", "INBOX", 4, "Archive"),)
    assert imap.further_copies({"gone": ("Archive",)}, scan) == ()  # not on the server now


def test_a_bad_answer_to_a_read_stops_loudly(server, monkeypatch):
    server.add("INBOX", msgid="<a@x>", flags={"\\Seen"})
    uid = server.uid

    def bad_fetch(command, *args):
        if command.upper() == "FETCH":
            raise imaplib.IMAP4.error("UID command error: BAD [b'invalid fetch']")
        return uid(command, *args)

    monkeypatch.setattr(server, "uid", bad_fetch)
    with pytest.raises(imaplib.IMAP4.error, match="BAD"):  # not a folder quietly skipped
        account(server).list_message_ids()


@pytest.mark.parametrize(
    "exc, answer",
    [
        (imaplib.IMAP4.error("UID command error: BAD [b'[CLIENTBUG] invalid']"), True),
        (imaplib.IMAP4.error("command UID illegal in state AUTH"), False),  # mailcellar's own
        (imaplib.IMAP4.error("Unknown IMAP4 UID command: STOR"), False),
        (imaplib.IMAP4.abort("socket error: EOF"), False),
        (OSError("timed out"), False),
    ],
)
def test_only_the_servers_bad_keeps_the_session(exc, answer):
    assert imap.answered(exc) is answer


@pytest.mark.parametrize(
    "text, said",
    [
        ("UID command error: BAD [b'[CLIENTBUG] invalid']", "UID: BAD [CLIENTBUG] invalid"),
        ("STORE command error: BAD [b'one', b'two']", "STORE: BAD one two"),
        ('STORE command error: BAD [b"it\'s"]', "STORE: BAD it's"),
        ("STORE command error: BAD [b'caf\\xc3\\xa9']", "STORE: BAD caf\u00e9"),
        ("command UID illegal in state AUTH", "command UID illegal in state AUTH"),
    ],
)
def test_the_servers_words_are_shown_as_it_sent_them(text, said):
    assert imap.server_said(imaplib.IMAP4.error(text)) == said


def test_an_unreadable_message_is_said_to_be_the_servers(server, tmp_path, caplog):
    server.add("INBOX", msgid="<a@x>", flags={"\\Seen"})
    box = account(server)
    (gid,) = box.list_message_ids()
    read = box.message_labels

    def one_unread(ids, failures=None):
        failures.append(f"{gid}: timed out")
        return {i: v for i, v in read(ids).items() if i != gid}

    box.message_labels = one_unread
    (entry,) = apply_plan([row(tmp_path, gid)], "label", lambda a, s: box, lambda p: True)
    assert (entry.outcome, entry.detail) == (
        "skipped",
        "couldn't read it from its mailbox: timed out",
    )
    assert "1 messages couldn't be read from the server" in caplog.text


# ── marking and trashing ─────────────────────────────────────────────────────


def test_a_mark_goes_on_every_copy_and_comes_back_as_the_label(server):
    one = server.add("INBOX", msgid="<a@x>", flags={"\\Seen"})
    server.add("Projects", msgid="<a@x>", flags={"\\Seen"})  # the same message, filed twice
    box = account(server)
    gid = ids_by_msgid(box, server)["<a@x>"]
    box.add_label([gid], "offload-candidate")
    assert "$mailcellar-offload-candidate" in server.folders["INBOX"]["uids"][one]["flags"]
    assert all(
        "$mailcellar-offload-candidate" in m["flags"]
        for m in server.folders["Projects"]["uids"].values()
    )
    assert "offload-candidate" in box.message_labels([gid])[gid]  # read again, not cached


def test_a_server_that_keeps_no_keywords_is_told_to_review_by_folder(server):
    server.add("INBOX", msgid="<a@x>", flags={"\\Seen"})
    server.permanentflags = b"(\\Seen \\Flagged)"
    box = account(server)
    with pytest.raises(gmailapi.GmailError, match="--review folder"):
        box.add_label(box.list_message_ids(), "offload-candidate")


def test_a_folder_keeping_no_keywords_fails_its_own_mail_only(server):
    server.add("INBOX", msgid="<a@x>", flags={"\\Seen"})
    server.add("Projects", msgid="<b@x>", flags={"\\Seen"})
    server.keywordless.add("Projects")  # PERMANENTFLAGS is per folder
    box = account(server)
    ids = ids_by_msgid(box, server)
    for review in (True, False):  # offload's mark, and restore's
        label = "offload-candidate" if review else "restored/2026-01-01"
        (failure,) = box.add_label(list(ids.values()), label, review=review)
        assert failure.startswith(f"{ids['<b@x>']}: marking {label} in Projects failed")
        assert "keeps no keywords" in failure
        assert label in box.message_labels([ids["<a@x>"]])[ids["<a@x>"]]  # marked all the same


def test_mail_only_in_keywordless_folders_fails_while_inbox_keeps_them(server):
    server.add("Projects", msgid="<b@x>", flags={"\\Seen"})
    server.keywordless.add("Projects")  # the only folder touched; INBOX keeps keywords
    box = account(server)
    (failure,) = box.add_label(box.list_message_ids(), "offload-candidate")
    assert "Projects keeps no keywords" in failure  # its own mail, not the account's setup
    server.keywordless.add("INBOX")
    with pytest.raises(imap.KeywordsRefused, match="INBOX, Projects.*--review folder"):
        account(server).add_label(box.list_message_ids(), "offload-candidate")


def test_an_inbox_check_that_fails_concludes_nothing(server, monkeypatch):
    server.add("Projects", msgid="<b@x>", flags={"\\Seen"})
    server.keywordless.add("Projects")
    select = server.select

    def inbox_refused(mailbox="INBOX", readonly=False):
        if mailbox == '"INBOX"' and not readonly:
            server._command("SELECT", mailbox)
            return "NO", [b"[UNAVAILABLE] try later"]
        return select(mailbox, readonly)

    monkeypatch.setattr(server, "select", inbox_refused)
    box = account(server)
    (failure,) = box.add_label(box.list_message_ids(), "offload-candidate")  # no raise
    assert "Projects keeps no keywords" in failure  # its own failure, as when INBOX keeps them
    assert ("SELECT", '"INBOX"') in server.commands  # it did check


def test_a_keywordless_folder_and_a_refusing_one_fail_their_own(server, monkeypatch):
    server.add_folder("Archive")
    server.add("INBOX", msgid="<a@x>", flags={"\\Seen"})
    server.add("Projects", msgid="<b@x>", flags={"\\Seen"})
    server.add("Archive", msgid="<c@x>", flags={"\\Seen"})
    server.keywordless.add("Projects")
    write = server._write

    def refuse_archive(command, uids, wanted, *rest):
        if command == "STORE" and server.selected == "Archive":
            return "NO", [b"[CANNOT] not now"]
        return write(command, uids, wanted, *rest)

    monkeypatch.setattr(server, "_write", refuse_archive)
    box = account(server)
    ids = ids_by_msgid(box, server)
    failures = sorted(box.add_label(list(ids.values()), "offload-candidate"))
    assert [f.split(": ", 1)[0] for f in failures] == sorted([ids["<b@x>"], ids["<c@x>"]])
    assert "offload-candidate" in box.message_labels([ids["<a@x>"]])[ids["<a@x>"]]


def test_trash_moves_every_copy_and_confirms(server):
    server.add("INBOX", msgid="<a@x>", flags={"\\Seen"})
    server.add("Projects", msgid="<a@x>", flags={"\\Seen"})
    server.add("INBOX", msgid="<b@x>", flags={"\\Seen"})
    box = account(server)
    ids = ids_by_msgid(box, server)
    assert box.trash([ids["<a@x>"], "0000000000000000"]) == ["0000000000000000: not on the server"]
    assert (
        len(server.folders["Trash"]["uids"]) == 2 and len(server.folders["Projects"]["uids"]) == 0
    )
    assert box.message_labels([ids["<a@x>"]])[ids["<a@x>"]] == {"TRASH"}
    assert box.message_labels([ids["<b@x>"]])[ids["<b@x>"]] == {"INBOX"}  # left alone
    moves = [c for c in server.commands if c[:2] == ("UID", "MOVE")]
    assert {c[3] for c in moves} == {'"Trash"'}


def test_nothing_is_trashed_while_a_folder_couldnt_be_read(server):
    server.add("INBOX", msgid="<a@x>", flags={"\\Seen"})
    server.refused.add("Projects")  # a copy might be there
    box = account(server)
    (gid,) = box.list_message_ids()
    (failure,) = box.trash([gid])
    assert "folders couldn't be read (Projects)" in failure
    assert not [c for c in server.commands if c[:2] == ("UID", "MOVE")]


# ── through apply_plan, as offload and dedup apply use it ────────────────────


def test_label_then_trash_takes_only_what_is_still_marked(server, tmp_path):
    server.add("INBOX", msgid="<a@x>", flags={"\\Seen"})
    server.add("INBOX", msgid="<b@x>", flags={"\\Seen"})
    box = account(server)
    ids = ids_by_msgid(box, server)
    rows = [row(tmp_path, ids["<a@x>"]), row(tmp_path, ids["<b@x>"])]
    log = apply_plan(rows, "label", lambda a, s: box, confirm=lambda p: True)
    assert {e.outcome for e in log} == {"labelled"}
    for m in server.folders["INBOX"]["uids"].values():  # the user keeps b@x in their client
        if b"<b@x>" in m["raw"]:
            m["flags"].discard(f"$mailcellar-{DEDUP_LABEL}")
    box = account(server)
    log = apply_plan(rows, "trash", lambda a, s: box, confirm=lambda p: True)
    assert {e.row["gmail_id"]: e.outcome for e in log} == {
        ids["<a@x>"]: "trashed",
        ids["<b@x>"]: "skipped",
    }
    assert [b"<a@x>" in m["raw"] for m in server.folders["Trash"]["uids"].values()] == [True]
    assert not [c for c in server.commands if c[0] in ("EXPUNGE", "DELETE")]


def test_a_refused_move_fails_its_folders_messages_and_the_rest_carry_on(server, monkeypatch):
    server.add("INBOX", msgid="<a@x>", flags={"\\Seen"})
    server.add("Projects", msgid="<b@x>", flags={"\\Seen"})
    box = account(server)
    ids = ids_by_msgid(box, server)
    move = server._write

    def over_quota(command, uids, wanted, *rest):
        if command == "MOVE" and server.selected == "Projects":
            return "NO", [b"[OVERQUOTA] Trash is full"]
        return move(command, uids, wanted, *rest)

    monkeypatch.setattr(server, "_write", over_quota)
    (failure,) = box.trash([ids["<a@x>"], ids["<b@x>"]])
    assert failure.startswith(f"{ids['<b@x>']}: moving it out of Projects failed")
    assert box.message_labels([ids["<a@x>"]])[ids["<a@x>"]] == {"TRASH"}  # it got through


def test_trash_confirms_messages_told_apart_by_content(server):
    # Same Message-ID, date and size, different bytes: the scan gives each a content ID.
    server.add_folder("Archive")
    server.add("INBOX", raw=make_eml(msgid="<same@x>", body="one!"), flags={"\\Seen"})
    server.add("Archive", raw=make_eml(msgid="<same@x>", body="two!"), flags={"\\Seen"})
    box = account(server)
    inbox = next(g for g, m in box._scan()[1].messages.items() if m.where[0] == "INBOX")
    assert box.trash([inbox]) == []  # confirmed under the same ID, not "not in Trash"
    assert box.message_labels([inbox])[inbox] == {"TRASH"}


def test_meta_fills_an_imap_thread_from_the_backed_up_message(server, tmp_path):
    raw = make_eml(msgid="<r2@x>", headers=[("In-Reply-To", "<root@x>")])
    server.add("INBOX", raw=raw, flags={"\\Seen"})
    folder = tmp_path / "backup"
    box = account(server, folder)
    (gid,) = box.list_message_ids()
    con = gyb.open_backup(folder, ADDRESS)
    box.get_raw([gid], lambda r: gyb.write_message(con, folder, r, ["INBOX"]))
    con.commit()
    con.close()
    thread = account(server, folder).message_details([gid])[gid]["threadId"]
    assert thread == imap.thread_of(raw, gid) != gid  # the conversation's, not its own


def test_the_trash_plan_is_decided_before_anything_moves(server):
    server.add("INBOX", msgid="<a@x>", flags={"\\Seen"})
    server.add("Projects", msgid="<a@x>", flags={"\\Seen"})
    server.add("Trash", msgid="<a@x>", flags={"\\Seen"})  # a copy already there stays
    scan = account(server)._scan()[1]
    (gid,) = scan.messages
    plan = imap.trash_plan(scan, [gid, "0000000000000000"])
    assert plan.trash == "Trash" and plan.moves == {"INBOX": (1,), "Projects": (1,)}
    assert plan.owners == {"INBOX": {gid}, "Projects": {gid}}
    assert plan.failures == ("0000000000000000: not on the server",) and plan.confirm == (gid,)


# ── offload apply, end to end on an IMAP account ────────────────────────────


@pytest.fixture
def home(server, tmp_path, monkeypatch):
    """A configured IMAP account `home` on `server`, three old messages in Projects."""
    root = tmp_path / "config"
    (root / "home").mkdir(parents=True)
    (root / "accounts.toml").write_text(f'home = "{ADDRESS}"\n')
    (root / "home" / gmailapi.IMAP_SETTINGS).write_text('host = "imap.example.org"\n')
    (root / "home" / gmailapi.IMAP_PASSWORD).write_text("secret\n")
    (root / "home" / gmailapi.IMAP_PASSWORD).chmod(0o600)
    monkeypatch.setenv("MAILCELLAR_CONFIG", str(root))
    monkeypatch.setattr(imap.ImapAccount, "_connect_ssl", lambda self: server)
    monkeypatch.setattr(offload_cli, "second_copy_report", lambda d, ms: ["(report)"])
    monkeypatch.chdir(tmp_path)
    for n in range(3):
        server.add("Projects", msgid=f"<p{n}@x>", flags={"\\Seen"}, date=OLD)
    return server


OLD = "01-Jan-2015 10:00:00 +0000"


def offload_two(server, tmp_path):
    """Back up, plan the three, label them, keep one, trash the other two."""
    assert cli.main(["backup", "home"]) == 0
    (tmp_path / "rules.toml").write_text(
        '[[rule]]\nname = "old"\nlabel = "Projects"\nbefore = "2020-01-01"\n'
    )
    offload = ["offload", "apply", "--plan", "plan.csv", "--log", "log.csv"]
    cli.main(["offload", "plan", "--rules", "rules.toml", "--plan", "plan.csv"])
    cli.main([*offload, "--label"])
    marked = [
        m
        for m in server.folders["Projects"]["uids"].values()
        if "$mailcellar-offload-candidate" in m["flags"]
    ]
    assert len(marked) == 3
    marked[0]["flags"].discard("$mailcellar-offload-candidate")  # kept, in the mail client
    assert cli.main(["backup", "home"]) == 0  # trashing wants a backup newer than the plan
    cli.main([*offload, "--trash", "--yes"])


def test_offload_on_an_imap_account_labels_trashes_and_records(home, tmp_path):
    offload_two(home, tmp_path)
    assert len(home.folders["Trash"]["uids"]) == 2 and len(home.folders["Projects"]["uids"]) == 1
    with open("log.csv", newline="", encoding="utf-8") as fh:
        outcomes = sorted(r["outcome"] for r in csv.DictReader(fh) if r["mode"].startswith("trash"))
    assert outcomes == ["skipped", "trashed", "trashed"]
    con = sqlite3.connect(backup_folder(ADDRESS) / "meta.sqlite")
    try:
        assert con.execute("SELECT count(*) FROM offloaded").fetchone() == (2,)
    finally:
        con.close()


def test_offload_label_on_a_keywordless_folder_fails_its_mail_and_goes_on(home, tmp_path):
    home.keywordless.add("Projects")  # INBOX keeps keywords: not the account's setup
    assert cli.main(["backup", "home"]) == 0
    (tmp_path / "rules.toml").write_text(
        '[[rule]]\nname = "old"\nlabel = "Projects"\nbefore = "2020-01-01"\n'
    )
    cli.main(["offload", "plan", "--rules", "rules.toml", "--plan", "plan.csv"])
    cli.main(["offload", "apply", "--plan", "plan.csv", "--log", "log.csv", "--label"])
    with open("log.csv", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 3 and {r["outcome"] for r in rows} == {"failed"}
    assert all("Projects keeps no keywords" in r["detail"] for r in rows)


def test_a_rule_selects_what_is_in_an_imap_account_s_inbox(home, tmp_path):
    for n in range(2):
        home.add("INBOX", msgid=f"<i{n}@x>", flags={"\\Seen"}, date=OLD)
    assert cli.main(["backup", "home"]) == 0
    (tmp_path / "rules.toml").write_text(
        '[[rule]]\nname = "inbox"\nlabel = "Inbox"\nbefore = "2020-01-01"\n'
    )
    cli.main(["offload", "plan", "--rules", "rules.toml", "--plan", "plan.csv"])
    with open("plan.csv", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 2  # the two in INBOX, none of the three in Projects


def test_a_server_without_move_or_a_marked_trash_opens_for_uploads(config, server):
    server.signed_in = ("IMAP4rev1",)
    server.folders["Trash"]["flags"].remove("\\Trash")
    box = mailbox.open_mailbox(ADDRESS, config, "append")
    assert not box.can_move()
    box.close()
    with pytest.raises(gmailapi.GmailError, match="no MOVE"):
        mailbox.open_mailbox(ADDRESS, config, "modify")  # offload and dedup still refused


# ── restore into an IMAP account ─────────────────────────────────────────────


def test_restore_takes_offloaded_mail_out_of_trash_home(home, tmp_path, capsys):
    offload_two(home, tmp_path)
    out = capsys.readouterr().out
    assert "The server frees their space when its Trash is emptied." in out
    assert cli.main(["restore", "home", "--offloaded", "--yes"]) == 0
    out, err = capsys.readouterr()
    assert "Reading the server's state" in err and "Gmail" not in err
    assert "where the server keeps keywords, they carry 'restored/" in out
    assert not home.folders["Trash"]["uids"] and len(home.folders["Projects"]["uids"]) == 3
    flags = [m["flags"] for m in home.folders["Projects"]["uids"].values()]
    assert not any("$mailcellar-offload-candidate" in f for f in flags)  # the mark taken off
    assert sum(any(k.startswith("$mailcellar-restored_") for k in f) for f in flags) == 2
    assert cli.main(["backup", "home"]) == 0
    capsys.readouterr()
    assert cli.main(["count", "home"]) == 0
    out = capsys.readouterr().out
    assert "Restored to the server" in out and "Gmail" not in out


def test_restore_appends_mail_the_server_no_longer_has(home, tmp_path, capsys):
    offload_two(home, tmp_path)
    home.folders["Trash"]["uids"].clear()  # the server, or a mail client, emptied Trash
    assert cli.main(["backup", "home"]) == 0
    capsys.readouterr()
    assert cli.main(["restore", "home", "--offloaded", "--yes"]) == 0
    err = capsys.readouterr().err
    assert "Searching the server for other copies" in err and "Gmail" not in err
    back = home.folders["Projects"]["uids"].values()
    assert len(back) == 3 and all("\\Seen" in m["flags"] for m in back)
    assert {m["date"] for m in back} == {OLD}  # the Date header's date, as Gmail's import takes it
    capsys.readouterr()
    assert cli.main(["backup", "home"]) == 0
    assert "Downloaded 0 messages" in capsys.readouterr().out  # the same IDs: nothing new


def test_without_move_restore_appends_and_leaves_trash_alone(home, tmp_path, capsys):
    offload_two(home, tmp_path)
    home.signed_in = tuple(c for c in home.signed_in if c != "MOVE")
    moved = len([c for c in home.commands if c[:2] == ("UID", "MOVE")])  # by the offload
    capsys.readouterr()
    assert cli.main(["restore", "home", "--offloaded", "--yes"]) == 1
    assert "has no MOVE" in capsys.readouterr().out
    assert len(home.folders["Trash"]["uids"]) == 2 and len(home.folders["Projects"]["uids"]) == 1
    home.folders["Trash"]["uids"].clear()  # emptied: now only an upload brings them back
    assert cli.main(["backup", "home"]) == 0
    assert cli.main(["restore", "home", "--offloaded", "--yes"]) == 0
    assert len(home.folders["Projects"]["uids"]) == 3
    assert len([c for c in home.commands if c[:2] == ("UID", "MOVE")]) == moved


def test_a_large_restore_reads_the_servers_state_from_its_listing(
    home, tmp_path, capsys, monkeypatch
):
    offload_two(home, tmp_path)
    monkeypatch.setattr(restore_apply, "LISTING_THRESHOLD", 0)  # as for over 1,000 messages
    assert cli.main(["restore", "home", "--offloaded", "--yes"]) == 0
    assert not home.folders["Trash"]["uids"] and len(home.folders["Projects"]["uids"]) == 3


@pytest.mark.parametrize("many", [False, True])
def test_restore_appends_nothing_while_a_folder_cant_be_read(
    home, tmp_path, capsys, monkeypatch, many
):
    offload_two(home, tmp_path)
    if many:  # answered from the listing, not one by one
        monkeypatch.setattr(restore_apply, "LISTING_THRESHOLD", 0)
    home.refused.add("Trash")  # the two offloaded messages are there, unseen
    cli.main(["restore", "home", "--offloaded", "--yes"])
    assert len(home.folders["Projects"]["uids"]) == 1  # the one kept: nothing appended
    assert len(home.folders["Trash"]["uids"]) == 2
    with next(data_root().rglob("restore-log.csv")).open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert {r["outcome"] for r in rows} == {"skipped"}
    assert all("folders couldn't be read (Trash)" in r["detail"] for r in rows)


# ── folder review: the review's findings ─────────────────────────────────────


def test_only_the_review_a_message_is_in_now_gives_it_labels(server, tmp_path):
    server.add_folder("Archive")
    server.add("Archive", msgid="<a@x>", flags={"\\Seen"})
    box = by_folder(server, folder=tmp_path)
    (gid,) = box.list_message_ids()
    con = sidecar.open_meta(tmp_path)  # an old, finished dedup review, from INBOX
    sidecar.record_imap_review(con, "dedup-redundant", {gid: (("INBOX", "INBOX"),)}, 1)
    con.commit()
    con.close()
    box.add_label([gid], "offload-candidate")
    labels = by_folder(server, folder=tmp_path)._scan()[1].messages[gid].labels
    assert labels == {"Archive", "offload-candidate"}  # not INBOX


def test_a_message_under_another_review_is_left_to_it(server, tmp_path):
    server.add("INBOX", msgid="<a@x>", flags={"\\Seen"})
    box = by_folder(server, folder=tmp_path)
    (gid,) = box.list_message_ids()
    assert box.add_label([gid], "dedup-redundant") == []
    (failure,) = by_folder(server, folder=tmp_path).add_label([gid], "offload-candidate")
    assert "under review in mailcellar/review/dedup-redundant already" in failure


def test_junk_and_trash_copies_stay_out_of_review(server, tmp_path):
    server.add_folder("Junk", special=["\\Junk"])
    server.add("INBOX", msgid="<a@x>", flags={"\\Seen"})
    server.add("Junk", msgid="<a@x>", flags={"\\Seen"})
    box = by_folder(server, folder=tmp_path)
    (gid,) = box.list_message_ids()
    assert box.add_label([gid], "offload-candidate") == []
    assert len(server.folders["Junk"]["uids"]) == 1  # left where it was
    assert sidecar.read_imap_reviews(tmp_path)[gid] == {"offload-candidate": (("INBOX", "INBOX"),)}


def test_a_folder_refusing_the_move_into_review_fails_its_messages_only(
    server, tmp_path, monkeypatch
):
    server.add("INBOX", msgid="<a@x>", flags={"\\Seen"})
    server.add("Projects", msgid="<b@x>", flags={"\\Seen"})
    box = by_folder(server, folder=tmp_path)
    ids = ids_by_msgid(box, server)
    move = server._write

    def refuse_projects(command, uids, wanted, *rest):
        if command == "MOVE" and server.selected == "Projects":
            return "NO", [b"[CANNOT] not now"]
        return move(command, uids, wanted, *rest)

    monkeypatch.setattr(server, "_write", refuse_projects)
    (failure,) = box.add_label([ids["<a@x>"], ids["<b@x>"]], "offload-candidate")
    review = "mailcellar/review/offload-candidate"
    assert failure.startswith(f"{ids['<b@x>']}: moving to {review} in Projects failed")
    assert not server.folders["INBOX"]["uids"]  # a@x moved all the same


def test_the_marking_is_decided_before_anything_changes(server):
    server.add("INBOX", msgid="<a@x>", flags={"\\Seen"})
    box = account(server)
    scan, entries = box._scan()[1], box._read_entries()
    (gid,) = scan.messages
    marking = imap.folder_marking(scan, [gid, "0000000000000000"], "offload-candidate", entries)
    assert marking.folder == "mailcellar/review/offload-candidate" and marking.create
    assert marking.copies == {"INBOX": (1,)} and marking.origins == {gid: (("INBOX", "INBOX"),)}
    assert marking.failures == ("0000000000000000: not on the server",)


def test_a_backup_during_review_keeps_labels_and_drops_finished_reviews(server, tmp_path):
    server.add("Projects", msgid="<a@x>", flags={"\\Seen"})
    server.add("Projects", msgid="<b@x>", flags={"\\Seen"})
    backup = backup_folder(ADDRESS)
    box = by_folder(server, folder=backup)
    ids = ids_by_msgid(box, server)
    assert box.add_label(list(ids.values()), "offload-candidate") == []

    def backed_up():
        b = by_folder(server, folder=backup)
        commands.backup(b, ADDRESS, backup)
        return b

    backed_up()
    con = sqlite3.connect(backup / "msg-db.sqlite")
    try:
        got = {
            uid: {
                lb
                for (lb,) in con.execute(
                    "SELECT label FROM labels NATURAL JOIN uids WHERE uid = ?", (uid,)
                )
            }
            for uid in ids.values()
        }
    finally:
        con.close()
    assert all(labels == {"Projects", "offload-candidate"} for labels in got.values())
    review = server.folders["mailcellar/review/offload-candidate"]["uids"]
    kept = next(u for u, m in review.items() if b"<b@x>" in m["raw"])
    server.move("mailcellar/review/offload-candidate", kept, "Projects")  # kept by the user
    backed_up()
    assert set(sidecar.read_imap_reviews(backup)) == {ids["<a@x>"]}  # b's record dropped


def test_a_review_recorded_while_a_backup_scanned_survives_its_save(server, tmp_path):
    server.add("INBOX", msgid="<a@x>", flags={"\\Seen"})
    backup = by_folder(server, folder=tmp_path)
    backup._scan()  # the backup has read the mailbox: a@x is in INBOX
    labeller = by_folder(server, folder=tmp_path)
    (gid,) = labeller.list_message_ids()
    assert labeller.add_label([gid], "offload-candidate") == []  # recorded, then moved
    con = sidecar.open_meta(tmp_path)
    backup.save_state(con)  # its scan predates the record
    con.commit()
    con.close()
    assert gid in sidecar.read_imap_reviews(tmp_path)


def test_a_review_folder_that_exists_by_now_is_used(server, tmp_path, monkeypatch):
    server.add("INBOX", msgid="<a@x>", flags={"\\Seen"})
    box = by_folder(server, folder=tmp_path)
    (gid,) = box.list_message_ids()
    box._read_entries()  # LIST read before another client makes the folder
    server.add_folder("mailcellar/review/offload-candidate")
    assert box.add_label([gid], "offload-candidate") == []
    assert len(server.folders["mailcellar/review/offload-candidate"]["uids"]) == 1


@pytest.mark.parametrize("keeps_keywords", [True, False])
def test_restore_after_review_by_folder_puts_mail_back_where_it_was(
    home, tmp_path, keeps_keywords, capsys
):
    (tmp_path / "config" / "home" / gmailapi.IMAP_SETTINGS).write_text(
        'host = "imap.example.org"\nreview = "folder"\n'
    )
    if not keeps_keywords:  # folder review's reason to be: no keyword is ever sent
        home.permanentflags = b"(\\Seen \\Flagged)"
    assert cli.main(["backup", "home"]) == 0
    (tmp_path / "rules.toml").write_text(
        '[[rule]]\nname = "old"\nlabel = "Projects"\nbefore = "2020-01-01"\n'
    )
    offload = ["offload", "apply", "--plan", "plan.csv", "--log", "log.csv"]
    cli.main(["offload", "plan", "--rules", "rules.toml", "--plan", "plan.csv"])
    cli.main([*offload, "--label"])
    review = "mailcellar/review/offload-candidate"
    assert len(home.folders[review]["uids"]) == 3 and not home.folders["Projects"]["uids"]
    home.move(review, min(home.folders[review]["uids"]), "Projects")  # kept by the user
    assert cli.main(["backup", "home"]) == 0
    cli.main([*offload, "--trash", "--yes"])
    assert len(home.folders["Trash"]["uids"]) == 2 and not home.folders[review]["uids"]
    assert cli.main(["restore", "home", "--offloaded", "--yes"]) == 0
    assert len(home.folders["Projects"]["uids"]) == 3  # home again, by the review's record
    assert not home.folders["Trash"]["uids"] and not home.folders["INBOX"]["uids"]
    restored = [
        f for m in home.folders["Projects"]["uids"].values() for f in m["flags"] if "restored" in f
    ]
    assert len(restored) == (2 if keeps_keywords else 0)
    err = capsys.readouterr().err
    assert "couldn't be labelled" not in err  # nothing that could ever be
    said = "the server keeps no keywords: not marked restored/"
    assert err.count(said) == (0 if keeps_keywords else 1)  # said once instead


def test_labels_become_folders_and_flags_for_an_append():
    folders = (
        imap.Folder("INBOX", "INBOX"),
        imap.Folder("Sent Messages", "SENT"),
        imap.Folder("Projects", "Projects"),
        imap.Folder("mailcellar.review.offload-candidate", "offload-candidate", review=True),
    )
    plan = imap.append_plan(
        {"Projects", "SENT", "Receipts/2026", "UNREAD", "STARRED", "restored/2026-10-27"},
        folders,
        ".",
    )
    assert plan.folders == ("Projects", "Receipts.2026", "Sent Messages")
    assert plan.create == ("Receipts.2026",)  # in the server's own hierarchy
    assert plan.flags == "(\\Flagged $mailcellar-restored_2026-10-27)"  # unread: no \Seen
    assert imap.append_plan({"INBOX"}, folders, ".").flags == "(\\Seen)"
    archived = imap.append_plan({"UNREAD"}, folders, ".")  # no folder label: archived in Gmail
    assert archived.folders == ("Archive",) and archived.create == ("Archive",)
    assert imap.append_plan({"Reçus"}, folders, ".").folders == ("Re&AOc-us",)  # modified UTF-7


def test_home_folders_skip_trash_junk_and_review():
    folders = (
        imap.Folder("INBOX", "INBOX"),
        imap.Folder("Trash", "TRASH"),
        imap.Folder("Projects", "Projects"),
    )
    assert imap.home_folders({"Projects", "INBOX", "TRASH", "UNREAD"}, folders) == (
        "INBOX",
        "Projects",
    )
    assert imap.home_folders({"TRASH", "Gone"}, folders) == ("Archive",)  # as an upload files it


def test_an_appended_message_gets_the_id_the_next_scan_gives_it():
    raw = make_eml(msgid="<a@x>")
    assert imap.appended_id(raw, 1_420_106_400_123) == imap.header_id(
        "<a@x>", 1_420_106_400_000, len(raw)
    )
    no_id = make_eml(msgid=None)
    assert imap.appended_id(no_id, 0) == imap.content_id(no_id)
    dated = b"Date: Thu, 01 Jan 2015 10:00:00 +0000\r\nSubject: s\r\n\r\nbody"
    assert imap.date_of(dated) == 1_420_106_400_000 and imap.date_of(b"Subject: s\r\n\r\n") is None


def test_mail_dated_apart_from_its_receipt_comes_back_under_the_same_id(home, tmp_path, capsys):
    for uids in home.folders["Projects"]["uids"].values():  # Date: nine seconds before receipt
        uids["raw"] = make_eml(
            msgid=uids["raw"].split(b"Message-ID: ")[1].split(b"\n")[0].decode(),
            headers=[("Date", "Thu, 01 Jan 2015 09:59:51 +0000")],
        )
    offload_two(home, tmp_path)
    home.folders["Trash"]["uids"].clear()
    assert cli.main(["backup", "home"]) == 0
    assert cli.main(["restore", "home", "--offloaded", "--yes"]) == 0
    assert {m["date"] for m in home.folders["Projects"]["uids"].values()} == {OLD}  # receipt
    capsys.readouterr()
    assert cli.main(["backup", "home"]) == 0
    assert "Downloaded 0 messages" in capsys.readouterr().out


def test_mail_trashed_from_two_folders_goes_back_to_each_once(home, tmp_path):
    home.add("INBOX", raw=home.folders["Projects"]["uids"][1]["raw"], flags={"\\Seen"}, date=OLD)
    assert cli.main(["backup", "home"]) == 0
    box = by_folder(home, "keyword", folder=backup_folder(ADDRESS))
    gid = next(g for g, m in box._scan()[1].messages.items() if len(m.places) == 2)
    assert box.trash([gid]) == []
    assert len(home.folders["Trash"]["uids"]) == 2
    box = by_folder(home, "keyword", folder=backup_folder(ADDRESS))
    assert box.untrash([gid]) == []
    places = sorted(n for n, _u in box._scan()[1].messages[gid].places)
    assert places == ["INBOX", "Projects"]  # one each, none doubled


def test_gmail_only_labels_make_no_folders():
    folders = (imap.Folder("INBOX", "INBOX"),)
    plan = imap.append_plan({"INBOX", "IMPORTANT", "CATEGORY_PROMOTIONS", "CHAT"}, folders, "/")
    assert plan.folders == ("INBOX",) and plan.create == ()


def test_the_newest_review_says_where_mail_goes_back(tmp_path):
    con = sidecar.open_meta(tmp_path)
    sidecar.record_imap_review(con, "offload-candidate", {"g": (("Archive", "Archive"),)}, 2)
    sidecar.record_imap_review(con, "dedup-redundant", {"g": (("INBOX", "INBOX"),)}, 1)
    con.commit()
    con.close()
    reviews = sidecar.read_imap_reviews(tmp_path)["g"]
    assert list(reviews.values())[-1] == (("Archive", "Archive"),)  # marked last


def test_a_partly_appended_message_counts_as_restored(server, monkeypatch, caplog):
    append = server.append

    def refuse_projects(mailbox, flags, date_time, message):
        if mailbox == '"Projects"':
            return "NO", [b"[OVERQUOTA] not now"]
        return append(mailbox, flags, date_time, message)

    monkeypatch.setattr(server, "append", refuse_projects)
    box = account(server)
    got = {}
    raw = make_eml(msgid="<r@x>")
    failures = box.import_messages({"k": (raw, ["INBOX", "Projects"])}, got.__setitem__, {})
    assert failures == [] and "k" in got  # back in INBOX
    assert "k not appended to Projects" in caplog.text


def test_message_ids_come_from_the_backup_where_it_has_them(home, tmp_path):
    assert cli.main(["backup", "home"]) == 0
    box = by_folder(home, "keyword", folder=backup_folder(ADDRESS))
    box._scan()
    home.commands.clear()
    found = box.find_message_ids({"k": "<p1@x>"})
    assert len(found["k"]) == 1
    assert not [c for c in home.commands if "MESSAGE-ID" in str(c)]  # no header fetched


def test_untrash_result_is_decided_from_ids(server):
    server.add("Trash", msgid="<a@x>", flags={"\\Seen"})
    scan = account(server)._scan()[1]
    (gid,) = scan.messages
    plan = imap.PutBack({}, {("Trash", "INBOX"): frozenset({gid})}, {gid: ("INBOX",)}, ())
    stuck = {gid: f"{gid}: moving it out of Trash to INBOX failed: no"}
    assert imap.untrash_result(plan, stuck, scan) == list(stuck.values())
    assert imap.untrash_result(plan, {}, scan) == [f"{gid}: still in Trash"]


def test_restore_into_another_imap_account(home, tmp_path, monkeypatch, capsys):
    work = FakeImap()
    work.writable = True
    work.signed_in = home.signed_in
    work.add_folder("Trash", special=["\\Trash"])
    root = tmp_path / "config"
    (root / "work").mkdir()
    (root / "accounts.toml").write_text(f'home = "{ADDRESS}"\nwork = "sam@example.org"\n')
    (root / "work" / gmailapi.IMAP_SETTINGS).write_text('host = "imap.example.net"\n')
    (root / "work" / gmailapi.IMAP_PASSWORD).write_text("secret\n")
    (root / "work" / gmailapi.IMAP_PASSWORD).chmod(0o600)
    servers = {"imap.example.org": home, "imap.example.net": work}
    monkeypatch.setattr(imap.ImapAccount, "_connect_ssl", lambda self: servers[self.settings.host])
    assert cli.main(["backup", "home"]) == 0
    assert cli.main(["restore", "home", "--all", "--to", "work", "--yes"]) == 0
    assert len(work.folders["Projects"]["uids"]) == 3  # the folder made, the mail in it
    capsys.readouterr()
    assert cli.main(["backup", "work"]) == 0  # the target's backup already has them
    assert "Downloaded 0 messages" in capsys.readouterr().out


# ── restore: the re-review's findings ───────────────────────────────────────


def test_a_custom_review_mark_isnt_put_back_as_a_label(home, tmp_path):
    from mailcellar.restore.apply import candidates

    for m in home.folders["Projects"]["uids"].values():
        m["flags"].add("$mailcellar-dupes")  # an earlier dedup apply --label dupes
    assert cli.main(["backup", "home"]) == 0
    folder = backup_folder(ADDRESS)
    assert "dupes" in sidecar.read_mark_labels(folder)
    cands = candidates(folder, everything=True)
    assert len(cands) == 3 and all(c.labels == ("Projects",) for c in cands)  # mark left off


def test_a_message_without_a_date_header_goes_back_byte_for_byte(home, tmp_path):
    originals = {m["raw"] for m in home.folders["Projects"]["uids"].values()}
    assert not any(b"\nDate:" in raw for raw in originals)
    offload_two(home, tmp_path)
    home.folders["Trash"]["uids"].clear()
    assert cli.main(["backup", "home"]) == 0
    assert cli.main(["restore", "home", "--offloaded", "--yes"]) == 0
    assert {m["raw"] for m in home.folders["Projects"]["uids"].values()} == originals


def test_an_append_fails_only_when_its_first_folder_does():
    assert imap.append_outcome("k", ("INBOX", "Projects"), {}) == (None, [])
    assert imap.append_outcome("k", ("INBOX", "Projects"), {"INBOX": "no"}) == ("k: no", [])
    assert imap.append_outcome("k", ("INBOX", "Projects"), {"Projects": "full"}) == (
        None,
        ["Projects (full)"],
    )


def test_backed_up_mail_without_a_message_id_isnt_fetched_again(home, tmp_path):
    home.add("INBOX", raw=make_eml(msgid=None, body="no id"), flags={"\\Seen"})
    assert cli.main(["backup", "home"]) == 0
    box = by_folder(home, "keyword", folder=backup_folder(ADDRESS))
    box._scan()
    home.commands.clear()
    box.find_message_ids({"k": "<p1@x>"})
    assert not [c for c in home.commands if "MESSAGE-ID" in str(c)]


def test_a_takeout_mbox_restores_into_an_imap_account_without_gmails_folders(home, tmp_path):
    mbox = tmp_path / "takeout.mbox"
    raw = make_eml(msgid="<t@x>").replace(b"\n", b"\r\n")
    mbox.write_bytes(
        b"From 1234@xxx Thu Jan 01 10:00:00 +0000 2015\r\n"
        b"X-Gmail-Labels: Inbox,Important,Category Promotions,Opened,Receipts\r\n" + raw + b"\r\n"
    )
    assert cli.main(["restore", "home", "--from-mbox", str(mbox), "--yes"]) == 0
    assert set(home.folders) == {"INBOX", "Trash", "Projects", "Receipts"}  # no Gmail-only ones
    assert len(home.folders["INBOX"]["uids"]) == 1 and len(home.folders["Receipts"]["uids"]) == 1


# ── restore between Gmail and IMAP accounts ─────────────────────────────────

GMAIL = "gee@example.com"
GMAIL_LABELS = {
    "INBOX": "INBOX",
    "STARRED": "STARRED",
    "UNREAD": "UNREAD",
    "IMPORTANT": "IMPORTANT",
    "CATEGORY_PROMOTIONS": "CATEGORY_PROMOTIONS",
    "Label_1": "Reçus",
    "Label_2": "Work/2026",
}


@pytest.fixture
def gmail(home, tmp_path, monkeypatch):
    """A Gmail account `g` (a FakeMailbox) beside the IMAP account `home`."""
    root = tmp_path / "config"
    (root / "g").mkdir()
    (root / "accounts.toml").write_text(f'home = "{ADDRESS}"\ng = "{GMAIL}"\n')
    box = FakeMailbox(account=GMAIL, labels=GMAIL_LABELS)
    monkeypatch.setattr(
        gmailapi, "load_credentials", lambda folder, account, scope, interactive=True: scope
    )
    monkeypatch.setattr(gmailapi, "GmailAccount", lambda account, creds: box)
    return box


def test_a_gmail_backup_restores_into_an_imap_account(gmail, home, tmp_path):
    source = FakeMailbox(
        {
            "g1": raw_response(
                "g1", ms=1_420_106_400_000, label_ids=["INBOX", "STARRED", "IMPORTANT"]
            ),
            "g2": raw_response("g2", label_ids=["Label_1", "UNREAD", "CATEGORY_PROMOTIONS"]),
            "g3": raw_response("g3", label_ids=["Label_2"]),
            "g4": raw_response("g4", label_ids=[]),  # archived, no label
        },
        account=GMAIL,
        labels=GMAIL_LABELS,
    )
    commands.backup(source, GMAIL, backup_folder(GMAIL))
    assert cli.main(["backup", "home"]) == 0
    assert cli.main(["restore", "g", "--all", "--to", "home", "--yes"]) == 0
    by_id = {
        name: [
            (
                m["raw"].split(b"Message-ID: <")[1][:2],
                {f for f in m["flags"] if not f.startswith("$mailcellar-restored_")},
            )
            for m in f["uids"].values()
        ]
        for name, f in home.folders.items()
    }
    assert (b"g1", {"\\Seen", "\\Flagged"}) in by_id["INBOX"]
    assert by_id["Re&AOc-us"] == [(b"g2", set())]  # unread, a UTF-7 folder name
    assert by_id["Work/2026"] == [(b"g3", {"\\Seen"})]
    assert by_id["Archive"] == [(b"g4", {"\\Seen"})]  # archived in Gmail
    assert not [n for n in home.folders if "IMPORTANT" in n.upper() or "CATEGORY" in n.upper()]
    restored = [m for f in home.folders.values() for m in f["uids"].values()]
    assert (
        sum(any(k.startswith("$mailcellar-restored_") for k in m["flags"]) for m in restored) == 4
    )


def test_an_imap_backup_restores_into_a_gmail_account(gmail, home, tmp_path):
    home.folders["Projects"]["uids"][1]["flags"].discard("\\Seen")
    home.folders["Projects"]["uids"][2]["flags"].add("\\Flagged")
    assert cli.main(["backup", "home"]) == 0
    assert cli.main(["restore", "home", "--all", "--to", "g", "--yes"]) == 0
    imported = [c for c in gmail.calls if c[0] == "import"]
    assert len(imported) == 3
    names = {gmail.labels.get(i, i) for c in imported for i in c[3]}
    assert {"Projects", "UNREAD", "STARRED"} <= names  # the folder as a label, flags as Gmail's


def test_folder_names_go_to_modified_utf7_and_back():
    for name in ("Reçus", "Work & play", "日本語/2026", "Plain"):
        assert imap.decode_utf7(imap.encode_utf7(name)) == name
    assert imap.encode_utf7("Work & play") == "Work &- play"


def test_a_rerun_after_an_unrecorded_restore_appends_nothing_twice(gmail, home, tmp_path):
    source = FakeMailbox(
        {f"g{n}": raw_response(f"g{n}", label_ids=["INBOX"]) for n in range(3)},
        account=GMAIL,
        labels=GMAIL_LABELS,
    )
    commands.backup(source, GMAIL, backup_folder(GMAIL))
    assert cli.main(["backup", "home"]) == 0
    assert cli.main(["restore", "g", "--all", "--to", "home", "--yes"]) == 0
    con = sqlite3.connect(backup_folder(GMAIL) / "meta.sqlite")
    con.execute("DELETE FROM restored")  # as if the run stopped before recording them
    con.commit()
    con.close()
    assert cli.main(["restore", "g", "--all", "--to", "home", "--yes"]) == 0
    assert len(home.folders["INBOX"]["uids"]) == 3  # found by Message-ID, not appended again


def test_labels_land_in_the_servers_own_folders():
    folders = (
        imap.Folder("INBOX", "INBOX"),
        imap.Folder("Archives", "Archives", archive=True),
        imap.Folder("Receipts", "Receipts"),
    )
    assert imap.append_plan(set(), folders, ".").folders == ("Archives",)  # \Archive, not new
    assert imap.append_plan({"receipts"}, folders, ".").folders == ("Receipts",)  # any case
    plan = imap.append_plan({"Release 1.2/notes"}, folders, ".")
    assert plan.folders == ("Release 1-2.notes",)  # its own '.' kept from nesting


def test_a_same_account_restore_across_providers_is_refused():
    from mailcellar.restore.cli import same_account_problem

    assert same_account_problem("imap", "imap") == ""
    assert "now set up as gmail" in same_account_problem("imap", "gmail")


def test_the_servers_archive_flag_is_read_from_its_listing():
    lines = [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\Archive) "/" "Archives"']
    folders = imap.folders_from(imap.list_entries(lines))
    assert [f.name for f in folders if f.archive] == ["Archives"]
    assert imap.archive_folder(folders) == ("Archives", False)


def test_an_exact_folder_match_wins_over_a_case_insensitive_one():
    folders = (imap.Folder("work", "work"), imap.Folder("Work", "Work"))
    assert imap.append_plan({"Work"}, folders, "/").folders == ("Work",)
    assert imap.append_plan({"work"}, folders, "/").folders == ("work",)
    assert imap.append_plan({"WORK"}, folders, "/").folders == ("work",)  # the first listed


def test_untrash_to_a_new_archive_creates_it(server):
    server.add("Trash", msgid="<a@x>", flags={"\\Seen"})
    box = account(server)
    (gid,) = box.list_message_ids(include_spam_trash=True)
    assert box.untrash([gid]) == []  # no folder known: the archive, made for it
    assert len(server.folders["Archive"]["uids"]) == 1


def test_restore_into_an_account_set_up_with_the_other_provider_is_refused(home, tmp_path):
    assert cli.main(["backup", "home"]) == 0
    (tmp_path / "config" / "home" / gmailapi.IMAP_SETTINGS).unlink()  # now a Gmail account
    with pytest.raises(SystemExit, match="holds imap IDs but the account is now set up as gmail"):
        cli.main(["restore", "home", "--all"])


# ── a dry run's checks ───────────────────────────────────────────────────────


def dry_run(home, tmp_path):
    assert cli.main(["backup", "home"]) == 0
    (tmp_path / "rules.toml").write_text(
        '[[rule]]\nname = "old"\nlabel = "Projects"\nbefore = "2020-01-01"\n'
    )
    cli.main(["offload", "plan", "--rules", "rules.toml", "--plan", "plan.csv"])
    home.commands.clear()
    cli.main(["offload", "apply", "--plan", "plan.csv", "--log", "log.csv"])
    sent = {c[0] for c in home.commands} | {c[1] for c in home.commands if c[0] == "UID"}
    assert not sent & {"STORE", "MOVE", "COPY", "CREATE", "APPEND", "EXPUNGE"}


def test_a_dry_run_says_when_inbox_keeps_no_keywords(home, tmp_path, capsys):
    home.keywordless.add("INBOX")
    dry_run(home, tmp_path)
    err = capsys.readouterr().err
    assert "the server keeps no keywords in INBOX" in err
    assert f"mailcellar setup {ADDRESS} --review folder" in err


def test_a_dry_run_says_when_the_server_cant_move(home, tmp_path, capsys):
    home.signed_in = ("IMAP4rev1", "SPECIAL-USE", "UIDPLUS")
    dry_run(home, tmp_path)
    assert "the server has no MOVE (RFC 6851)" in capsys.readouterr().err


def test_a_dry_run_says_nothing_where_marking_would_work(home, tmp_path, capsys):
    dry_run(home, tmp_path)
    assert "keeps no keywords" not in capsys.readouterr().err


def test_a_dry_run_whose_check_fails_carries_on(home, tmp_path, capsys, monkeypatch):
    def gives_out(self):
        raise gmailapi.GmailError("CAPABILITY failed: connection reset")

    monkeypatch.setattr(imap.ImapAccount, "review_problem", gives_out)
    dry_run(home, tmp_path)
    assert "couldn't check that --label would work here" in capsys.readouterr().err
    with open("log.csv", newline="", encoding="utf-8") as fh:
        assert {r["mode"] for r in csv.DictReader(fh)} == {"dry-run"}  # the run finished
