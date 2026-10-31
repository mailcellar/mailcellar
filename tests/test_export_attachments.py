"""export --attachments: the attached files themselves, to a zip or a folder."""

import csv
import io
import sqlite3
import zipfile
from email.message import EmailMessage
from pathlib import Path

import pytest

from fake_mailbox import ACCOUNT, FakeMailbox, raw_response
from mailcellar import cli, gmailapi
from mailcellar.backup import commands, sidecar
from mailcellar.message import PARSER_VERSION
from mailcellar.restore import attachments as att
from mailcellar.restore.apply import Candidate

DAY = 86_400_000
T0 = 1_700_006_400_000  # 2023-11-15 00:00 UTC
PDF = b"%PDF-1.4 an invoice"


# ── the pieces ───────────────────────────────────────────────────────────────


def part(**changes):
    base = dict(
        source_id="m1",
        internal_date=T0,
        sender="Alice <alice@example.com>",
        sender_addr="alice@example.com",
        subject="Invoice",
        idx=0,
        filename="invoice.pdf",
        mime_type="application/pdf",
        size=len(PDF),
        sha256="h",
    )
    return att.Part(**(base | changes))


def details(filename="invoice.pdf", mime_type="application/pdf", inline=False):
    return {"filename": filename, "mime_type": mime_type, "inline": inline}


def test_inline_parts_are_left_out_unless_asked_for():
    assert att.wanted(details(), inline=False, types=frozenset())
    assert not att.wanted(details("logo.png", "image/png", True), inline=False, types=frozenset())
    assert att.wanted(details("logo.png", "image/png", True), inline=True, types=frozenset())


@pytest.mark.parametrize("given", ["application/pdf", "pdf", ".PDF", " Pdf "])
def test_a_type_matches_the_mime_type_its_subtype_or_the_extension(given):
    types = att.type_names([given])
    assert att.wanted(details(), inline=False, types=types)
    assert not att.wanted(details("notes.txt", "text/plain"), False, types)
    # a .pdf sent as octet-stream: by its extension, so not by the full MIME type
    by_extension = att.wanted(details("scan.pdf", "application/octet-stream"), False, types)
    assert by_extension == ("/" not in given)


def test_an_empty_type_matches_nothing_extra():
    assert att.type_names(["", " ", "pdf"]) == {"pdf"}


def test_only_messages_with_a_wanted_part_or_an_old_parse_are_opened():
    def cand(gid, version=PARSER_VERSION):
        return Candidate(gid, "", "", Path(f"/b/{gid}.eml"), (), T0, "", version)

    rows = {"pdf": [details()], "logo": [details("logo.png", "image/png", True)]}
    cands = [cand("pdf"), cand("logo"), cand("none"), cand("old", "6"), cand("unparsed", "")]
    opened = att.to_open(cands, rows, inline=False, types=frozenset())
    assert [c.source_id for c in opened] == ["pdf", "old", "unparsed"]


def test_a_name_is_the_date_the_sender_and_the_filename():
    assert att.attachment_name(part()) == "2023-11-15 alice@example.com invoice.pdf"


def test_a_name_loses_what_some_filesystem_forbids():
    name = att.attachment_name(part(filename=' a/b\\c:d*e?f"g<h>i|j\x01k. '))
    assert name == "2023-11-15 alice@example.com a_b_c_d_e_f_g_h_i_j_k"


def test_a_part_with_no_filename_is_named_from_its_type():
    assert att.attachment_name(part(filename="", idx=3)).endswith(" attachment-3.pdf")
    unknown = part(filename="", idx=0, mime_type="application/x-unheard-of")
    assert att.attachment_name(unknown).endswith(" attachment-0.bin")


def test_a_long_name_is_cut_to_the_limit_with_its_extension_kept():
    name = att.attachment_name(part(filename="é" * 200 + ".pdf"))
    assert len(name.encode()) <= att.NAME_BYTES
    assert name.endswith("é.pdf")
    assert name.encode().decode() == name  # no character cut in half


def test_a_clash_gets_a_number_ignoring_case_and_normalisation():
    taken = {att.same_name("Report.pdf"), att.same_name("report (2).pdf")}
    assert att.place("REPORT.pdf", taken) == "REPORT (3).pdf"
    assert att.place("other.pdf", taken) == "other.pdf"
    assert att.place("café.txt", {att.same_name("café.txt")}) == ("café (2).txt")


def test_a_name_with_no_extension_is_numbered_and_cut_at_its_end():
    name = att.attachment_name(part(filename="README", sender_addr="bob@example.org"))
    assert att.place(name, {att.same_name(name)}) == "2023-11-15 bob@example.org README (2)"
    long = att.attachment_name(part(filename="x" * 200, sender_addr="bob@example.org"))
    assert long.startswith("2023-11-15 bob@example.org xxx") and len(long.encode()) == 150


def test_an_index_row_names_the_message_and_the_file():
    row = att.index_row(part(subject="=1+1", filename="-x.pdf"), "the file.pdf")
    assert row == {
        "date": "2023-11-15T00:00:00+00:00",
        "sender": "Alice <alice@example.com>",
        "subject": "'=1+1",
        "id": "m1",
        "filename": "'-x.pdf",
        "type": "application/pdf",
        "size": str(len(PDF)),
        "file": "the file.pdf",
    }


# ── the command ──────────────────────────────────────────────────────────────


def mail(sender, subject, *parts):
    """A message with the given (content, maintype, subtype, filename, inline) parts."""
    m = EmailMessage()
    m["From"] = sender
    m["To"] = "bob@example.com"
    m["Subject"] = subject
    m.set_content("See attached.")
    for content, maintype, subtype, filename, inline in parts:
        m.add_attachment(
            content,
            maintype=maintype,
            subtype=subtype,
            filename=filename,
            disposition="inline" if inline else "attachment",
        )
    return m.as_bytes()


@pytest.fixture
def backup(tmp_path, monkeypatch):
    """Four messages: a PDF twice (with a logo inline), a CSV and a PDF, and none."""
    monkeypatch.chdir(tmp_path)
    logo = (b"\x89PNG logo", "image", "png", "logo.png", True)
    invoice = (PDF, "application", "pdf", "invoice.pdf", False)
    report = (b"a,b\n", "text", "csv", "Report.csv", False)
    other = (b"%PDF other", "application", "pdf", "invoice.pdf", False)
    specs = {
        "a1": (T0, mail("Acc <acc@example.com>", "Invoice", invoice, logo)),
        "a2": (T0 + DAY, mail("Acc <acc@example.com>", "Again", invoice)),
        "a3": (T0 + 2 * DAY, mail("Bob <bob@example.org>", "=SUM(1)", report, other)),
        "a4": (T0 + 3 * DAY, mail("Carol <carol@example.org>", "Plain")),
    }
    messages = {gid: raw_response(gid, ms=ms, body=body) for gid, (ms, body) in specs.items()}
    folder = tmp_path / "data" / ACCOUNT
    commands.backup(FakeMailbox(messages), ACCOUNT, folder)
    monkeypatch.setattr(gmailapi, "load_credentials", lambda *a, **k: 1 / 0)
    return folder


def export_cmd(*args):
    return cli.main(["export", ACCOUNT, "--all", "--attachments", *args])


def index_of(text: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(text)))


def zip_contents(path):
    with zipfile.ZipFile(path) as zf:
        files = {n: zf.read(n) for n in zf.namelist() if n != "index.csv"}
        return files, index_of(zf.read("index.csv").decode())


def folder_contents(path):
    files = {f.name: f.read_bytes() for f in path.iterdir() if f.name != "index.csv"}
    return files, index_of((path / "index.csv").read_text(encoding="utf-8"))


INVOICE = "2023-11-15 acc@example.com invoice.pdf"
REPORT = "2023-11-17 bob@example.org Report.csv"
OTHER = "2023-11-17 bob@example.org invoice.pdf"


def test_a_zip_holds_each_attachment_once_and_an_index_of_every_message(backup, tmp_path, capsys):
    assert export_cmd("--to", str(tmp_path / "out.zip")) == 0
    files, index = zip_contents(tmp_path / "out.zip")
    assert files == {INVOICE: PDF, REPORT: b"a,b\n", OTHER: b"%PDF other"}
    assert [(r["id"], r["filename"], r["file"]) for r in index] == [
        ("a1", "invoice.pdf", INVOICE),
        ("a2", "invoice.pdf", INVOICE),  # the same content, written once
        ("a3", "Report.csv", REPORT),
        ("a3", "invoice.pdf", OTHER),
    ]
    assert index[2]["subject"] == "'=SUM(1)"
    assert index[0]["type"] == "application/pdf" and index[0]["size"] == str(len(PDF))
    out = capsys.readouterr().out
    assert "Exported 4 attachments from 3 messages" in out
    assert "1 had the same content as one before them" in out


def test_a_folder_holds_the_same_files_and_index_as_a_zip(backup, tmp_path):
    assert export_cmd("--to", str(tmp_path / "out.zip")) == 0
    assert export_cmd("--to", f"{tmp_path}/out/") == 0
    assert folder_contents(tmp_path / "out") == zip_contents(tmp_path / "out.zip")
    assert not (tmp_path / "out.part").exists()


def test_inline_parts_come_only_with_inline(backup, tmp_path):
    assert export_cmd("--inline", "--to", str(tmp_path / "out.zip")) == 0
    files, _index = zip_contents(tmp_path / "out.zip")
    assert files["2023-11-15 acc@example.com logo.png"] == b"\x89PNG logo"


def test_type_narrows_the_attachments_and_can_be_repeated(backup, tmp_path):
    assert export_cmd("--type", "csv", "--type", "pdf", "--to", str(tmp_path / "out.zip")) == 0
    files, _index = zip_contents(tmp_path / "out.zip")
    assert set(files) == {INVOICE, OTHER, REPORT}
    assert export_cmd("--type", "pdf", "--to", str(tmp_path / "pdf.zip")) == 0
    files, _index = zip_contents(tmp_path / "pdf.zip")
    assert set(files) == {INVOICE, OTHER}


def test_a_message_whose_rows_are_from_an_older_parser_is_read_itself(backup, tmp_path):
    con = sqlite3.connect(sidecar.meta_path(backup))
    con.execute("DELETE FROM attachments WHERE gmail_id = 'a3'")
    con.execute("UPDATE messages SET parser_version = '6' WHERE gmail_id = 'a3'")
    con.commit()
    con.close()
    assert export_cmd("--type", "csv", "--to", str(tmp_path / "out.zip")) == 0
    files, _index = zip_contents(tmp_path / "out.zip")
    assert set(files) == {REPORT}


def test_nothing_matching_writes_nothing(backup, tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        export_cmd("--type", "docx", "--to", str(tmp_path / "out.zip"))
    assert "none of the 4 messages has a matching attachment" in str(exc.value)
    assert list(tmp_path.glob("out*")) == []


@pytest.mark.parametrize(
    "args, message",
    [
        (["--to", "out.mbox"], "--attachments writes a zip or a folder"),
        (["--type", "pdf", "--to", "out.zip"], "--inline and --type go with --attachments"),
        (["--inline", "--to", "out.zip"], "--inline and --type go with --attachments"),
    ],
)
def test_attachment_options_are_checked(backup, capsys, args, message):
    if "--type" in args or "--inline" in args:
        run = ["export", ACCOUNT, "--all", *args]  # without --attachments
    else:
        run = ["export", ACCOUNT, "--all", "--attachments", *args]
    with pytest.raises(SystemExit) as exc:
        cli.main(run)
    assert exc.value.code == 2
    assert message in capsys.readouterr().err


@pytest.mark.parametrize("name", ["out.zip", "out/"])
def test_a_failed_export_leaves_nothing_behind(backup, tmp_path, monkeypatch, name):
    real, calls = att.read_parts, []

    def fail_midway(*args):
        calls.append(1)
        if len(calls) == 2:
            raise OSError("disk full")
        return real(*args)

    monkeypatch.setattr(att, "read_parts", fail_midway)
    with pytest.raises(OSError):
        export_cmd("--to", f"{tmp_path}/{name}")
    assert list(tmp_path.glob("out*")) == []


def offloaded_and_gone(folder, *ids):
    """Mail offloaded from Gmail whose .eml has since gone from the backup."""
    con = sidecar.open_meta(folder)
    sidecar.record_offloaded(con, list(ids), "2026-01-15-093000", "old work", 1_000)
    con.commit()
    con.close()
    for gid in ids:
        next(folder.rglob(f"{gid}.eml")).unlink()


def test_a_message_whose_file_is_missing_is_left_out_and_reported(backup, tmp_path, capsys):
    offloaded_and_gone(backup, "a3")
    assert export_cmd("--to", str(tmp_path / "out.zip")) == 1
    files, index = zip_contents(tmp_path / "out.zip")
    assert set(files) == {INVOICE}
    assert {r["id"] for r in index} == {"a1", "a2"}
    err = capsys.readouterr().err
    assert "1 messages have no .eml file" in err and "a3" in err


def test_missing_files_are_reported_when_nothing_else_matches(backup, tmp_path, capsys):
    offloaded_and_gone(backup, "a3")  # the only CSV
    con = sqlite3.connect(sidecar.meta_path(backup))
    con.execute("UPDATE messages SET parser_version = '6' WHERE gmail_id = 'a4'")  # read, no CSV
    con.commit()
    con.close()
    with pytest.raises(SystemExit) as exc:
        export_cmd("--type", "csv", "--to", str(tmp_path / "out.zip"))
    assert "none of the 4 messages has a matching attachment" in str(exc.value)
    assert "1 messages have no .eml file" in capsys.readouterr().err


def test_when_no_file_is_there_it_says_so(backup, tmp_path):
    offloaded_and_gone(backup, "a1", "a2", "a3", "a4")
    with pytest.raises(SystemExit) as exc:
        export_cmd("--to", str(tmp_path / "out.zip"))
    assert "none of the 3 messages with a matching attachment has its file" in str(exc.value)
    assert list(tmp_path.glob("out*")) == []
