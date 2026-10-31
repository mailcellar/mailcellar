"""
Command line for mailcellar search: find mail in the backups, including
mail offloaded out of Gmail, which Gmail's own search no longer finds.

The index (search.sqlite in the data root) is brought up to date for the
accounts searched before each search; the first time reads every message.
Read-only towards the backups and Gmail: no Gmail access is needed.
"""

import argparse
import logging
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from mailcellar.backup import sidecar
from mailcellar.backup.gyb import backup_account
from mailcellar.paths import data_root
from mailcellar.report.cli import parse_before, resolve_folders
from mailcellar.report.db import AnalyticsError
from mailcellar.search import db, index
from mailcellar.search.query import (
    Offload,
    QueryError,
    find,
    fts_query,
    keep,
    render,
    render_json,
    render_tsv,
)

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 50
FORMATS = {"tsv": render_tsv, "json": render_json}  # text is render()'s


def read_offloads(folder: Path, account: str) -> dict[tuple[str, str], Offload]:
    """The backup's messages offloaded now (not restored since), from meta.sqlite, read-only."""
    path = sidecar.meta_path(folder)
    if not path.is_file():
        return {}
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {
            (account, gid): Offload(batch, rule)
            for gid, batch, rule in con.execute(
                "SELECT gmail_id, batch, rule FROM offloaded WHERE restored_ms IS NULL"
            )
        }
    except sqlite3.Error as exc:
        logger.warning(f"{path}: cannot read what was offloaded ({exc}); run mailcellar meta")
        return {}
    finally:
        con.close()


def main(argv=None, prog: str | None = None) -> int:
    p = argparse.ArgumentParser(
        prog=prog or "mailcellar search",
        description="Find mail in the backups, offloaded mail included: by words in the "
        "sender, recipients, subject or body (not attachments). Words must all be found; "
        '"a phrase", from:, to: and subject: limit where, OR between two words takes either, '
        "-word leaves a word out, and word* matches the start of words. Newest first.",
    )
    p.add_argument(
        "query", help='What to look for, in quotes if more than one word: "invoice 2024"'
    )
    p.add_argument(
        "accounts",
        nargs="*",
        metavar="ACCOUNT",
        help="Backups to search: address, config folder name or unique part (default: all)",
    )
    p.add_argument(
        "--offloaded", action="store_true", help="Only mail offloaded out of its mailbox"
    )
    p.add_argument(
        "--rule", action="append", default=[], help="Only mail offloaded by this rule (repeatable)"
    )
    p.add_argument(
        "--batch", action="append", default=[], help="Only mail in this offload batch (repeatable)"
    )
    p.add_argument("--before", help="Only mail older than this: YYYY-MM-DD, or a time ago (6m, 2y)")
    p.add_argument("--after", help="Only mail from this on: YYYY-MM-DD, or a time ago (6m, 2y)")
    p.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Show at most N results, newest first (default {DEFAULT_LIMIT})",
    )
    p.add_argument(
        "--no-update",
        action="store_true",
        help="Search the index as it is, without first adding new mail to it",
    )
    p.add_argument(
        "--format",
        choices=["text", *FORMATS],
        default="text",
        help="text to read (default); tsv, one tab-separated line per result, or json, "
        "one object per line, for scripts and pickers such as fzf",
    )
    args = p.parse_args(argv)
    try:
        match = fts_query(args.query)
        before_ms, after_ms = parse_before(args.before), parse_before(args.after, "--after")
        folders = resolve_folders(args.accounts)
    except (QueryError, AnalyticsError) as exc:
        p.error(str(exc))
    if before_ms is not None and after_ms is not None and after_ms >= before_ms:
        p.error("--after must be earlier than --before")
    if not folders:
        sys.exit(f"error: no backups found in {data_root()}")

    by_account = {backup_account(f): f for f in folders}
    with closing(db.open_db(db.default_db())) as con:
        if not args.no_update:
            for folder in folders:
                stats = index.update(con, folder)
                if stats.errors:
                    logger.warning(f"{stats.account}: {stats.errors:,} messages could not be read")
        hits = find(con, match, list(by_account), after_ms, before_ms)

    offloads: dict[tuple[str, str], Offload] = {}
    for account, folder in by_account.items():
        offloads |= read_offloads(folder, account)
    hits = keep(hits, offloads, args.offloaded, set(args.rule), set(args.batch))
    # tsv and json keep stdout for results; what is said about them goes to stderr
    say = print if args.format == "text" else logger.info
    if not hits:
        say("No matches.")
        return 1
    shown = hits[: max(args.limit, 0)]
    for hit in shown:
        found = (hit, by_account.get(hit.account), offloads.get((hit.account, hit.gmail_id)))
        if args.format == "text":
            print("\n".join(render(*found)))
        else:
            print(FORMATS[args.format](*found))
    more = (
        f"; showing the newest {len(shown):,} (--limit for more)" if len(shown) < len(hits) else ""
    )
    count = f"{len(hits):,} {'match' if len(hits) == 1 else 'matches'}{more}"
    say(f"\n{count}" if args.format == "text" else count)
    return 0
