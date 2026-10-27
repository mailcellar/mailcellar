"""offload plan: rules, selection, protections and the plan CSV."""

import csv

import pytest

from fake_mailbox import FakeMailbox, raw_response
from mailcellar.backup import commands, sidecar
from mailcellar.backup.gyb import backup_account
from mailcellar.offload import cli
from mailcellar.offload.plan import PLAN_COLUMNS, build_plan
from mailcellar.offload.rules import DEFAULT_PROTECT, RuleError, fold, load_rules
from mailcellar.report.cli import parse_before, resolve_folders
from mailcellar.report.db import open_sidecars

A, B = "a@x.com", "b@y.com"
OLD = 1_300_000_000_000  # 2011-03-13
NEW = 1_700_000_000_000  # 2023-11-14
LABELS = {
    "INBOX": "INBOX",
    "SENT": "SENT",
    "STARRED": "STARRED",
    "TRASH": "TRASH",
    "CATEGORY_PROMOTIONS": "CATEGORY_PROMOTIONS",
    "Label_1": "Work",
    "Label_2": "other",
}


def message(
    gid, labels=("Label_1",), ms=OLD, thread=None, sender="colleague@work.com", subject=None, **h
):
    subject = subject or f"s-{gid}"
    lines = [f"From: {sender}", f"Subject: {subject}", f"Message-ID: <{gid}@x>"]
    lines += [f"{k.replace('_', '-')}: {v}" for k, v in h.items()]
    body = ("\r\n".join(lines) + f"\r\n\r\nbody {gid}\r\n").encode()
    r = raw_response(gid, ms=ms, label_ids=list(labels), body=body)
    r["threadId"] = thread or f"t-{gid}"
    return r


MAILBOX_A = [
    message("w1"),  # offloaded
    message("w2", ("Label_1", "Label_2")),  # another user label
    message("w3", ("Label_1", "SENT"), sender=A),  # yours
    message("w4", ("Label_1", "STARRED")),
    message("w5", thread="t5"),  # a thread you wrote in
    message("s5", ("SENT",), thread="t5", sender=A, To="friend@p.com"),
    message("w6"),  # you replied to it
    message("s6", ("SENT",), sender=A, In_Reply_To="<w6@x>", To="colleague@work.com"),
    message("w7", ms=NEW),  # after the cut-off
    message("w8", sender="friend@p.com"),  # from someone you wrote to: offloaded by default
    message("w9", ("Label_1", "TRASH")),
    message("w10"),  # already offloaded
    message("w11", sender="me@alias.com"),  # your alias (it sent s11)
    message("s11", ("SENT",), sender="me@alias.com"),
    message("p1", ("CATEGORY_PROMOTIONS",), sender="news@shop.com"),
    message("p2", ("Label_1", "CATEGORY_PROMOTIONS"), sender="deals@shop.com"),  # rule 1 first
    message("p3", ("CATEGORY_PROMOTIONS",), sender="news@shop_com.net"),  # another domain
    message("f1", ("INBOX",), X_Forwarded_For="me@old.com a@x.com"),
    message("f2", ("INBOX",), X_Forwarded_For="me@older.com a@x.com"),
    message("t1", To="Me <A@x.com>"),  # addressed to you (case ignored)
    message("n1", ("INBOX",), subject="=?utf-8?q?Caf=C3=A9_menu_July?="),
    message("n2", ("INBOX",), subject="Caf&eacute; menu May"),
    message("n3", ("INBOX",), subject="CAFES 2012"),
    message("n4", ("INBOX",), subject="=?iso-8859-1?q?Caf=C3=A9_June?="),  # → "CafÃ©"
    message("n5", ("INBOX",), subject="Tea menu July"),
]

RULES = """
[[rule]]
name = "work"
label = "work"
before = "2020-01-01"

[[rule]]
name = "shop"
category = "promotions"
sender = "@shop.com"
before = "2020-01-01"

[[rule]]
name = "old forwards"
forwarded_from = ["@old.com"]
before = "2020-01-01"
account = "a@x"
"""


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(FakeMailbox, "LABELS", LABELS)
    for account, msgs in {A: MAILBOX_A, B: [message("b1")]}.items():
        # Mail already in Trash isn't listed, as in Gmail: back it up first, then
        # trash it, and the next backup records the label.
        gmail = FakeMailbox({m["id"]: m for m in msgs})
        trashed = set(gmail.trashed)
        for gid in trashed:
            gmail.trashed.discard(gid)
        commands.backup(gmail, account, tmp_path / "data" / account)
        gmail.trashed.update(trashed)
        commands.backup(gmail, account, tmp_path / "data" / account)
    con = sidecar.open_meta(tmp_path / "data" / A)
    sidecar.record_offloaded(con, ["w10"], "batch", "work", 1)
    con.commit()
    con.close()
    monkeypatch.chdir(tmp_path)
    return tmp_path


def run(tmp_path, rules=RULES, args=()):
    (tmp_path / "rules.toml").write_text(rules)
    cli.main(["plan", "--rules", "rules.toml", "--plan", "plan.csv", *args])
    with open(tmp_path / "plan.csv", newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_selection_and_protections(data, capsys):
    rows = run(data)
    by_rule = {}
    for r in rows:
        by_rule.setdefault(r["rule"], set()).add(r["gmail_id"])
    assert by_rule == {
        "work": {"w1", "w8", "p2", "t1", "b1"},
        "shop": {"p1"},
        "old forwards": {"f1"},
    }
    out = capsys.readouterr().out
    assert "taken by an earlier rule" in out
    assert "already in Trash or Spam" in out and "already offloaded" in out


def test_kept_counts(data):
    (data / "rules.toml").write_text(RULES)
    folders = resolve_folders([])
    con, accounts = open_sidecars(folders)
    rules = load_rules(data / "rules.toml", parse_before, cli.resolve_account)
    work, shop, forwards = build_plan(
        con, rules, accounts, {backup_account(f): str(f) for f in folders}
    )
    # w3 and w11 are yours; w3 (sent) is also its own thread you wrote in
    assert dict(work.kept) == {"sent": 2, "starred": 1, "thread": 2, "replied": 1, "labels": 1}
    assert work.kept_total == 6 and dict(work.excluded) == {"offloaded": 1, "trash": 1}
    assert work.scope_total == 6 + 2 + 5  # kept, left out, offloaded (w7 is after the cut-off)
    assert dict(shop.excluded) == {"earlier": 1} and len(shop.selected) == 1
    assert [m.gmail_id for m in forwards.selected] == ["f1"]
    con.close()


def test_plan_csv(data):
    rows = run(data)
    assert list(rows[0]) == PLAN_COLUMNS
    w1 = next(r for r in rows if r["gmail_id"] == "w1")
    assert w1["account"] == A and w1["date"] == "2011-03-13"
    assert w1["labels"] == "Work" and w1["waived"] == ""
    assert (data / w1["path"]).is_file()
    assert w1["sender"] == "colleague@work.com" and w1["subject"] == "s-w1"


def test_waive_and_protect(data):
    rules = """
[[rule]]
name = "work"
label = "Work"
account = ["a@x.com"]
before = "2020-01-01"
waive = ["sent", "starred"]
protect = ["correspondents"]
"""
    rows = run(data, rules)
    # colleague@work.com and friend@p.com are written to (s6, s5); w3 is its own thread
    # you wrote in; b1 is in another account
    assert {r["gmail_id"] for r in rows} == {"w11", "p2"}
    assert {r["waived"] for r in rows} == {"sent;starred"}


def test_account_argument_limits_the_plan(data):
    rows = run(data, args=["b@y"])
    assert {(r["account"], r["gmail_id"]) for r in rows} == {(B, "b1")}


def test_plan_changes_nothing(data):
    meta = data / "data" / A / "meta.sqlite"
    before = meta.read_bytes()
    run(data)
    assert meta.read_bytes() == before


@pytest.mark.parametrize(
    ("rules", "error"),
    [
        ('[[rule]]\nname = "x"\nlabel = "a"\n', "before is required"),
        ('[[rule]]\nname = "x"\nbefore = "5"\n', "needs at least one of"),
        ('[[rule]]\nname = "x"\nbefore = "5"\nlable = "a"\n', "unknown keys ['lable']"),
        ('[[rule]]\nname = "x"\nbefore = "soon"\nlabel = "a"\n', "give a date"),
        ('[[rule]]\nname = "x"\nbefore = "5"\nlabel = "a"\nwaive = ["thred"]\n', "thred"),
        ('[[rule]]\nname = "x"\nbefore = "5"\ncategory = "spam"\n', "unknown categories"),
        (
            '[[rule]]\nname = "x"\nbefore = "5"\nlabel = "a"\nwaive = "sent"\nprotect = "sent"\n',
            "both waived and protected",
        ),
        ('[[rule]]\nname = "x"\nbefore = "5"\nlabel = "a"\n' * 2, "must be unique"),
        ('[rules]\nname = "x"\n', "unknown tables"),
        ("", "no [[rule]] tables"),
        ('[[rule]]\nname = "x"\nbefore = "5"\nlabel = "a"\nto_me = "yes"\n', "true or false"),
        ('[[rule]]\nname = "x"\nbefore = "5"\nsubject = " "\n', "must not be empty"),
        ('[[rule]]\nname = "x"\nbefore = "5"\nquery = ["a", ""]\n', "query must not be empty"),
        ('[[rule]]\nname = "x"\nbefore = "5"\nlabel = "a"\nexcept = "b"\n', "table of scope"),
        (
            '[[rule]]\nname = "x"\nbefore = "5"\nlabel = "a"\nexcept = { before = "1" }\n',
            "except: unknown keys",
        ),
        (
            '[[rule]]\nname = "x"\nbefore = "5"\nlabel = "a"\nexcept = { category = "x" }\n',
            "except: unknown categories",
        ),
        ('[[rule]]\nname = "x"\nbefore = "5"\nlabel = "a"\nforget_after = 0\n', "1 or more"),
        ('[[rule]]\nname = "x"\nbefore = "5"\nlabel = "a"\nforget_after = 1.5\n', "whole"),
        ('[[rule]]\nname = "x"\nbefore = "5"\nlabel = "a"\nforget_after = "7"\n', "whole"),
        ('[[rule]]\nname = "x"\nbefore = "5"\nlabel = "a"\nforget_after = true\n', "whole"),
    ],
)
def test_rule_errors(tmp_path, rules, error):
    path = tmp_path / "rules.toml"
    path.write_text(rules)
    with pytest.raises(RuleError, match=error.replace("[", r"\[").replace("]", r"\]")):
        load_rules(path, parse_before, lambda a: a)


def test_rule_defaults(tmp_path):
    path = tmp_path / "rules.toml"
    path.write_text('[[rule]]\nname = "x"\nbefore = 5\nlabel = "a"\n')
    (rule,) = load_rules(path, parse_before, lambda a: a)
    assert rule.protect == DEFAULT_PROTECT and rule.waived == [] and rule.accounts == []
    assert rule.forget_after is None and rule.forget_ms is None  # kept for ever


def test_forget_after_is_years_from_now(tmp_path):
    path = tmp_path / "rules.toml"
    path.write_text('[[rule]]\nname = "x"\nbefore = 2\nlabel = "a"\nforget_after = 7\n')
    (rule,) = load_rules(path, parse_before, lambda a: a)
    assert rule.forget_after == 7 and rule.forget_ms == parse_before("7")


def test_unknown_account_is_an_error(data, capsys):
    with pytest.raises(SystemExit, match="account"):
        run(data, '[[rule]]\nname = "x"\nbefore = "5"\nlabel = "a"\naccount = "nobody"\n')


def test_subject_ignores_case_accents_and_entities(data):
    rows = run(data, '[[rule]]\nname = "cafe"\nsubject = "café"\nbefore = "2020-01-01"\n')
    assert {r["gmail_id"] for r in rows} == {"n1", "n2", "n3", "n4"}  # a substring match


@pytest.mark.parametrize(
    ("text", "folded"),
    [
        ("Café menu 2012", "cafe menu 2012"),
        ("CAFE", "cafe"),
        ("Caf&eacute;", "cafe"),
        ("Caf\u00c3\u00a9 menu 2011", "cafe menu 2011"),  # UTF-8 read as Latin-1
        ("Résumé", "resume"),  # Latin-1 that isn't mojibake stays as it is
        ("Москва", "москва"),
    ],
)
def test_fold(text, folded):
    assert fold(text) == folded


def test_to_me(data):
    base = '[[rule]]\nname = "w"\nlabel = "work"\naccount = "a@x.com"\nbefore = "2020-01-01"\n'
    assert {r["gmail_id"] for r in run(data, base + "to_me = true\n")} == {"t1"}
    assert {r["gmail_id"] for r in run(data, base + "to_me = false\n")} == {"w1", "w8", "p2"}


def test_except_keeps_matches_of_any_key(data, capsys):
    rules = """
[[rule]]
name = "work"
label = "work"
before = "2020-01-01"
except = { subject = ["S-W1"], sender = "@shop.com", to_me = true }
"""
    rows = run(data, rules)
    assert {r["gmail_id"] for r in rows} == {"w8", "b1"}  # w1, p2, t1 excepted
    assert "kept: rule exception" in capsys.readouterr().out


# ── query: a Gmail search ────────────────────────────────────────────────────


@pytest.fixture
def gmail_search(monkeypatch):
    """What Gmail's search finds, per (account, query); the searches made, in order."""
    found = {(A, "has:attachment"): ["w1", "w4", "w8", "p1", "gone-from-backup"]}
    made = []

    def search(account, config, queries):
        made.append((account, tuple(queries)))
        return {q: found.get((account, q), []) for q in queries}

    monkeypatch.setattr(cli, "search_gmail", search)
    return made


def test_a_query_selects_what_gmail_finds_and_the_protections_still_apply(
    data, gmail_search, caplog
):
    rule = '[[rule]]\nname = "pdfs"\nquery = "has:attachment"\nbefore = "2020-01-01"\n'
    # w1, w4 and w8 carry Work, a user label the rule doesn't name
    assert {r["gmail_id"] for r in run(data, rule)} == {"p1"}
    assert gmail_search == [(A, ("has:attachment",)), (B, ("has:attachment",))]
    rows = run(data, rule + 'waive = ["labels"]\n')
    assert {r["gmail_id"] for r in rows} == {"w1", "w8", "p1"}  # w4 is starred
    assert "1 messages Gmail finds for 'has:attachment' aren't in the backup" in caplog.text


def test_a_query_narrows_like_any_scope_key_and_works_in_except(data, gmail_search):
    both = '[[rule]]\nname = "w"\nlabel = "work"\nquery = "has:attachment"\nbefore = "2020-01-01"\n'
    assert {r["gmail_id"] for r in run(data, both)} == {"w1", "w8"}  # p1 has no Work label
    excepted = (
        '[[rule]]\nname = "w"\nlabel = "work"\nbefore = "2020-01-01"\n'
        'except = { query = "has:attachment" }\n'
    )
    assert {r["gmail_id"] for r in run(data, excepted)} == {"p2", "t1", "b1"}


def test_no_query_no_gmail(data, gmail_search):
    run(data)
    assert gmail_search == []


def test_search_gmail_signs_in_read_only_and_stops_on_a_failed_search(tmp_path, monkeypatch):
    from mailcellar import gmailapi

    scopes = []
    monkeypatch.setattr(gmailapi, "find_config_folder", lambda config, account: config)
    monkeypatch.setattr(
        gmailapi,
        "load_credentials",
        lambda folder, account, scope, interactive=True: (
            scopes.append(scope) if interactive else pytest.fail("signed in non-interactively")
        ),
    )

    class Searching:
        def __init__(self, account, creds):
            pass

        def list_message_ids(self, query):
            if query == "bad":
                raise gmailapi.GmailError("HTTP 400")
            return [f"{query}-1"]

        def close(self):
            closed.append(True)

    closed = []
    monkeypatch.setattr(gmailapi, "GmailAccount", Searching)
    assert cli.search_gmail(A, tmp_path, ["a", "b"]) == {"a": ["a-1"], "b": ["b-1"]}
    assert scopes == ["readonly"] and closed == [True]
    with pytest.raises(SystemExit, match="search for 'bad' failed: HTTP 400"):
        cli.search_gmail(A, tmp_path, ["bad"])


def test_searches_are_per_account_and_asked_once():
    from mailcellar.offload.plan import searches
    from mailcellar.offload.rules import parse_rule

    def rule(**raw):
        return parse_rule({"before": "5", **raw}, parse_before, lambda a: a)

    rules = [
        rule(name="one", query=["a", "b"], account=A),
        rule(name="two", query="a"),
        rule(name="three", label="x", **{"except": {"query": "c"}}),
        rule(name="four", label="y"),
    ]
    assert searches(rules, [A, B]) == {A: ["a", "b", "c"], B: ["a", "c"]}


def test_every_plan_is_kept(data, monkeypatch):
    times = iter([1_800_000_000.0, 1_800_000_060.0])
    monkeypatch.setattr(cli.time, "time", lambda: next(times))
    run(data)
    run(data)
    kept = sorted((data / "data" / "offload" / "plans").iterdir())
    assert [p.name for p in kept] == ["2027-01-15-080000.csv", "2027-01-15-080100.csv"]
    assert kept[-1].read_text() == (data / "plan.csv").read_text()
    with open(kept[0], newline="", encoding="utf-8") as fh:
        assert {r["planned"] for r in csv.DictReader(fh)} == {"2027-01-15 08:00:00"}


def test_years_before_29_february():
    from datetime import UTC, datetime

    from mailcellar.report.cli import years_before

    leap = datetime(2028, 2, 29, 12, tzinfo=UTC)
    assert years_before(leap, 1) == datetime(2027, 2, 28, 12, tzinfo=UTC)
    assert years_before(leap, 4) == datetime(2024, 2, 29, 12, tzinfo=UTC)
    assert years_before(datetime(2026, 10, 27, tzinfo=UTC), 7) == datetime(2019, 10, 27, tzinfo=UTC)


def test_a_forget_after_no_longer_than_before_is_warned_about(data, caplog):
    rules = (
        '[[rule]]\nname = "slip"\nbefore = "7"\nlabel = "a"\nforget_after = 2\n'
        '[[rule]]\nname = "fine"\nbefore = "2"\nlabel = "a"\nforget_after = 7\n'
    )
    run(data, rules)
    assert "rule 'slip': forget_after (2 years) is no longer than before (7)" in caplog.text
    assert "'fine'" not in caplog.text
