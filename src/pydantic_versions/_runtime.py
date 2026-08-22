from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from functools import partial
from types import UnionType
from typing import TYPE_CHECKING, Annotated, Any, Literal, Union, cast, get_args, get_origin

from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    ConfigDict,
    create_model,
)
from pydantic_core import SchemaValidator, core_schema, to_jsonable_python

from pydantic_versions._compiler import (
    _CompiledFamily,
    _CompiledVersion,
)
from pydantic_versions.declarations import VersionedValidation, VersionPath
from pydantic_versions.exceptions import (
    InvalidMigrationError,
    MissingSchemaVersionError,
    SchemaCompilationError,
    SchemaVersionError,
    UnknownSchemaVersionError,
    UnsupportedWireModelError,
)

if TYPE_CHECKING:
    from pydantic_versions.family import SchemaFamily


def _runtime_label(value: object, *, family_name: str) -> str:
    if not isinstance(value, str) or not value:
        msg = f"Schema version for {family_name!r} must be a non-empty string"
        raise UnknownSchemaVersionError(msg)
    return value


def _extract_declared_fields(
    value: BaseModel,
    *,
    declared_model: type[BaseModel] | None = None,
) -> dict[str, Any]:
    """Build a private canonical payload from validated, declared fields only."""
    selected_model = type(value) if declared_model is None else declared_model
    fields = selected_model.model_fields
    return {
        name: _extract_declared_value(
            value.__dict__[name],
            config=selected_model.model_config,
            annotation=field_info.annotation,
        )
        for name, field_info in fields.items()
        if name in value.__dict__ and _field_crosses_wire_boundary(field_info)
    }


def _extract_declared_value(
    value: Any,
    *,
    config: Mapping[str, Any],
    annotation: Any = None,
) -> Any:
    declared_annotation = _matching_declared_annotation(annotation, value)
    if isinstance(value, BaseModel):
        declared_model = (
            declared_annotation
            if isinstance(declared_annotation, type)
            and issubclass(declared_annotation, BaseModel)
            and isinstance(value, declared_annotation)
            else None
        )
        return _extract_declared_fields(value, declared_model=declared_model)
    if isinstance(value, Mapping):
        arguments = get_args(declared_annotation)
        item_annotation = arguments[1] if len(arguments) == 2 else None
        return {
            _jsonable_declared_mapping_key(key, config=config): _extract_declared_value(
                item,
                config=config,
                annotation=item_annotation,
            )
            for key, item in value.items()
        }
    if isinstance(value, list | tuple | set | frozenset):
        # Pydantic's JSON-shaped transition payload has historically represented
        # every supported sequence and set container as a list.  Keep that shape
        # while reading nested model values without invoking serializers.
        arguments = get_args(declared_annotation)
        if isinstance(value, tuple) and len(arguments) > 1 and arguments[-1] is not Ellipsis:
            item_annotations = arguments
        else:
            item_annotation = arguments[0] if arguments else None
            item_annotations = (item_annotation,) * len(value)
        return [
            _extract_declared_value(
                item,
                config=config,
                annotation=item_annotation,
            )
            for item, item_annotation in zip(value, item_annotations, strict=True)
        ]
    return _jsonable_declared_scalar(value, config=config)


def _matching_declared_annotation(annotation: Any, value: Any) -> Any:
    if annotation is None:
        return None
    normalized = _strip_annotated(annotation)
    origin = get_origin(normalized)
    if origin not in (Union, UnionType):
        return normalized
    candidates = tuple(_strip_annotated(candidate) for candidate in get_args(normalized))
    for candidate in candidates:
        if isinstance(candidate, type) and type(value) is candidate:
            return candidate
    class_matches = tuple(
        candidate for candidate in candidates if _safe_annotation_instance(value, candidate)
    )
    if class_matches:
        mro = type(value).mro()
        return min(
            class_matches,
            key=lambda candidate: mro.index(candidate) if candidate in mro else len(mro),
        )
    for candidate in candidates:
        if candidate is type(None):
            if value is None:
                return candidate
            continue
        candidate_origin = get_origin(candidate)
        if candidate_origin is not None and isinstance(candidate_origin, type):
            try:
                if isinstance(value, candidate_origin):
                    return candidate
            except TypeError:
                continue
    return normalized


def _safe_annotation_instance(value: Any, annotation: Any) -> bool:
    if not isinstance(annotation, type):
        return False
    try:
        return isinstance(value, annotation)
    except TypeError:
        return False


def _field_crosses_wire_boundary(field_info: Any) -> bool:
    for value in (field_info.exclude, field_info.exclude_if):
        if value is None or value is False:
            continue
        if isinstance(value, Mapping | tuple | list | set | frozenset) and not value:
            continue
        return False
    return True


def _jsonable_declared_scalar(value: Any, *, config: Mapping[str, Any]) -> Any:
    if isinstance(value, bytes):
        return to_jsonable_python(
            value,
            bytes_mode=config.get("ser_json_bytes", "utf8"),
            fallback=_preserve_unknown_scalar,
        )
    if isinstance(value, dt.timedelta):
        temporal_mode = config.get("ser_json_temporal")
        if temporal_mode is not None:
            return to_jsonable_python(
                value,
                temporal_mode=temporal_mode,
                fallback=_preserve_unknown_scalar,
            )
        return to_jsonable_python(
            value,
            timedelta_mode=config.get("ser_json_timedelta", "iso8601"),
            fallback=_preserve_unknown_scalar,
        )
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return to_jsonable_python(
            value,
            temporal_mode=config.get("ser_json_temporal", "iso8601"),
            fallback=_preserve_unknown_scalar,
        )
    return to_jsonable_python(value, fallback=_preserve_unknown_scalar)


def _jsonable_declared_mapping_key(value: Any, *, config: Mapping[str, Any]) -> Any:
    temporal_mode = config.get("ser_json_temporal")
    if temporal_mode is not None:
        dumped = to_jsonable_python(
            {value: None},
            bytes_mode=config.get("ser_json_bytes", "utf8"),
            temporal_mode=temporal_mode,
            fallback=_preserve_unknown_scalar,
        )
    else:
        dumped = to_jsonable_python(
            {value: None},
            bytes_mode=config.get("ser_json_bytes", "utf8"),
            timedelta_mode=config.get("ser_json_timedelta", "iso8601"),
            fallback=_preserve_unknown_scalar,
        )
    return next(iter(dumped))


def _preserve_unknown_scalar(value: Any) -> Any:
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
    payload = _to_current_names(
        compiled,
        source,
        _extract_declared_fields(source_model),
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
    else:
        payload = _validated_current_render_payload(
            family=family,
            compiled=compiled,
            data=data,
        )

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

    if compiled.version_metadata is not None and (
        requested != compiled.current_version or compiled.version_metadata.owner == "model"
    ):
        payload = dict(payload)
        if compiled.version_metadata.owner == "family":
            _set_version_field(payload, compiled.version_metadata.path, requested)
        else:
            payload[_model_metadata_field_name(compiled)] = requested

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
        if compiled.version_metadata.owner == "model":
            _remove_model_metadata_output_aliases(compiled, dumped)
        if include_version:
            _set_version_field(dumped, compiled.version_metadata.path, requested)
        else:
            _remove_version_field(dumped, compiled.version_metadata.path)
    return dumped


def _validated_current_render_payload[T: BaseModel](
    *,
    family: SchemaFamily[T],
    compiled: _CompiledFamily,
    data: T | Mapping[str, Any],
) -> dict[str, Any]:
    current_version = compiled.version(compiled.current_version)
    current_wire = current_version.model
    if isinstance(data, family.model):
        _validate_base_model_render_metadata(compiled, data)
        current_model = family.model.model_validate(data)
        raw_payload = _extract_declared_fields(
            current_model,
            declared_model=family.model,
        )
        _validate_current_render_metadata(compiled, raw_payload)
    elif isinstance(data, BaseModel):
        _validate_base_model_render_metadata(compiled, data)
        raw_payload = _extract_declared_fields(data)
        _validate_current_render_metadata(compiled, raw_payload)
        validation_payload = (
            _to_current_names(compiled, current_version, raw_payload)
            if isinstance(data, current_wire)
            else _without_family_render_metadata(compiled, raw_payload)
        )
        if isinstance(data, current_wire):
            current_model = _current_wire_validation_adapter(
                family.model,
                family_name=compiled.name,
            ).validate_python(
                validation_payload,
                by_name=True,
            )
        else:
            current_model = family.model.model_validate(
                validation_payload,
                by_name=True,
            )
    elif isinstance(data, Mapping):
        raw_payload = _copy_render_input(data)
        _validate_current_render_metadata(compiled, raw_payload)
        current_model = family.model.model_validate(
            _without_family_render_metadata(compiled, raw_payload),
        )
    else:
        msg = (
            f"Render data for schema family {compiled.name!r} must be "
            "a current model instance or mapping"
        )
        raise TypeError(msg)

    _validate_base_model_render_metadata(compiled, current_model)
    return _to_current_names(
        compiled,
        current_version,
        _extract_declared_fields(
            current_model,
            declared_model=family.model,
        ),
    )


def _validate_base_model_render_metadata(
    compiled: _CompiledFamily,
    data: BaseModel,
) -> None:
    _validate_current_render_metadata(compiled, data.__dict__)
    extras = data.__pydantic_extra__
    if isinstance(extras, Mapping):
        _validate_current_render_metadata(compiled, extras)


def _copy_render_input(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _copy_render_input(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_render_input(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_copy_render_input(item) for item in value)
    if isinstance(value, set):
        return {_copy_render_input(item) for item in value}
    if isinstance(value, frozenset):
        return frozenset(_copy_render_input(item) for item in value)
    return value


def _without_family_render_metadata(
    compiled: _CompiledFamily,
    payload: dict[str, Any],
) -> dict[str, Any]:
    validation_payload = dict(payload)
    metadata = compiled.version_metadata
    if metadata is not None and metadata.owner == "family":
        _remove_version_field(validation_payload, metadata.path)
    return validation_payload


def _current_wire_validation_adapter(
    model: type[BaseModel],
    *,
    family_name: str,
) -> SchemaValidator:
    source_schema = model.__pydantic_core_schema__
    model_references = _render_validation_model_references(source_schema)
    changed_references: set[str] = set()
    carrier_cache: dict[type[BaseModel], type[BaseModel]] = {}
    while True:
        discovered_references: set[str] = set()
        schema, _changed = _clone_render_validation_schema(
            source_schema,
            family_name=family_name,
            model_references=model_references,
            changed_references=changed_references,
            discovered_references=discovered_references,
            carrier_cache=carrier_cache,
            hash_required=False,
        )
        if discovered_references <= changed_references:
            return SchemaValidator(cast(core_schema.CoreSchema, schema))
        changed_references.update(discovered_references)


class _RenderValidationShell:
    """Private allocation target for carrier-aware model-field validation."""


def _build_hashable_render_carrier(
    *,
    model: type[BaseModel],
    cache: dict[type[BaseModel], type[BaseModel]],
) -> type[BaseModel]:
    cached = cache.get(model)
    if cached is not None:
        return cached
    carrier = create_model(
        f"{model.__name__}__HashableSetElement",
        __base__=model,
        __module__=model.__module__,
        __config__=ConfigDict(
            frozen=True,
            revalidate_instances="never",
            title=model.model_config.get("title") or model.__name__,
        ),
    )
    cache[model] = carrier
    return carrier


def _construct_hashable_render_carrier(
    carrier: type[BaseModel],
    value: BaseModel,
) -> BaseModel:
    instance = object.__new__(carrier)
    _copy_validated_model_state(instance, value, model=carrier)
    return instance


def _render_validation_model_references(value: Any) -> dict[str, type[BaseModel]]:
    references: dict[str, type[BaseModel]] = {}
    while True:
        previous = dict(references)
        _collect_render_validation_model_references(value, references)
        if references == previous:
            return references


def _collect_render_validation_model_references(
    value: Any,
    references: dict[str, type[BaseModel]],
) -> None:
    if isinstance(value, dict):
        schema_ref = value.get("ref")
        if isinstance(schema_ref, str):
            model = _render_validation_schema_model(value, model_references=references)
            if model is not None:
                references[schema_ref] = model
        for item in value.values():
            _collect_render_validation_model_references(item, references)
        return
    if isinstance(value, list | tuple):
        for item in value:
            _collect_render_validation_model_references(item, references)


def _render_validation_schema_model(
    schema: Any,
    *,
    model_references: Mapping[str, type[BaseModel]],
) -> type[BaseModel] | None:
    if not isinstance(schema, dict):
        return None
    schema_type = schema.get("type")
    if schema_type == "model":
        model = schema.get("cls")
        if isinstance(model, type) and issubclass(model, BaseModel):
            return model
        return None
    if schema_type == "definition-ref":
        schema_ref = schema.get("schema_ref")
        return model_references.get(schema_ref) if isinstance(schema_ref, str) else None
    if schema_type in {
        "custom-error",
        "default",
        "definitions",
        "function-after",
        "function-before",
        "function-wrap",
    }:
        return _render_validation_schema_model(
            schema.get("schema"),
            model_references=model_references,
        )
    if schema_type == "chain":
        steps = schema.get("steps")
        if isinstance(steps, list) and steps:
            return _render_validation_schema_model(
                steps[-1],
                model_references=model_references,
            )
        return None
    if schema_type in {"json-or-python", "lax-or-strict"}:
        candidates = tuple(
            _render_validation_schema_model(
                schema.get(key),
                model_references=model_references,
            )
            for key in (
                ("json_schema", "python_schema")
                if schema_type == "json-or-python"
                else ("lax_schema", "strict_schema")
            )
        )
        if candidates[0] is not None and candidates[0] is candidates[1]:
            return candidates[0]
    return None


def _clone_render_validation_schema(
    value: Any,
    *,
    family_name: str,
    model_references: Mapping[str, type[BaseModel]],
    changed_references: set[str],
    discovered_references: set[str],
    carrier_cache: dict[type[BaseModel], type[BaseModel]],
    hash_required: bool,
) -> tuple[Any, bool]:
    """Clone an authoritative schema and bridge only hash-required model outputs."""
    if isinstance(value, list):
        cloned_items = [
            _clone_render_validation_schema(
                item,
                family_name=family_name,
                model_references=model_references,
                changed_references=changed_references,
                discovered_references=discovered_references,
                carrier_cache=carrier_cache,
                hash_required=hash_required,
            )
            for item in value
        ]
        return [item for item, _changed in cloned_items], any(
            changed for _item, changed in cloned_items
        )
    if isinstance(value, tuple):
        cloned_items = tuple(
            _clone_render_validation_schema(
                item,
                family_name=family_name,
                model_references=model_references,
                changed_references=changed_references,
                discovered_references=discovered_references,
                carrier_cache=carrier_cache,
                hash_required=hash_required,
            )
            for item in value
        )
        return tuple(item for item, _changed in cloned_items), any(
            changed for _item, changed in cloned_items
        )
    if isinstance(value, dict):
        schema_type = value.get("type")
        if hash_required:
            output_model = _render_validation_schema_model(
                value,
                model_references=model_references,
            )
            if output_model is not None:
                cloned, changed = _clone_render_validation_schema(
                    value,
                    family_name=family_name,
                    model_references=model_references,
                    changed_references=changed_references,
                    discovered_references=discovered_references,
                    carrier_cache=carrier_cache,
                    hash_required=False,
                )
                if output_model.__hash__ is not None:
                    return cloned, changed
                carrier = _build_hashable_render_carrier(
                    model=output_model,
                    cache=carrier_cache,
                )
                return (
                    core_schema.no_info_after_validator_function(
                        partial(_construct_hashable_render_carrier, carrier),
                        cast(core_schema.CoreSchema, cloned),
                    ),
                    True,
                )
        if schema_type == "definitions":
            definitions, _definitions_changed = _clone_render_validation_schema(
                value.get("definitions", []),
                family_name=family_name,
                model_references=model_references,
                changed_references=changed_references,
                discovered_references=discovered_references,
                carrier_cache=carrier_cache,
                hash_required=False,
            )
            schema, schema_changed = _clone_render_validation_schema(
                value.get("schema"),
                family_name=family_name,
                model_references=model_references,
                changed_references=changed_references,
                discovered_references=discovered_references,
                carrier_cache=carrier_cache,
                hash_required=hash_required,
            )
            cloned = dict(value)
            cloned["definitions"] = definitions
            cloned["schema"] = schema
            return cloned, schema_changed
        if schema_type == "definition-ref":
            schema_ref = value.get("schema_ref")
            return dict(value), isinstance(schema_ref, str) and schema_ref in changed_references

        propagate_hash_keys: set[str] = set()
        if schema_type is None and hash_required:
            propagate_hash_keys.update(value)
        elif schema_type in {"set", "frozenset"}:
            propagate_hash_keys.add("items_schema")
        elif hash_required and schema_type == "tuple":
            propagate_hash_keys.add("items_schema")
        elif hash_required and schema_type in {"union", "tagged-union"}:
            propagate_hash_keys.add("choices")
        elif hash_required and schema_type == "chain":
            propagate_hash_keys.add("steps")
        elif hash_required and schema_type == "json-or-python":
            propagate_hash_keys.update(("json_schema", "python_schema"))
        elif hash_required and schema_type == "lax-or-strict":
            propagate_hash_keys.update(("lax_schema", "strict_schema"))
        elif hash_required and schema_type in {
            "custom-error",
            "default",
            "function-after",
            "function-before",
            "function-wrap",
            "nullable",
        }:
            propagate_hash_keys.add("schema")

        cloned_items = {
            key: _clone_render_validation_schema(
                item,
                family_name=family_name,
                model_references=model_references,
                changed_references=changed_references,
                discovered_references=discovered_references,
                carrier_cache=carrier_cache,
                hash_required=key in propagate_hash_keys,
            )
            for key, item in value.items()
        }
        cloned = {key: item for key, (item, _changed) in cloned_items.items()}
        changed = any(changed for _item, changed in cloned_items.values())
        if schema_type == "model" and changed:
            model = value.get("cls")
            if not isinstance(model, type) or not issubclass(model, BaseModel):
                return cloned, changed
            if value.get("custom_init"):
                msg = (
                    f"Automatic current-wire render validation for family {family_name!r} "
                    f"cannot safely execute custom __init__ on model {model.__qualname__!r}"
                )
                raise UnsupportedWireModelError(msg)
            cloned["cls"] = _RenderValidationShell
            cloned["custom_init"] = False
            cloned.pop("post_init", None)
            schema_ref = cloned.pop("ref", None)
            cloned = core_schema.no_info_after_validator_function(
                partial(_construct_authoritative_render_model, model),
                cast(core_schema.CoreSchema, cloned),
                ref=schema_ref,
            )
        schema_ref = value.get("ref")
        if changed and isinstance(schema_ref, str):
            discovered_references.add(schema_ref)
        return cloned, changed
    return value, False


def _construct_authoritative_render_model(
    model: type[BaseModel],
    value: Any,
) -> BaseModel:
    instance = object.__new__(model)
    _copy_validated_model_state(instance, value, model=model)
    if model.__pydantic_post_init__:
        instance.model_post_init(None)
    return instance


def _copy_validated_model_state(
    target: BaseModel,
    source: Any,
    *,
    model: type[BaseModel],
) -> None:
    source_values = source.__dict__
    values = {name: source_values[name] for name in model.model_fields if name in source_values}
    object.__setattr__(target, "__dict__", values)
    object.__setattr__(
        target,
        "__pydantic_fields_set__",
        set(source.__pydantic_fields_set__),
    )
    if not model.__pydantic_root_model__:
        extras = source.__pydantic_extra__
        object.__setattr__(
            target,
            "__pydantic_extra__",
            None if extras is None else dict(extras),
        )
        private = source.__pydantic_private__
        object.__setattr__(
            target,
            "__pydantic_private__",
            None if private is None else dict(private),
        )


def _validate_current_render_metadata(
    compiled: _CompiledFamily,
    payload: Mapping[str, Any],
) -> None:
    metadata = compiled.version_metadata
    if metadata is None:
        return

    locations: list[tuple[Any, ...]] = [
        (metadata.path,) if isinstance(metadata.path, str) else metadata.path,
    ]
    if metadata.owner == "model":
        field_name = _model_metadata_field_name(compiled)
        field_info = compiled.model.model_fields[field_name]
        locations.append((field_name,))
        if compiled.model.model_config.get("validate_by_alias", True) is not False:
            locations.extend(_field_alias_paths(field_info))

    checked_locations: list[tuple[Any, ...]] = []
    for location in locations:
        if location in checked_locations:
            continue
        checked_locations.append(location)
        found, raw_value = _read_render_metadata_path(payload, location)
        if not found:
            continue
        declared = _runtime_label(
            raw_value,
            family_name=compiled.name,
        )
        if declared == compiled.current_version:
            continue
        msg = (
            f"Render data for schema family {compiled.name!r} declares version "
            f"{declared!r}; current-model input must declare "
            f"{compiled.current_version!r}"
        )
        raise SchemaVersionError(msg)


def _read_render_metadata_path(
    payload: Mapping[Any, Any],
    path: tuple[Any, ...],
) -> tuple[bool, Any]:
    current: Any = payload
    for part in path:
        if isinstance(current, Mapping):
            if part not in current:
                return False, None
            current = current[part]
            continue
        if isinstance(current, list | tuple) and isinstance(part, int):
            try:
                current = current[part]
            except IndexError:
                return False, None
            continue
        return False, None
    return True, current


def _remove_model_metadata_output_aliases(
    compiled: _CompiledFamily,
    payload: dict[str, Any],
) -> None:
    metadata = compiled.version_metadata
    if metadata is None:
        msg = f"Compiled family {compiled.name!r} lost its version metadata"
        raise SchemaCompilationError(msg)
    field_name = _model_metadata_field_name(compiled)
    field_info = compiled.model.model_fields[field_name]
    candidates = (
        field_name,
        field_info.alias,
        field_info.validation_alias,
        field_info.serialization_alias,
    )
    for candidate in candidates:
        if isinstance(candidate, str) and candidate != metadata.path:
            payload.pop(candidate, None)


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
            _extract_declared_fields(source_data),
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
