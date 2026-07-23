from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

from pydantic_versions.exceptions import SchemaCompilationError
from pydantic_versions.patches import VersionPatch

if TYPE_CHECKING:
    from pydantic_versions.family import SchemaFamily

type TransitionData = dict[str, Any]
type TransitionFunc = Callable[[TransitionData], TransitionData]
type VersionPath = str | tuple[str, ...]
type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]

type DowngradeSemantics = Literal["exact", "lossy"]


def _freeze_sequence[T](value: Sequence[T], *, parameter: str) -> tuple[T, ...]:
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        msg = f"{parameter} must be a sequence, not {type(value).__name__}"
        raise SchemaCompilationError(msg)
    return tuple(value)


def _freeze_version_path(path: VersionPath, *, parameter: str) -> VersionPath:
    if isinstance(path, str):
        if not path:
            msg = f"{parameter} cannot be empty"
            raise SchemaCompilationError(msg)
        return path
    if not isinstance(path, tuple) or not path:
        msg = f"{parameter} must be a non-empty string or tuple of strings"
        raise SchemaCompilationError(msg)
    if any(not isinstance(part, str) or not part for part in path):
        msg = f"{parameter} must contain only non-empty strings"
        raise SchemaCompilationError(msg)
    return tuple(path)


def _require_label(value: object, *, parameter: str) -> str:
    if not isinstance(value, str) or not value:
        msg = f"{parameter} must be a non-empty string"
        raise SchemaCompilationError(msg)
    return value


@dataclass(frozen=True)
class VersionedValidation[T: BaseModel]:
    source_version: str
    current_version: str
    source_model: BaseModel
    current_model: T
    migrations_applied: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class SchemaVersion:
    label: str
    patches: tuple[VersionPatch, ...] = ()
    wire_model: type[BaseModel] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "patches",
            _freeze_sequence(self.patches, parameter="SchemaVersion.patches"),
        )


@dataclass(frozen=True)
class VersionTransition:
    source: str
    target: str
    upgrade: TransitionFunc | None = None
    downgrade: TransitionFunc | None = None
    downgrade_semantics: DowngradeSemantics | None = None


@dataclass(frozen=True)
class VersionMetadata:
    path: VersionPath = "schema_version"
    owner: Literal["family", "model"] = "family"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "path",
            _freeze_version_path(self.path, parameter="VersionMetadata.path"),
        )
        if self.owner not in ("family", "model"):
            msg = "VersionMetadata.owner must be 'family' or 'model'"
            raise SchemaCompilationError(msg)

    def to_dict(self) -> dict[str, JsonValue]:
        path: JsonValue = self.path if isinstance(self.path, str) else list(self.path)
        return {"path": path, "owner": self.owner}


_DEFAULT_VERSION_METADATA = VersionMetadata()


@dataclass(frozen=True)
class MatchingLabels:
    pass


@dataclass(frozen=True)
class NestedFamily:
    path: VersionPath
    family: SchemaFamily[Any] | Callable[[], SchemaFamily[Any]]
    versions: Mapping[str, str] | MatchingLabels

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "path",
            _freeze_version_path(self.path, parameter="NestedFamily.path"),
        )
        if isinstance(self.versions, MatchingLabels):
            return
        if not isinstance(self.versions, Mapping):
            msg = "NestedFamily.versions must be a mapping or matching_labels()"
            raise SchemaCompilationError(msg)
        copied: dict[str, str] = {}
        for parent, child in self.versions.items():
            copied[_require_label(parent, parameter="nested parent label")] = _require_label(
                child,
                parameter="nested child label",
            )
        object.__setattr__(self, "versions", MappingProxyType(copied))


def matching_labels() -> MatchingLabels:
    return MatchingLabels()
