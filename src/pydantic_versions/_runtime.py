from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

from pydantic_versions._compiler import (
    _CompiledFamily,
    _CompiledVersion,
)
from pydantic_versions.declarations import VersionedValidation, VersionPath
from pydantic_versions.exceptions import (
    InvalidMigrationError,
    MissingSchemaVersionError,
    SchemaCompilationError,
    UnknownSchemaVersionError,
)

if TYPE_CHECKING:
    from pydantic_versions.family import SchemaFamily


def _runtime_label(value: object, *, family_name: str) -> str:
    if not isinstance(value, str) or not value:
        msg = f"Schema version for {family_name!r} must be a non-empty string"
        raise UnknownSchemaVersionError(msg)
    return value


def _validate_family[T: BaseModel](
    family: SchemaFamily[T],
    data: Any,
    *,
    version: str | None,
) -> VersionedValidation[T]:
    compiled = family._compiled_family()
    source_version = _detect_version(compiled, data, explicit_version=version)
    source = compiled.version(source_version)
    source_model = source.model.model_validate(data)
    payload = _to_current_names(compiled, source, source_model.model_dump(by_alias=False))

    migrations_applied: list[tuple[str, str]] = []
    source_index = compiled.index(source_version)
    for transition in compiled.transitions[source_index:]:
        if transition.upgrade is None:
            continue
        migrated = transition.upgrade(dict(payload))
        if not isinstance(migrated, dict):
            msg = f"Migration {transition.source!r} -> {transition.target!r} must return a dict"
            raise InvalidMigrationError(msg)
        payload = migrated
        migrations_applied.append((transition.source, transition.target))

    current_model = family.model.model_validate(
        _current_validation_input(family.model, payload),
        by_name=True,
    )
    return VersionedValidation(
        source_version=source_version,
        current_version=compiled.current_version,
        source_model=source_model,
        current_model=current_model,
        migrations_applied=tuple(migrations_applied),
    )


def _dump_family[T: BaseModel](
    family: SchemaFamily[T],
    *,
    version: str,
    data: T | Mapping[str, Any] | None,
    include_version: bool,
    dump_kwargs: Mapping[str, Any],
) -> dict[str, Any]:
    compiled = family._compiled_family()
    requested = _runtime_label(version, family_name=family.name)
    target = compiled.version(requested)

    if data is None:
        target_model = target.model()
    elif isinstance(data, BaseModel):
        target_model = target.model.model_validate(
            _to_version_names(target, data.model_dump(by_alias=False)),
            by_name=True,
        )
    else:
        target_model = target.model.model_validate(_to_version_names(target, data), by_name=True)

    dumped = target_model.model_dump(**dump_kwargs)
    if compiled.version_metadata is not None:
        if include_version:
            _set_version_field(dumped, compiled.version_metadata.path, requested)
        else:
            _remove_version_field(dumped, compiled.version_metadata.path)
    return dumped


def _infer_metadata_owner(
    model_cls: type[BaseModel],
    version_path: VersionPath,
) -> Literal["family", "model"]:
    if not isinstance(version_path, str):
        return "family"
    if version_path in model_cls.model_fields:
        return "model"
    if any(field.alias == version_path for field in model_cls.model_fields.values()):
        return "model"
    return "family"


def _detect_version(
    compiled: _CompiledFamily,
    data: Any,
    *,
    explicit_version: str | None,
) -> str:
    if explicit_version is not None:
        version = _runtime_label(explicit_version, family_name=compiled.name)
        compiled.index(version)
        return version
    if isinstance(data, Mapping) and compiled.version_metadata is not None:
        version_value = _get_version_field(data, compiled.version_metadata.path)
        if version_value is not None:
            version = _runtime_label(version_value, family_name=compiled.name)
            compiled.index(version)
            return version
    if compiled.missing_version is not None:
        return compiled.missing_version
    field_display = (
        "explicit version"
        if compiled.version_metadata is None
        else _version_field_display(compiled.version_metadata.path)
    )
    msg = f"Input data for {compiled.name!r} does not include {field_display!r}"
    raise MissingSchemaVersionError(msg)


def _get_version_field(data: Mapping[str, Any], version_field: VersionPath) -> Any:
    if isinstance(version_field, str):
        return data.get(version_field)
    current: Any = data
    for part in version_field:
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _set_version_field(data: dict[str, Any], version_field: VersionPath, value: str) -> None:
    if isinstance(version_field, str):
        data[version_field] = value
        return
    current = data
    for part in version_field[:-1]:
        next_value = current.get(part)
        if not isinstance(next_value, dict):
            next_value = {}
            current[part] = next_value
        current = next_value
    current[version_field[-1]] = value


def _remove_version_field(data: dict[str, Any], version_field: VersionPath) -> None:
    if isinstance(version_field, str):
        data.pop(version_field, None)
        return
    current: Any = data
    parents: list[tuple[dict[str, Any], str]] = []
    for part in version_field[:-1]:
        if not isinstance(current, dict) or not isinstance(current.get(part), dict):
            return
        parents.append((current, part))
        current = current[part]
    if isinstance(current, dict):
        current.pop(version_field[-1], None)
    for parent, part in reversed(parents):
        child = parent.get(part)
        if isinstance(child, dict) and not child:
            parent.pop(part, None)


def _version_field_display(version_field: VersionPath) -> str:
    if isinstance(version_field, str):
        return version_field
    return ".".join(version_field)


def _to_current_names(
    compiled: _CompiledFamily,
    version: _CompiledVersion,
    payload: dict[str, Any],
) -> dict[str, Any]:
    normalized = dict(payload)
    metadata = compiled.version_metadata
    if metadata is not None:
        if metadata.owner == "family":
            _remove_version_field(normalized, metadata.path)
        else:
            metadata_field = _model_metadata_field_name(compiled)
            if metadata.path != metadata_field:
                normalized.pop(metadata.path, None)
            normalized[metadata_field] = compiled.current_version
    renamed = tuple(
        field
        for field in version.projection.fields
        if field.version_name is not None and field.version_name != field.current_name
    )
    original = dict(normalized)
    renamed_values: dict[str, Any] = {}
    for field in renamed:
        if field.version_name is None:  # pragma: no cover - narrowed by renamed
            continue
        if field.version_name in original:
            renamed_values[field.current_name] = original[field.version_name]
    for field in renamed:
        normalized.pop(field.version_name, None)
    normalized.update(renamed_values)
    return normalized


def _current_validation_input(
    model_cls: type[BaseModel], payload: dict[str, Any]
) -> dict[str, Any]:
    current_payload = dict(payload)
    if model_cls.model_config.get("validate_by_alias", True) is False:
        return current_payload
    for name, field_info in model_cls.model_fields.items():
        if name not in current_payload:
            continue
        validation_alias = field_info.validation_alias
        alias = (
            validation_alias
            if isinstance(validation_alias, str)
            else field_info.alias
            if validation_alias is None
            else None
        )
        if isinstance(alias, str):
            current_payload[alias] = current_payload.pop(name)
    return current_payload


def _model_metadata_field_name(compiled: _CompiledFamily) -> str:
    metadata = compiled.version_metadata
    if metadata is None or metadata.owner != "model" or not isinstance(metadata.path, str):
        msg = f"Compiled family {compiled.name!r} has invalid model-owned version metadata"
        raise SchemaCompilationError(msg)
    for field_name, field_info in compiled.model.model_fields.items():
        if metadata.path in (
            field_name,
            field_info.alias,
            field_info.validation_alias,
            field_info.serialization_alias,
        ):
            return field_name
    msg = f"Compiled family {compiled.name!r} lost its model-owned version metadata field"
    raise SchemaCompilationError(msg)


def _to_version_names(version: _CompiledVersion, payload: Any) -> Any:
    if not isinstance(payload, Mapping):
        return payload
    original = dict(payload)
    versioned = dict(original)
    renamed = tuple(
        field
        for field in version.projection.fields
        if field.version_name is not None and field.version_name != field.current_name
    )
    for field in version.projection.fields:
        if field.version_name is None:
            versioned.pop(field.current_name, None)
    renamed_values: dict[str, Any] = {}
    for field in renamed:
        if field.version_name is None:  # pragma: no cover - narrowed by renamed
            continue
        if field.current_name in original:
            renamed_values[field.version_name] = original[field.current_name]
    for field in renamed:
        versioned.pop(field.current_name, None)
    versioned.update(renamed_values)
    return versioned
