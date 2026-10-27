# Workspace: backing up through a service account

A Google Workspace domain can let mailcellar read its mailboxes through a
**service account** with domain-wide delegation, instead of each person
signing in. Backups then run unattended for every address you set up,
with no token to renew, ever.

It is **read-only**, on purpose. The service account is granted
`gmail.readonly` and nothing more, and mailcellar refuses anything else
through it. Google's `gmail.modify` scope also allows sending mail, so a
key with it, if it leaked, could trash mail in every mailbox and send mail
as anyone in the domain. Offloading, removing duplicates and restoring
still need each user's own sign-in ([Setting up Google
Cloud](google-cloud.md)).

For one or two mailboxes, each person signing in is simpler: with the
consent screen set to **Internal**, a Workspace token doesn't expire after
7 days. The service account pays off when there are more.

You need to be a **super administrator** of the Workspace domain, and to
use a Google Cloud project with the Gmail API enabled (steps 1 and 2 of
[Setting up Google Cloud](google-cloud.md)).

## 1. Create the service account and its key

In the [Google Cloud console](https://console.cloud.google.com/iam-admin/serviceaccounts),
in your project: **IAM & Admin > Service accounts > Create service
account**. Give it a name such as `mailcellar-backup`. It needs no roles in
the project: its access comes from the Workspace domain, in step 2.

Open it, then **Keys > Add key > Create new key > JSON**. The key downloads
to your computer. Treat it like a password for every mailbox in the domain.

Note its **Client ID**: under **Advanced settings** on the service
account's page, or `client_id` in the key file.

### If key creation is disabled

Google Cloud organisations created on or after 3 May 2024 block
service-account keys by default (the organisation policy "Disable service
account key creation", `iam.disableServiceAccountKeyCreation`). To allow
it for this project only:

1. Give yourself the **Organization Policy Administrator** role on the
   organisation (**IAM & Admin > IAM**, with the organisation selected).
2. Select the project, open **IAM & Admin > Organization Policies >
   Disable service account key creation**, choose **Override parent's
   policy**, and set it to **Not enforced**.
3. Create the key as above.
4. Set the policy back to **Inherit parent's policy**. The key you made
   keeps working; no more can be made.

## 2. Grant domain-wide delegation, read-only

In the [Admin console](https://admin.google.com/ac/owl/domainwidedelegation):
**Security > Access and data control > API controls > Manage Domain Wide
Delegation > Add new**.

- **Client ID:** the service account's, from step 1.
- **OAuth scopes:** exactly one,
  `https://www.googleapis.com/auth/gmail.readonly`.

Click **Authorize**. Google says changes "can take up to 24 hours but
typically happen more quickly".

## 3. Add the addresses to mailcellar

```bash
mailcellar setup alex@example.org --service-account ~/Downloads/project-1a2b3c.json
```

`setup` checks the file is a service-account key, installs it once for the
domain at `<config>/service-accounts/example.org.json`, readable only by
you, lists the address in `accounts.toml`, and reads the mailbox once to
check the delegation works. Then delete the downloaded copy of the key.

Each further address in the domain is one more `setup`, which uses the key
already installed:

```bash
mailcellar setup sam@example.org --service-account ~/.config/mailcellar/service-accounts/example.org.json
```

From then on `mailcellar backup`, `count`, `meta`, `verify`,
`schedule install` and the reports work for these addresses with no
sign-in. `mailcellar config` shows `readonly (service account)` for them,
and `schedule status` says they have no token to renew.

To let someone offload or restore their own mail as well, give their
account an OAuth client too (`setup ADDRESS --client FILE`): those
commands then ask them to sign in, as for any account.

## Looking after the key

- It reads every mailbox in the domain. Keep the config folder private,
  out of shared folders and synced backups, as for tokens.
- mailcellar refuses to use the key if others can read the file.
- To stop it, delete the entry in **Manage Domain Wide Delegation**, or
  delete the key in the Cloud console. To replace it, make a new key, run
  `setup ADDRESS --service-account NEWKEY --force`, then delete the old
  key.

## Problems

**"Google refused the service account (unauthorized_client …)":** the
delegation isn't in place yet, or has the wrong client ID or scope. Check
the entry in **Manage Domain Wide Delegation** lists the client ID with
`gmail.readonly`, and wait a while after adding it. A scheduled backup that
fails this way is recorded as a failed run (`access-refused`), so
`report health` and `schedule status` show it.

**"… its private key can't be read":** the key file was damaged, perhaps
in copying; `setup` refuses it before installing anything. Download a new
key.

**"needs the account's own sign-in, and … has no OAuth client":** a
command that changes Gmail (offload, dedup, restore) was run for an
address that only has the service account. Add an OAuth client for it
with `setup ADDRESS --client FILE`.

**"… can be read by others":** the key file's permissions were widened,
perhaps by copying it. Run the `chmod 600` the message gives.
