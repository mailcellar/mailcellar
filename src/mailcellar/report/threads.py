"""
threads: how conversations are shaped, per account and across accounts.

A thread is Gmail's thread ID within one account (drafts included, as
Gmail shows them). Threads in different accounts are joined when they
share a Message-ID: the same message delivered to, forwarded to or sent
from both, so one conversation seen from both mailboxes.
"""

import sqlite3

from mailcellar.report.output import Table, day

BUCKETS = [(1, 1), (2, 2), (3, 5), (6, 10), (11, 20), (21, 50), (51, None)]


def _prepare(con: sqlite3.Connection) -> None:
    con.executescript("""
        DROP TABLE IF EXISTS temp.tmsg;
        CREATE TEMP TABLE tmsg AS
        SELECT m.account, m.gmail_id, m.thread_id, m.internal_date, m.message_id,
               m.sender_addr, coalesce(m.size_estimate, 0) AS size,
               first_value(m.subject) OVER (PARTITION BY m.account, m.thread_id
                                            ORDER BY m.internal_date, m.gmail_id) AS subject,
               coalesce(l.sent, 0) AS sent
        FROM all_messages m
        LEFT JOIN (SELECT account, gmail_id, max(label_id = 'SENT') AS sent
                   FROM all_message_labels GROUP BY account, gmail_id) l
               USING (account, gmail_id)
        WHERE m.thread_id != '';

        DROP TABLE IF EXISTS temp.thr;
        CREATE TEMP TABLE thr AS
        SELECT account, thread_id, max(subject) AS subject, count(*) AS messages,
               sum(sent) AS sent, count(DISTINCT nullif(sender_addr, '')) AS participants,
               sum(size) AS size, min(internal_date) AS first_ms, max(internal_date) AS last_ms
        FROM tmsg GROUP BY account, thread_id;
        CREATE INDEX temp.thr_key ON thr (account, thread_id);

        DROP TABLE IF EXISTS temp.tmid;
        CREATE TEMP TABLE tmid AS
        SELECT DISTINCT account, thread_id, message_id FROM tmsg WHERE message_id != '';
        CREATE INDEX temp.tmid_mid ON tmid (message_id);
    """)


def summary(con) -> Table:
    rows = con.execute("""
        SELECT account, count(*), sum(messages), sum(messages = 1), sum(sent > 0),
               max(messages), max(last_ms - first_ms) / 86400000
        FROM thr GROUP BY account ORDER BY account""").fetchall()
    return Table(
        "summary",
        "Summary",
        [
            ("Account", "text"),
            ("Threads", "int"),
            ("Messages", "int"),
            ("Single-message", "pct"),
            ("With your mail", "int"),
            ("Most messages", "int"),
            ("Longest span (days)", "int"),
        ],
        [(a, t, m, single / t, mine, most, span) for a, t, m, single, mine, most, span in rows],
        notes=["With your mail: threads with at least one message you sent (SENT label)."],
        limit=False,
    )


def distribution(con) -> Table:
    rows = []
    totals = dict(con.execute("SELECT account, count(*) FROM thr GROUP BY account"))
    msg_totals = dict(con.execute("SELECT account, sum(messages) FROM thr GROUP BY account"))
    for account in sorted(totals):
        for lo, hi in BUCKETS:
            n, m = con.execute(
                """SELECT count(*), coalesce(sum(messages), 0) FROM thr
                   WHERE account = ? AND messages >= ? AND messages <= ?""",
                (account, lo, hi if hi is not None else 1 << 62),
            ).fetchone()
            label = str(lo) if lo == hi else f"{lo}–{hi}" if hi else f"{lo}+"
            rows.append((label, account, n, m, n / totals[account], m / msg_totals[account]))
    return Table(
        "distribution",
        "Thread sizes",
        [
            ("Messages per thread", "text"),
            ("Account", "text"),
            ("Threads", "int"),
            ("Messages", "int"),
            ("Share of threads", "pct"),
            ("Share of messages", "pct"),
        ],
        rows,
        limit=False,
    )


THREAD_COLUMNS = [
    ("Subject", "text"),
    ("Account", "text"),
    ("Messages", "int"),
    ("Yours", "int"),
    ("Participants", "int"),
    ("Size", "size"),
    ("First", "text"),
    ("Last", "text"),
    ("Days", "int"),
    ("Thread ID", "text"),
]


def _threads(con, where: str, order: str) -> list[tuple]:
    rows = con.execute(f"""
        SELECT subject, account, messages, sent, participants, size, first_ms, last_ms,
               (last_ms - first_ms) / 86400000, thread_id
        FROM thr WHERE {where} ORDER BY {order}""").fetchall()
    return [
        (s, a, n, y, p, size, day(lo), day(hi), d, t) for s, a, n, y, p, size, lo, hi, d, t in rows
    ]


def longest(con) -> tuple[Table, Table, Table]:
    return (
        Table(
            "longest",
            "Most messages",
            THREAD_COLUMNS,
            _threads(con, "messages > 1", "messages DESC, last_ms DESC"),
            notes=[
                "Gmail starts a new thread after 100 messages. One sender's burst under one "
                "subject (a mailing run, a notification storm) shows as a long thread "
                "with one participant and no span."
            ],
        ),
        Table(
            "longest-yours",
            "Most messages, with your mail",
            THREAD_COLUMNS,
            _threads(con, "messages > 1 AND sent > 0", "messages DESC, last_ms DESC"),
        ),
        Table(
            "longest-running",
            "Longest-running (first to last message)",
            THREAD_COLUMNS,
            _threads(con, "messages > 1", "last_ms - first_ms DESC"),
        ),
    )


def across_accounts(con, accounts: list[str]) -> Table:
    rows = con.execute("""
        SELECT a.subject, a.account, a.messages, b.account, b.messages, p.shared,
               min(a.first_ms, b.first_ms), max(a.last_ms, b.last_ms), a.thread_id, b.thread_id
        FROM (SELECT x.account AS acc_a, x.thread_id AS thr_a, y.account AS acc_b,
                     y.thread_id AS thr_b, count(*) AS shared
              FROM tmid x JOIN tmid y ON y.message_id = x.message_id AND x.account < y.account
              GROUP BY 1, 2, 3, 4) p
        JOIN thr a ON a.account = p.acc_a AND a.thread_id = p.thr_a
        JOIN thr b ON b.account = p.acc_b AND b.thread_id = p.thr_b
        ORDER BY a.messages + b.messages DESC, p.shared DESC""").fetchall()
    rows = [
        (s, aa, an, ba, bn, sh, day(lo), day(hi), at, bt)
        for s, aa, an, ba, bn, sh, lo, hi, at, bt in rows
    ]
    multi = [r for r in rows if r[2] + r[4] > 2]
    notes = (
        [
            f"{len(rows):,} pairs of threads share a Message-ID; {len(multi):,} of them have "
            "more than one message on either side (a conversation, not just one message "
            "seen in both)."
        ]
        if len(accounts) > 1
        else ["Only one account chosen."]
    )
    return Table(
        "across-accounts",
        "Threads in more than one account",
        [
            ("Subject", "text"),
            ("Account", "text"),
            ("Messages", "int"),
            ("Other account", "text"),
            ("Messages", "int"),
            ("Shared", "int"),
            ("First", "text"),
            ("Last", "text"),
            ("Thread ID", "text"),
            ("Other thread ID", "text"),
        ],
        rows,
        notes=notes,
    )


def build(con: sqlite3.Connection, accounts: list[str]) -> list[Table]:
    _prepare(con)
    return [summary(con), distribution(con), *longest(con), across_accounts(con, accounts)]
