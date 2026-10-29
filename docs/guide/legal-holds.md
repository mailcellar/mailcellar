---
description: "Keep mail exactly as it is for a dispute, an audit or a records request: a hold stops mailcellar removing it from the mailbox or the backup until you release it."
---

# Keeping mail on hold

Sometimes mail has to be kept exactly as it is: you're in a dispute with a
landlord, an employer or an insurer, an accountant or a tax office has
questions, or someone has asked for their data. A **hold** makes sure
nothing mailcellar does removes that mail, from Gmail or from the backup,
until you release it. This guide walks through one, with invented names.

## 1. Place the hold

Say you're disputing a deposit with a letting agent at `lettings.example`,
and the tenancy began in September 2025:

```bash
mailcellar hold add deposit-2026 --reason "deposit dispute" \
    --party @lettings.example --after 2025-09-01
```

```
alex@example.com: hold 'deposit-2026' placed; it keeps 37 messages now
  party @lettings.example · from 2025-09-01
```

`--party` matches From, To, Cc and Bcc, so the mail you sent them is
covered too. Add more `--party` options for other people involved (a
deposit scheme, a solicitor), `--label` for a label you already use, or
`--id` for particular messages. Every kind of criterion you give must
match; several `--party` options match any of them.

The hold follows its criteria: the reply that arrives next week is held
too, as soon as a backup has it. So keep your backups running
([scheduled backups](scheduling.md)) while a hold is in force; `report
health` warns if a week passes without one.

## 2. Carry on as usual

Offloading and removing duplicates go on as before, and leave the held
mail alone: `offload apply` and `dedup apply` skip each held message, with
`held: deposit-2026` as the reason in their log.

`backup --prune` keeps held mail in the backup even after Gmail deletes it,
and `forget` won't forget it. Nothing stops Gmail itself deleting
a message, or you deleting it in Gmail, but the backup keeps its copy.

## 3. Check what it keeps

```bash
mailcellar hold show deposit-2026
```

```
alex@example.com: hold 'deposit-2026', in force, placed 2026-03-02
  reason: deposit dispute
  covers: party @lettings.example · from 2025-09-01
  keeps 41 messages, 2025-09-03 to 2026-04-18
  2 of them Gmail no longer has, as far as the backup knows: it holds the only copy
```

`mailcellar verify --chain` shows the backup hasn't changed since each
message was recorded, and the hold itself is recorded in the same hash
chain, so when it began and what it covered can't be rewritten later.

## 4. Hand the mail over

When a solicitor or an adjudicator asks for the correspondence:

```bash
mailcellar export alex --hold deposit-2026 --to ~/deposit-2026.zip
```

The zip has each message as an `.eml` file and an index of dates,
senders and subjects. `--to ….mbox` gives one file instead, for a mail
client.

## 5. Release it

Once the matter is settled:

```bash
mailcellar hold release deposit-2026
```

The mail is no longer held, but the hold's record stays (`hold list --all`),
with when it was placed and released. `--until DATE` when placing a hold
ends it by itself on that date, for a hold with a known end, such as a
tax enquiry window.

Every option is in the [reference](../reference/hold.md).
