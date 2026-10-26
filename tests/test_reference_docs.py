"""docs/reference/ names every option that a command's --help lists."""

import re
from pathlib import Path

import pytest

from mailcellar import cli

REFERENCE = Path(__file__).parent.parent / "docs" / "reference"

# Command words → the reference page that documents it.
COMMANDS = {
    ("backup",): "backup",
    ("count",): "backup",
    ("meta",): "backup",
    ("verify",): "backup",
    ("setup",): "configuration",
    ("auth",): "configuration",
    ("schedule", "install"): "configuration",
    ("schedule", "status"): "configuration",
    ("dedup", "index"): "dedup",
    ("dedup", "match"): "dedup",
    ("dedup", "plan"): "dedup",
    ("dedup", "apply"): "dedup",
    ("offload", "plan"): "offload",
    ("offload", "apply"): "offload",
    ("offload", "forget"): "offload",
    ("restore",): "restore",
    ("export",): "export",
    ("search",): "search",
    ("report", "storage"): "report",
    ("report", "bulk"): "report",
    ("report", "volume"): "report",
    ("report", "threads"): "report",
    ("report", "labels"): "report",
    ("report", "correspondents"): "report",
    ("report", "health"): "report",
    ("report", "senders"): "report",
}

SKIPPED = {"--help"}
# argparse indents an option by two spaces; wrapped usage and help text go
# deeper, and can start with "--" where textwrap breaks a path at a hyphen.
OPTION_LINE = re.compile(r"^  (?:-\w(?: \S+)?, )?(--[a-z][\w-]*)", re.MULTILINE)


def options_in(help_text: str) -> set[str]:
    return set(OPTION_LINE.findall(help_text)) - SKIPPED


def listed_options(argv: tuple[str, ...], capsys) -> set[str]:
    with pytest.raises(SystemExit) as done:
        cli.main([*argv, "--help"])
    assert done.value.code == 0
    return options_in(capsys.readouterr().out)


def test_only_option_lines_count_not_wrapped_text():
    help_text = (
        "usage: mailcellar backup [-h] [--prune]\n"
        "                         [--folder FOLDER]\n"
        "options:\n"
        "  -h, --help            show this help message and exit\n"
        "  --folder FOLDER       Backup folder (default: /tmp/a-Users-x-\n"
        "                        --claude-spares-y/data)\n"
        "  -q, --quiet           Less output\n"
    )
    assert options_in(help_text) == {"--folder", "--quiet"}


@pytest.mark.parametrize("argv", COMMANDS, ids=" ".join)
def test_reference_names_every_option(argv, capsys):
    page = (REFERENCE / f"{COMMANDS[argv]}.md").read_text()
    options = listed_options(argv, capsys)
    assert options, f"no options found in `{' '.join(argv)} --help`"
    missing = sorted(o for o in options if o not in page)
    assert not missing, f"docs/reference/{COMMANDS[argv]}.md does not mention {missing}"
