from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from functools import reduce
from operator import or_
from types import GenericAlias, UnionType
from typing import TYPE_CHECKING, Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, create_model
from pydantic_core import PydanticUndefined

from pydantic_versions._compiler import (
    _CompiledFamily,
    _CompiledVersion,
    _generated_model_name,
    _VersionProjection,
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
    source_model = source.model.model_validate(_source_validation_input(compiled, data))
    payload = _to_current_names(compiled, source, source_model.model_dump())

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

    current_model = family.model.model_validate(_current_validation_input(family.model, payload))
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
        target_model = target.model.model_validate(_to_version_names(target, data.model_dump()))
    else:
        target_model = target.model.model_validate(_to_version_names(target, data))

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


def _source_validation_input(compiled: _CompiledFamily, data: Any) -> Any:
    metadata = compiled.version_metadata
    if (
        metadata is None
        or metadata.owner == "model"
        or isinstance(metadata.path, str)
        or not isinstance(data, Mapping)
    ):
        return data
    source_data = dict(data)
    _remove_version_field(source_data, metadata.path)
    return source_data


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


def _version_field_default(
    family: SchemaFamily[Any],
    version: str,
) -> tuple[str, Any] | None:
    metadata = family.version_metadata
    if metadata is None or metadata.owner != "family" or not isinstance(metadata.path, str):
        return None
    if metadata.path in family.model.model_fields:
        return None
    return metadata.path, Annotated[str, Field(default=version)]


def _build_model_for_projection(
    family: SchemaFamily[Any],
    projection: _VersionProjection,
) -> type[BaseModel]:
    fields: dict[str, Any] = {}
    for compiled_field in projection.fields:
        if compiled_field.version_name is None:
            continue
        field_info = family.model.model_fields[compiled_field.current_name]
        field_dict = field_info.asdict()
        annotation = _rewrite_annotation(field_dict["annotation"], projection.label, family)
        attributes = dict(field_dict["attributes"])
        if compiled_field.version_name != compiled_field.current_name:
            attributes["alias"] = None
            attributes["alias_priority"] = None
            attributes["validation_alias"] = None
            attributes["serialization_alias"] = None
        _rewrite_nested_default(
            attributes,
            field_dict["annotation"],
            annotation,
            family,
        )
        if compiled_field.default is not None:
            if compiled_field.default.has_default:
                attributes["default"] = deepcopy(compiled_field.default.default)
                attributes["default_factory"] = None
            else:
                attributes["default"] = PydanticUndefined
                attributes["default_factory"] = compiled_field.default.default_factory
        fields[compiled_field.version_name] = Annotated[
            annotation,
            *field_dict["metadata"],
            Field(**attributes),
        ]

    version_field_default = _version_field_default(family, projection.label)
    if version_field_default is not None:
        field_name, field_definition = version_field_default
        if field_name not in fields:
            fields[field_name] = field_definition

    return create_model(
        _generated_model_name(family.model, family.name, projection.label),
        __config__=ConfigDict(**family.model.model_config),
        __module__=family.model.__module__,
        **fields,
    )


def _compat_child_family(
    owner: SchemaFamily[Any],
    annotation: Any,
) -> SchemaFamily[Any] | None:
    if not owner._decorator_created:
        return None
    if not isinstance(annotation, type) or not issubclass(annotation, BaseModel):
        return None
    from pydantic_versions.family import _default_family_for_model

    child = _default_family_for_model(annotation)
    if child is None or not child._decorator_created:
        return None
    owner_labels = tuple(version.label for version in owner.versions)
    child_labels = tuple(version.label for version in child.versions)
    if child_labels != owner_labels:
        msg = (
            f"Decorator child family {child.name!r} must use the exact labels of "
            f"parent {owner.name!r}; declare an explicit nested mapping instead"
        )
        raise SchemaCompilationError(msg)
    return child


def _rewrite_annotation(annotation: Any, version: str, family: SchemaFamily[Any]) -> Any:
    child = _compat_child_family(family, annotation)
    if child is not None:
        return child.model_for(version)

    origin = get_origin(annotation)
    if origin in (list, tuple, set, frozenset):
        args = tuple(_rewrite_annotation(arg, version, family) for arg in get_args(annotation))
        return GenericAlias(origin, args)
    if origin is dict:
        args = tuple(_rewrite_annotation(arg, version, family) for arg in get_args(annotation))
        return GenericAlias(dict, args)
    if origin in (Union, UnionType):
        args = tuple(_rewrite_annotation(arg, version, family) for arg in get_args(annotation))
        return reduce(or_, args)
    return annotation


def _rewrite_nested_default(
    attributes: dict[str, Any],
    original_annotation: Any,
    version_annotation: Any,
    family: SchemaFamily[Any],
) -> None:
    if original_annotation == version_annotation:
        return
    child = _compat_child_family(family, original_annotation)
    if child is None or not (
        isinstance(version_annotation, type) and issubclass(version_annotation, BaseModel)
    ):
        return
    if attributes.get("default_factory") is original_annotation:
        attributes["default_factory"] = version_annotation
        return
    default = attributes.get("default", PydanticUndefined)
    if isinstance(default, original_annotation):
        attributes["default"] = version_annotation.model_validate(
            default.model_dump(exclude_defaults=True)
        )


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
            _set_version_field(normalized, metadata.path, compiled.current_version)
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
    for name, field_info in model_cls.model_fields.items():
        alias = field_info.alias
        if alias is not None and name in current_payload and alias not in current_payload:
            current_payload[alias] = current_payload[name]
    return current_payload


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
