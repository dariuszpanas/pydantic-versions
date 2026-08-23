from __future__ import annotations

from pathlib import Path

import pytest

from scripts.validate_release import (
    ReleaseValidationError,
    release_is_prerelease,
    validate_release,
    write_github_output,
)

STABLE_CLASSIFIER = "Development Status :: 5 - Production/Stable"
BETA_CLASSIFIER = "Development Status :: 4 - Beta"
CHANGELOG_COMPARE_ROOT = "https://github.com/dariuszpanas/pydantic-versions/compare"


def write_release_metadata(
    root: Path,
    *,
    project_version: str,
    changelog: str,
    classifiers: tuple[str, ...] = (STABLE_CLASSIFIER,),
    previous_version: str = "0.0.0",
) -> None:
    (root / "docs").mkdir()
    classifier_lines = "\n".join(f'    "{classifier}",' for classifier in classifiers)
    release_section = changelog.removeprefix("# Changelog\n\n")
    finalized_changelog = (
        "# Changelog\n\n## [Unreleased]\n\n"
        f"{release_section.rstrip()}\n\n"
        f"## [{previous_version}] - 2020-01-01\n\n- Previous release.\n\n"
        f"[Unreleased]: {CHANGELOG_COMPARE_ROOT}/v{project_version}...HEAD\n"
        f"[{project_version}]: "
        f"{CHANGELOG_COMPARE_ROOT}/v{previous_version}...v{project_version}\n"
    )
    (root / "pyproject.toml").write_text(
        (
            f'[project]\nname = "example"\nversion = "{project_version}"\n'
            f"classifiers = [\n{classifier_lines}\n]\n"
        ),
        encoding="utf-8",
    )
    (root / "docs" / "changelog.md").write_text(finalized_changelog, encoding="utf-8")


@pytest.mark.parametrize("expected", ["1.2.3", "v1.2.3"])
def test_validate_release_accepts_plain_version_or_tag(tmp_path: Path, expected: str) -> None:
    write_release_metadata(
        tmp_path,
        project_version="1.2.3",
        changelog="# Changelog\n\n## [1.2.3] - 2026-07-22\n\n- Added a feature.\n",
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

    with pytest.raises(ReleaseValidationError, match="exactly one dated Keep a Changelog heading"):
        validate_release("1.2.3", project_root=tmp_path)


def test_validate_release_requires_valid_changelog_date(tmp_path: Path) -> None:
    write_release_metadata(
        tmp_path,
        project_version="1.2.3",
        changelog="# Changelog\n\n## [1.2.3] - 2026-02-30\n",
    )

    with pytest.raises(ReleaseValidationError, match="invalid ISO release date"):
        validate_release("1.2.3", project_root=tmp_path)


def test_validate_release_requires_empty_unreleased_section(tmp_path: Path) -> None:
    write_release_metadata(
        tmp_path,
        project_version="1.2.3",
        changelog="# Changelog\n\n## [1.2.3] - 2026-08-23\n",
    )
    changelog_path = tmp_path / "docs" / "changelog.md"
    changelog_path.write_text(
        changelog_path.read_text(encoding="utf-8").replace(
            "## [Unreleased]\n\n",
            "## [Unreleased]\n\n### Added\n- Pending.\n\n",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ReleaseValidationError, match="empty Unreleased section"):
        validate_release("1.2.3", project_root=tmp_path)


def test_validate_release_accepts_whitespace_only_unreleased_section(tmp_path: Path) -> None:
    write_release_metadata(
        tmp_path,
        project_version="1.2.3",
        changelog="# Changelog\n\n## [1.2.3] - 2026-08-23\n",
    )
    changelog_path = tmp_path / "docs" / "changelog.md"
    changelog_path.write_text(
        changelog_path.read_text(encoding="utf-8").replace(
            "## [Unreleased]\n\n",
            "## [Unreleased]\n \n\t\n",
        ),
        encoding="utf-8",
    )

    assert validate_release("1.2.3", project_root=tmp_path) == "1.2.3"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("v1.2.3...HEAD", "v1.2.2...HEAD"),
        ("v0.0.0...v1.2.3", "v1.2.3...v1.2.3"),
        ("v0.0.0...v1.2.3", "v0.0.1...v1.2.3"),
    ],
)
def test_validate_release_requires_current_comparison_links(
    tmp_path: Path,
    old: str,
    new: str,
) -> None:
    write_release_metadata(
        tmp_path,
        project_version="1.2.3",
        changelog="# Changelog\n\n## [1.2.3] - 2026-08-23\n",
    )
    changelog_path = tmp_path / "docs" / "changelog.md"
    changelog_path.write_text(
        changelog_path.read_text(encoding="utf-8").replace(old, new),
        encoding="utf-8",
    )

    with pytest.raises(ReleaseValidationError, match="incomplete or stale comparison links"):
        validate_release("1.2.3", project_root=tmp_path)


@pytest.mark.parametrize(
    "stale_definition",
    [
        f"[Unreleased]: {CHANGELOG_COMPARE_ROOT}/v1.2.2...HEAD",
        f"[1.2.3]: {CHANGELOG_COMPARE_ROOT}/v1.2.1...v1.2.3",
    ],
)
def test_validate_release_rejects_duplicate_comparison_link_definitions(
    tmp_path: Path,
    stale_definition: str,
) -> None:
    write_release_metadata(
        tmp_path,
        project_version="1.2.3",
        changelog="# Changelog\n\n## [1.2.3] - 2026-08-23\n",
    )
    changelog_path = tmp_path / "docs" / "changelog.md"
    changelog_path.write_text(
        f"{changelog_path.read_text(encoding='utf-8')}\n{stale_definition}\n",
        encoding="utf-8",
    )

    with pytest.raises(ReleaseValidationError, match="incomplete or stale comparison links"):
        validate_release("1.2.3", project_root=tmp_path)


def test_validate_release_requires_newest_first_changelog_order(tmp_path: Path) -> None:
    write_release_metadata(
        tmp_path,
        project_version="1.2.3",
        changelog="# Changelog\n\n## [1.2.3] - 2026-08-23\n",
        previous_version="2.0.0",
    )

    with pytest.raises(ReleaseValidationError, match="newest first"):
        validate_release("1.2.3", project_root=tmp_path)


@pytest.mark.parametrize(
    ("heading", "error"),
    [
        ("## [Unreleased]", "exactly one Unreleased heading"),
        ("## [1.2.3] - 2026-08-23", "exactly one dated Keep a Changelog heading"),
    ],
)
def test_validate_release_rejects_duplicate_changelog_headings(
    tmp_path: Path,
    heading: str,
    error: str,
) -> None:
    write_release_metadata(
        tmp_path,
        project_version="1.2.3",
        changelog="# Changelog\n\n## [1.2.3] - 2026-08-23\n",
    )
    changelog_path = tmp_path / "docs" / "changelog.md"
    changelog_path.write_text(
        f"{heading}\n\n" + changelog_path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    with pytest.raises(ReleaseValidationError, match=error):
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
    ("version", "classifier"),
    [
        ("0.3.0", BETA_CLASSIFIER),
        ("1.0.0", STABLE_CLASSIFIER),
        ("1.0.0.post1", STABLE_CLASSIFIER),
        ("1.0.0rc1", BETA_CLASSIFIER),
        ("1.0.0.dev1", "Development Status :: 3 - Alpha"),
    ],
)
def test_validate_release_accepts_matching_maturity_classifier(
    tmp_path: Path,
    version: str,
    classifier: str,
) -> None:
    write_release_metadata(
        tmp_path,
        project_version=version,
        changelog=f"# Changelog\n\n## [{version}] - 2026-08-23\n",
        classifiers=(classifier,),
    )

    assert validate_release(version, project_root=tmp_path) == version


@pytest.mark.parametrize(
    ("version", "classifier", "error"),
    [
        ("1.0.0", BETA_CLASSIFIER, "stable release"),
        ("0.3.0", STABLE_CLASSIFIER, "pre-stable release"),
        ("1.0.0rc1", STABLE_CLASSIFIER, "pre-stable release"),
        ("1.0.0", "Development Status :: 6 - Mature", "stable release"),
        ("0.3.0", "Development Status :: 7 - Inactive", "pre-stable release"),
    ],
)
def test_validate_release_rejects_mismatched_maturity_classifier(
    tmp_path: Path,
    version: str,
    classifier: str,
    error: str,
) -> None:
    write_release_metadata(
        tmp_path,
        project_version=version,
        changelog=f"# Changelog\n\n## [{version}] - 2026-08-23\n",
        classifiers=(classifier,),
    )

    with pytest.raises(ReleaseValidationError, match=error):
        validate_release(version, project_root=tmp_path)


@pytest.mark.parametrize(
    "classifiers",
    [
        (),
        (BETA_CLASSIFIER, STABLE_CLASSIFIER),
        (STABLE_CLASSIFIER, STABLE_CLASSIFIER),
    ],
)
def test_validate_release_requires_one_development_status(
    tmp_path: Path,
    classifiers: tuple[str, ...],
) -> None:
    write_release_metadata(
        tmp_path,
        project_version="1.0.0",
        changelog="# Changelog\n\n## [1.0.0] - 2026-08-23\n",
        classifiers=classifiers,
    )

    with pytest.raises(ReleaseValidationError, match="exactly one Development Status"):
        validate_release("1.0.0", project_root=tmp_path)


@pytest.mark.parametrize("classifiers", ["[1]", '"not a list"'])
def test_validate_release_requires_classifier_list_of_strings(
    tmp_path: Path,
    classifiers: str,
) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "example"\nversion = "1.0.0"\nclassifiers = {classifiers}\n',
        encoding="utf-8",
    )
    (tmp_path / "docs" / "changelog.md").write_text(
        "# Changelog\n\n## [1.0.0] - 2026-08-23\n",
        encoding="utf-8",
    )

    with pytest.raises(ReleaseValidationError, match="list of strings"):
        validate_release("1.0.0", project_root=tmp_path)


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


@pytest.mark.parametrize(
    ("version", "prerelease"),
    [("0.3.0", "false"), ("1.2.3rc1", "true")],
)
def test_write_github_output_appends_validated_metadata(
    tmp_path: Path,
    version: str,
    prerelease: str,
) -> None:
    output_path = tmp_path / "github-output"

    write_github_output(output_path, version=version)

    assert output_path.read_text(encoding="utf-8").splitlines() == [
        f"version={version}",
        f"prerelease={prerelease}",
    ]
