"""
verify: check that each backup's files are still intact. Read-only: both
databases are opened read-only, and nothing is recorded. It needs no Gmail
access.

Once mail is offloaded and Gmail has emptied its Trash, the backup holds the
only copy, so this looks for what would otherwise go unnoticed: a failing
disk, a sync tool rewriting files, an accidental delete. For each message
indexed in msg-db.sqlite it checks that

  - the .eml file exists and parses;
  - its content hash equals the one meta.sqlite recorded when it was parsed;

and that msg-db.sqlite and meta.sqlite agree on which messages exist. With
--chain it also replays the backup's hash chain (backup/chain.py) against
every file and the anchor in the config folder. With
--offloaded it also checks that every message recorded as offloaded is in the
index. A message whose meta.sqlite row is missing or from an older parser has
nothing to compare with: it is counted as unchecked, and `mailcellar meta`
fills it in.

The decisions are pure functions (judge, select, disagreements) over frozen
values; reading the files and databases is a thin layer around them.
"""

import argparse
import logging
import random
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from mailcellar.backup import chain, sidecar
from mailcellar.backup.gyb import DB_NAME, backup_account
from mailcellar.message import PARSER_VERSION, parse_eml
from mailcellar.progress import Progress
from mailcellar.report.cli import resolve_folders
from mailcellar.report.db import AnalyticsError

logger = logging.getLogger(__name__)


OK, UNCHECKED = "ok", "unchecked"
MISSING = "file missing"
UNREADABLE = "file does not parse"
MISMATCH = "content hash differs from meta.sqlite"
NOT_INDEXED = "offloaded, but not in the backup's index"
NOT_IN_INDEX = "in meta.sqlite, but not in the backup's index"


class VerifyError(Exception):
    pass


@dataclass(frozen=True)
class Entry:
    """One message of the index: its Gmail ID and its .eml path, relative to the folder."""

    gmail_id: str
    path: str


@dataclass(frozen=True)
class Read:
    """What reading an .eml gave: its content hash, or why there is none."""

    content_hash: str = ""
    problem: str = ""  # MISSING or UNREADABLE
    detail: str = ""


@dataclass(frozen=True)
class Stored:
    """What meta.sqlite recorded for a message."""

    content_hash: str
    parser_version: str


@dataclass(frozen=True)
class Finding:
    gmail_id: str
    problem: str
    detail: str = ""


@dataclass(frozen=True)
class Report:
    account: str
    indexed: int  # messages in msg-db.sqlite
    checked: int  # messages whose file was read and compared
    unchecked: int  # no current meta.sqlite row to compare with
    findings: tuple[Finding, ...]
    note: str = ""  # why every message is unchecked, when meta.sqlite can't be used

    @property
    def ok(self) -> bool:
        return not self.findings


def judge(read: Read, stored: Stored | None) -> str:
    """
    OK, UNCHECKED or a problem. The hash is compared only with a row the
    current parser wrote: after a parser change the stored hash is of
    another computation, and `mailcellar meta` brings it up to date.
    """
    if read.problem:
        return read.problem
    if stored is None or stored.parser_version != PARSER_VERSION or not stored.content_hash:
        return UNCHECKED
    return OK if read.content_hash == stored.content_hash else MISMATCH


def select(
    entries: list[Entry], offloaded: set[str] | None, sample: int | None, rng: random.Random
) -> list[Entry]:
    """
    The entries to check: only offloaded ones when `offloaded` is given, then
    `sample` of them at random if that is smaller. In index order otherwise,
    so progress and findings are stable from run to run.
    """
    chosen = [e for e in entries if offloaded is None or e.gmail_id in offloaded]
    if sample is not None and sample < len(chosen):
        keep = {e.gmail_id for e in rng.sample(chosen, sample)}
        chosen = [e for e in chosen if e.gmail_id in keep]
    return chosen


def disagreements(
    indexed: set[str], in_meta: set[str], offloaded: set[str] | None
) -> list[Finding]:
    """
    Where the two databases disagree about which messages exist: rows in
    meta.sqlite for messages the index doesn't have, and, with `offloaded`,
    offloaded messages the index doesn't have. (An indexed message with no
    meta.sqlite row is not a disagreement: `meta` has not run yet, and it
    is counted as unchecked.)
    """
    found = [Finding(g, NOT_IN_INDEX) for g in sorted(in_meta - indexed)]
    if offloaded is not None:
        found += [Finding(g, NOT_INDEXED) for g in sorted(offloaded - indexed)]
    return found


# ── reading ──────────────────────────────────────────────────────────────────


def read_eml(path: Path) -> Read:
    """Parse one .eml and return its content hash, or what is wrong with it."""
    if not path.is_file():
        return Read(problem=MISSING)
    try:
        return Read(content_hash=parse_eml(path)["content_hash"])
    except Exception as exc:
        return Read(problem=UNREADABLE, detail=f"{type(exc).__name__}: {exc}")


def read_index(folder: Path) -> list[Entry]:
    """The messages msg-db.sqlite indexes, in index order. Opens it read-only."""
    con = sqlite3.connect(f"file:{folder / DB_NAME}?mode=ro", uri=True)
    try:
        return [
            Entry(uid, fname)
            for uid, fname in con.execute(
                """SELECT u.uid, m.message_filename
                   FROM messages m JOIN uids u USING (message_num)
                   ORDER BY m.message_num"""
            )
        ]
    finally:
        con.close()


def read_meta(folder: Path) -> tuple[dict[str, Stored], set[str], str]:
    """
    (stored hashes by Gmail ID, offloaded IDs, why meta.sqlite can't be used or ""),
    from the folder's meta.sqlite, opened read-only. An absent file or an older
    schema (not yet migrated by backup or meta) gives no hashes to compare.
    """
    path = sidecar.meta_path(folder)
    if not path.is_file():
        return {}, set(), "no meta.sqlite: run mailcellar meta"
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT value FROM meta WHERE name = 'schema_version'").fetchone()
        if row is None or row[0] != sidecar.SCHEMA_VERSION:
            return (
                {},
                set(),
                f"meta.sqlite has schema {row[0] if row else None}: run mailcellar meta",
            )
        stored = {
            gid: Stored(content_hash, version)
            for gid, content_hash, version in con.execute(
                "SELECT gmail_id, content_hash, parser_version FROM messages"
            )
        }
        return stored, sidecar.offloaded_ids(con), ""
    finally:
        con.close()


def verify_backup(
    folder: Path,
    only_offloaded: bool = False,
    sample: int | None = None,
    rng: random.Random | None = None,
) -> Report:
    """Check one backup folder. Raises VerifyError when --offloaded can't be answered."""
    rng = rng or random.Random()
    account = backup_account(folder)
    entries = read_index(folder)
    stored, offloaded, note = read_meta(folder)
    if only_offloaded and note:
        raise VerifyError(f"{account}: the offload record is unavailable ({note})")

    scope = offloaded if only_offloaded else None
    chosen = select(entries, scope, sample, rng)
    # Rows without an index entry are only looked for when every message is checked.
    full = not only_offloaded and sample is None
    findings = disagreements({e.gmail_id for e in entries}, set(stored) if full else set(), scope)

    checked = unchecked = 0
    with Progress(f"{account}: checking", len(chosen)) as checking:
        for entry in chosen:
            read = read_eml(folder / entry.path)
            verdict = judge(read, stored.get(entry.gmail_id))
            if verdict == OK:
                checked += 1
            elif verdict == UNCHECKED:
                unchecked += 1
            else:
                findings.append(Finding(entry.gmail_id, verdict, read.detail or entry.path))
            checking.advance()
    return Report(account, len(entries), checked, unchecked, tuple(findings), note)


# ── command ──────────────────────────────────────────────────────────────────


def show(report: Report, top: int, only_offloaded: bool) -> None:
    what = "offloaded messages" if only_offloaded else "messages"
    print(
        f"{report.account}: {report.checked:,} {what} verified"
        + (f", {report.unchecked:,} unchecked" if report.unchecked else "")
        + (f", {len(report.findings):,} problems" if report.findings else ", no problems")
    )
    for finding in report.findings[:top]:
        print(
            f"  {finding.gmail_id}  {finding.problem}"
            + (f"  ({finding.detail})" if finding.detail else "")
        )
    if len(report.findings) > top:
        print(f"  ... and {len(report.findings) - top:,} more (--top N shows more)")
    if report.unchecked or report.note:
        logger.warning(
            f"{report.account}: {report.unchecked:,} messages have no current meta.sqlite "
            "row to compare with; run 'mailcellar meta' to fill it in."
        )


def show_chain(report: chain.Report, top: int) -> None:
    head = f", head {report.head[0]} {report.head[1][:16]}" if report.head else ""
    failures = report.failures
    print(
        f"{report.account}: hash chain of {report.links:,} links{head}"
        + (f", {len(failures):,} problems" if failures else ", no problems")
    )
    for p in failures[:top]:
        print(
            f"  {p.gmail_id + '  ' if p.gmail_id else ''}{p.kind}"
            + (f" ({p.detail})" if p.detail else "")
        )
    if len(failures) > top:
        print(f"  ... and {len(failures) - top:,} more (--top N shows more)")
    for p in report.warnings:
        logger.warning(f"{report.account}: {p.kind}: {p.detail}")


def main(argv=None, prog: str | None = None) -> int:
    p = argparse.ArgumentParser(
        prog=prog or "mailcellar verify",
        description="Check that each backup's files are intact: every indexed message "
        "has its .eml, it parses, and its content hash matches meta.sqlite. "
        "Changes nothing; needs no Gmail access. Exits 1 if anything is wrong.",
    )
    p.add_argument(
        "accounts",
        nargs="*",
        metavar="ACCOUNT",
        help="Accounts (address, config folder name or unique part of an address); "
        "default: every backup in the data root",
    )
    p.add_argument(
        "--offloaded",
        action="store_true",
        help="Only offloaded mail, the copies with nowhere else to come from",
    )
    p.add_argument(
        "--sample",
        type=int,
        default=None,
        metavar="N",
        help="Check N messages chosen at random (of the offloaded ones, with --offloaded)",
    )
    p.add_argument(
        "--chain",
        action="store_true",
        help="Also check the backup against its hash chain and the anchor in the config "
        "folder: every file's SHA-256 as recorded, nothing added or removed unrecorded",
    )
    p.add_argument("--top", type=int, default=50, help="Problems listed per account (default: 50)")
    args = p.parse_args(argv)
    if args.sample is not None and args.sample < 1:
        p.error("--sample needs a positive number")
    if args.chain and (args.sample is not None or args.offloaded):
        p.error("--chain checks every message: it can't be combined with --sample or --offloaded")

    try:
        folders = resolve_folders(args.accounts)
        if not folders:
            raise AnalyticsError("no backups found in the data root")
    except AnalyticsError as exc:
        sys.exit(f"error: {exc}")

    status = 0
    for n, folder in enumerate(folders):
        if len(folders) > 1:
            print(f"{'' if n == 0 else chr(10)}── {folder.name} ──")
        try:
            report = verify_backup(folder, args.offloaded, args.sample)
        except (VerifyError, sqlite3.Error, ValueError) as exc:
            if len(folders) == 1:
                sys.exit(f"error: {exc}")
            logger.error(f"{folder.name}: {exc}")
            status = 1
            continue
        show(report, args.top, args.offloaded)
        if not report.ok:
            status = 1
        if args.chain:
            checked = chain.check(folder, report.account)
            show_chain(checked, args.top)
            if checked.failures:
                status = 1
    return status
