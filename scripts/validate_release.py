from __future__ import annotations

import argparse
import re
import sys
import tomllib
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

from packaging.version import InvalidVersion, Version

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHANGELOG_REPOSITORY_ROOT = "https://github.com/dariuszpanas/pydantic-versions"
CHANGELOG_COMPARE_ROOT = f"{CHANGELOG_REPOSITORY_ROOT}/compare"
DEVELOPMENT_STATUS_PREFIX = "Development Status :: "
PRE_STABLE_CLASSIFIERS = frozenset(
    {
        "Development Status :: 1 - Planning",
        "Development Status :: 2 - Pre-Alpha",
        "Development Status :: 3 - Alpha",
        "Development Status :: 4 - Beta",
    }
)
STABLE_CLASSIFIER = "Development Status :: 5 - Production/Stable"


class ReleaseValidationError(ValueError):
    """Raised when release metadata is missing or inconsistent."""


def normalize_expected_version(expected: str) -> str:
    """Normalize a release version supplied as a plain version or a ``v`` tag."""
    version = expected.strip()
    if version.startswith("v"):
        version = version[1:]
    if not version:
        msg = "expected release version cannot be empty"
        raise ReleaseValidationError(msg)
    return version


def read_project_metadata(pyproject_path: Path) -> tuple[str, tuple[str, ...]]:
    """Read the release version and classifiers from a pyproject file."""
    try:
        pyproject: dict[str, Any] = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        msg = f"could not read {pyproject_path}: {exc}"
        raise ReleaseValidationError(msg) from exc

    project = pyproject.get("project")
    if not isinstance(project, dict):
        msg = f"{pyproject_path} must define a non-empty string at project.version"
        raise ReleaseValidationError(msg)

    version = project.get("version")
    if not isinstance(version, str) or not version:
        msg = f"{pyproject_path} must define a non-empty string at project.version"
        raise ReleaseValidationError(msg)

    classifiers = project.get("classifiers", [])
    if not isinstance(classifiers, list) or not all(
        isinstance(classifier, str) for classifier in classifiers
    ):
        msg = f"{pyproject_path} must define project.classifiers as a list of strings"
        raise ReleaseValidationError(msg)
    return version, tuple(classifiers)


def _reference_definitions(changelog: str, label: str) -> tuple[str, ...]:
    """Return every Markdown reference definition for ``label``."""
    pattern = re.compile(
        rf"[ \t]{{0,3}}\[{re.escape(label)}\]:.*",
        re.IGNORECASE,
    )
    return tuple(line for line in changelog.splitlines() if pattern.fullmatch(line))


def validate_changelog(changelog_path: Path, version: str) -> None:
    """Validate the finalized Keep a Changelog entry for ``version``."""
    try:
        changelog = changelog_path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"could not read {changelog_path}: {exc}"
        raise ReleaseValidationError(msg) from exc

    release_heading_pattern = re.compile(
        r"^## \[(?P<version>[^]]+)\] - (?P<date>\d{4}-\d{2}-\d{2})$",
        re.MULTILINE,
    )
    release_headings = list(release_heading_pattern.finditer(changelog))
    target_headings = [
        heading for heading in release_headings if heading.group("version") == version
    ]
    if len(target_headings) != 1:
        msg = (
            f"{changelog_path} must contain exactly one dated Keep a Changelog "
            f"heading for {version!r}"
        )
        raise ReleaseValidationError(msg)
    target_heading = target_headings[0]
    try:
        date.fromisoformat(target_heading.group("date"))
    except ValueError as exc:
        msg = f"{changelog_path} has an invalid ISO release date for {version!r}"
        raise ReleaseValidationError(msg) from exc

    unreleased_headings = list(re.finditer(r"^## \[Unreleased\]$", changelog, re.MULTILINE))
    if len(unreleased_headings) != 1:
        msg = f"{changelog_path} must contain exactly one Unreleased heading"
        raise ReleaseValidationError(msg)
    unreleased_heading = unreleased_headings[0]
    after_unreleased = changelog[unreleased_heading.end() :]
    next_heading = re.search(r"^## ", after_unreleased, re.MULTILINE)
    if next_heading is None:
        msg = f"{changelog_path} has no release entry below Unreleased"
        raise ReleaseValidationError(msg)
    if after_unreleased[: next_heading.start()].strip():
        msg = f"{changelog_path} must keep an empty Unreleased section above {version!r}"
        raise ReleaseValidationError(msg)
    next_heading_start = unreleased_heading.end() + next_heading.start()
    if next_heading_start != target_heading.start():
        msg = f"{changelog_path} must place release {version!r} directly below Unreleased"
        raise ReleaseValidationError(msg)

    unreleased_link = f"[Unreleased]: {CHANGELOG_COMPARE_ROOT}/v{version}...HEAD"
    after_target = changelog[target_heading.end() :]
    next_release_heading = re.search(r"^## ", after_target, re.MULTILINE)
    if next_release_heading is None:
        release_link = f"[{version}]: {CHANGELOG_REPOSITORY_ROOT}/releases/tag/v{version}"
    else:
        previous_heading_start = target_heading.end() + next_release_heading.start()
        previous_heading = next(
            (heading for heading in release_headings if heading.start() == previous_heading_start),
            None,
        )
        if previous_heading is None:
            msg = f"{changelog_path} has a malformed previous release heading"
            raise ReleaseValidationError(msg)
        previous_version = previous_heading.group("version")
        try:
            parsed_previous = Version(previous_version)
        except InvalidVersion as exc:
            msg = f"{changelog_path} has an invalid previous release {previous_version!r}"
            raise ReleaseValidationError(msg) from exc
        if parsed_previous >= _parse_release_version(version):
            msg = f"{changelog_path} must order releases newest first"
            raise ReleaseValidationError(msg)
        release_link = f"[{version}]: {CHANGELOG_COMPARE_ROOT}/v{previous_version}...v{version}"

    if _reference_definitions(changelog, "Unreleased") != (
        unreleased_link,
    ) or _reference_definitions(changelog, version) != (release_link,):
        msg = f"{changelog_path} has incomplete or stale comparison links for {version!r}"
        raise ReleaseValidationError(msg)


def _parse_release_version(version: str) -> Version:
    """Parse a PEP 440 release version with a release-specific error."""
    try:
        return Version(version)
    except InvalidVersion as exc:
        msg = f"project.version {version!r} is not a valid PEP 440 version"
        raise ReleaseValidationError(msg) from exc


def release_is_prerelease(version: str) -> bool:
    """Return whether a valid PEP 440 version is a pre- or development release."""
    parsed_version = _parse_release_version(version)
    return parsed_version.is_prerelease or parsed_version.is_devrelease


def validate_development_status(version: str, classifiers: Sequence[str]) -> None:
    """Require one maturity classifier consistent with the release version."""
    parsed_version = _parse_release_version(version)
    development_statuses = [
        classifier for classifier in classifiers if classifier.startswith(DEVELOPMENT_STATUS_PREFIX)
    ]
    if len(development_statuses) != 1:
        msg = "project.classifiers must contain exactly one Development Status classifier"
        raise ReleaseValidationError(msg)

    development_status = development_statuses[0]
    requires_stable_classifier = (
        parsed_version.major >= 1
        and not parsed_version.is_prerelease
        and not parsed_version.is_devrelease
    )
    if requires_stable_classifier:
        if development_status != STABLE_CLASSIFIER:
            msg = f"stable release {version!r} requires classifier {STABLE_CLASSIFIER!r}"
            raise ReleaseValidationError(msg)
    elif development_status not in PRE_STABLE_CLASSIFIERS:
        msg = (
            f"pre-stable release {version!r} requires a Development Status "
            "classifier from Planning through Beta"
        )
        raise ReleaseValidationError(msg)


def write_github_output(output_path: Path, *, version: str) -> None:
    """Append validated release metadata to a GitHub Actions output file."""
    prerelease = str(release_is_prerelease(version)).lower()
    try:
        with output_path.open("a", encoding="utf-8") as output:
            print(f"version={version}", file=output)
            print(f"prerelease={prerelease}", file=output)
    except OSError as exc:
        msg = f"could not write GitHub output file {output_path}: {exc}"
        raise ReleaseValidationError(msg) from exc


def validate_release(expected: str, *, project_root: Path = PROJECT_ROOT) -> str:
    """Validate an expected release version against project and changelog metadata."""
    expected_version = normalize_expected_version(expected)
    pyproject_path = project_root / "pyproject.toml"
    changelog_path = project_root / "docs" / "changelog.md"
    project_version, classifiers = read_project_metadata(pyproject_path)

    if expected_version != project_version:
        msg = (
            f"expected release version {expected_version!r} does not match "
            f"project.version {project_version!r}"
        )
        raise ReleaseValidationError(msg)

    validate_changelog(changelog_path, project_version)
    validate_development_status(project_version, classifiers)
    return project_version


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate release input against pyproject.toml and docs/changelog.md.",
    )
    parser.add_argument("version", help="Expected version or tag, with an optional leading 'v'.")
    parser.add_argument(
        "--github-output",
        type=Path,
        help="Append the validated version and prerelease flag to this GitHub output file.",
    )
    args = parser.parse_args(argv)

    try:
        version = validate_release(args.version)
        if args.github_output is not None:
            write_github_output(args.github_output, version=version)
    except ReleaseValidationError as exc:
        print(f"release validation failed: {exc}", file=sys.stderr)
        return 1

    print(f"release metadata is consistent for {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
