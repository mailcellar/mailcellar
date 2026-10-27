"""
apply: check planned removals against live Gmail, then label or trash them.

Shared with offload apply, whose rows have no keeper: rows without a
keeper_gmail_id skip the keeper checks.

The shape is restore's: apply_plan() reads what Gmail says, check_row() and
checked() decide, and act() carries out one account's rows; each step
returns new frozen Results rather than changing the ones it was given.
"""

import csv
import logging
import sys
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from mailcellar.dedup.plan import PLAN_COLUMNS
from mailcellar.dedup.rules import STAR_LABELS, is_user_label
from mailcellar.progress import Progress

logger = logging.getLogger(__name__)

DEDUP_LABEL = "dedup-redundant"
APPLY_LOG_COLUMNS = [
    "time",
    "mode",
    "account",
    "gmail_id",
    "keeper_account",
    "keeper_gmail_id",
    "outcome",
    "detail",
    "subject",
]


def safety_labels(labels, dedup_label: str = DEDUP_LABEL) -> set[str]:
    """
    The labels plan's safety rules look at: user labels, stars, SENT, DRAFT,
    leaving out the label apply itself adds.
    """
    return {
        label
        for label in labels
        if (is_user_label(label) or label in STAR_LABELS or label in ("SENT", "DRAFT"))
        and label != dedup_label
    }


def unusable_label(rows: list[dict], name: str) -> str:
    """
    Why `name` can't be apply's label, or "". --trash only trashes messages
    carrying the label, so it must be one apply alone puts on messages: not a
    Gmail system label, and on none of the planned messages at plan time.
    """
    if not name.strip():
        return "the label name is empty"
    if not is_user_label(name):
        return f"{name!r} is a Gmail system label"
    already = sum(name in r["labels"].split(";") for r in rows)
    if already:
        return (
            f"{already:,} planned messages already had the label {name!r} when the "
            "plan was made; --trash would trash them without a review step. "
            "Pick a label used only for this"
        )
    return ""


def has_keeper(row: dict) -> bool:
    return bool(row.get("keeper_gmail_id"))


def check_row(
    row: dict,
    mode: str,
    cand: set[str] | None,
    keeper: set[str] | None,
    dedup_label: str = DEDUP_LABEL,
) -> str:
    """
    "" if the planned removal is still safe, else why it is skipped.
    cand / keeper: live label names, or None if the message no longer exists.
    Rows without a keeper_gmail_id have no keeper to check.
    """
    if not row["path"] or not Path(row["path"]).is_file():
        return "no local backup copy"
    if cand is None:
        return "message no longer in Gmail"
    if "TRASH" in cand:
        return "already in trash"
    if has_keeper(row):
        if keeper is None:
            return "keeper no longer in Gmail"
        if keeper & {"TRASH", "SPAM"}:
            return "keeper in trash or spam"
    planned = set(filter(None, row["labels"].split(";")))
    live, then = safety_labels(cand, dedup_label), safety_labels(planned, dedup_label)
    if live != then:
        added = sorted(live - then)
        dropped = sorted(then - live)
        return (
            "labels changed since plan:"
            + (" +" + ",".join(added) if added else "")
            + (" -" + ",".join(dropped) if dropped else "")
        )
    if mode == "trash" and dedup_label not in cand:
        return f"not labelled {dedup_label}"
    return ""


def read_removals(plan_path: Path) -> list[dict]:
    with plan_path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    missing = set(PLAN_COLUMNS) - set(rows[0] if rows else PLAN_COLUMNS)
    if missing:
        sys.exit(f"error: {plan_path} is not a plan file (missing {sorted(missing)}).")
    return [r for r in rows if r["action"] == "remove"]


@dataclass(frozen=True)
class Result:
    """
    What came of one planned removal: its plan row (as read, never changed),
    the outcome and detail the apply log records, and the message's label
    names in Gmail when checked (None: it no longer exists there).
    """

    row: dict
    outcome: str
    detail: str = ""
    live: frozenset[str] | None = None


def after_dry_run(results: list[Result]) -> list[Result]:
    return [replace(r, outcome="would label or trash") for r in results]


def after_label(results: list[Result]) -> list[Result]:
    return [replace(r, outcome="labelled") for r in results]


def after_declined(results: list[Result]) -> list[Result]:
    return [replace(r, outcome="skipped", detail="not confirmed") for r in results]


def after_trash(results: list[Result], failures: dict[str, str]) -> list[Result]:
    """The results once trashing is done; failures maps a Gmail ID to why it failed."""
    return [
        replace(r, outcome="failed", detail=failures[r.row["gmail_id"]])
        if r.row["gmail_id"] in failures
        else replace(r, outcome="trashed")
        for r in results
    ]


def act(
    mode: str, account: str, results: list[Result], client, confirm, dedup_label: str, on_trashed
) -> list[Result]:
    """
    Carry out `mode` on the results of one account that passed the checks,
    and return them as new Results. client is that account's Gmail
    (add_label / trash); confirm and on_trashed are as in apply_plan.
    """
    ids = [r.row["gmail_id"] for r in results]
    if mode == "dry-run":
        return after_dry_run(results)
    if mode == "label":
        client.add_label(ids, dedup_label)
        return after_label(results)
    if not confirm(f"Move {len(ids):,} messages in {account} to Trash?"):
        return after_declined(results)
    with Progress(f"{account}: moving to Trash", len(ids)):
        failures = dict(f.split(": ", 1) for f in client.trash(ids))
    done = after_trash(results, failures)
    if on_trashed is not None:
        on_trashed(account, [r.row for r in done if r.outcome == "trashed"])
    return done


def unreadable(row: dict, unread: dict[tuple, str]) -> Result | None:
    """
    A skipped Result if Gmail couldn't tell apply about the message or its
    keeper, else None. Pure. Not knowing a message's state is never taken as
    its being gone, so nothing is labelled or trashed on a guess.
    """
    why = unread.get((row["account"], row["gmail_id"]))
    if why is not None:
        return Result(row, "skipped", f"couldn't read it from Gmail: {why}")
    why = unread.get((row.get("keeper_account"), row.get("keeper_gmail_id")))
    if why is not None:
        return Result(row, "skipped", f"couldn't read the keeper from Gmail: {why}")
    return None


def checked(
    row: dict,
    mode: str,
    live: set[str] | None,
    keeper: set[str] | None,
    dedup_label: str,
    local_check,
) -> Result:
    """
    The planned removal against what Gmail says about it: "ok" if it is still
    safe, else "skipped" with why. local_check(row) -> "" or why to skip is
    asked only of rows that pass the Gmail checks.
    """
    reason = check_row(row, mode, live, keeper, dedup_label)
    if not reason and local_check is not None:
        reason = local_check(row)
    return Result(
        row, "skipped" if reason else "ok", reason, None if live is None else frozenset(live)
    )


def apply_plan(
    rows: list[dict],
    mode: str,
    gmail_for,
    confirm,
    dedup_label: str = DEDUP_LABEL,
    local_check=None,
    on_trashed=None,
) -> list[Result]:
    """
    Check every planned removal against live Gmail state and, outside a dry
    run, act on the ones that pass. Returns one Result per row, in order;
    the rows are not changed.

    gmail_for(account, scope) -> a mailbox.Mailbox (message_labels, add_label and trash
    are used).
    confirm(prompt) -> bool, asked once per account before trashing.
    local_check(row) -> "" or why to skip; run on rows that pass the Gmail checks.
    on_trashed(account, rows) is called with the rows trashed in each account.
    """
    write_scope = "readonly" if mode == "dry-run" else "modify"
    remove_accounts = {r["account"] for r in rows}
    clients = {}

    def client(account):
        scope = write_scope if account in remove_accounts else "readonly"
        if account not in clients:
            clients[account] = gmail_for(account, scope)
        return clients[account]

    live: dict[tuple, set | None] = {}
    unread: dict[tuple, str] = {}  # (account, gmail_id) → why Gmail couldn't be read
    wanted: dict[str, set] = {}
    for r in rows:
        wanted.setdefault(r["account"], set()).add(r["gmail_id"])
        if has_keeper(r):
            wanted.setdefault(r["keeper_account"], set()).add(r["keeper_gmail_id"])
    for account in sorted(remove_accounts) + sorted(set(wanted) - remove_accounts):
        ids = sorted(wanted[account])
        logger.info(f"Reading {len(ids):,} messages from {account} …")
        failures: list[str] = []
        with Progress(f"{account}: reading", len(ids)):
            labels_by_id = client(account).message_labels(ids, failures)
        for gid, labels in labels_by_id.items():
            live[(account, gid)] = labels
        for failure in failures:
            gid, why = failure.split(": ", 1)
            unread[(account, gid)] = why
        if failures:
            logger.warning(
                f"{account}: {len(failures):,} messages couldn't be read from Gmail; "
                "they are skipped, and the next run tries them again"
            )

    results = [
        unreadable(r, unread)
        or checked(
            r,
            mode,
            live.get((r["account"], r["gmail_id"])),
            live.get((r.get("keeper_account"), r.get("keeper_gmail_id"))),
            dedup_label,
            local_check,
        )
        for r in rows
    ]

    passed: dict[str, list[int]] = {}
    for n, result in enumerate(results):
        if result.outcome == "ok":
            passed.setdefault(result.row["account"], []).append(n)
    for account, positions in sorted(passed.items()):
        acted = act(
            mode,
            account,
            [results[n] for n in positions],
            client(account),
            confirm,
            dedup_label,
            on_trashed,
        )
        for n, result in zip(positions, acted, strict=True):
            results[n] = result
    return results


def write_apply_log(
    log: list[Result], mode: str, log_path: Path, columns: list[str] = APPLY_LOG_COLUMNS
) -> None:
    """
    Append this run to the apply log (CSV, one row per planned removal).
    Columns other than time, mode, outcome and detail come from the plan row.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    new = not log_path.is_file()
    now = datetime.now().isoformat(timespec="seconds")
    with log_path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns)
        if new:
            w.writeheader()
        for e in log:
            entry = {"time": now, "mode": mode, "outcome": e.outcome, "detail": e.detail}
            w.writerow({c: entry[c] if c in entry else e.row.get(c, "") for c in columns})
