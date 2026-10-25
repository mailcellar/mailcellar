## What and why

<!-- What this changes, and why. Link the issue if there is one. -->

## How it was checked

<!-- The tests added or changed, and any check against real backups,
as counts only. -->

## Checklist

- [ ] `uv run ruff format && uv run ruff check` and `uv run pytest` pass
- [ ] Tests use the fakes in `tests/`; nothing calls the real Gmail API
- [ ] No real mail or credentials anywhere in the change, its tests or its
      commit messages: placeholders such as `example.com` only
- [ ] Anything that changes Gmail is a dry run by default and never deletes
      permanently
- [ ] Commit messages follow the conventions in CONTRIBUTING.md
- [ ] README, CONTRIBUTING.md or docs/architecture.md updated if behaviour
      or design changed
