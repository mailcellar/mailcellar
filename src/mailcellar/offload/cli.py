"""
Command line for mailcellar offload: argparse and one cmd_* per
subcommand (plan, apply).
"""

import argparse
import csv
import logging
import sys
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

from mailcellar.backup.gyb import backup_account
from mailcellar.dedup.apply import apply_plan, unusable_label, write_apply_log
from mailcellar.dedup.report import report_apply
from mailcellar.offload.apply import (
    APPLY_LOG_COLUMNS,
    OFFLOAD_LABEL,
    BackupChecks,
    last_backup_ms,
    log_state,
    manifest_path,
    planned_ms,
    read_plan,
    recorder,
    recover_trashed,
    remaining,
    second_copy_report,
    stale_backups,
    upgrade_log,
)
from mailcellar.offload.plan import PLAN_COLUMNS, build_plan, plan_rows, summary
from mailcellar.offload.rules import RuleError, load_rules
from mailcellar.paths import backup_folder, config_root, data_root
from mailcellar.report.cli import parse_before, resolve_folders
from mailcellar.report.db import AnalyticsError, completeness, open_sidecars
from mailcellar.report.output import human_size, render, write_csv

logger = logging.getLogger(__name__)


def offload_dir() -> Path:
    """offload/ in the data root (paths.data_root())."""
    return data_root() / "offload"


def resolve_account(name: str) -> str:
    try:
        (folder,) = resolve_folders([name])
    except AnalyticsError as exc:
        raise RuleError(f"account: {exc}") from None
    return backup_account(folder)


def write_plan(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=PLAN_COLUMNS)
        w.writeheader()
        w.writerows(rows)


def cmd_plan(args) -> None:
    folders = resolve_folders(args.accounts)
    if not folders:
        raise AnalyticsError(f"no backups found in {data_root()}")
    con, accounts = open_sidecars(folders)
    with closing(con):
        for warning in completeness(con, accounts):
            logger.warning(warning)
        rules = load_rules(Path(args.rules), parse_before, resolve_account)
        for rule in rules:
            if rule.accounts and not set(rule.accounts) & set(accounts):
                logger.warning(f"rule {rule.name!r}: none of its accounts is chosen")
        results = build_plan(con, rules, accounts, {backup_account(f): str(f) for f in folders})
    planned = int(time.time() * 1000)
    rows = plan_rows(results, planned)
    write_plan(rows, Path(args.plan))
    # Every plan is kept: apply logs name the plan they used by its planned time.
    kept = (
        offload_dir()
        / "plans"
        / f"{time.strftime('%Y-%m-%d-%H%M%S', time.gmtime(planned / 1000))}.csv"
    )
    write_plan(rows, kept)

    tables = summary(results, args.top)
    sys.stdout.write(
        f"Offload plan — {', '.join(accounts)}\n"
        f"Generated {datetime.now():%Y-%m-%d %H:%M} from {args.rules}\n"
    )
    render(tables, args.top, sys.stdout)
    if args.csv:
        paths = write_csv(tables, Path(args.csv), "offload")
        logger.info(f"CSV → {args.csv}/ ({len(paths)} files)")
    size = sum(r["size"] for r in rows)
    logger.info(
        f"Plan → {args.plan}: {len(rows):,} messages, {human_size(size)} (kept as {kept}). "
        "Nothing in Gmail or the backup was changed."
    )


def cmd_apply(args) -> None:
    mode = "label" if args.label is not None else "trash" if args.trash is not None else "dry-run"
    label = args.label or args.trash or OFFLOAD_LABEL
    rows = read_plan(Path(args.plan))
    plan_folders = {a: backup_folder(a) for a in {r["account"] for r in rows}}
    before = len(rows)
    rows = remaining(rows, mode, label, plan_folders, Path(args.log))
    if len(rows) < before:
        logger.info(
            f"{before - len(rows):,} plan rows already done (offloaded"
            + (", or labelled before" if mode == "label" else "")
            + (", or vetoed before" if mode == "trash" else "")
            + ")."
        )
    if args.rule:
        rows = [r for r in rows if r["rule"] in args.rule]
    if args.accounts:
        chosen = {resolve_account(a) for a in args.accounts}
        rows = [r for r in rows if r["account"] in chosen]
    if args.limit is not None:
        rows = rows[: args.limit]
    if not rows:
        sys.exit("Nothing to do: no plan rows match.")
    # Messages this tool labelled carry the label in a plan made after the next
    # backup; only a label already there for other reasons makes it unusable.
    labelled, _vetoed = log_state(Path(args.log), label)
    problem = unusable_label(
        [r for r in rows if (r["account"], r["gmail_id"]) not in labelled], label
    )
    if problem:
        sys.exit(f"error: can't use {label!r} as the offload label: {problem}.")

    accounts = sorted({r["account"] for r in rows})
    folders = {a: backup_folder(a) for a in accounts}
    stale = stale_backups(folders, accounts, planned_ms(rows))
    for account, why in stale.items():
        logger.warning(f"{account}: {why}.")
    if mode == "trash":
        if stale:
            sys.exit(
                "error: run a backup of each account above before trashing, so the backup "
                "is known to be complete."
            )
        print(
            "Trashed messages leave Gmail for good when its Trash is emptied (after 30 "
            f"days). The backup in {data_root()} then holds the only copy, unless it is "
            "copied elsewhere:"
        )
        for line in second_copy_report(data_root(), last_backup_ms(folders, accounts)):
            print(f"  {line}")

    from mailcellar import gmailapi

    config_root = Path(args.config)

    def gmail_for(account, scope):
        folder = gmailapi.find_config_folder(config_root, account)
        return gmailapi.GmailAccount(account, gmailapi.load_credentials(folder, account, scope))

    def confirm(prompt):
        if args.yes:
            return True
        return input(f"{prompt} [y/N] ").strip().lower() == "y"

    batch = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    checks = BackupChecks(folders)
    record = recorder(folders, batch, manifest_path(offload_dir(), batch))
    try:
        log = apply_plan(
            rows, mode, gmail_for, confirm, label, local_check=checks, on_trashed=record
        )
    except gmailapi.GmailError as exc:
        sys.exit(f"error: {exc}")
    recovered = recover_trashed(log, mode, label, checks, record)
    if recovered:
        print(f"Recorded {recovered:,} messages an earlier run trashed without recording.")
    logged_mode = mode if mode == "dry-run" else f"{mode} ({label})"
    report_apply(log, logged_mode)
    upgrade_log(Path(args.log), APPLY_LOG_COLUMNS)
    write_apply_log(log, logged_mode, Path(args.log), APPLY_LOG_COLUMNS)
    print(f"Log → {args.log}")
    trashed = sum(e["outcome"] in ("trashed", "recorded") for e in log)
    if trashed:
        print(
            f"Recorded {trashed:,} offloaded messages (batch {batch}); --sync keeps them in the "
            "backup. Gmail frees their space when it empties its Trash."
        )
        print(f"Manifest → {manifest_path(offload_dir(), batch)}")


def main(argv=None, prog: str | None = None) -> None:
    here = offload_dir()
    rules, plan, log = (
        here / "rules.toml",
        here / "offload-plan.csv",
        here / "offload-apply-log.csv",
    )
    p = argparse.ArgumentParser(
        prog=prog or "mailcellar offload",
        description="Take old mail out of Gmail, keeping it in the backup.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    pl = sub.add_parser("plan", help="Select the messages the rules would offload (read-only)")
    pl.add_argument(
        "accounts",
        nargs="*",
        help="Accounts (address, config folder name or unique part of an "
        "address); default: every backup in the data root",
    )
    pl.add_argument("--rules", default=str(rules), help=f"Rules file (default: {rules})")
    pl.add_argument("--plan", default=str(plan), help=f"Plan CSV to write (default: {plan})")
    pl.add_argument(
        "--top", type=int, default=15, help="Senders per rule in the summary (default: 15)"
    )
    pl.add_argument(
        "--csv", default=None, metavar="DIR", help="Also write the summary tables as CSV in DIR"
    )
    pl.set_defaults(func=cmd_plan)

    ap = sub.add_parser(
        "apply", help="Check the plan against Gmail and the backup; label, then trash"
    )
    ap.add_argument(
        "accounts", nargs="*", help="Only these accounts' plan rows (default: all of them)"
    )
    stage = ap.add_mutually_exclusive_group()
    stage.add_argument(
        "--label",
        nargs="?",
        const=OFFLOAD_LABEL,
        default=None,
        metavar="NAME",
        help="Add the label NAME (default: %(const)s) to messages that pass the checks; "
        "it is created if missing. Removing it in Gmail vetoes a message",
    )
    stage.add_argument(
        "--trash",
        nargs="?",
        const=OFFLOAD_LABEL,
        default=None,
        metavar="NAME",
        help="Move messages that pass the checks and carry the label NAME "
        "(default: %(const)s) to Trash, and record them as offloaded",
    )
    ap.add_argument("--rule", action="append", help="Only this rule's rows (repeatable)")
    ap.add_argument("--limit", type=int, default=None, help="Only the first N plan rows")
    ap.add_argument("--yes", action="store_true", help="Don't ask before trashing")
    ap.add_argument("--plan", default=str(plan), help=f"Plan CSV from 'plan' (default: {plan})")
    ap.add_argument(
        "--config",
        default=str(config_root()),
        help="Config folder: accounts.toml, settings.toml, one folder per account "
        f"(default: {config_root()})",
    )
    ap.add_argument("--log", default=str(log), help=f"Apply log, appended to (default: {log})")
    ap.set_defaults(func=cmd_apply)

    args = p.parse_args(argv)
    try:
        args.func(args)
    except (AnalyticsError, RuleError) as exc:
        sys.exit(f"error: {exc}")
