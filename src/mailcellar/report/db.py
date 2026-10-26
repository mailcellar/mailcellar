"""
Read-only access to the sidecars: every chosen account's meta.sqlite (and
msg-db.sqlite, for completeness checks) attached to one in-memory
connection, with temp views that combine the accounts.

  all_messages        account + messages, leaving out the old ID of a
                      message restored under a new one
  all_attachments     account + attachments
  all_recipients      account + recipients
  all_message_labels  account, gmail_id, label_id, name, type
  all_labels          account + the label catalogue (label_id, name, type)
  all_runs            account + runs (backup, count and prune runs)
  all_state           account + meta (name, value: schema_version, history_id)
  all_offloaded       account + offloaded (removed from Gmail, kept in the backup)
  all_forgotten       account + forgotten (offloaded, then deleted from the backup)
  all_index           account, gmail_id, filename: gyb's index (msg-db.sqlite)
"""

import sqlite3
from pathlib import Path

from mailcellar.backup import sidecar
from mailcellar.backup.gyb import backup_account
from mailcellar.message import PARSER_VERSION


class AnalyticsError(Exception):
    pass


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def open_sidecars(folders: list[Path]) -> tuple[sqlite3.Connection, list[str]]:
    """(connection, account per folder). Raises AnalyticsError if a sidecar is unusable."""
    con = sqlite3.connect("file::memory:", uri=True)
    try:
        accounts = []
        selects: dict[str, list[str]] = {
            "messages": [],
            "attachments": [],
            "recipients": [],
            "message_labels": [],
            "labels": [],
            "runs": [],
            "state": [],
            "offloaded": [],
            "forgotten": [],
            "index": [],
        }
        for i, folder in enumerate(folders):
            meta = sidecar.meta_path(folder)
            if not meta.is_file():
                raise AnalyticsError(f"{folder} has no meta.sqlite; run mailcellar meta")
            con.execute(f"ATTACH ? AS s{i}", (f"file:{meta}?mode=ro",))
            con.execute(f"ATTACH ? AS g{i}", (f"file:{folder / 'msg-db.sqlite'}?mode=ro",))
            row = con.execute(
                f"SELECT value FROM s{i}.meta WHERE name = 'schema_version'"
            ).fetchone()
            if row is None or row[0] != sidecar.SCHEMA_VERSION:
                raise AnalyticsError(
                    f"{meta} has schema {row[0] if row else '?'}, expected "
                    f"{sidecar.SCHEMA_VERSION}; run mailcellar meta to migrate it"
                )
            account = backup_account(folder)
            accounts.append(account)
            a = _quote(account)
            # A restored message's old ID shares its file with the new one; count it once.
            selects["messages"].append(
                f"""SELECT {a} AS account, * FROM s{i}.messages WHERE gmail_id NOT IN
                    (SELECT source_id FROM s{i}.restored
                     WHERE target = '' AND new_id != source_id)"""
            )
            selects["attachments"].append(f"SELECT {a} AS account, * FROM s{i}.attachments")
            selects["recipients"].append(f"SELECT {a} AS account, * FROM s{i}.recipients")
            selects["labels"].append(f"SELECT {a} AS account, * FROM s{i}.labels")
            selects["runs"].append(f"SELECT {a} AS account, * FROM s{i}.runs")
            selects["state"].append(f"SELECT {a} AS account, * FROM s{i}.meta")
            selects["offloaded"].append(f"SELECT {a} AS account, * FROM s{i}.offloaded")
            selects["forgotten"].append(f"SELECT {a} AS account, * FROM s{i}.forgotten")
            selects["index"].append(
                f"""SELECT {a} AS account, u.uid AS gmail_id, m.message_filename AS filename
                    FROM g{i}.uids u JOIN g{i}.messages m USING (message_num)"""
            )
            selects["message_labels"].append(
                f"""SELECT {a} AS account, ml.gmail_id, ml.label_id,
                           coalesce(l.name, ml.label_id) AS name,
                           coalesce(l.type, 'system') AS type
                    FROM s{i}.message_labels ml LEFT JOIN s{i}.labels l USING (label_id)"""
            )
        for view, parts in selects.items():
            con.execute(f"CREATE TEMP VIEW all_{view} AS " + " UNION ALL ".join(parts))
        return con, accounts
    except BaseException:
        con.close()  # a failed open leaves no connection behind
        raise


def completeness_counts(con: sqlite3.Connection, accounts: list[str]) -> dict[str, tuple]:
    """account → (sidecar rows parsed by an older parser, backed-up messages not in it)."""
    counts = {}
    for i, account in enumerate(accounts):
        stale = con.execute(
            f"SELECT count(*) FROM s{i}.messages WHERE parser_version != ?", (PARSER_VERSION,)
        ).fetchone()[0]
        missing = con.execute(
            f"""SELECT count(*) FROM g{i}.uids
                WHERE uid NOT IN (SELECT gmail_id FROM s{i}.messages)"""
        ).fetchone()[0]
        counts[account] = (stale, missing)
    return counts


def completeness(con: sqlite3.Connection, accounts: list[str]) -> list[str]:
    """Warnings about sidecar rows a report can't rely on: stale or missing."""
    return [
        f"{account}: {stale:,} messages parsed by an older parser, {missing:,} "
        "backed up but not in meta.sqlite; run mailcellar meta for exact figures"
        for account, (stale, missing) in completeness_counts(con, accounts).items()
        if stale or missing
    ]
