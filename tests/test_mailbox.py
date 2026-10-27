"""mailbox: the Mailbox protocol, open_mailbox and opener."""

import inspect
from argparse import Namespace
from pathlib import Path

import pytest

from mailcellar import gmailapi, mailbox
from mailcellar.backup import commands
from test_backup import ACCOUNT, FakeService


def protocol_methods():
    return {
        name: member
        for name, member in vars(mailbox.Mailbox).items()
        if inspect.isfunction(member) and not name.startswith("_")
    }


def parameters(function):
    return [(p.name, p.kind, p.default) for p in inspect.signature(function).parameters.values()]


def test_gmail_account_has_every_mailbox_method_with_the_same_parameters():
    assert len(protocol_methods()) == 16
    for name, method in protocol_methods().items():
        real = getattr(gmailapi.GmailAccount, name, None)
        assert real is not None, f"GmailAccount has no {name}"
        assert parameters(real) == parameters(method), name
    gmail = gmailapi.GmailAccount(ACCOUNT, None, service=FakeService({}), sleep=lambda s: None)
    assert gmail.account == ACCOUNT


def test_a_subclass_missing_a_method_fails_when_it_is_called():
    class Partial(mailbox.Mailbox):
        account = ACCOUNT

    with pytest.raises(NotImplementedError):
        Partial().trash(["g1"])


def test_open_mailbox_signs_in_with_the_scope_and_opens_the_account(monkeypatch, tmp_path):
    calls = []

    def load(folder, account, scope, interactive=True):
        calls.append((folder, account, scope, interactive))
        return "creds"

    monkeypatch.setattr(gmailapi, "load_credentials", load)
    monkeypatch.setattr(gmailapi, "GmailAccount", lambda account, creds: (account, creds))
    got = mailbox.open_mailbox("alex@example.com", tmp_path, "modify", interactive=False)
    assert got == ("alex@example.com", "creds")
    assert calls == [(tmp_path, "alex@example.com", "modify", False)]


def test_opener_finds_each_accounts_config_folder(monkeypatch, tmp_path):
    opened = []
    monkeypatch.setattr(gmailapi, "find_config_folder", lambda root, account: root / account)
    monkeypatch.setattr(
        mailbox,
        "open_mailbox",
        lambda account, folder, scope, interactive=True: opened.append(
            (account, folder, scope, interactive)
        ),
    )
    mailbox.opener(tmp_path)("alex@example.com", "modify")
    assert opened == [("alex@example.com", tmp_path / "alex@example.com", "modify", True)]


@pytest.mark.parametrize("unattended", [False, True])
def test_backup_signs_in_interactively_unless_unattended(monkeypatch, unattended):
    opened = []
    monkeypatch.setattr(gmailapi, "resolve_account", lambda root, name: (ACCOUNT, Path("cfg")))
    monkeypatch.setattr(
        mailbox,
        "open_mailbox",
        lambda account, folder, scope, interactive=True: opened.append(interactive),
    )
    args = Namespace(config="cfg", account=ACCOUNT, folder="data", unattended=unattended)
    commands.connect(args)
    assert opened == [not unattended]
