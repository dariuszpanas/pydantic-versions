from __future__ import annotations

import argparse
import re
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from packaging.version import InvalidVersion, Version

PROJECT_ROOT = Path(__file__).resolve().parents[1]


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


def read_project_version(pyproject_path: Path) -> str:
    """Read ``project.version`` from a pyproject file."""
    try:
        pyproject: dict[str, Any] = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        msg = f"could not read {pyproject_path}: {exc}"
        raise ReleaseValidationError(msg) from exc

    project = pyproject.get("project")
    version = project.get("version") if isinstance(project, dict) else None
    if not isinstance(version, str) or not version:
        msg = f"{pyproject_path} must define a non-empty string at project.version"
        raise ReleaseValidationError(msg)
    return version


def changelog_has_version(changelog_path: Path, version: str) -> bool:
    """Return whether the changelog has a level-two heading for ``version``."""
    try:
        changelog = changelog_path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"could not read {changelog_path}: {exc}"
        raise ReleaseValidationError(msg) from exc

    heading = re.compile(rf"^##[ \t]+{re.escape(version)}(?:[ \t]+.*)?$", re.MULTILINE)
    return heading.search(changelog) is not None


def release_is_prerelease(version: str) -> bool:
    """Return whether a valid PEP 440 version is a pre- or development release."""
    try:
        parsed_version = Version(version)
    except InvalidVersion as exc:
        msg = f"project.version {version!r} is not a valid PEP 440 version"
        raise ReleaseValidationError(msg) from exc
    return parsed_version.is_prerelease or parsed_version.is_devrelease


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
    project_version = read_project_version(pyproject_path)

    if expected_version != project_version:
        msg = (
            f"expected release version {expected_version!r} does not match "
            f"project.version {project_version!r}"
        )
        raise ReleaseValidationError(msg)

    if not changelog_has_version(changelog_path, project_version):
        msg = f"{changelog_path} has no level-two heading for version {project_version!r}"
        raise ReleaseValidationError(msg)

    release_is_prerelease(project_version)
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
