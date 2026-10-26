# mailcellar

**Gmail storage full? Move old mail out of Gmail without losing it.**

mailcellar keeps a local backup of your Gmail accounts. You can then take old
mail out of Gmail by rule while the backup keeps it, and it stays searchable,
exportable and restorable whenever you need it.

```bash
uv tool install mailcellar      # or: pipx install mailcellar
mailcellar config               # where the config and data live
mailcellar backup work          # the first backup; Google asks for consent once
```

- **Back up** every account incrementally, in Got Your Back's format; an
  existing gyb backup carries on where it is.
- **Offload** old mail by rule, in stages you check: plan, review, label,
  look in Gmail, then trash. Nothing is ever permanently deleted from Gmail,
  and the backup is checked for every message before it goes.
- **Find and restore** it later: `mailcellar search` searches the backups,
  offloaded mail included, and `mailcellar restore` puts mail back into
  Gmail, into the same account or another.
- **Deduplicate, export and report**: remove duplicate copies within and
  across accounts, copy mail out as an mbox or a zip, and see what takes up
  the space.

It runs on your own computer with your own Google Cloud project: there is no
service in between, and your mail goes nowhere but between Gmail and your
disk. mailcellar is in beta; it runs on macOS and Linux and needs Python
3.14, which [uv](https://docs.astral.sh/uv/) installs for you.

## Where to start

1. [Set up Google Cloud](guide/google-cloud.md): the project and OAuth
   client mailcellar signs in with. About ten minutes, once.
2. Run `mailcellar setup` with the OAuth client's JSON, then follow the
   [quick start](https://github.com/mailcellar/mailcellar#quick-start)
   to make the first backup.
3. [Schedule backups](guide/scheduling.md), so they happen at least every
   four weeks.
4. When you are ready to free space, [offload old mail](guide/offloading.md),
   one stage at a time.

Coming from gyb? [Migrating from gyb](guide/from-gyb.md) continues your
existing backup in place. Something went wrong?
[Recovering](guide/recovery.md) and [Troubleshooting](guide/troubleshooting.md)
cover the usual cases.

Every command and option is described in the
[command reference](https://github.com/mailcellar/mailcellar#readme), and
[How it works](architecture.md) explains the design.
