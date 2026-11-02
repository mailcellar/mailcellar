"""
imap.ImapAccount's incremental scan (backup and count) over FakeImap: a
folder unchanged since the last backup is read from its map, a changed one
fetches only what changed (CHANGEDSINCE), and the Scan is the one a full
read gives.
"""

from dataclasses import replace

import pytest

from fake_imap import FakeImap
from mailcellar import gmailapi, imap
from mailcellar.backup import sidecar
from test_imap_scan import ADDRESS, SETTINGS, Saving
from test_index import make_eml


def account(server, folder, incremental=True):
    """An account that saves its map after scanning, as backup does."""
    return Saving(
        ADDRESS,
        SETTINGS,
        "secret",
        folder=folder,
        connect=lambda: server,
        sleep=lambda s: None,
        incremental=incremental,
    )


def full_scan(server, folder):
    """A full read, saving nothing: what the incremental scan must equal."""
    box = imap.ImapAccount(
        ADDRESS, SETTINGS, "secret", folder=folder, connect=lambda: server, sleep=lambda s: None
    )
    return box._scan()[1]


def backed_up(server, folder):
    """A backup's scan: the map and counters saved. The scan."""
    return account(server, folder)._scan()[1]


def incremental_matches_full(server, folder):
    """The incremental scan, saved, after checking it against a full one. Its commands."""
    full = full_scan(server, folder)
    server.commands.clear()
    scan = backed_up(server, folder)
    sent = list(server.commands)
    assert scan.messages == full.messages
    assert scan.labels == full.labels
    assert scan.maps == full.maps  # what is saved: rows, flags and counters
    assert scan.skipped == full.skipped
    kept = {name: m for name, m in scan.maps.items() if m.rows or m.state}  # not empty, uncounted
    saved = imap.saved_folders(sidecar.read_imap_map(folder))
    assert {name: saved.get(name) for name in kept} == kept  # and what was saved
    return sent


def examined(commands) -> list[str]:
    return [c[1].removesuffix(" (CONDSTORE)").strip('"') for c in commands if c[0] == "EXAMINE"]


def fetched_flags_in_full(commands) -> bool:
    return any(
        c[:2] == ("UID", "FETCH") and "FLAGS" in c[3] and "CHANGEDSINCE" not in c[3]
        for c in commands
    )


@pytest.fixture
def server():
    """A CONDSTORE server with LIST-STATUS, as Dovecot is."""
    s = FakeImap()
    s.signed_in = (*s.signed_in, "CONDSTORE", "ENABLE", "LIST-STATUS")
    s.add_folder("Sent", special=["\\Sent"])
    s.add_folder("Trash", special=["\\Trash"])
    s.add_folder("Work")
    s.add_folder("Parent", selectable=False)
    s.add("INBOX", msgid="<a@x>", flags={"\\Seen"})
    s.add("INBOX", msgid="<b@x>")
    s.add("Work", msgid="<c@x>", flags={"\\Seen", "\\Flagged"})
    s.add("Sent", msgid="<d@x>", flags={"\\Seen"})
    return s


@pytest.fixture
def gmail():
    """Gmail's IMAP, with CONDSTORE and LIST-STATUS as Gmail lists them."""
    s = FakeImap()
    s.signed_in = (*s.signed_in, "X-GM-EXT-1", "CONDSTORE", "ENABLE", "LIST-STATUS")
    s.account_modseq = True
    s.add_folder("[Gmail]", selectable=False)
    s.add_folder("[Gmail]/All Mail", special=["\\All"])
    s.add_folder("[Gmail]/Trash", special=["\\Trash"])
    s.add_folder("[Gmail]/Spam", special=["\\Junk"])
    s.add_folder("Projects")
    for folders, msgid, gm, labels in (
        (["INBOX"], "<in@x>", 1, ("Receipts",)),  # a label shown in no folder: X-GM-LABELS only
        (["INBOX", "Projects"], "<both@x>", 2, ()),
        ([], "<archived@x>", 3, ()),
    ):
        raw = make_eml(msgid=msgid)
        for folder in (*folders, "[Gmail]/All Mail"):
            s.add(folder, raw=raw, flags={"\\Seen"}, gm=gm, labels=labels)
    s.add("[Gmail]/Trash", msgid="<gone@x>", gm=4, labels=("Projects",))
    return s


# ── pure helpers ─────────────────────────────────────────────────────────────


def test_status_answers_parse_as_each_folders_counters():
    data = [
        b'"INBOX" (MESSAGES 3 UIDNEXT 9 UIDVALIDITY 7 HIGHESTMODSEQ 41)',
        b"Work (messages 1 uidnext 2 uidvalidity 5 highestmodseq 6)",  # an atom, lower case
        (b"{9}", b"Two Words"),  # a name sent as a literal
        b" (MESSAGES 0 UIDNEXT 1 UIDVALIDITY 4 HIGHESTMODSEQ 1)",
        b'"Short" (MESSAGES 0 UIDNEXT 1 UIDVALIDITY 4)',  # no HIGHESTMODSEQ: read in full
        b'"Plain" (MESSAGES 2 UIDNEXT 3 UIDVALIDITY 4 HIGHESTMODSEQ 0)',  # NOMODSEQ
        b"garbage",
    ]
    assert imap.folder_states(data) == {
        "INBOX": imap.FolderState(7, 9, 3, 41),
        "Work": imap.FolderState(5, 2, 1, 6),
        "Two Words": imap.FolderState(4, 1, 0, 1),
    }


def test_examines_counters_need_all_four():
    assert imap.examined_state(7, 3, b"9", b"41") == imap.FolderState(7, 9, 3, 41)
    assert imap.examined_state(7, 3, b"9", None) is None  # [NOMODSEQ], or no CONDSTORE
    assert imap.examined_state(7, 3, None, b"41") is None
    assert imap.examined_state(7, None, b"9", b"41") is None
    assert imap.examined_state(7, 3, b"9", b"0") is None


def test_changedsince_asks_for_every_uid_at_once_unless_a_messagelimit_says_otherwise():
    assert imap.changed_sets([1, 2, 3], {"CONDSTORE"}) == ["1:*"]
    assert imap.changed_sets([], {"CONDSTORE"}) == []
    assert imap.changed_sets([3, 1, 2], {"CONDSTORE", "MESSAGELIMIT=2"}) == ["1,2", "3"]


def test_how_a_folder_is_read():
    state = imap.FolderState(7, 9, 3, 41)
    rows = {1: imap.Saved("a", frozenset())}
    saved = imap.SavedFolder(7, rows, state)
    assert imap.read_kind(saved, state, gmail=False, condstore=True) == "saved"
    moved = imap.FolderState(7, 9, 3, 42)
    assert imap.read_kind(saved, moved, gmail=False, condstore=True) == "changed"
    assert imap.read_kind(saved, moved, gmail=False, condstore=False) == "full"  # Yahoo
    assert imap.read_kind(saved, imap.FolderState(8, 9, 3, 41), False, True) == "full"
    assert imap.read_kind(saved, None, gmail=False, condstore=True) == "full"
    assert imap.read_kind(imap.SavedFolder(7, rows), state, False, True) == "full"
    assert imap.read_kind(None, state, gmail=False, condstore=True) == "full"
    # An empty folder that stayed empty: whatever HIGHESTMODSEQ did, as on Gmail.
    empty = imap.SavedFolder(7, {}, imap.FolderState(7, 9, 0, 41))
    assert imap.read_kind(empty, imap.FolderState(7, 9, 0, 60), False, True) == "saved"
    assert imap.read_kind(empty, imap.FolderState(7, 10, 0, 60), False, True) == "changed"
    # Rows saved before flags were, or on Gmail without a thread: fetched again.
    short = imap.SavedFolder(7, {1: imap.Saved("a")}, state)
    assert imap.read_kind(short, state, gmail=False, condstore=True) == "changed"
    assert imap.read_kind(saved, state, gmail=True, condstore=True) == "changed"


def test_saving_writes_only_the_rows_that_changed():
    seen, unseen = imap.Saved("a", frozenset({"\\Seen"})), imap.Saved("a", frozenset())
    before = imap.SavedFolder(7, {1: seen, 2: seen, 3: seen}, imap.FolderState(7, 4, 3, 41))
    after = imap.SavedFolder(7, {1: seen, 2: unseen, 4: seen}, imap.FolderState(7, 5, 3, 50))
    assert imap.row_changes(before, after) == ({3}, {2: unseen, 4: seen})
    assert imap.row_changes(before, replace(before, state=None)) == (frozenset(), {})
    assert imap.row_changes(before, replace(after, uidvalidity=8)) is None  # replaced whole
    assert imap.row_changes(None, after) is None


def test_recent_is_the_sessions_not_the_messages():
    # It never moves a mod-sequence, so saved it would outlive the session that saw it.
    fetched = imap.parse_fetched(b"1 (UID 4 FLAGS (\\Seen \\Recent))", None)
    assert fetched.flags == {"\\Seen"}


# ── the scan ─────────────────────────────────────────────────────────────────


def test_an_unchanged_mailbox_is_read_with_one_command(server, tmp_path):
    backed_up(server, tmp_path)
    sent = incremental_matches_full(server, tmp_path)
    assert examined(sent) == []
    assert [c[0] for c in sent].count("LIST") == 2  # the folders, then their counters


@pytest.mark.parametrize(
    "change",
    [
        "new mail",
        "a flag",
        "a keyword",
        "a move",
        "a copy",
        "mail deleted",
        "a folder created",
        "a folder deleted",
        "uidvalidity reset",
    ],
)
def test_after_each_kind_of_change_the_scan_is_a_full_reads(server, tmp_path, change):
    backed_up(server, tmp_path)
    inbox = server.folders["INBOX"]["uids"]
    if change == "new mail":
        server.add("INBOX", msgid="<new@x>")
    elif change == "a flag":
        inbox[2]["flags"].add("\\Seen")
    elif change == "a keyword":
        inbox[1]["flags"].add(imap.keyword_for("offload-candidate"))
    elif change == "a move":
        server.move("INBOX", 1, "Work")
    elif change == "a copy":
        server.add("Trash", raw=inbox[2]["raw"], date=inbox[2]["date"])
    elif change == "mail deleted":
        del server.folders["Work"]["uids"][1]
    elif change == "a folder created":
        server.add_folder("New")
        server.add("New", msgid="<e@x>")
    elif change == "a folder deleted":
        del server.folders["Work"]
    elif change == "uidvalidity reset":
        server.reset_uidvalidity("INBOX")

    sent = incremental_matches_full(server, tmp_path)

    assert "Sent" not in examined(sent)  # untouched by every change
    if change not in ("a folder created", "uidvalidity reset"):  # those are read in full
        assert not fetched_flags_in_full(sent)
    assert incremental_matches_full(server, tmp_path)  # and the run after, with nothing new


def test_a_changed_folder_fetches_only_what_changed(server, tmp_path):
    backed_up(server, tmp_path)
    server.folders["INBOX"]["uids"][2]["flags"].add("\\Seen")
    sent = incremental_matches_full(server, tmp_path)
    assert examined(sent) == ["INBOX"]
    fetches = [c for c in sent if c[:2] == ("UID", "FETCH")]
    assert fetches == [("UID", "FETCH", "1:*", "(FLAGS) (CHANGEDSINCE 3)")]
    # Nothing arrived or left: the saved UIDs are the folder's, with no listing.
    assert not [c for c in sent if c[:2] == ("UID", "SEARCH")]


def test_mail_arriving_or_leaving_has_the_folder_listed(server, tmp_path):
    backed_up(server, tmp_path)
    server.add("INBOX", msgid="<new@x>")
    del server.folders["Work"]["uids"][1]
    sent = incremental_matches_full(server, tmp_path)
    assert [c[1] for c in sent if c[:2] == ("UID", "SEARCH")] == ["SEARCH", "SEARCH"]


def test_the_saved_uids_stand_only_while_nothing_arrived_or_left():
    state = imap.FolderState(7, 9, 2, 41)
    rows = {3: imap.Saved("a", frozenset()), 5: imap.Saved("b", frozenset())}
    saved = imap.SavedFolder(7, rows, state)
    assert imap.same_uids(saved, imap.FolderState(7, 9, 2, 50)) == [3, 5]  # flags changed
    assert imap.same_uids(saved, imap.FolderState(7, 10, 2, 50)) is None  # one in, one out
    assert imap.same_uids(saved, imap.FolderState(7, 9, 1, 50)) is None  # one out
    assert imap.same_uids(saved, imap.FolderState(8, 9, 2, 50)) is None
    assert imap.same_uids(imap.SavedFolder(7, {3: rows[3]}, state), state) is None  # one unnamed
    assert imap.same_uids(imap.SavedFolder(7, {}, imap.FolderState(7, 1, 0, 4)), state) is None
    assert imap.same_uids(None, state) is None


@pytest.mark.parametrize(
    "change",
    ["new mail", "a flag", "a label added", "a label removed", "trashed", "a hidden label"],
)
def test_gmail_after_each_kind_of_change_the_scan_is_a_full_reads(gmail, tmp_path, change):
    backed_up(gmail, tmp_path)
    store = gmail.folders["[Gmail]/All Mail"]["uids"]
    if change == "new mail":
        raw = make_eml(msgid="<new@x>")
        for folder in ("INBOX", "[Gmail]/All Mail"):
            gmail.add(folder, raw=raw, gm=5)
    elif change == "a flag":
        for f in ("INBOX", "[Gmail]/All Mail"):
            gmail.folders[f]["uids"][1]["flags"].discard("\\Seen")
    elif change == "a label added":
        gmail.add("Projects", raw=store[1]["raw"], flags={"\\Seen"}, gm=1, labels=("Receipts",))
    elif change == "a label removed":
        del gmail.folders["Projects"]["uids"][1]
    elif change == "trashed":  # out of every folder, into Trash with its labels
        del gmail.folders["INBOX"]["uids"][2], gmail.folders["Projects"]["uids"][1], store[2]
        gmail.add("[Gmail]/Trash", msgid="<both@x>", flags={"\\Seen"}, gm=2, labels=("Projects",))
    elif change == "a hidden label":  # X-GM-LABELS changes on every copy
        for f in ("INBOX", "[Gmail]/All Mail"):
            gmail.folders[f]["uids"][1]["labels"] = ()

    incremental_matches_full(gmail, tmp_path)
    assert incremental_matches_full(gmail, tmp_path)


def test_gmail_reads_every_folder_that_has_mail_after_any_change_and_saves_only_that(
    gmail, tmp_path, monkeypatch
):
    # Gmail's HIGHESTMODSEQ is the account's: one flag moves it in every folder.
    backed_up(gmail, tmp_path)
    for f in ("INBOX", "[Gmail]/All Mail"):
        gmail.folders[f]["uids"][1]["flags"].discard("\\Seen")
    written = {}

    def update(con, folder, uidvalidity, gone, uids, state=None):
        written[folder] = (set(gone), set(uids))
        real(con, folder, uidvalidity, gone, uids, state)

    real = sidecar.update_imap_folder
    monkeypatch.setattr(sidecar, "update_imap_folder", update)
    monkeypatch.setattr(sidecar, "replace_imap_folder", None)  # no folder replaced whole
    sent = incremental_matches_full(gmail, tmp_path)

    assert sorted(examined(sent)) == ["INBOX", "Projects", "[Gmail]/All Mail", "[Gmail]/Trash"]
    assert not fetched_flags_in_full(sent)
    assert not [c for c in sent if c[:2] == ("UID", "SEARCH")]  # nothing arrived or left
    assert written == {
        "INBOX": (set(), {1}),
        "[Gmail]/All Mail": (set(), {1}),
        "Projects": (set(), set()),  # its counters only
        "[Gmail]/Trash": (set(), set()),
        "[Gmail]/Spam": (set(), set()),  # empty, not read: the counters LIST-STATUS gave
    }


def test_gmail_labels_with_no_folder_survive_an_unchanged_scan(gmail, tmp_path):
    backed_up(gmail, tmp_path)
    gmail.commands.clear()
    box = account(gmail, tmp_path)
    assert box.message_labels(["1", "4"]) == {
        "1": {"INBOX", "Receipts"},
        "4": {"TRASH", "Projects", "UNREAD"},
    }
    assert examined(gmail.commands) == []


def test_a_second_save_diffs_against_the_first(server, tmp_path):
    backed_up(server, tmp_path)
    box = account(server, tmp_path)
    flags = server.folders["INBOX"]["uids"][2]["flags"]
    flags.add("\\Seen")
    box._scan()
    flags.discard("\\Seen")  # back as the backup before last had it
    box._changed()
    scan = box._scan()[1]
    saved = imap.saved_folders(sidecar.read_imap_map(tmp_path))
    assert saved["INBOX"] == scan.maps["INBOX"]


def test_a_server_with_only_folder_counters_reads_changed_folders_in_full(server, tmp_path):
    # Yahoo: HIGHESTMODSEQ in EXAMINE and STATUS, CHANGEDSINCE refused.
    server.signed_in = ("IMAP4rev1", "SPECIAL-USE", "MOVE", "UIDPLUS", "XYMHIGHESTMODSEQ")
    backed_up(server, tmp_path)
    server.folders["INBOX"]["uids"][2]["flags"].add("\\Seen")
    sent = incremental_matches_full(server, tmp_path)
    assert examined(sent) == ["INBOX"]
    assert [c[0] for c in sent].count("STATUS") == 4  # no LIST-STATUS: one a folder
    assert fetched_flags_in_full(sent)
    assert not any("CHANGEDSINCE" in str(c) for c in sent)


def test_a_server_without_counters_reads_every_folder_every_time(server, tmp_path):
    server.signed_in = ("IMAP4rev1", "SPECIAL-USE", "MOVE", "UIDPLUS")
    backed_up(server, tmp_path)
    sent = incremental_matches_full(server, tmp_path)
    assert examined(sent) == ["INBOX", "Sent", "Trash", "Work"]
    assert "STATUS" not in [c[0] for c in sent]


def test_a_folder_that_keeps_no_mod_sequences_is_read_in_full(server, tmp_path):
    server.nomodseq.add("Work")
    backed_up(server, tmp_path)
    sent = incremental_matches_full(server, tmp_path)
    assert examined(sent) == ["Work"]


def test_a_skipped_folder_keeps_its_map_and_is_read_in_full_next_time(server, tmp_path):
    backed_up(server, tmp_path)
    server.refused.add("Work")
    server.folders["Work"]["uids"][1]["flags"].discard("\\Flagged")
    scan = backed_up(server, tmp_path)
    assert scan.skipped == {"Work"}
    assert sidecar.read_imap_map(tmp_path)["Work"][2] is None  # its counters forgotten
    server.refused.clear()
    server.commands.clear()
    incremental_matches_full(server, tmp_path)


def test_a_failed_list_status_reads_every_folder_in_full(server, tmp_path, caplog):
    backed_up(server, tmp_path)

    def refused(name, *args):
        server._command(name, *args)
        raise gmailapi.GmailError("LIST-STATUS: NO")

    server.xatom = refused
    sent = incremental_matches_full(server, tmp_path)
    assert examined(sent) == ["INBOX", "Sent", "Trash", "Work"]
    assert "reading every folder in full" in caplog.text


def test_mail_the_map_couldnt_name_has_its_folder_read_again(gmail, tmp_path, caplog):
    gmail.add("INBOX", msgid="<unnamed@x>")  # no X-GM-MSGID: left for the next run
    backed_up(gmail, tmp_path)
    assert sidecar.read_imap_map(tmp_path)["INBOX"][2] is None
    gmail.commands.clear()
    backed_up(gmail, tmp_path)
    assert "INBOX" in examined(gmail.commands)


def test_rows_saved_before_flags_were_are_fetched_once(server, tmp_path):
    backed_up(server, tmp_path)
    con = sidecar.open_meta(tmp_path)
    con.execute("UPDATE imap_map SET flags = NULL WHERE folder = 'Work'")
    con.commit()
    con.close()
    sent = incremental_matches_full(server, tmp_path)
    assert examined(sent) == ["Work"]
    assert ("UID", "FETCH", "1", "(FLAGS)") in sent  # what CHANGEDSINCE wouldn't give
    assert examined(incremental_matches_full(server, tmp_path)) == []


def test_without_incremental_every_folder_is_read(server, tmp_path):
    backed_up(server, tmp_path)
    server.commands.clear()
    account(server, tmp_path, incremental=False)._scan()
    assert examined(server.commands) == ["INBOX", "Sent", "Trash", "Work"]


def test_after_a_write_every_folder_is_read_in_full(server, tmp_path):
    backed_up(server, tmp_path)
    box = account(server, tmp_path)
    box._scan()
    box._changed()
    server.commands.clear()
    box._scan()
    assert examined(server.commands) == ["INBOX", "Sent", "Trash", "Work"]


def test_backup_and_count_are_incremental_and_full_isnt(monkeypatch, tmp_path):
    from mailcellar import mailbox
    from mailcellar.backup import commands

    seen = []
    monkeypatch.setattr(gmailapi, "resolve_account", lambda root, a: (ADDRESS, tmp_path))
    monkeypatch.setattr(
        mailbox, "open_mailbox", lambda *a, incremental=False, **k: seen.append(incremental)
    )

    class Args:
        config, account, folder = str(tmp_path), ADDRESS, str(tmp_path)

    for full, refresh in ((False, False), (True, False), (False, True)):
        args = Args()
        args.full, args.refresh_labels = full, refresh
        commands.connect(args)
    del args.full, args.refresh_labels  # count has neither option
    commands.connect(args)
    assert seen == [True, False, False, True]
