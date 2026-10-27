"""
Command line for mailcellar restore: selection, the dry run, progress
lines, and the summary.
"""

import argparse
import logging
import sys
import time
from collections import Counter
from contextlib import closing
from datetime import datetime
from pathlib import Path

from mailcellar.backup import sidecar
from mailcellar.paths import backup_folder, config_root, data_root
from mailcellar.report.cli import parse_before
from mailcellar.report.db import AnalyticsError
from mailcellar.report.output import human_size
from mailcellar.restore import sources
from mailcellar.restore.apply import (
    DONE,
    DRY_RUN,
    IMPORT,
    IMPORTS_PER_SECOND,
    Narrowing,
    RestoreLog,
    apply,
    candidates,
    dry_run,
    forgotten_in,
    matches,
    plan,
    restored_label,
)

logger = logging.getLogger(__name__)


def add_selection_arguments(
    p: argparse.ArgumentParser,
    all_help: str = "Every message in the backup; those still in Gmail are left alone",
) -> None:
    """The options that say which backed-up messages: shared by restore and export."""
    which = p.add_argument_group("which messages (one or more)")
    which.add_argument("--batch", action="append", default=[], help="An offload batch (repeatable)")
    which.add_argument("--rule", action="append", default=[], help="An offload rule (repeatable)")
    which.add_argument(
        "--id",
        action="append",
        default=[],
        help="A Gmail ID, offloaded or not (repeatable); with --offloaded, only if offloaded",
    )
    which.add_argument("--offloaded", action="store_true", help="Every offloaded message")
    which.add_argument("--all", action="store_true", help=all_help)
    narrow = p.add_argument_group(
        "narrow the selection (combine with any of the above; all must hold)"
    )
    narrow.add_argument(
        "--before",
        metavar="DATE",
        help="Only mail older than this: YYYY-MM-DD (UTC), or a number of years ago",
    )
    narrow.add_argument(
        "--after",
        metavar="DATE",
        help="Only mail from this date on: YYYY-MM-DD (UTC), or a number of years ago",
    )
    narrow.add_argument(
        "--has-label",
        action="append",
        default=[],
        metavar="NAME",
        help="Only mail with this user label, ignoring case (repeatable: any of them)",
    )
    narrow.add_argument(
        "--sender",
        action="append",
        default=[],
        metavar="ADDRESS|@DOMAIN",
        help="Only mail from this From address, or any address at @domain (repeatable: "
        "any of them)",
    )


def selection(p: argparse.ArgumentParser, args, required: bool = True) -> Narrowing:
    """
    Check a selection given with add_selection_arguments() and return its
    narrowing; an error ends the command through the parser. Not
    `required` when the messages come from a source that is selected whole.
    """
    if required and not (args.batch or args.rule or args.id or args.offloaded or args.all):
        p.error("say which messages: --batch, --rule, --id, --offloaded or --all")
    if args.all and (args.batch or args.rule or args.offloaded):
        p.error(
            "--all takes every message; use --id, --before, --after, --has-label or "
            "--sender to narrow it"
        )

    def date(option: str, value: str | None) -> int | None:
        try:
            return parse_before(value)
        except AnalyticsError:
            p.error(f"{option} {value!r}: give a date (YYYY-MM-DD) or a number of years")

    narrowing = Narrowing(
        date("--before", args.before),
        date("--after", args.after),
        tuple(args.has_label),
        tuple(args.sender),
    )
    if (
        narrowing.before_ms is not None
        and narrowing.after_ms is not None
        and narrowing.after_ms >= narrowing.before_ms
    ):
        p.error("--after must be earlier than --before")
    return narrowing


def restore_dir() -> Path:
    """restore/ in the data root (paths.data_root())."""
    return data_root() / "restore"


def source_provider(folder: Path) -> str:
    """
    Which provider's IDs `folder`'s backup holds; exits if its sidecar can't be
    read, so nothing is restored on a guess. Mail goes between Gmail and IMAP
    accounts either way, labels and folders mapped by the target.
    """
    try:
        return sidecar.provider(folder)
    except sidecar.MetaError as exc:
        sys.exit(f"error: {exc}")


def provider_of(config_folder: Path) -> str:
    """The provider an account is set up with: imap where it has imap.toml."""
    from mailcellar import gmailapi

    return "imap" if gmailapi.is_imap(config_folder) else "gmail"


def same_account_problem(source: str, target: str) -> str:
    """
    Why a backup can't go back into its own account, or "": one set up since
    with the other provider holds none of the backup's IDs, which a restore
    into its own account takes as its own. Pure.
    """
    if source == target:
        return ""
    return (
        f"the backup holds {source} IDs but the account is now set up as {target}; "
        "set it up as it was to restore into it, or restore into another account with --to"
    )


def main(argv=None, prog: str | None = None) -> int:
    log = restore_dir() / "restore-log.csv"
    p = argparse.ArgumentParser(
        prog=prog or "mailcellar restore",
        description="Put backed-up mail back into Gmail: offloaded messages, or a whole "
        "backup, into its own account or another one. A dry run unless --yes: it checks "
        "the backup and Gmail and says what it would do. (--has-label narrows by a label "
        "the mail has; --label names the label restored mail gets.)",
    )
    p.add_argument(
        "account",
        help="The backup: address, config folder name or unique part. With --from-mbox or "
        "--from-folder, the account restored into",
    )
    add_selection_arguments(p)
    elsewhere = p.add_argument_group(
        "restore from elsewhere (instead of the backup; every message, which --id and the "
        "narrowing options can narrow)"
    ).add_mutually_exclusive_group()
    elsewhere.add_argument(
        "--from-pruned",
        metavar="BATCH",
        help="Mail backup --prune moved out of the backup, in <data root>/_pruned/<email>/BATCH/",
    )
    elsewhere.add_argument(
        "--from-mbox",
        metavar="FILE",
        help="An mbox file: a Google Takeout export, or one written by mailcellar export",
    )
    elsewhere.add_argument(
        "--from-folder",
        metavar="DIR",
        help="A gyb backup folder from elsewhere, read as it is",
    )
    p.add_argument(
        "--to",
        default=None,
        metavar="ACCOUNT",
        help="Restore into this configured account instead (its backup gets the new IDs)",
    )
    p.add_argument("--limit", type=int, default=None, help="Only the first N (oldest first)")
    labels = p.add_argument_group("labels on imported messages")
    labels.add_argument(
        "--label",
        default=None,
        metavar="NAME",
        help="Label for restored messages (default: restored/<today>)",
    )
    labels.add_argument(
        "--label-prefix",
        default="",
        metavar="P",
        help="Nest the backed-up user labels under P (e.g. the old account's name)",
    )
    labels.add_argument(
        "--strip-labels",
        action="store_true",
        help="Restore no backed-up labels, only the restored/ label (messages are archived)",
    )
    p.add_argument("--yes", action="store_true", help="Restore; without it, a dry run")
    p.add_argument(
        "--config",
        default=str(config_root()),
        help=f"Config folder (default: {config_root()})",
    )
    p.add_argument("--log", default=str(log), help=f"Restore log, appended to (default: {log})")
    args = p.parse_args(argv)
    source = args.from_pruned or args.from_mbox or args.from_folder
    if source and (args.batch or args.rule or args.offloaded or args.all):
        p.error(
            "--from-pruned, --from-mbox and --from-folder take every message they hold; "
            "use --id, --before, --after, --has-label or --sender to narrow it"
        )
    if (args.from_mbox or args.from_folder) and args.to:
        p.error("with --from-mbox or --from-folder, name the account to restore into as ACCOUNT")
    narrowing = selection(p, args, required=not source)
    if source:
        return restore_from_elsewhere(p, args, narrowing, log)

    from mailcellar import gmailapi, mailbox

    config = Path(args.config)
    try:
        account, config_folder = gmailapi.resolve_account(config, args.account)
        target, target_config = account, config_folder
        if args.to:
            target, target_config = gmailapi.resolve_account(config, args.to)
    except gmailapi.GmailError as exc:
        sys.exit(f"error: {exc}")
    other = target != account
    folder = backup_folder(account)
    if not sidecar.meta_path(folder).is_file():
        sys.exit(f"error: {folder} has no meta.sqlite; run mailcellar meta")
    source = source_provider(folder)
    if not other and (problem := same_account_problem(source, provider_of(target_config))):
        sys.exit(f"error: {problem}")
    try:
        cands = candidates(
            folder,
            set(args.batch),
            set(args.rule),
            set(args.id),
            everything=args.all,
            target=target if other else "",
            narrow=narrowing,
            offloaded_only=args.offloaded,
        )
    except sidecar.MetaError as exc:
        sys.exit(f"error: {exc}")
    gone_for_good = forgotten_in(folder, set(args.batch), set(args.rule), set(args.id))
    if gone_for_good and (args.batch or args.rule or args.id):
        logger.warning(
            f"{gone_for_good:,} selected messages were deleted from the backup by "
            "'offload forget' and can't be restored from it."
        )
    if args.limit is not None:
        cands = cands[: args.limit]
    if not cands:
        sys.exit("Nothing to do: no matching messages left to restore.")

    mode = "restore" if args.yes else "dry-run"
    progress = logger.info
    target_folder = backup_folder(target) if other else None
    if target_folder is not None:
        from mailcellar.backup.commands import why_missing

        # A first backup of the other account starts with what is restored; but one it
        # had that isn't there (a volume not mounted) is refused, as backup refuses it.
        if problem := why_missing(target, target_folder):
            sys.exit(f"error: {problem}")
    try:
        with closing(
            mailbox.open_mailbox(
                target,
                target_config,
                "modify" if args.yes else "readonly",
                folder=backup_folder(target),  # an IMAP account's map, and where mail lived
            )
        ) as gmail:
            decisions = plan(gmail, cands, same_account=not other, progress=progress)
            with RestoreLog(Path(args.log), mode, account, target if other else "") as log:
                if args.yes:
                    run = datetime.now().strftime("%Y-%m-%d-%H%M%S")
                    results = apply(
                        gmail,
                        folder,
                        decisions,
                        run,
                        args.label or restored_label(),
                        target=target if other else "",
                        target_folder=target_folder,
                        prefix=args.label_prefix,
                        strip=args.strip_labels,
                        progress=progress,
                        on_result=log.add,
                    )
                else:
                    results = dry_run(decisions)
                    for r in results:
                        log.add(r)
    except gmailapi.GmailError as exc:
        sys.exit(f"error: {exc} (the log has what was done: {args.log})")

    report(results, mode, account, target, label=args.label or restored_label())
    print(f"Log → {args.log}")
    return 1 if any(r.outcome in ("failed", "skipped") for r in results) else 0


def restore_from_elsewhere(p: argparse.ArgumentParser, args, narrowing: Narrowing, log) -> int:
    """restore --from-pruned, --from-mbox or --from-folder: the same staging, another source."""
    from mailcellar import gmailapi, mailbox

    config = Path(args.config)
    try:
        account, config_folder = gmailapi.resolve_account(config, args.account)
        target, target_config = account, config_folder
        if args.to:
            target, target_config = gmailapi.resolve_account(config, args.to)
    except gmailapi.GmailError as exc:
        sys.exit(f"error: {exc}")
    try:
        if args.from_pruned:
            where = f"pruned batch {args.from_pruned}"
            source_provider(backup_folder(account))
            cands = sources.from_pruned(backup_folder(account), args.from_pruned)
        elif args.from_mbox:
            where = args.from_mbox
            cands = sources.from_mbox(Path(args.from_mbox).expanduser())
        else:
            where = args.from_folder
            folder = Path(args.from_folder).expanduser()
            if folder.resolve() == backup_folder(target).resolve():
                p.error(
                    "--from-folder is the backup of the account restored into; select its "
                    "mail with --offloaded, --batch, --rule, --id or --all instead"
                )
            source_provider(folder)
            cands = sources.from_folder(folder)
    except sources.SourceError as exc:
        sys.exit(f"error: {exc}")
    cands = sorted(
        (c for c in cands if (not args.id or c.source_id in args.id) and matches(c, narrowing)),
        key=lambda c: (c.internal_date, c.source_id),
    )
    if args.limit is not None:
        cands = cands[: args.limit]
    if not cands:
        sys.exit("Nothing to do: no matching messages.")

    mode = "restore" if args.yes else "dry-run"
    progress = logger.info
    try:
        with closing(
            mailbox.open_mailbox(
                target,
                target_config,
                "modify" if args.yes else "readonly",
                folder=backup_folder(target),  # an IMAP account's map, and where mail lived
            )
        ) as gmail:
            decisions = plan(gmail, cands, same_account=False, progress=progress)
            target_name = target if target != account else ""
            with RestoreLog(Path(args.log), mode, account, target_name) as log:
                if args.yes:
                    run = datetime.now().strftime("%Y-%m-%d-%H%M%S")
                    results = apply(
                        gmail,
                        None,
                        decisions,
                        run,
                        args.label or restored_label(),
                        target=target,
                        prefix=args.label_prefix,
                        strip=args.strip_labels,
                        progress=progress,
                        on_result=log.add,
                    )
                else:
                    results = dry_run(decisions)
                    for r in results:
                        log.add(r)
    except gmailapi.GmailError as exc:
        sys.exit(f"error: {exc} (the log has what was done: {args.log})")

    report(results, mode, account, target, where, label=args.label or restored_label())
    print(f"Log → {args.log}")
    return 1 if any(r.outcome in ("failed", "skipped") for r in results) else 0


def report(
    results, mode: str, account: str, target: str, source: str = "", label: str = ""
) -> None:
    counts = Counter(r.outcome for r in results)
    into = f" → {target}" if target != account else ""
    origin = f" from {source}" if source else ""
    print(f"Restore ({mode}) — {account}{into}{origin}: {len(results):,} messages")
    for outcome, n in counts.most_common():
        print(f"  {outcome:<34} {n:>8,}")
    problems = [r for r in results if r.outcome in ("skipped", "failed")]
    for r in problems[:20]:
        print(f"  {r.outcome}: {r.source_id} — {r.detail}")
    if len(problems) > 20:
        print(f"  … and {len(problems) - 20:,} more in the log")
    uploads = [r.decision for r in results if r.outcome == DRY_RUN[IMPORT]]
    if uploads:
        minutes = len(uploads) / IMPORTS_PER_SECOND / 60
        print(
            f"Would upload {len(uploads):,} messages, {human_size(sum(d.size for d in uploads))}: "
            f"about {max(1, round(minutes)):,} min at {IMPORTS_PER_SECOND} a second "
            "(more for large messages)."
        )
    if mode == "dry-run":
        print("Nothing in the mailbox or the backup was changed. Run again with --yes to restore.")
    else:
        done = sum(counts[o] for o in DONE)
        if done:
            print(
                f"Restored {done:,} at {time.strftime('%H:%M')}; they carry the label "
                f"{label or 'restored/'!r} (on an IMAP server, as a $mailcellar- keyword)."
            )
