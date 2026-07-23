from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic_versions.declarations import JsonValue, VersionMetadata

__all__ = [
    "ConversionPlan",
    "NestedFamilyDescription",
    "PlanStep",
    "ProjectionDescription",
    "SchemaInventory",
    "StepKind",
    "StepSemantics",
    "TransitionDescription",
    "VersionDescription",
]

type StepKind = Literal[
    "wire_validation",
    "projection",
    "implicit_identity",
    "custom_transition",
    "nested",
    "current_validation",
    "serialization",
    "metadata",
]
type StepSemantics = Literal[
    "not_applicable",
    "exact",
    "lossy",
    "unavailable",
]


class _SerializableRecord:
    def to_dict(self) -> dict[str, JsonValue]:
        raise NotImplementedError


def _record_list(records: tuple[_SerializableRecord, ...]) -> list[JsonValue]:
    return [record.to_dict() for record in records]


@dataclass(frozen=True)
class ProjectionDescription(_SerializableRecord):
    kind: Literal["default", "removed", "renamed"]
    current_field: str
    historical_field: str | None
    has_default: bool

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "kind": self.kind,
            "current_field": self.current_field,
            "historical_field": self.historical_field,
            "has_default": self.has_default,
        }


@dataclass(frozen=True)
class VersionDescription(_SerializableRecord):
    label: str
    wire_model: Literal["current", "generated", "explicit"]
    projections: tuple[ProjectionDescription, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "projections", tuple(self.projections))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "label": self.label,
            "wire_model": self.wire_model,
            "projections": _record_list(self.projections),
        }


@dataclass(frozen=True)
class TransitionDescription(_SerializableRecord):
    source: str
    target: str
    upgrade: Literal["implicit_identity", "custom"]
    downgrade: Literal["implicit_identity", "custom", "unavailable"]
    downgrade_semantics: StepSemantics

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "source": self.source,
            "target": self.target,
            "upgrade": self.upgrade,
            "downgrade": self.downgrade,
            "downgrade_semantics": self.downgrade_semantics,
        }


@dataclass(frozen=True)
class NestedFamilyDescription(_SerializableRecord):
    schema_path: str
    family: str
    versions: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "versions",
            tuple((parent, child) for parent, child in self.versions),
        )

    def to_dict(self) -> dict[str, JsonValue]:
        versions: list[JsonValue] = []
        for parent, child in self.versions:
            pair: list[JsonValue] = [parent, child]
            versions.append(pair)
        return {
            "schema_path": self.schema_path,
            "family": self.family,
            "versions": versions,
        }


@dataclass(frozen=True)
class SchemaInventory(_SerializableRecord):
    family: str
    model: str
    current_version: str
    versions: tuple[VersionDescription, ...]
    transitions: tuple[TransitionDescription, ...]
    nested: tuple[NestedFamilyDescription, ...]
    version_metadata: VersionMetadata | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "versions", tuple(self.versions))
        object.__setattr__(self, "transitions", tuple(self.transitions))
        object.__setattr__(self, "nested", tuple(self.nested))

    def to_dict(self) -> dict[str, JsonValue]:
        metadata: JsonValue = (
            None if self.version_metadata is None else self.version_metadata.to_dict()
        )
        return {
            "family": self.family,
            "model": self.model,
            "current_version": self.current_version,
            "versions": _record_list(self.versions),
            "transitions": _record_list(self.transitions),
            "nested": _record_list(self.nested),
            "version_metadata": metadata,
        }


@dataclass(frozen=True)
class PlanStep(_SerializableRecord):
    id: str
    family: str
    source_version: str
    target_version: str
    operation: Literal["validate", "render"]
    direction: Literal["upgrade", "downgrade"]
    kind: StepKind
    schema_path: str
    semantics: StepSemantics
    conditional: bool

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "id": self.id,
            "family": self.family,
            "source_version": self.source_version,
            "target_version": self.target_version,
            "operation": self.operation,
            "direction": self.direction,
            "kind": self.kind,
            "schema_path": self.schema_path,
            "semantics": self.semantics,
            "conditional": self.conditional,
        }


@dataclass(frozen=True)
class ConversionPlan(_SerializableRecord):
    family: str
    source_version: str
    target_version: str
    operation: Literal["validate", "render"]
    semantics: StepSemantics
    steps: tuple[PlanStep, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "steps", tuple(self.steps))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "family": self.family,
            "source_version": self.source_version,
            "target_version": self.target_version,
            "operation": self.operation,
            "semantics": self.semantics,
            "steps": _record_list(self.steps),
        }
