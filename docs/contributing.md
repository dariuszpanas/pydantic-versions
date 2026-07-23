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

For a material change, treat each retained logical commit as a portable,
PR-grade change record. Its message must stand on its own in `git log`, mirrors,
archives, and changelog tooling without relying on GitHub metadata. Record:

- the observable change and why it is needed;
- important invariants, boundaries, and non-goals;
- compatibility, migration, rollout, or release impact when applicable;
- exact validation results, or a specific reason validation was not run; and
- useful repository-local modules, tests, ADRs, issues, or documentation.

One large atomic commit is valid. Use proportional detail for small mechanical
changes and keep unrelated changes in separate logical commits.

The tracked [`.gitmessage`](../.gitmessage) template suggests this layout:

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

The headings are guidance, not a required format. Unstructured prose is equally
valid when it provides the same durable context. Do not retain template tokens,
development-only notes such as "address review feedback," a body that merely
repeats the subject, or validation commands without their result. For work that
cannot be run locally, state a concrete reason instead of writing a placeholder.

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

The comment-character setting preserves optional `##` headings when Git opens
the template; instructional comments begin with `;` and are removed by Git.

A PR should explain the problem and approach, call out compatibility or release
impact, list aggregate validation results, and link its issue with
`Closes #<number>` when appropriate. Issue links and PR descriptions supplement
commit messages; they are not the only place durable commit context should live.
Keep the material facts aligned while formatting each surface for its reader.

Before pushing, fetch and inspect the exact history the PR would retain:

```bash
git fetch origin
git log --format=fuller origin/main..HEAD
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

Register ordered schema versions and generate historical wire models from the
current Pydantic model. Historical validation upgrades values through explicit
migrations before the authoritative current-model validation boundary.

Keep schema labels opaque and leave YAML parsing to callers. Document the
legacy unversioned fallback and Django Ninja inspection boundary.

Validation: `uv run make ci` passed; strict docs and package build passed.
```
