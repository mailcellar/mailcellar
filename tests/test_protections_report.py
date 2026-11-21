"""The protections report: offload's keep names per message, for other tools."""

import csv
import hashlib
import sqlite3

import pytest

from fake_imap import FakeImap
from fake_mailbox import FakeMailbox, raw_response
from mailcellar import cli as top
from mailcellar import gmailapi, imap
from mailcellar.backup import commands, sidecar
from mailcellar.backup.gyb import backup_account
from mailcellar.offload.plan import in_scope, prepare
from mailcellar.offload.rules import load_rules
from mailcellar.report import cli
from mailcellar.report.cli import parse_before, resolve_folders
from mailcellar.report.db import open_sidecars
from mailcellar.report.protections import LEFT_OUT, NAMES, left_out, message_id_hash
from test_index import make_eml

A, B = "a@x.com", "b@y.com"
OLD = 1_300_000_000_000  # 2011-03-13
LABELS = {
    "INBOX": "INBOX",
    "SENT": "SENT",
    "STARRED": "STARRED",
    "TRASH": "TRASH",
    "Label_1": "Work",
    "Label_9": "offload-candidate",  # offload's review label
}


def message(gid, labels=("INBOX",), thread=None, sender="someone@p.org", msgid=None, **h):
    lines = [f"From: {sender}", f"Subject: s-{gid}"]
    if msgid != "":
        lines.append(f"Message-ID: <{msgid or gid + '@x'}>")
    lines += [f"{k.replace('_', '-')}: {v}" for k, v in h.items()]
    body = ("\r\n".join(lines) + f"\r\n\r\nbody {gid}\r\n").encode()
    r = raw_response(gid, ms=OLD, label_ids=list(labels), body=body)
    r["threadId"] = thread or f"t-{gid}"
    return r


MAILBOXES = {
    A: [
        message("plain", msgid="AbC.Def@Example.COM"),
        message("noid", msgid=""),
        message("sent", ("SENT",), sender=A, To="friend@p.org"),  # yours, and its own thread
        message("starred", ("STARRED",)),
        message("inthread", thread="t-sent"),  # a thread you wrote in
        message("replied"),  # replied to from the other account
        message("labelled", ("Label_1",)),
        message("review", ("Label_9",)),  # only offload's review label: not labelled
        message("friend", sender="friend@p.org"),  # from someone you wrote to
        message("trash", ("TRASH",)),
        message("offloaded", ("TRASH",)),  # offloaded comes before trash
        message("gone"),
        message("unindexed"),
    ],
    B: [message("b-sent", ("SENT",), sender=B, In_Reply_To="<replied@x>")],
}


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(FakeMailbox, "LABELS", LABELS)
    for account, msgs in MAILBOXES.items():
        gmail = FakeMailbox({m["id"]: m for m in msgs})
        trashed = set(gmail.trashed)  # back up mail in Trash first, as in test_offload_plan
        gmail.trashed.clear()
        commands.backup(gmail, account, tmp_path / "data" / account)
        gmail.trashed.update(trashed)
        commands.backup(gmail, account, tmp_path / "data" / account)
    folder = tmp_path / "data" / A
    con = sidecar.open_meta(folder)
    sidecar.record_offloaded(con, ["offloaded"], "batch", "rule", 1)
    con.execute("UPDATE messages SET thread_id = '' WHERE gmail_id = 'gone'")
    con.commit()
    con.close()
    with sqlite3.connect(folder / "msg-db.sqlite") as gyb:
        gyb.execute("DELETE FROM uids WHERE uid = 'unindexed'")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def report(directory, *accounts) -> dict[str, dict]:
    cli.main(["protections", *accounts, "--csv", str(directory / "out")])
    with open(directory / "out" / "protections-messages.csv", newline="") as fh:
        return {r["id"]: r for r in csv.DictReader(fh)}


def on(row) -> set[str]:
    return {k for k in NAMES if row[k] == "1"}


def test_each_protection_is_set_by_what_sets_it_in_offload(data, capsys):
    rows = report(data)
    assert list(next(iter(rows.values()))) == [
        "account",
        "id",
        "message_id_hash",
        *NAMES,
        "left_out",
    ]
    assert on(rows["plain"]) == set()
    assert on(rows["sent"]) == {"mine", "conversations"}
    assert on(rows["starred"]) == {"starred"}
    assert on(rows["inthread"]) == {"conversations"}
    assert on(rows["replied"]) == {"conversations"}  # the reply was sent from B
    assert on(rows["labelled"]) == {"labelled"}
    assert on(rows["review"]) == set()
    assert on(rows["friend"]) == {"correspondents"}
    assert rows["b-sent"]["account"] == B and on(rows["b-sent"]) == {"mine", "conversations"}
    assert "Protections per account" in capsys.readouterr().out


def test_replies_from_an_account_not_named_dont_count(data):
    assert on(report(data, A)["replied"]) == set()


def test_left_out_says_why_offload_would_never_select_it(data):
    rows = report(data)
    assert {i: r["left_out"] for i, r in rows.items() if r["left_out"]} == {
        "trash": "trash",
        "offloaded": "offloaded",
        "gone": "gone",
        "unindexed": "unindexed",
    }
    assert left_out([True] * len(LEFT_OUT)) == LEFT_OUT[0] == "offloaded"
    assert left_out([False] * len(LEFT_OUT)) == ""


def test_the_hash_is_of_the_bare_lower_cased_message_id(data):
    rows = report(data)
    want = hashlib.sha256(b"abc.def@example.com").hexdigest()[:16]
    assert rows["plain"]["message_id_hash"] == want == message_id_hash("abc.def@example.com")
    assert rows["noid"]["message_id_hash"] == ""


def test_summary_counts(data):
    report(data)
    with open(data / "out" / "protections-summary.csv", newline="") as fh:
        summary = {r["Account"]: r for r in csv.DictReader(fh)}
    a = summary[A]
    assert a["Messages"] == str(len(MAILBOXES[A]))
    assert (a["Mine"], a["Conversations"], a["Labelled"], a["Correspondents"]) == (
        "1",
        "3",
        "1",
        "1",
    )
    assert a["Default keep"] == "5" and a["Left out"] == "4"


def test_the_flags_agree_with_offload_plans(data):
    """Kept in step with in_scope: a rule over everything, naming no label."""
    (data / "rules.toml").write_text(
        '[[rule]]\nname = "all"\nscope = "all"\nbefore = "2020-01-01"\n'
    )
    rows = report(data)
    folders = resolve_folders([])
    con, accounts = open_sidecars(folders)
    (rule,) = load_rules(data / "rules.toml", parse_before, lambda name: name)
    prepare(con, accounts, {})
    messages = in_scope(con, rule, accounts, {backup_account(f): str(f) for f in folders})
    con.close()
    assert {m.gmail_id for m in messages} == set(rows)
    for m in messages:
        row = rows[m.gmail_id]
        assert row["mine"] == str(int(m.flags["sent"]))
        assert row["starred"] == str(int(m.flags["starred"]))
        assert row["conversations"] == str(int(m.flags["thread"] or m.flags["replied"]))
        assert row["correspondents"] == str(int(m.flags["correspondents"]))
        if m.gmail_id != "review":  # in_scope counts offload's review label as a user label
            assert row["labelled"] == str(int(m.flags["labels"]))
        assert row["left_out"] == left_out([m.flags[e] for e in LEFT_OUT])


IMAP_ADDRESS = "alex@example.org"


def test_an_imap_account(tmp_path, monkeypatch):
    root = tmp_path / "config"
    (root / "home").mkdir(parents=True)
    (root / "accounts.toml").write_text(f'home = "{IMAP_ADDRESS}"\n')
    (root / "home" / gmailapi.IMAP_SETTINGS).write_text('host = "imap.example.org"\n')
    password = root / "home" / gmailapi.IMAP_PASSWORD
    password.write_text("secret\n")
    password.chmod(0o600)
    monkeypatch.setenv("MAILCELLAR_CONFIG", str(root))
    s = FakeImap()
    s.add_folder("Sent", special=["\\Sent"])
    s.add_folder("Projects")
    s.add("INBOX", make_eml("<a@x>"), flags={"\\Seen", "\\Flagged"})
    s.add("Sent", make_eml("<c@x>", sender=IMAP_ADDRESS), flags={"\\Seen"})
    s.add("Projects", make_eml("<p@x>"), flags={"\\Seen"})
    monkeypatch.setattr(imap.ImapAccount, "_connect_ssl", lambda self: s)
    assert top.main(["backup", "home"]) == 0
    rows = report(tmp_path).values()
    by_hash = {r["message_id_hash"]: r for r in rows}
    assert all(r["id"] and r["account"] == IMAP_ADDRESS for r in rows)
    assert on(by_hash[message_id_hash("a@x")]) == {"starred"}
    assert on(by_hash[message_id_hash("c@x")]) == {"mine", "conversations"}
    assert on(by_hash[message_id_hash("p@x")]) == {"labelled"}
