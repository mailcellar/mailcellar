"""
holds: legal holds (#71). A hold names mail that must be kept as it is, for
a dispute, an audit or a records request, and every command that would
remove mail skips what a hold covers:

  offload apply, dedup apply   held mail stays in Gmail
  backup --prune               held mail stays in the backup after Gmail deletes it
  offload forget, --empty      held mail is neither forgotten nor emptied

A hold follows its criteria, checked when a command runs, so mail that
matches after the hold was placed (next week's reply from the other side)
is held too. Its criteria, all of which must match:

  parties    an address or "@domain" in From, To, Cc or Bcc (any of them)
  after_ms   mail from this date on;  before_ms  mail older than this
  label      a user label, case ignored

and, besides those, any message named by its Gmail ID. Holds live in each
covered backup's meta.sqlite (holds table); placing and releasing one is
recorded in its hash chain. A fact that isn't known, such as the addresses
of mail already forgotten, counts as matching: a hold errs on keeping.

Criteria, Hold, Facts and the functions up to "reading" decide from plain
values; the rest reads and writes.
"""

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from mailcellar.backup import chain, sidecar


class HoldError(Exception):
    pass


@dataclass(frozen=True)
class Criteria:
    parties: tuple[str, ...] = ()  # lower case: addresses or "@domain"s
    after_ms: int | None = None  # ms since epoch, UTC; mail from then on
    before_ms: int | None = None  # mail older than this
    label: str = ""  # a user label
    ids: tuple[str, ...] = ()  # Gmail IDs held whatever the rest says

    def selects(self) -> bool:
        """Whether anything but IDs is given."""
        return bool(self.parties or self.label) or (
            self.after_ms is not None or self.before_ms is not None
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> Criteria:
        d = json.loads(text)
        return cls(
            tuple(d.get("parties", ())),
            d.get("after_ms"),
            d.get("before_ms"),
            d.get("label", ""),
            tuple(d.get("ids", ())),
        )


@dataclass(frozen=True)
class Hold:
    name: str
    reason: str
    criteria: Criteria
    created_ms: int
    until_ms: int | None = None  # ends by itself then; None: until released
    released_ms: int | None = None

    def active(self, now_ms: int) -> bool:
        if self.released_ms is not None:
            return False
        return self.until_ms is None or now_ms < self.until_ms

    def note(self) -> str:
        """The hold as the hash chain records it."""
        return json.dumps(
            {
                "name": self.name,
                "reason": self.reason,
                "criteria": json.loads(self.criteria.to_json()),
                "until_ms": self.until_ms,
            },
            sort_keys=True,
        )


@dataclass(frozen=True)
class Facts:
    """What a hold is checked against, for one message; None: not known."""

    gmail_id: str
    internal_date: int | None = None
    addresses: frozenset[str] | None = None  # From, To, Cc, Bcc, lower case
    labels: frozenset[str] | None = None  # user label names, casefolded
    in_gmail: bool = True  # False when the backup knows Gmail no longer has it
    known: bool = True  # False for mail with no sidecar row (forgotten)


# ── decisions ────────────────────────────────────────────────────────────────


def criteria(parties=(), after_ms=None, before_ms=None, label: str = "", ids=()) -> Criteria:
    """
    Criteria from what was asked for, checked: something to hold, and dates
    the right way round. Raises HoldError. Pure.
    """
    parties = tuple(sorted({p.strip().lower() for p in parties if p.strip()}))
    for p in parties:
        if "@" not in p:
            raise HoldError(f"--party {p!r}: give an address or @domain")
    c = Criteria(parties, after_ms, before_ms, label.strip(), tuple(sorted(set(ids))))
    if not c.selects() and not c.ids:
        raise HoldError("say what to hold: --party, --after, --before, --label or --id")
    if after_ms is not None and before_ms is not None and after_ms >= before_ms:
        raise HoldError("--after must be earlier than --before")
    return c


def party_matches(party: str, address: str) -> bool:
    """An address, or "@domain" matching any address at exactly that domain. Pure."""
    address = address.lower()
    return address.endswith(party) if party.startswith("@") else address == party


def covers(c: Criteria, f: Facts) -> bool:
    """
    Whether the criteria cover the message: named by ID, or matching every
    criterion given. A fact that isn't known matches. Pure.
    """
    if f.gmail_id in c.ids:
        return True
    if not c.selects():
        return False
    if f.internal_date is not None:
        if c.after_ms is not None and f.internal_date < c.after_ms:
            return False
        if c.before_ms is not None and f.internal_date >= c.before_ms:
            return False
    if c.parties and f.addresses is not None:
        if not any(party_matches(p, a) for p in c.parties for a in f.addresses):
            return False
    if c.label and f.labels is not None and c.label.casefold() not in f.labels:
        return False
    return True


def holding(holds: list[Hold], f: Facts, now_ms: int) -> str | None:
    """The name of the first hold in force that covers the message, or None. Pure."""
    for h in holds:
        if h.active(now_ms) and covers(h.criteria, f):
            return h.name
    return None


def held_now(holds: list[Hold], facts: list[Facts], now_ms: int) -> dict[str, str]:
    """Gmail ID → the hold keeping it, for the messages any hold in force covers. Pure."""
    active = [h for h in holds if h.active(now_ms)]
    if not active:
        return {}
    out = {}
    for f in facts:
        name = holding(active, f, now_ms)
        if name is not None:
            out[f.gmail_id] = name
    return out


# ── reading ──────────────────────────────────────────────────────────────────


def _read(folder: Path) -> sqlite3.Connection | None:
    path = sidecar.meta_path(folder)
    if not path.is_file():
        return None
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def read_holds(folder: Path) -> list[Hold]:
    """Every hold the backup records, released ones too, oldest first; [] before schema 11."""
    con = _read(folder)
    if con is None:
        return []
    try:
        rows = con.execute(
            """SELECT name, reason, criteria, created_ms, until_ms, released_ms
               FROM holds ORDER BY created_ms, id"""
        ).fetchall()
    except sqlite3.OperationalError:
        return []  # an older schema: no holds yet
    finally:
        con.close()
    return [Hold(n, r, Criteria.from_json(c), cr, u, rel) for n, r, c, cr, u, rel in rows]


def read_facts(folder: Path, gmail_ids=None) -> list[Facts]:
    """
    What holds are checked against, for the backup's messages (or just
    `gmail_ids`, read by ID rather than reading the whole sidecar): from
    meta.sqlite, read-only, forgotten mail included with only what the
    forgotten table keeps.
    """
    con = _read(folder)
    if con is None:
        return [Facts(g, known=False) for g in sorted(gmail_ids or ())]
    try:
        only = ""
        if gmail_ids is not None:
            # A temporary table (the read-only file isn't written), so any number of IDs fits.
            con.execute("CREATE TEMP TABLE wanted (gmail_id TEXT PRIMARY KEY)")
            con.executemany("INSERT OR IGNORE INTO wanted VALUES (?)", ((g,) for g in gmail_ids))
            only = " AND {} IN (SELECT gmail_id FROM temp.wanted)"
        rows = {
            gid: (ms, sender, thread)
            for gid, ms, sender, thread in con.execute(
                "SELECT gmail_id, internal_date, sender_addr, thread_id FROM messages WHERE 1"
                + only.format("gmail_id")
            )
        }
        addresses: dict[str, set[str]] = {}
        for gid, address in con.execute(
            "SELECT gmail_id, address FROM recipients WHERE 1" + only.format("gmail_id")
        ):
            addresses.setdefault(gid, set()).add(address.lower())
        labels: dict[str, set[str]] = {}
        for gid, name in con.execute(
            """SELECT ml.gmail_id, l.name FROM message_labels ml
               JOIN labels l USING (label_id) WHERE l.type = 'user'"""
            + only.format("ml.gmail_id")
        ):
            labels.setdefault(gid, set()).add(name.casefold())
        forgotten = dict(
            con.execute(
                "SELECT gmail_id, internal_date FROM forgotten WHERE 1" + only.format("gmail_id")
            )
        )
        # What the backup knows Gmail no longer has: offloaded mail Gmail has
        # deleted (a backup or count saw it gone), and rows made after it went.
        gone = {
            g
            for (g,) in con.execute(
                "SELECT gmail_id FROM offloaded WHERE gone_ms IS NOT NULL AND restored_ms IS NULL"
                + only.format("gmail_id")
            )
        }
    finally:
        con.close()
    wanted = set(gmail_ids) if gmail_ids is not None else set(rows) | set(forgotten)
    out = []
    for gid in sorted(wanted):
        if gid in rows:
            ms, sender, thread = rows[gid]
            got = addresses.get(gid, set()) | ({sender.lower()} if sender else set())
            in_gmail = thread != "" and gid not in gone
            # Every message has a sender or a recipient: none recorded means its
            # headers weren't read (a parse that failed), so they aren't known.
            known_addresses = frozenset(got) if got else None
            out.append(Facts(gid, ms, known_addresses, frozenset(labels.get(gid, ())), in_gmail))
        else:  # forgotten, or no sidecar row: only what is recorded
            out.append(Facts(gid, forgotten.get(gid), in_gmail=False, known=False))
    return out


def held(folder: Path, now_ms: int, gmail_ids=None) -> dict[str, str]:
    """Gmail ID → the hold keeping it, for the backup's held messages (or among gmail_ids)."""
    holds = read_holds(folder)
    if not any(h.active(now_ms) for h in holds):
        return {}
    return held_now(holds, read_facts(folder, gmail_ids), now_ms)


class HoldCheck:
    """
    local_check for apply_plan: "" or "held: NAME" for a plan row, so offload
    and dedup apply leave held mail in Gmail. folders: account → backup;
    rows: the plan's rows, so each account is checked for exactly the
    messages planned: one the sidecar doesn't know (a gyb-era backup that
    hasn't been through `mailcellar meta`, which dedup index parses itself)
    has unknown facts and counts as held by any hold that could cover it.
    """

    def __init__(self, folders: dict[str, Path], now_ms: int, rows=()):
        self.folders, self.now_ms = folders, now_ms
        self._ids: dict[str, set[str]] = {}
        for row in rows:
            self._ids.setdefault(row["account"], set()).add(row["gmail_id"])
        self._held: dict[str, dict[str, str]] = {}

    def __call__(self, row: dict) -> str:
        account = row["account"]
        if account not in self._held:
            folder = self.folders.get(account)
            ids = self._ids.get(account) or {row["gmail_id"]}
            self._held[account] = held(folder, self.now_ms, sorted(ids)) if folder else {}
        elif (
            row["gmail_id"] not in self._ids.get(account, ())
            and row["gmail_id"] not in self._held[account]
        ):
            # Asked about a row the check wasn't given: look it up on its own.
            folder = self.folders.get(account)
            self._held[account].update(
                held(folder, self.now_ms, [row["gmail_id"]]) if folder else {}
            )
            self._ids.setdefault(account, set()).add(row["gmail_id"])  # held or not, known now
        name = self._held[account].get(row["gmail_id"])
        return f"held: {name}" if name else ""


# ── writing ──────────────────────────────────────────────────────────────────


def add(folder: Path, account: str, hold: Hold) -> None:
    """Record a hold in the backup and its hash chain. Raises HoldError on a name in force."""
    if any(h.name == hold.name and h.active(hold.created_ms) for h in read_holds(folder)):
        raise HoldError(f"{account}: a hold named {hold.name!r} is already in force")
    con = sidecar.open_meta(folder)  # brings an older sidecar up to schema 11
    try:
        chain.append(folder, account, "hold", note=hold.note())
        con.execute(
            """INSERT INTO holds (name, reason, criteria, created_ms, until_ms)
               VALUES (?, ?, ?, ?, ?)""",
            (hold.name, hold.reason, hold.criteria.to_json(), hold.created_ms, hold.until_ms),
        )
        con.commit()
    finally:
        con.close()


def release(folder: Path, account: str, name: str, now_ms: int) -> bool:
    """End a hold in force, keeping its record. False if the backup has none by that name."""
    hold = next((h for h in read_holds(folder) if h.name == name and h.active(now_ms)), None)
    if hold is None:
        return False
    con = sidecar.open_meta(folder)
    try:
        chain.append(folder, account, "release", note=hold.note())
        con.execute(
            "UPDATE holds SET released_ms = ? WHERE name = ? AND released_ms IS NULL",
            (now_ms, name),
        )
        con.commit()
    finally:
        con.close()
    return True
