"""
match: group copies of the same message, within and across accounts.

Two copies are the same message when they have the same content_hash (the
visible text plus attachments) and either the same Message-ID or the same
header_key (From, Date, Subject); groups are the connected components of
that relation. Every link needs the same content hash, so all copies in a
group have the same content. Messages with no content hash are never
grouped.
"""

import csv
import logging
import sqlite3
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger(__name__)


def find_groups(rows: list[tuple]) -> list[list[tuple]]:
    """
    rows: (account, gmail_id, message_id, content_hash, header_key).
    Returns groups of two or more rows, each sorted, ordered by first member.
    """
    rows = [r for r in rows if r[3]]
    parent = list(range(len(rows)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    first: dict[tuple, int] = {}
    for i, (_a, _g, mid, content, hkey) in enumerate(rows):
        for key in ((("mid", mid, content),) if mid else ()) + (
            (("hdr", hkey, content),) if hkey else ()
        ):
            j = first.setdefault(key, i)
            parent[find(i)] = find(j)

    components = defaultdict(list)
    for i, row in enumerate(rows):
        components[find(i)].append(row)
    return sorted((sorted(c) for c in components.values() if len(c) > 1), key=lambda c: c[0])


def describe(group: list[tuple]) -> tuple[str, str, str]:
    """(scope, method, key) for a group: how it was matched, for reports."""
    scope = "cross" if len({r[0] for r in group}) > 1 else "within"
    mids = [r[2] for r in group if r[2]]
    shared = next((m for m in sorted(set(mids)) if mids.count(m) > 1), None)
    if shared:
        return scope, "message-id", shared
    return scope, "header-key", group[0][4]


def match(con: sqlite3.Connection) -> None:
    """Rebuild match_groups / match_members from the messages table."""
    rows = con.execute(
        """SELECT account, gmail_id, message_id, content_hash, header_key
           FROM messages WHERE content_hash != ''"""
    ).fetchall()
    con.execute("DELETE FROM match_members")
    con.execute("DELETE FROM match_groups")
    for group_id, group in enumerate(find_groups(rows), 1):
        scope, method, key = describe(group)
        con.execute("INSERT INTO match_groups VALUES (?, ?, ?, ?)", (group_id, scope, method, key))
        con.executemany(
            "INSERT INTO match_members VALUES (?, ?, ?)", ((group_id, a, g) for a, g, *_ in group)
        )
    con.commit()


def reused_message_ids(con: sqlite3.Connection) -> list[tuple]:
    """(message_id, accounts, contents) for Message-IDs carried by different content."""
    return con.execute(
        """SELECT message_id, count(DISTINCT account), count(DISTINCT content_hash)
           FROM messages WHERE message_id != '' AND content_hash != ''
           GROUP BY message_id HAVING count(DISTINCT content_hash) > 1
           ORDER BY message_id"""
    ).fetchall()


def export_match_csv(con: sqlite3.Connection, csv_path: Path) -> None:
    """One row per copy in every matched group."""
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "group_id",
                "scope",
                "method",
                "key",
                "account",
                "gmail_id",
                "message_id",
                "internal_date",
                "sender",
                "subject",
                "content_hash",
                "path",
            ]
        )
        w.writerows(
            con.execute(
                """SELECT mg.group_id, mg.scope, mg.method, mg.key,
                      m.account, m.gmail_id, m.message_id, m.internal_date,
                      m.sender, m.subject, m.content_hash, b.folder || '/' || m.path
               FROM match_groups mg
               JOIN match_members mm USING (group_id)
               JOIN messages m USING (account, gmail_id)
               JOIN backups b USING (account)
               ORDER BY mg.group_id, m.account, m.gmail_id"""
            )
        )
    logger.info(f"Match CSV → {csv_path}")
