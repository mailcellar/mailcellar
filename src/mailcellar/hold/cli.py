"""
Command line for mailcellar hold: place, list, show and release legal holds
(holds.py). Placing or releasing one changes nothing in Gmail and removes
nothing from the backup: it records the hold, and the commands that remove
mail leave what it covers alone.
"""

import argparse
import logging
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from mailcellar.backup.gyb import backup_account
from mailcellar.hold import holds
from mailcellar.hold.holds import Hold, HoldError
from mailcellar.report.cli import parse_before, resolve_folders
from mailcellar.report.db import AnalyticsError
from mailcellar.report.output import Table, day, render

logger = logging.getLogger(__name__)

NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def now_ms() -> int:
    return int(time.time() * 1000)


def describe(c: holds.Criteria) -> str:
    """The criteria in one line, as the command takes them."""
    parts = [f"party {p}" for p in c.parties]
    if c.after_ms is not None:
        parts.append(f"from {day(c.after_ms)}")
    if c.before_ms is not None:
        parts.append(f"before {day(c.before_ms)}")
    if c.label:
        parts.append(f"label {c.label!r}")
    text = " · ".join(parts)
    if c.ids:
        text += (" · and " if text else "") + f"{len(c.ids):,} messages by ID"
    return text


def folders_for(names: list[str]) -> list[Path]:
    try:
        folders = resolve_folders(names)
    except AnalyticsError as exc:
        sys.exit(f"error: {exc}")
    if not folders:
        sys.exit("error: no backups found in the data root")
    return folders


def cmd_add(p: argparse.ArgumentParser, args) -> int:
    if not NAME_RE.fullmatch(args.name):
        p.error("NAME: letters, digits, . _ - (e.g. landlord-2026)")

    def date(option: str, value: str | None) -> int | None:
        try:
            return parse_before(value)
        except AnalyticsError:
            p.error(f"{option} {value!r}: give a date (YYYY-MM-DD) or a number of years")

    until = None
    if args.until is not None:
        try:
            until = int(datetime.strptime(args.until, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())
        except ValueError:
            p.error(f"--until {args.until!r}: give a date (YYYY-MM-DD)")
        until *= 1000
    try:
        after, before = date("--after", args.after), date("--before", args.before)
        c = holds.criteria(args.party, after, before, args.label or "", args.id)
    except HoldError as exc:
        p.error(str(exc))
    hold = Hold(args.name, args.reason, c, now_ms(), until)
    if until is not None and until <= hold.created_ms:
        p.error("--until must be in the future")
    status = 0
    for folder in folders_for(args.accounts):
        account = backup_account(folder)
        try:
            holds.add(folder, account, hold)
        except HoldError as exc:
            logger.error(str(exc))
            status = 1
            continue
        held = holds.held(folder, hold.created_ms)
        n = sum(1 for name in held.values() if name == hold.name)
        print(f"{account}: hold {hold.name!r} placed; it keeps {n:,} messages now")
    print(f"  {describe(c)}" + (f" · until {args.until}" if args.until else ""))
    return status


def cmd_release(args) -> int:
    done = []
    when = now_ms()
    for folder in folders_for(args.accounts):
        account = backup_account(folder)
        if holds.release(folder, account, args.name, when):
            done.append(account)
            print(f"{account}: hold {args.name!r} released; its record is kept")
    if not done:
        sys.exit(f"error: no hold {args.name!r} in force in those backups")
    return 0


def status_of(h: Hold, when: int) -> str:
    if h.released_ms is not None:
        return f"released {day(h.released_ms)}"
    if not h.active(when):
        return f"ended {day(h.until_ms)}"
    return "in force"


def cmd_list(args) -> int:
    when = now_ms()
    rows = []
    for folder in folders_for(args.accounts):
        account = backup_account(folder)
        held = holds.held(folder, when)
        for h in holds.read_holds(folder):
            if not args.all and not h.active(when):
                continue
            n = sum(1 for name in held.values() if name == h.name) if h.active(when) else None
            rows.append(
                (
                    h.name,
                    account,
                    h.reason,
                    day(h.created_ms),
                    day(h.until_ms) if h.until_ms else "",
                    status_of(h, when),
                    n,
                )
            )
    render(
        [
            Table(
                "holds",
                "Holds" + ("" if args.all else " in force (--all lists ended and released ones)"),
                [
                    ("Name", "text"),
                    ("Account", "text"),
                    ("Reason", "text"),
                    ("Since", "text"),
                    ("Until", "text"),
                    ("Status", "text"),
                    ("Held now", "int"),
                ],
                rows,
                limit=False,
            )
        ],
        0,
        sys.stdout,
    )
    return 0


def cmd_show(args) -> int:
    when = now_ms()
    found = False
    for folder in folders_for(args.accounts):
        account = backup_account(folder)
        mine = [h for h in holds.read_holds(folder) if h.name == args.name]
        if not mine:
            continue
        found = True
        h = mine[-1]  # the latest by that name
        print(f"{account}: hold {h.name!r}, {status_of(h, when)}, placed {day(h.created_ms)}")
        print(f"  reason: {h.reason}")
        print(f"  covers: {describe(h.criteria)}")
        if h.until_ms:
            print(f"  until:  {day(h.until_ms)}")
        facts = [f for f in holds.read_facts(folder) if holds.covers(h.criteria, f)]
        known = [f for f in facts if f.known]
        if known:
            dates = sorted(f.internal_date for f in known if f.internal_date is not None)
            print(
                f"  keeps {len(known):,} messages"
                + (f", {day(dates[0])} to {day(dates[-1])}" if dates else "")
            )
        else:
            print("  keeps no messages in the backup yet")
        gone = [f for f in known if not f.in_gmail]
        if gone:
            print(
                f"  {len(gone):,} of them no longer in their mailbox, as far as the backup knows: "
                "it holds the only copy"
            )
        unknown = len(facts) - len(known)
        if unknown:
            print(
                f"  {unknown:,} forgotten messages it may cover (their addresses and labels "
                "aren't kept): forget --empty leaves them held"
            )
    if not found:
        sys.exit(f"error: no hold {args.name!r} in those backups")
    return 0


def main(argv=None, prog: str | None = None) -> int:
    p = argparse.ArgumentParser(
        prog=prog or "mailcellar hold",
        description="Legal holds: mail that offload, dedup and forget "
        "leave alone, in its mailbox and in the backup. A hold follows its criteria, so mail "
        "that matches later is held too. Placing or releasing one changes nothing in the "
        "mailbox.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    ad = sub.add_parser("add", help="Place a hold")
    ad.add_argument("name", metavar="NAME", help="A short name for the hold, e.g. landlord-2026")
    ad.add_argument("accounts", nargs="*", metavar="ACCOUNT", help="Backups (default: all)")
    ad.add_argument("--reason", required=True, help="Why the mail is held (recorded with it)")
    ad.add_argument(
        "--party",
        action="append",
        default=[],
        metavar="ADDRESS|@DOMAIN",
        help="Mail from or to this address, or any address at @domain (repeatable: any)",
    )
    ad.add_argument("--after", metavar="DATE", help="Mail from this date on (YYYY-MM-DD)")
    ad.add_argument("--before", metavar="DATE", help="Mail older than this (YYYY-MM-DD)")
    ad.add_argument("--label", metavar="NAME", help="Mail with this user label (case ignored)")
    ad.add_argument(
        "--id", action="append", default=[], metavar="GMAIL_ID", help="A message (repeatable)"
    )
    ad.add_argument("--until", metavar="DATE", help="End the hold then (default: until released)")

    ls = sub.add_parser("list", help="List the holds in force")
    ls.add_argument("accounts", nargs="*", metavar="ACCOUNT", help="Backups (default: all)")
    ls.add_argument("--all", action="store_true", help="Released and ended holds too")

    sh = sub.add_parser("show", help="What a hold keeps, and what only the backup has")
    sh.add_argument("name", metavar="NAME")
    sh.add_argument("accounts", nargs="*", metavar="ACCOUNT", help="Backups (default: all)")

    rl = sub.add_parser("release", help="End a hold; its record is kept")
    rl.add_argument("name", metavar="NAME")
    rl.add_argument("accounts", nargs="*", metavar="ACCOUNT", help="Backups (default: all)")

    args = p.parse_args(argv)
    if args.command == "add":
        return cmd_add(ad, args)
    return {"list": cmd_list, "show": cmd_show, "release": cmd_release}[args.command](args)
