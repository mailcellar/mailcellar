"""mailcellar forget (#9): offloaded mail Gmail no longer has, deleted from the backup in stages."""

import csv
import sqlite3
from datetime import datetime

import pytest

from fake_mailbox import FakeMailbox, raw_response
from mailcellar import cli, mailbox
from mailcellar.backup import commands, gyb, sidecar
from mailcellar.dedup import db as dedup_db
from mailcellar.dedup import index as dedup_index
from mailcellar.dedup.paths import default_db as dedup_path
from mailcellar.offload import cli as offload_cli
from mailcellar.offload import forget, forget_cli
from mailcellar.offload.forget import Offloaded, choose, due, scrub_rows
from mailcellar.paths import data_root
from mailcellar.report.db import open_sidecars
from mailcellar.report.storage import offload_batches
from mailcellar.restore.apply import candidates as restore_candidates
from mailcellar.search import db as search_db
from mailcellar.search import index as search_index

A = "a@x.com"
BATCH = "2026-01-15-093000"


def marker(gid: str) -> bytes:
    """A notice's own word, in its sender, subject and body: any copy left is found."""
    return gid.encode() + b"zqxj"


def eml(gid: str, notice: bool) -> bytes:
    if notice:
        headers = (
            b"From: Notices <notices-" + marker(gid) + b"@example.com>\r\n"
            b"To: you@example.com\r\nSubject: Notice " + marker(gid) + b"\r\n"
        )
        body = b"Reference " + marker(gid) + b", see attached.\r\n"
    else:
        headers = b"From: Alice <alice@example.com>\r\nSubject: Lunch " + gid.encode() + b"\r\n"
        body = b"See you at noon.\r\n"
    return headers + b"Message-ID: <" + gid.encode() + b"@x>\r\n\r\n" + body


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def read_csv(path):
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


@pytest.fixture
def env(tmp_path, monkeypatch):
    """
    Notices p0–p3 and lunch mail l0–l1, backed up, indexed for search and
    dedup, named in plans, logs and a batch manifest. p0–p3 and l0 were
    offloaded; p0, p1 and p2 are recorded gone, but Gmail has p2 again;
    p3 is still in Gmail's Trash.
    """
    monkeypatch.chdir(tmp_path)
    gids = {"p0": True, "p1": True, "p2": True, "p3": True, "l0": False, "l1": False}
    gmail = FakeMailbox({g: raw_response(g, body=eml(g, p)) for g, p in gids.items()})
    folder = data_root() / A
    commands.backup(gmail, A, folder)
    con = sidecar.open_meta(folder)
    sidecar.record_offloaded(con, ["p0", "p1", "p2", "p3"], BATCH, "notices", 1_000)
    sidecar.record_offloaded(con, ["l0"], "2026-02-01-080000", "lunch", 2_000)
    sidecar.mark_gone(con, ["p0", "p1", "p2"], 5_000)
    con.commit()
    con.close()
    for gid in ("p0", "p1"):
        del gmail.messages[gid]  # Trash emptied

    scon = search_db.open_db(search_db.default_db())
    search_index.update(scon, folder)
    scon.commit()
    scon.close()
    dcon = dedup_db.open_db(dedup_path())
    dedup_index.index_backup(dcon, folder)
    dcon.commit()
    dcon.close()

    def row(gid, notice, **more):
        word = marker(gid).decode()
        sender = f"Notices <notices-{word}@example.com>" if notice else "Alice <alice@example.com>"
        subject = f"Notice {word}" if notice else f"Lunch {gid}"
        return {"account": A, "gmail_id": gid, "sender": sender, "subject": subject, **more}

    off = data_root() / "offload"
    plan_rows = [row(g, p, rule="notices") for g, p in gids.items()]
    write_csv(off / "offload-plan.csv", plan_rows)
    write_csv(off / "plans" / "2026-01-15-093000.csv", plan_rows)
    write_csv(
        off / "offload-apply-log.csv", [row(g, p, outcome="trashed") for g, p in gids.items()]
    )
    write_csv(
        off / "batches" / BATCH / "offloaded.csv",
        [row(g, True, batch=BATCH) for g in gids if gids[g]],
    )
    keeper = {"keeper_account": A, "keeper_gmail_id": "p0"}
    write_csv(data_root() / "dedup" / "dedup-plan.csv", [row("l1", False, **keeper)])
    (off / "review").mkdir(parents=True)
    (off / "review" / "notices.html").write_text("<p>Notice p0zqxj</p>")

    def live(account, config, ids):
        return {g: (set() if g in gmail.messages else None) for g in ids}, {}

    monkeypatch.setattr(forget_cli, "gmail_state", live)
    monkeypatch.setattr(forget_cli, "second_copy_report", lambda d, ms: ["(report)"])
    return folder, gmail


def run(*args, typed=None, monkeypatch=None):
    if typed is not None:
        monkeypatch.setattr("builtins.input", lambda prompt: typed)
    cli.main(["forget", *args])


def files_with_marker(root, *gids, held_too=False):
    """Files under `root` holding any of the messages' words (held forget runs apart)."""
    held = data_root() / forget.FORGOTTEN_DIR
    return sorted(
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.is_file()
        and (held_too or held not in p.parents)
        and any(marker(g) in p.read_bytes().lower() for g in gids)
    )


# ── decisions ────────────────────────────────────────────────────────────────


def o(gid, gone=1):
    return Offloaded(gid, BATCH, "notices", gone, 0, 10)


def test_choose_forgets_only_what_is_recorded_gone_and_gone_now():
    got = choose(
        [o("gone"), o("trash", gone=None), o("back"), o("ask")],
        {"gone": None, "back": {"INBOX"}},
        {"ask": "HTTP 500"},
    )
    assert [c.gmail_id for c in got.forget] == ["gone"]
    assert [c.gmail_id for c in got.not_gone] == ["trash"]
    assert [c.gmail_id for c in got.in_gmail] == ["back"]
    assert [(c.gmail_id, why) for c, why in got.unchecked] == [("ask", "HTTP 500")]


def test_due_after_the_hold():
    now = datetime(2026, 3, 1, 12, 0)
    runs = ["2026-01-30-120000", "2026-01-30-120001", "2026-02-20-090000", "notes"]
    assert due(runs, now) == ["2026-01-30-120000"]  # 30 days to the second
    assert due(runs, now, hold_days=0) == runs[:3]


def test_scrub_rows_blanks_sender_and_subject_of_forgotten_messages_and_their_keepers():
    rows = [
        {"account": A, "gmail_id": "p0", "sender": "s", "subject": "t", "size": "10"},
        {
            "account": A,
            "gmail_id": "l1",
            "keeper_account": A,
            "keeper_gmail_id": "p0",
            "sender": "s",
            "subject": "t",
        },
        {"account": A, "gmail_id": "l0", "sender": "s", "subject": "t"},
        {"account": "b@x.com", "gmail_id": "p0", "sender": "s", "subject": "t"},
    ]
    before = [dict(r) for r in rows]
    out, changed = scrub_rows(rows, {(A, "p0")})
    assert changed == 2 and rows == before  # new rows; the given ones untouched
    assert out[0] == {"account": A, "gmail_id": "p0", "sender": "", "subject": "", "size": "10"}
    assert out[1]["subject"] == "" and out[2] == rows[2] and out[3] == rows[3]


# ── the command ──────────────────────────────────────────────────────────────


def test_a_dry_run_says_what_it_would_do_and_changes_nothing(env, capsys):
    folder, _gmail = env
    before = {p: p.read_bytes() for p in data_root().rglob("*") if p.is_file()}
    run("--batch", BATCH)
    out = capsys.readouterr().out
    assert "4 offloaded messages selected" in out
    assert "can be forgotten                       2" in out
    assert "not yet gone from Gmail                1" in out
    assert "Gmail has them again                   1" in out
    assert "any export you made" in out and "Dry run: nothing was changed" in out
    assert {p: p.read_bytes() for p in data_root().rglob("*") if p.is_file()} == before


def test_forget_needs_the_count_typed(env, monkeypatch, capsys):
    run("--batch", BATCH, "--yes", typed="3", monkeypatch=monkeypatch)
    assert "Nothing forgotten." in capsys.readouterr().out
    assert not (data_root() / forget.FORGOTTEN_DIR).exists()


def test_forget_leaves_no_trace_of_the_messages_in_the_data_root(env, monkeypatch, capsys):
    folder, _gmail = env
    everywhere = files_with_marker(data_root(), "p0", "p1")
    assert len(everywhere) >= 10, everywhere  # .eml, databases, plans, logs, manifest, review
    run("--batch", BATCH, "--yes", typed="2", monkeypatch=monkeypatch)
    assert "Forgot 2 messages" in capsys.readouterr().out

    # Not a byte of p0 or p1 left outside the held run: not even in a database's
    # freed pages or the search index's segments. p2 and p3 stay.
    assert files_with_marker(data_root(), "p0", "p1") == []
    assert files_with_marker(data_root(), "p2", "p3")
    for store in ("meta.sqlite", "msg-db.sqlite", "search.sqlite", "dedup.sqlite"):
        con = sqlite3.connect(next(data_root().rglob(store)))
        dump = "\n".join(con.iterdump())
        con.close()
        assert "p0@x" not in dump and "p1@x" not in dump, store

    (run_dir,) = (data_root() / forget.FORGOTTEN_DIR / A).iterdir()
    assert sorted(p.name for p in run_dir.rglob("*.eml")) == ["p0.eml", "p1.eml"]
    assert not (folder / "2023").exists() or not list(folder.rglob("p0.eml"))
    con = sqlite3.connect(folder / "meta.sqlite")
    assert con.execute("SELECT gmail_id, batch, rule FROM forgotten ORDER BY 1").fetchall() == [
        ("p0", BATCH, "notices"),
        ("p1", BATCH, "notices"),
    ]
    con.close()


def test_forget_scrubs_sender_and_subject_but_keeps_the_rest(env, monkeypatch):
    run("--batch", BATCH, "--yes", typed="2", monkeypatch=monkeypatch)
    off = data_root() / "offload"
    plan = {r["gmail_id"]: r for r in read_csv(off / "offload-plan.csv")}
    assert (plan["p0"]["sender"], plan["p0"]["subject"], plan["p0"]["rule"]) == ("", "", "notices")
    assert plan["p2"]["subject"] == "Notice p2zqxj"  # not forgotten, untouched
    assert read_csv(data_root() / "dedup" / "dedup-plan.csv")[0]["subject"] == ""  # p0 kept it
    assert not list((off / "review").glob("*.html"))


def test_forgotten_mail_is_reported_apart_and_cannot_be_restored(env, monkeypatch, capsys):
    folder, gmail = env
    run("--batch", BATCH, "--yes", typed="2", monkeypatch=monkeypatch)
    assert "p0" not in {c.source_id for c in restore_candidates(folder, ids={"p0"})}
    assert commands.count(gmail, A, folder)["forgotten"] == 2
    con, _accounts = open_sidecars([folder])
    try:
        (batch_row,) = [r for r in offload_batches(con).rows if r[1] == BATCH]
    finally:
        con.close()
    assert batch_row[-1] == 2  # Forgotten


def test_undo_puts_a_held_run_back(env, monkeypatch, capsys):
    folder, gmail = env
    run("--batch", BATCH, "--yes", typed="2", monkeypatch=monkeypatch)
    (run_dir,) = (data_root() / forget.FORGOTTEN_DIR / A).iterdir()
    run("--undo", run_dir.name)
    assert "put 2 messages back" in capsys.readouterr().out
    assert not run_dir.exists()
    assert {"p0", "p1"} <= {bm.gmail_id for bm in gyb.iter_backup(folder)}
    con = sqlite3.connect(folder / "meta.sqlite")
    assert con.execute("SELECT count(*) FROM forgotten").fetchone() == (0,)
    con.close()
    commands.backfill(None, A, folder)  # `mailcellar meta`: parsed again
    assert {c.source_id for c in restore_candidates(folder, ids={"p0"})} == {"p0"}
    with pytest.raises(SystemExit, match="no held forget run"):
        run("--undo", run_dir.name)


def test_empty_deletes_runs_after_the_hold_for_good(env, monkeypatch, capsys):
    folder, _gmail = env
    run("--batch", BATCH, "--yes", typed="2", monkeypatch=monkeypatch)
    run("--empty")
    assert "is held until" in capsys.readouterr().out  # not yet: nothing to delete
    monkeypatch.setattr(forget, "HOLD_DAYS", 0)
    run("--empty")
    assert "Dry run: nothing deleted" in capsys.readouterr().out
    run("--empty", "--yes", typed="1", monkeypatch=monkeypatch)
    assert "deleted 2 forgotten messages for good" in capsys.readouterr().out
    assert not list((data_root() / forget.FORGOTTEN_DIR / A).iterdir())
    con = sqlite3.connect(folder / "meta.sqlite")
    assert con.execute(
        "SELECT count(*) FROM forgotten WHERE emptied_ms IS NOT NULL"
    ).fetchone() == (2,)
    con.close()
    assert files_with_marker(data_root(), "p0", "p1", held_too=True) == []  # held copies too


def test_offload_forget_is_handed_on_to_forget_and_says_so(env, monkeypatch, capsys):
    assert cli.main(["offload", "forget", "--batch", BATCH]) == 0
    captured = capsys.readouterr()
    assert "'offload forget' is now 'mailcellar forget'" in captured.err
    assert "Dry run: nothing was changed" in captured.out
    monkeypatch.setattr("builtins.input", lambda prompt: "2")
    assert cli.main(["offload", "forget", "--batch", BATCH, "--yes"]) == 0
    assert "Forgot 2 messages" in capsys.readouterr().out
    (run_dir,) = forget.runs_dir(A).iterdir()
    assert cli.main(["offload", "forget", "--undo", run_dir.name]) == 0
    assert "put 2 messages back" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["offload", "--help"])
    assert "Now 'mailcellar forget'" in capsys.readouterr().out
    cli.main(["--help"])
    assert "mailcellar forget" in capsys.readouterr().out


def test_offload_forget_past_the_hand_on_is_an_error_not_a_traceback(env):
    with pytest.raises(SystemExit, match="is now 'mailcellar forget'"):
        cli.main(["offload", "--", "forget"])


def test_runs_past_their_hold_are_mentioned_until_emptied(env, monkeypatch, capsys):
    run("--batch", BATCH, "--yes", typed="2", monkeypatch=monkeypatch)
    capsys.readouterr()
    run("--batch", BATCH)
    assert "forget runs held" not in capsys.readouterr().out  # not due yet
    monkeypatch.setattr(forget, "HOLD_DAYS", 0)
    (run_dir,) = (data_root() / forget.FORGOTTEN_DIR / A).iterdir()
    run("--batch", BATCH)
    out = capsys.readouterr().out
    assert f"{A}: 1 forget runs held 0 days or more ({run_dir.name})" in out
    assert "'mailcellar forget --empty' deletes them for good" in out
    assert forget.held(A) == [(run_dir.name, 2)]


def test_forget_undo_and_empty_keep_the_hash_chain_whole(env, monkeypatch):
    from mailcellar.backup import chain

    folder, _gmail = env

    def failures():
        return [(p.kind, p.gmail_id) for p in chain.check(folder, A).failures]

    def last_op():
        return chain.parse_line(chain.read_lines(folder)[-1])

    run("--batch", BATCH, "--yes", typed="2", monkeypatch=monkeypatch)
    assert last_op().op == "forget" and {g for g, _h in last_op().removed} == {"p0", "p1"}
    assert failures() == []
    (run_dir,) = (data_root() / forget.FORGOTTEN_DIR / A).iterdir()
    run("--undo", run_dir.name)
    assert last_op().op == "undo" and {g for g, _h in last_op().added} == {"p0", "p1"}
    assert failures() == []
    run("--batch", BATCH, "--yes", typed="2", monkeypatch=monkeypatch)
    monkeypatch.setattr(forget, "HOLD_DAYS", 0)
    run("--empty", "--yes", typed="1", monkeypatch=monkeypatch)
    assert last_op().op == "empty" and failures() == []


def test_forget_needs_a_selection(env):
    with pytest.raises(SystemExit, match="say which mail"):
        run()


# ── --deleted: mail deleted in its mailbox, never offloaded (B40) ────────────


@pytest.fixture
def deleted(env, monkeypatch):
    """env, with l1 (never offloaded) deleted in Gmail too, and forget listing the fake."""
    folder, gmail = env
    del gmail.messages["l1"]
    monkeypatch.setattr(mailbox, "opener", lambda config: lambda account, scope: gmail)
    return folder, gmail


def test_deleted_forgets_mail_the_mailbox_deleted_and_never_offloaded(deleted, monkeypatch, capsys):
    folder, gmail = deleted
    assert cli.main(["forget", "--deleted", "--force"]) == 0  # a dry run
    out = capsys.readouterr().out
    assert "1 messages gone from Gmail, never offloaded" in out
    assert "can be forgotten                       1  (" in out
    assert "l1" in {m.gmail_id for m in gyb.iter_backup(folder)}

    monkeypatch.setattr("builtins.input", lambda prompt: "1")
    assert cli.main(["forget", "--deleted", "--force", "--yes"]) == 0
    assert "Forgot 1 messages" in capsys.readouterr().out
    left = {m.gmail_id for m in gyb.iter_backup(folder)}
    assert "l1" not in left and {"p0", "p1"} <= left  # offloaded mail is --batch's, not this
    con = sqlite3.connect(sidecar.meta_path(folder))
    assert con.execute("SELECT gmail_id, batch, rule FROM forgotten").fetchall() == [
        ("l1", "", forget.DELETED)
    ]
    con.close()
    (run_dir,) = forget.runs_dir(A).iterdir()  # held, so --undo and --empty work on it
    assert forget.held(A) == [(run_dir.name, 1)]
    assert gmail.closed  # the listing logged out


def test_deleted_with_batch_forgets_both_in_one_run(deleted, monkeypatch, capsys):
    folder, _gmail = deleted
    monkeypatch.setattr("builtins.input", lambda prompt: "3")
    assert cli.main(["forget", "--batch", BATCH, "--deleted", "--force", "--yes"]) == 0
    assert "Forgot 3 messages" in capsys.readouterr().out
    assert not {"p0", "p1", "l1"} & {m.gmail_id for m in gyb.iter_backup(folder)}
    assert forget.held(A) == [(next(forget.runs_dir(A).iterdir()).name, 3)]
    from mailcellar.backup import chain

    links = [chain.parse_line(line) for line in chain.read_lines(folder)]
    assert [link.op for link in links].count("forget") == 1  # one link for the run
    assert {g for g, _h in links[-1].removed} == {"p0", "p1", "l1"}
    assert '"(deleted)"' in links[-1].note and '"notices"' in links[-1].note


def test_deleted_mail_leaves_no_trace_in_the_indexes_or_plans(deleted, monkeypatch):
    def dump(store):
        con = sqlite3.connect(next(data_root().rglob(store)))
        text = "\n".join(con.iterdump())
        con.close()
        return text

    stores = ("meta.sqlite", "search.sqlite", "dedup.sqlite")  # msg-db.sqlite has neither
    there = {s: "l1@x" in dump(s) or "Lunch l1" in dump(s) for s in stores}
    assert there == dict.fromkeys(stores, True), there  # there to begin with
    monkeypatch.setattr("builtins.input", lambda prompt: "1")
    assert cli.main(["forget", "--deleted", "--force", "--yes"]) == 0
    for store in stores:
        assert "l1@x" not in dump(store) and "Lunch l1" not in dump(store), store
    plan = {r["gmail_id"]: r for r in read_csv(data_root() / "offload" / "offload-plan.csv")}
    assert (plan["l1"]["sender"], plan["l1"]["subject"]) == ("", "")
    assert plan["l0"]["subject"] == "Lunch l0"  # offloaded, not forgotten: kept


def test_a_refused_deleted_part_leaves_the_batch_to_go_ahead(deleted, monkeypatch, capsys):
    folder, _gmail = deleted
    monkeypatch.setattr("builtins.input", lambda prompt: "2")
    assert cli.main(["forget", "--batch", BATCH, "--deleted", "--yes"]) == 1  # no --force
    captured = capsys.readouterr()
    assert "more than 10%" in captured.err and "--deleted is skipped" in captured.err
    assert "Forgot 2 messages" in captured.out
    left = {m.gmail_id for m in gyb.iter_backup(folder)}
    assert "l1" in left and not {"p0", "p1"} & left


def test_a_mailbox_that_cant_be_listed_is_an_error_not_a_traceback(deleted, monkeypatch, capsys):
    from mailcellar import gmailapi

    def refused(config):
        def open_(account, scope):
            raise gmailapi.NeedsConsent("the readonly token has expired")

        return open_

    monkeypatch.setattr(mailbox, "opener", refused)
    assert cli.main(["forget", "--deleted", "--force"]) == 1
    err = capsys.readouterr().err
    assert "listing Gmail: the readonly token has expired; nothing is forgotten" in err


def test_force_needs_deleted(env):
    with pytest.raises(SystemExit, match="--force only applies with --deleted"):
        run("--batch", BATCH, "--force")


def test_deleted_refuses_more_than_a_tenth_without_force(deleted, capsys):
    folder, _gmail = deleted
    assert cli.main(["forget", "--deleted"]) == 1
    assert "more than 10%; re-run with --force" in capsys.readouterr().err
    assert "l1" in {m.gmail_id for m in gyb.iter_backup(folder)}


def test_deleted_forgets_nothing_when_folders_couldnt_be_read(deleted, monkeypatch, capsys):
    folder, gmail = deleted
    monkeypatch.setattr(gmail, "unread", lambda: ("Archive",))
    assert cli.main(["forget", "--deleted", "--force"]) == 1
    assert "1 folders couldn't be read (Archive)" in capsys.readouterr().err
    assert forget.held(A) == []


def test_deleted_forgets_nothing_when_the_mailbox_lists_nothing(deleted, capsys):
    _folder, gmail = deleted
    gmail.messages.clear()
    assert cli.main(["forget", "--deleted", "--force"]) == 1
    assert "Gmail listed no messages at all; nothing is forgotten" in capsys.readouterr().err


def test_deleted_leaves_held_mail(deleted, monkeypatch, capsys):
    monkeypatch.setattr(forget_cli.holds, "held", lambda folder, now, ids: {"l1": "audit"})
    assert cli.main(["forget", "--deleted", "--force"]) == 0
    out = capsys.readouterr().out
    assert "held                                   1  (hold audit; left as they are)" in out
    assert "Dry run" not in out  # nothing left to forget


def test_a_lookup_with_folders_unread_checks_nothing(monkeypatch, tmp_path):
    box = FakeMailbox({})  # p0 isn't listed: gone, but only if every folder was read
    monkeypatch.setattr(box, "unread", lambda: ("Archive",))
    monkeypatch.setattr(mailbox, "opener", lambda config: lambda account, scope: box)
    live, unread = forget_cli.gmail_state(A, tmp_path, ["p0"])
    assert live == {} and unread == {"p0": "1 folders couldn't be read"}


def test_deleted_candidates_years_and_note():
    chosen = forget.deleted(["a", "b"], {"a": (1_262_304_000_000, 10)})  # 2010-01-01
    assert chosen[0] == Offloaded("a", "", forget.DELETED, None, 1_262_304_000_000, 10)
    assert chosen[1].internal_date is None
    assert forget.by_year(chosen) == [("2010", 1), ("unknown", 1)]
    note = forget.note(chosen, {"notices": 7})  # --due with --deleted: no forget_after for it
    assert note == '{"rules": ["(deleted)"], "forget_after": {}}'


# ── retention: forget_after and --due (#72) ──────────────────────────────────


def dated(gid, rule="notices", date="2010-06-01", gone=True):
    ms = int(datetime.strptime(date, "%Y-%m-%d").timestamp() * 1000)
    return Offloaded(gid, BATCH, rule, 5_000 if gone else None, ms, 100)


def test_due_by_age_takes_mail_older_than_its_rules_cut_off():
    cut = int(datetime(2019, 1, 1).timestamp() * 1000)
    found = [
        dated("old"),
        dated("new", date="2024-01-01"),
        dated("other", rule="lunch"),  # its rule has no forget_after
        Offloaded("undated", BATCH, "notices", 5_000, None, 1),
    ]
    assert [c.gmail_id for c in forget.due_by_age(found, {"notices": cut})] == ["old"]


def test_retention_counts_and_breakdown():
    due_now = [
        dated("a"),
        dated("b", gone=False),
        dated("c"),
        dated("d", rule="lunch", date="2011-02-01"),
    ]
    assert forget.retention_counts(due_now, {"c": "audit"}) == {
        "lunch": (1, 0, 0),
        "notices": (1, 1, 1),
    }
    assert forget.by_rule_and_year(due_now) == [("lunch", "2011", 1), ("notices", "2010", 3)]
    assert forget.note(due_now, {"notices": 7, "lunch": 2, "x": 1}) == (
        '{"rules": ["lunch", "notices"], "forget_after": {"lunch": 2, "notices": 7}}'
    )
    assert forget.note(due_now) == '{"rules": ["lunch", "notices"]}'


def date_messages(folder, dates):
    con = sidecar.open_meta(folder)
    for gid, date in dates.items():
        ms = int(datetime.strptime(date, "%Y-%m-%d").timestamp() * 1000)
        con.execute("UPDATE messages SET internal_date = ? WHERE gmail_id = ?", (ms, gid))
    con.commit()
    con.close()


@pytest.fixture
def retained(env):
    """env, with p0 and p2 dated 2010, p1 2025, p3 2011; notices forgotten after 5 years."""
    folder, gmail = env
    date_messages(folder, {"p0": "2010-03-01", "p1": "2025-03-01", "p2": "2010-04-01"})
    date_messages(folder, {"p3": "2011-01-01", "l0": "2001-01-01"})
    rules = data_root() / "offload" / "rules.toml"
    rules.write_text(
        '[[rule]]\nname = "notices"\nbefore = 1\nsubject = "notice"\nforget_after = 5\n'
        '[[rule]]\nname = "lunch"\nbefore = 1\nsubject = "lunch"\n'
    )
    return folder, gmail


def test_due_dry_run_lists_what_retention_would_take(retained, capsys):
    folder, _gmail = retained
    run("--due")
    out = capsys.readouterr().out
    assert "3 offloaded messages due under retention" in out  # p0, p2, p3; p1 is too new
    assert "notices (5 years), 2010         2" in out and "notices (5 years), 2011" in out
    assert "can be forgotten                       1" in out  # p0
    assert "Gmail has them again" in out and "not yet gone from Gmail" in out  # p2, p3
    assert "Dry run" in out and "p0" in {m.gmail_id for m in gyb.iter_backup(folder)}


def test_due_forgets_only_what_is_due_and_says_why_in_the_chain(retained, monkeypatch, capsys):
    from mailcellar.backup import chain

    folder, _gmail = retained
    run("--due", "--yes", typed="1", monkeypatch=monkeypatch)
    left = {m.gmail_id for m in gyb.iter_backup(folder)}
    assert "p0" not in left and {"p1", "p2", "p3", "l0"} <= left  # l0's rule has no forget_after
    link = chain.parse_line(chain.read_lines(folder)[-1])
    assert link.op == "forget" and link.removed[0][0] == "p0"
    assert link.note == '{"rules": ["notices"], "forget_after": {"notices": 5}}'


def test_a_legal_hold_keeps_mail_retention_would_take(retained, monkeypatch, capsys):
    folder, _gmail = retained
    cli.main(["hold", "add", "audit", "--reason", "r", "--id", "p0"])
    capsys.readouterr()
    run("--due", "--yes", typed="1", monkeypatch=monkeypatch)
    out = capsys.readouterr().out
    assert "held" in out and "audit" in out and "Forgot" not in out
    assert "p0" in {m.gmail_id for m in gyb.iter_backup(folder)}
    now = int(datetime.now().timestamp() * 1000)
    assert forget.retention(folder, now) == {
        "notices": (5, 1, 1, 1)
    }  # p2 due, p3 not gone, p0 held


def test_retention_reports_what_is_due_without_asking_gmail(retained):
    folder, _gmail = retained
    now = int(datetime.now().timestamp() * 1000)
    # p0 and p2 are recorded gone (p2 is back in Gmail, which only --due finds out); p3 isn't.
    assert forget.retention(folder, now) == {"notices": (5, 2, 1, 0)}
    from mailcellar.report import health

    (row,) = health.retention_table([folder], now).rows
    assert row == (A, "notices", "5 years", 2, 1, 0)


@pytest.mark.parametrize(
    ("args", "error"),
    [
        (("--due", "--batch", BATCH), "can't take --batch"),
        (("--due", "--rule", "lunch"), "no rule with forget_after named lunch"),
    ],
)
def test_due_refuses_what_would_widen_it(retained, args, error):
    with pytest.raises(SystemExit, match=error):
        run(*args)


def test_due_needs_a_rule_with_forget_after(env):
    with pytest.raises(SystemExit, match="no rules file"):
        run("--due")
    (data_root() / "offload" / "rules.toml").write_text(
        '[[rule]]\nname = "lunch"\nbefore = 1\nsubject = "lunch"\n'
    )
    with pytest.raises(SystemExit, match="has forget_after"):
        run("--due")


def test_the_review_line_says_when_a_rules_mail_is_forgotten(tmp_path):
    from mailcellar.offload.review import rule_text
    from mailcellar.offload.rules import load_rules
    from mailcellar.report.cli import parse_before

    path = tmp_path / "rules.toml"
    path.write_text('[[rule]]\nname = "x"\nbefore = 2\nlabel = "a"\nforget_after = 7\n')
    (rule,) = load_rules(path, parse_before, lambda a: a)
    assert rule_text(rule).endswith("forgotten from the backup 7 years after its date")


def test_retention_reports_say_when_the_rules_cant_be_read(env, caplog):
    folder, _gmail = env
    assert forget.policies() == {}  # no rules file: nothing to say
    (data_root() / "offload" / "rules.toml").write_text('[[rule]]\nname = "x"\nlable = "a"\n')
    assert forget.policies() == {} and forget.policies() == {}  # health asks once per backup
    assert caplog.text.count("can't read the offload rules") == 1  # said once


def test_an_interrupted_forget_changes_neither_database_and_a_rerun_completes(
    env, monkeypatch, capsys
):
    folder, _gmail = env
    real_move, moves = gyb.shutil.move, []

    def stopping_move(src, target):
        if moves:
            raise KeyboardInterrupt
        moves.append(src)
        return real_move(src, target)

    monkeypatch.setattr(gyb.shutil, "move", stopping_move)
    monkeypatch.setattr(forget, "RUN_FORMAT", "run-1")  # the rerun gets a folder of its own
    with pytest.raises(KeyboardInterrupt):
        run("--batch", BATCH, "--yes", typed="2", monkeypatch=monkeypatch)
    in_backup = {m.gmail_id for m in gyb.iter_backup(folder)} | {
        g for (g,) in sqlite3.connect(folder / gyb.DB_NAME).execute("SELECT uid FROM uids")
    }
    meta = sqlite3.connect(sidecar.meta_path(folder))
    in_meta = {g for (g,) in meta.execute("SELECT gmail_id FROM messages")}
    assert {"p0", "p1"} <= in_backup and {"p0", "p1"} <= in_meta  # the two still agree
    assert meta.execute("SELECT count(*) FROM forgotten").fetchone()[0] == 0
    meta.close()

    monkeypatch.setattr(gyb.shutil, "move", real_move)
    monkeypatch.setattr(forget, "RUN_FORMAT", "run-2")
    run("--batch", BATCH, "--yes", typed="2", monkeypatch=monkeypatch)
    assert "Forgot 2 messages" in capsys.readouterr().out
    assert not {"p0", "p1"} & {m.gmail_id for m in gyb.iter_backup(folder)}
    (run_dir,) = forget.runs_dir(A).iterdir()  # the stopped run's folder is gone
    assert all(p.stat().st_nlink == 1 for p in run_dir.rglob("*.eml"))  # --empty frees them


def test_forget_rows_name_the_mailbox_by_provider():
    choice = forget.Choice(
        forget=[], not_gone=["a"], in_gmail=["b"], held=[], unchecked=[("c", "timed out")]
    )
    rows = forget_cli.forget_rows(choice, "imap")
    assert rows[0] == "  can be forgotten                       0"
    assert rows[1].startswith("  not yet gone from the server           1")
    assert "after the server empties its Trash" in rows[1]
    assert rows[2].startswith("  The server has them again              1")
    assert rows[3].endswith("(asking the server, e.g. timed out; tried again next run)")
    assert not any("Gmail" in r for r in rows)


def test_an_unreadable_sidecar_stops_forget_with_an_error(env, monkeypatch):
    def unreadable(folder):
        raise sidecar.MetaError("meta.sqlite can't be read: file is not a database")

    monkeypatch.setattr(forget_cli.sidecar, "provider", unreadable)
    with pytest.raises(SystemExit, match="error: meta.sqlite can't be read"):  # not a traceback
        run("--batch", BATCH)


@pytest.mark.parametrize(
    "providers, said",
    [
        ({"gmail"}, "Gmail frees their space when it empties its Trash, after 30 days."),
        ({"imap"}, "The server frees their space when its Trash is emptied."),
        ({"gmail", "imap"}, "each mailbox's Trash is emptied (Gmail's after 30 days)"),
        ({""}, "each mailbox's Trash is emptied"),  # a sidecar that couldn't be read
    ],
)
def test_the_space_line_names_only_the_mailboxes_trashed_in(providers, said):
    assert said in offload_cli.space_freed(providers)


def test_providers_come_from_each_backup_or_are_unknown(tmp_path):
    (tmp_path / "bad").mkdir()
    sidecar.meta_path(tmp_path / "bad").write_bytes(b"not a database")
    assert offload_cli.providers_of({"a": tmp_path / "none", "b": tmp_path / "bad"}) == {
        "a": "gmail",  # no sidecar: a Gmail backup, as sidecar.provider has it
        "b": "",
    }
