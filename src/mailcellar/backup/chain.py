"""
chain: tamper evidence for a backup (#70). verify catches a changed .eml by
the hash meta.sqlite recorded, but both sit in the data root, so whatever
can rewrite one can rewrite the other. The chain records what each command
added to and removed from the backup, each link naming the one before by
its hash, and the latest link is kept outside the data root as well.

  <folder>/chain.jsonl   one link per line, only ever appended:
      {"seq": 12, "time": "2026-11-02T03:30:11Z", "op": "backup",
       "added": [[gmail_id, sha256 of the .eml], ...], "removed": [...],
       "prev": sha256 of link 11's line ("" for the first)}
  <config>/anchors/<email>.json   {"seq": ..., "hash": ...} of the latest link

ops: genesis (what the backup held when the chain started), backup,
refetch (a missing file downloaded again), restore (a restored message
indexed under its new ID), prune, forget, undo (forget --undo) and empty
(forget --empty: the held files deleted; the backup itself is unchanged);
hold and release record a legal hold being placed or lifted, its details
in "note", and change nothing in the backup.

A link is appended before the change it records is committed, so a crash
leaves a link whose messages never arrived (shown as missing; the next
backup fetches them again), never messages no link explains. Labels are
not covered: they change in Gmail all the time and aren't the mail.

next_link(), replay(), compare() and check_anchor() decide from plain
values; the rest reads and writes.
"""

import fcntl
import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from mailcellar.backup.gyb import DB_NAME
from mailcellar.paths import config_root, write_private
from mailcellar.progress import Progress

CHAIN_NAME = "chain.jsonl"
ANCHOR_DIR = "anchors"
OPS = (
    "genesis", "backup", "refetch", "restore", "prune", "forget", "undo", "empty",
    "hold", "release",
)  # fmt: skip
EVENTS = ("genesis", "hold", "release")  # appended even with no messages

# What check() finds. The first five fail verify; the last two are warnings.
BROKEN = "chain broken"
CHANGED = "changed since recorded"
MISSING = "recorded, but missing from the backup"
UNEXPLAINED = "in the backup, but no link added it"
ANCHOR_MISMATCH = "anchor doesn't match the chain"
UNANCHORED = "links after the anchor"
NO_ANCHOR = "no anchor"
WARNINGS = (UNANCHORED, NO_ANCHOR)

Pair = tuple[str, str]  # (Gmail ID, sha256 of its .eml)


class ChainError(Exception):
    pass


@dataclass(frozen=True)
class Link:
    seq: int
    time: str
    op: str
    added: tuple[Pair, ...] = ()
    removed: tuple[Pair, ...] = ()
    prev: str = ""
    note: str = ""  # hold and release: the hold, as JSON


@dataclass(frozen=True)
class Problem:
    kind: str
    gmail_id: str = ""
    detail: str = ""


# ── decisions ────────────────────────────────────────────────────────────────


def line_of(link: Link) -> str:
    """
    A link as its line in chain.jsonl (no newline): compact, keys in a fixed
    order; "note" only when there is one, so other links' lines are unchanged.
    """
    d = {
        "seq": link.seq,
        "time": link.time,
        "op": link.op,
        "added": [list(p) for p in link.added],
        "removed": [list(p) for p in link.removed],
        "prev": link.prev,
    }
    if link.note:
        d["note"] = link.note
    return json.dumps(d, separators=(",", ":"))


def hash_line(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def parse_line(line: str) -> Link:
    """A line of chain.jsonl as a Link. Raises ChainError if it isn't one."""
    try:
        d = json.loads(line)
        link = Link(
            int(d["seq"]),
            str(d["time"]),
            str(d["op"]),
            tuple((str(g), str(h)) for g, h in d["added"]),
            tuple((str(g), str(h)) for g, h in d["removed"]),
            str(d["prev"]),
            str(d.get("note", "")),
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise ChainError(f"not a link: {exc}") from None
    if link.op not in OPS:
        raise ChainError(f"not a link: unknown op {link.op!r}")
    return link


def next_link(
    last: tuple[int, str] | None, op: str, added, removed, time: str, note: str = ""
) -> Link:
    """The link after `last` ((seq, hash of its line), None for the first). Pure."""
    seq, prev = last if last else (0, "")
    return Link(seq + 1, time, op, tuple(added), tuple(removed), prev, note)


def replay(lines: list[str]) -> tuple[dict[str, str], list[Problem], list[tuple[int, str]]]:
    """
    What the chain says the backup holds now (Gmail ID → sha256), any break
    in it, and each link's (seq, hash) in order. A line that isn't a link,
    or one whose seq or prev doesn't follow, is a break; replay goes on past
    it so the rest is still checked. Pure.
    """
    held: dict[str, str] = {}
    problems: list[Problem] = []
    heads: list[tuple[int, str]] = []
    prev_seq, prev_hash = 0, ""
    for n, line in enumerate(lines, 1):
        h = hash_line(line)
        try:
            link = parse_line(line)
        except ChainError as exc:
            problems.append(Problem(BROKEN, detail=f"line {n}: {exc}"))
            prev_seq, prev_hash = prev_seq + 1, h
            heads.append((prev_seq, h))
            continue
        if link.seq != prev_seq + 1 or link.prev != prev_hash:
            problems.append(
                Problem(BROKEN, detail=f"line {n}: link {link.seq} doesn't follow link {prev_seq}")
            )
        if link.op not in ("empty", "hold", "release"):  # these leave the backup as it is
            for gid, _h in link.removed:
                held.pop(gid, None)
            for gid, sha in link.added:
                held[gid] = sha
        prev_seq, prev_hash = link.seq, h
        heads.append((link.seq, h))
    return held, problems, heads


def removals(lines: list[str]) -> dict[str, str]:
    """
    Gmail ID → the sha256 a prune or forget recorded as it moved the message
    out of the backup (the latest, if more than one). Pure; lines that aren't
    links are skipped (replay reports them).
    """
    out: dict[str, str] = {}
    for line in lines:
        try:
            link = parse_line(line)
        except ChainError:
            continue
        if link.op in ("prune", "forget"):
            out.update((gid, sha) for gid, sha in link.removed if sha)
    return out


def compare(held: dict[str, str], actual: dict[str, str | None]) -> list[Problem]:
    """
    The backup (Gmail ID → sha256 of its file, None if the file is missing)
    against what the chain says it holds. An indexed message whose file is
    missing and that no link recorded is left to plain verify. Pure.
    """
    problems = []
    for gid in sorted(held):
        sha = actual.get(gid)
        if sha is None:
            problems.append(Problem(MISSING, gid))
        elif sha != held[gid]:
            problems.append(Problem(CHANGED, gid))
    problems += [
        Problem(UNEXPLAINED, gid)
        for gid in sorted(actual)
        if gid not in held and actual[gid] is not None
    ]
    return problems


def check_anchor(heads: list[tuple[int, str]], anchor: dict | None) -> list[Problem]:
    """The chain's links (seq, hash) against the anchor kept outside the data root. Pure."""
    if anchor is None:
        return [Problem(NO_ANCHOR, detail="nothing outside the data root to check against")]
    seq, sha = anchor.get("seq"), anchor.get("hash")
    by_seq = dict(heads)
    if seq not in by_seq:
        last = heads[-1][0] if heads else 0
        return [
            Problem(ANCHOR_MISMATCH, detail=f"anchor names link {seq}; the chain ends at {last}")
        ]
    if by_seq[seq] != sha:
        return [Problem(ANCHOR_MISMATCH, detail=f"link {seq} isn't the one the anchor recorded")]
    later = heads[-1][0] - seq
    if later:
        return [Problem(UNANCHORED, detail=f"{later} links after the anchor's link {seq}")]
    return []


# ── reading and writing ──────────────────────────────────────────────────────


def chain_path(folder: Path) -> Path:
    return folder / CHAIN_NAME


def anchor_path(account: str) -> Path:
    return config_root() / ANCHOR_DIR / f"{account}.json"


def read_lines(folder: Path) -> list[str]:
    path = chain_path(folder)
    if not path.is_file():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def read_anchor(account: str) -> dict | None:
    try:
        return json.loads(anchor_path(account).read_text(encoding="utf-8"))
    except FileNotFoundError, ValueError:
        return None


def file_hash(path: Path) -> str | None:
    """sha256 of a file's bytes; None if there is no file."""
    try:
        with path.open("rb") as fh:
            return hashlib.file_digest(fh, "sha256").hexdigest()
    except FileNotFoundError, IsADirectoryError:
        return None


def index_paths(folder: Path) -> dict[str, str]:
    """Gmail ID → .eml path relative to the folder, from msg-db.sqlite, read-only."""
    con = sqlite3.connect(f"file:{folder / DB_NAME}?mode=ro", uri=True)
    try:
        return dict(
            con.execute(
                "SELECT u.uid, m.message_filename FROM uids u JOIN messages m USING (message_num)"
            )
        )
    finally:
        con.close()


def held(folder: Path) -> dict[str, str]:
    """What the chain says the backup holds (Gmail ID → sha256); {} with no chain."""
    return replay(read_lines(folder))[0]


def removed(folder: Path) -> dict[str, str]:
    """What the chain recorded of the messages pruned or forgotten from the backup."""
    return removals(read_lines(folder))


def append(
    folder: Path, account: str, op: str, added=(), removed=(), note: str = ""
) -> Link | None:
    """
    Append a link and update the anchor. Nothing to record (no messages, and
    not an event: genesis, hold, release) appends nothing. The line is on
    disk before this returns. The chain file is locked while the last link
    is read, the next written and the anchor updated, so two commands
    running at once (the scheduled backup and one typed by hand) can't both
    link to the same previous link. A genesis, if the chain has begun
    meanwhile, appends nothing.
    """
    added, removed = tuple(added), tuple(removed)
    if not added and not removed and op not in EVENTS:
        return None
    with chain_path(folder).open("a", encoding="utf-8") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX)  # released when the file closes
        except OSError:
            pass  # a filesystem without locks (some network shares): append as before
        lines = read_lines(folder)
        if lines and op == "genesis":
            return None
        last = None
        if lines:
            try:
                last = (parse_line(lines[-1]).seq, hash_line(lines[-1]))
            except ChainError:
                last = (len(lines), hash_line(lines[-1]))  # verify reports the bad line
        time = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        link = next_link(last, op, added, removed, time, note)
        line = line_of(link)
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())
        anchor = anchor_path(account)
        anchor.parent.mkdir(parents=True, exist_ok=True)
        write_private(anchor, json.dumps({"seq": link.seq, "hash": hash_line(line)}).encode())
    return link


def start(folder: Path, account: str) -> dict[str, str]:
    """
    The chain's view of the backup, starting the chain first if there is
    none: a genesis link with the hash of every indexed message's file.
    """
    if chain_path(folder).is_file():
        return held(folder)
    paths = index_paths(folder)
    pairs = []
    with Progress(f"{account}: starting the hash chain", len(paths)) as hashing:
        for gid, rel in sorted(paths.items()):
            sha = file_hash(folder / rel)
            if sha is not None:
                pairs.append((gid, sha))
            hashing.advance()
    append(folder, account, "genesis", pairs)
    return held(folder)  # another command may have begun the chain meanwhile


def removal(folder: Path, gmail_ids) -> list[Pair]:
    """(Gmail ID, the hash the chain recorded, or the file's) for messages about to leave."""
    recorded = held(folder)
    paths = index_paths(folder)
    out = []
    for gid in gmail_ids:
        if gid not in paths:
            continue
        sha = recorded.get(gid) or file_hash(folder / paths[gid]) or ""
        out.append((gid, sha))
    return out


@dataclass(frozen=True)
class Report:
    account: str
    links: int
    head: tuple[int, str] | None
    problems: tuple[Problem, ...]

    @property
    def failures(self) -> tuple[Problem, ...]:
        return tuple(p for p in self.problems if p.kind not in WARNINGS)

    @property
    def warnings(self) -> tuple[Problem, ...]:
        return tuple(p for p in self.problems if p.kind in WARNINGS)


def check(folder: Path, account: str) -> Report:
    """Replay the chain and compare it with every file in the backup and with the anchor."""
    lines = read_lines(folder)
    if not lines:
        return Report(account, 0, None, (Problem(BROKEN, detail="no chain: run a backup"),))
    expected, problems, heads = replay(lines)
    paths = index_paths(folder)
    actual = {}
    with Progress(f"{account}: checking the hash chain", len(paths)) as hashing:
        for gid, rel in paths.items():
            actual[gid] = file_hash(folder / rel)
            hashing.advance()
    problems += compare(expected, actual)
    problems += check_anchor(heads, read_anchor(account))
    return Report(account, len(lines), heads[-1], tuple(problems))
