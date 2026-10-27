"""restore --from-pruned, --from-mbox and --from-folder: mail back from other sources."""

import base64
import csv
import sqlite3
from pathlib import Path

import pytest

from mailcellar import cli as mailcellar
from mailcellar import gmailapi
from mailcellar.backup import chain, commands, gyb, sidecar
from mailcellar.paths import pruned_folder
from mailcellar.restore import apply as restore
from mailcellar.restore import mbox, sources
from mailcellar.restore.apply import read_raw
from test_index import make_eml
from test_restore import A, B, Gmail, message

PRUNED = "2026-03-01-120000"


# ── reading an mbox ──────────────────────────────────────────────────────────


def spans_of(data: bytes, tmp_path) -> list[bytes]:
    path = tmp_path / "box.mbox"
    path.write_bytes(data)
    with path.open("rb") as fh:
        return [data[s:e] for s, e in mbox.entries(fh)]


def test_messages_start_at_from_lines_at_the_start_or_after_a_blank_line(tmp_path):
    data = (
        b"From a Mon Jan  1 00:00:00 2024\nSubject: one\n\nbody\nFrom here on, a body line\n\n"
        b"From b Tue Jan  2 00:00:00 2024\nSubject: two\n\nbody\n\n"
    )
    assert spans_of(data, tmp_path) == [
        b"Subject: one\n\nbody\nFrom here on, a body line\n\n",
        b"Subject: two\n\nbody\n\n",
    ]


def test_crlf_mbox_files_are_split_too(tmp_path):
    data = b"From a\r\nSubject: one\r\n\r\nx\r\n\r\nFrom b\r\nSubject: two\r\n\r\ny\r\n"
    assert spans_of(data, tmp_path) == [
        b"Subject: one\r\n\r\nx\r\n\r\n",
        b"Subject: two\r\n\r\ny\r\n",
    ]


def test_a_file_without_from_lines_has_no_messages(tmp_path):
    assert spans_of(b"Subject: x\n\nbody\n", tmp_path) == []


@pytest.mark.parametrize(
    "entry, expected",
    [
        (b"A: b\n\n>From x\n>>From y\n>Fromage\n", b"A: b\n\nFrom x\n>From y\n>Fromage\n"),
        (b"A: b\n\nbody\n\n", b"A: b\n\nbody\n"),  # the blank line the mbox added
        (b"A: b\r\n\r\nbody\r\n\r\n", b"A: b\r\n\r\nbody\r\n"),
        (b"A: b\n\nbody", b"A: b\n\nbody"),
    ],
)
def test_an_entry_becomes_the_message(entry, expected):
    assert mbox.message(entry) == expected


def test_the_headers_takeout_adds_are_removed_and_nothing_else():
    entry = (
        b"X-GM-THRID: 123\nX-Gmail-Labels: Inbox,\n Work\nSubject: hi\n"
        b"Message-ID: <m@example.com>\n\nX-Gmail-Labels: in the body stays\n\n"
    )
    assert mbox.message(entry) == (
        b"Subject: hi\nMessage-ID: <m@example.com>\n\nX-Gmail-Labels: in the body stays\n"
    )
    plain = b"Subject: hi\n\nbody\n"
    assert mbox.without_added_headers(plain) is plain


@pytest.mark.parametrize(
    "names, expected",
    [
        (
            ["Inbox", "Sent", "Starred", "Important", "Unread"],
            ("INBOX", "SENT", "STARRED", "IMPORTANT", "UNREAD"),
        ),
        (["INBOX", "UNREAD", "work"], ("INBOX", "UNREAD", "work")),  # as export writes them
        (["Category Promotions", "CATEGORY_SOCIAL"], ("CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL")),
        (["Opened", "Archived", "Work", " Work "], ("Work",)),
        (["Drafts", "Spam", "Trash", "Chat"], ("DRAFT", "SPAM", "TRASH", "CHAT")),
        (["Clients/Acme", ""], ("Clients/Acme",)),
        # Takeout in the account's language: German, French, Turkish (dotless ı)
        (["Posteingang", "Gesendet", "Projekte"], ("INBOX", "SENT", "Projekte")),
        (["Boîte de réception", "Messages envoyés"], ("INBOX", "SENT")),
        (["Önemli", "Gelen  Kutusu"], ("IMPORTANT", "INBOX")),
    ],
)
def test_takeout_label_names_become_gyb_labels(names, expected):
    assert mbox.label_names(names) == expected


@pytest.mark.parametrize(
    "raw, end",
    [
        (b"A: 1\r\nB: 2\r\n\r\nbody", 10),
        (b"A: 1\nB: 2\n\nbody", 9),
        (b"A: 1\n\r\nbody", 4),  # an LF header line, then a CRLF blank line
        (b"A: 1\r\n\nbody", 4),
        (b"A: 1\nno blank line", 18),
    ],
)
def test_the_headers_end_at_the_first_blank_line_whatever_its_line_ending(raw, end):
    assert mbox.header_end(raw) == end


def test_a_body_line_like_an_added_header_stays_with_mixed_line_endings():
    raw = b"A: \n\r\nX-Gmail-Labels: in the body\n"
    assert mbox.without_added_headers(raw) == raw


@pytest.mark.parametrize(
    "header, expected",
    [
        # raw UTF-8, as an mbox from elsewhere may hold it
        ("X-Gmail-Labels: Café,Москва,📁 Files".encode(), ("Café", "Москва", "📁 Files")),
        ("X-Gmail-Labels: Entwürfe,Posteingang".encode(), ("DRAFT", "INBOX")),
        # RFC 2047, as `mailcellar export` writes non-ASCII labels
        (b"X-Gmail-Labels: =?utf-8?b?Q2Fmw6k=?=", ("Café",)),
        # folded onto a second line
        (b"X-Gmail-Labels: Inbox,\r\n Clients/Acme", ("INBOX", "Clients/Acme")),
        # not UTF-8: read as Latin-1 rather than lost
        (b"X-Gmail-Labels: Caf\xe9", ("Café",)),
    ],
)
def test_labels_are_read_from_the_header_bytes(header, expected):
    entry = header + b"\r\nSubject: x\r\n\r\nbody with X-Gmail-Labels: no\r\n"
    assert mbox.labels(entry) == expected


def test_an_export_mbox_round_trips_non_ascii_labels():
    from mailcellar.restore.export import labels_header

    entry = labels_header(("Café", "Москва"), b"\r\n") + b"Subject: x\r\n\r\nbody\r\n"
    assert mbox.labels(entry) == ("Café", "Москва")


def test_labels_come_from_the_x_gmail_labels_header():
    entry = b'X-Gmail-Labels: Inbox,"Clients, Acme",Opened\nSubject: x\n\nbody\n'
    assert mbox.labels(entry) == ("INBOX", "Clients, Acme")
    encoded = b"X-Gmail-Labels: =?utf-8?q?Caf=C3=A9?=\n\nbody\n"
    assert mbox.labels(encoded) == ("Café",)
    assert mbox.labels(b"Subject: x\n\nX-Gmail-Labels: Inbox\n") == ()


# ── the other pieces ─────────────────────────────────────────────────────────


def test_dates_are_read_from_gyb_and_from_the_date_header():
    assert sources.local_ms("not a date") == 0
    assert sources.local_ms("2024-01-02 03:04:05") > 0
    assert sources.date_header_ms(b"Date: Tue, 02 Jan 2024 00:00:00 +0000\n\nx") == 1704153600000
    assert sources.date_header_ms(b"Date: whenever\n\nx") == 0
    assert sources.date_header_ms(b"Subject: x\n\nDate: Tue, 02 Jan 2024 00:00:00 +0000\n") == 0


def test_sources_that_are_not_there_say_so(tmp_path):
    with pytest.raises(sources.SourceError, match="no msg-db.sqlite"):
        sources.from_folder(tmp_path)
    with pytest.raises(sources.SourceError, match="not a file"):
        sources.from_mbox(tmp_path / "missing.mbox")
    (tmp_path / "empty.mbox").write_bytes(b"Subject: x\n\nno separators\n")
    with pytest.raises(sources.SourceError, match="no messages"):
        sources.from_mbox(tmp_path / "empty.mbox")


# ── the command ──────────────────────────────────────────────────────────────


@pytest.fixture
def env(tmp_path, monkeypatch):
    """
    A's backup of p1, p2, p3 and k1; backup --prune has pruned p1, p2 and p3
    (gone from Gmail), and Gmail still has p3 under another ID (q3). k1 stays.
    """
    monkeypatch.chdir(tmp_path)
    msgs = {g: message(g) for g in ("p1", "p3", "k1")}
    msgs["p2"] = message("p2", labels=("Label_1", "INBOX"))
    gmail = Gmail(msgs)
    folder = tmp_path / "data" / A
    commands.backup(gmail, A, folder)
    # as backup --prune does: the chain records each file's hash, then the files move
    chain.append(folder, A, "prune", removed=chain.removal(folder, ["p1", "p2", "p3"]))
    con = gyb.open_backup(folder, A)
    gyb.prune(con, folder, ["p1", "p2", "p3"], pruned_folder(folder) / PRUNED)
    con.close()
    for gid in ("p1", "p2", "p3"):
        del gmail.messages[gid]
    gmail.messages["q3"] = message("q3", body=make_eml(msgid="<p3@x>"))

    gmails = {A: gmail, B: Gmail({}, account=B)}
    monkeypatch.setattr(
        gmailapi,
        "resolve_account",
        lambda root, name: (B, Path("cfg-b")) if name in ("b", B) else (A, Path("cfg")),
    )
    monkeypatch.setattr(
        gmailapi,
        "load_credentials",
        lambda f, account, scope, interactive=True: (
            scope if interactive else pytest.fail("signed in non-interactively")
        ),
    )
    monkeypatch.setattr(gmailapi, "GmailAccount", lambda name, scope: gmails[name])
    gmail.folder, gmail.other = folder, gmails[B]
    return gmail


def run(*args, account=A):
    return mailcellar.main(["restore", account, "--config", "cfg", "--log", "log.csv", *args])


def imports(gmail):
    return {c[1]: c for c in gmail.calls if c[0] == "import"}


def backup_state(folder):
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()), [
        r[0]
        for r in sqlite3.connect(folder / "msg-db.sqlite").execute(
            "SELECT uid FROM uids ORDER BY 1"
        )
    ]


def test_a_pruned_copy_changed_since_the_prune_is_not_uploaded(env):
    (c,) = [c for c in sources.from_pruned(env.folder, PRUNED) if c.source_id == "p1"]
    assert c.sha256 == chain.file_hash(c.path)  # as the prune recorded it
    c.path.write_bytes(c.path.read_bytes().replace(b"Subject: ", b"X-Added: 1\nSubject: ", 1))
    assert run("--from-pruned", PRUNED, "--yes") == 1
    with open("log.csv", newline="", encoding="utf-8") as fh:
        got = {r["source_id"]: (r["outcome"], r["detail"]) for r in csv.DictReader(fh)}
    assert got["p1"] == ("skipped", restore.CHANGED_SINCE_RECORDED)
    assert got["p2"][0] == "imported"


def test_a_gyb_folder_with_a_chain_is_checked_against_it(env):
    (path,) = [bm.path for bm in gyb.iter_backup(env.folder) if bm.gmail_id == "k1"]
    path.write_bytes(path.read_bytes() + b"\nAdded later.\n")
    (c,) = [c for c in sources.from_folder(env.folder) if c.source_id == "k1"]
    assert restore.check_backup(c)[0] == restore.CHANGED_SINCE_RECORDED


def test_pruned_mail_dry_run_says_what_it_would_do(env, capsys):
    before = backup_state(env.folder)
    assert run("--from-pruned", PRUNED) == 0
    out = capsys.readouterr().out
    assert f"from pruned batch {PRUNED}: 3 messages" in out
    assert "would import" in out and "would record (found in Gmail)" in out
    assert imports(env) == {} and env.calls == [("search", 3)]
    assert backup_state(env.folder) == before


def test_pruned_mail_is_imported_with_its_labels_and_found_mail_recorded(env, capsys):
    assert run("--from-pruned", PRUNED, "--yes") == 0
    got = imports(env)
    assert set(got) == {"p1", "p2"}  # p3 is in Gmail as q3
    assert sorted(env.label_names(got["p2"][3])) == ["INBOX", "work"]
    restored = [c for c in env.calls if c[0] == "label"][0]
    assert sorted(restored[1]) == sorted([got["p1"][2], got["p2"][2], "q3"])
    assert not any(c[0] == "unlabel" for c in env.calls)
    # nothing recorded in the backup or indexed there: the next backup downloads them
    con = sidecar.open_meta(env.folder)
    assert sidecar.restored_ids(con) == set()
    con.close()
    assert {bm.gmail_id for bm in gyb.iter_backup(env.folder)} == {"k1"}
    commands.backup(env, A, env.folder)
    assert {bm.gmail_id for bm in gyb.iter_backup(env.folder)} == set(env.messages)


def test_restoring_again_finds_what_was_restored_and_uploads_nothing(env):
    run("--from-pruned", PRUNED, "--yes")
    env.calls.clear()
    assert run("--from-pruned", PRUNED, "--yes") == 0
    assert imports(env) == {}


def test_a_pruned_file_that_is_missing_is_skipped(env, capsys):
    next((pruned_folder(env.folder) / PRUNED).rglob("p1.eml")).unlink()
    assert run("--from-pruned", PRUNED, "--yes") == 1
    assert "skipped: p1 — no .eml in the backup" in capsys.readouterr().out
    assert set(imports(env)) == {"p2"}


def test_an_unknown_pruned_batch_lists_the_ones_there_are(env):
    with pytest.raises(SystemExit, match=f"pruned batches: {PRUNED}"):
        run("--from-pruned", "2020-01-01-000000")


def test_pruned_mail_can_go_into_another_account(env):
    assert run("--from-pruned", PRUNED, "--to", "b", "--yes", "--label-prefix", "old") == 0
    got = imports(env.other)
    assert set(got) == {"p1", "p2", "p3"}  # B has none of them
    assert sorted(env.other.label_names(got["p2"][3])) == ["INBOX", "old/work"]
    assert env.calls == [] or all(c[0] != "import" for c in env.calls)


def test_an_mbox_from_export_restores_byte_for_byte_with_its_labels(env, capsys, tmp_path):
    # export the pruned mail's twins from a second backup, then restore the mbox into B
    folder_b = tmp_path / "data" / "src@example.org"
    src = Gmail({"s1": message("s1", labels=("Label_1", "INBOX")), "s2": message("s2")})
    commands.backup(src, "src@example.org", folder_b)
    assert mailcellar.main(["export", "src@example.org", "--all", "--to", "out.mbox"]) == 0
    capsys.readouterr()
    assert run("--from-mbox", "out.mbox", "--yes", account="b") == 0
    got = imports(env.other)
    assert set(got) == {"mbox-1", "mbox-2"}
    uploaded = {
        k: base64.urlsafe_b64decode(env.other.messages[c[2]]["raw"]) for k, c in got.items()
    }
    originals = sorted(p.read_bytes() for p in folder_b.rglob("*.eml"))
    assert sorted(uploaded.values()) == originals  # the X-Gmail-Labels header is gone
    labels = sorted(sorted(env.other.label_names(c[3])) for c in got.values())
    assert labels == [["INBOX", "work"], ["work"]]


def test_a_takeout_mbox_restores_with_its_labels(env, capsys, tmp_path):
    body = make_eml(msgid="<t1@example.com>", body="From the archive\nhello")
    escaped = body.replace(b"\nFrom the", b"\n>From the")
    box = (
        b"From 1234@xxx Mon Jan 01 00:00:00 +0000 2024\n"
        b"X-GM-THRID: 1\nX-Gmail-Labels: Inbox,Opened,Category Updates,Receipts\n" + escaped + b"\n"
    )
    (tmp_path / "takeout.mbox").write_bytes(box)
    assert run("--from-mbox", "takeout.mbox", "--yes", account="b") == 0
    (call,) = imports(env.other).values()
    assert base64.urlsafe_b64decode(env.other.messages[call[2]]["raw"]) == body
    # Opened is Takeout's marker, not a label (real Gmail has CATEGORY_UPDATES as a system label)
    assert sorted(env.other.label_names(call[3])) == ["CATEGORY_UPDATES", "INBOX", "Receipts"]


def test_a_gyb_folder_from_elsewhere_restores_and_is_left_as_it_was(env, tmp_path):
    other = tmp_path / "elsewhere"
    src = Gmail({"s1": message("s1"), "k1": message("k1")})
    commands.backup(src, "old@example.org", other)
    before = backup_state(other)
    assert run("--from-folder", str(other), "--yes") == 0
    assert set(imports(env)) == {"s1"}  # A's Gmail has k1 with the same content
    assert backup_state(other) == before


def test_sources_narrow_by_id_date_sender_and_limit(env, capsys):
    run("--from-pruned", PRUNED, "--id", "p2")
    assert f"batch {PRUNED}: 1 messages" in capsys.readouterr().out
    run("--from-pruned", PRUNED, "--limit", "2")
    assert f"batch {PRUNED}: 2 messages" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="Nothing to do"):
        run("--from-pruned", PRUNED, "--sender", "@nowhere.example")


@pytest.mark.parametrize(
    "args, message_",
    [
        (["--from-pruned", PRUNED, "--all"], "take every message they hold"),
        (["--from-pruned", PRUNED, "--offloaded"], "take every message they hold"),
        (["--from-mbox", "x.mbox", "--to", "b"], "name the account to restore into"),
        (["--from-mbox", "x.mbox", "--from-pruned", PRUNED], "not allowed with"),
    ],
)
def test_sources_refuse_what_makes_no_sense(env, capsys, args, message_):
    with pytest.raises(SystemExit):
        run(*args)
    assert message_ in capsys.readouterr().err


def test_the_backup_restored_into_is_not_a_folder_from_elsewhere(env, capsys):
    with pytest.raises(SystemExit):
        run("--from-folder", str(env.folder))
    assert "select its mail with --offloaded" in capsys.readouterr().err


def test_read_raw_reads_an_mbox_entry_where_the_candidate_says(tmp_path):
    (tmp_path / "a.mbox").write_bytes(
        b"From x\nSubject: one\n\n>From a\n\nFrom y\nSubject: two\n\nb\n"
    )
    first, second = sources.from_mbox(tmp_path / "a.mbox")
    assert read_raw(first) == b"Subject: one\n\nFrom a\n"
    assert read_raw(second) == b"Subject: two\n\nb\n"
    assert (first.source_id, second.source_id) == ("mbox-1", "mbox-2")


def test_a_folder_from_elsewhere_lists_restored_mail_once(env, tmp_path):
    other = tmp_path / "elsewhere"
    src = Gmail({"s1": message("s1"), "s2": message("s2")})
    commands.backup(src, "old@example.org", other)
    con = sidecar.open_meta(other)
    sidecar.record_restored(con, "s2", "s1", "import", "run", 1)  # s2 lives on as s1
    con.commit()
    con.close()
    assert [c.source_id for c in sources.from_folder(other)] == ["s1"]


def test_a_folder_whose_index_names_files_outside_it_is_kept_to_itself(tmp_path, caplog):
    outside = tmp_path / "outside.txt"
    outside.write_text("Subject: not mail\n\na file outside the folder\n")
    folder = tmp_path / "foreign"
    con = gyb.open_backup(folder, "someone@example.com")
    for uid, rel in (("r1", "../outside.txt"), ("a1", str(outside))):
        cur = con.execute(
            "INSERT INTO messages (message_filename, message_internaldate) VALUES (?, ?)",
            (rel, "2026-01-01 00:00:00"),
        )
        con.execute("INSERT INTO uids (message_num, uid) VALUES (?, ?)", (cur.lastrowid, uid))
    con.commit()
    assert sources.from_folder(folder) == []  # neither file is read, so neither is restored
    assert caplog.text.count("is outside the folder; skipped") == 2

    # Nor does a prune move them: their rows go, the files stay where they are.
    manifest = gyb.prune(con, folder, ["r1", "a1"], tmp_path / "pruned")
    con.close()
    assert [m["file"] for m in manifest] == ["was missing", "was missing"]
    assert outside.is_file() and not list((tmp_path / "pruned").rglob("*.txt"))
