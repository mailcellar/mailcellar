"""
forget: delete offloaded mail from the backup on purpose (#9), in stages.
It is the one irreversible thing mailcellar does, so:

  offload forget --batch B | --rule R
      Offloaded messages only. Each must be recorded gone from Gmail
      (offloaded.gone_ms, set by a backup or count once Gmail emptied its
      Trash) and still gone when Gmail is asked again now. A dry run unless
      --yes, and then the count must be typed.
  … --yes
      Moves their files to <data root>/_forgotten/<email>/<run>/ (with a
      manifest for --undo), deletes their rows from msg-db.sqlite and
      meta.sqlite, takes them out of the search index and the dedup
      catalogue, blanks their sender and subject in the offload and dedup
      plans, logs and batch manifests, deletes the review pages, and
      records them in meta.sqlite's forgotten table (ID, batch, rule, date,
      size: no sender, subject or content).
  --empty
      Deletes runs held longer than HOLD_DAYS, for good.
  --undo RUN
      Puts a held run back; `mailcellar meta` then re-parses it.

Copies outside the data root (Time Machine, a second copy, an export) still
hold the mail; the command says so. choose(), due() and scrub_rows() decide
from plain values; the rest reads and writes.
"""

import csv
import os
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from mailcellar.backup import chain, gyb, sidecar
from mailcellar.paths import data_root

HOLD_DAYS = 30  # like Gmail's Trash
FORGOTTEN_DIR = "_forgotten"  # <data root>/_forgotten/<email>/<run>/
MANIFEST = "forgotten.csv"
RUN_FORMAT = "%Y-%m-%d-%H%M%S"
SCRUBBED = ("sender", "subject")  # blanked in the CSVs; IDs, dates, rules and sizes stay
ROW_KEYS = (("account", "gmail_id"), ("keeper_account", "keeper_gmail_id"))


@dataclass(frozen=True)
class Offloaded:
    """An offloaded message that could be forgotten."""

    gmail_id: str
    batch: str
    rule: str
    gone_ms: int | None  # when a backup or count found Gmail no longer has it
    internal_date: int | None
    size_estimate: int | None


@dataclass(frozen=True)
class Choice:
    """What a forget run would do with each selected message."""

    forget: tuple[Offloaded, ...]
    not_gone: tuple[Offloaded, ...]  # not recorded gone: still in Gmail's Trash, or unseen
    in_gmail: tuple[Offloaded, ...]  # recorded gone, but Gmail has it now
    unchecked: tuple[tuple[Offloaded, str], ...]  # Gmail couldn't be asked: why


# ── decisions ────────────────────────────────────────────────────────────────


def choose(
    candidates: list[Offloaded],
    live: dict[str, set[str] | None],
    unread: dict[str, str],
) -> Choice:
    """
    Which candidates may be forgotten: recorded gone, and gone now. `live`
    is what Gmail says of each recorded-gone one (None: it doesn't have
    it), `unread` the lookups that failed. Pure; nothing is forgotten on
    a guess.
    """
    forget, not_gone, in_gmail, unchecked = [], [], [], []
    for c in candidates:
        if c.gone_ms is None:
            not_gone.append(c)
        elif c.gmail_id in unread:
            unchecked.append((c, unread[c.gmail_id]))
        elif live.get(c.gmail_id) is not None:
            in_gmail.append(c)
        else:
            forget.append(c)
    return Choice(tuple(forget), tuple(not_gone), tuple(in_gmail), tuple(unchecked))


def due(runs: list[str], now: datetime, hold_days: int | None = None) -> list[str]:
    """The runs (named RUN_FORMAT, local time) held hold_days (HOLD_DAYS) by `now`. Pure."""
    hold_days = HOLD_DAYS if hold_days is None else hold_days
    out = []
    for run in runs:
        try:
            started = datetime.strptime(run, RUN_FORMAT)
        except ValueError:
            continue  # not a forget run
        if now - started >= timedelta(days=hold_days):
            out.append(run)
    return sorted(out)


def held_until(run: str, hold_days: int | None = None) -> datetime:
    hold_days = HOLD_DAYS if hold_days is None else hold_days
    return datetime.strptime(run, RUN_FORMAT) + timedelta(days=hold_days)


def scrub_rows(rows: list[dict], forgotten: set[tuple[str, str]]) -> tuple[list[dict], int]:
    """
    The rows with SCRUBBED columns blanked where the row is about a
    forgotten (account, gmail_id), as itself or as a dedup keeper; and how
    many changed. Pure: new dicts, the given ones untouched.
    """
    out, changed = [], 0
    for row in rows:
        about = {(row.get(a, ""), row.get(g, "")) for a, g in ROW_KEYS}
        if about & forgotten and any(row.get(col) for col in SCRUBBED):
            row = {k: "" if k in SCRUBBED else v for k, v in row.items()}
            changed += 1
        out.append(row)
    return out, changed


# ── reading ──────────────────────────────────────────────────────────────────


def runs_dir(account: str) -> Path:
    """Where a backup's forget runs are held: <data root>/_forgotten/<email>/."""
    return data_root() / FORGOTTEN_DIR / account


def held_runs(account: str) -> list[str]:
    """The account's forget runs still held, oldest first."""
    folder = runs_dir(account)
    if not folder.is_dir():
        return []
    return sorted(p.name for p in folder.iterdir() if (p / MANIFEST).is_file())


def held(account: str) -> list[tuple[str, int]]:
    """The account's held runs, oldest first, each with how many messages it holds."""
    out = []
    for run in held_runs(account):
        with (runs_dir(account) / run / MANIFEST).open(newline="", encoding="utf-8") as fh:
            out.append((run, sum(1 for _ in csv.DictReader(fh))))
    return out


def candidates(folder: Path, batches=(), rules=()) -> list[Offloaded]:
    """
    The backup's offloaded messages (not restored, not forgotten) in any of
    the batches or rules, oldest first, read-only.
    """
    con = sqlite3.connect(f"file:{sidecar.meta_path(folder)}?mode=ro", uri=True)
    try:
        rows = con.execute("""
            SELECT o.gmail_id, o.batch, o.rule, o.gone_ms, m.internal_date, m.size_estimate
            FROM offloaded o LEFT JOIN messages m USING (gmail_id)
            WHERE o.restored_ms IS NULL
              AND o.gmail_id NOT IN (SELECT gmail_id FROM forgotten)
            ORDER BY m.internal_date, o.gmail_id""").fetchall()
    finally:
        con.close()
    return [
        Offloaded(*row)
        for row in rows
        if (not batches or row[1] in batches) and (not rules or row[2] in rules)
    ]


def derived_csvs() -> list[Path]:
    """Every CSV in the data root that names messages with their sender or subject."""
    root = data_root()
    found = [
        root / "offload" / "offload-plan.csv",
        root / "offload" / "offload-apply-log.csv",
        root / "dedup" / "dedup-plan.csv",
        root / "dedup" / "dedup-apply-log.csv",
    ]
    found += sorted((root / "offload" / "plans").glob("*.csv"))
    found += sorted((root / "offload" / "batches").glob("*/offloaded.csv"))
    return [p for p in found if p.is_file()]


def review_pages() -> list[Path]:
    """The offload review pages, which sample senders and subjects."""
    return sorted((data_root() / "offload" / "review").glob("*.html"))


# ── acting ───────────────────────────────────────────────────────────────────


def compact(con: sqlite3.Connection) -> None:
    """
    Rewrite a database without the pages its deleted rows left: SQLite keeps
    their bytes in free pages, readable in the file, until they are reused.
    """
    con.commit()
    con.execute("VACUUM")


def forget(folder: Path, account: str, chosen: list[Offloaded], run: str, now_ms: int) -> int:
    """
    Move the chosen messages' files to runs_dir(account)/run, delete their
    rows in msg-db.sqlite and meta.sqlite, and record them as forgotten.
    Returns how many were moved out. The caller scrubs the rest.
    """
    ids = [c.gmail_id for c in chosen]
    chain.append(folder, account, "forget", removed=chain.removal(folder, ids))
    con = gyb.open_backup(folder, account)
    try:
        manifest = gyb.prune(
            con, folder, [c.gmail_id for c in chosen], runs_dir(account) / run, MANIFEST
        )
        compact(con)
    finally:
        con.close()
    moved = {m["gmail_id"] for m in manifest}
    mcon = sidecar.open_meta(folder)
    try:
        mcon.executemany(
            """INSERT OR REPLACE INTO forgotten
               (gmail_id, batch, rule, internal_date, size_estimate, run, forgotten_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                (c.gmail_id, c.batch, c.rule, c.internal_date, c.size_estimate, run, now_ms)
                for c in chosen
                if c.gmail_id in moved
            ),
        )
        sidecar.delete_messages(mcon, moved)
        compact(mcon)
    finally:
        mcon.close()
    return len(moved)


def scrub_indexes(account: str, gmail_ids: list[str]) -> dict[str, int]:
    """Take the messages out of the search index and the dedup catalogue, where they exist."""
    from mailcellar.dedup import db as dedup_db
    from mailcellar.dedup.paths import default_db as dedup_path
    from mailcellar.search import db as search_db
    from mailcellar.search import index

    out = {"search": 0, "dedup": 0}
    if search_db.default_db().is_file():
        con = search_db.open_db(search_db.default_db())
        try:
            out["search"] = index.remove(con, account, gmail_ids)
            # FTS5 keeps a deleted document's words in its segments until they are merged.
            con.execute("INSERT INTO text (text) VALUES ('optimize')")
            compact(con)
        finally:
            con.close()
    if dedup_path().is_file():
        con = dedup_db.open_db(dedup_path())
        try:
            out["dedup"] = dedup_db.remove_messages(con, account, gmail_ids)
            compact(con)
        finally:
            con.close()
    return out


def scrub_files(forgotten: set[tuple[str, str]]) -> dict[str, int]:
    """
    Blank the forgotten messages' sender and subject in every derived CSV
    (rewritten in place, atomically), and delete the review pages.
    Returns {file: rows changed} and the pages deleted under "review pages".
    """
    out: dict[str, int] = {}
    for path in derived_csvs():
        with path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            columns, rows = reader.fieldnames or [], list(reader)
        rows, changed = scrub_rows(rows, forgotten)
        if not changed:
            continue
        part = path.with_name(f".{path.name}.part")
        with part.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(part, path)
        out[str(path)] = changed
    pages = review_pages()
    for page in pages:
        page.unlink()
    if pages:
        out["review pages"] = len(pages)
    return out


def empty(folder: Path, account: str, runs: list[str], now_ms: int) -> int:
    """Delete held runs for good, noting when in forgotten. Returns messages deleted."""
    deleted = 0
    mcon = sidecar.open_meta(folder)
    try:
        for run in runs:
            src = runs_dir(account) / run
            with (src / MANIFEST).open(newline="", encoding="utf-8") as fh:
                ids = [row["gmail_id"] for row in csv.DictReader(fh)]
            chain.append(folder, account, "empty", removed=[(gid, "") for gid in ids])
            shutil.rmtree(src)
            mcon.executemany(
                "UPDATE forgotten SET emptied_ms = ? WHERE gmail_id = ? AND run = ?",
                ((now_ms, gid, run) for gid in ids),
            )
            mcon.commit()
            deleted += len(ids)
    finally:
        mcon.close()
    return deleted


def returning(folder: Path, src: Path) -> list[tuple[str, str]]:
    """(Gmail ID, sha256) of what undo puts back: the held run's files the backup lacks."""
    with (src / MANIFEST).open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    have = chain.index_paths(folder)
    return [
        (row["gmail_id"], chain.file_hash(src / row["path"]) or "")
        for row in rows
        if row["gmail_id"] not in have
    ]


def undo(folder: Path, account: str, run: str) -> int:
    """
    Put a held run back into the backup and stop counting it as forgotten.
    Its meta.sqlite rows, search entries and catalogue rows come back with
    `mailcellar meta`, the next search and `dedup index`. Returns how many.
    """
    src = runs_dir(account) / run
    chain.append(folder, account, "undo", returning(folder, src))
    con = gyb.open_backup(folder, account)
    try:
        back = gyb.unprune(con, folder, src, MANIFEST)
    finally:
        con.close()
    mcon = sidecar.open_meta(folder)
    try:
        mcon.executemany(
            "DELETE FROM forgotten WHERE gmail_id = ? AND run = ?", ((g, run) for g in back)
        )
        mcon.commit()
    finally:
        mcon.close()
    shutil.rmtree(src)
    return len(back)
