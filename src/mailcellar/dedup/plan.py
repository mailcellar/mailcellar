"""
plan: decide keep / remove / review for every copy, and write the plan CSV.
"""

import csv
import sqlite3
import sys
from pathlib import Path

from mailcellar.dedup.rules import ORIGIN, plan_fixed_group, plan_origin_group, plan_within_group

PLAN_COLUMNS = [
    "action",
    "reason",
    "group_id",
    "scope",
    "account",
    "gmail_id",
    "keeper_account",
    "keeper_gmail_id",
    "message_id",
    "internal_date",
    "sender",
    "subject",
    "labels",
    "size",
    "path",
]

SCOPES = ("cross", "within", "both")


def resolve_account(con: sqlite3.Connection, keep: str) -> str:
    """Match --keep against indexed accounts: exact email or unique substring."""
    accounts = [a for (a,) in con.execute("SELECT DISTINCT account FROM messages")]
    if keep in accounts:
        return keep
    hits = [a for a in accounts if keep.lower() in a.lower()]
    if len(hits) != 1:
        sys.exit(
            f"ERROR: --keep {keep!r} matches {len(hits)} of the indexed "
            f"accounts ({', '.join(sorted(accounts))}); give the full address."
        )
    return hits[0]


def build_plan(
    con: sqlite3.Connection, keep: str | None, ignored: set[str], scope: str = "cross"
) -> list[dict]:
    """
    One dict per copy in every group of the given scope, in PLAN_COLUMNS
    order. Cross-account groups follow --keep (an account or 'origin');
    within-account groups keep one copy from the largest set with the same
    labels. scope: 'cross', 'within' or 'both'.
    """
    scopes = ("cross", "within") if scope == "both" else (scope,)
    labels: dict[tuple, set] = {}
    for account, gmail_id, label in con.execute(
        """SELECT l.account, l.gmail_id, l.label FROM labels l
               JOIN match_members USING (account, gmail_id)"""
    ):
        labels.setdefault((account, gmail_id), set()).add(label)

    copies: dict[int, list[dict]] = {}
    for row in con.execute(
        f"""SELECT mg.group_id, mg.scope, m.message_id,
                       m.account, m.gmail_id, m.internal_date, m.sender,
                       m.subject, m.size, b.folder || '/' || m.path,
                       m.forwarded_from
                FROM match_groups mg
                JOIN match_members USING (group_id)
                JOIN messages m USING (account, gmail_id)
                JOIN backups b USING (account)
                WHERE mg.scope IN ({",".join("?" * len(scopes))})
                ORDER BY mg.group_id, m.account, m.gmail_id""",
        scopes,
    ):
        (
            group_id,
            group_scope,
            mid,
            account,
            gmail_id,
            date,
            sender,
            subject,
            size,
            path,
            fwd_from,
        ) = row
        copies.setdefault(group_id, []).append(
            {
                "group_id": group_id,
                "scope": group_scope,
                "message_id": mid,
                "account": account,
                "gmail_id": gmail_id,
                "internal_date": date,
                "sender": sender,
                "subject": subject,
                "size": size,
                "path": path,
                "forwarded_from": fwd_from.lower(),
                "label_set": labels.get((account, gmail_id), set()),
            }
        )

    plan = []
    for group in copies.values():
        if group[0]["scope"] == "within":
            decisions = plan_within_group(group, ignored)
        elif keep == ORIGIN:
            decisions = plan_origin_group(group, keep, ignored)
        else:
            decisions = plan_fixed_group(group, keep, ignored)
        for c, action, reason, keeper_account, keeper_id in decisions:
            plan.append(
                {
                    **{k: c[k] for k in PLAN_COLUMNS if k in c},
                    "action": action,
                    "reason": reason,
                    "keeper_account": keeper_account,
                    "keeper_gmail_id": keeper_id,
                    "labels": ";".join(sorted(c["label_set"])),
                }
            )
    return plan


def write_plan(plan: list[dict], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=PLAN_COLUMNS)
        w.writeheader()
        w.writerows(plan)
