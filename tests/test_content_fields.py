"""content_hash / header_key (message.py) and the schema migrations that store them."""

import email
import email.policy
import sqlite3
from email.message import EmailMessage

import pytest

from mailcellar.backup import commands, sidecar
from mailcellar.dedup.db import SCHEMA_VERSION, open_db
from mailcellar.message import PARSER_VERSION, content_hash, header_key, parse_message
from test_backup import ACCOUNT, FakeGmail, raw_response
from test_index import FakeBackup, make_eml, run_index

HEADERS = [("Date", "Thu, 18 Sep 2025 10:00:00 +0000")]


def html_mail(
    html,
    attachments=(),
    msgid="<m@x>",
    subject="We got your dispute request",
    sender="Paybank <no-reply@paybank.example>",
    headers=HEADERS,
):
    m = EmailMessage()
    m["From"] = sender
    m["Subject"] = subject
    m["Message-ID"] = msgid
    for k, v in headers:
        m[k] = v
    m.set_content(html, subtype="html")
    for name, data in attachments:
        m.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    return email.message_from_bytes(m.as_bytes(), policy=email.policy.compat32)


# ── content_hash ─────────────────────────────────────────────────────────────


def test_resend_differing_only_in_tracking_links_matches():
    a = html_mail(
        '<p>Amount £58.20</p><a href="https://click.paybank.example/q/AAA">Open</a>'
        '<img src="https://click.paybank.example/o/111"> https://track.example/1'
    )
    b = html_mail(
        '<p>Amount £58.20</p><a href="https://click.paybank.example/q/BBB">Open</a>'
        '<img src="https://click.paybank.example/o/222"> https://track.example/2'
    )
    assert content_hash(a) == content_hash(b) != ""


def test_addresses_and_markup_are_ignored():
    # (tags become spaces, so "Alex</b>," would read as "Alex ," — keep punctuation clear)
    a = html_mail("<p>Hello <b>Alex</b> please write to unsubscribe+abc@mail.example.com</p>")
    b = html_mail("<div>Hello Alex please write to   unsubscribe+xyz@mail.example.com</div>")
    assert content_hash(a) == content_hash(b)


@pytest.mark.parametrize(
    "other",
    [
        "<p>Amount £58.21</p>",  # Paybank: a different dispute
        "<p>Amount £5.82</p>",
    ],
)
def test_different_visible_text_differs(other):
    assert content_hash(html_mail("<p>Amount £58.20</p>")) != content_hash(html_mail(other))


def test_different_attachments_differ():
    # An airline: same text, tickets for two different passengers
    a = html_mail("<p>Booking confirmation</p>", [("ticket - A.pdf", b"%PDF passenger A")])
    b = html_mail("<p>Booking confirmation</p>", [("ticket - B.pdf", b"%PDF passenger B")])
    c = html_mail("<p>Booking confirmation</p>", [("renamed.pdf", b"%PDF passenger A")])
    assert content_hash(a) != content_hash(b)
    assert content_hash(a) == content_hash(c)  # attachment bytes, not names


def test_empty_message_has_no_content_hash():
    m = EmailMessage()
    m["From"] = "a@x"
    assert content_hash(email.message_from_bytes(m.as_bytes(), policy=email.policy.compat32)) == ""


# ── header_key ───────────────────────────────────────────────────────────────


def test_header_key_normalises_case_and_whitespace():
    folded = (
        b"From: Paybank <no-reply@paybank.example>\r\n"
        b"Date: Thu, 18 Sep 2025 10:00:00 +0000\r\n"
        b"Subject: We  got your\r\n dispute request\r\n\r\nbody\r\n"
    )
    a = email.message_from_bytes(folded, policy=email.policy.compat32)
    b = html_mail("x", subject="WE GOT YOUR DISPUTE REQUEST")
    assert header_key(a) == header_key(b) != ""


def test_header_key_ignores_message_id_but_not_date():
    a = html_mail("x", msgid="<one@x>")
    b = html_mail("x", msgid="<two@x>")
    c = html_mail("x", headers=[("Date", "Thu, 18 Sep 2025 10:00:01 +0000")])
    assert header_key(a) == header_key(b)
    assert header_key(a) != header_key(c)


@pytest.mark.parametrize("sender, headers", [("", HEADERS), ("a@x", [])])
def test_header_key_needs_from_and_date(sender, headers):
    m = EmailMessage()
    if sender:
        m["From"] = sender
    for k, v in headers:
        m[k] = v
    m["Subject"] = "s"
    msg = email.message_from_bytes(m.as_bytes(), policy=email.policy.compat32)
    assert header_key(msg) == ""


def test_parse_message_returns_both():
    fields = parse_message(make_eml(headers=HEADERS))
    assert fields["content_hash"] and fields["header_key"].startswith("alice <alice@example.com>")


# ── meta.sqlite v1 → v2 ──────────────────────────────────────────────────────


def downgrade_meta(folder):
    """Turn a fresh meta.sqlite into schema v1: no new columns, old parser."""
    con = sqlite3.connect(folder / "meta.sqlite")
    con.execute("ALTER TABLE messages DROP COLUMN content_hash")
    con.execute("ALTER TABLE messages DROP COLUMN header_key")
    con.execute("UPDATE messages SET parser_version = '3'")
    con.execute("UPDATE meta SET value = '1' WHERE name = 'schema_version'")
    con.commit()
    con.close()


def test_v1_sidecar_is_ignored_until_migrated_then_reparsed(tmp_path, capsys):
    folder = tmp_path / "data" / ACCOUNT
    gmail = FakeGmail({"g1": raw_response("g1")})
    commands.backup(gmail, ACCOUNT, folder)
    downgrade_meta(folder)

    assert sidecar.current_fields(folder) == {}  # old schema: index parses instead
    st = commands.backfill(None, ACCOUNT, folder)  # migrates, then re-parses stale rows
    assert st["reparsed"] == 1
    fields = sidecar.current_fields(folder)["g1"]
    assert fields["content_hash"]
    assert fields["header_key"] == ""  # the fixture message has no Date
    con = sqlite3.connect(folder / "meta.sqlite")
    assert con.execute("SELECT value FROM meta WHERE name='schema_version'").fetchone() == (
        sidecar.SCHEMA_VERSION,
    )


def test_new_download_writes_both_fields(tmp_path):
    folder = tmp_path / "data" / ACCOUNT
    commands.backup(FakeGmail({"g1": raw_response("g1")}), ACCOUNT, folder)
    con = sqlite3.connect(folder / "meta.sqlite")
    row = con.execute("SELECT content_hash, header_key, parser_version FROM messages").fetchone()
    assert row[0] and row[2] == PARSER_VERSION


# ── dedup.sqlite v3 → v4 ─────────────────────────────────────────────────────


def test_v3_index_gains_columns_and_is_refilled(tmp_path, capsys):
    b = FakeBackup(tmp_path, "a@example.com")
    b.add("g1", make_eml(headers=HEADERS))
    con = run_index(tmp_path, b)
    con.execute("ALTER TABLE messages DROP COLUMN content_hash")
    con.execute("ALTER TABLE messages DROP COLUMN header_key")
    con.execute("UPDATE meta SET value = '3' WHERE name = 'schema_version'")
    con.execute("UPDATE meta SET value = '3' WHERE name = 'parser_version'")
    con.commit()
    con.close()

    con = run_index(tmp_path, b)
    assert con.execute("SELECT value FROM meta WHERE name = 'schema_version'").fetchone() == (
        SCHEMA_VERSION,
    )
    ch, hk = con.execute("SELECT content_hash, header_key FROM messages").fetchone()
    assert ch and hk

    # the migration can be repeated: the columns are already there
    con.execute("UPDATE meta SET value = '3' WHERE name = 'schema_version'")
    con.commit()
    con.close()
    open_db(tmp_path / "dedup.sqlite").close()
