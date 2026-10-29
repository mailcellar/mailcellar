"""
backup, count and meta: incremental backups of a Gmail or IMAP mailbox in
gyb's format (see gyb.py), one folder per account, <data root>/<email>/. A
gyb folder moved there is continued as it is.

  backup  Download every message not yet in the backup. After the first
          run it reads only what changed since the last one: new mail is
          downloaded and the labels of changed messages are updated. Gmail
          gives its history (kept about 4 weeks); an IMAP account reads
          every folder and compares it with the last run's. Gmail lists
          everything when there is no history to start from, the history
          has expired, or with --full, --search or --refresh-labels
          (which re-syncs the labels of every backed-up message).
          With --prune, for a release, it then runs `mailcellar forget
          --deleted` on the account (forget_cli), passing on --yes and
          --force: mail the mailbox has deleted for good, never offloaded,
          leaves the backup, held 30 days.
  count   Compare the mailbox with the backup: what is missing locally, and
          what the backup holds that the mailbox no longer has, offloaded
          mail apart. Changes nothing.
  meta    Fill meta.sqlite for messages backed up before it existed or
          parsed by an older parser: parse their .eml files, then ask the
          mailbox for thread IDs, label IDs and sizes (not with --offline).
          Also migrates the sidecar to the current schema. backup keeps it
          current for new mail.

Each takes every account in <config>/accounts.toml, or the ones named: a
full address, a config folder name or a unique part of an address. Only
read access to the mailbox is needed.

Usage
  mailcellar backup
  mailcellar backup work --full
  mailcellar backup personal --refresh-labels
  mailcellar backup personal --search "after:2026/10/01"
  mailcellar backup personal --prune
  mailcellar count
  mailcellar meta personal

Exit status is 1 if an account failed, any message could not be fetched,
--prune's forget refused (backup), or the backup is missing messages
(count).
"""

import argparse
import base64
import hashlib
import logging
import sys
import time
from collections.abc import Collection, Mapping
from contextlib import closing, nullcontext
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from mailcellar import gmailapi, mailbox
from mailcellar.backup import chain, gyb, sidecar
from mailcellar.mailbox import mailbox_name
from mailcellar.message import PARSER_VERSION, parse_eml, parse_message
from mailcellar.offload import forget
from mailcellar.paths import backup_folder, config_root
from mailcellar.progress import Progress

logger = logging.getLogger(__name__)

COMMIT_EVERY = 100


def now_ms() -> int:
    return int(time.time() * 1000)


def connect(args) -> tuple[str, Path, mailbox.Mailbox]:
    config_root = Path(args.config)
    account, config_folder = gmailapi.resolve_account(config_root, args.account)
    folder = Path(args.folder) if args.folder else backup_folder(account)
    gmail = mailbox.open_mailbox(
        account,
        config_folder,
        "readonly",
        interactive=not getattr(args, "unattended", False),
        folder=folder,
    )
    return account, folder, gmail


def write_meta(mcon, response: dict, raw: bytes | None = None) -> bool:
    """
    Record one downloaded message in meta.sqlite. Returns False (with a
    warning) if it can't be parsed; index then parses the .eml itself.
    """
    gid = response["id"]
    try:
        fields = parse_message(
            raw if raw is not None else base64.urlsafe_b64decode(response["raw"])
        )
    except Exception as exc:
        logger.warning(f"Cannot parse {gid} for meta.sqlite: {exc}")
        return False
    size = response.get("sizeEstimate")
    sidecar.upsert_message(
        mcon,
        gid,
        thread_id=response.get("threadId", ""),
        internal_date=int(response["internalDate"]),
        size_estimate=int(size) if size is not None else None,
        fields=fields,
        label_ids=response.get("labelIds", []),
    )
    return True


SKIP_LABELS = {"SPAM", "TRASH"}  # excluded from backups, as messages.list does


def apply_labels(con, mcon, gmail, gmail_id: str, label_ids) -> bool:
    """Set a backed-up message's labels in both databases. True if gyb's changed."""
    changed = gyb.replace_labels(con, gmail_id, sorted(gmail.label_names(label_ids)))
    sidecar.set_message_labels(mcon, gmail_id, label_ids)
    return changed


def current_labels(gmail, gmail_ids: list[str], mailbox: int) -> dict[str, list[str] | None]:
    """
    Each message's label IDs now, or None if Gmail no longer has it: by
    listing the mailbox's labels when that is quicker (gmailapi.prefer_listing,
    `mailbox` being the backup's size), else one lookup each.
    """
    if gmailapi.prefer_listing(len(gmail_ids), mailbox):
        listed = gmail.labels_by_listing()
        return {gid: listed.get(gid) for gid in gmail_ids}
    with Progress("Reading labels", len(gmail_ids)):
        details = gmail.message_details(gmail_ids)
    return {gid: None if d is None else d.get("labelIds", []) for gid, d in details.items()}


@dataclass(frozen=True)
class HistoryPlan:
    """What a history-based run does with what changed: decided by sort_changes()."""

    todo: tuple[str, ...] = ()  # IDs to download, in the order looked up
    labels: tuple[tuple[str, tuple[str, ...]], ...] = ()  # (backed-up ID, its label IDs now)
    skipped: int = 0  # new messages in Spam, Trash or Chat: never backed up
    gone: frozenset[str] = frozenset()  # backed-up IDs the mailbox deleted, offloaded apart
    gone_offloaded: frozenset[str] = frozenset()  # offloaded IDs it deleted, as expected


def sort_changes(
    labels_now: Mapping[str, list[str] | None],
    have: Collection[str],
    deleted: Collection[str],
    offloaded: Collection[str],
) -> HistoryPlan:
    """
    Sort the messages a history-based run looked up (`labels_now`: ID → its
    label IDs, None if the mailbox no longer has it): a backed-up one gets
    its labels updated, a new one is downloaded unless it is in Spam, Trash
    or Chat, and a backed-up one the history (`deleted`) or the lookup
    found gone is counted, or, offloaded, marked gone. Pure.
    """
    gone = {g for g in deleted if g in have}
    todo, labels, skipped = [], [], 0
    for gid, label_ids in labels_now.items():
        if label_ids is None:  # deleted since the history was read
            if gid in have:
                gone.add(gid)
        elif gid in have:
            labels.append((gid, tuple(label_ids)))
        elif SKIP_LABELS & set(label_ids) or gyb.is_chat(label_ids):
            skipped += 1
        else:
            todo.append(gid)
    return HistoryPlan(
        tuple(todo),
        tuple(labels),
        skipped,
        frozenset(g for g in gone if g not in offloaded),
        frozenset(g for g in gone if g in offloaded),
    )


def history_plan(
    gmail, con, mcon, have: set[str], indexed: set[str], start: str, stats: dict
) -> tuple[HistoryPlan, str]:
    """
    History-based run: (the plan, historyId to store afterwards). Works from
    each touched message's current state, so it can be repeated. Updates
    labels of backed-up messages and marks offloaded ones gone, counting
    into `stats` (backup's running tally) as it goes, so a run that fails
    part way records what it did. Indexed messages whose .eml has gone
    missing locally are looked up too, so they are re-fetched as a full run
    would. Raises HistoryExpired.
    """
    touched, deleted, new_history_id = gmail.history_changes(start)
    lookup = sorted((touched - deleted) | (indexed - have))
    # Offloaded messages are expected to go once Gmail empties its Trash.
    plan = sort_changes(
        current_labels(gmail, lookup, len(have)), have, deleted, sidecar.kept_ids(mcon)
    )
    stats |= {"skipped": plan.skipped, "deleted_backed_up": len(plan.gone)}
    for gid, ids in plan.labels:
        stats["labels_changed"] += apply_labels(con, mcon, gmail, gid, ids)
    sidecar.mark_gone(mcon, plan.gone_offloaded, now_ms())
    logger.info(
        history_summary(
            changed=len(touched),
            # Without a history (IMAP), how much mail is there outside Junk and Trash, as
            # backup and count say it elsewhere; the scan is already read, so it's free.
            present=None if gmail.provider == "gmail" else len(gmail.list_message_ids()),
            missing=len(indexed - have),
            todo=len(plan.todo),
            labels=stats["labels_changed"],
            skipped=plan.skipped,
        )
    )
    return plan, new_history_id


def history_summary(
    *, changed: int, present: int | None, missing: int, todo: int, labels: int, skipped: int
) -> str:
    """
    What a history-based run found. A mailbox without a history (IMAP)
    reads every message again and compares, so it gives `present`, the mail
    outside Junk and Trash, and the line says that, not that all changed;
    None for Gmail. Pure.
    """
    lead = (
        f"History since last run: {changed:,} messages changed"
        if present is None
        else f"Read the server's {present:,} messages again (excl. junk/trash)"
    )
    return (
        lead
        + (f", {missing:,} missing locally" if missing else "")
        + f"; {todo:,} to download, {labels:,} label updates"
        + (f", {skipped:,} in Spam/Trash/Chat skipped" if skipped else "")
    )


class BackupMissing(Exception):
    pass


def missing_backup(account: str, folder: Path, has_backup: bool, anchor_file: Path | None) -> str:
    """
    Why backing up into `folder` would be wrong, or "": the account has a
    hash chain anchor in the config folder, so it has been backed up
    before, but the folder has no backup. Most likely the data root is on
    a volume that isn't mounted; a backup would start afresh in its place,
    on the wrong disk. Pure.
    """
    if has_backup or anchor_file is None:
        return ""
    return (
        f"no backup in {folder}, though {account} has been backed up before (its hash "
        f"chain anchor is {anchor_file}). If the data root is on a volume that isn't "
        "mounted, mount it and run backup again. To start a new backup there on purpose, "
        "move that anchor file aside first."
    )


def provider_change(account: str, folder: Path, held: str, provider: str, messages: int) -> str:
    """
    Why backing up `provider`'s mail into a backup of `held`'s would be
    wrong, or "": their message IDs differ, so every message would look
    deleted and be downloaded again under another ID. Pure.
    """
    if held == provider or not messages:
        return ""
    return (
        f"{folder} holds a {held} backup of {account}, but the account is now set up as "
        f"{provider}; their message IDs differ, so backing up would mix them. Set the "
        "account up as it was, or move the backup folder aside to start a new one."
    )


def why_missing(account: str, folder: Path) -> str:
    """missing_backup() for this backup folder and the account's anchor, as they are now."""
    anchor = chain.anchor_path(account)
    return missing_backup(
        account,
        folder,
        (folder / gyb.DB_NAME).is_file(),
        anchor if chain.read_anchor(account) is not None else None,
    )


def backup(
    gmail,
    account: str,
    folder: Path,
    query: str | None = None,
    refresh_labels: bool = False,
    full: bool = False,
) -> dict:
    """
    Bring `folder` up to date with the account. Returns counts. New messages
    go into msg-db.sqlite (gyb format) and meta.sqlite (parsed fields).

    Uses Gmail's history since the last run when meta.sqlite has a
    history_id; otherwise (first run, history expired, --search,
    --refresh-labels or full=True) lists every message. Stores the next
    history_id only after a run without download failures. Records the run
    in meta.sqlite's runs table, also when it fails. Raises BackupMissing,
    creating nothing, when the account was backed up before and the
    backup isn't in `folder`.
    """
    problem = why_missing(account, folder)
    if problem:
        raise BackupMissing(problem)
    con = gyb.open_backup(folder, account)
    mcon = sidecar.open_meta(folder)
    started = now_ms()
    # Why every message is listed, when asked for; "" means history if possible.
    if query:
        reason = "search"
    elif refresh_labels:
        reason = "refresh-labels"
    elif full:
        reason = "full"
    else:
        reason = ""
    stats = {
        "mode": "full",
        "reason": reason,
        "listed": 0,
        "downloaded": 0,
        "chats": 0,
        "failed": [],
        "labels_changed": 0,
        "skipped": 0,
        "deleted_backed_up": 0,
        "unread": (),  # what the mailbox couldn't read (an IMAP account's folders)
    }
    try:
        _backup(gmail, account, con, mcon, folder, query, refresh_labels, stats)
    except Exception:
        _record_backup(con, mcon, started, stats, "error")
        raise
    else:
        problems = stats["failed"] or stats["unread"]
        _record_backup(con, mcon, started, stats, "failures" if problems else "ok")
        return stats
    finally:
        con.close()
        mcon.close()


def _record_backup(con, mcon, started: int, stats: dict, status: str) -> None:
    sidecar.record_run(
        mcon,
        "backup",
        started,
        now_ms(),
        status,
        mode=stats["mode"],
        reason=stats["reason"],
        listed=stats["listed"] if stats["mode"] == "full" else None,
        downloaded=stats["downloaded"],
        failed=len(stats["failed"]),
        labels_changed=stats["labels_changed"],
        skipped=stats["skipped"] + stats["chats"],
        deleted_in_gmail=stats["deleted_backed_up"],
        messages=con.execute("SELECT count(*) FROM uids").fetchone()[0],
    )


def _backup(
    gmail, account: str, con, mcon, folder: Path, query, refresh_labels: bool, stats: dict
) -> None:
    """The body of backup(): updates stats as it goes."""
    held = sidecar.get_state(mcon, "provider") or "gmail"
    messages = con.execute("SELECT count(*) FROM uids").fetchone()[0]
    problem = provider_change(account, folder, held, gmail.provider, messages)
    if problem:
        raise BackupMissing(problem)
    sidecar.replace_label_catalogue(mcon, gmail.label_catalogue())
    if gmail.provider != "gmail":  # its IDs aren't Gmail's
        sidecar.set_state(mcon, "provider", gmail.provider)
    gmail.save_state(mcon)  # an IMAP account's UID map, committed with the backup
    stats["unread"] = gmail.unread()
    logger.info(f"Using backup folder {folder}")
    have = gyb.backed_up_ids(con, folder)
    recorded = chain.start(folder, account)  # the hash chain, begun if there is none
    pending: dict[str, list] = {"backup": [], "refetch": []}

    def record():
        """Append the links for what is about to be committed, then forget them."""
        for op, pairs in pending.items():
            chain.append(folder, account, op, pairs)
            pairs.clear()

    start = None
    if not stats["reason"]:
        start = sidecar.get_state(mcon, "history_id")
        if not start:
            stats["reason"] = "no-history"
    todo = None
    if start:
        try:
            indexed = {u for (u,) in con.execute("SELECT uid FROM uids")}
            plan, next_history_id = history_plan(gmail, con, mcon, have, indexed, start, stats)
            todo = list(plan.todo)
            stats["mode"] = "history"
            con.commit()
            mcon.commit()
        except gmailapi.HistoryExpired:
            stats["reason"] = "expired"
            logger.info(
                "Gmail no longer has history back to the last run; listing every message instead."
            )
    if todo is None:
        # Taken before listing, so mail arriving meanwhile is in the next run.
        next_history_id = None if query else gmail.current_history_id()
        ids = gmail.list_message_ids(query)
        # Chats are never backed up, so they never count as backed up: leave
        # them out by their label rather than download each only to skip it.
        chats = set(gmail.list_message_ids(query, label_id=gyb.CHAT_LABEL))
        stats["listed"] = len(ids)
        todo = [i for i in ids if i not in have and i not in chats]
        stats["chats"] = sum(1 for i in ids if i in chats)
        logger.info(
            f"{mailbox_name(gmail.provider)} has {len(ids):,} messages"
            + (f" matching {query!r}" if query else "")
            + f"; {len(ids) - len(todo) - stats['chats']:,} already backed up; "
            + (f"{stats['chats']:,} chats skipped; " if stats["chats"] else "")
            + f"{len(todo):,} to download"
        )

    def on_message(response):
        label_ids = response.get("labelIds", [])
        if gyb.is_chat(label_ids):
            stats["chats"] += 1
            return
        gyb.write_message(con, folder, response, gmail.label_names(label_ids))
        sha = hashlib.sha256(base64.urlsafe_b64decode(response["raw"])).hexdigest()
        pending["refetch" if response["id"] in recorded else "backup"].append((response["id"], sha))
        write_meta(mcon, response)
        stats["downloaded"] += 1
        if stats["downloaded"] % COMMIT_EVERY == 0:
            record()
            con.commit()
            mcon.commit()

    if todo:
        with Progress("Downloading", len(todo)):
            stats["failed"] = gmail.get_raw(todo, on_message)
        record()
        con.commit()
        mcon.commit()

    if refresh_labels:
        existing = sorted(have)
        print(f"Refreshing labels of {len(existing):,} messages …")
        for gid, label_ids in current_labels(gmail, existing, len(have)).items():
            if label_ids is not None:
                stats["labels_changed"] += apply_labels(con, mcon, gmail, gid, label_ids)
        con.commit()

    if next_history_id and not stats["failed"]:
        sidecar.set_state(mcon, "history_id", next_history_id)
    mcon.commit()


def record_unattended_failure(args, reason: str = "needs-consent") -> None:
    """
    A backup that couldn't get access, needing a sign-in (unattended) or with
    its service account refused, is recorded as an `error` run in the
    account's meta.sqlite, so `report health` and `schedule status` show it.
    Nothing is recorded for an account that has no backup yet.
    """
    account, _config = gmailapi.resolve_account(Path(args.config), args.account)
    folder = Path(args.folder) if args.folder else backup_folder(account)
    if not sidecar.meta_path(folder).is_file():
        return
    mcon = sidecar.open_meta(folder)
    try:
        now = now_ms()
        sidecar.record_run(mcon, "backup", now, now, "error", reason=reason)
    finally:
        mcon.close()


def cmd_backup(args) -> int:
    if args.prune and args.search:
        sys.exit(
            "error: --prune can't be combined with --search: messages outside "
            "the search would look deleted."
        )
    if args.prune and args.folder:
        sys.exit(
            "error: --prune runs 'mailcellar forget --deleted', which works on the backups in "
            "the data root, not --folder."
        )
    try:
        account, folder, gmail = connect(args)
    except gmailapi.NeedsConsent:
        record_unattended_failure(args)
        raise
    except gmailapi.AccessRefused:
        record_unattended_failure(args, "access-refused")
        raise
    except gmailapi.GmailError:  # unreachable, or the account's settings can't be used
        if getattr(args, "unattended", False):
            record_unattended_failure(args, "no-access")
        raise
    with closing(gmail):  # an IMAP account logs out
        stats = backup(gmail, account, folder, args.search, args.refresh_labels, args.full)
        print(
            f"Downloaded {stats['downloaded']:,} messages"
            + (f", skipped {stats['chats']:,} chats" if stats["chats"] else "")
            + (
                f", updated labels on {stats['labels_changed']:,}"
                if args.refresh_labels or stats["mode"] == "history"
                else ""
            )
        )
        if stats["deleted_backed_up"] and not args.prune:
            print(
                f"{stats['deleted_backed_up']:,} backed-up messages were permanently deleted "
                f"from {mailbox_name(gmail.provider)}; 'backup --prune' would move them out "
                "of the backup."
            )
        if stats["failed"]:
            logger.warning(
                f"{len(stats['failed']):,} messages could not be fetched, e.g. "
                f"{stats['failed'][0]}. Re-run backup to retry them."
                + (" Prune skipped." if args.prune else "")
            )
            return 1
        if stats["unread"]:
            logger.warning(
                f"{len(stats['unread']):,} folders couldn't be read and were left as they "
                f"were in the backup: {', '.join(stats['unread'])}. Their mail isn't taken "
                "as deleted; the warnings above say why."
                + (" Prune skipped." if args.prune else "")
            )
            return 1

        if not args.prune:
            return 0
    return prune_by_forget(args, account)  # once the backup's own session has logged out


def prune_by_forget(args, account: str) -> int:
    """
    backup --prune, for a release: `mailcellar forget --deleted` on the
    account, after its backup, with --yes and --force passed on. Without
    --yes that is forget's dry run; with it, forget asks for the count.
    """
    from mailcellar.offload import forget_cli

    logger.warning(
        "'backup --prune' is now 'mailcellar forget --deleted', run after the backup: mail "
        "goes to <data root>/_forgotten/, held 30 days. --prune goes in a later release"
    )
    argv = [account, "--deleted", "--config", args.config]
    argv += ["--yes"] * args.yes + ["--force"] * args.force
    try:
        return forget_cli.main(argv, prog="mailcellar forget")
    except SystemExit as exc:  # forget's errors end its run, not the other accounts' backups
        if isinstance(exc.code, str):
            logger.error(f"{account}: {exc.code.removeprefix('error: ')}")
        return exc.code if isinstance(exc.code, int) and exc.code else 1


# ── meta backfill ────────────────────────────────────────────────────────────

DETAILS_CHUNK = 1000


def local_to_ms(sqlite_internaldate: str) -> int:
    """
    gyb's local-time message_internaldate → ms since epoch (UTC), in this
    machine's zone now. gyb's format records no zone, so a date written under
    another zone, or in the hour a clock change repeats, comes back off by the
    difference. Only messages Gmail no longer has rely on it: backfill takes
    Gmail's own date for the rest.
    """
    return int(datetime.fromisoformat(sqlite_internaldate).timestamp() * 1000)


def parse_action(version: str | None, in_meta: bool) -> str:
    """
    What meta does with a backed-up message, by the parser_version
    meta.sqlite has for it: "current" (this parser's: nothing), "reparse"
    (an older one's: update its fields) or "add" (no row: add one). Pure.
    """
    if version == PARSER_VERSION:
        return "current"
    return "reparse" if in_meta else "add"


def backfill(gmail, account: str, folder: Path) -> dict:
    """
    Bring meta.sqlite in line with the backup. Parses every backed-up
    message that is missing or was parsed by an older parser; then, unless
    gmail is None, fetches Gmail-only details for rows that lack them
    (messages gone from Gmail keep thread_id '' and no labels).
    """
    if not (folder / gyb.DB_NAME).is_file():
        raise gyb.FormatError(f"{folder} has no {gyb.DB_NAME}")
    con = gyb.open_backup(folder, account)
    try:
        mcon = sidecar.open_meta(folder)
    except BaseException:
        con.close()
        raise
    stats = {
        "backed_up": 0,
        "current": 0,
        "added": 0,
        "reparsed": 0,
        "errors": 0,
        "file_missing": 0,
        "details": 0,
        "gone_from_gmail": 0,
        "detail_failures": [],
    }
    try:
        backed_up = con.execute(
            """SELECT u.uid, m.message_filename, m.message_internaldate
               FROM uids u JOIN messages m USING (message_num)"""
        ).fetchall()
        known = dict(mcon.execute("SELECT gmail_id, parser_version FROM messages"))
        stats["backed_up"] = len(backed_up)

        with Progress("Parsing", len(backed_up)) as parsing:
            for gid, rel, local_date in backed_up:
                parsing.advance()
                action = parse_action(known.get(gid), gid in known)
                if action == "current":
                    stats["current"] += 1
                    continue
                path = folder / rel
                if not path.is_file():
                    stats["file_missing"] += 1
                    continue
                try:
                    fields = parse_eml(path)
                except Exception as exc:
                    logger.warning(f"Cannot parse {path}: {exc}")
                    stats["errors"] += 1
                    continue
                if action == "reparse":
                    sidecar.update_fields(mcon, gid, fields)
                    stats["reparsed"] += 1
                else:
                    sidecar.upsert_message(
                        mcon,
                        gid,
                        thread_id="",
                        internal_date=local_to_ms(local_date),
                        size_estimate=None,
                        fields=fields,
                    )
                    stats["added"] += 1
                done = stats["added"] + stats["reparsed"]
                if done % COMMIT_EVERY == 0:
                    mcon.commit()
        mcon.commit()

        if gmail is not None:
            sidecar.replace_label_catalogue(mcon, gmail.label_catalogue())
            need = [
                g
                for (g,) in mcon.execute(
                    "SELECT gmail_id FROM messages WHERE thread_id = '' ORDER BY gmail_id"
                )
            ]
            with Progress("Fetching details", len(need)):
                for start in range(0, len(need), DETAILS_CHUNK):
                    chunk = need[start : start + DETAILS_CHUNK]
                    for gid, d in gmail.message_details(chunk, stats["detail_failures"]).items():
                        if d is None:
                            stats["gone_from_gmail"] += 1
                            continue
                        size = d.get("sizeEstimate")
                        sidecar.update_gmail_details(
                            mcon,
                            gid,
                            thread_id=d.get("threadId", ""),
                            internal_date=int(d["internalDate"]),
                            size_estimate=int(size) if size is not None else None,
                        )
                        sidecar.set_message_labels(mcon, gid, d.get("labelIds", []))
                        stats["details"] += 1
                    mcon.commit()
        mcon.commit()
        return stats
    finally:
        con.close()
        mcon.close()


def cmd_meta(args) -> int:
    config_root = Path(args.config)
    account, config_folder = gmailapi.resolve_account(config_root, args.account)
    folder = Path(args.folder) if args.folder else backup_folder(account)
    gmail = None
    if not args.offline:
        gmail = mailbox.open_mailbox(account, config_folder, "readonly", folder=folder)
    with closing(gmail) if gmail is not None else nullcontext():
        st = backfill(gmail, account, folder)
    print(
        f"{account}: {st['backed_up']:,} backed up; {st['current']:,} already in "
        f"meta.sqlite; {st['added']:,} added, {st['reparsed']:,} re-parsed"
        + (f"; {st['errors']:,} unparseable" if st["errors"] else "")
        + (f"; {st['file_missing']:,} with no .eml" if st["file_missing"] else "")
    )
    if gmail is not None:
        print(
            f"{mailbox_name(gmail.provider)} details for {st['details']:,} messages"
            + (
                f"; {st['gone_from_gmail']:,} no longer in {mailbox_name(gmail.provider)}"
                if st["gone_from_gmail"]
                else ""
            )
        )
    failures = st["detail_failures"]
    if failures:
        logger.warning(
            f"{len(failures):,} lookups in {mailbox_name(gmail.provider)} failed, "
            f"e.g. {failures[0]}. "
            "Re-run meta to retry them."
        )
    return 1 if st["errors"] or failures else 0


# ── count ────────────────────────────────────────────────────────────────────


def count(gmail, account: str, folder: Path) -> dict:
    """
    Compare Gmail's message IDs with the backup. Read-only on Gmail and on
    the backed-up mail; records the run in meta.sqlite if there is one.
    """
    started = now_ms()
    if not (folder / gyb.DB_NAME).is_file():
        raise gyb.FormatError(f"{folder} has no {gyb.DB_NAME}")
    has_meta = sidecar.meta_path(folder).is_file()
    offloaded, kept, restored, forgotten = set(), set(), 0, 0
    back: set[str] = set()  # restored under their own IDs: the mailbox's again
    if has_meta:
        mcon = sidecar.open_meta(folder)
        offloaded = sidecar.offloaded_ids(mcon)  # offloaded now
        kept = sidecar.kept_ids(mcon)  # also those restored since
        restored_as = sidecar.restored_as(mcon)
        restored = len(restored_as)
        back = {s for s, n in restored_as.items() if s == n} - offloaded
        forgotten = len(sidecar.forgotten_ids(mcon))
        mcon.close()
    con = gyb.open_backup(folder, account)
    try:
        indexed = {uid for (uid,) in con.execute("SELECT uid FROM uids")}
        backed_up = gyb.backed_up_ids(con, folder)
        found = forget.gone_from(indexed, gmail.list_message_ids(include_spam_trash=True), kept)
        if found.listed_nothing:  # a mailbox answering wrongly, not every message deleted
            raise gmailapi.GmailError(
                f"{mailbox_name(gmail.provider, start=True)} listed no messages at all"
            )
        gone, gone_kept = found.deleted, found.offloaded
    finally:
        con.close()

    in_gmail = set(gmail.list_message_ids())
    missing = sorted(in_gmail - backed_up)
    # gyb never stores chats, so they always look missing; separate them out.
    chats = set()
    if missing:
        with Progress("Reading labels of messages not backed up", len(missing)):
            labels_by_id = gmail.message_labels(missing)
        for gid, labels in labels_by_id.items():
            if labels and gyb.is_chat(labels):
                chats.add(gid)
    result = {
        "in_gmail": len(in_gmail),
        "backed_up": len(backed_up),
        "missing": [g for g in missing if g not in chats],
        "chats": len(chats),
        # A message restored under its own ID that the mailbox lost again is only in
        # the backup, like any other; --prune still keeps it, as it keeps all kept_ids.
        "only_in_backup": len(backed_up - in_gmail - (kept - back)),
        "restored_gone": len(set(gone_kept) & back),
        "gone_from_server": len(gone),
        "offloaded": len(offloaded & indexed),
        "offloaded_in_gmail": len(offloaded & indexed) - len(offloaded & set(gone_kept)),
        "forgotten": forgotten,
        "restored": restored,
        "file_missing": len(indexed - backed_up),
    }
    if has_meta:
        mcon = sidecar.open_meta(folder)
        try:
            sidecar.mark_gone(mcon, gone_kept, now_ms())
            sidecar.record_run(
                mcon,
                "count",
                started,
                now_ms(),
                "missing" if result["missing"] else "ok",
                in_gmail=result["in_gmail"],
                missing=len(result["missing"]),
                only_in_backup=result["only_in_backup"],
                deleted_in_gmail=result["gone_from_server"],
                messages=result["backed_up"],
                offloaded=result["offloaded"],
            )
        finally:
            mcon.close()
    return result


def cmd_count(args) -> int:
    account, folder, gmail = connect(args)
    with closing(gmail):
        c = count(gmail, account, folder)
    server = mailbox_name(gmail.provider)
    on_server = (
        "In Gmail (excl. spam/trash)" if server == "Gmail" else "On the server (excl. junk/trash)"
    )
    in_server = "in Gmail" if server == "Gmail" else "on the server"
    lines = [
        (on_server, c["in_gmail"]),
        ("In backup", c["backed_up"]),
        ("Missing from backup", len(c["missing"])),
        ("Chats (never backed up)", c["chats"]),
        ("Only in backup (not in mailbox)", c["only_in_backup"]),
        ("  of which permanently deleted", c["gone_from_server"]),
        ("  of which restored, then deleted", c["restored_gone"]),
        ("Offloaded (kept in backup)", c["offloaded"]),
        (f"  of which still {in_server} (Trash)", c["offloaded_in_gmail"]),
        (f"Restored to {server}", c["restored"]),
        ("Forgotten (deleted from the backup)", c["forgotten"]),
        ("Indexed but .eml missing", c["file_missing"]),
    ]
    print(account)
    for label, n in lines:
        if n or label in (on_server, "In backup", "Missing from backup"):
            print(f"  {label:<34} {n:>8,}")
    if c["gone_from_server"]:
        print(
            f"\n'mailcellar forget --deleted' would take {c['gone_from_server']:,} "
            "permanently deleted messages out of the backup."
        )
    if c["restored_gone"]:
        print(
            f"\n{c['restored_gone']:,} messages restored before have gone from {server} again; "
            f"'restore {account} --all' would put them back."
        )
    if c["missing"]:
        print(f"\nRun 'backup' to fetch the missing messages, e.g. {c['missing'][0]}.")
        return 1
    return 0


# ── main ─────────────────────────────────────────────────────────────────────


def main(argv=None, prog: str | None = None) -> int:
    p = argparse.ArgumentParser(
        prog=prog, description="Incremental backup of Gmail and IMAP accounts, in gyb format."
    )
    sub = p.add_subparsers(dest="command", required=True)

    def add_common_args(sp):
        sp.add_argument(
            "accounts",
            nargs="*",
            metavar="ACCOUNT",
            help="Accounts: address, config folder name, or unique part "
            "(default: every account in accounts.toml)",
        )
        sp.add_argument(
            "--folder",
            default=None,
            help="Backup folder (default: <data root>/<email>; the data root is "
            "$MAILCELLAR_DATA, else data_dir in settings.toml, else ~/mailcellar)",
        )
        sp.add_argument(
            "--config",
            default=str(config_root()),
            help="Config folder: accounts.toml, settings.toml, one folder per account "
            f"(default: {config_root()})",
        )

    p_backup = sub.add_parser("backup", help="Download messages not yet backed up")
    add_common_args(p_backup)
    p_backup.add_argument(
        "--refresh-labels",
        action="store_true",
        help="Also re-sync labels of messages already backed up",
    )
    p_backup.add_argument(
        "--search",
        default=None,
        metavar="QUERY",
        help="Only consider messages matching this Gmail search",
    )
    p_backup.add_argument(
        "--full",
        action="store_true",
        help="List every message instead of reading Gmail's history since the last run",
    )
    p_backup.add_argument(
        "--prune",
        action="store_true",
        help="Then run 'mailcellar forget --deleted' on the account: what the mailbox has "
        "deleted for good leaves the backup, held 30 days (--prune goes in a later release)",
    )
    p_backup.add_argument(
        "--yes",
        action="store_true",
        help="With --prune: forget them after the count is typed; without it, a dry run",
    )
    p_backup.add_argument(
        "--unattended",
        action="store_true",
        help="For scheduled runs: never open a browser. A token that needs signing in fails "
        "the run, with the command to fix it, and is recorded for `report health`",
    )
    p_backup.add_argument(
        "--force",
        action="store_true",
        help="With --prune: go ahead although more than "
        f"{forget.MAX_DELETED_SHARE:.0%}% of the backup's messages are gone from the mailbox",
    )
    p_backup.set_defaults(func=cmd_backup)

    p_count = sub.add_parser("count", help="Compare the mailbox with the backup")
    add_common_args(p_count)
    p_count.set_defaults(func=cmd_count)

    p_meta = sub.add_parser("meta", help="Fill meta.sqlite for messages already backed up")
    add_common_args(p_meta)
    p_meta.add_argument(
        "--offline",
        action="store_true",
        help="Only parse .eml files; don't ask the mailbox for thread IDs, labels and sizes",
    )
    p_meta.set_defaults(func=cmd_meta)

    args = p.parse_args(argv)
    try:
        accounts = args.accounts or sorted(gmailapi.config_accounts(Path(args.config)))
    except gmailapi.GmailError as exc:
        sys.exit(f"error: {exc}")
    if args.folder and len(accounts) != 1:
        sys.exit("error: --folder needs exactly one account.")
    status = 0
    for account in accounts:
        if len(accounts) > 1:
            print(f"\n── {account} ──")
        try:
            status = max(status, args.func(argparse.Namespace(**vars(args), account=account)))
        except (gmailapi.GmailError, gyb.FormatError, sidecar.MetaError, BackupMissing) as exc:
            if len(accounts) == 1:
                sys.exit(f"error: {exc}")
            logger.error(f"{account}: {exc}")
            status = 1  # the other accounts still run
    return status
