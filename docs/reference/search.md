# Searching mail: search

Gmail's search no longer finds mail once it has been offloaded. `search`
finds mail in the backups, offloaded or not, by words in the sender,
recipients, subject and body (not attachments). It is read-only towards
Gmail and the backups, and needs no Gmail access.

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

The index is `<data root>/search/search.sqlite`. Each search first adds new
and changed mail to it for the backups searched, so the first search reads
every message once and takes a while; later ones read only what changed
(`--no-update` skips that). It holds what is needed to find a message, not
the text itself, so the results show where to look and the `.eml` file
holds the message. Deleting the file is safe: the next search builds it
again.
