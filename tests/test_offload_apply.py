"""offload apply: backup checks, label → trash, the offload record."""

import csv
import sqlite3
from pathlib import Path

import pytest

from fake_mailbox import FakeMailbox, raw_response
from mailcellar import gmailapi
from mailcellar.backup import commands, sidecar
from mailcellar.dedup.apply import Result
from mailcellar.offload import apply as offload_apply
from mailcellar.offload import cli
from mailcellar.offload.apply import OFFLOAD_LABEL, second_copy_report

A = "a@x.com"
OLD = 1_300_000_000_000  # 2011-03-13
LABELS = {"INBOX": "INBOX", "Label_1": "work"}
RULES = '[[rule]]\nname = "work"\nlabel = "work"\nbefore = "2020-01-01"\n'


def message(gid):
    headers = f"From: colleague@work.com\r\nSubject: s-{gid}\r\nMessage-ID: <{gid}@x>\r\n"
    body = (headers + f"\r\nbody {gid}\r\n").encode()
    return raw_response(gid, ms=OLD, label_ids=["Label_1"], body=body)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Five backed-up work messages, a plan for them, and a live Gmail that agrees."""
    monkeypatch.setattr(FakeMailbox, "LABELS", LABELS)
    monkeypatch.chdir(tmp_path)
    source = FakeMailbox({f"w{i}": message(f"w{i}") for i in range(5)})
    folder = tmp_path / "data" / A
    commands.backup(source, A, folder)
    (tmp_path / "rules.toml").write_text(RULES)
    cli.main(["plan", "--rules", "rules.toml", "--plan", "plan.csv"])
    set_planned("2000-01-01 00:00:00")  # the backup above counts as newer
    live = FakeMailbox.with_labels({f"w{i}": {"work"} for i in range(5)})
    scopes = []
    monkeypatch.setattr(gmailapi, "find_config_folder", lambda root, account: Path("cfg"))
    monkeypatch.setattr(
        gmailapi,
        "load_credentials",
        lambda f, account, scope, interactive=True: (
            scope if interactive else pytest.fail("signed in non-interactively")
        ),
    )

    def account(name, scope):
        scopes.append(scope)
        return live

    monkeypatch.setattr(gmailapi, "GmailAccount", account)
    monkeypatch.setattr(cli, "second_copy_report", lambda d, ms: ["(report)"])

    class Env:
        pass

    e = Env()
    e.folder, e.live, e.scopes, e.source = folder, live, scopes, source
    return e


def backup_checks(env):
    return offload_apply.BackupChecks({A: env.folder})


def set_planned(value):
    with open("plan.csv", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        r["planned"] = value
    with open("plan.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def apply(*args):
    cli.main(["apply", "--plan", "plan.csv", "--log", "log.csv", "--config", "cfg", *args])
    with open("log.csv", newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def last(log, n=5):
    return {r["gmail_id"]: (r["outcome"], r["detail"]) for r in log[-n:]}


def offloaded(folder):
    """gmail_id → rule of every offloaded message."""
    con = sqlite3.connect(folder / "meta.sqlite")
    return dict(con.execute("SELECT gmail_id, rule FROM offloaded"))


def test_dry_run_reads_only(env):
    log = apply()
    assert {o for o, _d in last(log).values()} == {"would label or trash"}
    assert env.scopes == ["readonly"]
    assert env.live.lookups == [("get", [f"w{i}" for i in range(5)])] and env.live.calls == []
    assert set(log[0]) == set(offload_apply.APPLY_LOG_COLUMNS) and log[0]["rule"] == "work"
    assert offloaded(env.folder) == {}


def test_backup_checks(env, capsys):
    next(env.folder.rglob("w0.eml")).unlink()
    eml = next(env.folder.rglob("w1.eml"))
    eml.write_bytes(eml.read_bytes().replace(b"body w1", b"body changed"))
    con = sidecar.open_meta(env.folder)
    con.execute("UPDATE messages SET parser_version = '1' WHERE gmail_id = 'w2'")
    sidecar.record_offloaded(con, ["w3"], "earlier", "work", 1)
    con.commit()
    con.close()
    assert last(apply(), 4) == {  # w3, already offloaded, isn't attempted at all
        "w0": ("skipped", "no local backup copy"),
        "w1": ("skipped", "backup copy differs from the sidecar"),
        "w2": ("skipped", "sidecar row is stale; run mailcellar meta"),
        "w4": ("would label or trash", ""),
    }
    assert "1 plan rows already done" in capsys.readouterr().err
    # offloaded by another run after this one read the plan: the check still catches it
    assert backup_checks(env)({"account": A, "gmail_id": "w3", "path": ""}) == ("already offloaded")


def test_label_then_trash_records_offloaded(env, capsys):
    apply("--label")
    assert env.scopes == ["modify"]
    assert all(OFFLOAD_LABEL in env.live.labels_of(g) for g in env.live.messages)
    env.live.set_labels("w0", env.live.labels_of("w0") - {OFFLOAD_LABEL})  # vetoed in Gmail
    env.live.set_labels("w1", env.live.labels_of("w1") | {"keep"})  # filed elsewhere since the plan
    log = apply("--trash", "--yes")
    assert last(log) == {
        "w0": ("skipped", f"not labelled {OFFLOAD_LABEL}"),
        "w1": ("skipped", "labels changed since plan: +keep"),
        "w2": ("trashed", ""),
        "w3": ("trashed", ""),
        "w4": ("trashed", ""),
    }
    assert offloaded(env.folder) == {"w2": "work", "w3": "work", "w4": "work"}
    out = capsys.readouterr().out
    assert "the only copy" in out and "(report)" in out and "Recorded 3 offloaded" in out

    # the batch manifest lists what was trashed, as recorded in the sidecar
    batch, rows = manifest_rows(env)
    assert list(rows[0]) == offload_apply.MANIFEST_COLUMNS
    assert [r["gmail_id"] for r in rows] == ["w2", "w3", "w4"]
    assert {r["batch"] for r in rows} == {batch} and all(r["trashed"] for r in rows)
    assert all(Path(r["path"]).is_file() for r in rows)  # plan paths are absolute
    con = sqlite3.connect(env.folder / "meta.sqlite")
    assert {b for (b,) in con.execute("SELECT DISTINCT batch FROM offloaded")} == {batch}

    # once Gmail empties its Trash, --prune keeps them in the backup
    for gid in ("w2", "w3", "w4"):
        del env.source.messages[gid]
    result = commands.prune(env.source, A, env.folder, confirm=lambda p: True)
    assert result["pruned"] == 0
    assert all(next(env.folder.rglob(f"{g}.eml"), None) for g in ("w2", "w3", "w4"))


def test_trash_declined_records_nothing(env, monkeypatch):
    apply("--label")
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    log = apply("--trash")
    assert {o for o, _d in last(log).values()} == {"skipped"}
    assert not env.live.trashed
    assert offloaded(env.folder) == {}


def test_trash_needs_a_good_backup_after_the_plan(env, capsys):
    set_planned("2999-01-01 00:00:00")
    log = apply("--label")  # labelling only warns
    assert {o for o, _d in last(log).values()} == {"labelled"}
    assert "before the plan was made" in capsys.readouterr().err
    with pytest.raises(SystemExit, match="run a backup"):
        apply("--trash", "--yes")
    assert offloaded(env.folder) == {}

    # a newer backup that failed doesn't count either
    set_planned("2000-01-01 00:00:00")
    con = sidecar.open_meta(env.folder)
    sidecar.record_run(con, "backup", 2, 3, "failures", "history")
    con.execute("UPDATE runs SET finished_ms = 9e15 WHERE status = 'failures'")
    con.commit()
    con.close()
    with pytest.raises(SystemExit, match="run a backup"):
        apply("--trash", "--yes")
    assert offloaded(env.folder) == {}


def test_filters(env):
    assert len(apply("--limit", "2")) == 2
    with pytest.raises(SystemExit, match="no plan rows match"):
        apply("--rule", "other")


def test_label_already_on_planned_mail_is_refused(env):
    with pytest.raises(SystemExit, match="already had the label"):
        apply("--label", "work")


def test_second_copy_report(monkeypatch, tmp_path):
    monkeypatch.setattr(offload_apply.sys, "platform", "darwin")
    monkeypatch.setattr(offload_apply.shutil, "which", lambda name: "/usr/bin/tmutil")
    answers = {
        "destinationinfo": (0, "Name          : backup\nKind          : Network"),
        "isexcluded": (0, f"[Excluded]  {tmp_path}"),
        "latestbackup": (0, "/Volumes/backup/2026-10-20-101500.previous"),
    }
    monkeypatch.setattr(offload_apply, "_tmutil", lambda *a: answers[a[0]])
    lines = second_copy_report(tmp_path, 1_792_800_000_000)  # 2026-10-24
    assert lines[0] == "Time Machine: backing up to backup."
    assert "EXCLUDED" in lines[1]
    assert "2026-10-20 10:15" in lines[2] and "older than the last mail backup" in lines[3]

    answers["latestbackup"] = (1, "tmutil: latestbackup requires Full Disk Access privileges.")
    answers["isexcluded"] = (0, f"[Included]  {tmp_path}")
    lines = second_copy_report(tmp_path, None)
    assert "is included" in lines[1] and "unknown (tmutil: latestbackup requires" in lines[2]

    answers["destinationinfo"] = (0, "tmutil: No destinations configured.")
    assert second_copy_report(tmp_path, None) == ["Time Machine: no backup destination configured."]

    monkeypatch.setattr(offload_apply.sys, "platform", "linux")
    assert "not available" in second_copy_report(tmp_path, None)[0]


def outcomes(log, outcome):
    return sorted(r["gmail_id"] for r in log if r["outcome"] == outcome)


def runs_of(*args):
    """Each run's log rows only (the log is appended to)."""
    seen = 0
    for a in args:
        log = apply(*a)
        yield log[seen:]
        seen = len(log)


def test_trash_batches_advance(env):
    apply("--label")
    batches = list(runs_of(*[("--trash", "--yes", "--limit", "2")] * 3))
    assert [outcomes(b, "trashed") for b in batches] == [["w0", "w1"], ["w2", "w3"], ["w4"]]
    assert set(offloaded(env.folder)) == {f"w{i}" for i in range(5)}
    with pytest.raises(SystemExit, match="no plan rows match"):
        apply("--trash", "--yes", "--limit", "2")


def test_label_batches_advance_and_vetoes_are_not_retried(env):
    batches = list(runs_of(*[("--label", "--limit", "2")] * 3))
    assert [outcomes(b, "labelled") for b in batches] == [["w0", "w1"], ["w2", "w3"], ["w4"]]
    env.live.set_labels("w0", env.live.labels_of("w0") - {OFFLOAD_LABEL})  # vetoed in Gmail
    first, second = runs_of(("--trash", "--yes", "--limit", "2"), ("--trash", "--yes"))
    assert outcomes(first, "skipped") == ["w0"] and outcomes(first, "trashed") == ["w1"]
    assert outcomes(second, "trashed") == ["w2", "w3", "w4"]  # w0 isn't tried again
    assert "w0" not in [r["gmail_id"] for r in second]
    with pytest.raises(SystemExit, match="no plan rows match"):
        apply("--label")  # w0 was labelled once; the veto stands


def test_trash_reads_only_the_messages_a_label_run_labelled(env):
    apply("--label", "--limit", "2")
    env.live.events.clear()
    log = apply("--trash", "--yes")
    assert env.live.events[0] == ("get", ["w0", "w1"])  # w2–w4 were never labelled
    assert outcomes(log[-2:], "trashed") == ["w0", "w1"]
    with pytest.raises(SystemExit, match="no plan rows match"):
        apply("--trash", "--yes")  # nothing labelled is left


def test_a_message_gmail_could_not_read_is_tried_again_next_run(env):
    apply("--label")
    env.live.fail_lookup = {"w1"}
    first = apply("--trash", "--yes")[-5:]
    assert outcomes(first, "trashed") == ["w0", "w2", "w3", "w4"]
    assert outcomes(first, "skipped") == ["w1"]
    env.live.fail_lookup = set()
    second = apply("--trash", "--yes")
    assert [(r["gmail_id"], r["outcome"]) for r in second[-1:]] == [("w1", "trashed")]


def test_messages_trashed_before_a_crash_are_recorded(env, monkeypatch, capsys):
    apply("--label")

    def crash(folders, batch, manifest):
        def on_trashed(account, rows):
            raise RuntimeError("killed after trash()")

        return on_trashed

    real = cli.recorder
    monkeypatch.setattr(cli, "recorder", crash)
    with pytest.raises(RuntimeError):
        apply("--trash", "--yes")
    assert env.live.trashed == set(env.live.messages)
    assert offloaded(env.folder) == {}
    monkeypatch.setattr(cli, "recorder", real)

    dry = apply()[-5:]
    assert {r["outcome"] for r in dry} == {"would record (already in trash)"}
    assert offloaded(env.folder) == {}

    next(env.folder.rglob("w0.eml")).unlink()  # recording still needs the backup copy
    log = apply("--trash", "--yes")[-5:]
    assert outcomes(log, "recorded") == ["w1", "w2", "w3", "w4"]
    w0 = next(r for r in log if r["gmail_id"] == "w0")
    assert w0["outcome"] == "skipped" and "restore it from its mailbox's Trash" in w0["detail"]
    assert set(offloaded(env.folder)) == {"w1", "w2", "w3", "w4"}
    assert "Recorded 4 messages an earlier run trashed" in capsys.readouterr().out


def test_trashed_by_hand_without_the_label_is_not_recorded(env):
    apply("--label")
    env.live.set_labels("w0", {"work", "TRASH"})  # unlabelled and trashed in Gmail by hand
    log = apply("--trash", "--yes")[-5:]
    w0 = next(r for r in log if r["gmail_id"] == "w0")
    assert (w0["outcome"], w0["detail"]) == ("skipped", "already in trash")
    assert "w0" not in offloaded(env.folder)


def test_a_plan_made_after_labelling_can_still_trash(env):
    apply("--label")
    with open("plan.csv", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:  # as a re-plan after a backup would see them
        r["labels"] = f"work;{OFFLOAD_LABEL}"
    rows.append({**rows[0], "gmail_id": "x9", "labels": f"work;{OFFLOAD_LABEL}"})
    with open("plan.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows[:-1])
    log = apply("--trash", "--yes")
    assert outcomes(log[-5:], "trashed") == [f"w{i}" for i in range(5)]

    with open("plan.csv", "a", newline="", encoding="utf-8") as fh:
        csv.DictWriter(fh, fieldnames=list(rows[0])).writerow(rows[-1])
    with pytest.raises(SystemExit, match="already had the label"):  # not labelled by us
        apply("--label")


def manifest_rows(env):
    (folder,) = (env.folder.parent / "offload" / "batches").iterdir()
    with open(folder / "offloaded.csv", newline="", encoding="utf-8") as fh:
        return folder.name, list(csv.DictReader(fh))


def test_recovered_messages_go_into_the_recovering_batchs_manifest(env, monkeypatch):
    apply("--label")

    def crash(folders, batch, manifest):
        def on_trashed(account, rows):
            raise RuntimeError("killed after trash()")

        return on_trashed

    real = cli.recorder
    monkeypatch.setattr(cli, "recorder", crash)
    with pytest.raises(RuntimeError):
        apply("--trash", "--yes")
    monkeypatch.setattr(cli, "recorder", real)
    apply("--trash", "--yes")
    _batch, rows = manifest_rows(env)
    assert sorted(r["gmail_id"] for r in rows) == [f"w{i}" for i in range(5)]


def test_apply_log_names_the_plan_and_old_logs_are_upgraded(env):
    old = [
        "time",
        "mode",
        "account",
        "gmail_id",
        "rule",
        "outcome",
        "detail",
        "date",
        "sender",
        "subject",
    ]
    with open("log.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=old)
        w.writeheader()
        w.writerow(
            {
                "time": "t0",
                "mode": "dry-run",
                "account": A,
                "gmail_id": "w9",
                "rule": "work",
                "outcome": "would label or trash",
            }
        )
    log = apply()
    assert list(log[0]) == offload_apply.APPLY_LOG_COLUMNS
    assert (log[0]["gmail_id"], log[0]["planned"]) == ("w9", "")  # kept, planned unknown
    assert {r["planned"] for r in log[1:]} == {"2000-01-01 00:00:00"}


# ── backup checks and their failure paths ────────────────────────────────────


def test_a_message_missing_from_the_sidecar_is_skipped_with_the_reason(env):
    con = sidecar.open_meta(env.folder)
    con.execute("DELETE FROM messages WHERE gmail_id = 'w0'")
    con.commit()
    con.close()
    assert last(apply(), 5)["w0"] == ("skipped", "not in the sidecar; run mailcellar meta")
    row = {"account": A, "gmail_id": "w0", "path": ""}
    assert backup_checks(env)(row) == "not in the sidecar; run mailcellar meta"


def test_an_unparseable_backup_copy_is_skipped_with_the_reason(env, monkeypatch):
    real = offload_apply.parse_eml

    def parse(path):
        if path.name == "w1.eml":
            raise ValueError("bad bytes")
        return real(path)

    monkeypatch.setattr(offload_apply, "parse_eml", parse)
    got = last(apply(), 5)
    assert got["w1"] == ("skipped", "backup copy doesn't parse: bad bytes")
    assert got["w0"][0] == "would label or trash"  # the others are still checked


def test_an_account_whose_backup_was_not_chosen_is_refused():
    checks = offload_apply.BackupChecks({})
    assert checks({"account": A, "gmail_id": "w0", "path": ""}) == "account's backup not chosen"


def test_an_account_with_no_backup_run_recorded_is_stale(env):
    assert offload_apply.stale_backups({A: env.folder}, [A], 0) == {}
    con = sidecar.open_meta(env.folder)
    con.execute("DELETE FROM runs")
    con.commit()
    con.close()
    assert offload_apply.stale_backups({A: env.folder}, [A], 0) == {A: "no backup run recorded"}


def test_trashing_is_refused_when_no_backup_run_is_recorded(env, capsys):
    apply("--label")
    con = sidecar.open_meta(env.folder)
    con.execute("DELETE FROM runs")
    con.commit()
    con.close()
    with pytest.raises(SystemExit, match="run a backup of each account"):
        apply("--trash", "--yes")
    assert "no backup run recorded" in capsys.readouterr().err
    assert env.live.labels_of("w0") == {"work", OFFLOAD_LABEL}  # nothing was trashed


def test_a_file_that_is_not_an_offload_plan_is_refused(tmp_path):
    path = tmp_path / "plan.csv"
    path.write_text("account,gmail_id\na@x.com,w0\n")
    with pytest.raises(SystemExit, match=r"is not an offload plan \(missing \["):
        offload_apply.read_plan(path)


def test_last_backup_is_the_newest_successful_run_of_any_account(env, tmp_path):
    other = tmp_path / "data" / "b@y.com"
    commands.backup(FakeMailbox({"b1": message("b1")}), "b@y.com", other)
    folders = {A: env.folder, "b@y.com": other}
    newest = offload_apply.last_backup_ms(folders, ["b@y.com", A])  # the older one comes second
    assert newest == offload_apply.last_backup_ms({"b@y.com": other}, ["b@y.com"])
    assert newest >= offload_apply.last_backup_ms({A: env.folder}, [A])
    con = sidecar.open_meta(other)
    con.execute("DELETE FROM runs")
    con.commit()
    con.close()
    assert offload_apply.last_backup_ms(folders, ["b@y.com"]) is None  # no successful run


def test_recording_nothing_writes_no_manifest(env, tmp_path):
    record = offload_apply.recorder({A: env.folder}, "batch", tmp_path / "manifest.csv")
    record(A, [])
    assert not (tmp_path / "manifest.csv").exists() and offloaded(env.folder) == {}


def test_a_label_run_leaves_messages_trashed_before_a_crash_alone(env):
    path = next(env.folder.rglob("w0.eml"))
    row = {"account": A, "gmail_id": "w0", "path": str(path)}
    entry = Result(row, "x", "", frozenset({"TRASH", OFFLOAD_LABEL}))
    recorded = []
    log, count = offload_apply.recover_trashed(
        [entry], "label", OFFLOAD_LABEL, lambda row: "", lambda a, rows: recorded.append(rows)
    )
    assert count == 0 and recorded == []
    assert log == [entry]


# ── recognising messages trashed before a crash ──────────────────────────────


def entry(live, gid="w0", path="x", outcome="ok", detail=""):
    row = {"account": A, "gmail_id": gid, "path": path}
    return Result(row, outcome, detail, None if live is None else frozenset(live))


@pytest.mark.parametrize(
    ("live", "expected"),
    [
        ({"TRASH", OFFLOAD_LABEL}, True),
        ({"TRASH", OFFLOAD_LABEL, "work"}, True),
        ({"TRASH"}, False),  # trashed by hand: never had, or lost, the label
        ({OFFLOAD_LABEL}, False),  # labelled, not trashed
        ({"INBOX"}, False),
        (set(), False),
        (None, False),  # no longer in Gmail
    ],
)
def test_trashed_before_needs_trash_and_the_label(live, expected):
    assert offload_apply.trashed_before(entry(live), OFFLOAD_LABEL) is expected


@pytest.mark.parametrize(
    ("mode", "problem", "outcome", "detail"),
    [
        ("trash", "", "recorded", "already in trash"),
        ("dry-run", "", "would record (already in trash)", ""),
        ("label", "", "ok", ""),  # a label run leaves it alone
        ("trash", "no local backup copy", "skipped", "in Trash, but no local backup copy; "),
        ("dry-run", "backup copy differs", "skipped", "in Trash, but backup copy differs; "),
        ("label", "backup copy differs", "skipped", "in Trash, but backup copy differs; "),
    ],
)
def test_recovered_by_mode_and_backup_problem(mode, problem, outcome, detail):
    before = entry({"TRASH", OFFLOAD_LABEL})
    after = offload_apply.recovered(before, mode, problem)
    assert after.outcome == outcome and after.detail.startswith(detail)
    assert after.row is before.row and before.outcome == "ok"  # a new value; the old is unchanged
    if problem:
        assert after.detail.endswith("restore it from its mailbox's Trash")


def test_recover_trashed_returns_a_new_log_and_changes_nothing(env):
    path = str(next(env.folder.rglob("w0.eml")))
    other = str(next(env.folder.rglob("w1.eml")))
    log = [
        entry({"INBOX"}, "w0", path),  # not trashed: passes through as it is
        entry({"TRASH", OFFLOAD_LABEL}, "w0", path),
        entry({"TRASH", OFFLOAD_LABEL}, "w1", other),
        entry({"TRASH", OFFLOAD_LABEL}, "w2", str(env.folder / "no-such.eml")),
    ]
    snapshot = list(log)
    recorded = []
    new, count = offload_apply.recover_trashed(
        log,
        "trash",
        OFFLOAD_LABEL,
        lambda row: "",
        lambda account, rows: recorded.append((account, [r["gmail_id"] for r in rows])),
    )
    assert [r.outcome for r in new] == ["ok", "recorded", "recorded", "skipped"]
    assert new[0] is log[0]  # an entry that needed nothing is the same value
    assert "no local backup copy" in new[3].detail
    assert count == 2 and recorded == [(A, ["w0", "w1"])]  # one call per account
    assert log == snapshot and [r.outcome for r in log] == ["ok"] * 4  # the old log is as it was


# ── the second-copy report ───────────────────────────────────────────────────


def test_tmutil_that_cannot_run_answers_with_a_failure(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("tmutil not found")

    monkeypatch.setattr(offload_apply.subprocess, "run", fail)
    assert offload_apply._tmutil("latestbackup") == (1, "tmutil not found")

    def slow(*args, **kwargs):
        raise offload_apply.subprocess.TimeoutExpired("tmutil", 20)

    monkeypatch.setattr(offload_apply.subprocess, "run", slow)
    code, why = offload_apply._tmutil("latestbackup")
    assert code == 1 and "timed out" in why


def test_tmutil_output_and_status_are_passed_on(monkeypatch):
    done = offload_apply.subprocess.CompletedProcess(["tmutil"], 0, stdout="out\n", stderr="err\n")
    monkeypatch.setattr(offload_apply.subprocess, "run", lambda *a, **k: done)
    assert offload_apply._tmutil("latestbackup") == (0, "out\nerr")


def test_second_copy_report_when_time_machine_answers_oddly(monkeypatch, tmp_path):
    monkeypatch.setattr(offload_apply.sys, "platform", "darwin")
    monkeypatch.setattr(offload_apply.shutil, "which", lambda name: "/usr/bin/tmutil")
    answers = {
        "destinationinfo": (0, "Name          : backup"),
        "isexcluded": (0, "something unexpected\nsecond line"),
        "latestbackup": (0, "/Volumes/backup/2026-10-26-101500.previous"),
    }
    monkeypatch.setattr(offload_apply, "_tmutil", lambda *a: answers[a[0]])
    lines = offload_apply.second_copy_report(tmp_path, 1_792_800_000_000)  # 2026-10-24
    assert "is unknown (something unexpected)" in lines[1]
    assert "2026-10-26 10:15" in lines[2]
    assert len(lines) == 3  # the backup is newer than the last Gmail backup: no warning

    answers["latestbackup"] = (0, "no date in this output")
    assert (
        "Latest Time Machine backup: unknown (no date in this output)"
        in (offload_apply.second_copy_report(tmp_path, None)[-1])
    )
