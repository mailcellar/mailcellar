"""
Report sections as tables: rendered as aligned text, or written as one CSV
per section. Column kinds control formatting: text (cut at TEXT_WIDTH in
the text report), full (text never cut, for a value to copy, such as a
hash), int, num (one decimal), size (bytes, shown human-readable), pct
(0–1, shown as a percentage).
"""

import csv
import time
from dataclasses import dataclass, field
from pathlib import Path


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


TEXT_WIDTH = 42  # longest text cell shown in the text report


@dataclass(frozen=True)
class Table:
    name: str  # file name stem for --csv
    title: str
    columns: list[tuple[str, str]]  # (heading, kind)
    rows: list[tuple]
    notes: list[str] = field(default_factory=list)
    limit: bool = True  # obey --top in the text report


def day(ms: int | None) -> str:
    """YYYY-MM-DD (UTC) for ms since the epoch; '' for None."""
    return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000)) if ms is not None else ""


def when(ms: int | None) -> str:
    """YYYY-MM-DD HH:MM (UTC) for ms since the epoch; '' for None."""
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ms / 1000)) if ms is not None else ""


def cell(value, kind: str) -> str:
    if value is None:
        return ""
    if kind == "int":
        return f"{value:,}"
    if kind == "num":
        return f"{value:,.1f}"
    if kind == "size":
        return human_size(value)
    if kind == "pct":
        return f"{value * 100:.1f}%"
    text = " ".join(str(value).split())
    if kind == "full" or len(text) <= TEXT_WIDTH:
        return text
    return text[: TEXT_WIDTH - 1] + "…"


def render(tables: list[Table], top: int, out) -> None:
    for t in tables:
        rows = t.rows[:top] if t.limit else t.rows
        out.write(f"\n{'═' * 78}\n  {t.title}\n{'═' * 78}\n")
        for note in t.notes:
            out.write(f"  {note}\n")
        if t.notes:
            out.write("\n")
        if not rows:
            out.write("  (none)\n")
            continue
        cells = [[cell(v, kind) for v, (_h, kind) in zip(r, t.columns, strict=True)] for r in rows]
        widths = [max(len(h), *(len(c[i]) for c in cells)) for i, (h, _k) in enumerate(t.columns)]
        right = [kind not in ("text", "full") for _h, kind in t.columns]
        out.write("  " + _line([h for h, _k in t.columns], widths, right) + "\n")
        out.write("  " + _line(["-" * w for w in widths], widths, right) + "\n")
        for c in cells:
            out.write("  " + _line(c, widths, right) + "\n")
        if t.limit and len(t.rows) > len(rows):
            out.write(f"  … {len(t.rows) - len(rows):,} more (all rows with --csv)\n")


def _line(values: list[str], widths: list[int], right: list[bool]) -> str:
    return "  ".join(
        v.rjust(w) if r else v.ljust(w) for v, w, r in zip(values, widths, right, strict=True)
    ).rstrip()


def write_csv(tables: list[Table], directory: Path, prefix: str) -> list[Path]:
    """One CSV per table, all rows, raw values (bytes, fractions)."""
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for t in tables:
        path = directory / f"{prefix}-{t.name}.csv"
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow([h for h, _k in t.columns])
            w.writerows(t.rows)
        paths.append(path)
    return paths
