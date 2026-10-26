"""
health: is each backup complete and current, and how have runs gone?

From the sidecar's runs table (every backup, count and prune run since
meta.sqlite schema 5), plus checks that need no run history: index
entries without an .eml file, sidecar rows missing or parsed by an older
parser, whether a history start point is stored, the newest message,
mail offloaded from Gmail and kept in the backup, and the batches --prune
moved to <data root>/_pruned/.

Gmail keeps mailbox history for about 28 days; a backup whose start
point is older than that lists every message instead (~30 s rather than
~1 s).
"""

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from mailcellar.backup.gyb import backup_account
from mailcellar.paths import pruned_folder
from mailcellar.report.db import completeness_counts
from mailcellar.report.output import Table, day, when

DAY_MS = 86_400_000
FAILED = ("failures", "error", "aborted")


@dataclass(frozen=True)
class Options:
    window_days: int = 28  # Gmail's history retention, as measured
    now_ms: int | None = None  # for tests; default: now


# Backup runs that listed the whole mailbox (a search lists only its matches).
FULL_LISTING = "command = 'backup' AND mode = 'full' AND reason != 'search'"
# Backup runs after which the next run can start from history.
HISTORY_POINT = "command = 'backup' AND status = 'ok' AND reason != 'search'"


def state(con, folders: list[Path], opts: Options, now: int) -> Table:
    rows = []
    stale_missing = completeness_counts(con, [backup_account(f) for f in folders])
    for folder in folders:
        account = backup_account(folder)
        p = {"a": account}
        files = [f for (f,) in con.execute("SELECT filename FROM all_index WHERE account = :a", p)]
        no_file = sum(not (folder / f).is_file() for f in files)
        stale, missing = stale_missing[account]
        (newest,) = con.execute(
            "SELECT max(internal_date) FROM all_messages WHERE account = :a", p
        ).fetchone()
        history = con.execute(
            "SELECT value FROM all_state WHERE account = :a AND name = 'history_id'", p
        ).fetchone()
        last, last_full, point = con.execute(
            f"""SELECT max(CASE WHEN command = 'backup' THEN finished_ms END),
                       max(CASE WHEN {FULL_LISTING} THEN finished_ms END),
                       max(CASE WHEN {HISTORY_POINT} THEN finished_ms END)
                FROM all_runs WHERE account = :a""",
            p,
        ).fetchone()
        offloaded, offloaded_in_gmail = con.execute(
            """SELECT count(*), coalesce(sum(gone_ms IS NULL), 0)
               FROM all_offloaded WHERE account = :a AND restored_ms IS NULL
                 AND gmail_id NOT IN (SELECT gmail_id FROM all_forgotten WHERE account = :a)""",
            p,
        ).fetchone()
        age = (now - point) / DAY_MS if point is not None else None
        if not history:
            next_run = "full listing: no start point"
        elif age is None:
            next_run = "history, unless expired (no runs yet)"
        elif age > opts.window_days:
            next_run = "full listing: start point too old"
        else:
            next_run = "history"
        rows.append(
            (
                account,
                len(files),
                no_file,
                missing,
                stale,
                offloaded,
                offloaded_in_gmail,
                day(newest),
                "yes" if history else "no",
                when(last),
                when(last_full),
                age,
                next_run,
            )
        )
    return Table(
        "state",
        "Current state",
        [
            ("Account", "text"),
            ("Indexed", "int"),
            ("No .eml", "int"),
            ("Not in sidecar", "int"),
            ("Older parser", "int"),
            ("Offloaded", "int"),
            ("Offloaded, in Trash", "int"),
            ("Newest message", "text"),
            ("Start point", "text"),
            ("Last backup", "text"),
            ("Last full listing", "text"),
            ("Days since start point", "num"),
            ("Next backup", "text"),
        ],
        rows,
        notes=[
            "No .eml: indexed in msg-db.sqlite but the file is missing (backup re-fetches it). "
            "Not in sidecar / Older parser: run mailcellar meta. Offloaded: removed from "
            "Gmail on purpose and kept in the backup; in Trash until Gmail empties it (30 "
            "days), as last seen by count, a prune or a backup. Start point: a stored "
            "history ID; its age is the time since the last backup that stored one. Times "
            "are UTC."
        ],
        limit=False,
    )


def runs(con) -> Table:
    rows = con.execute("""
        SELECT account, started_ms, command, mode, reason, status, downloaded, labels_changed,
               skipped, deleted_in_gmail, pruned, offloaded, missing, only_in_backup, failed,
               messages,
               (finished_ms - started_ms) / 1000.0
        FROM all_runs ORDER BY started_ms DESC, account""").fetchall()
    return Table(
        "runs",
        "Runs, newest first",
        [
            ("Account", "text"),
            ("Started", "text"),
            ("Command", "text"),
            ("Mode", "text"),
            ("Why full", "text"),
            ("Status", "text"),
            ("Downloaded", "int"),
            ("Label updates", "int"),
            ("Skipped", "int"),
            ("Deleted in Gmail", "int"),
            ("Pruned", "int"),
            ("Offloaded", "int"),
            ("Missing", "int"),
            ("Only in backup", "int"),
            ("Failed", "int"),
            ("Messages after", "int"),
            ("Seconds", "num"),
        ],
        [(r[0], when(r[1]), *r[2:]) for r in rows],
        notes=[
            "Recorded since meta.sqlite schema 5; earlier runs are not known (the pruned "
            "batches below and the newest message show earlier activity). Why full: "
            "no-history (no start point stored), expired (Gmail's history no longer reaches "
            "it), or asked for (full, search, refresh-labels); needs-consent marks an unattended "
            "backup that could not sign in (status error; renew with mailcellar auth). "
            "Deleted in Gmail: backed-up "
            "messages permanently deleted there (backup, count), offloaded mail apart. Offloaded: "
            "offloaded messages in the backup (count, prune). Missing / Only in backup: count's "
            "comparison (only in backup includes mail in Trash or Spam, not offloaded mail)."
        ],
    )


def gaps(con, accounts: list[str], opts: Options, now: int) -> Table:
    window = opts.window_days * DAY_MS
    rows = []
    for account in accounts:
        points = [
            ms
            for (ms,) in con.execute(
                f"""SELECT finished_ms FROM all_runs WHERE account = ? AND {HISTORY_POINT}
                    ORDER BY finished_ms""",
                (account,),
            )
        ]
        for i, a in enumerate(points):
            b = points[i + 1] if i + 1 < len(points) else None
            end = b if b is not None else now
            if end - a > window:
                rows.append((account, when(a), when(b) if b else "now", (end - a) / DAY_MS))
    return Table(
        "gaps",
        f"Gaps longer than the history window ({opts.window_days} days)",
        [("Account", "text"), ("From", "text"), ("To", "text"), ("Days", "num")],
        rows,
        notes=[
            "Between backups that left a start point. After such a gap the next backup lists "
            "every message; 'now' means the next one will."
        ],
        limit=False,
    )


def by_month(con) -> Table:
    failed = ",".join(f"'{s}'" for s in FAILED)
    rows = con.execute(f"""
        SELECT strftime('%Y-%m', started_ms / 1000, 'unixepoch') AS month, account,
               sum(command = 'backup'), sum({FULL_LISTING}),
               sum(command = 'count'), sum(command = 'sync'),  -- prunes: see sidecar.runs
               coalesce(sum(downloaded), 0), coalesce(sum(labels_changed), 0),
               coalesce(sum(pruned), 0), sum(status IN ({failed}))
        FROM all_runs GROUP BY month, account ORDER BY month DESC, account""").fetchall()
    return Table(
        "by-month",
        "Runs by month",
        [
            ("Month", "text"),
            ("Account", "text"),
            ("Backups", "int"),
            ("Full listings", "int"),
            ("Counts", "int"),
            ("Prunes", "int"),
            ("Downloaded", "int"),
            ("Label updates", "int"),
            ("Pruned", "int"),
            ("Failed runs", "int"),
        ],
        rows,
        notes=["Failed runs: download failures, errors, or a prune stopped by its guard."],
    )


def pruned(folders: list[Path]) -> Table:
    rows = []
    for folder in folders:
        account = backup_account(folder)
        for batch in sorted(pruned_folder(folder).glob("*/pruned.csv")):
            with batch.open(encoding="utf-8") as fh:
                n = sum(1 for _ in fh) - 1
            rows.append((account, batch.parent.name, n))
    return Table(
        "pruned",
        "Pruned batches (<data root>/_pruned/)",
        [("Account", "text"), ("Batch", "text"), ("Messages", "int")],
        rows,
        notes=["Messages moved out of the backup by --prune, one folder per run (local time)."],
        limit=False,
    )


def tokens(rows: list[tuple[str, str, datetime | None]], now: int) -> Table:
    """Each account's tokens and when consent was last given: (account, scope, authorised_at)."""
    shown = []
    for account, scope, authorised in rows:
        age = (now - authorised.timestamp() * 1000) / DAY_MS if authorised else None
        shown.append(
            (account, scope, day(int(authorised.timestamp() * 1000)) if authorised else "", age)
        )
    return Table(
        "tokens",
        "Tokens",
        [
            ("Account", "text"),
            ("Access", "text"),
            ("Authorised (UTC)", "text"),
            ("Days since", "num"),
        ],
        shown,
        notes=[
            "With the Google Cloud consent screen in Testing status, a token expires 7 days "
            "after consent; mailcellar can't see that status, so this is the time since "
            "consent, blank for a token from before it was recorded. Renew: "
            "mailcellar auth ACCOUNT --scope S --renew."
        ],
        limit=False,
    )


def build(
    con: sqlite3.Connection,
    folders: list[Path],
    opts: Options,
    token_rows: list[tuple[str, str, datetime | None]] = (),
) -> list[Table]:
    now = opts.now_ms if opts.now_ms is not None else int(time.time() * 1000)
    accounts = [backup_account(f) for f in folders]
    return [
        state(con, folders, opts, now),
        *([tokens(list(token_rows), now)] if token_rows else []),
        runs(con),
        gaps(con, accounts, opts, now),
        by_month(con),
        pruned(folders),
    ]
