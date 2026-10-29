"""
Command line for mailcellar forget: delete mail from the backup on purpose,
held HOLD_DAYS first (offload/forget.py does the work). `offload forget` is
the old name: cli.py hands it on here for a release.
"""

import argparse
import logging
import sys
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from mailcellar import gmailapi
from mailcellar.backup import sidecar
from mailcellar.backup.gyb import backup_account
from mailcellar.hold import holds
from mailcellar.mailbox import mailbox_name
from mailcellar.offload import forget
from mailcellar.offload.apply import second_copy_report
from mailcellar.offload.cli import confirm_count, resolve_account, rules_file
from mailcellar.offload.rules import RuleError, load_rules, short_retention, years_text
from mailcellar.paths import config_root, data_root
from mailcellar.report.cli import parse_before, resolve_folders
from mailcellar.report.db import AnalyticsError

logger = logging.getLogger(__name__)


def gmail_state(account: str, config: Path, ids: list[str]):
    """The mailbox's labels for each ID (None: gone), and the lookups that failed. Read-only."""
    from mailcellar import mailbox
    from mailcellar.progress import Progress

    failures: list[str] = []
    with closing(mailbox.opener(config)(account, "readonly")) as box:
        with Progress(f"{account}: asking {mailbox_name(box.provider)}", len(ids)):
            live = box.message_labels(ids, failures)
        skipped = box.unread()
    if skipped:  # an IMAP folder unread: its mail would look gone, so nothing is known
        return forget.presence((), skipped, ids)
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


def listing(account: str, config: Path) -> tuple[frozenset[str], tuple[str, ...], str]:
    """
    (every message the mailbox has, Trash and Spam included; the folders it
    couldn't read, an IMAP server's; its provider), from one sign-in.
    Read-only. Raises GmailError when the mailbox can't be reached.
    """
    from mailcellar import mailbox

    with closing(mailbox.opener(config)(account, "readonly")) as box:
        logger.info(f"{account}: listing {mailbox_name(box.provider)}")
        listed = frozenset(box.list_message_ids(include_spam_trash=True))
        return listed, box.unread(), box.provider


@dataclass(frozen=True)
class DeletedPart:
    """What forget --deleted would take from one backup, or why it may take nothing."""

    chosen: tuple[forget.Offloaded, ...] = ()
    held: tuple[tuple[str, str], ...] = ()  # (ID, the hold keeping it)
    refusal: str = ""  # why none of it may be forgotten; "" if it may


def deleted_part(
    folder: Path, listed: frozenset[str], unread: tuple[str, ...], provider: str, force: bool
) -> DeletedPart:
    """
    forget --deleted's selection for one backup, from its mailbox's listing:
    gone_from, then deleted_decision's guards and the legal holds. Reads the
    backup; a refusal rather than an error, so the run's other parts and
    accounts go on.
    """
    mcon = sidecar.open_meta(folder)
    try:
        kept = sidecar.kept_ids(mcon)  # ever offloaded, restored or not: --batch and --rule's
    finally:
        mcon.close()
    gone = forget.gone_from(forget.index_ids(folder), listed, kept)
    decision = forget.deleted_decision(
        gone,
        unread,
        holds.held(folder, int(time.time() * 1000), list(gone.deleted)),
        force=force,
        where=mailbox_name(provider),
    )
    if decision.refusal:
        return DeletedPart(held=decision.held, refusal=decision.refusal)
    move = list(decision.move)
    return DeletedPart(tuple(forget.deleted(move, forget.details(folder, move))), decision.held)


def deleted_rows(chosen: list, held: tuple) -> list[str]:
    """
    forget --deleted's count, as forget_rows: what can be forgotten, by
    year, and what a hold keeps. Pure.
    """
    years = ", ".join(f"{year} {n:,}" for year, n in forget.by_year(chosen))
    names = ", ".join(sorted({name for _id, name in held}))
    rows = [f"  {'can be forgotten':<32}{len(chosen):>8,}" + (f"  ({years})" if years else "")]
    if held:
        rows.append(f"  {'held':<32}{len(held):>8,}  (hold {names}; left as they are)")
    return rows


def select_offloaded(folder: Path, args, retain) -> list:
    """The offloaded mail --batch, --rule or --due selects. Read-only."""
    if retain is None:
        return forget.candidates(folder, set(args.batch or ()), set(args.rule or ()))
    cutoffs = {name: rule.forget_ms for name, rule in retain.items()}
    return forget.due_by_age(forget.candidates(folder, rules=set(retain)), cutoffs)


def cmd_forget(args) -> int:
    try:
        folders = resolve_folders(args.accounts)
    except AnalyticsError as exc:
        sys.exit(f"error: {exc}")
    if args.force and not args.deleted:
        sys.exit(
            f"error: --force only applies with --deleted (the {forget.MAX_DELETED_SHARE:.0%} guard)"
        )
    if args.undo:
        forget_undo(folders, args.undo)
        return 0
    if args.empty:
        forget_empty(folders, args.yes)
        return 0
    remind_due(folders)
    offloaded = bool(args.batch or args.rule or args.due)
    if not (offloaded or args.deleted):
        sys.exit(
            "error: say which mail: --batch, --rule, --due or --deleted (or --empty, --undo RUN)"
        )
    retain = retention_policy(args) if args.due else None
    run = datetime.now().strftime(forget.RUN_FORMAT)
    status = 0
    for folder in folders:
        account = backup_account(folder)
        sidecar.open_meta(folder).close()  # brings an older meta.sqlite up to date
        try:
            provider = sidecar.provider(folder)
        except sidecar.MetaError as exc:
            sys.exit(f"error: {exc}")
        found = select_offloaded(folder, args, retain) if offloaded else []
        listed = None  # (IDs, unreadable folders), once --deleted has listed the mailbox
        part = None  # the --deleted part, unless not asked for or refused
        if args.deleted:
            try:
                ids, skipped, provider = listing(account, Path(args.config))
                listed = (ids, skipped)
                part = deleted_part(folder, ids, skipped, provider, args.force)
            except gmailapi.GmailError as exc:
                part = DeletedPart(refusal=f"listing {mailbox_name(provider)}: {exc}")
            if part.refusal:
                logger.error(
                    f"{account}: {part.refusal}; "
                    + ("--deleted is skipped" if found else "nothing is forgotten")
                )
                status = 1
                part = None
                if not found:
                    continue
        deleted = list(part.chosen) if part else []
        held = holds.held(folder, int(time.time() * 1000), [c.gmail_id for c in found])
        if not found and not deleted and not (part and part.held):
            if retain is not None:
                print(f"Forget — {account}: nothing due under retention")
            if part is not None:
                print(f"Forget — {account}: nothing gone from {mailbox_name(provider)} to forget")
            continue
        gone = [c.gmail_id for c in found if c.gone_ms is not None and c.gmail_id not in held]
        try:
            if listed is not None:  # the listing answers it: no second sign-in
                live, unread = forget.presence(*listed, gone)
            else:
                live, unread = gmail_state(account, Path(args.config), gone) if gone else ({}, {})
        except gmailapi.GmailError as exc:
            logger.error(f"{account}: asking {mailbox_name(provider)}: {exc}; nothing is forgotten")
            status = 1
            continue
        choice = forget.choose(found, live, unread, held)
        if offloaded and retain is None:
            print(f"Forget — {account}: {len(found):,} offloaded messages selected")
        elif offloaded:
            print(f"Forget — {account}: {len(found):,} offloaded messages due under retention")
            for rule, year, n in forget.by_rule_and_year(found):
                print(f"  {rule} ({years_text(retain[rule].forget_after)}), {year}  {n:>8,}")
        if offloaded:
            for row in forget_rows(choice, provider):
                print(row)
        if part is not None:
            print(
                f"Forget — {account}: {len(deleted) + len(part.held):,} messages gone "
                f"from {mailbox_name(provider)}, never offloaded"
            )
            for row in deleted_rows(deleted, part.held):
                print(row)
        chosen = [*choice.forget, *deleted]
        if not chosen:
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
        if not confirm_count(len(chosen), f"Forget {len(chosen):,} messages?"):
            print("Nothing forgotten.")
            continue
        why = forget.note(
            chosen, None if retain is None else {n: r.forget_after for n, r in retain.items()}
        )
        moved = forget.forget(folder, account, chosen, run, int(time.time() * 1000), why)
        ids = [c.gmail_id for c in chosen]
        indexes = forget.scrub_indexes(account, ids)
        files = forget.scrub_files({(account, g) for g in ids})
        until = forget.held_until(run)
        print(
            f"Forgot {moved:,} messages (run {run}): held in {forget.runs_dir(account) / run} "
            f"until {until:%Y-%m-%d}, then 'mailcellar forget --empty' deletes them for good. "
            f"Taken out of the search index ({indexes['search']:,}) and the dedup catalogue "
            f"({indexes['dedup']:,}); sender and subject blanked in {len(files):,} files"
            + (
                f" and {files['review pages']:,} review pages deleted"
                if "review pages" in files
                else ""
            )
            + "."
        )
    return status


def retention_policy(args) -> dict:
    """
    forget --due: the rules with forget_after (or only --rule's), by
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
                f"({', '.join(due)}); 'mailcellar forget --empty' deletes them for good.\n"
            )


def forget_empty(folders: list[Path], yes: bool) -> None:
    """forget --empty: delete runs past the hold, for good."""
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
    """forget --undo RUN: put a held run back."""
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


def main(argv=None, prog: str | None = None) -> int:
    rules = rules_file()
    fg = argparse.ArgumentParser(
        prog=prog or "mailcellar forget",
        description="Delete mail its mailbox no longer has from the backup, held "
        f"{forget.HOLD_DAYS} days first: offloaded mail (--batch, --rule, --due), mail "
        "deleted in the mailbox (--deleted), or both. A dry run unless --yes.",
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
    fg.add_argument(
        "--deleted",
        action="store_true",
        help="Mail its mailbox has deleted, never offloaded (what backup --prune took): the "
        "mailbox is listed now, read-only",
    )
    fg.add_argument(
        "--force",
        action="store_true",
        help="With --deleted: go ahead although more than "
        f"{forget.MAX_DELETED_SHARE:.0%}% of the backup's messages are deleted in its mailbox",
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

    args = fg.parse_args(argv)
    try:
        return cmd_forget(args)
    except (AnalyticsError, RuleError) as exc:
        sys.exit(f"error: {exc}")
