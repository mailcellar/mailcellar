import csv
from pathlib import Path

import pytest

from mailcellar.dedup import cli as dedup
from test_index import FakeBackup, make_eml, run_index

A, B = "a@example.com", "b@example.com"


def run_plan(tmp_path, keep, *args):
    out = tmp_path / "plan.csv"
    dedup.main(
        ["plan", "--db", str(tmp_path / "dedup.sqlite"), "--keep", keep, "--out", str(out), *args]
    )
    with out.open(newline="", encoding="utf-8") as fh:
        return {(r["account"], r["id"]): r for r in csv.DictReader(fh)}


def pair(tmp_path, a_labels=(), b_labels=(), a_body="same", b_body="same"):
    """One message present in both accounts, indexed."""
    a, b = FakeBackup(tmp_path, A), FakeBackup(tmp_path, B)
    a.add("a1", make_eml(msgid="<m1@x>", body=a_body), labels=a_labels)
    b.add("b1", make_eml(msgid="<m1@x>", body=b_body), labels=b_labels)
    run_index(tmp_path, a, b)
    return a, b


def actions(plan):
    return {key: (r["action"], r["reason"]) for key, r in plan.items()}


def test_confirmed_copy_outside_keep_account_is_removed(tmp_path):
    pair(tmp_path)
    plan = run_plan(tmp_path, A)
    assert actions(plan) == {(A, "a1"): ("keep", ""), (B, "b1"): ("remove", "")}
    assert plan[(B, "b1")]["keeper_account"] == A
    assert plan[(B, "b1")]["keeper_id"] == "a1"


def test_plan_paths_are_absolute_and_follow_a_moved_folder(tmp_path):
    a, b = pair(tmp_path)
    plan = run_plan(tmp_path, A)
    assert Path(plan[(B, "b1")]["path"]).is_file()

    moved = tmp_path / "moved" / B
    moved.parent.mkdir()
    b.dir.rename(moved)
    b.dir = moved
    run_index(tmp_path, a, b)
    path = Path(run_plan(tmp_path, A)[(B, "b1")]["path"])
    assert path == moved.resolve() / "2026/1/1/b1.eml" and path.is_file()


def test_keep_flag_flips_direction(tmp_path):
    pair(tmp_path)
    assert actions(run_plan(tmp_path, B)) == {(A, "a1"): ("remove", ""), (B, "b1"): ("keep", "")}


def test_keep_accepts_unique_substring(tmp_path):
    pair(tmp_path)
    # both accounts contain "example.com"; "a@" is unique
    assert actions(run_plan(tmp_path, "a@"))[(B, "b1")] == ("remove", "")


def test_keep_rejects_ambiguous_or_unknown(tmp_path):
    pair(tmp_path)
    for keep in ("example.com", "nobody"):
        with pytest.raises(SystemExit):
            run_plan(tmp_path, keep)


def test_different_content_is_not_planned(tmp_path):
    # same Message-ID, different content: not a duplicate, so not in the plan
    pair(tmp_path, b_body="different")
    assert run_plan(tmp_path, A) == {}


@pytest.mark.parametrize("label", ["SENT", "DRAFT"])
def test_sent_or_draft_copy_goes_to_review(tmp_path, label):
    pair(tmp_path, b_labels=[label])
    assert actions(run_plan(tmp_path, A))[(B, "b1")] == ("review", "sent or draft copy")


def test_starred_copy_goes_to_review_unless_keeper_starred(tmp_path):
    pair(tmp_path / "keeper-not", b_labels=["YELLOW_STAR"])
    assert actions(run_plan(tmp_path / "keeper-not", A))[(B, "b1")] == (
        "review",
        "starred, keeper not",
    )
    pair(tmp_path / "both", a_labels=["STARRED"], b_labels=["YELLOW_STAR"])
    assert actions(run_plan(tmp_path / "both", A))[(B, "b1")] == ("remove", "")


def test_keeper_in_trash_blocks_removal(tmp_path):
    pair(tmp_path, a_labels=["TRASH"])
    assert actions(run_plan(tmp_path, A))[(B, "b1")] == ("review", "keeper in trash or spam")


def test_user_labels_missing_on_keeper_go_to_review(tmp_path):
    pair(tmp_path, a_labels=["work"], b_labels=["work", "travel", "Bills"])
    assert actions(run_plan(tmp_path, A))[(B, "b1")] == (
        "review",
        "labels not on keeper: Bills, travel",
    )


def test_system_and_ignored_labels_do_not_block_removal(tmp_path):
    system = tmp_path / "system"
    pair(system, b_labels=["INBOX", "UNREAD", "IMPORTANT", "CATEGORY_UPDATES"])
    assert actions(run_plan(system, A))[(B, "b1")] == ("remove", "")
    ignored = tmp_path / "ignored"
    pair(ignored, b_labels=["Work"])
    plan = run_plan(ignored, A, "--ignore-label", "Work")
    assert actions(plan)[(B, "b1")] == ("remove", "")
    assert plan[(B, "b1")]["labels"] == "Work"


def test_repeats_in_both_accounts(tmp_path):
    a, b = FakeBackup(tmp_path, A), FakeBackup(tmp_path, B)
    a.add("a1", make_eml(msgid="<m1@x>"), labels=["work"])
    a.add("a2", make_eml(msgid="<m1@x>"))
    b.add("b1", make_eml(msgid="<m1@x>"), labels=["work"])
    b.add("b2", make_eml(msgid="<m1@x>"))
    run_index(tmp_path, a, b)

    # labels are compared against the union over all keeper copies
    assert actions(run_plan(tmp_path, A)) == {
        (A, "a1"): ("keep", ""),
        (A, "a2"): ("keep", ""),
        (B, "b1"): ("remove", ""),
        (B, "b2"): ("remove", ""),
    }


# ── --keep origin ────────────────────────────────────────────────────────────


def fwd(origin, target):
    return [("X-Forwarded-For", f"{origin} {target}")]


def origin_backups(tmp_path, copies):
    """copies: (account, gmail_id, headers, labels, body) per copy of <m1@x>."""
    backups = {A: FakeBackup(tmp_path, A), B: FakeBackup(tmp_path, B)}
    for account, gmail_id, headers, labels, body in copies:
        backups[account].add(
            gmail_id, make_eml(msgid="<m1@x>", body=body, headers=headers), labels=labels
        )
    run_index(tmp_path, *backups.values())


def test_origin_removes_forwarded_copy_and_keeps_original(tmp_path):
    origin_backups(
        tmp_path,
        [
            (A, "a1", [], [], "same"),
            (B, "b1", fwd(A, B), [], "same"),
        ],
    )
    plan = run_plan(tmp_path, "origin")
    assert actions(plan) == {(A, "a1"): ("keep", ""), (B, "b1"): ("remove", "")}
    assert (plan[(B, "b1")]["keeper_account"], plan[(B, "b1")]["keeper_id"]) == (A, "a1")


def test_origin_follows_forwarding_direction(tmp_path):
    origin_backups(
        tmp_path,
        [
            (A, "a1", fwd(B, A), [], "same"),
            (B, "b1", [], [], "same"),
        ],
    )
    assert actions(run_plan(tmp_path, "origin")) == {
        (A, "a1"): ("remove", ""),
        (B, "b1"): ("keep", ""),
    }


def test_origin_leaves_independent_deliveries_untouched(tmp_path):
    origin_backups(
        tmp_path,
        [
            (A, "a1", [], [], "same"),
            (B, "b1", [], [], "same"),
        ],
    )
    assert actions(run_plan(tmp_path, "origin")) == {
        (A, "a1"): ("keep", "independent delivery"),
        (B, "b1"): ("keep", "independent delivery"),
    }


def test_origin_ignores_forwarding_from_unindexed_accounts(tmp_path):
    origin_backups(
        tmp_path,
        [
            (A, "a1", [], [], "same"),
            (B, "b1", fwd("someone@elsewhere.com", B), [], "same"),
        ],
    )
    assert actions(run_plan(tmp_path, "origin"))[(B, "b1")] == ("keep", "independent delivery")


def test_origin_keeps_unforwarded_repeat_in_forwarded_account(tmp_path):
    origin_backups(
        tmp_path,
        [
            (A, "a1", [], [], "same"),
            (B, "b1", fwd(A, B), [], "same"),
            (B, "b2", [], [], "same"),
        ],
    )
    assert actions(run_plan(tmp_path, "origin")) == {
        (A, "a1"): ("keep", ""),
        (B, "b1"): ("remove", ""),
        (B, "b2"): ("keep", "not forwarded"),
    }


def test_origin_forwarding_both_ways_goes_to_review(tmp_path):
    origin_backups(
        tmp_path,
        [
            (A, "a1", fwd(B, A), [], "same"),
            (B, "b1", fwd(A, B), [], "same"),
        ],
    )
    assert actions(run_plan(tmp_path, "origin")) == {
        (A, "a1"): ("review", "forwarded in both directions"),
        (B, "b1"): ("review", "forwarded in both directions"),
    }


def test_origin_still_applies_safety_rules(tmp_path):
    origin_backups(
        tmp_path,
        [
            (A, "a1", [], [], "same"),
            (B, "b1", fwd(A, B), ["garden"], "same"),
        ],
    )
    assert actions(run_plan(tmp_path, "origin"))[(B, "b1")] == (
        "review",
        "labels not on keeper: garden",
    )


def test_origin_forwarded_copy_with_different_content_is_not_planned(tmp_path):
    origin_backups(
        tmp_path,
        [
            (A, "a1", [], [], "original"),
            (B, "b1", fwd(A, B), [], "original plus a footer"),
        ],
    )
    assert run_plan(tmp_path, "origin") == {}


def test_plan_reflects_current_index_without_rerunning_match(tmp_path):
    a, b = pair(tmp_path)
    run_plan(tmp_path, A)
    b.remove("b1")
    run_index(tmp_path, a, b)
    assert run_plan(tmp_path, A) == {}


# ── --scope within / both ────────────────────────────────────────────────────


def run_scope(tmp_path, scope, *args):
    out = tmp_path / "plan.csv"
    dedup.main(
        ["plan", "--db", str(tmp_path / "dedup.sqlite"), "--scope", scope, "--out", str(out), *args]
    )
    with out.open(newline="", encoding="utf-8") as fh:
        return {(r["account"], r["id"]): r for r in csv.DictReader(fh)}


def within_backup(tmp_path, copies):
    """copies: (gmail_id, labels) — all the same message in account A."""
    a, b = FakeBackup(tmp_path, A), FakeBackup(tmp_path, B)
    for gid, labels in copies:
        a.add(gid, make_eml(msgid="<w1@x>", body="same"), labels=labels)
    run_index(tmp_path, a, b)
    return a, b


def test_within_same_labels_removable_and_keeper_in_same_account(tmp_path):
    within_backup(tmp_path, [("a1", ["INBOX"]), ("a2", ["INBOX"])])
    plan = run_scope(tmp_path, "within")
    assert actions(plan) == {(A, "a1"): ("keep", ""), (A, "a2"): ("remove", "")}
    assert (plan[(A, "a2")]["keeper_account"], plan[(A, "a2")]["keeper_id"]) == (A, "a1")
    assert plan[(A, "a2")]["scope"] == "within"


def test_within_keeper_from_largest_label_set(tmp_path):
    within_backup(tmp_path, [("a1", ["odd"]), ("a2", ["INBOX"]), ("a3", ["INBOX"])])
    assert actions(run_scope(tmp_path, "within")) == {
        (A, "a1"): ("review", "labels differ: +odd -INBOX"),
        (A, "a2"): ("keep", ""),
        (A, "a3"): ("remove", ""),
    }


def test_within_safety_rules_still_apply(tmp_path):
    within_backup(tmp_path, [("a1", ["SENT"]), ("a2", ["SENT"])])
    assert actions(run_scope(tmp_path, "within"))[(A, "a2")] == ("review", "sent or draft copy")


def test_scope_selects_groups(tmp_path):
    a, b = FakeBackup(tmp_path, A), FakeBackup(tmp_path, B)
    a.add("a1", make_eml(msgid="<x@x>"))
    b.add("b1", make_eml(msgid="<x@x>"))  # cross group
    a.add("a2", make_eml(msgid="<w@x>", body="w"))
    a.add("a3", make_eml(msgid="<w@x>", body="w"))  # within group
    run_index(tmp_path, a, b)

    assert set(run_scope(tmp_path, "cross", "--keep", A)) == {(A, "a1"), (B, "b1")}
    assert set(run_scope(tmp_path, "within")) == {(A, "a2"), (A, "a3")}
    assert set(run_scope(tmp_path, "both", "--keep", A)) == {
        (A, "a1"),
        (B, "b1"),
        (A, "a2"),
        (A, "a3"),
    }


def test_keep_is_required_except_for_within(tmp_path):
    within_backup(tmp_path, [("a1", []), ("a2", [])])
    for scope in ("cross", "both"):
        with pytest.raises(SystemExit):
            run_scope(tmp_path, scope)
    run_scope(tmp_path, "within")  # fine without --keep


def test_within_resend_with_new_message_id_is_removable(tmp_path):
    # re-delivery with a new Message-ID, same From/Date/Subject and content,
    # matched by the header key
    a, b = FakeBackup(tmp_path, A), FakeBackup(tmp_path, B)
    date = [("Date", "Mon, 20 Jan 2014 14:15:32 +0100")]
    a.add("a1", make_eml(msgid="<first@x>", body="Order shipped", headers=date), labels=["INBOX"])
    a.add("a2", make_eml(msgid="<second@x>", body="Order shipped", headers=date), labels=["INBOX"])
    run_index(tmp_path, a, b)
    assert actions(run_scope(tmp_path, "within")) == {
        (A, "a1"): ("keep", ""),
        (A, "a2"): ("remove", ""),
    }
