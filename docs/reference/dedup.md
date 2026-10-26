# Removing duplicates: dedup

```bash
mailcellar dedup index                     # catalogue the backups
mailcellar dedup match                     # report duplicate groups
mailcellar dedup plan --keep origin        # across accounts: drop forwarded copies
mailcellar dedup plan --scope within       # within each account
mailcellar dedup apply                     # dry run against live Gmail
mailcellar dedup apply --label --limit 5   # label 5 removals for review
mailcellar dedup apply --trash --limit 5   # move labelled ones to Trash
```

Review the plan (`<data root>/dedup/dedup-plan.csv`) and the labelled
messages in Gmail before trashing; removing the label vetoes a message.
Each run is logged in `<data root>/dedup/dedup-apply-log.csv`. How
copies are matched and which one is kept:
[docs/architecture.md](../architecture.md#deduplication).
