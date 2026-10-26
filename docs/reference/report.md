# Reports: report

```bash
mailcellar report storage                 # what takes up space; space freed per offload batch
mailcellar report storage personal --csv ~/reports/storage
mailcellar report bulk                    # mailing lists and bulk senders; unsubscribe candidates
mailcellar report volume                  # received, sent and forwarded mail over time
mailcellar report threads                 # thread sizes; threads in both accounts
mailcellar report labels                  # user labels per account; unlabelled mail
mailcellar report correspondents          # who you write to and hear from; reply rates
mailcellar report health                  # backup completeness, runs, gaps, pruned batches
mailcellar report senders personal --top 30  # top senders and domains in one backup
```

Reports read only the local backups, never Gmail. Each takes `--help`,
and prints notes on what its columns mean.
