"""
volume: how much mail arrives and goes out, over time.

Sent is mail with Gmail's SENT label (including mail to yourself); drafts
are left out; everything else counts as received. Forwarded is received
mail that Gmail auto-forwarded from another address (X-Forwarded-For,
the sidecar's forwarded_from), so the forwarding windows show as the
forwarded share.
"""

import sqlite3

from mailcellar.report.output import Table, day


def _prepare(con: sqlite3.Connection) -> None:
    con.executescript("""
        DROP TABLE IF EXISTS temp.vol;
        CREATE TEMP TABLE vol AS
        SELECT m.account, m.internal_date, coalesce(m.size_estimate, 0) AS size,
               strftime('%Y', m.internal_date / 1000, 'unixepoch') AS year,
               strftime('%Y-%m', m.internal_date / 1000, 'unixepoch') AS month,
               coalesce(l.sent, 0) AS sent, m.forwarded_from
        FROM all_messages m
        LEFT JOIN (SELECT account, gmail_id, max(label_id = 'SENT') AS sent,
                          max(label_id = 'DRAFT') AS draft
                   FROM all_message_labels GROUP BY account, gmail_id) l
               USING (account, gmail_id)
        WHERE NOT coalesce(l.draft, 0) OR coalesce(l.sent, 0);
    """)


PERIOD_COLUMNS = [
    ("Account", "text"),
    ("Received", "int"),
    ("Sent", "int"),
    ("Forwarded", "int"),
    ("Forwarded share", "pct"),
    ("Size", "size"),
]


def _by(con, period: str, order: str) -> list[tuple]:
    rows = con.execute(f"""
        SELECT {period}, account, sum(NOT sent), sum(sent),
               sum(NOT sent AND forwarded_from != ''), sum(size)
        FROM vol GROUP BY {period}, account ORDER BY {period} {order}, account""").fetchall()
    return [
        (p, a, rec, sent, fwd, fwd / rec if rec else 0, size) for p, a, rec, sent, fwd, size in rows
    ]


def totals(con) -> Table:
    rows = con.execute("""
        SELECT account, sum(NOT sent), sum(sent), sum(NOT sent AND forwarded_from != ''),
               sum(size), min(internal_date), max(internal_date)
        FROM vol GROUP BY account ORDER BY account""").fetchall()
    return Table(
        "totals",
        "Totals",
        [*PERIOD_COLUMNS[:5], ("Size", "size"), ("First", "text"), ("Last", "text")],
        [
            (a, r, s, f, f / r if r else 0, size, day(lo), day(hi))
            for a, r, s, f, size, lo, hi in rows
        ],
        notes=[
            "Sent: Gmail's SENT label (mail to yourself included). Drafts are left out. "
            "Forwarded: received mail auto-forwarded by Gmail from another address "
            "(X-Forwarded-For)."
        ],
        limit=False,
    )


def by_year(con) -> Table:
    return Table(
        "by-year",
        "By year",
        [("Year", "text"), *PERIOD_COLUMNS],
        _by(con, "year", "ASC"),
        limit=False,
    )


def by_month(con) -> Table:
    return Table(
        "by-month",
        "By month, newest first",
        [("Month", "text"), *PERIOD_COLUMNS],
        _by(con, "month", "DESC"),
    )


def forwarding(con) -> Table:
    rows = con.execute("""
        SELECT forwarded_from, account, count(*), sum(size), min(internal_date),
               max(internal_date),
               count(*) * 1.0 / (SELECT sum(NOT sent) FROM vol t WHERE t.account = vol.account)
        FROM vol WHERE NOT sent AND forwarded_from != ''
        GROUP BY forwarded_from, account ORDER BY count(*) DESC""").fetchall()
    return Table(
        "forwarding",
        "Forwarded from",
        [
            ("Forwarded from", "text"),
            ("Account", "text"),
            ("Messages", "int"),
            ("Size", "size"),
            ("First", "text"),
            ("Last", "text"),
            ("Share of received", "pct"),
        ],
        [(o, a, n, size, day(lo), day(hi), share) for o, a, n, size, lo, hi, share in rows],
        notes=["The address Gmail forwarded from, and the window it was active in."],
        limit=False,
    )


def build(con: sqlite3.Connection) -> list[Table]:
    _prepare(con)
    return [totals(con), by_year(con), by_month(con), forwarding(con)]
