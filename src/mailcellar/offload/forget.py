"""
forget: delete mail from the backup on purpose (#9), in stages. It is the
one irreversible thing mailcellar does, so:

  forget --batch B | --rule R
      Offloaded messages. Each must be recorded gone from Gmail
      (offloaded.gone_ms, set by a backup or count once Gmail emptied its
      Trash) and still gone when Gmail is asked again now. A dry run unless
      --yes, and then the count must be typed.
  forget --deleted
      Mail deleted in its mailbox, never offloaded (B40: what backup --prune
      took): the mailbox is listed now (gone_from, deleted_decision), and
      more than MAX_DELETED_SHARE of the backup needs --force. Recorded
      with batch "" and rule DELETED.
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
  --due
      Retention (#72): instead of a batch or rule, what each offload rule's
      forget_after makes due, mail it offloaded that is older than that
      many years, through the same checks and stages.

Copies outside the data root (Time Machine, a second copy, an export) still
hold the mail; the command says so. choose(), due(), due_by_age(),
by_rule_and_year(), retention_counts() and scrub_rows() decide from plain
values; the rest reads and writes.
"""

import csv
import json
import logging
import os
import shutil
import sqlite3
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mailcellar.backup import chain, gyb, sidecar
from mailcellar.paths import data_root

logger = logging.getLogger(__name__)
_warned: set[Path] = set()  # rules files already warned about in this run

HOLD_DAYS = 30  # like Gmail's Trash
FORGOTTEN_DIR = "_forgotten"  # <data root>/_forgotten/<email>/<run>/
MANIFEST = "forgotten.csv"
RUN_FORMAT = "%Y-%m-%d-%H%M%S"
DELETED = "(deleted)"  # forgotten.rule for mail deleted in its mailbox, never offloaded
MAX_DELETED_SHARE = 0.10  # --deleted stops without --force if more of the backup would go
SCRUBBED = ("sender", "subject")  # blanked in the CSVs; IDs, dates, rules and sizes stay
ROW_KEYS = (("account", "gmail_id"), ("keeper_account", "keeper_gmail_id"))


@dataclass(frozen=True)
class Offloaded:
    """
    A message that could be forgotten: offloaded, or deleted in its mailbox
    and never offloaded (batch "", rule DELETED, gone_ms None; deleted()).
    """

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
    held: tuple[tuple[Offloaded, str], ...] = ()  # under a legal hold: its name


@dataclass(frozen=True)
class Gone:
    """What the backup's index holds that its mailbox no longer lists (gone_from)."""

    deleted: tuple[str, ...]  # never offloaded: what --deleted may take
    offloaded: tuple[str, ...]  # ever offloaded: --batch, --rule and --due's
    total: int  # the index, offloaded mail apart: what MAX_DELETED_SHARE is of
    listed_nothing: bool = False  # the mailbox listed no messages, though the backup has some


@dataclass(frozen=True)
class DeletedDecision:
    """What --deleted takes out of the backup, and what it leaves."""

    move: tuple[str, ...] = ()  # IDs to forget, in the order found
    held: tuple[tuple[str, str], ...] = ()  # (ID, the hold keeping it in the backup)
    refusal: str = ""  # why nothing may be taken although some could; "" if it may


@dataclass(frozen=True)
class Stopped:
    """
    What forget runs stopped part-way left in a backup (#145). A run appends
    its removal link for its whole selection before it moves a file, and
    commits the index only at the end, so a stopped one leaves messages the
    backup still indexes out of the hash chain, and the files it had moved
    in a run folder with no manifest.
    """

    relink: tuple[tuple[str, str], ...]  # (Gmail ID, the sha256 its removal recorded)
    home: tuple[tuple[str, Path, Path], ...]  # (Gmail ID, file in a stopped run, its place)
    runs: tuple[Path, ...]  # the stopped runs' folders

    @property
    def count(self) -> int:
        return len({g for g, _ in self.relink} | {g for g, _, _ in self.home})


# ── decisions ────────────────────────────────────────────────────────────────


def unexplained(
    removed: Mapping[str, str], held: Collection[str], index: Collection[str]
) -> list[str]:
    """
    Messages a removal link named (`removed`: Gmail ID → sha256) that the
    backup still indexes but the hash chain no longer holds: what a run
    stopped before committing leaves. A finished one took them out of the
    index. Pure.
    """
    held, index = set(held), set(index)
    return sorted(g for g in removed if g in index and g not in held)


def strays(
    files: Collection[str], index: Mapping[str, str], present: Collection[str]
) -> list[tuple[str, str]]:
    """
    (Gmail ID, path) of a stopped run's `files` (paths relative to the
    backup) to move home: those the index (Gmail ID → path) still names,
    where the backup has no file (`present`). Pure.
    """
    by_path = {rel: gid for gid, rel in index.items()}
    present = set(present)
    return sorted((by_path[r], r) for r in files if r in by_path and r not in present)


def gone_from(indexed: Collection[str], listed: Collection[str], kept: Collection[str]) -> Gone:
    """
    The backup's indexed IDs its mailbox doesn't list (`listed`, Trash and
    Spam included, so only mail deleted for good counts), split by whether
    they were ever offloaded (`kept`, restored or not). Pure.
    """
    indexed, listed, kept = set(indexed), set(listed), set(kept)
    gone = indexed - listed
    return Gone(
        tuple(sorted(gone - kept)),
        tuple(sorted(gone & kept)),
        len(indexed - kept),
        bool(indexed) and not listed,
    )


def deleted_decision(
    gone: Gone,
    unread: Collection[str],
    held: Mapping[str, str],
    *,
    force: bool,
    where: str,
    max_share: float = MAX_DELETED_SHARE,
) -> DeletedDecision:
    """
    What --deleted may take: gone.deleted, less what holds keep (`held`, ID
    → hold). Nothing at all when the mailbox (`where`) listed no messages,
    or couldn't read some folders (`unread`), whose mail would look deleted;
    nor when more than `max_share` of the backup would go and it isn't
    `force`d. Pure.
    """
    if gone.listed_nothing:
        return DeletedDecision(refusal=f"{where} listed no messages at all")
    if unread:
        return DeletedDecision(
            refusal=f"{len(unread):,} folders couldn't be read ({', '.join(unread)}), so their "
            "mail would look deleted"
        )
    kept = tuple((g, held[g]) for g in gone.deleted if g in held)
    move = tuple(g for g in gone.deleted if g not in held)
    if len(move) > max_share * gone.total and not force:
        return DeletedDecision(
            (),
            kept,
            f"{len(move):,} of {gone.total:,} backed-up messages are gone from {where}, "
            f"more than {max_share:.0%}; re-run with --force if that is right",
        )
    return DeletedDecision(move, kept)


def presence(
    listed: Collection[str], unread: Collection[str], gmail_ids: list[str]
) -> tuple[dict[str, set[str] | None], dict[str, str]]:
    """
    choose()'s `live` and `unread` from a listing of the mailbox rather than
    a lookup per message: present (an empty label set) or gone (None), and
    every ID unchecked when some folders couldn't be read. Pure.
    """
    if unread:
        why = f"{len(unread):,} folders couldn't be read"
        return {}, dict.fromkeys(gmail_ids, why)
    listed = set(listed)
    return {g: (set() if g in listed else None) for g in gmail_ids}, {}


def choose(
    candidates: list[Offloaded],
    live: dict[str, set[str] | None],
    unread: dict[str, str],
    held: dict[str, str] | None = None,
) -> Choice:
    """
    Which candidates may be forgotten: not under a legal hold (`held`: Gmail
    ID → hold), recorded gone, and gone now. `live` is what Gmail says of
    each recorded-gone one (None: it doesn't have it), `unread` the lookups
    that failed. Pure; nothing is forgotten on a guess.
    """
    held = held or {}
    forget, not_gone, in_gmail, unchecked, kept = [], [], [], [], []
    for c in candidates:
        if c.gmail_id in held:
            kept.append((c, held[c.gmail_id]))
        elif c.gone_ms is None:
            not_gone.append(c)
        elif c.gmail_id in unread:
            unchecked.append((c, unread[c.gmail_id]))
        elif live.get(c.gmail_id) is not None:
            in_gmail.append(c)
        else:
            forget.append(c)
    return Choice(tuple(forget), tuple(not_gone), tuple(in_gmail), tuple(unchecked), tuple(kept))


def deleted(gone: list[str], details: dict[str, tuple[int | None, int | None]]) -> list[Offloaded]:
    """
    The messages its mailbox deleted, never offloaded (`gone`), as forget
    candidates, with each one's (internal_date, size_estimate) where
    `details` has them. Pure.
    """
    return [Offloaded(g, "", DELETED, None, *details.get(g, (None, None))) for g in gone]


def year(c: Offloaded) -> str:
    """The year of the message's date (UTC), or "unknown" without one. Pure."""
    if c.internal_date is None:
        return "unknown"
    return datetime.fromtimestamp(c.internal_date / 1000, UTC).strftime("%Y")


def by_year(chosen: list[Offloaded]) -> list[tuple[str, int]]:
    """(year of the message's date, messages), sorted. Pure."""
    counts: dict[str, int] = {}
    for c in chosen:
        counts[year(c)] = counts.get(year(c), 0) + 1
    return sorted(counts.items())


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


def due_by_age(candidates: list[Offloaded], cutoffs: dict[str, int]) -> list[Offloaded]:
    """
    The candidates their rule's forget_after makes due: offloaded by a rule
    in `cutoffs` (rule → ms) and dated before its cut-off. A message with no
    recorded date is never due. Pure.
    """
    return [
        c
        for c in candidates
        if c.rule in cutoffs and c.internal_date is not None and c.internal_date < cutoffs[c.rule]
    ]


def by_rule_and_year(chosen: list[Offloaded]) -> list[tuple[str, str, int]]:
    """(rule, year of the message's date, messages), sorted. Pure."""
    counts: dict[tuple[str, str], int] = {}
    for c in chosen:
        key = (c.rule, year(c))
        counts[key] = counts.get(key, 0) + 1
    return [(rule, year, n) for (rule, year), n in sorted(counts.items())]


def retention_counts(
    due_now: list[Offloaded], held: dict[str, str]
) -> dict[str, tuple[int, int, int]]:
    """
    Rule → (recorded gone, not yet gone from Gmail, under a legal hold) among
    the messages due by age: what forget --due would take, before
    Gmail is asked again. Pure.
    """
    out: dict[str, list[int]] = {}
    for c in due_now:
        n = out.setdefault(c.rule, [0, 0, 0])
        n[2 if c.gmail_id in held else 0 if c.gone_ms is not None else 1] += 1
    return {rule: (a, b, c) for rule, (a, b, c) in sorted(out.items())}


def note(chosen: list[Offloaded], forget_after: dict[str, int] | None = None) -> str:
    """The chain link's note: the rules whose mail a run forgot, and their forget_after."""
    rules = sorted({c.rule for c in chosen})
    if forget_after is None:
        return json.dumps({"rules": rules})
    return json.dumps(
        {"rules": rules, "forget_after": {r: forget_after[r] for r in rules if r in forget_after}}
    )


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


def stopped(folder: Path, account: str) -> Stopped:
    """What stopped forget runs left in this backup (Stopped). Read-only."""
    removed = chain.removed(folder)
    index = chain.index_paths(folder)
    relink = tuple((g, removed[g]) for g in unexplained(removed, chain.held(folder), index))
    root = runs_dir(account)
    runs = tuple(
        r
        for r in sorted(root.iterdir() if root.is_dir() else ())
        if r.is_dir() and gyb.interrupted(r, MANIFEST)
    )
    home = []
    for run in runs:
        files = [str(p.relative_to(run)) for p in run.rglob("*.eml")]
        present = [r for r in files if (folder / r).is_file()]
        home += [(gid, run / rel, folder / rel) for gid, rel in strays(files, index, present)]
    return Stopped(relink, tuple(home), runs)


def held(account: str) -> list[tuple[str, int]]:
    """The account's held runs, oldest first, each with how many messages it holds."""
    out = []
    for run in held_runs(account):
        with (runs_dir(account) / run / MANIFEST).open(newline="", encoding="utf-8") as fh:
            out.append((run, sum(1 for _ in csv.DictReader(fh))))
    return out


def runs_under_hold(
    folder: Path, account: str, runs: list[str], now_ms: int
) -> dict[str, list[str]]:
    """
    Held forget runs holding mail a legal hold may cover: run → the holds.
    Forgotten mail keeps no addresses or labels, so a hold that names
    parties or a label covers it unless its dates or IDs rule it out.
    """
    from mailcellar.hold import holds

    out = {}
    for run in runs:
        with (runs_dir(account) / run / MANIFEST).open(newline="", encoding="utf-8") as fh:
            ids = [row["gmail_id"] for row in csv.DictReader(fh)]
        names = sorted(set(holds.held(folder, now_ms, ids).values()))
        if names:
            out[run] = names
    return out


def rules_path() -> Path:
    """The offload rules file: <data root>/offload/rules.toml."""
    return data_root() / "offload" / "rules.toml"


def policies(path: Path | None = None) -> dict[str, tuple[int, int]]:
    """
    Rule → (forget_after years, cut-off ms) for the offload rules that have
    one; {} without a readable rules file. For the reports, which only say
    what is due: forget --due reads the rules itself and stops on
    an error.
    """
    from mailcellar.offload.rules import RuleError, load_rules
    from mailcellar.report.cli import parse_before

    path = path or rules_path()
    if not path.is_file():
        return {}  # no rules, so no retention
    try:
        rules = load_rules(path, parse_before, lambda name: name)
    except (RuleError, OSError) as exc:
        if path not in _warned:
            _warned.add(path)
            logger.warning(
                f"Retention: can't read the offload rules, so what is due isn't known: {exc}"
            )
        return {}
    return {r.name: (r.forget_after, r.forget_ms) for r in rules if r.forget_after}


def retention(folder: Path, now_ms: int, path: Path | None = None) -> dict[str, tuple[int, ...]]:
    """
    Rule → (forget_after, recorded gone, not yet gone, held) for the backup's
    mail due under retention; rules with nothing due are left out. Read-only.
    """
    from mailcellar.hold import holds

    found = policies(path)
    if not found or not sidecar.meta_path(folder).is_file():
        return {}
    due_now = due_by_age(
        candidates(folder, rules=set(found)), {r: ms for r, (_y, ms) in found.items()}
    )
    held_ids = holds.held(folder, now_ms, [c.gmail_id for c in due_now]) if due_now else {}
    return {rule: (found[rule][0], *n) for rule, n in retention_counts(due_now, held_ids).items()}


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


def index_ids(folder: Path) -> set[str]:
    """The Gmail IDs in the backup's index (msg-db.sqlite). Read-only."""
    con = sqlite3.connect(f"file:{folder / gyb.DB_NAME}?mode=ro", uri=True)
    try:
        return {uid for (uid,) in con.execute("SELECT uid FROM uids")}
    finally:
        con.close()


def details(folder: Path, gmail_ids: list[str], chunk: int = 500) -> dict:
    """Gmail ID → (internal_date, size_estimate) from meta.sqlite, where it has them. Read-only."""
    out = {}
    con = sqlite3.connect(f"file:{sidecar.meta_path(folder)}?mode=ro", uri=True)
    try:
        for start in range(0, len(gmail_ids), chunk):
            ids = gmail_ids[start : start + chunk]
            for gid, date, size in con.execute(
                f"""SELECT gmail_id, internal_date, size_estimate FROM messages
                    WHERE gmail_id IN ({",".join("?" * len(ids))})""",
                ids,
            ):
                out[gid] = (date, size)
    finally:
        con.close()
    return out


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


def forget(
    folder: Path, account: str, chosen: list[Offloaded], run: str, now_ms: int, why: str = ""
) -> int:
    """
    Move the chosen messages' files to runs_dir(account)/run, delete their
    rows in msg-db.sqlite and meta.sqlite, and record them as forgotten;
    the chain link carries `why` (note()). Returns how many were moved out.
    The caller scrubs the rest.
    """
    ids = [c.gmail_id for c in chosen]
    chain.append(folder, account, "forget", removed=chain.removal(folder, ids), note=why)
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


def repair(folder: Path, account: str, found: Stopped) -> None:
    """
    Put back what stopped runs left (stopped()): one `undo` link, appended
    first as for every change, records the messages again with the sha256
    their removal recorded (so verify --chain still sees a changed file),
    then the files go home and the emptied run folders go.
    """
    runs = [r.name for r in found.runs]
    chain.append(folder, account, "undo", found.relink, note=json.dumps({"stopped_runs": runs}))
    for _gid, src, dest in found.home:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(src, dest)
    for run in found.runs:
        gyb.remove_if_empty(run)


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
