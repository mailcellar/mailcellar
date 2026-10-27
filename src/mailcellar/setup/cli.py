"""
Command line for mailcellar setup: from a downloaded OAuth client to an account
that is ready to back up.

Creating the Google Cloud project and its OAuth client is Google's console and
stays by hand (docs/guide/google-cloud.md). Everything after that was copying
a file to the right folder, naming it in accounts.toml and signing in; this
does those three, checks the file is a Desktop app client (the usual mistake),
and prints what to do next. It changes nothing in Gmail: the sign-in is the
read-only one, the same as `mailcellar auth ACCOUNT --scope readonly`.

A Workspace address can instead read through a service account with
domain-wide delegation (--service-account KEY, #103): the key is installed
once for the domain, owner-only, and backups need no sign-in. Anything that
changes Gmail still needs the user's own OAuth client and sign-in.

The decisions (check_client, accounts_entries, pick_name, add_account,
latest_download) are pure; reading, writing and asking are a thin layer
around them. Prompts are skipped for what the command line already gives, so
a fully specified call (ADDRESS and --client) asks nothing.
"""

import argparse
import json
import logging
import re
import shlex
import sys
import tomllib
from pathlib import Path

from mailcellar import gmailapi
from mailcellar.paths import config_root, write_private

logger = logging.getLogger(__name__)

GUIDE = "https://mailcellar.github.io/mailcellar/guide/google-cloud/"
CONSOLE = (
    ("Create a project", "https://console.cloud.google.com/projectcreate"),
    ("Enable the Gmail API", "https://console.cloud.google.com/apis/library/gmail.googleapis.com"),
    ("Set up the consent screen, then create a Desktop app client and download its JSON",
     "https://console.cloud.google.com/apis/credentials"),
)  # fmt: skip
CLIENT_FILE = "client_secrets.json"  # in <config>/<name>/, where gmailapi looks
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")  # a bare TOML key, and a folder name
ADDRESS_RE = re.compile(r'^[^@\s"\\]+@[^@\s"\\]+$')
CLIENT_KEYS = ("client_id", "client_secret", "auth_uri", "token_uri")


class SetupError(Exception):
    """Something the user can fix; its message says what."""


def check_client(text: str) -> None:
    """Raise SetupError unless `text` is the JSON of a Desktop app OAuth client."""
    try:
        data = json.loads(text)
    except ValueError:
        raise SetupError("that is not a JSON file") from None
    if not isinstance(data, dict):
        raise SetupError("that is not an OAuth client file")
    if "web" in data and "installed" not in data:
        raise SetupError(
            f"that is a Web application client; create one of type Desktop app (step 4 of {GUIDE})"
        )
    installed = data.get("installed")
    if not isinstance(installed, dict):
        raise SetupError(
            'that is not an OAuth client file (it has no "installed" section); '
            "download the JSON of a Desktop app client"
        )
    missing = [k for k in CLIENT_KEYS if not isinstance(installed.get(k), str)]
    if missing:
        raise SetupError(f"the client file is incomplete: no {', '.join(missing)}")


def accounts_entries(text: str) -> dict[str, str]:
    """config name → address, from the text of accounts.toml ('' for no file)."""
    try:
        entries = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SetupError(f"accounts.toml is not valid TOML ({exc})") from None
    bad = [n for n, a in entries.items() if not isinstance(a, str)]
    if bad:
        raise SetupError(f"accounts.toml: {bad[0]} is not an address")
    return dict(entries)


def pick_name(entries: dict[str, str], address: str, requested: str | None) -> str:
    """
    The config name for `address`: the one asked for; else the name it already
    has (so a repeat run refreshes that account); else one made from the part
    before the @, made unique with the domain, then a number. Raises SetupError
    for a name that is taken by another address, or an address held under
    another name.
    """
    held = [n for n, a in entries.items() if a.lower() == address.lower()]
    if requested is not None:
        if not NAME_RE.match(requested):
            raise SetupError(f"{requested!r}: a name uses letters, digits, - and _ only")
        if requested in entries and requested not in held:
            raise SetupError(f"the name {requested!r} is already used for {entries[requested]}")
        if held and requested not in held:
            raise SetupError(f"{address} is already configured as {held[0]!r}")
        return requested
    if held:
        return held[0]
    local, _, domain = address.partition("@")
    base = re.sub(r"[^A-Za-z0-9_-]+", "-", local).strip("-").lower() or "account"
    for candidate in (base, f"{base}-{domain.split('.')[0].lower()}"):
        if NAME_RE.match(candidate) and candidate not in entries:
            return candidate
    n = 2
    while f"{base}-{n}" in entries:
        n += 1
    return f"{base}-{n}"


def add_account(text: str, name: str, address: str) -> str:
    """accounts.toml with `name = "address"` added; the rest is left as it was."""
    if text and not text.endswith("\n"):
        text += "\n"
    return f'{text}{name} = "{address}"\n'


def latest_download(folder: Path) -> Path | None:
    """The newest client_secret*.json in `folder` (where Google's download lands), if any."""
    try:
        found = [p for p in folder.glob("client_secret*.json") if p.is_file()]
        return max(found, key=lambda p: p.stat().st_mtime, default=None)
    except OSError:
        return None


def parse_path(raw: str) -> Path:
    """A path as typed or dragged into a terminal: quotes and escaped spaces undone, ~ expanded."""
    try:
        parts = shlex.split(raw)
    except ValueError:
        parts = []
    return Path(parts[0] if len(parts) == 1 else raw.strip()).expanduser()


def ask(question: str, default: str = "") -> str:
    try:
        answer = input(f"{question}{f' [{default}]' if default else ''}: ").strip()
    except EOFError:
        raise SetupError(f"{question}: nothing to read; pass it on the command line") from None
    return answer or default


def existing_client(config: Path, entries: dict[str, str]) -> Path | None:
    """The client file of the first configured account that has one (to reuse for another)."""
    for name in entries:
        path = config / name / CLIENT_FILE
        if path.is_file():
            return path
    return None


def install_private(data: bytes, dest: Path, force: bool, what: str, why: str) -> str:
    """Write `data` to `dest`, owner-only, in a private folder. 'added', 'kept' or 'replaced'."""
    status = "added"
    if dest.is_file():
        if dest.read_bytes() == data:
            return "kept"
        if not force:
            raise SetupError(f"{dest} holds a different {what}; --force replaces it ({why})")
        status = "replaced"
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_private(dest, data)
    return status


def install_client(source: Path, folder: Path, force: bool) -> str:
    """Copy the client into the account's folder, owner-only. 'added', 'kept' or 'replaced'."""
    return install_private(
        source.read_bytes(),
        folder / CLIENT_FILE,
        force,
        "client",
        "the account's tokens belong to the old client, so Google asks for consent again",
    )


def install_service_account(source: Path, config: Path, address: str, force: bool) -> Path:
    """Check the key and install it for the address's domain, owner-only. Returns where."""
    text = source.read_text()
    try:
        gmailapi.check_service_account(text)
    except gmailapi.GmailError as exc:
        raise SetupError(f"{source}: {exc}") from None
    dest = gmailapi.service_account_path(config, address.rpartition("@")[2])
    install_private(
        text.encode(),
        dest,
        force,
        "service-account key",
        "every account in the domain reads through the new one",
    )
    return dest


def show_steps() -> None:
    print("You need an OAuth client of type Desktop app, made in a Google Cloud project of your")
    print("own. Google's console changes often; the guide has each step and what to choose:")
    print(f"  {GUIDE}\n")
    for i, (what, url) in enumerate(CONSOLE, 1):
        print(f"  {i}. {what}\n     {url}")
    print()


def run(args) -> int:
    config = Path(args.config)
    accounts_file = config / gmailapi.ACCOUNTS_FILE
    text = accounts_file.read_text() if accounts_file.is_file() else ""
    entries = accounts_entries(text)

    if args.service_account is not None:
        return run_service_account(args, config, accounts_file, text, entries)
    if args.client is None:
        show_steps()
    address = (args.address or ask("Gmail or Workspace address to back up")).strip()
    if not ADDRESS_RE.match(address):
        raise SetupError(f"{address!r} is not an email address")
    name = pick_name(entries, address, args.name)

    if args.client is not None:
        source = parse_path(args.client)
    else:
        offer = latest_download(Path.home() / "Downloads") or existing_client(config, entries)
        source = parse_path(ask("Path to the downloaded client JSON", str(offer or "")))
    if not source.is_file():
        raise SetupError(f"{source}: no such file")
    check_client(source.read_text())

    folder = config / name
    installed = install_client(source, folder, args.force)
    if name not in entries:
        config.mkdir(parents=True, exist_ok=True, mode=0o700)
        accounts_file.write_text(add_account(text, name, address))
    print(f"{address} is account '{name}': client {installed} in {folder}")
    print(f"  accounts.toml: {'already listed' if name in entries else 'added'}")

    if args.no_auth:
        print(f"\nSign in when ready: mailcellar auth {name} --scope readonly")
        return 0
    print(f"\nSigning in {address} (read-only access; nothing in Gmail changes)...")
    from mailcellar.dedup.cli import main as dedup_main

    try:
        dedup_main(
            ["auth", name, "--scope", "readonly", "--config", str(config)], prog="mailcellar"
        )
    except SystemExit as exc:
        if exc.code not in (None, 0):
            print(f"\nThe account is set up; to finish: mailcellar auth {name} --scope readonly")
            return 1
    print(f"\nNext: mailcellar backup {name}      # the first backup; later ones take seconds")
    print("      mailcellar schedule install   # run backups weekly, unattended")
    print("With the consent screen in Testing status Google expires the token after 7 days:")
    print(f"publish the app, or renew with mailcellar auth {name} --scope readonly --renew.")
    return 0


def run_service_account(args, config: Path, accounts_file: Path, text: str, entries) -> int:
    """setup ADDRESS --service-account KEY: a Workspace address read through the key."""
    if not args.address or not ADDRESS_RE.match(args.address.strip()):
        raise SetupError("--service-account needs the address to back up")
    address = args.address.strip()
    name = pick_name(entries, address, args.name)
    source = parse_path(args.service_account)
    if not source.is_file():
        raise SetupError(f"{source}: no such file")
    key = install_service_account(source, config, address, args.force)
    folder = config / name
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.client is not None:
        client = parse_path(args.client)
        if not client.is_file():
            raise SetupError(f"{client}: no such file")
        check_client(client.read_text())
        install_client(client, folder, args.force)
    if name not in entries:
        accounts_file.write_text(add_account(text, name, address))
    print(f"{address} is account '{name}', read through the service account in {key}")
    print(f"  accounts.toml: {'already listed' if name in entries else 'added'}")
    print("  read-only: backups need no sign-in; anything that changes Gmail needs the")
    print(f"  user's own sign-in (mailcellar setup {address} --client FILE adds an OAuth client)")
    if args.no_auth:
        return 0
    print(f"\nChecking {address} through the service account (read-only)...")
    try:
        creds = gmailapi.load_credentials(folder, address, "readonly")
        gmailapi.GmailAccount(address, creds)  # confirms the mailbox identity
    except gmailapi.GmailError as exc:
        print(f"\nThe account is set up, but the check failed: {exc}")
        return 1
    print(f"OK.\n\nNext: mailcellar backup {name}")
    return 0


def main(argv=None, prog: str | None = None) -> int:
    p = argparse.ArgumentParser(
        prog=prog or "mailcellar setup",
        description="Add an account from a downloaded OAuth client: copies the client "
        "into the config folder, lists the account in accounts.toml and signs in "
        "(read-only). Asks for what is not given. Changes nothing in Gmail.",
    )
    p.add_argument("address", nargs="?", help="The Gmail or Workspace address to back up")
    p.add_argument(
        "--client",
        metavar="FILE",
        help="The OAuth client JSON downloaded from Google (a Desktop app client)",
    )
    p.add_argument(
        "--service-account",
        metavar="KEY",
        help="A Workspace address: read it through this service-account key, granted "
        "domain-wide delegation for gmail.readonly (installed once for the domain; no sign-in)",
    )
    p.add_argument("--name", help="Config name for the account (default: from the address)")
    p.add_argument("--no-auth", action="store_true", help="Don't sign in now")
    p.add_argument(
        "--force", action="store_true", help="Replace the account's client if it holds another"
    )
    p.add_argument(
        "--config",
        default=str(config_root()),
        help=f"Config folder: accounts.toml, settings.toml, one folder per account "
        f"(default: {config_root()})",
    )
    args = p.parse_args(argv)
    try:
        return run(args)
    except SetupError as exc:
        sys.exit(f"error: {exc}")
