from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import reduce
from operator import or_
from types import GenericAlias, UnionType
from typing import Annotated, Any, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, create_model
from pydantic_core import PydanticUndefined

from pydantic_versions.exceptions import (
    DuplicateSchemaVersionError,
    InvalidMigrationError,
    MissingSchemaVersionError,
    SchemaVersionError,
    UnknownSchemaVersionError,
)
from pydantic_versions.patches import FieldDefault, FieldRemoved, FieldRenamed, VersionPatch

MigrationFunc = Callable[[dict[str, Any]], dict[str, Any]]
VersionField = str | tuple[str, ...]


@dataclass(frozen=True)
class VersionedValidation[T: BaseModel]:
    source_version: str
    current_version: str
    source_model: BaseModel
    current_model: T
    migrations_applied: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class _VersionSpec:
    version: str
    patches: tuple[VersionPatch, ...] = ()


@dataclass
class _VersionedSchema:
    model_cls: type[BaseModel]
    name: str
    versions: tuple[str, ...]
    current: str
    version_field: VersionField
    missing_version: str | None
    patches: dict[str, tuple[VersionPatch, ...]]
    migrations: dict[tuple[str, str], MigrationFunc] = field(default_factory=dict)
    generated_models: dict[str, type[BaseModel]] = field(default_factory=dict)

    def index(self, version: str) -> int:
        try:
            return self.versions.index(version)
        except ValueError as exc:
            msg = f"Unknown schema version {version!r} for {self.name!r}"
            raise UnknownSchemaVersionError(msg) from exc


_REGISTRY: dict[type[BaseModel], _VersionedSchema] = {}
_PENDING_ATTR = "__pydantic_versions_pending__"


def schema_version(version: str, *, patches: Sequence[VersionPatch] = ()):
    return schema_versions([version], patches=patches)


def schema_versions(versions: Sequence[str], *, patches: Sequence[VersionPatch] = ()):
    def decorator[T: BaseModel](model_cls: type[T]) -> type[T]:
        pending = list(getattr(model_cls, _PENDING_ATTR, ()))
        version_order = tuple(str(version) for version in versions)
        _ensure_unique_versions(version_order, schema_name=model_cls.__name__)
        for version in version_order:
            pending.append(_VersionSpec(version=version, patches=tuple(patches)))
        setattr(model_cls, _PENDING_ATTR, tuple(pending))
        return model_cls

    return decorator


def versioned_schema(
    *,
    name: str,
    versions: Sequence[str],
    current: str,
    version_field: VersionField = "schema_version",
    missing_version: str | None = None,
):
    def decorator[T: BaseModel](model_cls: type[T]) -> type[T]:
        version_order = tuple(str(version) for version in versions)
        _ensure_unique_versions(version_order, schema_name=name)
        normalized_version_field = _normalize_version_field(version_field)
        current_version = str(current)
        if current_version not in version_order:
            msg = f"Current schema version {current_version!r} is not in versions for {name!r}"
            raise UnknownSchemaVersionError(msg)
        if missing_version is not None and str(missing_version) not in version_order:
            msg = f"Missing-version fallback {missing_version!r} is not in versions for {name!r}"
            raise UnknownSchemaVersionError(msg)

        pending = tuple(getattr(model_cls, _PENDING_ATTR, ()))
        patch_map: dict[str, tuple[VersionPatch, ...]] = dict.fromkeys(version_order, ())
        for spec in pending:
            if spec.version not in version_order:
                msg = f"Patch schema version {spec.version!r} is not in versions for {name!r}"
                raise UnknownSchemaVersionError(msg)
            if patch_map[spec.version]:
                msg = f"Schema version {spec.version!r} is declared more than once for {name!r}"
                raise DuplicateSchemaVersionError(msg)
            _validate_patches(model_cls, spec.version, spec.patches)
            patch_map[spec.version] = spec.patches

        _REGISTRY[model_cls] = _VersionedSchema(
            model_cls=model_cls,
            name=name,
            versions=version_order,
            current=current_version,
            version_field=normalized_version_field,
            missing_version=None if missing_version is None else str(missing_version),
            patches=patch_map,
        )
        return model_cls

    return decorator


def migration(model_cls: type[BaseModel], from_version: str, to_version: str):
    schema = _schema_for(model_cls)
    source = str(from_version)
    target = str(to_version)
    source_index = schema.index(source)
    target_index = schema.index(target)
    if source_index >= target_index:
        msg = f"Migration {source!r} -> {target!r} must move forward for {schema.name!r}"
        raise InvalidMigrationError(msg)

    def decorator(func: MigrationFunc) -> MigrationFunc:
        key = (source, target)
        if key in schema.migrations:
            msg = f"Migration {source!r} -> {target!r} is already registered for {schema.name!r}"
            raise DuplicateSchemaVersionError(msg)
        schema.migrations[key] = func
        return func

    return decorator


def model_for_version[T: BaseModel](model_cls: type[T], version: str) -> type[BaseModel]:
    schema = _schema_for(model_cls)
    requested = str(version)
    schema.index(requested)
    if requested not in schema.generated_models:
        schema.generated_models[requested] = _build_model_for_version(schema, requested)
    return schema.generated_models[requested]


def validate_versioned[T: BaseModel](
    model_cls: type[T],
    data: Any,
    *,
    version: str | None = None,
) -> VersionedValidation[T]:
    schema = _schema_for(model_cls)
    source_version = _detect_version(schema, data, explicit_version=version)
    source_model_cls = model_for_version(model_cls, source_version)
    source_model = source_model_cls.model_validate(_source_validation_input(schema, data))
    payload = _to_current_names(schema, source_version, source_model.model_dump())

    migrations_applied: list[tuple[str, str]] = []
    source_index = schema.index(source_version)
    current_index = schema.index(schema.current)
    for index in range(source_index, current_index):
        step = (schema.versions[index], schema.versions[index + 1])
        migrate = schema.migrations.get(step)
        if migrate is None:
            continue
        migrated = migrate(dict(payload))
        if not isinstance(migrated, dict):
            msg = f"Migration {step[0]!r} -> {step[1]!r} must return a dict"
            raise InvalidMigrationError(msg)
        payload = migrated
        migrations_applied.append(step)

    current_model = model_cls.model_validate(_current_validation_input(model_cls, payload))
    return VersionedValidation(
        source_version=source_version,
        current_version=schema.current,
        source_model=source_model,
        current_model=current_model,
        migrations_applied=tuple(migrations_applied),
    )


def dump_versioned[T: BaseModel](
    model_cls: type[T],
    *,
    version: str,
    data: Any = None,
    include_version: bool = True,
    **dump_kwargs: Any,
) -> dict[str, Any]:
    schema = _schema_for(model_cls)
    requested = str(version)
    schema.index(requested)
    target_model_cls = model_for_version(model_cls, requested)

    if data is None:
        target_model = target_model_cls()
    elif isinstance(data, BaseModel):
        target_model = target_model_cls.model_validate(
            _to_version_names(schema, requested, data.model_dump())
        )
    else:
        target_model = target_model_cls.model_validate(_to_version_names(schema, requested, data))

    dumped = target_model.model_dump(**dump_kwargs)
    if include_version:
        _set_version_field(dumped, schema.version_field, requested)
    else:
        _remove_version_field(dumped, schema.version_field)
    return dumped


def _schema_for(model_cls: type[BaseModel]) -> _VersionedSchema:
    try:
        return _REGISTRY[model_cls]
    except KeyError as exc:
        msg = f"{model_cls.__name__!r} is not registered with @versioned_schema"
        raise SchemaVersionError(msg) from exc


def _ensure_unique_versions(versions: tuple[str, ...], *, schema_name: str) -> None:
    duplicates = {version for version in versions if versions.count(version) > 1}
    if duplicates:
        msg = f"Duplicate schema versions for {schema_name!r}: {sorted(duplicates)!r}"
        raise DuplicateSchemaVersionError(msg)


def _validate_patches(
    model_cls: type[BaseModel],
    version: str,
    patches: tuple[VersionPatch, ...],
) -> None:
    field_names = set(model_cls.model_fields)
    output_names = set(field_names)
    for patch in patches:
        if isinstance(patch, FieldDefault | FieldRemoved):
            if patch.name not in field_names:
                msg = f"Patch for version {version!r} references unknown field {patch.name!r}"
                raise SchemaVersionError(msg)
        if isinstance(patch, FieldRenamed):
            if patch.current_name not in field_names:
                msg = f"Rename for version {version!r} references unknown field {patch.current_name!r}"
                raise SchemaVersionError(msg)
            output_names.discard(patch.current_name)
            if patch.version_name in output_names:
                msg = f"Rename target {patch.version_name!r} conflicts in version {version!r}"
                raise SchemaVersionError(msg)
            output_names.add(patch.version_name)


def _detect_version(
    schema: _VersionedSchema,
    data: Any,
    *,
    explicit_version: str | None,
) -> str:
    if explicit_version is not None:
        version = str(explicit_version)
        schema.index(version)
        return version
    if isinstance(data, Mapping):
        version_value = _get_version_field(data, schema.version_field)
        if version_value is not None:
            version = str(version_value)
            schema.index(version)
            return version
    if schema.missing_version is not None:
        return schema.missing_version
    msg = f"Input data for {schema.name!r} does not include {_version_field_display(schema.version_field)!r}"
    raise MissingSchemaVersionError(msg)


def _source_validation_input(schema: _VersionedSchema, data: Any) -> Any:
    if isinstance(schema.version_field, str) or not isinstance(data, Mapping):
        return data
    source_data = dict(data)
    _remove_version_field(source_data, schema.version_field)
    return source_data


def _normalize_version_field(version_field: VersionField) -> VersionField:
    if isinstance(version_field, str):
        if not version_field:
            msg = "version_field cannot be empty"
            raise SchemaVersionError(msg)
        return version_field
    if not version_field or any(not part for part in version_field):
        msg = "version_field path must contain non-empty field names"
        raise SchemaVersionError(msg)
    return tuple(str(part) for part in version_field)


def _get_version_field(data: Mapping[str, Any], version_field: VersionField) -> Any:
    if isinstance(version_field, str):
        return data.get(version_field)
    current: Any = data
    for part in version_field:
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _set_version_field(data: dict[str, Any], version_field: VersionField, value: str) -> None:
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


def _remove_version_field(data: dict[str, Any], version_field: VersionField) -> None:
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


def _version_field_display(version_field: VersionField) -> str:
    if isinstance(version_field, str):
        return version_field
    return ".".join(version_field)


def _version_field_default(schema: _VersionedSchema, version: str) -> tuple[str, Any] | None:
    if not isinstance(schema.version_field, str):
        return None
    version_field = schema.version_field
    if version_field in schema.model_cls.model_fields:
        return None
    return version_field, Annotated[str, Field(default=version)]


def _build_model_for_version(schema: _VersionedSchema, version: str) -> type[BaseModel]:
    removed = {patch.name for patch in schema.patches[version] if isinstance(patch, FieldRemoved)}
    defaults = {
        patch.name: patch for patch in schema.patches[version] if isinstance(patch, FieldDefault)
    }
    renames = {
        patch.current_name: patch.version_name
        for patch in schema.patches[version]
        if isinstance(patch, FieldRenamed)
    }

    fields: dict[str, Any] = {}
    for current_name, field_info in schema.model_cls.model_fields.items():
        if current_name in removed:
            continue
        version_name = renames.get(current_name, current_name)
        field_dict = field_info.asdict()
        annotation = _rewrite_annotation(field_dict["annotation"], version)
        attributes = dict(field_dict["attributes"])
        if current_name in renames:
            attributes["alias"] = None
            attributes["alias_priority"] = None
            attributes["validation_alias"] = None
            attributes["serialization_alias"] = None
        _rewrite_nested_default(attributes, field_dict["annotation"], annotation)
        if current_name in defaults:
            default_patch = defaults[current_name]
            if default_patch.has_default:
                attributes["default"] = default_patch.default
                attributes["default_factory"] = None
            else:
                attributes["default"] = PydanticUndefined
                attributes["default_factory"] = default_patch.default_factory
        fields[version_name] = Annotated[
            annotation,
            *field_dict["metadata"],
            Field(**attributes),
        ]

    version_field_default = _version_field_default(schema, version)
    if version_field_default is not None:
        field_name, field_definition = version_field_default
        if field_name not in fields:
            fields[field_name] = field_definition

    model_name = f"{schema.model_cls.__name__}Schema{_safe_model_suffix(version)}"
    return create_model(
        model_name,
        __config__=ConfigDict(**schema.model_cls.model_config),
        __module__=schema.model_cls.__module__,
        **fields,
    )


def _rewrite_annotation(annotation: Any, version: str) -> Any:
    if (
        isinstance(annotation, type)
        and issubclass(annotation, BaseModel)
        and annotation in _REGISTRY
    ):
        return model_for_version(annotation, version)

    origin = get_origin(annotation)
    if origin in (list, tuple, set, frozenset):
        args = tuple(_rewrite_annotation(arg, version) for arg in get_args(annotation))
        return GenericAlias(origin, args)
    if origin is dict:
        args = tuple(_rewrite_annotation(arg, version) for arg in get_args(annotation))
        return GenericAlias(dict, args)
    if origin in (Union, UnionType):
        args = tuple(_rewrite_annotation(arg, version) for arg in get_args(annotation))
        return reduce(or_, args)
    return annotation


def _rewrite_nested_default(
    attributes: dict[str, Any],
    original_annotation: Any,
    version_annotation: Any,
) -> None:
    if original_annotation == version_annotation:
        return
    if not (
        isinstance(original_annotation, type)
        and issubclass(original_annotation, BaseModel)
        and original_annotation in _REGISTRY
        and isinstance(version_annotation, type)
        and issubclass(version_annotation, BaseModel)
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
    schema: _VersionedSchema,
    version: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    normalized = dict(payload)
    _remove_version_field(normalized, schema.version_field)
    for patch in schema.patches[version]:
        if isinstance(patch, FieldRenamed) and patch.version_name in normalized:
            normalized[patch.current_name] = normalized.pop(patch.version_name)
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


def _to_version_names(schema: _VersionedSchema, version: str, payload: Any) -> Any:
    if not isinstance(payload, Mapping):
        return payload
    versioned = dict(payload)
    for patch in schema.patches[version]:
        if isinstance(patch, FieldRemoved):
            versioned.pop(patch.name, None)
        if isinstance(patch, FieldRenamed) and patch.current_name in versioned:
            versioned[patch.version_name] = versioned.pop(patch.current_name)
    return versioned


def _safe_model_suffix(version: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in version).title()
