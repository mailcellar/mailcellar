"""
apply: check planned removals against live Gmail, then label or trash them.

Shared with offload apply, whose rows have no keeper: rows without a
keeper_gmail_id skip the keeper checks.
"""

import csv
import logging
import sys
from datetime import datetime
from pathlib import Path

from mailcellar.dedup.plan import PLAN_COLUMNS
from mailcellar.dedup.rules import STAR_LABELS, is_user_label

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
        sys.exit(f"ERROR: {plan_path} is not a plan file (missing {sorted(missing)}).")
    return [r for r in rows if r["action"] == "remove"]


def apply_plan(
    rows: list[dict],
    mode: str,
    gmail_for,
    confirm,
    dedup_label: str = DEDUP_LABEL,
    local_check=None,
    on_trashed=None,
) -> list[dict]:
    """
    Check every planned removal against live Gmail state and, outside a dry
    run, act on the ones that pass. Returns one log entry per row.

    gmail_for(account, scope) -> object with message_labels / add_label / trash.
    confirm(prompt) -> bool, asked once per account before trashing.
    local_check(row) -> "" or why to skip; run on rows that pass the Gmail checks.
    on_trashed(account, rows) is called with the rows trashed in each account.
    Each log entry: row, outcome, detail, and live (the message's label names
    in Gmail when checked, or None).
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
    wanted: dict[str, set] = {}
    for r in rows:
        wanted.setdefault(r["account"], set()).add(r["gmail_id"])
        if has_keeper(r):
            wanted.setdefault(r["keeper_account"], set()).add(r["keeper_gmail_id"])
    for account in sorted(remove_accounts) + sorted(set(wanted) - remove_accounts):
        ids = sorted(wanted[account])
        logger.info(f"Reading {len(ids):,} messages from {account} …")
        for gid, labels in client(account).message_labels(ids).items():
            live[(account, gid)] = labels

    log = []
    for r in rows:
        reason = check_row(
            r,
            mode,
            live.get((r["account"], r["gmail_id"])),
            live.get((r.get("keeper_account"), r.get("keeper_gmail_id"))),
            dedup_label,
        )
        if not reason and local_check is not None:
            reason = local_check(r)
        log.append(
            {
                "row": r,
                "outcome": "skipped" if reason else "ok",
                "detail": reason,
                "live": live.get((r["account"], r["gmail_id"])),  # label names, or None
            }
        )

    passed: dict[str, list[dict]] = {}
    for entry in log:
        if entry["outcome"] == "ok":
            passed.setdefault(entry["row"]["account"], []).append(entry)

    for account, entries in sorted(passed.items()):
        ids = [e["row"]["gmail_id"] for e in entries]
        if mode == "dry-run":
            outcome = "would label or trash"
        elif mode == "label":
            client(account).add_label(ids, dedup_label)
            outcome = "labelled"
        else:
            if not confirm(f"Move {len(ids):,} messages in {account} to Trash?"):
                for e in entries:
                    e["outcome"], e["detail"] = "skipped", "not confirmed"
                continue
            failures = dict(f.split(": ", 1) for f in client(account).trash(ids))
            for e in entries:
                gid = e["row"]["gmail_id"]
                if gid in failures:
                    e["outcome"], e["detail"] = "failed", failures[gid]
                else:
                    e["outcome"] = "trashed"
            if on_trashed is not None:
                on_trashed(account, [e["row"] for e in entries if e["outcome"] == "trashed"])
            continue
        for e in entries:
            e["outcome"] = outcome
    return log


def write_apply_log(
    log: list[dict], mode: str, log_path: Path, columns: list[str] = APPLY_LOG_COLUMNS
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
            entry = {"time": now, "mode": mode, "outcome": e["outcome"], "detail": e["detail"]}
            w.writerow({c: entry[c] if c in entry else e["row"].get(c, "") for c in columns})
