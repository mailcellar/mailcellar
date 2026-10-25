import sqlite3

from mailcellar.dedup import cli as dedup
from mailcellar.dedup.match import reused_message_ids
from test_index import FakeBackup, make_eml, run_index

A, B = "a@example.com", "b@example.com"
DATE = [("Date", "Mon, 20 Jan 2014 14:15:32 +0100")]


def run_match(tmp_path, *args):
    db = tmp_path / "dedup.sqlite"
    dedup.main(["match", "--db", str(db), *args])
    return sqlite3.connect(db)


def groups(con):
    """{key: (scope, method, sorted [(account, gmail_id)])}"""
    out = {}
    for gid, scope, method, key in con.execute("SELECT * FROM match_groups"):
        members = sorted(
            con.execute("SELECT account, gmail_id FROM match_members WHERE group_id = ?", (gid,))
        )
        out[key] = (scope, method, members)
    return out


def two_backups(tmp_path):
    return FakeBackup(tmp_path, A), FakeBackup(tmp_path, B)


def test_same_message_id_and_content_across_accounts(tmp_path):
    a, b = two_backups(tmp_path)
    a.add("a1", make_eml(msgid="<m1@x>"))
    b.add("b1", make_eml(msgid="<m1@x>"))
    run_index(tmp_path, a, b)

    assert groups(run_match(tmp_path)) == {
        "m1@x": ("cross", "message-id", [(A, "a1"), (B, "b1")]),
    }


def test_same_message_id_different_content_is_not_grouped(tmp_path):
    a, b = two_backups(tmp_path)
    a.add("a1", make_eml(msgid="<m1@x>", body="original"))
    b.add("b1", make_eml(msgid="<m1@x>", body="original\n-- \nfooter added by a list"))
    run_index(tmp_path, a, b)

    con = run_match(tmp_path)
    assert groups(con) == {}
    assert reused_message_ids(con) == [("m1@x", 2, 2)]


def test_messages_without_content_are_never_grouped(tmp_path):
    a, b = two_backups(tmp_path)
    a.add("a1", make_eml(msgid="<m1@x>", body=""))
    b.add("b1", make_eml(msgid="<m1@x>", body=""))
    run_index(tmp_path, a, b)
    assert groups(run_match(tmp_path)) == {}


def test_repeats_within_one_account_form_a_within_group(tmp_path):
    a, _b = two_backups(tmp_path)
    a.add("a1", make_eml(msgid="<m1@x>"))
    a.add("a2", make_eml(msgid="<m1@x>"))
    run_index(tmp_path, a)
    assert groups(run_match(tmp_path)) == {
        "m1@x": ("within", "message-id", [(A, "a1"), (A, "a2")]),
    }


def test_header_key_matches_resends_with_new_message_ids(tmp_path):
    a, _b = two_backups(tmp_path)
    a.add("a1", make_eml(msgid="<first@x>", body="Order shipped", headers=DATE))
    a.add("a2", make_eml(msgid="<second@x>", body="Order shipped", headers=DATE))
    run_index(tmp_path, a)
    [(scope, method, members)] = groups(run_match(tmp_path)).values()
    assert (scope, method, members) == ("within", "header-key", [(A, "a1"), (A, "a2")])


def test_same_headers_different_content_is_not_grouped(tmp_path):
    a, _b = two_backups(tmp_path)
    a.add("a1", make_eml(msgid="<first@x>", body="Amount £58.20", headers=DATE))
    a.add("a2", make_eml(msgid="<second@x>", body="Amount £58.21", headers=DATE))
    run_index(tmp_path, a)
    assert groups(run_match(tmp_path)) == {}


def test_identical_text_at_different_times_is_not_grouped(tmp_path):
    a, _b = two_backups(tmp_path)
    a.add("a1", make_eml(msgid="<first@x>", body="Purchase Confirmation", headers=DATE))
    a.add(
        "a2",
        make_eml(
            msgid="<second@x>",
            body="Purchase Confirmation",
            headers=[("Date", "Tue, 21 Jan 2014 09:00:00 +0100")],
        ),
    )
    run_index(tmp_path, a)
    assert groups(run_match(tmp_path)) == {}


def test_links_are_transitive(tmp_path):
    # a1~b1 share a Message-ID; b1~b2 share the header key; all one group
    a, b = two_backups(tmp_path)
    a.add("a1", make_eml(msgid="<m1@x>", headers=DATE))
    b.add("b1", make_eml(msgid="<m1@x>", headers=DATE))
    b.add("b2", make_eml(msgid="<resent@x>", headers=DATE))
    run_index(tmp_path, a, b)
    assert groups(run_match(tmp_path)) == {
        "m1@x": ("cross", "message-id", [(A, "a1"), (B, "b1"), (B, "b2")]),
    }


def test_rerun_rebuilds_groups(tmp_path):
    a, b = two_backups(tmp_path)
    a.add("a1", make_eml(msgid="<m1@x>"))
    b.add("b1", make_eml(msgid="<m1@x>"))
    run_index(tmp_path, a, b)
    run_match(tmp_path)

    b.remove("b1")
    run_index(tmp_path, a, b)
    assert groups(run_match(tmp_path)) == {}


def test_csv_lists_every_copy(tmp_path):
    a, b = two_backups(tmp_path)
    a.add("a1", make_eml(msgid="<m1@x>"))
    b.add("b1", make_eml(msgid="<m1@x>"))
    run_index(tmp_path, a, b)
    csv_path = tmp_path / "matches.csv"
    run_match(tmp_path, "--csv", str(csv_path))

    lines = csv_path.read_text().splitlines()
    assert lines[0].startswith("group_id,scope,method,key,account")
    assert len(lines) == 3


def test_report_counts_scopes_and_reused_ids(tmp_path, capsys):
    a, b = two_backups(tmp_path)
    a.add("a1", make_eml(msgid="<m1@x>"))
    b.add("b1", make_eml(msgid="<m1@x>"))
    a.add("a2", make_eml(msgid="<m2@x>", body="one"))
    a.add("a3", make_eml(msgid="<m2@x>", body="two"))
    a.add("a4", make_eml(msgid="<m3@x>"))
    a.add("a5", make_eml(msgid="<m3@x>"))
    run_index(tmp_path, a, b)
    capsys.readouterr()
    run_match(tmp_path)
    out = " ".join(capsys.readouterr().out.split())
    assert "cross by message-id 1" in out
    assert "within by message-id 1" in out
    assert "reused for different content (not grouped): 1" in out


def with_attachment(data: bytes, msgid="<m1@x>") -> bytes:
    from email.message import EmailMessage

    m = EmailMessage()
    m["From"] = "Alice <alice@example.com>"
    m["Subject"] = "Your tickets"
    m["Message-ID"] = msgid
    m.set_content("see attached")
    m.add_attachment(data, maintype="application", subtype="pdf", filename="ticket.pdf")
    return m.as_bytes()


def test_attachments_count_as_content(tmp_path):
    # same text, different attachment bytes (e.g. tickets for two passengers)
    a, _b = two_backups(tmp_path)
    a.add("a1", with_attachment(b"%PDF passenger one"))
    a.add("a2", with_attachment(b"%PDF passenger two"))
    a.add("a3", with_attachment(b"%PDF passenger one"))
    run_index(tmp_path, a)
    assert groups(run_match(tmp_path)) == {
        "m1@x": ("within", "message-id", [(A, "a1"), (A, "a3")]),
    }
