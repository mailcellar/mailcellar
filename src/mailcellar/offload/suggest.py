"""
suggest: draft offload rules from what the bulk report finds, for
`offload suggest` to plan and print as TOML.

Per account, at most four rules, each a list you can cut down:

  bulk lists     mailing lists report bulk calls candidates: bulk, at least
                 min_messages messages, never engaged with, no user label
  bulk senders   senders (relayed mail: the original sender) of the same kind,
                 their bulk mail only (bulk = true)
  notifications  senders whose address looks automated (no-reply@, alerts@,
                 ...) with as many messages, not bulk, never engaged with,
                 no user label
  large mail     messages over larger_than_mb, when the account has any

Every rule keeps keep's default and has no forget_after; bulk rules carry
a commented `# forget_after = 2` as a hint only. read_sources() and
read_large() read the backups; decide() and render() are pure. Nothing
here changes anything.
"""

import re
import sqlite3
from dataclasses import dataclass, replace

from mailcellar.offload.rules import toml_value
from mailcellar.report import bulk
from mailcellar.report.output import day, human_size

# A sender's local part that names a system, never a person: mail from it is sent
# by one. Words a person or a small team might answer from (info, hello, team,
# admin, accounts) are left out: a person suggested here has nothing else to keep
# their mail, as they were never engaged with.
AUTOMATED = re.compile(
    r"^(no-?reply|do-?not-?reply|donotreply|notifications?|notify|alerts?|updates?|"
    r"news(letter)?s?|mailer(-daemon)?|bounces?|support|service|system|automated|"
    r"billing|receipts?|orders?|messages?)([._+-].*)?$"
)
FORGET_HINT = "# forget_after = 2   # a hint: also delete it from the backup 2 years on"


@dataclass(frozen=True)
class Source:
    """A source report bulk found: a List-Id, or a sender address."""

    account: str
    source: str
    kind: str  # list, sender or relayed
    messages: int
    size: int
    last_ms: int
    bulk: bool


@dataclass(frozen=True)
class Suggestion:
    name: str
    why: str  # one line: what it selects and why
    raw: dict  # the [[rule]] table
    notes: tuple[tuple[str, str], ...] = ()  # (value, comment) for the list it selects by
    hint: str = ""  # a commented line after the rule


def automated(address: str) -> bool:
    """Whether an address's local part looks like a system's, not a person's. Pure."""
    return bool(AUTOMATED.match(address.rsplit("@", 1)[0].lower()))


def read_sources(con: sqlite3.Connection, accounts: list[str], min_messages: int) -> list[Source]:
    """
    Report bulk's sources with at least min_messages messages, never engaged
    with and without a user label: the ones decide() may suggest.
    """
    bulk.prepare_sources(con, accounts, bulk.Options(min_messages=min_messages), 0)
    return [
        Source(*row[:6], bool(row[6]))
        for row in con.execute(
            """SELECT account, source, kind, messages, size, last_ms, bulk FROM sources
               WHERE messages >= ? AND NOT engaged AND NOT user_labelled
               ORDER BY account, size DESC, source""",
            (min_messages,),
        )
    ]


def read_large(
    con: sqlite3.Connection, accounts: list[str], larger_than_mb: float, before_ms: int
) -> dict[str, int]:
    """account → messages over larger_than_mb received before before_ms."""
    return dict(
        con.execute(
            f"""SELECT account, count(*) FROM all_messages
                WHERE coalesce(size_estimate, 0) > ? AND internal_date < ?
                  AND account IN ({bulk.sql_list(accounts)})
                GROUP BY account""",
            (int(larger_than_mb * 1024 * 1024), before_ms),
        ).fetchall()
    )


def _note(s: Source) -> str:
    return f"{s.messages:,} messages in all, {human_size(s.size)}, last {day(s.last_ms)}"


def decide(
    sources: list[Source],
    large: dict[str, int],
    before: str,
    larger_than_mb: float,
    taken_names: set[str] = frozenset(),
) -> list[Suggestion]:
    """The rules to suggest, in the order to plan them. Pure."""
    out: list[Suggestion] = []
    names = set(taken_names)

    def name(text: str) -> str:
        n, unique = 2, text
        while unique in names:
            unique, n = f"{text} {n}", n + 1
        names.add(unique)
        return unique

    for account in sorted({s.account for s in sources} | set(large)):
        mine = [s for s in sources if s.account == account]
        lists = [s for s in mine if s.bulk and s.kind == "list"]
        senders = [s for s in mine if s.bulk and s.kind != "list"]
        notices = [s for s in mine if not s.bulk and s.kind != "list" and automated(s.source)]
        for kind, chosen, key, extra, why in (
            ("bulk lists", lists, "list", {}, "mailing lists you never engaged with"),
            (
                "bulk senders",
                senders,
                "sender",
                {"bulk": True},
                "bulk mail from senders you never engaged with",
            ),
            (
                "notifications",
                notices,
                "sender",
                {},
                "automated senders you never engaged with, not sent as bulk",
            ),
        ):
            if not chosen:
                continue
            out.append(
                Suggestion(
                    name(f"{kind} ({account})"),
                    f"{why}, no user labels, {len(chosen)} of them",
                    {
                        "name": "",
                        "account": account,
                        key: [s.source for s in chosen],
                        **extra,
                        "before": before,
                    },
                    tuple((s.source, _note(s)) for s in chosen),
                    FORGET_HINT if kind != "notifications" else "",
                )
            )
        if large.get(account):
            out.append(
                Suggestion(
                    name(f"large mail ({account})"),
                    f"messages over {larger_than_mb:g} MB: {large[account]:,} before the cut-off, "
                    "before the protections",
                    {
                        "name": "",
                        "account": account,
                        "larger_than_mb": larger_than_mb,
                        "before": before,
                    },
                )
            )
    return [replace(s, raw={**s.raw, "name": s.name}) for s in out]  # name first, as written


def render(s: Suggestion, takes: str) -> str:
    """
    A suggestion as a [[rule]] table, with a comment on what it takes and
    why, and its list of sources one a line, each with its figures. Pure.
    """
    notes = dict(s.notes)
    lines = [f"# {s.why}", f"# takes {takes}", "[[rule]]"]
    for key, value in s.raw.items():
        if isinstance(value, list) and notes:
            lines.append(f"{key} = [")
            lines += [
                f"    {toml_value(v)},  # {notes[v]}" if v in notes else f"    {toml_value(v)},"
                for v in value
            ]
            lines.append("]")
        else:
            lines.append(f"{key} = {toml_value(value)}")
    if s.hint:
        lines.append(s.hint)
    return "\n".join(lines) + "\n"
