"""backup/chain.py: the hash chain (#70), its links and replay, and the commands that append."""

import json
import sqlite3

import pytest

from fake_mailbox import ACCOUNT, FakeMailbox, raw_response
from mailcellar.backup import chain, commands, gyb, verify
from mailcellar.backup.chain import (
    ANCHOR_MISMATCH,
    BROKEN,
    CHANGED,
    MISSING,
    NO_ANCHOR,
    UNANCHORED,
    UNEXPLAINED,
    Link,
)
from mailcellar.report import health


def lines_of(*links):
    """Lines of a well-formed chain of the given (op, added, removed), linked in order."""
    out, last = [], None
    for op, added, removed in links:
        line = chain.line_of(chain.next_link(last, op, added, removed, "2026-11-01T00:00:00Z"))
        last = (chain.parse_line(line).seq, chain.hash_line(line))
        out.append(line)
    return out


# ── decisions ────────────────────────────────────────────────────────────────


def test_a_link_reads_back_as_written():
    link = Link(3, "2026-11-01T00:00:00Z", "backup", (("g1", "aa"),), (), "ff")
    line = chain.line_of(link)
    assert chain.parse_line(line) == link
    assert line == chain.line_of(chain.parse_line(line))  # the hash depends on the exact line
    assert '"seq":3,"time":' in line and " " not in line


@pytest.mark.parametrize(
    "line",
    [
        "not json",
        '{"seq": 1}',
        '{"seq":1,"time":"t","op":"edit","added":[],"removed":[],"prev":""}',
    ],
)
def test_a_line_that_isnt_a_link_is_refused(line):
    with pytest.raises(chain.ChainError):
        chain.parse_line(line)


def test_replay_follows_additions_and_removals():
    lines = lines_of(
        ("genesis", [("g1", "a1"), ("g2", "a2")], []),
        ("backup", [("g3", "a3")], []),
        ("prune", [], [("g1", "a1")]),
        ("refetch", [("g2", "b2")], []),
        ("empty", [], [("g1", "")]),  # the held copy deleted: the backup is unchanged
    )
    held, problems, heads = chain.replay(lines)
    assert held == {"g2": "b2", "g3": "a3"}
    assert problems == [] and [s for s, _h in heads] == [1, 2, 3, 4, 5]


def test_replay_finds_an_edited_removed_or_reordered_link():
    lines = lines_of(("genesis", [("g1", "a1")], []), ("backup", [("g2", "a2")], []))
    edited = [lines[0].replace("a1", "zz"), lines[1]]  # link 2's prev no longer matches
    assert [p.kind for p in chain.replay(edited)[1]] == [BROKEN]
    assert [p.kind for p in chain.replay(lines[1:])[1]] == [BROKEN]  # link 1 cut out
    assert [p.kind for p in chain.replay([lines[0], "garbage", lines[1]])[1]] == [BROKEN, BROKEN]


def test_removals_are_what_prune_and_forget_recorded():
    lines = lines_of(
        ("genesis", [("g1", "a1"), ("g2", "a2"), ("g3", "a3")], []),
        ("prune", [], [("g1", "a1")]),
        ("forget", [], [("g2", "a2")]),
        ("undo", [("g2", "a2")], []),
        ("empty", [], [("g3", "")]),  # held files deleted: not a record of their bytes
    )
    assert chain.removals(lines + ["not a link"]) == {"g1": "a1", "g2": "a2"}


def test_compare_reports_changed_missing_and_unexplained():
    held = {"g1": "a1", "g2": "a2", "g3": "a3"}
    actual = {"g1": "a1", "g2": "XX", "g4": "a4", "g5": None}  # g3 gone; g5's file missing
    got = [(p.kind, p.gmail_id) for p in chain.compare(held, actual)]
    assert got == [(CHANGED, "g2"), (MISSING, "g3"), (UNEXPLAINED, "g4")]


def test_the_anchor_against_the_chain():
    heads = [(1, "h1"), (2, "h2"), (3, "h3")]
    assert chain.check_anchor(heads, {"seq": 3, "hash": "h3"}) == []
    assert [p.kind for p in chain.check_anchor(heads, {"seq": 2, "hash": "h2"})] == [UNANCHORED]
    assert [p.kind for p in chain.check_anchor(heads, {"seq": 2, "hash": "xx"})] == [
        ANCHOR_MISMATCH
    ]
    assert [p.kind for p in chain.check_anchor(heads, {"seq": 5, "hash": "h5"})] == [
        ANCHOR_MISMATCH  # the chain was cut short
    ]
    assert [p.kind for p in chain.check_anchor(heads, None)] == [NO_ANCHOR]


# ── the commands that append ─────────────────────────────────────────────────


def kinds(folder):
    return [(p.kind, p.gmail_id) for p in chain.check(folder, ACCOUNT).problems]


def ops(folder):
    return [chain.parse_line(line).op for line in chain.read_lines(folder)]


@pytest.fixture
def backed_up(tmp_path):
    folder = tmp_path / "data" / ACCOUNT
    gmail = FakeMailbox({g: raw_response(g) for g in ("g1", "g2", "g3")})
    commands.backup(gmail, ACCOUNT, folder)
    return gmail, folder


def eml(folder, gid):
    return next(folder.rglob(f"{gid}.eml"))


def test_a_backup_starts_the_chain_and_records_what_it_downloads(backed_up):
    gmail, folder = backed_up
    assert ops(folder) == ["genesis", "backup"]  # an empty genesis, then the first run
    assert sorted(chain.held(folder)) == ["g1", "g2", "g3"]
    gmail.messages["g4"] = raw_response("g4")
    commands.backup(gmail, ACCOUNT, folder)
    assert ops(folder) == ["genesis", "backup", "backup"]
    assert kinds(folder) == []
    anchor = json.loads(chain.anchor_path(ACCOUNT).read_text())
    assert anchor["seq"] == 3 and chain.anchor_path(ACCOUNT).stat().st_mode & 0o777 == 0o600


def test_a_backup_from_before_the_chain_gets_a_genesis_of_everything(backed_up):
    gmail, folder = backed_up
    chain.chain_path(folder).unlink()  # as made by gyb, or by mailcellar before the chain
    commands.backup(gmail, ACCOUNT, folder)
    (genesis,) = [chain.parse_line(line) for line in chain.read_lines(folder)]
    assert genesis.op == "genesis" and [g for g, _h in genesis.added] == ["g1", "g2", "g3"]
    assert dict(genesis.added)["g1"] == chain.file_hash(eml(folder, "g1"))
    assert kinds(folder) == []


def test_tampering_with_the_backup_shows(backed_up):
    gmail, folder = backed_up
    eml(folder, "g1").write_bytes(eml(folder, "g1").read_bytes().replace(b"Subject", b"Subjekt"))
    eml(folder, "g2").unlink()
    con = gyb.open_backup(folder, ACCOUNT)
    gyb.write_message(con, folder, raw_response("g9"), ["INBOX"])  # added behind its back
    con.commit()
    con.close()
    assert kinds(folder) == [(CHANGED, "g1"), (MISSING, "g2"), (UNEXPLAINED, "g9")]


def test_a_change_meta_sqlite_was_made_to_agree_with_still_shows(backed_up):
    _gmail, folder = backed_up
    path = eml(folder, "g1")
    path.write_bytes(path.read_bytes() + b"Added later.\r\n")
    # Rewrite meta.sqlite's hash too: plain verify no longer sees it ...
    con = sqlite3.connect(folder / "meta.sqlite")
    con.execute("UPDATE messages SET parser_version = 'old' WHERE gmail_id = 'g1'")
    con.commit()
    con.close()
    commands.backfill(None, ACCOUNT, folder)
    assert verify.verify_backup(folder).ok
    # ... but the chain does.
    assert kinds(folder) == [(CHANGED, "g1")]


def test_rewriting_the_chain_to_match_shows_at_the_anchor(backed_up):
    _gmail, folder = backed_up
    path = eml(folder, "g1")
    path.write_bytes(path.read_bytes() + b"\n")
    lines = chain.read_lines(folder)
    old = chain.held(folder)["g1"]
    forged = [line.replace(old, chain.file_hash(path)) for line in lines]
    relinked, last = [], None  # every prev recomputed, so the chain itself is whole
    for line in forged:
        link = chain.parse_line(line)
        link = chain.next_link(last, link.op, link.added, link.removed, link.time)
        relinked.append(chain.line_of(link))
        last = (link.seq, chain.hash_line(relinked[-1]))
    chain.chain_path(folder).write_text("\n".join(relinked) + "\n")
    assert kinds(folder) == [(ANCHOR_MISMATCH, "")]


def test_a_link_whose_messages_never_arrived_is_repaired_by_the_next_backup(backed_up):
    gmail, folder = backed_up
    gmail.messages["g4"] = raw_response("g4")
    chain.append(folder, ACCOUNT, "backup", [("g4", "0" * 64)])  # a crash before the commit
    assert kinds(folder) == [(MISSING, "g4")]
    commands.backup(gmail, ACCOUNT, folder)
    assert ops(folder)[-1] == "refetch" and kinds(folder) == []


def test_a_missing_file_downloaded_again_is_a_refetch(backed_up):
    gmail, folder = backed_up
    eml(folder, "g2").unlink()
    commands.backup(gmail, ACCOUNT, folder, full=True)
    assert ops(folder)[-1] == "refetch" and kinds(folder) == []


def test_a_prune_records_what_it_moves_out(backed_up):
    gmail, folder = backed_up
    del gmail.messages["g1"]
    commands.prune(gmail, ACCOUNT, folder, confirm=lambda prompt: True, force=True)
    last = chain.parse_line(chain.read_lines(folder)[-1])
    assert last.op == "prune" and [g for g, _h in last.removed] == ["g1"]
    assert kinds(folder) == []


# ── checking ─────────────────────────────────────────────────────────────────


def test_verify_chain_passes_and_fails_with_the_chain(backed_up, capsys):
    _gmail, folder = backed_up
    assert verify.main(["--chain"]) == 0
    out = capsys.readouterr().out
    assert f"{ACCOUNT}: hash chain of 2 links, head 2 " in out and "no problems" in out
    eml(folder, "g3").unlink()
    assert verify.main(["--chain"]) == 1
    assert f"g3  {MISSING}" in capsys.readouterr().out


def test_verify_chain_checks_everything(backed_up):
    with pytest.raises(SystemExit):
        verify.main(["--chain", "--sample", "5"])


def test_a_missing_anchor_is_a_warning_not_a_failure(backed_up, caplog):
    chain.anchor_path(ACCOUNT).unlink()
    assert verify.main(["--chain"]) == 0
    assert NO_ANCHOR in caplog.text


def test_health_shows_the_head(backed_up):
    _gmail, folder = backed_up
    (row,) = health.chains([folder]).rows
    head = chain.replay(chain.read_lines(folder))[2][-1]
    assert row == (ACCOUNT, 2, 2, head[1], "agrees")


def test_the_text_report_prints_the_head_hash_in_full(backed_up):
    import io

    from mailcellar.report.output import render

    _gmail, folder = backed_up
    out = io.StringIO()
    render([health.chains([folder])], 50, out)
    head = chain.replay(chain.read_lines(folder))[2][-1][1]
    assert len(head) == 64 and head in out.getvalue()  # all of it, not cut with …


def test_a_backup_refuses_a_backup_it_anchored_that_isnt_there(tmp_path):
    gmail = FakeMailbox({"g1": raw_response("g1")})
    folder = tmp_path / "volume" / ACCOUNT
    commands.backup(gmail, ACCOUNT, folder)  # the first backup: no anchor yet
    (tmp_path / "volume").rename(tmp_path / "unmounted")  # the volume goes away
    with pytest.raises(commands.BackupMissing, match="isn't mounted"):
        commands.backup(gmail, ACCOUNT, folder)
    assert not folder.exists()  # nothing started in its place
    # Starting afresh there on purpose: move the anchor aside first.
    chain.anchor_path(ACCOUNT).rename(tmp_path / "old-anchor.json")
    assert commands.backup(gmail, ACCOUNT, folder)["downloaded"] == 1


def test_a_missing_backup_is_refused_only_with_an_anchor():
    anchor = chain.anchor_path(ACCOUNT)
    assert commands.missing_backup(ACCOUNT, anchor.parent, False, None) == ""  # a first backup
    assert commands.missing_backup(ACCOUNT, anchor.parent, True, anchor) == ""  # it's there
    assert str(anchor) in commands.missing_backup(ACCOUNT, anchor.parent, False, anchor)


def test_commands_appending_at_once_cannot_fork_the_chain(backed_up):
    import threading

    _gmail, folder = backed_up
    before = len(chain.read_lines(folder))

    def work(n):
        for i in range(10):
            chain.append(folder, ACCOUNT, "backup", [(f"t{n}-{i}", "aa")])

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    lines = chain.read_lines(folder)
    assert len(lines) == before + 80
    _held, problems, heads = chain.replay(lines)
    assert [p for p in problems if p.kind == BROKEN] == []  # every link follows the one before
    assert [seq for seq, _h in heads] == list(range(1, len(lines) + 1))
    assert chain.read_anchor(ACCOUNT)["seq"] == len(lines)


def test_a_second_genesis_appends_nothing(backed_up):
    _gmail, folder = backed_up
    before = chain.read_lines(folder)
    assert chain.append(folder, ACCOUNT, "genesis", [("x", "aa")]) is None
    assert chain.read_lines(folder) == before


# ── prune: safe to interrupt, and to run again (review findings) ─────────────


def pruned_rows(folder):
    return [u for (u,) in sqlite3.connect(folder / gyb.DB_NAME).execute("SELECT uid FROM uids")]


def test_an_interrupted_prune_leaves_the_index_as_it_was(backed_up, monkeypatch):
    _gmail, folder = backed_up
    dest = folder.parent / "_pruned" / "run1"
    real_move, moves = gyb.shutil.move, []

    def stopping_move(src, target):
        if moves:
            raise KeyboardInterrupt
        moves.append(src)
        return real_move(src, target)

    monkeypatch.setattr(gyb.shutil, "move", stopping_move)
    con = gyb.open_backup(folder, ACCOUNT)
    with pytest.raises(KeyboardInterrupt):
        gyb.prune(con, folder, ["g1", "g2"], dest)
    con.close()  # uncommitted: rolled back
    assert sorted(pruned_rows(folder)) == ["g1", "g2", "g3"]  # still agrees with meta.sqlite
    assert not (dest / gyb.PRUNE_MANIFEST).exists()
    monkeypatch.setattr(gyb.shutil, "move", real_move)
    con = gyb.open_backup(folder, ACCOUNT)  # the next run finds g1's file in run1
    manifest = gyb.prune(con, folder, ["g1", "g2"], folder.parent / "_pruned" / "run2")
    con.close()
    assert [(m["gmail_id"], m["file"]) for m in manifest] == [("g1", "moved"), ("g2", "moved")]
    assert not dest.exists()  # its one file moved on: no folder left that nothing lists


def test_a_prune_run_again_after_a_crash_finds_the_file_the_first_moved(backed_up):
    _gmail, folder = backed_up
    first, second = folder.parent / "_pruned" / "run1", folder.parent / "_pruned" / "run2"
    src = eml(folder, "g1")
    rel = src.relative_to(folder)
    (first / rel).parent.mkdir(parents=True)
    src.rename(first / rel)  # moved, then the process died before the rows were committed
    con = gyb.open_backup(folder, ACCOUNT)
    manifest = gyb.prune(con, folder, ["g1"], second)
    con.close()
    assert [(m["gmail_id"], m["file"]) for m in manifest] == [("g1", "moved")]
    assert (second / rel).stat().st_nlink == 1  # one copy, under the one manifest
    assert not first.exists()
    assert "g1" not in pruned_rows(folder)


def test_a_rerun_keeps_what_an_interrupted_run_holds_for_other_messages(backed_up):
    _gmail, folder = backed_up
    first, second = folder.parent / "_pruned" / "run1", folder.parent / "_pruned" / "run2"
    for gid in ("g1", "g2"):  # both moved, then the process died
        src = eml(folder, gid)
        (first / src.relative_to(folder)).parent.mkdir(parents=True, exist_ok=True)
        src.rename(first / src.relative_to(folder))
    con = gyb.open_backup(folder, ACCOUNT)
    gyb.prune(con, folder, ["g1"], second)  # this run is asked for g1 only
    con.close()
    assert [p.name for p in first.rglob("*.eml")] == ["g2.eml"]  # still there for g2's run


def test_a_file_a_finished_run_still_lists_is_linked_not_moved(backed_up):
    _gmail, folder = backed_up
    first, second = folder.parent / "_pruned" / "run1", folder.parent / "_pruned" / "run2"
    src = eml(folder, "g1")
    rel = src.relative_to(folder)
    (first / rel).parent.mkdir(parents=True)
    src.rename(first / rel)
    (first / gyb.PRUNE_MANIFEST).write_text("gmail_id,path,internal_date,labels,file\n")
    con = gyb.open_backup(folder, ACCOUNT)
    gyb.prune(con, folder, ["g1"], second)
    con.close()
    assert (first / rel).is_file() and (second / rel).is_file()  # the finished run keeps it


def test_a_missing_file_with_no_earlier_copy_is_still_reported_missing(backed_up):
    _gmail, folder = backed_up
    eml(folder, "g1").unlink()
    con = gyb.open_backup(folder, ACCOUNT)
    (row,) = gyb.prune(con, folder, ["g1"], folder.parent / "_pruned" / "run1")
    con.close()
    assert row["file"] == "was missing"


def test_the_per_year_count_works_in_chunks_of_any_size(backed_up):
    _gmail, folder = backed_up
    con = gyb.open_backup(folder, ACCOUNT)
    ids = ["g1", "g2", "g3"]
    assert commands.gone_by_year(con, ids, chunk=1) == commands.gone_by_year(con, ids, chunk=500)
    assert sum(commands.gone_by_year(con, ids, chunk=2).values()) == 3
    assert commands.gone_by_year(con, []) == {}
    big = [f"x{i}" for i in range(40_000)]  # more than SQLite's limit of variables
    assert commands.gone_by_year(con, big) == {}
    con.close()
