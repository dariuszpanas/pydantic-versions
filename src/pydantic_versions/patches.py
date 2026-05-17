from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FieldDefault:
    name: str
    default: Any = None
    default_factory: Callable[[], Any] | None = None
    has_default: bool = True


@dataclass(frozen=True)
class FieldRemoved:
    name: str


@dataclass(frozen=True)
class FieldRenamed:
    current_name: str
    version_name: str


VersionPatch = FieldDefault | FieldRemoved | FieldRenamed


_MISSING = object()


def field_default(
    name: str,
    default: Any = _MISSING,
    *,
    default_factory: Callable[[], Any] | None = None,
) -> FieldDefault:
    if default is not _MISSING and default_factory is not None:
        msg = "field_default() accepts either default or default_factory, not both"
        raise ValueError(msg)
    if default is _MISSING and default_factory is None:
        msg = "field_default() requires default or default_factory"
        raise ValueError(msg)
    return FieldDefault(
        name=name,
        default=None if default is _MISSING else default,
        default_factory=default_factory,
        has_default=default is not _MISSING,
    )


def field_removed(name: str) -> FieldRemoved:
    return FieldRemoved(name=name)


def field_renamed(current_name: str, version_name: str) -> FieldRenamed:
    return FieldRenamed(current_name=current_name, version_name=version_name)
