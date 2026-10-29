---
description: "Reference for mailcellar offload: the rules file, every rule key, and the options of plan and apply for moving old mail out of the mailbox."
---

# Offloading old mail: offload

Rules go in `<data root>/offload/rules.toml`, one `[[rule]]` table each.
The keys are documented in `src/mailcellar/offload/rules.py`:

```toml
[[rule]]
name = "old work mail"
account = "work"
label = "work"           # or sender, list, forwarded_from, category, subject, to_me, to,
                         # bulk, attachment, larger_than_mb, query; different keys must all match
before = "2022-01-01"    # or a number of years ago, e.g. "5"
# after = "2012-01-01"   # and no older than this, written like before
# keep = ["mine", "starred", "labelled"]   # what stays in the mailbox; see below
# keep_if = [{ subject = "contract" }, { sender = "@client.example", to_me = true }]
# forget_after = 7       # retention: forget what it offloaded 7 years after its date
```

Three scope keys select what the others can't say:

- `to`: an address or `@domain` in To, Cc or Bcc, matched exactly, like
  `sender`.
- `bulk = true`: mail sent in bulk, as `report bulk` judges it: a List-Id,
  List-Unsubscribe or Precedence bulk, list or junk header, or a Gmail
  category tab. Mail relayed through your own domain is judged by its
  original sender's headers. `bulk = false` selects the rest.
- `attachment`: one or more of `any`, `image`, `pdf`, `video`, `audio`,
  `office`, `archive`. An attachment is a part that isn't inline, or has a
  file name; its kind comes from its MIME type, or from its file
  extension when the type says nothing (`application/octet-stream`).

They work in `keep_if` too. `after` doesn't: it bounds the rule's dates,
from `after` up to `before`, and must be earlier than `before`.

`keep` names what stays in the mailbox although the rule selects it. Left out, it
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
mailcellar offload plan --try 'bulk = true, before = "2017-01-01"'   # one rule, no file edited
```

`plan` changes nothing. It prints what each rule would offload (by year and
sender) and what its `keep` and `keep_if` kept, per reason. To keep particular messages, add a
user label in the mailbox (a folder, on an IMAP account) and run a backup.

`query` is a Gmail search, for Gmail accounts, written as in Gmail's search box
(`query = "has:attachment filename:pdf"`). It narrows a rule like the other
keys, and works in `keep_if` too. `plan` asks Gmail which messages each
search finds, with read-only access, and selects only those the backup
has; it says how many it found that the backup lacks (run a backup, then
plan again). `before`, `keep` and `keep_if` still apply, so a
search that finds labelled mail keeps it unless the rule names the label
or leaves `labelled` out of `keep`. A rule without `query` doesn't contact Gmail.

`--try RULE` plans one more rule without editing the rules file. `RULE` is
written as the inside of a TOML inline table, so the keys and checks are
the rules file's own; `name` defaults to `"try"`. The rule runs after the
file's rules, so its numbers count only what they don't already take
(`--rules none` runs it alone). Only its results are shown, no plan is
written unless `--plan FILE` is given, and `--review` writes only its
page. The output ends with the rule as a `[[rule]]` table, ready to paste
into the rules file.

`suggest` drafts rules from what `report bulk` finds, and prints them as
TOML, planned:

```bash
mailcellar offload suggest                     # every account; read-only
mailcellar offload suggest personal --before 2 --min-messages 20
mailcellar offload suggest --append            # also add them to rules.toml, commented out
```

Per account, it suggests at most four rules, each listing what it selects
one per line, with its figures, so you can cut the list down:

- **bulk lists**: mailing lists the bulk report calls candidates: mostly
  bulk mail, at least `--min-messages` messages (default 10), never engaged
  with (no thread you wrote in, no reply, no one you wrote to), no user
  label;
- **bulk senders**: senders of the same kind, their bulk mail only;
- **notifications**: senders with as many messages whose address looks
  automated (`noreply@`, `alerts@`, `billing@` and the like), not sent as
  bulk, never engaged with, no user label;
- **large mail**: messages over `--larger-than-mb` (default 5).

Each rule's `before` is `--before` (default 1, a year ago). Each keeps
`keep`'s default and sets no `forget_after`; the bulk rules carry a
commented `# forget_after = 2` as a hint. Each is planned after the rules
file's rules and the suggestions above it, and says how much it would
take after the protections; one that would take nothing isn't printed.
`--append` adds the suggestions to the rules file commented out, under a
dated header: uncomment a rule to use it. Nothing else is written, and an
uncommented rule still goes through plan, label, review and trash.

For a rule that selects thousands of messages, `--review` also writes one
HTML page per rule to `<data root>/offload/review/` (open it in a browser):
what the rule selects, in one line, the summary tables, a random sample of what the rule would offload and of
what each `keep` reason kept, the largest messages, the threads with the most
messages in the offload, and attachment types by size. It shows senders
and subjects, never message bodies, and is for you alone: keep it on your
machine. The same plan shows the same sample.

```bash
mailcellar offload apply                       # dry run: mailbox and backup checks
mailcellar offload apply --label --limit 2000  # label the next 2,000 offload-candidate
mailcellar backup personal                     # trash needs a backup newer than the plan
mailcellar offload apply --trash --limit 2000  # trash the next 2,000 labelled ones
```

Repeated runs with `--limit` move on through the plan; `--trash` takes only
the messages a `--label` run labelled, as its apply log shows. A message
restored since the plan was made is left out, as one already offloaded is. Removing the
label in the mailbox vetoes a message for good. A message the mailbox can't
be asked about, even after retries, is skipped and tried again by the next run. Trashed messages are recorded as
offloaded, so `backup --prune` keeps them in the backup after the mailbox
empties its Trash.

On an IMAP account (one the server lets mailcellar move mail on), the
label is a `$mailcellar-` keyword, or with `setup --review folder` a move
into `mailcellar/review/<label>`: clearing the keyword, or moving the
message out, vetoes it. Trash is the folder the server marks `\Trash`.
See [Backing up an IMAP account](../guide/imap.md#3-offload-and-remove-duplicates).

**How long it takes:**
- labelling is fast: thousands of messages a minute;
- trashing reads each message back to confirm it is in Trash; on Gmail,
  which takes a thousand messages per call, 2,000 messages take about a
  minute;
- run large batches in the background, or in batches with `--limit`.

Each trash run writes a manifest,
`<data root>/offload/batches/<batch>/offloaded.csv`: every message it
offloaded, with its `.eml` path, date, sender, subject and rule. Every plan
is also kept in `offload/plans/`, and the apply log names the plan each run
used. To put offloaded mail back, see [Restoring mail](restore.md).

`report storage` shows, per batch, how much the mailbox has freed: the
messages whose Trash has been emptied (as the last `backup` or `count`
found) and their size, what is still in Trash (and on Gmail, when it will be
emptied), and what was restored since. Gmail's own storage figure isn't available through its
API, so the sizes are its per-message estimates.

## Forgetting offloaded mail

Some offloaded mail shouldn't be kept at all, even in the backup.
[`forget`](forget.md) deletes it from the backup, in stages, once the
mailbox has deleted it too; a rule's `forget_after` makes it a standing
policy ([retention](forget.md#retention-forget_after)). It was
`offload forget`, which still works for a release.

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
| `--limit N` | Only the first N plan rows still to do (not offloaded, or restored since the plan was made) |
| `--yes` | Don't ask before trashing |
| `--plan PLAN` | Plan CSV from `plan` |
| `--log LOG` | Apply log, appended to (default `<data root>/offload/offload-apply-log.csv`) |
| `--config CONFIG` | Config folder |
