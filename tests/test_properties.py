"""
Rules that should hold for every input, checked against inputs Hypothesis
generates (#36): mail and queries are written by other people, and the odd
cases are the ones nobody thinks to write down. A failure found here also
gets an ordinary example test, next to the fix.
"""

import io
import sqlite3

from hypothesis import example, given
from hypothesis import strategies as st

from mailcellar.message import parse_message
from mailcellar.restore import mbox
from mailcellar.restore.export import mbox_entry
from mailcellar.search import db
from mailcellar.search.query import QueryError, fts_query

# ── 1. an mbox written by export reads back as the same messages ─────────────

NEWLINES = st.sampled_from([b"\n", b"\r\n"])
# Line starts that mboxrd has to escape, or nearly so.
TRICKY_STARTS = st.sampled_from(
    [b"", b"From ", b">From ", b">>From ", b"From", b" From ", b">", b"Fro", b"from "]
)
# Lines without line breaks of their own (a lone \r would end a line for some readers).
LINE_TEXT = st.binary(max_size=40).map(lambda b: b.replace(b"\r", b"").replace(b"\n", b""))
HEADER_NAME = st.from_regex(rb"\A[A-Za-z][A-Za-z0-9-]{0,15}\Z").filter(
    lambda n: n.lower() not in (b"x-gmail-labels", b"x-gm-thrid", b"from")  # Takeout's own
)


@st.composite
def messages(draw) -> bytes:
    """A message: header lines, a blank line, body lines, each line ending as drawn."""
    newline = draw(NEWLINES)
    headers = [
        draw(HEADER_NAME) + b": " + draw(LINE_TEXT) + (newline if draw(st.booleans()) else b"\n")
        for _ in range(draw(st.integers(1, 4)))
    ]
    body = [draw(TRICKY_STARTS) + draw(LINE_TEXT) for _ in range(draw(st.integers(0, 6)))]
    endings = [draw(NEWLINES) for _ in body]
    raw = (
        b"".join(headers)
        + newline
        + b"".join(line + end for line, end in zip(body, endings, strict=True))
    )
    if body and draw(st.booleans()):
        raw = raw[: -len(endings[-1])]  # no line ending after the last line
    return raw


def written_and_read_back(raws: list[bytes]) -> list[bytes]:
    out = b"".join(mbox_entry(raw, 1_700_000_000_000, ("INBOX", "work")) for raw in raws)
    return [mbox.message(out[start:end]) for start, end in mbox.entries(io.BytesIO(out))]


@given(st.lists(messages(), min_size=1, max_size=3))
def test_an_exported_mbox_reads_back_as_the_same_messages(raws):
    expected = [raw if raw.endswith(b"\n") else raw + mbox_newline(raw) for raw in raws]
    assert written_and_read_back(raws) == expected


def mbox_newline(raw: bytes) -> bytes:
    """What export ends a message with when it has no final line ending."""
    return b"\r\n" if b"\r\n" in raw[:4096] else b"\n"


# ── 2. parsing never stops on a message ──────────────────────────────────────

ENCODED_WORDS = st.sampled_from(
    [b"=?utf-8?b?w6k=?=", b"=?utf-8?q?caf=C3=A9?=", b"=?bogus?x?zz?=", b"=?utf-8?b?###?=", b"=?"]
)


@st.composite
def near_valid_messages(draw) -> bytes:
    """Messages that look right at first: odd encodings, broken MIME, missing parts."""
    boundary = draw(st.sampled_from([b"b1", b"", b'"b1', b"b1\x00"]))
    used = draw(st.sampled_from([boundary, b"other", b""]))
    headers = [
        b"From: " + draw(ENCODED_WORDS) + b" <" + draw(LINE_TEXT) + b">",
        b"Subject: " + draw(ENCODED_WORDS) + draw(LINE_TEXT),
        b"Date: " + draw(st.sampled_from([b"Mon, 1 Jan 2024 10:00:00 +0000", b"yesterday", b""])),
        b"Content-Type: multipart/"
        + draw(st.sampled_from([b"mixed", b"x"]))
        + b"; boundary="
        + boundary,
        b"Content-Transfer-Encoding: "
        + draw(st.sampled_from([b"base64", b"quoted-printable", b"7bit", b"x"])),
    ]
    part = (
        b"--"
        + used
        + b"\nContent-Type: "
        + draw(st.sampled_from([b"text/plain", b"text/html", b"application/x"]))
        + b'; charset="'
        + draw(st.sampled_from([b"utf-8", b"bogus", b""]))
        + b'"\n'
        + b'Content-Disposition: attachment; filename="'
        + draw(LINE_TEXT)
        + b'"\n\n'
        + draw(st.binary(max_size=200))
    )
    return b"\n".join(draw(st.permutations(headers))) + b"\n\n" + part + b"\n--" + used + b"--"


@given(st.one_of(st.binary(max_size=2_000), near_valid_messages()))
def test_parse_message_never_raises(raw):
    fields = parse_message(raw)
    assert isinstance(fields, dict)


# ── 3. any query either is refused or is one FTS5 accepts ────────────────────

QUERY_PIECES = st.sampled_from(
    ['"', "*", "-", "OR", "AND", "NOT", "from:", "to:", "subject:", "(", ")", ":", "^", "NEAR"]
)
QUERIES = st.lists(st.one_of(QUERY_PIECES, st.text(max_size=8)), max_size=8).map(" ".join)

_index = sqlite3.connect(":memory:")
_index.executescript(db.SCHEMA)


@given(st.one_of(QUERIES, st.text()))
@example('"unclosed')
@example("-only")
@example("from:")
def test_a_query_is_refused_or_accepted_by_fts5(query):
    try:
        expression = fts_query(query)
    except QueryError:
        return
    _index.execute("SELECT count(*) FROM text WHERE text MATCH ?", (expression,)).fetchone()
