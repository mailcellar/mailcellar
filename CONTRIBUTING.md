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
HYPOTHESIS_PROFILE=ci uv run pytest       # property tests with CI's 2,000 examples each
uv run pytest -m imap_server              # against Dovecot in a container (podman or docker)
uv run ruff format && uv run ruff check   # format, then lint; both must pass
```

CI runs the same three checks on every push and pull request
([.github/workflows/ci.yml](.github/workflows/ci.yml)). Read
[docs/architecture.md](docs/architecture.md) first.

## Rules for changes

- **Keep what [What stays stable](docs/reference/stability.md) lists.** A
  change that breaks it waits for a major version; a rename keeps the old
  name working, with a warning, for at least one minor version. A new
  column in a file scripts read goes at the end.
- **Coverage is a guide, not a target.** Use `--cov` to find branches no
  test reaches, especially in code that changes Gmail; there is no
  minimum percentage.
- **Property tests** (`tests/test_properties.py`, with Hypothesis) state a
  rule for every input where mail or queries come from other people. A
  failure Hypothesis finds also gets an ordinary example test, next to the
  fix.
- **Tests never touch Gmail or real data.** Use the fakes in `tests/`
  (`FakeMailbox`, `FakeService`, `FakeBackup`). `tests/conftest.py` points
  the data root and config folder at each test's own directory; keep it
  that way.
- **No real mail anywhere in the repository.** Not in tests, docs, issues,
  commit messages or pull requests: no addresses, names, subjects or
  message contents. Use `example.com`, `example.org` and invented names.
  Never paste tokens or `client_secrets.json`. A workflow
  ([.github/workflows/pii-scan.yml](.github/workflows/pii-scan.yml)) flags
  email addresses that aren't placeholders, token patterns and home-folder
  paths in new issues, discussions and their comments with the
  `needs-redaction` label. It
  can't stop a post or spot names and subjects, so the rule is still yours
  to keep.
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
  are Google's API client and OAuth libraries, plus a minimum version of
  certifi, the CA bundle they trust; adding one needs a reason.
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

Co-Authored-By: <model or agent> <address>   (only if AI helped)
```

- **Types:** `feat` (a new command, option or behaviour), `fix`, `docs`,
  `test`, `refactor`, `perf`, `build` (`pyproject.toml`, `uv.lock`), `ci`,
  `chore`.
- **Scopes,** optional: `backup`, `dedup`, `offload`, `restore`, `search`,
  `report`, `setup`, `hold`, `gmailapi`, `imap`, `mailbox`, `message`, `cli`, `paths`.
- **Summary** at most 72 characters.
- **Breaking changes** (a CLI option or output format changes, or the
  backup format) take `!` after the scope and a `BREAKING CHANGE:` footer
  saying what to do. A schema migration isn't breaking, since it runs by
  itself, but the body names it (`meta.sqlite schema 9 → 10`).
- **One logical change per commit:** its code, tests and docs together.
  `uv run ruff format && uv run ruff check` and `uv run pytest` pass at
  every commit.
- **Changelog:** a change a user would notice adds a line under
  `## Unreleased` in [`CHANGELOG.md`](CHANGELOG.md), in the same commit,
  under Added, Changed, Fixed, Removed or Security. Internal changes (tests, CI,
  refactors) don't.
- **AI assistance,** if any, is disclosed with a `Co-Authored-By:` trailer
  naming the model or agent; leave it out when none was used. It records how the change was made, and doesn't change who
  "the mailcellar authors" in NOTICE are.

## The documentation site

[`.github/workflows/docs.yml`](.github/workflows/docs.yml) builds
<https://mailcellar.github.io/mailcellar/> from `docs/` and
[`mkdocs.yml`](mkdocs.yml): pull requests that touch them build it in strict
mode (a broken link fails), and pushes to `main` deploy it. A new guide goes in
`docs/guide/` and in `mkdocs.yml`'s `nav`. Every command and option is
documented in `docs/reference/` (one page per command); guides link there for
options instead of repeating them. Links from a guide to the README use its
GitHub address, so they work on GitHub and on the site. To preview:

```bash
uvx --exclude-newer 2026-10-11 --from zensical==0.0.69 zensical serve
```

Once, a maintainer sets Settings → Pages → Source to *GitHub Actions*.

## Releasing

Pushing a tag `vX.Y.Z` releases that version:
[`.github/workflows/release.yml`](.github/workflows/release.yml) checks the
tag against `version` in `pyproject.toml`, runs the checks again, builds the
package, uploads it to [PyPI](https://pypi.org/project/mailcellar/) and
attaches it to a GitHub release whose notes are that version's section of
[`CHANGELOG.md`](CHANGELOG.md). The workflow stops before uploading if the
changelog has no section for the version.

**Once, before the first release** (a maintainer with access to both):

1. On PyPI, add a *pending trusted publisher* (Your account → Publishing):
   project `mailcellar`, owner `mailcellar`, repository `mailcellar`,
   workflow `release.yml`, environment `pypi`. PyPI then accepts uploads
   from that workflow alone, with no token stored anywhere.
2. On GitHub, create the environment `pypi` (Settings → Environments) and,
   preferably, add a required reviewer, so each upload waits for a person's
   approval.

**Each release:**

1. On `main`, with CI green: set `version` in `pyproject.toml` (`uv version
   X.Y.Z` also updates `uv.lock`), rename `## Unreleased` in `CHANGELOG.md`
   to `## X.Y.Z (YYYY-MM-DD)`, and commit both as `build: release X.Y.Z`
   through a pull request like any other change.
2. Once that is merged, tag the merge commit and push the tag:
   ```bash
   git switch main && git pull
   git tag -a vX.Y.Z -m "mailcellar X.Y.Z"
   git push origin vX.Y.Z
   ```
3. Approve the `pypi` environment's deployment when GitHub asks, then
   check the release page and `uvx mailcellar@X.Y.Z --help`.

A version on PyPI can never be uploaded again, even after it is deleted: a
broken release is fixed by the next version, not by replacing it.

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
