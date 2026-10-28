"""
bulk: which senders and mailing lists send the most, and which are worth
unsubscribing from or clearing out.

A source is a mailing list (List-Id) or, without one, a sender address.
Mail relayed by a list on one of your own domains (e.g. a catch-all
Google Group, catchall.example.org) is grouped by its original sender
instead: the relay adds its own List-Id, List-Unsubscribe and Precedence
to everything it passes on, so for those messages only Gmail's categories
count as a bulk signal, and the original sender is the Reply-To when the
relay rewrote From to its own address.

Your own mail (sent, or from one of the backed-up addresses) is not a
source. A message has bulk signals when it has a List-Id or
List-Unsubscribe header, Precedence bulk/list/junk, or sits in Gmail's
Promotions, Updates or Forums categories; a source is bulk when at least
`min_bulk_share` of its messages do (default: half).

Engagement replaces read state (most mail in a long-kept mailbox has been
read): a source is engaged if any of its messages is in a thread you sent
to, was replied to (In-Reply-To, from either account), or came from an
address you have written to (To/Cc/Bcc of your sent mail, either account).

The report never unsubscribes or removes anything.
"""

import sqlite3
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from mailcellar.report.output import Table, day, human_size

CATEGORIES = ("CATEGORY_PROMOTIONS", "CATEGORY_UPDATES", "CATEGORY_FORUMS")
# Address domains that are shared providers, never "your own" list host.
PUBLIC_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "hotmail.com",
    "outlook.com",
    "live.com",
    "yahoo.com",
    "icloud.com",
    "me.com",
    "aol.com",
}
DAY_MS = 86_400_000


@dataclass(frozen=True)
class Options:
    min_messages: int = 10  # candidates: at least this many messages
    active_days: int = 90  # "still sending": a message within this many days
    min_bulk_share: float = 0.5  # a source is bulk when this share of its mail has signals
    now_ms: int | None = None  # for tests; default: now


def own_domains(accounts: list[str]) -> list[str]:
    return sorted({a.rsplit("@", 1)[1] for a in accounts if "@" in a} - PUBLIC_DOMAINS)


def sql_list(values) -> str:
    return ",".join("'" + v.replace("'", "''") + "'" for v in values) or "''"


# Per-message bulk signal, over a row of temp table bm.
BULK_SIGNAL = """(bm.list_id != '' OR bm.list_unsubscribe != ''
                OR bm.precedence IN ('bulk', 'list', 'junk') OR bm.categorised)"""


def prepare_messages(con: sqlite3.Connection, accounts: list[str]) -> None:
    """
    TEMP bm: every message, with relayed mail's sender and list headers
    resolved as described above, and its category, user-label and sent flags.
    Shared with the correspondents report.
    """
    domains = own_domains(accounts)
    relay = (
        " OR ".join(
            f"m.list_id = {sql_list([d])} OR m.list_id LIKE {sql_list(['%.' + d])}" for d in domains
        )
        or "0"
    )
    own_sender = " OR ".join(f"m.sender_addr LIKE {sql_list(['%@' + d])}" for d in domains) or "0"
    con.executescript(f"""
        DROP TABLE IF EXISTS temp.bm;
        CREATE TEMP TABLE bm AS
        SELECT m.account, m.gmail_id, m.thread_id, m.internal_date, m.message_id,
               m.in_reply_to, coalesce(m.size_estimate, 0) AS size,
               ({relay}) AS relayed,
               CASE WHEN ({relay}) AND ({own_sender}) AND m.reply_to != '' THEN m.reply_to
                    WHEN m.sender_addr = '' THEN 'unknown'
                    ELSE m.sender_addr END AS sender,
               CASE WHEN ({relay}) THEN '' ELSE m.list_id END AS list_id,
               CASE WHEN ({relay}) THEN '' ELSE m.list_unsubscribe END AS list_unsubscribe,
               CASE WHEN ({relay}) THEN '' ELSE m.precedence END AS precedence,
               coalesce(l.categorised, 0) AS categorised,
               coalesce(l.user_labelled, 0) AS user_labelled,
               coalesce(l.sent, 0) AS sent
        FROM all_messages m
        LEFT JOIN (SELECT account, gmail_id,
                          max(label_id IN ({sql_list(CATEGORIES)})) AS categorised,
                          max(type = 'user') AS user_labelled,
                          max(label_id IN ('SENT', 'DRAFT')) AS sent
                   FROM all_message_labels GROUP BY account, gmail_id) l
               USING (account, gmail_id);
    """)


def prepare_engagement(con: sqlite3.Connection) -> None:
    """
    TEMP sent_threads (account, thread_id), replied_ids (message_id) and
    wrote_to (address), from bm's sent mail. Shared with offload.
    """
    con.executescript("""
        DROP TABLE IF EXISTS temp.sent_threads;
        CREATE TEMP TABLE sent_threads (account, thread_id, PRIMARY KEY (account, thread_id));
        INSERT INTO sent_threads
        SELECT DISTINCT account, thread_id FROM bm WHERE sent AND thread_id != '';

        DROP TABLE IF EXISTS temp.replied_ids;
        CREATE TEMP TABLE replied_ids (message_id PRIMARY KEY);
        INSERT INTO replied_ids
        SELECT DISTINCT in_reply_to FROM bm WHERE sent AND in_reply_to != '';

        DROP TABLE IF EXISTS temp.wrote_to;
        CREATE TEMP TABLE wrote_to (address PRIMARY KEY);
        INSERT INTO wrote_to
        SELECT DISTINCT r.address FROM all_recipients r
        JOIN bm USING (account, gmail_id) WHERE bm.sent;
    """)


def prepare_sources(con: sqlite3.Connection, accounts: list[str], opts: Options, now: int) -> None:
    """
    TEMP src_msg (bm with each message's source, bulk signal and engagement)
    and sources (one row per account and source). Shared with offload suggest.
    """
    prepare_messages(con, accounts)
    prepare_engagement(con)
    con.executescript(f"""
        DROP TABLE IF EXISTS temp.src_msg;
        CREATE TEMP TABLE src_msg AS
        SELECT bm.*,
               CASE WHEN bm.list_id != '' THEN bm.list_id ELSE bm.sender END AS source,
               CASE WHEN bm.list_id != '' THEN 'list'
                    WHEN bm.relayed THEN 'relayed' ELSE 'sender' END AS kind,
               {BULK_SIGNAL} AS bulk_signal,
               (st.thread_id IS NOT NULL OR ri.message_id IS NOT NULL
                OR wt.address IS NOT NULL) AS engaged
        FROM bm
        LEFT JOIN sent_threads st ON st.account = bm.account AND st.thread_id = bm.thread_id
        LEFT JOIN replied_ids ri ON ri.message_id = bm.message_id AND bm.message_id != ''
        LEFT JOIN wrote_to wt ON wt.address = bm.sender
        WHERE NOT bm.sent AND bm.sender NOT IN ({sql_list(accounts)});
    """)
    con.execute("DROP TABLE IF EXISTS temp.sources")
    con.execute(
        """CREATE TEMP TABLE sources AS
           SELECT account, source,
                  CASE WHEN max(kind = 'list') THEN 'list'
                       WHEN max(kind = 'relayed') THEN 'relayed' ELSE 'sender' END AS kind,
                  count(*) AS messages, sum(internal_date >= ?) AS last_year,
                  sum(size) AS size, min(internal_date) AS first_ms, max(internal_date) AS last_ms,
                  sum(bulk_signal) * 1.0 / count(*) AS bulk_share,
                  sum(bulk_signal) * 1.0 / count(*) >= ? AS bulk,
                  sum(categorised) * 1.0 / count(*) AS categorised_share,
                  max(engaged) AS engaged, max(user_labelled) AS user_labelled,
                  CASE WHEN sum(list_unsubscribe IN ('mailto', 'both')) > 0
                            AND sum(list_unsubscribe IN ('https', 'both')) > 0 THEN 'both'
                       WHEN sum(list_unsubscribe IN ('mailto', 'both')) > 0 THEN 'mailto'
                       WHEN sum(list_unsubscribe IN ('https', 'both')) > 0 THEN 'https'
                       ELSE '' END AS unsubscribe
           FROM src_msg GROUP BY account, source""",
        (now - 365 * DAY_MS, opts.min_bulk_share),
    )


CANDIDATE = "bulk AND messages >= :min AND NOT engaged AND NOT user_labelled"


def summary(con, opts: Options, now: int) -> Table:
    p = {"min": opts.min_messages, "active": now - opts.active_days * DAY_MS}
    rows = []
    for (account,) in con.execute("SELECT DISTINCT account FROM bm ORDER BY account"):
        p["account"] = account
        all_mail = con.execute("SELECT count(*) FROM bm WHERE account = :account", p).fetchone()
        m = con.execute(
            """SELECT count(*), sum(bulk_signal),
                      coalesce(sum(CASE WHEN bulk_signal THEN size END), 0),
                      sum(list_id != ''), sum(list_unsubscribe != ''), sum(relayed)
               FROM src_msg WHERE account = :account""",
            p,
        ).fetchone()
        s = con.execute(
            f"""SELECT sum(bulk), coalesce(sum(CASE WHEN bulk THEN messages END), 0),
                       coalesce(sum({CANDIDATE}), 0),
                       coalesce(sum(CASE WHEN {CANDIDATE} THEN messages END), 0),
                       coalesce(sum(CASE WHEN {CANDIDATE} THEN size END), 0),
                       coalesce(sum(CASE WHEN {CANDIDATE} AND last_ms >= :active THEN 1 END), 0)
                FROM sources WHERE account = :account""",
            p,
        ).fetchone()
        rows.append((account, all_mail[0], *m, *s))
    return Table(
        "summary",
        "Summary",
        [
            ("Account", "text"),
            ("All mail", "int"),
            ("From others", "int"),
            ("Bulk signals", "int"),
            ("Their size", "size"),
            ("List-Id", "int"),
            ("List-Unsubscribe", "int"),
            ("Relayed", "int"),
            ("Bulk sources", "int"),
            ("Their messages", "int"),
            ("Candidates", "int"),
            ("Their messages", "int"),
            ("Their size", "size"),
            ("Still sending", "int"),
        ],
        rows,
        notes=[
            "From others: not sent by you, not from a backed-up address. Bulk signals: "
            "List-Id, List-Unsubscribe, Precedence bulk/list/junk, or Promotions/Updates/"
            "Forums. Relayed: passed on by a list on your own domain "
            f"({', '.join(own_domains_of(con)) or 'none'}), grouped by original sender.",
            f"Bulk source: at least {opts.min_bulk_share:.0%} of its messages have bulk "
            f"signals. Candidates: bulk sources with at least {opts.min_messages} messages, "
            f"no engagement and no user labels; still sending = a message in the last "
            f"{opts.active_days} days.",
        ],
        limit=False,
    )


def own_domains_of(con) -> list[str]:
    return own_domains([a for (a,) in con.execute("SELECT DISTINCT account FROM bm")])


SOURCE_COLUMNS = [
    ("Source", "text"),
    ("Kind", "text"),
    ("Account", "text"),
    ("Messages", "int"),
    ("Last year", "int"),
    ("Size", "size"),
    ("First", "text"),
    ("Last", "text"),
    ("Bulk signals", "pct"),
    ("In categories", "pct"),
    ("Unsubscribe", "text"),
    ("Engaged", "text"),
    ("User-labelled", "text"),
]


def _source_rows(con, where: str, order: str, params: dict) -> list[tuple]:
    rows = con.execute(
        f"""SELECT source, kind, account, messages, last_year, size, first_ms, last_ms,
                   bulk_share, categorised_share, unsubscribe, engaged, user_labelled
            FROM sources WHERE {where} ORDER BY {order}""",
        params,
    ).fetchall()
    return [
        (
            src,
            kind,
            acct,
            n,
            recent,
            size,
            day(first),
            day(last),
            bulk_share,
            share,
            unsub or "—",
            "yes" if engaged else "no",
            "yes" if labelled else "no",
        )
        for (
            src,
            kind,
            acct,
            n,
            recent,
            size,
            first,
            last,
            bulk_share,
            share,
            unsub,
            engaged,
            labelled,
        ) in rows
    ]


def top_sources(con) -> tuple[Table, Table]:
    return (
        Table(
            "sources",
            "Bulk sources by messages",
            SOURCE_COLUMNS,
            _source_rows(con, "bulk", "messages DESC, size DESC", {}),
        ),
        Table(
            "sources-by-size",
            "Bulk sources by size",
            SOURCE_COLUMNS,
            _source_rows(con, "bulk", "size DESC", {}),
        ),
    )


def candidates(con, opts: Options, now: int) -> Table:
    active = now - opts.active_days * DAY_MS
    rows = _source_rows(
        con,
        CANDIDATE,
        "last_ms >= :active DESC, messages DESC",
        {"min": opts.min_messages, "active": active},
    )
    sending = sum(1 for r in rows if r[7] >= day(active))
    notes = [
        f"{len(rows):,} sources, {sum(r[3] for r in rows):,} messages, "
        f"{human_size(sum(r[5] for r in rows))}; {sending:,} still sending (listed first).",
        "Unsubscribe shows what List-Unsubscribe offers ('—' for relayed mail, whose header "
        "is the relay's). Nothing is unsubscribed or removed.",
    ]
    return Table(
        "candidates", "Unsubscribe / clear-out candidates", SOURCE_COLUMNS, rows, notes=notes
    )


def by_year(con) -> Table:
    rows = con.execute("""
        SELECT strftime('%Y', internal_date / 1000, 'unixepoch') AS year, account,
               count(*), sum(bulk_signal), sum(bulk_signal) * 1.0 / count(*),
               coalesce(sum(CASE WHEN bulk_signal THEN size END), 0)
        FROM src_msg GROUP BY year, account ORDER BY year, account""").fetchall()
    return Table(
        "by-year",
        "Mail from others by year",
        [
            ("Year", "text"),
            ("Account", "text"),
            ("Messages", "int"),
            ("Bulk signals", "int"),
            ("Share", "pct"),
            ("Their size", "size"),
        ],
        rows,
        limit=False,
    )


def build(con: sqlite3.Connection, accounts: list[str], opts: Options) -> list[Table]:
    now = opts.now_ms if opts.now_ms is not None else int(time.time() * 1000)
    prepare_sources(con, accounts, opts, now)
    return [summary(con, opts, now), *top_sources(con), candidates(con, opts, now), by_year(con)]


# ── where to unsubscribe (report bulk --unsubscribe) ─────────────────────────

# Second-level labels under a country code that are part of the suffix, as in
# example.co.uk: a rough registrable domain without a public suffix list.
COUNTRY_SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "edu", "ne", "or", "gen"}
HEAD_BYTES = 256 * 1024  # read this much of a message for its headers


def base_domain(host: str) -> str:
    """A host's registrable domain, roughly: mail.shop.example.co.uk → example.co.uk. Pure."""
    labels = host.lower().strip(".").split(".")
    keep = (
        3 if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in COUNTRY_SECOND_LEVEL else 2
    )
    return ".".join(labels[-keep:])


@dataclass(frozen=True)
class Newest:
    """A candidate source's most recent message, and the headers read from its .eml."""

    account: str
    source: str
    kind: str
    sender: str
    last_ms: int
    targets: tuple[str, str, bool] | None  # unsubscribe_targets; None: no file to read


def newest_messages(con, opts: Options) -> list[tuple]:
    """
    (account, source, kind, sender, last_ms, filename, gmail_id) for each
    unsubscribe candidate's most recent message (of two at the same time, the
    larger ID), newest first. After build().
    """
    return con.execute(
        f"""SELECT s.account, s.source, s.kind, m.sender, s.last_ms,
                   coalesce(i.filename, ''), max(m.gmail_id)
            FROM (SELECT * FROM sources WHERE {CANDIDATE}) s
            JOIN src_msg m ON m.account = s.account AND m.source = s.source
                          AND m.internal_date = s.last_ms
            LEFT JOIN all_index i ON i.account = m.account AND i.gmail_id = m.gmail_id
            GROUP BY s.account, s.source
            ORDER BY s.last_ms DESC, s.account, s.source""",
        {"min": opts.min_messages},
    ).fetchall()


def _host(url: str) -> str:
    try:
        return urlsplit(url).hostname or ""
    except ValueError:  # e.g. a malformed IPv6 host
        return ""


def read_newest(rows: list[tuple], folders: dict) -> list[Newest]:
    """
    Newest values for newest_messages()'s rows, with each message's
    unsubscribe targets read from the head of its .eml (folders: account →
    backup folder).
    """
    from email.parser import BytesHeaderParser
    from email.policy import compat32

    from mailcellar.message import strip_mbox_from_lines, unsubscribe_targets

    out = []
    for account, source, kind, sender, last_ms, filename, _gmail_id in rows:
        targets = None
        path = folders[account] / filename if filename else None
        if path is not None and path.is_file():
            with path.open("rb") as fh:
                head = strip_mbox_from_lines(fh.read(HEAD_BYTES))
            targets = unsubscribe_targets(BytesHeaderParser(policy=compat32).parsebytes(head))
        out.append(Newest(account, source, kind, sender, last_ms, targets))
    return out


def unsubscribe_table(newest: list[Newest], active_ms: int) -> Table:
    """Where to unsubscribe from each candidate, from its most recent message. Pure."""
    rows = []
    for n in newest:
        when = (n.source, n.account, day(n.last_ms), "yes" if n.last_ms >= active_ms else "no")
        if n.kind == "relayed" or n.targets is None:
            note = "relayed: the header is your own list's" if n.kind == "relayed" else "no .eml"
            rows.append((*when, "", "", "", note, ""))
            continue
        https, mailto, one_click = n.targets
        address = mailto.split(":", 1)[1] if mailto else ""  # with any ?subject= it asks for
        host = _host(https)
        mail_host = address.split("?", 1)[0].rsplit("@", 1)[-1] if "@" in address else ""
        sender = base_domain(n.sender.rsplit("@", 1)[-1]) if "@" in n.sender else ""
        other = sorted({base_domain(h) for h in (host, mail_host) if h} - {sender})
        note = f"not the sender's domain: {', '.join(other)}" if sender and other else ""
        if not https and not mailto:
            note = "no https: or mailto: unsubscribe"
        rows.append((*when, "yes" if one_click else "no", host, address, note, https))
    return Table(
        "unsubscribe",
        "Where to unsubscribe",
        [
            ("Source", "text"),
            ("Account", "text"),
            ("Last", "text"),
            ("Still sending", "text"),
            ("One-click", "text"),
            ("Link host", "text"),
            ("Mail to", "text"),
            ("Note", "text"),
            ("Link", "full"),
        ],
        rows,
        notes=[
            "From each candidate's most recent message: its List-Unsubscribe link (https: "
            "only) and address (mailto:), and whether it offers one-click unsubscribe "
            "(RFC 8058: the link alone does it, no page to fill in). These come from the mail "
            "itself: check the host before opening a link, especially one not on the "
            "sender's domain (mailing services are common, but so are lookalikes). "
            "mailcellar opens nothing and sends nothing.",
        ],
    )
