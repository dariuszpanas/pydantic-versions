from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

from pydantic_versions.declarations import (
    NestedFamily,
    SchemaVersion,
    TransitionFunc,
    VersionMetadata,
    VersionTransition,
    _require_label,
)
from pydantic_versions.exceptions import (
    DuplicateSchemaVersionError,
    SchemaCompilationError,
    SchemaVersionError,
    UnknownSchemaVersionError,
)
from pydantic_versions.patches import FieldDefault, FieldRemoved, FieldRenamed, VersionPatch

if TYPE_CHECKING:
    from pydantic_versions._planning import _PlanningCatalog

type UpgradeKind = Literal["implicit_identity", "custom_transition"]
type DowngradeKind = Literal["implicit_identity", "custom_transition", "unavailable"]
type WireModelKind = Literal["current", "generated", "explicit"]


@dataclass(frozen=True)
class _CompiledField:
    current_name: str
    version_name: str | None
    default: FieldDefault | None
    patch_ordinal: int | None


@dataclass(frozen=True)
class _VersionProjection:
    label: str
    fields: tuple[_CompiledField, ...]

    def field(self, current_name: str) -> _CompiledField:
        for field in self.fields:
            if field.current_name == current_name:
                return field
        msg = f"Compiled projection does not contain current field {current_name!r}"
        raise SchemaCompilationError(msg)


@dataclass(frozen=True)
class _CompiledVersion:
    projection: _VersionProjection
    model: type[BaseModel]
    wire_model_kind: WireModelKind


@dataclass(frozen=True)
class _CompiledTransition:
    source: str
    target: str
    upgrade_kind: UpgradeKind
    upgrade: TransitionFunc | None
    downgrade_kind: DowngradeKind
    downgrade_semantics: Literal["exact", "lossy", "unavailable"]


@dataclass(frozen=True)
class _CompiledFamily:
    model: type[BaseModel]
    name: str
    versions: tuple[_CompiledVersion, ...]
    transitions: tuple[_CompiledTransition, ...]
    version_metadata: VersionMetadata | None
    missing_version: str | None
    catalog: _PlanningCatalog

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(version.projection.label for version in self.versions)

    @property
    def current_version(self) -> str:
        return self.versions[-1].projection.label

    def index(self, label: str) -> int:
        for index, version in enumerate(self.versions):
            if version.projection.label == label:
                return index
        msg = f"Unknown schema version {label!r} for {self.name!r}"
        raise UnknownSchemaVersionError(msg)

    def version(self, label: str) -> _CompiledVersion:
        return self.versions[self.index(label)]


def _ensure_pydantic_v2_model(model_cls: object) -> None:
    if isinstance(model_cls, type) and issubclass(model_cls, BaseModel):
        return
    model_name = getattr(model_cls, "__name__", repr(model_cls))
    msg = f"{model_name!r} must inherit from pydantic.BaseModel from Pydantic v2"
    raise SchemaVersionError(msg)


def _ensure_unique_versions(versions: tuple[str, ...], *, schema_name: str) -> None:
    duplicates = {version for version in versions if versions.count(version) > 1}
    if duplicates:
        msg = f"Duplicate schema versions for {schema_name!r}: {sorted(duplicates)!r}"
        raise DuplicateSchemaVersionError(msg)


def _validate_family_declarations(
    *,
    model: type[BaseModel],
    name: str,
    versions: tuple[SchemaVersion, ...],
    transitions: tuple[VersionTransition, ...],
    nested: tuple[NestedFamily, ...],
    missing_version: str | None,
) -> None:
    if not versions:
        msg = f"Schema family {name!r} must declare at least one version"
        raise SchemaCompilationError(msg)
    if any(not isinstance(version, SchemaVersion) for version in versions):
        msg = "SchemaFamily.versions must contain only SchemaVersion values"
        raise SchemaCompilationError(msg)
    labels = tuple(
        _require_label(version.label, parameter="SchemaVersion.label") for version in versions
    )
    _ensure_unique_versions(labels, schema_name=name)
    current = versions[-1]
    if current.patches or current.wire_model is not None:
        msg = f"Current schema version {current.label!r} for {name!r} cannot be patched"
        raise SchemaCompilationError(msg)
    if missing_version is not None and missing_version not in labels:
        msg = f"Missing-version fallback {missing_version!r} is not in versions for {name!r}"
        raise UnknownSchemaVersionError(msg)
    for declaration in versions:
        if declaration.patches and declaration.wire_model is not None:
            msg = (
                f"Schema version {declaration.label!r} for {name!r} cannot "
                "combine patches with an explicit wire model"
            )
            raise SchemaCompilationError(msg)
        if declaration.wire_model is not None:
            _ensure_pydantic_v2_model(declaration.wire_model)
        _validate_patches(model, declaration.label, declaration.patches)
    if any(not isinstance(transition, VersionTransition) for transition in transitions):
        msg = "SchemaFamily.transitions must contain only VersionTransition values"
        raise SchemaCompilationError(msg)
    if any(not isinstance(declaration, NestedFamily) for declaration in nested):
        msg = "SchemaFamily.nested must contain only NestedFamily values"
        raise SchemaCompilationError(msg)
    _validate_transition_declarations(name, labels, transitions)


def _validate_compilation_boundary(
    *,
    name: str,
    versions: tuple[SchemaVersion, ...],
    transitions: tuple[VersionTransition, ...],
    nested: tuple[NestedFamily, ...],
) -> None:
    explicit = [version.label for version in versions if version.wire_model is not None]
    if explicit:
        msg = (
            f"Explicit wire models are not supported by the foundation compiler for "
            f"{name!r}: {explicit!r}"
        )
        raise SchemaCompilationError(msg)
    if nested:
        msg = f"Explicit nested family compilation is not supported yet for {name!r}"
        raise SchemaCompilationError(msg)
    if any(transition.downgrade is not None for transition in transitions):
        msg = f"Downgrade execution is not supported yet for {name!r}"
        raise SchemaCompilationError(msg)


def _validate_required_field_introductions(
    *,
    model: type[BaseModel],
    name: str,
    projections: tuple[_VersionProjection, ...],
    transitions: tuple[VersionTransition, ...],
) -> None:
    transitions_by_edge = {
        (transition.source, transition.target): transition for transition in transitions
    }
    for current_name, field_info in model.model_fields.items():
        if not field_info.is_required():
            continue
        for source, target in zip(projections, projections[1:], strict=False):
            absent_before = source.field(current_name).version_name is None
            present_after = target.field(current_name).version_name is not None
            transition = transitions_by_edge.get((source.label, target.label))
            if (
                absent_before
                and present_after
                and (transition is None or transition.upgrade is None)
            ):
                msg = (
                    f"Required field {current_name!r} is introduced on "
                    f"{source.label!r} -> {target.label!r} for {name!r} "
                    "without an upgrade"
                )
                raise SchemaCompilationError(msg)


def _validate_transition_declarations(
    family_name: str,
    labels: tuple[str, ...],
    transitions: tuple[VersionTransition, ...],
) -> None:
    seen: set[tuple[str, str]] = set()
    for transition in transitions:
        source = _require_label(transition.source, parameter="transition source")
        target = _require_label(transition.target, parameter="transition target")
        if source not in labels or target not in labels:
            msg = (
                f"Transition {source!r} -> {target!r} references an unknown version "
                f"for {family_name!r}"
            )
            raise SchemaCompilationError(msg)
        source_index = labels.index(source)
        target_index = labels.index(target)
        if target_index != source_index + 1:
            msg = (
                f"Transition {source!r} -> {target!r} for {family_name!r} must connect "
                "adjacent forward labels"
            )
            raise SchemaCompilationError(msg)
        key = (source, target)
        if key in seen:
            msg = f"Transition {source!r} -> {target!r} is declared more than once"
            raise DuplicateSchemaVersionError(msg)
        seen.add(key)
        if transition.upgrade is None and transition.downgrade is None:
            msg = f"Transition {source!r} -> {target!r} must provide at least one callable"
            raise SchemaCompilationError(msg)
        if transition.upgrade is not None and not callable(transition.upgrade):
            msg = f"Upgrade for {source!r} -> {target!r} must be callable"
            raise SchemaCompilationError(msg)
        if transition.downgrade is not None and not callable(transition.downgrade):
            msg = f"Downgrade for {source!r} -> {target!r} must be callable"
            raise SchemaCompilationError(msg)
        if transition.downgrade is None and transition.downgrade_semantics is not None:
            msg = "downgrade_semantics is forbidden when no downgrade is declared"
            raise SchemaCompilationError(msg)
        if transition.downgrade is not None and transition.downgrade_semantics not in (
            "exact",
            "lossy",
        ):
            msg = "downgrade_semantics must be 'exact' or 'lossy' when a downgrade is declared"
            raise SchemaCompilationError(msg)


def _validate_patches(
    model_cls: type[BaseModel],
    version: str,
    patches: tuple[VersionPatch, ...],
) -> None:
    field_names = set(model_cls.model_fields)
    touched: set[str] = set()
    removed: set[str] = set()
    renames: dict[str, str] = {}
    for patch in patches:
        if not isinstance(patch, FieldDefault | FieldRemoved | FieldRenamed):
            msg = f"Unsupported patch declaration for version {version!r}: {patch!r}"
            raise SchemaCompilationError(msg)
        current_name = patch.current_name if isinstance(patch, FieldRenamed) else patch.name
        if not isinstance(current_name, str) or not current_name:
            msg = f"Patch field names for version {version!r} must be non-empty strings"
            raise SchemaCompilationError(msg)
        if current_name not in field_names:
            msg = f"Patch for version {version!r} references unknown field {current_name!r}"
            raise SchemaVersionError(msg)
        if current_name in touched:
            msg = f"Field {current_name!r} has conflicting patches in version {version!r}"
            raise SchemaCompilationError(msg)
        touched.add(current_name)
        if isinstance(patch, FieldDefault):
            _validate_field_default(patch, version=version)
        elif isinstance(patch, FieldRemoved):
            removed.add(current_name)
        if isinstance(patch, FieldRenamed):
            version_name = _require_label(
                patch.version_name,
                parameter="rename target",
            )
            renames[current_name] = version_name

    output_owners: dict[str, str] = {}
    for current_name in model_cls.model_fields:
        if current_name in removed:
            continue
        output_name = renames.get(current_name, current_name)
        existing = output_owners.get(output_name)
        if existing is not None:
            msg = (
                f"Rename target {output_name!r} conflicts in version {version!r}: "
                f"fields {existing!r} and {current_name!r} share one output name"
            )
            raise SchemaVersionError(msg)
        output_owners[output_name] = current_name


def _validate_field_default(patch: FieldDefault, *, version: str) -> None:
    if not isinstance(patch.has_default, bool):
        msg = f"Field default for {patch.name!r} in version {version!r} has invalid state"
        raise SchemaCompilationError(msg)
    if patch.has_default:
        if patch.default_factory is not None:
            msg = (
                f"Field default for {patch.name!r} in version {version!r} cannot "
                "combine a value with default_factory"
            )
            raise SchemaCompilationError(msg)
        return
    if patch.default is not None or not callable(patch.default_factory):
        msg = (
            f"Field default factory for {patch.name!r} in version {version!r} "
            "must be callable and cannot include a direct default"
        )
        raise SchemaCompilationError(msg)


def _snapshot_field_default(patch: FieldDefault, *, version: str) -> FieldDefault:
    if not patch.has_default:
        return patch
    try:
        value = deepcopy(patch.default)
    except Exception as exc:
        msg = (
            f"Field default for {patch.name!r} in version {version!r} "
            "cannot be copied into the compiled plan"
        )
        raise SchemaCompilationError(msg) from exc
    return FieldDefault(name=patch.name, default=value)


def _compile_projection(
    model_cls: type[BaseModel],
    declaration: SchemaVersion,
) -> _VersionProjection:
    removed = {patch.name for patch in declaration.patches if isinstance(patch, FieldRemoved)}
    defaults = {
        patch.name: _snapshot_field_default(patch, version=declaration.label)
        for patch in declaration.patches
        if isinstance(patch, FieldDefault)
    }
    renames = {
        patch.current_name: patch.version_name
        for patch in declaration.patches
        if isinstance(patch, FieldRenamed)
    }
    patch_ordinals = {
        patch.current_name if isinstance(patch, FieldRenamed) else patch.name: ordinal
        for ordinal, patch in enumerate(declaration.patches)
    }
    fields = tuple(
        _CompiledField(
            current_name=current_name,
            version_name=None
            if current_name in removed
            else renames.get(current_name, current_name),
            default=defaults.get(current_name),
            patch_ordinal=patch_ordinals.get(current_name),
        )
        for current_name in model_cls.model_fields
    )
    return _VersionProjection(label=declaration.label, fields=fields)


def _compile_transition(
    source: str,
    target: str,
    declaration: VersionTransition | None,
) -> _CompiledTransition:
    upgrade = None if declaration is None else declaration.upgrade
    downgrade = None if declaration is None else declaration.downgrade
    if downgrade is not None:
        if declaration is None:  # pragma: no cover - derived from declaration
            msg = f"Downgrade {source!r} -> {target!r} has no declaration"
            raise SchemaCompilationError(msg)
        downgrade_kind: DowngradeKind = "custom_transition"
        downgrade_semantics = declaration.downgrade_semantics
        if downgrade_semantics not in ("exact", "lossy"):  # pragma: no cover - validated
            msg = f"Downgrade {source!r} -> {target!r} has no declared semantics"
            raise SchemaCompilationError(msg)
    elif upgrade is None:
        downgrade_kind = "implicit_identity"
        downgrade_semantics = "exact"
    else:
        downgrade_kind = "unavailable"
        downgrade_semantics = "unavailable"
    return _CompiledTransition(
        source=source,
        target=target,
        upgrade_kind="implicit_identity" if upgrade is None else "custom_transition",
        upgrade=upgrade,
        downgrade_kind=downgrade_kind,
        downgrade_semantics=downgrade_semantics,
    )


def _generated_model_name(
    model: type[BaseModel],
    family_name: str,
    label: str,
) -> str:
    components = (
        model.__module__,
        model.__qualname__,
        family_name,
        label,
    )
    suffix = _stable_digest(components)[:12]
    return (
        f"{_identifier_component(model.__name__)}"
        f"_{_identifier_component(family_name)}"
        f"_{_identifier_component(label)}_{suffix}"
    )


def _stable_digest(components: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for component in components:
        encoded = component.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, byteorder="big", signed=False))
        digest.update(encoded)
    return digest.hexdigest()


def _identifier_component(value: str) -> str:
    component = "".join(character if character.isalnum() else "_" for character in value)
    component = component.strip("_") or "value"
    if component[0].isdigit():
        component = f"v_{component}"
    return component
