"""
Offload rules, read from a TOML file: one [[rule]] table per collection of
mail to take out of Gmail.

    [[rule]]
    name = "old work mail"             # unique; recorded with every message
    before = "2022-01-01"              # required: a date, or a number of years ago ("5")
    after = "2012-01-01"               # optional: and no older than this, written like before
    account = "work"                   # optional: address, config name or unique part
    label = "projects"                 # scope: at least one of the keys below, or
                                       # scope = "all" for every message before the cut-off
    keep = ["mine", "starred"]         # optional: the protections that apply (see below)
    keep_if = [{ subject = "contract" }, { sender = "@a.example", larger_than_mb = 1 }]
                                       # optional: keep what matches any one table
    forget_after = 7                   # optional: years from a message's date after which
                                       # mailcellar forget --due deletes what this rule
                                       # offloaded from the backup, once Gmail has deleted it

Scope keys take a string or a list (any value matches); different keys
must all match. A rule with none is refused unless it says scope = "all",
which selects every message from after to before, and takes no scope key:

    label           user label name (case ignored)
    sender          sender address, or "@domain" (relayed mail: the original sender)
    list            List-Id
    forwarded_from  original recipient of auto-forwarded mail, or "@domain"
    category        Gmail category: promotions, updates, forums, social, personal
    subject         part of the subject, ignoring case and accents (see fold)
    to_me           true: one of your addresses is in To or Cc; false: it isn't
                    (the mail came by a list, Bcc or an alias)
    to              an address or "@domain" in To, Cc or Bcc
    bulk            true: sent in bulk (List-Id, List-Unsubscribe, Precedence bulk,
                    list or junk, or a Gmail category tab), as report bulk judges it;
                    false: not
    attachment      has an attachment (a part that isn't inline, or has a file name)
                    of a kind: any, image, pdf, video, audio, office, archive
    larger_than_mb  size in MB (a number)
    query           a Gmail search, as typed in Gmail ("has:attachment filename:pdf");
                    offload plan asks Gmail, read-only, which messages it finds

`keep_if` takes a table of the same keys, or a list of them: a message
matching every key of any one table is kept.

`keep` names the protections that keep a message in Gmail although the
rule selects it; left out, all but correspondents apply. keep = [] keeps
nothing the rule selects:

    mine            your mail: sent, drafts, or from one of your addresses
    starred
    conversations   in a thread you wrote in (same account), or a message you
                    replied to (from either account)
    labelled        has a user label the rule doesn't name
    correspondents  from an address you have written to

Until 1.0 a rule may instead say waive (default protections to drop) and
protect (protections to add), with the internal names below, and except
(a table whose keys each keep on their own). offload plan warns, with the
rule in the new keys. Internally a rule's protections are these flags:

    sent, starred, thread, replied, labels, correspondents
"""

import html
import tomllib
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

# Internal flags, one per protection; KEEP groups them under the names rules use
PROTECTIONS = ("sent", "starred", "thread", "replied", "labels", "correspondents")
DEFAULT_PROTECT = ("sent", "starred", "thread", "replied", "labels")
# keep name → the protections it stands for
KEEP = {
    "mine": ("sent",),
    "starred": ("starred",),
    "conversations": ("thread", "replied"),
    "labelled": ("labels",),
    "correspondents": ("correspondents",),
}
# why a message was kept, as the plan and review pages say it: its keep_if, or a keep name
KEPT_WHY = {
    "keep_if": "keep_if",
    "mine": "yours",
    "starred": "starred",
    "conversations": "a conversation you were in",
    "labelled": "labelled",
    "correspondents": "from someone you wrote to",
}
DEFAULT_KEEP = ("mine", "starred", "conversations", "labelled")
KEEP_OF = {p: k for k, ps in KEEP.items() for p in ps}  # protection → its keep name
OLD_KEYS = ("waive", "protect", "except")  # until 1.0
SCOPES = (
    "label",
    "sender",
    "list",
    "forwarded_from",
    "category",
    "subject",
    "to_me",
    "to",
    "bulk",
    "attachment",
    "larger_than_mb",
    "query",
)
CATEGORIES = ("promotions", "updates", "forums", "social", "personal")
# attachment kind → (MIME type prefixes, file extensions for a part typed only
# application/octet-stream or not at all); "any" is every attachment
ATTACHMENT_KINDS = {
    "any": ((), ()),
    "image": (("image/",), tuple("jpg jpeg png gif heic heif tif tiff bmp webp".split())),
    "pdf": (("application/pdf",), ("pdf",)),
    "video": (("video/",), tuple("mp4 mov m4v avi mkv wmv webm 3gp mpg mpeg".split())),
    "audio": (("audio/",), tuple("mp3 m4a wav aac flac ogg wma aiff amr".split())),
    "office": (
        (
            "application/msword",
            "application/msexcel",
            "application/mspowerpoint",
            "application/vnd.ms-",
            "application/vnd.openxmlformats-officedocument.",
            "application/vnd.oasis.opendocument.",
            "application/vnd.apple.pages",
            "application/vnd.apple.numbers",
            "application/vnd.apple.keynote",
            "application/rtf",
            "text/csv",
        ),
        tuple("doc docx xls xlsx ppt pptx pps ppsx vsd vsdx odt ods odp rtf csv".split())
        + ("pages", "numbers", "key"),
    ),
    "archive": (
        (
            "application/zip",
            "application/x-zip",
            "application/gzip",
            "application/x-gzip",
            "application/x-tar",
            "application/rar",
            "application/x-rar",
            "application/vnd.rar",
            "application/x-7z-compressed",
            "application/x-bzip2",
        ),
        tuple("zip gz tgz tar rar 7z bz2".split()),
    ),
}
UNTYPED = ("", "application/octet-stream")  # MIME types that say nothing: go by the extension
KEYS = {
    "name",
    "before",
    "after",
    "account",
    "scope",
    "keep",
    "keep_if",
    "forget_after",
    *OLD_KEYS,
    *SCOPES,
}


class RuleError(Exception):
    pass


@dataclass(frozen=True)
class Rule:
    name: str
    before: str  # as written
    before_ms: int  # ms since epoch, UTC
    accounts: list[str] = field(default_factory=list)  # [] means every account
    # scope key → values; {} only for scope = "all": every message from after to before
    scope: dict[str, list] = field(default_factory=dict)
    protect: tuple[str, ...] = DEFAULT_PROTECT
    # keep_if: scope tables; a message matching every key of any one is kept
    exceptions: tuple[dict[str, list], ...] = ()
    forget_after: int | None = None  # years, as written; None: kept for ever
    forget_ms: int | None = None  # its mail is due for forgetting when older than this
    deprecated: str = ""  # a warning: the rule uses waive, protect or except
    after: str = ""  # as written; "": no lower bound
    after_ms: int | None = None  # ms since epoch, UTC: nothing older is selected

    @property
    def waived(self) -> list[str]:
        return [p for p in DEFAULT_PROTECT if p not in self.protect]

    @property
    def keep(self) -> list[str]:
        """The keep names that apply, in KEEP order, a partial one marked as such."""
        out = []
        for name, ps in KEEP.items():
            have = [p for p in ps if p in self.protect]
            if have == list(ps):
                out.append(name)
            elif have:
                out.append(f"{name} ({have[0]} only)")
        return out

    @property
    def queries(self) -> list[str]:
        """The Gmail searches the rule's scope and keep_if name."""
        return [
            *self.scope.get("query", []),
            *(q for table in self.exceptions for q in table.get("query", [])),
        ]

    @property
    def labels(self) -> set[str]:
        """The user labels the rule names, lower case."""
        return {v.lower() for v in self.scope.get("label", [])}


def attachment_is(mime_type: str, filename: str, kind: str) -> bool:
    """Whether a part is an attachment of a kind (ATTACHMENT_KINDS). Pure."""
    if kind == "any":
        return True
    prefixes, extensions = ATTACHMENT_KINDS[kind]
    mime_type = (mime_type or "").lower()
    if mime_type.startswith(prefixes):
        return True
    extension = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    return mime_type in UNTYPED and extension in extensions


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
        f"before ({rule.before}), so all it offloads is due to be forgotten once its mailbox "
        "has deleted it"
    )


def keeps_nothing(rule: Rule) -> str:
    """
    Why the rule selects everything it covers, or "": scope = "all" with
    keep = [] and no keep_if takes every message before its cut-off, your
    own and starred mail included. Pure.
    """
    if rule.scope or rule.protect or rule.exceptions:
        return ""
    where = ", ".join(rule.accounts) if rule.accounts else "every account"
    return (
        f'rule {rule.name!r}: scope = "all" with keep = [] selects every message before '
        f"{rule.before} in {where}, your own and starred mail included"
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
    after, after_ms = raw.get("after", ""), None
    if "after" in raw:
        if isinstance(after, int | float) and not isinstance(after, bool):
            after = str(after)
        bad = f"rule {name!r}: after must be a date (YYYY-MM-DD) or a number of years"
        if not isinstance(after, str) or not after.strip():
            raise RuleError(bad)
        try:
            after_ms = parse_before(after.strip())
        except Exception:
            raise RuleError(bad) from None
        if after_ms >= before_ms:
            raise RuleError(
                f"rule {name!r}: after ({after}) must be earlier than before ({before})"
            )

    scope = _parse_scope(name, raw, "")
    all_mail = "scope" in raw
    if all_mail and raw["scope"] != "all":
        raise RuleError(
            f'rule {name!r}: scope can only be "all", for every message before {before}'
        )
    if all_mail and scope:
        raise RuleError(
            f'rule {name!r}: scope = "all" selects every message, so it takes no '
            f"{', '.join(scope)}; drop one or the other"
        )
    if not all_mail and not scope:
        raise RuleError(
            f"rule {name!r}: needs at least one of {', '.join(SCOPES)}, "
            'or scope = "all" for every message before the cut-off'
        )
    old = [k for k in OLD_KEYS if k in raw]
    if old and ("keep" in raw or "keep_if" in raw):
        raise RuleError(
            f"rule {name!r}: {' and '.join(old)} can't be mixed with keep and keep_if; "
            "write the rule with keep and keep_if only"
        )
    if old:
        protect, exceptions = _old_keys(name, raw)
        deprecated = _translation(name, raw, protect)
    else:
        protect = _parse_keep(name, raw["keep"]) if "keep" in raw else DEFAULT_PROTECT
        exceptions = _parse_keep_if(name, raw["keep_if"]) if "keep_if" in raw else ()
        deprecated = ""

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
        name=name.strip(),
        before=before,
        before_ms=before_ms,
        accounts=accounts,
        scope=scope,
        protect=protect,
        exceptions=exceptions,
        forget_after=forget_after,
        forget_ms=forget_ms,
        deprecated=deprecated,
        after=after,
        after_ms=after_ms,
    )


def _parse_keep(name: str, value) -> tuple[str, ...]:
    """keep = [...] → the protections it applies, in PROTECTIONS order. [] keeps nothing."""
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        raise RuleError(f"rule {name!r}: keep must be a list of {', '.join(KEEP)}")
    for v in values:
        if v.strip() in KEEP_OF and v.strip() not in KEEP:
            raise RuleError(
                f"rule {name!r}: keep: {v.strip()!r} is called {KEEP_OF[v.strip()]!r} in keep"
            )
    bad = sorted({v.strip() for v in values} - set(KEEP))
    if bad:
        raise RuleError(f"rule {name!r}: keep: unknown names {bad}; use {', '.join(KEEP)}")
    chosen = {p for v in values for p in KEEP[v.strip()]}
    return tuple(p for p in PROTECTIONS if p in chosen)


def _parse_keep_if(name: str, value) -> tuple[dict[str, list], ...]:
    """keep_if = a table or a list of tables of scope keys, each validated as a scope."""
    tables = [value] if isinstance(value, dict) else value
    if not isinstance(tables, list) or not tables or not all(isinstance(t, dict) for t in tables):
        raise RuleError(f"rule {name!r}: keep_if must be a table or a list of tables")
    out = []
    for table in tables:
        unknown = set(table) - set(SCOPES)
        if not table or unknown:
            raise RuleError(
                f"rule {name!r}: keep_if: each table needs scope keys"
                + (f"; unknown keys {sorted(unknown)}" if unknown else "")
            )
        out.append(_parse_scope(name, table, "keep_if: "))
    return tuple(out)


def _old_keys(name: str, raw: dict) -> tuple[tuple[str, ...], tuple[dict[str, list], ...]]:
    """waive, protect and except (until 1.0) → (protections, keep_if tables)."""
    exceptions: tuple[dict[str, list], ...] = ()
    if "except" in raw:
        if not isinstance(raw["except"], dict) or not raw["except"]:
            raise RuleError(f"rule {name!r}: except must be a table of scope keys")
        unknown = set(raw["except"]) - set(SCOPES)
        if unknown:
            raise RuleError(f"rule {name!r}: except: unknown keys {sorted(unknown)}")
        # except's keys each keep on their own: one keep_if table per key
        parsed = _parse_scope(name, raw["except"], "except: ")
        exceptions = tuple({key: values} for key, values in parsed.items())

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
    return protect, exceptions


def toml_value(value) -> str:
    """A TOML value as a rule would write it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(map(toml_value, value)) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{k} = {toml_value(v)}" for k, v in value.items()) + " }"
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _translation(name: str, raw: dict, protect: tuple[str, ...]) -> str:
    """The deprecation warning for a rule with waive, protect or except: its new keys. Pure."""
    used = " and ".join(k for k in OLD_KEYS if k in raw)
    lines = []
    if "waive" in raw or "protect" in raw:
        names = [k for k, ps in KEEP.items() if all(p in protect for p in ps)]
        if names != list(DEFAULT_KEEP):
            lines.append("keep = " + toml_value(names))
    if "except" in raw and isinstance(raw["except"], dict):
        lines.append("keep_if = " + toml_value([{k: v} for k, v in raw["except"].items()]))
    text = f"rule {name!r}: {used} will be removed in 1.0; "
    if ("thread" in protect) != ("replied" in protect):
        return text + (
            "keep can't say this rule's protections, as it keeps one of thread and replied "
            "without the other ('conversations' is both)"
            + (f"; its except is: {lines[-1]}" if "except" in raw else "")
        )
    return text + "write it as: " + ("; ".join(lines) or "nothing; keep's default is the same")


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
        if key in ("to_me", "bulk"):
            if not isinstance(value, bool):
                raise RuleError(f"rule {name!r}: {where}{key} must be true or false")
            scope[key] = [value]
            continue
        values = _strings(name, where + key, value)
        if key == "category":
            values = [v.lower() for v in values]
            bad = sorted(set(values) - set(CATEGORIES))
            if bad:
                raise RuleError(f"rule {name!r}: {where}unknown categories {bad}; use {CATEGORIES}")
        if key == "attachment":
            values = [v.lower() for v in values]
            bad = sorted(set(values) - set(ATTACHMENT_KINDS))
            if bad:
                raise RuleError(
                    f"rule {name!r}: {where}unknown attachment kinds {bad}; "
                    f"use {', '.join(ATTACHMENT_KINDS)}"
                )
        if key in ("sender", "forwarded_from", "to"):
            values = [v.lower() for v in values]
        if key == "subject":
            values = [fold(v) for v in values]
        if key in ("subject", "query") and not all(values):
            raise RuleError(f"rule {name!r}: {where}{key} must not be empty")
        scope[key] = values
    return scope


TRY_NAME = "try"  # a --try rule's name when it gives none


def read_try(text: str) -> dict:
    """
    offload plan --try's RULE, the inside of a TOML inline table
    ('bulk = true, before = "2017-01-01"'), as a [[rule]] table: name
    defaults to "try". Pure; raises RuleError.
    """
    try:
        raw = tomllib.loads(f"r = {{ {text} }}")["r"]
    except tomllib.TOMLDecodeError as exc:
        raise RuleError(
            f"--try: {exc}; write the rule as in a [[rule]] table, keys "
            "separated by commas: 'bulk = true, before = \"5\"'"
        ) from None
    return {"name": TRY_NAME, **raw}


def rule_toml(raw: dict) -> str:
    """A [[rule]] table, ready to paste into rules.toml. Pure."""
    return "[[rule]]\n" + "".join(f"{key} = {toml_value(value)}\n" for key, value in raw.items())


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
