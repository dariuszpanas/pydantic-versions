from __future__ import annotations

from pathlib import Path

import pytest

from scripts.validate_release import (
    ReleaseValidationError,
    release_is_prerelease,
    validate_release,
    write_github_output,
)


def write_release_metadata(root: Path, *, project_version: str, changelog: str) -> None:
    (root / "docs").mkdir()
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "example"\nversion = "{project_version}"\n',
        encoding="utf-8",
    )
    (root / "docs" / "changelog.md").write_text(changelog, encoding="utf-8")


@pytest.mark.parametrize("expected", ["1.2.3", "v1.2.3"])
def test_validate_release_accepts_plain_version_or_tag(tmp_path: Path, expected: str) -> None:
    write_release_metadata(
        tmp_path,
        project_version="1.2.3",
        changelog="# Changelog\n\n## 1.2.3 - 2026-07-22\n\n- Added a feature.\n",
    )

    assert validate_release(expected, project_root=tmp_path) == "1.2.3"


def test_validate_release_rejects_version_mismatch(tmp_path: Path) -> None:
    write_release_metadata(
        tmp_path,
        project_version="1.2.3",
        changelog="# Changelog\n\n## 1.2.3 - 2026-07-22\n",
    )

    with pytest.raises(ReleaseValidationError, match="does not match project.version"):
        validate_release("v1.2.4", project_root=tmp_path)


def test_validate_release_requires_exact_changelog_heading(tmp_path: Path) -> None:
    write_release_metadata(
        tmp_path,
        project_version="1.2.3",
        changelog="# Changelog\n\n## 1.2.30 - 2026-07-22\n",
    )

    with pytest.raises(ReleaseValidationError, match="no level-two heading"):
        validate_release("1.2.3", project_root=tmp_path)


def test_validate_release_requires_project_version(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "example"\n',
        encoding="utf-8",
    )
    (tmp_path / "docs" / "changelog.md").write_text(
        "# Changelog\n\n## 1.2.3 - 2026-07-22\n",
        encoding="utf-8",
    )

    with pytest.raises(ReleaseValidationError, match="project.version"):
        validate_release("1.2.3", project_root=tmp_path)


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("1.2.3", False),
        ("1.2.3.post1", False),
        ("1.2.3+build", False),
        ("1.2.3a", True),
        ("1.2.3b2", True),
        ("1.2.3rc1", True),
        ("1.2.3c1", True),
        ("1.2.3-preview1", True),
        ("1.2.3.dev", True),
    ],
)
def test_release_is_prerelease_uses_pep_440(version: str, expected: bool) -> None:
    assert release_is_prerelease(version) is expected


def test_release_is_prerelease_rejects_invalid_versions() -> None:
    with pytest.raises(ReleaseValidationError, match="PEP 440"):
        release_is_prerelease("not a version")


def test_write_github_output_appends_validated_metadata(tmp_path: Path) -> None:
    output_path = tmp_path / "github-output"

    write_github_output(output_path, version="1.2.3rc1")

    assert output_path.read_text(encoding="utf-8").splitlines() == [
        "version=1.2.3rc1",
        "prerelease=true",
    ]
