# mailcellar

**Gmail full? Keep every email on your own disk, and free the space.**

Your mail lives on someone else's server, in a mailbox with a size limit.
When it fills up you can pay for more storage, or delete mail you may need
later. mailcellar gives you a third way: a complete, checked backup on your
own disk, then old mail taken out of the mailbox by rules you write and
review, still searchable and restorable whenever you need it. It works
with Gmail and Google Workspace, and with any IMAP mailbox: iCloud Mail,
Yahoo Mail, Fastmail, or your own server.

```bash
uv tool install mailcellar      # or: pipx install mailcellar
mailcellar setup alex@example.org --client client_secret.json   # Gmail; or --imap HOST
mailcellar backup alex          # the first backup; resumes where it left off if stopped
mailcellar count alex           # what the mailbox has against what the backup has
```

## Why mailcellar

- **Your storage is full.** Offload old mail by rule (older than five
  years, newsletters, large attachments) and keep it in the backup: the
  space comes back and nothing is lost.
- **You have no backup of your own.** Providers keep your mail safe from
  their own failures, not from a deletion, a compromised account, or being
  locked out. A provider's export copies everything again each time, with
  nothing to say it's complete. mailcellar fetches only what changed,
  compares each account with its backup (`count`) and checks the files
  (`verify`).
- **Old mail must stay findable.** `search` covers every backup, offloaded
  mail included, and `restore` puts back one message, a batch or a whole
  account.
- **Your mail is with more than one provider.** One backup, one search and
  one set of reports cover Gmail and IMAP accounts side by side.
- **You're moving to another provider.** `restore --to` copies mail from
  one account to another, Gmail to IMAP or back: labels become folders and
  folders labels, and the backup records what moved.

## What it does

- **Back up** every account incrementally, in Got Your Back's format; an
  existing gyb backup carries on where it is.
- **Offload** old mail by rule, in stages you check: plan, review, mark,
  look in your mail client, then trash. The backup is checked for every
  message before it goes, and nothing is ever deleted permanently.
- **Find and restore**: search the backups, offloaded mail included, and
  put mail back into the same account or another.
- **Deduplicate, export and report**: remove duplicate copies within and
  across accounts, copy mail out as an mbox or a zip, and see what takes up
  the space.
- **Keep mail as it is**: a hash chain shows the backup hasn't changed
  since each message was saved (`verify --chain`), and a legal hold keeps
  the mail a dispute or an audit needs out of reach of offloading, pruning
  and dedup, in the mailbox and in the backup.

Only `offload apply`, `dedup apply` and `restore --yes` change a mailbox,
each a dry run until you add a flag; on an IMAP server, only where the
server can move mail safely ([What changes your mailbox](reference/what-changes-gmail.md)).

**Your mail stays yours.** mailcellar runs on your own computer: there is
no service in between. Gmail is read through your own Google Cloud project;
an IMAP account needs only its password (an app password, where the
provider asks for one). mailcellar is in beta; it runs
on macOS and Linux and needs Python 3.14, which
[uv](https://docs.astral.sh/uv/) installs for you.

## Where to start

1. **Add an account**, one of two ways:
    - **Gmail or Google Workspace:** [set up Google Cloud](guide/google-cloud.md),
      the project and OAuth client mailcellar signs in with (about ten
      minutes, once), then run `mailcellar setup` with the client's JSON.
    - **iCloud Mail, Yahoo Mail, Fastmail or your own server:** run
      `mailcellar setup ADDRESS --imap HOST` with the account's password (an
      app password, where the provider asks for one)
      ([Backing up an IMAP account](guide/imap.md) has the servers and
      what each one allows).
2. Follow the [quick start](https://github.com/mailcellar/mailcellar#quick-start)
   to make the first backup and check it.
3. [Schedule backups](guide/scheduling.md); for Gmail, at least every four
   weeks. Then [keep the backup safe](guide/backing-up.md): check it, and
   keep a second copy of it.
4. When you are ready to free space, [offload old mail](guide/offloading.md),
   one stage at a time.

Coming from gyb? [Migrating from gyb](guide/from-gyb.md) continues your
existing backup in place. Something went wrong?
[Recovering](guide/recovery.md) and [Troubleshooting](guide/troubleshooting.md)
cover the usual cases.

Every command and option is described in the
[command reference](reference/index.md), and
[How it works](architecture.md) explains the design.
