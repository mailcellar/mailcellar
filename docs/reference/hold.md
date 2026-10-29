---
description: "Reference for mailcellar hold: add, list, show and release legal holds that keep mail in the mailbox and the backup."
---

# Legal holds: hold

A hold names mail that must be kept as it is, for a dispute, an audit or a
records request. The commands that remove mail leave what a hold covers
alone, in the mailbox and in the backup:

| Command | With a hold |
| --- | --- |
| `offload apply`, `dedup apply` | held mail stays in the mailbox (the log says `held: NAME`) |
| `backup --prune` | held mail stays in the backup after the mailbox deletes it |
| `offload forget` | held mail isn't forgotten |
| `offload forget --empty` | a forget run that may contain held mail is kept |

Restore, export, search and the reports are unaffected: a hold keeps mail,
it doesn't hide it.

```bash
mailcellar hold add landlord-2026 --reason "deposit dispute" --party @landlord.example --after 2025-09-01
mailcellar hold list
mailcellar hold show landlord-2026
mailcellar export personal --hold landlord-2026 --to ~/landlord-2026.zip
mailcellar hold release landlord-2026
```

**What a hold covers.** Every criterion given must match:

- `--party ADDRESS|@DOMAIN`: the address, or any address at exactly that
  domain, in From, To, Cc or Bcc, so mail you sent is covered too
  (repeatable: any of them);
- `--after DATE`, `--before DATE`: mail from that date on, or older than it;
- `--label NAME`: mail with that user label, ignoring case.

Messages named with `--id` are held whatever the rest says.

**A hold follows its criteria.** They are checked each time a command
would remove mail, so mail that matches later, such as a reply that
arrives next week, is held too. A hold can only keep what has been backed
up: keep backups running while one is in force. `report health` warns
when a hold is in force and there has been no backup for 7 days.

**Mail already forgotten** keeps no addresses or labels, so a hold that
names parties or a label covers it unless its dates or IDs rule it out,
and `offload forget --empty` keeps that run. A hold errs on keeping.

**Where holds are kept.** Each covered backup's `meta.sqlite` records its
holds, released ones included (schema 11). Placing and releasing a hold
are also recorded in the backup's [hash chain](backup.md#verify), with its
criteria, so when a hold began and what it covered can't be rewritten
later.

## Options

### `hold add NAME [ACCOUNT ...]`

Places a hold on the backups named (default: all). The name is letters,
digits, `.`, `_` and `-`, unique among the holds in force.

| Option | What it does |
| --- | --- |
| `--reason TEXT` | Required. Why the mail is held; recorded with it |
| `--party ADDRESS\|@DOMAIN` | Mail from or to this address, or any address at the domain (repeatable) |
| `--after DATE` | Mail from this date on: YYYY-MM-DD (UTC), or a number of years ago |
| `--before DATE` | Mail older than this |
| `--label NAME` | Mail with this user label |
| `--id GMAIL_ID` | A message, whatever the rest says (repeatable) |
| `--until DATE` | End the hold then (YYYY-MM-DD); default: until released |

### `hold list [ACCOUNT ...]`

The holds in force, with how many messages each keeps now.

| Option | What it does |
| --- | --- |
| `--all` | Released and ended holds too |

### `hold show NAME [ACCOUNT ...]`

The hold's reason and criteria; how many messages it keeps and their dates;
how many of them the mailbox no longer has, as far as the backup knows, so the
backup holds the only copy; and forgotten mail it may cover.

### `hold release NAME [ACCOUNT ...]`

Ends a hold. Its record is kept, with when it was released.
