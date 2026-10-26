"""GmailAccount's write and lookup methods, against a service that records the requests sent."""

import pytest
from googleapiclient.errors import HttpError

from mailcellar import gmailapi
from test_backup import ACCOUNT, Req, account, http_error


class RecordingService:
    """
    A small stateful stand-in for the Gmail service: it holds the labels,
    pages of message IDs and per-message labels, and records every request
    as a tuple in `requests`. `fail` maps (operation, id) to a list of
    exceptions raised, one per attempt, before the request succeeds.
    """

    def __init__(self, labels=(), pages=(), messages=None, fail=None):
        self.labels_ = [dict(label) for label in labels]
        self.pages = list(pages)  # messages.list responses, in order
        self.messages_ = messages or {}  # gmail_id → label IDs
        self.fail = {key: list(errors) for key, errors in (fail or {}).items()}
        self.requests = []

    def of(self, kind):
        return [r[1:] for r in self.requests if r[0] == kind]

    def _request(self, kind, detail, result, key=None):
        def run():
            self.requests.append((kind, *detail))
            errors = self.fail.get((kind, key), [])
            if errors:
                raise errors.pop(0)
            return result() if callable(result) else result

        return Req(run)

    def users(self):
        return self

    def getProfile(self, userId):
        return Req(lambda: {"emailAddress": ACCOUNT, "historyId": "9"})

    def labels(self):
        service = self

        class Labels:
            def list(self, userId):
                return service._request("labels.list", (), lambda: {"labels": service.labels_})

            def create(self, userId, body):
                def create():
                    label = {"id": f"Label_{len(service.labels_) + 1}", "type": "user", **body}
                    service.labels_.append(label)
                    return label

                return service._request("labels.create", (body["name"], body), create)

        return Labels()

    def messages(self):
        service = self

        class Messages:
            def list(self, userId, q=None, includeSpamTrash=False, pageToken=None, **kwargs):
                return service._request(
                    "messages.list",
                    (q, includeSpamTrash, pageToken, kwargs.get("maxResults")),
                    lambda: service.pages.pop(0) if service.pages else {},
                )

            def get(self, userId, id, **kwargs):
                def get():
                    if id not in service.messages_:
                        raise http_error(404)
                    return {"id": id, "labelIds": service.messages_[id]}

                return service._request("get", (id,), get, key=id)

            def batchModify(self, userId, body):
                add, remove = body.get("addLabelIds", []), body.get("removeLabelIds", [])

                def modify():
                    for gid in body["ids"]:
                        if gid in service.messages_:
                            kept = [x for x in service.messages_[gid] if x not in remove]
                            service.messages_[gid] = kept + [x for x in add if x not in kept]
                    return {}

                return service._request(
                    "batchModify", (body["ids"], add, remove), modify, key=body["ids"][0]
                )

            def trash(self, userId, id):
                return service._request("trash", (id,), {"id": id}, key=id)

            def untrash(self, userId, id):
                return service._request("untrash", (id,), {"id": id}, key=id)

        return Messages()

    def new_batch_http_request(self, callback):
        class Batch:
            def __init__(self):
                self.items = []

            def add(self, request, request_id):
                self.items.append((request_id, request))

            def execute(self):
                for request_id, request in self.items:
                    try:
                        callback(request_id, request.execute(), None)
                    except HttpError as exc:
                        callback(request_id, None, exc)

        return Batch()


SYSTEM = [{"id": "INBOX", "name": "INBOX", "type": "system"}]
WORK = {"id": "Label_1", "name": "work", "type": "user"}


def mailbox(**kwargs):
    labels = kwargs.pop("labels", [*SYSTEM, WORK])
    service = RecordingService(labels=labels, **kwargs)
    return service, account(service)


# ── labels ───────────────────────────────────────────────────────────────────


def test_ensure_label_finds_an_existing_label_without_creating_one():
    service, gmail = mailbox()
    assert gmail.ensure_label("work") == "Label_1"
    assert gmail.ensure_label("INBOX") == "INBOX"  # system labels are their own IDs
    assert service.of("labels.create") == []


def test_ensure_label_creates_a_missing_label_once():
    service, gmail = mailbox()
    new_id = gmail.ensure_label("offload-candidate")
    assert gmail.ensure_label("offload-candidate") == new_id  # remembered, not created again
    created = service.of("labels.create")
    assert [name for name, _body in created] == ["offload-candidate"]
    assert created[0][1] == {
        "name": "offload-candidate",
        "labelListVisibility": "labelShow",
        "messageListVisibility": "show",
    }


def test_label_ids_maps_system_labels_by_id_and_user_labels_by_name():
    service, gmail = mailbox()
    assert gmail.label_ids(["INBOX", "work"]) == ["INBOX", "Label_1"]
    assert service.of("labels.create") == []


def test_label_ids_creates_parent_labels_for_nested_names():
    service, gmail = mailbox()
    ids = gmail.label_ids(["clients/acme/2026"])
    assert [name for name, _body in service.of("labels.create")] == [
        "clients",
        "clients/acme",
        "clients/acme/2026",
    ]
    assert ids == [service.labels_[-1]["id"]]


def test_label_ids_does_not_recreate_an_existing_parent():
    service, gmail = mailbox(labels=[*SYSTEM, WORK, {"id": "Label_2", "name": "a", "type": "user"}])
    gmail.label_ids(["a/b"])
    assert [name for name, _body in service.of("labels.create")] == ["a/b"]


def test_label_catalogue_lists_every_label_and_names_them_gyb_style():
    service, gmail = mailbox()
    assert gmail.label_catalogue() == [
        {"id": "INBOX", "name": "INBOX", "type": "system"},
        {"id": "Label_1", "name": "work", "type": "user"},
    ]
    assert gmail.label_names(["INBOX", "Label_1"]) == ["INBOX", "work"]
    assert len(service.of("labels.list")) == 1  # label_names used what was fetched


def test_label_names_refreshes_the_list_once_for_an_unknown_id():
    service, gmail = mailbox()
    assert gmail.label_names(["Label_1"]) == ["work"]
    service.labels_.append({"id": "Label_2", "name": "later", "type": "user"})
    assert gmail.label_names(["Label_2"]) == ["later"]
    assert len(service.of("labels.list")) == 2


def test_add_label_sends_chunks_of_a_thousand():
    service, gmail = mailbox()
    ids = [f"g{i}" for i in range(2_500)]
    gmail.add_label(ids, "work")
    sent = service.of("batchModify")
    assert [len(chunk) for chunk, _add, _remove in sent] == [1000, 1000, 500]
    assert [i for chunk, _add, _remove in sent for i in chunk] == ids
    assert all(add == ["Label_1"] and remove == [] for _chunk, add, remove in sent)


def test_add_label_creates_the_label_first_and_nothing_to_add_sends_nothing():
    service, gmail = mailbox()
    gmail.add_label(["g1"], "offload-candidate")
    assert service.of("labels.create")[0][0] == "offload-candidate"
    (chunk, add, _remove) = service.of("batchModify")[0]
    assert chunk == ["g1"] and add == [service.labels_[-1]["id"]]
    gmail.add_label([], "work")
    assert len(service.of("batchModify")) == 1


def test_remove_label_sends_chunks_of_a_thousand():
    service, gmail = mailbox()
    ids = [f"g{i}" for i in range(1_001)]
    gmail.remove_label(ids, "work")
    sent = service.of("batchModify")
    assert [len(chunk) for chunk, _add, _remove in sent] == [1000, 1]
    assert all(add == [] and remove == ["Label_1"] for _chunk, add, remove in sent)


def test_remove_label_that_does_not_exist_does_nothing():
    service, gmail = mailbox()
    gmail.remove_label(["g1"], "never-made")
    assert service.of("labels.create") == [] and service.of("batchModify") == []


# ── trash and untrash ────────────────────────────────────────────────────────

TRASH = {"id": "TRASH", "name": "TRASH", "type": "system"}


def test_trash_adds_the_trash_label_a_thousand_at_a_time_then_reads_each_back():
    ids = [f"g{i}" for i in range(2_500)]
    service, gmail = mailbox(labels=[*SYSTEM, TRASH, WORK], messages={g: ["INBOX"] for g in ids})
    assert gmail.trash(ids) == []
    sent = service.of("batchModify")
    assert [len(chunk) for chunk, _add, _remove in sent] == [1000, 1000, 500]
    assert all(add == ["TRASH"] and remove == [] for _chunk, add, remove in sent)
    assert service.of("trash") == []  # no call per message
    assert len(service.of("get")) == 2_500  # each one read back
    assert all("TRASH" in labels for labels in service.messages_.values())


def test_trash_reports_what_gmail_does_not_show_in_trash_and_carries_on(monkeypatch, caplog):
    monkeypatch.setattr(gmailapi, "MODIFY_CHUNK", 2)
    messages = {"g1": ["INBOX"], "g2": ["INBOX"], "g3": ["INBOX"]}
    refused = http_error(400, b'{"error": {"reason": "invalidArgument"}}')
    service, gmail = mailbox(
        labels=[*SYSTEM, TRASH], messages=messages, fail={("batchModify", "g3"): [refused]}
    )
    failed = gmail.trash(["g1", "g2", "g3", "gone"])
    assert service.of("batchModify")[0][0] == ["g1", "g2"]
    assert [f.split(":")[0] for f in failed] == ["g3", "gone"]
    assert failed[0].startswith("g3: not in Trash after trashing (")
    assert failed[1] == "gone: gone from Gmail"
    assert "moving 2 to Trash failed" in caplog.text


def test_trash_retries_a_rate_limited_call():
    service, gmail = mailbox(
        labels=[*SYSTEM, TRASH],
        messages={"g1": ["INBOX"]},
        fail={("batchModify", "g1"): [http_error(429), http_error(503)]},
    )
    assert gmail.trash(["g1"]) == []
    assert len(service.of("batchModify")) == 3


def test_not_moved_names_each_message_not_where_it_should_be():
    live = {"t": {"TRASH", "work"}, "i": {"INBOX"}, "e": {"INBOX"}, "g": None}
    unread, errors = ["u: HTTP 500"], {"e": "boom"}
    assert gmailapi.not_moved(["t", "i", "e", "g", "u"], live, unread, errors, True) == [
        "i: not in Trash after trashing",
        "e: not in Trash after trashing (boom)",
        "g: gone from Gmail",
        "u: couldn't check it was trashed: HTTP 500",
    ]
    assert gmailapi.not_moved(["t", "i", "g", "u"], live, unread, {}, False) == [
        "t: still in Trash",
        "g: gone from Gmail",
        "u: couldn't check it was out of Trash: HTTP 500",
    ]


def test_untrash_removes_the_trash_label_a_thousand_at_a_time_then_reads_each_back():
    ids = [f"g{i}" for i in range(1_500)]
    messages = {g: ["TRASH", "Label_1"] for g in ids}
    service, gmail = mailbox(labels=[*SYSTEM, TRASH, WORK], messages=messages)
    assert gmail.untrash(ids) == []
    sent = service.of("batchModify")
    assert [len(chunk) for chunk, _add, _remove in sent] == [1000, 500]
    assert all(add == [] and remove == ["TRASH"] for _chunk, add, remove in sent)
    assert service.of("untrash") == []  # no call per message
    assert len(service.of("get")) == 1_500
    assert all(labels == ["Label_1"] for labels in service.messages_.values())  # others kept


def test_untrash_reports_what_is_still_in_trash_and_carries_on(monkeypatch, caplog):
    monkeypatch.setattr(gmailapi, "MODIFY_CHUNK", 1)
    refused = http_error(400, b'{"error": {"reason": "invalidArgument"}}')
    service, gmail = mailbox(
        labels=[*SYSTEM, TRASH],
        messages={"g1": ["TRASH"], "g2": ["TRASH"]},
        fail={("batchModify", "g1"): [refused]},
    )
    failed = gmail.untrash(["g1", "g2", "gone"])
    assert [f.split(":")[0] for f in failed] == ["g1", "gone"]
    assert failed[0].startswith("g1: still in Trash (")
    assert service.messages_["g2"] == []
    assert "moving 1 out of Trash failed" in caplog.text


# ── lookups ──────────────────────────────────────────────────────────────────


def test_message_labels_names_the_labels_and_marks_vanished_messages():
    service, gmail = mailbox(messages={"g1": ["INBOX", "Label_1"], "g2": [], "g3": ["Label_9"]})
    assert gmail.message_labels(["g1", "g2", "g3", "gone"]) == {
        "g1": {"INBOX", "work"},
        "g2": set(),
        "g3": {"Label_9"},  # an ID the account doesn't list stays as it is
        "gone": None,  # 404: the message no longer exists
    }


def test_message_labels_raises_when_a_lookup_fails():
    service, gmail = mailbox(
        messages={"g1": ["INBOX"], "g2": ["INBOX"]}, fail={("get", "g2"): [http_error(400)]}
    )
    with pytest.raises(gmailapi.GmailError, match=r"1 lookups failed, e\.g\. g2: "):
        gmail.message_labels(["g1", "g2"])


def test_prefer_listing_above_a_thousand_and_a_twentieth_of_the_mailbox():
    assert not gmailapi.prefer_listing(1_000, 2_000)  # quick enough one by one
    assert gmailapi.prefer_listing(3_000, 52_000)
    assert not gmailapi.prefer_listing(2_500, 52_000)
    assert gmailapi.prefer_listing(56_000, 56_000)  # --refresh-labels


class ListingService(RecordingService):
    """messages.list that filters by label and pages, from messages_."""

    def messages(self):
        service, inner = self, super().messages()

        class Messages:
            def __getattr__(self, name):
                return getattr(inner, name)

            def list(self, userId, includeSpamTrash=False, pageToken=None, labelIds=None, **kw):
                ids = [
                    g for g, ls in service.messages_.items() if not labelIds or labelIds[0] in ls
                ]
                start = int(pageToken or 0)
                page = ids[start : start + kw["maxResults"]]
                more = start + kw["maxResults"] < len(ids)
                result = {"messages": [{"id": g} for g in page]}
                if more:
                    result["nextPageToken"] = str(start + kw["maxResults"])
                return service._request("messages.list", (labelIds, pageToken), result)

        return Messages()


def test_labels_by_listing_gives_every_messages_labels_from_pages(monkeypatch):
    monkeypatch.setattr(gmailapi, "LIST_PAGE_SIZE", 2)
    messages = {"a": ["INBOX", "Label_1"], "b": ["INBOX"], "c": [], "d": ["Label_1", "TRASH"]}
    service = ListingService(labels=[*SYSTEM, TRASH, WORK], messages=messages)
    got = account(service).labels_by_listing()
    assert {g: sorted(ls) for g, ls in got.items()} == {g: sorted(ls) for g, ls in messages.items()}
    listed = service.of("messages.list")
    assert listed[0] == (None, None) and (["TRASH"], None) in listed  # all, then per label
    assert len(listed) == 2 + 3  # all mail in 2 pages, then INBOX, TRASH and work in 1 each


def test_list_message_ids_follows_the_pages():
    service, gmail = mailbox(
        pages=[
            {"messages": [{"id": "a"}, {"id": "b"}], "nextPageToken": "t1"},
            {"messages": [{"id": "c"}], "nextPageToken": "t2"},
            {"messages": [{"id": "d"}]},
        ]
    )
    assert gmail.list_message_ids("label:work", include_spam_trash=True) == ["a", "b", "c", "d"]
    assert service.of("messages.list") == [
        ("label:work", True, None, gmailapi.LIST_PAGE_SIZE),
        ("label:work", True, "t1", gmailapi.LIST_PAGE_SIZE),
        ("label:work", True, "t2", gmailapi.LIST_PAGE_SIZE),
    ]


def test_list_message_ids_of_an_empty_mailbox():
    service, gmail = mailbox(pages=[{}])
    assert gmail.list_message_ids() == []
    assert service.of("messages.list") == [(None, False, None, gmailapi.LIST_PAGE_SIZE)]
