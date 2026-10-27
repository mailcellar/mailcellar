"""
Offload rules, read from a TOML file: one [[rule]] table per collection of
mail to take out of Gmail.

    [[rule]]
    name = "old work mail"             # unique; recorded with every message
    before = "2022-01-01"              # required: a date, or a number of years ago ("5")
    account = "work"                   # optional: address, config name or unique part
    label = "projects"                 # scope: at least one of the keys below
    except = { subject = "contract" }  # optional: keep what matches any of these
    waive = ["thread"]                 # optional: default protections to drop
    protect = ["correspondents"]       # optional: protections to add
    forget_after = 7                   # optional: years from a message's date after which
                                       # offload forget --due deletes what this rule
                                       # offloaded from the backup, once Gmail has deleted it

Scope keys take a string or a list (any value matches); different keys
must all match:

    label           user label name (case ignored)
    sender          sender address, or "@domain" (relayed mail: the original sender)
    list            List-Id
    forwarded_from  original recipient of auto-forwarded mail, or "@domain"
    category        Gmail category: promotions, updates, forums, social, personal
    subject         part of the subject, ignoring case and accents (see fold)
    to_me           true: one of your addresses is in To or Cc; false: it isn't
                    (the mail came by a list, Bcc or an alias)
    larger_than_mb  size in MB (a number)
    query           a Gmail search, as typed in Gmail ("has:attachment filename:pdf");
                    offload plan asks Gmail, read-only, which messages it finds

`except` takes the same keys; a message matching any one of them is kept.

Protections keep a message in Gmail although the rule selects it:

    sent            your mail: sent, drafts, or from one of your addresses
    starred
    thread          in a thread you wrote in (same account)
    replied         a message you replied to (from either account)
    labels          has a user label the rule doesn't name
    correspondents  from an address you have written to (off by default)
"""

import html
import tomllib
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

PROTECTIONS = {
    "sent": "your mail (sent, drafts, from your addresses)",
    "starred": "starred",
    "thread": "in a thread you wrote in",
    "replied": "you replied to it",
    "labels": "another user label",
    "correspondents": "from someone you wrote to",
}
DEFAULT_PROTECT = ("sent", "starred", "thread", "replied", "labels")
SCOPES = (
    "label",
    "sender",
    "list",
    "forwarded_from",
    "category",
    "subject",
    "to_me",
    "larger_than_mb",
    "query",
)
CATEGORIES = ("promotions", "updates", "forums", "social", "personal")
KEYS = {"name", "before", "account", "except", "waive", "protect", "forget_after", *SCOPES}


class RuleError(Exception):
    pass


@dataclass(frozen=True)
class Rule:
    name: str
    before: str  # as written
    before_ms: int  # ms since epoch, UTC
    accounts: list[str] = field(default_factory=list)  # [] means every account
    scope: dict[str, list] = field(default_factory=dict)  # scope key → values
    protect: tuple[str, ...] = DEFAULT_PROTECT
    exceptions: dict[str, list] = field(default_factory=dict)  # same keys; any one keeps
    forget_after: int | None = None  # years, as written; None: kept for ever
    forget_ms: int | None = None  # its mail is due for forgetting when older than this

    @property
    def waived(self) -> list[str]:
        return [p for p in DEFAULT_PROTECT if p not in self.protect]

    @property
    def queries(self) -> list[str]:
        """The Gmail searches the rule's scope and exception name."""
        return [*self.scope.get("query", []), *self.exceptions.get("query", [])]

    @property
    def labels(self) -> set[str]:
        """The user labels the rule names, lower case."""
        return {v.lower() for v in self.scope.get("label", [])}


def years_text(n: int) -> str:
    """A forget_after as text: "1 year", "7 years"."""
    return f"{n} year" + ("" if n == 1 else "s")


def short_retention(rule: Rule) -> str:
    """
    Why the rule's forget_after looks like a slip, or "": one no longer than
    its `before` makes everything the rule offloads due to be forgotten as
    soon as Gmail has deleted it. Pure.
    """
    if rule.forget_ms is None or rule.forget_ms < rule.before_ms:
        return ""
    return (
        f"rule {rule.name!r}: forget_after ({years_text(rule.forget_after)}) is no longer than "
        f"before ({rule.before}), so all it offloads is due to be forgotten once Gmail has "
        "deleted it"
    )


def fold(text: str) -> str:
    """
    Text for subject matching: HTML entities unescaped, mojibake (UTF-8 read
    as Latin-1, e.g. "CafÃ©") repaired where that round-trips, accents
    removed, case folded. "Café", "CAFE", "Caf&eacute;" → "cafe".
    """
    text = html.unescape(text or "")
    try:
        repaired = text.encode("latin-1").decode("utf-8")
    except UnicodeError:
        pass
    else:
        text = repaired
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def _strings(rule: str, key: str, value) -> list[str]:
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list) or not values or not all(isinstance(v, str) for v in values):
        raise RuleError(f"rule {rule!r}: {key} must be a string or a list of strings")
    return [v.strip() for v in values]


def parse_rule(raw: dict, parse_before, resolve_account) -> Rule:
    """
    One [[rule]] table. parse_before(text) -> ms; resolve_account(name) ->
    address, raising RuleError. Raises RuleError on anything unexpected: a typo must not widen
    or narrow an offload silently.
    """
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise RuleError("every rule needs a name")
    unknown = set(raw) - KEYS
    if unknown:
        raise RuleError(f"rule {name!r}: unknown keys {sorted(unknown)}")
    before = raw.get("before")
    if isinstance(before, int | float) and not isinstance(before, bool):
        before = str(before)
    if not isinstance(before, str):
        raise RuleError(f"rule {name!r}: before is required (a date or a number of years)")
    try:
        before_ms = parse_before(before)
    except Exception as exc:
        raise RuleError(f"rule {name!r}: {exc}") from None

    scope = _parse_scope(name, raw, "")
    if not scope:
        raise RuleError(f"rule {name!r}: needs at least one of {', '.join(SCOPES)}")
    exceptions = {}
    if "except" in raw:
        if not isinstance(raw["except"], dict) or not raw["except"]:
            raise RuleError(f"rule {name!r}: except must be a table of scope keys")
        unknown = set(raw["except"]) - set(SCOPES)
        if unknown:
            raise RuleError(f"rule {name!r}: except: unknown keys {sorted(unknown)}")
        exceptions = _parse_scope(name, raw["except"], "except: ")

    waive = _strings(name, "waive", raw["waive"]) if "waive" in raw else []
    extra = _strings(name, "protect", raw["protect"]) if "protect" in raw else []
    for key, values in (("waive", waive), ("protect", extra)):
        bad = sorted(set(values) - set(PROTECTIONS))
        if bad:
            raise RuleError(f"rule {name!r}: {key}: unknown protections {bad}")
    clash = sorted(set(waive) & set(extra))
    if clash:
        raise RuleError(f"rule {name!r}: {clash} both waived and protected")
    protect = tuple(
        p for p in PROTECTIONS if (p in DEFAULT_PROTECT and p not in waive) or p in extra
    )

    forget_after = forget_ms = None
    if "forget_after" in raw:
        forget_after = raw["forget_after"]
        if isinstance(forget_after, bool) or not isinstance(forget_after, int) or forget_after < 1:
            raise RuleError(
                f"rule {name!r}: forget_after must be a whole number of years, 1 or more"
            )
        forget_ms = parse_before(str(forget_after))

    accounts = []
    if "account" in raw:
        accounts = [resolve_account(a) for a in _strings(name, "account", raw["account"])]
    return Rule(
        name.strip(),
        before,
        before_ms,
        accounts,
        scope,
        protect,
        exceptions,
        forget_after,
        forget_ms,
    )


def _parse_scope(name: str, raw: dict, where: str) -> dict[str, list]:
    """The scope keys in raw, validated and normalised: key → values."""
    scope: dict[str, list] = {}
    for key in SCOPES:
        if key not in raw:
            continue
        value = raw[key]
        if key == "larger_than_mb":
            if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
                raise RuleError(f"rule {name!r}: {where}larger_than_mb must be a number")
            scope[key] = [value]
            continue
        if key == "to_me":
            if not isinstance(value, bool):
                raise RuleError(f"rule {name!r}: {where}to_me must be true or false")
            scope[key] = [value]
            continue
        values = _strings(name, where + key, value)
        if key == "category":
            values = [v.lower() for v in values]
            bad = sorted(set(values) - set(CATEGORIES))
            if bad:
                raise RuleError(f"rule {name!r}: {where}unknown categories {bad}; use {CATEGORIES}")
        if key in ("sender", "forwarded_from"):
            values = [v.lower() for v in values]
        if key == "subject":
            values = [fold(v) for v in values]
        if key in ("subject", "query") and not all(values):
            raise RuleError(f"rule {name!r}: {where}{key} must not be empty")
        scope[key] = values
    return scope


def load_rules(path: Path, parse_before, resolve_account) -> list[Rule]:
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        raise RuleError(f"no rules file at {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise RuleError(f"{path}: {exc}") from None
    unknown = set(data) - {"rule"}
    if unknown:
        raise RuleError(f"{path}: unknown tables {sorted(unknown)}; rules go in [[rule]]")
    raws = data.get("rule", [])
    if not isinstance(raws, list) or not raws:
        raise RuleError(f"{path} has no [[rule]] tables")
    rules = [parse_rule(raw, parse_before, resolve_account) for raw in raws]
    names = [r.name for r in rules]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise RuleError(f"{path}: rule names must be unique: {dupes}")
    return rules
