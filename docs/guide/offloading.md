# Offloading old mail safely: a worked example

Offloading takes old mail out of Gmail, to free its storage, while the local
backup keeps it. It is staged so that you look at the result before anything
leaves Gmail, and nothing is ever deleted permanently: the last step moves
messages to Gmail's Trash, which Gmail empties after 30 days.

This page walks through one offload from start to finish. The mail in it is
invented. The README has the reference for each command
([Offloading old mail](../reference/offload.md)).

On an IMAP account the stages are the same, with a keyword or a review
folder in place of the label; see
[Backing up an IMAP account](imap.md#3-offload-and-remove-duplicates).

The stages, and where to stop and check:

| Stage | Command | What changes |
| --- | --- | --- |
| 1. Write a rule | edit `rules.toml` | nothing |
| 2. Plan | `offload plan` | only mailcellar's own files in the data root |
| 3. Dry run | `offload apply` | nothing |
| 4. Label | `offload apply --label` | Gmail: adds the label `offload-candidate` |
| 5. Review | in Gmail | you remove the label from anything to keep |
| 6. Fresh backup | `backup` | adds to the backup |
| 7. Trash | `offload apply --trash` | Gmail: moves labelled mail to Trash |
| 8. Check | `count`, `report health` | nothing |

Before you start, have a current backup of the account and a second copy of
the data root somewhere else (another disk, Time Machine, a sync folder you
trust). Once Gmail empties its Trash, the backup holds the only copy of
what you offloaded.

## 1. Write a rule

Rules live in `<data root>/offload/rules.toml` (`mailcellar config` shows the
data root), one `[[rule]]` table each. Suppose a work account has years of
automated notifications from a build system, and you want those out of
Gmail except the ones about releases:

```toml
[[rule]]
name = "old build notifications"
account = "work"
sender = "@ci.example.com"
before = "2023-01-01"
except = { subject = "release" }
```

- `name` is recorded with every message the rule offloads, and is how you
  select it later.
- `before` is required: a date, or a number of years ago (`"5"`). Mail
  newer than it is never selected.
- The scope keys say what to select: `label`, `sender` (an address or
  `@domain`), `list`, `forwarded_from`, `category`, `subject`, `to_me`,
  `larger_than_mb`, and `query`, a search written as in Gmail's search box
  (`"has:attachment older_than:5y"`), which `plan` runs in Gmail,
  read-only. At least one is needed; different keys must all
  match, and a key given a list matches any of its values. Here one key,
  `sender`, does it.
- `except` takes the same keys and keeps a message that matches **any** of
  them.
- `account` limits the rule to one account (an address, config name or a
  unique part of an address); without it the rule covers every account.

A mistake in the file stops the plan with an error: unknown keys, a rule
with no scope, a missing `before`. A typo cannot quietly widen a rule.

### What is protected

Whatever the rule selects, mailcellar keeps in Gmail, by default:

- **sent**: your own mail (sent, drafts, from one of your addresses);
- **starred**;
- **thread**: any message in a thread you wrote in;
- **replied**: a message you replied to;
- **labels**: a message with a user label the rule does not name.

`waive = ["thread"]` drops a default protection for one rule, and
`protect = ["correspondents"]` adds one (keeps mail from anyone you have
written to). Waive only what you have a reason to, and read the plan
afterwards to see what the rule now takes. A `query` rule finds mail
across your labels, so **labels** often keeps much of what it finds: the
plan's "kept: another user label" line says how much.

## 2. Plan

```bash
mailcellar backup work           # the plan reads the backup, so bring it up to date
mailcellar offload plan work
```

`plan` changes nothing in Gmail or the backup. It writes the plan to
`<data root>/offload/offload-plan.csv` (and keeps a copy in
`offload/plans/`), and prints, for each rule:

- how many messages it would offload and how much space they take, by year
  and by sender (`--top N` sets how many senders are shown);
- how many it kept, and which protection kept them.

For a rule that selects more than you can read through, add `--review`:
it writes a page per rule to `<data root>/offload/review/`, to open in a
browser, with a random sample of what the rule would offload and of what
each protection keeps, the largest messages, the threads with most messages
in the offload, and attachment types. It shows senders and subjects only;
keep it to yourself.

Read it against what you expected. If the numbers are far larger than you
had in mind, or a sender you recognise is in the list, adjust the rule and
plan again; each run replaces the plan file.

Two typical adjustments:

- **Too much.** Tighten `before`, add a scope key, or add an `except`.
- **A message must stay.** Give it a user label in Gmail (any label that
  the rule does not name, so the `labels` protection keeps it), run
  `backup`, and plan again.

`--csv DIR` also writes the summary tables as CSV, for a spreadsheet.

## 3. A dry run

```bash
mailcellar offload apply
```

With no flag, `apply` is a dry run: it checks each planned message against
live Gmail and against the backup and reports what it would do. Nothing
changes. Per message it checks that:

- the message is still in Gmail, and its labels have not changed since the
  plan (a message that gained or lost a user label is skipped);
- the backup holds it: the `.eml` file parses, its content matches what
  `meta.sqlite` recorded, and the record was made by the current parser. A
  message failing this is skipped with the reason, usually
  `run mailcellar meta`;
- it has not been offloaded already.

Fix what the reasons point at, or accept the skips; skipped messages simply
stay in Gmail. A message Gmail doesn't answer for, even after retries, is
skipped too (`couldn't read it from its mailbox`), and the next run tries it
again; it never stops the run.

## 4. Label

```bash
mailcellar offload apply --label --limit 2000
```

This adds the Gmail label `offload-candidate` to the next 2,000 planned
messages. Labelling is fast, and reversible: it changes no message, only
tags it. `--limit` takes plan rows in order, and a repeated run carries on
where the last stopped. `--rule NAME` and an account name narrow it down,
for a first pass over one rule.

Start small. Label a few hundred messages with `--limit 200` first, review
them (step 5), and only then label the rest.

The label must not exist on any planned message already; if it does,
`apply` refuses to continue, because `--trash` would trash messages nobody
had reviewed. Use `--label OTHERNAME` and the same name with `--trash` to
choose another.

## 5. Review in Gmail

Open the `offload-candidate` label in Gmail and look through it. Search
within it by sender or date to look for surprises. For anything you want to
keep, **remove the label**.

Removing the label is a veto: `--trash` only trashes messages that still
carry it, and a message it finds unlabelled is skipped for good, not
labelled again by a later `--label` run. You can also add a different user
label to a message; the next `apply` sees the labels changed since the plan
and skips it.

## 6. A fresh backup

```bash
mailcellar backup work
```

`--trash` refuses to run until each account's last backup succeeded **and**
finished after the plan was made, so that the backup is known to be
complete when mail leaves Gmail. If you skip this step, `apply --trash`
tells you which account needs one.

## 7. Trash

```bash
mailcellar offload apply --trash --limit 2000
```

`--trash` works through the messages a `--label` run labelled, and only
those: the rest of the plan isn't read from Gmail, since nothing unlabelled
is trashed. Before it moves anything, it repeats the Gmail and backup
checks of step 3, prints a reminder that Trash is emptied after 30 days
and, on a Mac, whether Time Machine includes the data root and has run since
the last backup. On other systems it asks you to check your second copy
yourself. It then asks, once per account, `Move N messages in <account> to
Trash? [y/N]`. (`--yes` skips the question; use it only for batches you have
reviewed.)

Trashing moves a thousand messages per Gmail call, then reads each one back
and records it as offloaded only if Gmail shows it in Trash. 2,000 messages
take about a minute. For a large batch, run it in the background, or in
steps of a few thousand with `--limit`.

What is recorded:

- each trashed message goes into the account's `meta.sqlite` as offloaded,
  so `backup --prune` never moves it out of the backup;
- a manifest of the batch, `<data root>/offload/batches/<batch>/offloaded.csv`:
  every message, with its `.eml` path, date, sender, subject and rule;
- a line per message in `<data root>/offload/offload-apply-log.csv`, naming
  the plan it used.

If the run is interrupted, run it again: messages already trashed but not
yet recorded are found (in Trash, still carrying the label) and recorded
on the next run, and the rest carry on.

## 8. Check

```bash
mailcellar count work
mailcellar report health work
```

`count` compares Gmail with the backup. Offloaded mail is no longer in Gmail
on purpose, and it is listed apart from mail that is missing, so what you
want to see is no mail missing from the backup. `report health` shows the
backup's completeness, its recent runs and how much mail is offloaded.

Gmail's storage figure drops only after the Trash is emptied, which
Gmail does 30 days after the move. Do not empty it earlier unless you are
certain of the backup and your second copy. `mailcellar report
storage work` shows each batch's progress: how much has been freed so far,
how much is still in Trash, and when Gmail will empty it.

## Finding offloaded mail

Gmail's search no longer finds mail once it is offloaded. `mailcellar search`
does, and says how to put each match back:

```bash
mailcellar search "build failed" work --offloaded
```

See [Searching mail](../reference/search.md).

## Changing your mind

Offloaded mail can be put back with `mailcellar restore`, which is a dry run
until you add `--yes`. The name of each batch is a folder in
`<data root>/offload/batches/`.

```bash
mailcellar restore work --batch 2026-01-15-093000          # dry run
mailcellar restore work --batch 2026-01-15-093000 --yes
mailcellar restore work --rule "old build notifications" --yes
```

- **Within Gmail's 30 days**, a message still in Trash is moved out of it,
  with its original ID, labels and thread. This is quick, and exact.
- **After that**, the message is uploaded again from the backup with its
  labels, dates and headers, under a new Gmail ID. It looks the same, but
  is a new message; the backup indexes the new ID, so the next backup does
  not download it again.

Restored mail loses the offload label and gains `restored/<date>`, so you can
find it. Restore only adds: it deletes nothing, in Gmail or the backup. See
[Restoring mail](../reference/restore.md) for every option.

## Deleting offloaded mail for good

Offloading keeps everything in the backup. For mail you shouldn't keep at
all, `offload forget` deletes it from the backup too, once Gmail has
deleted it:

```bash
mailcellar backup work                                     # records what Gmail has deleted
mailcellar offload forget --batch 2026-01-15-093000        # dry run
mailcellar offload forget --batch 2026-01-15-093000 --yes  # type the count to go ahead
```

Messages still in Gmail's Trash aren't touched, so run it after Gmail has
emptied its Trash (30 days after the batch, or sooner if you empty it
yourself) and a backup has run. Forgotten files are held for 30 days in
`<data root>/_forgotten/`, where `--undo RUN` can still put them back;
`offload forget --empty --yes` then deletes them; `report health` and the
next `offload forget` say when a run is due. Copies outside the data
root, such as an export or Time Machine, keep the mail. The details are in
the [reference](../reference/offload.md#forgetting-offloaded-mail).

To make that a standing policy, give the rule a `forget_after`, in years
from each message's date:

```toml
[[rule]]
name = "newsletters"
category = "promotions"
before = "1"
forget_after = 2
```

`mailcellar offload forget --due` then forgets what each rule's
`forget_after` makes due, through the same checks and the same 30-day
hold; `report health` shows how much is due. A legal hold always wins
([Keeping mail on hold](legal-holds.md)).
