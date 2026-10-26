# Reports: report

```bash
mailcellar report storage                 # what takes up space; space freed per offload batch
mailcellar report storage personal --csv ~/reports/storage
mailcellar report bulk                    # mailing lists and bulk senders; unsubscribe candidates
mailcellar report volume                  # received, sent and forwarded mail over time
mailcellar report threads                 # thread sizes; threads in both accounts
mailcellar report labels                  # user labels per account; unlabelled mail
mailcellar report correspondents          # who you write to and hear from; reply rates
mailcellar report health                  # completeness, runs, gaps, pruned and forgotten mail, chain heads
mailcellar report senders personal --top 30  # top senders and domains in one backup
```

Reports read only the local backups, never Gmail. Each takes `--help`,
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

### `report health`

| Option | What it does |
| --- | --- |
| `--window-days DAYS` | Gmail's history retention: longer gaps between backups mean a full listing (default 28) |

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
