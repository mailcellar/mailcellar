import csv
import sqlite3
from email.message import EmailMessage

import pytest

from mailcellar.backup import commands, sidecar
from mailcellar.message import display_name, extract_address, parse_message
from mailcellar.report import senders
from test_backup import ACCOUNT, MS, FakeGmail, raw_response

# ── address fields in message.py ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Alice <ALICE@Example.com>", "alice@example.com"),
        ("=?utf-8?q?Doe=2C_John?= <j@x.com>", "j@x.com"),  # comma only after decoding
        ("sam taylor sam.taylor10@health.example <>", "sam.taylor10@health.example"),  # malformed
        ("undisclosed-recipients", "undisclosed-recipients"),  # no address at all
        ("", ""),
        # folded inside the quoted name
        (
            '"=?iso-8859-1?B?Wm/r?=\r\n\t" <zoe.ng@accounts.example>',
            "zoe.ng@accounts.example",
        ),
        (
            '"TAYLOR, Sam (NORTHFIELD HEALTH\r\n TRUST)" <sam.taylor10@health.example>',
            "sam.taylor10@health.example",
        ),
    ],
)
def test_extract_address(raw, expected):
    assert extract_address(raw) == expected


def test_display_name():
    assert display_name('"=?iso-8859-1?B?Wm/r?=\r\n\t" <z@x.example>') == "Zoë"
    assert (
        display_name('"TAYLOR, Sam (NORTHFIELD HEALTH\r\n TRUST)" <s@health.example>')
        == "TAYLOR, Sam (NORTHFIELD HEALTH TRUST)"
    )
    # decoded after parsing, so an encoded comma doesn't split the name
    assert display_name("=?utf-8?q?Doe=2C_John?= <j@x.com>") == "Doe, John"
    assert display_name("j@x.com") == ""


ENCODED_COMMA_NAME = (
    b"From: =?utf-8?q?Doe=2C_John?= <j@x.com>\r\nSubject: s\r\nMessage-ID: <doe@x>\r\n\r\nthree\r\n"
)


def mail(sender="Paybank <no-reply@paybank.example>", reply_to=None, body="hi"):
    # (EmailMessage decodes encoded words itself, so encoded headers are
    # written as raw bytes instead: see ENCODED_COMMA_NAME)
    m = EmailMessage()
    m["From"] = sender
    m["Subject"] = "s"
    m["Message-ID"] = f"<{abs(hash((sender, reply_to, body)))}@x>"
    if reply_to:
        m["Reply-To"] = reply_to
    m.set_content(body)
    return m.as_bytes()


def test_parse_message_sender_fields():
    f = parse_message(mail(reply_to="Support <Help@Paybank.example>"))
    assert (f["sender_addr"], f["sender_name"], f["reply_to"]) == (
        "no-reply@paybank.example",
        "Paybank",
        "help@paybank.example",
    )
    assert parse_message(mail())["reply_to"] == ""


# ── meta.sqlite v2 → v3 ──────────────────────────────────────────────────────


def test_meta_v2_gains_sender_columns_and_rows_go_stale(tmp_path):
    folder = tmp_path / "data" / ACCOUNT
    commands.backup(FakeGmail({"g1": raw_response("g1")}), ACCOUNT, folder)
    con = sqlite3.connect(folder / "meta.sqlite")
    con.execute("ALTER TABLE messages DROP COLUMN sender_name")
    con.execute("ALTER TABLE messages DROP COLUMN reply_to")
    con.execute("UPDATE messages SET parser_version = '4'")
    con.execute("UPDATE meta SET value = '2' WHERE name = 'schema_version'")
    con.commit()
    con.close()

    assert sidecar.current_rows(folder) == {}  # old schema: not used
    assert commands.backfill(None, ACCOUNT, folder)["reparsed"] == 1
    fields, date = sidecar.current_rows(folder)["g1"]
    assert fields["sender_name"] == "Alice" and date == MS


# ── the report ───────────────────────────────────────────────────────────────


def backup(tmp_path):
    folder = tmp_path / "data" / ACCOUNT
    gmail = FakeGmail(
        {
            "g1": raw_response("g1", body=mail(reply_to="help@paybank.example", body="one")),
            "g2": raw_response("g2", body=mail(body="two")),
            "g3": raw_response("g3", ms=1_000_000_000_000, body=ENCODED_COMMA_NAME),
            "g4": raw_response("g4", body=mail(sender="", body="four")),
        }
    )
    commands.backup(gmail, ACCOUNT, folder)
    return folder


def run_report(tmp_path, folder, capsys):
    out_csv = tmp_path / "senders.csv"
    senders.main(
        [
            "--folder",
            str(folder),
            "--no-reply-to",
            "--output",
            str(tmp_path / "report.txt"),
            "--csv",
            str(out_csv),
        ]
    )
    with out_csv.open(newline="", encoding="utf-8") as fh:
        rows = {r["address"]: r for r in csv.DictReader(fh)}
    return rows, capsys.readouterr().err


def test_report_from_the_sidecar(tmp_path, capsys):
    folder = backup(tmp_path)
    rows, err = run_report(tmp_path, folder, capsys)
    assert "4 messages: 4 from meta.sqlite, 0 parsed, 0 skipped" in err
    header = (tmp_path / "senders.csv").read_text().splitlines()[0]
    assert header == (
        "address,display_name,domain,count,pct_total,total_size_bytes,has_reply_to,active_years"
    )
    assert set(rows) == {"no-reply@paybank.example", "j@x.com", "unknown"}
    paybank = rows["no-reply@paybank.example"]
    assert (
        paybank["count"],
        paybank["display_name"],
        paybank["has_reply_to"],
        paybank["active_years"],
    ) == ("2", "Paybank", "yes", "2026")
    assert rows["j@x.com"]["display_name"] == "Doe, John"
    assert rows["j@x.com"]["active_years"] == "2001"  # year Gmail received it
    assert int(paybank["total_size_bytes"]) == sum(
        (folder / f"2026/10/22/{g}.eml").stat().st_size for g in ("g1", "g2")
    )
    assert (
        "no-reply@paybank.example"
        not in (tmp_path / "report.txt")
        .read_text()
        .split("Senders with no Reply-To")[1]
        .split("Sender Activity")[0]
    )


def test_same_report_without_the_sidecar(tmp_path, capsys):
    folder = backup(tmp_path)
    with_meta, _ = run_report(tmp_path, folder, capsys)
    (folder / "meta.sqlite").unlink()
    without, err = run_report(tmp_path, folder, capsys)
    assert "4 messages: 0 from meta.sqlite, 4 parsed, 0 skipped" in err
    assert with_meta == without


def test_not_a_backup(tmp_path):
    with pytest.raises(SystemExit, match="not a backup"):
        senders.main(["--folder", str(tmp_path)])
