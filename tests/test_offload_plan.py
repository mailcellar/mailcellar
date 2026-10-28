"""offload plan: rules, selection, protections and the plan CSV."""

import csv
import re

import pytest

from fake_mailbox import FakeMailbox, raw_response
from mailcellar.backup import commands, sidecar
from mailcellar.backup.gyb import backup_account
from mailcellar.offload import cli
from mailcellar.offload.plan import (
    EXCLUSIONS,
    PLAN_COLUMNS,
    Message,
    Verdict,
    build_plan,
    classify,
    rule_result,
)
from mailcellar.offload.rules import (
    DEFAULT_KEEP,
    DEFAULT_PROTECT,
    KEEP,
    PROTECTIONS,
    Rule,
    RuleError,
    fold,
    load_rules,
)
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
    # w3 and w11 are yours; w3 (sent) is also its own thread you wrote in; w5 is in a
    # thread you wrote in, w6 you replied to: three conversations, each counted once
    assert dict(work.kept) == {"mine": 2, "starred": 1, "conversations": 3, "labelled": 1}
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


def test_keep_says_what_stays(data):
    rules = """
[[rule]]
name = "work"
label = "Work"
account = ["a@x.com"]
before = "2020-01-01"
keep = ["conversations", "labelled", "correspondents"]
"""
    rows = run(data, rules)  # the same as waive = ["sent", "starred"], protect = [...]
    assert {r["gmail_id"] for r in rows} == {"w11", "p2"}
    assert {r["waived"] for r in rows} == {"sent;starred"}
    nothing = rules.replace('["conversations", "labelled", "correspondents"]', "[]")
    assert {r["gmail_id"] for r in run(data, nothing)} == {
        "w1",
        "w2",
        "w3",
        "w4",
        "w5",
        "w6",
        "w8",
        "w11",
        "p2",
        "t1",
    }  # all it selects but what is left out: offloaded, in Trash, after the cut-off


def test_keep_if_ands_within_a_table_and_ors_across_them(data, capsys):
    base = '[[rule]]\nname = "work"\nlabel = "work"\nbefore = "2020-01-01"\n'
    one = 'keep_if = { subject = "s-w", sender = "@work.com" }\n'
    # every key of the table: s-w1 from work.com; s-w8 is from p.com
    assert {r["gmail_id"] for r in run(data, base + one)} == {"w8", "p2", "t1", "b1"}
    either = 'keep_if = [{ subject = "s-w1" }, { sender = "@shop.com" }]\n'
    assert {r["gmail_id"] for r in run(data, base + either)} == {"w8", "t1", "b1"}
    out = capsys.readouterr().out
    assert "kept: keep_if" in out and "keep_if, default" in out


def test_the_rules_table_says_what_each_rule_keeps():
    from mailcellar.offload.plan import keeps_text
    from mailcellar.offload.rules import parse_rule

    def keeps(**keys):
        raw = {"name": "x", "before": "5", "label": "a", **keys}
        return keeps_text(parse_rule(raw, parse_before, lambda a: a))

    assert keeps() == "default"
    assert keeps(keep_if={"sender": "@a.example"}) == "keep_if, default"
    assert keeps(keep=[*DEFAULT_KEEP, "correspondents"]) == "default, correspondents"
    assert keeps(keep=["starred", "mine"]) == "mine, starred"
    assert keeps(keep=[]) == "nothing"
    assert keeps(waive="thread") == "mine, starred, conversations (replied only), labelled"


def scoped(gid, size=10, account=A, **flags):
    names = [*PROTECTIONS, *EXCLUSIONS, "except"]
    values = {n: False for n in names} | flags
    return Message(account, gid, OLD, size, "s@example.com", "", "", [], set(), values)


def test_classify_leaves_out_then_keeps_then_takes():
    rule = Rule("r", "5", NEW)  # keep's default: mine, starred, conversations, labelled
    assert classify(rule, scoped("a", trash=True, starred=True), set()) == Verdict("trash")
    assert classify(rule, scoped("a", starred=True), {(A, "a")}) == Verdict("earlier")
    assert classify(rule, scoped("a"), {(B, "a")}) == Verdict()  # taken: B's "a" is another
    # keep_if first; thread and replied count once, as conversations
    both = scoped("a", replied=True, thread=True, sent=True, **{"except": True})
    assert classify(rule, both, set()) == Verdict(keeps=("keep_if", "mine", "conversations"))
    assert classify(rule, scoped("a", correspondents=True), set()) == Verdict()  # not in keep


def test_rule_result_counts_each_verdict_and_passes_on_what_it_took():
    messages = [
        scoped("a", size=5),
        scoped("b", size=7, starred=True, sent=True),
        scoped("c", size=11, starred=True),
        scoped("d", offloaded=True),
        scoped("e"),
        scoped("a", size=5),  # a second row for a: taken already, by this rule
    ]
    r, own = rule_result(Rule("r", "5", NEW), messages, {(A, "e")})
    assert [m.gmail_id for m in r.selected] == ["a"]
    assert own == {(A, "a")}  # only its own: build_plan adds them to what earlier rules took
    assert dict(r.excluded) == {"offloaded": 1, "earlier": 2}
    assert (r.kept_total, r.scope_total) == (2, 6)
    assert dict(r.kept) == {"mine": 1, "starred": 2} and dict(r.kept_size) == {
        "mine": 7,
        "starred": 18,
    }
    assert [m.gmail_id for m in r.kept_messages["starred"]] == ["b", "c"]


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


@pytest.mark.parametrize(
    ("rules", "error"),
    [
        ('keep = ["sent"]', "'sent' is called 'mine' in keep"),
        ('keep = ["mine", "replied"]', "'replied' is called 'conversations' in keep"),
        ('keep = ["labels"]', "'labels' is called 'labelled' in keep"),
        ('keep = ["mine", "friends"]', "keep: unknown names ['friends']"),
        ("keep = true", "keep must be a list"),
        ('keep = ["mine"]\nwaive = ["starred"]', "waive can't be mixed with keep"),
        ('keep_if = { sender = "a" }\nexcept = { label = "b" }', "except can't be mixed"),
        ('keep_if = "b"', "keep_if must be a table or a list of tables"),
        ("keep_if = []", "keep_if must be a table or a list of tables"),
        ("keep_if = [{}]", "each table needs scope keys"),
        ('keep_if = { before = "1" }', "unknown keys ['before']"),
        ('keep_if = [{ label = "a" }, { category = "x" }]', "keep_if: unknown categories"),
    ],
)
def test_keep_errors(tmp_path, rules, error):
    path = tmp_path / "rules.toml"
    path.write_text(f'[[rule]]\nname = "x"\nbefore = "5"\nlabel = "a"\n{rules}\n')
    with pytest.raises(RuleError, match=error.replace("[", r"\[").replace("]", r"\]")):
        load_rules(path, parse_before, lambda a: a)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('waive = ["thread", "replied"]', 'keep = ["mine", "starred", "labelled"]'),
        ('protect = "correspondents"', f"keep = {list(KEEP)}".replace("'", '"')),
        ('waive = ["sent", "starred", "thread", "replied", "labels"]', "keep = []"),
        ('waive = "correspondents"', ""),  # off by default already
        (
            'except = { subject = "Invoice", larger_than_mb = 1 }',
            'keep_if = [{ subject = "Invoice" }, { larger_than_mb = 1 }]',
        ),
        (
            'waive = ["labels"]\nexcept = { sender = ["@a.example", "b@c.example"] }',
            'keep = ["mine", "starred", "conversations"]\n'
            'keep_if = [{ sender = ["@a.example", "b@c.example"] }]',
        ),
    ],
)
def test_old_keys_read_as_their_new_form_says(tmp_path, old, new):
    path = tmp_path / "rules.toml"
    base = '[[rule]]\nname = "x"\nbefore = "5"\nlabel = "a"\n'
    path.write_text(base + old + "\n")
    (was,) = load_rules(path, parse_before, lambda a: a)
    path.write_text(base + new + "\n")
    (now,) = load_rules(path, parse_before, lambda a: a)
    assert (was.protect, was.exceptions) == (now.protect, now.exceptions)
    assert now.deprecated == ""
    assert was.deprecated.startswith("rule 'x': ") and "will be removed in 1.0" in was.deprecated
    said = new.replace("\n", "; ") or "nothing; keep's default is the same"
    assert was.deprecated.endswith("write it as: " + said)


def test_waiving_one_of_thread_and_replied_still_works_and_says_keep_cant(tmp_path):
    path = tmp_path / "rules.toml"
    path.write_text('[[rule]]\nname = "x"\nbefore = "5"\nlabel = "a"\nwaive = "replied"\n')
    (rule,) = load_rules(path, parse_before, lambda a: a)
    assert rule.protect == ("sent", "starred", "thread", "labels")
    assert rule.keep == ["mine", "starred", "conversations (thread only)", "labelled"]
    assert "keep can't say this rule's protections" in rule.deprecated


def test_plan_warns_about_the_old_keys(data, caplog):
    run(data, '[[rule]]\nname = "w"\nlabel = "work"\nbefore = "2020"\nwaive = "labels"\n')
    assert (
        "rule 'w': waive will be removed in 1.0; write it as: "
        'keep = ["mine", "starred", "conversations"]'
    ) in caplog.text


def test_rule_defaults(tmp_path):
    path = tmp_path / "rules.toml"
    path.write_text('[[rule]]\nname = "x"\nbefore = 5\nlabel = "a"\n')
    (rule,) = load_rules(path, parse_before, lambda a: a)
    assert rule.protect == DEFAULT_PROTECT and rule.waived == [] and rule.accounts == []
    assert rule.keep == list(DEFAULT_KEEP) and rule.exceptions == () and rule.deprecated == ""
    assert rule.forget_after is None and rule.forget_ms is None  # kept for ever


def test_forget_after_is_years_from_now(tmp_path):
    path = tmp_path / "rules.toml"
    path.write_text('[[rule]]\nname = "x"\nbefore = 2\nlabel = "a"\nforget_after = 7\n')
    earliest = parse_before("7")
    (rule,) = load_rules(path, parse_before, lambda a: a)
    latest = parse_before("7")  # "now" moves on while the rules load
    assert rule.forget_after == 7 and earliest <= rule.forget_ms <= latest


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
    assert "kept: keep_if" in capsys.readouterr().out


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


# ── bulk, to, attachment, after ──────────────────────────────────────────────

EARLIER = 1_240_000_000_000  # 2009-04-17


def mime(gid, parts, ms=OLD, labels=("INBOX",), **h):
    """A multipart message: parts are (mime type, disposition or None, file name or None)."""
    lines = ["From: someone@example.org", f"Subject: s-{gid}", f"Message-ID: <{gid}@x>"]
    lines += [f"{k.replace('_', '-')}: {v}" for k, v in h.items()]
    lines += ["MIME-Version: 1.0", 'Content-Type: multipart/mixed; boundary="b"', "", "--b"]
    lines += ["Content-Type: text/plain", "", f"body {gid}"]
    for kind, disposition, name in parts:
        lines += ["--b", f"Content-Type: {kind}"]
        if disposition:
            lines.append(
                f"Content-Disposition: {disposition}" + (f'; filename="{name}"' if name else "")
            )
        lines += ["Content-Transfer-Encoding: base64", "", "AAAA"]
    body = ("\r\n".join(lines + ["--b--", ""])).encode()
    r = raw_response(gid, ms=ms, label_ids=list(labels), body=body)
    r["threadId"] = f"t-{gid}"
    return r


KEYED = [
    message("k1", ("INBOX",), ms=EARLIER, To="Friend <Friend@P.example>"),
    message("k2", ("INBOX",), List_Id="<news.shop.example>"),
    message("k3", ("INBOX",), Precedence="bulk", Cc="team@lists.example"),
    message("k4", ("INBOX", "CATEGORY_PROMOTIONS")),
    message("k5", ("INBOX",), List_Unsubscribe="<https://shop.example/u>"),
    message("k10", ("INBOX",), List_Id="<announce.x.com>"),  # relayed through your domain
    mime("k6", [("image/jpeg", "attachment", "photo.jpg")]),
    mime("k7", [("image/png", "inline", None)]),  # a logo in the body: no attachment
    mime("k8", [("application/octet-stream", "attachment", "scan.PDF")]),
    mime("k9", [("image/png", "inline", "chart.png")]),  # inline, but named: an attachment
]


@pytest.fixture
def keyed(tmp_path, monkeypatch):
    monkeypatch.setattr(FakeMailbox, "LABELS", LABELS)
    gmail = FakeMailbox({m["id"]: m for m in KEYED})
    commands.backup(gmail, A, tmp_path / "data" / A)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def selected(tmp_path, keys):
    rule = f'[[rule]]\nname = "r"\nbefore = "2020-01-01"\n{keys}\n'
    return {r["gmail_id"] for r in run(tmp_path, rule)}


def test_bulk_is_report_bulks_signal(keyed):
    assert selected(keyed, "bulk = true") == {"k2", "k3", "k4", "k5"}  # k10's list is yours
    assert selected(keyed, "bulk = false") == {"k1", "k6", "k7", "k8", "k9", "k10"}


def test_to_matches_to_cc_and_bcc_exactly(keyed):
    assert selected(keyed, 'to = "friend@p.example"') == {"k1"}  # case ignored
    assert selected(keyed, 'to = ["@lists.example", "@p.example"]') == {"k1", "k3"}
    assert selected(keyed, 'to = "@example"') == set()  # a domain, not a suffix of one


def test_attachment_by_kind(keyed):
    assert selected(keyed, 'attachment = "any"') == {"k6", "k8", "k9"}
    assert selected(keyed, 'attachment = "image"') == {"k6", "k9"}
    assert selected(keyed, 'attachment = ["pdf", "video"]') == {"k8"}  # by its extension


def test_after_sets_a_lower_bound(keyed):
    assert selected(keyed, 'to = "@p.example"\nafter = "2009-01-01"') == {"k1"}
    assert selected(keyed, 'to = "@p.example"\nafter = "2010-01-01"') == set()


def test_the_new_keys_work_in_keep_if(keyed):
    keep_if = 'keep_if = [{ attachment = "image" }, { to = "@p.example" }]'
    assert selected(keyed, f"bulk = false\n{keep_if}") == {"k7", "k8", "k10"}


@pytest.mark.parametrize(
    ("keys", "error"),
    [
        ('label = "a"\nafter = "2030-01-01"', "after (2030-01-01) must be earlier than before"),
        ('label = "a"\nafter = "soon"', "after must be a date (YYYY-MM-DD) or a number"),
        ('label = "a"\nafter = true', "after must be a date"),
        ('label = "a"\nafter = " "', "after must be a date"),  # not "no lower bound"
        ('bulk = "yes"', "bulk must be true or false"),
        ('attachment = "photos"', "unknown attachment kinds ['photos']"),
        ('label = "a"\nkeep_if = { after = "2010" }', "unknown keys ['after']"),
    ],
)
def test_new_key_errors(tmp_path, keys, error):
    path = tmp_path / "rules.toml"
    path.write_text(f'[[rule]]\nname = "x"\nbefore = "2020-01-01"\n{keys}\n')
    with pytest.raises(RuleError, match=re.escape(error)):
        load_rules(path, parse_before, lambda a: a)


@pytest.mark.parametrize(
    ("mime_type", "filename", "kinds"),
    [
        ("image/jpeg", "", {"any", "image"}),
        ("IMAGE/HEIC", "x.heic", {"any", "image"}),
        ("application/pdf", "", {"any", "pdf"}),
        ("application/octet-stream", "Scan.PDF", {"any", "pdf"}),
        ("", "clip.mov", {"any", "video"}),
        ("audio/mpeg", "song.mp3", {"any", "audio"}),
        (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "",
            {"any", "office"},
        ),
        ("application/vnd.ms-excel", "", {"any", "office"}),
        ("application/x-zip-compressed", "", {"any", "archive"}),
        ("application/rar", "", {"any", "archive"}),
        ("text/csv", "", {"any", "office"}),
        ("application/vnd.apple.pkpass", "pass.pkpass", {"any"}),  # Apple, but not a document
        ("text/plain", "notes.pdf", {"any"}),  # typed: the extension doesn't count
        ("application/octet-stream", "README", {"any"}),
    ],
)
def test_attachment_kinds(mime_type, filename, kinds):
    from mailcellar.offload.rules import ATTACHMENT_KINDS, attachment_is

    assert {k for k in ATTACHMENT_KINDS if attachment_is(mime_type, filename, k)} == kinds


# ── plan --try ───────────────────────────────────────────────────────────────

WORK = 'label = "work", before = "2020-01-01"'


def plan_try(tmp_path, rule, *args, rules=RULES):
    (tmp_path / "rules.toml").write_text(rules)
    cli.main(["plan", "--rules", "rules.toml", "--try", rule, *args])


def test_try_writes_no_plan_and_ends_with_the_rule_to_paste(data, capsys):
    plan_try(data, WORK, "--rules", "none")
    offload = data / "data" / "offload"
    assert not (offload / "offload-plan.csv").exists() and not (offload / "plans").exists()
    out = capsys.readouterr().out
    assert "the --try rule\n" in out and "  try " in out  # the rule alone; no file to follow
    pasted = out.split("paste this into ")[1].split(":\n\n", 1)[1]
    assert pasted == '[[rule]]\nname = "try"\nlabel = "work"\nbefore = "2020-01-01"\n'
    (data / "pasted.toml").write_text(pasted)
    assert [r.name for r in load_rules(data / "pasted.toml", parse_before, lambda a: a)] == ["try"]


def test_try_runs_after_the_files_rules(data, capsys):
    plan_try(data, 'name = "again", ' + WORK, "--plan", "tried.csv")
    out = capsys.readouterr().out
    assert "after the rules in rules.toml" in out and "taken by an earlier rule" in out
    assert "  shop " not in out  # the file's rules aren't shown again
    with open(data / "tried.csv", newline="", encoding="utf-8") as fh:
        assert list(csv.DictReader(fh)) == []  # work took all of it first
    assert len(list((data / "data" / "offload" / "plans").iterdir())) == 1  # --plan: kept too
    plan_try(data, WORK, "--rules", "none", "--plan", "tried.csv")
    with open(data / "tried.csv", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert {r["gmail_id"] for r in rows} == {"w1", "w8", "p2", "t1", "b1"}  # as the file's work
    assert {r["rule"] for r in rows} == {"try"}


def test_try_reviews_only_its_rule(data, capsys):
    plan_try(data, WORK, "--review")
    assert [p.name for p in (data / "data" / "offload" / "review").iterdir()] == ["try.html"]


@pytest.mark.parametrize(
    ("rule", "args", "error"),
    [
        ('lable = "a", before = "5"', (), "rule 'try': unknown keys ['lable']"),  # as in the file
        ('label = "a" before = "5"', (), "--try: "),  # not TOML
        ('name = "work", ' + WORK, (), "rules.toml has a rule named 'work' already"),
        ('keep = ["sent"], ' + WORK, (), "'sent' is called 'mine' in keep"),
    ],
)
def test_try_refuses_a_bad_rule_as_the_file_would(data, rule, args, error):
    with pytest.raises(SystemExit, match=re.escape(error)):
        plan_try(data, rule, *args)


def test_rules_none_needs_try(data):
    with pytest.raises(SystemExit, match="--rules none plans nothing without --try"):
        cli.main(["plan", "--rules", "none"])


def test_rule_toml_round_trips():
    import tomllib

    from mailcellar.offload.rules import read_try, rule_toml

    raw = read_try(
        'name = "a \\"b\\"", bulk = true, larger_than_mb = 1.5, keep = [], '
        'keep_if = [{ sender = ["@a.example", "b@c.example"] }, { attachment = "pdf" }]'
    )
    assert tomllib.loads(rule_toml(raw))["rule"] == [raw]
