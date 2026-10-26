# Setting up Google Cloud for mailcellar

mailcellar reads your mail through the Gmail API, so it needs an *OAuth
client*: a credential, made in a Google Cloud project that you own, that lets
the program ask you for permission. It is free, takes about ten minutes, and
is the hardest step of setup. You do it once; one client can serve every
account.

Google changes its console often, so this page names what to look for and
links to Google's own instructions for each screen rather than showing
screenshots. If a menu has moved, Google's page is the one to follow; the
result you need is stated in each step.

You need:

- a Google account to own the project. It does not have to be a mailbox you
  back up, but it can be: with only a personal `@gmail.com` account, that
  one account owns the project, is its test user and is the mailbox backed
  up.
- the address of each Gmail or Google Workspace account to back up.

An account enrolled in Google's
[Advanced Protection Program](https://support.google.com/accounts/answer/7539956)
can't use mailcellar: it lets only Google's apps and apps Google has
verified reach the account, and a client you make yourself is never
verified, in Testing or in production. Back up such an account only after
leaving the programme, or not at all.

## 1. Create a Google Cloud project

Open the [Google Cloud console](https://console.cloud.google.com/) and
create a project, with any name (`mailcellar` will do), by following
[Create a Google Cloud project](https://developers.google.com/workspace/guides/create-project).
Make sure it is the selected project, shown at the top of the console,
for the steps that follow. You need no billing account.

## 2. Enable the Gmail API

In that project, enable the **Gmail API**, as described in
[Enable Google Workspace APIs](https://developers.google.com/workspace/guides/enable-apis).
Enable nothing else; mailcellar uses no other API.

## 3. Set up the OAuth consent screen

The consent screen is what Google shows when mailcellar asks for access.
Follow
[Configure OAuth consent](https://developers.google.com/workspace/guides/configure-oauth-consent)
and choose:

- **App name and support email:** anything; only you will see them.
- **Audience (user type):** **External** for a personal `@gmail.com` account.
  For a Google Workspace account, **Internal** is simpler: it limits the app
  to your organisation and skips the test-user list below. (Internal is only
  offered when the project belongs to that Workspace organisation.)
- **Contact information:** your address.

You do not need to add scopes here; mailcellar asks for them when it runs.

### Testing or In production

An External app has a *publishing status*, shown on the consent screen's
Audience page:

| Status | Who can use it | What it means for mailcellar |
| --- | --- | --- |
| **Testing** | Only the *test users* you list, up to 100 | Google **expires the tokens after 7 days**. The next `mailcellar backup` opens the browser again to ask for consent. |
| **In production** | Anyone who has the client | Tokens last until you revoke them. Google shows an "unverified app" warning at consent, because the app has not been through Google's review; for your own use you can continue past it. |

Testing is the default and needs no more work, but expect to approve access
weekly. mailcellar warns when a token is 5 days past consent, and
`mailcellar auth ACCOUNT --scope readonly --renew` renews it before it runs
out (see [recovery](recovery.md#a-token-expired-or-was-revoked)). **Publish app**, on the same page, moves it to production. Google's
verification review only matters if more than a hundred users are to approve
the app, which a program you run for yourself never needs.
Google's rules on both are in
[OAuth 2.0 for installed applications](https://developers.google.com/identity/protocols/oauth2#expiration).

When an expired token is found, mailcellar says so ("Stored ... token ... was
rejected") and starts a new consent flow; nothing is lost.

### Add yourself as a test user

While the status is **Testing**, add the address of every account you will
back up under **Test users** (Audience page). An account that is not listed
gets "Error 403: access_denied" at consent. Skip this once you are in
production, or if the app is Internal.

## 4. Create an OAuth client

Create a credential of type **OAuth client ID**, with application type
**Desktop app**, following
[Create access credentials](https://developers.google.com/workspace/guides/create-credentials#oauth-client-id)
(the console path is *Google Auth platform → Clients*, or *APIs & Services →
Credentials*). The name is yours to choose.

Other types will not work: mailcellar signs in through a temporary web
server on your own machine, which only the *Desktop app* type allows.

Then **download the JSON**. The file is named `client_secret_<long
number>.apps.googleusercontent.com.json`. Save it as
`client_secrets.json`, in a folder per account in the config folder:

```
~/.config/mailcellar/<name>/client_secrets.json
```

`mailcellar setup` does the rest of this step for you: it checks the file is a
Desktop app client, saves it there, adds the account to `accounts.toml` and
signs in (step 6), and asks for the address and the file's path if you don't
give them:

```bash
mailcellar setup alex@example.com --client ~/Downloads/client_secret_*.json
```

To do it by hand instead, `<name>` is the short name you give the account in
`accounts.toml`:

```toml
# ~/.config/mailcellar/accounts.toml
personal = "alex@example.com"
```

so the example above goes in `~/.config/mailcellar/personal/`. The
[README](https://github.com/mailcellar/mailcellar#configuration) describes the config folder, and
`mailcellar config` shows where it is on your machine and whether each
account's client is found.

The file holds the client's secret. Keep it private like the tokens that sit
beside it, and never commit it or paste it anywhere. For a desktop app,
Google does not treat the secret as confidential from the person running the
program, but it still identifies *your* project.

## 5. Using one client for several accounts

The client belongs to the project, not to an account. Copy the same JSON into
each account's folder:

```
~/.config/mailcellar/personal/client_secrets.json
~/.config/mailcellar/work/client_secrets.json     # the same file
```

Each account still signs in separately and gets its own tokens in its own
folder. With the app in **Testing**, add each account as a test user (step
3). Use a separate client per account only if you want the projects kept apart,
for example a Workspace account with a client in its own organisation's
project.

## 6. The first backup

```bash
mailcellar config        # check the folders and that each account has a client
mailcellar backup work   # opens the browser once per account
```

mailcellar prints `Authorising work@... for gmail.readonly` and opens your
browser (if it cannot, it prints the address to open). On the way:

1. **Choose the account.** The one named in `accounts.toml` is preselected.
   Sign in as that account, not another one you happen to be signed in to.
2. **"Google hasn't verified this app".** Expected, since you made the app
   yourself. Choose **Advanced**, then **Go to *app name* (unsafe)**. It is
   your own project, so the warning is about Google not having reviewed it.
3. **The permission list.** Google asks for *Read all resources and their
   metadata — no write operations*, the Gmail permission
   (`gmail.readonly`). **Tick it.** The box is unticked by default, and
   *Continue* with it unticked gives mailcellar nothing, so the next
   step fails with "Google did not grant gmail.readonly". If that happens,
   delete the `dedup-token-readonly.json` it names, in the account's folder,
   and run the command again.
4. The browser shows "The authentication flow has completed". Return to the
   terminal; the backup starts.

A backup needs only read access. Later, `dedup apply`, `offload apply` and
`restore --yes` ask once more, for *modify* access, the same way; see
[Backups need only read access](https://github.com/mailcellar/mailcellar#what-changes-gmail).

The tokens are saved beside the client as `dedup-token-readonly.json` and
`dedup-token-modify.json`, readable only by you. To revoke access, delete
them and remove mailcellar at
[Google Account → Security → Third-party access](https://myaccount.google.com/connections).

## Problems

| You see | Cause |
| --- | --- |
| `Error 403: access_denied`, "has not completed the Google verification process" | The app is in Testing and the account is not a test user (step 3). |
| `Error 400: redirect_uri_mismatch`, or "invalid_client" | The client is not of type *Desktop app*, or the JSON is from another project. Create a new client (step 4). |
| "Access blocked: ... Gmail API has not been used" or a `403` from the API | The Gmail API is not enabled in the project that owns the client (step 2). |
| Asked to approve again after a week | Testing status (step 3). Publish the app, or accept it. |
| "Stored ... token ... was rejected" | Revoked, or expired as above. Approve again; nothing else to do. |
| "Google did not grant gmail.readonly" | The Gmail box was not ticked (step 6). |
| Consent refused for an account in Advanced Protection | The programme admits only verified apps; see *You need* above. |
| Workspace account blocked by an admin | Your administrator controls which apps may reach mail. Ask them to allow the client, or use a project in your own organisation. |
