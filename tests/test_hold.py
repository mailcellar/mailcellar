"""hold: legal holds (#71), their matching, the command, and what they stop."""

import csv
import mailbox
from datetime import UTC, datetime

import pytest

from mailcellar import cli
from mailcellar.backup import chain, commands, sidecar
from mailcellar.hold import holds
from mailcellar.hold.holds import Criteria, Facts, Hold, HoldError
from mailcellar.offload import forget
from mailcellar.paths import data_root
from mailcellar.report import health
from test_backup import ACCOUNT, FakeGmail, raw_response
from test_index import make_eml


def ms(date: str) -> int:
    return int(datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000)


NOW = ms("2026-11-01")


# ── decisions ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({}, "say what to hold"),
        ({"parties": ["landlord"]}, "give an address or @domain"),
        ({"after_ms": ms("2026-01-01"), "before_ms": ms("2025-01-01")}, "--after must be earlier"),
    ],
)
def test_criteria_are_checked(kwargs, error):
    with pytest.raises(HoldError, match=error):
        holds.criteria(**kwargs)


def test_parties_match_addresses_and_whole_domains():
    assert holds.party_matches("@landlord.example", "Agent@Landlord.example")
    assert not holds.party_matches("@landlord.example", "x@sub.landlord.example.org")
    assert holds.party_matches("a@x.org", "A@X.org") and not holds.party_matches(
        "a@x.org", "b@x.org"
    )


def f(gid="g", date="2026-03-01", addresses=("me@example.com",), labels=("tax",), **more):
    return Facts(
        gid,
        ms(date) if date else None,
        None if addresses is None else frozenset(addresses),
        None if labels is None else frozenset(labels),
        **more,
    )


def test_every_criterion_given_must_match_and_ids_always_do():
    c = holds.criteria(["@landlord.example"], ms("2026-01-01"), ms("2027-01-01"), "Tax", ["x9"])
    assert holds.covers(c, f(addresses=["me@example.com", "agent@landlord.example"]))
    assert not holds.covers(c, f(addresses=["me@example.com"]))  # no party
    assert not holds.covers(c, f(date="2025-06-01", addresses=["agent@landlord.example"]))
    assert not holds.covers(c, f(labels=["work"], addresses=["agent@landlord.example"]))
    assert holds.covers(c, f("x9", date="2001-01-01", addresses=[], labels=[]))  # by ID


def test_what_isnt_known_counts_as_held():
    c = holds.criteria(["@landlord.example"], ms("2026-01-01"))
    forgotten = f(date="2026-05-01", addresses=None, labels=None, known=False)
    assert holds.covers(c, forgotten)  # its addresses aren't kept: keep it
    assert not holds.covers(c, f(date="2025-05-01", addresses=None, labels=None))  # dates rule out


def test_only_holds_in_force_hold():
    c = holds.criteria(label="tax")
    hs = [
        Hold("old", "r", c, ms("2026-01-01"), released_ms=ms("2026-02-01")),
        Hold("ended", "r", c, ms("2026-01-01"), until_ms=ms("2026-10-01")),
    ]
    assert holds.holding(hs, f(), NOW) is None
    hs.append(Hold("audit", "r", c, ms("2026-01-01")))
    assert holds.holding(hs, f(), NOW) == "audit"
    assert holds.held_now(hs, [f("a"), f("b", labels=["x"])], NOW) == {"a": "audit"}


def test_criteria_round_trip_as_json():
    c = holds.criteria(["@b.example", "a@x.org"], ms("2026-01-01"), None, "Tax", ["z", "y"])
    assert Criteria.from_json(c.to_json()) == c and c.parties == ("@b.example", "a@x.org")


# ── the command ──────────────────────────────────────────────────────────────


def landlord(gid, cc=None, sender="Landlord <agent@landlord.example>"):
    headers = [("Cc", cc)] if cc else []
    return raw_response(gid, body=make_eml(msgid=f"<{gid}@x>", sender=sender, headers=headers))


@pytest.fixture
def backup():
    """d1 from the landlord, d2 sent with the landlord in Cc, u1 unrelated."""
    folder = data_root() / ACCOUNT
    gmail = FakeGmail(
        {
            "d1": landlord("d1"),
            "d2": landlord("d2", cc="agent@landlord.example", sender="Me <me@example.com>"),
            "u1": landlord("u1", sender="Alice <alice@example.com>"),
        }
    )
    commands.backup(gmail, ACCOUNT, folder)
    return gmail, folder


def hold(*args):
    return cli.main(["hold", *args])


def test_add_list_show_release(backup, capsys):
    _gmail, folder = backup
    assert (
        hold("add", "landlord-2026", "--reason", "deposit dispute", "--party", "@landlord.example")
        == 0
    )
    out = capsys.readouterr().out
    assert "hold 'landlord-2026' placed; it keeps 2 messages now" in out  # d2: sent, in Cc
    assert set(holds.held(folder, holds_now())) == {"d1", "d2"}

    assert hold("list") == 0
    assert "landlord-2026" in capsys.readouterr().out
    assert hold("show", "landlord-2026") == 0
    out = capsys.readouterr().out
    assert "reason: deposit dispute" in out and "keeps 2 messages" in out

    # One in force by that name already: refused, and nothing changes.
    assert hold("add", "landlord-2026", "--reason", "again", "--party", "x@y.org") == 1
    assert "already in force" in capsys.readouterr().err

    assert hold("release", "landlord-2026") == 0
    assert "released; its record is kept" in capsys.readouterr().out
    assert holds.held(folder, holds_now()) == {}
    assert hold("list") == 0
    out = capsys.readouterr().out
    assert "landlord-2026" not in out and "(none)" in out
    assert hold("list", "--all") == 0 and "released" in capsys.readouterr().out

    # Placing and releasing are recorded in the hash chain, which still checks clean.
    ops = [chain.parse_line(line).op for line in chain.read_lines(folder)]
    assert ops[-2:] == ["hold", "release"]
    assert "deposit dispute" in chain.parse_line(chain.read_lines(folder)[-2]).note
    assert chain.check(folder, ACCOUNT).failures == ()


def holds_now():
    from mailcellar.hold.cli import now_ms

    return now_ms()


def test_a_hold_follows_its_criteria_to_mail_that_arrives_later(backup):
    gmail, folder = backup
    hold("add", "landlord-2026", "--reason", "dispute", "--party", "@landlord.example")
    gmail.messages["d3"] = landlord("d3")  # the reply that came after the hold
    commands.backup(gmail, ACCOUNT, folder)
    assert set(holds.held(folder, holds_now())) == {"d1", "d2", "d3"}


def test_holds_need_the_new_schema_and_copy_the_old_one_aside(backup):
    _gmail, folder = backup
    con = sidecar.open_meta(folder)
    con.execute("DROP TABLE holds")
    con.execute("UPDATE meta SET value = '10' WHERE name = 'schema_version'")
    con.commit()
    con.close()
    assert holds.read_holds(folder) == []  # an older sidecar has none
    hold("add", "audit", "--reason", "tax", "--label", "work")
    assert (folder / "meta.sqlite.pre-v11").is_file()
    assert [h.name for h in holds.read_holds(folder)] == ["audit"]


# ── what a hold stops ────────────────────────────────────────────────────────


def test_prune_keeps_held_mail_in_the_backup(backup, capsys):
    gmail, folder = backup
    hold("add", "landlord-2026", "--reason", "dispute", "--party", "@landlord.example")
    for gid in ("d1", "u1"):
        del gmail.messages[gid]  # deleted in Gmail for good
    result = commands.prune(gmail, ACCOUNT, folder, confirm=lambda p: True, force=True)
    assert result["pruned"] == 1  # u1 only
    assert "keeping 1 messages under hold landlord-2026" in capsys.readouterr().out
    assert "d1" in chain.held(folder)


def test_show_says_which_held_mail_only_the_backup_has(backup, capsys):
    _gmail, folder = backup
    hold("add", "landlord-2026", "--reason", "dispute", "--party", "@landlord.example")
    con = sidecar.open_meta(folder)
    sidecar.record_offloaded(con, ["d1"], "b", "r", 1)
    sidecar.mark_gone(con, ["d1"], 2)  # offloaded, then Gmail emptied its Trash
    con.commit()
    con.close()
    capsys.readouterr()
    hold("show", "landlord-2026")
    assert "1 of them Gmail no longer has, as far as the backup knows" in capsys.readouterr().out


def test_offload_and_dedup_apply_leave_held_mail_in_gmail(backup):
    _gmail, folder = backup
    hold("add", "landlord-2026", "--reason", "dispute", "--party", "@landlord.example")
    check = holds.HoldCheck({ACCOUNT: folder}, holds_now())
    assert check({"account": ACCOUNT, "gmail_id": "d1"}) == "held: landlord-2026"
    assert check({"account": ACCOUNT, "gmail_id": "u1"}) == ""
    assert check({"account": "other@example.org", "gmail_id": "d1"}) == ""


def test_forget_and_empty_leave_held_mail(backup):
    _gmail, folder = backup
    hold("add", "landlord-2026", "--reason", "dispute", "--party", "@landlord.example")
    offloaded = [forget.Offloaded(g, "b", "r", 1, 0, 10) for g in ("d1", "u1")]
    choice = forget.choose(offloaded, {}, {}, holds.held(folder, holds_now(), ["d1", "u1"]))
    assert [(c.gmail_id, name) for c, name in choice.held] == [("d1", "landlord-2026")]
    assert [c.gmail_id for c in choice.forget] == ["u1"]

    # A held forget run: its mail keeps no addresses, so the party hold keeps it.
    run = forget.runs_dir(ACCOUNT) / "2026-01-01-000000"
    run.mkdir(parents=True)
    with (run / forget.MANIFEST).open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerows([["gmail_id", "path"], ["f9", "2026/1/1/f9.eml"]])
    under = forget.runs_under_hold(folder, ACCOUNT, ["2026-01-01-000000"], holds_now())
    assert under == {"2026-01-01-000000": ["landlord-2026"]}


def test_export_hold_writes_what_the_hold_keeps(backup, tmp_path, capsys):
    hold("add", "landlord-2026", "--reason", "dispute", "--party", "@landlord.example")
    dest = tmp_path / "out" / "landlord.mbox"
    dest.parent.mkdir()
    assert cli.main(["export", ACCOUNT, "--hold", "landlord-2026", "--to", str(dest)]) == 0
    box = mailbox.mbox(dest)
    got = {m["Message-ID"] for m in box}
    box.close()
    assert got == {"<d1@x>", "<d2@x>"}
    with pytest.raises(SystemExit, match="no hold 'nope'"):
        cli.main(["export", ACCOUNT, "--hold", "nope", "--to", str(tmp_path / "x.mbox")])


def test_health_lists_holds_and_warns_when_backups_lapse(backup):
    _gmail, folder = backup
    hold("add", "landlord-2026", "--reason", "dispute", "--party", "@landlord.example")
    (row,) = health.holds_table([folder], holds_now()).rows
    assert row[:3] == (ACCOUNT, "landlord-2026", "dispute") and row[5] == 2 and row[8] == ""
    later = holds_now() + 30 * 86_400_000
    assert health.holds_table([folder], later).rows[0][8] == "no backup in 7 days"


def test_a_message_the_sidecar_doesnt_know_counts_as_held(backup):
    _gmail, folder = backup
    hold("add", "landlord-2026", "--reason", "dispute", "--party", "@landlord.example")
    con = sidecar.open_meta(folder)  # a gyb-era backup that never had `mailcellar meta` run
    sidecar.delete_messages(con, ["u1"])
    con.commit()
    con.close()
    row = {"account": ACCOUNT, "gmail_id": "u1"}
    # The sidecar-only universe wouldn't list u1; asked about it, a hold that can't rule it
    # out holds it.
    assert holds.HoldCheck({ACCOUNT: folder}, holds_now(), [row])(row) == "held: landlord-2026"
    assert holds.HoldCheck({ACCOUNT: folder}, holds_now())(row) == "held: landlord-2026"
    # Its date isn't known either, so even a hold bounded to the year 2000 can't rule it out.
    hold("release", "landlord-2026")
    hold("add", "old", "--reason", "r", "--party", "@landlord.example", "--before", "2000-01-01")
    assert holds.HoldCheck({ACCOUNT: folder}, holds_now(), [row])(row) == "held: old"


def test_a_message_whose_addresses_werent_read_counts_as_held(backup):
    _gmail, folder = backup
    hold("add", "landlord-2026", "--reason", "dispute", "--party", "@landlord.example")
    con = sidecar.open_meta(folder)  # u1's headers weren't read: no sender, no recipients
    con.execute("UPDATE messages SET sender_addr = '' WHERE gmail_id = 'u1'")
    con.execute("DELETE FROM recipients WHERE gmail_id = 'u1'")
    con.commit()
    con.close()
    (facts,) = holds.read_facts(folder, ["u1"])
    assert facts.addresses is None
    assert "u1" in holds.held(folder, holds_now(), ["u1"])
