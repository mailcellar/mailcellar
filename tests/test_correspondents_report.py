"""The correspondents report."""

import pytest

from fake_mailbox import FakeMailbox, raw_response
from mailcellar.backup import commands
from mailcellar.report import cli, correspondents
from mailcellar.report.db import open_sidecars

A, B, ALIAS = "a@x.com", "b@y.com", "me@old.com"
OLD = 1_300_000_000_000  # 2011-03-13
HOUR = 3_600_000


def message(gid, ms, thread, sender, labels=("INBOX",), msgid=None, irt=None, **headers):
    lines = [f"From: {sender}", f"Subject: s-{thread}", f"Message-ID: <{msgid or gid}@x>"]
    if irt:
        lines.append(f"In-Reply-To: <{irt}@x>")
    lines += [f"{k.replace('_', '-')}: {v}" for k, v in headers.items()]
    body = ("\r\n".join(lines) + f"\r\n\r\nbody {gid}\r\n").encode()
    r = raw_response(gid, ms=ms, label_ids=list(labels), body=body)
    r["threadId"] = thread
    return r


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(
        FakeMailbox,
        "LABELS",
        {"INBOX": "INBOX", "SENT": "SENT", "DRAFT": "DRAFT"},
    )
    sent = ["SENT"]
    mailboxes = {
        A: [
            message("a1", OLD, "t1", A, sent, "y1", To="Ann <ann@p.com>", Cc="bob@p.com", Bcc=A),
            message("a2", OLD + 2 * HOUR, "t1", "Ann <ann@p.com>", msgid="ann1", irt="y1"),
            message("a3", OLD + 5 * HOUR, "t1", "bob@p.com"),  # later in thread, not a reply
            message("a4", OLD, "t2", A, sent, "y2", To="carol@q.com"),
            message("a5", OLD + HOUR, "t2", "carol@q.com", irt="other"),
            message("a6", OLD, "t6", A, ["DRAFT"], To="dan@q.com"),  # draft: left out
            message("a7", OLD, "t7", "news@shop.com", List_Id="<news.shop.com>"),  # bulk
            message("a8", OLD + 3 * HOUR, "t8", ALIAS, sent, "y3", irt="ann1", To="ann@p.com"),
            message("a9", OLD, "t9", ALIAS),  # from an alias, no SENT label: still yours
        ],
        B: [
            message("b1", OLD, "u1", A, msgid="y1", To="ann@p.com"),  # same message as a1
            message("b2", OLD + 7 * HOUR, "u1", "ann@p.com", irt="y3"),
            message("b3", OLD, "u3", B, sent, "y4", To="erin@r.com"),
        ],
    }
    for account, msgs in mailboxes.items():
        commands.backup(
            FakeMailbox({m["id"]: m for m in msgs}), account, tmp_path / "data" / account
        )
    monkeypatch.chdir(tmp_path)
    con, accounts = open_sidecars(cli.resolve_folders([]))
    assert accounts == [A, B]
    yield con
    con.close()


def tables(con, accounts=(A, B)):
    return {t.name: t for t in correspondents.build(con, list(accounts))}


def test_summary(con):
    t = tables(con)["summary"]
    rows = {r[0]: r for r in t.rows}
    # your messages, recipients, replied, rate, later in thread, median hours
    assert rows[A][1:7] == (4, 3, 2, 0.5, 2, 3.0)
    assert rows[B][1:7] == (2, 2, 1, 0.5, 1, 2.0)
    assert rows["all accounts"][1:7] == (5, 4, 2, 0.4, 2, 3.0)  # y1 counted once
    # from others, personal, personal senders, two-way
    assert rows[A][7:] == (4, 3, 3, 3)
    assert rows[B][7:] == (1, 1, 1, 1)
    assert rows["all accounts"][7:] == (5, 4, 3, 3)
    assert t.notes[0] == f"Your addresses: {A}, {B}, {ALIAS}."


def test_correspondent_tables(con):
    t = tables(con)
    out = {r[0]: r for r in t["you-write-to"].rows}
    assert set(out) == {"ann@p.com", "bob@p.com", "carol@q.com", "erin@r.com"}  # not dan, not you
    ann = out["ann@p.com"]
    assert ann[1] == "Ann"
    assert ann[2:7] == (2, 2, 2, 2, 1)  # wrote, they wrote, personal, they replied, you replied
    assert ann[9] == f"{A}, {B}"
    assert [r[0] for r in t["you-write-to"].rows][0] == "ann@p.com"
    assert "news@shop.com" not in {r[0] for r in t["write-to-you"].rows}
    assert [r[0] for r in t["two-way"].rows] == ["ann@p.com", "bob@p.com", "carol@q.com"]


def test_replies_by_year(con):
    rows = {(r[0], r[1]): r[2:] for r in tables(con)["replies-by-year"].rows}
    assert rows[("2011", A)] == (4, 2, 0.5, 3.0)
    assert rows[("2011", B)] == (2, 1, 0.5, 2.0)


def test_one_account(con):
    t = tables(con, [A])  # B's messages are still attached; the summary rows follow accounts
    assert [r[0] for r in t["summary"].rows] == [A]


def test_cli(con, capsys, tmp_path):
    cli.main(["correspondents", "--top", "2", "--csv", "reports"])
    out = capsys.readouterr().out
    assert out.startswith(f"Correspondents report — {A}, {B}")
    assert len(list((tmp_path / "reports").glob("correspondents-*.csv"))) == 5
