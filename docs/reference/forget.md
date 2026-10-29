# Forgetting mail: forget

Some offloaded mail shouldn't be kept at all, even in the backup.
`forget` deletes it from the backup, in stages, and only once the
mailbox has deleted it too:

```bash
mailcellar forget --batch 2026-01-15-093000          # dry run: what would go
mailcellar forget --batch 2026-01-15-093000 --yes    # asks you to type the count
mailcellar forget --undo 2026-02-20-101500           # put a held run back
mailcellar forget --empty --yes                      # delete runs held 30 days
```

- **Only what the mailbox no longer has.** A message can be forgotten once a
  `backup` or `count` has recorded it gone (the mailbox emptied its Trash),
  and the mailbox, asked again (read-only), still doesn't have it. The rest
  of the selection is listed with the reason and left alone: messages still
  in its Trash, ones it has again, and ones it couldn't be asked about.
- **Held 30 days.** The files move to `<data root>/_forgotten/<email>/<run>/`,
  with a manifest that `--undo RUN` uses to put them back; `mailcellar meta`
  then re-parses them. `--empty` deletes runs held 30 days or more, for
  good, after you type the count. `report health` lists the held runs and
  which are due, and `forget` says so when any are.
- **Nothing else keeps them.** Their rows leave `msg-db.sqlite` and
  `meta.sqlite` (attachment names included), the search index and the
  dedup catalogue, and each database is rewritten so their bytes don't
  linger in its free space. Their sender and subject are blanked in the
  offload and dedup plans, apply logs and batch manifests, which keep the
  ID, date, rule and size; the review pages are deleted.
- **What is left:** a `forgotten` record in `meta.sqlite` (ID, batch,
  rule, date, size, when forgotten and emptied). `count` and
  `report storage` show forgotten mail on a line of its own, and `restore`
  says it can't bring it back.
- **Other copies** are out of mailcellar's reach: any `export` you made, a
  second copy of the data root, Time Machine. The dry run says so, and on a
  Mac whether Time Machine covers the data root.
- **Legal holds win.** Mail a [hold](hold.md) covers is never forgotten.

## Retention: `forget_after`

A rule's `forget_after = N` (a whole number of years) is a standing policy:
mail the rule offloaded is due to be forgotten once it is more than N years
old, counted from the message's date. `forget --due` forgets what is
due, through every stage above, so mail still in the mailbox is never touched;
the rule's `before` offloads it first.

```bash
mailcellar forget --due                     # dry run: what is due, by rule and year
mailcellar forget --due --rule invoices     # only this rule
mailcellar forget --due --yes               # asks you to type the count
```

[`offload plan`](offload.md) and the review pages show each rule's `forget_after`;
`report health` and `schedule status` show how much is due, and nothing
runs it unattended. Mail whose rule has been renamed or removed is under no
policy and stays. `offload plan` and `--due` warn about a `forget_after` no
longer than the rule's `before`: everything it offloads would be due as soon
as the mailbox has deleted it. Each forget run's link in the backup's hash chain names
the rules it forgot for, and their `forget_after`.

## Options

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
| `--config CONFIG` | Config folder (the mailbox is asked, read-only, whether each message is gone) |
