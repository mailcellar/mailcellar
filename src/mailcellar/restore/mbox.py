"""
mbox: reading the messages of an mbox file, such as a Google Takeout export
or one written by `mailcellar export`.

Messages are found by their `From ` separator lines (at the start of the
file, or after a blank line), read one at a time by their place in the
file, and given back as the message itself: without the separator, the
blank line the mbox adds after each message, mboxrd's `>` before lines
starting `From `, and the headers Takeout adds (X-Gmail-Labels, X-GM-THRID),
which were never part of the message. Their labels come from X-Gmail-Labels.

Everything but entries() is pure.
"""

import csv
import re
from email.parser import BytesHeaderParser

from mailcellar.message import decode_header_value
from mailcellar.restore import takeout_labels

SEPARATOR = b"From "
ADDED_HEADERS = ("x-gmail-labels", "x-gm-thrid")

# Takeout's names for Gmail's system labels, by their name in lower case, as
# the label IDs gyb stores (and restore imports). `mailcellar export` writes
# the IDs themselves, which map to themselves.
SYSTEM_LABELS = {
    "inbox": "INBOX",
    "sent": "SENT",
    "starred": "STARRED",
    "important": "IMPORTANT",
    "unread": "UNREAD",
    "draft": "DRAFT",
    "drafts": "DRAFT",
    "spam": "SPAM",
    "trash": "TRASH",
    "chat": "CHAT",
    "chats": "CHAT",
}
# Takeout's own markers, not labels in Gmail.
NOT_LABELS = {"opened", "archived"}


def entries(fh) -> list[tuple[int, int]]:
    """(start, end) of each message in an open binary mbox file, its separator line excluded."""
    spans = []
    start = None
    offset = 0
    blank_before = True
    for line in iter(fh.readline, b""):
        if line.startswith(SEPARATOR) and blank_before:
            if start is not None:
                spans.append((start, offset))
            start = offset + len(line)
        blank_before = line in (b"\n", b"\r\n")
        offset += len(line)
    if start is not None:
        spans.append((start, offset))
    return spans


_ESCAPED = re.compile(rb"^>(>*From )", re.M)


def unescape(entry: bytes) -> bytes:
    """Undo mboxrd's escaping: one `>` comes off each line that starts with `>`s and `From `."""
    return _ESCAPED.sub(rb"\1", entry)


def _header_end(raw: bytes) -> int:
    ends = [i for i in (raw.find(b"\r\n\r\n"), raw.find(b"\n\n")) if i != -1]
    return min(ends) if ends else len(raw)


def without_added_headers(raw: bytes) -> bytes:
    """The message without the headers Takeout adds (and their continuation lines)."""
    end = _header_end(raw)
    lines = raw[:end].splitlines(keepends=True)
    kept, dropping = [], False
    for line in lines:
        if line[:1] in (b" ", b"\t"):  # continues the header above
            if not dropping:
                kept.append(line)
            continue
        name = line.split(b":", 1)[0].strip().lower()
        dropping = name.decode("ascii", "replace") in ADDED_HEADERS
        if not dropping:
            kept.append(line)
    if len(kept) == len(lines):
        return raw
    if lines and not lines[-1].endswith(b"\n") and kept and kept[-1].endswith(b"\n"):
        kept[-1] = kept[-1].rstrip(b"\r\n")  # the last header line had no line ending of its own
    return b"".join(kept) + raw[end:]


def message(entry: bytes) -> bytes:
    """One message as Gmail should get it, from its entry in the mbox."""
    if entry.endswith(b"\r\n\r\n"):
        entry = entry[:-2]
    elif entry.endswith(b"\n\n"):
        entry = entry[:-1]
    return without_added_headers(unescape(entry))


def label_names(names) -> tuple[str, ...]:
    """Label names as Takeout or `export` wrote them, as gyb stores labels."""
    out = []
    for name in names:
        key = " ".join(name.split()).lower()
        if not key or key in NOT_LABELS:
            continue
        if key in SYSTEM_LABELS:
            label = SYSTEM_LABELS[key]
        elif (upper := " ".join(name.split()).upper()) in takeout_labels.NAMES:
            label = takeout_labels.NAMES[upper]  # Takeout in another language
        elif re.fullmatch(r"category[ _]\w+", key):
            label = "CATEGORY_" + key.split(None, 1)[-1].split("_", 1)[-1].upper()
        else:
            label = name.strip()
        if label not in out:
            out.append(label)
    return tuple(out)


def labels(entry: bytes) -> tuple[str, ...]:
    """The labels in an entry's X-Gmail-Labels header; () if it has none."""
    value = BytesHeaderParser().parsebytes(entry[: _header_end(entry)]).get("X-Gmail-Labels")
    if value is None:
        return ()
    text = decode_header_value(str(value))
    return label_names(next(csv.reader([text], skipinitialspace=True), []))
