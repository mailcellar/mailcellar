"""docs/ renders the same on the site (Python-Markdown) and on GitHub (GFM).

Each check is a construct the two read differently, which the site's strict
build doesn't catch: it fails only on a broken link or anchor.
"""

import re
from pathlib import Path

import pytest

DOCS = Path(__file__).parent.parent / "docs"
PAGES = sorted(p for p in DOCS.rglob("*.md") if "internal" not in p.relative_to(DOCS).parts)

LIST_ITEM = re.compile(r"(?:[-*]|\d+\.) ")
NESTED_ITEM = re.compile(r" {1,3}(?:[-*]|\d+\.) ")
PIPE_IN_CODE = re.compile(r"`[^`]*\\\|[^`]*`")
ATTR_LIST = re.compile(r"\{:")


def problems(text: str) -> list[tuple[int, str]]:
    """Each line, numbered from 1, that one of the two renders differently."""
    found = []
    fenced = False
    previous = ""
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            previous = line
            continue
        if fenced:
            continue
        if LIST_ITEM.match(line) and is_paragraph(previous):
            found.append((number, "a list needs a blank line after a paragraph"))
        if NESTED_ITEM.match(line):
            found.append((number, "a nested list needs a 4-space indent"))
        if line.startswith("|") and PIPE_IN_CODE.search(line):
            found.append((number, "the site shows the backslash of \\| in code in a table"))
        if ATTR_LIST.search(line):
            found.append((number, "GitHub shows {: ...} as text: use an inline <a id>"))
        previous = line
    return found


def is_paragraph(line: str) -> bool:
    """A line of paragraph text: not blank, indented, a list item or another block."""
    return bool(
        line.strip()
        and not line[0].isspace()
        and not LIST_ITEM.match(line)
        and not line.startswith(("#", "|", "<", ">", "```", "~~~"))
    )


def test_each_difference_is_found_and_nothing_else():
    text = (
        "A paragraph:\n"
        "- straight after it\n"
        "\n"
        "1. a step\n"
        "   continued\n"
        "   - nested by 3\n"
        "    - nested by 4\n"
        "2. the next step\n"
        "\n"
        "| `--party A\\|@D` | x |\n"
        "| `--party A`, a \\| b | x |\n"
        "\n"
        '<a id="here"></a>**Here** {: #here }\n'
        "\n"
        "```\n"
        "text\n"
        "- not a list\n"
        "```\n"
        "## A heading\n"
        "- after a heading\n"
    )
    assert [number for number, _ in problems(text)] == [2, 6, 10, 13]


@pytest.mark.parametrize("page", PAGES, ids=lambda p: str(p.relative_to(DOCS)))
def test_page_renders_the_same_on_the_site_and_github(page):
    found = problems(page.read_text())
    assert not found, "\n".join(f"{page.relative_to(DOCS)}:{n}: {why}" for n, why in found)
