"""
Message parsing: the fields dedup, the sidecar and the reports work from,
read from a message's raw bytes (.eml content). parse_message() is the
entry point; bump PARSER_VERSION whenever its output changes.
"""

import email.header
import email.parser
import email.policy
import email.utils
import hashlib
import re
from pathlib import Path

# ── headers ──────────────────────────────────────────────────────────────────


def decode_header_value(raw) -> str:
    """Decode RFC-2047 encoded header values (e.g. =?utf-8?b?...?=)."""
    if not raw:
        return ""
    parts = email.header.decode_header(str(raw))
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            try:
                decoded.append(part.decode(charset or "utf-8", errors="replace"))
            except LookupError, UnicodeDecodeError:
                # Pseudo-charsets like 'unknown-8bit' are not real Python codecs;
                # latin-1 is a lossless 8-bit fallback.
                decoded.append(part.decode("latin-1", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded).strip()


_ADDR_RE = re.compile(r"[\w.+\-]+@[\w.\-]+\.[a-zA-Z]{2,}")
_FOLD_RE = re.compile(r"\r?\n(?=[ \t])")


def unfold(raw) -> str:
    """
    RFC 5322 unfolding: drop the line breaks of a folded header. getaddresses
    can't parse a break inside a quoted display name (it returns the encoded
    name, or nothing, instead of the address).
    """
    return _FOLD_RE.sub("", str(raw or "")).replace("\r", "").replace("\n", "")


def extract_address(raw) -> str:
    """
    The first email address in a raw (undecoded) address header, lower-cased.
    Parsing before decoding matters: a decoded display name like "Doe, John"
    would split the header. Falls back to a regex for malformed headers that
    getaddresses can't parse, then to the header itself; "" when empty.
    """
    raw = unfold(raw).strip()
    if not raw:
        return ""
    for _name, addr in email.utils.getaddresses([raw]):
        if addr:
            return addr.strip().lower()
    m = _ADDR_RE.search(raw)
    return m.group(0).lower() if m else raw.lower()


def display_name(raw) -> str:
    """Decoded display name of the first address in a raw address header."""
    addrs = email.utils.getaddresses([unfold(raw)])
    return " ".join(decode_header_value(addrs[0][0]).split()) if addrs else ""


_MSGID_RE = re.compile(r"<([^<>]+)>")


def normalise_message_id(raw) -> str:
    """
    Return the bare Message-ID, lower-cased, or "" if there is none.
    Takes the first <...> token, so trailing comments or folding are ignored.
    """
    if not raw:
        return ""
    raw = str(raw)
    m = _MSGID_RE.search(raw)
    mid = m.group(1) if m else raw
    return "".join(mid.split()).lower()


_MBOX_FROM_RE = re.compile(rb"^>*From ")


def strip_mbox_from_lines(raw: bytes) -> bytes:
    """
    Drop mbox separator lines ('From ...' / '>From ...') from the header block.
    Old messages imported from mbox files (e.g. Thunderbird exports) can carry
    one at the top or between headers; it is not a valid header, so the email
    parser stops there and treats every later header as body.
    """
    end = raw.find(b"\n\n")
    end_crlf = raw.find(b"\r\n\r\n")
    if end_crlf != -1 and (end == -1 or end_crlf < end):
        end = end_crlf
    head, rest = (raw, b"") if end == -1 else (raw[:end], raw[end:])
    lines = head.split(b"\n")
    kept = [line for line in lines if not _MBOX_FROM_RE.match(line)]
    if len(kept) == len(lines):
        return raw
    return b"\n".join(kept) + rest


# ── body ─────────────────────────────────────────────────────────────────────


def body_text(msg) -> str:
    """
    Concatenated decoded text/plain and text/html parts, excluding
    attachments, with whitespace collapsed. "" if the message has no text.
    """
    chunks = []
    for part in msg.walk():
        if part.is_multipart() or part.get_content_maintype() != "text":
            continue
        if part.get_content_subtype() not in ("plain", "html"):
            continue
        if part.get_content_disposition() == "attachment":
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            chunks.append(payload.decode(charset, errors="replace"))
        except LookupError:
            chunks.append(payload.decode("latin-1", errors="replace"))
    return " ".join(" ".join(chunks).split())


def body_hash(msg) -> str:
    """SHA-256 of body_text(), or "" when the message has no text body."""
    text = body_text(msg)
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# Links and addresses differ between re-sends of the same mail (tracking
# URLs, per-recipient unsubscribe links), so visible_text() drops them.
_LINK_RE = re.compile(r"""(https?://\S+|(?:href|src)=["'][^"']*["']|[\w.+-]+@[\w-]+\.[\w.]+)""")
_TAG_RE = re.compile(r"<[^>]+>")


def visible_text(msg) -> str:
    """body_text() without HTML tags, URLs and email addresses, whitespace collapsed."""
    return " ".join(_TAG_RE.sub(" ", _LINK_RE.sub(" ", body_text(msg))).split())


def attachments(msg) -> list[dict]:
    """
    Every part that isn't body text (attachments, inline images, parts of
    forwarded messages), in message order: idx, filename (decoded; "" if
    none), mime_type, size (decoded bytes), sha256, inline (True unless the
    part says Content-Disposition: attachment).
    """
    out = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        disposition = part.get_content_disposition()
        if part.get_content_type() in ("text/plain", "text/html") and disposition != "attachment":
            continue
        payload = part.get_payload(decode=True) or b""
        out.append(
            {
                "idx": len(out),
                "filename": " ".join(decode_header_value(part.get_filename() or "").split()),
                "mime_type": part.get_content_type(),
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "inline": disposition != "attachment",
            }
        )
    return out


def attachment_hashes(msg) -> list[str]:
    """SHA-256 of every part that isn't body text (attachments, inline images), sorted."""
    return sorted(a["sha256"] for a in attachments(msg))


def content_hash(msg, attachment_list: list[dict] | None = None) -> str:
    """
    SHA-256 of what a reader sees: visible_text() plus every attachment's
    hash. Re-sends that differ only in tracking links match; messages with
    different text or attachments don't. "" when a message has neither text
    nor attachments, so such messages never match. Identical text is common
    across separate mail, so dedup only uses this together with the
    Message-ID or header_key().
    """
    text = visible_text(msg)
    if attachment_list is None:
        attachment_list = attachments(msg)
    hashes = sorted(a["sha256"] for a in attachment_list)
    if not text and not hashes:
        return ""
    h = hashlib.sha256(text.encode("utf-8"))
    for a in hashes:
        h.update(b"\0" + a.encode())
    return h.hexdigest()


RECIPIENT_HEADERS = (("to", "To"), ("cc", "Cc"), ("bcc", "Bcc"))


def recipients(msg) -> list[dict]:
    """
    To / Cc / Bcc addresses (Bcc survives only on sent mail and drafts), in
    header order: kind, address (lower-cased), name (decoded). Headers are
    unfolded before parsing; entries without an address (e.g. group syntax
    like "undisclosed-recipients:;") are skipped, and repeats within a kind
    are kept once.
    """
    out, seen = [], set()
    for kind, header in RECIPIENT_HEADERS:
        values = [unfold(v) for v in msg.get_all(header) or []]
        for name, addr in email.utils.getaddresses(values):
            addr = addr.strip().lower()
            if "@" not in addr or (kind, addr) in seen:
                continue
            seen.add((kind, addr))
            out.append(
                {"kind": kind, "address": addr, "name": " ".join(decode_header_value(name).split())}
            )
    return out


def list_id(msg) -> str:
    """The List-Id's identifier (the part in <>), lower-cased; "" if none."""
    raw = unfold(msg.get("List-Id", "")).strip()
    m = re.search(r"<([^<>]+)>", raw)
    return (m.group(1) if m else raw).strip().lower()


def list_unsubscribe(msg) -> str:
    """How List-Unsubscribe offers to unsubscribe: 'mailto', 'https', 'both' or ''."""
    raw = unfold(msg.get("List-Unsubscribe", "")).lower()
    kinds = [k for k, marker in (("mailto", "<mailto:"), ("https", "<http")) if marker in raw]
    return "both" if len(kinds) == 2 else (kinds[0] if kinds else "")


def header_key(msg) -> str:
    """
    From, Date and Subject as written (decoded, whitespace collapsed,
    lower-cased), one per line: identical for re-deliveries of the same
    mail even when their Message-IDs differ. "" when From or Date is missing.
    """
    values = [
        " ".join(decode_header_value(str(msg.get(h, ""))).split()).lower()
        for h in ("From", "Date", "Subject")
    ]
    if not values[0] or not values[1]:
        return ""
    return "\n".join(values)


# ── message fields ───────────────────────────────────────────────────────────

# Bump when parse_message() output changes; mailcellar dedup index and
# mailcellar meta then re-parse every message.
PARSER_VERSION = "7"


def parse_message(raw: bytes) -> dict:
    """
    The fields dedup and the reports work from, for one message's raw bytes
    (.eml content). Scalars, plus "attachments" and "recipients" lists.
    """
    raw = strip_mbox_from_lines(raw)
    msg = email.parser.BytesParser(policy=email.policy.compat32).parsebytes(raw)

    from_raw = str(msg.get("From", ""))
    attachment_list = attachments(msg)

    return {
        "message_id": normalise_message_id(msg.get("Message-ID", "")),
        "sender": decode_header_value(from_raw),
        "sender_addr": extract_address(from_raw),
        "sender_name": display_name(from_raw),
        "reply_to": extract_address(msg.get("Reply-To", "")),
        "subject": decode_header_value(msg.get("Subject", "")),
        "body_hash": body_hash(msg),
        "forwarded_from": forwarded_from(msg),
        "content_hash": content_hash(msg, attachment_list),
        "header_key": header_key(msg),
        "list_id": list_id(msg),
        "list_unsubscribe": list_unsubscribe(msg),
        "precedence": unfold(msg.get("Precedence", "")).strip().lower(),
        "in_reply_to": normalise_message_id(msg.get("In-Reply-To", "")),
        # child rows, stored in meta.sqlite's attachments / recipients tables
        "attachments": attachment_list,
        "recipients": recipients(msg),
    }


def parse_eml(path: Path) -> dict:
    """parse_message() for an .eml file. Raises on unreadable files."""
    return parse_message(path.read_bytes())


def forwarded_from(msg) -> str:
    """
    Original recipients of an auto-forwarded message, space-separated.
    Gmail adds 'X-Forwarded-For: <original recipient> <forward target>' when
    it forwards a message; the first address of each such header is taken.
    "" when the message was not auto-forwarded.
    """
    found = []
    for value in msg.get_all("X-Forwarded-For") or []:
        tokens = str(value).split()
        if tokens:
            addr = tokens[0].strip("<>,;").lower()
            if "@" in addr and addr not in found:
                found.append(addr)
    return " ".join(found)
