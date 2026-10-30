---
description: "Run email backups weekly and unattended: a launchd agent on macOS, a cron line on Linux, and how to check each account's last backup and token."
---

# Scheduled backups

Backups read Gmail's change history, which Gmail keeps for about four weeks,
and `offload apply --trash` wants a recent backup. Both assume backups
happen on time, so it is worth not depending on remembering.

```bash
mailcellar schedule install     # weekly, Sunday 03:30, every account
mailcellar schedule status      # is it installed; each account's last backup and token
mailcellar schedule remove
```

## What install does

**On macOS** it writes a launchd agent to
`~/Library/LaunchAgents/com.mailcellar.backup.plist` and loads it. The agent
runs `mailcellar backup --unattended` and appends its output to
`<data root>/logs/backup.log` (the log is not rotated). It records where your
config folder and data root are, so it doesn't depend on the folder it runs
from. Installing does not start a backup. Running `install` again replaces
the agent.

**Elsewhere** it prints the line to add to your crontab (`crontab -e`) and
changes nothing; mailcellar never edits a crontab.

`schedule install --print` shows what would be installed, on any system,
without installing it.

Choose when with `--daily` or `--weekly` (the default), `--at HH:MM` (03:30)
and, for weekly, `--weekday` (`sun`). Name accounts to back up only those
(`schedule install work`); with none named, the agent backs up every account
in `accounts.toml`, including ones you add later.

launchd doesn't run an agent while the Mac sleeps. A run that was missed
happens when it wakes, so a laptop that is closed on Sunday backs up on
Monday.

## It can't sign in

An unattended run has nobody to click through Google's consent screen. So
`--unattended` never opens a browser: if a token needs signing in, that
account's backup fails with the command that fixes it, the other accounts
still run, and the failure is recorded as an `error` run. It shows in
`mailcellar schedule status` and in `mailcellar report health` (the run's
reason is `needs-consent`). `schedule install` refuses an account that has no
token yet:

```bash
mailcellar auth work --scope readonly
```

With the Google Cloud consent screen in *Testing* status, a token expires
7 days after consent, which is the weekly schedule's own interval, so
publishing the app is the way to stop this (see
[Setting up Google Cloud](google-cloud.md#testing-or-in-production)).
Otherwise renew before it runs out:
`mailcellar auth work --scope readonly --renew`. The log carries mailcellar's
warning when a token is 5 days past consent.

## Status

```
Scheduled weekly on Sun at 03:30
Agent: ~/Library/LaunchAgents/com.mailcellar.backup.plist
Log:   <data root>/logs/backup.log
  alex@example.com: last backup 2026-10-25 03:30 UTC, ok; readonly token, 3 days since consent
```

`schedule status` exits 1 if anything needs attention: an account whose last
backup failed, or is more than two intervals old (one missed run is made up
when the Mac wakes), has no backup run recorded, or has no token; damaged
files that the checks after the last eight backups found (a damaged file
is found again each pass until it is repaired); a token
more than `token_warn_days` past consent; or an agent that is installed but
not loaded.

## Removing it

`schedule remove` unloads the agent and deletes its file. The log is kept.
