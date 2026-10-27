"""An IMAP account through the commands: backup, count, meta, verify, and what refuses it."""

import sqlite3

import pytest

from fake_imap import FakeImap
from mailcellar import cli, gmailapi, imap
from mailcellar.backup import commands as backup_commands
from mailcellar.backup import sidecar
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
    got = labels()
    assert got["<a@x>"] == {"Projects"} and got["<b@x>"] == {"INBOX"}
    assert got["<d@x>"] == {"INBOX"} and "<c@x>" in got  # still in the backup

    assert cli.main(["count", "home"]) == 0
    out = capsys.readouterr().out
    assert "On the server (excl. junk/trash)" in out and "Gmail" not in out
    assert cli.main(["verify", "home", "--chain"]) == 0
    assert cli.main(["meta", "home"]) == 0
    assert all(c[0] in ("LOGIN", "LIST", "EXAMINE", "UID", "LOGOUT") for c in server.commands)
    # Each command logged out when done: backup twice, count, meta.
    assert [c[0] for c in server.commands].count("LOGOUT") == 4


def test_a_backup_of_another_provider_is_refused(server, capsys):
    assert cli.main(["backup", "home"]) == 0
    con = sidecar.open_meta(backup_folder(ADDRESS))
    sidecar.set_state(con, "provider", "gmail")  # as if the folder held a Gmail backup
    con.commit()
    con.close()
    with pytest.raises(SystemExit, match="holds a gmail backup .* now set up as imap"):
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


def test_provider_change_allows_a_first_backup_and_the_same_provider(tmp_path):
    assert backup_commands.provider_change(ADDRESS, tmp_path, "gmail", "imap", 0) == ""
    assert backup_commands.provider_change(ADDRESS, tmp_path, "imap", "imap", 5) == ""
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


def test_restore_refuses_an_imap_backup(server):
    assert cli.main(["backup", "home"]) == 0
    with pytest.raises(SystemExit, match="backup of an IMAP account"):
        cli.main(["restore", "home", "--all"])


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


def test_restore_refuses_imap_mail_from_any_source(server, tmp_path):
    assert cli.main(["backup", "home"]) == 0
    (tmp_path / "config" / "work").mkdir()
    (tmp_path / "config" / "accounts.toml").write_text(
        f'home = "{ADDRESS}"\nwork = "sam@example.org"\n'
    )
    for args in (
        ["home", "--from-pruned", "2026-01-01-000000"],
        ["work", "--from-folder", str(backup_folder(ADDRESS))],  # ACCOUNT: restored into
    ):
        with pytest.raises(SystemExit, match="backup of an IMAP account"):
            cli.main(["restore", *args])


def test_restore_refuses_a_backup_whose_sidecar_cant_be_read(server):
    assert cli.main(["backup", "home"]) == 0
    sidecar.meta_path(backup_folder(ADDRESS)).write_bytes(b"not a database")
    with pytest.raises(SystemExit, match="can't be read"):
        cli.main(["restore", "home", "--all"])
