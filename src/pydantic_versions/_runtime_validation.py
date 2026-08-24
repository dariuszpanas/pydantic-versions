"""Canonical-payload validation against authoritative Pydantic core schemas."""

from __future__ import annotations

import operator
from collections.abc import Mapping, Sized
from contextvars import ContextVar
from enum import Enum
from functools import partial
from typing import TYPE_CHECKING, Any, NoReturn, cast

from pydantic import BaseModel
from pydantic_core import (
    PydanticCustomError,
    SchemaError,
    SchemaValidator,
    ValidationError,
    core_schema,
)

from pydantic_versions._runtime_payload import (
    _canonical_source_identities,
    _extract_declared_fields,
    _HashableCanonicalMapping,
    _model_revalidation_input,
)
from pydantic_versions._runtime_render import (
    _APPLICATION_VALIDATOR_CHANGE,
    _CARRIER_UNWRAP_CHANGE,
    _ENUM_BRIDGE_CHANGE,
    _ENUM_UNION_CHANGE,
    _OPAQUE_HASH_CHANGE,
    _STRUCTURAL_CHANGE,
    _authoritative_core_config,
    _build_adapted_validation_schema,
)
from pydantic_versions.exceptions import InvalidMigrationError, UnsupportedWireModelError

if TYPE_CHECKING:
    from pydantic_versions._compiler import _CompiledFamilyRuntimeCache


_COLLECTION_CARDINALITY_ERROR = "pydantic_versions_collection_cardinality"
_ENUM_UNION_TYPE_ERROR = "pydantic_versions_enum_union_type"
_PRIVATE_CARRIER_ERROR = "pydantic_versions_private_carrier"
_CANONICAL_ERROR_TYPES = {
    _COLLECTION_CARDINALITY_ERROR,
    _ENUM_UNION_TYPE_ERROR,
    _PRIVATE_CARRIER_ERROR,
}
_ATTRGETTER_TYPE = type(operator.attrgetter("value"))
_NOT_FOUND = object()
_UNION_CHOICE_LABEL_METADATA = "pydantic_versions_union_choice_label"


class _CanonicalUnionFrame:
    __slots__ = ("choice_results", "suppressed_enum")

    def __init__(self) -> None:
        self.suppressed_enum = False
        self.choice_results: list[tuple[Any, bool]] = []


class _CanonicalChoiceFrame:
    __slots__ = ("application_validator_ran", "collection_violation")

    def __init__(self) -> None:
        self.application_validator_ran = False
        self.collection_violation: tuple[str, int, int] | None = None


class _CanonicalApplicationFrame:
    __slots__ = ("collection_violation",)

    def __init__(self) -> None:
        self.collection_violation: tuple[str, int, int] | None = None


_CANONICAL_UNION_FRAMES: ContextVar[tuple[_CanonicalUnionFrame, ...]] = ContextVar(
    "pydantic_versions_canonical_union_frames",
    default=(),
)
_CANONICAL_CHOICE_FRAMES: ContextVar[tuple[_CanonicalChoiceFrame, ...]] = ContextVar(
    "pydantic_versions_canonical_choice_frames",
    default=(),
)
_CANONICAL_APPLICATION_FRAMES: ContextVar[tuple[_CanonicalApplicationFrame, ...]] = ContextVar(
    "pydantic_versions_canonical_application_frames",
    default=(),
)
_MATERIALIZED_MODEL_IDS: ContextVar[frozenset[int]] = ContextVar(
    "pydantic_versions_materialized_model_ids",
    default=frozenset(),
)
_MATERIALIZED_CONTAINER_IDS: ContextVar[frozenset[int]] = ContextVar(
    "pydantic_versions_materialized_container_ids",
    default=frozenset(),
)


def _is_call_local_materialized_model(value: BaseModel) -> bool:
    return id(value) in _MATERIALIZED_MODEL_IDS.get()


def _validate_canonical_model[T: BaseModel](
    model: type[T],
    payload: Mapping[str, Any],
    *,
    cache: _CompiledFamilyRuntimeCache | None = None,
    by_name: bool | None = True,
    materialized_model_ids: frozenset[int] = frozenset(),
    materialized_container_ids: frozenset[int] = frozenset(),
) -> T:
    """Validate an already-canonical payload exactly once at a model boundary."""
    validation_payload: Mapping[str, Any] = (
        dict(payload) if isinstance(payload, _HashableCanonicalMapping) else payload
    )
    guard_collections = bool(materialized_container_ids) or _contains_hashable_canonical_mapping(
        validation_payload
    )
    adapter = _canonical_validation_adapter(
        model,
        cache=cache,
        guard_collections=guard_collections,
        materialized_models=bool(materialized_model_ids),
    )
    if adapter is not None:
        _ensure_canonical_validation_supported(model)
    union_token = _CANONICAL_UNION_FRAMES.set(())
    choice_token = _CANONICAL_CHOICE_FRAMES.set(())
    application_token = _CANONICAL_APPLICATION_FRAMES.set(())
    model_token = _MATERIALIZED_MODEL_IDS.set(materialized_model_ids)
    container_token = _MATERIALIZED_CONTAINER_IDS.set(materialized_container_ids)
    try:
        if adapter is None:
            return model.model_validate(validation_payload, by_name=by_name)
        return cast(
            T,
            adapter.validate_python(validation_payload, by_name=by_name),
        )
    except ValidationError as exc:
        _raise_canonical_migration_error(exc)
        raise
    finally:
        _MATERIALIZED_CONTAINER_IDS.reset(container_token)
        _MATERIALIZED_MODEL_IDS.reset(model_token)
        _CANONICAL_APPLICATION_FRAMES.reset(application_token)
        _CANONICAL_CHOICE_FRAMES.reset(choice_token)
        _CANONICAL_UNION_FRAMES.reset(union_token)


def _revalidate_canonical_model_instance[T: BaseModel](
    model: type[T],
    value: BaseModel,
    *,
    cache: _CompiledFamilyRuntimeCache | None = None,
) -> T:
    """Revalidate one exact/subclass instance with its complete native state."""
    adapter = _canonical_validation_adapter(
        model,
        cache=cache,
        guard_collections=False,
        materialized_models=False,
    )
    if adapter is None:
        return model.model_validate(value)
    validated = _validate_canonical_model(
        model,
        _model_revalidation_input(value),
        cache=cache,
        by_name=None,
    )
    object.__setattr__(
        validated,
        "__pydantic_fields_set__",
        set(value.__pydantic_fields_set__),
    )
    return validated


def _validate_canonical_adapter_payload(
    adapter: SchemaValidator,
    payload: Mapping[str, Any],
    *,
    by_name: bool = True,
) -> BaseModel:
    """Validate through a caller-owned canonical adapter and translate invariants."""
    union_token = _CANONICAL_UNION_FRAMES.set(())
    choice_token = _CANONICAL_CHOICE_FRAMES.set(())
    application_token = _CANONICAL_APPLICATION_FRAMES.set(())
    model_token = _MATERIALIZED_MODEL_IDS.set(frozenset())
    container_token = _MATERIALIZED_CONTAINER_IDS.set(frozenset())
    try:
        return cast(BaseModel, adapter.validate_python(payload, by_name=by_name))
    except ValidationError as exc:
        _raise_canonical_migration_error(exc)
        raise
    finally:
        _MATERIALIZED_CONTAINER_IDS.reset(container_token)
        _MATERIALIZED_MODEL_IDS.reset(model_token)
        _CANONICAL_APPLICATION_FRAMES.reset(application_token)
        _CANONICAL_CHOICE_FRAMES.reset(choice_token)
        _CANONICAL_UNION_FRAMES.reset(union_token)


def _canonical_validation_adapter(
    model: type[BaseModel],
    *,
    cache: _CompiledFamilyRuntimeCache | None,
    guard_collections: bool,
    materialized_models: bool,
) -> SchemaValidator | None:
    core_schema_identity = model.__pydantic_core_schema__
    cache_key = (model, guard_collections, materialized_models)
    if cache is not None:
        with cache.lock:
            cached = cache.canonical_adapters.get(cache_key)
            if cached is not None and cached[0] is core_schema_identity:
                return cast(SchemaValidator | None, cached[1])
            adapter = _build_canonical_validation_adapter(
                model,
                guard_collections=guard_collections,
                materialized_models=materialized_models,
            )
            cache.canonical_adapters[cache_key] = (core_schema_identity, adapter)
            return adapter
    return _build_canonical_validation_adapter(
        model,
        guard_collections=guard_collections,
        materialized_models=materialized_models,
    )


def _build_canonical_validation_adapter(
    model: type[BaseModel],
    *,
    guard_collections: bool,
    materialized_models: bool,
) -> SchemaValidator | None:
    schema, changes = _build_canonical_validation_schema(
        model,
        family_name=model.__qualname__,
        bypass_materialized_models=materialized_models,
        guard_collections=guard_collections,
    )
    if not changes:
        return None
    return SchemaValidator(
        schema,
        config=_authoritative_core_config(model),
    )


def _build_canonical_validation_schema(
    model: type[BaseModel],
    *,
    family_name: str,
    bypass_materialized_models: bool,
    guard_collections: bool,
) -> tuple[core_schema.CoreSchema, int]:
    """Build one canonical schema, enabling app tracing only when required."""
    instrument_applications = guard_collections
    schema, changes = _build_adapted_validation_schema(
        model,
        family_name=family_name,
        bypass_materialized_models=bypass_materialized_models,
        schema_transform=partial(
            _canonical_validation_schema_transform,
            guard_collections=guard_collections,
            instrument_applications=instrument_applications,
        ),
    )
    if not instrument_applications and changes & _ENUM_UNION_CHANGE:
        schema, changes = _build_adapted_validation_schema(
            model,
            family_name=family_name,
            bypass_materialized_models=bypass_materialized_models,
            schema_transform=partial(
                _canonical_validation_schema_transform,
                guard_collections=guard_collections,
                instrument_applications=True,
            ),
        )
    if changes:
        _ensure_canonical_validation_supported(model)
    return schema, changes


def _ensure_canonical_validation_supported(model: type[BaseModel]) -> None:
    callback = getattr(model.model_validate, "__func__", model.model_validate)
    standard = getattr(BaseModel.model_validate, "__func__", BaseModel.model_validate)
    if callback is not standard:
        msg = (
            f"Automatic canonical validation for model {model.__qualname__!r} "
            "cannot safely bypass an overridden model_validate"
        )
        raise UnsupportedWireModelError(msg)
    if type(model.__pydantic_validator__) is not SchemaValidator:
        msg = (
            f"Automatic canonical validation for model {model.__qualname__!r} "
            "cannot safely bypass a wrapped __pydantic_validator__"
        )
        raise UnsupportedWireModelError(msg)


def _canonical_validation_schema_transform(
    schema: dict[str, Any],
    descendant_changes: int,
    allow_enum_bridges: bool,
    hash_required: bool,
    *,
    guard_collections: bool,
    instrument_applications: bool,
) -> tuple[dict[str, Any], int]:
    """Add only canonical-input bridges local to authoritative schema nodes."""
    schema_type = schema.get("type")
    if allow_enum_bridges and schema_type == "function-after":
        enum_values = _enum_values(schema)
        if enum_values is not None:
            choice_label = SchemaValidator(cast(core_schema.CoreSchema, schema)).title
            wrapped = _wrap_validation_schema(
                partial(_retry_enum_value, enum_values=enum_values),
                schema,
            )
            metadata = wrapped.get("metadata")
            wrapped["metadata"] = {
                **(dict(metadata) if isinstance(metadata, Mapping) else {}),
                _UNION_CHOICE_LABEL_METADATA: choice_label,
            }
            return (
                wrapped,
                _ENUM_BRIDGE_CHANGE,
            )
    if guard_collections and schema_type == "any":
        if hash_required:
            return (
                _before_validation_schema(_reject_hash_required_canonical_carrier, schema),
                _STRUCTURAL_CHANGE | _OPAQUE_HASH_CHANGE,
            )
        return (
            _before_validation_schema(_unwrap_non_hash_canonical_value, schema),
            _CARRIER_UNWRAP_CHANGE,
        )
    if guard_collections and schema_type in {"dict", "set", "frozenset"}:
        wrapped = _wrap_validation_schema(
            partial(_guard_collection_cardinality, collection_type=schema_type),
            schema,
        )
        return wrapped, _STRUCTURAL_CHANGE
    if (
        instrument_applications
        and allow_enum_bridges
        and _is_application_validation_function(schema)
    ):
        wrapped = _wrap_validation_schema(_record_union_application_result, schema)
        changes = _APPLICATION_VALIDATOR_CHANGE
        if hash_required or descendant_changes & _OPAQUE_HASH_CHANGE:
            wrapped = _before_validation_schema(
                _reject_hash_required_canonical_carrier,
                wrapped,
            )
            changes |= _STRUCTURAL_CHANGE
        elif (
            not hash_required
            and not descendant_changes & _STRUCTURAL_CHANGE
            and schema_type
            in {
                "function-before",
                "function-plain",
                "function-wrap",
            }
        ):
            wrapped = _before_validation_schema(_unwrap_non_hash_canonical_value, wrapped)
            changes |= _CARRIER_UNWRAP_CHANGE
        if (
            guard_collections
            and descendant_changes & _STRUCTURAL_CHANGE
            and schema_type == "function-before"
        ):
            wrapped = _wrap_validation_schema(
                partial(_guard_collection_cardinality, collection_type="before-validator"),
                wrapped,
            )
            changes |= _STRUCTURAL_CHANGE
        return wrapped, changes
    if (
        guard_collections
        and descendant_changes & _STRUCTURAL_CHANGE
        and schema_type == "function-before"
    ):
        wrapped = _wrap_validation_schema(
            partial(_guard_collection_cardinality, collection_type="before-validator"),
            schema,
        )
        return wrapped, _STRUCTURAL_CHANGE
    if schema_type == "union" and descendant_changes & (_ENUM_BRIDGE_CHANGE | _STRUCTURAL_CHANGE):
        labeled = _label_canonical_union_choices(schema)
        instrumented = (
            _instrument_union_application_choices(labeled)
            if instrument_applications
            and descendant_changes & (_APPLICATION_VALIDATOR_CHANGE | _STRUCTURAL_CHANGE)
            else labeled
        )
        if descendant_changes & _ENUM_BRIDGE_CHANGE:
            return (
                _wrap_validation_schema(_validate_canonical_union, instrumented),
                _ENUM_BRIDGE_CHANGE | _ENUM_UNION_CHANGE,
            )
        return instrumented, 0
    return schema, 0


def _wrap_validation_schema(callback: Any, schema: dict[str, Any]) -> dict[str, Any]:
    nested = dict(schema)
    schema_ref = nested.pop("ref", None)
    wrapped = core_schema.no_info_wrap_validator_function(
        callback,
        cast(core_schema.CoreSchema, nested),
        ref=schema_ref,
    )
    return cast(dict[str, Any], wrapped)


def _before_validation_schema(callback: Any, schema: dict[str, Any]) -> dict[str, Any]:
    nested = dict(schema)
    schema_ref = nested.pop("ref", None)
    wrapped = core_schema.no_info_before_validator_function(
        callback,
        cast(core_schema.CoreSchema, nested),
        ref=schema_ref,
    )
    return cast(dict[str, Any], wrapped)


def _enum_values(schema: Mapping[str, Any]) -> tuple[Enum, ...] | None:
    function = schema.get("function")
    nested = schema.get("schema")
    if not isinstance(function, Mapping) or not isinstance(nested, Mapping):
        return None
    callback = function.get("function")
    if (
        nested.get("type") == "enum"
        and isinstance(nested.get("cls"), type)
        and type(callback) is _ATTRGETTER_TYPE
        and repr(callback) == "operator.attrgetter('value')"
    ):
        return tuple(nested["cls"])
    expected = tuple(nested.get("expected", ()))
    if (
        nested.get("type") == "literal"
        and any(isinstance(item, Enum) for item in expected)
        and getattr(callback, "__module__", "") == "pydantic._internal._generate_schema"
        and getattr(callback, "__qualname__", "")
        == "GenerateSchema._literal_schema.<locals>.<lambda>"
    ):
        return tuple(item for item in expected if isinstance(item, Enum))
    return None


def _retry_enum_value(
    value: Any,
    handler: Any,
    *,
    enum_values: tuple[Enum, ...],
) -> Any:
    validation_error: ValidationError | None = None
    try:
        validated = handler(value)
    except ValidationError as exc:
        validation_error = exc
        validated = _NOT_FOUND
    if validated is not _NOT_FOUND and _same_python_value(value, validated):
        return validated
    matches = tuple(member for member in enum_values if _same_python_value(value, member.value))
    if len(matches) != 1:
        if validated is not _NOT_FOUND:
            return validated
        assert validation_error is not None
        raise validation_error
    if _suppress_enum_repair_inside_union():
        if validation_error is not None:
            raise validation_error
        return validated
    return handler(matches[0])


def _suppress_enum_repair_inside_union() -> bool:
    frames = _CANONICAL_UNION_FRAMES.get()
    if not frames:
        return False
    for frame in frames:
        frame.suppressed_enum = True
    return True


def _instrument_union_application_choices(schema: dict[str, Any]) -> dict[str, Any]:
    choices = schema.get("choices")
    if not isinstance(choices, list):
        return schema

    def instrument(choice: Any) -> Any:
        if isinstance(choice, tuple) and choice and isinstance(choice[0], dict):
            return (
                _wrap_validation_schema(_validate_canonical_union_choice, choice[0]),
                *choice[1:],
            )
        if isinstance(choice, dict):
            wrapped = _wrap_validation_schema(_validate_canonical_union_choice, choice)
            try:
                choice_label = SchemaValidator(cast(core_schema.CoreSchema, choice)).title
            except SchemaError:
                return wrapped
            return (wrapped, choice_label)
        return choice

    instrumented = dict(schema)
    instrumented["choices"] = [instrument(choice) for choice in choices]
    return instrumented


def _label_canonical_union_choices(schema: dict[str, Any]) -> dict[str, Any]:
    choices = schema.get("choices")
    if not isinstance(choices, list):
        return schema

    def label(choice: Any) -> Any:
        if not isinstance(choice, dict):
            return choice
        metadata = choice.get("metadata")
        choice_label = (
            metadata.get(_UNION_CHOICE_LABEL_METADATA) if isinstance(metadata, Mapping) else None
        )
        return (choice, choice_label) if isinstance(choice_label, str) else choice

    labeled = dict(schema)
    labeled["choices"] = [label(choice) for choice in choices]
    return labeled


def _is_application_validation_function(schema: dict[str, Any]) -> bool:
    if schema.get("type") not in {
        "function-after",
        "function-before",
        "function-plain",
        "function-wrap",
    }:
        return False
    if _enum_values(schema) is not None:
        return False
    function = schema.get("function")
    callback = function.get("function") if isinstance(function, Mapping) else None
    while isinstance(callback, partial):
        callback = callback.func
    callback = getattr(callback, "__func__", callback)
    if any(
        callback is internal
        for internal in (
            _guard_collection_cardinality,
            _record_union_application_result,
            _retry_enum_value,
            _validate_canonical_union,
            _validate_canonical_union_choice,
        )
    ):
        return False
    module = getattr(callback, "__module__", None)
    if module is None:
        module = getattr(type(callback), "__module__", "")
    if module == "pydantic" or module == "pydantic_core":
        return False
    if module.startswith(("pydantic.", "pydantic_core.")):
        return False
    return True


def _record_union_application_result(value: Any, handler: Any) -> Any:
    frame = _CanonicalApplicationFrame()
    frames = _CANONICAL_APPLICATION_FRAMES.get()
    token = _CANONICAL_APPLICATION_FRAMES.set((*frames, frame))
    try:
        validated = handler(value)
    finally:
        _CANONICAL_APPLICATION_FRAMES.reset(token)
    if frame.collection_violation is not None:
        _raise_collection_cardinality_error(*frame.collection_violation)
    choice_frames = _CANONICAL_CHOICE_FRAMES.get()
    if choice_frames:
        choice_frames[-1].application_validator_ran = True
    return validated


def _validate_canonical_union_choice(value: Any, handler: Any) -> Any:
    frame = _CanonicalChoiceFrame()
    frames = _CANONICAL_CHOICE_FRAMES.get()
    token = _CANONICAL_CHOICE_FRAMES.set((*frames, frame))
    try:
        validated = handler(value)
    finally:
        _CANONICAL_CHOICE_FRAMES.reset(token)
    if frame.collection_violation is not None:
        _raise_collection_cardinality_error(*frame.collection_violation)
    union_frames = _CANONICAL_UNION_FRAMES.get()
    if union_frames:
        union_frames[-1].choice_results.append(
            (validated, frame.application_validator_ran),
        )
    return validated


def _validate_canonical_union(value: Any, handler: Any) -> Any:
    frame = _CanonicalUnionFrame()
    frames = _CANONICAL_UNION_FRAMES.get()
    token = _CANONICAL_UNION_FRAMES.set((*frames, frame))
    try:
        validated = handler(value)
    finally:
        _CANONICAL_UNION_FRAMES.reset(token)
    matching_choice_results = tuple(
        application_ran for result, application_ran in frame.choice_results if validated is result
    )
    application_result_selected = bool(matching_choice_results) and all(matching_choice_results)
    if application_result_selected:
        choice_frames = _CANONICAL_CHOICE_FRAMES.get()
        if choice_frames:
            choice_frames[-1].application_validator_ran = True
    if (
        frame.suppressed_enum
        and not application_result_selected
        and not _same_canonical_python_shape(value, validated)
    ):
        raise PydanticCustomError(
            _ENUM_UNION_TYPE_ERROR,
            "Canonical union validation would change a stored enum value",
        )
    return validated


def _contains_hashable_canonical_mapping(value: Any) -> bool:
    pending = [value]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if isinstance(current, _HashableCanonicalMapping):
            return True
        if not isinstance(current, Mapping | list | tuple | set | frozenset):
            continue
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        if isinstance(current, Mapping):
            pending.extend(current.keys())
            pending.extend(current.values())
        else:
            pending.extend(current)
    return False


def _unwrap_non_hash_canonical_value(value: Any) -> Any:
    if not _contains_hashable_canonical_mapping(value):
        return value
    return _unwrap_non_hash_canonical_value_with_memo(value, memo={})


def _unwrap_non_hash_canonical_value_with_memo(
    value: Any,
    *,
    memo: dict[int, Any],
) -> Any:
    identity = id(value)
    if identity in memo:
        return memo[identity]
    if isinstance(value, _HashableCanonicalMapping) or type(value) is dict:
        copied: dict[Any, Any] = {}
        memo[identity] = copied
        try:
            for key, item in value.items():
                copied[_unwrap_non_hash_canonical_value_with_memo(key, memo=memo)] = (
                    _unwrap_non_hash_canonical_value_with_memo(item, memo=memo)
                )
        except TypeError:
            _raise_private_carrier_error()
        return copied
    if type(value) is list:
        copied_list: list[Any] = []
        memo[identity] = copied_list
        copied_list.extend(
            _unwrap_non_hash_canonical_value_with_memo(item, memo=memo) for item in value
        )
        return copied_list
    if type(value) is tuple:
        memo[identity] = value
        copied_tuple = tuple(
            _unwrap_non_hash_canonical_value_with_memo(item, memo=memo) for item in value
        )
        memo[identity] = copied_tuple
        return copied_tuple
    if type(value) in {set, frozenset}:
        try:
            copied_collection = type(value)(
                _unwrap_non_hash_canonical_value_with_memo(item, memo=memo) for item in value
            )
        except TypeError:
            _raise_private_carrier_error()
        memo[identity] = copied_collection
        return copied_collection
    return value


def _reject_hash_required_canonical_carrier(value: Any) -> Any:
    if _contains_hashable_canonical_mapping(value):
        _raise_private_carrier_error()
    return value


def _raise_private_carrier_error() -> NoReturn:
    raise PydanticCustomError(
        _PRIVATE_CARRIER_ERROR,
        "Canonical validation cannot expose a private hash carrier",
    )


def _same_canonical_python_shape(
    left: Any,
    right: Any,
    active_pairs: set[tuple[int, int]] | None = None,
) -> bool:
    if left is right:
        return True
    if isinstance(left, _HashableCanonicalMapping):
        left = dict(left)
    if isinstance(right, BaseModel) and isinstance(left, Mapping):
        right = _extract_declared_fields(right, declared_model=type(right))
    if isinstance(right, _HashableCanonicalMapping):
        right = dict(right)
    if type(left) is not type(right):
        return False
    if not isinstance(left, Mapping | list | tuple | set | frozenset):
        return True
    if active_pairs is None:
        active_pairs = set()
    pair = (id(left), id(right))
    if pair in active_pairs:
        return True
    active_pairs.add(pair)
    try:
        return _same_canonical_container_shape(left, right, active_pairs)
    finally:
        active_pairs.remove(pair)


def _same_canonical_container_shape(
    left: Mapping[Any, Any] | list[Any] | tuple[Any, ...] | set[Any] | frozenset[Any],
    right: Any,
    active_pairs: set[tuple[int, int]],
) -> bool:
    if isinstance(left, Mapping):
        if len(left) != len(right):
            return False
        unmatched = list(right.items())
        for left_key, left_value in left.items():
            match = next(
                (
                    index
                    for index, (right_key, right_value) in enumerate(unmatched)
                    if _same_canonical_python_shape(left_key, right_key, active_pairs)
                    and _same_canonical_python_shape(left_value, right_value, active_pairs)
                ),
                None,
            )
            if match is None:
                return False
            unmatched.pop(match)
        return True
    if isinstance(left, list | tuple):
        return len(left) == len(right) and all(
            _same_canonical_python_shape(left_item, right_item, active_pairs)
            for left_item, right_item in zip(left, right, strict=True)
        )
    if isinstance(left, set | frozenset):
        if len(left) != len(right):
            return False
        unmatched = list(right)
        for left_item in left:
            match = next(
                (
                    index
                    for index, right_item in enumerate(unmatched)
                    if _same_canonical_python_shape(left_item, right_item, active_pairs)
                ),
                None,
            )
            if match is None:
                return False
            unmatched.pop(match)
        return True
    return False


def _guard_collection_cardinality(
    value: Any,
    handler: Any,
    *,
    collection_type: str,
) -> Any:
    if id(value) in _MATERIALIZED_CONTAINER_IDS.get():
        guarded = True
    elif collection_type in {"set", "frozenset", "before-validator"} and isinstance(
        value,
        list | tuple | set | frozenset,
    ):
        guarded = any(_canonical_source_identities(item) for item in value)
    elif isinstance(value, Mapping):
        guarded = isinstance(value, Mapping) and any(
            _canonical_source_identities(key) for key in value
        )
    else:
        guarded = False
    if not guarded:
        return handler(value)
    before = len(value) if isinstance(value, Sized) else None
    validated = handler(value)
    after = len(validated) if isinstance(validated, Sized) else None
    if before is not None and after is not None and before != after:
        violation = (collection_type, before, after)
        choice_frames = _CANONICAL_CHOICE_FRAMES.get()
        if choice_frames:
            choice_frames[-1].collection_violation = violation
        else:
            for frame in _CANONICAL_APPLICATION_FRAMES.get():
                frame.collection_violation = violation
        _raise_collection_cardinality_error(collection_type, before, after)
    return validated


def _raise_collection_cardinality_error(
    collection_type: str,
    before: int,
    after: int,
) -> NoReturn:
    raise PydanticCustomError(
        _COLLECTION_CARDINALITY_ERROR,
        "Canonical {collection_type} validation changed cardinality from {before} to {after}",
        {
            "collection_type": collection_type,
            "before": before,
            "after": after,
        },
    )


def _same_python_value(left: Any, right: Any) -> bool:
    if left is right:
        return True
    if type(left) is not type(right):
        return False
    try:
        return bool(left == right)
    except (TypeError, ValueError):
        return left is right


def _raise_canonical_migration_error(exc: ValidationError) -> None:
    errors = exc.errors(include_url=False)
    invariants = [error for error in errors if error.get("type") in _CANONICAL_ERROR_TYPES]
    if not invariants:
        return
    invariant = invariants[0]
    location = tuple(invariant.get("loc", ()))
    error_type = invariant.get("type")
    if error_type == _COLLECTION_CARDINALITY_ERROR:
        msg = (
            "Canonical validation cannot preserve set cardinality or mapping-key "
            f"cardinality at {location!r}"
        )
    elif error_type == _ENUM_UNION_TYPE_ERROR:
        msg = f"Canonical validation cannot preserve a stored enum value at {location!r}"
    elif error_type == _PRIVATE_CARRIER_ERROR:
        msg = f"Canonical validation cannot expose a private hash carrier at {location!r}"
    else:
        return
    raise InvalidMigrationError(msg) from exc
