"""
mailbox: what the commands need from a mail account, and the one place one
is opened.

    with closing(open_mailbox(account, config_folder, "readonly")) as gmail:
        ids = gmail.list_message_ids("before:2020/01/01")

`Mailbox` is the set of operations backup, dedup, offload and restore use;
`gmailapi.GmailAccount` provides it, and so does the tests' `FakeMailbox`
(tests/fake_mailbox.py). Another provider (IMAP, #108) would be a second
class, chosen by open_mailbox. Signing in and managing tokens
(`auth`, `setup`) stay in gmailapi: they are Gmail's.

The operations are Gmail's as they stand: IDs are Gmail message IDs, and
labels are Gmail label IDs or gyb-style names, as each method says.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol


class Mailbox(Protocol):
    """
    One account's mail, as backup, dedup, offload and restore use it. Provide
    it by having these methods, not by subclassing: an inherited method raises.
    """

    account: str
    provider: str  # "gmail" (the Gmail API), or "imap"
    id_kind: str  # whose message IDs: "gmail" (the API's, or Gmail's over IMAP), or "imap"

    # ── listing ──────────────────────────────────────────────────────────────
    def list_message_ids(
        self,
        query: str | None = None,
        include_spam_trash: bool = False,
        label_id: str | None = None,
    ) -> list[str]:
        """IDs of every message matching `query` and carrying `label_id` (all mail by default)."""
        raise NotImplementedError

    def labels_by_listing(self) -> dict[str, list[str]]:
        """Every message's label IDs, Spam and Trash included, from listings."""
        raise NotImplementedError

    # ── changes ──────────────────────────────────────────────────────────────
    def current_history_id(self) -> str:
        """A point in the mailbox's history to read changes from later."""
        raise NotImplementedError

    def history_changes(self, start_history_id: str) -> tuple[set[str], set[str], str]:
        """(IDs any change touched, IDs deleted, the latest point) since `start_history_id`."""
        raise NotImplementedError

    # ── content ──────────────────────────────────────────────────────────────
    def get_raw(self, gmail_ids: list[str], on_message: Callable[[dict], None]) -> list[str]:
        """Fetch whole messages, calling on_message(response) for each; returns failures."""
        raise NotImplementedError

    def message_details(
        self, gmail_ids: list[str], failures: list[str] | None = None
    ) -> dict[str, dict | None]:
        """threadId, labelIds, internalDate and sizeEstimate per message; None if gone."""
        raise NotImplementedError

    # ── labels ───────────────────────────────────────────────────────────────
    def message_labels(
        self, gmail_ids: list[str], failures: list[str] | None = None
    ) -> dict[str, set[str] | None]:
        """Current label names per message; None for messages that don't exist."""
        raise NotImplementedError

    def label_catalogue(self) -> list[dict]:
        """The account's labels as {id, name, type}."""
        raise NotImplementedError

    def label_names(self, label_ids) -> list[str]:
        """gyb-style names for label IDs."""
        raise NotImplementedError

    def label_ids(self, names) -> list[str]:
        """IDs for gyb-style label names, creating user labels that don't exist."""
        raise NotImplementedError

    def add_label(self, gmail_ids: list[str], label_name: str, review: bool = True) -> list[str]:
        """
        Add a user label to messages, creating it if needed; returns "<id>: <why>"
        for those it couldn't mark, as trash() does. `review`: the label marks
        mail for review (offload, dedup), which an IMAP account may do by moving
        it into a review folder; restore's label is only a mark.
        """
        raise NotImplementedError

    def remove_label(self, gmail_ids: list[str], label_name: str) -> list[str]:
        """Remove a user label from messages; failures as "<id>: <why>", as add_label's."""
        raise NotImplementedError

    # ── moves ────────────────────────────────────────────────────────────────
    def trash(self, gmail_ids: list[str]) -> list[str]:
        """Move messages to Trash; returns "<id>: <why>" for those that didn't move."""
        raise NotImplementedError

    def untrash(self, gmail_ids: list[str]) -> list[str]:
        """Move messages out of Trash; returns failures as trash() does."""
        raise NotImplementedError

    def can_move(self) -> bool:
        """Whether untrash() can work here: an IMAP server without MOVE can't."""
        raise NotImplementedError

    def review_problem(self) -> str:
        """Why add_label(review=True) would be refused here, or "": for a dry run."""
        raise NotImplementedError

    # ── upload ───────────────────────────────────────────────────────────────
    def find_message_ids(self, message_ids: dict[str, str]) -> dict[str, list[str]]:
        """{key: Message-ID header} → {key: IDs of the messages with it}, Trash included."""
        raise NotImplementedError

    def import_messages(
        self,
        messages: dict[str, tuple[bytes, list[str]]],
        on_imported: Callable[[str, str], None],
        dates: dict[str, int] | None = None,
    ) -> list[str]:
        """
        Upload {key: (raw, label IDs)}, calling on_imported(key, new ID); returns
        failures. `dates` (key → ms) is when the backup says each was received,
        for a mailbox that keeps it (IMAP's INTERNALDATE); Gmail dates by header.
        """
        raise NotImplementedError

    # ── state kept in the backup ─────────────────────────────────────────────
    def save_state(self, con) -> None:
        """Write what the mailbox keeps in the backup's meta.sqlite (con). No commit."""
        raise NotImplementedError

    def unread(self) -> tuple[str, ...]:
        """
        What this run couldn't read and so left as it was in the backup (an
        IMAP account's folders a server refused or listed short); empty when
        everything was read.
        """
        raise NotImplementedError

    # ── the connection ───────────────────────────────────────────────────────
    def close(self) -> None:
        """
        Let go of the connection, if the mailbox holds one open (an IMAP
        account's); it isn't used again. Callers open it with
        contextlib.closing.
        """
        raise NotImplementedError


def open_mailbox(
    account: str,
    config_folder: Path,
    scope: str,
    interactive: bool = True,
    folder: Path | None = None,
) -> Mailbox:
    """
    The account's mailbox with `scope` ("readonly", "modify", or "append" for
    restore, which only uploads and asks can_move() before untrashing),
    signing in if needed and `interactive`. Raises gmailapi.GmailError when
    it can't, and for an IMAP account asked for a change it can't make
    safely (imap.write_refusal): offload and dedup ask for "modify", so they
    never open a server without MOVE. `folder`, the
    account's backup folder, keeps an IMAP account's UID map; without it the
    map lasts as long as the mailbox object.
    """
    from mailcellar import gmailapi

    if gmailapi.is_imap(config_folder):
        from mailcellar import imap

        settings = gmailapi.imap_settings(config_folder, account)
        box = imap.ImapAccount(
            account, settings, gmailapi.imap_password(config_folder), folder=folder
        )
        box.sign_in()  # as GmailAccount checks its identity: a refusal shows here
        if scope != "readonly":  # changes only where the server can make them safely
            refusal = imap.write_refusal(box.server_info(), scope)
            if refusal:
                box.close()
                raise gmailapi.GmailError(f"{account}: {refusal}")
        return box
    gmail_scope = "modify" if scope == "append" else scope  # Gmail's import needs modify
    creds = gmailapi.load_credentials(config_folder, account, gmail_scope, interactive=interactive)
    return gmailapi.GmailAccount(account, creds)


# Whose message IDs a backup holds (Mailbox.id_kind), as output names them.
ID_KINDS = {"gmail": "Gmail's message IDs", "imap": "IDs made from each message's headers"}


def mailbox_name(provider: str, start: bool = False) -> str:
    """
    How output names an account's mailbox: Gmail, or "the server" for an
    IMAP account; capitalised to start a sentence. Pure.
    """
    name = "Gmail" if provider == "gmail" else "the server"
    return name[:1].upper() + name[1:] if start else name


def opener(config_root: Path, folders=None) -> Callable[[str, str], Mailbox]:
    """
    open(account, scope) for accounts named in `config_root`'s accounts.toml:
    what dedup and offload pass around to open each account as they reach it.
    """
    from mailcellar import gmailapi

    def open_(account: str, scope: str) -> Mailbox:
        from mailcellar.paths import backup_folder

        # The backup folder lets an IMAP account read its UID map (read-only), so it
        # reads only new messages' headers, as a backup does: the command's own for the
        # account (`folders`), else the default one.
        return open_mailbox(
            account,
            gmailapi.find_config_folder(config_root, account),
            scope,
            folder=(folders or {}).get(account) or backup_folder(account),
        )

    return open_
