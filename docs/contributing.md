# Contributing

## Local setup

Use uv 0.12.23 or newer, as specified by `tool.uv.required-version` in
`pyproject.toml`.
Install dependencies from the lockfile:

```bash
uv sync --frozen
```

Run checks after committing the candidate changes. `make ci` includes YAGA's
committed-tree checks and workflow lint, which requires Docker with Linux
containers:

```bash
make ci
```

To use a compatible uv without changing a global installation, run
`uv tool run --from "uv>=0.12.23" uv run make ci`.

## Development commands

- `make format`: format with Ruff.
- `make lint`: lint and auto-fix with Ruff.
- `make typecheck`: run `ty` plus the external mypy consumer contract.
- `make dead-code`: scan production code with Vulture and reviewed local exceptions.
- `make test`: run the test suite.
- `make docs-build`: build the documentation site.
- `make policy-check`: run YAGA workflow and committed repository policies;
  set `REVISION` to inspect a different commit (default: `HEAD`).
- `make workflow-lint`: lint workflows with YAGA's pinned actionlint image.
- `make commit-check`: validate one commit with YAGA (`REVISION=HEAD`).
- `make change-check`: require changed tests alongside changed Python source
  (`RANGE=origin/main...HEAD`). This checks paths, not test effectiveness.

Repository policy lives in `.yaga/checks/repository.toml` and the referenced
`.yaga/*-policy.toml` files. Snapshot providers inspect committed modes, paths,
blob sizes, and required or forbidden files; uncommitted edits are not included.
Workflow providers inspect the working files for immutable action references,
permissions, and checkout safety. CI runs these checks against the exact PR head
alongside the existing tests, typing, docs, security audit, and package build.

## Commits and pull requests

Use [Conventional Commits](https://www.conventionalcommits.org/) for commit
messages and pull request titles:

```text
<type>[optional scope][!]: <imperative summary>
```

Common types are `build`, `chore`, `ci`, `docs`, `feat`, `fix`, `perf`,
`refactor`, `release`, `revert`, `style`, and `test`. Keep the summary short
enough to scan in `git log --oneline`. Use `!` for an intentional breaking
change and add a `BREAKING CHANGE:` footer when the history needs migration
detail.

Treat each retained human-authored logical commit as a portable, PR-grade
change record. Its message must stand on its own in `git log`, mirrors,
archives, and changelog tooling without relying on GitHub metadata. Record:

- the observable change and why it is needed;
- important invariants, boundaries, and non-goals;
- compatibility, migration, rollout, or release impact when applicable;
- exact validation results, or a specific reason validation was not run; and
- useful repository-local modules, tests, ADRs, issues, or documentation.

One large atomic commit is valid. Use proportional detail for small mechanical
changes and keep unrelated changes in separate logical commits.

The tracked [`.gitmessage`](https://github.com/dariuszpanas/pydantic-versions/blob/main/.gitmessage)
template recommends this layout for changes needing detailed context:

```text
<type>[optional scope][!]: <imperative summary>

## Summary

- Describe the observable change and why it is needed.

## Boundaries and compatibility

- Record important invariants, non-goals, and compatibility impact.

## Investigation

- Point to useful repository-local modules, tests, ADRs, issues, or docs.

## Validation

- `<command>`: result
```

YAGA enforces the shared policy in `.yaga.toml`: an allowed lowercase
Conventional Commit type, an optional lowercase scope, a header of at most
100 characters without ending punctuation, and a prose body of at least eight
words. Merge commits are rejected. The four-section template is authoring
guidance; YAGA does not enforce section names or interpret validation claims.
Small changes may use a short explanatory body. Record actual results and keep
multiple independent validation commands in separate Markdown list items.

The required `Commit Messages` workflow reads committed default-branch policy
and validates the PR title plus every commit in the exact PR range. It fetches
PR objects without executing PR code. YAGA skips message validation for
authenticated Dependabot PR events, including security updates, after checking
event identity and complete Git history. All other CI checks still run. Local
`yaga commit check` never infers a bot exemption from author names, branches,
or generated message text.

Wrap ordinary commit prose at about 72 characters so terminal history stays
readable. This wrapping guidance does not apply to PR descriptions, which should
use natural Markdown. URLs, complete Markdown tables, generated dependency
metadata, and recognized Git trailers do not need artificial wrapping.

Install the tracked template for this checkout or worktree:

```bash
git config extensions.worktreeConfig true
git config --worktree commit.template "$(git rev-parse --show-toplevel)/.gitmessage"
git config --worktree core.commentChar ";"
```

The comment-character setting preserves the `##` headings when Git opens
the template; instructional comments begin with `;` and are removed by Git.
Keep these settings worktree-scoped: `--local` writes shared repository config
and can make one linked checkout use another checkout's template path.

A PR should explain the problem and approach, call out compatibility or release
impact, list aggregate validation results, and link its issue with
`Closes #<number>` when appropriate. Issue links and PR descriptions supplement
commit messages; they are not the only place durable commit context should live.
Keep the material facts aligned while formatting each surface for its reader.

Before pushing, fetch and inspect the exact history the PR would retain:

```bash
git fetch origin
git log --format=fuller origin/main..HEAD
uv run --frozen yaga commit check --range origin/main..HEAD
make change-check
```

Compare every material commit body with the PR description. Fold `fixup!` and
`squash!` commits, CI or review repairs, formatting-only follow-ups, and other
development iterations into the logical commit they correct. Preserve genuinely
independent changes as focused commits with their own descriptive bodies. Push a
rewritten branch with `--force-with-lease`, never an unconditional force push.

When a PR intentionally retains more than one logical commit, prefer a rebase
merge so those commits remain visible. Do not squash independent changes merely
to make a PR appear smaller.

Avoid transient process context in durable history. Do not record private
planning conversations, temporary scaffolding sources, secrets, run-specific
identifiers, or who requested the work unless that fact is part of the product
or operational contract.

Example:

```text
feat: add versioned schema API

## Summary

- Register ordered schema versions and generate historical wire models from
  the current Pydantic model.

## Boundaries and compatibility

- Keep schema labels opaque and leave YAML parsing to callers.

## Investigation

- Document the legacy fallback and Django Ninja inspection boundary.

## Validation

- `uv run make ci`: passed.
- Strict docs and package build: passed.
```
