"""
gmailapi: the Gmail API, for every command that talks to Gmail.

Accounts: <config>/accounts.toml maps each config folder name to an
address. Auth: an installed-app OAuth flow with the account's own OAuth
client (<config>/<name>/client_secrets.json); the read-only token is stored
next to it as token-readonly.json. A gmail.modify token, which can change
the mailbox, is held in memory for the run that needs it, unless
keep_modify_token in settings.toml stores it as token-modify.json; a stored
modify token also serves reads. The token file also records when consent was last given
(authorised_at): with the consent screen in Testing status Google expires
the token 7 days after that, so ageing tokens are reported before a run
fails. Every account checks it reached the mailbox it expects.

GmailAccount batches requests, shrinking batches when Gmail answers 429
and retrying rate limits, server errors and timeouts with backoff. Label
names follow gyb's convention: system labels by ID (INBOX, SENT,
YELLOW_STAR, …), user labels by name, which makes them comparable with
the labels gyb records in msg-db.sqlite.
"""

import base64
import io
import json
import logging
import random
import time
from collections import Counter, deque
from datetime import UTC, datetime
from pathlib import Path

from mailcellar import progress
from mailcellar.paths import TOKEN_WARN_DAYS, keep_modify_token, token_warn_days, write_private

SCOPES = {
    "readonly": "https://www.googleapis.com/auth/gmail.readonly",
    "modify": "https://www.googleapis.com/auth/gmail.modify",
}
BATCH_SIZE = 50  # Gmail recommends at most 50 requests per batch
RAW_BATCH_SIZE = 10  # full messages are large; gyb uses 10 for backups
MODIFY_CHUNK = 1000  # messages.batchModify limit per call
# Imports up to this size go in batches, in the request body; larger ones
# alone, as a resumable upload. gyb flushes its batches at 8 MB.
IMPORT_INLINE_LIMIT = 512 * 1024
IMPORT_BATCH_SIZE = 10
LIST_PAGE_SIZE = 500  # messages.list maximum
# Per request, including the first. With backoff of 1, 2, 4 … 64 s between
# attempts, the retries span about two minutes: Gmail's per-user quota is
# counted per minute, so a shorter span can give up inside one window.
MAX_ATTEMPTS = 8
# Per id in a batched call. The first retry is immediate (in a halved
# batch); the rest back off as above, three more at 64 s, so about five
# minutes in all. Batched calls are bulk work, and a 25,000-message read
# (offload apply, 2026-10-25) saw the per-minute quota stay exhausted for
# more than two minutes.
BATCH_ATTEMPTS = MAX_ATTEMPTS + 4
RETRY_STATUSES = {429, 500, 502, 503, 504}
# Waits at least this long (seconds) are logged at INFO, so a run held up by
# Gmail's quota says so; shorter ones, and every retried request, at DEBUG (-v).
LONG_WAIT = 10

logger = logging.getLogger(__name__)


class GmailError(Exception):
    pass


class NeedsConsent(GmailError):
    """A token needs the user to sign in, which an unattended run can't do."""


class HistoryExpired(GmailError):
    """history.list refused the start ID (404): too old, so do a full sync."""


ACCOUNTS_FILE = "accounts.toml"


def config_accounts(config_root: Path) -> dict[str, Path]:
    """
    account → config folder, from <config_root>/accounts.toml, which maps
    each folder name to its account:

        work = "alex@example.org"
        gmail    = "alex@example.com"
    """
    import tomllib

    path = config_root / ACCOUNTS_FILE
    if not path.is_file():
        raise GmailError(
            f"{path} not found; it maps config folder names to accounts "
            "(`mailcellar setup` creates it)"
        )
    with path.open("rb") as fh:
        entries = tomllib.load(fh)
    found: dict[str, Path] = {}
    for name, account in entries.items():
        if not isinstance(account, str) or "@" not in account:
            raise GmailError(f"{path}: {name} = {account!r} is not an email address")
        found[account.lower()] = config_root / name
    return found


def resolve_account(config_root: Path, name: str) -> tuple[str, Path]:
    """
    (account, config folder) for `name`: a full address, a config folder
    name from accounts.toml (e.g. 'gmail'), or a unique part of an address.
    """
    known = config_accounts(config_root)
    name_l = name.lower()
    hits = [a for a, folder in known.items() if name_l in (a, folder.name.lower())] or [
        a for a in known if name_l in a
    ]
    if len(hits) != 1:
        raise GmailError(
            f"{name!r} matches {len(hits)} of the configured accounts ({', '.join(sorted(known))})"
        )
    return hits[0], known[hits[0]]


def find_config_folder(config_root: Path, account: str) -> Path:
    """The config/<folder> that accounts.toml assigns to `account`."""
    folder = config_accounts(config_root).get(account.lower())
    if folder is None:
        raise GmailError(f"{account} is not listed in {config_root / ACCOUNTS_FILE}")
    if not (folder / "client_secrets.json").is_file():
        raise GmailError(f"{folder} has no client_secrets.json")
    return folder


def new_token_file(config_folder: Path, scope: str) -> Path:
    return config_folder / f"token-{scope}.json"


def legacy_token_file(config_folder: Path, scope: str) -> Path:
    """The name before 0.10, from when only dedup needed a token."""
    return config_folder / f"dedup-token-{scope}.json"


def token_file(config_folder: Path, scope: str) -> Path:
    """
    The account's token file for this scope: token-<scope>.json, or the old
    dedup-token-<scope>.json while that is all there is. load_credentials
    moves the old one to the new name the next time it stores a token.
    """
    new = new_token_file(config_folder, scope)
    old = legacy_token_file(config_folder, scope)
    return old if old.is_file() and not new.is_file() else new


def effective_scope(config_folder: Path, scope: str) -> str:
    """
    The scope whose token load_credentials will use. gmail.modify includes
    read access, so a read-only request reuses an existing modify token
    rather than asking for a second consent.
    """
    if (
        scope == "readonly"
        and not token_file(config_folder, "readonly").is_file()
        and token_file(config_folder, "modify").is_file()
    ):
        return "modify"
    return scope


TOKEN_FIELD = "authorised_at"  # in the token file: when consent was last given, UTC
TOKEN_LIFE_DAYS = 7  # a Testing-status token's life, from consent


def token_authorised_at(config_folder: Path, scope: str) -> datetime | None:
    """
    When consent was last given for the account's token of this scope, or
    None: no token, or one from before mailcellar recorded the time. Reads only.
    """
    try:
        return datetime.fromisoformat(
            json.loads(token_file(config_folder, scope).read_text())[TOKEN_FIELD]
        )
    except OSError, ValueError, KeyError, TypeError:
        return None


def token_age_days(authorised_at: datetime, now: datetime) -> float:
    return (now - authorised_at).total_seconds() / 86400


def renew_command(account: str, scope: str) -> str:
    return f"mailcellar auth {account} --scope {scope} --renew"


def aging_notice(
    account: str, scope: str, authorised_at: datetime | None, now: datetime, warn_days: int
) -> str:
    """
    A warning that the token is getting old, or "". Pure. mailcellar can't
    tell whether the Google Cloud consent screen is in Testing status (where
    the token expires after 7 days), so this goes by the time since consent
    and says so; warn_days = 0 turns it off.
    """
    if not warn_days or authorised_at is None:
        return ""
    age = token_age_days(authorised_at, now)
    if age < warn_days:
        return ""
    return (
        f"{account}: the {scope} token was authorised {int(age)} days ago. If the Google "
        f"Cloud consent screen is in Testing status, Google expires it {TOKEN_LIFE_DAYS} "
        f"days after that (mailcellar can't see the status). Renew it now with: "
        f"{renew_command(account, scope)}"
    )


# gmail.modify tokens held for this run only (unless keep_modify_token):
# (config folder, scope) → credentials, so a run signs in once per account.
_held: dict[tuple[Path, str], object] = {}


def stores_modify_token(config_folder: Path) -> bool:
    """keep_modify_token from settings.toml in the config root above `config_folder`."""
    try:
        return keep_modify_token(config_folder.parent)
    except ValueError as exc:
        raise GmailError(str(exc)) from None


def forget_modify_token(config_folder: Path, account: str) -> None:
    """
    Delete a stored gmail.modify token, from before they were held for a
    run only, saying so. Google isn't asked to revoke it: that would revoke
    the read-only token too, which the scheduled backup needs.
    """
    for path in (
        new_token_file(config_folder, "modify"),
        legacy_token_file(config_folder, "modify"),
    ):
        if not path.is_file():
            continue
        path.unlink()
        logger.info(
            f"{account}: deleted {path}. gmail.modify tokens are no longer stored: a command "
            "that changes Gmail signs in when it runs (keep_modify_token = true in "
            "settings.toml stores them again)"
        )
        if not token_file(config_folder, "readonly").is_file():
            logger.warning(
                f"{account}: there is no read-only token, which backups use; before the "
                f"next unattended backup, run: mailcellar auth {account} --scope readonly"
            )


def load_credentials(
    config_folder: Path,
    account: str,
    scope: str,
    renew: bool = False,
    interactive: bool = True,
):
    """
    Cached or freshly authorised credentials for `account` with `scope`.
    A token Google no longer accepts (revoked, or expired after 7 days in a
    "Testing" Cloud project) falls through to a new consent flow; so does
    renew=True, which replaces a token that still works with a new one,
    starting Google's 7 days afresh. A token that is getting old is
    reported (see aging_notice). With interactive=False (an unattended run:
    nobody to sign in) a token that needs consent raises NeedsConsent, naming
    the command that fixes it, instead of opening a browser. A gmail.modify
    token is held in memory for the run, never stored, unless
    keep_modify_token is set (see forget_modify_token for older ones).
    """
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    # A modify token is held for the run unless kept; a read that falls back
    # to a modify token stored earlier keeps using it, for unattended backups.
    keep = scope != "modify" or stores_modify_token(config_folder)
    scope = effective_scope(config_folder, scope)
    scope_url = SCOPES[scope]
    token_path = token_file(config_folder, scope)
    previous = token_authorised_at(config_folder, scope)
    creds = None
    why = "has no token yet"
    held = None if keep or renew else _held.get((config_folder, scope))
    if held is not None:
        creds = held  # signed in earlier in this run
    elif token_path.is_file() and not renew:
        creds = Credentials.from_authorized_user_file(str(token_path), [scope_url])
        why = "is not valid"
    if creds is not None and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            why = f"was rejected ({exc})"
            if interactive:
                ahead = f" To do it ahead of an unattended run: {renew_command(account, scope)}"
                print(
                    f"Stored {scope} token for {account} {why}; authorising again."
                    + (ahead if keep else "")
                )
            creds = None
    consented = not creds or not creds.valid
    if consented and not interactive:
        raise NeedsConsent(
            f"{account}: the {scope} token {why}, and an unattended run can't sign in. "
            f"Run: mailcellar auth {account} --scope {scope}"
        )
    if consented:
        print(
            f"Authorising {account} for gmail.{scope} — sign in as {account} "
            "in the browser and tick the Gmail permission."
            + ("" if keep else " The token is held for this run only, never stored.")
        )
        flow = InstalledAppFlow.from_client_secrets_file(
            str(config_folder / "client_secrets.json"), [scope_url]
        )
        creds = flow.run_local_server(port=0, login_hint=account)
    if not creds.has_scopes([scope_url]):
        raise GmailError(
            f"{account}: Google did not grant gmail.{scope}. On the consent "
            "screen, tick the Gmail permission. Delete "
            f"{token_path.name} and retry."
        )
    if not keep:
        _held[(config_folder, scope)] = creds
        forget_modify_token(config_folder, account)
        return creds
    now = datetime.now(UTC)
    authorised_at = now if consented else previous
    data = json.loads(creds.to_json())
    if authorised_at is not None:
        data[TOKEN_FIELD] = authorised_at.isoformat()
    stored = new_token_file(config_folder, scope)
    write_private(stored, json.dumps(data).encode())
    legacy_token_file(config_folder, scope).unlink(missing_ok=True)  # moved to the new name
    if not consented:
        try:
            warn_days = token_warn_days(config_folder.parent)
        except ValueError as exc:
            logger.warning(str(exc))
            warn_days = TOKEN_WARN_DAYS
        notice = aging_notice(account, scope, authorised_at, now, warn_days)
        if notice:
            logger.warning(notice)
    return creds


def is_retryable(exc) -> bool:
    """
    Rate limiting and transient server errors, which Google says to retry,
    socket timeouts (a stalled connection), and 400 "Precondition check
    failed": during heavy rate limiting Gmail answered a few lookups of
    ordinary messages that way, and the same lookups worked minutes later
    (2026-10-26). Repeating a request that did
    reach Gmail is harmless: reads, trash and adding a label are idempotent,
    and a repeated label create fails with 409, leaving the label for the
    next run to find.
    """
    from googleapiclient.errors import HttpError

    if isinstance(exc, TimeoutError):
        return True
    if not isinstance(exc, HttpError):
        return False
    if exc.status_code in RETRY_STATUSES:
        return True
    content = exc.content or b""
    if exc.status_code == 400:
        return b"failedPrecondition" in content
    return exc.status_code == 403 and b"ateLimitExceeded" in content


def describe(exc) -> str:
    """A short name for a retryable failure, for the log."""
    from googleapiclient.errors import HttpError

    if isinstance(exc, TimeoutError):
        return "a timeout"
    if isinstance(exc, HttpError):
        if exc.status_code == 429 or exc.status_code == 403:
            return f"{exc.status_code} (rate limit)"
        if exc.status_code == 400:
            return "400 (precondition check failed)"
        return f"HTTP {exc.status_code}"
    return type(exc).__name__


def is_not_found(exc) -> bool:
    from googleapiclient.errors import HttpError

    return isinstance(exc, HttpError) and exc.status_code == 404


def not_moved(
    gmail_ids: list[str],
    live: dict[str, set[str] | None],
    unread: list[str],
    errors: dict[str, str],
    into_trash: bool,
) -> list[str]:
    """
    "<id>: <why>" for each message not where trash (into_trash) or untrash
    should have put it, when read back: `live` its labels (None: gone from
    Gmail), `unread` the "<id>: <error>" lookups that failed, `errors` why
    its batchModify call failed. Pure.
    """
    why_unread = dict(u.split(": ", 1) for u in unread)
    verb = "trashed" if into_trash else "out of Trash"
    out = []
    for gid in gmail_ids:
        labels = live.get(gid)
        if gid in why_unread:
            out.append(f"{gid}: couldn't check it was {verb}: {why_unread[gid]}")
        elif labels is None:
            out.append(f"{gid}: gone from Gmail")
        elif ("TRASH" in labels) != into_trash:
            where = "not in Trash after trashing" if into_trash else "still in Trash"
            out.append(f"{gid}: {where}" + (f" ({errors[gid]})" if gid in errors else ""))
    return out


def prefer_listing(wanted: int, mailbox: int) -> bool:
    """
    Whether to read `wanted` messages' labels by listing the whole mailbox
    (labels_by_listing) rather than one lookup each. Pure. Measured on a
    52,000-message mailbox (2026-10-26): listing took 45 s, lookups ran at
    about 60 a second, so listing wins above about 5% of the mailbox; below
    1,000 messages lookups are quick enough either way.
    """
    return wanted > 1_000 and wanted * 20 > mailbox


class GmailAccount:
    """The handful of Gmail operations backup and apply need, for one account."""

    def __init__(self, account: str, creds, service=None, sleep=time.sleep):
        self.account = account
        if service is None:
            from googleapiclient.discovery import build

            service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self.service = service
        self._sleep = sleep
        self._label_names: dict[str, str] | None = None
        self._batch_sizes: dict[int, int] = {}  # batch_size → size that works now
        self._check_identity()

    # ── request plumbing ─────────────────────────────────────────────────────

    def _backoff(self, attempt: int, why: str) -> None:
        delay = min(2**attempt, 64) + random.random()
        level = logging.INFO if delay >= LONG_WAIT else logging.DEBUG
        logger.log(level, "%s: %s; waiting %.0f s before retrying", self.account, why, delay)
        progress.waited(delay)
        self._sleep(delay)

    def _execute(self, request):
        """Execute one request, retrying rate-limit and transient errors."""
        for attempt in range(MAX_ATTEMPTS):
            try:
                return request.execute()
            except Exception as exc:
                if not is_retryable(exc) or attempt == MAX_ATTEMPTS - 1:
                    raise
                self._backoff(attempt, f"{describe(exc)}, attempt {attempt + 1} of {MAX_ATTEMPTS}")

    def _run_batches(
        self, ids: list[str], make_request, on_result, batch_size: int = BATCH_SIZE
    ) -> None:
        """
        Send make_request(id) for every id in batches. on_result(id, response,
        exception) is called once per id with its final outcome; retryable
        failures are re-sent first, up to BATCH_ATTEMPTS attempts per id.
        Each final outcome advances the open Progress, if the caller made one.

        Gmail answers 429 "Too many concurrent requests" when a batch runs
        too many calls at once, and a batch's calls run concurrently. So the
        batch size adapts: halved after a batch with retryable failures, grown
        by one after a clean batch, up to batch_size. The size carries over
        to the next call with the same batch_size. Backoff starts once
        batches keep failing, so a quota exhausted for the minute still gets
        about five minutes of retries per id (see BATCH_ATTEMPTS).
        """
        queue = deque(ids)
        attempts: dict[str, int] = {}
        size = self._batch_sizes.get(batch_size, batch_size)
        failing = 0  # consecutive batches with retryable failures
        while queue:
            chunk = [queue.popleft() for _ in range(min(size, len(queue)))]
            retry: list[str] = []
            refused: Counter = Counter()  # why requests in this batch were refused

            def callback(request_id, response, exception, retry=retry, refused=refused):
                if exception is not None and is_retryable(exception):
                    attempts[request_id] = attempts.get(request_id, 0) + 1
                    refused[describe(exception)] += 1
                    if attempts[request_id] < BATCH_ATTEMPTS:
                        retry.append(request_id)
                        return
                    logger.debug(
                        "%s: %s refused %d times; giving up",
                        self.account,
                        request_id,
                        attempts[request_id],
                    )
                on_result(request_id, response, exception)
                progress.advance()

            # A failure of the batch request itself means no callbacks ran;
            # rebuild and resend the whole chunk.
            for batch_attempt in range(MAX_ATTEMPTS):
                batch = self.service.new_batch_http_request(callback=callback)
                for gid in chunk:
                    batch.add(make_request(gid), request_id=gid)
                try:
                    batch.execute()
                    break
                except Exception as exc:
                    if not is_retryable(exc) or batch_attempt == MAX_ATTEMPTS - 1:
                        raise
                    size = max(1, len(chunk) // 2)
                    self._backoff(batch_attempt, f"a batch request failed with {describe(exc)}")
            if retry:
                size = max(1, len(chunk) // 2)
                queue.extendleft(reversed(retry))  # retried next, in order
                reasons = ", ".join(f"{n} × {why}" for why, n in refused.most_common())
                logger.debug(
                    "%s: %d of %d requests in a batch refused (%s); batches of %d now",
                    self.account,
                    len(retry),
                    len(chunk),
                    reasons,
                    size,
                )
                if failing:
                    self._backoff(failing - 1, f"batches keep being refused ({reasons})")
                failing += 1
            else:
                size = min(batch_size, size + 1)
                failing = 0
            self._batch_sizes[batch_size] = size

    def _check_identity(self):
        profile = self._execute(self.service.users().getProfile(userId="me"))
        if profile["emailAddress"].lower() != self.account:
            raise GmailError(f"signed in as {profile['emailAddress']}, expected {self.account}")

    def label_catalogue(self) -> list[dict]:
        """The account's labels as {id, name, type}, fetched fresh."""
        resp = self._execute(self.service.users().labels().list(userId="me"))
        labels = [
            {"id": label["id"], "name": label["name"], "type": label["type"]}
            for label in resp.get("labels", [])
        ]
        self._label_names = {
            label["id"]: label["id"] if label["type"] == "system" else label["name"]
            for label in labels
        }
        return labels

    def _labels(self) -> dict[str, str]:
        """label ID → gyb-style name."""
        if self._label_names is None:
            self.label_catalogue()
        return self._label_names

    def label_names(self, label_ids) -> list[str]:
        """gyb-style names for label IDs, refreshing the label list for unknown IDs."""
        if any(i not in self._labels() for i in label_ids):
            self._label_names = None
        names = self._labels()
        return [names[i] for i in label_ids if i in names]

    # ── reads ────────────────────────────────────────────────────────────────

    def current_history_id(self) -> str:
        """The mailbox's latest historyId (users.getProfile)."""
        return str(self._execute(self.service.users().getProfile(userId="me"))["historyId"])

    def history_changes(self, start_history_id: str) -> tuple[set[str], set[str], str]:
        """
        (IDs of messages any event touched, IDs in messagesDeleted, latest
        historyId) for everything since start_history_id. Raises
        HistoryExpired if Gmail no longer has history that far back.
        """
        touched: set[str] = set()
        deleted: set[str] = set()
        page_token = None
        while True:
            try:
                resp = self._execute(
                    self.service.users()
                    .history()
                    .list(
                        userId="me",
                        startHistoryId=start_history_id,
                        maxResults=500,
                        pageToken=page_token,
                        fields="history(messagesAdded/message/id,messagesDeleted/message/id,"
                        "labelsAdded/message/id,labelsRemoved/message/id),"
                        "nextPageToken,historyId",
                    )
                )
            except Exception as exc:
                if is_not_found(exc):
                    raise HistoryExpired(
                        f"{self.account}: history from {start_history_id} is no longer available"
                    ) from exc
                raise
            for record in resp.get("history", []):
                for kind in ("messagesAdded", "messagesDeleted", "labelsAdded", "labelsRemoved"):
                    for event in record.get(kind, []):
                        touched.add(event["message"]["id"])
                        if kind == "messagesDeleted":
                            deleted.add(event["message"]["id"])
            page_token = resp.get("nextPageToken")
            if not page_token:
                return touched, deleted, str(resp["historyId"])

    def list_message_ids(
        self,
        query: str | None = None,
        include_spam_trash: bool = False,
        label_id: str | None = None,
    ) -> list[str]:
        """IDs of every message matching `query` and carrying `label_id` (all mail by default)."""
        ids: list[str] = []
        page_token = None
        while True:
            resp = self._execute(
                self.service.users()
                .messages()
                .list(
                    userId="me",
                    q=query,
                    includeSpamTrash=include_spam_trash,
                    maxResults=LIST_PAGE_SIZE,
                    pageToken=page_token,
                    fields="nextPageToken,messages/id",
                    **({"labelIds": [label_id]} if label_id else {}),
                )
            )
            ids.extend(m["id"] for m in resp.get("messages", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                return ids

    def labels_by_listing(self) -> dict[str, list[str]]:
        """
        Every message's label IDs, Spam and Trash included, from listings: all
        message IDs, then the IDs carrying each label, LIST_PAGE_SIZE per call.
        A message missing from the result is gone from Gmail. Far fewer calls
        than message_details when most of the mailbox is wanted.
        """
        labels = self.label_catalogue()
        with progress.Progress(f"{self.account}: listing labels", len(labels) + 1) as listing:
            out: dict[str, list[str]] = {
                gid: [] for gid in self.list_message_ids(include_spam_trash=True)
            }
            listing.advance()
            for label in labels:
                for gid in self.list_message_ids(include_spam_trash=True, label_id=label["id"]):
                    if gid in out:  # one that arrived after the first listing: next run
                        out[gid].append(label["id"])
                listing.advance()
        return out

    def find_message_ids(self, message_ids: dict[str, str]) -> dict[str, list[str]]:
        """
        {key: Message-ID header} → {key: IDs of the messages with it}, Trash
        and Spam included; one search each, in batches.
        """
        out: dict[str, list[str]] = {}
        errors: list[str] = []

        def on_result(key, response, exception):
            if exception is None:
                out[key] = [m["id"] for m in response.get("messages", [])]
            else:
                errors.append(f"{key}: {exception}")

        self._run_batches(
            list(message_ids),
            lambda key: (
                self.service.users()
                .messages()
                .list(
                    userId="me",
                    q=f"rfc822msgid:{message_ids[key]}",
                    includeSpamTrash=True,
                    maxResults=10,
                    fields="messages/id",
                )
            ),
            on_result,
        )
        if errors:
            raise GmailError(f"{self.account}: {len(errors)} searches failed, e.g. {errors[0]}")
        return out

    def get_raw(self, gmail_ids: list[str], on_message) -> list[str]:
        """
        Fetch full messages (format=raw), calling on_message(response) for
        each as it arrives. Messages deleted since listing are skipped.
        Returns "<id>: <error>" for messages that could not be fetched.
        """
        failed: list[str] = []

        def on_result(gid, response, exception):
            if exception is None:
                on_message(response)
            elif not is_not_found(exception):
                failed.append(f"{gid}: {exception}")

        self._run_batches(
            gmail_ids,
            lambda gid: (
                self.service.users()
                .messages()
                .get(
                    userId="me",
                    id=gid,
                    format="raw",
                    fields="id,threadId,labelIds,internalDate,sizeEstimate,raw",
                )
            ),
            on_result,
            batch_size=RAW_BATCH_SIZE,
        )
        return failed

    def message_details(
        self, gmail_ids: list[str], failures: list[str] | None = None
    ) -> dict[str, dict | None]:
        """
        threadId, labelIds, internalDate and sizeEstimate per message (no
        body); None for messages that don't exist. Lookups that still fail
        after retries raise GmailError, or are appended to `failures` as
        "<id>: <error>" (and left out of the result) when it is given.
        """
        out: dict[str, dict | None] = {}
        errors: list[str] = failures if failures is not None else []

        def on_result(gid, response, exception):
            if exception is None:
                out[gid] = response
            elif is_not_found(exception):
                out[gid] = None
            else:
                errors.append(f"{gid}: {exception}")

        self._run_batches(
            gmail_ids,
            lambda gid: (
                self.service.users()
                .messages()
                .get(
                    userId="me",
                    id=gid,
                    format="minimal",
                    fields="id,threadId,labelIds,internalDate,sizeEstimate",
                )
            ),
            on_result,
        )
        if errors and failures is None:
            raise GmailError(f"{self.account}: {len(errors)} lookups failed, e.g. {errors[0]}")
        return out

    def message_labels(
        self, gmail_ids: list[str], failures: list[str] | None = None
    ) -> dict[str, set[str] | None]:
        """
        Current label names per message; None for messages that don't exist.
        Lookups that still fail after retries raise GmailError, or are appended
        to `failures` as "<id>: <error>" (and left out of the result) when it
        is given.
        """
        names = self._labels()
        out: dict[str, set[str] | None] = {}
        errors: list[str] = failures if failures is not None else []

        def on_result(gid, response, exception):
            if exception is None:
                out[gid] = {names.get(i, i) for i in response.get("labelIds", [])}
            elif is_not_found(exception):
                out[gid] = None
            else:
                errors.append(f"{gid}: {exception}")

        self._run_batches(
            gmail_ids,
            lambda gid: (
                self.service.users()
                .messages()
                .get(userId="me", id=gid, format="minimal", fields="id,labelIds")
            ),
            on_result,
        )
        if errors and failures is None:
            raise GmailError(f"{self.account}: {len(errors)} lookups failed, e.g. {errors[0]}")
        return out

    # ── writes ───────────────────────────────────────────────────────────────

    def ensure_label(self, name: str) -> str:
        """ID of the user label `name`, created if missing."""
        for label_id, label_name in self._labels().items():
            if label_name == name:
                return label_id
        created = self._execute(
            self.service.users()
            .labels()
            .create(
                userId="me",
                body={
                    "name": name,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            )
        )
        self._label_names[created["id"]] = name
        return created["id"]

    def add_label(self, gmail_ids: list[str], label_name: str) -> None:
        label_id = self.ensure_label(label_name)
        for start in range(0, len(gmail_ids), MODIFY_CHUNK):
            self._execute(
                self.service.users()
                .messages()
                .batchModify(
                    userId="me",
                    body={
                        "ids": gmail_ids[start : start + MODIFY_CHUNK],
                        "addLabelIds": [label_id],
                    },
                )
            )

    def label_ids(self, names) -> list[str]:
        """
        IDs for gyb-style label names: system labels by ID, user labels by
        name, created (with their parents, for nested names) if missing.
        """
        system = {i for i, name in self._labels().items() if i == name}
        out = []
        for name in names:
            if name in system:
                out.append(name)
                continue
            parts = name.split("/")
            for depth in range(1, len(parts)):
                self.ensure_label("/".join(parts[:depth]))
            out.append(self.ensure_label(name))
        return out

    def remove_label(self, gmail_ids: list[str], label_name: str) -> None:
        """Remove a user label from messages; nothing to do if the label doesn't exist."""
        label_id = next((i for i, n in self._labels().items() if n == label_name), None)
        if label_id is None:
            return
        for start in range(0, len(gmail_ids), MODIFY_CHUNK):
            self._execute(
                self.service.users()
                .messages()
                .batchModify(
                    userId="me",
                    body={
                        "ids": gmail_ids[start : start + MODIFY_CHUNK],
                        "removeLabelIds": [label_id],
                    },
                )
            )

    def untrash(self, gmail_ids: list[str]) -> list[str]:
        """Move messages out of Trash, as trash() moves them in. Returns failures."""
        return self._move(gmail_ids, into_trash=False)

    def import_messages(self, messages: dict[str, tuple[bytes, list[str]]], on_imported):
        """
        Upload messages with users.messages.import: {key: (raw bytes, label
        IDs)}, calling on_imported(key, new Gmail ID) for each as it is
        stored. The date comes from the Date header, and Gmail's spam
        classifier is skipped. Returns "<key>: <error>" for failures.

        Retrying is safe for a message with a Message-ID: Gmail matches an
        import to the message already holding that Message-ID and returns
        its ID (tested against Gmail, 2026-10-25).
        """
        from googleapiclient.http import MediaIoBaseUpload

        failed: list[str] = []
        params = {
            "userId": "me",
            "internalDateSource": "dateHeader",
            "neverMarkSpam": True,
            "processForCalendar": False,
            "fields": "id",
        }

        def request(key, media=False):
            raw, label_ids = messages[key]
            body = {"labelIds": label_ids}
            if not media:
                body["raw"] = base64.urlsafe_b64encode(raw).decode()
                return self.service.users().messages().import_(body=body, **params)
            upload = MediaIoBaseUpload(io.BytesIO(raw), mimetype="message/rfc822", resumable=True)
            return self.service.users().messages().import_(body=body, media_body=upload, **params)

        def on_result(key, response, exception):
            if exception is None:
                on_imported(key, response["id"])
            else:
                failed.append(f"{key}: {exception}")

        small = [k for k, (raw, _labels) in messages.items() if len(raw) <= IMPORT_INLINE_LIMIT]
        large = [k for k in messages if len(messages[k][0]) > IMPORT_INLINE_LIMIT]
        self._run_batches(small, request, on_result, batch_size=IMPORT_BATCH_SIZE)
        for key in large:
            try:
                on_result(key, self._execute(request(key, media=True)), None)
            except Exception as exc:
                on_result(key, None, exc)
            progress.advance()
        return failed

    def trash(self, gmail_ids: list[str]) -> list[str]:
        """
        Move messages to Trash (recoverable for 30 days). Returns "<id>: <why>"
        for those Gmail doesn't show in Trash afterwards; see _move.
        """
        return self._move(gmail_ids, into_trash=True)

    def _move(self, gmail_ids: list[str], into_trash: bool) -> list[str]:
        """
        Add (into_trash) or remove the TRASH label, MODIFY_CHUNK messages per
        messages.batchModify call, where messages.trash and messages.untrash
        take one call per message and are held to about 5 a second. A batch
        call says nothing about single messages, and can fail after Gmail
        applied part of it, so every message's labels are then read back:
        only one Gmail shows where it should be counts. Returns "<id>: <why>"
        for the rest (see not_moved).
        """
        change = "addLabelIds" if into_trash else "removeLabelIds"
        where = "to Trash" if into_trash else "out of Trash"
        errors: dict[str, str] = {}
        for start in range(0, len(gmail_ids), MODIFY_CHUNK):
            chunk = gmail_ids[start : start + MODIFY_CHUNK]
            try:
                self._execute(
                    self.service.users()
                    .messages()
                    .batchModify(userId="me", body={"ids": chunk, change: ["TRASH"]})
                )
            except Exception as exc:  # the read-back below says what happened
                logger.warning(f"{self.account}: moving {len(chunk):,} {where} failed: {exc}")
                errors.update(dict.fromkeys(chunk, str(exc)))
        unread: list[str] = []
        live = self.message_labels(gmail_ids, unread)
        return not_moved(gmail_ids, live, unread, errors, into_trash)
