from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

from pydantic_versions._compiler import (
    _ensure_pydantic_v2_model,
    _ensure_unique_versions,
)
from pydantic_versions._runtime import _infer_metadata_owner, _runtime_label
from pydantic_versions.declarations import (
    JsonValue as JsonValue,
)
from pydantic_versions.declarations import (
    MatchingLabels as MatchingLabels,
)
from pydantic_versions.declarations import (
    NestedFamily,
    SchemaVersion,
    TransitionFunc,
    VersionedValidation,
    VersionMetadata,
    VersionPath,
    VersionTransition,
    _freeze_sequence,
    _freeze_version_path,
    _require_label,
)
from pydantic_versions.declarations import (
    TransitionData as TransitionData,
)
from pydantic_versions.declarations import (
    matching_labels as matching_labels,
)
from pydantic_versions.exceptions import (
    DuplicateSchemaVersionError,
    SchemaCompilationError,
    UnknownSchemaVersionError,
)
from pydantic_versions.family import SchemaFamily, _family_for
from pydantic_versions.patches import VersionPatch

MigrationFunc = TransitionFunc
VersionField = VersionPath

_PENDING_ATTR = "__pydantic_versions_pending__"


@dataclass(frozen=True)
class _VersionSpec:
    label: str
    patches: tuple[VersionPatch, ...] = ()


def schema_version(version: str, *, patches: Sequence[VersionPatch] = ()):
    return schema_versions((version,), patches=patches)


def schema_versions(versions: Sequence[str], *, patches: Sequence[VersionPatch] = ()):
    version_order = _freeze_sequence(versions, parameter="schema_versions.versions")
    patch_order = _freeze_sequence(patches, parameter="schema_versions.patches")
    labels = tuple(
        _require_label(version, parameter="schema version label") for version in version_order
    )
    _ensure_unique_versions(labels, schema_name="pending schema declaration")

    def decorator[T: BaseModel](model_cls: type[T]) -> type[T]:
        _ensure_pydantic_v2_model(model_cls)
        pending: list[_VersionSpec] = list(model_cls.__dict__.get(_PENDING_ATTR, ()))
        pending.extend(_VersionSpec(label=label, patches=patch_order) for label in labels)
        setattr(model_cls, _PENDING_ATTR, tuple(pending))
        return model_cls

    return decorator


def versioned_schema(
    *,
    name: str,
    versions: Sequence[str],
    current: str,
    version_field: VersionPath = "schema_version",
    missing_version: str | None = None,
    metadata_owner: Literal["family", "model"] | None = None,
    transitions: Sequence[VersionTransition] = (),
    nested: Sequence[NestedFamily] = (),
):
    version_order = _freeze_sequence(versions, parameter="versioned_schema.versions")
    labels = tuple(
        _require_label(version, parameter="schema version label") for version in version_order
    )
    _ensure_unique_versions(labels, schema_name=name)
    current_version = _require_label(current, parameter="current")
    if current_version not in labels:
        msg = f"Current schema version {current_version!r} is not in versions for {name!r}"
        raise UnknownSchemaVersionError(msg)
    if not labels or current_version != labels[-1]:
        msg = f"Current schema version for {name!r} must be the final declared label"
        raise SchemaCompilationError(msg)
    normalized_path = _freeze_version_path(version_field, parameter="version_field")
    transition_order = _freeze_sequence(
        transitions,
        parameter="versioned_schema.transitions",
    )
    nested_order = _freeze_sequence(nested, parameter="versioned_schema.nested")

    def decorator[T: BaseModel](model_cls: type[T]) -> type[T]:
        _ensure_pydantic_v2_model(model_cls)
        pending = tuple(model_cls.__dict__.get(_PENDING_ATTR, ()))
        patches_by_label: dict[str, tuple[VersionPatch, ...]] = dict.fromkeys(labels, ())
        declared_patch_labels: set[str] = set()
        for spec in pending:
            if spec.label not in labels:
                msg = f"Patch schema version {spec.label!r} is not in versions for {name!r}"
                raise UnknownSchemaVersionError(msg)
            if spec.label in declared_patch_labels:
                msg = f"Schema version {spec.label!r} is declared more than once for {name!r}"
                raise DuplicateSchemaVersionError(msg)
            declared_patch_labels.add(spec.label)
            patches_by_label[spec.label] = spec.patches

        owner = metadata_owner
        if owner is None:
            owner = _infer_metadata_owner(model_cls, normalized_path)
        declarations = tuple(
            SchemaVersion(label=label, patches=patches_by_label[label]) for label in labels
        )
        family = SchemaFamily(
            model=model_cls,
            name=name,
            versions=declarations,
            transitions=transition_order,
            nested=nested_order,
            version_metadata=VersionMetadata(path=normalized_path, owner=owner),
            missing_version=missing_version,
        )
        family._decorator_created = True
        family.as_default()
        if _PENDING_ATTR in model_cls.__dict__:
            delattr(model_cls, _PENDING_ATTR)
        return model_cls

    return decorator


def migration[T: BaseModel](
    subject: type[T] | SchemaFamily[T],
    from_version: str,
    to_version: str,
):
    family = _family_for(subject)
    source = _runtime_label(from_version, family_name=family.name)
    target = _runtime_label(to_version, family_name=family.name)
    family._ensure_legacy_transition_allowed(source, target)

    def decorator(func: TransitionFunc) -> TransitionFunc:
        family._register_legacy_transition(source, target, func)
        return func

    return decorator


def model_for_version[T: BaseModel](
    subject: type[T] | SchemaFamily[T],
    version: str,
) -> type[BaseModel]:
    return _family_for(subject).model_for(version)


def validate_versioned[T: BaseModel](
    subject: type[T] | SchemaFamily[T],
    data: Any,
    *,
    version: str | None = None,
) -> VersionedValidation[T]:
    return _family_for(subject).validate(data, version=version)


def dump_versioned[T: BaseModel](
    subject: type[T] | SchemaFamily[T],
    *,
    version: str,
    data: T | Mapping[str, Any] | None = None,
    include_version: bool = True,
    **dump_kwargs: Any,
) -> dict[str, Any]:
    return _family_for(subject).dump(
        version=version,
        data=data,
        include_version=include_version,
        **dump_kwargs,
    )
