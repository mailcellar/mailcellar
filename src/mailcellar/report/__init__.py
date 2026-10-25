"""
report: reports over the backups in the data root, read from each
account's meta.sqlite (and gyb's index, for completeness). They never
contact Gmail or change anything, and print to stdout or write CSV files.

  storage         what takes up space, and removal candidates
  bulk            mailing lists and bulk senders; unsubscribe candidates
  volume          mail received, sent and forwarded over time
  threads         thread sizes, and threads seen from both accounts
  labels          user labels per account, and where accounts differ
  correspondents  who you write to and hear from; reply rates
  health          backup completeness, runs, gaps, offloaded and pruned mail
  senders         top senders and domains in one backup (senders.py)

db.py attaches the sidecars; output.py renders tables; cli.py dispatches.

Usage
  mailcellar report storage                       # every backup
  mailcellar report storage personal --top 25
  mailcellar report storage --min-size 10 --before 2020-01-01
  mailcellar report bulk --min-messages 20 --csv ~/reports/bulk
  mailcellar report senders personal --top 30
"""
