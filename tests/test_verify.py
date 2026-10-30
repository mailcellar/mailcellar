"""verify: a backup's files are checked against the content hashes in meta.sqlite."""

import random
import sqlite3

import pytest

from fake_mailbox import ACCOUNT, MS, FakeMailbox, raw_response
from mailcellar import cli
from mailcellar.backup import commands, gyb, sidecar, verify
from mailcellar.backup.verify import (
    MISMATCH,
    MISSING,
    NOT_IN_INDEX,
    NOT_INDEXED,
    OK,
    UNCHECKED,
    UNREADABLE,
    Entry,
    Read,
    Stored,
)
from mailcellar.message import PARSER_VERSION
from mailcellar.paths import backup_folder
from test_index import make_eml

IDS = ("g1", "g2", "g3")


def eml(gid, text=None):
    return make_eml(msgid=f"<{gid}@x>", body=text or f"text of {gid}")


@pytest.fixture
def folder(tmp_path, monkeypatch):
    """A backup of three messages under ./data, with meta.sqlite filled in."""
    monkeypatch.chdir(tmp_path)
    gmail = FakeMailbox({g: raw_response(g, body=eml(g)) for g in IDS})
    path = backup_folder(ACCOUNT)
    commands.backup(gmail, ACCOUNT, path)
    return path


def eml_path(folder, gid):
    return folder / gyb.relative_path(gid, MS)


def sql(folder, name, statement, *args):
    con = sqlite3.connect(folder / name)
    try:
        con.execute(statement, args)
        con.commit()
    finally:
        con.close()


def problems(report):
    return {(f.gmail_id, f.problem) for f in report.findings}


# ── the decisions ────────────────────────────────────────────────────────────


def test_judge_compares_hashes_only_with_a_current_row():
    stored = Stored("abc", PARSER_VERSION)
    assert verify.judge(Read(content_hash="abc"), stored) == OK
    assert verify.judge(Read(content_hash="xyz"), stored) == MISMATCH
    assert verify.judge(Read(problem=MISSING), stored) == MISSING
    assert verify.judge(Read(problem=UNREADABLE), None) == UNREADABLE  # damage needs no row
    assert verify.judge(Read(content_hash="xyz"), None) == UNCHECKED
    assert verify.judge(Read(content_hash="xyz"), Stored("abc", "0")) == UNCHECKED  # old parser
    # Recorded with no text or attachments: still none is EMPTY; some now is a change.
    assert verify.judge(Read(content_hash=""), Stored("", PARSER_VERSION)) == verify.EMPTY
    assert verify.judge(Read(content_hash="xyz"), Stored("", PARSER_VERSION)) == MISMATCH


def test_select_keeps_index_order_and_samples_within_the_scope():
    entries = [Entry(g, f"{g}.eml") for g in ("a", "b", "c", "d")]
    assert verify.select(entries, None, None, random.Random(0)) == entries
    assert [e.gmail_id for e in verify.select(entries, {"b", "d"}, None, random.Random(0))] == [
        "b",
        "d",
    ]
    sampled = verify.select(entries, {"a", "b", "c"}, 2, random.Random(0))
    assert len(sampled) == 2 and {e.gmail_id for e in sampled} <= {"a", "b", "c"}
    assert sampled == sorted(sampled, key=lambda e: e.gmail_id)
    assert verify.select(entries, None, 10, random.Random(0)) == entries  # fewer than N


def test_a_slice_from_a_start_wraps_and_successive_slices_cover_every_entry():
    entries = [Entry(g, f"{g}.eml") for g in ("a", "b", "c", "d", "e")]
    ids = lambda chosen: [e.gmail_id for e in chosen]  # noqa: E731
    assert ids(verify.select(entries, None, 2, random.Random(0), start=0)) == ["a", "b"]
    assert ids(verify.select(entries, None, 3, random.Random(0), start=4)) == ["e", "a", "b"]
    assert ids(verify.select(entries, None, 9, random.Random(0), start=7)) == list("cdeab")
    seen, start = [], 0
    for _ in range(3):
        seen += ids(verify.select(entries, None, 2, random.Random(0), start=start))
        start = verify.advance(start, 2, len(entries))
    assert sorted(set(seen)) == list("abcde") and start == 1
    assert verify.select([], None, 2, random.Random(0), start=3) == []


def test_advance_and_the_stored_cursor():
    assert verify.advance(0, 2, 5) == 2
    assert verify.advance(4, 2, 5) == 1
    assert verify.advance(3, 9, 5) == 3  # the whole backup: the same start next time
    assert verify.advance(3, 2, 0) == 0  # an empty backup
    assert [verify.cursor(v) for v in (None, "", "12", "x", "-1")] == [0, 0, 12, 0, 0]
    assert [verify.slice_size(n) for n in (0, 3, 8_000, 60_001)] == [1000, 1000, 1000, 7501]


def test_disagreements_name_each_side():
    found = verify.disagreements({"a", "b"}, {"b", "z"}, {"a", "y"})
    assert {(f.gmail_id, f.problem) for f in found} == {
        ("z", NOT_IN_INDEX),
        ("y", NOT_INDEXED),
    }
    assert verify.disagreements({"a"}, {"a"}, None) == []
    assert verify.disagreements({"a", "b"}, {"a"}, None) == []  # no meta row yet: unchecked


def test_unreadable_files_are_reported_with_the_reason(tmp_path, monkeypatch):
    path = tmp_path / "x.eml"
    path.write_bytes(b"x")

    def boom(_path):
        raise ValueError("bad bytes")

    monkeypatch.setattr(verify, "parse_eml", boom)
    read = verify.read_eml(path)
    assert read.problem == UNREADABLE and "bad bytes" in read.detail
    assert verify.read_eml(tmp_path / "none.eml").problem == MISSING


# ── a backup ─────────────────────────────────────────────────────────────────


def test_an_intact_backup_verifies(folder, capsys):
    report = verify.verify_backup(folder)
    assert (report.indexed, report.checked, report.unchecked) == (3, 3, 0)
    assert report.ok
    assert cli.main(["verify"]) == 0
    assert f"{ACCOUNT}: 3 messages verified, no problems" in capsys.readouterr().out


def test_a_missing_file_is_a_problem(folder, capsys):
    eml_path(folder, "g2").unlink()
    assert problems(verify.verify_backup(folder)) == {("g2", MISSING)}
    assert cli.main(["verify"]) == 1
    out = capsys.readouterr().out
    assert "g2  file missing" in out and "2 messages verified, 1 problems" in out


def test_a_changed_file_is_a_problem(folder):
    eml_path(folder, "g1").write_bytes(eml("g1", "tampered with"))
    assert problems(verify.verify_backup(folder)) == {("g1", MISMATCH)}
    assert cli.main(["verify"]) == 1


def test_rows_from_an_older_parser_are_unchecked_not_wrong(folder, capsys):
    sql(folder, "meta.sqlite", "UPDATE messages SET parser_version = '0'")
    eml_path(folder, "g1").write_bytes(eml("g1", "tampered with"))
    report = verify.verify_backup(folder)
    assert (report.checked, report.unchecked) == (0, 3) and report.ok
    assert cli.main(["verify"]) == 0  # nothing to compare with is a warning, not a failure
    captured = capsys.readouterr()
    assert "3 unchecked" in captured.out and "mailcellar meta" in captured.err


@pytest.fixture
def link_only(tmp_path, monkeypatch):
    """A backup of g1 and l1, whose body is only a link: no text left to hash."""
    monkeypatch.chdir(tmp_path)
    msgs = {"g1": eml("g1"), "l1": eml("l1", "https://example.com/shared/doc")}
    gmail = FakeMailbox({g: raw_response(g, body=b) for g, b in msgs.items()})
    path = backup_folder(ACCOUNT)
    commands.backup(gmail, ACCOUNT, path)
    return path


def test_a_message_with_nothing_to_hash_is_counted_apart_without_the_meta_advice(link_only, capsys):
    report = verify.verify_backup(link_only)
    assert (report.checked, report.empty, report.unchecked) == (1, 1, 0) and report.ok
    assert cli.main(["verify"]) == 0
    captured = capsys.readouterr()
    assert (
        "1 messages verified, 1 with no text or attachments to compare "
        "(--chain checks their bytes), no problems"
    ) in captured.out
    assert "mailcellar meta" not in captured.err
    assert cli.main(["verify", "--chain"]) == 0
    assert "(their bytes are in the hash chain check)" in capsys.readouterr().out


def test_a_message_recorded_empty_that_now_has_text_is_a_change(link_only):
    eml_path(link_only, "l1").write_bytes(eml("l1", "words added later"))
    assert problems(verify.verify_backup(link_only)) == {("l1", MISMATCH)}


def test_without_meta_sqlite_nothing_can_be_compared(folder):
    (folder / "meta.sqlite").unlink()
    report = verify.verify_backup(folder)
    assert (report.checked, report.unchecked) == (0, 3) and "run mailcellar meta" in report.note
    assert not (folder / "meta.sqlite").exists()  # and verify did not create it
    eml_path(folder, "g3").unlink()
    assert problems(verify.verify_backup(folder)) == {("g3", MISSING)}  # damage still shows


def test_a_meta_row_for_a_message_the_index_lacks_is_a_problem(folder):
    sql(folder, "msg-db.sqlite", "DELETE FROM uids WHERE uid = 'g3'")
    report = verify.verify_backup(folder)
    assert problems(report) == {("g3", NOT_IN_INDEX)} and report.indexed == 2


def test_offloaded_checks_only_that_mail(folder):
    con = sidecar.open_meta(folder)
    sidecar.record_offloaded(con, ["g1", "gone"], "batch", "rule", 1)
    con.commit()
    con.close()
    eml_path(folder, "g2").unlink()  # not offloaded: not looked at
    report = verify.verify_backup(folder, only_offloaded=True)
    assert (report.checked, problems(report)) == (1, {("gone", NOT_INDEXED)})
    eml_path(folder, "g1").write_bytes(eml("g1", "tampered with"))
    assert problems(verify.verify_backup(folder, only_offloaded=True)) == {
        ("g1", MISMATCH),
        ("gone", NOT_INDEXED),
    }


def test_restored_messages_are_no_longer_offloaded(folder):
    con = sidecar.open_meta(folder)
    sidecar.record_offloaded(con, ["g1"], "batch", "rule", 1)
    sidecar.record_restored(con, "g1", "g1", "untrash", "run", 2)
    con.commit()
    con.close()
    assert verify.verify_backup(folder, only_offloaded=True).checked == 0


def test_offloaded_needs_a_usable_offload_record(folder):
    (folder / "meta.sqlite").unlink()
    with pytest.raises(verify.VerifyError, match="offload record"):
        verify.verify_backup(folder, only_offloaded=True)


def test_sample_checks_n_messages(folder):
    report = verify.verify_backup(folder, sample=2, rng=random.Random(1))
    assert (report.indexed, report.checked) == (3, 2)
    sql(folder, "msg-db.sqlite", "DELETE FROM uids WHERE uid = 'g3'")
    assert verify.verify_backup(folder, sample=1).ok  # a sample doesn't count meta-only rows


def test_verify_changes_nothing(folder):
    eml_path(folder, "g2").unlink()
    before = {p: p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    assert cli.main(["verify"]) == 1
    assert cli.main(["verify", "--offloaded", ACCOUNT]) == 0
    assert {p: p.read_bytes() for p in folder.rglob("*") if p.is_file()} == before


# ── the command ──────────────────────────────────────────────────────────────


def test_no_backups_is_an_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="error: no backups found"):
        cli.main(["verify"])


def test_one_bad_account_does_not_stop_the_others(folder, capsys):
    other = backup_folder("b@example.org")
    commands.backup(FakeMailbox({"h1": raw_response("h1", body=eml("h1"))}), "b@example.org", other)
    (other / "msg-db.sqlite").write_bytes(b"not a database")
    assert cli.main(["verify"]) == 1
    captured = capsys.readouterr()
    assert f"{ACCOUNT}: 3 messages verified" in captured.out
    assert "error: b@example.org" in captured.err


def test_problem_listing_is_capped(folder, capsys):
    for gid in IDS:
        eml_path(folder, gid).unlink()
    assert cli.main(["verify", "--top", "1"]) == 1
    assert "... and 2 more" in capsys.readouterr().out


def test_sample_must_be_positive(folder):
    with pytest.raises(SystemExit):
        cli.main(["verify", "--sample", "0"])


# ── backup's check of the next slice ─────────────────────────────────────────


def test_successive_checks_cover_the_backup_and_keep_their_place(folder):
    starts, checked = [], 0
    for _ in range(3):
        report, start, taken = verify.verify_next(folder, 2)
        assert taken == 2
        starts.append(start)
        checked += report.checked
    assert starts == [0, 2, 1] and checked == 6  # g1 g2, g3 g1, g2 g3
    con = sidecar.open_meta(folder)
    try:
        assert sidecar.get_state(con, verify.CURSOR) == "0"
        runs = con.execute("SELECT status, failed FROM runs WHERE command = 'verify'")
        assert runs.fetchall() == [("ok", 0)] * 3
    finally:
        con.close()


def test_a_slice_finds_a_damaged_file_in_it_and_only_then(folder):
    eml_path(folder, "g3").write_bytes(eml("g3", "changed"))
    assert verify.verify_next(folder, 2)[0].ok  # g1, g2
    assert problems(verify.verify_next(folder, 2)[0]) == {("g3", MISMATCH)}
    con = sidecar.open_meta(folder)
    try:
        last = "SELECT status, failed FROM runs WHERE command = 'verify' ORDER BY run_id DESC"
        assert con.execute(last).fetchone() == ("failures", 1)  # for report health
    finally:
        con.close()


@pytest.fixture
def run_backup(folder, monkeypatch):
    """`mailcellar backup` on the fixture's backup, its mailbox a FakeMailbox."""
    gmail = FakeMailbox({g: raw_response(g, body=eml(g)) for g in IDS})
    monkeypatch.setattr(commands, "connect", lambda args: (ACCOUNT, folder, gmail))
    return lambda *argv: commands.main(["backup", ACCOUNT, *argv])


def test_backup_checks_the_next_slice_and_exits_1_on_damage(folder, run_backup, capsys):
    assert run_backup("--verify", "2") == 0
    assert f"{ACCOUNT}: 2 messages verified, no problems" in capsys.readouterr().out
    eml_path(folder, "g3").write_bytes(eml("g3", "changed"))  # the next slice: g3, g1
    assert run_backup("--verify", "2") == 1
    out = capsys.readouterr()
    assert f"g3  {MISMATCH}" in out.out and f"mailcellar verify {ACCOUNT} --chain" in out.err


def test_backup_checks_the_whole_of_a_small_backup_by_default(run_backup, capsys):
    assert run_backup() == 0
    assert f"{ACCOUNT}: 3 messages verified, no problems" in capsys.readouterr().out


def test_backup_verify_0_checks_nothing(folder, run_backup, capsys):
    sql(folder, "meta.sqlite", "UPDATE messages SET content_hash = 'x'")
    assert run_backup("--verify", "0") == 0
    assert "verified" not in capsys.readouterr().out


def test_backup_verify_must_not_be_negative(run_backup):
    with pytest.raises(SystemExit):
        run_backup("--verify", "-1")


def test_a_check_that_cant_run_fails_the_check_not_the_backup(run_backup, monkeypatch, capsys):
    def unreadable(folder, sample):
        raise sidecar.MetaError("meta.sqlite can't be read")

    monkeypatch.setattr(verify, "verify_next", unreadable)
    assert run_backup() == 1
    out = capsys.readouterr()
    assert "Downloaded 0 messages" in out.out
    assert "the check after the backup failed: meta.sqlite can't be read" in out.err
