"""Current-model render validation and Pydantic core-schema adaptation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import partial
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, create_model
from pydantic_core import SchemaValidator, core_schema

from pydantic_versions._compiler import _CompiledFamily
from pydantic_versions._runtime_payload import _HashableCanonicalMapping
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
    *,
    guard_collections: bool = False,
) -> SchemaValidator:
    from pydantic_versions._runtime_validation import (  # noqa: PLC0415
        _ensure_canonical_validation_supported,
    )

    _ensure_canonical_validation_supported(compiled.model)
    cache = compiled._runtime_cache
    with cache.lock:
        adapter = cache.guarded_adapter if guard_collections else cache.adapter
        if adapter is None:
            if guard_collections:
                adapter = _build_current_wire_validation_adapter(
                    compiled.model,
                    family_name=compiled.name,
                    guard_collections=True,
                )
            else:
                adapter = _build_current_wire_validation_adapter(
                    compiled.model,
                    family_name=compiled.name,
                )
            if guard_collections:
                cache.guarded_adapter = adapter
            else:
                cache.adapter = adapter
        return cast(SchemaValidator, adapter)


def _build_current_wire_validation_adapter(
    model: type[BaseModel],
    *,
    family_name: str,
    guard_collections: bool = False,
) -> SchemaValidator:
    # Imported lazily so the validation module can reuse the schema-cloning
    # machinery below without introducing an import cycle.
    from pydantic_versions._runtime_validation import (  # noqa: PLC0415
        _build_canonical_validation_schema,
    )

    schema, _changes = _build_canonical_validation_schema(
        model,
        family_name=family_name,
        bypass_materialized_models=False,
        guard_collections=guard_collections,
    )
    return SchemaValidator(
        schema,
        config=_authoritative_core_config(model),
    )


_ENUM_BRIDGE_CHANGE = 1
_STRUCTURAL_CHANGE = 2
_ENUM_UNION_CHANGE = 4
_APPLICATION_VALIDATOR_CHANGE = 8
_CARRIER_UNWRAP_CHANGE = 16
_OPAQUE_HASH_CHANGE = 32
type _SchemaChanges = int
type _ValidationSchemaTransform = Callable[
    [dict[str, Any], _SchemaChanges, bool, bool],
    tuple[dict[str, Any], _SchemaChanges],
]


def _build_adapted_validation_schema(
    model: type[BaseModel],
    *,
    family_name: str,
    bypass_materialized_models: bool,
    schema_transform: _ValidationSchemaTransform | None,
) -> tuple[core_schema.CoreSchema, _SchemaChanges]:
    """Clone a model schema while preserving its reference and model shells."""
    source_schema = model.__pydantic_core_schema__
    reachable_references = _validation_reachable_references(source_schema)
    model_references = _render_validation_model_references(
        source_schema,
        reachable_references=reachable_references,
    )
    reference_changes: dict[str, _SchemaChanges] = {}
    carrier_cache: dict[type[BaseModel], type[BaseModel]] = {}
    while True:
        discovered_reference_changes: dict[str, _SchemaChanges] = {}
        schema, changes = _clone_render_validation_schema(
            source_schema,
            family_name=family_name,
            model_references=model_references,
            reachable_references=reachable_references,
            reference_changes=reference_changes,
            discovered_reference_changes=discovered_reference_changes,
            carrier_cache=carrier_cache,
            hash_required=False,
            allow_enum_bridges=True,
            bypass_materialized_models=bypass_materialized_models,
            schema_transform=schema_transform,
        )
        if all(
            reference_changes.get(ref, 0) | discovered == reference_changes.get(ref, 0)
            for ref, discovered in discovered_reference_changes.items()
        ):
            return cast(core_schema.CoreSchema, schema), changes
        for ref, discovered in discovered_reference_changes.items():
            reference_changes[ref] = reference_changes.get(ref, 0) | discovered


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


def _validation_schema_child_keys(schema: Mapping[str, Any]) -> tuple[str, ...]:
    """Return only runtime-validation child-schema keys for one core node."""
    schema_type = schema.get("type")
    if schema_type is None:
        return tuple(schema)
    if schema_type in {"list", "set", "frozenset", "generator"}:
        return ("items_schema",)
    if schema_type == "tuple":
        return ("items_schema",)
    if schema_type == "dict":
        return ("keys_schema", "values_schema")
    if schema_type in {
        "custom-error",
        "dataclass",
        "dataclass-field",
        "default",
        "function-after",
        "function-before",
        "function-wrap",
        "json",
        "model",
        "model-field",
        "nullable",
        "typed-dict-field",
    }:
        return ("schema",)
    if schema_type in {"union", "tagged-union"}:
        return ("choices",)
    if schema_type == "chain":
        return ("steps",)
    if schema_type == "lax-or-strict":
        return ("lax_schema", "strict_schema")
    if schema_type == "json-or-python":
        return ("json_schema", "python_schema")
    if schema_type in {"typed-dict", "model-fields"}:
        return ("fields", "extras_schema", "extras_keys_schema")
    if schema_type == "dataclass-args":
        return ("fields",)
    if schema_type == "arguments":
        return ("arguments_schema", "var_args_schema", "var_kwargs_schema")
    if schema_type == "arguments-v3":
        return ("arguments_schema",)
    if schema_type == "call":
        return ("arguments_schema", "return_schema")
    if schema_type == "definitions":
        return ("schema",)
    return ()


def _validation_reachable_references(value: Any) -> frozenset[str]:
    definitions: dict[str, Mapping[str, Any]] = {}

    def collect_definitions(current: Any) -> None:
        if isinstance(current, list | tuple):
            for item in current:
                collect_definitions(item)
            return
        if not isinstance(current, Mapping):
            return
        schema_ref = current.get("ref")
        if isinstance(schema_ref, str):
            definitions[schema_ref] = current
        if current.get("type") == "definitions":
            stored = current.get("definitions")
            if isinstance(stored, list):
                for definition in stored:
                    collect_definitions(definition)
        for key in _validation_schema_child_keys(current):
            if key in current:
                collect_definitions(current[key])

    collect_definitions(value)
    reachable: set[str] = set()
    active_values: set[int] = set()

    def visit(current: Any) -> None:
        if isinstance(current, list | tuple):
            for item in current:
                visit(item)
            return
        if not isinstance(current, Mapping):
            return
        identity = id(current)
        if identity in active_values:
            return
        active_values.add(identity)
        try:
            if current.get("type") == "definition-ref":
                schema_ref = current.get("schema_ref")
                if isinstance(schema_ref, str) and schema_ref not in reachable:
                    reachable.add(schema_ref)
                    target = definitions.get(schema_ref)
                    if target is not None:
                        visit(target)
                return
            for key in _validation_schema_child_keys(current):
                if key in current:
                    visit(current[key])
        finally:
            active_values.remove(identity)

    visit(value)
    return frozenset(reachable)


def _authoritative_core_config(model: type[BaseModel]) -> core_schema.CoreConfig:
    source_schema = model.__pydantic_core_schema__
    reachable_references = _validation_reachable_references(source_schema)
    pending: list[Any] = [source_schema]
    while pending:
        current = pending.pop()
        if not isinstance(current, Mapping):
            if isinstance(current, list | tuple):
                pending.extend(current)
            continue
        if current.get("type") == "model" and current.get("cls") is model:
            config = current.get("config")
            values = dict(config) if isinstance(config, Mapping) else {}
            values["title"] = values.get("title") or model.__name__
            return core_schema.CoreConfig(**values)
        if current.get("type") == "definitions":
            pending.append(current.get("schema"))
            definitions = current.get("definitions")
            if isinstance(definitions, list):
                pending.extend(
                    definition
                    for definition in definitions
                    if isinstance(definition, Mapping)
                    and definition.get("ref") in reachable_references
                )
            continue
        pending.extend(
            current[key] for key in _validation_schema_child_keys(current) if key in current
        )
    return core_schema.CoreConfig(title=model.model_config.get("title") or model.__name__)


def _render_validation_model_references(
    value: Any,
    *,
    reachable_references: frozenset[str],
) -> dict[str, type[BaseModel]]:
    references: dict[str, type[BaseModel]] = {}
    while True:
        previous = dict(references)
        _collect_render_validation_model_references(
            value,
            references,
            reachable_references=reachable_references,
        )
        if references == previous:
            return references


def _collect_render_validation_model_references(
    value: Any,
    references: dict[str, type[BaseModel]],
    *,
    reachable_references: frozenset[str],
) -> None:
    if isinstance(value, dict):
        schema_ref = value.get("ref")
        if isinstance(schema_ref, str):
            model = _render_validation_schema_model(value, model_references=references)
            if model is not None:
                references[schema_ref] = model
        if value.get("type") == "definitions":
            _collect_render_validation_model_references(
                value.get("schema"),
                references,
                reachable_references=reachable_references,
            )
            definitions = value.get("definitions")
            if isinstance(definitions, list):
                for definition in definitions:
                    if (
                        isinstance(definition, dict)
                        and definition.get("ref") in reachable_references
                    ):
                        _collect_render_validation_model_references(
                            definition,
                            references,
                            reachable_references=reachable_references,
                        )
            return
        for key in _validation_schema_child_keys(value):
            if key not in value:
                continue
            _collect_render_validation_model_references(
                value[key],
                references,
                reachable_references=reachable_references,
            )
        return
    if isinstance(value, list | tuple):
        for item in value:
            _collect_render_validation_model_references(
                item,
                references,
                reachable_references=reachable_references,
            )


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
    reachable_references: frozenset[str],
    reference_changes: Mapping[str, _SchemaChanges],
    discovered_reference_changes: dict[str, _SchemaChanges],
    carrier_cache: dict[type[BaseModel], type[BaseModel]],
    hash_required: bool,
    allow_enum_bridges: bool,
    bypass_materialized_models: bool,
    schema_transform: _ValidationSchemaTransform | None,
) -> tuple[Any, _SchemaChanges]:
    """Clone an authoritative schema and bridge changed model definitions."""
    if isinstance(value, list | tuple):
        cloned_items = [
            _clone_render_validation_schema(
                item,
                family_name=family_name,
                model_references=model_references,
                reachable_references=reachable_references,
                reference_changes=reference_changes,
                discovered_reference_changes=discovered_reference_changes,
                carrier_cache=carrier_cache,
                hash_required=hash_required,
                allow_enum_bridges=allow_enum_bridges,
                bypass_materialized_models=bypass_materialized_models,
                schema_transform=schema_transform,
            )
            for item in value
        ]
        cloned_values = [item for item, _changes in cloned_items]
        if isinstance(value, tuple):
            return tuple(cloned_values), _combined_schema_changes(cloned_items)
        return cloned_values, _combined_schema_changes(cloned_items)
    if not isinstance(value, dict):
        return value, 0

    schema_type = value.get("type")
    child_enum_bridges_allowed = allow_enum_bridges and not (
        schema_type == "model" and value.get("custom_init") is True
    )
    if hash_required:
        output_model = _render_validation_schema_model(
            value,
            model_references=model_references,
        )
        if output_model is not None:
            cloned, changes = _clone_render_validation_schema(
                value,
                family_name=family_name,
                model_references=model_references,
                reachable_references=reachable_references,
                reference_changes=reference_changes,
                discovered_reference_changes=discovered_reference_changes,
                carrier_cache=carrier_cache,
                hash_required=False,
                allow_enum_bridges=child_enum_bridges_allowed,
                bypass_materialized_models=bypass_materialized_models,
                schema_transform=schema_transform,
            )
            unwrapped = core_schema.no_info_before_validator_function(
                _unwrap_hashable_canonical_mapping,
                cast(core_schema.CoreSchema, cloned),
            )
            if output_model.__hash__ is not None:
                return unwrapped, changes | _STRUCTURAL_CHANGE
            carrier = _build_hashable_render_carrier(
                model=output_model,
                cache=carrier_cache,
            )
            return (
                core_schema.no_info_after_validator_function(
                    partial(_construct_hashable_render_carrier, carrier),
                    unwrapped,
                ),
                changes | _STRUCTURAL_CHANGE,
            )
    if schema_type == "definitions":
        definitions = []
        for definition in value.get("definitions", []):
            if isinstance(definition, dict) and definition.get("ref") in reachable_references:
                cloned_definition, _changes = _clone_render_validation_schema(
                    definition,
                    family_name=family_name,
                    model_references=model_references,
                    reachable_references=reachable_references,
                    reference_changes=reference_changes,
                    discovered_reference_changes=discovered_reference_changes,
                    carrier_cache=carrier_cache,
                    hash_required=False,
                    allow_enum_bridges=child_enum_bridges_allowed,
                    bypass_materialized_models=bypass_materialized_models,
                    schema_transform=schema_transform,
                )
                definitions.append(cloned_definition)
            else:
                definitions.append(definition)
        schema, changes = _clone_render_validation_schema(
            value.get("schema"),
            family_name=family_name,
            model_references=model_references,
            reachable_references=reachable_references,
            reference_changes=reference_changes,
            discovered_reference_changes=discovered_reference_changes,
            carrier_cache=carrier_cache,
            hash_required=hash_required,
            allow_enum_bridges=child_enum_bridges_allowed,
            bypass_materialized_models=bypass_materialized_models,
            schema_transform=schema_transform,
        )
        cloned = dict(value)
        cloned["definitions"] = definitions
        cloned["schema"] = schema
        return cloned, changes
    if schema_type == "definition-ref":
        schema_ref = value.get("schema_ref")
        changes = reference_changes.get(schema_ref, 0) if isinstance(schema_ref, str) else 0
        if not allow_enum_bridges:
            changes &= ~(_ENUM_BRIDGE_CHANGE | _ENUM_UNION_CHANGE | _APPLICATION_VALIDATOR_CHANGE)
        return dict(value), changes

    propagate_hash_keys: set[str] = set()
    if schema_type is None and hash_required:
        propagate_hash_keys.update(value)
    elif schema_type in {"set", "frozenset"}:
        propagate_hash_keys.add("items_schema")
    elif schema_type == "dict":
        propagate_hash_keys.add("keys_schema")
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

    child_keys = frozenset(_validation_schema_child_keys(value))
    cloned_items: dict[str, tuple[Any, _SchemaChanges]] = {}
    for key, item in value.items():
        if key not in child_keys:
            cloned_items[key] = (item, 0)
            continue
        cloned_items[key] = _clone_render_validation_schema(
            item,
            family_name=family_name,
            model_references=model_references,
            reachable_references=reachable_references,
            reference_changes=reference_changes,
            discovered_reference_changes=discovered_reference_changes,
            carrier_cache=carrier_cache,
            hash_required=key in propagate_hash_keys,
            allow_enum_bridges=child_enum_bridges_allowed,
            bypass_materialized_models=bypass_materialized_models,
            schema_transform=schema_transform,
        )
    cloned = {key: item for key, (item, _changes) in cloned_items.items()}
    changes = _combined_schema_changes(cloned_items.values())
    if schema_transform is not None:
        cloned, transformed = schema_transform(
            cloned,
            changes,
            allow_enum_bridges,
            hash_required,
        )
        changes |= transformed
    if schema_type == "model" and changes:
        model = value.get("cls")
        if not isinstance(model, type) or not issubclass(model, BaseModel):
            return cloned, changes
        if value.get("custom_init"):
            msg = (
                f"Automatic canonical validation for family {family_name!r} "
                f"cannot safely execute custom __init__ on model {model.__qualname__!r}"
            )
            raise UnsupportedWireModelError(msg)
        if model.__new__ is not object.__new__:
            msg = (
                f"Automatic canonical validation for family {family_name!r} "
                f"cannot safely execute custom __new__ on model {model.__qualname__!r}"
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
    output_model = _render_validation_schema_model(
        value,
        model_references=model_references,
    )
    if bypass_materialized_models and isinstance(schema_ref, str) and output_model is not None:
        nested = dict(cloned)
        nested.pop("ref", None)
        cloned = core_schema.no_info_wrap_validator_function(
            partial(_validate_or_bypass_materialized_model, output_model),
            cast(core_schema.CoreSchema, nested),
            ref=schema_ref,
        )
        changes |= _STRUCTURAL_CHANGE
    if changes and isinstance(schema_ref, str):
        discovered_reference_changes[schema_ref] = (
            discovered_reference_changes.get(schema_ref, 0) | changes
        )
    return cloned, changes


def _combined_schema_changes(items: Any) -> _SchemaChanges:
    changes = 0
    for _item, item_changes in items:
        changes |= item_changes
    return changes


def _unwrap_hashable_canonical_mapping(value: Any) -> Any:
    if isinstance(value, _HashableCanonicalMapping):
        return dict(value)
    return value


def _validate_or_bypass_materialized_model(
    model: type[BaseModel],
    value: Any,
    handler: Any,
) -> Any:
    # Imported lazily to keep the shared schema clone usable by canonical
    # validation without introducing a module-import cycle.
    from pydantic_versions._runtime_validation import (  # noqa: PLC0415
        _is_call_local_materialized_model,
    )

    if type(value) is model and _is_call_local_materialized_model(value):
        return value
    return handler(value)


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
