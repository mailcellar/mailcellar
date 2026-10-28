# Offloading old mail: offload

Rules go in `<data root>/offload/rules.toml`, one `[[rule]]` table each.
The keys are documented in `src/mailcellar/offload/rules.py`:

```toml
[[rule]]
name = "old work mail"
account = "work"
label = "work"           # or sender, list, forwarded_from, category, subject, to_me,
                         # larger_than_mb, query; different keys must all match
before = "2022-01-01"    # or a number of years ago, e.g. "5"
# keep = ["mine", "starred", "labelled"]   # what stays in Gmail; see below
# keep_if = [{ subject = "contract" }, { sender = "@client.example", to_me = true }]
# forget_after = 7       # retention: forget what it offloaded 7 years after its date
```

`keep` names what stays in Gmail although the rule selects it. Left out, it
is `["mine", "starred", "conversations", "labelled"]`; `keep = []` keeps
nothing the rule selects.

| `keep` name | Keeps |
| --- | --- |
| `mine` | your mail: sent, drafts, from one of your addresses |
| `starred` | starred |
| `conversations` | in a thread you wrote in, or a message you replied to |
| `labelled` | a user label the rule doesn't name |
| `correspondents` | from someone you have written to (not in the default) |

`keep_if` keeps what matches a table of scope keys, or any one of a list of
tables; the keys of one table must all match, as in the rule itself.

Before 1.0, rules said this with `waive` (default protections to drop:
`sent`, `starred`, `thread`, `replied`, `labels`), `protect` (protections to
add, `correspondents`) and `except` (a table whose keys each keep on their
own: `except = { a = …, b = … }` is `keep_if = [{ a = … }, { b = … }]`).
They still work until 1.0, can't be mixed with `keep` and `keep_if` in one
rule, and `plan` warns about each rule that uses them, with the rule
written in the new keys.

```bash
mailcellar offload plan                    # all accounts → <data root>/offload/offload-plan.csv
mailcellar offload plan personal --top 30 --csv ~/reports/offload
mailcellar offload plan --review           # also a review page per rule
```

`plan` changes nothing. It prints what each rule would offload (by year and
sender) and what its `keep` and `keep_if` kept, per reason. To keep particular messages, add a
user label in Gmail and run a backup.

`query` is a Gmail search, written as in Gmail's search box
(`query = "has:attachment filename:pdf"`). It narrows a rule like the other
keys, and works in `keep_if` too. `plan` asks Gmail which messages each
search finds, with read-only access, and selects only those the backup
has; it says how many it found that the backup lacks (run a backup, then
plan again). `before`, `keep` and `keep_if` still apply, so a
search that finds labelled mail keeps it unless the rule names the label
or leaves `labelled` out of `keep`. A rule without `query` doesn't contact Gmail.

For a rule that selects thousands of messages, `--review` also writes one
HTML page per rule to `<data root>/offload/review/` (open it in a browser):
what the rule selects, in one line, the summary tables, a random sample of what the rule would offload and of
what each `keep` reason kept, the largest messages, the threads with the most
messages in the offload, and attachment types by size. It shows senders
and subjects, never message bodies, and is for you alone: keep it on your
machine. The same plan shows the same sample.

```bash
mailcellar offload apply                       # dry run: Gmail and backup checks
mailcellar offload apply --label --limit 2000  # label the next 2,000 offload-candidate
mailcellar backup personal                     # trash needs a backup newer than the plan
mailcellar offload apply --trash --limit 2000  # trash the next 2,000 labelled ones
```

Repeated runs with `--limit` move on through the plan; `--trash` takes only
the messages a `--label` run labelled, as its apply log shows. Removing the
label in Gmail vetoes a message for good. A message Gmail can't be asked
about, even after retries, is skipped and tried again by the next run. Trashed messages are recorded as
offloaded, so `backup --prune` keeps them in the backup after Gmail empties
its Trash.

On an IMAP account (one the server lets mailcellar move mail on), the
label is a `$mailcellar-` keyword, or with `setup --review folder` a move
into `mailcellar/review/<label>`: clearing the keyword, or moving the
message out, vetoes it. Trash is the folder the server marks `\Trash`.
See [Backing up an IMAP account](../guide/imap.md#3-offload-and-remove-duplicates).

**How long it takes:**
- labelling is fast: thousands of messages a minute;
- trashing adds Gmail's `TRASH` label a thousand messages per call, then
  reads each message back to confirm it is in Trash: 2,000 messages take
  about a minute;
- run large batches in the background, or in batches with `--limit`.

Each trash run writes a manifest,
`<data root>/offload/batches/<batch>/offloaded.csv`: every message it
offloaded, with its `.eml` path, date, sender, subject and rule. Every plan
is also kept in `offload/plans/`, and the apply log names the plan each run
used. To put offloaded mail back, see [Restoring mail](restore.md).

`report storage` shows, per batch, how much Gmail has freed: the messages
whose Trash has been emptied (as the last `backup` or `count` found) and
their size, what is still in Trash and when Gmail will empty it, and what
was restored since. Gmail's own storage figure isn't available through its
API, so the sizes are its per-message estimates.

## Forgetting offloaded mail

Some offloaded mail shouldn't be kept at all, even in the backup.
`offload forget` deletes it from the backup, in stages, and only once Gmail
has deleted it too:

```bash
mailcellar offload forget --batch 2026-01-15-093000          # dry run: what would go
mailcellar offload forget --batch 2026-01-15-093000 --yes    # asks you to type the count
mailcellar offload forget --undo 2026-02-20-101500           # put a held run back
mailcellar offload forget --empty --yes                      # delete runs held 30 days
```

- **Only what Gmail no longer has.** A message can be forgotten once a
  `backup` or `count` has recorded it gone (Gmail emptied its Trash), and
  Gmail, asked again (read-only), still doesn't have it. The rest of the
  selection is listed with the reason and left alone: messages still in
  Gmail's Trash, ones Gmail has again, and ones it couldn't be asked about.
- **Held 30 days.** The files move to `<data root>/_forgotten/<email>/<run>/`,
  with a manifest that `--undo RUN` uses to put them back; `mailcellar meta`
  then re-parses them. `--empty` deletes runs held 30 days or more, for
  good, after you type the count. `report health` lists the held runs and
  which are due, and `offload forget` says so when any are.
- **Nothing else keeps them.** Their rows leave `msg-db.sqlite` and
  `meta.sqlite` (attachment names included), the search index and the
  dedup catalogue, and each database is rewritten so their bytes don't
  linger in its free space. Their sender and subject are blanked in the
  offload and dedup plans, apply logs and batch manifests, which keep the
  Gmail ID, date, rule and size; the review pages are deleted.
- **What is left:** a `forgotten` record in `meta.sqlite` (Gmail ID, batch,
  rule, date, size, when forgotten and emptied). `count` and
  `report storage` show forgotten mail on a line of its own, and `restore`
  says it can't bring it back.
- **Other copies** are out of mailcellar's reach: any `export` you made, a
  second copy of the data root, Time Machine. The dry run says so, and on a
  Mac whether Time Machine covers the data root.
- **Legal holds win.** Mail a [hold](hold.md) covers is never forgotten.

### Retention: `forget_after`

A rule's `forget_after = N` (a whole number of years) is a standing policy:
mail the rule offloaded is due to be forgotten once it is more than N years
old, counted from the message's date. `offload forget --due` forgets what is
due, through every stage above, so mail still in Gmail is never touched;
the rule's `before` offloads it first.

```bash
mailcellar offload forget --due                     # dry run: what is due, by rule and year
mailcellar offload forget --due --rule invoices     # only this rule
mailcellar offload forget --due --yes               # asks you to type the count
```

`offload plan` and the review pages show each rule's `forget_after`;
`report health` and `schedule status` show how much is due, and nothing
runs it unattended. Mail whose rule has been renamed or removed is under no
policy and stays. `offload plan` and `--due` warn about a `forget_after` no
longer than the rule's `before`: everything it offloads would be due as soon
as Gmail has deleted it. Each forget run's link in the backup's hash chain names
the rules it forgot for, and their `forget_after`.

## Options

### `offload plan`

| Option | What it does |
| --- | --- |
| `ACCOUNT ...` | Accounts to plan for (default: every backup in the data root) |
| `--rules RULES` | Rules file (default `<data root>/offload/rules.toml`) |
| `--plan PLAN` | Plan CSV to write (default `<data root>/offload/offload-plan.csv`) |
| `--top N` | Senders per rule in the summary (default 15) |
| `--csv DIR` | Also write the summary tables as CSV in DIR |
| `--review` | Also write a review page per rule to `<data root>/offload/review/` |
| `--config CONFIG` | Config folder, for rules with a `query` (Gmail is searched, read-only) |

### `offload apply`

A dry run unless `--label` or `--trash` is given.

| Option | What it does |
| --- | --- |
| `ACCOUNT ...` | Only these accounts' plan rows (default: all) |
| `--label [NAME]` | Add the label NAME (default `offload-candidate`) to messages that pass the checks; it is created if missing. Removing it in the mailbox vetoes a message |
| `--trash [NAME]` | Move messages that pass the checks and carry the label NAME to Trash, and record them as offloaded |
| `--rule RULE` | Only this rule's rows (repeatable) |
| `--limit N` | Only the first N plan rows |
| `--yes` | Don't ask before trashing |
| `--plan PLAN` | Plan CSV from `plan` |
| `--log LOG` | Apply log, appended to (default `<data root>/offload/offload-apply-log.csv`) |
| `--config CONFIG` | Config folder |

### `offload forget`

A dry run unless `--yes`, which also asks you to type the count.

| Option | What it does |
| --- | --- |
| `ACCOUNT ...` | Only these accounts' backups (default: all) |
| `--batch BATCH` | Offloaded messages of this batch (repeatable) |
| `--rule RULE` | Offloaded messages of this rule (repeatable); with `--due`, only these rules |
| `--due` | Offloaded mail the rules' `forget_after` makes due: older than that many years |
| `--rules RULES` | Rules file, for `--due` (default `<data root>/offload/rules.toml`) |
| `--yes` | Forget them (with `--empty`: delete the runs) |
| `--empty` | Delete forget runs held 30 days or more, for good |
| `--undo RUN` | Put a held forget run back into the backup |
| `--config CONFIG` | Config folder (Gmail is asked, read-only, whether each message is gone) |
