from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version


def _package_version(distribution: str = "pydantic-versions") -> str:
    try:
        return version(distribution)
    except PackageNotFoundError:
        return "0.0.0"


__version__ = _package_version()

__all__ = ["__version__"]
