"""
protections: for each message, the protections `offload plan` keeps by,
for tools outside mailcellar that need to know which mail a rule would
leave alone.

The protections are offload's keep names (offload.rules.KEEP), derived as
offload plan derives them, across the accounts loaded (offload.plan.prepare):
your addresses, the threads you wrote in, the messages you replied to and
the addresses you wrote to come from all of them. With no rule, labelled is
any user label but offload's and dedup's review labels. left_out says why
offload plan would never select the message (offload.plan.EXCLUSIONS), the
first reason when several apply.

Each message is named by account and id, and by a hash of its Message-ID
(message_id_hash) for joining with mail read from elsewhere: the first 16
hex digits of SHA-256 of the Message-ID as message.normalise_message_id
gives it (bare, no whitespace, lower case); empty when it has none. No
thread id, address or subject is written.
"""

import hashlib
import sqlite3
from collections.abc import Sequence

from mailcellar.offload.plan import EXCLUSIONS, prepare
from mailcellar.offload.rules import DEFAULT_KEEP, KEEP
from mailcellar.report.output import Table

NAMES = tuple(KEEP)  # mine, starred, conversations, labelled, correspondents
LEFT_OUT = tuple(e for e in EXCLUSIONS if e != "earlier")  # "earlier" needs rules


def _column(protection: str) -> str:
    """A protection flag as SQL over pm joined to bm. labels: any user label (no rule)."""
    return "bm.user_labelled" if protection == "labels" else f"pm.{protection}"


def message_id_hash(message_id: str) -> str:
    """16 hex digits of SHA-256 of a normalised Message-ID; '' for none. Pure."""
    return hashlib.sha256(message_id.encode()).hexdigest()[:16] if message_id else ""


def left_out(flags: Sequence[bool]) -> str:
    """The first LEFT_OUT reason whose flag (in LEFT_OUT order) is set; '' if none. Pure."""
    return next((e for e, on in zip(LEFT_OUT, flags, strict=True) if on), "")


def message_rows(con: sqlite3.Connection, accounts: list[str]) -> list[tuple]:
    """
    (account, id, message_id_hash, one 0/1 per NAMES, left_out) for every
    message, by account and date.
    """
    prepare(con, accounts, {})
    keeps = [" OR ".join(_column(p) for p in KEEP[k]) for k in NAMES]
    sql = f"""SELECT pm.account, pm.gmail_id, bm.message_id,
                     {", ".join(f"({k}) AS k{i}" for i, k in enumerate(keeps))},
                     {", ".join(f"pm.{e}" for e in LEFT_OUT)}
              FROM pm JOIN bm USING (account, gmail_id)
              ORDER BY pm.account, pm.internal_date, pm.gmail_id"""
    n = len(NAMES)
    return [
        (
            account,
            gid,
            message_id_hash(mid or ""),
            *(int(bool(v)) for v in flags[:n]),
            left_out([bool(v) for v in flags[n:]]),
        )
        for account, gid, mid, *flags in con.execute(sql)
    ]


def summary_rows(rows: Sequence[tuple]) -> list[tuple]:
    """
    Per account, in order of first appearance: messages, each protection's
    count, how many the default keep protects, and how many are left out. Pure.
    """
    default = [NAMES.index(k) for k in DEFAULT_KEEP]
    out: dict[str, list[int]] = {}
    for account, _gid, _hash, *rest in rows:
        flags, reason = rest[: len(NAMES)], rest[len(NAMES)]
        counts = out.setdefault(account, [0] * (len(NAMES) + 3))
        counts[0] += 1
        for i, f in enumerate(flags):
            counts[1 + i] += f
        counts[-2] += any(flags[i] for i in default)
        counts[-1] += bool(reason)
    return [(account, *counts) for account, counts in out.items()]


def build(con: sqlite3.Connection, accounts: list[str]) -> list[Table]:
    rows = message_rows(con, accounts)
    return [
        Table(
            "summary",
            "Protections per account",
            [("Account", "text"), ("Messages", "int")]
            + [(k.capitalize(), "int") for k in NAMES]
            + [("Default keep", "int"), ("Left out", "int")],
            summary_rows(rows),
            notes=[
                "Messages each of offload's keep names protects, worked out across the "
                "accounts named (default: all), as 'offload plan' does. Labelled: any user "
                "label. Default keep: protected by mine, starred, conversations or labelled. "
                "Left out: offload plan would never select it (already offloaded, in Trash "
                "or Spam, gone from its mailbox, or not in the backup index)."
            ],
            limit=False,
        ),
        Table(
            "messages",
            "Protections per message",
            [("account", "text"), ("id", "full"), ("message_id_hash", "full")]
            + [(k, "int") for k in NAMES]
            + [("left_out", "text")],
            rows,
            notes=[
                "One row per message; all of them with --csv. message_id_hash: the first 16 "
                "hex digits of SHA-256 of the Message-ID, bare and lower-cased; empty when "
                "there is none. left_out: offloaded, trash (Trash or Spam), gone or "
                "unindexed; empty when offload plan could select it."
            ],
        ),
    ]
