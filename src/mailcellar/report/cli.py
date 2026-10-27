"""Command line for mailcellar report: argparse and one cmd_* per report."""

import argparse
import logging
import re
import sys
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

from mailcellar.paths import config_root, data_root, find_backups
from mailcellar.report import bulk, correspondents, health, labels, storage, threads, volume
from mailcellar.report.db import AnalyticsError, completeness, open_sidecars
from mailcellar.report.output import render, write_csv

logger = logging.getLogger(__name__)


def resolve_folders(names: list[str], config: Path | None = None) -> list[Path]:
    """
    Backup folders for account names: a full address, a config folder name
    from config/accounts.toml ('gmail', 'work'), or a unique part of an
    address. No names: every backup in the data root.
    """
    backups = {f.name.lower(): f for f in find_backups()}
    if not names:
        return list(backups.values())
    aliases = {}
    try:
        from mailcellar import gmailapi

        aliases = {
            folder.name.lower(): account
            for account, folder in gmailapi.config_accounts(config or config_root()).items()
        }
    except Exception:
        pass  # no accounts.toml: addresses and parts of them still work
    folders = []
    for name in names:
        n = name.lower()
        hits = [a for a in backups if a == n or a == aliases.get(n)] or [
            a for a in backups if n in a
        ]
        if len(hits) != 1:
            raise AnalyticsError(
                f"{name!r} matches {len(hits)} of the backups in {data_root()} "
                f"({', '.join(sorted(backups))})"
            )
        folders.append(backups[hits[0]])
    return folders


def years_before(when: datetime, years: int) -> datetime:
    """`when`, `years` years earlier; 29 February becomes the 28th in a year without one."""
    try:
        return when.replace(year=when.year - years)
    except ValueError:
        return when.replace(year=when.year - years, day=28)


def parse_before(value: str | None) -> int | None:
    """--before: a date (YYYY-MM-DD) or a number of years ago; ms since epoch, UTC."""
    if value is None:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        when = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    elif re.fullmatch(r"\d+(\.\d+)?", value):
        when = years_before(datetime.now(UTC), int(float(value)))
    else:
        raise AnalyticsError(f"--before {value!r}: give a date (YYYY-MM-DD) or a number of years")
    return int(when.timestamp() * 1000)


@contextmanager
def open_report(args):
    """
    (connection, accounts) for the accounts named in args, with completeness
    warnings; the connection is closed when the block ends.
    """
    folders = resolve_folders(args.accounts)
    if not folders:
        raise AnalyticsError(f"no backups found in {data_root()}")
    con, accounts = open_sidecars(folders)
    with closing(con):
        for warning in completeness(con, accounts):
            logger.warning(warning)
        yield con, accounts


def emit(args, title: str, prefix: str, accounts: list[str], tables) -> None:
    """Print or write the text report, and the CSVs if asked for."""
    header = f"{title} — {', '.join(accounts)}\nGenerated {datetime.now():%Y-%m-%d %H:%M}\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(header)
            render(tables, args.top, fh)
        logger.info(f"Report → {args.output}")
    else:
        sys.stdout.write(header)
        render(tables, args.top, sys.stdout)
    if args.csv:
        paths = write_csv(tables, Path(args.csv), prefix)
        logger.info(f"CSV → {args.csv}/ ({len(paths)} files)")


def cmd_storage(args) -> None:
    opts = storage.Options(
        min_size=int(args.min_size * 1_000_000),
        before_ms=parse_before(args.before),
        include_labelled=args.include_labelled,
    )
    with open_report(args) as (con, accounts):
        emit(args, "Storage report", "storage", accounts, storage.build(con, opts))


def cmd_bulk(args) -> None:
    opts = bulk.Options(
        min_messages=args.min_messages,
        active_days=args.active_days,
        min_bulk_share=args.min_bulk_share / 100,
    )
    with open_report(args) as (con, accounts):
        emit(args, "Bulk mail report", "bulk", accounts, bulk.build(con, accounts, opts))


def cmd_volume(args) -> None:
    with open_report(args) as (con, accounts):
        emit(args, "Volume report", "volume", accounts, volume.build(con))


def cmd_threads(args) -> None:
    with open_report(args) as (con, accounts):
        emit(args, "Threads report", "threads", accounts, threads.build(con, accounts))


def cmd_labels(args) -> None:
    with open_report(args) as (con, accounts):
        emit(args, "Labels report", "labels", accounts, labels.build(con, accounts))


def cmd_correspondents(args) -> None:
    with open_report(args) as (con, accounts):
        tables = correspondents.build(con, accounts)
        emit(args, "Correspondents report", "correspondents", accounts, tables)


def token_rows(accounts: list[str]) -> list[tuple[str, str, object]]:
    """(account, scope, when consent was given or None) for each configured token of `accounts`."""
    from mailcellar import gmailapi

    try:
        configured = gmailapi.config_accounts(config_root())
    except gmailapi.GmailError:
        return []  # no accounts.toml: nothing to report
    return [
        (account, scope, gmailapi.token_authorised_at(configured[account], scope))
        for account in accounts
        if account in configured
        for scope in gmailapi.SCOPES
        if gmailapi.token_file(configured[account], scope).is_file()
    ]


def cmd_health(args) -> None:
    # No completeness warnings: the report's state section shows them.
    folders = resolve_folders(args.accounts)
    if not folders:
        raise AnalyticsError(f"no backups found in {data_root()}")
    con, accounts = open_sidecars(folders)
    opts = health.Options(window_days=args.window_days)
    with closing(con):
        tables = health.build(con, folders, opts, token_rows(accounts))
        emit(args, "Backup health report", "health", accounts, tables)


def cmd_senders(argv: list[str]) -> None:
    """report senders ACCOUNT [options]: the senders report on that account's backup."""
    from mailcellar.report import senders

    if not argv or argv[0].startswith("-"):
        if argv[:1] in (["-h"], ["--help"]):
            senders.main(["--help"], prog="mailcellar report senders ACCOUNT")
        sys.exit("usage: mailcellar report senders ACCOUNT [--top N] [...]; see --help")
    try:
        (folder,) = resolve_folders([argv[0]])
    except AnalyticsError as exc:
        sys.exit(f"error: {exc}")
    senders.main(["--folder", str(folder), *argv[1:]], prog="mailcellar report senders")


def add_common(parser) -> None:
    parser.add_argument(
        "accounts",
        nargs="*",
        help="Accounts (address, config folder name or unique part of an "
        "address); default: every backup in the data root",
    )
    parser.add_argument(
        "--top", type=int, default=15, help="Rows per section in the text report (default: 15)"
    )
    parser.add_argument("--output", default=None, help="Write the text report to this file")
    parser.add_argument(
        "--csv",
        default=None,
        metavar="DIR",
        help="Also write every section, all rows, as CSV files in DIR",
    )


def main(argv=None, prog: str | None = None) -> None:
    p = argparse.ArgumentParser(
        prog=prog or "mailcellar report", description="Reports over the backups in the data root."
    )
    sub = p.add_subparsers(dest="command", required=True)
    # Listed so `report --help` shows it; it takes its own arguments (cmd_senders).
    sub.add_parser("senders", help="Top senders and domains in one account's backup")

    s = sub.add_parser("storage", help="What takes up space, and what could go")
    add_common(s)
    s.add_argument(
        "--min-size",
        type=float,
        default=5,
        metavar="MB",
        help="Removal candidates: at least this size in MB (default: 5)",
    )
    s.add_argument(
        "--before",
        default="5",
        metavar="DATE|YEARS",
        help="Removal candidates: received before this date (YYYY-MM-DD) or "
        "this many years ago (default: 5)",
    )
    s.add_argument(
        "--include-labelled",
        action="store_true",
        help="Removal candidates: include sent, starred and user-labelled mail",
    )
    s.set_defaults(func=cmd_storage)

    b = sub.add_parser("bulk", help="Mailing lists and bulk senders; unsubscribe candidates")
    add_common(b)
    b.add_argument(
        "--min-messages",
        type=int,
        default=10,
        metavar="N",
        help="Candidates: sources with at least N messages (default: 10)",
    )
    b.add_argument(
        "--active-days",
        type=int,
        default=90,
        metavar="DAYS",
        help="'Still sending': a message within this many days (default: 90)",
    )
    b.add_argument(
        "--min-bulk-share",
        type=float,
        default=50,
        metavar="PERCENT",
        help="A source is bulk when at least this share of its messages has bulk "
        "signals (default: 50)",
    )
    b.set_defaults(func=cmd_bulk)

    for name, func, text in [
        ("volume", cmd_volume, "Mail received, sent and forwarded, by year and month"),
        ("threads", cmd_threads, "Thread sizes, longest threads, threads in both accounts"),
        ("labels", cmd_labels, "User labels in use, per account and across accounts"),
        ("correspondents", cmd_correspondents, "Who you write to, who writes to you, replies"),
    ]:
        r = sub.add_parser(name, help=text)
        add_common(r)
        r.set_defaults(func=func)

    h = sub.add_parser("health", help="Backup completeness, runs, gaps and pruned batches")
    add_common(h)
    h.add_argument(
        "--window-days",
        type=int,
        default=28,
        metavar="DAYS",
        help="Gmail's history retention: longer gaps between backups mean a full "
        "listing (default: 28)",
    )
    h.set_defaults(func=cmd_health)

    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["senders"]:
        cmd_senders(argv[1:])
        return
    args = p.parse_args(argv)
    try:
        args.func(args)
    except AnalyticsError as exc:
        sys.exit(f"error: {exc}")
