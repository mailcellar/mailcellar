"""offload suggest: rules drafted from the bulk report, planned and printed as TOML."""

import tomllib

import pytest

from fake_mailbox import FakeMailbox, raw_response
from mailcellar.backup import commands
from mailcellar.offload import cli
from mailcellar.offload.rules import load_rules
from mailcellar.offload.suggest import FORGET_HINT, Source, automated, decide, render
from mailcellar.report.cli import parse_before

A = "a@x.com"
OLD = 1_300_000_000_000  # 2011-03-13
LABELS = {"INBOX": "INBOX", "SENT": "SENT", "Label_1": "Keep"}


def message(gid, sender, labels=("INBOX",), body="", **h):
    lines = [f"From: {sender}", f"Subject: s-{gid}", f"Message-ID: <{gid}@x>"]
    lines += [f"{k.replace('_', '-')}: {v}" for k, v in h.items()]
    raw = ("\r\n".join(lines) + f"\r\n\r\nbody {gid}{body}\r\n").encode()
    return raw_response(gid, ms=OLD, label_ids=list(labels), body=raw)


def three(prefix, sender, labels=("INBOX",), **h):
    return [message(f"{prefix}{i}", sender, labels, **h) for i in range(3)]


MAILBOX = [
    *three("l", "news@shop.example", List_Id="<news.shop.example>"),  # a bulk list
    *three("d", "deals@store.example", List_Unsubscribe="<https://store.example/u>"),  # sender
    *three("n", "noreply@bank.example"),  # automated, not bulk: a notification
    *three("p", "pat@friend.example"),  # a person: never suggested
    *three("c", "club@club.example", List_Id="<club.example>"),  # engaged: you replied
    message("s1", A, ("SENT",), In_Reply_To="<c0@x>", To="club@club.example"),
    *three("k", "offers@kept.example", ("INBOX", "Label_1"), Precedence="bulk"),  # labelled
    message("big", "pat@friend.example", body="x" * 6000),  # large mail
]


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(FakeMailbox, "LABELS", LABELS)
    gmail = FakeMailbox({m["id"]: m for m in MAILBOX})
    commands.backup(gmail, A, tmp_path / "data" / A)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def suggest(tmp_path, *args, rules=None):
    if rules is not None:
        (tmp_path / "rules.toml").write_text(rules)
    cli.main(
        [
            "suggest",
            "--rules",
            "rules.toml",
            "--min-messages",
            "3",
            "--larger-than-mb",
            "0.005",
            *args,
        ]
    )


def test_suggest_prints_rules_the_rules_file_accepts(data, capsys):
    suggest(data)
    out = capsys.readouterr().out
    (data / "suggested.toml").write_text(out)
    rules = {r.name: r for r in load_rules(data / "suggested.toml", parse_before, lambda a: a)}
    assert set(rules) == {
        f"bulk lists ({A})",
        f"bulk senders ({A})",
        f"notifications ({A})",
        f"large mail ({A})",
    }
    assert rules[f"bulk lists ({A})"].scope == {"list": ["news.shop.example"]}
    assert rules[f"bulk senders ({A})"].scope == {"sender": ["deals@store.example"], "bulk": [True]}
    assert rules[f"notifications ({A})"].scope == {"sender": ["noreply@bank.example"]}
    assert rules[f"large mail ({A})"].scope == {"larger_than_mb": [0.005]}
    assert all(r.accounts == [A] and r.before == "1" for r in rules.values())
    assert all(r.forget_after is None for r in rules.values())  # a hint at most
    assert out.count(FORGET_HINT) == 2  # the bulk rules only
    # the person, the engaged list and the labelled sender are never suggested
    assert "pat@" not in out.replace("large mail", "") and "club" not in out and "kept" not in out
    assert "# takes 3 messages, " in out and '"news.shop.example",  # 3 messages in all' in out


def test_suggestions_count_what_the_rules_file_leaves(data, capsys, caplog):
    rules = '[[rule]]\nname = "lists"\nlist = "news.shop.example"\nbefore = "1"\n'
    suggest(data, rules=rules)
    out = capsys.readouterr().out
    assert "bulk lists" not in out and "bulk senders" in out
    assert f"Not suggested: 'bulk lists ({A})' would take nothing" in caplog.text


def test_append_adds_only_commented_lines(data, capsys):
    rules = '[[rule]]\nname = "lists"\nlist = "news.shop.example"\nbefore = "1"\n'
    suggest(data, "--append", rules=rules)
    text = (data / "rules.toml").read_text()
    assert text.startswith(rules)
    added = text[len(rules) :].splitlines()
    assert added[0] == "" and all(line.startswith("#") for line in added[1:])
    assert "# ── offload suggest, " in added[1]
    assert [r.name for r in load_rules(data / "rules.toml", parse_before, lambda a: a)] == ["lists"]
    uncommented = "\n".join(line[2:] for line in added[2:])  # what a user gets by uncommenting
    assert len(tomllib.loads(uncommented)["rule"]) == 3


def test_nothing_to_suggest(data, capsys):
    rules = '[[rule]]\nname = "all"\nlarger_than_mb = 0\nbefore = "1"\nkeep = []\n'
    suggest(data, rules=rules)
    assert capsys.readouterr().out.startswith("Nothing to suggest")


def source(name, kind="sender", bulk=True, account=A, messages=12):
    return Source(account, name, kind, messages, 1024 * messages, OLD, bulk)


def test_decide_groups_sources_per_account_and_kind():
    sources = [
        source("news.shop.example", "list"),
        source("deals@store.example"),
        source("orig@relayed.example", "relayed"),
        source("alerts@bank.example", bulk=False),
        source("pat@friend.example", bulk=False),  # a person: not automated
        source("hello.list.example", "list", bulk=False),  # a list that isn't bulk: never
        source("news@b.example", account="b@y.com"),
    ]
    got = decide(sources, {A: 4}, "2", 5, taken_names={f"bulk lists ({A})"})
    assert [s.name for s in got] == [
        f"bulk lists ({A}) 2",  # the rules file has the name already
        f"bulk senders ({A})",
        f"notifications ({A})",
        f"large mail ({A})",
        "bulk senders (b@y.com)",
    ]
    lists, senders, notices, large, other = (s.raw for s in got)
    assert lists == {
        "name": f"bulk lists ({A}) 2",
        "account": A,
        "list": ["news.shop.example"],
        "before": "2",
    }
    assert senders["sender"] == ["deals@store.example", "orig@relayed.example"]
    assert senders["bulk"] is True and "bulk" not in notices
    assert notices["sender"] == ["alerts@bank.example"]
    assert large == {"name": f"large mail ({A})", "account": A, "larger_than_mb": 5, "before": "2"}
    assert [s.hint for s in got] == [FORGET_HINT, FORGET_HINT, "", "", FORGET_HINT]


def test_render_puts_each_source_on_its_own_line_with_its_figures():
    (s,) = decide([source("a@b.example"), source("c@d.example")], {}, "1", 5)[:1]
    text = render(s, "24 messages, 24.0 KB")
    assert text.splitlines()[:3] == [
        "# bulk mail from senders you never engaged with, no user labels, 2 of them",
        "# takes 24 messages, 24.0 KB",
        "[[rule]]",
    ]
    assert '    "a@b.example",  # 12 messages in all, 12.0 KB, last 2011-03-13' in text
    assert text.endswith(f'before = "1"\n{FORGET_HINT}\n')
    assert tomllib.loads(text)["rule"][0]["sender"] == ["a@b.example", "c@d.example"]


@pytest.mark.parametrize(
    ("address", "is_automated"),
    [
        ("noreply@a.example", True),
        ("no-reply@a.example", True),
        ("do-not-reply@a.example", True),
        ("alerts@a.example", True),
        ("billing.support@a.example", True),
        ("notifications+abc@a.example", True),
        ("pat@a.example", False),
        ("info@a.example", False),  # someone may answer these
        ("hello@a.example", False),
        ("team@a.example", False),
        ("admin@a.example", False),
        ("accounts@a.example", False),
        ("supporter@a.example", False),
    ],
)
def test_automated(address, is_automated):
    assert automated(address) is is_automated
