from __future__ import annotations

from importlib.metadata import PackageNotFoundError

import pydantic_versions


def test_package_exports_version() -> None:
    assert isinstance(pydantic_versions.__version__, str)


def test_package_version_falls_back_when_distribution_is_missing(monkeypatch) -> None:
    def missing_version(distribution: str) -> str:
        raise PackageNotFoundError(distribution)

    monkeypatch.setattr(pydantic_versions, "version", missing_version)

    assert pydantic_versions._package_version() == "0.0.0"
