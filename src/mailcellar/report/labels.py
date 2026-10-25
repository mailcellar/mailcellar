"""
labels: how user labels are used, and where they differ between accounts.

Only user labels (type 'user' in the sidecar's label catalogue) are
counted; system labels and categories are in the storage report. Names
are compared across accounts ignoring case. A nested label ('a/b') counts
only its own messages, not its children's.
"""

import sqlite3

from mailcellar.report.output import Table, day


def _prepare(con: sqlite3.Connection) -> None:
    con.executescript("""
        DROP TABLE IF EXISTS temp.ulab;
        CREATE TEMP TABLE ulab AS
        SELECT c.account, c.label_id, c.name, lower(c.name) AS key,
               count(m.gmail_id) AS messages, coalesce(sum(m.size_estimate), 0) AS size,
               min(m.internal_date) AS first_ms, max(m.internal_date) AS last_ms
        FROM all_labels c
        LEFT JOIN all_message_labels l ON l.account = c.account AND l.label_id = c.label_id
        LEFT JOIN all_messages m ON m.account = l.account AND m.gmail_id = l.gmail_id
        WHERE c.type = 'user'
        GROUP BY c.account, c.label_id;
    """)


def by_label(con) -> Table:
    rows = con.execute("""
        SELECT u.name, u.account, u.messages, u.size, u.first_ms, u.last_ms,
               (SELECT group_concat(o.account || ' (' || o.messages || ')', '; ')
                FROM ulab o WHERE o.key = u.key AND o.account != u.account)
        FROM ulab u WHERE u.messages > 0 ORDER BY u.messages DESC, u.name""").fetchall()
    return Table(
        "by-label",
        "User labels in use",
        [
            ("Label", "text"),
            ("Account", "text"),
            ("Messages", "int"),
            ("Size", "size"),
            ("First", "text"),
            ("Last", "text"),
            ("Same name elsewhere", "text"),
        ],
        [(n, a, m, s, day(lo), day(hi), o or "") for n, a, m, s, lo, hi, o in rows],
        notes=["Same name elsewhere: the label in other accounts, with its message count."],
    )


def one_account_only(con, accounts: list[str]) -> Table:
    rows = con.execute("""
        SELECT u.name, u.account, u.messages, u.size,
               (SELECT group_concat(o.account, '; ') FROM ulab o
                WHERE o.key = u.key AND o.account != u.account)
        FROM ulab u
        WHERE u.messages > 0
          AND NOT EXISTS (SELECT 1 FROM ulab o
                          WHERE o.key = u.key AND o.account != u.account AND o.messages > 0)
        ORDER BY u.account, u.messages DESC""").fetchall()
    if len(accounts) < 2:
        rows, notes = [], ["Only one account chosen."]
    else:
        shared, used = con.execute("""
            SELECT count(*), coalesce(sum(used = n), 0)
            FROM (SELECT key, count(DISTINCT account) AS n,
                         count(DISTINCT CASE WHEN messages > 0 THEN account END) AS used
                  FROM ulab GROUP BY key)
            WHERE n > 1""").fetchone()
        notes = [
            "Labels with messages in one account and none in the others: mail filed in one "
            "mailbox only. 'Empty elsewhere' names the accounts where the label exists unused.",
            f"{shared:,} label names exist in more than one account; {used:,} of them have "
            "messages in each.",
        ]
    return Table(
        "one-account-only",
        "Labels used in one account only",
        [
            ("Label", "text"),
            ("Account", "text"),
            ("Messages", "int"),
            ("Size", "size"),
            ("Empty elsewhere", "text"),
        ],
        [(n, a, m, s, e or "") for n, a, m, s, e in rows],
        notes=notes,
    )


def empty(con) -> Table:
    rows = con.execute("""
        SELECT u.name, u.account,
               (SELECT count(*) FROM ulab c
                WHERE c.account = u.account AND c.key LIKE u.key || '/%') AS nested,
               u.label_id
        FROM ulab u WHERE u.messages = 0 ORDER BY u.account, u.key""").fetchall()
    return Table(
        "empty",
        "User labels with no messages",
        [("Label", "text"), ("Account", "text"), ("Nested labels", "int"), ("Label ID", "text")],
        rows,
        notes=[
            "Nested labels: labels under this one ('name/…'), which keeps it as a parent. "
            "Trash and Spam are not backed up, so a label used only there shows as empty."
        ],
        limit=False,
    )


def unlabelled(con) -> Table:
    rows = con.execute("""
        SELECT account, count(*), sum(user), sum(NOT user), sum(NOT user AND inbox),
               sum(NOT user AND NOT inbox AND NOT sent), sum(NOT user AND NOT inbox AND sent)
        FROM (SELECT m.account, m.gmail_id,
                     coalesce(max(x.type = 'user'), 0) AS user,
                     coalesce(max(x.label_id = 'INBOX'), 0) AS inbox,
                     coalesce(max(x.label_id IN ('SENT', 'DRAFT')), 0) AS sent
              FROM all_messages m
              LEFT JOIN all_message_labels x USING (account, gmail_id)
              GROUP BY m.account, m.gmail_id)
        GROUP BY account ORDER BY account""").fetchall()
    return Table(
        "unlabelled",
        "Messages with and without a user label",
        [
            ("Account", "text"),
            ("Messages", "int"),
            ("With a user label", "int"),
            ("Without", "int"),
            ("…in Inbox", "int"),
            ("…archived", "int"),
            ("…sent or draft, not in Inbox", "int"),
        ],
        rows,
        notes=["Archived: not in Inbox, not sent, no user label — findable only by search."],
        limit=False,
    )


def build(con: sqlite3.Connection, accounts: list[str]) -> list[Table]:
    _prepare(con)
    return [by_label(con), one_account_only(con, accounts), empty(con), unlabelled(con)]
