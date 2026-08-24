#!/usr/bin/env python3
"""Validate descriptive Conventional Commits for pull requests and rebase auto-merge."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

_HEADER_RE = re.compile(
    r"^(?P<type>[a-z][a-z0-9-]*)(?:\((?P<scope>[^()\r\n]+)\))?(?P<breaking>!)?: (?P<summary>\S.*)$"
)
_HEADER_TITLE_ISSUE_REF_RE = re.compile(r"(^|[\s(])#\d+(?!\d)")
_ALLOWED_TYPES = frozenset(
    {
        "build",
        "chore",
        "ci",
        "docs",
        "feat",
        "fix",
        "perf",
        "refactor",
        "release",
        "revert",
        "style",
        "test",
    }
)
_MAX_LINE_LENGTH = 72
_MARKDOWN_LINK_RE = re.compile(r"\[([^]\r\n]+)\]\(https?://[^)\s]+\)", re.IGNORECASE)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_MARKDOWN_TABLE_DELIMITER_CELL_RE = re.compile(r"^:?-{3,}:?$")
_HEADING_RE = re.compile(r"^#{1,6}\s+(?P<name>\S.*?)(?:\s+#+)?$")
_SETEXT_UNDERLINE_RE = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*$")
_SETEXT_H2_UNDERLINE_RE = re.compile(r"^ {0,3}-+[ \t]*$")
_ATX_HEADING_RE = re.compile(r"^ {0,3}#{1,6}(?=$|[ \t])(?:[ \t]+.*)?$")
_ATX_H2_RE = re.compile(r"^ {0,3}##(?=$|[ \t])(?:[ \t]+.*)?$")
_FENCE_RE = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})(?P<rest>.*)$")
_THEMATIC_BREAK_RE = re.compile(r"^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$")
_LINK_REFERENCE_DEFINITION_RE = re.compile(r"^ {0,3}\[[^]\r\n]+\]:[ \t]*\S.*$")
_HTML_ENTITY_RE = re.compile(r"&(?:#[0-9]+|#x[0-9a-f]+|[a-z][a-z0-9]+);", re.IGNORECASE)
_RAW_HTML_RE = re.compile(
    r"<!--|--!?>|</?[A-Za-z][A-Za-z0-9-]*(?:[ \t/>]|$)|"
    r"<![A-Za-z][A-Za-z0-9-]*(?:[ \t>]|$)|<!\[CDATA\[|<\?",
    re.IGNORECASE,
)
_BLOCK_STRUCTURE_RE = re.compile(
    r"^(?: {4}|\t| {0,3}(?:>|#{1,6}\s|[-+*]\s+|\d+[.)]\s+|`{3,}|~{3,}))"
)
_VALIDATION_LABEL_RE = re.compile(
    r"^(?:tests?|testing|validation|verification|checks?|all checks)\s*[:=-]\s*",
    re.IGNORECASE,
)
_VALIDATION_COMMAND_RE = re.compile(
    r"^(?:"
    r"uv\s+run\s+\S+|"
    r"python\s+-m\s+(?:pytest|mypy)\b|"
    r"pytest\b|"
    r"ruff\s+(?:check|format)\b|"
    r"ty\s+check\b|"
    r"mypy\b|"
    r"make\s+(?:ci|check|test\S*|lint\S*|typecheck|docs\S*|build|format|fix)\b"
    r")"
)
_VALIDATION_RESULT_RE = re.compile(
    r"^(?:all\s+)?(?:tests?|checks?)\s+"
    r"(?:passed|succeeded|completed|are\s+green)\b",
    re.IGNORECASE,
)
_VALIDATION_STATUS_RE = re.compile(
    r"^[A-Za-z0-9_. /+()\-]+:\s*"
    r"(?:pass(?:ed)?|fail(?:ed)?|success(?:ful)?|succeeded|skipped|green|ok|not run)"
    r"(?:[.!]|\s+\([^)]*\)|\s+(?:in|on|with)\b.*)?$",
    re.IGNORECASE,
)
_VALIDATION_SECTION_NAMES = frozenset({"checks", "testing", "tests", "validation", "verification"})
_REQUIRED_SECTION_HEADINGS = (
    "## Summary",
    "## Boundaries and compatibility",
    "## Investigation",
    "## Validation",
)
_VALIDATION_OUTCOME_TRAIL_RE = re.compile(
    r"(?:\b(?:clean|completed|failed|green|ok|passed|succeeded|"
    r"successful(?:ly)?)\b|"
    r"\bno (?:errors?|issues?) found\b)"
    r"(?:[.!]|\s+\([^)]*\)|\s+(?:as|in|on|with)\b.*)?$",
    re.IGNORECASE,
)
_VALIDATION_EXPLICIT_OUTCOME_TRAIL_RE = re.compile(
    r"(?:\b(?:clean|completed|fail(?:ed|ure)?|green|ok|pass(?:ed)?|"
    r"succeed(?:ed)?|success(?:ful(?:ly)?)?)\b|"
    r"\bno (?:errors?|issues?) found\b)"
    r"(?:[.!]|\s+\([^)]*\)|\s+(?:as|in|on|with)\b.*)?$",
    re.IGNORECASE,
)
_VALIDATION_COUNT_RESULT_RE = re.compile(
    r"\b(?:exit code \d+|\d+\s+(?:deselected|errors?|failed|passed|skipped|"
    r"warnings?|xfailed|xpassed)|no failures|\d+\s+files? left unchanged)\b",
    re.IGNORECASE,
)
_VALIDATION_EXECUTED_COUNT_RE = re.compile(
    r"\b\d+\s+(?:errors?|failed|passed|xfailed|xpassed)\b",
    re.IGNORECASE,
)
_VALIDATION_MODAL_RESULT_RE = re.compile(
    r"\b(?:can|could|expected to|may|might|must|should|will|would)\s+"
    r"(?:have\s+)?(?:fail(?:ed)?|pass(?:ed)?|succeed(?:ed)?)\b",
    re.IGNORECASE,
)
_VALIDATION_SUBJECT_RE = re.compile(
    r"(?:\b(?:build|coverage|docs?|documentation|gate|lint|matrix|package|"
    r"pytest|ruff|suite|tests?|checks?|typecheck|typing)\b|\bcommit messages\b)"
    r"(?:\s+(?:across|against|for|in|on|under|with)\s+\S+(?:\s+\S+){0,7})?"
    r"(?:\s+(?:are|have been|is|was|were))?$",
    re.IGNORECASE,
)
_VALIDATION_NARRATIVE_RE = re.compile(
    r"\b(?:allegedly|describes?|documents?|explains?|if|meant|means?|perhaps|"
    r"possibly|records?|reportedly|verifies?|when|why)\b|"
    r"\b(?:earlier|historical|previous|prior)(?!-)\b",
    re.IGNORECASE,
)
_VALIDATION_LAYOUT_NARRATIVE_RE = re.compile(
    r"\b(?:brings?|ensures?|holds?|keeps?|leaves?|makes?|maintains?|"
    r"preserves?|restores?|returns?|sets?)\b",
    re.IGNORECASE,
)
_GENERIC_VALIDATION_SUBJECT_RE = re.compile(
    r"^(?:(?:all|the)\s+)?(?:tests?|checks?)$",
    re.IGNORECASE,
)
_VALIDATION_NOT_RUN_RE = re.compile(
    r"\b(?:not run|not executed|not applicable)\b(?P<suffix>.*)$",
    re.IGNORECASE,
)
_VALIDATION_SKIPPED_RE = re.compile(r"\bskipped\b(?P<suffix>.*)$", re.IGNORECASE)
_VALIDATION_REASON_RE = re.compile(
    r"^(?:(?:because|as|due to)\s+|[:;\-\N{EM DASH}]\s*|\()"
    r"(?P<reason>\S.*?)(?:\))?[.!]?$",
    re.IGNORECASE,
)
_GENERIC_VALIDATION_REASON_RE = re.compile(
    r"^(?:reasons?|not (?:applicable|needed|required)|"
    r"(?:it|this|checks?|tests?|validation)\s+(?:(?:was|were)\s+)?"
    r"(?:not run|skipped)|skipped)$",
    re.IGNORECASE,
)
_TEMPLATE_TOKEN_RE = re.compile(r"<(?:command|result|describe[^>]*)>", re.IGNORECASE)
_PLACEHOLDER_PREFIX_RE = re.compile(
    r"^(?:(?:wip|todo|tbd)(?:\s*(?::|-|\N{EM DASH})\s*|\s+)"
    r"|(?:work[ -]in[ -]progress|placeholder)\s*(?::|-|\N{EM DASH})\s*)\S",
    re.IGNORECASE,
)
_DEVELOPMENT_ONLY_RE = re.compile(
    r"\b(?:"
    r"(?:address(?:ed|es|ing)?|apply|applied|fix(?:ed|es|ing)?)\s+"
    r"(?:the\s+)?(?:latest\s+)?(?:review(?:er)?\s+)?"
    r"(?:feedback|comments?|changes)"
    r"|fix(?:ed|es|ing)?\s+(?:ci|tests?|lint)(?:\s+(?:failures?|errors?))?"
    r"|(?:ci|tests?|lint)\s+fix(?:es)?"
    r")\b",
    re.IGNORECASE,
)
_DEPENDABOT_METADATA_RECORD_RE = re.compile(r"^- dependency-name: (?P<value>\S+)$")
_DEPENDABOT_METADATA_FIELD_RE = re.compile(r"^  (?P<key>[a-z][a-z-]*): (?P<value>\S+)$")
_DEPENDABOT_REQUIRED_FIELDS = frozenset(
    {"dependency-name", "dependency-version", "dependency-type", "update-type"}
)
_DEPENDABOT_ALLOWED_FIELDS = _DEPENDABOT_REQUIRED_FIELDS | {"dependency-group"}
_DEPENDABOT_METADATA_PREFIXES = tuple(
    ["- dependency-name:"] + [f"  {field}:" for field in sorted(_DEPENDABOT_ALLOWED_FIELDS)]
)
_DEPENDABOT_SIGNOFF = "Signed-off-by: dependabot[bot] <support@github.com>"
_DEPENDABOT_SINGLE_SUMMARY_RE = re.compile(
    r"^bump (?P<dependency>\S+) from (?P<old>\S+) to (?P<new>\S+)$",
    re.IGNORECASE,
)
_DEPENDABOT_GROUP_SUMMARY_RE = re.compile(
    r"^bump the (?P<group>\S+) group"
    r"(?: across \d+ director(?:y|ies))? with (?P<count>\d+) updates$",
    re.IGNORECASE,
)
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(?:\[[ xX]\]\s+)?")
_MARKDOWN_LIST_ITEM_RE = re.compile(
    r"^(?P<indent> {0,3})(?P<marker>[-*+]|[0-9]{1,9}[.)])"
    r"[ \t]+(?:\[[ xX]\][ \t]+)?"
)
_VALIDATION_SENTENCE_BOUNDARY_RE = re.compile(r"(?:(?<=[.!?])|;)[ \t]+")
_ISSUE_TRAILER_RE = re.compile(
    r"^(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|refs?)\s+#\d+(?:\s*,\s*#\d+)*$",
    re.IGNORECASE,
)
_KNOWN_TRAILER_RE = re.compile(
    r"^(?:BREAKING CHANGE|BREAKING-CHANGE|Co-authored-by|Signed-off-by|Reviewed-by|"
    r"Acked-by|Tested-by|Reported-by|Suggested-by|Helped-by): \S",
    re.IGNORECASE,
)
_PLACEHOLDER_RE = re.compile(
    r"""
    ^(?:
        <[^>]*>
        | \[[^]]*\]
        | \.{3}
        | …
        | wip
        | work[ -]in[ -]progress
        | todo
        | tbd
        | n/?a
        | none
        | pending
        | placeholder
        | temp(?:orary)?
        | changes?
        | updates?
        | misc(?:ellaneous)?
        | more[ -]changes
        | (?:iteration|round|pass|attempt)(?:\s*\#?\d+)?
        | (?:address(?:ed|es)?|apply|applied)\s+
          (?:the\s+)?(?:latest\s+)?(?:review(?:er)?\s+)?
          (?:feedback|comments?|changes)
        | fix(?:ed|es)?\s+(?:review\s+)?(?:feedback|comments?)
        | fix(?:ed|es)?\s+(?:ci|tests?|lint)
        | (?:ci|tests?|lint)\s+fix(?:es)?
        | cleanup
        | polish
    )[.!]?
    $
    """,
    re.IGNORECASE | re.VERBOSE,
)
_MIN_BODY_WORDS = 8


def validate_header(header: str, *, label: str) -> str | None:
    """Return an actionable error for an invalid Conventional Commit header."""
    match = _HEADER_RE.fullmatch(header.strip())
    if match is None:
        return (
            f"{label} is not a Conventional Commit header: {header!r}. "
            "Expected <type>[optional scope][!]: <imperative summary>."
        )
    if match.group("type") not in _ALLOWED_TYPES:
        allowed = ", ".join(sorted(_ALLOWED_TYPES))
        return f"{label} uses unsupported type {match.group('type')!r}; use one of: {allowed}."
    summary = match.group("summary")
    if _MARKDOWN_LINK_RE.search(summary):
        return f"{label} summary must be plain text: {summary!r}. Move links and issue references to the body."
    if _HEADER_TITLE_ISSUE_REF_RE.search(summary):
        return f"{label} summary must stay issue-agnostic: {summary!r}. Put issue references in body sections instead."
    if _is_placeholder(match.group("summary")):
        return (
            f"{label} uses a development placeholder as its summary: "
            f"{match.group('summary')!r}. Describe the durable change instead."
        )
    return None


def _strip_markup(line: str) -> str:
    """Return visible prose without list markers or unwrappable destinations."""
    stripped = _MARKDOWN_LIST_ITEM_RE.sub("", line.strip()).strip()
    stripped = _BULLET_RE.sub("", stripped).strip()
    stripped = _MARKDOWN_LINK_RE.sub(lambda match: match.group(1), stripped)
    stripped = _URL_RE.sub("", stripped)
    return re.sub(r"[`*_~]+", "", stripped).strip()


def _markdown_structure(
    lines: Sequence[str],
) -> tuple[set[int], set[int], set[int], set[int]]:
    """Return visible, top-level, code-content, and raw-HTML indexes."""
    visible_indexes: set[int] = set()
    top_level_indexes: set[int] = set()
    code_content_indexes: set[int] = set()
    raw_html_indexes: set[int] = set()
    fence_character: str | None = None
    fence_length = 0

    for index, line in enumerate(lines):
        fence_match = _FENCE_RE.match(line)
        if fence_character is not None:
            if fence_match is not None:
                marker = fence_match.group("marker")
                rest = fence_match.group("rest")
                if (
                    marker[0] == fence_character
                    and len(marker) >= fence_length
                    and not rest.strip(" \t")
                ):
                    fence_character = None
                    fence_length = 0
                    continue
            visible_indexes.add(index)
            code_content_indexes.add(index)
            continue

        if fence_match is not None:
            marker = fence_match.group("marker")
            rest = fence_match.group("rest")
            if marker[0] != "`" or "`" not in rest:
                fence_character = marker[0]
                fence_length = len(marker)
                continue

        indented_code = line.startswith("    ") or re.match(r"^ {0,3}\t", line)
        if indented_code:
            visible_indexes.add(index)
            code_content_indexes.add(index)
            continue

        if _RAW_HTML_RE.search(line):
            raw_html_indexes.add(index)
            continue

        visible_indexes.add(index)
        if re.match(r"^ {0,3}>", line):
            continue
        top_level_indexes.add(index)

    return visible_indexes, top_level_indexes, code_content_indexes, raw_html_indexes


def _validation_list_item_owners(lines: Sequence[str]) -> dict[int, int]:
    """Map canonical top-level validation items and their continuations."""
    owners: dict[int, int] = {}
    active_owner: int | None = None
    setext_lines = _setext_heading_lines(lines)
    top_level_indexes = _top_level_line_indexes(lines)

    for index, line in enumerate(lines):
        raw = line.strip()
        if (
            index not in top_level_indexes
            or not raw
            or index in setext_lines
            or raw.startswith(";")
            or _HEADING_RE.fullmatch(raw)
            or _is_trailer(raw)
        ):
            active_owner = None
            continue

        if line.startswith("- "):
            active_owner = index
            owners[index] = index
            continue

        if active_owner is not None:
            owners[index] = active_owner

    return owners


def _is_placeholder(line: str) -> bool:
    """Return whether one complete line is development-only placeholder prose."""
    visible = _strip_markup(line)
    return bool(_PLACEHOLDER_RE.fullmatch(visible) or _PLACEHOLDER_PREFIX_RE.match(visible))


def _is_trailer(line: str) -> bool:
    """Return whether a line is commit metadata rather than explanatory prose."""
    stripped = line.strip()
    return bool(_ISSUE_TRAILER_RE.fullmatch(stripped) or _KNOWN_TRAILER_RE.match(stripped))


def _meaningful_lines(lines: Sequence[str]) -> list[str]:
    """Return rendered body content, excluding structure-only constructs."""
    meaningful: list[str] = []
    visible_indexes, _, code_content_indexes, _ = _markdown_structure(lines)
    setext_lines = _setext_heading_lines(lines)
    for index, line in enumerate(lines):
        if index not in visible_indexes:
            continue
        if index in code_content_indexes:
            if code := line.strip():
                meaningful.append(code)
            continue

        stripped = _strip_markup(line)
        if (
            not stripped
            or index in setext_lines
            or stripped.startswith(";")
            or _HEADING_RE.fullmatch(stripped)
            or _is_trailer(stripped)
            or _THEMATIC_BREAK_RE.fullmatch(line)
            or _LINK_REFERENCE_DEFINITION_RE.fullmatch(line)
        ):
            continue
        rendered = _RAW_HTML_RE.sub("", _HTML_ENTITY_RE.sub("", stripped))
        if re.search(r"[A-Za-z0-9]", rendered):
            meaningful.append(stripped)
    return meaningful


def _context_lines(lines: Sequence[str]) -> list[str]:
    """Return change-context prose, excluding mechanical validation evidence."""
    context: list[str] = []
    validation_section = False
    visible_indexes, top_level_indexes, _, _ = _markdown_structure(lines)
    setext_lines = _setext_heading_lines(lines)
    for index, line in enumerate(lines):
        if index not in visible_indexes:
            continue
        raw = line.strip()
        if index in setext_lines:
            if index + 1 < len(lines) and _SETEXT_UNDERLINE_RE.fullmatch(lines[index + 1]):
                validation_section = _strip_markup(raw).casefold() in _VALIDATION_SECTION_NAMES
            continue
        if index in top_level_indexes and (heading := _HEADING_RE.fullmatch(raw)):
            section_name = _strip_markup(heading.group("name")).casefold()
            validation_section = section_name in _VALIDATION_SECTION_NAMES
            continue

        visible = _strip_markup(line)
        if (
            not visible
            or visible.startswith(";")
            or _is_trailer(visible)
            or validation_section
            or _VALIDATION_LABEL_RE.match(visible)
            or _VALIDATION_COMMAND_RE.match(visible)
            or _VALIDATION_RESULT_RE.match(visible)
            or _VALIDATION_STATUS_RE.match(visible)
        ):
            continue
        context.append(visible)
    return context


def _has_explicit_not_run_reason(text: str) -> bool:
    """Require a concrete reason after a validation not-run statement."""
    match = _VALIDATION_NOT_RUN_RE.search(text)
    if match is None or not (
        reason_match := _VALIDATION_REASON_RE.match(match.group("suffix").strip())
    ):
        return False
    reason = _strip_markup(reason_match.group("reason")).strip(" ().")
    return bool(reason) and not (
        _is_placeholder(reason) or _GENERIC_VALIDATION_REASON_RE.fullmatch(reason)
    )


def _validation_blocks(
    lines: Sequence[str],
    *,
    metadata_candidate_bounds: tuple[int, int] | None,
) -> list[tuple[tuple[str, ...], bool, tuple[int, ...]]]:
    """Return top-level logical body blocks and validation-section state."""
    blocks: list[tuple[tuple[str, ...], bool, tuple[int, ...]]] = []
    parts: list[str] = []
    part_indexes: list[int] = []
    parts_are_validation = False
    validation_section = False
    setext_lines = _setext_heading_lines(lines)
    top_level_indexes = _top_level_line_indexes(lines)
    body_start = 2 if len(lines) >= 2 and lines[1] == "" else 1

    def flush() -> None:
        nonlocal part_indexes, parts
        if parts:
            blocks.append((tuple(parts), parts_are_validation, tuple(part_indexes)))
            parts = []
            part_indexes = []

    for index, line in enumerate(lines):
        raw = line.strip()
        if index < body_start:
            continue
        if index not in top_level_indexes or (
            metadata_candidate_bounds is not None
            and metadata_candidate_bounds[0] <= index <= metadata_candidate_bounds[1]
        ):
            flush()
            continue
        if index in setext_lines:
            flush()
            if index + 1 < len(lines) and _SETEXT_UNDERLINE_RE.fullmatch(lines[index + 1]):
                validation_section = _strip_markup(raw).casefold() in _VALIDATION_SECTION_NAMES
            continue
        if heading := _HEADING_RE.fullmatch(raw):
            flush()
            validation_section = (
                _strip_markup(heading.group("name")).casefold() in _VALIDATION_SECTION_NAMES
            )
            continue

        visible = _strip_markup(line)
        if not visible or visible.startswith(";") or _is_trailer(visible):
            flush()
            continue

        starts_record = bool(
            _MARKDOWN_LIST_ITEM_RE.match(line)
            or _VALIDATION_LABEL_RE.match(visible)
            or _VALIDATION_COMMAND_RE.match(visible)
        )
        if parts and (starts_record or parts_are_validation != validation_section):
            flush()
        if not parts:
            parts_are_validation = validation_section
        parts.append(visible)
        part_indexes.append(index)
    flush()
    return blocks


def _has_concrete_command_result(candidate: str, command_match: re.Match[str] | None) -> bool:
    """Accept an explicit result attached to a recognized command."""
    if command_match is None:
        return False
    result_match = re.search(r":\s+(?P<result>\S.*)$", candidate)
    if result_match is None:
        return False
    result = _strip_markup(result_match.group("result")).strip()
    return bool(
        result
        and not _is_placeholder(result)
        and not _VALIDATION_MODAL_RESULT_RE.search(result)
        and (
            _VALIDATION_COUNT_RESULT_RE.search(result)
            or _VALIDATION_EXPLICIT_OUTCOME_TRAIL_RE.search(result)
        )
    )


def _has_unstructured_validation_subject(subject: str) -> bool:
    """Reject narrative prose while accepting a concise named check or suite."""
    return bool(
        _VALIDATION_SUBJECT_RE.search(subject) and not _VALIDATION_NARRATIVE_RE.search(subject)
    )


def _is_validation_record(candidate: str, *, validation_section: bool) -> bool:
    """Return whether one logical candidate records a concrete validation result."""
    label_match = _VALIDATION_LABEL_RE.match(candidate)
    explicit_context = validation_section or label_match is not None
    if label_match:
        candidate = candidate[label_match.end() :].strip()
    command_match = _VALIDATION_COMMAND_RE.match(candidate)

    if not_run_match := _VALIDATION_NOT_RUN_RE.search(candidate):
        not_run_subject = candidate[: not_run_match.start()].strip(" :=-\N{EM DASH}")
        return bool(
            (
                explicit_context
                or command_match
                or _has_unstructured_validation_subject(not_run_subject)
            )
            and _has_explicit_not_run_reason(candidate)
        )

    if skipped_match := _VALIDATION_SKIPPED_RE.search(candidate):
        if not _VALIDATION_EXECUTED_COUNT_RE.search(candidate):
            skipped_subject = candidate[: skipped_match.start()].strip(" :=-\N{EM DASH}")
            return bool(
                (
                    explicit_context
                    or command_match
                    or _has_unstructured_validation_subject(skipped_subject)
                )
                and _has_explicit_not_run_reason(f"not run {skipped_match.group('suffix')}")
            )

    if command_match:
        return _has_concrete_command_result(candidate, command_match)

    if count_result := _VALIDATION_COUNT_RESULT_RE.search(candidate):
        subject = candidate[: count_result.start()].strip(" :=-\N{EM DASH}")
        normalized_subject = " ".join(subject.casefold().split())
        if subject and _GENERIC_VALIDATION_SUBJECT_RE.fullmatch(normalized_subject):
            return False
        return bool(explicit_context or _has_unstructured_validation_subject(subject))

    outcome = _VALIDATION_OUTCOME_TRAIL_RE.search(candidate)
    if outcome is None and explicit_context:
        outcome = _VALIDATION_EXPLICIT_OUTCOME_TRAIL_RE.search(candidate)
    if outcome is None or _VALIDATION_MODAL_RESULT_RE.search(candidate):
        return False
    subject = candidate[: outcome.start()].strip(" :=-\N{EM DASH}")
    normalized_subject = " ".join(subject.casefold().split())
    if not subject or _GENERIC_VALIDATION_SUBJECT_RE.fullmatch(normalized_subject):
        return False
    return bool(explicit_context or _has_unstructured_validation_subject(subject))


def _classify_validation_block(
    parts: Sequence[str], *, validation_section: bool
) -> tuple[bool, str]:
    """Return validation status and any descriptive prefix in a mixed block."""
    block = " ".join(parts)
    if validation_section or _VALIDATION_LABEL_RE.match(block):
        return _is_validation_record(block, validation_section=validation_section), ""

    candidate_starts = {0}
    offset = 0
    for part in parts[:-1]:
        offset += len(part) + 1
        candidate_starts.add(offset)
    candidate_starts.update(match.end() for match in re.finditer(r"(?<=[.!?])\s+", block))
    for start in sorted(candidate_starts, reverse=True):
        candidate = block[start:].strip()
        if _is_validation_record(candidate, validation_section=False):
            return True, block[:start].strip()
    return False, block


def _validation_result_indexes(
    parts: Sequence[str],
    block_indexes: Sequence[int],
    *,
    explicit_context: bool,
) -> tuple[int, ...]:
    """Return high-confidence concrete result locations in one block."""
    if not explicit_context:
        return ()

    def without_label(candidate: str) -> str:
        if label_match := _VALIDATION_LABEL_RE.match(candidate):
            return candidate[label_match.end() :].strip()
        return candidate

    def has_result_subject(candidate: str) -> bool:
        if _VALIDATION_LABEL_RE.match(candidate):
            return True
        candidate = without_label(candidate)
        if _VALIDATION_COMMAND_RE.match(candidate):
            return True
        result_matches = tuple(
            match
            for pattern in (
                _VALIDATION_NOT_RUN_RE,
                _VALIDATION_SKIPPED_RE,
                _VALIDATION_COUNT_RESULT_RE,
                _VALIDATION_OUTCOME_TRAIL_RE,
                _VALIDATION_EXPLICIT_OUTCOME_TRAIL_RE,
            )
            if (match := pattern.search(candidate)) is not None
        )
        if not result_matches:
            return False
        first_result = min(result_matches, key=lambda match: match.start())
        return bool(candidate[: first_result.start()].strip(" :=-\N{EM DASH}"))

    def is_explicit_generic_result(candidate: str) -> bool:
        candidate = without_label(candidate)
        for result_pattern in (
            _VALIDATION_COUNT_RESULT_RE,
            _VALIDATION_OUTCOME_TRAIL_RE,
            _VALIDATION_EXPLICIT_OUTCOME_TRAIL_RE,
        ):
            if result := result_pattern.search(candidate):
                subject = candidate[: result.start()].strip(" :=-\N{EM DASH}")
                return bool(
                    subject
                    and _GENERIC_VALIDATION_SUBJECT_RE.fullmatch(
                        " ".join(subject.casefold().split())
                    )
                )
        return False

    def is_concrete_result(candidate: str) -> bool:
        unlabeled = without_label(candidate)
        is_command = _VALIDATION_COMMAND_RE.match(unlabeled) is not None
        return bool(
            candidate
            and (
                is_command
                or not (
                    _VALIDATION_NARRATIVE_RE.search(candidate)
                    or _VALIDATION_LAYOUT_NARRATIVE_RE.search(candidate)
                )
            )
            and (
                _is_validation_record(candidate, validation_section=True)
                or is_explicit_generic_result(candidate)
            )
        )

    results: list[int] = []
    has_prior_result = False
    for part, index in zip(parts, block_indexes, strict=True):
        for candidate in _VALIDATION_SENTENCE_BOUNDARY_RE.split(part):
            candidate = candidate.strip()
            if is_concrete_result(candidate) and not (
                has_prior_result and not has_result_subject(candidate)
            ):
                results.append(index)
                has_prior_result = True
    if results:
        return tuple(results)

    block = " ".join(parts)
    if is_concrete_result(block):
        return (block_indexes[0],)
    return ()


def _analyze_validation(
    lines: Sequence[str],
    *,
    metadata_candidate_bounds: tuple[int, int] | None,
) -> tuple[bool, set[int], list[str], list[bool]]:
    """Return evidence state, excluded lines, context, and list-item layout."""
    has_evidence = False
    indexes: set[int] = set()
    context_prefixes: list[str] = []
    list_item_records: list[bool] = []
    list_item_owners = _validation_list_item_owners(lines)
    claimed_list_items: set[int] = set()
    previous_block_end: int | None = None
    previous_block_explicit = False
    for parts, validation_section, block_indexes in _validation_blocks(
        lines,
        metadata_candidate_bounds=metadata_candidate_bounds,
    ):
        first = parts[0]
        directly_explicit = bool(
            validation_section
            or _VALIDATION_LABEL_RE.match(first)
            or _VALIDATION_COMMAND_RE.match(first)
        )
        continues_previous_block = bool(
            previous_block_end is not None and block_indexes[0] == previous_block_end + 1
        )
        continues_explicit_block = bool(previous_block_explicit and continues_previous_block)
        explicit_context = directly_explicit or continues_explicit_block
        result_indexes = _validation_result_indexes(
            parts,
            block_indexes,
            explicit_context=explicit_context,
        )
        for result_index in result_indexes:
            owner = list_item_owners.get(result_index)
            owns_distinct_item = owner is not None and owner not in claimed_list_items
            list_item_records.append(owns_distinct_item)
            if owner is not None:
                claimed_list_items.add(owner)

        is_validation, context_prefix = _classify_validation_block(
            parts,
            validation_section=validation_section,
        )
        if is_validation:
            has_evidence = True
            indexes.update(block_indexes)
            if context_prefix:
                context_prefixes.append(context_prefix)
        previous_block_end = block_indexes[-1]
        previous_block_explicit = explicit_context
    return has_evidence, indexes, context_prefixes, list_item_records


def _word_count(lines: Sequence[str]) -> int:
    return sum(len(re.findall(r"[A-Za-z][A-Za-z0-9_-]*", line)) for line in lines)


def _normalized_prose(lines: Sequence[str]) -> str:
    visible = " ".join(_strip_markup(line) for line in lines)
    return " ".join(re.findall(r"[a-z0-9]+", visible.casefold()))


def _setext_heading_lines(lines: Sequence[str]) -> set[int]:
    """Return text and underline indexes for Setext-style headings."""
    headings: set[int] = set()
    _, top_level_indexes, _, _ = _markdown_structure(lines)
    for index in range(1, len(lines)):
        if (
            index not in top_level_indexes
            or index - 1 not in top_level_indexes
            or not _SETEXT_UNDERLINE_RE.fullmatch(lines[index])
        ):
            continue
        heading_line = lines[index - 1]
        heading = heading_line.strip()
        if (
            heading
            and not _ATX_HEADING_RE.fullmatch(heading_line)
            and not _BLOCK_STRUCTURE_RE.match(heading_line)
            and not _THEMATIC_BREAK_RE.fullmatch(heading_line)
            and not _LINK_REFERENCE_DEFINITION_RE.fullmatch(heading_line)
        ):
            headings.update({index - 1, index})
    return headings


def _without_summary_repetitions(
    context: Sequence[str], summary: str | None
) -> tuple[list[str], bool]:
    """Remove complete header-summary token sequences from joined body prose."""
    context_tokens = _normalized_prose(context).split()
    summary_tokens = _normalized_prose([summary]).split() if summary is not None else []
    if not summary_tokens:
        return context_tokens, False

    remaining: list[str] = []
    repeated = False
    index = 0
    while index < len(context_tokens):
        if context_tokens[index : index + len(summary_tokens)] == summary_tokens:
            repeated = True
            index += len(summary_tokens)
            continue
        remaining.append(context_tokens[index])
        index += 1
    return remaining, repeated


def _generated_metadata_candidate_bounds(lines: Sequence[str]) -> tuple[int, int] | None:
    """Return syntactic bounds for a generated dependency metadata block."""
    body_start = 2 if len(lines) >= 2 and lines[1] == "" else 1
    top_level_indexes = _top_level_line_indexes(lines)
    metadata_headers = [
        index
        for index in range(body_start, len(lines))
        if index in top_level_indexes and lines[index].strip() == "updated-dependencies:"
    ]
    if len(metadata_headers) != 1:
        return None

    metadata_header = metadata_headers[0]
    metadata_start = metadata_header - 1
    if (
        metadata_start < body_start
        or metadata_start not in top_level_indexes
        or lines[metadata_start].strip() != "---"
    ):
        return None

    metadata_ends = [
        index
        for index in range(metadata_header + 1, len(lines))
        if index in top_level_indexes and lines[index].strip() == "..."
    ]
    if len(metadata_ends) != 1:
        return None
    metadata_end = metadata_ends[0]

    return metadata_start, metadata_end


def _generated_metadata_records(
    lines: Sequence[str], bounds: tuple[int, int] | None
) -> list[dict[str, str]] | None:
    """Parse complete, recognized records from a candidate metadata block."""
    if bounds is None:
        return None
    metadata_start, metadata_end = bounds
    metadata_lines = list(lines[metadata_start + 2 : metadata_end])
    if not metadata_lines:
        return None

    records: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in metadata_lines:
        if record_match := _DEPENDABOT_METADATA_RECORD_RE.fullmatch(line):
            if current is not None:
                records.append(current)
            current = {"dependency-name": record_match.group("value")}
            continue
        if current is None or not (field_match := _DEPENDABOT_METADATA_FIELD_RE.fullmatch(line)):
            return None
        key = field_match.group("key")
        if key not in _DEPENDABOT_ALLOWED_FIELDS or key in current:
            return None
        current[key] = field_match.group("value")
    if current is not None:
        records.append(current)
    if not records or any(not _DEPENDABOT_REQUIRED_FIELDS <= record.keys() for record in records):
        return None

    return records


def _generated_metadata_bounds(lines: Sequence[str]) -> tuple[int, int] | None:
    """Return bounds only for a complete, recognized Dependabot metadata block."""
    bounds = _generated_metadata_candidate_bounds(lines)
    if _generated_metadata_records(lines, bounds) is None:
        return None
    assert bounds is not None
    trailers = [line for line in lines[bounds[1] + 1 :] if line.strip()]
    return bounds if trailers == [_DEPENDABOT_SIGNOFF] else None


def _top_level_line_indexes(lines: Sequence[str]) -> set[int]:
    """Return lines outside fenced, quoted, and indented code examples."""
    return _markdown_structure(lines)[1]


def _raw_html_errors(lines: Sequence[str], *, label: str) -> list[str]:
    """Reject ambiguous raw HTML in the body except inside code blocks."""
    body_start = 2 if len(lines) >= 2 and lines[1] == "" else 1
    *_, raw_html_indexes = _markdown_structure(lines)
    body_html_indexes = raw_html_indexes & set(range(body_start, len(lines)))
    if not body_html_indexes:
        return []
    line_number = min(body_html_indexes) + 1
    return [
        f"{label} line {line_number} contains raw HTML outside a fenced or "
        "indented code block. Put literal HTML examples in fenced code."
    ]


def _required_section_errors(lines: Sequence[str], *, label: str) -> list[str]:
    """Require the repository's durable four-section commit record."""
    body_start = 2 if len(lines) >= 2 and lines[1] == "" else 1
    _, top_level_indexes, _, _ = _markdown_structure(lines)
    setext_lines = _setext_heading_lines(lines)
    headings: list[tuple[int, str]] = []
    for index in range(body_start, len(lines)):
        if index not in top_level_indexes:
            continue
        if _ATX_H2_RE.fullmatch(lines[index]):
            headings.append((index, lines[index]))
        elif (
            index in setext_lines
            and index + 1 in setext_lines
            and _SETEXT_H2_UNDERLINE_RE.fullmatch(lines[index + 1])
        ):
            headings.append((index, f"{lines[index].strip()}\n{lines[index + 1].strip()}"))
    heading_text = tuple(heading for _, heading in headings)
    if heading_text != _REQUIRED_SECTION_HEADINGS:
        expected = ", ".join(repr(heading) for heading in _REQUIRED_SECTION_HEADINGS)
        found = ", ".join(repr(heading) for heading in heading_text) or "none"
        return [
            f"{label} must contain exactly these second-level headings once each "
            f"and in order: {expected}. Found: {found}."
        ]

    errors: list[str] = []
    if any(line.strip() for line in lines[body_start : headings[0][0]]):
        errors.append(f"{label} body must begin with '## Summary'; remove the nonblank preamble.")
    for position, (start, heading) in enumerate(headings):
        end = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
        if not _meaningful_lines(lines[start + 1 : end]):
            errors.append(
                f"{label} section {heading!r} must contain rendered alphanumeric "
                "prose or nonempty fenced or indented code."
            )
    return errors


def _contains_generated_metadata_shape(lines: Sequence[str]) -> bool:
    """Return whether commit text contains dependency metadata-shaped lines."""
    top_level_indexes = _top_level_line_indexes(lines)
    return any(
        lines[index].strip() == "updated-dependencies:"
        or lines[index].startswith(_DEPENDABOT_METADATA_PREFIXES)
        for index in top_level_indexes
    )


def _split_markdown_table_row(line: str) -> list[str] | None:
    """Split a GFM table row on unescaped pipes, including one-cell rows."""
    stripped = line.strip()
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    saw_pipe = False
    for character in stripped:
        if escaped:
            current.append(character)
            escaped = False
        elif character == "\\":
            current.append(character)
            escaped = True
        elif character == "|":
            cells.append("".join(current).strip())
            current = []
            saw_pipe = True
        else:
            current.append(character)
    cells.append("".join(current).strip())
    if not saw_pipe:
        return None
    if cells and not cells[0]:
        cells.pop(0)
    if cells and not cells[-1]:
        cells.pop()
    return cells or None


def _markdown_table_lines(lines: Sequence[str]) -> set[int]:
    """Return line indexes belonging to complete GFM-style tables."""
    table_lines: set[int] = set()
    for index in range(1, len(lines)):
        header_cells = _split_markdown_table_row(lines[index - 1])
        delimiter_cells = _split_markdown_table_row(lines[index])
        if (
            header_cells is None
            or delimiter_cells is None
            or len(header_cells) != len(delimiter_cells)
            or not all(
                _MARKDOWN_TABLE_DELIMITER_CELL_RE.fullmatch(cell) for cell in delimiter_cells
            )
        ):
            continue
        table_lines.update({index - 1, index})
        row_index = index + 1
        while row_index < len(lines):
            if _BLOCK_STRUCTURE_RE.match(lines[row_index]):
                break
            row_cells = _split_markdown_table_row(lines[row_index])
            if row_cells is None:
                break
            table_lines.add(row_index)
            row_index += 1
    return table_lines


def _has_generated_dependency_header(header: str, records: Sequence[dict[str, str]] | None) -> bool:
    """Recognize a canonical Dependabot header backed by validated metadata."""
    if records is None or not records:
        return False
    header_match = _HEADER_RE.fullmatch(header.strip())
    if (
        header_match is None
        or header_match.group("type") != "chore"
        or header_match.group("scope") not in {"actions", "deps"}
        or header_match.group("breaking") is not None
    ):
        return False
    summary = header_match.group("summary")

    if single_match := _DEPENDABOT_SINGLE_SUMMARY_RE.fullmatch(summary):
        if len(records) != 1:
            return False
        record = records[0]
        return (
            _normalized_prose([single_match.group("dependency")])
            == _normalized_prose([record["dependency-name"]])
            and single_match.group("new").casefold() == record["dependency-version"].casefold()
        )

    if group_match := _DEPENDABOT_GROUP_SUMMARY_RE.fullmatch(summary):
        group = group_match.group("group").casefold()
        return len(records) == int(group_match.group("count")) and all(
            record.get("dependency-group", "").casefold() == group for record in records
        )
    return False


def _validate_descriptive_body(
    lines: Sequence[str],
    *,
    label: str,
    summary: str | None,
    metadata_candidate_bounds: tuple[int, int] | None,
    validation_indexes: set[int],
    validation_context_prefixes: Sequence[str],
) -> list[str]:
    """Require useful historical context without prescribing Markdown sections."""
    errors: list[str] = []
    if len(lines) < 2 or lines[1] != "":
        errors.append(f"{label} must separate its header and body with a blank line.")

    body_start = 2 if len(lines) >= 2 and lines[1] == "" else 1
    full_body = list(lines[body_start:])
    body = [
        "" if index in validation_indexes else line
        for index, line in enumerate(lines[body_start:], start=body_start)
        if metadata_candidate_bounds is None
        or not metadata_candidate_bounds[0] <= index <= metadata_candidate_bounds[1]
    ]
    placeholder_body = [
        line
        for index, line in enumerate(full_body, start=body_start)
        if metadata_candidate_bounds is None
        or index
        not in {
            metadata_candidate_bounds[0],
            metadata_candidate_bounds[0] + 1,
            metadata_candidate_bounds[1],
        }
    ]
    placeholder_indexes = _top_level_line_indexes(placeholder_body)
    meaningful = _meaningful_lines(
        [
            line if index in placeholder_indexes else ""
            for index, line in enumerate(placeholder_body)
        ]
    )
    placeholders = [
        line for line in meaningful if _is_placeholder(line) or _TEMPLATE_TOKEN_RE.search(line)
    ]
    context = [*_context_lines(body), *validation_context_prefixes]
    joined_context = " ".join(context)
    if joined_context and _is_placeholder(joined_context):
        placeholders.append(joined_context)
    if placeholders:
        errors.append(
            f"{label} body contains placeholder content {placeholders[0]!r}. "
            "Describe the durable change and its context instead."
        )

    scored_context = [_DEVELOPMENT_ONLY_RE.sub(" ", " ".join(context))]
    descriptive_tokens, repeated_summary = _without_summary_repetitions(scored_context, summary)
    descriptive_word_count = _word_count([" ".join(descriptive_tokens)])
    if repeated_summary and descriptive_word_count < _MIN_BODY_WORDS:
        errors.append(
            f"{label} body repeats the header summary without enough additional context. "
            "Explain enough context to understand the change without reading its diff."
        )
    elif descriptive_word_count < _MIN_BODY_WORDS:
        errors.append(
            f"{label} body must contain meaningful context "
            f"({_MIN_BODY_WORDS} or more prose words outside headings, validation, "
            "metadata, and trailers)."
        )
    return errors


def _line_length_errors(
    lines: Sequence[str],
    *,
    label: str,
    metadata_bounds: tuple[int, int] | None = None,
) -> list[str]:
    """Validate wrappable prose while tolerating mechanical Markdown structures."""
    errors: list[str] = []
    table_lines = _markdown_table_lines(lines)
    for index, line in enumerate(lines):
        line_number = index + 1
        if metadata_bounds is not None and metadata_bounds[0] <= index <= metadata_bounds[1]:
            continue

        if index in table_lines or _is_trailer(line):
            continue

        measured = _MARKDOWN_LINK_RE.sub(lambda match: match.group(1), line)
        measured = _URL_RE.sub("", measured)
        if len(measured) > _MAX_LINE_LENGTH:
            errors.append(
                f"{label} line {line_number} exceeds {_MAX_LINE_LENGTH} characters "
                f"({len(measured)} visible characters). "
                "Wrap commit-message prose for narrow terminals."
            )
    return errors


def validate_message(
    message: str,
    *,
    label: str,
    allow_generated_dependency: bool = False,
) -> list[str]:
    """Validate a full commit message, including its explanatory body."""
    lines = message.strip().splitlines()
    if not lines:
        return [f"{label} is empty; provide a Conventional Commit header and descriptive body."]

    errors: list[str] = []
    header_match = _HEADER_RE.fullmatch(lines[0].strip())
    if error := validate_header(lines[0], label=label):
        errors.append(error)
    summary = header_match.group("summary") if header_match is not None else None
    metadata_candidate_bounds = _generated_metadata_candidate_bounds(lines)
    metadata_bounds = _generated_metadata_bounds(lines)
    metadata_records = _generated_metadata_records(lines, metadata_bounds)
    generated_dependency_message = (
        allow_generated_dependency
        and metadata_bounds is not None
        and _has_generated_dependency_header(lines[0], metadata_records)
    )
    if (metadata_candidate_bounds is not None and metadata_bounds is None) or (
        metadata_candidate_bounds is None and _contains_generated_metadata_shape(lines)
    ):
        errors.append(
            f"{label} contains an unrecognized generated dependency metadata block. "
            "Use complete Dependabot fields with single-token values and its signed-off trailer."
        )
    if generated_dependency_message:
        return errors

    (
        validation_evidence,
        validation_indexes,
        validation_context_prefixes,
        validation_list_item_records,
    ) = _analyze_validation(lines, metadata_candidate_bounds=metadata_candidate_bounds)
    errors.extend(_raw_html_errors(lines, label=label))
    errors.extend(_required_section_errors(lines, label=label))
    errors.extend(
        _validate_descriptive_body(
            lines,
            label=label,
            summary=summary,
            metadata_candidate_bounds=metadata_candidate_bounds,
            validation_indexes=validation_indexes,
            validation_context_prefixes=validation_context_prefixes,
        )
    )
    if not validation_evidence:
        errors.append(
            f"{label} must record validation evidence or a specific reason validation "
            "was not run. Include a result such as `uv run make ci: passed` or "
            "`Validation: not run because this changes documentation only`."
        )
    if len(validation_list_item_records) > 1 and not all(validation_list_item_records):
        errors.append(
            f"{label} records multiple independent validation commands or results "
            "outside separate top-level `- ` list items. Put each independent result "
            "in its own item under a `## Validation` heading so rendered history "
            "keeps the results separate."
        )
    errors.extend(
        _line_length_errors(
            lines,
            label=label,
            metadata_bounds=metadata_bounds,
        )
    )
    return errors


def _messages_from_git(commit_range: str) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "log", "--format=%B%x1e", "--no-merges", commit_range],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or "git log failed"
        raise RuntimeError(f"Unable to inspect commit range {commit_range!r}: {detail}") from exc
    return [message.strip() for message in result.stdout.split("\x1e") if message.strip()]


def _messages_from_file(path: str) -> list[str]:
    """Read one full commit message from a file."""
    try:
        contents = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Unable to read commit message file {path!r}: {exc}") from exc
    message = contents.strip()
    return [message] if message else []


def _messages_from_json_file(path: str) -> list[str]:
    """Read a JSON array of full commit messages for newline-safe transport."""
    try:
        values = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unable to read commit message file {path!r}: {exc}") from exc
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise RuntimeError(f"Commit message file {path!r} must contain a JSON string array")
    return [value for value in values if value.strip()]


def validate(
    *,
    title: str | None,
    commits: Sequence[str],
    commit_range: str | None,
    commit_file: str | None = None,
    commit_json_file: str | None = None,
    allow_generated_dependency: bool = False,
) -> list[str]:
    """Validate a PR title and/or commit headers and return all errors."""
    messages = list(commits)
    if commit_range is not None:
        messages.extend(_messages_from_git(commit_range))
    if commit_file is not None:
        messages.extend(_messages_from_file(commit_file))
    if commit_json_file is not None:
        messages.extend(_messages_from_json_file(commit_json_file))

    errors: list[str] = []
    if title is not None:
        if error := validate_header(title, label="PR title"):
            errors.append(error)
    if not messages:
        errors.append("No commit headers were found to validate.")
    for index, message in enumerate(messages, start=1):
        errors.extend(
            validate_message(
                message,
                label=f"Commit {index}",
                allow_generated_dependency=allow_generated_dependency,
            )
        )
    return errors


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", help="Pull request title to validate.")
    parser.add_argument(
        "--commit",
        action="append",
        default=[],
        help="Full commit message to validate; may be supplied multiple times.",
    )
    parser.add_argument(
        "--range",
        dest="commit_range",
        help="Git revision range whose non-merge commit messages should be validated.",
    )
    parser.add_argument(
        "--commit-file",
        help="File containing one full commit message.",
    )
    parser.add_argument(
        "--commit-json-file",
        help="JSON file containing an array of full commit messages.",
    )
    parser.add_argument(
        "--allow-generated-dependency",
        action="store_true",
        help="Trust metadata-backed dependency messages from an authenticated bot event.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        errors = validate(
            title=args.title,
            commits=args.commit,
            commit_range=args.commit_range,
            commit_file=args.commit_file,
            commit_json_file=args.commit_json_file,
            allow_generated_dependency=args.allow_generated_dependency,
        )
    except RuntimeError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    if errors:
        for error in errors:
            print(f"::error::{error}", file=sys.stderr)
        return 1
    print("Conventional Commit validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
