# Contributing

## Local setup

Install dependencies:

```bash
uv sync
```

Run checks:

```bash
make ci
```

## Development commands

- `make format`: format with Ruff.
- `make lint`: lint and auto-fix with Ruff.
- `make typecheck`: run `ty`.
- `make test`: run the test suite.
- `make docs-build`: build the documentation site.

## Commit messages

Use a concise conventional subject followed by a descriptive body when the change is more than a small mechanical edit.

The subject should:

- Use a conventional prefix such as `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`, or `release:`.
- Describe the project change directly.
- Stay short enough to scan in `git log --oneline`.

The body should:

- Explain what changed and why it changed.
- Describe user-facing, developer-facing, or maintenance impact when relevant.
- Mention important tradeoffs, compatibility notes, or follow-up constraints.
- Include a `Validation:` paragraph with the checks that were actually run.

Avoid putting transient process context in commit history. Commit messages should not mention private planning conversations, temporary scaffolding sources, other repositories used only as inspiration, or who requested the work unless that context is directly relevant to the project itself.
