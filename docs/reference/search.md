---
description: "Reference for mailcellar search: full-text search of email backups, offloaded mail included, by sender, recipients, subject and body."
---

# Searching mail: search

A mailbox's own search no longer finds mail once it has been offloaded.
`search` finds mail in the backups, offloaded or not, by words in the
sender, recipients, subject and body (not attachments). It is read-only
towards the mailboxes and the backups, and needs no access to the
mailboxes.

```bash
mailcellar search "invoice 2024"                     # every backup
mailcellar search "from:alex subject:contract" work  # one backup
mailcellar search '"quarterly figures"' --offloaded --after 2020-01-01
```

Every word must be found, whole and ignoring case and accents. A
`"phrase"` must be found as written; `from:`, `to:` and `subject:` look only
in that field (`from:example.org` finds a domain); `OR` between two words
takes either; `-word` leaves out mail with that word (after `--` if the query
starts with one); and `word*` matches words starting with it. Narrow with
`--offloaded`, `--rule`, `--batch`, `--after` and `--before`.

Results are newest first, at most 50 (`--limit`): the date, sender, subject
and `.eml` file, and for offloaded mail the `mailcellar restore … --id`
command that puts it back. The exit status is 1 when nothing matches.

## Output for scripts and pickers

`--format tsv` prints one tab-separated line per result, and `--format json`
one JSON object per line (JSON Lines). Either way stdout holds only the
results; the match count, "No matches." and progress go to stderr. The
exit status, `--limit` and the filters are the same as for the default
`--format text`.

| Column | `tsv` | `json` key |
| --- | --- | --- |
| 1 | date, `YYYY-MM-DD` (UTC) | `date` |
| 2 | sender | `sender` |
| 3 | subject | `subject` |
| 4 | account | `account` |
| 5 | ID in the backup (Gmail's, or an IMAP account's) | `id` |
| 6 | path of the `.eml` file | `path` |
| 7 | offload rule, empty if not offloaded | `offloaded`: `null`, or `{"rule": …, "batch": …}` |
| 8 | offload batch, likewise | |

In `tsv`, tabs and line breaks in the sender and subject become spaces; in
`json` they are kept, escaped. The readable columns come first so a picker
can show the whole line, and `cut -f` takes what a script needs from the
end. The account and ID are what `restore ACCOUNT --id` and
`export ACCOUNT --id` take, offloaded or not. A recipe with fzf is in
[Finding mail](../guide/finding-and-sizing.md#picking-results-with-fzf).

The files mailcellar writes and reads back call the same ID `id`: the
offload and dedup plans and apply logs, the batch and forget manifests and
`dedup match --csv`. A dedup row's keeper is `keeper_id`. The restore log
calls it `source_id`, with the restored copy's ID in `new_id`. For an IMAP
account it is that account's ID, not Gmail's.

The index is `<data root>/search/search.sqlite`. Each search first adds new
and changed mail to it for the backups searched, so the first search reads
every message once and takes a while; later ones read only what changed
(`--no-update` skips that). It holds what is needed to find a message, not
the text itself, so the results show where to look and the `.eml` file
holds the message. Deleting the file is safe: the next search builds it
again.
