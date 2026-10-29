---
description: "Search every email backup, including mail no longer in Gmail, and see where the storage goes and what to offload first. Read-only, with no Gmail access needed."
---

# Finding mail, and deciding what to offload

Two read-only commands help you work with what is in the backups. `search`
finds a message, including mail you have offloaded, which Gmail's own search
no longer sees. `report` shows where the space goes and what to offload
first. Neither needs Gmail access or changes anything. Every option is in
the reference: [search](../reference/search.md) and
[report](../reference/report.md).

## Finding a message

```bash
mailcellar search "invoice 2024"                     # every backup
mailcellar search "from:alex subject:contract" work  # one account
mailcellar search '"quarterly figures"' --offloaded --after 2020-01-01
```

Every word has to be found, whole, ignoring case and accents. It looks in the
sender, recipients, subject and body, but not in attachments. The query
language is small:

| Write | To find |
| --- | --- |
| `invoice 2024` | mail containing both words |
| `"quarterly figures"` | the phrase, as written |
| `from:example.org` | the word in the sender only (a domain works); `to:` and `subject:` likewise |
| `refund OR credit` | either word |
| `-newsletter` | leave out mail containing it (put `--` before a query that starts with one) |
| `invo*` | words starting with `invo` |

Narrow with `--offloaded` (only mail taken out of Gmail), `--rule`, `--batch`,
`--after` and `--before`. A date is `YYYY-MM-DD` or a number of years ago.
Results are newest first, at most 50 (`--limit`), each with its date,
sender, subject and `.eml` file. The exit status is 1 when nothing matches.

### From a result to the message

- **Read it.** The `.eml` file opens in any mail client.
- **Get it back into Gmail.** For offloaded mail the result prints the
  `mailcellar restore ACCOUNT --id …` command that does it. See
  [Restoring mail](restore.md).
- **Send it on.** [Exporting mail](exporting.md) copies a selection out as an
  mbox or a zip.

### Picking results with fzf

`--format tsv` prints each result as one tab-separated line: date, sender,
subject, then account, ID, `.eml` path, offload rule and batch
(the columns are in the [reference](../reference/search.md#output-for-scripts-and-pickers)).
Any picker that reads lines can choose from it; with
[fzf](https://github.com/junegunn/fzf):

```bash
mailcellar search "invoice 2024" --format tsv \
  | fzf -m --delimiter '\t' --with-nth 1,2,3 --preview 'cat {6}' \
  | while IFS=$'\t' read -r _ _ _ account id _ _ _; do
      mailcellar restore "$account" --id "$id"      # a dry run: add --yes to restore
    done
```

`--with-nth 1,2,3` shows the date, sender and subject, `--preview` shows the
message, and `-m` lets you choose several with Tab. Mail Gmail still has is
left alone, so the dry run says what is missing from Gmail. `export
"$account" --id "$id" --to …` in the loop copies them out instead. skim,
gum, peco and fzy read the same lines. `--format json` prints one JSON object per line, for
`jq` and scripts.

### The first search is slow

`search` keeps an index in `<data root>/search/search.sqlite`. Each search
first adds new and changed mail to it, so the first one reads every message
once and takes a while. Later ones read only what changed. `--no-update`
searches the index as it is, which is quicker if you know nothing is new.
The index holds what is needed to find a message, not the text, and
deleting it is safe: the next search builds it again.

## Deciding what to offload

Before you write an [offload rule](offloading.md), look at what the backup
holds.

```bash
mailcellar report storage
```

`storage` shows what takes up space, by year, label, sender, domain and
attachment type, and lists **removal candidates**: large old mail that nothing protects. By
default that means at least 5 MB, received over 5 years ago, and not sent,
starred or labelled; `--min-size`, `--before` and `--include-labelled`
change it. It also shows, for each offload batch, how much Gmail has freed
once its Trash was emptied. The sizes are Gmail's per-message estimates, not
the storage figure Gmail shows, which its API doesn't give.

Which report answers which question:

| Question | Report |
| --- | --- |
| What is taking the space? What could go? | `storage` |
| Which mailing lists and bulk senders fill my mailbox? | `bulk` |
| How much mail do I get and send, by year and month? | `volume` |
| Which threads are huge, or are in both accounts? | `threads` |
| Which labels do I use, and what is unlabelled? | `labels` |
| Who do I write to, and who writes to me? | `correspondents` |
| Is the backup complete and recent? | `health` |

A few uses:

- **Writing a rule.** `storage` for the years and senders that dominate, then
  `bulk` for the mailing lists you no longer read. Between them you have the
  senders and the cut-off year for a rule.
- **Before you offload.** `labels` shows mail that is unlabelled, which a
  rule using `label` would never select, and `correspondents` shows the
  people the `correspondents` protection would keep mail from.
- **Checking in.** `health` after a schedule has been running for a while:
  the last run of each account, gaps longer than four weeks, tokens that
  need signing in, pruned batches, and forget runs due to be emptied.

Reports read only the local backups. Add `--csv DIR` to write every section,
all rows, as CSV files, and `--output FILE` to save the text. Each report
ends with notes on what its columns mean, and `--top N` sets how many rows
each section shows. After writing a rule, `offload plan` shows what it would
select ([Offloading old mail safely](offloading.md)).
