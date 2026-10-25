"""Parsed details: attachments, recipients, list headers and In-Reply-To."""

import email
import email.policy
import hashlib
import sqlite3
from email.message import EmailMessage

from mailcellar.backup import commands, sidecar
from mailcellar.message import (
    attachment_hashes,
    attachments,
    content_hash,
    list_id,
    list_unsubscribe,
    parse_message,
    recipients,
)
from test_backup import ACCOUNT, FakeGmail, raw_response


def parse(raw: bytes):
    return email.message_from_bytes(raw, policy=email.policy.compat32)


def with_parts(*parts, body="see attached"):
    """parts: (data, maintype, subtype, filename or None for inline)."""
    m = EmailMessage()
    m["From"] = "Alice <alice@example.com>"
    m["Subject"] = "Docs"
    m.set_content(body)
    for data, maintype, subtype, filename in parts:
        if filename:
            m.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
        else:
            m.add_attachment(data, maintype=maintype, subtype=subtype, disposition="inline")
    return m.as_bytes()


# ── attachments ──────────────────────────────────────────────────────────────


def test_attachments_in_order_with_sizes_and_hashes():
    pdf, png = b"%PDF ticket", b"\x89PNG logo"
    found = attachments(
        parse(with_parts((pdf, "application", "pdf", "ticket.pdf"), (png, "image", "png", None)))
    )
    assert found == [
        {
            "idx": 0,
            "filename": "ticket.pdf",
            "mime_type": "application/pdf",
            "size": len(pdf),
            "sha256": hashlib.sha256(pdf).hexdigest(),
            "inline": False,
        },
        {
            "idx": 1,
            "filename": "",
            "mime_type": "image/png",
            "size": len(png),
            "sha256": hashlib.sha256(png).hexdigest(),
            "inline": True,
        },
    ]
    assert attachments(parse(with_parts())) == []


def test_encoded_filename_is_decoded():
    m = EmailMessage()
    m["From"] = "a@x"
    m.set_content("x")
    m.add_attachment(b"data", maintype="application", subtype="pdf", filename="Invoice Zoë.pdf")
    [a] = attachments(parse(m.as_bytes()))
    assert a["filename"] == "Invoice Zoë.pdf"


def test_forwarded_message_parts_are_included():
    inner = parse(with_parts((b"%PDF inner", "application", "pdf", "inner.pdf")))
    outer = EmailMessage()
    outer["From"] = "Bob <bob@example.com>"
    outer.set_content("forwarding")
    outer.add_attachment(inner, filename="forwarded.eml")
    names = [a["filename"] for a in attachments(parse(outer.as_bytes()))]
    assert "inner.pdf" in names


def test_content_hash_is_unchanged_by_reusing_the_attachment_list():
    msg = parse(with_parts((b"%PDF", "application", "pdf", "a.pdf")))
    assert content_hash(msg) == content_hash(msg, attachments(msg))
    assert attachment_hashes(msg) == [hashlib.sha256(b"%PDF").hexdigest()]


# ── recipients ───────────────────────────────────────────────────────────────

RAW_RECIPIENTS = (
    b"From: Alex <alex@x.com>\r\n"
    b"To: =?utf-8?q?Doe=2C_John?= <J@X.com>, ann@y.org,\r\n"
    b' "TAYLOR, Sam (NORTHFIELD HEALTH\r\n TRUST)" <sam@health.example>\r\n'
    b"Cc: undisclosed-recipients:;, ann@y.org, Bea <bea@z.es>\r\n"
    b"Bcc: secret@x.com\r\n"
    b"Subject: s\r\n\r\nbody\r\n"
)


def test_recipients_kinds_names_and_cleaning():
    assert recipients(parse(RAW_RECIPIENTS)) == [
        {"kind": "to", "address": "j@x.com", "name": "Doe, John"},
        {"kind": "to", "address": "ann@y.org", "name": ""},
        {
            "kind": "to",
            "address": "sam@health.example",
            "name": "TAYLOR, Sam (NORTHFIELD HEALTH TRUST)",
        },
        {"kind": "cc", "address": "ann@y.org", "name": ""},  # group syntax skipped
        {"kind": "cc", "address": "bea@z.es", "name": "Bea"},
        {"kind": "bcc", "address": "secret@x.com", "name": ""},
    ]


def test_repeated_address_within_a_kind_is_kept_once():
    raw = b"From: a@x\r\nTo: b@y.com, B <B@Y.com>\r\nTo: b@y.com\r\n\r\nx\r\n"
    assert [r["address"] for r in recipients(parse(raw))] == ["b@y.com"]


# ── list headers and In-Reply-To ─────────────────────────────────────────────


def headers(**h):
    raw = b"From: a@x\r\n" + b"".join(
        f"{k.replace('_', '-')}: {v}\r\n".encode() for k, v in h.items()
    )
    return parse(raw + b"\r\nx\r\n")


def test_list_id():
    assert list_id(headers(List_Id='"News" <News.Example.COM>')) == "news.example.com"
    assert list_id(headers(List_Id="plain.list.id")) == "plain.list.id"
    assert list_id(headers()) == ""


def test_list_unsubscribe_kinds():
    assert list_unsubscribe(headers(List_Unsubscribe="<mailto:u@x.com>")) == "mailto"
    assert list_unsubscribe(headers(List_Unsubscribe="<https://x.com/u?id=1>")) == "https"
    assert list_unsubscribe(headers(List_Unsubscribe="<https://x/u>, <mailto:u@x>")) == "both"
    assert list_unsubscribe(headers()) == ""


def test_parse_message_scalar_details():
    raw = (
        b"From: a@x\r\nList-Id: <n.example.com>\r\nPrecedence: Bulk\r\n"
        b"In-Reply-To: <ABC@Mail.example.com>\r\n\r\nx\r\n"
    )
    f = parse_message(raw)
    assert (f["list_id"], f["precedence"], f["in_reply_to"], f["list_unsubscribe"]) == (
        "n.example.com",
        "bulk",
        "abc@mail.example.com",
        "",
    )


# ── meta.sqlite v3 → v4, child rows ──────────────────────────────────────────


def children(folder):
    con = sqlite3.connect(folder / "meta.sqlite")
    try:
        return (
            sorted(con.execute("SELECT gmail_id, idx, filename, size FROM attachments")),
            sorted(con.execute("SELECT gmail_id, kind, address FROM recipients")),
        )
    finally:
        con.close()


def backed_up(tmp_path, n=1):
    folder = tmp_path / "data" / ACCOUNT
    body = with_parts((b"%PDF ticket", "application", "pdf", "ticket.pdf"))
    gmail = FakeGmail({f"g{i}": raw_response(f"g{i}", body=body) for i in range(n)})
    commands.backup(gmail, ACCOUNT, folder)
    return gmail, folder


def test_download_writes_child_rows(tmp_path):
    _gmail, folder = backed_up(tmp_path)
    atts, recips = children(folder)
    assert atts == [("g0", 0, "ticket.pdf", len(b"%PDF ticket"))]
    assert recips == []  # the fixture has no To


def test_v3_sidecar_migrates_and_reparse_fills_children_once(tmp_path):
    _gmail, folder = backed_up(tmp_path)
    con = sqlite3.connect(folder / "meta.sqlite")
    for col in ("list_id", "list_unsubscribe", "precedence", "in_reply_to"):
        con.execute(f"ALTER TABLE messages DROP COLUMN {col}")
    con.execute("DROP TABLE attachments")
    con.execute("DROP TABLE recipients")
    con.execute("UPDATE messages SET parser_version = '6'")
    con.execute("UPDATE meta SET value = '3' WHERE name = 'schema_version'")
    con.commit()
    con.close()

    assert sidecar.current_rows(folder) == {}
    assert commands.backfill(None, ACCOUNT, folder)["reparsed"] == 1
    assert children(folder)[0] == [("g0", 0, "ticket.pdf", len(b"%PDF ticket"))]
    # a second re-parse replaces the rows rather than adding to them
    con = sqlite3.connect(folder / "meta.sqlite")
    con.execute("UPDATE messages SET parser_version = 'old'")
    con.commit()
    con.close()
    commands.backfill(None, ACCOUNT, folder)
    assert len(children(folder)[0]) == 1


def test_sync_removes_child_rows(tmp_path):
    gmail, folder = backed_up(tmp_path, n=20)
    del gmail.messages["g0"]
    commands.sync(gmail, ACCOUNT, folder, confirm=lambda p: True)
    assert "g0" not in {g for g, *_ in children(folder)[0]}
    assert len(children(folder)[0]) == 19
