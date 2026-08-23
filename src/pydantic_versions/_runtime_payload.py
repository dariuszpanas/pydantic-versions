"""Canonical payload extraction and schema-directed runtime traversal."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Mapping
from dataclasses import fields as dataclass_fields
from dataclasses import is_dataclass
from types import NoneType, UnionType
from typing import (
    Annotated,
    Any,
    Literal,
    NoReturn,
    TypeVar,
    Union,
    cast,
    get_args,
    get_origin,
    get_type_hints,
)
from typing import TypeAliasType as StdlibTypeAliasType
from typing import is_typeddict as stdlib_is_typeddict

from pydantic import BaseModel
from pydantic_core import to_jsonable_python
from typing_extensions import TypeAliasType as ExtensionsTypeAliasType  # noqa: UP035
from typing_extensions import is_typeddict as extensions_is_typeddict

from pydantic_versions._runtime_versioning import (
    _MISSING,
    _alias_paths,
    _serialized_field_name,
)
from pydantic_versions.exceptions import InvalidMigrationError

_TYPE_ALIAS_TYPES = (StdlibTypeAliasType, ExtensionsTypeAliasType)


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
    normalized = _runtime_annotation_value(annotation)
    origin = get_origin(normalized)
    if isinstance(normalized, TypeVar):
        candidates = tuple(
            _runtime_annotation_value(candidate)
            for candidate in _runtime_type_parameter_values(normalized)
        )
    elif origin in (Union, UnionType):
        candidates = tuple(
            _runtime_annotation_value(candidate) for candidate in get_args(normalized)
        )
    else:
        return normalized
    if not candidates:
        return normalized
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
    generic_matches: list[Any] = []
    for candidate in candidates:
        candidate_origin = get_origin(candidate)
        if candidate_origin is not None and isinstance(candidate_origin, type):
            try:
                if isinstance(value, candidate_origin):
                    generic_matches.append(candidate)
            except TypeError:
                continue
    shape_matches = tuple(
        candidate for candidate in candidates if _runtime_value_matches_annotation(value, candidate)
    )
    if len(shape_matches) == 1:
        return shape_matches[0]
    if shape_matches:
        return shape_matches[0]
    if generic_matches:
        return generic_matches[0]
    return normalized


def _safe_annotation_instance(value: Any, annotation: Any) -> bool:
    if not isinstance(annotation, type):
        return False
    try:
        return isinstance(value, annotation)
    except TypeError:
        return False


def _is_runtime_base_model_type(annotation: Any) -> bool:
    if not isinstance(annotation, type):
        return False
    try:
        return issubclass(annotation, BaseModel)
    except TypeError:
        return False


def _runtime_value_matches_annotation(value: Any, annotation: Any) -> bool:
    supertype = getattr(annotation, "__supertype__", None)
    if (
        isinstance(annotation, type)
        and type(value) is annotation
        and (supertype is None or supertype is annotation)
    ):
        return True
    normalized = _runtime_annotation_value(annotation)
    if normalized is Any:
        return True
    if isinstance(normalized, type) and type(value) is normalized:
        return True
    if isinstance(normalized, TypeVar):
        candidates = _runtime_type_parameter_values(normalized)
        return not candidates or any(
            _runtime_value_matches_annotation(value, candidate) for candidate in candidates
        )
    if _is_typed_dict(normalized):
        return _runtime_value_matches_typed_dict(value, normalized)
    origin = get_origin(normalized)
    arguments = get_args(normalized)
    if origin in (Union, UnionType):
        return any(_runtime_value_matches_annotation(value, item) for item in arguments)
    if origin is Literal:
        return value in arguments
    if origin in (list, set, frozenset):
        if not isinstance(value, origin):
            return False
        if not arguments:
            return True
        return all(_runtime_value_matches_annotation(item, arguments[0]) for item in value)
    if origin is tuple:
        if not isinstance(value, tuple):
            return False
        if not arguments:
            return not value
        if arguments[-1] is Ellipsis:
            return all(_runtime_value_matches_annotation(item, arguments[0]) for item in value)
        return len(value) == len(arguments) and all(
            _runtime_value_matches_annotation(item, item_annotation)
            for item, item_annotation in zip(value, arguments, strict=True)
        )
    if origin is dict:
        if not isinstance(value, dict):
            return False
        if len(arguments) != 2:
            return True
        key_annotation, item_annotation = arguments
        return all(
            _runtime_value_matches_annotation(key, key_annotation)
            and _runtime_value_matches_annotation(item, item_annotation)
            for key, item in value.items()
        )
    runtime_type = origin if isinstance(origin, type) else normalized
    return isinstance(runtime_type, type) and _safe_annotation_instance(value, runtime_type)


def _is_typed_dict(annotation: Any) -> bool:
    if stdlib_is_typeddict(annotation) or extensions_is_typeddict(annotation):
        return True
    origin = get_origin(annotation)
    return origin is not None and (stdlib_is_typeddict(origin) or extensions_is_typeddict(origin))


def _runtime_annotation_value(annotation: Any) -> Any:
    normalized = annotation
    while True:
        stripped = _strip_annotated(normalized)
        if stripped is not normalized:
            normalized = stripped
            continue
        if isinstance(normalized, _TYPE_ALIAS_TYPES):
            normalized = normalized.__value__
            continue
        origin = get_origin(normalized)
        if isinstance(origin, _TYPE_ALIAS_TYPES):
            arguments = get_args(normalized)
            replacements = list(zip(origin.__type_params__, arguments, strict=False))
            for parameter in origin.__type_params__[len(arguments) :]:
                default = _runtime_type_parameter_default(parameter)
                if default is not _MISSING:
                    replacements.append((parameter, default))
            normalized = _replace_runtime_type_parameters(
                origin.__value__,
                replacements=tuple(replacements),
            )
            continue
        supertype = getattr(normalized, "__supertype__", None)
        if supertype is not None and supertype is not normalized:
            normalized = supertype
            continue
        return normalized


def _replace_runtime_type_parameters(
    annotation: Any,
    *,
    replacements: tuple[tuple[Any, Any], ...],
) -> Any:
    replacement = next(
        (value for parameter, value in replacements if annotation is parameter),
        _MISSING,
    )
    if replacement is not _MISSING:
        return replacement
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin is None or not arguments or origin is Literal:
        return annotation
    rewritten = tuple(
        _replace_runtime_type_parameters(argument, replacements=replacements)
        if argument is not Ellipsis
        else argument
        for argument in arguments
    )
    if rewritten == arguments:
        return annotation
    if origin in (Union, UnionType):
        rewritten_union: Any = rewritten[0]
        for argument in rewritten[1:]:
            rewritten_union |= argument
        return rewritten_union
    if origin is Annotated:
        return Annotated[rewritten[0], *rewritten[1:]]
    parameters: Any = rewritten[0] if len(rewritten) == 1 else rewritten
    return origin[parameters]


def _runtime_type_parameter_values(parameter: TypeVar) -> tuple[Any, ...]:
    values = list(parameter.__constraints__)
    if parameter.__bound__ is not None:
        values.append(parameter.__bound__)
    default = _runtime_type_parameter_default(parameter)
    if default is not _MISSING:
        values.append(default)
    return tuple(values)


def _runtime_type_parameter_default(parameter: Any) -> Any:
    default = getattr(parameter, "__default__", _MISSING)
    default_is_sentinel = "NoDefault" in type(default).__name__ and repr(default) in (
        "typing.NoDefault",
        "typing_extensions.NoDefault",
    )
    if default is not _MISSING and not default_is_sentinel:
        return default
    return _MISSING


def _runtime_value_matches_typed_dict(value: Any, annotation: Any) -> bool:
    if not isinstance(value, dict):
        return False
    origin = _runtime_typed_dict_origin(annotation)
    assert origin is not None
    fields = _runtime_typed_dict_fields(annotation, origin=origin)
    required_keys = cast(frozenset[str], origin.__required_keys__)
    if not required_keys.issubset(value):
        return False
    if any(key not in fields for key in value):
        return False
    return all(
        key not in value or _runtime_value_matches_annotation(value[key], field_annotation)
        for key, field_annotation in fields.items()
    )


def _runtime_typed_dict_origin(annotation: Any) -> type[Any] | None:
    normalized = _runtime_annotation_value(annotation)
    if stdlib_is_typeddict(normalized) or extensions_is_typeddict(normalized):
        return normalized
    origin = get_origin(normalized)
    if stdlib_is_typeddict(origin) or extensions_is_typeddict(origin):
        return cast(type[Any], origin)
    return None


def _runtime_typed_dict_fields(
    annotation: Any,
    *,
    origin: type[Any],
) -> dict[str, Any]:
    try:
        fields = get_type_hints(origin, include_extras=True)
    except (NameError, TypeError):
        # Explicit wire-model compilation has already rejected unresolved
        # managed annotations. Keep this structural fallback for annotations
        # whose local namespace is no longer available to get_type_hints().
        fields = origin.__annotations__
    replacements = _runtime_generic_replacements(annotation, origin=origin)
    return {
        key: _runtime_typed_dict_field_annotation(
            _replace_runtime_type_parameters(field_annotation, replacements=replacements),
        )
        for key, field_annotation in fields.items()
    }


def _runtime_generic_replacements(
    annotation: Any,
    *,
    origin: type[Any],
) -> tuple[tuple[Any, Any], ...]:
    parameters = tuple(getattr(origin, "__type_params__", ())) or tuple(
        getattr(origin, "__parameters__", ()),
    )
    arguments = get_args(_runtime_annotation_value(annotation))
    replacements = list(zip(parameters, arguments, strict=False))
    for parameter in parameters[len(arguments) :]:
        default = _runtime_type_parameter_default(parameter)
        if default is not _MISSING:
            replacements.append((parameter, default))
    return tuple(replacements)


def _runtime_typed_dict_field_annotation(annotation: Any) -> Any:
    origin = get_origin(annotation)
    if _is_typed_dict_field_qualifier(origin):
        arguments = get_args(annotation)
        return arguments[0] if arguments else Any
    return annotation


def _is_typed_dict_field_qualifier(origin: Any) -> bool:
    return getattr(origin, "__module__", None) in ("typing", "typing_extensions") and getattr(
        origin,
        "_name",
        None,
    ) in ("NotRequired", "ReadOnly", "Required")


def _runtime_nested_structural_occurrences(
    value: Any,
    *,
    annotation: Any,
    model: type[BaseModel],
    conservative_structural_unions: bool = False,
) -> tuple[tuple[Any, Any], ...]:
    if isinstance(value, model):
        return ((value, model),)
    document_body = _explicit_runtime_body(value, model=model)
    if document_body is not value:
        return ((document_body, model),)
    declared = _runtime_annotation_value(annotation)
    declared_origin = get_origin(declared)
    if conservative_structural_unions and (
        isinstance(declared, TypeVar) or declared_origin in (Union, UnionType)
    ):
        candidates = (
            _runtime_type_parameter_values(declared)
            if isinstance(declared, TypeVar)
            else tuple(candidate for candidate in get_args(declared) if candidate is not NoneType)
        )
        return tuple(
            occurrence
            for candidate in candidates
            for occurrence in _runtime_nested_structural_occurrences(
                value,
                annotation=candidate,
                model=model,
                conservative_structural_unions=True,
            )
        )
    selected = _runtime_annotation_value(_matching_declared_annotation(annotation, value))
    items = _declared_collection_items(value, annotation=selected)
    if items is not None:
        return tuple(
            occurrence
            for item, item_annotation in items
            for occurrence in _runtime_nested_structural_occurrences(
                item,
                annotation=item_annotation,
                model=model,
                conservative_structural_unions=conservative_structural_unions,
            )
        )
    if _runtime_structural_fields(selected) is None:
        return ()
    return ((value, selected),)


def _explicit_runtime_body_model(model: type[BaseModel]) -> type[BaseModel]:
    body_model = getattr(model, "_document_body_model", None)
    return cast(type[BaseModel], body_model) if _is_runtime_base_model_type(body_model) else model


def _explicit_runtime_body(value: Any, *, model: type[BaseModel]) -> Any:
    if isinstance(value, model):
        return value
    if getattr(type(value), "_document_body_model", None) is not model:
        return value
    try:
        body = object.__getattribute__(
            value,
            "_FamilyDocumentAdapterBase__document_body",
        )
    except AttributeError:
        return value
    return body if isinstance(body, model) else value


def _declared_runtime_values_at_path(
    value: Any,
    *,
    annotation: Any,
    path: tuple[str, ...],
) -> tuple[tuple[Any, Any], ...] | None:
    selected = _runtime_annotation_value(_matching_declared_annotation(annotation, value))
    if isinstance(selected, TypeVar) or get_origin(selected) in (Union, UnionType):
        return None
    if not _runtime_value_matches_annotation(value, selected):
        return None
    if not path:
        return ((value, selected),)

    items = _declared_collection_items(value, annotation=selected)
    if items is not None:
        occurrences: list[tuple[Any, Any]] = []
        for item, item_annotation in items:
            nested = _declared_runtime_values_at_path(
                item,
                annotation=item_annotation,
                path=path,
            )
            if nested is None:
                return None
            occurrences.extend(nested)
        return tuple(occurrences)

    structure = _runtime_structural_fields(selected)
    if structure is None:
        # A concrete historical scalar may intentionally replace the rest of
        # a current nested route.
        return ()
    kind, _owner, field_annotations, required_keys = structure
    field_name, *remaining = path
    field_annotation = field_annotations.get(field_name, _MISSING)
    if field_annotation is _MISSING:
        return (
            None
            if _runtime_undeclared_field_is_present(
                value,
                kind=kind,
                field_name=field_name,
            )
            else ()
        )
    present, field_value = _runtime_structural_field_value(
        value,
        kind=kind,
        field_name=field_name,
    )
    if not present:
        return () if kind == "typed_dict" and field_name not in required_keys else None
    if not _runtime_value_matches_annotation(field_value, field_annotation):
        return None
    if not remaining:
        return ((field_value, field_annotation),)
    return _declared_runtime_values_at_path(
        field_value,
        annotation=field_annotation,
        path=tuple(remaining),
    )


def _runtime_structural_fields(
    annotation: Any,
) -> (
    tuple[
        Literal["model", "dataclass", "typed_dict"],
        type[Any],
        dict[str, Any],
        frozenset[str],
    ]
    | None
):
    normalized = _runtime_annotation_value(annotation)
    if _is_runtime_base_model_type(normalized):
        fields = {name: field.annotation for name, field in normalized.model_fields.items()}
        return "model", normalized, fields, frozenset(fields)

    typed_dict_origin = _runtime_typed_dict_origin(normalized)
    if typed_dict_origin is not None:
        fields = _runtime_typed_dict_fields(normalized, origin=typed_dict_origin)
        required = cast(frozenset[str], typed_dict_origin.__required_keys__)
        return "typed_dict", typed_dict_origin, fields, required

    origin = get_origin(normalized)
    dataclass_type = origin if isinstance(origin, type) and is_dataclass(origin) else normalized
    if not isinstance(dataclass_type, type) or not is_dataclass(dataclass_type):
        return None
    pydantic_fields = getattr(dataclass_type, "__pydantic_fields__", None)
    if isinstance(pydantic_fields, Mapping):
        fields = {name: field.annotation for name, field in pydantic_fields.items()}
    else:
        try:
            resolved = get_type_hints(dataclass_type, include_extras=True)
        except (NameError, TypeError):
            resolved = dict(getattr(dataclass_type, "__annotations__", {}))
        declared_names = {field.name for field in dataclass_fields(dataclass_type)}
        fields = {name: resolved[name] for name in declared_names if name in resolved}
    replacements = _runtime_generic_replacements(normalized, origin=dataclass_type)
    fields = {
        name: _replace_runtime_type_parameters(field_annotation, replacements=replacements)
        for name, field_annotation in fields.items()
    }
    return "dataclass", dataclass_type, fields, frozenset(fields)


def _runtime_structural_field_value(
    value: Any,
    *,
    kind: Literal["model", "dataclass", "typed_dict"],
    field_name: str,
) -> tuple[bool, Any]:
    if kind == "typed_dict":
        if not isinstance(value, Mapping) or field_name not in value:
            return False, None
        return True, value[field_name]
    if kind == "model":
        if isinstance(value, BaseModel):
            if field_name not in value.__dict__:
                return False, None
            return True, value.__dict__[field_name]
        if isinstance(value, Mapping) and field_name in value:
            return True, value[field_name]
        return False, None
    if isinstance(value, Mapping):
        if field_name not in value:
            return False, None
        return True, value[field_name]
    try:
        return True, object.__getattribute__(value, field_name)
    except AttributeError:
        return False, None


def _runtime_undeclared_field_is_present(
    value: Any,
    *,
    kind: Literal["model", "dataclass", "typed_dict"],
    field_name: str,
) -> bool:
    if kind == "typed_dict":
        return isinstance(value, Mapping) and field_name in value
    storage = getattr(value, "__dict__", None)
    if kind == "model":
        extras = getattr(value, "__pydantic_extra__", None)
        return isinstance(extras, Mapping) and field_name in extras
    return isinstance(storage, Mapping) and field_name in storage


def _field_crosses_wire_boundary(field_info: Any) -> bool:
    for value in (field_info.exclude, field_info.exclude_if):
        if value is None or value is False:
            continue
        if isinstance(value, Mapping | tuple | list | set | frozenset) and not value:
            continue
        return False
    return True


def _jsonable_declared_scalar(value: Any, *, config: Mapping[str, Any]) -> Any:
    value_type = type(value)
    if (
        value_type is NoneType
        or value_type is bool
        or value_type is int
        or value_type is float
        or value_type is str
    ):
        return value
    try:
        if isinstance(value, bytes):
            return to_jsonable_python(
                value,
                bytes_mode=config.get("ser_json_bytes", "utf8"),
                fallback=_signal_unknown_scalar,
            )
        if isinstance(value, dt.timedelta):
            temporal_mode = config.get("ser_json_temporal")
            if temporal_mode is not None:
                return to_jsonable_python(
                    value,
                    temporal_mode=temporal_mode,
                    fallback=_signal_unknown_scalar,
                )
            return to_jsonable_python(
                value,
                timedelta_mode=config.get("ser_json_timedelta", "iso8601"),
                fallback=_signal_unknown_scalar,
            )
        if isinstance(value, dt.datetime | dt.date | dt.time):
            return to_jsonable_python(
                value,
                temporal_mode=config.get("ser_json_temporal", "iso8601"),
                fallback=_signal_unknown_scalar,
            )
        return to_jsonable_python(value, fallback=_signal_unknown_scalar)
    except _UnknownScalarError:
        return value


def _jsonable_declared_mapping_key(value: Any, *, config: Mapping[str, Any]) -> Any:
    temporal_mode = config.get("ser_json_temporal")
    try:
        if temporal_mode is not None:
            dumped = to_jsonable_python(
                {value: None},
                bytes_mode=config.get("ser_json_bytes", "utf8"),
                temporal_mode=temporal_mode,
                fallback=_signal_unknown_scalar,
            )
        else:
            dumped = to_jsonable_python(
                {value: None},
                bytes_mode=config.get("ser_json_bytes", "utf8"),
                timedelta_mode=config.get("ser_json_timedelta", "iso8601"),
                fallback=_signal_unknown_scalar,
            )
    except _UnknownScalarError:
        return value
    return next(iter(dumped))


class _UnknownScalarError(Exception):
    pass


def _signal_unknown_scalar(_value: Any) -> NoReturn:
    raise _UnknownScalarError


def _extract_preflight_fields(
    value: BaseModel,
    *,
    preserve_nested_models: bool = False,
) -> dict[str, Any]:
    fields = type(value).model_fields
    extracted = {
        name: _extract_preflight_value(
            value.__dict__[name],
            preserve_nested_models=preserve_nested_models,
        )
        for name in fields
        if name in value.__dict__
    }
    extras = value.__pydantic_extra__
    if extras is not None:
        extracted.update(
            (
                name,
                _extract_preflight_value(
                    item,
                    preserve_nested_models=preserve_nested_models,
                ),
            )
            for name, item in extras.items()
        )
    return extracted


def _extract_preflight_value(
    value: Any,
    *,
    preserve_nested_models: bool,
) -> Any:
    if isinstance(value, BaseModel):
        if preserve_nested_models:
            return value
        return _extract_preflight_fields(value)
    if isinstance(value, Mapping):
        return {
            key: _extract_preflight_value(
                item,
                preserve_nested_models=preserve_nested_models,
            )
            for key, item in value.items()
        }
    if isinstance(value, list | tuple | set | frozenset):
        return tuple(
            _extract_preflight_value(
                item,
                preserve_nested_models=preserve_nested_models,
            )
            for item in value
        )
    return value


def _collection_kind(
    annotation: Any,
) -> Literal["list", "tuple", "set", "frozenset"] | None:
    normalized = _runtime_annotation_value(annotation)
    if isinstance(normalized, TypeVar):
        kinds = {
            kind
            for candidate in _runtime_type_parameter_values(normalized)
            if (kind := _collection_kind(candidate)) is not None
        }
        return kinds.pop() if len(kinds) == 1 else None
    origin = get_origin(normalized)
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
        return get_args(annotation)[0]
    return annotation


def _unwrap_optional_annotated(annotation: Any) -> Any:
    current = annotation
    while True:
        normalized = _runtime_annotation_value(current)
        if normalized is not current:
            current = normalized
            continue
        if isinstance(current, TypeVar):
            candidates = _runtime_type_parameter_values(current)
            if len(candidates) != 1:
                return current
            current = candidates[0]
            continue
        origin = get_origin(current)
        if origin not in (Union, UnionType):
            return current
        concrete = tuple(argument for argument in get_args(current) if argument is not NoneType)
        if len(concrete) != 1:
            return current
        current = concrete[0]


def _runtime_structural_serialized_field_name(
    *,
    kind: Literal["model", "dataclass", "typed_dict"],
    owner: type[Any],
    field_name: str,
    field_annotation: Any,
    by_alias: Any,
) -> str:
    if kind == "model":
        return _serialized_field_name(
            cast(type[BaseModel], owner),
            field_name,
            by_alias=by_alias,
        )
    (
        config,
        resolved_field_info,
        assigned_field_info,
        field_metadata,
        annotated_field_infos,
    ) = _runtime_structural_field_sources(
        kind=kind,
        owner=owner,
        field_name=field_name,
        field_annotation=field_annotation,
    )
    use_alias = (
        config.get("serialize_by_alias", False) is True if by_alias is None else by_alias is True
    )
    if use_alias:
        serialization_alias = _runtime_effective_structural_field_attribute(
            attribute="serialization_alias",
            resolved_field_info=resolved_field_info,
            assigned_field_info=assigned_field_info,
            field_metadata=field_metadata,
            annotated_field_infos=annotated_field_infos,
        )
        output_alias = serialization_alias
        if serialization_alias is _MISSING:
            output_alias = _runtime_effective_structural_field_attribute(
                attribute="alias",
                resolved_field_info=resolved_field_info,
                assigned_field_info=assigned_field_info,
                field_metadata=field_metadata,
                annotated_field_infos=annotated_field_infos,
            )
        if isinstance(output_alias, str):
            return output_alias
    return field_name


def _runtime_structural_field_sources(
    *,
    kind: Literal["dataclass", "typed_dict"],
    owner: type[Any],
    field_name: str,
    field_annotation: Any,
) -> tuple[Mapping[str, Any], Any, Any, Mapping[Any, Any], tuple[Any, ...]]:
    fields = getattr(owner, "__pydantic_fields__", None) if kind == "dataclass" else None
    config = getattr(owner, "__pydantic_config__", {})
    if not isinstance(config, Mapping):
        config = {}
    resolved_field_info = fields.get(field_name) if isinstance(fields, Mapping) else None
    assigned_field_info: Any = None
    field_metadata: Mapping[Any, Any] = {}
    if resolved_field_info is None and kind == "dataclass":
        declared_field = next(
            (field for field in dataclass_fields(owner) if field.name == field_name),
            None,
        )
        if declared_field is not None:
            field_metadata = declared_field.metadata
            if hasattr(declared_field.default, "_attributes_set"):
                assigned_field_info = declared_field.default
    annotated_field_infos = (
        () if resolved_field_info is not None else _runtime_annotation_field_infos(field_annotation)
    )
    return (
        config,
        resolved_field_info,
        assigned_field_info,
        field_metadata,
        annotated_field_infos,
    )


def _runtime_effective_structural_field_attribute(
    *,
    attribute: str,
    resolved_field_info: Any,
    assigned_field_info: Any,
    field_metadata: Mapping[Any, Any],
    annotated_field_infos: tuple[Any, ...],
) -> Any:
    if resolved_field_info is not None:
        return getattr(resolved_field_info, attribute, None)
    selected: Any = _MISSING
    for field_info in annotated_field_infos:
        if attribute in getattr(field_info, "_attributes_set", {}):
            selected = getattr(field_info, attribute, None)
    if attribute in field_metadata:
        selected = field_metadata[attribute]
    if assigned_field_info is not None and attribute in getattr(
        assigned_field_info,
        "_attributes_set",
        {},
    ):
        selected = getattr(assigned_field_info, attribute, None)
    return selected


def _runtime_structural_validation_alias_paths(
    *,
    kind: Literal["dataclass", "typed_dict"],
    owner: type[Any],
    field_name: str,
    field_annotation: Any,
) -> tuple[Mapping[str, Any], tuple[tuple[Any, ...], ...]]:
    (
        config,
        resolved_field_info,
        assigned_field_info,
        field_metadata,
        annotated_field_infos,
    ) = _runtime_structural_field_sources(
        kind=kind,
        owner=owner,
        field_name=field_name,
        field_annotation=field_annotation,
    )
    validation_alias = _runtime_effective_structural_field_attribute(
        attribute="validation_alias",
        resolved_field_info=resolved_field_info,
        assigned_field_info=assigned_field_info,
        field_metadata=field_metadata,
        annotated_field_infos=annotated_field_infos,
    )
    alias = _runtime_effective_structural_field_attribute(
        attribute="alias",
        resolved_field_info=resolved_field_info,
        assigned_field_info=assigned_field_info,
        field_metadata=field_metadata,
        annotated_field_infos=annotated_field_infos,
    )
    return config, _alias_paths(validation_alias, fallback=alias)


def _runtime_annotation_field_infos(annotation: Any) -> tuple[Any, ...]:
    origin = get_origin(annotation)
    if _is_typed_dict_field_qualifier(origin):
        arguments = get_args(annotation)
        return _runtime_annotation_field_infos(arguments[0]) if arguments else ()
    if origin is Annotated:
        return tuple(
            metadata
            for metadata in get_args(annotation)[1:]
            if hasattr(metadata, "_attributes_set")
        )
    return ()


def _is_concrete_runtime_scalar_annotation(annotation: Any) -> bool:
    annotation = _runtime_annotation_value(annotation)
    origin = get_origin(annotation)
    if origin is Literal:
        return True
    return (
        origin is None
        and isinstance(annotation, type)
        and annotation not in (Any, object)
        and not _is_runtime_base_model_type(annotation)
        and not is_dataclass(annotation)
        and not _is_typed_dict(annotation)
    )


def _annotation_declares_path(
    annotation: Any,
    path: tuple[str, ...],
    *,
    _seen: frozenset[Any] = frozenset(),
) -> bool:
    annotation_key = _runtime_annotation_cycle_key(annotation)
    if annotation_key in _seen:
        return False
    seen = _seen | {annotation_key}
    annotation = _runtime_annotation_value(annotation)
    if isinstance(annotation, TypeVar):
        return any(
            _annotation_declares_path(candidate, path, _seen=seen)
            for candidate in _runtime_type_parameter_values(annotation)
        )
    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        return any(
            argument is not NoneType and _annotation_declares_path(argument, path, _seen=seen)
            for argument in get_args(annotation)
        )
    if _is_runtime_base_model_type(annotation):
        field_info = annotation.model_fields.get(path[0])
        if field_info is None:
            return False
        if len(path) == 1:
            return True
        return _annotation_declares_path(field_info.annotation, path[1:], _seen=seen)
    structure = _runtime_structural_fields(annotation)
    if structure is not None:
        _kind, _owner, field_annotations, _required_keys = structure
        field_annotation = field_annotations.get(path[0], _MISSING)
        if field_annotation is _MISSING:
            return False
        if len(path) == 1:
            return True
        return _annotation_declares_path(field_annotation, path[1:], _seen=seen)
    if _collection_kind(annotation) is None:
        return False
    return any(
        argument is not Ellipsis and _annotation_declares_path(argument, path, _seen=seen)
        for argument in get_args(annotation)
    )


def _annotation_contains_model(
    annotation: Any,
    model: type[BaseModel],
    *,
    _seen: frozenset[Any] = frozenset(),
) -> bool:
    annotation_key = _runtime_annotation_cycle_key(annotation)
    if annotation_key in _seen:
        return False
    seen = _seen | {annotation_key}
    annotation = _runtime_annotation_value(annotation)
    if annotation is model or (
        _is_runtime_base_model_type(annotation)
        and _explicit_runtime_body_model(annotation) is model
    ):
        return True
    if isinstance(annotation, TypeVar):
        return any(
            _annotation_contains_model(candidate, model, _seen=seen)
            for candidate in _runtime_type_parameter_values(annotation)
        )
    return any(
        argument is not Ellipsis and _annotation_contains_model(argument, model, _seen=seen)
        for argument in get_args(annotation)
    )


def _runtime_annotation_cycle_key(annotation: Any) -> Any:
    try:
        hash(annotation)
    except TypeError:
        return ("identity", id(annotation))
    return ("annotation", annotation)


def _declared_payload_occurrences_at_path(
    payload: Any,
    *,
    annotation: Any,
    path: tuple[str, ...],
    prefer_aliases: bool = False,
    conservative_structural_unions: bool = False,
) -> tuple[tuple[Any, Any], ...]:
    declared = _runtime_annotation_value(annotation)
    declared_origin = get_origin(declared)
    if conservative_structural_unions and (
        isinstance(declared, TypeVar) or declared_origin in (Union, UnionType)
    ):
        candidates = (
            _runtime_type_parameter_values(declared)
            if isinstance(declared, TypeVar)
            else tuple(candidate for candidate in get_args(declared) if candidate is not NoneType)
        )
        return tuple(
            occurrence
            for candidate in candidates
            for occurrence in _declared_payload_occurrences_at_path(
                payload,
                annotation=candidate,
                path=path,
                prefer_aliases=prefer_aliases,
                conservative_structural_unions=True,
            )
        )
    selected = _declared_traversal_annotation(
        annotation,
        payload=payload,
        path=path,
    )
    if selected is None:
        return ()
    if not path:
        return ((payload, selected),)

    items = _declared_collection_items(payload, annotation=selected)
    if items is not None:
        return tuple(
            occurrence
            for item, item_annotation in items
            for occurrence in _declared_payload_occurrences_at_path(
                item,
                annotation=item_annotation,
                path=path,
                prefer_aliases=prefer_aliases,
                conservative_structural_unions=conservative_structural_unions,
            )
        )

    structure = _runtime_structural_fields(selected)
    if structure is None:
        return ()
    kind, owner, field_annotations, _required_keys = structure
    field_name, *remaining = path
    field_annotation = field_annotations.get(field_name, _MISSING)
    if field_annotation is _MISSING:
        return ()

    if kind == "model":
        model = cast(type[BaseModel], owner)
        payload_is_canonical = isinstance(payload, model)
        if isinstance(payload, BaseModel):
            payload = _extract_preflight_fields(
                payload,
                preserve_nested_models=True,
            )
            if payload_is_canonical:
                prefer_aliases = False
        if not isinstance(payload, Mapping):
            return ()
        field_info = model.model_fields[field_name]
        found, field_value = _declared_field_payload_value(
            payload,
            field_name=field_name,
            field_info=field_info,
            model_config=model.model_config,
            prefer_aliases=prefer_aliases,
        )
    elif isinstance(payload, Mapping):
        config, alias_paths = _runtime_structural_validation_alias_paths(
            kind=kind,
            owner=owner,
            field_name=field_name,
            field_annotation=field_annotation,
        )
        access_path = _declared_payload_path_for_aliases(
            payload,
            field_name=field_name,
            alias_paths=alias_paths,
            model_config=config,
            prefer_aliases=prefer_aliases,
        )
        found = access_path is not None
        field_value = (
            _payload_value_at_access_path(payload, access_path) if access_path is not None else None
        )
    else:
        found, field_value = _runtime_structural_field_value(
            payload,
            kind=kind,
            field_name=field_name,
        )
    if not found:
        return ()
    if not remaining:
        return ((field_value, field_annotation),)
    return _declared_payload_occurrences_at_path(
        field_value,
        annotation=field_annotation,
        path=tuple(remaining),
        prefer_aliases=prefer_aliases,
        conservative_structural_unions=conservative_structural_unions,
    )


def _transform_declared_payload_at_path(
    payload: Any,
    *,
    model: type[BaseModel],
    path: tuple[str, ...],
    transform: Callable[[Any], Any],
) -> Any:
    if isinstance(payload, BaseModel):
        payload = _extract_declared_fields(payload)
    if not isinstance(payload, Mapping):
        return payload

    field_name, *remaining = path
    field_info = model.model_fields[field_name]
    if field_name not in payload:
        return payload
    field_value = payload[field_name]
    if not remaining:
        transformed = transform(field_value)
    else:
        transformed = _transform_declared_payload_through_annotation(
            field_value,
            annotation=field_info.annotation,
            path=tuple(remaining),
            transform=transform,
        )
    if transformed is field_value:
        return payload
    updated = dict(payload)
    updated[field_name] = transformed
    return updated


def _transform_declared_payload_through_annotation(
    payload: Any,
    *,
    annotation: Any,
    path: tuple[str, ...],
    transform: Callable[[Any], Any],
) -> Any:
    selected = _declared_traversal_annotation(
        annotation,
        payload=payload,
        path=path,
    )
    if selected is None:
        return payload
    if _is_runtime_base_model_type(selected):
        return _transform_declared_payload_at_path(
            payload,
            model=selected,
            path=path,
            transform=transform,
        )

    items = _declared_collection_items(payload, annotation=selected)
    if items is None:
        return payload
    transformed_items = [
        _transform_declared_payload_through_annotation(
            item,
            annotation=item_annotation,
            path=path,
            transform=transform,
        )
        for item, item_annotation in items
    ]
    if all(
        transformed is original
        for transformed, original in zip(transformed_items, payload, strict=True)
    ):
        return payload
    if isinstance(payload, list):
        return transformed_items
    if isinstance(payload, tuple):
        return tuple(transformed_items)
    try:
        transformed_set = type(payload)(transformed_items)
    except TypeError:
        # Canonical transition payloads use JSON-shaped lists for set-like
        # fields, so an intermediate model-to-mapping conversion may cease to
        # be hashable without losing any declared member.
        return transformed_items
    if len(transformed_set) != len(payload):
        msg = "Nested migration cannot preserve set cardinality along its declared path"
        raise InvalidMigrationError(msg)
    return transformed_set


def _declared_traversal_annotation(
    annotation: Any,
    *,
    payload: Any,
    path: tuple[str, ...],
) -> Any | None:
    normalized = _runtime_annotation_value(annotation)
    origin = get_origin(normalized)
    if isinstance(normalized, TypeVar):
        candidates = _runtime_type_parameter_values(normalized)
    elif origin in (Union, UnionType):
        candidates = tuple(
            candidate for candidate in get_args(normalized) if candidate is not NoneType
        )
    else:
        return normalized
    if not candidates:
        return normalized

    selected = _runtime_annotation_value(
        _matching_declared_annotation(normalized, payload),
    )
    if not isinstance(selected, TypeVar) and get_origin(selected) not in (
        Union,
        UnionType,
    ):
        if _runtime_value_matches_annotation(payload, selected) or _annotation_declares_path(
            selected,
            path,
        ):
            return selected

    path_candidates = tuple(
        _runtime_annotation_value(candidate)
        for candidate in candidates
        if _annotation_declares_path(candidate, path)
    )
    if not path_candidates:
        return selected
    runtime_matches = tuple(
        candidate
        for candidate in path_candidates
        if _runtime_value_matches_annotation(payload, candidate)
    )
    if runtime_matches:
        return runtime_matches[0]
    if isinstance(payload, Mapping):
        model_candidate = next(
            (candidate for candidate in path_candidates if _is_runtime_base_model_type(candidate)),
            None,
        )
        if model_candidate is not None:
            return model_candidate
    if isinstance(payload, list | tuple | set | frozenset):
        payload_kind = type(payload).__name__
        collection_candidate = next(
            (
                candidate
                for candidate in path_candidates
                if _collection_kind(candidate) == payload_kind
            ),
            None,
        )
        if collection_candidate is not None:
            return collection_candidate
        return path_candidates[0]
    return selected


def _declared_collection_items(
    payload: Any,
    *,
    annotation: Any,
) -> tuple[tuple[Any, Any], ...] | None:
    if not isinstance(payload, list | tuple | set | frozenset):
        return None
    annotation = _runtime_annotation_value(annotation)
    if _collection_kind(annotation) is None:
        return None
    values = tuple(payload)
    arguments = get_args(annotation)
    if get_origin(annotation) is tuple and arguments and arguments[-1] is not Ellipsis:
        return tuple(zip(values, arguments, strict=False))
    item_annotation = arguments[0] if arguments else Any
    return tuple((item, item_annotation) for item in values)


def _declared_field_payload_value(
    payload: Mapping[Any, Any],
    *,
    field_name: str,
    field_info: Any,
    model_config: Mapping[str, Any],
    prefer_aliases: bool,
) -> tuple[bool, Any]:
    access_path = _declared_field_payload_path(
        payload,
        field_name=field_name,
        field_info=field_info,
        model_config=model_config,
        prefer_aliases=prefer_aliases,
    )
    if access_path is None:
        return False, None
    return True, _payload_value_at_access_path(payload, access_path)


def _declared_field_payload_path(
    payload: Mapping[Any, Any],
    *,
    field_name: str,
    field_info: Any,
    model_config: Mapping[str, Any],
    prefer_aliases: bool,
) -> tuple[Any, ...] | None:
    return _declared_payload_path_for_aliases(
        payload,
        field_name=field_name,
        alias_paths=_alias_paths(
            field_info.validation_alias,
            fallback=field_info.alias,
        ),
        model_config=model_config,
        prefer_aliases=prefer_aliases,
    )


def _declared_payload_path_for_aliases(
    payload: Mapping[Any, Any],
    *,
    field_name: str,
    alias_paths: tuple[tuple[Any, ...], ...],
    model_config: Mapping[str, Any],
    prefer_aliases: bool,
) -> tuple[Any, ...] | None:
    if prefer_aliases:
        if not alias_paths:
            # An unaliased field is always accepted by its field name, even
            # when validate_by_name is otherwise disabled for aliased fields.
            candidates = ((field_name,),)
        else:
            aliases_enabled = model_config.get("validate_by_alias", True) is not False
            configured_names = model_config.get("validate_by_name", _MISSING)
            names_enabled = (
                configured_names is True
                if configured_names is not _MISSING
                else model_config.get("populate_by_name", False) is True or not aliases_enabled
            )
            name_candidates = ((field_name,),) if names_enabled else ()
            candidates = (
                *(alias_paths if aliases_enabled else ()),
                *name_candidates,
            )
    else:
        candidates = ((field_name,),)
    visited: list[tuple[Any, ...]] = []
    for candidate in candidates:
        if not candidate or candidate in visited:
            continue
        visited.append(candidate)
        if _payload_access_path_exists(payload, candidate):
            return candidate
    return None


def _payload_access_path_exists(payload: Any, path: tuple[Any, ...]) -> bool:
    current = payload
    for part in path:
        if isinstance(current, Mapping):
            if part not in current:
                return False
            current = current[part]
            continue
        if isinstance(current, list | tuple) and isinstance(part, int):
            try:
                current = current[part]
            except IndexError:
                return False
            continue
        return False
    return True


def _payload_value_at_access_path(payload: Any, path: tuple[Any, ...]) -> Any:
    current = payload
    for part in path:
        current = current[part]
    return current
