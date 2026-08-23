from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Mapping
from dataclasses import dataclass, is_dataclass
from dataclasses import fields as dataclass_fields
from functools import partial
from types import NoneType, UnionType
from typing import (
    TYPE_CHECKING,
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
from typing import (
    is_typeddict as stdlib_is_typeddict,
)

from pydantic import BaseModel, ConfigDict, create_model
from pydantic_core import SchemaValidator, core_schema, to_jsonable_python
from typing_extensions import (  # noqa: UP035
    TypeAliasType as ExtensionsTypeAliasType,
)
from typing_extensions import is_typeddict as extensions_is_typeddict

from pydantic_versions._compiler import (
    _CompiledDecoratorNestedFamily,
    _CompiledFamily,
    _CompiledVersion,
)
from pydantic_versions._runtime_versioning import (
    _MISSING,
    _alias_paths,
    _apply_serialized_version_metadata,
    _copy_alias_payload_value,
    _current_validation_input,
    _detect_version,
    _get_version_field,
    _matches_version_label,
    _model_metadata_field_name,
    _normalize_payload_field_aliases,
    _path_has_payload,
    _remove_version_field,
    _safe_nested_version_display,
    _serialized_field_name,
    _set_version_field,
    _to_current_names,
    _to_version_names,
    _validate_include_version_mode,
    _verify_serialized_model_metadata,
    _version_field_display,
)
from pydantic_versions._runtime_versioning import (
    _runtime_label as _runtime_label,
)
from pydantic_versions.declarations import VersionedValidation, VersionPath
from pydantic_versions.exceptions import (
    InvalidMigrationError,
    IrreversibleTransitionError,
    SchemaVersionError,
    UnsupportedWireModelError,
)

if TYPE_CHECKING:
    from pydantic_versions.family import SchemaFamily


@dataclass
class _DecoratorRouteSelection:
    route: _CompiledDecoratorNestedFamily
    location: tuple[str | int, ...]
    relative_location: tuple[str | int, ...]
    site_routes: tuple[_CompiledDecoratorNestedFamily, ...]
    label: str
    parent: _DecoratorRouteSelection | None = None
    value_identity: int | None = None


_CONTRACT_FIELD_OMISSION_OPTIONS = frozenset(
    {
        "exclude",
        "exclude_computed_fields",
        "exclude_defaults",
        "exclude_none",
        "exclude_unset",
        "include",
    }
)
_SUPPORTED_MODEL_DUMP_OPTIONS = frozenset(
    {
        "by_alias",
        "context",
        "fallback",
        "mode",
        "polymorphic_serialization",
        "round_trip",
        "serialize_as_any",
        "warnings",
    }
)
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
    normalized = _runtime_annotation_value(annotation)
    if normalized is Any:
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


def _validate_explicit_nested_runtime_shapes(
    value: Any,
    *,
    compiled: _CompiledFamily,
    version: _CompiledVersion,
    label: str,
    recurse_nested_targets: bool = False,
) -> None:
    root_value = value
    root_annotation: Any = version.model
    if version.wire_model_kind == "explicit":
        explicit_model = _explicit_runtime_body_model(version.model)
        explicit_value = _explicit_runtime_body(value, model=explicit_model)
        root_value = explicit_value
        root_annotation = explicit_model
        for nested in compiled.nested:
            target_path = _target_nested_path(version, nested.path)
            if (
                _declared_runtime_values_at_path(
                    explicit_value,
                    annotation=explicit_model,
                    path=target_path,
                )
                is not None
            ):
                continue
            _raise_explicit_nested_runtime_shape(compiled, label=label, path=nested.path)
    if recurse_nested_targets:
        _validate_nested_target_runtime_shapes(
            root_value,
            annotation=root_annotation,
            compiled=compiled,
            version=version,
            label=label,
        )


def _validate_nested_target_runtime_shapes(
    value: Any,
    *,
    annotation: Any,
    compiled: _CompiledFamily,
    version: _CompiledVersion,
    label: str,
) -> None:
    for nested in compiled.nested:
        child_label = nested.child_label(label)
        child_compiled = nested.family._compiled_family()
        child_version = child_compiled.version(child_label)
        child_model = _explicit_runtime_body_model(child_version.model)
        target_path = _target_nested_path(version, nested.path)
        route_values = _declared_runtime_values_at_path(
            value,
            annotation=annotation,
            path=target_path,
        )
        if route_values is None:
            _raise_explicit_nested_runtime_shape(compiled, label=label, path=nested.path)
        for route_value, route_annotation in route_values:
            for child_value, child_annotation in _runtime_nested_structural_occurrences(
                route_value,
                annotation=route_annotation,
                model=child_model,
            ):
                _validate_nested_target_runtime_shapes(
                    child_value,
                    annotation=child_annotation,
                    compiled=child_compiled,
                    version=child_version,
                    label=child_label,
                )


def _raise_explicit_nested_runtime_shape(
    compiled: _CompiledFamily,
    *,
    label: str,
    path: tuple[str, ...],
) -> NoReturn:
    msg = (
        f"Explicit wire model for schema family {compiled.name!r} and version "
        f"{label!r} returned a value outside its declared annotation at "
        f"nested path {path!r}"
    )
    raise ValueError(msg)


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


def _select_decorator_routes(
    value: BaseModel,
    *,
    compiled: _CompiledFamily,
    parent_label: str,
    source_version: _CompiledVersion | None,
    location_prefix: tuple[str | int, ...] = (),
    parent_selection: _DecoratorRouteSelection | None = None,
) -> tuple[_DecoratorRouteSelection, ...]:
    if not compiled.decorator_nested:
        return ()
    root_names = {
        route.path[0]: (
            route.path[0]
            if source_version is None
            else source_version.projection.field(route.path[0]).version_name
        )
        for route in compiled.decorator_nested
    }
    selected: list[_DecoratorRouteSelection] = []
    for route in compiled.decorator_nested:
        if root_names[route.path[0]] is None:
            continue
        for nested_value, location in _walk_authoritative_decorator_route(
            value,
            annotation=type(value),
            route=route,
            root_names=root_names,
        ):
            expected = (route.family.model, route.family.model_for(parent_label))
            if isinstance(nested_value, expected):
                site = _decorator_dispatch_site(route)
                selection = _DecoratorRouteSelection(
                    route=route,
                    location=(*location_prefix, *location),
                    relative_location=location,
                    site_routes=tuple(
                        candidate
                        for candidate in compiled.decorator_nested
                        if _decorator_dispatch_site(candidate) == site
                    ),
                    label=route.child_label(parent_label),
                    parent=parent_selection,
                )
                selected.append(selection)
                child_compiled = route.family._compiled_family()
                child_source = (
                    None
                    if isinstance(nested_value, route.family.model)
                    else child_compiled.version(selection.label)
                )
                selected.extend(
                    _select_decorator_routes(
                        nested_value,
                        compiled=child_compiled,
                        parent_label=selection.label,
                        source_version=child_source,
                        location_prefix=selection.location,
                        parent_selection=selection,
                    )
                )
    return tuple(selected)


def _walk_authoritative_decorator_route(
    value: Any,
    *,
    annotation: Any,
    route: _CompiledDecoratorNestedFamily,
    root_names: Mapping[str, str | None],
) -> tuple[tuple[Any, tuple[str | int, ...]], ...]:
    states: list[tuple[Any, Any, tuple[str | int, ...]]] = [(value, annotation, ())]
    for step_index, step in enumerate(route.traversal):
        next_states: list[tuple[Any, Any, tuple[str | int, ...]]] = []
        for current, current_annotation, location in states:
            normalized_annotation = _strip_annotated(current_annotation)
            if step.kind == "field":
                if not isinstance(current, BaseModel):
                    continue
                actual_name = root_names.get(step.value) if step_index == 0 else step.value
                if actual_name is None:
                    continue
                model = type(current)
                field_info = model.model_fields.get(actual_name)
                if field_info is None or actual_name not in current.__dict__:
                    continue
                next_states.append(
                    (
                        current.__dict__[actual_name],
                        field_info.annotation,
                        (*location, step.value),
                    )
                )
                continue
            if step.kind == "union_arm":
                if get_origin(normalized_annotation) not in (Union, UnionType):
                    continue
                arguments = get_args(normalized_annotation)
                ordinal = int(step.value)
                if ordinal >= len(arguments):
                    continue
                selected = _matching_declared_annotation(normalized_annotation, current)
                candidate = _strip_annotated(arguments[ordinal])
                if _strip_annotated(selected) != candidate:
                    continue
                next_states.append((current, arguments[ordinal], location))
                continue
            if step.kind == "each":
                if not isinstance(current, list | tuple | set | frozenset):
                    continue
                arguments = get_args(normalized_annotation)
                if not arguments:
                    continue
                item_annotation = arguments[0]
                next_states.extend(
                    (item, item_annotation, (*location, ordinal))
                    for ordinal, item in enumerate(current)
                )
                continue
            if step.kind == "tuple_index":
                if not isinstance(current, tuple | list):
                    continue
                arguments = get_args(normalized_annotation)
                ordinal = int(step.value)
                if ordinal >= len(current) or ordinal >= len(arguments):
                    continue
                next_states.append((current[ordinal], arguments[ordinal], (*location, ordinal)))
                continue
            if step.kind == "mapping_values":
                if not isinstance(current, Mapping):
                    continue
                arguments = get_args(normalized_annotation)
                if len(arguments) != 2:
                    continue
                next_states.extend(
                    (item, arguments[1], (*location, key))
                    for key, item in current.items()
                    if isinstance(key, str | int)
                )
        states = next_states
        if not states:
            break
    return tuple((item, location) for item, _, location in states)


def _raw_decorator_route_values(
    payload: Any,
    *,
    model: type[BaseModel],
    route: _CompiledDecoratorNestedFamily,
    root_names: Mapping[str, str | None],
) -> tuple[tuple[Any, tuple[str | int, ...]], ...]:
    states: list[tuple[Any, Any, tuple[str | int, ...]]] = [(payload, model, ())]
    for step_index, step in enumerate(route.traversal):
        next_states: list[tuple[Any, Any, tuple[str | int, ...]]] = []
        for current, current_annotation, location in states:
            normalized_annotation = _strip_annotated(current_annotation)
            if step.kind == "field":
                if isinstance(current, BaseModel):
                    current = _extract_preflight_fields(current)
                if not isinstance(current, Mapping):
                    continue
                actual_name = root_names.get(step.value) if step_index == 0 else step.value
                if actual_name is None:
                    continue
                field_info = normalized_annotation.model_fields[actual_name]
                found, field_value = _declared_field_payload_value(
                    current,
                    field_name=actual_name,
                    field_info=field_info,
                    model_config=normalized_annotation.model_config,
                    prefer_aliases=True,
                )
                if found:
                    next_states.append(
                        (field_value, field_info.annotation, (*location, step.value))
                    )
                continue
            if step.kind == "union_arm":
                arguments = get_args(normalized_annotation)
                ordinal = int(step.value)
                next_states.append((current, arguments[ordinal], location))
                continue
            if step.kind == "each":
                if not isinstance(current, list | tuple | set | frozenset):
                    continue
                arguments = get_args(normalized_annotation)
                if arguments:
                    next_states.extend(
                        (item, arguments[0], (*location, ordinal))
                        for ordinal, item in enumerate(current)
                    )
                continue
            if step.kind == "tuple_index":
                if not isinstance(current, list | tuple):
                    continue
                arguments = get_args(normalized_annotation)
                ordinal = int(step.value)
                if ordinal < len(current) and ordinal < len(arguments):
                    next_states.append((current[ordinal], arguments[ordinal], (*location, ordinal)))
                continue
            if step.kind == "mapping_values":
                if not isinstance(current, Mapping):
                    continue
                arguments = get_args(normalized_annotation)
                if len(arguments) == 2:
                    next_states.extend(
                        (item, arguments[1], (*location, key))
                        for key, item in current.items()
                        if isinstance(key, str)
                    )
        states = next_states
        if not states:
            break
    return tuple((item, location) for item, _, location in states)


def _field_crosses_wire_boundary(field_info: Any) -> bool:
    for value in (field_info.exclude, field_info.exclude_if):
        if value is None or value is False:
            continue
        if isinstance(value, Mapping | tuple | list | set | frozenset) and not value:
            continue
        return False
    return True


def _jsonable_declared_scalar(value: Any, *, config: Mapping[str, Any]) -> Any:
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


def _validate_family[T: BaseModel](
    family: SchemaFamily[T],
    data: Any,
    *,
    version: str | None,
) -> VersionedValidation[T]:
    compiled = family._compiled_family()
    source_version = _detect_version(compiled, data, explicit_version=version)
    _preflight_validation_route(family, compiled, source_version=source_version)
    _preflight_nested_version_metadata(
        payload=data,
        compiled=compiled,
        parent_label=source_version,
    )
    source = compiled.version(source_version)
    source_model = source.model.model_validate(data)
    _validate_explicit_nested_runtime_shapes(
        source_model,
        compiled=compiled,
        version=source,
        label=source_version,
        recurse_nested_targets=True,
    )
    _verify_validated_family_version_metadata(
        value=source_model,
        compiled=compiled,
        label=source_version,
    )
    _preflight_nested_version_metadata(
        payload=source_model,
        compiled=compiled,
        parent_label=source_version,
    )
    decorator_selections = _select_decorator_routes(
        source_model,
        compiled=compiled,
        parent_label=source_version,
        source_version=source,
    )
    _preflight_selected_decorator_version_metadata(
        payload=data,
        compiled=compiled,
        parent_label=source_version,
        selections=decorator_selections,
    )
    payload = _to_current_names(
        compiled,
        source,
        _extract_declared_fields(source_model),
    )
    payload = _normalize_selected_decorator_payloads(
        payload=payload,
        selections=decorator_selections,
        parent_label=source_version,
    )

    migrations_applied: list[tuple[str, str]] = []
    source_index = compiled.index(source_version)
    nested_payload_is_canonical = False
    for transition in compiled.transitions[source_index:]:
        payload = _apply_nested_family_migrations(
            payload=payload,
            compiled=compiled,
            source_label=transition.source,
            target_label=transition.target,
            source_payload_is_canonical=nested_payload_is_canonical,
        )
        payload = _apply_selected_decorator_migrations(
            payload=payload,
            selections=decorator_selections,
            target_label=transition.target,
        )
        decorator_selections = _reconcile_decorator_selections(
            payload=payload,
            selections=decorator_selections,
            compiled=compiled,
            discover_new=False,
        )
        nested_payload_is_canonical = True
        if transition.upgrade is None:
            continue
        migrated = transition.upgrade(dict(payload))
        if not isinstance(migrated, dict):
            msg = f"Migration {transition.source!r} -> {transition.target!r} must return a dict"
            raise InvalidMigrationError(msg)
        payload = migrated
        decorator_selections = _reconcile_decorator_selections(
            payload=payload,
            selections=decorator_selections,
            compiled=compiled,
            discover_new=True,
        )
        migrations_applied.append((transition.source, transition.target))

    payload = _apply_selected_decorator_migrations(
        payload=payload,
        selections=decorator_selections,
        target_label=compiled.current_version,
    )
    decorator_selections = _reconcile_decorator_selections(
        payload=payload,
        selections=decorator_selections,
        compiled=compiled,
        discover_new=False,
    )
    payload = _project_nested_family_payloads(
        payload=payload,
        compiled=compiled,
        parent_label=compiled.current_version,
        wire_boundary=False,
    )
    payload = _project_selected_decorator_payloads(
        payload=payload,
        selections=decorator_selections,
        parent_label=compiled.current_version,
        wire_boundary=False,
    )
    payload = _materialize_selected_decorator_models(
        payload=payload,
        selections=decorator_selections,
    )
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
    _validate_model_dump_options(dump_kwargs)
    if data is None:
        return _defaults_family(
            family,
            version=version,
            include_version=include_version,
            dump_kwargs=dump_kwargs,
        )

    compiled = family._compiled_family()
    _validate_include_version_mode(compiled, include_version)
    requested = _runtime_label(version, family_name=family.name)
    target = compiled.version(requested)
    target_index = compiled.index(requested)
    current_index = len(compiled.versions) - 1
    if requested != compiled.current_version:
        family.plan_render(requested)

    _preflight_nested_version_metadata(
        payload=data,
        compiled=compiled,
        parent_label=compiled.current_version,
    )

    payload, decorator_selections = _validated_current_render_payload(
        family=family,
        compiled=compiled,
        data=data,
    )

    # Authoritative current-model validation already returns canonical field
    # names for the complete declared subtree. Nested conversion must not
    # validate those child values a second time merely to normalize them.
    nested_payload_is_canonical = True
    if requested != compiled.current_version:
        for edge_index in range(current_index - 1, target_index - 1, -1):
            transition = compiled.transitions[edge_index]
            payload = _apply_nested_family_migrations(
                payload=payload,
                compiled=compiled,
                source_label=transition.target,
                target_label=transition.source,
                source_payload_is_canonical=nested_payload_is_canonical,
            )
            payload = _apply_selected_decorator_migrations(
                payload=payload,
                selections=decorator_selections,
                target_label=transition.source,
            )
            decorator_selections = _reconcile_decorator_selections(
                payload=payload,
                selections=decorator_selections,
                compiled=compiled,
                discover_new=False,
            )
            nested_payload_is_canonical = True
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
            decorator_selections = _reconcile_decorator_selections(
                payload=payload,
                selections=decorator_selections,
                compiled=compiled,
                discover_new=True,
            )

    payload = _apply_selected_decorator_migrations(
        payload=payload,
        selections=decorator_selections,
        target_label=requested,
    )
    decorator_selections = _reconcile_decorator_selections(
        payload=payload,
        selections=decorator_selections,
        compiled=compiled,
        discover_new=False,
    )
    payload = _project_nested_family_payloads(
        payload=payload,
        compiled=compiled,
        parent_label=requested,
        wire_boundary=True,
    )
    payload = _project_selected_decorator_payloads(
        payload=payload,
        selections=decorator_selections,
        parent_label=requested,
        wire_boundary=True,
    )
    if compiled.version_metadata is not None and (
        compiled.version_metadata.owner == "model"
        or (requested != compiled.current_version and target.wire_model_kind != "explicit")
    ):
        payload = dict(payload)
        if compiled.version_metadata.owner == "family":
            _set_version_field(payload, compiled.version_metadata.path, requested)
        else:
            payload[_model_metadata_field_name(compiled)] = requested

    target_payload = _to_version_names(target, payload)
    target_model = target.model.model_validate(target_payload, by_name=True)
    _validate_explicit_nested_runtime_shapes(
        target_model,
        compiled=compiled,
        version=target,
        label=requested,
        recurse_nested_targets=True,
    )
    _validate_nested_collection_cardinality(
        input_payload=target_payload,
        validated_model=target_model,
        compiled=compiled,
        parent_label=requested,
        selections=decorator_selections,
    )
    return _serialize_target_model(
        compiled=compiled,
        requested=requested,
        target_model=target_model,
        include_version=include_version,
        dump_kwargs=dump_kwargs,
    )


def _defaults_family[T: BaseModel](
    family: SchemaFamily[T],
    *,
    version: str,
    include_version: bool,
    dump_kwargs: Mapping[str, Any],
) -> dict[str, Any]:
    _validate_model_dump_options(dump_kwargs)
    compiled = family._compiled_family()
    _validate_include_version_mode(compiled, include_version)
    requested = _runtime_label(version, family_name=family.name)
    target = compiled.version(requested)
    target_model = target.model.model_validate({})
    _validate_explicit_nested_runtime_shapes(
        target_model,
        compiled=compiled,
        version=target,
        label=requested,
        recurse_nested_targets=True,
    )
    return _serialize_target_model(
        compiled=compiled,
        requested=requested,
        target_model=target_model,
        include_version=include_version,
        dump_kwargs=dump_kwargs,
    )


def _serialize_target_model(
    *,
    compiled: _CompiledFamily,
    requested: str,
    target_model: BaseModel,
    include_version: bool,
    dump_kwargs: Mapping[str, Any],
) -> dict[str, Any]:
    serialization_options = dict(dump_kwargs)
    serialization_options.pop("polymorphic_serialization", None)
    serialization_options.setdefault("by_alias", True)
    serialization_options.setdefault("mode", "json")
    serialized = target_model.model_dump(**serialization_options)
    if not isinstance(serialized, Mapping):
        msg = (
            f"Target wire model for family {compiled.name!r} and version "
            f"{requested!r} must serialize to an object"
        )
        raise ValueError(msg)
    dumped = dict(serialized)

    target = compiled.version(requested)
    for nested in compiled.nested:
        _prune_nested_family_metadata_at_path(
            payload=dumped,
            source_payload=target_model,
            model=target.model,
            path=_target_nested_path(target, nested.path),
            family=nested.family._compiled_family(),
            target_label=nested.child_label(requested),
            by_alias=serialization_options["by_alias"],
        )

    selections = _select_decorator_routes(
        target_model,
        compiled=compiled,
        parent_label=requested,
        source_version=target,
    )
    _prune_serialized_decorator_metadata(
        dumped=dumped,
        source_model=target_model,
        compiled=compiled,
        parent_label=requested,
        selections=selections,
        by_alias=serialization_options["by_alias"],
    )
    _validate_target_extra_output_collisions(
        target_model,
        by_alias=serialization_options["by_alias"],
    )
    _apply_serialized_version_metadata(
        dumped=dumped,
        compiled=compiled,
        requested=requested,
        target_model=type(target_model),
        include_version=include_version,
        by_alias=serialization_options["by_alias"],
    )
    return dumped


def _validate_target_extra_output_collisions(
    target_model: BaseModel,
    *,
    by_alias: Any,
) -> None:
    seen: set[int] = set()

    def visit(value: Any) -> None:
        if id(value) in seen:
            return
        seen.add(id(value))
        if isinstance(value, BaseModel):
            model = type(value)
            extras = value.__pydantic_extra__
            if isinstance(extras, Mapping) and extras:
                output_names = {
                    _serialized_field_name(model, field_name, by_alias=by_alias)
                    for field_name in model.model_fields
                }
                use_alias = (
                    model.model_config.get("serialize_by_alias", False) is True
                    if by_alias is None
                    else by_alias is True
                )
                output_names.update(
                    field_info.alias
                    if use_alias and isinstance(field_info.alias, str)
                    else field_name
                    for field_name, field_info in model.model_computed_fields.items()
                )
                collisions = sorted(name for name in extras if name in output_names)
                if collisions:
                    formatted = ", ".join(repr(name) for name in collisions)
                    msg = (
                        f"Target wire model {model.__name__!r} extras overwrite "
                        f"declared serialization location(s): {formatted}"
                    )
                    raise ValueError(msg)
                for item in extras.values():
                    visit(item)
            for field_name in model.model_fields:
                if field_name in value.__dict__:
                    visit(value.__dict__[field_name])
            return
        if isinstance(value, Mapping):
            for item in value.values():
                visit(item)
            return
        if isinstance(value, list | tuple | set | frozenset):
            for item in value:
                visit(item)

    visit(target_model)


def _validate_model_dump_options(dump_kwargs: Mapping[str, Any]) -> None:
    omission_options = sorted(_CONTRACT_FIELD_OMISSION_OPTIONS.intersection(dump_kwargs))
    if omission_options:
        formatted = ", ".join(repr(option) for option in omission_options)
        msg = (
            "Versioned rendering cannot omit target contract fields; unsupported "
            f"model_dump option(s): {formatted}"
        )
        raise ValueError(msg)

    unsupported = sorted(set(dump_kwargs).difference(_SUPPORTED_MODEL_DUMP_OPTIONS))
    if unsupported:
        formatted = ", ".join(repr(option) for option in unsupported)
        msg = f"Versioned rendering received unsupported model_dump option(s): {formatted}"
        raise ValueError(msg)

    polymorphic = tuple(
        option
        for option in ("serialize_as_any", "polymorphic_serialization")
        if dump_kwargs.get(option)
    )
    if polymorphic:
        formatted = ", ".join(repr(option) for option in polymorphic)
        msg = (
            "Versioned rendering cannot use polymorphic serialization because it may "
            f"expose fields outside the target wire contract: {formatted}"
        )
        raise ValueError(msg)
    if dump_kwargs.get("round_trip"):
        msg = (
            "Versioned rendering cannot use round_trip=True because Pydantic may "
            "omit computed target contract fields"
        )
        raise ValueError(msg)


def _prune_serialized_decorator_metadata(
    *,
    dumped: dict[str, Any],
    source_model: BaseModel,
    compiled: _CompiledFamily,
    parent_label: str,
    selections: tuple[_DecoratorRouteSelection, ...],
    by_alias: Any,
) -> None:
    source_payload = _extract_declared_fields(source_model)
    for selection in _decorator_selections_child_first(selections):
        location = _serialized_decorator_selection_location(
            compiled=compiled,
            parent_label=parent_label,
            selection=selection,
            by_alias=by_alias,
        )
        if location is None:
            continue
        found, payload = _payload_at_location(dumped, location)
        if not found:
            continue
        source_found, source_value = _payload_at_location(
            source_payload,
            selection.location,
        )
        if not source_found:
            source_value = selection
        _prune_nested_family_metadata_payload(
            payload,
            selection.route.family._compiled_family(),
            selection.label,
            by_alias=by_alias,
            source_value=source_value,
        )


def _serialized_decorator_selection_location(
    *,
    compiled: _CompiledFamily,
    parent_label: str,
    selection: _DecoratorRouteSelection,
    by_alias: Any,
) -> tuple[str | int, ...] | None:
    if selection.parent is None:
        prefix: tuple[str | int, ...] = ()
        owner_compiled = compiled
        owner_label = parent_label
    else:
        parent_location = _serialized_decorator_selection_location(
            compiled=compiled,
            parent_label=parent_label,
            selection=selection.parent,
            by_alias=by_alias,
        )
        if parent_location is None:
            return None
        prefix = parent_location
        owner_compiled = selection.parent.route.family._compiled_family()
        owner_label = selection.parent.label

    owner_target = owner_compiled.version(owner_label)
    annotation: Any = owner_target.model
    relative = iter(selection.relative_location)
    serialized: list[str | int] = list(prefix)
    for step_index, step in enumerate(selection.route.traversal):
        normalized = _strip_annotated(annotation)
        if step.kind == "field":
            next(relative)
            field_name = step.value
            if step_index == 0:
                projected_name = owner_target.projection.field(field_name).version_name
                if projected_name is None:
                    return None
                field_name = projected_name
            field_info = normalized.model_fields[field_name]
            serialized.append(_serialized_field_name(normalized, field_name, by_alias=by_alias))
            annotation = field_info.annotation
            continue
        if step.kind == "union_arm":
            arguments = get_args(normalized)
            ordinal = int(step.value)
            annotation = arguments[ordinal]
            continue
        if step.kind == "each":
            occurrence = next(relative)
            arguments = get_args(normalized)
            serialized.append(occurrence)
            annotation = arguments[0]
            continue
        if step.kind == "tuple_index":
            occurrence = next(relative)
            ordinal = int(step.value)
            arguments = get_args(normalized)
            serialized.append(occurrence)
            annotation = arguments[ordinal]
            continue
        if step.kind == "mapping_values":
            occurrence = next(relative)
            arguments = get_args(normalized)
            serialized.append(occurrence)
            annotation = arguments[1]
            continue
    return tuple(serialized)


def _validated_current_render_payload[T: BaseModel](
    *,
    family: SchemaFamily[T],
    compiled: _CompiledFamily,
    data: T | Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[_DecoratorRouteSelection, ...]]:
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
                compiled,
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
    selections = _select_decorator_routes(
        current_model,
        compiled=compiled,
        parent_label=compiled.current_version,
        source_version=None,
    )
    _preflight_selected_decorator_version_metadata(
        payload=data,
        compiled=compiled,
        parent_label=compiled.current_version,
        selections=selections,
    )
    payload = _to_current_names(
        compiled,
        current_version,
        _extract_declared_fields(
            current_model,
            declared_model=family.model,
        ),
    )
    _refresh_decorator_selection_identities(payload, selections)
    return payload, selections


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


def _normalize_selected_decorator_payloads(
    *,
    payload: dict[str, Any],
    selections: tuple[_DecoratorRouteSelection, ...],
    parent_label: str,
) -> dict[str, Any]:
    normalized = payload
    # Historical ancestor field names must become canonical before a
    # descendant's canonical location is reachable.  Conversion and final
    # projection remain child-first, but input-name normalization is the
    # inverse traversal.
    for selection in _decorator_selections_parent_first(selections):
        child_compiled = selection.route.family._compiled_family()
        child_label = selection.route.child_label(parent_label)
        normalized = _transform_payload_at_location(
            normalized,
            location=selection.location,
            transform=partial(
                _normalize_decorator_value,
                compiled=child_compiled,
                child_label=child_label,
            ),
        )
        _refresh_decorator_selection_identity(normalized, selection)
    return normalized


def _normalize_decorator_value(
    value: Any,
    *,
    compiled: _CompiledFamily,
    child_label: str,
) -> Any:
    if isinstance(value, BaseModel):
        value = _extract_declared_fields(value)
    if not isinstance(value, Mapping):
        return value
    normalized = _to_current_names(
        compiled,
        compiled.version(child_label),
        dict(value),
    )
    return _normalize_validated_explicit_nested_payloads(
        payload=normalized,
        compiled=compiled,
        parent_label=child_label,
    )


def _normalize_validated_explicit_nested_payloads(
    *,
    payload: dict[str, Any],
    compiled: _CompiledFamily,
    parent_label: str,
) -> dict[str, Any]:
    """Canonicalize explicit descendants already validated by a wire model."""
    normalized = payload
    for nested in compiled.nested:
        normalized = _transform_declared_payload_at_path(
            normalized,
            model=compiled.model,
            path=nested.path,
            transform=partial(
                _normalize_validated_nested_family_value,
                family=nested.family,
                child_label=nested.child_label(parent_label),
            ),
        )
    return normalized


def _normalize_validated_nested_family_value(
    value: Any,
    *,
    family: SchemaFamily[Any],
    child_label: str,
) -> Any:
    if value is None:
        return value
    if isinstance(value, BaseModel):
        value = _extract_declared_fields(value)
    if isinstance(value, list | tuple | set | frozenset):
        # Transition payloads intentionally use JSON-shaped lists for every
        # supported collection.  The source wire model already validated each
        # item, so normalizing here must not invoke child validators again.
        return [
            _normalize_validated_nested_family_value(
                item,
                family=family,
                child_label=child_label,
            )
            for item in value
        ]
    if not isinstance(value, Mapping):
        return value
    child_compiled = family._compiled_family()
    normalized = _to_current_names(
        child_compiled,
        child_compiled.version(child_label),
        dict(value),
    )
    return _normalize_validated_explicit_nested_payloads(
        payload=normalized,
        compiled=child_compiled,
        parent_label=child_label,
    )


def _apply_selected_decorator_migrations(
    *,
    payload: dict[str, Any],
    selections: tuple[_DecoratorRouteSelection, ...],
    target_label: str,
) -> dict[str, Any]:
    converted = payload
    for selection in _decorator_selections_child_first(selections):
        nested_source = selection.label
        nested_target = selection.route.child_label(target_label)
        if nested_source == nested_target:
            continue

        converted = _transform_payload_at_location(
            converted,
            location=selection.location,
            transform=partial(
                _convert_decorator_value,
                family=selection.route.family,
                source_label=nested_source,
                target_label=nested_target,
                collection_kind=_decorator_collection_kind(selection.route),
            ),
        )
        selection.label = nested_target
        _refresh_decorator_selection_identity(converted, selection)
    return converted


def _convert_decorator_value(
    value: Any,
    *,
    family: SchemaFamily[Any],
    source_label: str,
    target_label: str,
    collection_kind: Literal["list", "tuple", "set", "frozenset"] | None,
) -> Any:
    return _convert_nested_family_payload(
        family=family,
        payload=value,
        source_label=source_label,
        target_label=target_label,
        source_payload_is_canonical=True,
        collection_kind=collection_kind,
    )


def _project_selected_decorator_payloads(
    *,
    payload: dict[str, Any],
    selections: tuple[_DecoratorRouteSelection, ...],
    parent_label: str,
    wire_boundary: bool,
) -> dict[str, Any]:
    projected = payload
    for selection in _decorator_selections_child_first(selections):
        target_label = selection.route.child_label(parent_label)

        projected = _transform_payload_at_location(
            projected,
            location=selection.location,
            transform=partial(
                _project_decorator_value,
                family=selection.route.family,
                target_label=target_label,
                wire_boundary=wire_boundary,
                collection_kind=_decorator_collection_kind(selection.route),
            ),
        )
        _refresh_decorator_selection_identity(projected, selection)
    return projected


def _project_decorator_value(
    value: Any,
    *,
    family: SchemaFamily[Any],
    target_label: str,
    wire_boundary: bool,
    collection_kind: Literal["list", "tuple", "set", "frozenset"] | None,
) -> Any:
    return _project_nested_family_payload(
        family=family,
        payload=value,
        target_label=target_label,
        wire_boundary=wire_boundary,
        collection_kind=collection_kind,
    )


def _materialize_selected_decorator_models(
    *,
    payload: dict[str, Any],
    selections: tuple[_DecoratorRouteSelection, ...],
) -> dict[str, Any]:
    materialized = payload
    for selection in _decorator_selections_child_first(selections):
        materialized = _transform_payload_at_location(
            materialized,
            location=selection.location,
            transform=partial(
                _materialize_decorator_model,
                model=selection.route.family.model,
            ),
        )
        _refresh_decorator_selection_identity(materialized, selection)
    return materialized


def _materialize_decorator_model(value: Any, *, model: type[BaseModel]) -> Any:
    if type(value) is model:
        return value
    if not isinstance(value, Mapping):
        return value
    validation_input = _current_validation_input(model, dict(value))
    if model.model_config.get("revalidate_instances") == "always":
        # Preserve the authoritative branch as an exact instance, but let the
        # final parent validation perform the one real validation required by
        # the model's explicit revalidation policy.
        return model.model_construct(**validation_input)
    return model.model_validate(
        validation_input,
        by_name=True,
    )


def _decorator_collection_kind(
    route: _CompiledDecoratorNestedFamily,
) -> Literal["list", "tuple", "set", "frozenset"] | None:
    if route.collection_kind == "mapping":
        return None
    return route.collection_kind


def _transform_payload_at_location(
    payload: Any,
    *,
    location: tuple[str | int, ...],
    transform: Callable[[Any], Any],
) -> Any:
    if not location:
        return transform(payload)
    head, *remaining = location
    tail = tuple(remaining)
    if isinstance(payload, BaseModel):
        return _transform_payload_at_location(
            _extract_declared_fields(payload),
            location=location,
            transform=transform,
        )
    if isinstance(payload, Mapping):
        if head not in payload:
            return payload
        current = payload[head]
        transformed = _transform_payload_at_location(
            current,
            location=tail,
            transform=transform,
        )
        if transformed is current:
            return payload
        copied = dict(payload)
        copied[head] = transformed
        return copied
    if isinstance(payload, list | tuple) and isinstance(head, int):
        if head < 0 or head >= len(payload):
            return payload
        current = payload[head]
        transformed = _transform_payload_at_location(
            current,
            location=tail,
            transform=transform,
        )
        if transformed is current:
            return payload
        copied_items = list(payload)
        copied_items[head] = transformed
        return copied_items if isinstance(payload, list) else tuple(copied_items)
    return payload


def _payload_at_location(
    payload: Any,
    location: tuple[str | int, ...],
) -> tuple[bool, Any]:
    current = payload
    for part in location:
        if isinstance(current, Mapping):
            if part not in current:
                return False, None
            current = current[part]
            continue
        if isinstance(current, list | tuple) and isinstance(part, int):
            if part < 0 or part >= len(current):
                return False, None
            current = current[part]
            continue
        return False, None
    return True, current


def _refresh_decorator_selection_identities(
    payload: Any,
    selections: tuple[_DecoratorRouteSelection, ...],
) -> None:
    for selection in selections:
        _refresh_decorator_selection_identity(payload, selection)


def _refresh_decorator_selection_identity(
    payload: Any,
    selection: _DecoratorRouteSelection,
) -> None:
    found, value = _payload_at_location(payload, selection.location)
    selection.value_identity = (
        id(value) if found and isinstance(value, Mapping | BaseModel) else None
    )


def _decorator_selection_depth(selection: _DecoratorRouteSelection) -> int:
    depth = 0
    parent = selection.parent
    while parent is not None:
        depth += 1
        parent = parent.parent
    return depth


def _decorator_selections_child_first(
    selections: tuple[_DecoratorRouteSelection, ...],
) -> tuple[_DecoratorRouteSelection, ...]:
    return tuple(
        sorted(
            selections,
            key=_decorator_selection_depth,
            reverse=True,
        )
    )


def _decorator_selections_parent_first(
    selections: tuple[_DecoratorRouteSelection, ...],
) -> tuple[_DecoratorRouteSelection, ...]:
    return tuple(sorted(selections, key=_decorator_selection_depth))


def _decorator_dispatch_site(
    route: _CompiledDecoratorNestedFamily,
) -> tuple[tuple[str, str], ...]:
    return tuple(
        (step.kind, "*") if step.kind == "union_arm" else (step.kind, step.value)
        for step in route.traversal
    )


def _walk_decorator_payload_candidates(
    payload: Any,
    route: _CompiledDecoratorNestedFamily,
) -> tuple[tuple[Any, tuple[str | int, ...]], ...]:
    states: list[tuple[Any, tuple[str | int, ...]]] = [(payload, ())]
    for step in route.traversal:
        next_states: list[tuple[Any, tuple[str | int, ...]]] = []
        for current, location in states:
            if step.kind == "field":
                if isinstance(current, BaseModel):
                    if step.value not in current.__dict__:
                        continue
                    next_states.append((current.__dict__[step.value], (*location, step.value)))
                elif isinstance(current, Mapping) and step.value in current:
                    next_states.append((current[step.value], (*location, step.value)))
                continue
            if step.kind == "union_arm":
                next_states.append((current, location))
                continue
            if step.kind == "each":
                if not isinstance(current, list | tuple | set | frozenset):
                    continue
                next_states.extend(
                    (item, (*location, ordinal)) for ordinal, item in enumerate(current)
                )
                continue
            if step.kind == "tuple_index":
                if not isinstance(current, list | tuple):
                    continue
                ordinal = int(step.value)
                if ordinal < len(current):
                    next_states.append((current[ordinal], (*location, ordinal)))
                continue
            if step.kind == "mapping_values":
                if not isinstance(current, Mapping):
                    continue
                if any(not isinstance(key, str) for key in current):
                    msg = (
                        "Parent migration introduced a non-string decorator nested "
                        f"mapping key at path {route.path!r}"
                    )
                    raise InvalidMigrationError(msg)
                next_states.extend(
                    (item, (*location, key))
                    for key, item in current.items()
                    if isinstance(key, str)
                )
        states = next_states
        if not states:
            break
    return tuple(
        (value, location) for value, location in states if isinstance(value, Mapping | BaseModel)
    )


def _route_has_mapping_anchor(route: _CompiledDecoratorNestedFamily) -> bool:
    if not any(step.kind == "mapping_values" for step in route.traversal):
        return False
    # A mapping key is only an occurrence anchor when the complete route to it
    # is structurally stable.  A dynamic collection on either side of the
    # mapping can reorder while retaining the same inner key, so anchoring the
    # new absolute location would silently transfer a union branch identity.
    return not any(step.kind == "each" for step in route.traversal)


def _reconcile_decorator_selections(
    *,
    payload: dict[str, Any],
    selections: tuple[_DecoratorRouteSelection, ...],
    compiled: _CompiledFamily,
    discover_new: bool,
) -> tuple[_DecoratorRouteSelection, ...]:
    if not selections:
        if not discover_new:
            return selections
        return _discover_typed_decorator_selections(
            payload=payload,
            compiled=compiled,
            selections=selections,
        )

    selected_by_site: dict[
        tuple[int, tuple[tuple[str, str], ...]],
        list[_DecoratorRouteSelection],
    ] = {}
    for selection in selections:
        site = _decorator_dispatch_site(selection.route)
        selected_by_site.setdefault((id(selection.parent), site), []).append(selection)

    identity_sites = {
        selection.value_identity: site_key
        for site_key, site_selections in selected_by_site.items()
        for selection in site_selections
        if selection.value_identity is not None
    }
    replaced_owner_ids: set[int] = set()
    groups = tuple(
        sorted(
            selected_by_site.items(),
            key=lambda item: _decorator_selection_depth(item[1][0]),
        )
    )
    for site_key, previous in groups:
        if any(
            _selection_has_replaced_ancestor(selection, replaced_owner_ids)
            for selection in previous
        ):
            continue
        declarations = previous[0].site_routes
        parent = previous[0].parent
        if parent is None:
            base_payload = payload
            location_prefix: tuple[str | int, ...] = ()
        else:
            found, base_payload = _payload_at_location(payload, parent.location)
            if not found:
                msg = (
                    "Parent migration removed a decorator nested owner occurrence at "
                    f"path {parent.route.path!r}"
                )
                raise InvalidMigrationError(msg)
            location_prefix = parent.location
        candidates = [
            (value, (*location_prefix, *relative_location), relative_location)
            for value, relative_location in _walk_decorator_payload_candidates(
                base_payload,
                declarations[0],
            )
        ]
        if any(
            id(value) in identity_sites and identity_sites[id(value)] != site_key
            for value, _, _ in candidates
        ):
            msg = (
                "Parent migration moved a decorator nested occurrence across dispatch "
                f"sites at path {declarations[0].path!r}"
            )
            raise InvalidMigrationError(msg)
        if len(candidates) < len(previous):
            msg = (
                "Parent migration changed decorator nested occurrence cardinality at "
                f"path {declarations[0].path!r}; typed replacements must remain one-to-one"
            )
            raise InvalidMigrationError(msg)

        remaining_candidates = list(enumerate(candidates))
        remaining_previous = list(previous)

        # Object identity is the authoritative occurrence token. It permits a
        # heterogeneous list to reorder without rediscovering its union branch.
        for selection in tuple(remaining_previous):
            if selection.value_identity is None:
                continue
            matches = [
                (index, candidate)
                for index, candidate in remaining_candidates
                if id(candidate[0]) == selection.value_identity
            ]
            if len(matches) > 1:
                msg = (
                    "Parent migration reused one decorator nested occurrence at "
                    f"path {selection.route.path!r}"
                )
                raise InvalidMigrationError(msg)
            if not matches:
                continue
            candidate_index, (value, location, relative_location) = matches[0]
            selection.location = location
            selection.relative_location = relative_location
            selection.value_identity = id(value)
            remaining_previous.remove(selection)
            remaining_candidates = [
                item for item in remaining_candidates if item[0] != candidate_index
            ]

        # An exact current-model instance is an explicit, authoritative branch
        # replacement and may establish a new route without validation.
        for candidate_index, (value, location, relative_location) in tuple(remaining_candidates):
            if not remaining_previous:
                break
            if not isinstance(value, BaseModel):
                continue
            matching = tuple(route for route in declarations if type(value) is route.family.model)
            if len(matching) != 1:
                msg = (
                    "Parent migration supplied a typed decorator nested replacement "
                    f"with no unique family at path {declarations[0].path!r}"
                )
                raise InvalidMigrationError(msg)
            route = matching[0]
            replaced = next(
                (selection for selection in remaining_previous if selection.location == location),
                remaining_previous[0],
            )
            replaced.route = route
            replaced.location = location
            replaced.relative_location = relative_location
            replaced.label = route.family.current_version
            replaced.value_identity = id(value)
            replaced_owner_ids.add(id(replaced))
            remaining_previous.remove(replaced)
            remaining_candidates = [
                item for item in remaining_candidates if item[0] != candidate_index
            ]

        if not remaining_previous:
            if remaining_candidates and not all(
                len(tuple(route for route in declarations if type(value) is route.family.model))
                == 1
                for _, (value, _, _) in remaining_candidates
            ):
                msg = (
                    "Parent migration changed decorator nested occurrence cardinality at "
                    f"path {declarations[0].path!r}; new occurrences must be exact "
                    "current-model instances"
                )
                raise InvalidMigrationError(msg)
            continue

        # Dictionary keys are stable anchors even if a callback copies the
        # nested value. List/set positions and bare union fields are not.
        if all(_route_has_mapping_anchor(selection.route) for selection in remaining_previous):
            anchored = {selection.location: selection for selection in remaining_previous}
            if len(anchored) == len(remaining_previous) and all(
                location in anchored for _, (_, location, _) in remaining_candidates
            ):
                for _, (value, location, relative_location) in remaining_candidates:
                    selection = anchored[location]
                    selection.relative_location = relative_location
                    selection.value_identity = id(value)
                continue

        remaining_families = {selection.route.family for selection in remaining_previous}
        declared_families = {route.family for route in declarations}
        if len(remaining_families) == 1 and len(declared_families) == 1:
            if len(remaining_candidates) != len(remaining_previous):
                msg = (
                    "Parent migration changed decorator nested occurrence cardinality at "
                    f"path {declarations[0].path!r}; typed replacements must remain "
                    "one-to-one"
                )
                raise InvalidMigrationError(msg)
            for selection, (_, (value, location, relative_location)) in zip(
                remaining_previous,
                remaining_candidates,
                strict=True,
            ):
                selection.location = location
                selection.relative_location = relative_location
                selection.value_identity = id(value)
            continue

        msg = (
            "Parent migration replaced overlapping decorator nested union values "
            f"with ambiguous raw mappings at path {declarations[0].path!r}; use exact "
            "current-model instances to establish branch identity"
        )
        raise InvalidMigrationError(msg)

    retained = tuple(
        selection
        for selection in selections
        if not _selection_has_replaced_ancestor(selection, replaced_owner_ids)
    )
    _validate_unique_decorator_selection_identities(retained)
    if not discover_new:
        return retained
    discovered = _discover_typed_decorator_selections(
        payload=payload,
        compiled=compiled,
        selections=retained,
    )
    _validate_unique_decorator_selection_identities(discovered)
    return discovered


def _selection_has_replaced_ancestor(
    selection: _DecoratorRouteSelection,
    replaced_owner_ids: set[int],
) -> bool:
    parent = selection.parent
    while parent is not None:
        if id(parent) in replaced_owner_ids:
            return True
        parent = parent.parent
    return False


def _validate_unique_decorator_selection_identities(
    selections: tuple[_DecoratorRouteSelection, ...],
) -> None:
    locations_by_identity: dict[int, tuple[str | int, ...]] = {}
    for selection in selections:
        identity = selection.value_identity
        if identity is None:
            continue
        prior_location = locations_by_identity.get(identity)
        if prior_location is not None and prior_location != selection.location:
            msg = (
                "Parent migration reused one decorator nested occurrence at "
                f"path {selection.route.path!r}"
            )
            raise InvalidMigrationError(msg)
        locations_by_identity[identity] = selection.location


def _discover_typed_decorator_selections(
    *,
    payload: dict[str, Any],
    compiled: _CompiledFamily,
    selections: tuple[_DecoratorRouteSelection, ...],
) -> tuple[_DecoratorRouteSelection, ...]:
    discovered = list(selections)

    def visit_owner(
        owner_payload: Any,
        owner_compiled: _CompiledFamily,
        *,
        location_prefix: tuple[str | int, ...],
        parent: _DecoratorRouteSelection | None,
    ) -> None:
        sites: dict[
            tuple[tuple[str, str], ...],
            list[_CompiledDecoratorNestedFamily],
        ] = {}
        for route in owner_compiled.decorator_nested:
            sites.setdefault(_decorator_dispatch_site(route), []).append(route)

        for routes in sites.values():
            declarations = tuple(routes)
            for value, relative_location in _walk_decorator_payload_candidates(
                owner_payload,
                declarations[0],
            ):
                location = (*location_prefix, *relative_location)
                if any(
                    selection.parent is parent and selection.location == location
                    for selection in discovered
                ):
                    continue
                matches = tuple(
                    route for route in declarations if type(value) is route.family.model
                )
                if len(matches) != 1:
                    if isinstance(value, Mapping) and not _route_has_non_child_mapping_arm(
                        owner_compiled.model,
                        declarations[0],
                    ):
                        msg = (
                            "Parent migration introduced an untyped decorator nested "
                            f"mapping at path {declarations[0].path!r}; use an exact "
                            "current-model instance to establish branch identity"
                        )
                        raise InvalidMigrationError(msg)
                    continue
                route = matches[0]
                if any(
                    selection.value_identity == id(value) and selection.location != location
                    for selection in discovered
                ):
                    msg = (
                        "Parent migration reused one decorator nested occurrence at "
                        f"path {route.path!r}"
                    )
                    raise InvalidMigrationError(msg)
                discovered.append(
                    _DecoratorRouteSelection(
                        route=route,
                        location=location,
                        relative_location=relative_location,
                        site_routes=declarations,
                        label=route.family.current_version,
                        parent=parent,
                        value_identity=id(value),
                    )
                )

        children = tuple(selection for selection in discovered if selection.parent is parent)
        for child in children:
            found, child_payload = _payload_at_location(payload, child.location)
            if not found:
                continue
            visit_owner(
                child_payload,
                child.route.family._compiled_family(),
                location_prefix=child.location,
                parent=child,
            )

    visit_owner(payload, compiled, location_prefix=(), parent=None)
    return tuple(discovered)


def _route_has_non_child_mapping_arm(
    model: type[BaseModel],
    route: _CompiledDecoratorNestedFamily,
) -> bool:
    annotation: Any = model
    for step in route.traversal:
        normalized = _strip_annotated(annotation)
        if step.kind == "field":
            annotation = normalized.model_fields[step.value].annotation
            continue
        if step.kind == "union_arm":
            arguments = get_args(normalized)
            ordinal = int(step.value)
            if any(
                _annotation_accepts_raw_mapping(argument)
                for index, argument in enumerate(arguments)
                if index != ordinal
            ):
                return True
            annotation = arguments[ordinal]
            continue
        arguments = get_args(normalized)
        if step.kind in ("each", "mapping_values"):
            annotation = arguments[1] if step.kind == "mapping_values" else arguments[0]
            continue
        if step.kind == "tuple_index":
            ordinal = int(step.value)
            annotation = arguments[ordinal]
    return False


def _annotation_accepts_raw_mapping(annotation: Any) -> bool:
    normalized = _strip_annotated(annotation)
    if normalized is Any:
        return True
    origin = get_origin(normalized)
    if origin in (Union, UnionType):
        return any(_annotation_accepts_raw_mapping(item) for item in get_args(normalized))
    runtime_type = origin if isinstance(origin, type) else normalized
    if not isinstance(runtime_type, type):
        return False
    try:
        return issubclass(dict, runtime_type)
    except TypeError:
        return False


def _apply_nested_family_migrations(
    *,
    payload: dict[str, Any],
    compiled: _CompiledFamily,
    source_label: str,
    target_label: str,
    source_payload_is_canonical: bool = False,
) -> dict[str, Any]:
    if not compiled.nested:
        return payload
    current_payload: dict[str, Any] = payload
    for nested in compiled.nested:
        nested_source = nested.child_label(source_label)
        nested_target = nested.child_label(target_label)
        if nested_source == nested_target and source_payload_is_canonical:
            continue
        current_payload = _convert_nested_child_family(
            payload=current_payload,
            model=compiled.model,
            path=nested.path,
            family=nested.family,
            source_label=nested_source,
            target_label=nested_target,
            source_payload_is_canonical=source_payload_is_canonical,
            collection_kind=_nested_family_collection_kind(
                model=compiled.model,
                path=nested.path,
            ),
        )
    return current_payload


def _preflight_validation_route(
    family: SchemaFamily[Any],
    compiled: _CompiledFamily,
    *,
    source_version: str,
) -> None:
    candidate = compiled.catalog.validation_plans[compiled.index(source_version)]
    blocked = next(
        (step for step in candidate.steps if step.semantics == "unavailable"),
        None,
    )
    if blocked is None:
        return
    msg = (
        f"Schema family {family.name!r} cannot validate "
        f"{candidate.source_version!r} -> {candidate.target_version!r}: nested route "
        f"at path {blocked.schema_path!r} has no complete route "
        f"{blocked.source_version!r} -> {blocked.target_version!r}"
    )
    raise IrreversibleTransitionError(msg)


def _preflight_nested_version_metadata(
    *,
    payload: Any,
    compiled: _CompiledFamily,
    parent_label: str,
    representation_annotation: Any | None = None,
) -> None:
    if not compiled.nested and not compiled.decorator_nested:
        return
    parent_version = compiled.version(parent_label)
    root_annotation = (
        parent_version.model if representation_annotation is None else representation_annotation
    )
    payload_is_canonical = False
    if isinstance(payload, BaseModel):
        payload_is_canonical = isinstance(payload, parent_version.model) or (
            parent_label == compiled.current_version and isinstance(payload, compiled.model)
        )
        payload = _extract_preflight_fields(
            payload,
            preserve_nested_models=True,
        )
    if not isinstance(payload, Mapping) and _runtime_structural_fields(root_annotation) is None:
        return

    for nested in compiled.nested:
        source_path = _target_nested_path(parent_version, nested.path)
        expected = nested.child_label(parent_label)
        child_compiled = nested.family._compiled_family()
        child_model = _explicit_runtime_body_model(
            child_compiled.version(expected).model,
        )
        route_occurrences = _declared_payload_occurrences_at_path(
            payload,
            annotation=root_annotation,
            path=source_path,
            prefer_aliases=not payload_is_canonical,
            conservative_structural_unions=True,
        )
        for nested_payload, nested_annotation in route_occurrences:
            for item, item_annotation in _runtime_nested_structural_occurrences(
                nested_payload,
                annotation=nested_annotation,
                model=child_model,
                conservative_structural_unions=True,
            ):
                metadata_payload = _runtime_preflight_metadata_payload(
                    item,
                    annotation=item_annotation,
                    family_metadata_path=(
                        child_compiled.version_metadata.path
                        if child_compiled.version_metadata is not None
                        and child_compiled.version_metadata.owner == "family"
                        else None
                    ),
                )
                found, declared = _declared_nested_version_metadata(
                    payload=metadata_payload,
                    compiled=child_compiled,
                    source_label=expected,
                )
                if found and not _matches_version_label(declared, expected):
                    declared_display = _safe_nested_version_display(
                        declared,
                        compiled=child_compiled,
                    )
                    msg = (
                        f"Nested family {nested.family.name!r} at path {nested.path!r} "
                        f"expects version {expected!r} for parent label {parent_label!r}, "
                        f"but the payload declares {declared_display}"
                    )
                    raise SchemaVersionError(msg)
                _preflight_nested_version_metadata(
                    payload=item,
                    compiled=child_compiled,
                    parent_label=expected,
                    representation_annotation=item_annotation,
                )

    if not isinstance(payload, Mapping):
        return
    root_names = {
        route.path[0]: parent_version.projection.field(route.path[0]).version_name
        for route in compiled.decorator_nested
    }
    for route in compiled.decorator_nested:
        expected = route.child_label(parent_label)
        child_compiled = route.family._compiled_family()
        for item, _ in _raw_decorator_route_values(
            payload,
            model=parent_version.model,
            route=route,
            root_names=root_names,
        ):
            if isinstance(item, BaseModel):
                item = _extract_preflight_fields(item)
            if not isinstance(item, Mapping):
                continue
            found, declared = _declared_nested_version_metadata(
                payload=item,
                compiled=child_compiled,
                source_label=expected,
            )
            if found and not _matches_version_label(declared, expected):
                declared_display = _safe_nested_version_display(
                    declared,
                    compiled=child_compiled,
                )
                msg = (
                    f"Decorator nested family {route.family.name!r} at path {route.path!r} "
                    f"expects version {expected!r} for parent label {parent_label!r}, "
                    f"but the payload declares {declared_display}"
                )
                raise SchemaVersionError(msg)
            _preflight_nested_version_metadata(
                payload=item,
                compiled=child_compiled,
                parent_label=expected,
            )


def _runtime_preflight_metadata_payload(
    value: Any,
    *,
    annotation: Any,
    family_metadata_path: VersionPath | None,
) -> dict[Any, Any]:
    structure = _runtime_structural_fields(annotation)
    assert structure is not None
    kind, owner, field_annotations, _required_keys = structure
    extracted = (
        {key: _copy_alias_payload_value(item) for key, item in value.items()}
        if isinstance(value, Mapping)
        else {}
    )
    if isinstance(value, BaseModel):
        extras = value.__pydantic_extra__
        if isinstance(extras, Mapping):
            extracted.update(
                (key, _copy_alias_payload_value(item))
                for key, item in extras.items()
                if isinstance(key, str)
            )
    for field_name, field_annotation in field_annotations.items():
        present, field_value = _runtime_structural_field_value(
            value,
            kind=kind,
            field_name=field_name,
        )
        if not present:
            continue
        extracted.setdefault(field_name, field_value)
        if kind == "model":
            alias_config = cast(type[BaseModel], owner).model_config
            model_field = cast(type[BaseModel], owner).model_fields[field_name]
            alias_paths = _alias_paths(
                model_field.validation_alias,
                fallback=model_field.alias,
            )
        else:
            alias_config, alias_paths = _runtime_structural_validation_alias_paths(
                kind=kind,
                owner=owner,
                field_name=field_name,
                field_annotation=field_annotation,
            )
        if alias_config.get("validate_by_alias", True) is False:
            continue
        for alias_path in alias_paths:
            if family_metadata_path is not None:
                expected_path = (
                    (family_metadata_path,)
                    if isinstance(family_metadata_path, str)
                    else family_metadata_path
                )
                if alias_path != expected_path:
                    continue
            _set_preflight_alias_payload_value(
                extracted,
                path=alias_path,
                value=field_value,
            )
    return extracted


def _set_preflight_alias_payload_value(
    payload: dict[Any, Any],
    *,
    path: tuple[Any, ...],
    value: Any,
) -> None:
    if not path or not all(isinstance(part, str) for part in path):
        return
    current = payload
    for part in path[:-1]:
        nested = current.get(part, _MISSING)
        if nested is _MISSING:
            child: dict[Any, Any] = {}
            current[part] = child
            current = child
            continue
        if not isinstance(nested, Mapping):
            return
        child = dict(nested)
        current[part] = child
        current = child
    current.setdefault(path[-1], value)


def _preflight_selected_decorator_version_metadata(
    *,
    payload: Any,
    compiled: _CompiledFamily,
    parent_label: str,
    selections: tuple[_DecoratorRouteSelection, ...],
) -> None:
    if not selections:
        return
    parent_version = compiled.version(parent_label)
    if isinstance(payload, BaseModel):
        payload = _extract_preflight_fields(payload)
    if not isinstance(payload, Mapping):
        return
    canonical = _to_current_names(compiled, parent_version, dict(payload))
    for selection in _decorator_selections_parent_first(selections):
        route = selection.route
        found_raw, raw = _payload_at_location(canonical, selection.location)
        if not found_raw:
            continue
        if isinstance(raw, BaseModel):
            raw = _extract_preflight_fields(raw)
        if not isinstance(raw, Mapping):
            continue
        expected = route.child_label(parent_label)
        child_compiled = route.family._compiled_family()
        found, declared = _declared_nested_version_metadata(
            payload=raw,
            compiled=child_compiled,
            source_label=expected,
        )
        if found and not _matches_version_label(declared, expected):
            declared_display = _safe_nested_version_display(
                declared,
                compiled=child_compiled,
            )
            msg = (
                f"Decorator nested family {route.family.name!r} at path {route.path!r} "
                f"expects version {expected!r} for parent label {parent_label!r}, "
                f"but the payload declares {declared_display}"
            )
            raise SchemaVersionError(msg)
        _preflight_nested_version_metadata(
            payload=raw,
            compiled=child_compiled,
            parent_label=expected,
        )
        canonical = _transform_payload_at_location(
            canonical,
            location=selection.location,
            transform=partial(
                _normalize_decorator_value,
                compiled=child_compiled,
                child_label=expected,
            ),
        )


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


def _declared_nested_version_metadata(
    *,
    payload: Mapping[str, Any],
    compiled: _CompiledFamily,
    source_label: str,
) -> tuple[bool, Any]:
    metadata = compiled.version_metadata
    if metadata is None:
        return False, None
    if metadata.owner == "family":
        metadata_path = (metadata.path,) if isinstance(metadata.path, str) else metadata.path
        if len(metadata_path) > 1 and metadata_path[0] in payload:
            current: Any = payload[metadata_path[0]]
            for part in metadata_path[1:]:
                if not isinstance(current, Mapping) or set(current) != {part}:
                    msg = (
                        f"Nested family {compiled.name!r} reserves the entire version "
                        f"metadata root {metadata_path[0]!r} without siblings"
                    )
                    raise SchemaVersionError(msg)
                current = current[part]
        if not _path_has_payload(payload, metadata_path):
            return False, None
        declared = _get_version_field(payload, metadata.path)
        return True, declared

    source_model = compiled.version(source_label).model
    normalized = _normalize_payload_field_aliases(source_model, payload)
    metadata_field = _model_metadata_field_name(compiled)
    if metadata_field not in normalized:
        return False, None
    return True, normalized[metadata_field]


def _verify_validated_family_version_metadata(
    *,
    value: BaseModel,
    compiled: _CompiledFamily,
    label: str,
) -> None:
    metadata = compiled.version_metadata
    if metadata is None:
        return
    metadata_payload = _extract_preflight_fields(value)
    found, declared = _declared_nested_version_metadata(
        payload=metadata_payload,
        compiled=compiled,
        source_label=label,
    )
    if not found:
        msg = (
            f"Validated source for schema family {compiled.name!r} and version {label!r} "
            "omitted required version metadata"
        )
        raise SchemaVersionError(msg)
    if _matches_version_label(declared, label):
        return
    declared_display = _safe_nested_version_display(declared, compiled=compiled)
    msg = (
        f"Validated source for schema family {compiled.name!r} and version {label!r} "
        f"contains version metadata declaring {declared_display}"
    )
    raise SchemaVersionError(msg)


def _nested_family_collection_kind(
    *,
    model: type[BaseModel],
    path: tuple[str, ...],
) -> Literal["list", "tuple", "set", "frozenset"] | None:
    annotation: Any = model
    for index, key in enumerate(path):
        if not _is_runtime_base_model_type(annotation):
            return None
        field_info = annotation.model_fields.get(key)
        if field_info is None:
            return None
        annotation = _unwrap_optional_annotated(field_info.annotation)
        kind = _collection_kind(annotation)
        if index == len(path) - 1:
            return kind
        if kind is None:
            continue
        args = get_args(_runtime_annotation_value(annotation))
        if not args:
            return None
        annotation = _unwrap_optional_annotated(args[0])
    return None


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


def _has_duplicate_payload(payload: list[Any]) -> bool:
    for index, item in enumerate(payload):
        if item in payload[:index]:
            return True
    return False


def _prune_nested_family_metadata_payload(
    payload: Any,
    family: _CompiledFamily,
    target_label: str | None = None,
    *,
    by_alias: Any = False,
    source_value: Any = _MISSING,
    representation_annotation: Any | None = None,
) -> None:
    resolved_target = family.current_version if target_label is None else target_label
    if not isinstance(payload, Mapping):
        msg = (
            f"Nested target wire model for family {family.name!r} and version "
            f"{resolved_target!r} must serialize to an object"
        )
        raise ValueError(msg)
    metadata = family.version_metadata
    if metadata is not None:
        if metadata.owner == "family":
            _verify_serialized_nested_family_metadata(
                payload,
                metadata_path=metadata.path,
                expected=resolved_target,
                family_name=family.name,
            )
            _remove_version_field(payload, metadata.path)
        else:
            _verify_serialized_model_metadata(
                payload,
                compiled=family,
                requested=resolved_target,
                target_model=family.version(resolved_target).model,
                by_alias=by_alias,
            )

    if not family.nested:
        return

    target = family.version(resolved_target)
    root_annotation = (
        target.model if representation_annotation is None else representation_annotation
    )
    for child in family.nested:
        target_path = _target_nested_path(target, child.path)
        _prune_serialized_nested_path_through_annotation(
            payload,
            source_payload=source_value,
            annotation=root_annotation,
            path=target_path,
            family=child.family._compiled_family(),
            target_label=child.child_label(resolved_target),
            by_alias=by_alias,
        )


def _verify_serialized_nested_family_metadata(
    payload: Mapping[Any, Any],
    *,
    metadata_path: VersionPath,
    expected: str,
    family_name: str,
) -> None:
    path = (metadata_path,) if isinstance(metadata_path, str) else metadata_path
    root = path[0]
    if root not in payload:
        return
    current: Any = payload[root]
    for part in path[1:]:
        if not isinstance(current, Mapping):
            msg = (
                f"Nested target wire model for family {family_name!r} serialized a "
                f"non-object version metadata component below {root!r}"
            )
            raise ValueError(msg)
        if set(current) != {part}:
            msg = (
                f"Nested target wire model for family {family_name!r} reserves the "
                f"entire version metadata root {root!r} without siblings"
            )
            raise ValueError(msg)
        current = current[part]
    if not _matches_version_label(current, expected):
        msg = (
            f"Nested target wire model for family {family_name!r} serialized version "
            f"metadata {_version_field_display(metadata_path)!r} that does not match "
            f"expected label {expected!r}"
        )
        raise ValueError(msg)


def _prune_nested_family_metadata_at_path(
    *,
    payload: Any,
    source_payload: Any = _MISSING,
    path: tuple[str, ...],
    family: SchemaFamily[Any] | _CompiledFamily,
    model: type[BaseModel],
    target_label: str | None = None,
    by_alias: Any = False,
) -> None:
    compiled_family = family if isinstance(family, _CompiledFamily) else family._compiled_family()
    resolved_target = compiled_family.current_version if target_label is None else target_label
    _prune_serialized_nested_path(
        payload,
        source_payload=source_payload,
        model=model,
        path=path,
        family=compiled_family,
        target_label=resolved_target,
        by_alias=by_alias,
    )


def _prune_serialized_nested_path(
    payload: Any,
    *,
    source_payload: Any,
    model: type[BaseModel],
    path: tuple[str, ...],
    family: _CompiledFamily,
    target_label: str,
    by_alias: Any,
) -> None:
    if not isinstance(payload, Mapping):
        msg = (
            f"Target wire model containing nested family {family.name!r} must "
            f"serialize declared path {path!r} through objects"
        )
        raise ValueError(msg)
    field_name, *remaining = path
    field_info = model.model_fields.get(field_name)
    if field_info is None:
        if field_name in payload:
            msg = (
                f"Target wire model emitted omitted nested family {family.name!r} at path {path!r}"
            )
            raise ValueError(msg)
        return
    found, field_payload = _serialized_nested_field_payload(
        payload,
        source_payload=source_payload,
        model=model,
        field_name=field_name,
        field_info=field_info,
        family_name=family.name,
        by_alias=by_alias,
    )
    if not found:
        msg = f"Target wire model omitted declared nested family {family.name!r} at path {path!r}"
        raise ValueError(msg)
    source_field_payload = _declared_source_field_payload(
        source_payload,
        field_name=field_name,
    )
    if remaining:
        _prune_serialized_nested_path_through_annotation(
            field_payload,
            source_payload=source_field_payload,
            annotation=field_info.annotation,
            path=tuple(remaining),
            family=family,
            target_label=target_label,
            by_alias=by_alias,
        )
        return
    _prune_serialized_nested_value(
        field_payload,
        source_payload=source_field_payload,
        annotation=field_info.annotation,
        family=family,
        target_label=target_label,
        by_alias=by_alias,
    )


def _serialized_nested_field_payload(
    payload: Mapping[Any, Any],
    *,
    source_payload: Any,
    model: type[BaseModel],
    field_name: str,
    field_info: Any,
    family_name: str,
    by_alias: Any,
) -> tuple[bool, Any]:
    output_name = _serialized_field_name(model, field_name, by_alias=by_alias)
    if isinstance(source_payload, BaseModel):
        extras = source_payload.__pydantic_extra__
        if isinstance(extras, Mapping) and output_name in extras:
            msg = (
                f"Target wire model extra {output_name!r} overwrites the declared "
                f"location for nested family {family_name!r}"
            )
            raise ValueError(msg)
    candidates = [field_name]
    for candidate in (field_info.alias, field_info.serialization_alias):
        if isinstance(candidate, str) and candidate not in candidates:
            candidates.append(candidate)
    present = tuple(candidate for candidate in candidates if candidate in payload)
    if len(present) > 1:
        formatted = ", ".join(repr(candidate) for candidate in present)
        msg = (
            f"Target wire model serialized duplicate locations for nested family "
            f"{family_name!r}: {formatted}"
        )
        raise ValueError(msg)
    if output_name not in payload:
        return False, None
    return True, payload[output_name]


def _prune_serialized_nested_path_through_annotation(
    payload: Any,
    *,
    source_payload: Any,
    annotation: Any,
    path: tuple[str, ...],
    family: _CompiledFamily,
    target_label: str,
    by_alias: Any,
) -> None:
    runtime_value = payload if source_payload is _MISSING else source_payload
    annotation = _runtime_annotation_value(annotation)
    if isinstance(annotation, TypeVar):
        selected = _matching_declared_annotation(annotation, runtime_value)
        if isinstance(selected, TypeVar):
            selected = next(
                (
                    candidate
                    for candidate in _runtime_type_parameter_values(annotation)
                    if _annotation_declares_path(candidate, path)
                ),
                None,
            )
        if selected is None:
            return
        annotation = _runtime_annotation_value(selected)
    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        if (
            payload is None
            and NoneType in get_args(annotation)
            and (source_payload is None or source_payload is _MISSING)
        ):
            return
        selected = _matching_declared_annotation(annotation, runtime_value)
        if get_origin(selected) not in (Union, UnionType):
            if selected is NoneType or not _annotation_declares_path(selected, path):
                return
            _prune_serialized_nested_path_through_annotation(
                payload,
                source_payload=source_payload,
                annotation=selected,
                path=path,
                family=family,
                target_label=target_label,
                by_alias=by_alias,
            )
            return
        candidates = tuple(
            argument
            for argument in get_args(annotation)
            if argument is not NoneType and _annotation_declares_path(argument, path)
        )
        if candidates:
            _prune_serialized_nested_path_through_annotation(
                payload,
                source_payload=source_payload,
                annotation=candidates[0],
                path=path,
                family=family,
                target_label=target_label,
                by_alias=by_alias,
            )
        return
    if _is_runtime_base_model_type(annotation):
        _prune_serialized_nested_path(
            payload,
            source_payload=source_payload,
            model=annotation,
            path=path,
            family=family,
            target_label=target_label,
            by_alias=by_alias,
        )
        return
    structure = _runtime_structural_fields(annotation)
    if structure is not None:
        _prune_serialized_nested_structural_path(
            payload,
            source_payload=source_payload,
            structure=structure,
            path=path,
            family=family,
            target_label=target_label,
            by_alias=by_alias,
        )
        return
    kind = _collection_kind(annotation)
    if kind is None:
        return
    items = _serialized_collection_items(
        payload,
        annotation,
        source_payload=source_payload,
        family_name=family.name,
    )
    for item, item_annotation, source_item in items:
        _prune_serialized_nested_path_through_annotation(
            item,
            source_payload=source_item,
            annotation=item_annotation,
            path=path,
            family=family,
            target_label=target_label,
            by_alias=by_alias,
        )
    _validate_serialized_set_cardinality(
        payload,
        kind=kind,
        family_name=family.name,
    )


def _prune_serialized_nested_structural_path(
    payload: Any,
    *,
    source_payload: Any,
    structure: tuple[
        Literal["model", "dataclass", "typed_dict"],
        type[Any],
        dict[str, Any],
        frozenset[str],
    ],
    path: tuple[str, ...],
    family: _CompiledFamily,
    target_label: str,
    by_alias: Any,
) -> None:
    if not isinstance(payload, Mapping):
        msg = (
            f"Target wire model containing nested family {family.name!r} must "
            f"serialize declared path {path!r} through objects"
        )
        raise ValueError(msg)
    kind, owner, field_annotations, required_keys = structure
    field_name, *remaining = path
    field_annotation = field_annotations.get(field_name, _MISSING)
    if field_annotation is _MISSING:
        if field_name in payload:
            msg = (
                f"Target wire model emitted omitted nested family {family.name!r} at path {path!r}"
            )
            raise ValueError(msg)
        return

    output_name = _runtime_structural_serialized_field_name(
        kind=kind,
        owner=owner,
        field_name=field_name,
        field_annotation=field_annotation,
        by_alias=by_alias,
    )
    candidates = tuple(dict.fromkeys((field_name, output_name)))
    present = tuple(candidate for candidate in candidates if candidate in payload)
    if len(present) > 1:
        formatted = ", ".join(repr(candidate) for candidate in present)
        msg = (
            f"Target wire model serialized duplicate locations for nested family "
            f"{family.name!r}: {formatted}"
        )
        raise ValueError(msg)
    if output_name not in payload:
        source_present, _source_field = _runtime_structural_field_value(
            source_payload,
            kind=kind,
            field_name=field_name,
        )
        if kind == "typed_dict" and field_name not in required_keys and not source_present:
            return
        msg = f"Target wire model omitted declared nested family {family.name!r} at path {path!r}"
        raise ValueError(msg)

    source_present, source_field = _runtime_structural_field_value(
        source_payload,
        kind=kind,
        field_name=field_name,
    )
    if not source_present:
        source_field = _MISSING
    field_payload = payload[output_name]
    if remaining:
        _prune_serialized_nested_path_through_annotation(
            field_payload,
            source_payload=source_field,
            annotation=field_annotation,
            path=tuple(remaining),
            family=family,
            target_label=target_label,
            by_alias=by_alias,
        )
        return
    _prune_serialized_nested_value(
        field_payload,
        source_payload=source_field,
        annotation=field_annotation,
        family=family,
        target_label=target_label,
        by_alias=by_alias,
    )


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


def _prune_serialized_nested_value(
    payload: Any,
    *,
    source_payload: Any,
    annotation: Any,
    family: _CompiledFamily,
    target_label: str,
    by_alias: Any,
) -> None:
    runtime_value = payload if source_payload is _MISSING else source_payload
    annotation = _runtime_annotation_value(annotation)
    if isinstance(annotation, TypeVar):
        selected = _matching_declared_annotation(annotation, runtime_value)
        if isinstance(selected, TypeVar):
            child_model = _explicit_runtime_body_model(family.version(target_label).model)
            selected = next(
                (
                    candidate
                    for candidate in _runtime_type_parameter_values(annotation)
                    if _annotation_contains_model(candidate, child_model)
                ),
                None,
            )
        if selected is None:
            return
        annotation = _runtime_annotation_value(selected)
    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        arguments = get_args(annotation)
        if (
            payload is None
            and NoneType in arguments
            and (source_payload is None or source_payload is _MISSING)
        ):
            return
        concrete = tuple(argument for argument in arguments if argument is not NoneType)
        runtime_value = payload if source_payload is _MISSING else source_payload
        selected = _matching_declared_annotation(annotation, runtime_value)
        if get_origin(selected) in (Union, UnionType):
            child_model = _explicit_runtime_body_model(family.version(target_label).model)
            selected = next(
                (
                    argument
                    for argument in concrete
                    if _annotation_contains_model(argument, child_model)
                ),
                concrete[0] if concrete else annotation,
            )
        _prune_serialized_nested_value(
            payload,
            source_payload=source_payload,
            annotation=selected,
            family=family,
            target_label=target_label,
            by_alias=by_alias,
        )
        return
    kind = _collection_kind(annotation)
    if kind is not None:
        items = _serialized_collection_items(
            payload,
            annotation,
            source_payload=source_payload,
            family_name=family.name,
        )
        for item, item_annotation, source_item in items:
            _prune_serialized_nested_value(
                item,
                source_payload=source_item,
                annotation=item_annotation,
                family=family,
                target_label=target_label,
                by_alias=by_alias,
            )
        _validate_serialized_set_cardinality(
            payload,
            kind=kind,
            family_name=family.name,
        )
        return
    annotation_is_object = _runtime_structural_fields(annotation) is not None
    if (
        source_payload is not _MISSING
        and _is_concrete_runtime_scalar_annotation(annotation)
        and _runtime_value_matches_annotation(runtime_value, annotation)
        and not isinstance(
            runtime_value,
            BaseModel | Mapping | list | tuple | set | frozenset,
        )
    ):
        # A conforming historical scalar owns its serialized representation.
        # Enum values and field serializers may legitimately emit mappings that
        # happen to contain a child family's metadata key.
        return
    if (
        not annotation_is_object
        and not isinstance(runtime_value, BaseModel | Mapping)
        and not isinstance(payload, Mapping)
    ):
        return
    _prune_nested_family_metadata_payload(
        payload,
        family,
        target_label,
        by_alias=by_alias,
        source_value=source_payload,
        representation_annotation=annotation,
    )


def _serialized_collection_items(
    payload: Any,
    annotation: Any,
    *,
    source_payload: Any,
    family_name: str,
) -> tuple[tuple[Any, Any, Any], ...]:
    if not isinstance(payload, list | tuple | set | frozenset):
        msg = (
            f"Nested target wire collection for family {family_name!r} changed "
            "its declared container shape during serialization"
        )
        raise ValueError(msg)
    values = tuple(payload)
    source_values = (
        tuple(source_payload)
        if isinstance(source_payload, list | tuple | set | frozenset)
        else (_MISSING,) * len(values)
    )
    if source_payload is not _MISSING and len(source_values) != len(values):
        msg = (
            f"Nested rendering for family {family_name!r} cannot preserve "
            "collection cardinality after target serialization"
        )
        raise InvalidMigrationError(msg)
    runtime_value = payload if source_payload is _MISSING else source_payload
    annotation = _runtime_annotation_value(
        _matching_declared_annotation(annotation, runtime_value),
    )
    arguments = get_args(annotation)
    if get_origin(annotation) is tuple and arguments and arguments[-1] is not Ellipsis:
        return tuple(zip(values, arguments, source_values, strict=False))
    item_annotation = arguments[0] if arguments else Any
    return tuple(
        (item, item_annotation, source_item)
        for item, source_item in zip(values, source_values, strict=False)
    )


def _declared_source_field_payload(source: Any, *, field_name: str) -> Any:
    if isinstance(source, BaseModel):
        return source.__dict__.get(field_name, _MISSING)
    if isinstance(source, Mapping):
        return source.get(field_name, _MISSING)
    return _MISSING


def _validate_serialized_set_cardinality(
    payload: Any,
    *,
    kind: Literal["list", "tuple", "set", "frozenset"],
    family_name: str,
) -> None:
    if kind not in ("set", "frozenset"):
        return
    items = list(payload)
    if _has_duplicate_payload(items):
        msg = (
            f"Nested rendering for family {family_name!r} cannot preserve set "
            "cardinality after target serialization"
        )
        raise InvalidMigrationError(msg)


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


def _convert_nested_child_family(
    *,
    payload: Any,
    model: type[BaseModel],
    path: tuple[str, ...],
    family: SchemaFamily[Any],
    source_label: str,
    target_label: str,
    source_payload_is_canonical: bool = False,
    collection_kind: Literal["list", "tuple", "set", "frozenset"] | None = None,
) -> Any:
    def convert(nested_payload: Any) -> Any:
        return _convert_nested_family_payload(
            family=family,
            payload=nested_payload,
            source_label=source_label,
            target_label=target_label,
            source_payload_is_canonical=source_payload_is_canonical,
            collection_kind=collection_kind,
        )

    return _transform_declared_payload_at_path(
        payload,
        model=model,
        path=path,
        transform=convert,
    )


def _convert_nested_family_payload(
    family: SchemaFamily[Any],
    payload: Any,
    source_label: str,
    target_label: str,
    source_payload_is_canonical: bool = False,
    collection_kind: Literal["list", "tuple", "set", "frozenset"] | None = None,
) -> Any:
    compiled = family._compiled_family()
    source_index = compiled.index(source_label)
    target_index = compiled.index(target_label)
    if payload is None:
        return payload
    if isinstance(payload, BaseModel):
        # Parent migrations operate on canonical payloads and may replace a
        # nested value with the current child model between route edges. Keep
        # walking the child route from its declared fields without invoking
        # model or field serializers (or validating the instance again).
        if not isinstance(payload, family.model):
            msg = (
                f"Nested migration for family {family.name!r} received BaseModel "
                f"{type(payload).__name__!r}; expected current model "
                f"{family.model.__name__!r}"
            )
            raise InvalidMigrationError(msg)
        payload = _extract_declared_fields(
            payload,
            declared_model=family.model,
        )
        source_payload_is_canonical = True
    if isinstance(payload, list):
        converted = [
            _convert_nested_family_payload(
                family=family,
                payload=item,
                source_label=source_label,
                target_label=target_label,
                source_payload_is_canonical=source_payload_is_canonical,
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
                source_payload_is_canonical=source_payload_is_canonical,
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
    if isinstance(payload, set | frozenset):
        converted_items = [
            _convert_nested_family_payload(
                family=family,
                payload=item,
                source_label=source_label,
                target_label=target_label,
                source_payload_is_canonical=source_payload_is_canonical,
                collection_kind=collection_kind,
            )
            for item in payload
        ]
        if _has_duplicate_payload(converted_items):
            msg = (
                f"Nested migration for family {family.name!r} "
                "cannot preserve set cardinality while converting mixed payload values"
            )
            raise InvalidMigrationError(msg)
        try:
            return type(payload)(converted_items)
        except TypeError:
            return converted_items
    if not isinstance(payload, Mapping):
        return payload
    if source_payload_is_canonical:
        current_payload = dict(payload)
    else:
        source_version = compiled.version(source_label)
        source_data = source_version.model.model_validate(payload, by_name=True)
        _validate_explicit_nested_runtime_shapes(
            source_data,
            compiled=compiled,
            version=source_version,
            label=source_label,
            recurse_nested_targets=True,
        )
        _verify_validated_family_version_metadata(
            value=source_data,
            compiled=compiled,
            label=source_label,
        )
        _preflight_nested_version_metadata(
            payload=source_data,
            compiled=compiled,
            parent_label=source_label,
        )
        current_payload = dict(
            _to_current_names(
                compiled,
                source_version,
                _extract_declared_fields(source_data),
            )
        )
    nested_payload_is_canonical = source_payload_is_canonical
    if source_index == target_index:
        current_payload = _apply_nested_family_migrations(
            payload=current_payload,
            compiled=compiled,
            source_label=source_label,
            target_label=target_label,
            source_payload_is_canonical=nested_payload_is_canonical,
        )
        _rebase_canonical_version_metadata(
            payload=current_payload,
            compiled=compiled,
            target_label=target_label,
        )
        return current_payload
    if source_index < target_index:
        for edge_index in range(source_index, target_index):
            transition = compiled.transitions[edge_index]
            current_payload = _apply_nested_family_migrations(
                payload=current_payload,
                compiled=compiled,
                source_label=transition.source,
                target_label=transition.target,
                source_payload_is_canonical=nested_payload_is_canonical,
            )
            nested_payload_is_canonical = True
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
            current_payload = _apply_nested_family_migrations(
                payload=current_payload,
                compiled=compiled,
                source_label=transition.target,
                target_label=transition.source,
                source_payload_is_canonical=nested_payload_is_canonical,
            )
            nested_payload_is_canonical = True
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
    _rebase_canonical_version_metadata(
        payload=current_payload,
        compiled=compiled,
        target_label=target_label,
    )
    return current_payload


def _rebase_canonical_version_metadata(
    *,
    payload: dict[str, Any],
    compiled: _CompiledFamily,
    target_label: str,
) -> None:
    metadata = compiled.version_metadata
    if metadata is None:
        return
    if metadata.owner == "family":
        _remove_version_field(payload, metadata.path)
        return
    payload[_model_metadata_field_name(compiled)] = target_label


def _project_nested_family_payloads(
    *,
    payload: dict[str, Any],
    compiled: _CompiledFamily,
    parent_label: str,
    wire_boundary: bool,
) -> dict[str, Any]:
    if not compiled.nested:
        return payload
    projected: dict[str, Any] = payload
    for nested in compiled.nested:
        projected = _project_nested_child_family(
            payload=projected,
            model=compiled.model,
            path=nested.path,
            family=nested.family,
            target_label=nested.child_label(parent_label),
            wire_boundary=wire_boundary,
            collection_kind=_nested_family_collection_kind(
                model=compiled.model,
                path=nested.path,
            ),
        )
    return projected


def _project_nested_child_family(
    *,
    payload: Any,
    model: type[BaseModel],
    path: tuple[str, ...],
    family: SchemaFamily[Any],
    target_label: str,
    wire_boundary: bool,
    collection_kind: Literal["list", "tuple", "set", "frozenset"] | None,
) -> Any:
    def project(nested_payload: Any) -> Any:
        return _project_nested_family_payload(
            family=family,
            payload=nested_payload,
            target_label=target_label,
            wire_boundary=wire_boundary,
            collection_kind=collection_kind,
        )

    return _transform_declared_payload_at_path(
        payload,
        model=model,
        path=path,
        transform=project,
    )


def _project_nested_family_payload(
    *,
    family: SchemaFamily[Any],
    payload: Any,
    target_label: str,
    wire_boundary: bool,
    collection_kind: Literal["list", "tuple", "set", "frozenset"] | None,
) -> Any:
    if payload is None:
        return payload
    if isinstance(payload, list):
        return [
            _project_nested_family_payload(
                family=family,
                payload=item,
                target_label=target_label,
                wire_boundary=wire_boundary,
                collection_kind=collection_kind,
            )
            for item in payload
        ]
    if isinstance(payload, tuple):
        return tuple(
            _project_nested_family_payload(
                family=family,
                payload=item,
                target_label=target_label,
                wire_boundary=wire_boundary,
                collection_kind=collection_kind,
            )
            for item in payload
        )
    if isinstance(payload, set | frozenset):
        return [
            _project_nested_family_payload(
                family=family,
                payload=item,
                target_label=target_label,
                wire_boundary=wire_boundary,
                collection_kind=collection_kind,
            )
            for item in payload
        ]
    if isinstance(payload, BaseModel):
        payload = _extract_declared_fields(payload)
    if not isinstance(payload, Mapping):
        return payload

    compiled = family._compiled_family()
    metadata = compiled.version_metadata
    if metadata is not None and metadata.owner == "family":
        _verify_serialized_nested_family_metadata(
            payload,
            metadata_path=metadata.path,
            expected=target_label,
            family_name=compiled.name,
        )
    projected = _project_nested_family_payloads(
        payload=dict(payload),
        compiled=compiled,
        parent_label=target_label,
        wire_boundary=wire_boundary,
    )
    _rebase_canonical_version_metadata(
        payload=projected,
        compiled=compiled,
        target_label=target_label,
    )
    if not wire_boundary:
        return projected

    target_payload = _to_version_names(compiled.version(target_label), projected)
    if metadata is not None and metadata.owner == "family":
        if collection_kind in ("set", "tuple", "frozenset"):
            _set_version_field(target_payload, metadata.path, target_label)
        else:
            _remove_version_field(target_payload, metadata.path)
    return target_payload


def _validate_nested_collection_cardinality(
    *,
    input_payload: dict[str, Any],
    validated_model: BaseModel,
    compiled: _CompiledFamily,
    parent_label: str,
    selections: tuple[_DecoratorRouteSelection, ...],
) -> None:
    del selections
    if not compiled.nested and not compiled.decorator_nested:
        return
    target = compiled.version(parent_label)
    validated_payload = _extract_declared_fields(validated_model)
    _validate_nested_collection_cardinality_payloads(
        input_payloads=(input_payload,),
        validated_payloads=(validated_payload,),
        input_annotations=(target.model,),
        validated_annotations=(target.model,),
        compiled=compiled,
        parent_label=parent_label,
    )


def _decorator_set_cardinalities(
    payload: Any,
    route: _CompiledDecoratorNestedFamily,
) -> dict[int, tuple[int, ...]]:
    states: list[Any] = [payload]
    cardinalities: dict[int, list[int]] = {}
    for step_index, step in enumerate(route.traversal):
        next_states: list[Any] = []
        for current in states:
            if step.kind == "field":
                if isinstance(current, BaseModel) and step.value in current.__dict__:
                    next_states.append(current.__dict__[step.value])
                elif isinstance(current, Mapping) and step.value in current:
                    next_states.append(current[step.value])
                continue
            if step.kind == "union_arm":
                next_states.append(current)
                continue
            if step.kind == "each":
                if not isinstance(current, list | tuple | set | frozenset):
                    continue
                if step.value in ("set", "frozenset"):
                    cardinalities.setdefault(step_index, []).append(len(current))
                next_states.extend(current)
                continue
            if step.kind == "tuple_index":
                if not isinstance(current, list | tuple):
                    continue
                ordinal = int(step.value)
                if ordinal < len(current):
                    next_states.append(current[ordinal])
                continue
            if step.kind == "mapping_values" and isinstance(current, Mapping):
                next_states.extend(current.values())
        states = next_states
        if not states:
            break
    return {index: tuple(values) for index, values in cardinalities.items()}


def _validate_nested_collection_cardinality_payloads(
    *,
    input_payloads: tuple[Any, ...],
    validated_payloads: tuple[Any, ...],
    input_annotations: tuple[Any, ...] | None = None,
    validated_annotations: tuple[Any, ...] | None = None,
    compiled: _CompiledFamily,
    parent_label: str,
) -> None:
    target = compiled.version(parent_label)
    resolved_input_annotations = (
        (target.model,) * len(input_payloads) if input_annotations is None else input_annotations
    )
    resolved_validated_annotations = (
        (target.model,) * len(validated_payloads)
        if validated_annotations is None
        else validated_annotations
    )
    for nested in compiled.nested:
        target_path = _target_nested_path(target, nested.path)
        input_occurrences = tuple(
            occurrence
            for payload, annotation in zip(
                input_payloads,
                resolved_input_annotations,
                strict=True,
            )
            for occurrence in _declared_payload_occurrences_at_path(
                payload,
                annotation=annotation,
                path=target_path,
            )
        )
        validated_occurrences = tuple(
            occurrence
            for payload, annotation in zip(
                validated_payloads,
                resolved_validated_annotations,
                strict=True,
            )
            for occurrence in _declared_payload_occurrences_at_path(
                payload,
                annotation=annotation,
                path=target_path,
            )
        )
        input_values = tuple(value for value, _annotation in input_occurrences)
        validated_values = tuple(value for value, _annotation in validated_occurrences)
        collection_kind = _nested_family_collection_kind(
            model=compiled.model,
            path=nested.path,
        )
        if collection_kind in ("set", "frozenset"):
            before = sorted(
                len(value)
                for value in input_values
                if isinstance(value, list | tuple | set | frozenset)
            )
            after = sorted(
                len(value)
                for value in validated_values
                if isinstance(value, list | tuple | set | frozenset)
            )
            collapsed = len(after) < len(before) or any(
                actual < expected for expected, actual in zip(before, after, strict=False)
            )
            if collapsed:
                msg = (
                    f"Nested migration for family {nested.family.name!r} cannot "
                    "preserve set cardinality after target wire validation at path "
                    f"{nested.path!r}"
                )
                raise InvalidMigrationError(msg)

        child_compiled = nested.family._compiled_family()
        if child_compiled.nested or child_compiled.decorator_nested:
            child_label = nested.child_label(parent_label)
            child_model = _explicit_runtime_body_model(
                child_compiled.version(child_label).model,
            )
            child_input_occurrences = tuple(
                child_occurrence
                for value, annotation in input_occurrences
                for child_occurrence in _runtime_nested_structural_occurrences(
                    value,
                    annotation=annotation,
                    model=child_model,
                )
            )
            child_validated_occurrences = tuple(
                child_occurrence
                for value, annotation in validated_occurrences
                for child_occurrence in _runtime_nested_structural_occurrences(
                    value,
                    annotation=annotation,
                    model=child_model,
                )
            )
            _validate_nested_collection_cardinality_payloads(
                input_payloads=tuple(value for value, _annotation in child_input_occurrences),
                validated_payloads=tuple(
                    value for value, _annotation in child_validated_occurrences
                ),
                input_annotations=tuple(
                    annotation for _value, annotation in child_input_occurrences
                ),
                validated_annotations=tuple(
                    annotation for _value, annotation in child_validated_occurrences
                ),
                compiled=child_compiled,
                parent_label=child_label,
            )

    canonical_input = tuple(
        _to_current_names(compiled, target, payload) if isinstance(payload, Mapping) else payload
        for payload in input_payloads
    )
    canonical_validated = tuple(
        _to_current_names(compiled, target, payload) if isinstance(payload, Mapping) else payload
        for payload in validated_payloads
    )
    for route in compiled.decorator_nested:
        set_steps = tuple(
            index
            for index, step in enumerate(route.traversal)
            if step.kind == "each" and step.value in ("set", "frozenset")
        )
        before_maps = tuple(
            _decorator_set_cardinalities(payload, route) for payload in canonical_input
        )
        after_maps = tuple(
            _decorator_set_cardinalities(payload, route) for payload in canonical_validated
        )
        for step_index in set_steps:
            expected = sorted(
                value
                for cardinalities in before_maps
                for value in cardinalities.get(step_index, ())
            )
            actual = sorted(
                value for cardinalities in after_maps for value in cardinalities.get(step_index, ())
            )
            collapsed = len(actual) < len(expected) or any(
                observed < original for original, observed in zip(expected, actual, strict=False)
            )
            if collapsed:
                msg = (
                    f"Nested migration for family {route.family.name!r} cannot preserve "
                    "set cardinality after target wire validation at path "
                    f"{route.path!r}"
                )
                raise InvalidMigrationError(msg)

        child_compiled = route.family._compiled_family()
        if not child_compiled.nested and not child_compiled.decorator_nested:
            continue
        child_input = tuple(
            value
            for payload in canonical_input
            for value, _ in _walk_decorator_payload_candidates(payload, route)
        )
        child_validated = tuple(
            value
            for payload in canonical_validated
            for value, _ in _walk_decorator_payload_candidates(payload, route)
        )
        _validate_nested_collection_cardinality_payloads(
            input_payloads=child_input,
            validated_payloads=child_validated,
            compiled=child_compiled,
            parent_label=route.child_label(parent_label),
        )


def _target_nested_path(
    target: _CompiledVersion,
    path: tuple[str, ...],
) -> tuple[str, ...]:
    first = target.projection.field(path[0]).version_name
    assert first is not None
    return (first, *path[1:])


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
