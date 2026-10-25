"""
storage: what takes up space, and what could go.

Message sizes are Gmail's sizeEstimate, which equals the stored .eml size
(checked against real backups); attachment sizes are decoded bytes, so
"attachment share" is approximate (base64 adds about a third).
"""

import sqlite3
from dataclasses import dataclass

from mailcellar.dedup.rules import STAR_LABELS
from mailcellar.report.output import Table, human_size


@dataclass(frozen=True)
class Options:
    min_size: int = 5_000_000  # candidates: at least this many bytes
    before_ms: int | None = None  # candidates: received before this (ms, UTC)
    include_labelled: bool = False  # candidates: also sent, starred, user-labelled


def _summary(con: sqlite3.Connection) -> None:
    """TEMP msg: one row per message with the per-message figures every section uses."""
    stars = ",".join(f"'{s}'" for s in sorted(STAR_LABELS))
    con.executescript(f"""
        DROP TABLE IF EXISTS temp.msg;
        CREATE TEMP TABLE msg AS
        SELECT m.account, m.gmail_id, m.internal_date,
               coalesce(m.size_estimate, 0) AS size,
               CASE WHEN m.sender_addr = '' THEN 'unknown' ELSE m.sender_addr END AS sender,
               m.subject,
               coalesce(a.n, 0) AS att_count, coalesce(a.size, 0) AS att_size,
               coalesce(l.names, '') AS labels, coalesce(l.user, 0) AS user_labelled,
               coalesce(l.starred, 0) AS starred, coalesce(l.sent, 0) AS sent
        FROM all_messages m
        LEFT JOIN (SELECT account, gmail_id, count(*) AS n, sum(size) AS size
                   FROM all_attachments GROUP BY account, gmail_id) a
               USING (account, gmail_id)
        LEFT JOIN (SELECT account, gmail_id, group_concat(name, ';') AS names,
                          max(type = 'user') AS user,
                          max(label_id IN ({stars})) AS starred,
                          max(label_id IN ('SENT', 'DRAFT')) AS sent
                   FROM all_message_labels GROUP BY account, gmail_id) l
               USING (account, gmail_id);
        CREATE INDEX temp.msg_key ON msg (account, gmail_id);
    """)


DATE = "date(internal_date / 1000, 'unixepoch')"
DOMAIN = "CASE WHEN instr(sender, '@') THEN substr(sender, instr(sender, '@') + 1) ELSE sender END"


def totals(con) -> Table:
    rows = con.execute("""
        SELECT account, count(*), sum(size), sum(att_count > 0), sum(att_size)
        FROM msg GROUP BY account ORDER BY account""").fetchall()
    if len(rows) > 1:
        rows.append(("all accounts", *(sum(r[i] for r in rows) for i in range(1, 5))))
    rows = [(*r, (r[4] / r[2]) if r[2] else 0) for r in rows]
    return Table(
        "totals",
        "Totals",
        [
            ("Account", "text"),
            ("Messages", "int"),
            ("Size", "size"),
            ("With attachments", "int"),
            ("Attachments", "size"),
            ("Attachment share", "pct"),
        ],
        rows,
        notes=[
            "Size is Gmail's sizeEstimate (equal to the stored .eml size on these "
            "backups); attachments are decoded bytes, so their share is approximate. "
            "Sizes use binary units (1 GB = 1024³ bytes)."
        ],
        limit=False,
    )


def by_year(con) -> Table:
    rows = con.execute("""
        SELECT strftime('%Y', internal_date / 1000, 'unixepoch') AS year, account,
               count(*), sum(size),
               sum(size) * 1.0 / (SELECT sum(size) FROM msg t WHERE t.account = msg.account)
        FROM msg GROUP BY year, account ORDER BY year, account""").fetchall()
    return Table(
        "by-year",
        "By year (received)",
        [
            ("Year", "text"),
            ("Account", "text"),
            ("Messages", "int"),
            ("Size", "size"),
            ("Share of account", "pct"),
        ],
        rows,
        limit=False,
    )


def by_label(con) -> Table:
    rows = con.execute("""
        SELECT l.name, l.type, l.account, count(*), sum(m.size)
        FROM all_message_labels l JOIN msg m USING (account, gmail_id)
        GROUP BY l.account, l.label_id ORDER BY sum(m.size) DESC""").fetchall()
    return Table(
        "by-label",
        "By label and category",
        [
            ("Label", "text"),
            ("Type", "text"),
            ("Account", "text"),
            ("Messages", "int"),
            ("Size", "size"),
        ],
        rows,
        notes=["A message with several labels counts under each."],
    )


def by_sender(con) -> Table:
    rows = con.execute("""
        SELECT sender, account, count(*), sum(size), max(size)
        FROM msg GROUP BY sender, account ORDER BY sum(size) DESC""").fetchall()
    return Table(
        "by-sender",
        "By sender",
        [
            ("Sender", "text"),
            ("Account", "text"),
            ("Messages", "int"),
            ("Size", "size"),
            ("Largest", "size"),
        ],
        rows,
    )


def by_domain(con) -> Table:
    rows = con.execute(f"""
        SELECT {DOMAIN} AS domain, account, count(*), sum(size), count(DISTINCT sender)
        FROM msg GROUP BY domain, account ORDER BY sum(size) DESC""").fetchall()
    return Table(
        "by-domain",
        "By sender domain",
        [
            ("Domain", "text"),
            ("Account", "text"),
            ("Messages", "int"),
            ("Size", "size"),
            ("Senders", "int"),
        ],
        rows,
    )


def largest_messages(con) -> Table:
    rows = con.execute(f"""
        SELECT account, {DATE}, sender, subject, size, att_count, labels, gmail_id
        FROM msg ORDER BY size DESC""").fetchall()
    return Table(
        "largest-messages",
        "Largest messages",
        [
            ("Account", "text"),
            ("Date", "text"),
            ("Sender", "text"),
            ("Subject", "text"),
            ("Size", "size"),
            ("Attachments", "int"),
            ("Labels", "text"),
            ("Gmail ID", "text"),
        ],
        rows,
    )


def attachment_types(con) -> tuple[Table, Table]:
    by_mime = con.execute("""
        SELECT mime_type, count(*), sum(size), count(DISTINCT sha256)
        FROM all_attachments GROUP BY mime_type ORDER BY sum(size) DESC""").fetchall()
    by_ext = con.execute("""
        SELECT extension(filename) AS ext, count(*), sum(size)
        FROM all_attachments GROUP BY ext ORDER BY sum(size) DESC""").fetchall()
    return (
        Table(
            "attachment-types",
            "Attachments by MIME type",
            [
                ("MIME type", "text"),
                ("Attachments", "int"),
                ("Size", "size"),
                ("Distinct files", "int"),
            ],
            by_mime,
        ),
        Table(
            "attachment-extensions",
            "Attachments by file extension",
            [("Extension", "text"), ("Attachments", "int"), ("Size", "size")],
            by_ext,
        ),
    )


def largest_attachments(con) -> Table:
    rows = con.execute(f"""
        SELECT a.account, a.filename, a.mime_type, a.size, {DATE}, m.sender, m.subject,
               a.gmail_id
        FROM all_attachments a JOIN msg m USING (account, gmail_id)
        ORDER BY a.size DESC""").fetchall()
    return Table(
        "largest-attachments",
        "Largest attachments",
        [
            ("Account", "text"),
            ("File", "text"),
            ("Type", "text"),
            ("Size", "size"),
            ("Date", "text"),
            ("Sender", "text"),
            ("Subject", "text"),
            ("Gmail ID", "text"),
        ],
        rows,
    )


def duplicate_attachments(con) -> Table:
    rows = con.execute("""
        SELECT sha256, max(filename), max(mime_type), max(size) AS size,
               count(DISTINCT account || ' ' || gmail_id) AS copies,
               count(DISTINCT account) AS accounts
        FROM all_attachments WHERE size > 0
        GROUP BY sha256 HAVING copies > 1""").fetchall()
    rows = sorted(
        (
            (name, mime, size, copies, accounts, (copies - 1) * size, sha[:16])
            for sha, name, mime, size, copies, accounts in rows
        ),
        key=lambda r: -r[5],
    )
    wasted = sum(r[5] for r in rows)
    big = [r for r in rows if r[2] >= 1_000_000]
    small = [r for r in rows if r[2] < 100_000]
    notes = [
        f"{len(rows):,} files are attached to more than one message: "
        f"{sum(r[3] - 1 for r in rows):,} extra copies, {human_size(wasted)}.",
        f"Over 1 MB: {len(big):,} files, {human_size(sum(r[5] for r in big))}; "
        f"under 100 KB (mostly logos and icons): {len(small):,} files, "
        f"{human_size(sum(r[5] for r in small))}.",
    ]
    return Table(
        "duplicate-attachments",
        "Attachments stored more than once",
        [
            ("File", "text"),
            ("Type", "text"),
            ("Size", "size"),
            ("Copies", "int"),
            ("Accounts", "int"),
            ("Extra copies", "size"),
            ("SHA-256", "text"),
        ],
        rows,
        notes=notes,
    )


def candidates(con, opts: Options) -> Table:
    where = ["size >= ?"]
    params: list = [opts.min_size]
    if opts.before_ms is not None:
        where.append("internal_date < ?")
        params.append(opts.before_ms)
    if not opts.include_labelled:
        where.append("NOT sent AND NOT starred AND NOT user_labelled")
    rows = con.execute(
        f"""SELECT account, {DATE}, sender, subject, size, att_count, labels, gmail_id
            FROM msg WHERE {" AND ".join(where)} ORDER BY size DESC""",
        params,
    ).fetchall()
    per_account: dict[str, list] = {}
    for r in rows:
        per_account.setdefault(r[0], []).append(r[4])
    rule = f"at least {opts.min_size / 1e6:g} MB"
    if opts.before_ms is not None:
        rule += ", received before the cutoff"
    if not opts.include_labelled:
        rule += ", not sent, starred or user-labelled"
    notes = (
        [f"Messages {rule}."]
        + [
            f"{account}: {len(sizes):,} messages, {human_size(sum(sizes))}"
            for account, sizes in sorted(per_account.items())
        ]
        + ["Nothing is removed; these are only listed."]
    )
    return Table(
        "candidates",
        "Removal candidates",
        [
            ("Account", "text"),
            ("Date", "text"),
            ("Sender", "text"),
            ("Subject", "text"),
            ("Size", "size"),
            ("Attachments", "int"),
            ("Labels", "text"),
            ("Gmail ID", "text"),
        ],
        rows,
        notes=notes,
    )


def extension(filename: str | None) -> str:
    """Lower-case file extension, '(none)' without one."""
    name = (filename or "").rsplit("/", 1)[-1]
    if "." not in name.strip(".") or name.endswith("."):
        return "(none)"
    return name.rsplit(".", 1)[1].lower()


def build(con: sqlite3.Connection, opts: Options) -> list[Table]:
    con.create_function("extension", 1, extension, deterministic=True)
    _summary(con)
    return [
        totals(con),
        by_year(con),
        by_label(con),
        by_sender(con),
        by_domain(con),
        largest_messages(con),
        *attachment_types(con),
        largest_attachments(con),
        duplicate_attachments(con),
        candidates(con, opts),
    ]
