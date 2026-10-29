"""
apply: take the messages of an offload plan out of Gmail, after checking that
the backup holds each one. The Gmail checks and the label → trash loop
are dedup apply's (dedup/apply.py); this adds:

- backup checks per message: the .eml parses, its content hash equals the
  sidecar's, the sidecar row is current, and it isn't already offloaded;
- before trashing: each account's last backup succeeded and ran after the
  plan was made, and a report on a second copy (Time Machine on macOS);
- the offload record: trashed messages go into meta.sqlite's offloaded table,
  so --prune keeps them in the backup, and into a manifest per batch,
  <data root>/offload/batches/<batch>/offloaded.csv (like --prune's pruned.csv).

Gmail empties its Trash after 30 days; nothing here deletes permanently.
"""

import csv
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from calendar import timegm
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from mailcellar.backup import sidecar
from mailcellar.dedup.apply import Result, labelled_rows, log_state
from mailcellar.message import PARSER_VERSION, parse_eml
from mailcellar.offload.plan import PLAN_COLUMNS, PLANNED_FORMAT

OFFLOAD_LABEL = "offload-candidate"
APPLY_LOG_COLUMNS = [
    "time",
    "mode",
    "account",
    "gmail_id",
    "rule",
    "planned",  # the plan the run used (its planned time; see offload/plans/)
    "outcome",
    "detail",
    "date",
    "sender",
    "subject",
]


def read_plan(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    missing = set(PLAN_COLUMNS) - set(rows[0] if rows else PLAN_COLUMNS)
    if missing:
        sys.exit(f"error: {path} is not an offload plan (missing {sorted(missing)}).")
    return rows


def planned_ms(rows: list[dict]) -> int:
    """When the plan was made (the latest, if rows were combined), ms UTC."""
    return max(timegm(time.strptime(r["planned"], PLANNED_FORMAT)) * 1000 for r in rows)


class BackupChecks:
    """local_check for apply_plan: is this message safely in the backup?"""

    def __init__(self, folders: dict[str, Path]):
        self.folders = folders  # account → backup folder
        self._rows: dict[str, dict[str, tuple]] = {}
        self._offloaded: dict[str, set[str]] = {}

    def _load(self, account: str) -> None:
        path = sidecar.meta_path(self.folders[account])
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            self._rows[account] = {
                gid: (content_hash, version)
                for gid, content_hash, version in con.execute(
                    "SELECT gmail_id, content_hash, parser_version FROM messages"
                )
            }
            self._offloaded[account] = {
                g
                for (g,) in con.execute("SELECT gmail_id FROM offloaded WHERE restored_ms IS NULL")
            }
        finally:
            con.close()

    def __call__(self, row: dict) -> str:
        account = row["account"]
        if account not in self.folders:
            return "account's backup not chosen"
        if account not in self._rows:
            self._load(account)
        gid = row["gmail_id"]
        if gid in self._offloaded[account]:
            return "already offloaded"
        stored = self._rows[account].get(gid)
        if stored is None:
            return "not in the sidecar; run mailcellar meta"
        content_hash, version = stored
        if version != PARSER_VERSION:
            return "sidecar row is stale; run mailcellar meta"
        try:
            fields = parse_eml(Path(row["path"]))
        except Exception as exc:
            return f"backup copy doesn't parse: {exc}"
        if fields["content_hash"] != content_hash:
            return "backup copy differs from the sidecar"
        return ""


def last_backups(folders: dict[str, Path], accounts) -> dict[str, tuple[int, str] | None]:
    """Each account's last backup run, (finished_ms, status), or None if none is recorded."""
    out = {}
    for account in sorted(accounts):
        path = sidecar.meta_path(folders[account])
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            out[account] = con.execute(
                """SELECT finished_ms, status FROM runs WHERE command = 'backup'
                   ORDER BY finished_ms DESC LIMIT 1"""
            ).fetchone()
        finally:
            con.close()
    return out


def stale_backups(
    last: Mapping[str, tuple[int, str] | None], since_ms: int | None
) -> dict[str, str]:
    """
    Accounts whose last backup (from last_backups) failed, or ran before
    since_ms if given → why. Pure.
    """
    problems = {}
    for account, run in sorted(last.items()):
        if run is None:
            problems[account] = "no backup run recorded"
        elif run[1] != "ok":
            problems[account] = f"the last backup ended with {run[1]!r}"
        elif since_ms is not None and run[0] < since_ms:
            problems[account] = "the last backup ran before the plan was made"
    return problems


def backup_problems(
    last: Mapping[str, tuple[int, str] | None], planned_ms: int, mode: str
) -> tuple[dict[str, str], tuple[str, ...]]:
    """
    What apply says of each account's last backup (from last_backups). Pure.
    Problems (account → why) warn in every mode and stop --trash, which also
    counts a backup older than the plan as one. Outside --trash that is the
    normal order, so those accounts are returned separately, to back up
    before --trash.
    """
    if mode == "trash":
        return stale_backups(last, planned_ms), ()
    behind = tuple(
        a for a, run in sorted(last.items()) if run and run[1] == "ok" and run[0] < planned_ms
    )
    return stale_backups(last, None), behind


def _tmutil(*args: str) -> tuple[int, str]:
    try:
        done = subprocess.run(
            ["tmutil", *args], capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return done.returncode, (done.stdout + done.stderr).strip()


def second_copy_report(data_dir: Path, last_backup_ms: int | None) -> list[str]:
    """
    Lines on whether the data root has a copy elsewhere. Only Time Machine can be
    checked (macOS); the report informs, it never blocks.
    """
    if sys.platform != "darwin" or shutil.which("tmutil") is None:
        return [
            "Time Machine: not available here. Check that the data root is copied somewhere else."
        ]
    lines = []
    code, out = _tmutil("destinationinfo")
    names = re.findall(r"^Name\s*:\s*(.+)$", out, re.M)
    if code or not names:
        lines.append("Time Machine: no backup destination configured.")
        return lines
    lines.append(f"Time Machine: backing up to {', '.join(n.strip() for n in names)}.")
    code, out = _tmutil("isexcluded", str(data_dir.resolve()))
    if "[Excluded]" in out:
        lines.append(f"  {data_dir} is EXCLUDED from Time Machine.")
    elif "[Included]" in out:
        lines.append(f"  {data_dir} is included.")
    else:
        lines.append(f"  Whether {data_dir} is included is unknown ({out.splitlines()[0]}).")
    code, out = _tmutil("latestbackup")
    found = re.search(r"(\d{4}-\d{2}-\d{2}-\d{6})", out)
    if code or not found:
        first = out.splitlines()[0] if out else "no output"
        lines.append(f"  Latest Time Machine backup: unknown ({first}).")
        return lines
    latest = datetime.strptime(found.group(1), "%Y-%m-%d-%H%M%S")  # local time
    lines.append(f"  Latest Time Machine backup: {latest:%Y-%m-%d %H:%M}.")
    if last_backup_ms is not None and latest.timestamp() * 1000 < last_backup_ms:
        lines.append(
            "  It is older than the last mail backup: the newest mail isn't in it yet. "
            "Run Time Machine first to have every offloaded message twice."
        )
    return lines


MANIFEST_NAME = "offloaded.csv"
# A batch manifest row: the plan row, plus the batch and when it was trashed (UTC).
MANIFEST_COLUMNS = [*PLAN_COLUMNS, "batch", "trashed"]


def manifest_path(offload_dir: Path, batch: str) -> Path:
    return offload_dir / "batches" / batch / MANIFEST_NAME


def write_manifest(path: Path, rows: list[dict], batch: str, trashed_ms: int) -> None:
    """Append rows to a batch manifest (CSV, MANIFEST_COLUMNS), with a header if new."""
    path.parent.mkdir(parents=True, exist_ok=True)
    trashed = time.strftime(PLANNED_FORMAT, time.gmtime(trashed_ms / 1000))
    new = not path.is_file()
    with path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=MANIFEST_COLUMNS, extrasaction="ignore")
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({**r, "batch": batch, "trashed": trashed})


def recorder(folders: dict[str, Path], batch: str, manifest: Path):
    """
    on_trashed for apply_plan: record trashed messages in the account's
    offloaded table, then in the batch manifest. The table is the record
    (--prune reads it); the manifest is the readable copy, so it comes second.
    """

    def on_trashed(account: str, rows: list[dict]) -> None:
        if not rows:
            return
        now = int(time.time() * 1000)
        con = sidecar.open_meta(folders[account])
        try:
            by_rule: dict[str, list[str]] = {}
            for r in rows:
                by_rule.setdefault(r["rule"], []).append(r["gmail_id"])
            for rule, ids in by_rule.items():
                sidecar.record_offloaded(con, ids, batch, rule, now)
            con.commit()
        finally:
            con.close()
        write_manifest(manifest, rows, batch, now)

    return on_trashed


def upgrade_log(path: Path, columns: list[str]) -> None:
    """
    Rewrite an apply log written with other columns (an older version) to
    `columns`, values kept, new columns empty, so appended rows line up.
    """
    if not path.is_file():
        return
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames == columns:
            return
        rows = list(reader)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


def offloaded_by_account(folders: dict[str, Path]) -> dict[str, set[str]]:
    """account → Gmail IDs already offloaded, from each sidecar (read-only)."""
    out = {}
    for account, folder in folders.items():
        path = sidecar.meta_path(folder)
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            out[account] = {
                g
                for (g,) in con.execute("SELECT gmail_id FROM offloaded WHERE restored_ms IS NULL")
            }
        finally:
            con.close()
    return out


def restored_by_account(folders: dict[str, Path]) -> dict[str, dict[str, int]]:
    """account → {Gmail ID: when restored, ms} of offloaded messages put back since (read-only)."""
    out = {}
    for account, folder in folders.items():
        path = sidecar.meta_path(folder)
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            out[account] = dict(
                con.execute(
                    "SELECT gmail_id, restored_ms FROM offloaded WHERE restored_ms IS NOT NULL"
                )
            )
        finally:
            con.close()
    return out


def restored_since_planned(row: Mapping, restored: Mapping[str, Mapping[str, int]]) -> bool:
    """
    Whether the row's message was restored after its plan row was made
    (restored from restored_by_account). The restore changed its labels
    (restored/<date> on, the offload label off), so every run would skip it,
    and it has no place under --limit. A message restored before the plan
    was made was planned as it is now. Pure.
    """
    when = restored.get(row["account"], {}).get(row["gmail_id"])
    return when is not None and when >= timegm(time.strptime(row["planned"], PLANNED_FORMAT)) * 1000


def remaining(
    rows: list[dict], mode: str, label: str, folders: dict[str, Path], log_path: Path
) -> list[dict]:
    """
    The plan rows still to do, so that repeated runs with --limit advance:
    never offloaded ones, and not those restored since the plan was made;
    for --label, not those labelled before; for
    --trash, only those a label run labelled and not vetoed since. --trash
    only acts on labelled messages, so reading Gmail for the rest would only
    cost time (15,664 lookups, over four minutes, on 2026-10-26); a message
    labelled by a run that stopped before writing its log is labelled, and
    logged, again by the next --label run.
    """
    done = offloaded_by_account(folders)
    rows = [r for r in rows if r["gmail_id"] not in done.get(r["account"], set())]
    restored = restored_by_account(folders)
    rows = [r for r in rows if not restored_since_planned(r, restored)]
    labelled, vetoed = log_state(log_path, label)
    if mode == "label":
        rows = [r for r in rows if (r["account"], r["gmail_id"]) not in labelled]
    elif mode == "trash":
        rows = list(labelled_rows(rows, labelled, vetoed))
    return rows


def trashed_before(entry: Result, label: str) -> bool:
    """
    Whether the message looks trashed by an earlier run: in Trash and still
    carrying the label (a message trashed by hand has lost it, or never had it).
    """
    live = entry.live or frozenset()
    return "TRASH" in live and label in live


def recovered(entry: Result, mode: str, problem: str) -> Result:
    """
    A message trashed by an earlier run, as this run's mode treats it, given
    why its backup copy can't be used ("" if it can). A copy that can't be used
    is skipped in every mode; a dry run reports the message; --trash records it;
    any other mode leaves it alone. Pure.
    """
    if problem:
        return replace(
            entry,
            outcome="skipped",
            detail=f"in Trash, but {problem}; restore it from its mailbox's Trash",
        )
    if mode == "dry-run":
        return replace(entry, outcome="would record (already in trash)")
    if mode != "trash":
        return entry
    return replace(entry, outcome="recorded", detail="already in trash")


def recover_trashed(
    log: list[Result], mode: str, label: str, checks, on_trashed
) -> tuple[list[Result], int]:
    """
    Messages trashed by an earlier run that died before recording them: in
    Trash, still carrying the label, not offloaded. --trash records them
    (after the backup checks); a dry run reports them. Returns the log with
    those entries replaced, and how many were recorded; `log` is not changed.
    """
    out: list[Result] = []
    found: dict[str, list[dict]] = {}
    for entry in log:
        if not trashed_before(entry, label):
            out.append(entry)
            continue
        # The Gmail checks stop at the first problem: the backup checks decide.
        problem = "no local backup copy" if not Path(entry.row["path"]).is_file() else ""
        problem = problem or checks(entry.row)
        new = recovered(entry, mode, problem)
        out.append(new)
        if new.outcome == "recorded":
            found.setdefault(entry.row["account"], []).append(entry.row)
    for account, rows in found.items():
        on_trashed(account, rows)
    return out, sum(len(rows) for rows in found.values())
