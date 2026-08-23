from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import metadata as distribution_metadata
from importlib.metadata import version as distribution_version

import pydantic_versions


def test_package_exports_version() -> None:
    assert pydantic_versions.__version__ == distribution_version("pydantic-versions")


def test_installed_package_reports_stable_maturity() -> None:
    classifiers = distribution_metadata("pydantic-versions").get_all("Classifier") or []

    assert "Development Status :: 5 - Production/Stable" in classifiers


def test_package_version_falls_back_when_distribution_is_missing(monkeypatch) -> None:
    def missing_version(distribution: str) -> str:
        raise PackageNotFoundError(distribution)

    monkeypatch.setattr(pydantic_versions, "version", missing_version)

    assert pydantic_versions._package_version() == "0.0.0"
