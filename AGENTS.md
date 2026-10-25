# Agent Guide (`AGENTS.md`)

This file provides persistent operational context for coding agents working in
`pydantic-versions`. Read and follow these rules at the start of every task and
after any context compaction or harness change.

## Authoritative repository references

- Contributor workflow and commit policy: [`CONTRIBUTING.md`](CONTRIBUTING.md)
  (mirrored identically at [`docs/contributing.md`](docs/contributing.md))
- Keep a Changelog release history: [`docs/changelog.md`](docs/changelog.md)
- Public documentation index for agents: [`docs/llms.txt`](docs/llms.txt)
- Public API and 1.x stability contract:
  [`docs/reference/stability-policy.md`](docs/reference/stability-policy.md)
- Enforced commit and repository policies: [`.yaga.toml`](.yaga.toml) and
  [`.yaga/checks/repository.toml`](.yaga/checks/repository.toml)
- Release metadata and changelog validator:
  [`scripts/validate_release.py`](scripts/validate_release.py)

## Keep a Changelog discipline (`docs/changelog.md`)

- The project changelog lives at `docs/changelog.md` (there is no root
  `CHANGELOG.md`).
- Format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/)
  and [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html).
- Every notable change (`feat`, `fix`, `perf`, user-visible `refactor` or
  `chore`, `removed`, `security`) **must** update `## [Unreleased]` in
  `docs/changelog.md` in the same pull request or before release preparation,
  using the standard headings (`### Added`, `### Changed`, `### Deprecated`,
  `### Removed`, `### Fixed`, `### Security`).

## Documentation and repository synchronization invariants

1. **Contributor guide parity**: `CONTRIBUTING.md` and `docs/contributing.md`
   must remain byte-for-byte identical
   (`tests/unit/test_package_metadata.py::test_rendered_contributor_guide_matches_the_repository_guide`).
2. **Public API reference parity**: Every public helper signature and exported
   frozen dataclass in `src/pydantic_versions/__init__.py` must match
   `docs/reference/api.md`
   (`tests/integration/test_documentation_examples.py`).
3. **Executable documentation blocks**: Fenced blocks marked with
   `<!-- pv-doc-test: ... -->` in `README.md` and `docs/guide/*.md` execute in
   isolated subprocesses during `pytest`.
4. **Source changes require test changes**: Any commit range touching
   `src/**/*.py` must also touch `tests/**/*.py`
   (`.yaga/change-policy.toml`).
5. **Coverage floor**: Statement coverage across `src/` must remain `>= 95.00%`
   on every supported Python (3.12–3.15) and Pydantic (minimum, locked, latest)
   matrix target. Aim for `>= 95.15%` locally so minor interpreter/version
   branch differences never drop a matrix job below `95.00%`.
6. **Clean working tree**: Never commit scratch files, coverage artifacts, or
   paths forbidden by `.yaga/tree-policy.toml`.

## Git, worktree, commit, and pull request workflow

- **Issue-first tracking**: Link each pull request to its focused issue with
  `Closes #<number>` in the commit body and PR description.
- **Worktree-safe branching**: `main` may be checked out in another linked
  worktree. Never run `git checkout main` or `gh pr merge --delete-branch`.
  Create feature branches from `origin/main`:
  ```bash
  git fetch origin main
  git checkout -B <type>/<short-slug> origin/main
  ```
- **Conventional Commits (`.yaga.toml`)**:
  - Allowed types: `build`, `chore`, `ci`, `docs`, `feat`, `fix`, `perf`,
    `refactor`, `release`, `revert`, `style`, `test` (lowercase).
  - Header `<= 100` characters, no trailing period.
  - Body is required (`>= 8` words), wrapped at ~72 characters.
  - Write commit messages and PR descriptions to scratch files outside the
    repository and pass `git commit -F <path>` and
    `gh pr create --body-file <path>` to avoid shell quoting issues.
- **Linear history**: Merge pull requests with `gh pr merge <number> --rebase`
  after all CI checks pass (`gh pr checks <number> --watch`). Merge commits are
  rejected by `.yaga.toml`.

## Required local verification gate before pushing

Run these checks locally before pushing a branch (commit candidate changes first
so YAGA snapshot checks inspect the new `HEAD`):

```bash
uv run ruff format --check .
uv run ruff check .
uv run ty check
uv run mypy --strict tests/typing/schema_family_contract.py
uv run vulture
uv run pytest --cov=src --cov-report=term-missing --cov-fail-under=95
uv run zensical build --strict
uv run --frozen yaga repo check --plan .yaga/checks/repository.toml --revision HEAD
uv run --frozen yaga commit check --range origin/main..HEAD
uv run --frozen yaga change check --policy .yaga/change-policy.toml --range origin/main...HEAD
```

## Release preparation checklist (`X.Y.Z`)

When preparing a release `X.Y.Z`:

1. Verify that all user-facing guide and reference pages (`docs/guide/*.md`,
   `docs/reference/*.md`, `docs/llms.txt`) and all notable changes since the
   previous tag under `## [Unreleased]` in `docs/changelog.md` are up to date.
2. Move the `## [Unreleased]` entries under a new dated heading
   `## [X.Y.Z] - YYYY-MM-DD` placed directly below an empty `## [Unreleased]`
   section.
3. Update the comparison reference definitions at the bottom of
   `docs/changelog.md`:
   - `[Unreleased]: https://github.com/dariuszpanas/pydantic-versions/compare/vX.Y.Z...HEAD`
   - `[X.Y.Z]: https://github.com/dariuszpanas/pydantic-versions/compare/v<previous>...vX.Y.Z`
4. Update `version = "X.Y.Z"` in `pyproject.toml` and refresh `uv.lock` with
   `uv lock`.
5. Validate release metadata and changelog structure:
   ```bash
   uv run python scripts/validate_release.py X.Y.Z
   ```
6. Run the full local verification gate, open the release PR, and merge via
   rebase once CI passes. Wait for the Read the Docs `latest` build for the
   merged release commit to finish before pushing the `vX.Y.Z` tag (so Read the
   Docs does not miss the tag sync while `latest` is mid-build), then push
   `vX.Y.Z` to trigger `.github/workflows/release.yml` and verify both `latest`
   and `stable` on Read the Docs.
