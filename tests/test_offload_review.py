"""offload plan --review (a page per rule) and report storage's offload batches."""

import html
import re
import sqlite3
from collections import Counter

import pytest

from mailcellar.backup import sidecar
from mailcellar.backup.gyb import backup_account
from mailcellar.offload import cli, review
from mailcellar.offload.plan import Message, build_plan
from mailcellar.offload.rules import load_rules
from mailcellar.report import storage
from mailcellar.report.cli import main as report_main
from mailcellar.report.cli import parse_before, resolve_folders
from mailcellar.report.db import open_sidecars
from test_offload_plan import RULES, A, data, run  # noqa: F401  (data is a fixture)

DAY = 86_400_000


def msg(gid, ms=0, size=10, thread="", subject="s", account=A):
    return Message(account, gid, ms, size, "x@example.com", subject, "", [], set(), {}, thread)


# ── the pieces ───────────────────────────────────────────────────────────────


def test_a_sample_is_random_repeatable_and_oldest_first():
    ms = [msg(f"m{i}", ms=i) for i in range(200)]
    one = review.sample(ms, 10, "rule")
    assert len(one) == 10 and one == sorted(one, key=lambda m: m.internal_date)
    assert review.sample(ms, 10, "rule") == one
    assert review.sample(ms, 10, "other rule") != one
    assert review.sample(ms[:5], 10, "rule") == ms[:5]


def test_largest_messages_come_first():
    ms = [msg("a", size=5), msg("b", size=50), msg("c", size=20)]
    assert [m.gmail_id for m in review.largest(ms, 2)] == ["b", "c"]


def test_busiest_threads_count_their_messages_in_the_offload():
    ms = [
        msg("a1", ms=2 * DAY, thread="t1", subject="Re: plans"),
        msg("a2", ms=1 * DAY, thread="t1", subject="plans"),
        msg("b1", thread="t2", size=1000),
        msg("c1", thread=""),  # thread unknown: not a thread
        msg("d1", thread="t1", account="other@example.org"),  # another account's t1
    ]
    rows = review.busiest_threads(ms, 10)
    assert rows[0] == (A, "plans", 2, 20, "1970-01-02", "1970-01-03")
    assert [r[2] for r in rows] == [2, 1, 1] and len(rows) == 3


@pytest.mark.parametrize(
    "name, expected",
    [("old work", "old-work.html"), ("Shop: promos!", "shop-promos.html"), ("***", "rule.html")],
)
def test_file_names_come_from_the_rule_name(name, expected):
    assert review.file_name(name) == expected


def test_rule_text_says_what_a_rule_selects():
    from mailcellar.offload.rules import parse_rule

    rule = parse_rule(
        {
            "name": "r",
            "before": "2016-10-26",
            "after": "2006-01-01",
            "query": ["has:attachment", "larger:10M"],
            "to_me": False,
            "larger_than_mb": 5,
            "keep_if": [{"subject": "Contract", "sender": "@example.org"}, {"label": "x"}],
        },
        parse_before,
        lambda a: a,
    )
    assert review.rule_text(rule) == (
        'to_me false · larger_than_mb 5 · query "has:attachment" or "larger:10M" · '
        "from 2006-01-01 · before 2016-10-26 · "
        'keep if (sender "@example.org" · subject "contract") or (label "x")'
    )


def test_rule_text_of_a_rule_for_all_mail():
    from mailcellar.offload.rules import parse_rule

    rule = parse_rule({"name": "r", "before": "2020-01-01", "scope": "all"}, parse_before, str)
    assert review.rule_text(rule) == "all mail · before 2020-01-01"


def test_html_cells_are_escaped():
    t = review.Table("t", "<Title>", [("Subject", "text"), ("Size", "size")], [("<b>&", 2048)])
    out = review.table_html(t)
    assert "&lt;b&gt;&amp;" in out and "<b>" not in out and "&lt;Title&gt;" in out
    assert "2.0 KB" in out
    assert "None." in review.table_html(review.Table("t", "Empty", [("A", "text")], []))


# ── offload plan --review ────────────────────────────────────────────────────


def pages(data):  # noqa: F811
    return {
        p.name: p.read_text(encoding="utf-8")
        for p in (data / "data" / "offload" / "review").iterdir()
    }


def text_of(page: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", page))


def test_review_writes_a_page_per_rule(data, capsys):  # noqa: F811
    run(data, args=["--review"])
    got = pages(data)
    assert set(got) == {"work.html", "shop.html", "old-forwards.html"}
    work = text_of(got["work.html"])
    assert "Offload review: work" in work and "for you alone" in work
    assert 'Selects: label "work" · before 2020-01-01' in work
    for gid in ("w1", "w8", "p2", "t1", "b1"):  # its offload: all five, in the sample
        assert f"s-{gid}" in work
    assert "Kept: starred" in work and "Kept: yours" in work
    assert "Kept: a conversation you were in" in work and "Kept: labelled" in work
    assert "s-w4" in work  # starred, kept: shown under its keep reason
    assert "body w1" not in work  # never bodies
    assert "Threads with the most messages" in work and "Attachments in the offload" in work
    assert "Largest senders in the offload" in work  # the summary tables


def test_review_pages_only_hold_their_own_rule(data, capsys):  # noqa: F811
    run(data, args=["--review"])
    shop = text_of(pages(data)["shop.html"])
    assert "s-p1" in shop and "s-w1" not in shop


def test_without_review_no_pages_are_written(data, capsys):  # noqa: F811
    run(data)
    assert not (data / "data" / "offload" / "review").exists()


def test_kept_messages_are_kept_per_protection(data):  # noqa: F811
    (data / "rules.toml").write_text(RULES)
    folders = resolve_folders([])
    con, accounts = open_sidecars(folders)
    rules = load_rules(data / "rules.toml", parse_before, cli.resolve_account)
    work, _shop, _forwards = build_plan(
        con, rules, accounts, {backup_account(f): str(f) for f in folders}
    )
    con.close()
    assert {p: len(ms) for p, ms in work.kept_messages.items()} == dict(work.kept)
    assert [m.gmail_id for m in work.kept_messages["starred"]] == ["w4"]
    assert {m.thread_id for m in work.selected} >= {"t-w1"}


def test_attachment_types_are_summed_over_the_offload():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE all_attachments (account, gmail_id, mime_type, size)")
    con.executemany(
        "INSERT INTO all_attachments VALUES (?, ?, ?, ?)",
        [
            (A, "w1", "application/pdf", 100),
            (A, "w1", "image/png", 300),
            (A, "w8", "application/pdf", 50),
            (A, "zz", "application/pdf", 9999),  # not in the offload
            ("other@example.org", "w1", "image/png", 9999),  # another account's w1
        ],
    )
    rows = review.attachment_rows(con, [msg("w1"), msg("w8"), msg("w8")])
    assert rows == [("image/png", 1, 300), ("application/pdf", 2, 150)]
    assert review.attachment_rows(con, []) == []


# ── report storage: offload batches ──────────────────────────────────────────


def test_batch_rows_say_what_is_freed_and_when_the_rest_will_be():
    raw = [(A, "b1", "work\x1fwork\x1fshop", 4, 1, 2, 300, 1, 50, 0, 10 * DAY, 0)]
    (row,) = storage.batch_rows(raw)
    assert row == (A, "b1", "shop, work", 4, 1, 2, 300, 1, 50, "1970-02-10", 0)
    done = [(A, "b2", "work", 2, 0, 2, 100, 0, None, 0, DAY, 2)]
    assert storage.batch_rows(done)[0][-3:] == (0, "", 2)  # 2 of them forgotten since


def test_storage_report_shows_each_batch(data, capsys):  # noqa: F811
    folder = data / "data" / A
    con = sidecar.open_meta(folder)
    sidecar.record_offloaded(con, ["w1", "w8"], "2026-01-15-093000", "work", 5 * DAY)
    sidecar.record_offloaded(con, ["p1"], "2026-01-15-093000", "shop", 5 * DAY)
    sidecar.mark_gone(con, ["w1", "w8"], 40 * DAY)
    sidecar.record_restored(con, "w8", "n8", "imported", "run", 41 * DAY)  # back after emptying
    con.commit()
    con.close()
    folders = resolve_folders([])
    rcon, _accounts = open_sidecars(folders)
    table = storage.offload_batches(rcon)
    rcon.close()
    by_batch = {r[1]: r for r in table.rows}
    row = by_batch["2026-01-15-093000"]
    assert row[2:6] == ("shop, work", 3, 1, 1)  # offloaded, restored, Trash emptied
    assert row[7] == 1 and row[9] == "1970-02-05"  # p1 still in Trash, until 30 days on
    assert row[6] > 0 and row[8] > 0
    assert by_batch["batch"][3] == 1  # the fixture's own offload of w10
    report_main(["storage"])
    out = capsys.readouterr().out
    assert "Offload batches: space freed in their mailboxes" in out
    assert Counter(out.split())["2026-01-15-093000"] == 1


def test_a_rule_name_is_escaped_in_its_page(data, capsys):  # noqa: F811
    rules = '[[rule]]\nname = "<b>news & co</b>"\ncategory = "promotions"\nbefore = "2020-01-01"\n'
    run(data, rules=rules, args=["--review"])
    (page,) = pages(data).values()
    assert "<b>news" not in page
    assert "<title>Offload review: &lt;b&gt;news &amp; co&lt;/b&gt;</title>" in page
    assert "<h1>Offload review: &lt;b&gt;news &amp; co&lt;/b&gt;</h1>" in page
