"""
mailcellar: Gmail backups kept as an archive. One command, `mailcellar`
(cli.py), over these modules:

  paths.py      where the config folder and the data root are
  gmailapi.py   the Gmail API: accounts, OAuth tokens, batched calls
  message.py    message parsing (parse_message, content_hash, header_key)
  backup/       backup, count, meta and sync (commands.py); gyb's on-disk
                format (gyb.py); the per-message sidecar (sidecar.py)
  dedup/        duplicates within and across accounts
  offload/      old mail out of Gmail, kept in the backup
  restore/      backed-up mail back into Gmail
  report/       reports over the sidecars, and the sender report

How it fits together: docs/architecture.md.
"""
