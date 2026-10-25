"""
apply: take the messages of an offload plan out of Gmail, after checking that
the backup holds each one. The Gmail checks and the label → trash loop
are dedup apply's (dedup/apply.py); this adds:

- backup checks per message: the .eml parses, its content hash equals the
  sidecar's, the sidecar row is current, and it isn't already offloaded;
- before trashing: each account's last backup succeeded and ran after the
  plan was made, and a report on a second copy (Time Machine on macOS);
- the offload record: trashed messages go into meta.sqlite's offloaded table,
  so --sync keeps them in the backup, and into a manifest per batch,
  <data root>/offload/batches/<batch>/offloaded.csv (like --sync's pruned.csv).

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
from datetime import datetime
from pathlib import Path

from mailcellar.backup import sidecar
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
        sys.exit(f"ERROR: {path} is not an offload plan (missing {sorted(missing)}).")
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


def stale_backups(folders: dict[str, Path], accounts, since_ms: int) -> dict[str, str]:
    """Accounts whose last backup failed or ran before since_ms → why."""
    problems = {}
    for account in sorted(accounts):
        path = sidecar.meta_path(folders[account])
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = con.execute(
                """SELECT finished_ms, status FROM runs WHERE command = 'backup'
                   ORDER BY finished_ms DESC LIMIT 1"""
            ).fetchone()
        finally:
            con.close()
        if row is None:
            problems[account] = "no backup run recorded"
        elif row[1] != "ok":
            problems[account] = f"the last backup ended with {row[1]!r}"
        elif row[0] < since_ms:
            problems[account] = "the last backup ran before the plan was made"
    return problems


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
            "  It is older than the last Gmail backup: the newest mail isn't in it yet. "
            "Run Time Machine first to have every offloaded message twice."
        )
    return lines


def last_backup_ms(folders: dict[str, Path], accounts) -> int | None:
    latest = None
    for account in accounts:
        path = sidecar.meta_path(folders[account])
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            (ms,) = con.execute(
                "SELECT max(finished_ms) FROM runs WHERE command = 'backup' AND status = 'ok'"
            ).fetchone()
        finally:
            con.close()
        if ms is not None and (latest is None or ms > latest):
            latest = ms
    return latest


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
    (--sync reads it); the manifest is the readable copy, so it comes second.
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


def log_state(log_path: Path, label: str) -> tuple[set[tuple], set[tuple]]:
    """
    From earlier runs with this label: (account, gmail_id) of the messages
    labelled, and of those vetoed (a trash run found the label removed).
    """
    labelled, vetoed = set(), set()
    if not log_path.is_file():
        return labelled, vetoed
    with log_path.open(newline="", encoding="utf-8") as fh:
        for e in csv.DictReader(fh):
            key = (e["account"], e["gmail_id"])
            if e["mode"] == f"label ({label})" and e["outcome"] == "labelled":
                labelled.add(key)
            elif (
                e["mode"] == f"trash ({label})"
                and e["outcome"] == "skipped"
                and e["detail"] == f"not labelled {label}"
            ):
                vetoed.add(key)
    return labelled, vetoed


def remaining(
    rows: list[dict], mode: str, label: str, folders: dict[str, Path], log_path: Path
) -> list[dict]:
    """
    The plan rows still to do, so that repeated runs with --limit advance:
    never offloaded ones; for --label, not those labelled before; for
    --trash, not those vetoed before.
    """
    done = offloaded_by_account(folders)
    rows = [r for r in rows if r["gmail_id"] not in done.get(r["account"], set())]
    labelled, vetoed = log_state(log_path, label)
    if mode == "label":
        rows = [r for r in rows if (r["account"], r["gmail_id"]) not in labelled]
    elif mode == "trash":
        rows = [r for r in rows if (r["account"], r["gmail_id"]) not in vetoed]
    return rows


def recover_trashed(log: list[dict], mode: str, label: str, checks, on_trashed) -> int:
    """
    Messages trashed by an earlier run that died before recording them: in
    Trash, still carrying the label, not offloaded. --trash records them
    (after the backup checks); a dry run reports them. Returns how many.
    """
    found: dict[str, list[dict]] = {}
    for e in log:
        live = e.get("live") or set()
        if "TRASH" not in live or label not in live:
            continue
        # The Gmail checks stop at the first problem: the backup checks decide.
        problem = "no local backup copy" if not Path(e["row"]["path"]).is_file() else ""
        problem = problem or checks(e["row"])
        if problem:
            e["outcome"] = "skipped"
            e["detail"] = f"in Trash, but {problem}; restore it from Gmail's Trash"
            continue
        if mode == "dry-run":
            e["outcome"] = "would record (already in trash)"
            continue
        if mode != "trash":
            continue
        e["outcome"], e["detail"] = "recorded", "already in trash"
        found.setdefault(e["row"]["account"], []).append(e["row"])
    for account, rows in found.items():
        on_trashed(account, rows)
    return sum(len(rows) for rows in found.values())
