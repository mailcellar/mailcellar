"""
review: one local HTML page per rule, for deciding whether to label what
`offload plan` selected, when its CSV is too long to read.

Each page has what the rule selects, its summary tables, a random sample of the messages it
would offload and of those each protection keeps, the largest messages, the
threads with the most messages in the offload, and attachment types by
size. Senders and subjects only, never bodies; the pages are written to
<data root>/offload/review/ and are meant for no one else.

Everything but attachment_rows() and write_reviews() is pure. Samples are
drawn with a seed from the rule's name, so the same plan shows the same
sample.
"""

import html
import random
import re
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

from mailcellar.offload.plan import Message, RuleResult, summary
from mailcellar.offload.rules import PROTECTIONS, Rule, years_text
from mailcellar.report.output import Table, day, human_size

SAMPLE = 50  # messages of the offload shown at random
KEPT_SAMPLE = 20  # per protection
TOP = 20  # largest messages, busiest threads

MESSAGE_COLUMNS = [
    ("Date", "text"),
    ("Account", "text"),
    ("Sender", "text"),
    ("Subject", "text"),
    ("Size", "size"),
    ("Labels", "text"),
]


# ── decisions ────────────────────────────────────────────────────────────────


def sample(messages: list[Message], n: int, seed: str) -> list[Message]:
    """n of the messages at random (all of them if fewer), oldest first."""
    chosen = messages if len(messages) <= n else random.Random(seed).sample(messages, n)
    return sorted(chosen, key=lambda m: (m.internal_date, m.account, m.gmail_id))


def largest(messages: list[Message], n: int) -> list[Message]:
    return sorted(messages, key=lambda m: (-m.size, m.internal_date, m.gmail_id))[:n]


def busiest_threads(messages: list[Message], n: int) -> list[tuple]:
    """(account, first subject, messages, size, first date, last date) of the n biggest threads."""
    threads: dict[tuple[str, str], list[Message]] = defaultdict(list)
    for m in messages:
        if m.thread_id:
            threads[(m.account, m.thread_id)].append(m)
    ranked = sorted(
        threads.values(),
        key=lambda ms: (-len(ms), -sum(m.size for m in ms), ms[0].internal_date),
    )
    rows = []
    for ms in ranked[:n]:
        ms = sorted(ms, key=lambda m: m.internal_date)
        rows.append(
            (
                ms[0].account,
                ms[0].subject,
                len(ms),
                sum(m.size for m in ms),
                day(ms[0].internal_date),
                day(ms[-1].internal_date),
            )
        )
    return rows


def message_rows(messages: list[Message]) -> list[tuple]:
    return [
        (day(m.internal_date), m.account, m.sender, m.subject, m.size, ", ".join(m.labels))
        for m in messages
    ]


def _keys(scope: dict[str, list]) -> str:
    """Scope keys as a rule writes them: label "work" · sender "@a.com" or "@b.com"."""

    def value(v) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        return str(v) if isinstance(v, int | float) else f'"{v}"'

    return " · ".join(f"{key} " + " or ".join(map(value, vs)) for key, vs in scope.items())


def rule_text(rule: Rule) -> str:
    """What the rule selects, in one line: its scope, cut-off, exception and retention."""
    text = f"{_keys(rule.scope)} · before {day(rule.before_ms)}"
    text += f" · except {_keys(rule.exceptions)}" if rule.exceptions else ""
    if rule.forget_after:
        text += f" · forgotten from the backup {years_text(rule.forget_after)} after its date"
    return text


def kept_reason(protection: str) -> str:
    return "the rule's exception" if protection == "except" else PROTECTIONS[protection]


def review_tables(r: RuleResult, attachments: list[tuple], top: int = TOP) -> list[Table]:
    """The tables of one rule's page, after its summary."""
    sel = r.selected
    shown = sample(sel, SAMPLE, r.rule.name)
    tables = [
        Table(
            "sample",
            f"A random sample of what it would offload ({len(shown):,} of {len(sel):,})",
            MESSAGE_COLUMNS,
            message_rows(shown),
        )
    ]
    for p, kept in r.kept_messages.items():
        some = sample(kept, KEPT_SAMPLE, f"{r.rule.name}\0{p}")
        tables.append(
            Table(
                f"kept-{p}",
                f"Kept: {kept_reason(p)} ({len(some):,} of {len(kept):,} at random)",
                MESSAGE_COLUMNS,
                message_rows(some),
            )
        )
    tables += [
        Table(
            "largest",
            f"The largest messages in the offload (top {top})",
            MESSAGE_COLUMNS,
            message_rows(largest(sel, top)),
        ),
        Table(
            "threads",
            f"Threads with the most messages in the offload (top {top})",
            [
                ("Account", "text"),
                ("Subject", "text"),
                ("Messages", "int"),
                ("Size", "size"),
                ("First", "text"),
                ("Last", "text"),
            ],
            busiest_threads(sel, top),
            notes=["Only the thread's messages in the offload count; others in it stay."],
        ),
        Table(
            "attachments",
            "Attachments in the offload, by type",
            [("Type", "text"), ("Attachments", "int"), ("Size", "size")],
            attachments,
        ),
    ]
    return tables


def _cell(value, kind: str) -> str:
    if value is None:
        return ""
    if kind == "int":
        return f"{value:,}"
    if kind == "size":
        return human_size(value)
    return html.escape(" ".join(str(value).split()))


def table_html(t: Table) -> str:
    right = [kind in ("int", "size", "num", "pct") for _h, kind in t.columns]
    head = "".join(
        f'<th class="{"r" if r else ""}">{html.escape(h)}</th>'
        for (h, _k), r in zip(t.columns, right, strict=True)
    )
    body = "".join(
        "<tr>"
        + "".join(
            f'<td class="{"r" if r else ""}">{_cell(v, kind)}</td>'
            for v, (_h, kind), r in zip(row, t.columns, right, strict=True)
        )
        + "</tr>"
        for row in t.rows
    )
    notes = "".join(f'<p class="note">{html.escape(n)}</p>' for n in t.notes)
    empty = "" if t.rows else '<p class="note">None.</p>'
    table = f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>" if t.rows else ""
    return f"<section><h2>{html.escape(t.title)}</h2>{table}{empty}{notes}</section>"


STYLE = """
:root { --bg: #fff; --fg: #1d1d1f; --muted: #6e6e73; --line: #e3e3e6; --head: #f5f5f7; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #1c1c1e; --fg: #f2f2f7; --muted: #a1a1a6; --line: #3a3a3c; --head: #2c2c2e; }
}
body { background: var(--bg); color: var(--fg); margin: 0 auto; max-width: 72rem;
       padding: 1.5rem 1rem 4rem; font: 14px/1.45 system-ui, sans-serif; }
h1 { font-size: 1.5rem; margin: 0 0 .25rem; }
h2 { font-size: 1.05rem; margin: 2rem 0 .5rem; }
.meta, .note { color: var(--muted); }
.warn { border-left: 3px solid #c93; padding: .25rem .75rem; margin: 1rem 0; }
section { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: .3rem .5rem; border-bottom: 1px solid var(--line);
         vertical-align: top; }
th { background: var(--head); font-weight: 600; white-space: nowrap; }
td.r, th.r { text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
"""


def page(r: RuleResult, accounts: list[str], attachments: list[tuple], generated: str) -> str:
    """One rule's review page, as HTML."""
    tables = summary([r], TOP) + review_tables(r, attachments)
    title = f"Offload review: {r.rule.name}"
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{STYLE}</style></head><body>"
        f"<h1>{html.escape(title)}</h1>"
        f'<p class="meta">Selects: {html.escape(rule_text(r.rule))}</p>'
        f'<p class="meta">{html.escape(", ".join(accounts))} · generated {generated} · '
        f"{len(r.selected):,} messages, {human_size(sum(m.size for m in r.selected))}</p>"
        '<p class="warn">This page shows senders and subjects from your mail. It is for you '
        "alone: keep it on this machine and don't send or publish it.</p>"
        + "".join(table_html(t) for t in tables)
        + "</body></html>\n"
    )


def file_name(rule_name: str) -> str:
    """A file name for a rule's page: its name, lower case, with only letters, digits and -."""
    slug = re.sub(r"[^a-z0-9]+", "-", rule_name.lower()).strip("-")
    return f"{slug or 'rule'}.html"


# ── reading and writing ──────────────────────────────────────────────────────


def attachment_rows(con: sqlite3.Connection, messages: list[Message]) -> list[tuple]:
    """(MIME type, attachments, bytes) of the messages' attachments, largest first."""
    con.execute("DROP TABLE IF EXISTS temp.review_keys")
    con.execute(
        "CREATE TEMP TABLE review_keys (account, gmail_id, PRIMARY KEY (account, gmail_id))"
    )
    con.executemany(
        "INSERT OR IGNORE INTO review_keys VALUES (?, ?)",
        ((m.account, m.gmail_id) for m in messages),
    )
    rows = con.execute(
        """SELECT a.mime_type, count(*), sum(a.size)
           FROM review_keys k JOIN all_attachments a USING (account, gmail_id)
           GROUP BY a.mime_type ORDER BY sum(a.size) DESC, a.mime_type"""
    ).fetchall()
    con.execute("DROP TABLE temp.review_keys")
    return rows


def write_reviews(
    con: sqlite3.Connection, results: list[RuleResult], accounts: list[str], folder: Path
) -> list[Path]:
    """Write each rule's page into `folder`; returns the paths, in rule order."""
    folder.mkdir(parents=True, exist_ok=True)
    generated = time.strftime("%Y-%m-%d %H:%M")
    paths, used = [], set()
    for r in results:
        name = file_name(r.rule.name)
        n = 2
        while name in used:  # two rules whose names differ only in punctuation
            name = f"{file_name(r.rule.name)[:-5]}-{n}.html"
            n += 1
        used.add(name)
        path = folder / name
        path.write_text(
            page(r, accounts, attachment_rows(con, r.selected), generated), encoding="utf-8"
        )
        paths.append(path)
    return paths
