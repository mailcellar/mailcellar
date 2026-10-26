"""
query: what a search finds, and how a result reads.

A query is words, all of which must be found, in the sender, recipients,
subject or body, whole words with diacritics ignored:

    invoice 2024            both words
    "quarterly figures"     the phrase
    from:alex to:example.org subject:invoice     in that field only
    invoice OR receipt      either word
    -newsletter             without the word
    report*                 words starting with "report"

It is translated to SQLite FTS5's syntax by fts_query(), so punctuation in a
word (an address, a hyphen) never breaks the query.
"""

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

FIELDS = {"from": "sender", "to": "recipients", "subject": "subject"}
_TERM_RE = re.compile(r'(-?)(?:(from|to|subject):)?("[^"]*"?|[^\s"]+)', re.I)


class QueryError(ValueError):
    pass


# ── decisions ────────────────────────────────────────────────────────────────


def _phrase(value: str, column: str) -> str | None:
    """One word or quoted phrase as an FTS5 phrase; None if it has no letters or digits."""
    quoted = value.startswith('"')
    inner = value.strip('"')
    prefix = not quoted and len(inner) > 1 and inner.endswith("*")
    inner = inner.rstrip("*") if prefix else inner
    if not re.search(r"\w", inner):
        return None
    phrase = '"' + inner.replace('"', '""') + '"' + (" *" if prefix else "")
    return f"{column} : {phrase}" if column else phrase


def fts_query(query: str) -> str:
    """The FTS5 MATCH expression for a query (see the module docstring). Raises QueryError."""
    query = query.replace("\x00", " ")  # FTS5 reads a NUL as the end of the query
    groups: list[list[str]] = []  # every group must match; any phrase within one will do
    excluded: list[str] = []
    join = False
    for m in _TERM_RE.finditer(query):
        negated, field, value = m.groups()
        if not negated and not field and value == "OR":
            join = bool(groups)
            continue
        phrase = _phrase(value, FIELDS[field.lower()] if field else "")
        if phrase is None:
            join = False
        elif negated:
            excluded.append(phrase)
            join = False
        elif join:
            groups[-1].append(phrase)
            join = False
        else:
            groups.append([phrase])
    if not groups:
        raise QueryError(
            'say what to look for: a word, a "phrase", or from:, to: or subject: and a word'
            + (" (a query can't only leave words out)" if excluded else "")
        )
    expr = " AND ".join(g[0] if len(g) == 1 else "(" + " OR ".join(g) + ")" for g in groups)
    if excluded:
        expr = f"({expr}) NOT ({' OR '.join(excluded)})"
    return expr


@dataclass(frozen=True)
class Hit:
    account: str
    gmail_id: str
    path: str  # relative to the account's backup folder
    internal_date: int  # ms UTC
    sender: str
    subject: str


@dataclass(frozen=True)
class Offload:
    """An offloaded message's batch and rule (not restored since)."""

    batch: str
    rule: str


def keep(
    hits: list[Hit],
    offloads: dict[tuple[str, str], Offload],
    offloaded_only: bool = False,
    rules=(),
    batches=(),
) -> list[Hit]:
    """
    The hits that are offloaded, when asked; by rule and by batch (any of
    each), which implies offloaded. Pure.
    """
    if not (offloaded_only or rules or batches):
        return list(hits)
    out = []
    for h in hits:
        o = offloads.get((h.account, h.gmail_id))
        if o is None or (rules and o.rule not in rules) or (batches and o.batch not in batches):
            continue
        out.append(h)
    return out


def _date(hit: Hit) -> str:
    return datetime.fromtimestamp(hit.internal_date / 1000, UTC).strftime("%Y-%m-%d")


def _path(hit: Hit, folder: Path | None) -> Path:
    return folder / hit.path if folder else Path(hit.path)


def render(hit: Hit, folder: Path | None, offload: Offload | None) -> list[str]:
    """A result's lines: date and sender; subject; file; if offloaded, how to restore it."""
    date, path = _date(hit), _path(hit, folder)
    lines = [
        f"{date}  {hit.sender or '(no sender)'}",
        f"            {hit.subject or '(no subject)'}",
        f"            {path}",
    ]
    if offload:
        lines.append(
            f"            offloaded ({offload.rule}, batch {offload.batch}); bring it back with: "
            f"mailcellar restore {hit.account} --id {hit.gmail_id}"
        )
    return lines


def render_tsv(hit: Hit, folder: Path | None, offload: Offload | None) -> str:
    """
    A result as one tab-separated line: date, sender, subject, account, Gmail
    ID, file, offload rule, offload batch (both empty if not offloaded). Tabs
    and line breaks in the sender and subject become spaces.
    """
    fields = [
        _date(hit),
        re.sub(r"\s", " ", hit.sender),
        re.sub(r"\s", " ", hit.subject),
        hit.account,
        hit.gmail_id,
        str(_path(hit, folder)),
        offload.rule if offload else "",
        offload.batch if offload else "",
    ]
    return "\t".join(fields)


def render_json(hit: Hit, folder: Path | None, offload: Offload | None) -> str:
    """A result as one JSON object on one line, the same fields as render_tsv()."""
    return json.dumps(
        {
            "date": _date(hit),
            "sender": hit.sender,
            "subject": hit.subject,
            "account": hit.account,
            "gmail_id": hit.gmail_id,
            "path": str(_path(hit, folder)),
            "offloaded": {"rule": offload.rule, "batch": offload.batch} if offload else None,
        },
        ensure_ascii=False,
    )


# ── reading ──────────────────────────────────────────────────────────────────


def find(
    con: sqlite3.Connection,
    match: str,
    accounts: list[str],
    after_ms: int | None = None,
    before_ms: int | None = None,
) -> list[Hit]:
    """Every indexed message of the accounts that matches, newest first."""
    sql = [
        """SELECT d.account, d.gmail_id, d.path, d.internal_date, d.sender, d.subject
           FROM text JOIN docs d ON d.doc = text.rowid
           WHERE text MATCH ?""",
        f"AND d.account IN ({', '.join('?' * len(accounts))})",
    ]
    params: list = [match, *accounts]
    if after_ms is not None:
        sql.append("AND d.internal_date >= ?")
        params.append(after_ms)
    if before_ms is not None:
        sql.append("AND d.internal_date < ?")
        params.append(before_ms)
    sql.append("ORDER BY d.internal_date DESC, d.gmail_id")
    return [Hit(*row) for row in con.execute(" ".join(sql), params)]
