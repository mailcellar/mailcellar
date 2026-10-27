"""IMAP step 3 (#113): writing to an IMAP account, where the server can do it safely."""

import csv
import sqlite3

import pytest

from fake_imap import FakeImap
from mailcellar import cli, gmailapi, imap, mailbox
from mailcellar.backup import commands, gyb, sidecar
from mailcellar.dedup.apply import DEDUP_LABEL, apply_plan
from mailcellar.offload import cli as offload_cli
from mailcellar.paths import backup_folder
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


# ── restore into an IMAP account ─────────────────────────────────────────────


def test_restore_takes_offloaded_mail_out_of_trash_home(home, tmp_path):
    offload_two(home, tmp_path)
    assert cli.main(["restore", "home", "--offloaded", "--yes"]) == 0
    assert not home.folders["Trash"]["uids"] and len(home.folders["Projects"]["uids"]) == 3
    flags = [m["flags"] for m in home.folders["Projects"]["uids"].values()]
    assert not any("$mailcellar-offload-candidate" in f for f in flags)  # the mark taken off
    assert sum(any(k.startswith("$mailcellar-restored.") for k in f) for f in flags) == 2
    assert cli.main(["backup", "home"]) == 0


def test_restore_appends_mail_the_server_no_longer_has(home, tmp_path, capsys):
    offload_two(home, tmp_path)
    home.folders["Trash"]["uids"].clear()  # the server, or a mail client, emptied Trash
    assert cli.main(["backup", "home"]) == 0
    assert cli.main(["restore", "home", "--offloaded", "--yes"]) == 0
    back = home.folders["Projects"]["uids"].values()
    assert len(back) == 3 and all("\\Seen" in m["flags"] for m in back)
    assert {m["date"] for m in back} == {OLD}  # the Date header's date, as Gmail's import takes it
    capsys.readouterr()
    assert cli.main(["backup", "home"]) == 0
    assert "Downloaded 0 messages" in capsys.readouterr().out  # the same IDs: nothing new


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
    assert failure.startswith(f"{ids['<b@x>']}: marking it in Projects failed")
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


def test_restore_after_review_by_folder_puts_mail_back_where_it_was(home, tmp_path):
    (tmp_path / "config" / "home" / gmailapi.IMAP_SETTINGS).write_text(
        'host = "imap.example.org"\nreview = "folder"\n'
    )
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
    assert plan.flags == "(\\Flagged $mailcellar-restored.2026-10-27)"  # unread: no \Seen
    assert imap.append_plan({"INBOX"}, folders, ".").flags == "(\\Seen)"
    assert imap.append_plan(set(), folders, ".").folders == ("INBOX",)  # no folder label


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
    assert imap.home_folders({"TRASH", "Gone"}, folders) == ("INBOX",)


def test_an_appended_message_gets_the_id_the_next_scan_gives_it():
    raw = make_eml(msgid="<a@x>")
    assert imap.appended_id(raw, 1_420_106_400_123) == imap.header_id(
        "<a@x>", 1_420_106_400_000, len(raw)
    )
    no_id = make_eml(msgid=None)
    assert imap.appended_id(no_id, 0) == imap.content_id(no_id)
    dated = b"Date: Thu, 01 Jan 2015 10:00:00 +0000\r\nSubject: s\r\n\r\nbody"
    assert imap.date_of(dated) == 1_420_106_400_000 and imap.date_of(b"Subject: s\r\n\r\n") is None


def test_restore_refuses_only_a_provider_mismatch():
    from mailcellar.restore.cli import provider_problem

    assert provider_problem("imap", "imap") == "" and provider_problem("gmail", "gmail") == ""
    assert "a Gmail backup into an IMAP account" in provider_problem("gmail", "imap")


# ── restore: the review's findings ───────────────────────────────────────────


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
    plan = imap.PutBack({}, {"Trash": frozenset({gid})}, {gid: ("INBOX",)}, ())
    assert imap.untrash_result(plan, {"Trash": "no"}, scan) == [
        f"{gid}: moving it out of Trash failed: no"
    ]
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
