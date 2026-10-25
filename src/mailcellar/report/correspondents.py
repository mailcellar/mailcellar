"""
correspondents: who you write to, who writes to you, and how often your
mail gets a reply.

Your mail is mail with Gmail's SENT label, or from an address you have
sent from (the backed-up accounts and any send-as aliases found in SENT
mail); drafts are left out. A message in both accounts (same Message-ID)
counts once in the combined tables. Senders are resolved as in the bulk
report (mail relayed by a list on your own domain counts under its
original sender), and "personal" mail is mail from others without bulk
signals.

A reply is a message from someone else whose In-Reply-To is one of your
Message-IDs, in either account; the looser "later mail in the thread"
counts any later message from others in the same Gmail thread, in either
account's copy of the thread.
"""

import sqlite3
import statistics
from collections import Counter, defaultdict

from mailcellar.report import bulk
from mailcellar.report.output import Table, day

HOUR_MS = 3_600_000


def _prepare(con: sqlite3.Connection, accounts: list[str]) -> None:
    bulk.prepare_messages(con, accounts)
    con.executescript(f"""
        DROP TABLE IF EXISTS temp.flags;
        CREATE TEMP TABLE flags AS
        SELECT account, gmail_id, max(label_id = 'SENT') AS sent, max(label_id = 'DRAFT') AS draft
        FROM all_message_labels GROUP BY account, gmail_id;
        CREATE INDEX temp.flags_key ON flags (account, gmail_id);

        DROP TABLE IF EXISTS temp.own;
        CREATE TEMP TABLE own (address PRIMARY KEY);
        INSERT OR IGNORE INTO own VALUES {",".join(f"({bulk.sql_list([a])})" for a in accounts)};
        INSERT OR IGNORE INTO own
        SELECT DISTINCT m.sender_addr FROM all_messages m JOIN flags f USING (account, gmail_id)
        WHERE f.sent AND m.sender_addr != '';

        DROP TABLE IF EXISTS temp.cm;
        CREATE TEMP TABLE cm AS
        SELECT bm.account, bm.gmail_id, bm.thread_id, bm.internal_date AS ms, bm.message_id,
               bm.in_reply_to, bm.sender, m.sender_name,
               CASE WHEN bm.message_id != '' THEN bm.message_id
                    ELSE bm.account || ' ' || bm.gmail_id END AS key,
               (coalesce(f.sent, 0) OR m.sender_addr IN own) AS mine,
               {bulk.BULK_SIGNAL} AS bulk
        FROM bm
        JOIN all_messages m USING (account, gmail_id)
        LEFT JOIN flags f USING (account, gmail_id)
        WHERE NOT (coalesce(f.draft, 0) AND NOT coalesce(f.sent, 0));
        CREATE INDEX temp.cm_key ON cm (key);
        CREATE INDEX temp.cm_thread ON cm (account, thread_id);

        -- your messages and theirs, one row per message across accounts
        DROP TABLE IF EXISTS temp.ymsg;
        CREATE TEMP TABLE ymsg AS
        SELECT key, min(ms) AS ms, max(message_id) AS message_id, max(in_reply_to) AS in_reply_to
        FROM cm WHERE mine GROUP BY key;
        CREATE INDEX temp.ymsg_mid ON ymsg (message_id);

        DROP TABLE IF EXISTS temp.tmsg;
        CREATE TEMP TABLE tmsg AS
        SELECT key, max(sender) AS sender, min(ms) AS ms, max(message_id) AS message_id,
               max(in_reply_to) AS in_reply_to, max(bulk) AS bulk
        FROM cm WHERE NOT mine AND sender != 'unknown' GROUP BY key;
        CREATE INDEX temp.tmsg_irt ON tmsg (in_reply_to);
        CREATE INDEX temp.tmsg_mid ON tmsg (message_id);

        -- who your messages went to (not yourself)
        DROP TABLE IF EXISTS temp.yto;
        CREATE TEMP TABLE yto AS
        SELECT DISTINCT cm.account, cm.key, r.address, r.name
        FROM cm JOIN all_recipients r USING (account, gmail_id)
        WHERE cm.mine AND r.address != '' AND r.address NOT IN own;
        CREATE INDEX temp.yto_key ON yto (key);

        -- replies to you, and your replies
        DROP TABLE IF EXISTS temp.reply;
        CREATE TEMP TABLE reply AS
        SELECT y.key AS your_key, t.key AS their_key, t.sender, t.ms - y.ms AS delay
        FROM ymsg y JOIN tmsg t ON t.in_reply_to = y.message_id
        WHERE y.message_id != '';

        DROP TABLE IF EXISTS temp.yreply;
        CREATE TEMP TABLE yreply AS
        SELECT y.key AS your_key, t.sender
        FROM ymsg y JOIN tmsg t ON t.message_id = y.in_reply_to
        WHERE y.in_reply_to != '';

        DROP TABLE IF EXISTS temp.first_reply;
        CREATE TEMP TABLE first_reply (key PRIMARY KEY, delay);
        INSERT INTO first_reply SELECT your_key, max(min(delay), 0) FROM reply GROUP BY your_key;

        DROP TABLE IF EXISTS temp.later;
        CREATE TEMP TABLE later (key PRIMARY KEY);
        INSERT OR IGNORE INTO later
        SELECT y.key FROM cm y JOIN cm t
               ON t.account = y.account AND t.thread_id = y.thread_id AND NOT t.mine
              AND t.ms > y.ms
        WHERE y.mine AND y.thread_id != '';
    """)
    _correspondents(con)


def _correspondents(con) -> None:
    """TEMP corr: one row per address, both directions, across accounts."""
    con.executescript("""
        DROP TABLE IF EXISTS temp.ev;
        CREATE TEMP TABLE ev AS
        SELECT 'out' AS dir, y.account, y.key, y.address, y.name, m.ms, 0 AS bulk
        FROM yto y JOIN ymsg m USING (key)
        UNION ALL
        SELECT 'in', account, key, sender, sender_name, ms, bulk FROM cm
        WHERE NOT mine AND sender != 'unknown';

        DROP TABLE IF EXISTS temp.corr;
        CREATE TEMP TABLE corr AS
        SELECT e.address,
               count(DISTINCT CASE WHEN dir = 'out' THEN key END) AS you_wrote,
               count(DISTINCT CASE WHEN dir = 'in' THEN key END) AS they_wrote,
               count(DISTINCT CASE WHEN dir = 'in' AND NOT bulk THEN key END) AS personal,
               coalesce(r.n, 0) AS they_replied, coalesce(yr.n, 0) AS you_replied,
               min(ms) AS first_ms, max(ms) AS last_ms,
               group_concat(DISTINCT account) AS accounts
        FROM ev e
        LEFT JOIN (SELECT sender, count(DISTINCT their_key) AS n FROM reply GROUP BY sender) r
               ON r.sender = e.address
        LEFT JOIN (SELECT sender, count(DISTINCT your_key) AS n FROM yreply GROUP BY sender) yr
               ON yr.sender = e.address
        GROUP BY e.address;
    """)


def _names(con) -> dict[str, str]:
    """Most used display name per address."""
    counts: dict[str, Counter] = defaultdict(Counter)
    for address, name, n in con.execute(
        "SELECT address, name, count(*) FROM ev WHERE name != '' GROUP BY address, name"
    ):
        counts[address][name] += n
    return {a: c.most_common(1)[0][0] for a, c in counts.items()}


def _median_hours(delays: list[int]) -> float | None:
    return statistics.median(delays) / HOUR_MS if delays else None


def summary(con, accounts: list[str]) -> Table:
    rows = []
    for account in [*accounts, None] if len(accounts) > 1 else accounts:
        p = {"acc": account}
        acc = "(:acc IS NULL OR account = :acc)"
        mine, recips, replied, later = con.execute(
            f"""SELECT count(DISTINCT key),
                       (SELECT count(DISTINCT address) FROM yto WHERE {acc}),
                       count(DISTINCT CASE WHEN key IN (SELECT key FROM first_reply) THEN key END),
                       count(DISTINCT CASE WHEN key IN later THEN key END)
                FROM cm WHERE mine AND {acc}""",
            p,
        ).fetchone()
        delays = [
            d
            for (d,) in con.execute(
                f"""SELECT delay FROM first_reply
                    WHERE key IN (SELECT key FROM cm WHERE mine AND {acc})""",
                p,
            )
        ]
        theirs, personal, senders = con.execute(
            f"""SELECT count(DISTINCT key), count(DISTINCT CASE WHEN NOT bulk THEN key END),
                       count(DISTINCT CASE WHEN NOT bulk THEN sender END)
                FROM cm WHERE NOT mine AND sender != 'unknown' AND {acc}""",
            p,
        ).fetchone()
        two_way = con.execute(
            f"""SELECT count(*) FROM (SELECT address FROM ev WHERE {acc}
                GROUP BY address HAVING max(dir = 'out') AND max(dir = 'in' AND NOT bulk))""",
            p,
        ).fetchone()[0]
        rows.append(
            (
                account or "all accounts",
                mine,
                recips,
                replied,
                replied / mine if mine else 0,
                later,
                _median_hours(delays),
                theirs,
                personal,
                senders,
                two_way,
            )
        )
    own = [a for (a,) in con.execute("SELECT address FROM own ORDER BY address")]
    return Table(
        "summary",
        "Summary",
        [
            ("Account", "text"),
            ("Your messages", "int"),
            ("Recipients", "int"),
            ("Replied", "int"),
            ("Reply rate", "pct"),
            ("Later mail in thread", "int"),
            ("Median hours to reply", "num"),
            ("From others", "int"),
            ("Personal", "int"),
            ("Personal senders", "int"),
            ("Two-way", "int"),
        ],
        rows,
        notes=[
            f"Your addresses: {', '.join(own)}.",
            "Replied: someone else's message has In-Reply-To set to yours. Later mail in "
            "thread: any later message from others in its thread (a looser measure). Both "
            "count a message once, wherever the reply or later mail arrived. "
            "Personal: mail from others without bulk signals. Two-way: you wrote to them and "
            "they sent you personal mail.",
        ],
        limit=False,
    )


CORR_COLUMNS = [
    ("Address", "text"),
    ("Name", "text"),
    ("You wrote", "int"),
    ("They wrote", "int"),
    ("Personal", "int"),
    ("They replied", "int"),
    ("You replied", "int"),
    ("First", "text"),
    ("Last", "text"),
    ("Accounts", "text"),
]


def _corr_rows(con, where: str, order: str, names: dict[str, str]) -> list[tuple]:
    rows = con.execute(f"""
        SELECT address, you_wrote, they_wrote, personal, they_replied, you_replied,
               first_ms, last_ms, accounts
        FROM corr WHERE {where} ORDER BY {order}, address""").fetchall()
    return [
        (a, names.get(a, ""), yw, tw, p, tr, yr, day(lo), day(hi), accts.replace(",", ", "))
        for a, yw, tw, p, tr, yr, lo, hi, accts in rows
    ]


def correspondents(con) -> list[Table]:
    names = _names(con)
    return [
        Table(
            "you-write-to",
            "Who you write to",
            CORR_COLUMNS,
            _corr_rows(con, "you_wrote > 0", "you_wrote DESC, last_ms DESC", names),
            notes=[
                "You wrote: your messages with them in To, Cc or Bcc. They wrote: all their "
                "mail to you; Personal: without bulk signals. They replied / You replied: "
                "by In-Reply-To. First / Last: either direction."
            ],
        ),
        Table(
            "write-to-you",
            "Who writes to you (personal mail)",
            CORR_COLUMNS,
            _corr_rows(con, "personal > 0", "personal DESC, last_ms DESC", names),
        ),
        Table(
            "two-way",
            "Two-way correspondents",
            CORR_COLUMNS,
            _corr_rows(
                con,
                "you_wrote > 0 AND personal > 0",
                "they_replied + you_replied DESC, you_wrote DESC",
                names,
            ),
            notes=[
                "You wrote to them and they sent you personal mail; ordered by replies "
                "exchanged (theirs to you plus yours to them), since a sender of many "
                "automated messages without bulk headers can top the plain counts."
            ],
        ),
    ]


def replies_by_year(con) -> Table:
    data: dict[tuple, list] = {}
    for year, account, key, delay in con.execute("""
        SELECT strftime('%Y', cm.ms / 1000, 'unixepoch'), cm.account, cm.key, f.delay
        FROM cm LEFT JOIN first_reply f USING (key) WHERE cm.mine"""):
        n, delays, seen = data.setdefault((year, account), [0, [], set()])
        if key in seen:
            continue
        seen.add(key)
        data[(year, account)][0] = n + 1
        if delay is not None:
            delays.append(delay)
    rows = [
        (y, a, n, len(d), len(d) / n if n else 0, _median_hours(d))
        for (y, a), (n, d, _s) in sorted(data.items())
    ]
    return Table(
        "replies-by-year",
        "Your mail and replies, by year",
        [
            ("Year", "text"),
            ("Account", "text"),
            ("Your messages", "int"),
            ("Replied", "int"),
            ("Reply rate", "pct"),
            ("Median hours to reply", "num"),
        ],
        rows,
        limit=False,
    )


def build(con: sqlite3.Connection, accounts: list[str]) -> list[Table]:
    _prepare(con, accounts)
    return [summary(con, accounts), *correspondents(con), replies_by_year(con)]
