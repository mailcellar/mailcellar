"""progress: the line for a long run, when it is shown, and where it goes."""

import io
import logging
import sys

import pytest

from mailcellar import gmailapi, progress
from mailcellar.cli import configure_logging
from mailcellar.progress import Progress, clock, describe, due, recent_rate, speed
from test_backup import ACCOUNT, ConcurrencyLimitedService

# ── what a line says ─────────────────────────────────────────────────────────


def test_a_line_says_done_of_total_rate_and_time_so_far_and_left():
    line = describe("work: reading", 6_000, 17_664, 115, 52.0)
    assert line == "work: reading: 6,000 of 17,664 (33%), 52/s, 1:55 so far, about 3:44 left"


def test_without_a_rate_yet_there_is_no_estimate():
    assert describe("Parsing", 10, 100, 2.5, None) == "Parsing: 10 of 100 (10%), 0:02 so far"


def test_the_last_line_says_how_long_it_took():
    assert describe("Exporting", 900, 900, 3_723, 4.0, final=True) == (
        "Exporting: 900 of 900 in 1:02:03"
    )


def test_clock_and_speed():
    assert [clock(s) for s in (0, 65, 3_600, 3_723.9)] == ["0:00", "1:05", "1:00:00", "1:02:03"]
    assert [speed(r) for r in (1_234.4, 4.0, 0.5)] == ["1,234/s", "4/s", "30/min"]
    assert describe("x", 0, 0, 1, None) == "x: 0 of 0, 0:01 so far"  # nothing to divide by


# ── when it is shown ─────────────────────────────────────────────────────────


def test_nothing_is_shown_for_a_quick_run_then_redrawn_or_logged_at_its_pace():
    start = 100.0
    assert not due(start + progress.DELAY - 0.1, start, None, terminal=True)
    assert due(start + progress.DELAY, start, None, terminal=True)
    shown = start + 5
    assert not due(shown + progress.REDRAW / 2, start, shown, terminal=True)
    assert due(shown + progress.REDRAW, start, shown, terminal=True)
    assert not due(shown + progress.LOG_EVERY - 1, start, shown, terminal=False)
    assert due(shown + progress.LOG_EVERY, start, shown, terminal=False)


def test_the_rate_needs_a_few_seconds_of_samples():
    assert recent_rate([(0, 0)]) is None
    assert recent_rate([(0, 0), (progress.MIN_SPAN / 2, 10)]) is None
    assert recent_rate([(0, 0), (10, 500)]) == 50


# ── a run, on a terminal or not ──────────────────────────────────────────────


class Clock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now


class Terminal(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def terminal(monkeypatch):
    """
    Call it in the test to make stderr a terminal, shared by log records and
    the progress line (pytest sets stderr again between fixtures and the test).
    """
    monkeypatch.setattr(progress, "_open_line", False)

    def install():
        out = Terminal()
        monkeypatch.setattr(sys, "stderr", out)
        return out

    return install


def test_on_a_terminal_the_line_is_rewritten_and_ended_with_the_last_state(terminal):
    terminal = terminal()
    now = Clock()
    with Progress("Downloading", 100, now=now) as p:
        p.advance(10)  # under DELAY: nothing yet
        assert terminal.getvalue() == ""
        now.now += 3
        p.advance(20)
        now.now += 1
        p.advance(70)
    out = terminal.getvalue()
    assert out.count("\r") == 3 and out.endswith("\rDownloading: 100 of 100 in 0:04\x1b[K\n")
    assert "\rDownloading: 30 of 100 (30%), 10/s, 0:03 so far, about 0:07 left\x1b[K" in out
    assert progress.current == []


def test_a_run_stopped_part_way_ends_on_its_last_state(terminal):
    terminal = terminal()
    now = Clock()
    with pytest.raises(RuntimeError), Progress("Downloading", 100, now=now) as p:
        now.now += 3
        p.advance(30)
        raise RuntimeError("interrupted")
    assert terminal.getvalue().endswith("0:03 so far, about 0:07 left\x1b[K\n")
    assert progress.current == []


def test_a_log_record_gets_a_line_of_its_own(terminal):
    terminal = terminal()
    configure_logging()
    now = Clock()
    with Progress("Downloading", 100, now=now) as p:
        now.now += 3
        p.advance(30)
        logging.getLogger("mailcellar.test").warning("a rate-limit wait")
        now.now += 1
        p.advance(10)
    out = terminal.getvalue()
    assert "0:07 left\x1b[K\r\x1b[Kwarning: a rate-limit wait\n\rDownloading: 40" in out


def test_off_a_terminal_it_is_logged_now_and_then(capsys):
    now = Clock()
    with Progress("Checking", 1_000, now=now) as p:
        for _ in range(10):
            now.now += 20
            p.advance(100)
    lines = capsys.readouterr().err.splitlines()
    # shown at 20 s, then every LOG_EVERY (60 s): 80 s, 140 s, 200 s, and the end
    assert [line.split(": ")[1].split(" (")[0] for line in lines] == [
        "100 of 1,000",
        "400 of 1,000",
        "700 of 1,000",
        "1,000 of 1,000",
        "1,000 of 1,000 in 3:20",
    ]
    assert "5/s" in lines[1] and "about 2:00 left" in lines[1]


def test_quick_runs_and_quiet_runs_show_nothing(terminal):
    terminal = terminal()
    with Progress("Quick", 5) as p:
        p.advance(5)
    configure_logging(logging.WARNING)  # -q
    now = Clock()
    with Progress("Quiet", 5, now=now) as p:
        now.now += 10
        p.advance(5)
    assert terminal.getvalue() == ""


def test_the_estimate_follows_the_recent_rate(capsys):
    now = Clock()
    with Progress("Trashing", 10_000, now=now) as p:
        for _ in range(60):  # a slow minute: 1 a second
            now.now += 1
            p.advance(1)
        for _ in range(30):  # then 10 a second
            now.now += 1
            p.advance(10)
        assert progress.recent_rate(p._samples) == pytest.approx(10, rel=0.1)


# ── Gmail's batches advance the open Progress ────────────────────────────────


def test_gmail_batches_count_each_message_once_however_often_it_was_retried():
    service = ConcurrencyLimitedService(limit=8)  # many 429s, each retried
    gmail = gmailapi.GmailAccount(ACCOUNT, None, service=service, sleep=lambda s: None)
    ids = [f"g{i}" for i in range(300)]
    with Progress("Reading", len(ids)) as p:
        gmail.message_details(ids)
    assert p.done == 300
    gmail.message_details(ids[:10])  # no Progress open: nothing to advance, no error


def test_the_innermost_progress_is_the_one_advanced():
    with Progress("outer", 10) as outer:
        with Progress("inner", 10) as inner:
            progress.advance(3)
        progress.advance()
    assert (outer.done, inner.done) == (1, 3)
