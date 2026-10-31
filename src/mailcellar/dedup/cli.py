"""
Command line for mailcellar dedup: argparse and one cmd_* per subcommand
(index, match, plan, apply).
"""

import argparse
import logging
import sys
import time
from pathlib import Path

from mailcellar.dedup.apply import (
    DEDUP_LABEL,
    apply_plan,
    labelled_rows,
    log_state,
    read_removals,
    unusable_label,
    within_limit,
    write_apply_log,
)
from mailcellar.dedup.db import open_db
from mailcellar.dedup.index import index_backup
from mailcellar.dedup.match import export_match_csv, match
from mailcellar.dedup.paths import default_db, default_log, default_plan
from mailcellar.dedup.plan import SCOPES, build_plan, resolve_account, write_plan
from mailcellar.dedup.report import report_apply, report_match, report_plan
from mailcellar.dedup.rules import ORIGIN
from mailcellar.message import PARSER_VERSION
from mailcellar.paths import config_root, data_root, find_backups

logger = logging.getLogger(__name__)


def cmd_index(args) -> None:
    # Default: every backup in the data root (find_backups skips folders with
    # no msg-db.sqlite, such as _forgotten/).
    folders = [Path(f) for f in args.folders] or find_backups()
    if not folders:
        sys.exit(f"error: no backup folders given or found in {data_root()}.")

    backups = []
    for folder in folders:
        backup_dir = folder.expanduser().resolve()
        if not (backup_dir / "msg-db.sqlite").is_file():
            sys.exit(f"error: '{backup_dir}' is not a GYB backup (no msg-db.sqlite).")
        backups.append(backup_dir)

    con = open_db(Path(args.db))
    try:
        row = con.execute("SELECT value FROM meta WHERE name = 'parser_version'").fetchone()
        reparse = row is None or row[0] != PARSER_VERSION
        if reparse:
            logger.info("Parser changed since last index; re-parsing every message.")
        results = [index_backup(con, b, reparse) for b in backups]
        con.execute("INSERT OR REPLACE INTO meta VALUES ('parser_version', ?)", (PARSER_VERSION,))
        con.commit()
        summary = con.execute(
            """SELECT account, count(*),
                      sum(message_id = ''), sum(body_hash = '')
               FROM messages GROUP BY account ORDER BY account"""
        ).fetchall()
    finally:
        con.close()

    print(
        f"\n{'Account':<30} {'Total':>8} {'Parsed':>8} {'Same':>8} "
        f"{'Removed':>8} {'Errors':>7} {'No MsgID':>9} {'No body':>8} {'Meta':>8}"
    )
    no_mid = {acct: (m, b) for acct, _n, m, b in summary}
    for r in results:
        m, b = no_mid.get(r["account"], (0, 0))
        print(
            f"{r['account']:<30} {r['total']:>8,} {r['parsed']:>8,} "
            f"{r['unchanged']:>8,} {r['removed']:>8,} {r['errors']:>7,} "
            f"{m:>9,} {b:>8,} {r['from_meta']:>8,}"
        )
    print(f"\nIndex → {args.db}")


def cmd_match(args) -> None:
    db_path = Path(args.db)
    if not db_path.is_file():
        sys.exit(f"error: {db_path} not found. Run 'index' first.")
    con = open_db(db_path)
    try:
        match(con)
        report_match(con, args.show)
        if args.csv:
            export_match_csv(con, Path(args.csv))
    finally:
        con.close()


def cmd_plan(args) -> None:
    db_path = Path(args.db)
    if not db_path.is_file():
        sys.exit(f"error: {db_path} not found. Run 'index' first.")
    if args.scope != "within" and not args.keep:
        sys.exit(
            f"error: --keep is required with --scope {args.scope} "
            "(it decides which copy of a cross-account duplicate stays)."
        )
    con = open_db(db_path)
    try:
        keep = None
        if args.scope != "within":
            keep = ORIGIN if args.keep == ORIGIN else resolve_account(con, args.keep)
        match(con)  # cheap; keeps the plan in line with the current index
        ignored = set(args.ignore_label)
        plan = build_plan(con, keep, ignored, args.scope)
    finally:
        con.close()

    report_plan(plan, keep, ignored, args.scope)
    write_plan(plan, Path(args.out))
    print(f"\nPlan → {args.out}")


def cmd_apply(args) -> None:
    mode = "label" if args.label is not None else "trash" if args.trash is not None else "dry-run"
    dedup_label = args.label or args.trash or DEDUP_LABEL
    rows = read_removals(Path(args.plan))
    state = log_state(Path(args.log), dedup_label)
    if mode == "trash":
        before = len(rows)
        # Trashed by an earlier run: reading it again only finds it in Trash.
        rows = list(labelled_rows(rows, state.labelled, state.vetoed | state.trashed))
        if len(rows) < before:
            logger.info(
                f"{before - len(rows):,} removals not labelled by a label run, vetoed "
                "or trashed before: not read."
            )
    if not rows:
        sys.exit(
            "Nothing to do: "
            + (
                "no removal a label run labelled that wasn't vetoed or trashed since."
                if mode == "trash"
                else "the plan has no 'remove' rows."
            )
        )
    # Messages a label run labelled carry the label in a plan made after the
    # next backup; only a label already there for other reasons makes it unusable.
    problem = unusable_label(
        [r for r in rows if (r["account"], r["id"]) not in state.labelled], dedup_label
    )
    if problem:
        sys.exit(f"error: can't use {dedup_label!r} as the apply label: {problem}.")

    from mailcellar import gmailapi, mailbox
    from mailcellar.hold.holds import HoldCheck
    from mailcellar.paths import backup_folder

    folders = {a: backup_folder(a) for a in {r["account"] for r in rows}}
    gmail_for = mailbox.opener(Path(args.config), folders)

    def confirm(prompt):
        if args.yes:
            return True
        return input(f"{prompt} [y/N] ").strip().lower() == "y"

    # Legal holds keep their mail in Gmail: a held duplicate is skipped, saying which hold.
    held = HoldCheck(folders, int(time.time() * 1000), rows)
    rows, local_check, skipped = within_limit(rows, args.limit, held)
    try:
        log = skipped + apply_plan(
            rows, mode, gmail_for, confirm, dedup_label, local_check=local_check
        )
    except gmailapi.GmailError as exc:
        sys.exit(f"error: {exc}")
    # The log's mode column records which label a label or trash run used.
    logged_mode = mode if mode == "dry-run" else f"{mode} ({dedup_label})"
    report_apply(log, logged_mode)
    write_apply_log(log, logged_mode, Path(args.log))
    print(f"Log → {args.log}")


def main(argv=None, prog: str | None = None) -> None:
    db, plan, log = default_db(), default_plan(), default_log()
    p = argparse.ArgumentParser(
        prog=prog or "mailcellar dedup",
        description="Deduplication within and across accounts, for the backups in the data root.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    p_index = sub.add_parser("index", help="Catalogue backup messages into the index DB")
    p_index.add_argument(
        "folders", nargs="*", help="Backup folders (default: every backup in the data root)"
    )
    p_index.add_argument("--db", default=str(db), help=f"Index database path (default: {db})")
    p_index.set_defaults(func=cmd_index)

    p_match = sub.add_parser(
        "match", help="Group copies of the same message, within and across accounts"
    )
    p_match.add_argument("--db", default=str(db), help=f"Index database path (default: {db})")
    p_match.add_argument("--csv", default=None, help="Also write every matched copy to this CSV")
    p_match.add_argument(
        "--show",
        type=int,
        default=20,
        help="Within-account groups to list in the report (default: 20)",
    )
    p_match.set_defaults(func=cmd_match)

    p_plan = sub.add_parser("plan", help="Decide keep/remove/review per copy")
    p_plan.add_argument(
        "--scope",
        choices=SCOPES,
        default="cross",
        help="Which duplicates to plan for: across accounts (default), within one account, or both",
    )
    p_plan.add_argument(
        "--keep",
        default=None,
        help="Account whose copies are kept: full address or a "
        "unique part of it (e.g. 'example.org', 'gmail'); or "
        "'origin' to remove only auto-forwarded copies and "
        "keep the one in the account they came from. "
        "Required unless --scope within",
    )
    p_plan.add_argument(
        "--ignore-label",
        action="append",
        default=[],
        metavar="LABEL",
        help="User label that may be lost on removal (repeatable)",
    )
    p_plan.add_argument("--db", default=str(db), help=f"Index database path (default: {db})")
    p_plan.add_argument("--out", default=str(plan), help=f"Plan CSV path (default: {plan})")
    p_plan.set_defaults(func=cmd_plan)

    p_apply = sub.add_parser(
        "apply", help="Check the plan against Gmail; label, then trash, removals"
    )
    stage = p_apply.add_mutually_exclusive_group()
    stage.add_argument(
        "--label",
        nargs="?",
        const=DEDUP_LABEL,
        default=None,
        metavar="NAME",
        help="Add the label NAME (default: %(const)s) to removals that "
        "pass the checks; it is created if missing",
    )
    stage.add_argument(
        "--trash",
        nargs="?",
        const=DEDUP_LABEL,
        default=None,
        metavar="NAME",
        help="Move removals that pass the checks and carry the label NAME "
        "(default: %(const)s) to Trash",
    )
    p_apply.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only the first N removals in the plan that no legal hold keeps; held ones "
        "on the way are logged as skipped",
    )
    p_apply.add_argument(
        "--yes", action="store_true", help="Don't ask for confirmation before trashing"
    )
    p_apply.add_argument(
        "--plan", default=str(plan), help=f"Plan CSV from 'plan' (default: {plan})"
    )
    p_apply.add_argument(
        "--config",
        default=str(config_root()),
        help="Config folder: accounts.toml, settings.toml, one folder per account "
        f"(default: {config_root()})",
    )
    p_apply.add_argument("--log", default=str(log), help=f"Apply log, appended to (default: {log})")
    p_apply.set_defaults(func=cmd_apply)

    args = p.parse_args(argv)
    args.func(args)
