"""imap.ImapAccount over FakeImap: what it lists, labels and fetches, without writing."""

import base64

import pytest

from fake_imap import FakeImap
from mailcellar import gmailapi, imap
from mailcellar.backup import sidecar
from test_index import make_eml

ADDRESS = "alex@example.org"
SETTINGS = gmailapi.ImapSettings("imap.example.org", 993, ADDRESS)


class Saving(imap.ImapAccount):
    """An ImapAccount whose map is saved after it scans, as backup saves it."""

    def _scan(self):
        first = self._cache is None
        result = super()._scan()
        if first and self.folder is not None:
            con = sidecar.open_meta(self.folder)
            self.save_state(con)
            con.commit()
            con.close()
        return result


def account(server, folder=None):
    return Saving(
        ADDRESS, SETTINGS, "secret", folder=folder, connect=lambda: server, sleep=lambda s: None
    )


@pytest.fixture
def server():
    s = FakeImap()
    s.add_folder("Sent", special=["\\Sent"])
    s.add_folder("Trash", special=["\\Trash"])
    s.add_folder("Junk", special=["\\Junk"])
    s.add_folder("Projects/2026")
    s.add_folder("[Virtual]", special=["\\All"])
    s.add_folder("Parent", selectable=False)
    return s


def ids_by_msgid(box, server):
    """{Message-ID: backup ID}, from what the account reports."""
    out = {}
    for gid in box.list_message_ids(include_spam_trash=True):
        got = []
        box.get_raw([gid], got.append)
        raw = base64.urlsafe_b64decode(got[0]["raw"])
        out[raw.split(b"Message-ID: ")[1].split(b"\n")[0].decode().strip()] = gid
    return out


# ── pure helpers ─────────────────────────────────────────────────────────────


def test_folder_names_decode_from_modified_utf7():
    assert imap.decode_utf7("Entw&APw-rfe") == "Entwürfe"
    assert imap.decode_utf7("&ZeVnLIqe-") == "日本語"
    assert imap.decode_utf7("Tom &- Jerry") == "Tom & Jerry"


def test_folders_become_labels():
    lines = [
        b'(\\HasNoChildren) "/" "INBOX"',
        b'(\\HasNoChildren \\Sent) "/" "Sent Messages"',
        b'(\\HasNoChildren) "." "Archive.2026"',
        b'(\\Noselect \\HasChildren) "/" "Parent"',
        b'(\\All) "/" "All Mail"',
        b'(\\HasNoChildren) "/" "Trash"',
        b'(\\HasNoChildren) "/" Plain',
    ]
    got = {f.name: f.label for f in imap.folders_from(imap.list_entries(lines))}
    assert got == {
        "INBOX": "INBOX",
        "Sent Messages": "SENT",
        "Archive.2026": "Archive/2026",
        "Trash": "TRASH",  # marks Sent, not Trash: the name stands for Trash
        "Plain": "Plain",
    }
    plain = [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "Trash"']
    assert [f.label for f in imap.folders_from(imap.list_entries(plain))] == ["INBOX", "TRASH"]
    assert imap.decode_utf7("Broken &AAA") == "Broken &AAA"  # malformed: kept as given
    assert imap.decode_utf7("Bad &!!!-") == "Bad &!!!-"


def test_fetch_answers_parse_with_and_without_literals():
    data = [
        (b'1 (UID 7 INTERNALDATE "17-Jul-2026 02:44:25 +0000" RFC822.SIZE 9 BODY[] {3}', b"abc"),
        b" FLAGS (\\Seen))",
        b"2 (UID 9 FLAGS (\\Flagged))",
    ]
    got = imap.parse_fetch(data)
    assert got[7].literal == b"abc" and got[7].size == 9 and got[7].flags == {"\\Seen"}
    assert got[7].internal == 1_784_256_265_000
    assert got[9] == imap.Fetched(9, flags=frozenset({"\\Flagged"}))
    assert imap.message_id_of(b"Message-ID:\r\n <a@b>\r\n\r\n") == "<a@b>"
    assert imap.message_id_of(b"\r\n") == ""


# ── listing and labels ───────────────────────────────────────────────────────


def test_messages_are_labelled_by_folder_and_flags(server):
    server.add("INBOX", msgid="<i@x>", flags={"\\Seen", "\\Flagged"})
    server.add("Projects/2026", msgid="<p@x>")
    server.add("Sent", msgid="<s@x>", flags={"\\Seen"})
    server.add("Trash", msgid="<t@x>", flags={"\\Seen"})
    server.add("Junk", msgid="<j@x>")
    box = account(server)
    ids = ids_by_msgid(box, server)
    labels = box.message_labels(list(ids.values()))
    assert labels[ids["<i@x>"]] == {"INBOX", "STARRED"}
    assert labels[ids["<p@x>"]] == {"Projects/2026", "UNREAD"}
    assert labels[ids["<s@x>"]] == {"SENT"}
    assert labels[ids["<t@x>"]] == {"TRASH"} and labels[ids["<j@x>"]] == {"SPAM", "UNREAD"}
    assert sorted(box.list_message_ids()) == sorted(ids[m] for m in ("<i@x>", "<p@x>", "<s@x>"))
    assert box.list_message_ids(label_id="SENT") == [ids["<s@x>"]]
    catalogue = {c["name"]: c["type"] for c in box.label_catalogue()}
    assert catalogue["INBOX"] == "system" and catalogue["Projects/2026"] == "user"
    assert all(len(gid) == 16 for gid in ids.values())


def test_a_message_in_two_folders_is_one_message_with_both_labels(server):
    raw = make_eml(msgid="<both@x>")
    server.add("INBOX", raw=raw, flags={"\\Seen"})
    server.add("Projects/2026", raw=raw, flags={"\\Seen"})
    box = account(server)
    (gid,) = box.list_message_ids()
    assert box.message_labels([gid]) == {gid: {"INBOX", "Projects/2026"}}


def test_a_moved_message_keeps_its_id(server, tmp_path):
    uid = server.add("INBOX", msgid="<mv@x>", flags={"\\Seen"})
    (before,) = account(server, tmp_path).list_message_ids()
    server.move("INBOX", uid, "Projects/2026")
    box = account(server, tmp_path)
    assert box.list_message_ids() == [before]
    assert box.message_labels([before]) == {before: {"Projects/2026"}}


def test_messages_without_a_message_id_are_told_apart_by_content(server):
    one = make_eml(msgid=None, body="one!")
    two = make_eml(msgid=None, body="two!")  # same length and date
    server.add("INBOX", raw=one)
    server.add("INBOX", raw=two)
    box = account(server)
    ids = box.list_message_ids()
    assert len(set(ids)) == 2
    fetched_before = len([c for c in server.commands if "(BODY.PEEK[])" in c])
    got = []
    assert box.get_raw(ids, got.append) == []
    assert sorted(base64.urlsafe_b64decode(r["raw"]) for r in got) == sorted([one, two])
    assert len([c for c in server.commands if "(BODY.PEEK[])" in c]) == fetched_before  # kept


def test_gmail_search_is_refused(server):
    with pytest.raises(gmailapi.GmailError, match="Gmail's search"):
        account(server).list_message_ids("older_than:1y")


# ── the map ──────────────────────────────────────────────────────────────────


def test_a_second_run_reads_no_headers_of_mail_it_knows(server, tmp_path):
    server.add("INBOX", msgid="<a@x>")
    server.add("INBOX", msgid="<b@x>")
    first = account(server, tmp_path).list_message_ids()
    server.commands.clear()
    server.add("INBOX", msgid="<c@x>")
    again = account(server, tmp_path).list_message_ids()
    assert set(first) < set(again) and len(again) == 3
    header_reads = [
        c for c in server.commands if c[:2] == ("UID", "FETCH") and "MESSAGE-ID" in c[3]
    ]
    assert [c[2] for c in header_reads] == ["3"]  # only the new message


def test_a_uidvalidity_reset_rebuilds_the_folder_with_the_same_ids(server, tmp_path):
    for n in range(3):
        server.add("INBOX", msgid=f"<r{n}@x>")
    before = sorted(account(server, tmp_path).list_message_ids())
    server.reset_uidvalidity("INBOX")
    assert sorted(account(server, tmp_path).list_message_ids()) == before
    con = sidecar.open_meta(tmp_path)
    assert {v for (v,) in con.execute("SELECT DISTINCT uidvalidity FROM imap_map")} == {2}
    con.close()


def test_history_reports_what_is_gone(server, tmp_path):
    keep = server.add("INBOX", msgid="<k@x>")
    gone = server.add("INBOX", msgid="<g@x>")
    box = account(server, tmp_path)
    ids = ids_by_msgid(box, server)
    point = box.current_history_id()
    del server.folders["INBOX"]["uids"][gone]
    touched, deleted, _latest = account(server, tmp_path).history_changes(point)
    assert deleted == {ids["<g@x>"]} and ids["<k@x>"] in touched
    assert keep in server.folders["INBOX"]["uids"]
    with pytest.raises(gmailapi.HistoryExpired):
        box.history_changes("12345")  # a Gmail history ID


# ── content ──────────────────────────────────────────────────────────────────


def test_get_raw_returns_gmail_shaped_responses_and_skips_gone_mail(server):
    raw = make_eml(msgid="<c@x>")
    server.add("INBOX", raw=raw, flags={"\\Seen"})
    box = account(server)
    (gid,) = box.list_message_ids()
    got = []
    assert box.get_raw([gid, "0123456789abcdef"], got.append) == []
    (response,) = got
    assert base64.urlsafe_b64decode(response["raw"]) == raw
    assert response["labelIds"] == ["INBOX"] and response["internalDate"] == "1784256265000"
    assert response["threadId"] == imap.thread_of(raw, gid) != ""  # '' would read as gone
    assert response["sizeEstimate"] == len(raw)


def test_replies_share_the_thread_of_the_conversations_first_message():
    first = make_eml(msgid="<root@x>")
    reply = make_eml(msgid="<r2@x>", headers=[("In-Reply-To", "<root@x>")])
    later = make_eml(msgid="<r3@x>", headers=[("References", "<root@x> <r2@x>")])
    threads = {imap.thread_of(raw, "g") for raw in (first, reply, later)}
    assert len(threads) == 1 and threads != {"g"}
    assert imap.thread_of(make_eml(msgid=None), "0123456789abcdef") == "0123456789abcdef"


def test_details_of_known_mail_read_its_date(server, tmp_path):
    server.add("INBOX", msgid="<d@x>", flags={"\\Seen"})
    (gid,) = account(server, tmp_path).list_message_ids()
    details = account(server, tmp_path).message_details([gid, "0123456789abcdef"])
    assert details[gid]["internalDate"] == "1784256265000" and details[gid]["sizeEstimate"] > 0
    assert details["0123456789abcdef"] is None


# ── never writing; failures ──────────────────────────────────────────────────


def test_nothing_on_the_server_changes(server):
    server.add("INBOX", msgid="<n@x>")
    box = account(server)
    (gid,) = box.list_message_ids()
    box.get_raw([gid], lambda r: None)
    assert server.folders["INBOX"]["uids"][1]["flags"] == set()  # still unread
    for write in (  # restore's writes come later (#113)
        lambda: box.untrash([gid]),
        lambda: box.import_messages({}, None),
    ):
        with pytest.raises(gmailapi.GmailError, match="restore into it isn't supported yet"):
            write()
    assert all(c[0] in ("LOGIN", "LIST", "EXAMINE", "UID") for c in server.commands)


def test_a_refused_login_is_access_refused(server):
    box = imap.ImapAccount(ADDRESS, SETTINGS, "wrong", connect=lambda: server)
    with pytest.raises(gmailapi.AccessRefused, match="refused the login"):
        box.list_message_ids()


def test_a_dropped_connection_is_retried(server):
    server.add("INBOX", msgid="<x@x>")
    server.drops = 1
    assert len(account(server).list_message_ids()) == 1


@pytest.mark.parametrize("fault", ["stalls", "drops"])
def test_a_login_that_times_out_or_drops_closes_its_socket_and_is_retried(server, fault):
    setattr(server, fault, 1)
    account(server).sign_in()
    assert [c[0] for c in server.commands] == ["LOGIN", "SHUTDOWN", "LOGIN"]


@pytest.mark.parametrize("fault", ["stalls", "drops"])
def test_close_closes_the_socket_and_raises_nothing_when_logout_fails(server, fault):
    box = account(server)
    box.sign_in()
    setattr(server, fault, 1)
    box.close()  # a backup that is done isn't failed by its goodbye
    assert [c[0] for c in server.commands][-2:] == ["LOGOUT", "SHUTDOWN"]


def test_an_interrupted_command_drops_the_connection_rather_than_logging_out(server):
    box = account(server)
    box.sign_in()

    server.breaks = 1  # Ctrl-C halfway through LIST's reply
    with pytest.raises(KeyboardInterrupt):
        box.list_message_ids()
    box.close()
    assert [c[0] for c in server.commands] == ["LOGIN", "LIST", "SHUTDOWN"]  # no LOGOUT into it


def test_a_server_no_keeps_the_session(server):
    server.refused.add("INBOX")
    server.add_folder("Projects")
    account(server).list_message_ids()
    assert [c[0] for c in server.commands].count("LOGIN") == 1


# ── review findings ──────────────────────────────────────────────────────────


def test_deletions_reach_a_backup_that_reads_labels_first(server, tmp_path):
    # backup asks for the label catalogue before history_changes, on one account
    server.add("INBOX", msgid="<k@x>")
    gone = server.add("INBOX", msgid="<g@x>")
    ids = ids_by_msgid(account(server, tmp_path), server)
    del server.folders["INBOX"]["uids"][gone]
    box = account(server, tmp_path)
    box.label_catalogue()
    touched, deleted, _latest = box.history_changes(box.current_history_id())
    assert deleted == {ids["<g@x>"]} and ids["<k@x>"] in touched
    lists = [c for c in server.commands if c == ("LIST",)]
    assert len(lists) == 2  # one scan per account object: labels and history share it


def test_a_copy_in_trash_doesnt_hide_mail_that_lives_in_the_inbox(server):
    raw = make_eml(msgid="<kept@x>")
    server.add("INBOX", raw=raw, flags={"\\Seen"})
    server.add("Trash", raw=raw, flags={"\\Seen"})
    server.add("Junk", msgid="<junk@x>", flags={"\\Seen"})
    box = account(server)
    (inbox,) = box.list_message_ids()
    assert box.message_labels([inbox]) == {inbox: {"INBOX"}}
    hidden = set(box.list_message_ids(include_spam_trash=True)) - {inbox}
    assert [box.message_labels([g])[g] for g in hidden] == [{"SPAM"}]


def test_names_stand_in_for_the_roles_a_server_marks_no_folder_for():
    # A server marking Sent and Trash only: Junk and Drafts are known by name.
    lines = [
        b'(\\Noinferiors) "/" "INBOX"',
        b'(\\HasNoChildren) "/" "Archive"',
        b'(\\HasNoChildren \\Trash) "/" "Deleted Messages"',
        b'(\\HasNoChildren) "/" "Junk"',
        b'(\\HasNoChildren \\Sent) "/" "Sent Messages"',
        b'(\\HasNoChildren) "/" "Drafts"',
        b'(\\HasNoChildren) "/" "Trash"',  # a user's own: the server marks its Trash
    ]
    entries = imap.list_entries(lines)
    assert {f.name: f.label for f in imap.folders_from(entries)} == {
        "INBOX": "INBOX",
        "Archive": "Archive",
        "Deleted Messages": "TRASH",
        "Junk": "SPAM",
        "Sent Messages": "SENT",
        "Drafts": "DRAFT",
        "Trash": "Trash",
    }
    assert imap.marked_roles(entries) == {"SENT", "TRASH"}


def test_mailcellars_labels_travel_as_keywords_and_back():
    assert imap.keyword_for("offload-candidate") == "$mailcellar-offload-candidate"
    assert imap.keyword_for("restored/2026-10-27") == "$mailcellar-restored.2026-10-27"
    assert imap.label_from_keyword("$mailcellar-restored.2026-10-27") == "restored/2026-10-27"
    assert imap.label_from_keyword("$MailCellar-Dedup-Redundant") == "dedup-redundant"  # any case
    with pytest.raises(gmailapi.GmailError, match="can't be marked on an IMAP server"):
        imap.keyword_for("Dedup/Check")  # capitals: a keyword can't carry it back
    assert (
        imap.label_from_keyword("$Forwarded") is None and imap.label_from_keyword("\\Seen") is None
    )
    assert imap.label_from_keyword("$mailcellar-restored/2026") is None  # not a form we write
    assert imap.flag_labels({"$mailcellar-offload-candidate", "$Forwarded"}) == {
        "UNREAD",
        "offload-candidate",
    }


def test_a_review_folder_stands_for_its_label():
    review = "mailcellar/review/offload-candidate"
    assert imap.folder_label(review, "/", set()) == "offload-candidate"
    assert imap.folder_label("mailcellar.review.offload-candidate", ".", set()) == (
        "offload-candidate"
    )
    assert imap.folder_label("mailcellar/notes", "/", set()) == "mailcellar/notes"  # the user's


def test_the_scan_reads_review_marks_as_labels(server):
    server.add("INBOX", msgid="<k@x>", flags={"\\Seen", "$mailcellar-offload-candidate"})
    server.add_folder("mailcellar/review/dedup-redundant")
    server.add("mailcellar/review/dedup-redundant", msgid="<f@x>", flags={"\\Seen"})
    box = account(server)
    labels = box.message_labels(box.list_message_ids())
    assert sorted(map(sorted, labels.values())) == [
        ["INBOX", "offload-candidate"],
        ["dedup-redundant"],
    ]
    names = {entry["name"] for entry in box.label_catalogue()}
    assert {"offload-candidate", "dedup-redundant"} <= names


def test_a_users_folder_named_like_a_mark_gives_the_label_not_the_mark(server):
    server.add_folder("offload-candidate")  # the user's own
    server.add("offload-candidate", msgid="<u@x>", flags={"\\Seen"})
    server.add("INBOX", msgid="<k@x>", flags={"\\Seen", "$mailcellar-offload-candidate"})
    server.add_folder("mailcellar/review/offload-candidate")
    server.add("mailcellar/review/offload-candidate", msgid="<f@x>", flags={"\\Seen"})
    scan = account(server)._scan()[1]
    by_folder = {m.where[0]: m for m in scan.messages.values()}
    assert by_folder["offload-candidate"].labels == {"offload-candidate"}
    assert by_folder["offload-candidate"].marks == frozenset()  # never the mark
    assert by_folder["INBOX"].marks == {"offload-candidate"}
    assert by_folder["mailcellar/review/offload-candidate"].marks == {"offload-candidate"}


def test_listing_passes_and_when_they_stop():
    first = frozenset(range(1001, 2001))
    assert imap.next_search(frozenset(), first, 2500) == ("UID", "1:1000")
    assert imap.stalled(first, first) and not imap.stalled(frozenset(), first)  # range ignored
    assert imap.short_by(first, 2500) == 1500 and imap.short_by(first, None) == 0
    assert imap.next_search(frozenset(), frozenset(), 5) is None  # an empty pass
    assert imap.next_search(frozenset(), frozenset({1, 2}), 9) is None  # down to UID 1
    assert imap.next_search(frozenset(), frozenset({5, 6}), 2) is None  # the count reached
    assert imap.next_search(frozenset(), frozenset({5, 6}), None) == ("UID", "1:4")


def test_a_listing_cut_short_skips_the_folder_rather_than_lose_its_mail(
    server, monkeypatch, caplog
):
    server.message_limit = 2
    server.add("INBOX", msgid="<kept@x>", flags={"\\Seen"})
    for n in range(5):
        server.add("Projects/2026", msgid=f"<p{n}@x>", flags={"\\Seen"})
    search = server.uid

    def ignoring_ranges(command, *args):  # answers every SEARCH as SEARCH ALL
        return search(command, *((None, "ALL") if command == "SEARCH" else args))

    monkeypatch.setattr(server, "uid", ignoring_ranges)
    box = account(server)
    assert len(box.list_message_ids()) == 1  # INBOX's; no hang, and no partial folder
    assert "skipped Projects/2026: listed 2 of 5 messages" in caplog.text
    assert box.unread() == ("Projects/2026",)


def test_a_listing_short_of_the_folders_count_skips_it(server, monkeypatch, caplog):
    server.message_limit = 2
    for n in range(5):
        server.add("Projects/2026", msgid=f"<p{n}@x>", flags={"\\Seen"})
    search = server.uid

    def empty_below(command, *args):  # SEARCH UID lo:hi finds nothing
        if command == "SEARCH" and args[1:2] == ("UID",):
            return "OK", [b""]
        return search(command, *args)

    monkeypatch.setattr(server, "uid", empty_below)
    box = account(server)
    assert box.list_message_ids() == []
    assert "listed 2 of 5 messages" in caplog.text and box.unread() == ("Projects/2026",)


def test_a_folder_examine_gives_no_count_for_is_listed_in_full(server):
    server.message_limit = 1000  # fetches go 200 at a time, under it
    server.no_exists = True
    for n in range(1500):
        server.add("Projects/2026", msgid=f"<p{n}@x>", flags={"\\Seen"})
    assert len(account(server).list_message_ids()) == 1500  # passes until one is empty


def test_examine_without_a_count_lists_until_a_pass_is_empty():
    assert imap.exists_from([b"12"]) == 12
    assert imap.exists_from([None]) is None and imap.exists_from([]) is None


def test_a_flag_on_a_folder_that_cant_be_opened_marks_no_role():
    lines = [
        b'(\\Noselect \\Trash) "/" "Parent"',
        b'(\\HasNoChildren) "/" "Trash"',
    ]
    entries = imap.list_entries(lines)
    assert imap.marked_roles(entries) == frozenset()
    assert [f.label for f in imap.folders_from(entries)] == ["TRASH"]


def test_a_users_own_junk_folder_is_backed_up_on_a_special_use_server(server):
    server.add_folder("Junk Mail")  # the user's own folder; the server marks Junk itself
    server.folders["Spam"] = server.folders.pop("Junk Mail")
    server.add("Spam", msgid="<mine@x>", flags={"\\Seen"})
    box = account(server)
    (gid,) = box.list_message_ids()
    assert box.message_labels([gid]) == {gid: {"Spam"}}


def test_different_messages_sharing_a_header_id_are_told_apart(server):
    one = make_eml(msgid="<same@x>", body="one!")
    two = make_eml(msgid="<same@x>", body="two!")  # same Message-ID, date and size
    server.add("INBOX", raw=one, flags={"\\Seen"})
    server.add("Projects/2026", raw=two, flags={"\\Seen"})
    box = account(server)
    assert len(box.list_message_ids()) == 2


def test_identical_copies_keep_the_header_id_so_a_later_move_keeps_it(server, tmp_path):
    raw = make_eml(msgid="<copy@x>")
    server.add("INBOX", raw=raw, flags={"\\Seen"})
    uid = server.add("Projects/2026", raw=raw, flags={"\\Seen"})
    (gid,) = account(server, tmp_path).list_message_ids()
    server.folders["Projects/2026"]["uids"].pop(uid)
    del server.folders["INBOX"]["uids"][1]
    server.add("Sent", raw=raw, flags={"\\Seen"})  # moved by hand, header ID alone this time
    assert account(server, tmp_path).list_message_ids() == [gid]


def test_a_folder_that_cant_be_read_is_skipped_and_its_mail_not_taken_for_gone(
    server, tmp_path, caplog
):
    server.add("INBOX", msgid="<i@x>")
    server.add("Projects/2026", msgid="<p@x>")
    ids = ids_by_msgid(account(server, tmp_path), server)
    server.refused.add("Projects/2026")
    box = account(server, tmp_path)
    assert box.list_message_ids() == [ids["<i@x>"]]
    assert "skipped Projects/2026" in caplog.text
    _touched, deleted, _ = box.history_changes(box.current_history_id())
    assert deleted == set()


def test_a_fetch_without_a_body_is_a_failure(server):
    uid = server.add("INBOX", msgid="<nb@x>")
    box = account(server)
    (gid,) = box.list_message_ids()
    server.bodiless.add(uid)
    got = []
    assert box.get_raw([gid], got.append) == [f"{gid}: INBOX returned no body for UID {uid}"]
    assert got == []
