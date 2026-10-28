"""
plan: select the messages each rule takes out of Gmail, and summarise
what it takes and what its protections keep. Read-only: works on the
sidecars (meta.sqlite) and gyb's index, attached by report.db, and, for
rules with a `query`, the IDs Gmail's search found, which the caller gets.

Rules apply in file order; a message taken by one rule is not offered to
the next. Never selected, whatever the rule: messages already offloaded,
already in Trash or Spam, no longer in its mailbox (thread unknown), or missing
from the backup's index.
"""

import sqlite3
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from mailcellar.offload.rules import (
    DEFAULT_KEEP,
    KEEP,
    KEEP_OF,
    KEPT_WHY,
    PROTECTIONS,
    Rule,
    fold,
    years_text,
)
from mailcellar.report import bulk
from mailcellar.report.output import Table, day

PLAN_COLUMNS = [
    "account",
    "gmail_id",
    "path",
    "date",
    "sender",
    "subject",
    "size",
    "labels",
    "rule",
    "waived",
    "planned",  # when the plan was made, UTC (apply checks the backup is newer)
]

PLANNED_FORMAT = "%Y-%m-%d %H:%M:%S"

EXCLUSIONS = {
    "offloaded": "already offloaded",
    "trash": "already in Trash or Spam",
    "gone": "no longer in its mailbox",  # Gmail or IMAP: a plan spans both
    "unindexed": "not in the backup index",
    "earlier": "taken by an earlier rule",
}


@dataclass(frozen=True)
class Message:
    account: str
    gmail_id: str
    internal_date: int
    size: int
    sender: str
    subject: str
    path: str  # '' when not in gyb's index
    labels: list[str]  # names, sorted
    user_labels: set[str]  # lower case
    flags: dict[str, bool]  # protection and exclusion name → applies
    thread_id: str = ""  # '' when unknown (gone from its mailbox)


Hits = dict[tuple[str, str], list[str]]  # (account, Gmail search) → message IDs found


def searches(rules: list[Rule], accounts: list[str]) -> dict[str, list[str]]:
    """account → the Gmail searches its rules need, each once, in rule order. Pure."""
    out: dict[str, list[str]] = {}
    for rule in rules:
        for account in accounts:
            if rule.accounts and account not in rule.accounts:
                continue
            for q in rule.queries:
                if q not in out.setdefault(account, []):
                    out[account].append(q)
    return {a: qs for a, qs in out.items() if qs}


def prepare(con: sqlite3.Connection, accounts: list[str], hits: Hits) -> None:
    """
    TEMP pm: every message with what selection, protections and the plan
    need; TEMP query_hits: what each Gmail search found.
    """
    con.create_function("fold", 1, fold, deterministic=True)
    con.executescript("""
        DROP TABLE IF EXISTS temp.query_hits;
        CREATE TEMP TABLE query_hits (account, query, gmail_id,
                                      PRIMARY KEY (account, query, gmail_id));
    """)
    con.executemany(
        "INSERT OR IGNORE INTO query_hits VALUES (?, ?, ?)",
        ((a, q, gid) for (a, q), ids in hits.items() for gid in ids),
    )
    bulk.prepare_messages(con, accounts)
    bulk.prepare_engagement(con)
    con.executescript(f"""
        DROP TABLE IF EXISTS temp.own;
        CREATE TEMP TABLE own (address PRIMARY KEY);
        INSERT OR IGNORE INTO own VALUES {",".join(f"({bulk.sql_list([a])})" for a in accounts)};
        INSERT OR IGNORE INTO own
        SELECT DISTINCT m.sender_addr FROM all_messages m JOIN bm USING (account, gmail_id)
        WHERE bm.sent AND m.sender_addr != '';

        DROP TABLE IF EXISTS temp.pm;
        CREATE TEMP TABLE pm AS
        SELECT bm.account, bm.gmail_id, bm.internal_date, bm.size, bm.sender, bm.list_id,
               bm.thread_id, m.subject, m.forwarded_from, coalesce(i.filename, '') AS filename,
               (bm.sent OR m.sender_addr IN own) AS sent,
               coalesce(l.starred, 0) AS starred,
               (st.thread_id IS NOT NULL) AS thread,
               (ri.message_id IS NOT NULL) AS replied,
               (wt.address IS NOT NULL) AS correspondents,
               EXISTS (SELECT 1 FROM all_recipients r
                       WHERE r.account = bm.account AND r.gmail_id = bm.gmail_id
                         AND r.kind IN ('to', 'cc')
                         AND lower(r.address) IN (SELECT lower(address) FROM own)) AS to_me,
               (p.gmail_id IS NOT NULL) AS offloaded,
               coalesce(l.trash, 0) AS trash,
               (bm.thread_id = '') AS gone,
               (i.filename IS NULL) AS unindexed
        FROM bm
        JOIN all_messages m USING (account, gmail_id)
        LEFT JOIN (SELECT account, gmail_id, max(label_id = 'STARRED') AS starred,
                          max(label_id IN ('TRASH', 'SPAM')) AS trash
                   FROM all_message_labels GROUP BY account, gmail_id) l
               USING (account, gmail_id)
        LEFT JOIN all_index i USING (account, gmail_id)
        LEFT JOIN all_offloaded p USING (account, gmail_id)
        LEFT JOIN sent_threads st ON st.account = bm.account AND st.thread_id = bm.thread_id
        LEFT JOIN replied_ids ri ON ri.message_id = bm.message_id AND bm.message_id != ''
        LEFT JOIN wrote_to wt ON wt.address = bm.sender;
    """)


def _domain_or_address(column: str, values: list[str], params: list) -> str:
    """SQL matching an address column against addresses and "@domain"s (exactly, no LIKE)."""
    parts = []
    for v in values:
        if v.startswith("@"):
            parts.append(f"substr(lower({column}), -?) = ?")
            params += [len(v), v]
        else:
            parts.append(f"lower({column}) = ?")
            params.append(v)
    return "(" + " OR ".join(parts) + ")"


def _conditions(scope: dict[str, list], params: list) -> list[str]:
    """One SQL condition over pm per scope key (its values OR'd), appending params."""
    conds = []
    if "label" in scope:
        conds.append(
            "EXISTS (SELECT 1 FROM all_message_labels ml WHERE ml.account = pm.account "
            "AND ml.gmail_id = pm.gmail_id AND ml.type = 'user' "
            f"AND lower(ml.name) IN ({','.join('?' * len(scope['label']))}))"
        )
        params += [v.lower() for v in scope["label"]]
    if "sender" in scope:
        conds.append(_domain_or_address("pm.sender", scope["sender"], params))
    if "list" in scope:
        conds.append(f"pm.list_id IN ({','.join('?' * len(scope['list']))})")
        params += scope["list"]
    if "forwarded_from" in scope:
        # forwarded_from is a space-separated list of addresses.
        parts = []
        for v in scope["forwarded_from"]:
            parts.append("instr(' ' || lower(pm.forwarded_from) || ' ', ?) > 0")
            params.append(f"{v} " if v.startswith("@") else f" {v} ")
        conds.append("(" + " OR ".join(parts) + ")")
    if "category" in scope:
        conds.append(
            "EXISTS (SELECT 1 FROM all_message_labels ml WHERE ml.account = pm.account "
            "AND ml.gmail_id = pm.gmail_id "
            f"AND ml.label_id IN ({','.join('?' * len(scope['category']))}))"
        )
        params += ["CATEGORY_" + c.upper() for c in scope["category"]]
    if "subject" in scope:
        # fold() is registered on the connection by prepare(); values are folded already.
        conds.append(
            "(" + " OR ".join("instr(fold(pm.subject), ?) > 0" for _ in scope["subject"]) + ")"
        )
        params += scope["subject"]
    if "to_me" in scope:
        conds.append("pm.to_me = ?")
        params.append(int(scope["to_me"][0]))
    if "larger_than_mb" in scope:
        conds.append("pm.size > ?")
        params.append(int(scope["larger_than_mb"][0] * 1024 * 1024))
    if "query" in scope:
        conds.append(
            "EXISTS (SELECT 1 FROM query_hits q WHERE q.account = pm.account "
            f"AND q.gmail_id = pm.gmail_id AND q.query IN ({','.join('?' * len(scope['query']))}))"
        )
        params += scope["query"]
    return conds


def scope_sql(rule: Rule, accounts: list[str]) -> tuple[str, list]:
    """WHERE clause over pm for the rule's scope, cut-off and accounts."""
    params: list = [rule.before_ms]
    where = ["pm.internal_date < ?"]
    chosen = [a for a in accounts if not rule.accounts or a in rule.accounts]
    where.append(f"pm.account IN ({bulk.sql_list(chosen)})")
    where += _conditions(rule.scope, params)
    return " AND ".join(where), params


def except_sql(rule: Rule) -> tuple[str, list]:
    """
    SQL expression over pm: 1 when the rule's keep_if matches (every key of
    any one table), else 0.
    """
    params: list = []
    tables = ["(" + " AND ".join(_conditions(t, params)) + ")" for t in rule.exceptions]
    return ("(" + " OR ".join(tables) + ")" if tables else "0"), params


def _labels(con, where: str, params: list) -> dict[tuple, list[tuple]]:
    out: dict[tuple, list[tuple]] = defaultdict(list)
    for account, gid, name, kind in con.execute(
        f"""SELECT ml.account, ml.gmail_id, ml.name, ml.type
            FROM pm JOIN all_message_labels ml USING (account, gmail_id) WHERE {where}""",
        params,
    ):
        out[(account, gid)].append((name, kind))
    return out


def in_scope(con, rule: Rule, accounts: list[str], folders: dict[str, str]) -> list[Message]:
    """Every message the rule's scope and cut-off cover, oldest first."""
    where, params = scope_sql(rule, accounts)
    excepted, except_params = except_sql(rule)
    labels = _labels(con, where, params)
    # Columns of pm; "labels" depends on the rule and "earlier" on the rules before it.
    flags = [p for p in PROTECTIONS if p != "labels"] + [e for e in EXCLUSIONS if e != "earlier"]
    out = []
    for row in con.execute(
        f"""SELECT account, gmail_id, internal_date, size, sender, subject, filename,
                   thread_id, {excepted}, {", ".join(flags)}
            FROM pm WHERE {where} ORDER BY internal_date, account, gmail_id""",
        except_params + params,
    ):
        account, gid, ms, size, sender, subject, filename, thread_id = row[:8]
        names = labels.get((account, gid), [])
        user = {n.lower() for n, kind in names if kind == "user"}
        values = dict(zip(flags, (bool(v) for v in row[9:]), strict=True))
        values["except"] = bool(row[8])
        values["labels"] = bool(user - rule.labels)
        out.append(
            Message(
                account,
                gid,
                ms,
                size,
                sender,
                subject,
                f"{folders[account]}/{filename}" if filename else "",
                sorted(n for n, _k in names),
                user,
                values,
                thread_id or "",
            )
        )
    return out


@dataclass
class RuleResult:
    rule: Rule
    selected: list[Message]
    kept: Counter  # keep name → messages it applies to (one message may count under several)
    kept_size: Counter
    excluded: Counter  # exclusion → messages
    kept_total: int = 0
    scope_total: int = 0
    # keep name (or "keep_if") → the messages it keeps, oldest first, for the review report
    kept_messages: dict[str, list[Message]] = field(default_factory=dict)


def build_plan(
    con: sqlite3.Connection,
    rules: list[Rule],
    accounts: list[str],
    folders: dict[str, str],
    hits: Hits | None = None,
) -> list[RuleResult]:
    """
    Apply the rules in order. folders: account → backup folder (for plan
    paths); hits: what each rule's Gmail searches found (see searches()).
    """
    prepare(con, accounts, hits or {})
    taken: set[tuple] = set()
    results = []
    for rule in rules:
        r = RuleResult(rule, [], Counter(), Counter(), Counter())
        for m in in_scope(con, rule, accounts, folders):
            r.scope_total += 1
            key = (m.account, m.gmail_id)
            exclusion = next((e for e in EXCLUSIONS if e != "earlier" and m.flags[e]), None)
            if exclusion is None and key in taken:
                exclusion = "earlier"
            if exclusion:
                r.excluded[exclusion] += 1
                continue
            # keep_if counts with the protections, ahead of them; thread and replied count
            # once, as conversations.
            protections = list(
                dict.fromkeys(
                    ["keep_if"] * m.flags["except"]
                    + [KEEP_OF[p] for p in rule.protect if m.flags[p]]
                )
            )
            if protections:
                r.kept_total += 1
                for p in protections:
                    r.kept[p] += 1
                    r.kept_size[p] += m.size
                    r.kept_messages.setdefault(p, []).append(m)
                continue
            taken.add(key)
            r.selected.append(m)
        results.append(r)
    return results


def not_backed_up(con: sqlite3.Connection) -> dict[tuple[str, str], int]:
    """
    (account, Gmail search) → messages it found that the backup doesn't have,
    which no rule can offload. After build_plan.
    """
    return {
        (a, q): n
        for a, q, n in con.execute("""
            SELECT q.account, q.query, count(*) FROM query_hits q
            LEFT JOIN pm ON pm.account = q.account AND pm.gmail_id = q.gmail_id
            WHERE pm.gmail_id IS NULL GROUP BY q.account, q.query""")
    }


def plan_rows(results: list[RuleResult], planned_ms: int) -> list[dict]:
    planned = time.strftime(PLANNED_FORMAT, time.gmtime(planned_ms / 1000))
    return [
        {
            "account": m.account,
            "gmail_id": m.gmail_id,
            "path": m.path,
            "date": day(m.internal_date),
            "sender": m.sender,
            "subject": m.subject,
            "size": m.size,
            "labels": ";".join(m.labels),
            "rule": r.rule.name,
            "waived": ";".join(r.rule.waived),
            "planned": planned,
        }
        for r in results
        for m in r.selected
    ]


def keeps_text(rule: Rule) -> str:
    """
    What a rule keeps, for the Rules table: "keep_if, default", "default,
    correspondents", "mine, starred", "nothing".
    """
    keep = rule.keep
    if set(DEFAULT_KEEP) <= set(keep):
        keep = ["default"] + [k for k in keep if k not in DEFAULT_KEEP]
    return ", ".join(["keep_if"] * bool(rule.exceptions) + keep) or "nothing"


def summary(results: list[RuleResult], top: int) -> list[Table]:
    rules, kept, years, senders = [], [], [], []
    for r in results:
        rule = r.rule
        sel = r.selected
        rules.append(
            (
                rule.name,
                ", ".join(rule.accounts) or "all",
                day(rule.before_ms),
                keeps_text(rule),
                years_text(rule.forget_after) if rule.forget_after else "",
                r.scope_total,
                sum(r.excluded.values()),
                r.kept_total,
                len(sel),
                sum(m.size for m in sel),
                day(sel[0].internal_date) if sel else "",
                day(sel[-1].internal_date) if sel else "",
            )
        )
        names = ["keep_if"] * bool(rule.exceptions)
        names += [k for k, ps in KEEP.items() if any(p in rule.protect for p in ps)]
        for k in names:
            kept.append((rule.name, f"kept: {KEPT_WHY[k]}", r.kept[k], r.kept_size[k]))
        for e, text in EXCLUSIONS.items():
            if r.excluded[e]:
                kept.append((rule.name, f"left out: {text}", r.excluded[e], None))
        by_year: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        by_sender: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for m in sel:
            for d, k in ((by_year, day(m.internal_date)[:4]), (by_sender, m.sender)):
                d[k][0] += 1
                d[k][1] += m.size
        years += [(rule.name, y, n, size) for y, (n, size) in sorted(by_year.items())]
        ranked = sorted(by_sender.items(), key=lambda kv: (-kv[1][1], kv[0]))[:top]
        senders += [(rule.name, s, n, size) for s, (n, size) in ranked]
    return [
        Table(
            "rules",
            "Rules",
            [
                ("Rule", "text"),
                ("Accounts", "text"),
                ("Before", "text"),
                ("Keeps", "text"),
                ("Forget after", "text"),
                ("In scope", "int"),
                ("Left out", "int"),
                ("Kept", "int"),
                ("Offload", "int"),
                ("Size", "size"),
                ("First", "text"),
                ("Last", "text"),
            ],
            rules,
            notes=[
                "In scope: the rule's scope, before its cut-off. Left out: already offloaded, in "
                "Trash or Spam, gone from its mailbox, not indexed, or taken by an earlier rule. "
                "Kept: its keep or keep_if applies. Offload: in the plan. Sizes are the "
                "mailbox's own figures. Keeps: its keep and keep_if (keep's default: mine, "
                "starred, conversations, labelled). Forget after: 'offload forget --due' "
                "deletes what the rule offloaded from the backup once it is that old and its "
                "mailbox has deleted it."
            ],
            limit=False,
        ),
        Table(
            "kept",
            "Kept and left out, per rule",
            [("Rule", "text"), ("Why", "text"), ("Messages", "int"), ("Size", "size")],
            kept,
            notes=["A message counts under every keep reason that applies to it."],
            limit=False,
        ),
        Table(
            "years",
            "Offload by year",
            [("Rule", "text"), ("Year", "text"), ("Messages", "int"), ("Size", "size")],
            years,
            limit=False,
        ),
        Table(
            "senders",
            f"Largest senders in the offload (top {top} per rule)",
            [("Rule", "text"), ("Sender", "text"), ("Messages", "int"), ("Size", "size")],
            senders,
            limit=False,
        ),
    ]
