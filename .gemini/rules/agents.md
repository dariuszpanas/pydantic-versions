# Git Commit Message Rules

## Critical: How to Create Commit Messages

**NEVER use multiple `-m` flags** to build multi-line commit messages. Each `-m`
creates a separate paragraph with a blank line before it, producing malformed
messages with double-spacing between every line.

**ALWAYS write the commit message to a temporary file** and use `git commit -F <path>`:

```bash
# Write message to a temp file first
cat > /tmp/commit_msg.txt << 'EOF'
feat(scope): short imperative summary

Body paragraph that explains the change. Wrap prose at 72 characters
so terminal history stays readable.

Key changes:
- First change description, wrapped at 72 chars if needed to keep
  lines from running too long.
- Second change description.

Validation: uv run make ci: passed
EOF

# Then commit using the file
git commit -F /tmp/commit_msg.txt
# Or amend:
git commit --amend -F /tmp/commit_msg.txt
```

## Format

Follow Conventional Commits. The header format is:

```
<type>(<optional scope>): <imperative summary>
```

Allowed types: `build`, `chore`, `ci`, `docs`, `feat`, `fix`, `perf`,
`refactor`, `revert`, `style`, `test`.

## Structure

```
<type>(<scope>): <summary, <=72 chars>
                                          <- blank line
Body prose explaining the change and why it is needed. Wrap at 72
characters. No blank lines between continuation lines of the same
paragraph.
                                          <- blank line
Key changes:                              <- optional structured list
- First item, can wrap to the next line
  with 2-space indent.
- Second item.
                                          <- blank line
Validation: uv run make ci: passed        <- required
```

## Rules Enforced by CI

The script `scripts/check_conventional_commits.py` runs in CI and checks:

1. **Header**: Must match `<type>(<scope>): <summary>` or `<type>: <summary>`.
2. **Line length**: All prose lines must be **<= 72 characters**.
3. **Body**: Must contain at least **8 prose words** (excluding headings,
   validation lines, metadata, and trailers).
4. **Validation line**: Must include validation evidence such as
   `uv run make ci: passed` or `Validation: not run because <reason>`.

Run locally before pushing:

```bash
python scripts/check_conventional_commits.py --range origin/main..HEAD
```

## Self-Documenting Commits

Commit messages must be **self-documenting** and portable. A reader of
`git log` must understand what the commit does without accessing GitHub.

- **Do NOT** put `Closes #N` or issue references in commit messages.
  Those belong in the **PR description only** (a GitHub construct).
- **Do** describe the actual changes in plain English in the body.
- **Do NOT** repeat the subject line as the body.
- **Do NOT** leave template tokens or placeholder text.

## Squashing

When a PR contains fixups, CI repairs, or review iterations, squash them
into the logical commit they belong to before merge. Use:

```bash
git reset --soft origin/main
git commit -F /tmp/commit_msg.txt
```

Or interactive rebase for multi-commit PRs.

Push rewritten history with `--force-with-lease`, never unconditional
`--force`.
