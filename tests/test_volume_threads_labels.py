"""The volume, threads and labels reports."""

import pytest

from fake_mailbox import FakeMailbox, raw_response
from mailcellar.backup import commands
from mailcellar.report import cli, labels, threads, volume
from mailcellar.report.db import open_sidecars

A, B = "a@x.com", "b@y.com"
OLD = 1_300_000_000_000  # 2011-03-13
NEW = 1_792_711_800_000  # 2026-10-22
DAY = 86_400_000


def message(gid, ms, thread, labels=("INBOX",), msgid=None, forwarded=None):
    lines = ["From: someone@z.org", f"Subject: s-{thread}", f"Message-ID: <{msgid or gid}@x>"]
    if forwarded:
        lines.append(f"X-Forwarded-For: {forwarded} {A}")
    body = ("\r\n".join(lines) + f"\r\n\r\nbody {gid}\r\n").encode()
    r = raw_response(gid, ms=ms, label_ids=list(labels), body=body)
    r["threadId"] = thread
    return r


@pytest.fixture
def con(tmp_path, monkeypatch):
    mailboxes = {
        A: (
            {
                "INBOX": "INBOX",
                "SENT": "SENT",
                "DRAFT": "DRAFT",
                "Label_1": "work",
                "Label_2": "Study",
                "Label_3": "Travel",
                "Label_4": "Travel/Nepal",
            },
            [
                message("a1", OLD, "t1", labels=[], forwarded="old@employer.example"),  # archived
                message("a2", OLD + DAY, "t1", labels=["SENT"]),
                message("a3", NEW, "t1"),
                message("a4", NEW, "t4", labels=["DRAFT"]),
                message("a5", NEW, "t5", labels=["Label_1"], msgid="shared"),
                message("a6", NEW, "t6", labels=["Label_2"]),
                message("a7", NEW, "t7", labels=["INBOX", "Label_4"]),
            ],
        ),
        B: (
            {"INBOX": "INBOX", "SENT": "SENT", "Label_1": "Work", "Label_2": "garden"},
            [
                message("b1", NEW, "u1", labels=["Label_1"], msgid="shared"),
                message("b2", NEW + DAY, "u1", labels=["SENT"]),
            ],
        ),
    }
    for account, (catalogue, msgs) in mailboxes.items():
        monkeypatch.setattr(FakeMailbox, "LABELS", catalogue)
        commands.backup(
            FakeMailbox({m["id"]: m for m in msgs}), account, tmp_path / "data" / account
        )
    monkeypatch.chdir(tmp_path)
    con, accounts = open_sidecars(cli.resolve_folders([]))
    assert accounts == [A, B]
    yield con
    con.close()


def tables(built):
    return {t.name: t for t in built}


def test_volume(con):
    t = tables(volume.build(con))
    totals = {r[0]: r for r in t["totals"].rows}
    assert totals[A][1:5] == (5, 1, 1, 0.2)  # draft left out
    assert totals[B][1:5] == (1, 1, 0, 0)
    years = {(r[0], r[1]): r[2:6] for r in t["by-year"].rows}
    assert years[("2011", A)] == (1, 1, 1, 1.0)
    assert years[("2026", A)] == (4, 0, 0, 0)
    assert [r[0] for r in t["by-month"].rows] == ["2026-10", "2026-10", "2011-03"]
    assert t["forwarding"].rows[0][:3] == ("old@employer.example", A, 1)
    assert t["forwarding"].rows[0][6] == 0.2


def test_threads(con):
    t = tables(threads.build(con, [A, B]))
    summary = {r[0]: r for r in t["summary"].rows}
    assert summary[A][1:6] == (5, 7, 0.8, 1, 3)
    assert summary[B][1:5] == (1, 2, 0, 1)
    dist = {(r[0], r[1]): r[2:4] for r in t["distribution"].rows}
    assert dist[("1", A)] == (4, 4) and dist[("3–5", A)] == (1, 3) and dist[("51+", A)] == (0, 0)
    (row,) = [r for r in t["longest"].rows if r[1] == A]
    assert row[0] == "s-t1" and row[2:5] == (3, 1, 1) and row[8] > 5000  # 2011 → 2026
    assert [r[-1] for r in t["longest-yours"].rows] == ["t1", "u1"]
    assert t["longest-running"].rows[0][-1] == "t1"
    (pair,) = t["across-accounts"].rows
    assert pair[1:6] == (A, 1, B, 2, 1) and pair[-2:] == ("t5", "u1")
    assert t["across-accounts"].notes[0].startswith("1 pairs of threads share a Message-ID; 1 ")


def test_labels(con):
    t = tables(labels.build(con, [A, B]))
    used = {(r[0], r[1]): r for r in t["by-label"].rows}
    assert used[("work", A)][2] == 1 and used[("work", A)][6] == f"{B} (1)"  # case ignored
    assert used[("Study", A)][6] == ""
    assert ("Travel", A) not in used and ("garden", B) not in used
    only = {r[0]: r for r in t["one-account-only"].rows}
    assert set(only) == {"Study", "Travel/Nepal"}
    assert t["one-account-only"].notes[1].startswith("1 label names exist in more than one")
    assert {(r[0], r[1], r[2]) for r in t["empty"].rows} == {("Travel", A, 1), ("garden", B, 0)}
    unl = {r[0]: r for r in t["unlabelled"].rows}
    assert unl[A][1:] == (7, 3, 4, 1, 1, 2)  # a3 inbox; a1 archived; a2 sent, a4 draft
    assert unl[B][1:] == (2, 1, 1, 0, 0, 1)


def test_one_account(con):
    t = tables(labels.build(con, [A]))
    assert t["one-account-only"].rows == [] and t["one-account-only"].notes == [
        "Only one account chosen."
    ]


def test_cli(con, capsys, tmp_path):
    for name, n in (("volume", 4), ("threads", 6), ("labels", 4)):
        cli.main([name, "--top", "2", "--csv", "reports"])
        assert capsys.readouterr().out.startswith(f"{name.title()} report — {A}, {B}")
        assert len(list((tmp_path / "reports").glob(f"{name}-*.csv"))) == n
    cli.main(["threads", "b@y"])
    assert "Only one account chosen." in capsys.readouterr().out
