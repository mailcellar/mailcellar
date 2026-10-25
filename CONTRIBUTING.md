# Contributing to mailcellar

mailcellar works on people's mail, and some of its commands change Gmail.
Changes are welcome; the rules below keep both the code and other people's
mail safe.

## Set up and check

You need [uv](https://docs.astral.sh/uv/); it provides the Python version
in `.python-version`.

```bash
uv sync                                   # dependencies and the mailcellar command
uv run pytest                             # all tests; no network
uv run pytest --cov                       # with a branch-coverage report
uv run ruff format && uv run ruff check   # format, then lint; both must pass
```

CI runs the same three checks on every push and pull request
([.github/workflows/ci.yml](.github/workflows/ci.yml)). Read
[docs/architecture.md](docs/architecture.md) first.

## Rules for changes

- **Coverage is a guide, not a target.** Use `--cov` to find branches no
  test reaches, especially in code that changes Gmail; there is no
  minimum percentage.
- **Tests never touch Gmail or real data.** Use the fakes in `tests/`
  (`FakeGmail`, `FakeService`, `FakeBackup`). `tests/conftest.py` points
  the data root and config folder at each test's own directory; keep it
  that way.
- **No real mail anywhere in the repository.** Not in tests, docs, issues,
  commit messages or pull requests: no addresses, names, subjects or
  message contents. Use `example.com`, `example.org` and invented names.
  Never paste tokens or `client_secrets.json`.
- **Anything that changes Gmail is staged.** A dry run by default, then an
  explicit flag (`--label`, `--trash`, `--yes`), and nothing is ever
  permanently deleted from Gmail. New commands follow the same pattern.
- **The backup format stays gyb's,** byte for byte (`backup/gyb.py`).
  New per-message data goes in `meta.sqlite`, never in `msg-db.sqlite`.
- **Schema changes** go through `SCHEMA_VERSION` and `MIGRATIONS`
  (`backup/sidecar.py`, `dedup/db.py`), and migrations must be safe to
  run twice. Bump `message.PARSER_VERSION` when `parse_message()` output
  changes.
- **Layout:** all code lives in `src/mailcellar/`. Each command's module
  exposes `main(argv, prog)` and the top-level `cli.py` dispatches to it.
- **Style:** decisions are pure functions over frozen values; I/O stays in
  a thin layer that gathers inputs and carries out the decisions, and
  returns new values rather than changing its arguments
  (`restore/apply.py` shows the pattern).
- **Output:** `print` for results and prompts; `logging` (a module-level
  `logger`) for progress, warnings and diagnostics, which `-v`/`-q` control.
- **Dependencies:** standard library first. The only runtime dependencies
  are Google's API client and OAuth libraries; adding one needs a reason.
- **Evidence:** a change to matching, offload rules or restore behaviour
  says in the pull request why, and what it was checked against
  (synthetic examples, or counts from real backups without the mail
  itself). Update [docs/architecture.md](docs/architecture.md) when the
  design changes.

## Commits

Messages follow [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/):

```
type(scope): summary in the imperative, lower case, no full stop

Why the change was needed and what it does, wrapped at 72 columns.
What it was checked against: the tests, and any real-data check as
counts only (never addresses, subjects or other mail content).

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```

- **Types:** `feat` (a new command, option or behaviour), `fix`, `docs`,
  `test`, `refactor`, `perf`, `build` (`pyproject.toml`, `uv.lock`), `ci`,
  `chore`.
- **Scopes,** optional: `backup`, `dedup`, `offload`, `restore`, `report`,
  `gmailapi`, `message`, `cli`, `paths`.
- **Summary** at most 72 characters.
- **Breaking changes** (a CLI option or output format changes, or the
  backup format) take `!` after the scope and a `BREAKING CHANGE:` footer
  saying what to do. A schema migration isn't breaking, since it runs by
  itself, but the body names it (`meta.sqlite schema 9 → 10`).
- **One logical change per commit:** its code, tests and docs together.
  `uv run ruff format && uv run ruff check` and `uv run pytest` pass at
  every commit.
- **AI assistance** is disclosed with a `Co-Authored-By:` trailer naming
  the model. It records how the change was made, and doesn't change who
  "the mailcellar authors" in NOTICE are.

## Reporting problems

Describe what you ran and what happened, with the counts and error
messages, but not the mail itself. If a message triggers a bug, describe
its structure (headers present, MIME parts, sizes) or build a synthetic
example that reproduces it.

## Licence

mailcellar is licensed under the [Apache License 2.0](LICENSE).
Contributions are accepted under the same licence (section 5 of the
licence), and contributors become part of "the mailcellar authors" named
in [NOTICE](NOTICE).
