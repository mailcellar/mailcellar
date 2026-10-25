# CLAUDE.md

mailcellar: Gmail backups (`backup/`), deduplication (`dedup/`),
offloading old mail out of Gmail (`offload/`) and back (`restore/`), and
reports (`report/`) for the configured accounts, all in one package,
`src/mailcellar/`, behind one `mailcellar` command (`cli.py`).
Read [docs/architecture.md](docs/architecture.md) first.
User-facing usage: [README.md](README.md).

## Commands

```bash
uv sync                                # after any pyproject.toml change: the command is installed
uv run pytest                          # all tests; no network
uv run pytest --cov                    # with branch coverage; a guide, no minimum
uv run ruff format && uv run ruff check   # format, then lint; both must pass
uv run mailcellar config               # where the config and data are; each account's state
uv run mailcellar <backup|count|meta|verify> [ACCOUNT ...] [options]   # all accounts by default
uv run mailcellar auth [ACCOUNT ...] [--scope readonly|modify] [--renew]   # `dedup auth` still works
uv run mailcellar schedule <install|remove|status>   # the unattended backup job
uv run mailcellar dedup <index|match|plan|apply> [options]
uv run mailcellar offload <plan|apply> [options]
uv run mailcellar restore ACCOUNT <--batch|--rule|--id|--offloaded|--all> [--to ACCOUNT] [--yes]
uv run mailcellar export ACCOUNT <--batch|--rule|--id|--offloaded|--all> --to FILE.mbox|FILE.zip
uv run mailcellar report <storage|bulk|volume|threads|labels|correspondents|health|senders>
```

## Rules

- The data root (mail, derived databases, plans, logs) and the config
  folder (OAuth clients, tokens) live outside the repo: `$MAILCELLAR_DATA`
  or `data_dir` in settings.toml, and `$MAILCELLAR_CONFIG` or
  `~/.config/mailcellar`; `mailcellar config` shows where. The old
  in-repo `data/` and `config/` stay gitignored.
- Mail is personal data. Show message details to the user when they ask,
  but never write them, or the user's addresses, names, contacts or
  setup, into anything committed: code, tests, docs, commit messages.
  Use placeholders there (`example.com`, `example.org`, invented names).
  Never print credentials. Write scratch output to the scratchpad, not
  the repo root or the data root.
- Ask before anything that changes Gmail, the backups, or access:
  `dedup apply --label/--trash`, `offload apply --label/--trash`,
  `restore --yes`, `backup --sync`, `auth --scope modify` (also as
  `dedup auth`), `schedule install/remove`, and
  moving or deleting the data root or config folder. Fine without asking:
  - dry runs, `config`, `count`, `verify`, `schedule status`, the reports;
  - `export`, read-only, with `--to` in the scratchpad (it copies mail);
  - `dedup index/match/plan` and `offload plan`, which write only their
    own files in the data root;
  - plain `backup` and `meta`, which add to and refresh the backup but
    never delete from it.
- Copy a database aside before running a schema migration on real data
  (`meta.sqlite.pre-vN` next to it).
- Check changes against the real backups, read-only. Committed files
  (code, README, architecture.md) never quote figures from them.
- Run live Gmail operations that take more than a couple of minutes in the
  background. Trashing goes at about 4 messages a second.
- Keep the backup format byte-compatible with gyb (`backup/gyb.py`);
  new per-message data goes in `meta.sqlite`, never `msg-db.sqlite`.
- Schema changes go through `SCHEMA_VERSION` + `MIGRATIONS`
  (`dedup/db.py`, `backup/sidecar.py`); migrations must be safe to repeat.
- Bump `message.PARSER_VERSION` whenever `parse_message()` output changes.
- Matching changes need evidence from the real backups.
- Tests use the fakes in `tests/` (`FakeGmail`, `FakeService`,
  `FakeBackup`); never call the real API from tests. `tests/conftest.py`
  pins the data root and config folder to each test's directory, so no
  test can reach the real backups or tokens; keep it that way.
- All code lives in `src/mailcellar/`. Each command's module exposes
  `main(argv, prog)` (`backup/commands.py`, `dedup/cli.py`,
  `offload/cli.py`, `restore/cli.py`, `report/cli.py`) and the top-level
  `cli.py` dispatches to it. Add no top-level modules or other entry points.
- Commits: Conventional Commits, `type(scope): summary` (types and scopes
  in CONTRIBUTING.md); the body says why and what it was checked against,
  real data as counts only; one logical change per commit, passing ruff
  and pytest; end with the Co-Authored-By trailer. The user reviews every
  commit and push: propose the message, stage explicit paths.
- Style: decisions are pure functions over frozen values (`@dataclass(frozen=True)`,
  tuples; `dataclasses.replace` for a changed copy); I/O happens in a thin
  layer that reads what a decision needs and carries it out. restore is the
  model: `plan()` reads, `decide()` is pure, `apply()` returns new `Result`s
  rather than changing what it was given.
- Output: `print` for results and prompts (stdout); progress, warnings
  and diagnostics go through each module's `logger =
  logging.getLogger(__name__)` to stderr, set up once in `cli.main`
  (`-v` DEBUG, `-q` WARNING). Gmail's retries log at DEBUG, waits of
  `LONG_WAIT` seconds or more at INFO.
- Standard library first; the only runtime dependencies are the Google
  API client and OAuth libraries.
- Code is formatted by `ruff format` (line length 100, config in
  `pyproject.toml`). With Python 3.14 as the target it writes
  `except A, B:` without parentheses (PEP 758); that is intended.
