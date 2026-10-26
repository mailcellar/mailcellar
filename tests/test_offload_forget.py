"""offload forget (#9): offloaded mail Gmail no longer has, deleted from the backup in stages."""

import csv
import sqlite3
from datetime import datetime

import pytest

from mailcellar import cli
from mailcellar.backup import commands, gyb, sidecar
from mailcellar.dedup import db as dedup_db
from mailcellar.dedup import index as dedup_index
from mailcellar.dedup.paths import default_db as dedup_path
from mailcellar.offload import cli as offload_cli
from mailcellar.offload import forget
from mailcellar.offload.forget import Offloaded, choose, due, scrub_rows
from mailcellar.paths import data_root
from mailcellar.report.db import open_sidecars
from mailcellar.report.storage import offload_batches
from mailcellar.restore.apply import candidates as restore_candidates
from mailcellar.search import db as search_db
from mailcellar.search import index as search_index
from test_backup import FakeGmail, raw_response

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
    gmail = FakeGmail({g: raw_response(g, body=eml(g, p)) for g, p in gids.items()})
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

    monkeypatch.setattr(offload_cli, "gmail_state", live)
    monkeypatch.setattr(offload_cli, "second_copy_report", lambda d, ms: ["(report)"])
    return folder, gmail


def run(*args, typed=None, monkeypatch=None):
    if typed is not None:
        monkeypatch.setattr("builtins.input", lambda prompt: typed)
    cli.main(["offload", "forget", *args])


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


def test_forget_needs_a_selection(env):
    with pytest.raises(SystemExit, match="say which offloaded mail"):
        run()
