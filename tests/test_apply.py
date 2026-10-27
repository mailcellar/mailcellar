import copy
import csv
import dataclasses

import pytest

from fake_mailbox import FakeMailbox
from mailcellar.dedup import apply as dedup_apply
from mailcellar.dedup import cli as dedup
from mailcellar.dedup.apply import (
    DEDUP_LABEL,
    Result,
    apply_plan,
    check_row,
    unusable_label,
)
from mailcellar.dedup.plan import PLAN_COLUMNS

A, B = "a@example.com", "b@example.com"


def row(tmp_path, gid="b1", keeper="a1", labels="", exists=True):
    path = tmp_path / f"{gid}.eml"
    if exists:
        path.write_text("x")
    return {
        "action": "remove",
        "reason": "",
        "group_id": "1",
        "account": B,
        "gmail_id": gid,
        "keeper_account": A,
        "keeper_gmail_id": keeper,
        "message_id": "m@x",
        "internal_date": "",
        "sender": "",
        "subject": "s",
        "labels": labels,
        "size": "1",
        "path": str(path),
    }


def setup(a_msgs, b_msgs, **b_kwargs):
    gmail = {A: FakeMailbox.with_labels(a_msgs), B: FakeMailbox.with_labels(b_msgs, **b_kwargs)}
    scopes = {}

    def gmail_for(account, scope):
        scopes[account] = scope
        return gmail[account]

    return gmail, scopes, gmail_for


def outcomes(log):
    return {e.row["gmail_id"]: (e.outcome, e.detail) for e in log}


# ── check_row ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "cand, keeper, labels, mode, expected",
    [
        ({"INBOX"}, {"INBOX"}, "", "label", ""),
        (None, {"INBOX"}, "", "label", "message no longer in Gmail"),
        ({"TRASH"}, {"INBOX"}, "", "label", "already in trash"),
        ({"INBOX"}, None, "", "label", "keeper no longer in Gmail"),
        ({"INBOX"}, {"SPAM"}, "", "label", "keeper in trash or spam"),
        # system-label churn (read, archived, re-categorised) is not a change
        ({"UNREAD", "CATEGORY_UPDATES"}, set(), "INBOX;IMPORTANT", "label", ""),
        ({"garden"}, set(), "", "label", "labels changed since plan: +garden"),
        (
            {"INBOX"},
            set(),
            "work;YELLOW_STAR",
            "label",
            "labels changed since plan: -YELLOW_STAR,work",
        ),
        ({"SENT"}, set(), "", "label", "labels changed since plan: +SENT"),
        ({"INBOX"}, set(), "", "trash", f"not labelled {DEDUP_LABEL}"),
        ({"INBOX", DEDUP_LABEL}, set(), "", "trash", ""),
    ],
)
def test_check_row(tmp_path, cand, keeper, labels, mode, expected):
    assert check_row(row(tmp_path, labels=labels), mode, cand, keeper) == expected


def test_check_row_requires_local_backup(tmp_path):
    assert (
        check_row(row(tmp_path, exists=False), "label", {"INBOX"}, {"INBOX"})
        == "no local backup copy"
    )


# ── apply_plan ───────────────────────────────────────────────────────────────


def test_dry_run_reads_only(tmp_path):
    gmail, scopes, gmail_for = setup({"a1": set()}, {"b1": set()})
    log = apply_plan([row(tmp_path)], "dry-run", gmail_for, confirm=lambda p: True)
    assert outcomes(log) == {"b1": ("would label or trash", "")}
    assert scopes == {A: "readonly", B: "readonly"}
    assert all(g.calls == [] for g in gmail.values())  # only read
    assert all(g.closed for g in gmail.values())  # each mailbox let go of when done


def test_label_stage_labels_only_passing_rows(tmp_path):
    gmail, scopes, gmail_for = setup(
        {"a1": set(), "a2": {"TRASH"}}, {"b1": set(), "b2": set(), "b3": {"garden"}}
    )
    rows = [row(tmp_path, "b1", "a1"), row(tmp_path, "b2", "a2"), row(tmp_path, "b3", "a1")]
    log = apply_plan(rows, "label", gmail_for, confirm=lambda p: True)

    assert outcomes(log) == {
        "b1": ("labelled", ""),
        "b2": ("skipped", "keeper in trash or spam"),
        "b3": ("skipped", "labels changed since plan: +garden"),
    }
    assert scopes == {A: "readonly", B: "modify"}
    assert ("label", ["b1"], DEDUP_LABEL) in gmail[B].calls
    assert gmail[A].lookups == [("get", ["a1", "a2"])] and gmail[A].calls == []


def test_trash_stage_needs_label_and_confirmation(tmp_path):
    gmail, _scopes, gmail_for = setup({"a1": set()}, {"b1": {DEDUP_LABEL}, "b2": set()})
    rows = [row(tmp_path, "b1"), row(tmp_path, "b2")]
    prompts = []
    log = apply_plan(rows, "trash", gmail_for, confirm=lambda p: prompts.append(p) or True)

    assert outcomes(log) == {
        "b1": ("trashed", ""),
        "b2": ("skipped", f"not labelled {DEDUP_LABEL}"),
    }
    assert prompts == [f"Move 1 messages in {B} to Trash?"]
    assert "TRASH" in gmail[B].labels_of("b1")

    # declined: nothing is trashed
    gmail, _scopes, gmail_for = setup({"a1": set()}, {"b1": {DEDUP_LABEL}})
    log = apply_plan([row(tmp_path)], "trash", gmail_for, confirm=lambda p: False)
    assert outcomes(log) == {"b1": ("skipped", "not confirmed")}
    assert not any(c[0] == "trash" for c in gmail[B].calls)


def test_a_message_gmail_could_not_read_is_skipped_and_the_rest_carry_on(tmp_path, caplog):
    rows = [row(tmp_path, "b1", "a1"), row(tmp_path, "b2", "a2"), row(tmp_path, "b3", "a3")]
    gmail, _, gmail_for = setup(
        {"a1": set(), "a2": set(), "a3": set()},
        {"b1": set(), "b2": set(), "b3": set()},
        fail_lookup={"b2"},
    )
    gmail[A].fail_lookup = {"a3"}  # b3's keeper
    log = apply_plan(rows, "label", gmail_for, lambda p: True)
    by_id = {r.row["gmail_id"]: r for r in log}
    assert by_id["b1"].outcome == "labelled"
    assert (by_id["b2"].outcome, by_id["b3"].outcome) == ("skipped", "skipped")
    assert by_id["b2"].detail.startswith("couldn't read it from Gmail: HttpError 400")
    assert by_id["b3"].detail.startswith("couldn't read the keeper from Gmail")
    assert gmail[B].events[-1] == ("label", ["b1"], DEDUP_LABEL)  # nothing done on a guess
    assert "1 messages couldn't be read from Gmail" in caplog.text


def test_trash_failures_are_reported(tmp_path):
    gmail, _scopes, gmail_for = setup(
        {"a1": set()}, {"b1": {DEDUP_LABEL}, "b2": {DEDUP_LABEL}}, fail_trash={"b2"}
    )
    log = apply_plan(
        [row(tmp_path, "b1"), row(tmp_path, "b2")], "trash", gmail_for, confirm=lambda p: True
    )
    assert outcomes(log) == {
        "b1": ("trashed", ""),
        "b2": ("failed", "not in Trash after trashing (HttpError 500)"),
    }


def test_label_then_trash_round_trip(tmp_path):
    gmail, _scopes, gmail_for = setup({"a1": set()}, {"b1": {"INBOX"}})
    rows = [row(tmp_path, labels="INBOX")]
    apply_plan(rows, "label", gmail_for, confirm=lambda p: True)
    log = apply_plan(rows, "trash", gmail_for, confirm=lambda p: True)
    assert outcomes(log) == {"b1": ("trashed", "")}


# ── CLI ──────────────────────────────────────────────────────────────────────


def test_cli_limit_and_log(tmp_path, monkeypatch):
    plan = tmp_path / "plan.csv"
    rows = [row(tmp_path, f"b{i}") for i in range(3)]
    rows.append({**row(tmp_path, "b9"), "action": "keep"})
    with plan.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=PLAN_COLUMNS)
        w.writeheader()
        w.writerows(rows)

    gmail = {
        A: FakeMailbox.with_labels({"a1": set()}),
        B: FakeMailbox.with_labels({f"b{i}": set() for i in range(3)}),
    }
    from mailcellar import gmailapi

    monkeypatch.setattr(gmailapi, "find_config_folder", lambda root, acct: tmp_path)
    monkeypatch.setattr(
        gmailapi,
        "load_credentials",
        lambda folder, acct, scope, interactive=True: (
            None if interactive else pytest.fail("signed in non-interactively")
        ),
    )
    monkeypatch.setattr(gmailapi, "GmailAccount", lambda acct, creds: gmail[acct])

    log_path = tmp_path / "log.csv"
    dedup.main(["apply", "--plan", str(plan), "--limit", "2", "--label", "--log", str(log_path)])
    dedup.main(["apply", "--plan", str(plan), "--limit", "2", "--log", str(log_path)])

    with log_path.open(newline="") as fh:
        logged = [(r["mode"], r["gmail_id"], r["outcome"]) for r in csv.DictReader(fh)]
    assert logged == [
        ("label (dedup-redundant)", "b0", "labelled"),
        ("label (dedup-redundant)", "b1", "labelled"),
        # after labelling, the safety labels still match: the dedup label is ignored
        ("dry-run", "b0", "would label or trash"),
        ("dry-run", "b1", "would label or trash"),
    ]


# ── within-account rows: the keeper is in the same account ───────────────────


def test_within_account_rows_label_and_trash_in_one_account(tmp_path):
    gmail, scopes, gmail_for = setup({}, {"b1": set(), "b2": set()})
    rows = [{**row(tmp_path, "b2"), "keeper_account": B, "keeper_gmail_id": "b1"}]

    log = apply_plan(rows, "label", gmail_for, confirm=lambda p: True)
    assert outcomes(log) == {"b2": ("labelled", "")}
    assert scopes == {B: "modify"}  # A is never contacted
    assert gmail[B].lookups[0] == ("get", ["b1", "b2"])  # copy and keeper in one read

    log = apply_plan(rows, "trash", gmail_for, confirm=lambda p: True)
    assert outcomes(log) == {"b2": ("trashed", "")}
    assert "TRASH" in gmail[B].labels_of("b2") and "TRASH" not in gmail[B].labels_of("b1")


def test_within_account_keeper_trashed_meanwhile_blocks_removal(tmp_path):
    gmail, _scopes, gmail_for = setup({}, {"b1": {"TRASH"}, "b2": set()})
    rows = [{**row(tmp_path, "b2"), "keeper_account": B, "keeper_gmail_id": "b1"}]
    log = apply_plan(rows, "label", gmail_for, confirm=lambda p: True)
    assert outcomes(log) == {"b2": ("skipped", "keeper in trash or spam")}


# ── choosing the label ───────────────────────────────────────────────────────


def test_custom_label_is_added_ignored_in_comparisons_and_required_for_trash(tmp_path):
    gmail, _scopes, gmail_for = setup({"a1": set()}, {"b1": set()})
    rows = [row(tmp_path)]
    log = apply_plan(rows, "label", gmail_for, confirm=lambda p: True, dedup_label="to-trash")
    assert outcomes(log) == {"b1": ("labelled", "")}
    assert ("label", ["b1"], "to-trash") in gmail[B].calls

    # a trash run needs its own label …
    assert check_row(rows[0], "trash", {"INBOX"}, set(), "to-trash") == "not labelled to-trash"
    # … and another dedup label is just a user label added since the plan
    assert (
        check_row(rows[0], "trash", {DEDUP_LABEL}, set(), "to-trash")
        == f"labels changed since plan: +{DEDUP_LABEL}"
    )
    log = apply_plan(rows, "trash", gmail_for, confirm=lambda p: True, dedup_label="to-trash")
    assert outcomes(log) == {"b1": ("trashed", "")}


@pytest.mark.parametrize(
    "name, rows_labels, expected",
    [
        ("to-trash", ["INBOX", "work"], ""),
        ("TRASH", [""], "is a Gmail system label"),
        ("CATEGORY_UPDATES", [""], "is a Gmail system label"),
        ("  ", [""], "the label name is empty"),
        ("work", ["INBOX;work", "INBOX"], "1 planned messages already had the label 'work'"),
    ],
)
def test_unusable_label(tmp_path, name, rows_labels, expected):
    rows = [{**row(tmp_path, f"b{i}"), "labels": label} for i, label in enumerate(rows_labels)]
    problem = unusable_label(rows, name)
    assert (expected in problem) if expected else problem == ""


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """A plan with two removals, and the CLI wired to fake Gmail accounts."""
    plan = tmp_path / "plan.csv"
    with plan.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=PLAN_COLUMNS)
        w.writeheader()
        w.writerows([row(tmp_path, "b0"), {**row(tmp_path, "b1"), "labels": "INBOX;work"}])
    gmail = {
        A: FakeMailbox.with_labels({"a1": set()}),
        B: FakeMailbox.with_labels({"b0": set(), "b1": {"INBOX", "work"}}),
    }
    from mailcellar import gmailapi

    monkeypatch.setattr(gmailapi, "find_config_folder", lambda root, acct: tmp_path)
    monkeypatch.setattr(
        gmailapi,
        "load_credentials",
        lambda folder, acct, scope, interactive=True: (
            None if interactive else pytest.fail("signed in non-interactively")
        ),
    )
    monkeypatch.setattr(gmailapi, "GmailAccount", lambda acct, creds: gmail[acct])
    log = tmp_path / "log.csv"

    def run(*args):
        dedup.main(["apply", "--plan", str(plan), "--log", str(log), "--yes", *args])
        with log.open(newline="") as fh:
            return [(r["mode"], r["gmail_id"], r["outcome"]) for r in csv.DictReader(fh)]

    return gmail, run


def test_cli_label_and_trash_with_a_named_label(cli):
    gmail, run = cli
    run("--label", "to-trash")
    assert ("label", ["b0", "b1"], "to-trash") in gmail[B].calls
    logged = run("--trash", "to-trash")
    assert logged[-2:] == [
        ("trash (to-trash)", "b0", "trashed"),
        ("trash (to-trash)", "b1", "trashed"),
    ]


def test_cli_trash_with_the_default_label_skips_unlabelled(cli):
    gmail, run = cli
    run("--label", "to-trash")
    logged = run("--trash")  # default: dedup-redundant
    assert [o for *_x, o in logged[-2:]] == ["skipped", "skipped"]
    assert not any(c[0] == "trash" for c in gmail[B].calls)


@pytest.mark.parametrize("stage", ["--label", "--trash"])
def test_cli_refuses_a_label_planned_messages_already_had(cli, stage):
    gmail, run = cli
    with pytest.raises(SystemExit, match="already had the label 'work'"):
        run(stage, "work")
    assert gmail[B].calls == gmail[B].lookups == []  # Gmail never contacted


# ── results: frozen values, and the pure steps ───────────────────────────────


def ok(tmp_path, gid="b1", **kwargs):
    return Result(row(tmp_path, gid, **kwargs), "ok", "", frozenset({"INBOX"}))


def test_a_result_is_a_frozen_value(tmp_path):
    result = ok(tmp_path)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.outcome = "trashed"
    assert dataclasses.replace(result, outcome="trashed").outcome == "trashed"
    assert result.outcome == "ok"  # the copy is a new value


def test_the_steps_after_acting_name_each_outcome(tmp_path):
    results = [ok(tmp_path, "b1"), ok(tmp_path, "b2")]
    assert [r.outcome for r in dedup_apply.after_dry_run(results)] == ["would label or trash"] * 2
    assert [r.outcome for r in dedup_apply.after_label(results, {})] == ["labelled"] * 2
    failed = dedup_apply.after_label(results, {results[0].row["gmail_id"]: "refused"})
    assert [(r.outcome, r.detail) for r in failed][0] == ("failed", "refused")
    declined = dedup_apply.after_declined(results)
    assert [(r.outcome, r.detail) for r in declined] == [("skipped", "not confirmed")] * 2
    after = dedup_apply.after_trash(results, {"b2": "HttpError 500"})
    assert [(r.outcome, r.detail) for r in after] == [("trashed", ""), ("failed", "HttpError 500")]
    assert [r.outcome for r in results] == ["ok", "ok"]  # none of them changed the originals
    assert all(a.row is b.row for a, b in zip(after, results, strict=True))  # the same plan rows


def test_act_in_a_dry_run_touches_nothing(tmp_path):
    gmail = FakeMailbox.with_labels({"b1": set()})
    done = dedup_apply.act(
        "dry-run", B, [ok(tmp_path)], gmail, lambda p: 1 / 0, DEDUP_LABEL, lambda *a: 1 / 0
    )
    assert [r.outcome for r in done] == ["would label or trash"] and gmail.calls == []


def test_act_labels_the_ids_once(tmp_path):
    gmail = FakeMailbox.with_labels({"b1": set(), "b2": set()})
    results = [ok(tmp_path, "b1"), ok(tmp_path, "b2")]
    done = dedup_apply.act("label", B, results, gmail, lambda p: 1 / 0, "to-trash", None)
    assert [r.outcome for r in done] == ["labelled", "labelled"]
    assert gmail.calls == [("label", ["b1", "b2"], "to-trash")]


def test_act_asks_once_and_trashes_only_when_confirmed(tmp_path):
    results = [ok(tmp_path, "b1"), ok(tmp_path, "b2")]
    prompts, trashed = [], []
    gmail = FakeMailbox.with_labels({"b1": set(), "b2": set()}, fail_trash={"b2"})
    done = dedup_apply.act(
        "trash",
        B,
        results,
        gmail,
        lambda p: prompts.append(p) or True,
        DEDUP_LABEL,
        lambda account, rows: trashed.append((account, [r["gmail_id"] for r in rows])),
    )
    assert prompts == [f"Move 2 messages in {B} to Trash?"]
    assert [(r.outcome, r.detail) for r in done] == [
        ("trashed", ""),
        ("failed", "not in Trash after trashing (HttpError 500)"),
    ]
    assert trashed == [(B, ["b1"])]  # only what was trashed is recorded

    gmail = FakeMailbox.with_labels({"b1": set()})
    done = dedup_apply.act(
        "trash", B, [ok(tmp_path)], gmail, lambda p: False, DEDUP_LABEL, lambda *a: 1 / 0
    )
    assert [(r.outcome, r.detail) for r in done] == [("skipped", "not confirmed")]
    assert gmail.calls == []  # not asked to trash, not recorded
    assert dedup_apply.act(
        "trash",
        B,
        [ok(tmp_path)],
        FakeMailbox.with_labels({"b1": set()}),
        lambda p: True,
        DEDUP_LABEL,
        None,
    )


def test_checked_asks_the_local_check_only_of_rows_that_pass_the_gmail_checks(tmp_path):
    asked = []

    def local(r):
        asked.append(r["gmail_id"])
        return "not in the sidecar"

    good = row(tmp_path, "b1")
    bad = row(tmp_path, "b2")
    passed = dedup_apply.checked(good, "label", {"INBOX"}, {"INBOX"}, DEDUP_LABEL, local)
    assert (passed.outcome, passed.detail) == ("skipped", "not in the sidecar")
    gone = dedup_apply.checked(bad, "label", None, {"INBOX"}, DEDUP_LABEL, local)
    assert (gone.outcome, gone.detail, gone.live) == ("skipped", "message no longer in Gmail", None)
    assert asked == ["b1"]  # b2 failed the Gmail check first
    fine = dedup_apply.checked(good, "label", {"INBOX"}, {"INBOX"}, DEDUP_LABEL, None)
    assert (fine.outcome, fine.detail, fine.live) == ("ok", "", frozenset({"INBOX"}))
    assert fine.row is good


def test_apply_plan_returns_results_in_order_and_leaves_the_rows_alone(tmp_path):
    rows = [row(tmp_path, "b1"), row(tmp_path, "b2"), row(tmp_path, "b3")]
    rows[1]["account"] = A  # a second account, in the middle
    before = copy.deepcopy(rows)
    gmail, _scopes, gmail_for = setup(
        {"a1": set(), "b2": {DEDUP_LABEL}}, {"b1": {DEDUP_LABEL}, "b3": set()}
    )
    log = apply_plan(rows, "trash", gmail_for, confirm=lambda p: True)
    assert all(isinstance(r, Result) for r in log)
    assert [r.row["gmail_id"] for r in log] == ["b1", "b2", "b3"]  # the rows' order
    assert [r.outcome for r in log] == ["trashed", "trashed", "skipped"]
    assert [r.row for r in log] == before and all(
        r.row is o for r, o in zip(log, rows, strict=True)
    )
    assert rows == before  # the plan rows were not changed
