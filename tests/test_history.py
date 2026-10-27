import httplib2
import pytest
from googleapiclient.errors import HttpError

from fake_mailbox import ACCOUNT, FakeMailbox, raw_response
from mailcellar import gmailapi
from mailcellar.backup import commands, sidecar
from test_backup import rows
from test_meta import meta_rows


def first_run(tmp_path, messages=None):
    gmail = FakeMailbox(messages or {"g1": raw_response("g1"), "g2": raw_response("g2")})
    folder = tmp_path / "data" / ACCOUNT
    stats = commands.backup(gmail, ACCOUNT, folder)
    assert stats["mode"] == "full"
    return gmail, folder


def history_id(folder):
    con = sidecar.open_meta(folder)
    try:
        return sidecar.get_state(con, "history_id")
    finally:
        con.close()


# ── choosing the mode ────────────────────────────────────────────────────────


def test_first_run_lists_everything_and_stores_a_start_point(tmp_path):
    gmail, folder = first_run(tmp_path)
    assert gmail.list_calls == 1
    assert history_id(folder) in gmail.snapshots


def test_later_runs_use_history_not_listing(tmp_path):
    gmail, folder = first_run(tmp_path)
    gmail.messages["g3"] = raw_response("g3")
    gmail.fetched.clear()

    stats = commands.backup(gmail, ACCOUNT, folder)
    assert stats["mode"] == "history"
    assert gmail.list_calls == 1  # no second listing
    assert gmail.fetched == ["g3"]
    assert rows(folder)["uids"] == ["g1", "g2", "g3"]


@pytest.mark.parametrize(
    "kwargs", [{"full": True}, {"refresh_labels": True}, {"query": "after:2026/10/01"}]
)
def test_full_listing_when_asked(tmp_path, kwargs):
    gmail, folder = first_run(tmp_path)
    before = history_id(folder)
    stats = commands.backup(gmail, ACCOUNT, folder, **kwargs)
    assert stats["mode"] == "full" and gmail.history_calls == 0
    if "query" in kwargs:  # a partial listing moves nothing on
        assert history_id(folder) == before
    else:
        assert history_id(folder) != before


def test_expired_history_falls_back_to_listing(tmp_path, capsys):
    gmail, folder = first_run(tmp_path)
    gmail.expired.add(history_id(folder))
    gmail.messages["g3"] = raw_response("g3")

    stats = commands.backup(gmail, ACCOUNT, folder)
    assert stats["mode"] == "full" and gmail.list_calls == 2
    assert "g3" in rows(folder)["uids"]
    assert history_id(folder) not in gmail.expired
    captured = capsys.readouterr()
    assert "listing every message instead" in captured.err
    assert "listing every message instead" not in captured.out


# ── what a history run does ──────────────────────────────────────────────────


def test_label_changes_update_both_databases(tmp_path):
    gmail, folder = first_run(tmp_path)
    gmail.messages["g1"]["labelIds"] = ["Label_1"]

    stats = commands.backup(gmail, ACCOUNT, folder)
    assert stats["labels_changed"] == 1
    assert ("g1", "work") in rows(folder)["labels"]
    assert ("g1", "INBOX") not in rows(folder)["labels"]
    assert ("g1", "Label_1") in meta_rows(folder)["labels"]
    assert gmail.fetched.count("g1") == 1  # labels only, no re-download


def test_spam_is_skipped_until_rescued(tmp_path):
    gmail, folder = first_run(tmp_path)
    gmail.messages["s1"] = raw_response("s1", label_ids=["SPAM"])
    stats = commands.backup(gmail, ACCOUNT, folder)
    assert stats["skipped"] == 1 and "s1" not in rows(folder)["uids"]

    gmail.messages["s1"]["labelIds"] = ["INBOX"]  # marked not spam
    commands.backup(gmail, ACCOUNT, folder)
    assert "s1" in rows(folder)["uids"]


def test_trashing_keeps_the_backup_and_updates_labels(tmp_path):
    gmail, folder = first_run(tmp_path)
    gmail.messages["g1"]["labelIds"] = ["TRASH"]
    stats = commands.backup(gmail, ACCOUNT, folder)
    assert stats["deleted_backed_up"] == 0
    assert ("g1", "TRASH") in meta_rows(folder)["labels"]
    assert (folder / "2026/10/22/g1.eml").is_file()


def test_permanent_deletions_are_reported_not_pruned(tmp_path, capsys, monkeypatch):
    gmail, folder = first_run(tmp_path)
    del gmail.messages["g1"]
    monkeypatch.setattr(commands, "connect", lambda args: (ACCOUNT, folder, gmail))
    assert commands.main(["backup", "a"]) == 0
    assert "1 backed-up messages were permanently deleted" in capsys.readouterr().out
    assert "g1" in rows(folder)["uids"]


def test_missing_local_file_is_refetched(tmp_path):
    gmail, folder = first_run(tmp_path)
    (folder / "2026/10/22/g2.eml").unlink()
    gmail.fetched.clear()
    stats = commands.backup(gmail, ACCOUNT, folder)
    assert stats["mode"] == "history" and gmail.fetched == ["g2"]
    assert (folder / "2026/10/22/g2.eml").is_file()


def test_failed_download_keeps_the_start_point(tmp_path):
    gmail, folder = first_run(tmp_path)
    start = history_id(folder)
    gmail.messages["g3"] = raw_response("g3")
    gmail.fail = {"g3"}
    stats = commands.backup(gmail, ACCOUNT, folder)
    assert stats["failed"] and history_id(folder) == start

    gmail.fail.clear()
    commands.backup(gmail, ACCOUNT, folder)
    assert "g3" in rows(folder)["uids"] and history_id(folder) != start


def test_reprocessing_the_same_history_is_harmless(tmp_path):
    gmail, folder = first_run(tmp_path)
    start = history_id(folder)
    gmail.messages["g3"] = raw_response("g3")
    gmail.messages["g1"]["labelIds"] = ["Label_1"]
    commands.backup(gmail, ACCOUNT, folder)
    after = rows(folder), meta_rows(folder)

    con = sidecar.open_meta(folder)  # as if the run had crashed
    sidecar.set_state(con, "history_id", start)  # before storing its result
    con.commit()
    con.close()
    commands.backup(gmail, ACCOUNT, folder)
    assert (rows(folder), meta_rows(folder)) == after


# ── gmailapi.history_changes ─────────────────────────────────────────────────


class HistoryService:
    """Fake Gmail service exposing just getProfile and history.list."""

    def __init__(self, pages=None, not_found=False):
        self.pages, self.not_found, self.calls = pages or [], not_found, []

    def users(self):
        return self

    def history(self):
        return self

    def getProfile(self, userId):
        return Req(lambda: {"emailAddress": ACCOUNT, "historyId": "555"})

    def list(self, userId, **kwargs):
        self.calls.append(kwargs)

        def run():
            if self.not_found:
                raise HttpError(httplib2.Response({"status": 404}), b"{}")
            return self.pages[len(self.calls) - 1]

        return Req(run)


class Req:
    def __init__(self, fn):
        self.execute = fn


def msg(gid):
    return {"message": {"id": gid}}


def test_history_changes_collects_every_event_across_pages():
    service = HistoryService(
        [
            {
                "history": [
                    {"messagesAdded": [msg("a")]},
                    {"labelsAdded": [msg("b")], "labelsRemoved": [msg("c")]},
                ],
                "nextPageToken": "p2",
                "historyId": "900",
            },
            {"history": [{"messagesDeleted": [msg("d")]}], "historyId": "901"},
        ]
    )
    g = gmailapi.GmailAccount(ACCOUNT, None, service=service, sleep=lambda s: None)
    assert g.current_history_id() == "555"
    touched, deleted, new = g.history_changes("400")
    assert (touched, deleted, new) == ({"a", "b", "c", "d"}, {"d"}, "901")
    assert [c["startHistoryId"] for c in service.calls] == ["400", "400"]
    assert [c.get("pageToken") for c in service.calls] == [None, "p2"]


def test_history_404_is_history_expired():
    g = gmailapi.GmailAccount(
        ACCOUNT, None, service=HistoryService(not_found=True), sleep=lambda s: None
    )
    with pytest.raises(gmailapi.HistoryExpired):
        g.history_changes("1")
