import re
from pathlib import Path

from scripts.check_conventional_commits import _ALLOWED_TYPES, validate_header, validate_message

ROOT = Path(__file__).parents[2]
LAYOUT_ERROR = [
    "Commit records multiple validation results outside separate top-level "
    "`- ` list items. Put each result in its own item under a `## Validation` "
    "heading so rendered history keeps the results separate."
]


def _policy_message(validation: str) -> str:
    return f"""\
refactor: preserve validation evidence structure

Keep commit history readable across Git and rendered GitHub views while
retaining durable compatibility context for every logical change.

{validation}
"""


def test_documented_commit_types_match_the_checker() -> None:
    guide = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    documented_section = guide.split("Common types are ", maxsplit=1)[1].split(
        ". Keep the summary", maxsplit=1
    )[0]

    assert frozenset(re.findall(r"`([^`]+)`", documented_section)) == _ALLOWED_TYPES


def test_release_is_an_accepted_commit_type() -> None:
    assert validate_header("release: publish stable package", label="Commit") is None


def test_single_inline_validation_result_remains_valid() -> None:
    message = """\
docs: clarify commit validation policy

Explain how durable commit messages should present validation evidence.
Keep the complete template optional for small documentation changes.

Validation: uv run pytest: 4 passed.
"""

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

Keep commit history readable when a material change records results from
multiple independent quality gates and compatibility environments.

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

- Preserve concise unstructured messages for genuinely small changes.

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
        "## Validation\n\n \t- Python tests: passed.\n \t- Strict docs: passed.",
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
        """\
## Validation

<!--
- hidden owner one
-->
`uv run pytest`: 859 passed.
<!--
- hidden owner two
-->
`uv run make docs`: passed.
""",
    )

    for validation in candidates:
        assert validate_message(_policy_message(validation), label="Commit") == LAYOUT_ERROR


def test_indented_code_is_not_accepted_as_a_validation_list() -> None:
    validation = """\
## Validation

    - Python tests: passed.
    - Strict docs: passed.
"""

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

Validation: uv run pytest: 859 passed.
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


def test_valid_dependabot_message_keeps_generated_summary_lines() -> None:
    message = """\
chore(actions): bump the github-actions group with 3 updates

Bumps the github-actions group with 3 updates: [actions/checkout](https://github.com/actions/checkout), [astral-sh/setup-uv](https://github.com/astral-sh/setup-uv) and [pypa/gh-action-pypi-publish](https://github.com/pypa/gh-action-pypi-publish).

Updates `actions/checkout` from 7.0.0 to 7.0.1
- [Release notes](https://github.com/actions/checkout/releases)
- [Changelog](https://github.com/actions/checkout/blob/main/CHANGELOG.md)
- [Commits](https://github.com/actions/checkout/compare/v7...3d3c42e5aac5ba805825da76410c181273ba90b1)

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

    assert validate_message(message, label="Dependabot commit") == []
