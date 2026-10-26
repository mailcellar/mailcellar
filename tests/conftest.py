"""Shared test setup."""

import sqlite3
import sys
from pathlib import Path

import pytest

from mailcellar import gmailapi
from mailcellar.cli import configure_logging
from mailcellar.paths import CONFIG_ENV, DATA_ENV

TESTS = Path(__file__).parent


@pytest.fixture(autouse=True)
def default_roots(monkeypatch, tmp_path):
    """
    Every test uses ./data and ./config under its working directory, whatever
    the developer's $MAILCELLAR_DATA, $MAILCELLAR_CONFIG or
    ~/.config/mailcellar say: a test must never reach real backups or tokens.
    A test that unsets them falls back to the defaults under ~, so ~ is the
    test's own folder too, never the developer's home with ~/mailcellar in it.
    """
    monkeypatch.setenv(DATA_ENV, "data")
    monkeypatch.setenv(CONFIG_ENV, "config")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    configure_logging()  # as `mailcellar` does; tests calling a command's main see its log
    monkeypatch.setattr(gmailapi, "_held", {})  # modify tokens held by one "run" stay in it


@pytest.fixture(autouse=True)
def close_test_connections(monkeypatch):
    """
    Close the SQLite connections a test opens itself (to inspect a database)
    when it ends. Connections the code under test opens are left alone: it
    must close them, and pyproject.toml makes a leaked one fail the test.
    """
    opened = []
    connect = sqlite3.connect

    def tracked(*args, **kwargs):
        con = connect(*args, **kwargs)
        if Path(sys._getframe(1).f_code.co_filename).parent == TESTS:
            opened.append(con)
        return con

    monkeypatch.setattr(sqlite3, "connect", tracked)
    yield
    for con in opened:
        con.close()
