from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Annotated, Any, Literal, get_args, get_origin

from pydantic import AliasChoices, AliasPath, BaseModel

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
    source_model = source.model.model_validate(data, by_name=True)
    payload = _to_current_names(
        compiled,
        source,
        source_model.model_dump(by_alias=False, mode="json"),
    )

    migrations_applied: list[tuple[str, str]] = []
    source_index = compiled.index(source_version)
    for transition in compiled.transitions[source_index:]:
        if source_version != compiled.current_version:
            payload = _apply_nested_family_migrations(
                payload=payload,
                compiled=compiled,
                source_label=transition.source,
                target_label=transition.target,
            )
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
    target_index = compiled.index(requested)
    current_index = len(compiled.versions) - 1
    if requested != compiled.current_version:
        family.plan_render(requested)

    if data is None:
        payload = {}
    elif isinstance(data, BaseModel):
        raw_payload = data.model_dump(by_alias=False, mode="json")
        if requested == compiled.current_version:
            payload = raw_payload
        else:
            payload = _to_current_names(
                compiled,
                compiled.version(compiled.current_version),
                raw_payload,
            )
    else:
        payload = dict(data)

    if requested != compiled.current_version:
        for edge_index in range(current_index - 1, target_index - 1, -1):
            transition = compiled.transitions[edge_index]
            payload = _apply_nested_family_migrations(
                payload=payload,
                compiled=compiled,
                source_label=transition.target,
                target_label=transition.source,
            )
            if transition.downgrade is None:
                continue
            migrated = transition.downgrade(dict(payload))
            if not isinstance(migrated, dict):
                msg = (
                    f"Migration {transition.source!r} -> {transition.target!r} "
                    f"downgrade must return a dict"
                )
                raise InvalidMigrationError(msg)
            payload = migrated

    if (
        requested != compiled.current_version
        and compiled.version_metadata is not None
        and compiled.version_metadata.owner == "family"
    ):
        payload = dict(payload)
        _set_version_field(payload, compiled.version_metadata.path, requested)

    target_model = target.model.model_validate(_to_version_names(target, payload), by_name=True)
    if "mode" in dump_kwargs:
        dumped = target_model.model_dump(**dump_kwargs)
    else:
        dumped = target_model.model_dump(mode="json", **dump_kwargs)
    if compiled.nested:
        for nested in compiled.nested:
            collection_kind = _nested_family_collection_kind(
                model=compiled.model,
                path=nested.path,
            )
            if collection_kind != "list":
                continue
            _prune_nested_family_metadata_at_path(
                payload=dumped,
                path=nested.path,
                family=nested.family,
            )
    if compiled.version_metadata is not None:
        if include_version:
            _set_version_field(dumped, compiled.version_metadata.path, requested)
        else:
            _remove_version_field(dumped, compiled.version_metadata.path)
    return dumped


def _apply_nested_family_migrations(
    *,
    payload: dict[str, Any],
    compiled: _CompiledFamily,
    source_label: str,
    target_label: str,
) -> dict[str, Any]:
    if not compiled.nested:
        return payload
    current_payload: dict[str, Any] = payload
    if source_label == target_label:
        return current_payload
    for nested in compiled.nested:
        nested_source = nested.child_label(source_label)
        nested_target = nested.child_label(target_label)
        if nested_source == nested_target:
            continue
        current_payload = _convert_nested_child_family(
            payload=current_payload,
            path=nested.path,
            family=nested.family,
            source_label=nested_source,
            target_label=nested_target,
            collection_kind=_nested_family_collection_kind(
                model=compiled.model,
                path=nested.path,
            ),
        )
    return current_payload


def _nested_family_collection_kind(
    *,
    model: type[BaseModel],
    path: tuple[str, ...],
) -> Literal["list", "tuple", "set", "frozenset"] | None:
    annotation: Any = model
    for index, key in enumerate(path):
        if not isinstance(annotation, type) or not issubclass(annotation, BaseModel):
            return None
        field_info = annotation.model_fields.get(key)
        if field_info is None:
            return None
        annotation = _strip_annotated(field_info.annotation)
        kind = _collection_kind(annotation)
        if index == len(path) - 1:
            return kind
        if kind is None:
            continue
        args = get_args(annotation)
        if not args:
            return None
        annotation = args[0]
    return None


def _collection_kind(
    annotation: Any,
) -> Literal["list", "tuple", "set", "frozenset"] | None:
    origin = get_origin(annotation)
    if origin is list:
        return "list"
    if origin is tuple:
        return "tuple"
    if origin is set:
        return "set"
    if origin is frozenset:
        return "frozenset"
    return None


def _strip_annotated(annotation: Any) -> Any:
    origin = get_origin(annotation)
    if origin is Annotated:
        args = get_args(annotation)
        if not args:
            return annotation
        return args[0]
    return annotation


def _has_duplicate_payload(payload: list[Any]) -> bool:
    for index, item in enumerate(payload):
        if item in payload[:index]:
            return True
    return False


def _prune_nested_family_metadata(
    *,
    payload: dict[str, Any],
    compiled: _CompiledFamily,
) -> None:
    for nested in compiled.nested:
        _prune_nested_family_metadata_at_path(
            payload=payload,
            path=nested.path,
            family=nested.family,
        )


def _prune_nested_family_metadata_payload(
    payload: Any,
    family: _CompiledFamily,
) -> None:
    metadata = family.version_metadata
    if metadata is not None and metadata.owner == "family" and isinstance(payload, Mapping):
        _remove_version_field(payload, metadata.path)

    if not family.nested:
        return

    for child in family.nested:
        _prune_nested_family_metadata_at_path(
            payload=payload,
            path=child.path,
            family=child.family,
        )


def _prune_nested_family_metadata_at_path(
    *,
    payload: Any,
    path: tuple[str, ...],
    family: SchemaFamily[Any],
) -> None:
    if not path:
        compiled_family = family._compiled_family()
        if isinstance(payload, Mapping | list | tuple | set | frozenset):
            if isinstance(payload, list):
                for item in payload:
                    _prune_nested_family_metadata_payload(item, compiled_family)
                return
            if isinstance(payload, tuple):
                for item in payload:
                    _prune_nested_family_metadata_payload(item, compiled_family)
                return
            if isinstance(payload, set):
                for item in payload:
                    _prune_nested_family_metadata_payload(item, compiled_family)
                return
            if isinstance(payload, frozenset):
                for item in payload:
                    _prune_nested_family_metadata_payload(item, compiled_family)
                return
            _prune_nested_family_metadata_payload(payload, compiled_family)
        return

    key, *remaining = path
    if isinstance(payload, Mapping):
        if key in payload:
            _prune_nested_family_metadata_at_path(
                payload=payload[key],
                path=tuple(remaining),
                family=family,
            )
            return
        for field_value in payload.values():
            _prune_nested_family_metadata_at_path(
                payload=field_value,
                path=path,
                family=family,
            )
        return

    if isinstance(payload, list):
        for field_value in payload:
            _prune_nested_family_metadata_at_path(
                payload=field_value,
                path=path,
                family=family,
            )
        return

    if isinstance(payload, tuple):
        for field_value in payload:
            _prune_nested_family_metadata_at_path(
                payload=field_value,
                path=path,
                family=family,
            )
        return

    if isinstance(payload, set):
        for field_value in payload:
            _prune_nested_family_metadata_at_path(
                payload=field_value,
                path=path,
                family=family,
            )
        return

    if isinstance(payload, frozenset):
        for field_value in payload:
            _prune_nested_family_metadata_at_path(
                payload=field_value,
                path=path,
                family=family,
            )


def _convert_nested_child_family(
    *,
    payload: Any,
    path: tuple[str, ...],
    family: SchemaFamily[Any],
    source_label: str,
    target_label: str,
    collection_kind: Literal["list", "tuple", "set", "frozenset"] | None = None,
) -> Any:
    if not path:
        return _convert_nested_family_payload(
            family=family,
            payload=payload,
            source_label=source_label,
            target_label=target_label,
            collection_kind=collection_kind,
        )
    key, *remaining = path
    if isinstance(payload, Mapping):
        if key in payload:
            nested_payload = payload[key]
            converted = _convert_nested_child_family(
                payload=nested_payload,
                path=tuple(remaining),
                family=family,
                source_label=source_label,
                target_label=target_label,
                collection_kind=collection_kind,
            )
            if converted is nested_payload:
                return payload
            updated = dict(payload)
            updated[key] = converted
            return updated
        converted_children = {
            field_name: _convert_nested_child_family(
                payload=field_value,
                path=path,
                family=family,
                source_label=source_label,
                target_label=target_label,
                collection_kind=collection_kind,
            )
            for field_name, field_value in payload.items()
        }
        if any(
            converted_children[field_name] is not field_value
            for field_name, field_value in payload.items()
        ):
            return converted_children
        return payload
    if isinstance(payload, list):
        converted_items = [
            _convert_nested_child_family(
                payload=item,
                path=path,
                family=family,
                source_label=source_label,
                target_label=target_label,
                collection_kind=collection_kind,
            )
            for item in payload
        ]
        if all(converted is item for converted, item in zip(converted_items, payload, strict=True)):
            return payload
        return converted_items
    if isinstance(payload, tuple):
        converted_items = tuple(
            _convert_nested_child_family(
                payload=item,
                path=path,
                family=family,
                source_label=source_label,
                target_label=target_label,
                collection_kind=collection_kind,
            )
            for item in payload
        )
        if all(converted is item for converted, item in zip(converted_items, payload, strict=True)):
            return payload
        return converted_items
    if isinstance(payload, set):
        converted_items = {
            _convert_nested_child_family(
                payload=item,
                path=path,
                family=family,
                source_label=source_label,
                target_label=target_label,
                collection_kind=collection_kind,
            )
            for item in payload
        }
        if len(converted_items) != len(payload):
            msg = (
                f"Nested migration for family {family.name!r} "
                "cannot preserve set cardinality while converting mixed payload values"
            )
            raise InvalidMigrationError(msg)
        return converted_items
    if isinstance(payload, frozenset):
        converted_items = frozenset(
            _convert_nested_child_family(
                payload=item,
                path=path,
                family=family,
                source_label=source_label,
                target_label=target_label,
                collection_kind=collection_kind,
            )
            for item in payload
        )
        if len(converted_items) != len(payload):
            msg = (
                f"Nested migration for family {family.name!r} "
                "cannot preserve set cardinality while converting mixed payload values"
            )
            raise InvalidMigrationError(msg)
        return converted_items
    return payload


def _convert_nested_family_payload(
    family: SchemaFamily[Any],
    payload: Any,
    source_label: str,
    target_label: str,
    collection_kind: Literal["list", "tuple", "set", "frozenset"] | None = None,
) -> Any:
    if payload is None:
        return payload
    if isinstance(payload, list):
        converted = [
            _convert_nested_family_payload(
                family=family,
                payload=item,
                source_label=source_label,
                target_label=target_label,
                collection_kind=collection_kind,
            )
            for item in payload
        ]
        if any(
            converted_item is not item
            for converted_item, item in zip(converted, payload, strict=True)
        ):
            if collection_kind in ("set", "frozenset") and _has_duplicate_payload(converted):
                msg = (
                    f"Nested migration for family {family.name!r} "
                    "cannot preserve set cardinality while converting mixed payload values"
                )
                raise InvalidMigrationError(msg)
            return converted
        return payload
    if isinstance(payload, tuple):
        converted = tuple(
            _convert_nested_family_payload(
                family=family,
                payload=item,
                source_label=source_label,
                target_label=target_label,
                collection_kind=collection_kind,
            )
            for item in payload
        )
        if any(
            converted_item is not item
            for converted_item, item in zip(converted, payload, strict=True)
        ):
            return converted
        return payload
    if isinstance(payload, set):
        converted = {
            _convert_nested_family_payload(
                family=family,
                payload=item,
                source_label=source_label,
                target_label=target_label,
                collection_kind=collection_kind,
            )
            for item in payload
        }
        if len(converted) != len(payload):
            msg = (
                f"Nested migration for family {family.name!r} "
                "cannot preserve set cardinality while converting mixed payload values"
            )
            raise InvalidMigrationError(msg)
        return converted
    if isinstance(payload, frozenset):
        converted = frozenset(
            _convert_nested_family_payload(
                family=family,
                payload=item,
                source_label=source_label,
                target_label=target_label,
                collection_kind=collection_kind,
            )
            for item in payload
        )
        if len(converted) != len(payload):
            msg = (
                f"Nested migration for family {family.name!r} "
                "cannot preserve set cardinality while converting mixed payload values"
            )
            raise InvalidMigrationError(msg)
        return converted
    if not isinstance(payload, Mapping):
        return payload
    compiled = family._compiled_family()
    source_index = compiled.index(source_label)
    target_index = compiled.index(target_label)
    if source_index == target_index:
        return dict(payload)
    source_version = compiled.version(source_label)
    source_model = source_version.model
    source_data = source_model.model_validate(payload, by_name=True)
    current_payload = dict(
        _to_current_names(
            compiled,
            source_version,
            source_data.model_dump(by_alias=False, mode="json"),
        )
    )
    if source_index < target_index:
        for edge_index in range(source_index, target_index):
            transition = compiled.transitions[edge_index]
            if transition.upgrade is None:
                continue
            migrated = transition.upgrade(dict(current_payload))
            if not isinstance(migrated, dict):
                msg = (
                    f"Nested migration {source_label!r} -> {target_label!r} for family "
                    f"{family.name!r} must return a dict"
                )
                raise InvalidMigrationError(msg)
            current_payload = migrated
    else:
        for edge_index in range(source_index - 1, target_index - 1, -1):
            transition = compiled.transitions[edge_index]
            if transition.downgrade is None:
                continue
            migrated = transition.downgrade(dict(current_payload))
            if not isinstance(migrated, dict):
                msg = (
                    f"Nested migration {source_label!r} -> {target_label!r} for family "
                    f"{family.name!r} must return a dict"
                )
                raise InvalidMigrationError(msg)
            current_payload = migrated
    if compiled.version_metadata is not None and compiled.version_metadata.owner == "family":
        if collection_kind in ("set", "tuple", "frozenset"):
            _set_version_field(current_payload, compiled.version_metadata.path, target_label)
        else:
            _remove_version_field(current_payload, compiled.version_metadata.path)
    return current_payload


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
        if part not in current:
            next_value = {}
            current[part] = next_value
        elif not isinstance(next_value, dict):
            msg = (
                f"Cannot set version metadata at {version_field!r} because "
                f"intermediate value {part!r} is not an object"
            )
            raise InvalidMigrationError(msg)
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
    return _normalize_payload_field_aliases(model_cls, current_payload, prefer_aliases=True)


def _normalize_payload_field_aliases(
    model_cls: type[BaseModel],
    payload: Mapping[str, Any],
    *,
    prefer_aliases: bool = False,
) -> dict[str, Any]:
    normalized = dict(payload)
    for name, field_info in model_cls.model_fields.items():
        alias_paths = _field_alias_paths(field_info)
        if name in normalized:
            if prefer_aliases:
                value = normalized[name]
                mapped_aliases = tuple(
                    path for path in alias_paths if not (len(path) == 1 and path[0] == name)
                )
                if mapped_aliases:
                    mapped_alias = mapped_aliases[0]
                    for alias_path in mapped_aliases:
                        _remove_payload_path(normalized, alias_path)
                    _set_payload_path(normalized, mapped_alias, value)
                    normalized.pop(name, None)
                continue

            for alias_path in alias_paths:
                if len(alias_path) == 1 and alias_path[0] == name:
                    continue
                _remove_payload_path(normalized, alias_path)
            continue
        source_path = _next_alias_path(field_info)
        if source_path is not None and _path_has_payload(normalized, source_path):
            value = _get_payload_path(normalized, source_path)
            _remove_payload_path(normalized, source_path)
            normalized[name] = value
    return normalized


def _field_alias_paths(field_info: Any) -> tuple[tuple[Any, ...], ...]:
    validation_alias = field_info.validation_alias
    if validation_alias is None:
        return _alias_path(field_info.alias)
    if isinstance(validation_alias, str):
        return ((validation_alias,),)
    if isinstance(validation_alias, AliasChoices):
        return tuple(path for choice in validation_alias.choices for path in _alias_path(choice))
    if isinstance(validation_alias, AliasPath):
        return (tuple(validation_alias.path),)
    return ()


def _alias_path(alias: Any) -> tuple[tuple[Any, ...], ...]:
    if isinstance(alias, str):
        return ((alias,),)
    if isinstance(alias, AliasPath):
        return (tuple(alias.path),)
    if isinstance(alias, AliasChoices):
        return tuple(path for choice in alias.choices for path in _alias_path(choice))
    return ()


def _next_alias_path(field_info: Any) -> tuple[Any, ...] | None:
    paths = _field_alias_paths(field_info)
    for path in paths:
        if path:
            return path
    return None


def _path_has_payload(payload: Mapping[Any, Any], path: tuple[Any, ...]) -> bool:
    current: Any = payload
    for part in path:
        if not isinstance(current, Mapping) or part not in current:
            return False
        current = current[part]
    return True


def _get_payload_path(payload: Mapping[Any, Any], path: tuple[Any, ...]) -> Any:
    current: Any = payload
    for part in path:
        current = current[part]
    return current


def _remove_payload_path(payload: dict[str, Any], path: tuple[Any, ...]) -> None:
    if not path:
        return
    parent_path: list[tuple[dict[str, Any], Any]] = []
    current: Any = payload
    for part in path[:-1]:
        if not isinstance(current, dict) or part not in current:
            return
        if not isinstance(current[part], Mapping):
            return
        parent_path.append((current, part))
        current = current[part]
    if not isinstance(current, Mapping):
        return
    removed = path[-1] in current
    if removed:
        current.pop(path[-1], None)
    if removed:
        for parent, part in reversed(parent_path):
            child = parent[part]
            if isinstance(child, Mapping) and len(child) == 0:
                parent.pop(part, None)


def _set_payload_path(payload: dict[str, Any], path: tuple[Any, ...], value: Any) -> None:
    if not path:
        return
    current: Any = payload
    for part in path[:-1]:
        if not isinstance(current, dict):
            return
        next_value = current.get(part)
        if part not in current:
            next_value = {}
            current[part] = next_value
        elif not isinstance(next_value, Mapping):
            return
        current = next_value
    if not isinstance(current, Mapping):
        return
    current[path[-1]] = value


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
    normalized = _normalize_payload_field_aliases(version.model, payload)
    original = dict(normalized)
    versioned = dict(normalized)
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
