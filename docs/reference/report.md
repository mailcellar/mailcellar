---
description: "Reference for mailcellar report: storage, bulk senders, volume, threads, labels, correspondents, health and senders, over the backups."
---

# Reports: report

```bash
mailcellar report storage                 # what takes up space; space freed per offload batch
mailcellar report storage personal --csv ~/reports/storage
mailcellar report bulk                    # mailing lists and bulk senders; unsubscribe candidates
mailcellar report volume                  # received, sent and forwarded mail over time
mailcellar report threads                 # thread sizes; threads in both accounts
mailcellar report labels                  # user labels per account; unlabelled mail
mailcellar report correspondents          # who you write to and hear from; reply rates
mailcellar report health                  # completeness, runs, gaps, forgotten mail, holds, chain heads
mailcellar report senders personal --top 30  # top senders and domains in one backup
```

Reports read only the local backups, never the mailboxes. Each takes `--help`,
and prints notes on what its columns mean.

## Options

Accounts are named by address, config name or a unique part of an address;
with none named, every backup in the data root.

### Every report

| Option | What it does |
| --- | --- |
| `--top N` | Rows per section in the text report (default 15) |
| `--output FILE` | Write the text report to this file |
| `--csv DIR` | Also write every section, all rows, as CSV files in DIR |

### `report storage`

| Option | What it does |
| --- | --- |
| `--min-size MB` | Removal candidates: at least this size (default 5) |
| `--before DATE` | Removal candidates: received before this date (`YYYY-MM-DD`) or this many years ago (default 5) |
| `--include-labelled` | Removal candidates: include sent, starred and user-labelled mail |

### `report bulk`

| Option | What it does |
| --- | --- |
| `--min-messages N` | Candidates: sources with at least N messages (default 10) |
| `--active-days DAYS` | "Still sending": a message within this many days (default 90) |
| `--min-bulk-share PERCENT` | A source is bulk when at least this share of its messages has bulk signals (default 50) |
| `--unsubscribe` | Also list where to unsubscribe from each candidate, from its most recent message |

`--unsubscribe` adds a section, "Where to unsubscribe". For each candidate it
reads the most recent message's `.eml` and shows its List-Unsubscribe link
(`https:` only) and address (`mailto:`), whether it offers one-click
unsubscribe (RFC 8058: `List-Unsubscribe-Post` with an `https:` link, so
opening the link is all it takes), the message's date and whether the
source is still sending. The link's host is shown on its own, with a note
when its domain isn't the sender's. Mailing services are common, but the
link comes from the mail itself, so check it before opening it. Relayed
mail shows no link: its header is your own list's. The `Link`
column is never cut, so it can be copied; with `--csv` it is in
`bulk-unsubscribe.csv`. mailcellar opens no link and sends no mail.

### `report health`

Also lists the [legal holds](hold.md) in force, with how many messages each
keeps and how many only the backup has, and warns when a backup under a
hold hasn't run for 7 days.

| Option | What it does |
| --- | --- |
| `--window-days DAYS` | Gmail's history retention: longer gaps between backups mean a full listing (default 28; Gmail accounts) |

### `report senders ACCOUNT`

Takes one account, not several.

| Option | What it does |
| --- | --- |
| `--top N` | Senders and domains to show (default 25) |
| `--min-count N` | Minimum message count to include a sender or domain (default 1) |
| `--output FILE` | Write the text report to this file |
| `--csv FILE` | Also write every sender's figures to this file (one file, not a folder) |
| `--no-reply-to` | Add a section of senders with no Reply-To header |

`volume`, `threads`, `labels` and `correspondents` take only the common options.
