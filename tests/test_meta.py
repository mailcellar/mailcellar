import sqlite3

import pytest

from mailcellar.backup import commands, sidecar
from mailcellar.dedup import cli as dedup
from mailcellar.message import PARSER_VERSION, parse_eml
from test_backup import ACCOUNT, MS, FakeGmail, raw_response, rows
from test_index import make_eml


def scalars(fields):
    """The per-message columns of a parse (its lists go to child tables)."""
    return {k: v for k, v in fields.items() if k in sidecar.FIELDS}


def meta_rows(folder):
    con = sqlite3.connect(folder / "meta.sqlite")
    try:
        return {
            "messages": {
                r[0]: r[1:]
                for r in con.execute(
                    "SELECT gmail_id, thread_id, internal_date, size_estimate, parser_version "
                    "FROM messages"
                )
            },
            "labels": sorted(con.execute("SELECT gmail_id, label_id FROM message_labels")),
            "catalogue": sorted(con.execute("SELECT * FROM labels")),
        }
    finally:
        con.close()


def backed_up(tmp_path, messages=None):
    folder = tmp_path / "data" / ACCOUNT
    gmail = FakeGmail(
        messages
        or {
            "g1": raw_response("g1", label_ids=["INBOX", "Label_1"]),
            "g2": raw_response(
                "g2",
                body=make_eml(
                    msgid="<g2@x>",
                    sender="Bob <bob@x.org>",
                    headers=[("X-Forwarded-For", "a@x b@y")],
                ),
            ),
        }
    )
    commands.backup(gmail, ACCOUNT, folder)
    return gmail, folder


def index(tmp_path, folder, name="dedup.sqlite"):
    db = tmp_path / name
    dedup.main(["index", "--db", str(db), str(folder)])
    return sqlite3.connect(db)


def dedup_rows(con):
    return sorted(
        con.execute(
            "SELECT gmail_id, path, size, message_id, sender, sender_addr, subject, "
            "body_hash, forwarded_from FROM messages"
        )
    )


def index_report(out):
    """(parsed, from_meta) from index's report line."""
    # total, parsed, same, removed, errors, no msgid, no body, meta
    cols = out.split(ACCOUNT)[-1].split()
    return int(cols[1].replace(",", "")), int(cols[7].replace(",", ""))


# ── written at download ──────────────────────────────────────────────────────


def test_backup_writes_meta_for_new_messages(tmp_path):
    gmail, folder = backed_up(tmp_path)
    m = meta_rows(folder)
    assert m["messages"]["g1"] == ("t-g1", MS, len(make_eml(msgid="<g1@x>")), PARSER_VERSION)
    assert m["labels"] == [("g1", "INBOX"), ("g1", "Label_1"), ("g2", "INBOX")]
    assert ("Label_1", "work", "user") in m["catalogue"]

    fields = sidecar.current_fields(folder)
    for gid in ("g1", "g2"):
        assert fields[gid] == scalars(parse_eml(folder / f"2026/10/22/{gid}.eml"))
    assert fields["g2"]["forwarded_from"] == "a@x"


def test_unparseable_download_still_backs_up(tmp_path, monkeypatch, capsys):
    def boom(raw):
        raise ValueError("bad mail")

    monkeypatch.setattr(commands, "parse_message", boom)
    _gmail, folder = backed_up(tmp_path)
    assert rows(folder)["uids"] == ["g1", "g2"]
    assert meta_rows(folder)["messages"] == {}
    assert "Cannot parse g1 for meta.sqlite" in capsys.readouterr().err


def test_refresh_labels_updates_meta_label_ids(tmp_path):
    gmail, folder = backed_up(tmp_path)
    gmail.messages["g1"]["labelIds"] = ["Label_1"]
    commands.backup(gmail, ACCOUNT, folder, refresh_labels=True)
    assert ("g1", "INBOX") not in meta_rows(folder)["labels"]
    assert ("g1", "Label_1") in meta_rows(folder)["labels"]


def test_sync_removes_meta_rows(tmp_path):
    gmail, folder = backed_up(tmp_path, {f"g{i}": raw_response(f"g{i}") for i in range(20)})
    del gmail.messages["g0"]
    commands.prune(gmail, ACCOUNT, folder, confirm=lambda p: True)
    m = meta_rows(folder)
    assert "g0" not in m["messages"] and len(m["messages"]) == 19
    assert not any(gid == "g0" for gid, _ in m["labels"])


# ── index reads it ───────────────────────────────────────────────────────────


def test_index_uses_meta_and_matches_parsing(tmp_path, capsys):
    _gmail, folder = backed_up(tmp_path)
    with_meta = dedup_rows(index(tmp_path, folder, "a.sqlite"))
    assert index_report(capsys.readouterr().out) == (0, 2)

    (folder / "meta.sqlite").unlink()
    without = dedup_rows(index(tmp_path, folder, "b.sqlite"))
    assert index_report(capsys.readouterr().out) == (2, 0)
    assert with_meta == without


def test_index_ignores_meta_from_an_older_parser(tmp_path, capsys):
    _gmail, folder = backed_up(tmp_path)
    con = sqlite3.connect(folder / "meta.sqlite")
    con.execute("UPDATE messages SET parser_version = 'old' WHERE gmail_id = 'g1'")
    con.commit()
    con.close()
    index(tmp_path, folder)
    assert index_report(capsys.readouterr().out) == (1, 1)


# ── backfill ─────────────────────────────────────────────────────────────────


def test_backfill_offline_then_details(tmp_path):
    gmail, folder = backed_up(tmp_path)
    (folder / "meta.sqlite").unlink()

    st = commands.backfill(None, ACCOUNT, folder)
    assert (st["added"], st["current"]) == (2, 0)
    m = meta_rows(folder)
    assert m["messages"]["g1"][:2] == ("", MS)  # thread unknown; date from msg-db
    assert m["labels"] == []
    assert sidecar.current_fields(folder)["g1"] == scalars(parse_eml(folder / "2026/10/22/g1.eml"))

    del gmail.messages["g2"]  # deleted from Gmail since
    st = commands.backfill(gmail, ACCOUNT, folder)
    assert (st["added"], st["current"], st["details"], st["gone_from_gmail"]) == (0, 2, 1, 1)
    m = meta_rows(folder)
    assert m["messages"]["g1"][:3] == ("t-g1", MS, len(make_eml(msgid="<g1@x>")))
    assert m["messages"]["g2"][0] == ""
    assert m["labels"] == [("g1", "INBOX"), ("g1", "Label_1")]

    gmail.detail_lookups = []
    commands.backfill(gmail, ACCOUNT, folder)
    assert gmail.detail_lookups == ["g2"]  # only rows still lacking details


def test_backfill_lookup_failures_are_retried_next_run(tmp_path):
    gmail, folder = backed_up(tmp_path)
    (folder / "meta.sqlite").unlink()
    gmail.fail_details = {"g1"}
    st = commands.backfill(gmail, ACCOUNT, folder)
    assert st["detail_failures"] == ["g1: HttpError 500"]
    assert st["details"] == 1
    assert meta_rows(folder)["messages"]["g1"][0] == ""

    gmail.fail_details = set()
    gmail.detail_lookups = []
    st = commands.backfill(gmail, ACCOUNT, folder)
    assert gmail.detail_lookups == ["g1"]
    assert meta_rows(folder)["messages"]["g1"][0] == "t-g1"


def test_backfill_reparses_stale_rows_and_keeps_details(tmp_path):
    _gmail, folder = backed_up(tmp_path)
    con = sqlite3.connect(folder / "meta.sqlite")
    con.execute("UPDATE messages SET parser_version = 'old', subject = 'stale'")
    con.commit()
    con.close()

    st = commands.backfill(None, ACCOUNT, folder)
    assert st["reparsed"] == 2
    assert sidecar.current_fields(folder)["g1"]["subject"] == "Hi"
    assert meta_rows(folder)["messages"]["g1"][0] == "t-g1"


def test_backfill_needs_existing_backup(tmp_path):
    with pytest.raises(commands.gyb.FormatError):
        commands.backfill(None, ACCOUNT, tmp_path / "nothing")


def test_local_to_ms_round_trips_gyb_dates():
    from mailcellar.backup.gyb import sqlite_internaldate

    for ms in (MS, MS + 514, 1_000_000_000_000):
        assert commands.local_to_ms(sqlite_internaldate(ms)) == ms


def test_cli_meta_offline(tmp_path, monkeypatch, capsys):
    _gmail, folder = backed_up(tmp_path)
    (folder / "meta.sqlite").unlink()
    config = tmp_path / "config"
    config.mkdir()
    (config / "accounts.toml").write_text(f'a = "{ACCOUNT}"\n')
    monkeypatch.chdir(tmp_path)
    assert commands.main(["meta", "a", "--offline", "--config", str(config)]) == 0
    assert "2 added" in capsys.readouterr().out
    assert len(meta_rows(folder)["messages"]) == 2


# ── copying aside before a migration ─────────────────────────────────────────


def older_sidecar(tmp_path):
    """A backed-up message's sidecar, set back to schema 9 (before the forgotten table)."""
    folder = tmp_path / "data" / ACCOUNT
    commands.backup(FakeGmail({"g1": raw_response("g1")}), ACCOUNT, folder)
    con = sqlite3.connect(folder / "meta.sqlite")
    con.execute("DROP TABLE forgotten")
    con.execute("UPDATE meta SET value = '9' WHERE name = 'schema_version'")
    con.commit()
    con.close()
    return folder


def schema_and_tables(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        (version,) = con.execute("SELECT value FROM meta WHERE name = 'schema_version'").fetchone()
        tables = {n for (n,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        return version, tables
    finally:
        con.close()


def test_an_older_sidecar_is_copied_aside_untouched_before_migrating(tmp_path, caplog):
    folder = older_sidecar(tmp_path)
    (folder / "meta.sqlite").chmod(0o600)
    sidecar.open_meta(folder).close()
    copy = folder / f"meta.sqlite.pre-v{sidecar.SCHEMA_VERSION}"
    version, tables = schema_and_tables(copy)
    assert version == "9" and "forgotten" not in tables  # as it was, not half-migrated
    assert schema_and_tables(folder / "meta.sqlite")[0] == sidecar.SCHEMA_VERSION
    assert copy.stat().st_mode & 0o777 == 0o600  # as private as the original
    assert "before migrating it" in caplog.text
    assert not list(folder.glob(".*.part"))


def test_a_copy_already_there_is_kept_and_a_current_sidecar_is_not_copied(tmp_path):
    folder = older_sidecar(tmp_path)
    copy = folder / f"meta.sqlite.pre-v{sidecar.SCHEMA_VERSION}"
    copy.write_bytes(b"the first copy")
    sidecar.open_meta(folder).close()
    assert copy.read_bytes() == b"the first copy"  # the true pre-migration state stays
    copy.unlink()
    sidecar.open_meta(folder).close()  # current now: nothing to copy
    assert not copy.exists()


def test_a_sidecar_from_a_newer_mailcellar_is_neither_copied_nor_opened(tmp_path):
    folder = older_sidecar(tmp_path)
    con = sqlite3.connect(folder / "meta.sqlite")
    con.execute("UPDATE meta SET value = '99' WHERE name = 'schema_version'")
    con.commit()
    con.close()
    with pytest.raises(sidecar.MetaError, match="has schema 99"):
        sidecar.open_meta(folder)
    assert list(folder.glob("meta.sqlite.pre-v*")) == []
