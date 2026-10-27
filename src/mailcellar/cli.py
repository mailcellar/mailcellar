"""
mailcellar: one command for the Gmail backups and what is done with them.

  mailcellar setup   [ADDRESS] [--client FILE]   add an account from a downloaded OAuth client
  mailcellar backup  [ACCOUNT ...] [--full | --search Q | --refresh-labels] [--prune]
  mailcellar count   [ACCOUNT ...]      compare Gmail with the backup
  mailcellar meta    [ACCOUNT ...]      fill meta.sqlite for older mail
  mailcellar verify  [ACCOUNT ...] [--offloaded] [--sample N]   check the backup's files
  mailcellar schedule install | status | remove   run backups regularly, unattended
  mailcellar auth    [ACCOUNT ...] --scope readonly|modify [--renew]   authorise or renew access
  mailcellar dedup   index | match | plan | apply
  mailcellar offload plan | apply       take old mail out of Gmail, keep it here
  mailcellar restore ACCOUNT --batch B | --rule R | --id G | --offloaded | --all
                     | --from-pruned B | --from-mbox F | --from-folder D
                     [--to ACCOUNT] [--yes]   put backed-up mail back into Gmail
  mailcellar export  ACCOUNT <selection> --to FILE.mbox|FILE.zip   copy mail out, read-only
  mailcellar hold    add | list | show | release   legal holds: mail nothing may remove
  mailcellar search  QUERY [ACCOUNT ...] [--offloaded]   find mail in the backups
  mailcellar report  storage | bulk | volume | threads | labels | correspondents
                     | health | senders
  mailcellar config  where the config and data live; each account's state

backup, count and meta take every account in accounts.toml unless given
some. Each command takes --help. Anywhere on the line, -v also shows
Gmail's retries and waits, and -q shows only warnings and errors. The
work is done by the modules each command dispatches to (backup/, dedup/,
offload/, restore/, search/, report/, setup/, hold/).
"""

import difflib
import logging
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

SYNOPSIS, FOOTER = __doc__.split("\n\n")[1:3]


def _backup_command(name: str):
    def run(argv: list[str]) -> int:
        from mailcellar.backup import commands

        return commands.main([name, *argv], prog="mailcellar")

    return run


def _setup(argv: list[str]) -> int:
    from mailcellar.setup.cli import main

    return main(argv, prog="mailcellar setup")


def _hold(argv: list[str]) -> int:
    from mailcellar.hold.cli import main

    return main(argv, prog="mailcellar hold")


def _verify(argv: list[str]) -> int:
    from mailcellar.backup.verify import main

    return main(argv, prog="mailcellar verify")


def _schedule(argv: list[str]) -> int:
    from mailcellar.backup.schedule import main

    return main(argv, prog="mailcellar schedule")


def _auth(argv: list[str]) -> int:
    from mailcellar.dedup.cli import main

    main(["auth", *argv], prog="mailcellar")
    return 0


def _dedup(argv: list[str]) -> int:
    from mailcellar.dedup.cli import main

    main(argv, prog="mailcellar dedup")
    return 0


def _offload(argv: list[str]) -> int:
    from mailcellar.offload.cli import main

    main(argv, prog="mailcellar offload")
    return 0


def _restore(argv: list[str]) -> int:
    from mailcellar.restore.cli import main

    return main(argv, prog="mailcellar restore")


def _export(argv: list[str]) -> int:
    from mailcellar.restore.export import main

    return main(argv, prog="mailcellar export")


def _search(argv: list[str]) -> int:
    from mailcellar.search.cli import main

    return main(argv, prog="mailcellar search")


def _report(argv: list[str]) -> int:
    from mailcellar.report.cli import main

    main(argv, prog="mailcellar report")
    return 0


def _home(path: Path) -> str:
    home = str(Path.home())
    text = str(path)
    return "~" + text[len(home) :] if text == home or text.startswith(home + "/") else text


def _config(argv: list[str]) -> int:
    """Where the config and data live, and each account's state. Never prints a secret."""
    import argparse

    from mailcellar import gmailapi
    from mailcellar.backup import sidecar
    from mailcellar.backup.gyb import backup_account
    from mailcellar.paths import (
        SETTINGS_NAME,
        config_setting,
        data_root,
        data_setting,
        find_backups,
    )
    from mailcellar.report.output import Table, render, when

    argparse.ArgumentParser(
        prog="mailcellar config",
        description="Show where the config and data live, and each account's state. "
        "Changes nothing; prints no credentials.",
    ).parse_args(argv)

    config, config_from = config_setting()
    _value, data_from = data_setting()
    data = data_root()
    settings = config / SETTINGS_NAME
    paths = [
        ("Config", _home(config), config_from if config.is_dir() else f"{config_from}; missing"),
        ("Settings", _home(settings), "present" if settings.is_file() else "none"),
        (
            "Data",
            _home(data),
            (_home(Path(data_from)) if data_from.startswith("/") else data_from)
            + ("" if data.is_dir() else "; missing"),
        ),
    ]
    try:
        configured = gmailapi.config_accounts(config)
        problem = ""
    except gmailapi.GmailError as exc:
        configured, problem = {}, str(exc)
    backups = {}
    for folder in find_backups(data):
        try:
            backups[backup_account(folder)] = folder
        except ValueError, sqlite3.Error:
            continue

    rows = []
    now = datetime.now(UTC)
    for account in sorted(set(configured) | set(backups)):
        cfg = configured.get(account)
        tokens = []
        for scope in gmailapi.SCOPES:
            if cfg is not None and gmailapi.token_file(cfg, scope).is_file():
                authorised = gmailapi.token_authorised_at(cfg, scope)
                tokens.append(
                    f"{scope} ({gmailapi.token_age_days(authorised, now):.0f} days)"
                    if authorised
                    else f"{scope} (age unknown)"
                )
        folder = backups.get(account)
        messages = last = None
        if folder is not None:
            con = sqlite3.connect(f"file:{folder / 'msg-db.sqlite'}?mode=ro", uri=True)
            try:
                (messages,) = con.execute("SELECT count(*) FROM uids").fetchone()
            finally:
                con.close()
            meta = sidecar.meta_path(folder)
            if meta.is_file():
                con = sqlite3.connect(f"file:{meta}?mode=ro", uri=True)
                try:
                    row = con.execute(
                        """SELECT finished_ms, status FROM runs WHERE command = 'backup'
                           ORDER BY finished_ms DESC LIMIT 1"""
                    ).fetchone()
                except sqlite3.Error:  # a sidecar from before the runs table
                    row = None
                finally:
                    con.close()
                if row:
                    last = f"{when(row[0])} {row[1]}"
        rows.append(
            (
                account,
                cfg.name if cfg is not None else "not configured",
                "yes" if cfg is not None and (cfg / "client_secrets.json").is_file() else "no",
                ", ".join(tokens) or "none",
                _home(folder) if folder is not None else "none yet",
                messages,
                last or "",
            )
        )
    tables = [
        Table(
            "paths",
            "Where things are",
            [("", "text"), ("Path", "text"), ("From", "text")],
            paths,
            limit=False,
        ),
        Table(
            "accounts",
            "Accounts",
            [
                ("Account", "text"),
                ("Config name", "text"),
                ("OAuth client", "text"),
                ("Tokens", "text"),
                ("Backup", "text"),
                ("Messages", "int"),
                ("Last backup (UTC)", "text"),
            ],
            rows,
            notes=[problem]
            if problem
            else [
                "Config: $MAILCELLAR_CONFIG, else ~/.config/mailcellar, else ./config. "
                "Data: $MAILCELLAR_DATA, else data_dir in settings.toml, else ~/mailcellar. "
                "Tokens: the access each account has granted (readonly, modify) and the days "
                "since consent was given: with the consent screen in Testing status, Google "
                "expires a token 7 days after that (renew: mailcellar auth ACCOUNT --scope S "
                "--renew)."
            ],
            limit=False,
        ),
    ]
    render(tables, 0, sys.stdout)
    return 0


COMMANDS = {
    "setup": _setup,
    "backup": _backup_command("backup"),
    "count": _backup_command("count"),
    "meta": _backup_command("meta"),
    "verify": _verify,
    "auth": _auth,
    "schedule": _schedule,
    "dedup": _dedup,
    "offload": _offload,
    "restore": _restore,
    "export": _export,
    "hold": _hold,
    "search": _search,
    "report": _report,
    "config": _config,
}


VERBOSE, QUIET = ("-v", "--verbose"), ("-q", "--quiet")


class _Stderr(logging.StreamHandler):
    """
    A handler that writes to whatever sys.stderr is at the time (tests
    capture it), on a line of its own when a progress line is showing.
    """

    @property
    def stream(self):
        return sys.stderr

    @stream.setter
    def stream(self, _value):
        pass

    def emit(self, record):
        from mailcellar import progress

        progress.clear_line()
        super().emit(record)


class _Format(logging.Formatter):
    """Progress as it is; warnings, errors and -v detail with their level."""

    def format(self, record):
        message = record.getMessage()
        if record.levelno == logging.INFO:
            return message
        return f"{record.levelname.lower()}: {message}"


def configure_logging(level: int = logging.INFO) -> None:
    """
    Diagnostics (progress, warnings, retries) go to stderr through logging;
    results and prompts are printed. Safe to call again: it replaces the
    handler.
    """
    root = logging.getLogger("mailcellar")
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = _Stderr()
    handler.setFormatter(_Format())
    root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    level = logging.INFO
    if any(a in VERBOSE for a in argv):
        level = logging.DEBUG
    if any(a in QUIET for a in argv):
        level = logging.WARNING
    argv = [a for a in argv if a not in VERBOSE + QUIET]
    configure_logging(level)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(f"usage: mailcellar <command> [options]\n\n{SYNOPSIS}\n\n{FOOTER.strip()}")
        return 0 if argv else 2
    name, rest = argv[0], argv[1:]
    if name not in COMMANDS:
        close = difflib.get_close_matches(name, COMMANDS, n=1)
        hint = f" Did you mean '{close[0]}'?" if close else ""
        print(
            f"mailcellar: unknown command '{name}'.{hint} See 'mailcellar --help'.", file=sys.stderr
        )
        return 2
    return COMMANDS[name](rest) or 0


if __name__ == "__main__":
    sys.exit(main())
