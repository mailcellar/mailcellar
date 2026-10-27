"""
schedule: run `mailcellar backup` regularly, with nobody there.

Backups read Gmail's change history, which Gmail keeps for about four weeks,
and `offload apply --trash` wants a recent backup, so both assume backups
happen on time. This installs one:

  schedule install [ACCOUNT ...] [--daily | --weekly] [--at HH:MM]
      macOS: a launchd agent in ~/Library/LaunchAgents running
      `mailcellar backup --unattended`, logging to <data root>/logs/.
      Elsewhere: prints the cron line to add (mailcellar never edits a
      crontab). --print shows what would be installed and installs nothing.
  schedule status   whether it is installed, and each account's last backup
      and token age, as the sidecars and token files record them.
  schedule remove   macOS: unloads the agent and deletes its file.

An unattended run can't open a browser, so a token that needs signing in
fails the run (backup --unattended) with the command that fixes it, and the
failure is recorded for `report health` and for `schedule status`.

The decisions here (describe, calendar_interval, launchd_plist, cron_line,
overdue) are pure functions over frozen values; installing and asking
launchctl is a thin layer around them.
"""

import argparse
import logging
import plistlib
import shlex
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from mailcellar import gmailapi
from mailcellar.backup import sidecar
from mailcellar.backup.gyb import backup_account
from mailcellar.paths import (
    backup_folder,
    config_root,
    data_root,
    find_backups,
    token_warn_days,
)

logger = logging.getLogger(__name__)

LABEL = "com.mailcellar.backup"
WEEKDAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")  # index = launchd and cron number
GMAIL_HISTORY_DAYS = 28  # a gap longer than this means a full listing
DAY_MS = 86_400_000


@dataclass(frozen=True)
class Schedule:
    frequency: str  # "daily" or "weekly"
    hour: int
    minute: int
    weekday: int = 0  # weekly only; 0 = Sunday


@dataclass(frozen=True)
class Job:
    """What is scheduled: when, the command line (executable first), its environment, its log."""

    schedule: Schedule
    arguments: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    log: Path


# ── decisions ────────────────────────────────────────────────────────────────


def parse_time(text: str) -> tuple[int, int]:
    """'03:30' → (3, 30). Raises ValueError."""
    try:
        hour, minute = (int(part) for part in text.split(":"))
    except ValueError:
        raise ValueError(f"{text!r} is not a time of day; use HH:MM, for example 03:30") from None
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"{text!r} is not a time of day; use HH:MM, for example 03:30")
    return hour, minute


def describe(schedule: Schedule) -> str:
    at = f"{schedule.hour:02d}:{schedule.minute:02d}"
    if schedule.frequency == "daily":
        return f"daily at {at}"
    return f"weekly on {WEEKDAYS[schedule.weekday].capitalize()} at {at}"


def interval_days(schedule: Schedule | None) -> int:
    """How often a backup should happen: the schedule's interval, else Gmail's history window."""
    if schedule is None:
        return GMAIL_HISTORY_DAYS
    return 1 if schedule.frequency == "daily" else 7


def overdue(last_ms: int | None, now_ms: int, schedule: Schedule | None) -> bool:
    """
    No backup yet, or the last one is more than two intervals ago (a missed run
    is made up when the Mac wakes, so one is not worth reporting).
    """
    return last_ms is None or (now_ms - last_ms) > 2 * interval_days(schedule) * DAY_MS


def calendar_interval(schedule: Schedule) -> dict[str, int]:
    interval = {"Hour": schedule.hour, "Minute": schedule.minute}
    if schedule.frequency == "weekly":
        interval["Weekday"] = schedule.weekday
    return interval


def launchd_plist(job: Job) -> bytes:
    """The launchd agent's property list."""
    return plistlib.dumps(
        {
            "Label": LABEL,
            "ProgramArguments": list(job.arguments),
            "StartCalendarInterval": calendar_interval(job.schedule),
            "EnvironmentVariables": dict(job.environment),
            "StandardOutPath": str(job.log),
            "StandardErrorPath": str(job.log),
            "RunAtLoad": False,
        }
    )


def job_from_plist(data: bytes) -> Job:
    """The Job a launchd agent written by launchd_plist() describes."""
    plist = plistlib.loads(data)
    interval = plist["StartCalendarInterval"]
    weekly = "Weekday" in interval
    return Job(
        Schedule(
            "weekly" if weekly else "daily",
            interval["Hour"],
            interval["Minute"],
            interval.get("Weekday", 0),
        ),
        tuple(plist["ProgramArguments"]),
        tuple(sorted(plist.get("EnvironmentVariables", {}).items())),
        Path(plist["StandardOutPath"]),
    )


def cron_line(job: Job) -> str:
    """
    The crontab line for a job. cron reads an unescaped % as the end of the
    command (the rest goes to its stdin), so every % is written \\%.
    """
    s = job.schedule
    day = str(s.weekday) if s.frequency == "weekly" else "*"
    env = " ".join(f"{k}={shlex.quote(v)}" for k, v in job.environment)
    command = " ".join(shlex.quote(a) for a in job.arguments)
    line = f"{command} >> {shlex.quote(str(job.log))} 2>&1"
    return f"{s.minute} {s.hour} * * {day} " + f"{env} {line}".replace("%", "\\%")


def accounts_of(job: Job) -> tuple[str, ...]:
    """The accounts a job backs up: what follows `--unattended` on its command line."""
    arguments = job.arguments
    return arguments[arguments.index("--unattended") + 1 :] if "--unattended" in arguments else ()


# ── the system ───────────────────────────────────────────────────────────────


def plist_path(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def launchctl(*args: str) -> tuple[int, str]:
    """Run launchctl: (exit status, its output)."""
    try:
        done = subprocess.run(
            ["launchctl", *args], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return done.returncode, (done.stdout + done.stderr).strip()


def uid() -> int:
    import os

    return os.getuid()


def executable() -> list[str]:
    """How to run mailcellar from a launchd or cron job, which has no PATH worth trusting."""
    found = shutil.which("mailcellar")
    return [found] if found else [sys.executable, "-m", "mailcellar"]


def resolve_accounts(config: Path, names: list[str]) -> list[str]:
    """Full addresses for the names given, or for every account in accounts.toml."""
    known = gmailapi.config_accounts(config)
    if not names:
        return sorted(known)
    return [gmailapi.resolve_account(config, name)[0] for name in names]


def build_job(schedule: Schedule, accounts: list[str], explicit: bool, config: Path) -> Job:
    """
    The command is `backup --unattended`, naming the accounts only when they
    were chosen, so a later account in accounts.toml is included.
    """
    data = data_root().resolve()
    return Job(
        schedule,
        (*executable(), "backup", "--unattended", *(accounts if explicit else ())),
        (("MAILCELLAR_CONFIG", str(config.resolve())), ("MAILCELLAR_DATA", str(data))),
        data / "logs" / "backup.log",
    )


def last_backup(account: str) -> tuple[int, str] | None:
    """(finished_ms, status) of the account's last backup run, or None. Read-only."""
    folders = {backup_account(f): f for f in find_backups()}
    folder = folders.get(account)
    if folder is None or not sidecar.meta_path(folder).is_file():
        return None
    con = sqlite3.connect(f"file:{sidecar.meta_path(folder)}?mode=ro", uri=True)
    try:
        return con.execute(
            """SELECT finished_ms, status FROM runs WHERE command = 'backup'
               ORDER BY finished_ms DESC, run_id DESC LIMIT 1"""
        ).fetchone()
    except sqlite3.Error:  # a sidecar from before the runs table
        return None
    finally:
        con.close()


def token_scopes(config: Path, account: str) -> list[str]:
    """The scopes the account has a token for."""
    folder = gmailapi.config_accounts(config).get(account)
    return [s for s in gmailapi.SCOPES if folder and gmailapi.token_file(folder, s).is_file()]


def token_note(config: Path, account: str, now: datetime) -> tuple[str, bool]:
    """(what to say about the account's token, whether it needs attention)."""
    folder = gmailapi.config_accounts(config).get(account)
    if folder is not None and gmailapi.service_account_key(folder, account) is not None:
        return "reads through the service account (no token to renew)", False
    scopes = token_scopes(config, account)
    if not scopes:
        return f"no token: run mailcellar auth {account} --scope readonly", True
    authorised = gmailapi.token_authorised_at(folder, scopes[0])
    if authorised is None:
        return f"{scopes[0]} token, age unknown", False
    days = gmailapi.token_age_days(authorised, now)
    notice = gmailapi.aging_notice(account, scopes[0], authorised, now, token_warn_days(config))
    return f"{scopes[0]} token, {days:.0f} days since consent", bool(notice)


# ── commands ─────────────────────────────────────────────────────────────────


def cmd_install(args) -> int:
    config = Path(args.config)
    try:
        accounts = resolve_accounts(config, args.accounts)
        hour, minute = parse_time(args.at)
    except (gmailapi.GmailError, ValueError) as exc:
        sys.exit(f"error: {exc}")
    schedule = Schedule(
        "daily" if args.daily else "weekly", hour, minute, WEEKDAYS.index(args.weekday)
    )
    job = build_job(schedule, accounts, bool(args.accounts), config)
    mac = sys.platform == "darwin"

    if not args.print:
        # An unattended run can't sign in: without a token it would fail every time.
        missing = [a for a in accounts if not token_scopes(config, a)]
        if missing:
            sys.exit(
                f"error: no token for {', '.join(missing)}: a scheduled run can't sign in. "
                f"Run mailcellar auth {missing[0]} --scope readonly first."
            )
    if args.print or not mac:
        if mac:
            print(launchd_plist(job).decode())
        else:
            print(f"# {describe(schedule)}; add to your crontab (crontab -e):")
            print(cron_line(job))
            if not args.print:
                print(
                    "\nmailcellar installs a schedule itself only on macOS (a launchd agent); "
                    "it never edits a crontab."
                )
        return 0

    path = plist_path(Path.home())
    path.parent.mkdir(parents=True, exist_ok=True)
    job.log.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(launchd_plist(job))
    domain = f"gui/{uid()}"
    launchctl("bootout", f"{domain}/{LABEL}")  # an earlier one, if any; not an error if absent
    code, output = launchctl("bootstrap", domain, str(path))
    if code:
        sys.exit(f"error: launchctl bootstrap failed: {output or code}")
    print(f"Scheduled {describe(schedule)}: mailcellar backup for {', '.join(accounts)}.")
    print(f"Agent: {path}\nLog:   {job.log}")
    print(
        "launchd doesn't run agents while the Mac sleeps; a missed run happens when it wakes. "
        "Check with: mailcellar schedule status"
    )
    return 0


def cmd_status(args) -> int:
    from mailcellar.offload import forget

    config = Path(args.config)
    now = datetime.now(UTC)
    now_ms = int(now.timestamp() * 1000)
    mac = sys.platform == "darwin"
    job = None
    if mac and plist_path(Path.home()).is_file():
        job = job_from_plist(plist_path(Path.home()).read_bytes())
        loaded = launchctl("print", f"gui/{uid()}/{LABEL}")[0] == 0
        print(f"Scheduled {describe(job.schedule)}" + ("" if loaded else " (not loaded)"))
        print(f"Agent: {plist_path(Path.home())}\nLog:   {job.log}")
        if job.log.is_file():
            modified = datetime.fromtimestamp(job.log.stat().st_mtime, UTC)
            print(f"Log last written {modified:%Y-%m-%d %H:%M} UTC")
        problems = not loaded
    else:
        print(
            "No schedule installed by mailcellar"
            + ("." if mac else " (it installs one only on macOS).")
            + " See: mailcellar schedule install --print"
        )
        problems = False

    try:
        accounts = (
            list(accounts_of(job)) if job and accounts_of(job) else resolve_accounts(config, [])
        )
    except gmailapi.GmailError as exc:
        sys.exit(f"error: {exc}")
    for account in accounts:
        last = last_backup(account)
        schedule = job.schedule if job else None
        if last is None:
            line, bad = "no backup run recorded", True
        else:
            when = datetime.fromtimestamp(last[0] / 1000, UTC)
            bad = last[1] != "ok" or overdue(last[0], now_ms, schedule)
            line = f"last backup {when:%Y-%m-%d %H:%M} UTC, {last[1]}"
            if overdue(last[0], now_ms, schedule):
                line += " (overdue)"
        token, token_bad = token_note(config, account, now)
        due = sum(n[1] for n in forget.retention(backup_folder(account), now_ms).values())
        if due:
            token += f"; {due:,} messages due under retention (offload forget --due)"
        print(f"  {account}: {line}; {token}")
        problems = problems or bad or token_bad
    return 1 if problems else 0


def cmd_remove(args) -> int:
    path = plist_path(Path.home())
    if sys.platform != "darwin":
        print("Nothing to remove: mailcellar installs a schedule only on macOS.")
        return 0
    if not path.is_file():
        print("Nothing to remove: no schedule is installed.")
        return 0
    launchctl("bootout", f"gui/{uid()}/{LABEL}")  # not loaded is fine
    path.unlink()
    print(f"Removed {path}. The log is kept.")
    return 0


def main(argv=None, prog: str | None = None) -> int:
    p = argparse.ArgumentParser(
        prog=prog or "mailcellar schedule",
        description="Run `mailcellar backup` regularly, unattended: a launchd agent on macOS, "
        "a cron line elsewhere.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    def add_config(sp):
        sp.add_argument(
            "--config",
            default=str(config_root()),
            help=f"Config folder: accounts.toml, settings.toml (default: {config_root()})",
        )

    install = sub.add_parser("install", help="Install the schedule (or print the cron line)")
    install.add_argument(
        "accounts",
        nargs="*",
        metavar="ACCOUNT",
        help="Accounts (address, config folder name or unique part of an address); "
        "default: every account in accounts.toml, including ones added later",
    )
    when = install.add_mutually_exclusive_group()
    when.add_argument("--daily", action="store_true", help="Every day")
    when.add_argument("--weekly", action="store_true", help="Every week (the default)")
    install.add_argument("--at", default="03:30", metavar="HH:MM", help="Time of day (03:30)")
    install.add_argument(
        "--weekday", default="sun", choices=WEEKDAYS, help="With --weekly: the day (sun)"
    )
    install.add_argument(
        "--print", action="store_true", help="Show what would be installed; install nothing"
    )
    add_config(install)
    install.set_defaults(func=cmd_install)

    status = sub.add_parser("status", help="Is it installed; each account's last backup and token")
    add_config(status)
    status.set_defaults(func=cmd_status)

    remove = sub.add_parser("remove", help="Remove the schedule")
    remove.set_defaults(func=cmd_remove)

    args = p.parse_args(argv)
    return args.func(args)
