"""
progress: how far a long run has got, how fast it goes, and how long is left.

    with Progress("alex@example.com: reading Gmail", len(ids)) as p:
        for item in items:
            ...
            p.advance()

shows, once the run has taken DELAY seconds:

    alex@example.com: reading Gmail: 6,000 of 17,664 (34%), 52/s, 1:55 so far, about 3:45 left

On a terminal the line is rewritten in place (at most every REDRAW
seconds); otherwise (a scheduled run, output piped to a file) it is logged
every LOG_EVERY seconds. Either way it goes through logging's INFO level, so
-q hides it, and the run's last state is shown when it ends. A run quicker
than DELAY shows nothing.

The innermost open Progress is `current`: GmailAccount's batches advance it
once per message, so wrapping a Gmail call in a Progress is all it takes,
and its rate-limit waits are reported with waited(): the rate and the time
left go by time spent working, so a 30 s wait doesn't read as "1/min".
cli's stderr handler calls clear_line() before each log record, so a
warning or a rate-limit wait gets its own line and the progress line
returns at the next update.

describe() and due() decide what to show and when, from plain values; the
Progress class only keeps the counts and writes.
"""

import logging
import shutil
import sys
import time
from collections import deque

logger = logging.getLogger(__name__)

DELAY = 2.0  # seconds before anything is shown
REDRAW = 0.25  # seconds between rewrites of a terminal line
LOG_EVERY = 60.0  # seconds between log lines when not on a terminal
WINDOW = 30.0  # seconds of recent progress the rate is measured over
# Seconds of work needed before a rate is given: Gmail answers in batches, so the
# first seconds are bursty (3 s gave estimates like "2:09:53 left").
MIN_SPAN = 10.0


# ── decisions ────────────────────────────────────────────────────────────────


def clock(seconds: float) -> str:
    """1:05, or 1:02:03 from an hour."""
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def speed(per_second: float) -> str:
    """52/s; 40/min below one a second."""
    if per_second >= 1:
        return f"{per_second:,.0f}/s"
    return f"{per_second * 60:,.0f}/min"


def describe(
    what: str, done: int, total: int, elapsed: float, rate: float | None, final: bool = False
) -> str:
    """
    The line for a run `elapsed` seconds in, at `rate` per second lately
    (None: not measured yet). Pure.
    """
    if final:
        return f"{what}: {done:,} of {total:,} in {clock(elapsed)}"
    pct = f" ({done * 100 // total}%)" if total else ""
    parts = [f"{what}: {done:,} of {total:,}{pct}"]
    if rate:
        parts.append(speed(rate))
    parts.append(f"{clock(elapsed)} so far")
    if rate and total > done:
        parts.append(f"about {clock((total - done) / rate)} left")
    return ", ".join(parts)


def due(now: float, started: float, last_shown: float | None, terminal: bool) -> bool:
    """Whether to show the line now. Pure."""
    if now - started < DELAY:
        return False
    if last_shown is None:
        return True
    return now - last_shown >= (REDRAW if terminal else LOG_EVERY)


def recent_rate(samples) -> float | None:
    """Items a second over (time, done) samples, oldest first; None over less than MIN_SPAN."""
    if len(samples) < 2:
        return None
    (t0, d0), (t1, d1) = samples[0], samples[-1]
    if t1 - t0 < MIN_SPAN:
        return None
    return (d1 - d0) / (t1 - t0)


# ── output ───────────────────────────────────────────────────────────────────

current: list[Progress] = []  # open ones, innermost last
_open_line = False  # a progress line is on the terminal without a newline


def advance(n: int = 1) -> None:
    """Advance the innermost open Progress, if any."""
    if current:
        current[-1].advance(n)


def waited(seconds: float) -> None:
    """Tell the innermost open Progress that this much time went on waiting, not working."""
    if current:
        current[-1].waited(seconds)


def clear_line() -> None:
    """Rub out a progress line on the terminal, so other output starts clean."""
    global _open_line
    if _open_line:
        sys.stderr.write("\r\x1b[K")
        sys.stderr.flush()
        _open_line = False


class Progress:
    """A long run's counts, shown as describe() lines when due()."""

    def __init__(self, what: str, total: int, *, now=time.monotonic, stream=None):
        self.what, self.total, self.done = what, total, 0
        self._now = now
        self._stream = stream
        self.started = now()
        self._last_shown: float | None = None
        self._waited = 0.0  # seconds spent waiting (rate limits), left out of the rate
        self._samples: deque = deque([(self.started, 0)])  # (working time, done)

    @property
    def stream(self):
        return self._stream or sys.stderr  # whatever stderr is now (tests capture it)

    @property
    def terminal(self) -> bool:
        isatty = getattr(self.stream, "isatty", None)
        return bool(isatty and isatty())

    def __enter__(self):
        current.append(self)
        return self

    def __exit__(self, *exc) -> None:
        current.remove(self)
        self.finish()

    def advance(self, n: int = 1) -> None:
        self.update(self.done + n)

    def waited(self, seconds: float) -> None:
        self._waited += seconds

    def update(self, done: int) -> None:
        now = self._now()
        self.done = done
        working = now - self._waited
        self._samples.append((working, done))
        while len(self._samples) > 2 and working - self._samples[1][0] >= WINDOW:
            self._samples.popleft()
        if due(now, self.started, self._last_shown, self.terminal):
            self._show(self._line(now))
            self._last_shown = now

    def finish(self) -> None:
        """Show the last state, if anything was shown; end a terminal line."""
        global _open_line
        if self._last_shown is None:
            return
        now = self._now()
        self._show(self._line(now, final=self.done >= self.total))
        if self.terminal and _open_line:
            self.stream.write("\n")
            self.stream.flush()
            _open_line = False

    def _line(self, now: float, final: bool = False) -> str:
        rate = recent_rate(self._samples)
        return describe(self.what, self.done, self.total, now - self.started, rate, final)

    def _show(self, line: str) -> None:
        global _open_line
        if not logger.isEnabledFor(logging.INFO):
            return
        if not self.terminal:
            logger.info(line)
            return
        width = shutil.get_terminal_size().columns - 1  # a wrapped line can't be rewritten
        self.stream.write("\r" + line[:width] + "\x1b[K")
        self.stream.flush()
        _open_line = True
