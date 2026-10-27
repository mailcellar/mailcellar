"""
mailbox: what the commands need from a mail account, and the one place one
is opened.

    gmail = open_mailbox(account, config_folder, "readonly")
    ids = gmail.list_message_ids("before:2020/01/01")

`Mailbox` is the set of operations backup, dedup, offload and restore use;
`gmailapi.GmailAccount` provides it today. Another provider (IMAP, #108)
would be a second class, chosen by open_mailbox. Signing in and managing tokens
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

    def add_label(self, gmail_ids: list[str], label_name: str) -> None:
        """Add a user label to messages, creating it if needed."""
        raise NotImplementedError

    def remove_label(self, gmail_ids: list[str], label_name: str) -> None:
        """Remove a user label from messages."""
        raise NotImplementedError

    # ── moves ────────────────────────────────────────────────────────────────
    def trash(self, gmail_ids: list[str]) -> list[str]:
        """Move messages to Trash; returns "<id>: <why>" for those that didn't move."""
        raise NotImplementedError

    def untrash(self, gmail_ids: list[str]) -> list[str]:
        """Move messages out of Trash; returns failures as trash() does."""
        raise NotImplementedError

    # ── upload ───────────────────────────────────────────────────────────────
    def find_message_ids(self, message_ids: dict[str, str]) -> dict[str, list[str]]:
        """{key: Message-ID header} → {key: IDs of the messages with it}, Trash included."""
        raise NotImplementedError

    def import_messages(
        self,
        messages: dict[str, tuple[bytes, list[str]]],
        on_imported: Callable[[str, str], None],
    ) -> list[str]:
        """Upload {key: (raw, label IDs)}, calling on_imported(key, new ID); returns failures."""
        raise NotImplementedError


def open_mailbox(
    account: str, config_folder: Path, scope: str, interactive: bool = True
) -> Mailbox:
    """
    The account's mailbox with `scope` ("readonly" or "modify"), signing in if
    needed and `interactive`. Raises gmailapi.GmailError when it can't.
    """
    from mailcellar import gmailapi

    creds = gmailapi.load_credentials(config_folder, account, scope, interactive=interactive)
    return gmailapi.GmailAccount(account, creds)


def opener(config_root: Path) -> Callable[[str, str], Mailbox]:
    """
    open(account, scope) for accounts named in `config_root`'s accounts.toml:
    what dedup and offload pass around to open each account as they reach it.
    """
    from mailcellar import gmailapi

    def open_(account: str, scope: str) -> Mailbox:
        return open_mailbox(account, gmailapi.find_config_folder(config_root, account), scope)

    return open_
