"""
Text reports printed by match, plan and apply.
"""

import sqlite3

from mailcellar.dedup.apply import Result
from mailcellar.dedup.rules import ORIGIN
from mailcellar.report.output import human_size


def report_match(con: sqlite3.Connection, show: int) -> None:
    from mailcellar.dedup.match import reused_message_ids

    accounts = [a for (a,) in con.execute("SELECT DISTINCT account FROM messages ORDER BY account")]
    by_kind = con.execute(
        """SELECT scope, method, count(*) FROM match_groups
           GROUP BY scope, method ORDER BY scope, method"""
    ).fetchall()
    no_content = con.execute(
        """SELECT account, sum(message_id = ''), sum(content_hash = '')
           FROM messages GROUP BY account ORDER BY account"""
    ).fetchall()
    reused = reused_message_ids(con)

    print("Duplicate match")
    print(f"Accounts   : {', '.join(accounts)}")
    print(f"Groups     : {sum(n for *_k, n in by_kind):,}")
    for scope, method, n in by_kind:
        print(f"  {scope:<7} by {method:<11} {n:>6,}")
    print(
        f"Message-IDs reused for different content (not grouped): {len(reused):,}"
        + (f", {sum(a > 1 for _m, a, _c in reused):,} of them across accounts" if reused else "")
    )
    for account, no_mid, no_hash in no_content:
        if no_mid or no_hash:
            print(
                f"  {account}: {no_mid:,} without Message-ID, "
                f"{no_hash:,} without text or attachments (never grouped)"
            )

    print("\nCopies per account in matched groups")
    print(f"  {'Account':<30} {'Cross':>8} {'Within':>8}")
    for account, cross, within in con.execute(
        """SELECT mm.account, sum(mg.scope = 'cross'), sum(mg.scope = 'within')
               FROM match_members mm JOIN match_groups mg USING (group_id)
               GROUP BY mm.account ORDER BY mm.account"""
    ):
        print(f"  {account:<30} {cross:>8,} {within:>8,}")

    if show:
        print(f"\nWithin-account groups (first {show})")
        for group_id, method, n, sender, subject in con.execute(
            """SELECT mg.group_id, mg.method, count(*), max(m.sender), max(m.subject)
                   FROM match_groups mg
                   JOIN match_members mm USING (group_id)
                   JOIN messages m USING (account, gmail_id)
                   WHERE mg.scope = 'within'
                   GROUP BY mg.group_id ORDER BY mg.group_id LIMIT ?""",
            (show,),
        ):
            print(f"  #{group_id:<6} {method:<11} {n} copies  {sender[:32]:<32} {subject[:45]}")


def report_plan(
    plan: list[dict], keep: str | None, ignored: set[str], scope: str = "cross"
) -> None:
    if scope in ("cross", "both"):
        if keep == ORIGIN:
            print("Dedup plan — across accounts: remove auto-forwarded copies, keep the original")
        else:
            print(f"Dedup plan — across accounts: keep copies in {keep}")
    if scope in ("within", "both"):
        print(
            "Dedup plan — within accounts: keep one copy from the largest set with the same labels"
        )
    if ignored:
        print(f"Ignored labels: {', '.join(sorted(ignored))}")

    for group_scope in ("cross", "within"):
        scoped = [r for r in plan if r["scope"] == group_scope]
        if not scoped and scope != group_scope and scope != "both":
            continue
        print(
            f"\n  {group_scope.capitalize() + '-account':<30} {'Keep':>7} {'Remove':>7} "
            f"{'Review':>7} {'Remove size':>12}"
        )
        for account in sorted({r["account"] for r in scoped}):
            rows = [r for r in scoped if r["account"] == account]
            n = {a: sum(r["action"] == a for r in rows) for a in ("keep", "remove", "review")}
            size = sum(r["size"] for r in rows if r["action"] == "remove")
            print(
                f"  {account:<30} {n['keep']:>7,} {n['remove']:>7,} {n['review']:>7,} "
                f"{human_size(size):>12}"
            )

    reasons: dict[str, int] = {}
    for r in plan:
        if r["action"] == "review":
            key = r["reason"].split(":")[0]
            reasons[key] = reasons.get(key, 0) + 1
    if reasons:
        print("\n  Review reasons")
        for reason, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"    {reason:<28} {n:>6,}")

    untouched: dict[str, int] = {}
    for r in plan:
        if r["action"] == "keep" and r["reason"]:
            untouched[r["reason"]] = untouched.get(r["reason"], 0) + 1
    if untouched:
        print("\n  Left untouched (copies)")
        for reason, n in sorted(untouched.items(), key=lambda kv: -kv[1]):
            print(f"    {reason:<28} {n:>6,}")

    missing: dict[str, int] = {}
    for r in plan:
        if r["reason"].startswith("labels not on keeper: "):
            for label in r["reason"].split(": ", 1)[1].split(", "):
                missing[label] = missing.get(label, 0) + 1
    if missing:
        print("\n  Labels missing on keeper (use --ignore-label to drop one)")
        for label, n in sorted(missing.items(), key=lambda kv: -kv[1]):
            print(f"    {label:<28} {n:>6,}")


def report_apply(log: list[Result], mode: str) -> None:
    print(f"\nApply ({mode})")
    counts: dict[tuple, int] = {}
    for e in log:
        key = (e.row["account"], e.outcome, e.detail.split(":")[0])
        counts[key] = counts.get(key, 0) + 1
    for (account, outcome, detail), n in sorted(counts.items()):
        print(f"  {account:<30} {outcome:<22} {n:>6,}  {detail}")
    if mode == "dry-run":
        print("\nDry run: nothing was changed. Use --label, then --trash.")
