"""Version metadata and field-name normalization runtime helpers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Literal

from pydantic import AliasChoices, AliasPath, BaseModel

from pydantic_versions._compiler import _CompiledFamily, _CompiledVersion
from pydantic_versions.declarations import VersionPath
from pydantic_versions.exceptions import (
    InvalidMigrationError,
    MissingSchemaVersionError,
    SchemaCompilationError,
    UnknownSchemaVersionError,
)

_MISSING = object()
type _CanonicalMappingCopy = Callable[
    [Mapping[Any, Any], dict[Any, Any]],
    Mapping[Any, Any],
]


def _runtime_label(value: object, *, family_name: str) -> str:
    if not isinstance(value, str) or not value:
        msg = f"Schema version for {family_name!r} must be a non-empty string"
        raise UnknownSchemaVersionError(msg)
    return value


def _safe_nested_version_display(value: Any, *, compiled: _CompiledFamily) -> str:
    if type(value) is str and any(
        value == version.projection.label for version in compiled.versions
    ):
        return repr(value)
    return "a different label"


def _matches_version_label(value: Any, label: str) -> bool:
    return type(value) is str and value == label


def _validate_include_version_mode(
    compiled: _CompiledFamily,
    include_version: bool,
) -> None:
    metadata = compiled.version_metadata
    if include_version or metadata is None or metadata.owner != "model":
        return
    msg = (
        f"Schema family {compiled.name!r} uses model-owned version metadata; "
        "include_version=False is unavailable because that field is part of the body contract"
    )
    raise ValueError(msg)


def _apply_serialized_version_metadata(
    *,
    dumped: dict[str, Any],
    compiled: _CompiledFamily,
    requested: str,
    target_model: type[BaseModel],
    include_version: bool,
    by_alias: Any,
) -> None:
    metadata = compiled.version_metadata
    if metadata is None:
        return
    if metadata.owner == "family":
        if include_version:
            _ensure_serialized_version_field(
                dumped,
                metadata.path,
                requested,
                family_name=compiled.name,
            )
        else:
            _remove_version_field(dumped, metadata.path)
        return

    _verify_serialized_model_metadata(
        dumped,
        compiled=compiled,
        requested=requested,
        target_model=target_model,
        by_alias=by_alias,
    )


def _verify_serialized_model_metadata(
    dumped: Mapping[str, Any],
    *,
    compiled: _CompiledFamily,
    requested: str,
    target_model: type[BaseModel],
    by_alias: Any,
) -> None:
    field_name = _model_metadata_field_name(compiled)
    output_key = _serialized_field_name(target_model, field_name, by_alias=by_alias)
    candidate_keys = _model_metadata_serialization_keys(
        compiled,
        target_model=target_model,
        field_name=field_name,
    )
    value = dumped.get(output_key, _MISSING)
    if value is _MISSING:
        msg = (
            f"Target wire model for family {compiled.name!r} and version "
            f"{requested!r} omitted model-owned version metadata {output_key!r}"
        )
        raise ValueError(msg)
    if not _matches_version_label(value, requested):
        value_display = _safe_nested_version_display(value, compiled=compiled)
        msg = (
            f"Target wire model for family {compiled.name!r} serialized version "
            f"metadata {output_key!r} as {value_display}, expected {requested!r}"
        )
        raise ValueError(msg)
    duplicate_keys = tuple(key for key in candidate_keys if key != output_key and key in dumped)
    if duplicate_keys:
        formatted = ", ".join(repr(key) for key in duplicate_keys)
        msg = (
            f"Target wire model for family {compiled.name!r} serialized duplicate "
            f"version metadata at {formatted}"
        )
        raise ValueError(msg)


def _ensure_serialized_version_field(
    data: dict[str, Any],
    version_field: VersionPath,
    value: str,
    *,
    family_name: str,
) -> None:
    existing = _serialized_version_field(data, version_field)
    if existing is not _MISSING:
        if existing != value:
            msg = (
                f"Target wire model for family {family_name!r} serialized version "
                f"metadata {_version_field_display(version_field)!r} as "
                f"{existing!r}, expected {value!r}"
            )
            raise ValueError(msg)
        return
    _set_version_field(data, version_field, value)


def _serialized_version_field(data: Mapping[str, Any], version_field: VersionPath) -> Any:
    if isinstance(version_field, str):
        return data[version_field] if version_field in data else _MISSING
    current: Any = data
    for part in version_field:
        if not isinstance(current, Mapping) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _model_metadata_serialization_keys(
    compiled: _CompiledFamily,
    *,
    target_model: type[BaseModel],
    field_name: str,
) -> tuple[str, ...]:
    metadata = compiled.version_metadata
    keys: list[str] = [field_name]
    if metadata is not None and isinstance(metadata.path, str):
        keys.append(metadata.path)
    for model in (compiled.model, target_model):
        field_info = model.model_fields[field_name]
        for candidate in (
            field_info.alias,
            field_info.validation_alias,
            field_info.serialization_alias,
        ):
            if isinstance(candidate, str):
                keys.append(candidate)
    return tuple(dict.fromkeys(keys))


def _serialized_field_name(
    model: type[BaseModel],
    field_name: str,
    *,
    by_alias: Any,
) -> str:
    use_alias = (
        model.model_config.get("serialize_by_alias", False) is True
        if by_alias is None
        else by_alias is True
    )
    field_info = model.model_fields[field_name]
    if use_alias:
        output_alias = field_info.serialization_alias
        if isinstance(output_alias, str):
            return output_alias
    return field_name


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
        (field.current_name, field.version_name)
        for field in version.projection.fields
        if field.version_name is not None and field.version_name != field.current_name
    )
    original = dict(normalized)
    renamed_values: dict[str, Any] = {}
    for current_name, version_name in renamed:
        if version_name in original:
            renamed_values[current_name] = original[version_name]
    for _, version_name in renamed:
        normalized.pop(version_name, None)
    normalized.update(renamed_values)
    return normalized


def _current_validation_input(
    model_cls: type[BaseModel],
    payload: dict[str, Any],
    *,
    tracked_container_ids: set[int] | None = None,
    mapping_copy: _CanonicalMappingCopy | None = None,
) -> dict[str, Any]:
    current_payload = dict(payload)
    if model_cls.model_config.get("validate_by_alias", True) is False:
        return current_payload
    return _normalize_payload_field_aliases(
        model_cls,
        current_payload,
        prefer_aliases=True,
        tracked_container_ids=tracked_container_ids,
        mapping_copy=mapping_copy,
    )


def _normalize_payload_field_aliases(
    model_cls: type[BaseModel],
    payload: Mapping[str, Any],
    *,
    prefer_aliases: bool = False,
    tracked_container_ids: set[int] | None = None,
    mapping_copy: _CanonicalMappingCopy | None = None,
) -> dict[str, Any]:
    memo: dict[int, Any] = {}
    normalized = {
        key: _copy_alias_payload_value(
            value,
            tracked_container_ids=tracked_container_ids,
            mapping_copy=mapping_copy,
            memo=memo,
        )
        for key, value in payload.items()
    }
    for name, field_info in model_cls.model_fields.items():
        alias_paths = _alias_paths(
            field_info.validation_alias,
            fallback=field_info.alias,
        )
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


def _copy_alias_payload_value(
    value: Any,
    *,
    tracked_container_ids: set[int] | None = None,
    mapping_copy: _CanonicalMappingCopy | None = None,
    memo: dict[int, Any] | None = None,
) -> Any:
    if memo is None:
        memo = {}
    identity = id(value)
    if identity in memo:
        return memo[identity]
    if isinstance(value, Mapping):
        copied_items: dict[Any, Any] = {}
        memo[identity] = copied_items
        copied_items.update(
            {
                key: _copy_alias_payload_value(
                    item,
                    tracked_container_ids=tracked_container_ids,
                    mapping_copy=mapping_copy,
                    memo=memo,
                )
                for key, item in value.items()
            },
        )
        copied = copied_items if mapping_copy is None else mapping_copy(value, copied_items)
        memo[identity] = copied
    elif isinstance(value, list):
        copied = []
        memo[identity] = copied
        copied.extend(
            _copy_alias_payload_value(
                item,
                tracked_container_ids=tracked_container_ids,
                mapping_copy=mapping_copy,
                memo=memo,
            )
            for item in value
        )
    elif isinstance(value, tuple):
        # A tuple can participate in a cycle only through a mutable child. Keep
        # that back-edge authoritative while detaching the ordinary contents.
        memo[identity] = value
        copied = tuple(
            _copy_alias_payload_value(
                item,
                tracked_container_ids=tracked_container_ids,
                mapping_copy=mapping_copy,
                memo=memo,
            )
            for item in value
        )
        memo[identity] = copied
    else:
        return value
    if tracked_container_ids is not None and id(value) in tracked_container_ids:
        tracked_container_ids.add(id(copied))
    return copied


def _alias_paths(
    alias: Any,
    *,
    fallback: Any = _MISSING,
) -> tuple[tuple[str | int, ...], ...]:
    if (alias is _MISSING or alias is None) and fallback is not _MISSING:
        alias = fallback
    if isinstance(alias, str):
        return ((alias,),)
    if isinstance(alias, AliasPath):
        return (tuple(alias.path),)
    if isinstance(alias, AliasChoices):
        return tuple(path for choice in alias.choices for path in _alias_paths(choice))
    return ()


def _next_alias_path(field_info: Any) -> tuple[Any, ...] | None:
    paths = _alias_paths(
        field_info.validation_alias,
        fallback=field_info.alias,
    )
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
    parent_path: list[tuple[dict[str, Any], Any]] = []
    current: Any = payload
    for part in path[:-1]:
        if part not in current:
            return
        if not isinstance(current[part], dict):
            return
        parent_path.append((current, part))
        current = current[part]
    removed = path[-1] in current
    if removed:
        current.pop(path[-1], None)
    if removed:
        for parent, part in reversed(parent_path):
            child = parent[part]
            if isinstance(child, Mapping) and len(child) == 0:
                parent.pop(part, None)


def _set_payload_path(payload: dict[str, Any], path: tuple[Any, ...], value: Any) -> None:
    current = payload
    for part in path[:-1]:
        next_value = current.get(part)
        if part not in current:
            next_value = {}
            current[part] = next_value
        elif not isinstance(next_value, dict):
            return
        current = next_value
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


def _to_version_names(
    version: _CompiledVersion,
    payload: dict[str, Any],
    *,
    tracked_container_ids: set[int] | None = None,
    mapping_copy: _CanonicalMappingCopy | None = None,
) -> dict[str, Any]:
    normalized = _normalize_payload_field_aliases(
        version.model,
        payload,
        tracked_container_ids=tracked_container_ids,
        mapping_copy=mapping_copy,
    )
    original = dict(normalized)
    versioned = dict(normalized)
    renamed = tuple(
        (field.current_name, field.version_name)
        for field in version.projection.fields
        if field.version_name is not None and field.version_name != field.current_name
    )
    for field in version.projection.fields:
        if field.version_name is None:
            versioned.pop(field.current_name, None)
    renamed_values: dict[str, Any] = {}
    for current_name, version_name in renamed:
        if current_name in original:
            renamed_values[version_name] = original[current_name]
    for current_name, _ in renamed:
        versioned.pop(current_name, None)
    versioned.update(renamed_values)
    return versioned
