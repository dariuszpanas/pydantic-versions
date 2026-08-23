"""Current-model render validation and Pydantic core-schema adaptation."""

from __future__ import annotations

from collections.abc import Mapping
from functools import partial
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, create_model
from pydantic_core import SchemaValidator, core_schema

from pydantic_versions._compiler import _CompiledFamily
from pydantic_versions._runtime_versioning import (
    _alias_paths,
    _model_metadata_field_name,
    _remove_version_field,
    _runtime_label,
)
from pydantic_versions.exceptions import SchemaVersionError, UnsupportedWireModelError


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
    compiled: _CompiledFamily,
) -> SchemaValidator:
    cache = compiled._runtime_cache
    with cache.lock:
        adapter = cache.adapter
        if adapter is None:
            adapter = _build_current_wire_validation_adapter(
                compiled.model,
                family_name=compiled.name,
            )
            cache.adapter = adapter
        return cast(SchemaValidator, adapter)


def _build_current_wire_validation_adapter(
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
            locations.extend(
                _alias_paths(
                    field_info.validation_alias,
                    fallback=field_info.alias,
                ),
            )

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
