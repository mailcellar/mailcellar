import email.utils
from contextlib import closing

import pytest

from fake_mailbox import FakeMailbox, raw_response
from mailcellar.backup import commands
from mailcellar.report import bulk, cli
from mailcellar.report.db import open_sidecars

ME, OTHER = "me@club.org", "me2@gmail.com"  # club.org is "own"; gmail.com is public
DAY = 86_400_000
NOW = 1_792_711_800_000  # 2026-10-22
PROMO = ["CATEGORY_PROMOTIONS"]


def mail(sender, subject, *, msgid, headers=(), to=None, in_reply_to=None):
    lines = [
        f"From: {sender}",
        f"Subject: {subject}",
        f"Message-ID: <{msgid}>",
        f"Date: {email.utils.formatdate(0)}",
    ]
    if to:
        lines.append(f"To: {to}")
    if in_reply_to:
        lines.append(f"In-Reply-To: <{in_reply_to}>")
    lines += [f"{k}: {v}" for k, v in headers]
    return ("\r\n".join(lines) + f"\r\n\r\nabout {subject}\r\n").encode()


class Mailbox:
    """Messages for one fake account, with explicit thread IDs and dates."""

    def __init__(self):
        self.messages = {}

    def add(self, gid, sender, *, labels=("INBOX",), days_ago=400, thread=None, **kw):
        r = raw_response(
            gid,
            ms=NOW - days_ago * DAY,
            label_ids=list(labels),
            body=mail(sender, gid, msgid=f"{gid}@x", **kw),
        )
        r["threadId"] = thread or f"t-{gid}"
        self.messages[gid] = r

    def series(self, prefix, sender, n=12, **kw):
        for i in range(n):
            self.add(f"{prefix}{i}", sender, **kw)


LIST = [
    ("List-Id", "Shop news <news.shop.com>"),
    ("List-Unsubscribe", "<mailto:u@shop.com>, <https://shop.com/u>"),
]
RELAY = [
    ("List-Id", "<catchall.club.org>"),
    ("List-Unsubscribe", "<mailto:catchall+u@club.org>"),
    ("Precedence", "list"),
]


@pytest.fixture
def report(tmp_path, monkeypatch):
    monkeypatch.setattr(
        FakeMailbox,
        "LABELS",
        {**FakeMailbox.LABELS, "SENT": "SENT", "CATEGORY_PROMOTIONS": "CATEGORY_PROMOTIONS"},
    )
    a, b = Mailbox(), Mailbox()
    a.series("news", "Shop <news@shop.com>", labels=PROMO, headers=LIST, days_ago=10)
    a.series("bob", "Bob <bob@work.com>")
    a.add("bobcat", "Bob <bob@work.com>", labels=PROMO)  # 1 of 13 categorised
    a.series("deal", "deals@shop.net", labels=PROMO)  # you wrote to them
    a.add("sent1", ME, labels=["SENT"], to="Deals <deals@shop.net>")
    a.series("alert", "alerts@bank.com", labels=PROMO)  # replied to from the other account
    b.add("sent2", OTHER, labels=["SENT"], in_reply_to="alert3@x")
    a.series("promo", "promo@x.com", labels=PROMO)  # you posted in one of their threads
    a.add("sent3", ME, labels=["SENT"], thread="t-promo5")
    a.series("lab", "labelled@x.com", labels=PROMO)
    a.add("lab-user", "labelled@x.com", labels=[*PROMO, "Label_1"])  # user label
    a.series(
        "via",
        "'Shop' via Catchall <catchall@club.org>",
        labels=PROMO,
        headers=[*RELAY, ("Reply-To", "shop@real.com")],
    )
    a.series("kept", "info@kept.com", headers=RELAY)  # relayed, not categorised
    a.series("own", f"Me <{ME}>")  # from a backed-up address
    for account, box in ((ME, a), (OTHER, b)):
        commands.backup(FakeMailbox(box.messages), account, tmp_path / "data" / account)
    monkeypatch.chdir(tmp_path)

    def run(**opts):
        con, accounts = open_sidecars(cli.resolve_folders([]))
        with closing(con):
            tables = bulk.build(con, accounts, bulk.Options(now_ms=NOW, **opts))
        return {t.name: t for t in tables}

    return run


def sources(table):
    return {r[0]: r for r in table.rows}


def test_sources_kinds_and_relay_handling(report):
    t = report()
    s = sources(t["sources"])
    assert s["news.shop.com"][1:4] == ("list", ME, 12)
    assert s["news.shop.com"][10] == "both"  # unsubscribe offered
    assert s["shop@real.com"][1] == "relayed"  # original sender from Reply-To
    assert s["shop@real.com"][10] == "—"  # the relay's header doesn't count
    assert "catchall.club.org" not in s and "catchall@club.org" not in s
    assert "info@kept.com" not in s  # relay signals ignored: not bulk
    assert "bob@work.com" not in s  # 1 of 13 categorised: not bulk
    assert ME not in s  # own mail is not a source


def test_engagement_rules(report):
    s = sources(report()["sources"])
    assert s["deals@shop.net"][11] == "yes"  # wrote to them
    assert s["alerts@bank.com"][11] == "yes"  # In-Reply-To from the other account
    assert s["promo@x.com"][11] == "yes"  # sent in one of their threads
    assert s["news.shop.com"][11] == "no"
    assert s["labelled@x.com"][12] == "yes"


def test_candidates(report):
    t = report()["candidates"]
    assert [r[0] for r in t.rows] == ["news.shop.com", "shop@real.com"]  # still sending first
    assert t.notes[0].startswith("2 sources, 24 messages")
    assert "1 still sending" in t.notes[0]


def test_options(report):
    assert "bob@work.com" in sources(report(min_bulk_share=0.05)["sources"])
    assert report(min_messages=13)["candidates"].rows == []


def test_summary_counts(report):
    rows = {r[0]: r for r in report()["summary"].rows}
    me = rows[ME]
    # all mail (9 series + 4 singles), from others (all but 2 sent + 12 own)
    assert me[1] == 12 * 9 + 4 and me[2] == me[1] - 2 - 12
    assert me[7] == 24  # relayed (via + kept)
    assert (me[10], me[11]) == (2, 24)


def test_cli_bulk(report, capsys, tmp_path):
    report()
    cli.main(["bulk", "--top", "3", "--csv", "reports"])
    out = capsys.readouterr().out
    assert out.startswith("Bulk mail report — ")
    assert "Unsubscribe / clear-out candidates" in out
    assert len(list((tmp_path / "reports").glob("bulk-*.csv"))) == 5


def test_own_domains():
    # A shared provider (gmail.com) is never "your own" domain.
    assert bulk.own_domains(["alex@example.org", "someone@gmail.com"]) == ["example.org"]
