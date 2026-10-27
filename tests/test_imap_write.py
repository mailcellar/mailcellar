"""IMAP step 3 (#113): writing to an IMAP account, where the server can do it safely."""

import csv
import sqlite3

import pytest

from fake_imap import FakeImap
from mailcellar import cli, gmailapi, imap, mailbox
from mailcellar.backup import gyb
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


def test_review_by_folder_isnt_supported_yet(config, server):
    (config / gmailapi.IMAP_SETTINGS).write_text('host = "imap.example.org"\nreview = "folder"\n')
    server.add("INBOX", msgid="<a@x>", flags={"\\Seen"})
    box = mailbox.open_mailbox(ADDRESS, config, "modify")  # restore, say, may still write
    with pytest.raises(gmailapi.GmailError, match="--review keyword"):
        box.add_label(box.list_message_ids(), "offload-candidate")
    box.close()


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


def test_offload_on_an_imap_account_labels_trashes_and_records(server, tmp_path, monkeypatch):
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
    old = "01-Jan-2015 10:00:00 +0000"
    for n in range(3):
        server.add("Projects", msgid=f"<p{n}@x>", flags={"\\Seen"}, date=old)
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
    assert (
        len(server.folders["Trash"]["uids"]) == 2 and len(server.folders["Projects"]["uids"]) == 1
    )
    with open("log.csv", newline="", encoding="utf-8") as fh:
        outcomes = sorted(r["outcome"] for r in csv.DictReader(fh) if r["mode"].startswith("trash"))
    assert outcomes == ["skipped", "trashed", "trashed"]
    con = sqlite3.connect(backup_folder(ADDRESS) / "meta.sqlite")
    try:
        assert con.execute("SELECT count(*) FROM offloaded").fetchone() == (2,)
    finally:
        con.close()
