"""export: copying a selection of the backup out as an mbox file or a zip."""

import csv
import email.header
import io
import mailbox
import re
import zipfile
from pathlib import Path

import pytest

from fake_mailbox import ACCOUNT, FakeMailbox, raw_response
from mailcellar import cli, gmailapi
from mailcellar.backup import commands, gyb, sidecar
from mailcellar.restore import apply as restore
from mailcellar.restore import export
from test_index import make_eml

DAY = 86_400_000
T0 = 1_700_006_400_000  # 2023-11-15 00:00 UTC
BATCH = "2026-01-15-093000"

BODY_WITH_FROM_LINES = "Dear all,\nFrom the desk of the manager,\n>From a quote\nBye\n"


# ── the pieces ───────────────────────────────────────────────────────────────


def candidate(**changes):
    base = restore.Candidate(
        "m1",
        "",
        "",
        Path("/b/2023/11/15/m1.eml"),
        ("work", "INBOX"),
        T0,
        "h",
        "7",
        sender="a@x.com",
    )
    from dataclasses import replace

    return replace(base, **changes)


@pytest.mark.parametrize(
    ("name", "expected"),
    [("a.mbox", "mbox"), ("A.MBOX", "mbox"), ("a.zip", "zip"), ("/x/y/mail.Zip", "zip")],
)
def test_the_format_comes_from_the_file_name(name, expected):
    assert export.export_format(Path(name)) == expected


@pytest.mark.parametrize("name", ["mail", "mail.eml", "mail.tar", "mail.mbox.bak", ".mbox"])
def test_other_names_are_refused(name):
    with pytest.raises(ValueError, match=r"name the file \*\.mbox"):
        export.export_format(Path(name))


def test_inside_means_within_the_folder(tmp_path):
    folder = tmp_path / "data" / "a@x.com"
    folder.mkdir(parents=True)
    assert export.inside(folder / "out.mbox", folder)
    assert export.inside(folder / "sub" / "dir" / "out.zip", folder)
    assert not export.inside(tmp_path / "out.mbox", folder)
    assert not export.inside(tmp_path / "data" / "a@x.com.mbox", folder)  # a sibling name
    assert not export.inside(folder.parent / "other" / "out.mbox", folder)


def test_the_separator_line_has_the_messages_date_in_utc():
    assert export.from_line(1792711800000) == b"From MAILER-DAEMON Thu Oct 22 23:30:00 2026\n"
    assert export.from_line(0) == b"From MAILER-DAEMON Thu Jan  1 00:00:00 1970\n"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"From a\n", b">From a\n"),
        (b"x\nFrom a\ny\n", b"x\n>From a\ny\n"),
        (b">From a\n", b">>From a\n"),  # mboxrd: reversible
        (b">>From a\n", b">>>From a\n"),
        (b"x\r\nFrom a\r\n", b"x\r\n>From a\r\n"),
        (b"a From b\n", b"a From b\n"),  # only at the start of a line
        (b"Fromage\n", b"Fromage\n"),  # needs the space
        (b"from a\n", b"from a\n"),  # and the capital
        (b"", b""),
    ],
)
def test_lines_starting_with_from_are_escaped(raw, expected):
    assert export.escape_from(raw) == expected


def test_the_line_ending_is_the_messages_own():
    assert export.newline_of(b"Subject: x\r\n\r\nbody") == b"\r\n"
    assert export.newline_of(b"Subject: x\n\nbody") == b"\n"
    assert export.newline_of(b"") == b"\n"


def test_labels_header_lists_the_labels_like_takeout():
    assert export.labels_header((), b"\n") == b""
    assert export.labels_header(("INBOX",), b"\n") == b"X-Gmail-Labels: INBOX\n"
    assert export.labels_header(("work", "INBOX"), b"\r\n") == b"X-Gmail-Labels: work,INBOX\r\n"


def test_system_labels_go_by_takeouts_names_after_the_users_own():
    labels = (
        "INBOX",
        "YELLOW_STAR",
        "IMPORTANT",
        "CATEGORY_UPDATES",
        "shopping",
        "DRAFT",
        "W/2024",
    )
    assert export.takeout_names(labels) == (
        "shopping",
        "W/2024",
        "Inbox",
        "YELLOW_STAR",  # a superstar: a system label, after the user's
        "Important",
        "Category Updates",
        "Drafts",
    )
    assert export.takeout_names(("CATEGORY_x1",)) == ("CATEGORY_x1",)  # not a category ID


def test_restore_reads_takeouts_names_back_as_the_label_ids():
    from mailcellar.restore import mbox

    labels = ("INBOX", "SENT", "STARRED", "IMPORTANT", "UNREAD", "DRAFT", "CHAT", "work")
    labels += tuple(
        f"CATEGORY_{c}" for c in ("PERSONAL", "SOCIAL", "PROMOTIONS", "UPDATES", "FORUMS")
    )
    assert set(mbox.label_names(export.takeout_names(labels))) == set(labels)


def test_labels_header_cannot_be_broken_by_a_label_name():
    header = export.labels_header(("a\r\nBcc: x@example.com", "b\x00c"), b"\n")
    assert header == b"X-Gmail-Labels: a  Bcc: x@example.com,b c\n"  # one line, still one header


def test_labels_header_encodes_non_ascii_names():
    header = export.labels_header(("projets/été", "work"), b"\n")
    assert header.isascii() and header.endswith(b"\n") and header.count(b"\n") == 1
    value = header.split(b": ", 1)[1].decode()
    decoded = str(email.header.make_header(email.header.decode_header(value)))
    assert decoded == "projets/été,work"
    folded = export.labels_header(("é" * 80, "x"), b"\r\n")
    assert b"\r\n" in folded and b"\n" not in folded.replace(
        b"\r\n", b""
    )  # folds with its own ending


def test_an_mbox_entry_is_separator_labels_message_and_a_blank_line():
    raw = b"Subject: hi\r\nFrom: a@x.com\r\n\r\nFrom the desk\r\nbye\r\n"
    entry = export.mbox_entry(raw, 1792711800000, ("work",))
    assert entry == (
        b"From MAILER-DAEMON Thu Oct 22 23:30:00 2026\n"
        b"X-Gmail-Labels: work\r\n"
        b"Subject: hi\r\nFrom: a@x.com\r\n\r\n>From the desk\r\nbye\r\n"
        b"\n"
    )


def test_an_mbox_entry_without_labels_has_no_header_and_ends_its_last_line():
    entry = export.mbox_entry(b"Subject: hi\n\nbye", T0, ())
    assert entry == b"From MAILER-DAEMON " + export.from_line(T0)[19:] + b"Subject: hi\n\nbye\n\n"
    assert b"X-Gmail-Labels" not in entry


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("=SUM(A1)", "'=SUM(A1)"),
        ("+1", "'+1"),
        ("-1", "'-1"),
        ("@cmd", "'@cmd"),
        ("\tx", "'\tx"),
        ("plain", "plain"),
        ("a=b", "a=b"),
        ("", ""),
    ],
)
def test_cells_that_could_be_formulas_are_defused(text, expected):
    assert export.safe_cell(text) == expected


def test_an_index_row_has_the_date_sender_subject_labels_id_and_file():
    row = export.index_row(candidate(), "Alice <a@x.com>", "=HYPERLINK(1)", "2023/11/15/m1.eml")
    assert row == {
        "date": "2023-11-15T00:00:00+00:00",
        "sender": "Alice <a@x.com>",
        "subject": "'=HYPERLINK(1)",
        "labels": "work;Inbox",
        "id": "m1",
        "file": "2023/11/15/m1.eml",
    }
    assert list(row) == export.INDEX_COLUMNS


def test_a_zip_path_is_the_path_in_the_backup():
    assert export.arcname(candidate(), Path("/b")) == "2023/11/15/m1.eml"
    assert export.arcname(candidate(), Path("/elsewhere")) == "m1.eml"


# ── the command ──────────────────────────────────────────────────────────────


@pytest.fixture
def backup(tmp_path, monkeypatch):
    """Five backed-up messages with their own dates, senders and labels."""
    monkeypatch.chdir(tmp_path)
    specs = {
        "m1": (
            T0,
            "Alice <alice@example.com>",
            "Hello",
            ["INBOX", "Label_1"],
            BODY_WITH_FROM_LINES,
        ),
        "m2": (T0 + 10 * DAY, "Bob <bob@example.org>", "=SUM(1,2)", ["Label_1"], "Second"),
        "m3": (T0 + 20 * DAY, "Bob <bob@example.org>", "Third", ["INBOX"], "Third"),
        "m4": (T0 + 30 * DAY, "Carol <carol@mail.example.org>", "Fourth", ["Label_1"], "Fourth"),
        "m5": (T0 + 40 * DAY, "Alice <alice@example.com>", "Fifth", ["INBOX"], "Fifth"),
    }
    messages = {
        gid: raw_response(
            gid,
            ms=ms,
            label_ids=labels,
            body=make_eml(msgid=f"<{gid}@x>", body=body, subject=subject, sender=sender),
        )
        for gid, (ms, sender, subject, labels, body) in specs.items()
    }
    folder = tmp_path / "data" / ACCOUNT
    commands.backup(FakeMailbox(messages), ACCOUNT, folder)
    con = sidecar.open_meta(folder)
    sidecar.record_offloaded(con, ["m2", "m3", "m4"], BATCH, "old work", 1_000)
    con.commit()
    con.close()
    # nothing in an export may need Gmail
    monkeypatch.setattr(gmailapi, "load_credentials", lambda *a, **k: 1 / 0)
    return folder


def export_cmd(*args):
    return cli.main(["export", ACCOUNT, *args])


def messages_of(path):
    box = mailbox.mbox(path)
    try:
        return [(m["Subject"], m["X-Gmail-Labels"], m.get_payload()) for m in box]
    finally:
        box.close()


def test_an_mbox_export_can_be_read_back_by_a_mail_reader(backup, tmp_path, capsys):
    dest = tmp_path / "out.mbox"
    assert export_cmd("--all", "--to", str(dest)) == 0
    got = messages_of(dest)
    assert [subject for subject, _labels, _body in got] == [
        "Hello",
        "=SUM(1,2)",
        "Third",
        "Fourth",
        "Fifth",
    ]
    assert [labels for _subject, labels, _body in got] == [
        "work,Inbox",
        "work",
        "Inbox",
        "work",
        "Inbox",
    ]
    assert "Exported 5 messages" in capsys.readouterr().out
    assert not Path(str(dest) + ".part").exists()


def test_lines_starting_with_from_survive_the_mbox_round_trip(backup, tmp_path):
    dest = tmp_path / "out.mbox"
    assert export_cmd("--all", "--id", "m1", "--to", str(dest)) == 0
    (_subject, _labels, body) = messages_of(dest)[0]
    assert len(messages_of(dest)) == 1  # a reader doesn't split the message at its From line
    # the file has them escaped (mboxrd) ...
    assert b"\n>From the desk of the manager," in dest.read_bytes()
    assert b"\n>>From a quote" in dest.read_bytes()
    # ... and undoing that, one > off each such line, gives the body back
    original = body.replace("\r\n", "\n")
    assert re.sub(r"(?m)^>(>*From )", r"\1", original) == BODY_WITH_FROM_LINES


def test_a_zip_export_holds_the_eml_files_and_an_index(backup, tmp_path):
    dest = tmp_path / "out.zip"
    assert export_cmd("--all", "--to", str(dest)) == 0
    with zipfile.ZipFile(dest) as zf:
        names = zf.namelist()
        assert sorted(n for n in names if n != "index.csv") == sorted(
            p.relative_to(backup).as_posix() for p in backup.rglob("*.eml")
        )
        for name in names:
            if name != "index.csv":
                assert zf.read(name) == (backup / name).read_bytes()  # byte for byte
        index = list(csv.DictReader(io.StringIO(zf.read("index.csv").decode())))
        assert zf.testzip() is None
    assert [r["id"] for r in index] == ["m1", "m2", "m3", "m4", "m5"]
    first = index[0]
    assert (first["sender"], first["subject"], first["labels"]) == (
        "Alice <alice@example.com>",
        "Hello",
        "work;Inbox",
    )
    assert first["date"] == "2023-11-15T00:00:00+00:00"
    assert first["file"] in names
    assert index[1]["subject"] == "'=SUM(1,2)"  # a spreadsheet won't run it


def test_the_selection_and_narrowing_are_restores(backup, tmp_path):
    def ids(*args):
        dest = tmp_path / f"out-{len(list(tmp_path.glob('out-*')))}.zip"
        assert export_cmd(*args, "--to", str(dest)) == 0
        with zipfile.ZipFile(dest) as zf:
            return [r["id"] for r in csv.DictReader(io.StringIO(zf.read("index.csv").decode()))]

    assert ids("--offloaded") == ["m2", "m3", "m4"]
    assert ids("--batch", BATCH) == ["m2", "m3", "m4"]
    assert ids("--rule", "old work", "--sender", "@example.org") == ["m2", "m3"]
    assert ids("--all", "--sender", "alice@example.com") == ["m1", "m5"]
    assert ids("--all", "--after", "2023-12-05", "--before", "2023-12-25") == ["m3", "m4"]
    assert ids("--all", "--has-label", "WORK") == ["m1", "m2", "m4"]
    assert ids("--all", "--id", "m3", "--id", "m5") == ["m3", "m5"]
    assert ids("--id", "m1", "--id", "m3") == ["m1", "m3"]  # m1 was never offloaded
    assert ids("--offloaded", "--id", "m1", "--id", "m3") == ["m3"]
    assert ids("--offloaded", "--limit", "2") == ["m2", "m3"]  # the oldest first


def test_mail_already_restored_is_exported_once(backup, tmp_path):
    con = sidecar.open_meta(backup)
    sidecar.record_restored(con, "m2", "m2", "untrashed", "run", 5)  # back, under its own ID
    sidecar.record_restored(con, "m3", "n3", "imported", "run", 5)  # back, under a new ID
    con.commit()
    con.close()
    assert restore.candidates(backup) and "m2" not in [
        c.source_id for c in restore.candidates(backup)
    ]
    dest = tmp_path / "out.zip"
    assert export_cmd("--offloaded", "--to", str(dest)) == 0
    with zipfile.ZipFile(dest) as zf:
        ids = [r["id"] for r in csv.DictReader(io.StringIO(zf.read("index.csv").decode()))]
    assert ids == ["m2", "m4"]  # m2 is included though restored; m3's old ID is not, its copy is


def test_export_changes_nothing_in_the_backup(backup, tmp_path):
    before = {p: p.read_bytes() for p in backup.rglob("*") if p.is_file()}
    assert export_cmd("--all", "--to", str(tmp_path / "out.mbox")) == 0
    assert export_cmd("--all", "--to", str(tmp_path / "out.zip")) == 0
    assert {p: p.read_bytes() for p in backup.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("suffix", ["mbox", "zip"])
def test_a_file_that_exists_is_never_overwritten(backup, tmp_path, suffix):
    dest = tmp_path / f"out.{suffix}"
    dest.write_text("keep me")
    with pytest.raises(SystemExit, match="already exists"):
        export_cmd("--all", "--to", str(dest))
    assert dest.read_text() == "keep me"


def test_it_will_not_write_into_the_backup(backup):
    with pytest.raises(SystemExit, match="inside the backup"):
        export_cmd("--all", "--to", str(backup / "out.mbox"))
    assert not (backup / "out.mbox").exists()


def test_the_destination_must_be_named_and_placed_sensibly(backup, tmp_path, capsys):
    with pytest.raises(SystemExit) as stopped:
        export_cmd("--all", "--to", str(tmp_path / "out.txt"))
    assert stopped.value.code == 2 and "name the file *.mbox" in capsys.readouterr().err
    with pytest.raises(SystemExit, match="does not exist"):
        export_cmd("--all", "--to", str(tmp_path / "nowhere" / "out.mbox"))
    with pytest.raises(SystemExit):
        export_cmd("--all")  # --to is required


def test_nothing_selected_or_matching_is_an_error(backup, tmp_path, capsys):
    with pytest.raises(SystemExit) as stopped:
        export_cmd("--to", str(tmp_path / "out.mbox"))
    assert stopped.value.code == 2 and "say which messages" in capsys.readouterr().err
    with pytest.raises(SystemExit, match="Nothing to export"):
        export_cmd("--all", "--sender", "nobody@example.net", "--to", str(tmp_path / "out.mbox"))
    assert not (tmp_path / "out.mbox").exists()


def test_a_message_whose_file_is_missing_is_left_out_and_reported(backup, tmp_path, capsys):
    next(backup.rglob("m3.eml")).unlink()
    dest = tmp_path / "out.zip"
    assert export_cmd("--all", "--to", str(dest)) == 1
    with zipfile.ZipFile(dest) as zf:
        ids = [r["id"] for r in csv.DictReader(io.StringIO(zf.read("index.csv").decode()))]
    assert ids == ["m1", "m2", "m4", "m5"]
    captured = capsys.readouterr()
    assert "Exported 4 messages" in captured.out
    assert "1 messages have no .eml file" in captured.err and "m3" in captured.err
    assert "mailcellar verify" in captured.err


def test_when_no_file_is_there_nothing_is_written(backup, tmp_path):
    for eml in backup.rglob("*.eml"):
        eml.unlink()
    with pytest.raises(SystemExit, match="has its file in the backup"):
        export_cmd("--all", "--to", str(tmp_path / "out.mbox"))
    assert list(tmp_path.glob("out.mbox*")) == []


@pytest.mark.parametrize("name", ["out.mbox", "out.zip"])
def test_a_failed_export_leaves_no_file_behind(backup, tmp_path, monkeypatch, name):
    def fail(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(export, "write_mbox", fail)
    monkeypatch.setattr(export, "write_zip", fail)
    with pytest.raises(RuntimeError):
        export_cmd("--all", "--to", str(tmp_path / name))
    assert list(tmp_path.glob("out.*")) == []  # neither the file nor its .part


def test_export_is_a_command_and_documented_in_the_help(backup, capsys):
    assert "export" in cli.COMMANDS and "mailcellar export" in cli.__doc__
    with pytest.raises(SystemExit):
        cli.main(["export", "--help"])
    out = capsys.readouterr().out
    assert out.startswith("usage: mailcellar export ")
    flat = " ".join(out.split())  # argparse wraps to the width of the terminal
    assert "--to FILE" in flat and "--has-label" in flat
    assert "Every message in the backup" in flat and "left alone" not in flat


def test_restore_still_says_all_leaves_mail_in_the_mailbox_alone(capsys):
    with pytest.raises(SystemExit):
        cli.main(["restore", "--help"])
    assert "those still in the mailbox are left alone" in " ".join(capsys.readouterr().out.split())


def test_a_backup_cannot_be_exported_before_meta_has_run(backup, tmp_path):
    (backup / "meta.sqlite").unlink()
    with pytest.raises(SystemExit, match="run mailcellar meta"):
        export_cmd("--all", "--to", str(tmp_path / "out.mbox"))
    assert gyb  # (the backup index itself is untouched)
