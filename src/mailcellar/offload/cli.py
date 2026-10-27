"""
Command line for mailcellar offload: argparse and one cmd_* per
subcommand (plan, apply, forget).
"""

import argparse
import csv
import logging
import sys
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

from mailcellar.backup import sidecar
from mailcellar.backup.gyb import backup_account
from mailcellar.dedup.apply import apply_plan, unusable_label, write_apply_log
from mailcellar.dedup.report import report_apply
from mailcellar.hold import holds
from mailcellar.hold.holds import HoldCheck
from mailcellar.offload import forget
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
from mailcellar.offload.plan import (
    PLAN_COLUMNS,
    build_plan,
    not_backed_up,
    plan_rows,
    searches,
    summary,
)
from mailcellar.offload.review import write_reviews
from mailcellar.offload.rules import RuleError, load_rules, short_retention, years_text
from mailcellar.paths import backup_folder, config_root, data_root
from mailcellar.report.cli import parse_before, resolve_folders
from mailcellar.report.db import AnalyticsError, completeness, open_sidecars
from mailcellar.report.output import human_size, render, write_csv

logger = logging.getLogger(__name__)


def offload_dir() -> Path:
    """offload/ in the data root (paths.data_root())."""
    return data_root() / "offload"


def review_dir() -> Path:
    """Where offload plan --review writes its pages."""
    return offload_dir() / "review"


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


def search_gmail(account: str, config: Path, queries: list[str]) -> dict[str, list[str]]:
    """Gmail's own search, read-only: each query's message IDs (Spam and Trash left out)."""
    from mailcellar import gmailapi

    folder = gmailapi.find_config_folder(config, account)
    gmail = gmailapi.GmailAccount(account, gmailapi.load_credentials(folder, account, "readonly"))
    found = {}
    for q in queries:
        logger.info(f"{account}: searching Gmail for {q!r}")
        try:
            found[q] = gmail.list_message_ids(q)
        except gmailapi.GmailError as exc:
            sys.exit(f"error: {account}: Gmail's search for {q!r} failed: {exc}")
    return found


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
            if why := short_retention(rule):
                logger.warning(why)
        hits = {
            (account, q): ids
            for account, queries in searches(rules, accounts).items()
            for q, ids in search_gmail(account, Path(args.config), queries).items()
        }
        results = build_plan(
            con, rules, accounts, {backup_account(f): str(f) for f in folders}, hits
        )
        for (account, q), n in not_backed_up(con).items():
            logger.warning(
                f"{account}: {n:,} messages Gmail finds for {q!r} aren't in the backup, so no "
                "rule can offload them; run a backup, then plan again."
            )
        reviews = write_reviews(con, results, accounts, review_dir()) if args.review else []
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
    for path in reviews:
        logger.info(f"Review → {path}")
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
            + (", not labelled by a label run, or vetoed before" if mode == "trash" else "")
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
    held = HoldCheck(folders, int(time.time() * 1000), rows)  # legal holds keep their mail in Gmail
    record = recorder(folders, batch, manifest_path(offload_dir(), batch))
    try:
        log = apply_plan(
            rows,
            mode,
            gmail_for,
            confirm,
            label,
            local_check=lambda row: held(row) or checks(row),
            on_trashed=record,
        )
    except gmailapi.GmailError as exc:
        sys.exit(f"error: {exc}")
    log, recovered = recover_trashed(log, mode, label, checks, record)
    if recovered:
        print(f"Recorded {recovered:,} messages an earlier run trashed without recording.")
    logged_mode = mode if mode == "dry-run" else f"{mode} ({label})"
    report_apply(log, logged_mode)
    upgrade_log(Path(args.log), APPLY_LOG_COLUMNS)
    write_apply_log(log, logged_mode, Path(args.log), APPLY_LOG_COLUMNS)
    print(f"Log → {args.log}")
    trashed = sum(e.outcome in ("trashed", "recorded") for e in log)
    if trashed:
        print(
            f"Recorded {trashed:,} offloaded messages (batch {batch}); 'backup --prune' "
            "keeps them in the backup. Gmail frees their space when it empties its Trash."
        )
        print(f"Manifest → {manifest_path(offload_dir(), batch)}")


def confirm_count(n: int, what: str) -> bool:
    """Ask for the count to be typed: for the steps that can't be undone by a click."""
    answer = input(f"{what} Type {n} to go ahead: ").strip().replace(",", "")
    return answer == str(n)


def gmail_state(account: str, config: Path, ids: list[str]):
    """Gmail's labels for each ID (None: gone), and the lookups that failed. Read-only."""
    from mailcellar import gmailapi
    from mailcellar.progress import Progress

    folder = gmailapi.find_config_folder(config, account)
    gmail = gmailapi.GmailAccount(account, gmailapi.load_credentials(folder, account, "readonly"))
    failures: list[str] = []
    with Progress(f"{account}: asking Gmail", len(ids)):
        live = gmail.message_labels(ids, failures)
    return live, dict(f.split(": ", 1) for f in failures)


def cmd_forget(args) -> None:
    try:
        folders = resolve_folders(args.accounts)
    except AnalyticsError as exc:
        sys.exit(f"error: {exc}")
    if args.undo:
        return forget_undo(folders, args.undo)
    if args.empty:
        return forget_empty(folders, args.yes)
    remind_due(folders)
    if not (args.batch or args.rule or args.due):
        sys.exit(
            "error: say which offloaded mail: --batch, --rule or --due (or --empty, --undo RUN)"
        )
    retain = retention_policy(args) if args.due else None
    run = datetime.now().strftime(forget.RUN_FORMAT)
    for folder in folders:
        account = backup_account(folder)
        sidecar.open_meta(folder).close()  # brings an older meta.sqlite up to date
        if retain is None:
            found = forget.candidates(folder, set(args.batch or ()), set(args.rule or ()))
        else:
            cutoffs = {name: rule.forget_ms for name, rule in retain.items()}
            found = forget.due_by_age(forget.candidates(folder, rules=set(retain)), cutoffs)
        if not found:
            if retain is not None:
                print(f"Forget — {account}: nothing due under retention")
            continue
        held = holds.held(folder, int(time.time() * 1000), [c.gmail_id for c in found])
        gone = [c.gmail_id for c in found if c.gone_ms is not None and c.gmail_id not in held]
        live, unread = gmail_state(account, Path(args.config), gone) if gone else ({}, {})
        choice = forget.choose(found, live, unread, held)
        if retain is None:
            print(f"Forget — {account}: {len(found):,} offloaded messages selected")
        else:
            print(f"Forget — {account}: {len(found):,} offloaded messages due under retention")
            for rule, year, n in forget.by_rule_and_year(found):
                print(f"  {rule} ({years_text(retain[rule].forget_after)}), {year}  {n:>8,}")
        print(f"  can be forgotten                {len(choice.forget):>8,}")
        if choice.not_gone:
            print(
                f"  not yet gone from Gmail         {len(choice.not_gone):>8,}  (in its Trash, or "
                "not seen gone: run a backup after Gmail empties its Trash)"
            )
        if choice.in_gmail:
            print(
                f"  Gmail has them again            {len(choice.in_gmail):>8,}  (restored, or "
                "moved back from Trash; left alone)"
            )
        if choice.held:
            names = sorted({name for _c, name in choice.held})
            print(
                f"  held                            {len(choice.held):>8,}  (hold "
                + ", ".join(names)
                + "; left as they are)"
            )
        if choice.unchecked:
            print(
                f"  couldn't be checked with Gmail  {len(choice.unchecked):>8,}  "
                f"(e.g. {choice.unchecked[0][1]}; tried again next run)"
            )
        if not choice.forget:
            continue
        print(
            "\nForgetting deletes them from this backup: their files move to "
            f"{forget.runs_dir(account)}/ for {forget.HOLD_DAYS} days (undo with --undo), and "
            "their sender, subject and text leave the indexes, plans, logs and manifests. "
            "Copies elsewhere keep them, and mailcellar can't reach those: any export you made, "
            "any second copy of the data root, and backups of this computer:"
        )
        for line in second_copy_report(data_root(), None):
            print(f"  {line}")
        if not args.yes:
            print("\nDry run: nothing was changed. Run again with --yes to forget them.")
            continue
        if not confirm_count(len(choice.forget), f"Forget {len(choice.forget):,} messages?"):
            print("Nothing forgotten.")
            continue
        why = forget.note(
            list(choice.forget),
            None if retain is None else {n: r.forget_after for n, r in retain.items()},
        )
        moved = forget.forget(
            folder, account, list(choice.forget), run, int(time.time() * 1000), why
        )
        ids = [c.gmail_id for c in choice.forget]
        indexes = forget.scrub_indexes(account, ids)
        files = forget.scrub_files({(account, g) for g in ids})
        until = forget.held_until(run)
        print(
            f"Forgot {moved:,} messages (run {run}): held in {forget.runs_dir(account) / run} "
            f"until {until:%Y-%m-%d}, then 'offload forget --empty' deletes them for good. "
            f"Taken out of the search index ({indexes['search']:,}) and the dedup catalogue "
            f"({indexes['dedup']:,}); sender and subject blanked in {len(files):,} files"
            + (
                f" and {files['review pages']:,} review pages deleted"
                if "review pages" in files
                else ""
            )
            + "."
        )


def retention_policy(args) -> dict:
    """
    offload forget --due: the rules with forget_after (or only --rule's), by
    name. Stops on an unreadable rules file, a --batch, or a --rule without
    forget_after: a retention run must never widen by accident.
    """
    if args.batch:
        sys.exit("error: --due selects by the rules' forget_after; it can't take --batch")
    try:
        rules = load_rules(Path(args.rules), parse_before, resolve_account)
    except RuleError as exc:
        sys.exit(f"error: {exc}")
    retain = {r.name: r for r in rules if r.forget_after}
    for rule in retain.values():
        if why := short_retention(rule):
            logger.warning(why)
    if args.rule:
        missing = sorted(set(args.rule) - set(retain))
        if missing:
            sys.exit(f"error: no rule with forget_after named {', '.join(missing)}")
        retain = {name: retain[name] for name in args.rule}
    if not retain:
        sys.exit(f"error: no rule in {args.rules} has forget_after")
    return retain


def remind_due(folders: list[Path]) -> None:
    """Say which held forget runs are past their hold, so they don't stay held for ever."""
    now = datetime.now()
    for folder in folders:
        account = backup_account(folder)
        due = forget.due(forget.held_runs(account), now)
        if due:
            print(
                f"{account}: {len(due):,} forget runs held {forget.HOLD_DAYS} days or more "
                f"({', '.join(due)}); 'offload forget --empty' deletes them for good.\n"
            )


def forget_empty(folders: list[Path], yes: bool) -> None:
    """offload forget --empty: delete runs past the hold, for good."""
    now = datetime.now()
    for folder in folders:
        account = backup_account(folder)
        runs = forget.held_runs(account)
        due = forget.due(runs, now)
        waiting = [r for r in runs if r not in due]
        for run in waiting:
            print(f"{account}: run {run} is held until {forget.held_until(run):%Y-%m-%d}")
        under_hold = forget.runs_under_hold(folder, account, due, int(now.timestamp() * 1000))
        for run, names in under_hold.items():
            print(
                f"{account}: run {run} may hold mail under hold {', '.join(names)}; "
                "it is kept (release the hold, or --undo the run)"
            )
        due = [r for r in due if r not in under_hold]
        if not due:
            continue
        print(
            f"{account}: {len(due):,} runs held {forget.HOLD_DAYS} days or more: {', '.join(due)}"
        )
        if not yes:
            print("Dry run: nothing deleted. Run again with --yes to delete them for good.")
            continue
        if not confirm_count(len(due), f"Delete {len(due):,} runs for good?"):
            print("Nothing deleted.")
            continue
        n = forget.empty(folder, account, due, int(time.time() * 1000))
        print(f"{account}: deleted {n:,} forgotten messages for good.")


def forget_undo(folders: list[Path], run: str) -> None:
    """offload forget --undo RUN: put a held run back."""
    done = False
    for folder in folders:
        account = backup_account(folder)
        if run not in forget.held_runs(account):
            continue
        n = forget.undo(folder, account, run)
        done = True
        print(
            f"{account}: put {n:,} messages back from run {run}. Run 'mailcellar meta {account}' "
            "to re-parse them; the next search and 'dedup index' take them in again."
        )
    if not done:
        sys.exit(f"error: no held forget run {run!r} (emptied, undone, or never made)")


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
    pl.add_argument(
        "--review",
        action="store_true",
        help="Also write a review page per rule, in <data root>/offload/review/: samples of "
        "what it would offload and what it keeps, the largest messages and threads, "
        "attachment types (senders and subjects, never bodies; for you alone)",
    )
    pl.add_argument(
        "--config",
        default=str(config_root()),
        help="Config folder, for rules with a query: Gmail is searched, read-only "
        f"(default: {config_root()})",
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

    fg = sub.add_parser(
        "forget",
        help="Delete offloaded mail Gmail no longer has from the backup, held "
        f"{forget.HOLD_DAYS} days first",
    )
    fg.add_argument(
        "accounts", nargs="*", help="Only these accounts' backups (default: all of them)"
    )
    fg.add_argument(
        "--batch", action="append", help="Offloaded messages of this batch (repeatable)"
    )
    fg.add_argument("--rule", action="append", help="Offloaded messages of this rule (repeatable)")
    fg.add_argument(
        "--due",
        action="store_true",
        help="Offloaded mail the rules' forget_after makes due: older than that many years "
        "(with --rule, only those rules)",
    )
    fg.add_argument("--rules", default=str(rules), help=f"Rules file, for --due (default: {rules})")
    fg.add_argument(
        "--yes",
        action="store_true",
        help="Forget (or with --empty, delete) after the count is typed; without it, a dry run",
    )
    fg.add_argument(
        "--empty",
        action="store_true",
        help=f"Delete forget runs held {forget.HOLD_DAYS} days or more, for good",
    )
    fg.add_argument("--undo", metavar="RUN", help="Put a held forget run back into the backup")
    fg.add_argument(
        "--config",
        default=str(config_root()),
        help="Config folder: accounts.toml, settings.toml, one folder per account "
        f"(default: {config_root()})",
    )
    fg.set_defaults(func=cmd_forget)

    args = p.parse_args(argv)
    try:
        args.func(args)
    except (AnalyticsError, RuleError) as exc:
        sys.exit(f"error: {exc}")
