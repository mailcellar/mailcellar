import csv
from email.message import EmailMessage

import pytest

from mailcellar.backup import commands
from mailcellar.report import cli, storage
from mailcellar.report.db import AnalyticsError, completeness, open_sidecars
from mailcellar.report.output import Table, cell, render
from test_backup import FakeGmail, raw_response

A, B = "a@example.com", "b@example.com"
OLD = 1_300_000_000_000  # 2011-03-13
NEW = 1_792_711_800_000  # 2026-10-22
PDF = b"%PDF " + b"x" * 3000


def mail(sender, subject, attach=None):
    m = EmailMessage()
    m["From"] = sender
    m["Subject"] = subject
    m["Message-ID"] = f"<{subject}@x>"
    m.set_content(f"about {subject}")
    if attach:
        m.add_attachment(attach, maintype="application", subtype="pdf", filename="report.pdf")
    return m.as_bytes()


def message(gid, sender, subject, ms, size, labels=("INBOX",), attach=None):
    r = raw_response(gid, ms=ms, label_ids=list(labels), body=mail(sender, subject, attach))
    r["sizeEstimate"] = size
    return r


@pytest.fixture
def two_accounts(tmp_path, monkeypatch):
    """Two backups in tmp_path/data with known sizes, labels and attachments."""
    monkeypatch.setattr(
        FakeGmail, "LABELS", {**FakeGmail.LABELS, "STARRED": "STARRED", "SENT": "SENT"}
    )
    a = {
        "a1": message("a1", "Big <big@corp.com>", "old-big", OLD, 9_000_000, attach=PDF),
        "a2": message("a2", "Big <big@corp.com>", "new-big", NEW, 7_000_000, attach=PDF),
        "a3": message("a3", "small@corp.com", "small", OLD, 1_000),
        "a4": message("a4", "me@a.com", "sent-big", OLD, 8_000_000, labels=["SENT"]),
        "a5": message("a5", "x@y.org", "starred-big", OLD, 6_000_000, labels=["STARRED"]),
        "a6": message("a6", "x@y.org", "work-big", OLD, 6_500_000, labels=["Label_1"]),
    }
    b = {"b1": message("b1", "other@z.net", "b-copy", NEW, 2_000_000, attach=PDF)}
    for account, msgs in ((A, a), (B, b)):
        commands.backup(FakeGmail(msgs), account, tmp_path / "data" / account)
    monkeypatch.chdir(tmp_path)
    con, accounts = open_sidecars(cli.resolve_folders([]))
    yield con, accounts
    con.close()


def section(tables, name) -> Table:
    return next(t for t in tables if t.name == name)


def build(con, **opts):
    return storage.build(con, storage.Options(**opts))


def test_totals(two_accounts):
    con, accounts = two_accounts
    t = section(build(con), "totals")
    rows = {r[0]: r for r in t.rows}
    assert accounts == [A, B]
    assert rows[A][1:4] == (6, 9_000_000 + 7_000_000 + 1_000 + 8_000_000 + 6_000_000 + 6_500_000, 2)
    assert rows[A][4] == 2 * len(PDF)
    assert rows["all accounts"][1] == 7


def test_by_year_and_sender_and_domain(two_accounts):
    con, _ = two_accounts
    tables = build(con)
    years = {(r[0], r[1]): r[2:4] for r in section(tables, "by-year").rows}
    assert years[("2011", A)] == (5, 9_000_000 + 1_000 + 8_000_000 + 6_000_000 + 6_500_000)
    assert years[("2026", B)] == (1, 2_000_000)
    top_sender = section(tables, "by-sender").rows[0]
    assert top_sender[:5] == ("big@corp.com", A, 2, 16_000_000, 9_000_000)
    domains = {(r[0], r[1]): r[2:] for r in section(tables, "by-domain").rows}
    assert domains[("corp.com", A)] == (3, 16_001_000, 2)


def test_largest_messages_and_labels(two_accounts):
    con, _ = two_accounts
    tables = build(con)
    largest = section(tables, "largest-messages").rows
    assert [r[3] for r in largest[:3]] == ["old-big", "sent-big", "new-big"]
    assert largest[0][5] == 1  # one attachment
    labels = {(r[0], r[2]): r[3:] for r in section(tables, "by-label").rows}
    assert labels[("INBOX", A)] == (3, 9_000_000 + 7_000_000 + 1_000)
    assert labels[("work", A)] == (1, 6_500_000)


def test_attachment_sections_and_duplicates(two_accounts):
    con, _ = two_accounts
    tables = build(con)
    [mime] = section(tables, "attachment-types").rows
    assert mime == ("application/pdf", 3, 3 * len(PDF), 1)
    assert section(tables, "attachment-extensions").rows == [("pdf", 3, 3 * len(PDF))]
    [dup] = section(tables, "duplicate-attachments").rows
    assert dup[:6] == ("report.pdf", "application/pdf", len(PDF), 3, 2, 2 * len(PDF))


def test_candidates(two_accounts):
    con, _ = two_accounts
    before = cli.parse_before("2020-01-01")
    t = section(build(con, before_ms=before), "candidates")
    # old and large; sent, starred and user-labelled excluded; new-big too recent
    assert [r[3] for r in t.rows] == ["old-big"]
    assert any(note.startswith(f"{A}: 1 messages") for note in t.notes)

    # options: labelled mail included; no date limit, by size only
    t = section(build(con, include_labelled=True, before_ms=before), "candidates")
    assert [r[3] for r in t.rows] == ["old-big", "sent-big", "work-big", "starred-big"]
    t = section(build(con, min_size=1_000_000), "candidates")
    assert [r[3] for r in t.rows] == ["old-big", "new-big", "b-copy"]


@pytest.mark.parametrize(
    "name, ext",
    [
        ("Report.PDF", "pdf"),
        ("archive.tar.gz", "gz"),
        ("README", "(none)"),
        (".bashrc", "(none)"),
        ("", "(none)"),
        (None, "(none)"),
    ],
)
def test_extension(name, ext):
    assert storage.extension(name) == ext


def test_parse_before():
    assert cli.parse_before("2020-01-01") == 1_577_836_800_000
    assert cli.parse_before(None) is None
    with pytest.raises(AnalyticsError):
        cli.parse_before("last tuesday")


def test_resolve_folders(two_accounts):
    assert [f.name for f in cli.resolve_folders(["b@"])] == [B]
    with pytest.raises(AnalyticsError, match="matches 2"):
        cli.resolve_folders(["example"])


def test_completeness_warns_about_stale_rows(two_accounts, tmp_path):
    import sqlite3

    con, accounts = two_accounts
    assert completeness(con, accounts) == []
    side = sqlite3.connect(tmp_path / "data" / A / "meta.sqlite")
    side.execute("UPDATE messages SET parser_version = 'old' WHERE gmail_id = 'a1'")
    side.commit()
    side.close()
    [warning] = completeness(con, accounts)
    assert warning.startswith(f"{A}: 1 messages parsed by an older parser")


def test_render_and_cells():
    t = Table(
        "t",
        "Title",
        [("Name", "text"), ("N", "int"), ("Size", "size"), ("Share", "pct")],
        [("x" * 60, 1234, 2048, 0.5), ("b", 1, 1, 0.0)],
        notes=["a note"],
    )
    import io

    out = io.StringIO()
    render([t], top=1, out=out)
    text = out.getvalue()
    assert "a note" in text and "1,234" in text and "2.0 KB" in text and "50.0%" in text
    assert "… 1 more" in text
    assert cell("x" * 60, "text").endswith("…")


def test_cli_text_and_csv(two_accounts, tmp_path, capsys):
    cli.main(["storage", "--top", "3", "--before", "2020-01-01", "--csv", "reports"])
    out = capsys.readouterr().out
    assert out.startswith(f"Storage report — {A}, {B}")
    assert "Removal candidates" in out
    files = sorted(p.name for p in (tmp_path / "reports").iterdir())
    assert "storage-candidates.csv" in files and len(files) == 11
    with (tmp_path / "reports" / "storage-largest-messages.csv").open() as fh:
        assert len(list(csv.reader(fh))) == 1 + 7  # header + every message


def test_cli_rejects_unmigrated_sidecar(tmp_path, monkeypatch):
    import sqlite3

    folder = tmp_path / "data" / A
    commands.backup(FakeGmail({"a1": message("a1", "x@y", "s", NEW, 10)}), A, folder)
    side = sqlite3.connect(folder / "meta.sqlite")
    side.execute("UPDATE meta SET value = '3' WHERE name = 'schema_version'")
    side.commit()
    side.close()
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="run mailcellar meta"):
        cli.main(["storage"])
