"""
Command line for mailcellar offload: argparse and one cmd_* per
subcommand (plan, suggest, apply, forget).
"""

import argparse
import csv
import logging
import sys
import time
from collections.abc import Collection, Mapping, Sequence
from contextlib import closing
from datetime import datetime
from pathlib import Path

from mailcellar.backup import sidecar
from mailcellar.backup.gyb import backup_account
from mailcellar.dedup.apply import apply_plan, log_state, unusable_label, write_apply_log
from mailcellar.dedup.report import report_apply
from mailcellar.hold import holds
from mailcellar.hold.holds import HoldCheck
from mailcellar.mailbox import mailbox_name
from mailcellar.offload import forget, suggest
from mailcellar.offload.apply import (
    APPLY_LOG_COLUMNS,
    OFFLOAD_LABEL,
    BackupChecks,
    backup_problems,
    last_backups,
    manifest_path,
    planned_ms,
    read_plan,
    recorder,
    recover_trashed,
    remaining,
    second_copy_report,
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
from mailcellar.offload.rules import (
    RuleError,
    load_rules,
    parse_rule,
    read_try,
    rule_toml,
    short_retention,
    years_text,
)
from mailcellar.paths import backup_folder, config_root, data_root
from mailcellar.report.cli import parse_before, resolve_folders
from mailcellar.report.db import AnalyticsError, completeness, open_sidecars
from mailcellar.report.output import human_size, render, write_csv

logger = logging.getLogger(__name__)


def offload_dir() -> Path:
    """offload/ in the data root (paths.data_root())."""
    return data_root() / "offload"


def rules_file() -> Path:
    """The rules file plan reads unless told another."""
    return offload_dir() / "rules.toml"


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
    from mailcellar import gmailapi, mailbox

    found = {}
    with closing(mailbox.opener(config)(account, "readonly")) as gmail:
        for q in queries:
            logger.info(f"{account}: searching Gmail for {q!r}")
            try:
                found[q] = gmail.list_message_ids(q)
            except gmailapi.GmailError as exc:
                sys.exit(f"error: {account}: Gmail's search for {q!r} failed: {exc}")
    return found


NO_RULES = "none"  # --rules none: no rules file, only the --try rule


def plan_rules(args) -> tuple[list, dict | None]:
    """
    (the rules to plan, the --try rule as written or None): the rules file's,
    unless --rules none, then the --try rule after them, so its numbers count
    only what the file's rules don't already take.
    """
    if args.rules == NO_RULES and not args.try_rule:
        raise RuleError("--rules none plans nothing without --try")
    rules = (
        []
        if args.rules == NO_RULES
        else load_rules(Path(args.rules), parse_before, resolve_account)
    )
    if not args.try_rule:
        return rules, None
    raw = read_try(args.try_rule)
    tried = parse_rule(raw, parse_before, resolve_account)
    if tried.name in {r.name for r in rules}:
        raise RuleError(
            f"--try: {args.rules} has a rule named {tried.name!r} already; give it another name"
        )
    return [*rules, tried], raw


def cmd_plan(args) -> None:
    folders = resolve_folders(args.accounts)
    if not folders:
        raise AnalyticsError(f"no backups found in {data_root()}")
    con, accounts = open_sidecars(folders)
    with closing(con):
        for warning in completeness(con, accounts):
            logger.warning(warning)
        rules, tried = plan_rules(args)
        for rule in rules:
            if rule.accounts and not set(rule.accounts) & set(accounts):
                logger.warning(f"rule {rule.name!r}: none of its accounts is chosen")
            if why := short_retention(rule):
                logger.warning(why)
            if rule.deprecated:
                logger.warning(rule.deprecated)
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
        # A tried rule is all that is shown, reviewed and planned: the file's are as before.
        shown = results[-1:] if tried else results
        reviews = write_reviews(con, shown, accounts, review_dir()) if args.review else []
    planned = int(time.time() * 1000)
    rows = plan_rows(shown, planned)
    plan = args.plan or (None if tried else str(offload_dir() / "offload-plan.csv"))
    if plan:
        write_plan(rows, Path(plan))
        # Every plan is kept: apply logs name the plan they used by its planned time.
        kept = (
            offload_dir()
            / "plans"
            / f"{time.strftime('%Y-%m-%d-%H%M%S', time.gmtime(planned / 1000))}.csv"
        )
        write_plan(rows, kept)

    tables = summary(shown, args.top)
    source = "" if args.rules == NO_RULES else f" after the rules in {args.rules}"
    sys.stdout.write(
        f"Offload plan — {', '.join(accounts)}\n"
        + (
            f"Generated {datetime.now():%Y-%m-%d %H:%M}: the --try rule{source}\n"
            if tried
            else f"Generated {datetime.now():%Y-%m-%d %H:%M} from {args.rules}\n"
        )
    )
    render(tables, args.top, sys.stdout)
    if tried:
        target = rules_file() if args.rules == NO_RULES else args.rules
        sys.stdout.write(f"\nTo keep it, paste this into {target}:\n\n{rule_toml(tried)}")
    if args.csv:
        paths = write_csv(tables, Path(args.csv), "offload")
        logger.info(f"CSV → {args.csv}/ ({len(paths)} files)")
    for path in reviews:
        logger.info(f"Review → {path}")
    size = sum(r["size"] for r in rows)
    if plan:
        logger.info(
            f"Plan → {plan}: {len(rows):,} messages, {human_size(size)} (kept as {kept}). "
            "Nothing in the mailboxes or the backup was changed."
        )
    else:
        logger.info(
            f"The --try rule would offload {len(rows):,} messages, {human_size(size)}. No plan "
            "written (--plan FILE writes one); nothing in the mailboxes or the backup was changed."
        )


def cmd_suggest(args) -> None:
    """
    Draft rules from the bulk report's candidates and the account's large
    mail, plan them after the rules file's, and print them as TOML; with
    --append, add them to the rules file, commented out.
    """
    folders = resolve_folders(args.accounts)
    if not folders:
        raise AnalyticsError(f"no backups found in {data_root()}")
    before_ms = parse_before(args.before)
    path = Path(args.rules)
    rules = load_rules(path, parse_before, resolve_account) if path.is_file() else []
    if not rules:
        logger.info(f"No rules file at {path}: the suggestions count all the mail.")
    con, accounts = open_sidecars(folders)
    with closing(con):
        for warning in completeness(con, accounts):
            logger.warning(warning)
        sources = suggest.read_sources(con, accounts, args.min_messages)
        large = suggest.read_large(con, accounts, args.larger_than_mb, before_ms)
        drafts = suggest.decide(
            sources, large, args.before, args.larger_than_mb, {r.name for r in rules}
        )
        tried = [parse_rule(d.raw, parse_before, resolve_account) for d in drafts]
        hits = {
            (account, q): ids
            for account, queries in searches(rules, accounts).items()
            for q, ids in search_gmail(account, Path(args.config), queries).items()
        }
        results = build_plan(
            con, [*rules, *tried], accounts, {backup_account(f): str(f) for f in folders}, hits
        )[len(rules) :]
    blocks = []
    for draft, r in zip(drafts, results, strict=True):
        if not r.selected:
            logger.info(f"Not suggested: {draft.name!r} would take nothing the rules don't.")
            continue
        takes = (
            f"{len(r.selected):,} messages, {human_size(sum(m.size for m in r.selected))}, "
            "after the protections and the rules above it"
        )
        blocks.append(suggest.render(draft, takes))
    if not blocks:
        print("Nothing to suggest: the rules file already takes what these would.")
        return
    text = (
        f"# Suggested by 'mailcellar offload suggest' on {datetime.now():%Y-%m-%d}. Plan "
        "with --review before you label anything:\n# every rule still goes through label, "
        "review in Gmail and trash.\n\n" + "\n".join(blocks)
    )
    print(text, end="")
    if args.append:
        commented = "".join(f"# {line}\n" if line else "#\n" for line in text.splitlines())
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(
                f"\n# ── offload suggest, {datetime.now():%Y-%m-%d}: uncomment a rule "
                "to use it ──\n" + commented
            )
        logger.info(f"Appended {len(blocks)} suggestions to {path}, commented out.")


def select_rows(
    rows: Sequence[Mapping], rules: Collection[str], accounts: Collection[str], limit: int | None
) -> tuple[Mapping, ...]:
    """
    The plan rows of the given rules and accounts (any of each; none means
    all), the first `limit` of them if given. Pure.
    """
    chosen = tuple(
        r
        for r in rows
        if (not rules or r["rule"] in rules) and (not accounts or r["account"] in accounts)
    )
    return chosen if limit is None else chosen[:limit]


def cmd_apply(args) -> None:
    mode = "label" if args.label is not None else "trash" if args.trash is not None else "dry-run"
    label = args.label or args.trash or OFFLOAD_LABEL
    rows = read_plan(Path(args.plan))
    plan_folders = {a: backup_folder(a) for a in {r["account"] for r in rows}}
    before = len(rows)
    rows = remaining(rows, mode, label, plan_folders, Path(args.log))
    if len(rows) < before:
        why = ["offloaded", "restored since the plan"]
        why += {
            "label": ["labelled before"],
            "trash": ["not labelled by a label run", "vetoed before"],
        }.get(mode, [])
        logger.info(
            f"{before - len(rows):,} plan rows left out, not read: "
            f"{', '.join(why[:-1])}, or {why[-1]}."
        )
    chosen = {resolve_account(a) for a in args.accounts or ()}
    rows = select_rows(rows, set(args.rule or ()), chosen, args.limit)
    if not rows:
        sys.exit("Nothing to do: no plan rows match.")
    # Messages this tool labelled carry the label in a plan made after the next
    # backup; only a label already there for other reasons makes it unusable.
    labelled = log_state(Path(args.log), label).labelled
    problem = unusable_label(
        [r for r in rows if (r["account"], r["gmail_id"]) not in labelled], label
    )
    if problem:
        sys.exit(f"error: can't use {label!r} as the offload label: {problem}.")

    accounts = sorted({r["account"] for r in rows})
    folders = {a: backup_folder(a) for a in accounts}
    backups = last_backups(folders, accounts)
    stale, behind = backup_problems(backups, planned_ms(rows), mode)
    for account, why in stale.items():
        logger.warning(f"{account}: {why}.")
    if behind:
        logger.info(
            f"Before --trash, run a backup of {', '.join(behind)}: trashing needs one newer "
            "than the plan."
        )
    if mode == "trash":
        if stale:
            sys.exit(
                "error: run a backup of each account above before trashing, so the backup "
                "is known to be complete."
            )
        print(
            "Trashed messages leave the server for good when its Trash is emptied (Gmail: "
            "after 30 days; an IMAP server: whenever it, or a mail client, empties it). "
            f"The backup in {data_root()} then holds the only copy, unless it is copied "
            "elsewhere:"
        )
        # Every last backup is good here, or trashing has stopped above.
        newest = max(run[0] for run in backups.values())
        for line in second_copy_report(data_root(), newest):
            print(f"  {line}")

    from mailcellar import gmailapi, mailbox

    gmail_for = mailbox.opener(Path(args.config), folders)

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
    trashed = [e for e in log if e.outcome in ("trashed", "recorded")]
    if trashed:
        providers = providers_of({e.row["account"]: folders[e.row["account"]] for e in trashed})
        print(
            f"Recorded {len(trashed):,} offloaded messages (batch {batch}); 'backup --prune' "
            f"keeps them in the backup. {space_freed(set(providers.values()))}"
        )
        print(f"Manifest → {manifest_path(offload_dir(), batch)}")


def confirm_count(n: int, what: str) -> bool:
    """Ask for the count to be typed: for the steps that can't be undone by a click."""
    answer = input(f"{what} Type {n} to go ahead: ").strip().replace(",", "")
    return answer == str(n)


def providers_of(folders: dict[str, Path]) -> dict[str, str]:
    """Each account's provider, from its backup; "" where its sidecar can't be read."""
    out = {}
    for account, folder in folders.items():
        try:
            out[account] = sidecar.provider(folder)
        except sidecar.MetaError:
            out[account] = ""  # not known: worded for either
    return out


def space_freed(providers: set[str]) -> str:
    """When trashed mail's space comes back, for the providers trashed in. Pure."""
    if providers == {"gmail"}:
        return "Gmail frees their space when it empties its Trash, after 30 days."
    if providers == {"imap"}:
        return "The server frees their space when its Trash is emptied."
    return "Their space is freed when each mailbox's Trash is emptied (Gmail's after 30 days)."


def gmail_state(account: str, config: Path, ids: list[str]):
    """The mailbox's labels for each ID (None: gone), and the lookups that failed. Read-only."""
    from mailcellar import mailbox
    from mailcellar.progress import Progress

    failures: list[str] = []
    with closing(mailbox.opener(config)(account, "readonly")) as box:
        with Progress(f"{account}: asking {mailbox_name(box.provider)}", len(ids)):
            live = box.message_labels(ids, failures)
    return live, dict(f.split(": ", 1) for f in failures)


def forget_rows(choice, provider: str) -> list[str]:
    """
    forget's count of what it can and can't forget, a row per kind that has
    any; the mailbox named by `provider` (mailbox_name). Pure.
    """
    where, start = mailbox_name(provider), mailbox_name(provider, start=True)
    held = ", ".join(sorted({name for _c, name in choice.held}))
    rows = [  # (label, messages, note, shown when there are none)
        ("can be forgotten", choice.forget, "", True),
        (
            f"not yet gone from {where}",
            choice.not_gone,
            f"in its Trash, or not seen gone: run a backup after {where} empties its Trash",
            False,
        ),
        (
            f"{start} has them again",
            choice.in_gmail,
            "restored, or moved back from Trash; left alone",
            False,
        ),
        ("held", choice.held, f"hold {held}; left as they are", False),
        (
            "couldn't be checked",
            choice.unchecked,
            f"asking {where}, e.g. {choice.unchecked[0][1]}; tried again next run"
            if choice.unchecked
            else "",
            False,
        ),
    ]
    return [
        f"  {label:<32}{len(items):>8,}" + (f"  ({note})" if note else "")
        for label, items, note, always in rows
        if items or always
    ]


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
        try:
            provider = sidecar.provider(folder)
        except sidecar.MetaError as exc:
            sys.exit(f"error: {exc}")
        live, unread = gmail_state(account, Path(args.config), gone) if gone else ({}, {})
        choice = forget.choose(found, live, unread, held)
        if retain is None:
            print(f"Forget — {account}: {len(found):,} offloaded messages selected")
        else:
            print(f"Forget — {account}: {len(found):,} offloaded messages due under retention")
            for rule, year, n in forget.by_rule_and_year(found):
                print(f"  {rule} ({years_text(retain[rule].forget_after)}), {year}  {n:>8,}")
        for row in forget_rows(choice, provider):
            print(row)
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
        rules_file(),
        here / "offload-plan.csv",
        here / "offload-apply-log.csv",
    )
    p = argparse.ArgumentParser(
        prog=prog or "mailcellar offload",
        description="Take old mail out of its mailbox, Gmail or IMAP, keeping it in the backup.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    pl = sub.add_parser("plan", help="Select the messages the rules would offload (read-only)")
    pl.add_argument(
        "accounts",
        nargs="*",
        help="Accounts (address, config folder name or unique part of an "
        "address); default: every backup in the data root",
    )
    pl.add_argument(
        "--rules",
        default=str(rules),
        help=f"Rules file, or 'none' with --try (default: {rules})",
    )
    pl.add_argument(
        "--plan",
        default=None,
        help=f"Plan CSV to write (default: {plan}; with --try, none unless given)",
    )
    pl.add_argument(
        "--try",
        dest="try_rule",
        metavar="RULE",
        help="Plan one more rule, written as the inside of a TOML inline table "
        "('bulk = true, before = \"2017-01-01\"'), after the file's rules: shows only "
        "what it would take, writes no plan unless --plan is given, and ends with the "
        "rule as a [[rule]] table to paste into the rules file",
    )
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

    sg = sub.add_parser(
        "suggest",
        help="Draft rules from the bulk report and the large mail, planned and printed as TOML "
        "(read-only unless --append)",
    )
    sg.add_argument(
        "accounts",
        nargs="*",
        help="Accounts (address, config folder name or unique part of an "
        "address); default: every backup in the data root",
    )
    sg.add_argument(
        "--rules",
        default=str(rules),
        help=f"Rules file: suggestions count what its rules leave (default: {rules})",
    )
    sg.add_argument(
        "--before",
        default="1",
        help="Each suggestion's before: a date or a number of years ago (default: 1)",
    )
    sg.add_argument(
        "--min-messages",
        type=int,
        default=10,
        help="Suggest a source only with at least this many messages (default: 10)",
    )
    sg.add_argument(
        "--larger-than-mb",
        type=float,
        default=5,
        help="The large-mail rule's larger_than_mb (default: 5)",
    )
    sg.add_argument(
        "--append",
        action="store_true",
        help="Also add the suggestions to the rules file, commented out under a dated header",
    )
    sg.add_argument(
        "--config",
        default=str(config_root()),
        help="Config folder, for rules with a query: Gmail is searched, read-only "
        f"(default: {config_root()})",
    )
    sg.set_defaults(func=cmd_suggest)

    ap = sub.add_parser(
        "apply", help="Check the plan against the mailbox and the backup; label, then trash"
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
        "it is created if missing. Removing it in the mailbox vetoes a message",
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
        help="Delete offloaded mail its mailbox no longer has from the backup, held "
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
