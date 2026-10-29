# CLAUDE.md

mailcellar: Gmail backups, with verify and scheduling (`backup/`),
account setup (`setup/`), deduplication (`dedup/`), offloading old mail out
of Gmail (`offload/`) and back, with export (`restore/`), legal holds
(`hold/`), search (`search/`), and reports (`report/`) for the configured
accounts, all in one package, `src/mailcellar/`, behind one `mailcellar`
command (`cli.py`).
Read [docs/architecture.md](docs/architecture.md) first.
User-facing usage: [README.md](README.md); every command and option is in
[docs/reference/](docs/reference/index.md).

## Commands

```bash
uv sync                                # after any pyproject.toml change: the command is installed
uv run pytest                          # all tests; no network
uv run pytest --cov                    # with branch coverage; a guide, no minimum
HYPOTHESIS_PROFILE=ci uv run pytest    # property tests at CI's 2,000 examples each
uv run pytest -m imap_server           # IMAP against Dovecot in a container; opt-in
uv run ruff format && uv run ruff check   # format, then lint; both must pass
uvx --exclude-newer 2026-10-11 --from zensical==0.0.69 zensical build --strict   # docs; writes site/ and .cache/
uv run mailcellar config               # where the config and data are; each account's state
uv run mailcellar setup [ADDRESS] [--client FILE] [--name N] [--no-auth] [--force]   # add an account
uv run mailcellar <backup|count|meta|verify> [ACCOUNT ...] [options]   # all accounts by default
uv run mailcellar auth [ACCOUNT ...] [--scope readonly|modify] [--renew]   # `dedup auth` still works
uv run mailcellar schedule <install|remove|status>   # the unattended backup job
uv run mailcellar dedup <index|match|plan|apply> [options]
uv run mailcellar offload <plan [--review]|apply> [options]
uv run mailcellar forget [ACCOUNT ...] <--batch|--rule|--due|--empty|--undo RUN> [--yes]
uv run mailcellar restore ACCOUNT <--batch|--rule|--id|--offloaded|--all> [--to ACCOUNT] [--yes]
uv run mailcellar restore ACCOUNT <--from-pruned BATCH|--from-mbox FILE|--from-folder DIR> [--yes]
uv run mailcellar search QUERY [ACCOUNT ...] [--offloaded] [--rule R] [--before DATE]
uv run mailcellar export ACCOUNT <--batch|--rule|--id|--offloaded|--all|--hold H> --to FILE.mbox|FILE.zip
uv run mailcellar hold <add|list|show|release> [NAME] [ACCOUNT ...]   # legal holds
uv run mailcellar report <storage|bulk|volume|threads|labels|correspondents|health|senders>
```

## Rules

- The data root (mail, derived databases, plans, logs) and the config
  folder (OAuth clients, tokens) live outside the repo: `$MAILCELLAR_DATA`,
  `data_dir` in settings.toml or `~/mailcellar`, and `$MAILCELLAR_CONFIG` or
  `~/.config/mailcellar`; `mailcellar config` shows where.
- Mail is personal data. Show message details to the user when they ask,
  but never write them, or the user's addresses, names, contacts or
  setup, into anything committed: code, tests, docs, commit messages.
  Use placeholders there (`example.com`, `example.org`, invented names).
  Never print credentials. Write scratch output to the scratchpad, not
  the repo root or the data root.
- Ask before anything that changes Gmail, the backups, or access:
  `dedup apply --label/--trash`, `offload apply --label/--trash`,
  `restore --yes`, `backup --prune`, `forget --yes`,
  `forget --empty --yes` and `--undo` (they delete from, or move
  within, the backup), `auth --scope modify` (also as
  `dedup auth`), `setup` (it installs an OAuth client or a
  service-account key and opens a sign-in), `schedule install/remove`, and
  moving or deleting the data root or config folder. Fine without asking:
  - dry runs, `config`, `count`, `verify`, `schedule status`, the reports;
  - `export`, read-only, with `--to` in the scratchpad (it copies mail);
  - `dedup index/match/plan`, `offload plan` and `search`, which write
    only their own files in the data root;
  - plain `backup` and `meta`, which add to and refresh the backup but
    never delete from it.
- Copy a database aside before running a schema migration on real data.
  `sidecar.open_meta` does it for `meta.sqlite` (`meta.sqlite.pre-vN` next
  to it, before the first write); for `dedup.sqlite` and `search.sqlite`,
  copy them yourself, or rebuild them (`dedup index`, a search).
- Check changes against the real backups, read-only. Committed files
  (code, README, docs/) never quote figures from them.
- Run live Gmail operations that take more than a couple of minutes in the
  background. Trashing goes at about 35 messages a second (2,000 in about
  a minute), most of it reading each message back.
- Keep the backup format byte-compatible with gyb (`backup/gyb.py`);
  new per-message data goes in `meta.sqlite`, never `msg-db.sqlite`.
- Anything that adds messages to a backup or removes them appends a link
  to its hash chain first (`chain.append`, `backup/chain.py`), so
  `verify --chain` can tell it from tampering.
- Schema changes go through `SCHEMA_VERSION` + `MIGRATIONS`
  (`dedup/db.py`, `backup/sidecar.py`); migrations must be safe to repeat.
- Bump `message.PARSER_VERSION` whenever `parse_message()` output changes.
- Matching changes need evidence from the real backups.
- Tests use the fakes in `tests/` (`FakeMailbox`, `FakeService`,
  `FakeBackup`); never call the real API from tests. `tests/conftest.py`
  pins the data root and config folder to each test's directory, so no
  test can reach the real backups or tokens; keep it that way.
- All code lives in `src/mailcellar/`. Each command's module exposes
  `main(argv, prog)` and the top-level `cli.py` dispatches to it (its
  `_dedup`, `_search`, … functions list them). Add no other entry
  points; a new top-level module is for code several commands share
  (as `gmailapi.py`, `mailbox.py` and `message.py` are), never for a
  command.
- Docs: every option of a command goes in its page under `docs/reference/`
  (`tests/test_reference_docs.py` fails when `--help` lists one the page
  doesn't name); a new guide goes in `docs/guide/`, `mkdocs.yml`'s `nav` and
  the README's Documentation list. The README stays a landing page. Build in
  strict mode (a broken link fails) with the command above. A change a user
  would notice also gets a line under `## Unreleased` in `CHANGELOG.md`.
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
  API client and OAuth libraries, plus a floor on certifi (the CA bundle
  they trust) to keep out a bundle with distrusted roots.
- Code is formatted by `ruff format` (line length 100, config in
  `pyproject.toml`). With Python 3.14 as the target it writes
  `except A, B:` without parentheses (PEP 758); that is intended.
