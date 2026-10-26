# Offloading old mail: offload

Rules go in `<data root>/offload/rules.toml`, one `[[rule]]` table each.
The keys are documented in `src/mailcellar/offload/rules.py`:

```toml
[[rule]]
name = "old work mail"
account = "work"
label = "work"           # or sender, list, forwarded_from, category, subject, to_me,
                         # larger_than_mb; different keys must all match
before = "2022-01-01"    # or a number of years ago, e.g. "5"
# except = { subject = "contract", to_me = true }   # keep what matches any of these
# waive = ["thread"]     # drop default protections (sent, starred, thread, replied, labels)
# protect = ["correspondents"]
```

```bash
mailcellar offload plan                    # all accounts → <data root>/offload/offload-plan.csv
mailcellar offload plan personal --top 30 --csv ~/reports/offload
mailcellar offload plan --review           # also a review page per rule
```

`plan` changes nothing. It prints what each rule would offload (by year and
sender) and what its protections keep. To keep particular messages, add a
user label in Gmail and run a backup.

For a rule that selects thousands of messages, `--review` also writes one
HTML page per rule to `<data root>/offload/review/` (open it in a browser):
the summary tables, a random sample of what the rule would offload and of
what each protection keeps, the largest messages, the threads with the most
messages in the offload, and attachment types by size. It shows senders
and subjects, never message bodies, and is for you alone: keep it on your
machine. The same plan shows the same sample.

```bash
mailcellar offload apply                       # dry run: Gmail and backup checks
mailcellar offload apply --label --limit 2000  # label the next 2,000 offload-candidate
mailcellar backup personal                     # trash needs a backup newer than the plan
mailcellar offload apply --trash --limit 2000  # trash the next 2,000 labelled ones
```

Repeated runs with `--limit` move on through the plan. Removing the label
in Gmail vetoes a message for good. Trashed messages are recorded as
offloaded, so `backup --sync` keeps them in the backup after Gmail empties
its Trash.

**How long it takes:**
- labelling is fast: thousands of messages a minute;
- trashing takes one Gmail call per message, about 4 a second, so 4,500
  messages take about 17 minutes;
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

### `offload apply`

A dry run unless `--label` or `--trash` is given.

| Option | What it does |
| --- | --- |
| `ACCOUNT ...` | Only these accounts' plan rows (default: all) |
| `--label [NAME]` | Add the label NAME (default `offload-candidate`) to messages that pass the checks; it is created if missing. Removing it in Gmail vetoes a message |
| `--trash [NAME]` | Move messages that pass the checks and carry the label NAME to Trash, and record them as offloaded |
| `--rule RULE` | Only this rule's rows (repeatable) |
| `--limit N` | Only the first N plan rows |
| `--yes` | Don't ask before trashing |
| `--plan PLAN` | Plan CSV from `plan` |
| `--log LOG` | Apply log, appended to (default `<data root>/offload/offload-apply-log.csv`) |
| `--config CONFIG` | Config folder |
