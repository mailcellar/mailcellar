"""
dedup: duplicate messages within and across the backed-up accounts,
removed from Gmail in stages.

  index   Catalogue every message in the backups into
          <data root>/dedup/dedup.sqlite (Message-ID, content hash, labels,
          …). Incremental: reruns look only at new or changed messages,
          taking their fields from each backup's meta.sqlite where it has
          them and parsing the .eml files otherwise.
  match   Group copies of the same message, within and across accounts:
          the same visible content (text without links, plus attachments)
          and the same Message-ID or the same From/Date/Subject.
          Message-IDs reused for different content are reported, not
          grouped. Results go to match_groups / match_members.
  plan    Re-run match, then write a plan CSV: one row per copy, with
          action keep / remove / review. --scope picks cross-account groups
          (the default), within-account groups, or both.
          Across accounts: copies in the --keep account are kept; copies
          elsewhere are removed unless a safety rule sends them to review
          (sent/draft, starred, user labels the keeper lacks, …). --keep
          origin removes only copies auto-forwarded from another indexed
          account (X-Forwarded-For) and keeps the original; groups with no
          forwarding evidence are left alone.
          Within an account: keep one copy from the largest set with the
          same labels; the others with those labels are removed (safety
          rules apply), copies with other labels go to review.
  apply   Check every 'remove' row of the plan against Gmail: the local
          backup copy exists, the message and its keeper are still in
          Gmail, the keeper isn't in Trash or Spam, and the safety labels
          haven't changed since the plan. A dry run by default; --label
          [NAME] adds the label NAME (default dedup-redundant) to rows that
          pass, then --trash [NAME] moves rows carrying it to Trash. NAME
          must be a label no planned message already had. Nothing is ever
          permanently deleted, and every run is appended to an audit log.
  auth    Obtain or renew an account's Gmail token (--scope readonly or
          modify) and check that it reaches the right mailbox; --renew asks
          for consent again before a token runs out. Changes nothing in
          Gmail. Also available as `mailcellar auth`.

Usage
  mailcellar dedup index
  mailcellar dedup index ~/elsewhere/a@example.com ~/elsewhere/b@example.com
  mailcellar dedup match --csv matches.csv --show 50
  mailcellar dedup plan --keep alex@example.org
  mailcellar dedup plan --keep origin
  mailcellar dedup plan --scope within
  mailcellar auth work --scope modify
  mailcellar dedup apply                     # dry run
  mailcellar dedup apply --label --limit 5
  mailcellar dedup apply --trash --limit 5

The backups are only read; msg-db.sqlite is opened read-only. Everything
dedup writes (the index, plan and apply log) goes to <data root>/dedup/
by default.
"""
