"""An IMAP account through the commands: backup, count, meta, verify, and what refuses it."""

import sqlite3

import pytest

from fake_imap import FakeImap
from mailcellar import cli, gmailapi, imap
from mailcellar.backup import commands as backup_commands
from mailcellar.backup import gyb, sidecar
from mailcellar.paths import backup_folder
from test_index import make_eml

ADDRESS = "alex@example.org"


@pytest.fixture
def server(tmp_path, monkeypatch):
    """A configured IMAP account `home` whose server is a FakeImap."""
    root = tmp_path / "config"
    (root / "home").mkdir(parents=True)
    (root / "accounts.toml").write_text(f'home = "{ADDRESS}"\n')
    (root / "home" / gmailapi.IMAP_SETTINGS).write_text('host = "imap.example.org"\n')
    password = root / "home" / gmailapi.IMAP_PASSWORD
    password.write_text("secret\n")
    password.chmod(0o600)
    monkeypatch.setenv("MAILCELLAR_CONFIG", str(root))
    s = FakeImap()
    s.add_folder("Sent", special=["\\Sent"])
    s.add_folder("Trash", special=["\\Trash"])
    s.add_folder("Projects")
    s.add("INBOX", msgid="<a@x>", flags={"\\Seen"})
    s.add("INBOX", msgid="<b@x>")
    s.add("Sent", msgid="<c@x>", flags={"\\Seen"})
    s.add("Trash", msgid="<t@x>", flags={"\\Seen"})  # already in Trash: not backed up
    monkeypatch.setattr(imap.ImapAccount, "_connect_ssl", lambda self: s)
    return s


def rows(sql, *args):
    con = sqlite3.connect(backup_folder(ADDRESS) / "msg-db.sqlite")
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def labels():
    """{Message-ID: labels} from the backup's gyb index."""
    out = {}
    for filename, num in rows("SELECT message_filename, message_num FROM messages"):
        raw = (backup_folder(ADDRESS) / filename).read_bytes()
        mid = raw.split(b"Message-ID: ")[1].split(b"\n")[0].decode().strip()
        out[mid] = {lb for (lb,) in rows("SELECT label FROM labels WHERE message_num = ?", num)}
    return out


def test_an_imap_account_backs_up_and_follows_changes(server, capsys):
    assert cli.main(["backup", "home"]) == 0
    assert labels() == {"<a@x>": {"INBOX"}, "<b@x>": {"INBOX", "UNREAD"}, "<c@x>": {"SENT"}}
    assert sidecar.provider(backup_folder(ADDRESS)) == "imap"
    capsys.readouterr()

    server.move("INBOX", 1, "Projects")  # a@x filed
    server.folders["INBOX"]["uids"][2]["flags"].add("\\Seen")  # b@x read
    server.add("INBOX", msgid="<d@x>", flags={"\\Seen"})  # new mail
    del server.folders["Sent"]["uids"][1]  # c@x deleted for good
    assert cli.main(["backup", "home"]) == 0
    out = capsys.readouterr().out
    assert "Downloaded 1 messages" in out
    assert "1 backed-up messages were permanently deleted from the server" in out
    assert "'mailcellar forget --deleted' would take them, and any deleted before," in out
    got = labels()
    assert got["<a@x>"] == {"Projects"} and got["<b@x>"] == {"INBOX"}
    assert got["<d@x>"] == {"INBOX"} and "<c@x>" in got  # still in the backup

    assert cli.main(["count", "home"]) == 0
    out = capsys.readouterr().out
    assert "On the server (excl. junk/trash)" in out and "Gmail" not in out
    assert cli.main(["verify", "home", "--chain"]) == 0
    assert cli.main(["meta", "home"]) == 0
    read_only = ("LOGIN", "CAPABILITY", "LIST", "EXAMINE", "UID", "LOGOUT")
    assert all(c[0] in read_only for c in server.commands)
    # Each command logged out when done: backup twice, count, meta.
    assert [c[0] for c in server.commands].count("LOGOUT") == 4


def test_a_backup_of_another_provider_is_refused(server, capsys):
    assert cli.main(["backup", "home"]) == 0
    con = sidecar.open_meta(backup_folder(ADDRESS))
    assert sidecar.held_id_kind(con) == "imap"
    sidecar.set_state(con, "provider", "gmail")  # as if the folder held a Gmail backup
    sidecar.set_state(con, "id_kind", "gmail")
    con.commit()
    con.close()
    with pytest.raises(SystemExit, match="under Gmail's message IDs.* made from each message's"):
        cli.main(["backup", "home"])
    assert rows("SELECT count(*) FROM uids") == [(3,)]  # nothing added


def test_a_backup_from_before_the_id_kind_was_recorded_is_read_by_its_provider(tmp_path):
    con = sidecar.open_meta(tmp_path)
    assert sidecar.held_id_kind(con) == "gmail"  # the Gmail API's backups never said
    sidecar.set_state(con, "provider", "imap")
    assert sidecar.held_id_kind(con) == "imap"  # any IMAP backup's, Gmail's included
    sidecar.set_state(con, "id_kind", "gmail")
    assert sidecar.held_id_kind(con) == "gmail"
    con.close()


def as_gmail(server):
    """The server as Gmail's: X-GM-EXT-1, each message's X-GM-MSGID, an All Mail."""
    server.signed_in = (*server.signed_in, "X-GM-EXT-1")
    server.add_folder("[Gmail]/All Mail", special=["\\All"])
    for n, folder in enumerate(("INBOX", "Sent", "Trash")):
        for uid, m in server.folders[folder]["uids"].items():
            m["gm"] = m["thr"] = 0x100 * (n + 1) + uid
            if folder != "Trash":
                server.add("[Gmail]/All Mail", raw=m["raw"], flags=m["flags"], gm=m["gm"])


def set_state(**states):
    con = sidecar.open_meta(backup_folder(ADDRESS))
    for name, value in states.items():
        if value is None:
            con.execute("DELETE FROM meta WHERE name = ?", (name,))
        else:
            sidecar.set_state(con, name, value)
    con.commit()
    con.close()


def test_a_gmail_api_backup_is_carried_on_over_gmails_imap(server, capsys):
    as_gmail(server)
    assert cli.main(["backup", "home"]) == 0
    assert {g for (g,) in rows("SELECT uid FROM uids")} == {"101", "102", "201"}
    set_state(provider=None, id_kind=None)  # as the Gmail API's backups leave it
    capsys.readouterr()
    server.add("INBOX", msgid="<d@x>", flags={"\\Seen"}, gm=0x1FF)
    assert cli.main(["backup", "home"]) == 0
    assert "Downloaded 1 messages" in capsys.readouterr().out  # the rest are known
    folder = backup_folder(ADDRESS)
    assert (sidecar.provider(folder), sidecar.id_kind(folder)) == ("imap", "gmail")
    assert cli.main(["verify", "home", "--chain"]) == 0


def test_a_search_backs_up_only_what_gmails_search_finds(server, capsys):
    as_gmail(server)
    server.gmail_finds["from:example.com"] = {0x101, 0x301}  # 0x301 is in Trash
    assert cli.main(["backup", "home", "--search", "from:example.com"]) == 0
    assert {g for (g,) in rows("SELECT uid FROM uids")} == {"101"}
    assert "matching 'from:example.com'" in capsys.readouterr().err


def test_a_search_is_refused_by_a_server_without_gmails(server):
    with pytest.raises(SystemExit, match=r"no Gmail search \(X-GM-RAW\)"):
        cli.main(["backup", "home", "--search", "from:example.com"])


def test_gmail_mail_moved_to_trash_keeps_its_labels_in_the_backup(server):
    as_gmail(server)
    server.folders["INBOX"]["uids"][1]["labels"] = ("Receipts",)
    assert cli.main(["backup", "home"]) == 0
    assert labels()["<a@x>"] == {"INBOX", "Receipts"}
    # Trashed (offloaded, say): out of every folder, All Mail too; Gmail keeps its labels.
    uid = server.move("INBOX", 1, "Trash")
    server.folders["Trash"]["uids"][uid]["labels"] = ("Receipts", "\\Important")
    all_mail = server.folders["[Gmail]/All Mail"]["uids"]
    del all_mail[next(u for u, m in all_mail.items() if m["gm"] == 0x101)]
    assert cli.main(["backup", "home"]) == 0
    assert labels()["<a@x>"] == {"TRASH", "Receipts", "IMPORTANT"}


def test_a_move_from_the_gmail_api_gives_labels_their_names_and_reads_them_again(server, capsys):
    as_gmail(server)
    server.folders["INBOX"]["uids"][1]["labels"] = ("Receipts",)
    assert cli.main(["backup", "home"]) == 0
    folder = backup_folder(ADDRESS)
    con = sidecar.open_meta(folder)  # as the Gmail API leaves it: Label_N, its own history
    con.execute("UPDATE message_labels SET label_id = 'Label_9' WHERE label_id = 'Receipts'")
    con.execute("INSERT INTO message_labels VALUES ('201', 'Label_9')")  # c@x
    api_catalogue = [
        {"id": "INBOX", "name": "INBOX", "type": "system"},
        {"id": "Label_9", "name": "Receipts", "type": "user"},
    ]
    sidecar.replace_label_catalogue(con, api_catalogue)
    for name in ("provider", "id_kind"):
        con.execute("DELETE FROM meta WHERE name = ?", (name,))
    sidecar.set_state(con, "history_id", "12345")
    con.commit()
    con.close()
    del server.folders["Sent"]["uids"][1]  # c@x offloaded: only the backup has it
    del server.folders["[Gmail]/All Mail"]["uids"][3]
    server.folders["INBOX"]["uids"][2]["labels"] = ("Receipts",)  # b@x labelled meanwhile
    capsys.readouterr()
    assert cli.main(["backup", "home"]) == 0  # the first run after the move
    assert "updated labels on 1" in capsys.readouterr().out  # b@x's Receipts
    con = sidecar.open_meta(folder)
    held = {}
    for gid, label in con.execute("SELECT gmail_id, label_id FROM message_labels"):
        held.setdefault(gid, set()).add(label)
    con.close()
    assert held == {
        "101": {"INBOX", "Receipts"},
        "102": {"INBOX", "UNREAD", "Receipts"},
        "201": {"SENT", "Receipts"},
    }


def test_gmails_categories_are_read_over_imap(server):
    as_gmail(server)
    server.categories = {0x101: "updates", 0x201: "personal", 0x301: "promotions"}
    assert cli.main(["backup", "home"]) == 0
    assert labels() == {
        "<a@x>": {"INBOX", "CATEGORY_UPDATES"},
        "<b@x>": {"INBOX", "UNREAD"},  # in none, as Gmail leaves some
        "<c@x>": {"SENT", "CATEGORY_PERSONAL"},
    }
    server.categories[0x101] = "social"  # moved to another tab
    assert cli.main(["backup", "home"]) == 0
    assert labels()["<a@x>"] == {"INBOX", "CATEGORY_SOCIAL"}


def held_categories():
    """The backup as the Gmail API left it: a@x in Updates."""
    folder = backup_folder(ADDRESS)
    con, mcon = gyb.open_backup(folder, ADDRESS), sidecar.open_meta(folder)
    for gid, labels_now in (("101", ["INBOX", "CATEGORY_UPDATES"]), ("102", ["INBOX", "UNREAD"])):
        gyb.replace_labels(con, gid, labels_now)
        sidecar.set_message_labels(mcon, gid, labels_now)
    for c in (con, mcon):
        c.commit()
        c.close()


@pytest.mark.parametrize("fails", ["search", "uidvalidity", "two"])
def test_a_backup_keeps_its_categories_when_gmails_search_cant_give_them(
    server, capsys, monkeypatch, fails
):
    as_gmail(server)
    assert cli.main(["backup", "home"]) == 0
    held_categories()
    server.move("INBOX", 1, "Projects")  # a@x filed: a label change to apply
    if fails == "search":
        server.gmail_finds["category:forums"] = None
    elif fails == "uidvalidity":  # renumbered between the scan and the search
        categorise = imap.ImapAccount._categorise

        def renumbered(self, scan):
            server.folders["[Gmail]/All Mail"]["uidvalidity"] = 2
            return categorise(self, scan)

        monkeypatch.setattr(imap.ImapAccount, "_categorise", renumbered)
    else:
        server.gmail_finds["category:social"] = {0x101}
        server.gmail_finds["category:updates"] = {0x101}
    assert cli.main(["backup", "home"]) == 0
    got = labels()
    assert got["<a@x>"] == {"Projects", "CATEGORY_UPDATES"}  # the search can't say: kept
    assert got["<b@x>"] == {"INBOX", "UNREAD"}
    assert "Gmail's categories couldn't be read" in capsys.readouterr().err
    meta = sqlite3.connect(backup_folder(ADDRESS) / "meta.sqlite")
    held = meta.execute("SELECT label_id FROM message_labels WHERE gmail_id = '101'").fetchall()
    meta.close()
    assert sorted(held) == [("CATEGORY_UPDATES",), ("Projects",)]


def test_another_servers_backup_keeps_its_categories(server):
    assert cli.main(["backup", "home"]) == 0
    folder = backup_folder(ADDRESS)
    con, mcon = gyb.open_backup(folder, ADDRESS), sidecar.open_meta(folder)
    gid = next(g for (g,) in rows("SELECT uid FROM uids"))
    gyb.replace_labels(con, gid, ["INBOX", "CATEGORY_UPDATES"])  # a backup moved from Gmail's
    sidecar.set_message_labels(mcon, gid, ["INBOX", "CATEGORY_UPDATES"])
    for c in (con, mcon):
        c.commit()
        c.close()
    server.add("INBOX", msgid="<d@x>")  # a change: the run applies labels
    assert cli.main(["backup", "home"]) == 0
    mcon = sidecar.open_meta(folder)
    assert "CATEGORY_UPDATES" in sidecar.message_label_ids(mcon, gid)
    mcon.close()


def test_categories_are_kept_where_the_mailbox_cant_say():
    held = {"INBOX", "CATEGORY_UPDATES", "Projects"}
    assert backup_commands.with_categories(["Work"], held, False) == ["CATEGORY_UPDATES", "Work"]
    assert backup_commands.with_categories(["Work"], held, True) == ["Work"]  # the mailbox's say


def test_an_imap_backup_of_gmail_from_before_its_own_ids_is_refused(server):
    assert cli.main(["backup", "home"]) == 0  # header IDs, as before
    set_state(id_kind=None)  # as an older version left it: provider imap only
    as_gmail(server)
    with pytest.raises(SystemExit, match="can only be started again"):
        cli.main(["backup", "home"])
    assert rows("SELECT count(*) FROM uids") == [(3,)]  # nothing added


def test_an_imap_run_doesnt_call_every_message_changed():
    counts = dict(missing=0, todo=0, labels=0, skipped=0)
    imap_line = backup_commands.history_summary(changed=20, present=7, **counts)
    assert imap_line == (
        "Read the server's 7 messages again (excl. junk/trash); 0 to download, 0 label updates"
    )
    gmail_line = backup_commands.history_summary(
        changed=3, present=None, missing=1, todo=2, labels=1, skipped=1
    )
    assert gmail_line == (
        "History since last run: 3 messages changed, 1 missing locally; "
        "2 to download, 1 label updates, 1 in Spam/Trash/Chat skipped"
    )


def test_provider_change_allows_a_first_backup_and_the_same_ids(tmp_path):
    assert backup_commands.provider_change(ADDRESS, tmp_path, "gmail", "imap", 0) == ""
    assert backup_commands.provider_change(ADDRESS, tmp_path, "imap", "imap", 5) == ""
    assert backup_commands.provider_change(ADDRESS, tmp_path, "gmail", "gmail", 5) == ""
    assert "move the backup folder aside" in backup_commands.provider_change(
        ADDRESS, tmp_path, "imap", "gmail", 5
    )


def test_a_folder_over_the_servers_message_limit_is_listed_in_full(server, capsys):
    server.message_limit = 1000  # MESSAGELIMIT (RFC 9738), at a usual value
    server.add_folder("Big")
    for n in range(2500):
        server.add("Big", msgid=f"<big{n}@x>", flags={"\\Seen"})
    assert cli.main(["backup", "home"]) == 0
    assert rows("SELECT count(*) FROM uids") == [(2503,)]  # nothing missed
    searches = [c for c in server.commands if c[:2] == ("UID", "SEARCH")]
    assert len(searches) > len(server.folders)  # Big took three passes
    capsys.readouterr()
    assert cli.main(["backup", "home"]) == 0
    out = capsys.readouterr().out
    assert "Downloaded 0 messages" in out and "deleted" not in out  # nothing reported gone


def test_a_folder_listed_short_keeps_its_mail_and_fails_the_run(server, monkeypatch, capsys):
    for n in range(5):
        server.add("Projects", msgid=f"<p{n}@x>", flags={"\\Seen"})
    assert cli.main(["backup", "home"]) == 0  # backed up whole first
    server.message_limit = 2
    search = server.uid

    def ignoring_ranges(command, *args):  # a server that cuts SEARCH and ignores UID ranges
        return search(command, *((None, "ALL") if command == "SEARCH" else args))

    monkeypatch.setattr(server, "uid", ignoring_ranges)
    capsys.readouterr()
    assert cli.main(["backup", "home"]) == 1
    out, err = capsys.readouterr()
    assert "skipped Projects: listed 2 of 5 messages" in err
    assert "folders couldn't be read and were left as they were in the backup: Projects" in err
    assert "permanently deleted" not in out
    assert rows("SELECT count(*) FROM uids") == [(8,)]  # all still there
    con = sidecar.open_meta(backup_folder(ADDRESS))
    last = con.execute(
        "SELECT status, deleted_in_gmail FROM runs WHERE command = 'backup' "
        "ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    con.close()
    assert last == ("failures", 0)  # an unattended run says so


def test_a_review_keyword_is_backed_up_as_a_label_and_its_clearing_seen(server):
    server.folders["INBOX"]["uids"][1]["flags"].add("$mailcellar-offload-candidate")
    assert cli.main(["backup", "home"]) == 0
    assert labels()["<a@x>"] == {"INBOX", "offload-candidate"}
    server.folders["INBOX"]["uids"][1]["flags"].discard("$mailcellar-offload-candidate")
    assert cli.main(["backup", "home"]) == 0
    assert labels()["<a@x>"] == {"INBOX"}  # cleared in the mail client: kept


def test_a_message_without_a_message_id_is_backed_up(server):
    server.add("INBOX", raw=make_eml(msgid=None, body="no id here"), flags={"\\Seen"})
    assert cli.main(["backup", "home"]) == 0
    assert len(rows("SELECT uid FROM uids")) == 4


def test_a_password_others_can_read_is_refused(server, tmp_path):
    (tmp_path / "config" / "home" / gmailapi.IMAP_PASSWORD).chmod(0o644)
    with pytest.raises(SystemExit, match="can be read by others"):
        cli.main(["backup", "home"])


def test_a_refused_login_is_recorded_for_an_unattended_backup(server):
    assert cli.main(["backup", "home"]) == 0  # recorded in the backup's own sidecar
    server.password = "changed"
    with pytest.raises(SystemExit, match="refused the login"):
        cli.main(["backup", "home", "--unattended"])
    con = sidecar.open_meta(backup_folder(ADDRESS))
    last = con.execute(
        "SELECT status, reason FROM runs WHERE command = 'backup' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    con.close()
    assert last == ("error", "access-refused")


def test_the_uid_map_is_kept_with_the_backup_and_spares_rereading(server):
    assert cli.main(["backup", "home"]) == 0
    assert sidecar.read_imap_map(backup_folder(ADDRESS))  # saved with the backup
    server.commands.clear()
    assert cli.main(["backup", "home"]) == 0
    assert not [c for c in server.commands if "MESSAGE-ID" in str(c)]  # nothing new to read


def test_a_scan_writes_nothing_until_backup_saves_it(server, tmp_path):
    folder = tmp_path / "data"
    box = imap.ImapAccount(
        ADDRESS,
        gmailapi.ImapSettings("imap.example.org"),
        "secret",
        folder=folder,
        connect=lambda: server,
    )
    box.list_message_ids()
    assert not sidecar.meta_path(folder).exists()


def test_an_unattended_backup_that_cant_get_in_is_recorded(server, tmp_path):
    assert cli.main(["backup", "home"]) == 0
    (tmp_path / "config" / "home" / gmailapi.IMAP_PASSWORD).chmod(0o644)
    with pytest.raises(SystemExit, match="can be read by others"):
        cli.main(["backup", "home", "--unattended"])
    con = sidecar.open_meta(backup_folder(ADDRESS))
    last = con.execute(
        "SELECT status, reason FROM runs WHERE command = 'backup' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    con.close()
    assert last == ("error", "no-access")


def test_restore_refuses_a_backup_whose_sidecar_cant_be_read(server):
    assert cli.main(["backup", "home"]) == 0
    sidecar.meta_path(backup_folder(ADDRESS)).write_bytes(b"not a database")
    with pytest.raises(SystemExit, match="can't be read"):
        cli.main(["restore", "home", "--all"])


def test_forget_deleted_forgets_nothing_from_a_server_that_lists_nothing(server, capsys):
    assert cli.main(["backup", "home"]) == 0
    for folder in server.folders.values():
        folder["uids"].clear()  # a server that answers with an empty mailbox
    assert cli.main(["forget", "home", "--deleted", "--yes"]) == 1
    said = capsys.readouterr().err
    assert "the server listed no messages at all; nothing is forgotten" in said
    assert "Gmail" not in said
    assert len(rows("SELECT message_num FROM messages")) == 3  # nothing left the backup
