"""
Command line for mailcellar auth: obtain or renew each account's Gmail token,
for every command that reads or changes Gmail. Changes nothing in the
mailbox. setup runs it read-only for a new account. It was `dedup auth`
until 0.21.0.
"""

import argparse
import sys
from pathlib import Path

from mailcellar.paths import config_root


def cmd_auth(args) -> None:
    """Obtain or renew the Gmail token for each account, changing nothing in the mailbox."""
    from datetime import UTC, datetime

    from mailcellar import gmailapi

    config_root = Path(args.config)
    try:
        known = gmailapi.config_accounts(config_root)
        accounts = [gmailapi.resolve_account(config_root, n)[0] for n in args.accounts] or sorted(
            known
        )
    except gmailapi.GmailError as exc:
        sys.exit(f"error: {exc}")

    failed = 0
    for account in accounts:
        folder = known[account]
        if gmailapi.is_imap(folder):
            print(f"  {account}: an IMAP account, signing in with its password: nothing to do")
            continue
        try:
            if args.scope == "modify" and not gmailapi.stores_modify_token(folder):
                print(
                    f"  {account}: gmail.modify tokens aren't stored; dedup apply, offload "
                    "apply and restore --yes sign in when they run. To store one, set "
                    "keep_modify_token = true in settings.toml"
                )
                continue
            # exact: a read-only token of its own, not the stored modify token's
            creds = gmailapi.load_credentials(
                folder, account, args.scope, renew=args.renew, exact=True
            )
            gmailapi.GmailAccount(account, creds)  # confirms the mailbox identity
        except gmailapi.GmailError as exc:
            print(f"  {account}: FAILED — {exc}")
            failed += 1
            continue
        used = args.scope
        if used == "readonly" and (key := gmailapi.service_account_key(folder, account)):
            print(f"  {account}: gmail.readonly OK, through the service account in {key}")
            if args.renew:
                marker = folder / gmailapi.SERVICE_ACCOUNT_MARKER
                print(
                    "    --renew doesn't apply: the service account has no token to renew. To "
                    f"have {account} sign in for itself instead, delete {marker} and run "
                    "this again."
                )
            continue
        authorised = gmailapi.token_authorised_at(folder, used)
        print(
            f"  {account}: gmail.{args.scope} OK"
            + (
                f", authorised {authorised:%Y-%m-%d}"
                f" ({gmailapi.token_age_days(authorised, datetime.now(UTC)):.0f} days ago)"
                if authorised
                else ", authorised before mailcellar recorded when"
            )
            + f" → {gmailapi.token_file(folder, used)}"
        )
    if failed:
        sys.exit(1)


def main(argv=None, prog: str | None = None) -> int:
    p = argparse.ArgumentParser(
        prog=prog or "mailcellar auth",
        description="Authorise Gmail access for accounts, or renew it; changes nothing in "
        "the mailbox",
    )
    p.add_argument(
        "accounts",
        nargs="*",
        help="Accounts (full address, config folder name, or a "
        "unique part of an address); default: every account "
        "in <config>/accounts.toml",
    )
    p.add_argument(
        "--scope",
        required=True,
        choices=["readonly", "modify"],
        help="readonly for backups, counts and reports; modify for the account "
        "apply --label/--trash or restore --yes changes, stored only with "
        "keep_modify_token = true in settings.toml",
    )
    p.add_argument(
        "--renew",
        action="store_true",
        help="Ask for consent again even if the token still works, starting Google's "
        "7 days afresh (the Testing-status expiry); use it before a token runs out",
    )
    p.add_argument(
        "--config",
        default=str(config_root()),
        help="Config folder: accounts.toml, settings.toml, one folder per account "
        f"(default: {config_root()})",
    )
    cmd_auth(p.parse_args(argv))
    return 0
