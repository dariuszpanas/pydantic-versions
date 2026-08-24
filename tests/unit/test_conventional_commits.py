import re
from pathlib import Path

import pytest

from scripts.check_conventional_commits import (
    _ALLOWED_TYPES,
    main,
    validate_header,
    validate_message,
)

ROOT = Path(__file__).parents[2]
LAYOUT_ERROR = [
    "Commit records multiple independent validation commands or results outside "
    "separate top-level `- ` list items. Put each independent result in its own item "
    "under a `## Validation` heading so rendered history keeps the results separate."
]


def _policy_message(validation: str) -> str:
    validation_section = (
        validation if validation.startswith("## Validation") else f"## Validation\n\n{validation}"
    )
    return f"""\
refactor: preserve validation evidence structure

## Summary

- Keep commit history readable across Git and rendered GitHub views.

## Boundaries and compatibility

- Retain durable compatibility context for every logical change.

## Investigation

- Exercise the commit-message checker independently of pull requests.

{validation_section}
"""


def _single_dependabot_message(body: str | None = None) -> str:
    generated_body = (
        body
        or """\
Bumps [example-package](https://example.com/package) from 1.0.0 to 1.0.1.
- [Release notes](https://example.com/package/releases)
- [Commits](https://example.com/package/compare/1.0.0...1.0.1)"""
    )
    return f"""\
chore(deps): bump example-package from 1.0.0 to 1.0.1

{generated_body}

---
updated-dependencies:
- dependency-name: example-package
  dependency-version: 1.0.1
  dependency-type: direct:production
  update-type: version-update:semver-patch
...

Signed-off-by: dependabot[bot] <support@github.com>
"""


def _dependabot_errors(message: str, *, trusted: bool = False) -> list[str]:
    return validate_message(
        message,
        label="Dependabot commit",
        allow_generated_dependency=trusted,
    )


def test_documented_commit_types_match_the_checker() -> None:
    guide = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    documented_section = guide.split("Common types are ", maxsplit=1)[1].split(
        ". Keep the summary", maxsplit=1
    )[0]

    assert frozenset(re.findall(r"`([^`]+)`", documented_section)) == _ALLOWED_TYPES


def test_release_is_an_accepted_commit_type() -> None:
    assert validate_header("release: publish stable package", label="Commit") is None


def test_single_inline_validation_result_remains_valid() -> None:
    message = _policy_message("uv run pytest: 4 passed.")

    assert validate_message(message, label="Commit") == []


def test_generic_result_does_not_satisfy_the_existing_evidence_gate() -> None:
    candidates = (
        "Validation: Tests passed.",
        "## Validation\n\nTests passed.",
    )

    for validation in candidates:
        errors = validate_message(_policy_message(validation), label="Commit")
        assert any("must record validation evidence" in error for error in errors)


def test_repeated_inline_validation_results_are_rejected() -> None:
    message = """\
refactor: preserve validation evidence structure

## Summary

- Keep commit history readable across rendered GitHub views.

## Boundaries and compatibility

- Record independent quality gates without collapsing their results.

## Investigation

- Exercise repeated validation records in the commit-message checker.

## Validation

Validation: 859 tests passed on Pydantic 2.13.4.
Validation: 859 tests passed on Pydantic 2.12.3.
Validation: strict docs and package build passed.
"""

    assert validate_message(message, label="Commit") == LAYOUT_ERROR


def test_multiple_results_cannot_hide_inside_one_explicit_block() -> None:
    candidates = (
        """\
## Validation

Pydantic 2.13.4 tests: passed.
Pydantic 2.12.3 tests: passed.
""",
        """\
## Validation

`uv run pytest`: 859 passed.
Strict docs and package build: passed.
""",
        "`uv run pytest`: 859 passed.\n`uv run make docs`: passed.",
        "Validation: Python tests passed. Strict docs passed.",
        "Validation: Python tests passed; strict docs passed.",
        "Validation: Python tests passed; Validation: 0 failed.",
        "Validation: Python tests passed. Tests: 0 failed.",
        "Validation: Python tests passed; Validation: not run because docs only.",
        """\
## Validation

- Python tests passed. Strict docs passed.
""",
        """\
## Validation

- Python tests passed; strict docs passed.
""",
        """\
## Validation

Tests passed.
Docs passed.
""",
    )

    for validation in candidates:
        assert validate_message(_policy_message(validation), label="Commit") == LAYOUT_ERROR


def test_template_validation_list_is_valid() -> None:
    message = """\
refactor: preserve validation evidence structure

## Summary

- Keep durable commit history readable across Git and rendered GitHub
  views.

## Boundaries and compatibility

- Preserve proportional detail for genuinely small changes.

## Investigation

- Exercise the canonical tracked commit layout.

## Validation

- `uv run pytest`: 4 passed.
- Strict docs and package build: passed.
"""

    assert validate_message(message, label="Commit") == []


def test_wrapped_validation_results_keep_their_list_item_owner() -> None:
    validation = """\
## Validation

- Python lower-bound environment:
  `uv run pytest`: 859 passed.
- Static quality gates:
  `uv run make check`: passed.
"""

    assert validate_message(_policy_message(validation), label="Commit") == []


def test_render_unsafe_list_markers_are_rejected() -> None:
    candidates = (
        """\
## Validation

1234567890. Python tests: passed.
1234567891. Strict docs: passed.
""",
        "## Validation\n\n-\N{NO-BREAK SPACE}Python tests: passed.\n"
        "-\N{NO-BREAK SPACE}Strict docs: passed.",
        "Validation: Python tests passed.\n2. Strict docs passed.\n3. Package build passed.",
        "## Validation\n\n- Python tests: passed.\n  - Strict docs: passed.",
        "## Validation\n\n1. Python tests: passed.\n   2. Strict docs: passed.",
        "## Validation\n\n* Python tests: passed.\n* Strict docs: passed.",
        "## Validation\n\n - Python tests: passed.\n - Strict docs: passed.",
        "* Validation: Python tests passed.\n* Validation: Strict docs passed.",
        "1. Validation: Python tests passed.\n2. Validation: Strict docs passed.",
        "1. uv run pytest: 4 passed.\n2. uv run make docs: passed.",
        "1) uv run pytest: 4 passed.\n2) uv run make docs: passed.",
    )

    for validation in candidates:
        assert validate_message(_policy_message(validation), label="Commit") == LAYOUT_ERROR


def test_indented_code_is_not_accepted_as_a_validation_list() -> None:
    candidates = (
        "## Validation\n\n    - Python tests: passed.\n    - Strict docs: passed.",
        "## Validation\n\n \t- Python tests: passed.\n \t- Strict docs: passed.",
    )

    for validation in candidates:
        errors = validate_message(_policy_message(validation), label="Commit")
        assert any("must record validation evidence" in error for error in errors)


def test_normative_summary_prose_does_not_count_as_an_executed_result() -> None:
    message = """\
refactor: preserve validation evidence structure

## Summary

- Keep the compatibility test suite green.
- Keep the release build clean.
- Make the release build clean.
- Set the compatibility test suite green.
- Stabilize the compatibility test suite green.
- Turn the release build green.
* Add an example showing Python tests: 4 passed.
* Quote an example showing strict docs suite: 1 passed.

Explain the durable parser boundary and retain enough historical context
for readers who inspect the logical change outside its pull request.

## Boundaries and compatibility

- Keep normative prose separate from executed validation evidence.

## Investigation

- Exercise validation-like words in the summary section.

## Validation

uv run pytest: 859 passed.
"""

    assert validate_message(message, label="Commit") == []


def test_validation_narrative_does_not_create_an_extra_result() -> None:
    candidates = (
        "Validation: pytest: 859 passed. This keeps the build clean.",
        "Validation: pytest: 859 passed; this keeps the build clean.",
        """\
## Validation

- `pytest`: 859 passed. This keeps the build clean.
""",
    )

    for validation in candidates:
        assert validate_message(_policy_message(validation), label="Commit") == []


def test_one_command_summary_remains_one_validation_result() -> None:
    candidates = (
        "Validation: uv run pytest: 859 passed; 0 failed.",
        "Validation: uv run pytest: 859 passed; 2 skipped; 0 failed.",
        "Validation: uv run pytest documents: 4 passed.",
        "## Validation\n\n- `make docs`: passed; 0 warnings.",
    )

    for validation in candidates:
        assert validate_message(_policy_message(validation), label="Commit") == []


def test_authenticated_grouped_dependabot_message_skips_human_body_policy() -> None:
    message = """\
chore(actions): bump the github-actions group with 3 updates

Bumps the github-actions group with 3 updates: [actions/checkout](https://github.com/actions/checkout), [astral-sh/setup-uv](https://github.com/astral-sh/setup-uv) and [pypa/gh-action-pypi-publish](https://github.com/pypa/gh-action-pypi-publish).

Updates `actions/checkout` from 7.0.0 to 7.0.1
- [Release notes](https://github.com/actions/checkout/releases)
- [Changelog](https://github.com/actions/checkout/blob/main/CHANGELOG.md)
- [Commits](https://github.com/actions/checkout/compare/v7...3d3c42e5aac5ba805825da76410c181273ba90b1)

Updates `astral-sh/setup-uv` from 9.0.0 to 10.0.1
- [Release notes](https://github.com/astral-sh/setup-uv/releases)
- [Commits](https://github.com/astral-sh/setup-uv/compare/9...10)

Updates `pypa/gh-action-pypi-publish` from 1.14.1 to 1.14.2
- [Release notes](https://github.com/pypa/gh-action-pypi-publish/releases)
- [Commits](https://github.com/pypa/gh-action-pypi-publish/compare/1.14.1...1.14.2)

---
updated-dependencies:
- dependency-name: actions/checkout
  dependency-version: 7.0.1
  dependency-type: direct:production
  update-type: version-update:semver-patch
  dependency-group: github-actions
- dependency-name: astral-sh/setup-uv
  dependency-version: 10.0.1
  dependency-type: direct:production
  update-type: version-update:semver-major
  dependency-group: github-actions
- dependency-name: pypa/gh-action-pypi-publish
  dependency-version: 1.14.2
  dependency-type: direct:production
  update-type: version-update:semver-patch
  dependency-group: github-actions
...

Signed-off-by: dependabot[bot] <support@github.com>
"""

    assert _dependabot_errors(message, trusted=True) == []
    assert any(
        "must contain exactly these second-level headings" in error
        for error in _dependabot_errors(message)
    )

    noncanonical_body = message.replace(
        "Updates `pypa/gh-action-pypi-publish` from 1.14.1 to 1.14.2",
        "<details>Noncanonical upstream prose that the trusted path intentionally "
        "ignores despite this deliberately overlong generated line.</details>",
    )
    assert _dependabot_errors(noncanonical_body, trusted=True) == []


@pytest.mark.parametrize(
    "body",
    (
        """\
Bumps [example-package](https://example.com/package) from 1.0.0 to 1.0.1.

| Package | From | To |
| --- | --- | --- |
| example-package | 1.0.0 | 1.0.1 |""",
        """\
Bumps [example-package](https://example.com/package) from 1.0.0 to 1.0.1.
- [Release notes](https://example.com/package/releases)
- [Upgrade guide](https://example.com/package/upgrade)""",
        """\
Bumps [example-package](https://example.com/package) from 1.0.0 to 1.0.1.
- [Release notes](https://example.com/package/releases)""",
        """\
Bumps [example-package](https://example.com/package) from 1.0.0 to 1.0.1 to
address a security vulnerability in the dependency tree.""",
    ),
    ids=("table", "upgrade-guide", "without-commits-link", "security-sentence"),
)
def test_authenticated_dependabot_accepts_current_upstream_body_variants(body: str) -> None:
    message = _single_dependabot_message(body)

    assert _dependabot_errors(message, trusted=True) == []
    assert any(
        "must contain exactly these second-level headings" in error
        for error in _dependabot_errors(message)
    )


def test_authenticated_dependabot_still_requires_canonical_identity_metadata() -> None:
    valid = _single_dependabot_message()

    candidates = (
        valid.replace("chore(deps):", "feat(deps):"),
        valid.replace("chore(deps):", "chore(other):"),
        valid.replace("Signed-off-by: dependabot[bot]", "Signed-off-by: automation[bot]"),
        valid.replace("Signed-off-by: dependabot[bot]", " Signed-off-by: dependabot[bot]"),
        valid.replace("  update-type: version-update:semver-patch\n", ""),
        valid.replace(
            "  dependency-version: 1.0.1",
            "  dependency-version: 1.0.2",
        ),
    )

    for candidate in candidates:
        errors = _dependabot_errors(candidate, trusted=True)
        assert any("must contain exactly these second-level headings" in error for error in errors)


def test_generated_dependency_cli_flag_is_explicit(capsys: pytest.CaptureFixture[str]) -> None:
    message = _single_dependabot_message()

    assert main(["--commit", message]) == 1
    capsys.readouterr()
    assert main(["--commit", message, "--allow-generated-dependency"]) == 0


def test_required_sections_must_be_exact_complete_and_ordered() -> None:
    valid = _policy_message("uv run pytest: 4 passed.")
    candidates = (
        valid.replace("## Investigation\n\n", ""),
        valid.replace("## Summary", "## Overview"),
        valid.replace(
            "## Investigation",
            "## Investigation\n\n- Duplicate context.\n\n## Investigation",
        ),
        valid.replace(
            "## Summary\n\n- Keep commit history readable across Git and rendered GitHub views.\n\n"
            "## Boundaries and compatibility",
            "## Boundaries and compatibility\n\n"
            "- Retain durable compatibility context for every logical change.\n\n"
            "## Summary",
        ),
    )

    for candidate in candidates:
        errors = validate_message(candidate, label="Commit")
        assert any("must contain exactly these second-level headings" in error for error in errors)


def test_rendered_noncanonical_h2s_are_rejected() -> None:
    valid = _policy_message("uv run pytest: 4 passed.")
    additions = (
        "##\tExtra\n\n- Tab-delimited heading.",
        *(f"{' ' * width}## Extra\n\n- Indented heading." for width in range(1, 4)),
        "##",
        "Extra\n-----",
    )

    for addition in additions:
        errors = validate_message(f"{valid}\n{addition}\n", label="Commit")
        assert any("must contain exactly these second-level headings" in error for error in errors)


def test_h3_and_code_examples_do_not_create_extra_h2s() -> None:
    valid = _policy_message("uv run pytest: 4 passed.")
    additions = (
        "### Extra context\n\n- A third-level heading remains ordinary section content.",
        "```markdown\n## Extra\n\nExtra\n-----\n```",
        "    ## Extra\n    Extra\n    -----",
    )

    for addition in additions:
        assert validate_message(f"{valid}\n{addition}\n", label="Commit") == []


def test_body_cannot_have_a_nonblank_preamble_before_summary() -> None:
    message = _policy_message("uv run pytest: 4 passed.").replace(
        "## Summary",
        "Preamble text that would be detached from the required record.\n\n## Summary",
    )

    assert (
        "Commit body must begin with '## Summary'; remove the nonblank preamble."
        in validate_message(message, label="Commit")
    )


def test_required_headings_cannot_hide_in_markdown_structure() -> None:
    valid = _policy_message("uv run pytest: 4 passed.")
    candidates = (
        valid.replace("## Summary", "> ## Summary"),
        valid.replace("## Summary", "  ## Summary"),
        valid.replace("## Summary", "- Nested record\n  ## Summary"),
        valid.replace("## Summary", "```text\n```not-a-closing-fence\n## Summary"),
        valid.replace("## Summary", "````text\n```\n## Summary"),
        valid.replace("## Summary", "```text\n~~~\n## Summary"),
        valid.replace("## Summary", "```text\n    ```\n## Summary"),
    )

    for candidate in candidates:
        errors = validate_message(candidate, label="Commit")
        assert any("must contain exactly these second-level headings" in error for error in errors)


def test_raw_html_policy_preserves_headers_autolinks_and_code() -> None:
    valid = _policy_message("uv run pytest: 4 passed.")
    investigation = "- Exercise the commit-message checker independently of pull requests."
    candidates = (
        valid.replace(
            "refactor: preserve validation evidence structure",
            "docs: explain <details> syntax",
        ),
        valid.replace(
            investigation,
            "- Inspect <https://example.com> as a normal Markdown autolink.",
        ),
        valid.replace(
            investigation,
            "```html\n<!--\n<details></details>\n--!>\n```` \t\n\n" + investigation,
        ),
        valid.replace(
            investigation,
            "    <!-- literal comment -->\n    <details></details>\n\n" + investigation,
        ),
    )

    for message in candidates:
        assert validate_message(message, label="Commit") == []


def test_raw_html_outside_code_is_rejected() -> None:
    valid = _policy_message("uv run pytest: 4 passed.")
    summary = "- Keep commit history readable across Git and rendered GitHub views."
    investigation = "- Exercise the commit-message checker independently of pull requests."
    candidates = (
        valid.replace("## Summary", "<pre>\n## Summary"),
        valid.replace("## Summary", "<pre\n## Summary\n>"),
        valid.replace("## Summary", "--!>\n## Summary"),
        valid.replace(summary, "<details></details>"),
        valid.replace(summary, "<!--\n## Hidden summary\n--!>"),
        valid.replace(investigation, "<!-- hidden context -->\n" + investigation),
        valid.replace(investigation, "<!DOCTYPE html>\n" + investigation),
    )

    for message in candidates:
        errors = validate_message(message, label="Commit")
        assert any(
            "contains raw HTML outside a fenced or indented code block" in error for error in errors
        )

    details_errors = validate_message(candidates[3], label="Commit")
    assert any(
        "section '## Summary' must contain rendered alphanumeric prose" in error
        for error in details_errors
    )


def test_required_sections_must_contain_meaningful_content() -> None:
    valid = _policy_message("uv run pytest: 4 passed.")
    contents = {
        "## Summary": "- Keep commit history readable across Git and rendered GitHub views.",
        "## Boundaries and compatibility": (
            "- Retain durable compatibility context for every logical change."
        ),
        "## Investigation": (
            "- Exercise the commit-message checker independently of pull requests."
        ),
        "## Validation": "uv run pytest: 4 passed.",
    }

    for heading, content in contents.items():
        message = valid.replace(f"{heading}\n\n{content}", heading)
        errors = validate_message(message, label="Commit")
        assert any(
            f"Commit section {heading!r} must contain rendered alphanumeric prose" in error
            for error in errors
        )

    content_free = (
        ("## Summary", contents["## Summary"], "---"),
        ("## Summary", contents["## Summary"], "!!!"),
        ("## Summary", contents["## Summary"], "[]()"),
        ("## Investigation", contents["## Investigation"], "[parser]: https://example.com"),
        ("## Investigation", contents["## Investigation"], "```text\n```"),
        ("## Validation", contents["## Validation"], "&nbsp;"),
    )
    for heading, content, replacement in content_free:
        message = valid.replace(f"{heading}\n\n{content}", f"{heading}\n\n{replacement}")
        errors = validate_message(message, label="Commit")
        assert any(
            f"Commit section {heading!r} must contain rendered alphanumeric prose" in error
            for error in errors
        )
