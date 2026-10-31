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
once for the domain, owner-only, and the account is marked to read through
it, so its backups need no sign-in. Other accounts in the domain are
unchanged until they are set up the same way. Anything that
changes Gmail still needs the user's own OAuth client and sign-in.

An IMAP account (--imap HOST, #111) is set up with its server and password:
imap.toml in its config folder, the password owner-only beside it. It is
backed up only; nothing on the server changes. Microsoft's servers are
refused before the password is asked: they take only OAuth over IMAP.

The decisions (check_client, accounts_entries, pick_name, add_account,
latest_download, imap_toml, microsoft_refusal,
server_report) are pure; reading, writing and asking are a thin layer
around them. Prompts are skipped for what the command line already gives, so
a fully specified call (ADDRESS and --client) asks nothing.
"""

import argparse
import getpass
import json
import logging
import re
import shlex
import sys
import tomllib
from contextlib import closing
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
# Microsoft's mail, which signs in over IMAP by OAuth only: by its servers, which every
# address there uses (Microsoft 365's own domains too), or by its main domains, whatever
# server was typed
MICROSOFT_HOSTS = ("outlook.office365.com", "outlook.office.com", "imap-mail.outlook.com")
MICROSOFT_DOMAINS = ("outlook.com", "hotmail.com", "live.com", "msn.com")


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


def private_status(data: bytes, dest: Path, force: bool, what: str, why: str) -> str:
    """
    What writing `data` to `dest` would do: 'added', 'kept' or 'replaced'.
    Raises SetupError for a different file there without --force. Writes nothing.
    """
    if not dest.is_file():
        return "added"
    if dest.read_bytes() == data:
        return "kept"
    if not force:
        raise SetupError(f"{dest} holds a different {what}; --force replaces it ({why})")
    return "replaced"


def install_private(data: bytes, dest: Path, force: bool, what: str, why: str) -> str:
    """Write `data` to `dest`, owner-only, in a private folder. 'added', 'kept' or 'replaced'."""
    status = private_status(data, dest, force, what, why)
    if status != "kept":
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_private(dest, data)
    return status


CLIENT_WHY = "the account's tokens belong to the old client, so Google asks for consent again"
IMAP_WHY = "the account reads from the server it names"
KEY_WHY = "every account in the domain set up with it reads through the new one"


def make_config_root(config: Path) -> None:
    """
    Create the config folder owner-only, if it isn't there yet: mkdir with
    parents gives only the last folder its mode, so creating an account's
    folder first left the config root open to other users.
    """
    if not config.is_dir():
        config.mkdir(parents=True, mode=0o700)


def install_client(source: Path, folder: Path, force: bool) -> str:
    """Copy the client into the account's folder, owner-only. 'added', 'kept' or 'replaced'."""
    return install_private(source.read_bytes(), folder / CLIENT_FILE, force, "client", CLIENT_WHY)


def read_service_account(source: Path, config: Path, address: str) -> tuple[bytes, Path]:
    """The key's bytes, checked, and where it goes for the address's domain. Writes nothing."""
    text = source.read_text()
    try:
        gmailapi.check_service_account(text)
        dest = gmailapi.service_account_path(config, address.rpartition("@")[2])
    except gmailapi.GmailError as exc:
        raise SetupError(f"{source}: {exc}") from None
    return text.encode(), dest


def toml_string(text: str) -> str:
    """A TOML basic string: quotes, backslashes and control characters escaped. Pure."""
    escaped = (
        "\\" + ch if ch in '"\\' else f"\\u{ord(ch):04x}" if ch < " " or ch == "\x7f" else ch
        for ch in text
    )
    return '"' + "".join(escaped) + '"'


def imap_toml(settings: gmailapi.ImapSettings, address: str) -> str:
    """imap.toml's text for these settings, leaving out what is the default. Pure."""
    lines = [f"host = {toml_string(settings.host)}"]
    if settings.port != 993:
        lines.append(f"port = {settings.port}")
    if settings.username != address:
        lines.append(f"username = {toml_string(settings.username)}")
    if settings.cafile is not None:
        lines.append(f"cafile = {toml_string(str(settings.cafile))}")
    if settings.review != gmailapi.DEFAULT_REVIEW:
        lines.append(f"review = {toml_string(settings.review)}")
    return "\n".join(lines) + "\n"


IMAP_KEPT = ("port", "username", "cafile", "review")  # what setting up again keeps


def kept_settings(folder: Path, address: str) -> dict:
    """
    The settings an account's imap.toml holds that setting up again keeps unless
    given (port, username, cafile, review); none from a file that is missing or
    doesn't pass imap_settings' checks, which setting up again then replaces.
    """
    try:
        current = gmailapi.imap_settings(folder, address)
    except gmailapi.GmailError, OSError:
        return {}
    if current is None:
        return {}
    cafile = str(current.cafile) if current.cafile is not None else None
    values = dict(
        zip(IMAP_KEPT, (current.port, current.username, cafile, current.review), strict=True)
    )
    return {k: v for k, v in values.items() if v is not None}


def gmail_files(folder: Path) -> list[str]:
    """What in an account's config folder makes it a Gmail account: client, tokens, key."""
    if not folder.is_dir():
        return []
    named = [n for n in (CLIENT_FILE, gmailapi.SERVICE_ACCOUNT_MARKER) if (folder / n).is_file()]
    return named + sorted(p.name for p in folder.glob("*token-*.json"))


def server_report(info) -> list[str]:
    """What setup says about an IMAP server: its folders, and what it allows. Pure."""
    from mailcellar.imap import BY_NAME, HIDDEN

    kept = [f for f in info.folders if f.label not in HIDDEN]
    # Roles taken from a name, where the server flags no folder for them. Sent and
    # Drafts are only relabelled; a folder of the user's own taken for Spam or Trash
    # would be left out, so that gets the warning.
    named = [
        f
        for f in info.folders
        if f.label in set(BY_NAME.values()) - info.marked and f.name.upper() != "INBOX"
    ]
    by_name = [f"{f.name!r} as {f.label}" for f in named]
    hides = any(f.label in HIDDEN for f in named)
    labels = sorted({f.label for f in kept})
    shown = ", ".join(labels[:8]) + (", …" if len(labels) > 8 else "")
    left_out = sorted({f.label for f in info.folders} & HIDDEN)
    return [
        f"  folders backed up: {len(kept)} ({shown})",
        *(
            [f"  read, not backed up: {', '.join(left_out)} (mail moved there stays backed up)"]
            if left_out
            else []
        ),
        *(
            [
                f"  known by name (the server flags none): {', '.join(by_name)}"
                + ("; rename a folder of your own taken for Spam or Trash" if hides else "")
            ]
            if by_name
            else []
        ),
        "  backup: read-only (EXAMINE and BODY.PEEK: not even \\Seen changes)",
        "  offload and dedup: "
        + (
            "possible (MOVE and a special-use Trash)"
            if info.can_offload
            else "refused on this server (they need MOVE and a special-use Trash)"
        ),
        "  restore: possible"
        + ("" if info.can_move else " (uploads only: without MOVE, mail in its Trash stays there)"),
    ]


def show_steps() -> None:
    print("You need an OAuth client of type Desktop app, made in a Google Cloud project of your")
    print("own. Google's console changes often; the guide has each step and what to choose:")
    print(f"  {GUIDE}\n")
    for i, (what, url) in enumerate(CONSOLE, 1):
        print(f"  {i}. {what}\n     {url}")
    print()


def microsoft_refusal(address: str, host: str) -> str:
    """
    Why setup won't add this IMAP account, or "": Microsoft's servers accept
    only OAuth over IMAP, which mailcellar doesn't do, so a password always
    fails there. Pure.
    """
    domain = address.rpartition("@")[2].lower()
    if domain not in MICROSOFT_DOMAINS and host.lower() not in MICROSOFT_HOSTS:
        return ""
    return (
        f"{address} at {host}: Microsoft's mail (Outlook.com, Hotmail, Live, Microsoft 365) "
        "accepts only OAuth sign-in over IMAP, not a password, and mailcellar signs in to "
        "IMAP with a password only; nothing was written"
    )


def checked_address(raw: str) -> str:
    address = raw.strip()
    if not ADDRESS_RE.match(address):
        raise SetupError(f"{address!r} is not an email address")
    return address


def checked_client(raw: str) -> Path:
    """The OAuth client file named, checked to be a Desktop app client."""
    source = parse_path(raw)
    if not source.is_file():
        raise SetupError(f"{source}: no such file")
    check_client(source.read_text())
    return source


def write_account(
    config: Path, text: str, entries, name: str, address: str, client: Path | None, force: bool
) -> str | None:
    """
    Write what any account needs: the config root (owner-only), the account's
    folder, its OAuth client if one is given, and its line in accounts.toml.
    Returns the client's status ('added', 'kept', 'replaced'), or None.
    """
    make_config_root(config)
    folder = config / name
    folder.mkdir(exist_ok=True, mode=0o700)
    status = install_client(client, folder, force) if client is not None else None
    if name not in entries:
        (config / gmailapi.ACCOUNTS_FILE).write_text(add_account(text, name, address))
    return status


def run(args) -> int:
    config = Path(args.config)
    accounts_file = config / gmailapi.ACCOUNTS_FILE
    text = accounts_file.read_text() if accounts_file.is_file() else ""
    entries = accounts_entries(text)

    if args.imap is not None:
        return run_imap(args, config, text, entries)
    stray = [o for o in IMAP_KEPT if getattr(args, o) is not None]
    if stray:
        raise SetupError(f"{', '.join('--' + o for o in stray)}: only with --imap")
    if args.service_account is not None:
        return run_service_account(args, config, text, entries)
    if args.client is None:
        show_steps()
    address = checked_address(args.address or ask("Gmail or Workspace address to back up"))
    name = pick_name(entries, address, args.name)
    if args.client is None:
        offer = latest_download(Path.home() / "Downloads") or existing_client(config, entries)
        source = checked_client(ask("Path to the downloaded client JSON", str(offer or "")))
    else:
        source = checked_client(args.client)

    folder = config / name
    installed = write_account(config, text, entries, name, address, source, args.force)
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


def run_service_account(args, config: Path, text: str, entries) -> int:
    """setup ADDRESS --service-account KEY: a Workspace address read through the key."""
    if not args.address or not ADDRESS_RE.match(args.address.strip()):
        raise SetupError("--service-account needs the address to back up")
    address = args.address.strip()
    name = pick_name(entries, address, args.name)
    source = parse_path(args.service_account)
    if not source.is_file():
        raise SetupError(f"{source}: no such file")
    folder = config / name
    # Everything is checked before anything is written: a key, once installed, serves
    # every account in the domain set up with it.
    data, key = read_service_account(source, config, address)
    key_status = private_status(data, key, args.force, "service-account key", KEY_WHY)
    client = None
    if args.client is not None:
        client = checked_client(args.client)
        private_status(client.read_bytes(), folder / CLIENT_FILE, args.force, "client", CLIENT_WHY)

    make_config_root(config)
    install_private(data, key, args.force, "service-account key", KEY_WHY)
    write_account(config, text, entries, name, address, client, args.force)
    # This account, and only this one, reads through the key (gmailapi.service_account_key).
    (folder / gmailapi.SERVICE_ACCOUNT_MARKER).write_text(f"{key.name}\n")
    print(f"{address} is account '{name}', read through the service account in {key}")
    print(
        f"  key: {key_status}"
        + (
            " (every account in the domain set up with it reads through the new one)"
            if key_status == "replaced"
            else ""
        )
    )
    print(f"  accounts.toml: {'already listed' if name in entries else 'added'}")
    print("  read-only: backups need no sign-in; anything that changes Gmail needs the")
    if (folder / CLIENT_FILE).is_file():
        print("  user's own sign-in, with the account's OAuth client")
    else:
        print(
            f"  user's own sign-in (mailcellar setup {address} --client FILE adds an OAuth client)"
        )
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


def run_imap(args, config: Path, text: str, entries) -> int:
    """setup ADDRESS --imap HOST: an IMAP account, backed up only."""
    if not args.address:
        raise SetupError("--imap needs the address to back up")
    if args.service_account is not None or args.client is not None:
        raise SetupError("--imap takes neither --client nor --service-account")
    address = checked_address(args.address)
    refusal = microsoft_refusal(address, args.imap)
    if refusal:
        raise SetupError(refusal)
    name = pick_name(entries, address, args.name)
    folder = config / name
    held = gmail_files(folder)
    if held and not gmailapi.is_imap(folder):
        raise SetupError(
            f"{address} is set up as a Gmail account ({', '.join(held)} in {folder}), "
            "and its backup holds Gmail's message IDs; an address is backed up one way only"
        )
    # Absolute: a scheduled backup runs from another folder.
    cafile = str(Path(args.cafile).expanduser().resolve()) if args.cafile else None
    options = {"port": args.port, "username": args.username, "cafile": cafile}
    given = {k: v for k, v in (options | {"review": args.review}).items() if v is not None}
    # What imap.toml already holds stays unless an option changes it: setting up again
    # for a new password shouldn't drop the port, login name, CA file or review mode.
    try:
        settings = gmailapi.settings_from(
            kept_settings(folder, address) | given | {"host": args.imap},
            address,
            "--imap",
        )
    except gmailapi.GmailError as exc:
        raise SetupError(str(exc)) from None
    toml = imap_toml(settings, address).encode()
    dest = folder / gmailapi.IMAP_SETTINGS
    toml_status = private_status(toml, dest, args.force, "imap.toml", IMAP_WHY)
    try:
        password = getpass.getpass(f"Password for {settings.username} at {settings.host}: ")
    except EOFError:
        raise SetupError("the password: nothing to read; run setup in a terminal") from None
    if not password:
        raise SetupError("no password given")
    # Everything is checked before anything is written: signing in, unless --no-auth.
    info = None
    if not args.no_auth:
        from mailcellar import imap

        print(f"Signing in to {settings.host} as {settings.username} (read-only)...")
        try:
            with closing(imap.ImapAccount(address, settings, password)) as box:
                info = box.server_info()
        except gmailapi.GmailError as exc:
            raise SetupError(f"{exc}; nothing was written") from None

    # The account's line last: listed without imap.toml, it would be taken for Gmail.
    make_config_root(config)
    install_private(toml, dest, args.force, "imap.toml", IMAP_WHY)
    install_private(f"{password}\n".encode(), folder / gmailapi.IMAP_PASSWORD, True, "", "")
    write_account(config, text, entries, name, address, None, args.force)
    print(f"{address} is account '{name}', an IMAP account at {settings.host}")
    print(f"  imap.toml: {toml_status}; password: kept owner-only beside it, in {folder}")
    print(f"  accounts.toml: {'already listed' if name in entries else 'added'}")
    if info is None:
        print(f"\nNot signed in (--no-auth): mailcellar backup {name} will check it.")
        return 0
    print("\n".join(server_report(info)))
    print(f"\nNext: mailcellar backup {name}")
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
    p.add_argument(
        "--imap",
        metavar="HOST",
        help="An IMAP account on this server (port 993, TLS): backed up only, read-only; "
        "asks for the password and keeps it owner-only in the config folder",
    )
    p.add_argument("--port", type=int, help="With --imap: the server's port (default 993)")
    p.add_argument("--username", help="With --imap: the login name (default: the address)")
    p.add_argument(
        "--cafile",
        metavar="FILE",
        help="With --imap: a CA certificate to trust besides the system's (a private server)",
    )
    p.add_argument(
        "--review",
        choices=gmailapi.REVIEW_MODES,
        help="With --imap: how offload and dedup mark mail for review, a keyword (the "
        "default) or a mailcellar/review/ folder",
    )
    p.add_argument("--name", help="Config name for the account (default: from the address)")
    p.add_argument("--no-auth", action="store_true", help="Don't sign in now")
    p.add_argument(
        "--force",
        action="store_true",
        help="Replace the account's client, service-account key or imap.toml if it holds another",
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
