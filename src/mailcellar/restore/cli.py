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
from itertools import accumulate
from pathlib import Path

from mailcellar.backup import chain, sidecar
from mailcellar.dedup.apply import limit_count
from mailcellar.paths import backup_folder, config_root, data_root
from mailcellar.report.cli import parse_before
from mailcellar.report.db import AnalyticsError
from mailcellar.report.output import human_size
from mailcellar.restore import sources
from mailcellar.restore.apply import (
    CHANGES,
    DONE,
    DRY_RUN,
    IMPORT,
    IMPORTS_PER_SECOND,
    PUT_BACK,
    Narrowing,
    RestoreLog,
    apply,
    candidates,
    dry_run,
    forgotten_in,
    matches,
    plan,
    restored_into,
    restored_label,
    within_limit,
)

logger = logging.getLogger(__name__)


def add_selection_arguments(
    p: argparse.ArgumentParser,
    all_help: str = "Every message in the backup; those still in the mailbox are left alone",
) -> None:
    """The options that say which backed-up messages: shared by restore and export."""
    which = p.add_argument_group("which messages (one or more)")
    which.add_argument("--batch", action="append", default=[], help="An offload batch (repeatable)")
    which.add_argument("--rule", action="append", default=[], help="An offload rule (repeatable)")
    which.add_argument(
        "--id",
        action="append",
        default=[],
        help="A message's ID in the backup, offloaded or not (repeatable); with --offloaded, "
        "only if offloaded",
    )
    which.add_argument("--offloaded", action="store_true", help="Every offloaded message")
    which.add_argument("--all", action="store_true", help=all_help)
    narrow = p.add_argument_group(
        "narrow the selection (combine with any of the above; all must hold)"
    )
    narrow.add_argument(
        "--before",
        metavar="DATE",
        help="Only mail older than this: YYYY-MM-DD (UTC), or a time ago (90d, 3w, 6m, 2y)",
    )
    narrow.add_argument(
        "--after",
        metavar="DATE",
        help="Only mail from this date on: YYYY-MM-DD (UTC), or a time ago (90d, 3w, 6m, 2y)",
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
            return parse_before(value, option)
        except AnalyticsError as exc:
            p.error(str(exc))

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


def index_problem(account: str, folder: Path, provider: str) -> str:
    """
    Why mail restored into `account` can't be indexed in its backup `folder`,
    or "": a backup it had that isn't there (a volume not mounted), as backup
    refuses it, a chain that isn't the one its anchor recorded, which the
    index's link would be refused on after the upload, or one holding the
    other provider's IDs, which the new ones would mix with. A first backup
    of the account starts with what is restored.
    """
    import sqlite3

    from mailcellar.backup import gyb
    from mailcellar.backup.commands import provider_change, why_missing

    if problem := why_missing(account, folder) or chain.why_unanchored(folder, account):
        return problem
    if not (folder / gyb.DB_NAME).is_file():
        return ""
    con = sqlite3.connect(f"file:{folder / gyb.DB_NAME}?mode=ro", uri=True)
    try:
        messages = con.execute("SELECT count(*) FROM uids").fetchone()[0]
    finally:
        con.close()
    try:
        held = sidecar.provider(folder)
    except sidecar.MetaError as exc:
        return str(exc)
    return provider_change(account, folder, held, provider, messages)


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


def into_problem(name: str) -> str:
    """Why --into can't name this folder, or "": it must be one restore can file mail in. Pure."""
    from mailcellar import imap

    if imap.place_label(name, {}, {}, "/").kind != "folder":
        return (
            f"--into {name}: not a folder restore can put mail in "
            "(restored/…, Trash, Spam and Gmail's own labels aren't)"
        )
    return ""


def restored_there(folder: Path, target: str = "") -> frozenset[str]:
    """
    The IDs, in `target` ('' = its own account), of mail a restore from the
    backup in `folder` put where it is (PUT_BACK): for --into, mail restore
    may gather. Empty for a backup not made yet; exits if its sidecar can't
    be read.
    """
    if not sidecar.meta_path(folder).is_file():
        return frozenset()
    try:
        return frozenset(restored_into(folder, target, PUT_BACK).values())
    except sidecar.MetaError as exc:
        sys.exit(f"error: {exc}")


def gmail_into(provider: str, into: str) -> str:
    """Why --into doesn't apply to the account restored into, or "". Pure."""
    if into and provider == "gmail":
        return (
            "--into is for an IMAP account; into a Gmail account, "
            "--strip-labels --label NAME gives every message that one label"
        )
    return ""


# Google's daily limit on uploads over IMAP: its figure for Workspace; consumer Gmail's isn't given
GMAIL_IMAP_UPLOADS = 500_000_000


def upload_limit_note(decisions, limit: int) -> str:
    """
    What to say where `decisions` would upload more than `limit` bytes, Gmail's
    daily limit over IMAP, or "": how many to take a day with --limit, which
    counts the changes oldest first (within_limit), each run carrying on
    where the last stopped. Pure.
    """
    uploaded = accumulate(d.size if d.way == IMPORT else 0 for d in decisions)
    past = next((n for n, total in enumerate(uploaded) if total > limit), None)
    if past is None:
        return ""
    changes = sum(d.way in CHANGES for d in decisions[:past])  # what --limit counts
    return (
        f"Gmail allows about {limit // 1_000_000:,} MB of uploads over IMAP a day (Google's "
        "figure for Workspace) and may slow down or refuse sign-in for up to a day past it: "
        f"restore this over several days, with --limit {max(changes, 1)} a day; each run carries "
        "on where the last stopped."
    )


def gmail_limit_note(box, decisions) -> str:
    """
    upload_limit_note() where `box` is Gmail over IMAP (X-GM-EXT-1), else "".
    The server is asked only when the uploads would pass the limit.
    """
    from mailcellar import imap

    note = upload_limit_note(decisions, GMAIL_IMAP_UPLOADS) if box.provider == "imap" else ""
    return note if note and imap.GMAIL_EXT in box.server_info().capabilities else ""


def main(argv=None, prog: str | None = None) -> int:
    log = restore_dir() / "restore-log.csv"
    p = argparse.ArgumentParser(
        prog=prog or "mailcellar restore",
        description="Put backed-up mail back into its mailbox, Gmail or IMAP: offloaded "
        "messages, or a whole backup, into its own account or another one. A dry run unless "
        "--yes: it checks the backup and the mailbox and says what it would do. (--has-label "
        "narrows by a label the mail has; --label names the label restored mail gets.)",
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
    p.add_argument(
        "--limit", type=limit_count, default=None, help="Only the first N to restore, oldest first"
    )
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
    labels.add_argument(
        "--into",
        default="",
        metavar="FOLDER",
        help="IMAP: put everything restored into this one folder (created if missing), mail "
        "restored before included, whatever its labels",
    )
    p.add_argument("--yes", action="store_true", help="Restore; without it, a dry run")
    p.add_argument(
        "--config",
        default=str(config_root()),
        help=f"Config folder (default: {config_root()})",
    )
    p.add_argument("--log", default=str(log), help=f"Restore log, appended to (default: {log})")
    args = p.parse_args(argv)
    source = args.from_mbox or args.from_folder
    if source and (args.batch or args.rule or args.offloaded or args.all):
        p.error(
            "--from-mbox and --from-folder take every message they hold; "
            "use --id, --before, --after, --has-label or --sender to narrow it"
        )
    if source and args.to:
        p.error("with --from-mbox or --from-folder, name the account to restore into as ACCOUNT")
    narrowing = selection(p, args, required=not source)
    if args.into and (args.label_prefix or args.strip_labels):
        p.error("--into puts mail in one folder: leave out --label-prefix and --strip-labels")
    if args.into and (problem := into_problem(args.into)):
        p.error(problem)
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
    if problem := gmail_into(provider_of(target_config), args.into):
        sys.exit(f"error: {problem}")
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
            include_restored=True,  # plan() leaves those the mailbox still has
        )
        restored = restored_into(folder, target if other else "")
    except sidecar.MetaError as exc:
        sys.exit(f"error: {exc}")
    gone_for_good = forgotten_in(folder, set(args.batch), set(args.rule), set(args.id))
    if gone_for_good and (args.batch or args.rule or args.id):
        logger.warning(
            f"{gone_for_good:,} selected messages were deleted from the backup by "
            "'mailcellar forget' and can't be restored from it."
        )
    if not cands:
        sys.exit("Nothing to do: no matching messages left to restore.")

    mode = "restore" if args.yes else "dry-run"
    progress = logger.info
    target_folder = backup_folder(target) if other else None
    if other and (problem := index_problem(target, target_folder, provider_of(target_config))):
        sys.exit(f"error: {problem}")
    if not other and (problem := chain.why_unanchored(folder, account)):
        sys.exit(f"error: {problem}")  # what is uploaded is indexed here
    try:
        with closing(
            mailbox.open_mailbox(
                target,
                target_config,
                "append" if args.yes else "readonly",  # untrashing needs MOVE: can_move()
                folder=backup_folder(target),  # an IMAP account's map, and where mail lived
            )
        ) as gmail:
            decisions = plan(
                gmail,
                cands,
                same_account=not other,
                progress=progress,
                restored=restored,
                into=args.into,
                recorded=(
                    restored_there(backup_folder(target))
                    | restored_there(folder, target if other else "")
                    if args.into
                    else frozenset()
                ),
                backup=backup_folder(target),
            )
            decisions = within_limit(decisions, args.limit)
            note = gmail_limit_note(gmail, decisions)
            with RestoreLog(Path(args.log), mode, account, target if other else "") as log:
                if args.yes:
                    if note:
                        logger.warning(note)
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
                        into=args.into,
                        progress=progress,
                        on_result=log.add,
                    )
                else:
                    results = dry_run(decisions)
                    for r in results:
                        log.add(r)
    except gmailapi.GmailError as exc:
        sys.exit(f"error: {exc} (the log has what was done: {args.log})")

    report(
        results,
        mode,
        account,
        target,
        label=args.label or restored_label(),
        provider=provider_of(target_config),
        note=note,
    )
    print(f"Log → {args.log}")
    return 1 if any(r.outcome in ("failed", "skipped") for r in results) else 0


def restore_from_elsewhere(p: argparse.ArgumentParser, args, narrowing: Narrowing, log) -> int:
    """restore --from-mbox or --from-folder: the same staging, another source."""
    from mailcellar import gmailapi, mailbox

    config = Path(args.config)
    try:
        account, config_folder = gmailapi.resolve_account(config, args.account)
        target, target_config = account, config_folder
        if args.to:
            target, target_config = gmailapi.resolve_account(config, args.to)
    except gmailapi.GmailError as exc:
        sys.exit(f"error: {exc}")
    if problem := gmail_into(provider_of(target_config), args.into):
        sys.exit(f"error: {problem}")
    try:
        if args.from_mbox:
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
    if not cands:
        sys.exit("Nothing to do: no matching messages.")

    target_folder = backup_folder(target)  # where what is uploaded is indexed
    if problem := index_problem(target, target_folder, provider_of(target_config)):
        sys.exit(f"error: {problem}")
    mode = "restore" if args.yes else "dry-run"
    progress = logger.info
    try:
        with closing(
            mailbox.open_mailbox(
                target,
                target_config,
                "append" if args.yes else "readonly",  # untrashing needs MOVE: can_move()
                folder=target_folder,  # an IMAP account's map, and where mail lived
            )
        ) as gmail:
            decisions = plan(
                gmail,
                cands,
                same_account=False,
                progress=progress,
                into=args.into,
                recorded=restored_there(target_folder) if args.into else frozenset(),
                backup=target_folder,
            )
            decisions = within_limit(decisions, args.limit)
            note = gmail_limit_note(gmail, decisions)
            target_name = target if target != account else ""
            with RestoreLog(Path(args.log), mode, account, target_name) as log:
                if args.yes:
                    if note:
                        logger.warning(note)
                    run = datetime.now().strftime("%Y-%m-%d-%H%M%S")
                    results = apply(
                        gmail,
                        None,
                        decisions,
                        run,
                        args.label or restored_label(),
                        target=target,
                        target_folder=target_folder,
                        prefix=args.label_prefix,
                        strip=args.strip_labels,
                        into=args.into,
                        progress=progress,
                        on_result=log.add,
                    )
                else:
                    results = dry_run(decisions)
                    for r in results:
                        log.add(r)
    except gmailapi.GmailError as exc:
        sys.exit(f"error: {exc} (the log has what was done: {args.log})")

    report(
        results,
        mode,
        account,
        target,
        where,
        label=args.label or restored_label(),
        provider=provider_of(target_config),
        note=note,
    )
    print(f"Log → {args.log}")
    return 1 if any(r.outcome in ("failed", "skipped") for r in results) else 0


def restored_line(done: int, at: str, label: str, provider: str) -> str:
    """
    restore's closing line: the label restored mail carries. On an IMAP server
    it is a keyword, only where the server keeps them (restore says when it
    doesn't), so the line says so rather than claim it. Pure.
    """
    if provider == "gmail":
        return f"Restored {done:,} at {at}; they carry the label {label!r}."
    return (
        f"Restored {done:,} at {at}; where the server keeps keywords, they carry "
        f"{label!r} as a $mailcellar- keyword."
    )


def report(
    results,
    mode: str,
    account: str,
    target: str,
    source: str = "",
    label: str = "",
    *,
    provider: str,
    note: str = "",
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
        size = human_size(sum(d.size for d in uploads))
        # How fast a server takes APPENDs is its own; only the Gmail API publishes a rate
        minutes = len(uploads) / IMPORTS_PER_SECOND / 60
        print(
            f"Would upload {len(uploads):,} messages, {size}: about {max(1, round(minutes)):,} "
            f"min at {IMPORTS_PER_SECOND} a second, Google's limit (more for large messages)."
            if provider == "gmail"
            else f"Would upload {len(uploads):,} messages, {size}."
        )
    if mode == "dry-run":
        if note:
            print(note)
        print("Nothing in the mailbox or the backup was changed. Run again with --yes to restore.")
    else:
        done = sum(counts[o] for o in DONE)
        if done:
            print(restored_line(done, time.strftime("%H:%M"), label or "restored/", provider))
