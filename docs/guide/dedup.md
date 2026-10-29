---
description: "Remove duplicate emails from Gmail or IMAP, found in your local backups: forwarded copies across accounts and repeats within one, in stages, never deleted permanently."
---

# Removing duplicate mail: a worked example

Duplicates come from two places: mail that one account auto-forwards to
another, so both mailboxes hold a copy, and mail that arrived twice in the
same account (a re-sent message, an import run twice). `dedup` finds them in
your backups and removes the extra copies from Gmail, in stages you check.
Nothing is deleted permanently: the last step moves messages to Gmail's
Trash, which Gmail empties after 30 days.

This page walks through one run. The mail and addresses in it are invented.
Every option is in the [reference](../reference/dedup.md).

On an IMAP account the stages are the same, with a keyword or a review
folder in place of the label; see
[Backing up an IMAP account](imap.md#3-offload-and-remove-duplicates).

| Stage | Command | What changes |
| --- | --- | --- |
| 1. Back up, index | `backup`, `dedup index` | the backup grows; mailcellar's own index |
| 2. Match | `dedup match` | nothing |
| 3. Plan | `dedup plan` | only mailcellar's own plan file |
| 4. Dry run | `dedup apply` | nothing |
| 5. Label | `dedup apply --label` | Gmail: adds the label `dedup-redundant` |
| 6. Review | in Gmail | you remove the label from anything to keep |
| 7. Trash | `dedup apply --trash` | Gmail: moves labelled mail to Trash |
| 8. Check | `count` | nothing |

Stages 1 to 4 can be repeated as often as you like. Stages 5 and 7 need
modify access to the account: each asks you to sign in in the browser when
it runs, and keeps that access only until it ends.

## 1. Back up, then index

`dedup` works on the backup, not on Gmail, and before it removes a copy it
checks that the copy is in the backup. So back up first, then catalogue:

```bash
mailcellar backup
mailcellar dedup index
```

`index` reads each backup's `meta.sqlite` and builds a small index in
`<data root>/dedup/`. It is only an index: deleting it is safe, and `dedup
index` builds it again. If an account's `meta.sqlite` lacks rows for older
mail, run `mailcellar meta` first.

## 2. See what matches

```bash
mailcellar dedup match
```

This prints the groups of copies of the same message, split into **cross**
groups (copies in different accounts) and **within** groups (copies in one
account), and how big they are. Two copies are the same message when their
visible text and attachments are identical **and** they share a Message-ID
or the same From, Date and Subject. Sender, date and subject alone would
group different messages, such as two invoices, so they are never used on
their own. [How it works](../architecture.md#deduplication) has the reasoning.

Message-IDs that were reused for different content are reported, not
grouped, so they can't cause a removal. `--csv FILE` writes every matched
copy to a file if you want to look through them.

## 3. Plan

The plan decides, for each copy, `keep`, `remove` or `review`. You choose how
by saying which copies win.

**Across accounts.** Suppose `work` auto-forwards to `personal`, so every
work message exists twice.

```bash
mailcellar dedup plan --keep origin
```

`--keep origin` removes only copies created by auto-forwarding between
your accounts, and keeps the one in the account the message came from.
Mail you received in both accounts independently is left alone, because it
isn't a copy of anything. This is the safe choice when you don't have a
reason for another.

```bash
mailcellar dedup plan --keep personal
```

`--keep personal` keeps every copy in `personal` and removes the other
accounts' copies of the same messages, forwarded or not. Use it when one
account is where you want everything to live. A group with no copy in
`personal` goes to `review`.

**Within one account.**

```bash
mailcellar dedup plan --scope within
```

For each group it keeps one copy from the largest set of copies with the
same labels and removes the others. A copy with different labels goes to
`review`, since removing it would lose a label. `--scope both` plans both
kinds, and `--keep` is then needed for the cross part.

**What goes to `review`, not `remove`.** The plan won't remove a copy when:

- it is a sent message or a draft;
- the copy that would be kept is in Trash or Spam;
- the copy carries stars or user labels that the kept copy lacks.

If a label is of no value, name it with `--ignore-label NAME` (repeat it for
more) and copies that differ only by it are removed. Nothing in the
`review` rows is ever touched by `apply`: they are for you to decide by
hand in Gmail.

The plan is a file you can open, `<data root>/dedup/dedup-plan.csv`. Each
row has the action, the reason, the group, the account and Gmail ID, the
copy that is kept, and the sender, subject, labels and size. Open it in a
spreadsheet and sort by `action` and `reason`. If something looks wrong,
change the options and plan again: a new plan replaces the old file.

## 4. Dry run

```bash
mailcellar dedup apply
```

With no flag, `apply` changes nothing. It checks every planned removal
against live Gmail and the backup and reports what it would do. A removal
passes only if:

- the copy's `.eml` is in the backup;
- the message and the copy that is kept are both still in Gmail;
- the kept copy isn't in Trash or Spam;
- the message's labels haven't changed since the plan was made.

Anything that fails is skipped, with the reason. A plan that is days old
may skip some; plan again after a new backup. A passing removal shows
`would label or trash`.

## 5. Label

Start small:

```bash
mailcellar dedup apply --label --limit 5
```

This looks at the first five removals in the plan and adds the label
`dedup-redundant` (`--label NAME` to choose another) to those that pass the
checks. Open Gmail and look at the label:
each message should be an extra copy of one you can see elsewhere.

When you are satisfied, label the rest by leaving out `--limit`, or in
steps.

## 6. Review in Gmail

The label is the veto. **Remove `dedup-redundant` from any message you want
to keep**, and `apply --trash` skips it, for good. Messages you leave
labelled are the ones that will go.

## 7. Trash

```bash
mailcellar backup                      # so the backup is current
mailcellar dedup apply --trash --limit 5
```

`--trash` moves labelled messages to Trash. It repeats the checks of the dry
run, so a message whose labels changed since the plan is skipped, and it
asks once per account: `Move N messages in <account> to Trash? [y/N]`
(`--yes` skips the question). Trashing takes about a minute for 2,000
messages, so for tens of thousands run it in the background or with
`--limit`.

Each run is appended to `<data root>/dedup/dedup-apply-log.csv`.

Gmail empties Trash after 30 days. Until then you can move a message back by
hand in Gmail.

## 8. Check

```bash
mailcellar count
```

`count` compares Gmail with the backup. The trashed copies stay in the
backup, so after Gmail empties its Trash they will show as in the backup but
not in Gmail.

**Do not use `forget --deleted` to clear them out unthinkingly.** It
protects mail that was *offloaded* or restored, because those are recorded
in the backup. `dedup` records nothing, so removed duplicates look like mail
Gmail lost, and `forget --deleted` will offer to take them out of the
backup. They are duplicates, and they are held where `--undo` can put
them back until `--empty` deletes them, but read what it lists before you
type the count. See
[Backing up](backing-up.md#mail-deleted-from-gmail-forget-deleted).

## If it went wrong

- **Before you trashed:** remove the label from what you want to keep, or
  delete the label from every message. Nothing else has changed.
- **After trashing, within 30 days:** open Gmail's Trash and move the
  messages back. Search `label:dedup-redundant in:trash`.
- **After Gmail emptied the Trash:** the copy is in the backup. Put it back
  with `mailcellar restore ACCOUNT --id GMAIL_ID`; the ID is in the plan
  and in the apply log. A copy you removed was a duplicate of one that is
  still in Gmail, so this is rarely needed. See
  [Restoring mail](restore.md).
