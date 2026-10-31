"""search: the full-text index over the backups, and finding mail with it."""

import json
import sqlite3
from email.message import EmailMessage
from pathlib import Path

import pytest

from fake_mailbox import ACCOUNT, FakeMailbox, raw_response
from mailcellar import cli, gmailapi
from mailcellar.backup import commands, sidecar
from mailcellar.search import db, index
from mailcellar.search.query import (
    Hit,
    Offload,
    QueryError,
    fts_query,
    keep,
    render,
    render_json,
    render_tsv,
)

DAY = 86_400_000
T0 = 1_700_006_400_000  # 2023-11-15 00:00 UTC
BATCH = "2026-01-15-093000"


def eml(subject="Hi", body="Hello", sender="Alice <alice@example.com>", html=None, to=None):
    m = EmailMessage()
    m["From"] = sender
    m["To"] = to or "Bob Example <bob@example.com>"
    m["Subject"] = subject
    m["Message-ID"] = f"<{subject.replace(' ', '.')}@example.com>"
    if html is not None and body is None:
        m.set_content(html, subtype="html")
    else:
        m.set_content(body)
        if html is not None:
            m.add_alternative(html, subtype="html")
    return m.as_bytes()


# ── the query ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "query, expected",
    [
        ("invoice", '"invoice"'),
        ("invoice 2024", '"invoice" AND "2024"'),
        ('"quarterly figures"', '"quarterly figures"'),
        ("from:alex", 'sender : "alex"'),
        (
            "FROM:alex To:example.org subject:pay",
            'sender : "alex" AND recipients : "example.org" AND subject : "pay"',
        ),
        ('from:"Alex Example"', 'sender : "Alex Example"'),
        ("invoice OR receipt", '("invoice" OR "receipt")'),
        ("tax invoice OR receipt", '"tax" AND ("invoice" OR "receipt")'),
        ("invoice -newsletter -promo", '("invoice") NOT ("newsletter" OR "promo")'),
        ("report*", '"report" *'),
        ("alex@example.com", '"alex@example.com"'),
        ('say "hi"there', '"say" AND "hi" AND "there"'),
        ("invoice or receipt", '"invoice" AND "or" AND "receipt"'),  # only OR in capitals
        ("OR invoice", '"invoice"'),
        ("invoice ... -", '"invoice"'),
        ('a"b', '"a" AND "b"'),
        ('"unclosed phrase', '"unclosed phrase"'),
        ("*", None),
    ],
)
def test_queries_become_fts5_expressions(query, expected):
    if expected is None:
        with pytest.raises(QueryError):
            fts_query(query)
    else:
        assert fts_query(query) == expected


@pytest.mark.parametrize("query", ["", "   ", "-newsletter", '""', "OR"])
def test_a_query_must_look_for_something(query):
    with pytest.raises(QueryError):
        fts_query(query)


def test_a_nul_in_a_query_is_a_space():
    # Found by Hypothesis (#36): FTS5 read the NUL as the end of the query
    # and refused the quoted phrase as unterminated.
    assert fts_query("0\x00") == '"0"'
    assert fts_query("invoice\x002024") == '"invoice" AND "2024"'


def test_a_quote_inside_a_word_cannot_break_out_of_the_phrase():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE VIRTUAL TABLE t USING fts5(sender, recipients, subject, body)")
    con.execute("INSERT INTO t VALUES ('a', 'b', 'c', 'd')")
    for query in ['x"y', 'from:"', 'a" OR "b', "NEAR(a b)", "a:b", "^a", "{a}", "(a"]:
        try:
            match = fts_query(query)
        except QueryError:
            continue
        con.execute("SELECT * FROM t WHERE t MATCH ?", (match,)).fetchall()  # no syntax error
    con.close()


# ── the indexed text ─────────────────────────────────────────────────────────


def test_html_text_is_what_a_reader_sees():
    markup = (
        "<html><head><title>T</title><style>p{color:red}</style></head>"
        "<body><!-- hidden --><p>Fish &amp; chips</p><script>var x=1;</script>"
        "<p>caf&eacute;</p></body></html>"
    )
    assert index.html_text(markup) == "Fish & chips café"


def test_the_plain_text_is_indexed_when_there_is_one():
    doc = index.document(eml(body="Plain words", html="<p>Html words</p>"))
    assert doc.body == "Plain words"


def test_html_only_mail_is_indexed_as_text():
    doc = index.document(eml(body=None, html="<p>Only <b>html</b></p><style>x{}</style>"))
    assert doc.body == "Only html"


def test_attachments_are_not_indexed():
    m = EmailMessage()
    m["From"] = "a@example.com"
    m["Subject"] = "With a file"
    m.set_content("The body")
    m.add_attachment(b"secret attachment words", maintype="text", subtype="plain", filename="a.txt")
    assert index.document(m.as_bytes()).body == "The body"


def test_the_fields_are_decoded():
    raw = eml(
        subject="=?utf-8?q?Caf=C3=A9_menu?=",
        sender="=?utf-8?q?Zo=C3=AB?= <zoe@example.org>",
        to="Bob Example <bob@example.com>, carol@example.net",
    )
    doc = index.document(raw)
    assert doc.subject == "Café menu"
    assert doc.sender == "Zoë <zoe@example.org>"
    assert doc.recipients == "Bob Example bob@example.com carol@example.net"


def test_a_long_body_is_cut_off():
    doc = index.document(eml(body="word " * index.MAX_BODY))
    assert len(doc.body) == index.MAX_BODY


@pytest.mark.parametrize(
    "known, expected_index, expected_remove",
    [
        ({}, ("a", "b"), ()),
        ({"a": ("p/a", 1, index.TEXT_VERSION), "b": ("p/b", 2, index.TEXT_VERSION)}, (), ()),
        ({"a": ("p/a", 9, index.TEXT_VERSION), "b": ("p/b", 2, index.TEXT_VERSION)}, ("a",), ()),
        ({"a": ("q/a", 1, index.TEXT_VERSION), "b": ("p/b", 2, "old")}, ("a", "b"), ()),
        (
            {
                "a": ("p/a", 1, index.TEXT_VERSION),
                "b": ("p/b", 2, index.TEXT_VERSION),
                "c": ("p/c", 3, "x"),
            },
            (),
            ("c",),
        ),
    ],
)
def test_only_new_changed_or_outdated_messages_are_read_again(
    known, expected_index, expected_remove
):
    current = {"a": ("p/a", 1), "b": ("p/b", 2)}
    todo = index.changes(known, current)
    assert (todo.index, todo.remove) == (expected_index, expected_remove)


# ── results ──────────────────────────────────────────────────────────────────


def hit(gid, account=ACCOUNT, **changes):
    fields = dict(path=f"2023/11/15/{gid}.eml", internal_date=T0, sender="Alice", subject="Hi")
    fields.update(changes)
    return Hit(account, gid, **fields)


def test_offload_filters_keep_only_offloaded_mail_of_the_rules_and_batches():
    hits = [hit("m1"), hit("m2"), hit("m3"), hit("m2", account="other@example.org")]
    offloads = {(ACCOUNT, "m2"): Offload(BATCH, "old work"), (ACCOUNT, "m3"): Offload("b2", "news")}
    ids = lambda hs: [(h.account, h.gmail_id) for h in hs]  # noqa: E731
    assert keep(hits, offloads) == hits
    assert ids(keep(hits, offloads, offloaded_only=True)) == [(ACCOUNT, "m2"), (ACCOUNT, "m3")]
    assert ids(keep(hits, offloads, rules={"news"})) == [(ACCOUNT, "m3")]
    assert ids(keep(hits, offloads, batches={BATCH})) == [(ACCOUNT, "m2")]
    assert ids(keep(hits, offloads, rules={"news"}, batches={BATCH})) == []


def test_a_result_shows_date_sender_subject_file_and_how_to_restore(tmp_path):
    lines = render(hit("m2"), tmp_path, Offload(BATCH, "old work"))
    assert lines[0] == "2023-11-15  Alice"
    assert lines[1].strip() == "Hi"
    assert lines[2].strip() == str(tmp_path / "2023/11/15/m2.eml")
    assert "old work" in lines[3] and BATCH in lines[3]
    assert lines[3].endswith(f"mailcellar restore {ACCOUNT} --id m2")
    assert len(render(hit("m1", sender="", subject=""), tmp_path, None)) == 3
    assert "(no sender)" in render(hit("m1", sender=""), tmp_path, None)[0]


def test_a_tsv_result_is_one_line_readable_part_first(tmp_path):
    line = render_tsv(hit("m2", subject="Re:\tthe\r\nplan"), tmp_path, Offload(BATCH, "old work"))
    assert line.split("\t") == [
        "2023-11-15",
        "Alice",
        "Re: the  plan",
        ACCOUNT,
        "m2",
        str(tmp_path / "2023/11/15/m2.eml"),
        "old work",
        BATCH,
    ]
    fields = render_tsv(hit("m1", sender=""), tmp_path, None).split("\t")
    assert len(fields) == 8 and fields[1] == "" and fields[6:] == ["", ""]


def test_a_json_result_is_one_object_on_one_line(tmp_path):
    line = render_json(hit("m2", subject="Café\nplan"), tmp_path, Offload(BATCH, "old work"))
    assert "\n" not in line and "Café" in line
    assert json.loads(line) == {
        "date": "2023-11-15",
        "sender": "Alice",
        "subject": "Café\nplan",
        "account": ACCOUNT,
        "id": "m2",
        "path": str(tmp_path / "2023/11/15/m2.eml"),
        "offloaded": {"rule": "old work", "batch": BATCH},
    }
    assert json.loads(render_json(hit("m1"), tmp_path, None))["offloaded"] is None


# ── the command ──────────────────────────────────────────────────────────────


@pytest.fixture
def backup(tmp_path, monkeypatch):
    """Four backed-up messages; m2 and m3 offloaded."""
    monkeypatch.chdir(tmp_path)
    specs = {
        "m1": (T0, eml("Quarterly figures", "The numbers for the quarter.")),
        "m2": (
            T0 + 10 * DAY,
            eml("Invoice 1042", "Please pay the invoice.", sender="Bob <bob@example.org>"),
        ),
        "m3": (T0 + 20 * DAY, eml("Newsletter", None, html="<p>Invoice offers, caf&eacute;</p>")),
        "m4": (T0 + 30 * DAY, eml("Receipt", "Thanks for paying.", to="carol@example.net")),
    }
    messages = {gid: raw_response(gid, ms=ms, body=body) for gid, (ms, body) in specs.items()}
    folder = tmp_path / "data" / ACCOUNT
    commands.backup(FakeMailbox(messages), ACCOUNT, folder)
    con = sidecar.open_meta(folder)
    sidecar.record_offloaded(con, ["m2"], BATCH, "old work", 1_000)
    sidecar.record_offloaded(con, ["m3"], "2026-02-01-080000", "news", 1_000)
    con.commit()
    con.close()
    # nothing in a search may need Gmail
    monkeypatch.setattr(gmailapi, "load_credentials", lambda *a, **k: 1 / 0)
    return folder


def search(*args):
    return cli.main(["search", *args])


def found(capsys):
    """The Gmail IDs a search printed, in order."""
    out = capsys.readouterr().out
    return [
        line.rsplit("/", 1)[1].removesuffix(".eml")
        for line in out.splitlines()
        if line.endswith(".eml")
    ]


def test_search_finds_words_in_the_subject_and_body_newest_first(backup, capsys):
    assert search("invoice") == 0
    assert found(capsys) == ["m3", "m2"]


def test_search_finds_by_sender_recipient_phrase_and_prefix(backup, capsys):
    cases = {
        "from:bob": ["m2"],
        "to:carol": ["m4"],
        "to:example.com": ["m3", "m2", "m1"],
        '"pay the invoice"': ["m2"],
        "pay*": ["m4", "m2"],
        "cafe": ["m3"],  # diacritics ignored
        "invoice -newsletter": ["m2"],
        "receipt OR quarterly": ["m4", "m1"],
        "subject:invoice": ["m2"],
    }
    for query, expected in cases.items():
        search(query)
        assert found(capsys) == expected, query


def test_offloaded_mail_says_how_to_bring_it_back(backup, capsys):
    search("invoice")
    out = capsys.readouterr().out
    assert f"mailcellar restore {ACCOUNT} --id m2" in out
    assert f"mailcellar restore {ACCOUNT} --id m3" in out
    assert "2 matches" in out


def test_offload_and_date_filters(backup, capsys):
    cases = [
        (["--offloaded"], ["m3", "m2"]),
        (["--rule", "news"], ["m3"]),
        (["--batch", BATCH], ["m2"]),
        (["--after", "2023-11-26"], ["m3"]),  # m2 is from 2023-11-25, m3 from 2023-12-05
        (["--before", "2023-11-26"], ["m2"]),
        (["--after", "2023-11-25", "--before", "2023-11-26"], ["m2"]),  # after is inclusive
        (["--before", "2023-11-25"], []),  # before is not
    ]
    for options, expected in cases:
        search("invoice", *options)
        assert found(capsys) == expected, options


def test_mail_restored_since_its_offload_is_not_offloaded(backup, capsys):
    con = sidecar.open_meta(backup)
    sidecar.record_restored(con, "m2", "m2", "untrash", "run", 2_000)
    con.commit()
    con.close()
    search("invoice", "--offloaded")
    assert found(capsys) == ["m3"]


def test_no_match_is_said_and_exits_1(backup, capsys):
    assert search("nonexistentword") == 1
    assert capsys.readouterr().out.strip() == "No matches."


def test_tsv_and_json_print_only_results_on_stdout(backup, capsys):
    assert search("invoice", "--format", "tsv") == 0
    out, err = capsys.readouterr()
    rows = [line.split("\t") for line in out.splitlines()]
    assert [(r[3], r[4], r[6]) for r in rows] == [
        (ACCOUNT, "m3", "news"),
        (ACCOUNT, "m2", "old work"),
    ]
    assert all(Path(r[5]).is_file() for r in rows)
    assert "2 matches" in err

    assert search("invoice", "--format", "json", "--limit", "1") == 0
    out, err = capsys.readouterr()
    assert [json.loads(line)["id"] for line in out.splitlines()] == ["m3"]
    assert "showing the newest 1" in err

    assert search("nonexistentword", "--format", "tsv") == 1
    out, err = capsys.readouterr()
    assert (out, err.strip()) == ("", "No matches.")


def test_the_limit_shows_the_newest_and_says_how_many_more(backup, capsys):
    search("invoice", "--limit", "1")
    out = capsys.readouterr().out
    assert [line.strip() for line in out.splitlines() if line.endswith(".eml")] == [
        str(next(backup.rglob("m3.eml")))
    ]
    assert "2 matches; showing the newest 1 (--limit for more)" in out


def test_the_index_follows_the_backup(backup, capsys):
    search("invoice")
    capsys.readouterr()
    con = sqlite3.connect(db.default_db())
    assert con.execute("SELECT count(*) FROM docs").fetchone()[0] == 4
    # a message leaves the backup, another changes on disk
    next(backup.rglob("m3.eml")).unlink()
    next(backup.rglob("m2.eml")).write_bytes(
        eml("Invoice 1042", "Now about apples.", sender="Bob <bob@example.org>")
    )
    search("apples")
    assert found(capsys) == ["m2"]
    search("invoice")
    assert found(capsys) == ["m2"]  # m3 gone, m2's subject still says invoice
    search('"pay the invoice"')
    assert found(capsys) == []
    assert con.execute("SELECT count(*) FROM docs").fetchone()[0] == 3
    # no text is left behind for a message read again or gone
    assert con.execute("SELECT count(*) FROM text").fetchone()[0] == 3


def test_an_unchanged_backup_is_not_read_again(backup, capsys, monkeypatch):
    search("invoice")
    capsys.readouterr()
    calls = []
    monkeypatch.setattr(index, "document", lambda raw: calls.append(raw) or 1 / 0)
    search("invoice")
    assert calls == []
    assert found(capsys) == ["m3", "m2"]


def test_no_update_searches_the_index_as_it_is(backup, capsys):
    search("invoice")
    capsys.readouterr()
    next(backup.rglob("m1.eml")).write_bytes(eml("Invoice too", "x"))
    search("invoice", "--no-update")
    assert found(capsys) == ["m3", "m2"]
    search("invoice")
    assert found(capsys) == ["m3", "m2", "m1"]


def test_a_new_text_version_reads_everything_again(backup, capsys, monkeypatch):
    search("invoice")
    monkeypatch.setattr(index, "TEXT_VERSION", "next")
    read = []
    document = index.document
    monkeypatch.setattr(index, "document", lambda raw: read.append(1) or document(raw))
    search("invoice")
    assert len(read) == 4


def test_an_unreadable_message_is_reported_and_the_rest_indexed(backup, capsys, monkeypatch):
    document = index.document

    def fussy(raw):
        if b"Receipt" in raw:
            raise ValueError("bad")
        return document(raw)

    monkeypatch.setattr(index, "document", fussy)
    search("invoice")
    out, err = capsys.readouterr()
    assert "m2.eml" in out and "m3.eml" in out
    assert "Cannot read" in err and "1 messages could not be read" in err


def test_search_changes_nothing_in_the_backup(backup, capsys):
    before = {p: p.read_bytes() for p in backup.rglob("*") if p.is_file()}
    search("invoice")
    after = {p: p.read_bytes() for p in backup.rglob("*") if p.is_file()}
    assert after == before


def test_the_index_lives_in_the_data_root(backup, capsys):
    search("invoice")
    assert db.default_db().is_file()
    assert db.default_db().parent.name == "search"
    assert not db.default_db().is_relative_to(backup)


def test_an_account_must_name_one_backup(backup, capsys):
    with pytest.raises(SystemExit):
        search("invoice", "nobody")
    assert "matches 0 of the backups" in capsys.readouterr().err


@pytest.mark.parametrize(
    "args, message",
    [
        (["--", "-only"], "only leave words out"),
        (["x", "--after", "2024-01-02", "--before", "2024-01-01"], "earlier than --before"),
        (["x", "--before", "soon"], "give a date"),
        (["x", "--after", "0.5"], "--after '0.5': a number of years must be whole"),
    ],
)
def test_bad_queries_and_dates_are_refused(backup, capsys, args, message):
    with pytest.raises(SystemExit):
        search(*args)
    assert message in capsys.readouterr().err


def test_search_is_a_command_and_documented_in_the_help(capsys):
    assert "search" in cli.COMMANDS and "mailcellar search" in cli.__doc__
    with pytest.raises(SystemExit):
        search("--help")
    out = capsys.readouterr().out
    assert out.startswith("usage: mailcellar search ")
    assert "--offloaded" in out and "from:" in out


def test_no_backups_is_an_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="no backups found"):
        search("invoice")


def test_the_old_id_of_mail_restored_under_a_new_one_is_not_listed(backup, capsys):
    con = sidecar.open_meta(backup)
    sidecar.record_restored(con, "m2", "n2", "import", "run", 2_000)
    con.commit()
    con.close()
    search("invoice")
    assert found(capsys) == ["m3"]
