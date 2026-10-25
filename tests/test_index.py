import email
import email.policy
import sqlite3
from email.message import EmailMessage
from pathlib import Path

import pytest

from mailcellar.dedup import cli as dedup
from mailcellar.dedup.db import SCHEMA_VERSION
from mailcellar.message import (
    body_hash,
    decode_header_value,
    normalise_message_id,
    strip_mbox_from_lines,
)

# ── fixtures ─────────────────────────────────────────────────────────────────

GYB_SCHEMA = """
CREATE TABLE settings (name TEXT PRIMARY KEY, value TEXT);
CREATE TABLE messages(message_num INTEGER PRIMARY KEY,
                      message_filename TEXT,
                      message_internaldate TIMESTAMP);
CREATE TABLE labels (message_num INTEGER, label TEXT);
CREATE TABLE uids (message_num INTEGER, uid TEXT PRIMARY KEY);
"""


def make_eml(
    msgid="<abc@example.com>",
    body="Hello there",
    subject="Hi",
    sender="Alice <alice@example.com>",
    headers=(),
) -> bytes:
    """headers: extra (name, value) pairs, e.g. X-Forwarded-For."""
    m = EmailMessage()
    m["From"] = sender
    m["To"] = "bob@example.com"
    m["Subject"] = subject
    if msgid:
        m["Message-ID"] = msgid
    for name, value in headers:
        m[name] = value
    m.set_content(body)
    return m.as_bytes()


class FakeBackup:
    """A minimal GYB backup folder: msg-db.sqlite plus .eml files."""

    def __init__(self, root: Path, account: str):
        self.dir = root / "data" / account
        self.dir.mkdir(parents=True)
        self.db = self.dir / "msg-db.sqlite"
        con = sqlite3.connect(self.db)
        con.executescript(GYB_SCHEMA)
        con.execute("INSERT INTO settings VALUES ('email_address', ?)", (account,))
        con.commit()
        con.close()
        self.num = 0

    def add(self, gmail_id: str, raw: bytes, labels=()):
        self.num += 1
        rel = f"2026/1/1/{gmail_id}.eml"
        path = self.dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO messages VALUES (?, ?, '2026-01-01 00:00:00')", (self.num, rel))
        con.execute("INSERT INTO uids VALUES (?, ?)", (self.num, gmail_id))
        con.executemany("INSERT INTO labels VALUES (?, ?)", ((self.num, label) for label in labels))
        con.commit()
        con.close()

    def remove(self, gmail_id: str):
        con = sqlite3.connect(self.db)
        con.execute(
            """DELETE FROM messages WHERE message_num =
                       (SELECT message_num FROM uids WHERE uid = ?)""",
            (gmail_id,),
        )
        con.execute("DELETE FROM uids WHERE uid = ?", (gmail_id,))
        con.commit()
        con.close()

    def set_labels(self, gmail_id: str, labels):
        con = sqlite3.connect(self.db)
        (num,) = con.execute("SELECT message_num FROM uids WHERE uid = ?", (gmail_id,)).fetchone()
        con.execute("DELETE FROM labels WHERE message_num = ?", (num,))
        con.executemany("INSERT INTO labels VALUES (?, ?)", ((num, label) for label in labels))
        con.commit()
        con.close()


def run_index(tmp_path, *backups):
    db = tmp_path / "dedup.sqlite"
    dedup.main(["index", "--db", str(db), *(str(b.dir) for b in backups)])
    return sqlite3.connect(db)


def parse(raw: bytes):
    return email.message_from_bytes(raw, policy=email.policy.compat32)


# ── helpers ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("<ABC.123@Example.COM>", "abc.123@example.com"),
        ("  <abc@example.com> (comment)", "abc@example.com"),
        ("<abc@\n example.com>", "abc@example.com"),
        ("abc@example.com", "abc@example.com"),
        ("", ""),
    ],
)
def test_normalise_message_id(raw, expected):
    assert normalise_message_id(raw) == expected


def test_decode_header_adjacent_encoded_words_have_no_extra_space():
    raw = "=?utf-8?q?Caf=C3=A9?= =?utf-8?q?_au_lait?="
    assert decode_header_value(raw) == "Café au lait"


def test_body_hash_ignores_whitespace_and_transfer_encoding():
    plain = EmailMessage()
    plain.set_content("Hello   there\nfriend", cte="7bit")
    b64 = EmailMessage()
    b64.set_content("Hello there friend", cte="base64")
    assert body_hash(parse(plain.as_bytes())) == body_hash(parse(b64.as_bytes()))


def test_body_hash_differs_for_different_text():
    assert body_hash(parse(make_eml(body="one"))) != body_hash(parse(make_eml(body="two")))


def test_body_hash_excludes_attachments():
    a = EmailMessage()
    a.set_content("Same body")
    b = EmailMessage()
    b.set_content("Same body")
    b.add_attachment("attached text", filename="note.txt")
    assert body_hash(parse(a.as_bytes())) == body_hash(parse(b.as_bytes()))


def test_body_hash_empty_without_text():
    m = EmailMessage()
    m.add_attachment(b"\x00\x01", maintype="application", subtype="octet-stream", filename="x.bin")
    assert body_hash(parse(m.as_bytes())) == ""


@pytest.mark.parametrize(
    "prefix, middle",
    [
        (b">From someone@example.com Mon Jun 16 19:17:19 2003\r\n", b""),
        (b"From someone@example.com Mon Jun 16 19:17:19 2003\n", b""),
        (b"X-Mozilla-Status: 0001\r\n", b">From someone@example.com Wed Dec 01 00:24:09 2004\r\n"),
    ],
)
def test_mbox_from_lines_in_headers_are_dropped(prefix, middle):
    eml = make_eml(body=">From the body, which must stay\n")
    raw = prefix + middle + eml
    msg = parse(strip_mbox_from_lines(raw))
    assert normalise_message_id(msg.get("Message-ID")) == "abc@example.com"
    assert ">From the body" in msg.get_payload()


def test_strip_mbox_from_lines_leaves_normal_messages_untouched():
    raw = make_eml()
    assert strip_mbox_from_lines(raw) is raw


# ── index ────────────────────────────────────────────────────────────────────


def test_index_records_fields_and_labels(tmp_path):
    b = FakeBackup(tmp_path, "a@example.com")
    b.add("g1", make_eml(), labels=["INBOX", "work"])
    b.add("g2", make_eml(msgid=None, body="no id"))

    con = run_index(tmp_path, b)
    rows = dict(
        (
            (gid, (mid, addr, subj, bool(bh)))
            for gid, mid, addr, subj, bh in con.execute(
                "SELECT gmail_id, message_id, sender_addr, subject, body_hash FROM messages"
            )
        )
    )
    assert rows == {
        "g1": ("abc@example.com", "alice@example.com", "Hi", True),
        "g2": ("", "alice@example.com", "Hi", True),
    }
    labels = sorted(con.execute("SELECT gmail_id, label FROM labels"))
    assert labels == [("g1", "INBOX"), ("g1", "work")]


def test_index_is_incremental_and_tracks_removals(tmp_path, capsys):
    b = FakeBackup(tmp_path, "a@example.com")
    b.add("g1", make_eml(), labels=["INBOX"])
    b.add("g2", make_eml(msgid="<other@example.com>"))
    run_index(tmp_path, b)

    b.remove("g2")
    b.add("g3", make_eml(msgid="<new@example.com>"))
    b.set_labels("g1", ["INBOX", "starred-ish"])
    capsys.readouterr()
    con = run_index(tmp_path, b)

    out = capsys.readouterr().out
    # total, parsed, unchanged, removed
    assert "a@example.com" in out
    assert [int(x.replace(",", "")) for x in out.split("a@example.com")[1].split()[:4]] == [
        2,
        1,
        1,
        1,
    ]
    assert sorted(g for (g,) in con.execute("SELECT gmail_id FROM messages")) == ["g1", "g3"]
    assert sorted(
        label for (label,) in con.execute("SELECT label FROM labels WHERE gmail_id = 'g1'")
    ) == [
        "INBOX",
        "starred-ish",
    ]


@pytest.mark.parametrize(
    "headers, expected",
    [
        ([], ""),
        ([("X-Forwarded-For", "A@Example.com b@example.com")], "a@example.com"),
        (
            [
                ("X-Forwarded-For", "a@example.com b@example.com"),
                ("X-Forwarded-For", "c@example.com a@example.com"),
            ],
            "a@example.com c@example.com",
        ),
    ],
)
def test_index_records_forwarding_origin(tmp_path, headers, expected):
    b = FakeBackup(tmp_path, "b@example.com")
    b.add("g1", make_eml(headers=headers))
    con = run_index(tmp_path, b)
    assert con.execute("SELECT forwarded_from FROM messages").fetchone() == (expected,)


def test_schema_v1_index_is_migrated_and_reparsed(tmp_path):
    db = tmp_path / "dedup.sqlite"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE meta (name TEXT PRIMARY KEY, value TEXT);
        INSERT INTO meta VALUES ('schema_version', '1'), ('parser_version', '2');
        CREATE TABLE messages (
            account TEXT NOT NULL, gmail_id TEXT NOT NULL, path TEXT NOT NULL,
            size INTEGER NOT NULL, internal_date TEXT, message_id TEXT NOT NULL,
            sender TEXT NOT NULL, sender_addr TEXT NOT NULL, subject TEXT NOT NULL,
            body_hash TEXT NOT NULL, PRIMARY KEY (account, gmail_id));
    """)
    con.close()

    b = FakeBackup(tmp_path, "b@example.com")
    b.add("g1", make_eml(headers=[("X-Forwarded-For", "a@example.com b@example.com")]))
    con = run_index(tmp_path, b)
    assert con.execute("SELECT value FROM meta WHERE name = 'schema_version'").fetchone() == (
        SCHEMA_VERSION,
    )
    assert con.execute("SELECT forwarded_from FROM messages").fetchone() == ("a@example.com",)


def index_counts(out, account):
    """(total, parsed, unchanged, removed) from index's report."""
    return [int(x.replace(",", "")) for x in out.split(account)[-1].split()[:4]]


def test_paths_are_relative_so_moving_the_backup_needs_no_reparse(tmp_path, capsys):
    b = FakeBackup(tmp_path, "a@example.com")
    b.add("g1", make_eml())
    con = run_index(tmp_path, b)
    assert con.execute("SELECT path FROM messages").fetchone() == ("2026/1/1/g1.eml",)
    assert con.execute("SELECT * FROM backups").fetchall() == [
        ("a@example.com", str(b.dir.resolve()))
    ]

    moved = tmp_path / "elsewhere" / "a@example.com"
    moved.parent.mkdir()
    b.dir.rename(moved)
    b.dir = moved
    capsys.readouterr()
    con = run_index(tmp_path, b)

    assert index_counts(capsys.readouterr().out, "a@example.com") == [1, 0, 1, 0]
    assert con.execute("SELECT folder FROM backups").fetchone() == (str(moved.resolve()),)


def test_schema_v2_absolute_paths_are_migrated_without_reparse(tmp_path, capsys):
    b = FakeBackup(tmp_path, "a@example.com")
    b.add("g1", make_eml())
    b.add("g2", make_eml(msgid="<other@x>"))
    con = run_index(tmp_path, b)
    folder = str(b.dir.resolve())
    # turn it back into a v2 index: absolute paths, no backups table rows
    con.execute("UPDATE messages SET path = ? || '/' || path", (folder,))
    con.execute("DELETE FROM backups")
    con.execute("UPDATE meta SET value = '2' WHERE name = 'schema_version'")
    con.commit()
    con.close()
    capsys.readouterr()

    con = run_index(tmp_path, b)
    assert index_counts(capsys.readouterr().out, "a@example.com") == [2, 0, 2, 0]
    assert sorted(p for (p,) in con.execute("SELECT path FROM messages")) == [
        "2026/1/1/g1.eml",
        "2026/1/1/g2.eml",
    ]
    assert con.execute("SELECT folder FROM backups").fetchone() == (folder,)
    assert con.execute("SELECT value FROM meta WHERE name = 'schema_version'").fetchone() == (
        SCHEMA_VERSION,
    )


def test_parser_version_change_forces_reparse(tmp_path, capsys):
    b = FakeBackup(tmp_path, "a@example.com")
    b.add("g1", make_eml())
    con = run_index(tmp_path, b)
    con.execute("UPDATE meta SET value = 'old' WHERE name = 'parser_version'")
    con.commit()
    capsys.readouterr()
    run_index(tmp_path, b)
    out = capsys.readouterr().out
    # total, parsed, unchanged
    assert [int(x) for x in out.split("a@example.com")[1].split()[:3]] == [1, 1, 0]


def test_default_folders_skip_non_backups(tmp_path, monkeypatch, capsys):
    b = FakeBackup(tmp_path, "a@example.com")
    b.add("g1", make_eml())
    (tmp_path / "data" / "_pruned" / "a@example.com" / "2026-10-22-121708").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    dedup.main(["index", "--db", str(tmp_path / "dedup.sqlite")])
    assert "a@example.com" in capsys.readouterr().out


def test_default_outputs_go_to_data_dedup(tmp_path, monkeypatch):
    a = FakeBackup(tmp_path, "a@example.com")
    b = FakeBackup(tmp_path, "b@example.com")
    a.add("a1", make_eml())
    b.add("b1", make_eml())
    monkeypatch.chdir(tmp_path)

    dedup.main(["index"])
    dedup.main(["plan", "--keep", "a@"])
    out = tmp_path / "data" / "dedup"
    assert sorted(p.name for p in out.iterdir()) == ["dedup-plan.csv", "dedup.sqlite"]

    # data/dedup/ is not mistaken for a backup on the next run
    dedup.main(["index"])


def test_index_keeps_accounts_separate(tmp_path):
    a = FakeBackup(tmp_path, "a@example.com")
    b = FakeBackup(tmp_path, "b@example.com")
    a.add("same-id", make_eml())
    b.add("same-id", make_eml())

    con = run_index(tmp_path, a, b)
    assert sorted(con.execute("SELECT account, message_id FROM messages")) == [
        ("a@example.com", "abc@example.com"),
        ("b@example.com", "abc@example.com"),
    ]
