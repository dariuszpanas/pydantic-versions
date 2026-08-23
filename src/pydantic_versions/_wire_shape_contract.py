from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import fields as dataclass_fields
from dataclasses import is_dataclass
from enum import Enum
from types import UnionType
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ForwardRef,
    Literal,
    TypeVar,
    Union,
    cast,
    get_args,
    get_origin,
    get_type_hints,
)

from pydantic import BaseModel, Field, GetPydanticSchema
from pydantic.fields import FieldInfo
from pydantic.functional_serializers import PlainSerializer, WrapSerializer
from typing_extensions import NoExtraItems

from pydantic_versions._compiler import _CompiledNestedFamily, _VersionProjection
from pydantic_versions._wire_contract import (
    _CUSTOM_MODEL_HOOKS,
    _MISSING,
    _SERIALIZE_AS_ANY_METADATA_TYPE,
    _TYPE_ALIAS_TYPES,
    _annotation_type_parameters,
    _bound_annotation_parameters,
    _computed_field_output_paths,
    _effective_static_class_items,
    _effective_static_class_values,
    _field_contract_paths,
    _first_defining_class,
    _has_custom_annotation_schema_hook,
    _has_schema_hook,
    _instance_dict,
    _is_no_extra_items,
    _is_typed_dict_field_qualifier,
    _model_display,
    _model_has_model_serializer,
    _pydantic_decorator_info_kind,
    _raise_projection_unsupported,
    _safe_runtime_subclass,
    _type_parameter_values,
    _typed_dict_origin,
)

if TYPE_CHECKING:
    from pydantic_versions.family import SchemaFamily

_MANAGED_FIELD_CONTRACT_ATTRIBUTES = (
    "alias",
    "exclude",
    "exclude_if",
    "validation_alias",
    "serialization_alias",
)
_HOMOGENEOUS_COLLECTION_POSITIONS = frozenset(
    {"frozenset:item", "list:item", "set:item", "tuple:item"},
)

type _DataclassManagedShape = tuple[
    Literal["dataclass"],
    type[Any],
    tuple[Any, ...],
]
type _ManagedAnnotationShape = Literal["mapping_enum", "structural"] | _DataclassManagedShape


def _managed_alternative_shape_positions(
    annotations: tuple[Any, ...],
    *,
    type_parameters: Mapping[Any, Any],
) -> dict[tuple[str, ...], list[_ManagedAnnotationShape]]:
    return _merge_managed_shape_positions(
        *(
            _managed_annotation_shape_positions(
                annotation,
                type_parameters=type_parameters,
                path=(),
                seen=set(),
            )
            for annotation in annotations
        ),
    )


def _mapping_enum_structural_ambiguity(
    shapes_by_position: Mapping[tuple[str, ...], Sequence[_ManagedAnnotationShape]],
) -> bool:
    enum_positions: set[tuple[str, ...]] = set()
    structural_positions: set[tuple[str, ...]] = set()
    for position, shapes in shapes_by_position.items():
        if "mapping_enum" in shapes:
            enum_positions.add(position)
        if "structural" in shapes:
            structural_positions.add(position)
    return any(
        _managed_shape_positions_overlap(enum_position, structural_position)
        for enum_position in enum_positions
        for structural_position in structural_positions
    )


def _dataclass_parameterization_ambiguity(
    shapes_by_position: Mapping[tuple[str, ...], Sequence[_ManagedAnnotationShape]],
) -> bool:
    parameterizations: list[tuple[tuple[str, ...], type[Any], tuple[Any, ...]]] = []
    for position, shapes in shapes_by_position.items():
        parameterizations.extend(
            (position, shape[1], shape[2])
            for shape in shapes
            if isinstance(shape, tuple) and shape[0] == "dataclass"
        )
    return any(
        left_origin is right_origin
        and left_bindings != right_bindings
        and _managed_shape_positions_overlap(left_position, right_position)
        for index, (left_position, left_origin, left_bindings) in enumerate(parameterizations)
        for right_position, right_origin, right_bindings in parameterizations[index + 1 :]
    )


def _managed_shape_positions_overlap(
    left: tuple[str, ...],
    right: tuple[str, ...],
) -> bool:
    if len(left) != len(right):
        return False
    return all(
        left_component == right_component
        or _coercible_collection_item_positions(left_component, right_component)
        for left_component, right_component in zip(left, right, strict=True)
    )


def _coercible_collection_item_positions(left: str, right: str) -> bool:
    left_fixed = left.removeprefix("tuple:").isdigit()
    right_fixed = right.removeprefix("tuple:").isdigit()
    return not (left_fixed and right_fixed) and (
        (left in _HOMOGENEOUS_COLLECTION_POSITIONS or left_fixed)
        and (right in _HOMOGENEOUS_COLLECTION_POSITIONS or right_fixed)
    )


def _managed_annotation_shape_positions(
    annotation: Any,
    *,
    type_parameters: Mapping[Any, Any],
    path: tuple[str, ...],
    seen: set[tuple[int, tuple[tuple[int, int], ...]]],
) -> dict[tuple[str, ...], list[_ManagedAnnotationShape]]:
    visit_key = (
        id(annotation),
        tuple(sorted((id(key), id(value)) for key, value in type_parameters.items())),
    )
    if visit_key in seen:
        return {}
    seen = {*seen, visit_key}

    if isinstance(annotation, _TYPE_ALIAS_TYPES):
        return _managed_annotation_shape_positions(
            annotation.__value__,
            type_parameters=type_parameters,
            path=path,
            seen=seen,
        )
    if isinstance(annotation, TypeVar):
        resolved = type_parameters.get(annotation, _MISSING)
        values = _type_parameter_values(annotation) if resolved is _MISSING else (resolved,)
        return _merge_managed_shape_positions(
            *(
                _managed_annotation_shape_positions(
                    value,
                    type_parameters=type_parameters,
                    path=path,
                    seen=seen,
                )
                for value in values
            ),
        )
    supertype = _instance_dict(annotation).get("__supertype__")
    if supertype is not None and supertype is not annotation:
        return _managed_annotation_shape_positions(
            supertype,
            type_parameters=type_parameters,
            path=path,
            seen=seen,
        )

    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if isinstance(origin, _TYPE_ALIAS_TYPES):
        return _managed_annotation_shape_positions(
            origin.__value__,
            type_parameters=_bound_annotation_parameters(
                annotation,
                inherited=type_parameters,
            ),
            path=path,
            seen=seen,
        )
    if origin is Annotated or _is_typed_dict_field_qualifier(origin):
        return _managed_annotation_shape_positions(
            arguments[0],
            type_parameters=type_parameters,
            path=path,
            seen=seen,
        )
    if origin in (Union, UnionType):
        return _merge_managed_shape_positions(
            *(
                _managed_annotation_shape_positions(
                    argument,
                    type_parameters=type_parameters,
                    path=path,
                    seen=seen,
                )
                for argument in arguments
            ),
        )
    if origin in (list, set, frozenset) and arguments:
        return _managed_annotation_shape_positions(
            arguments[0],
            type_parameters=type_parameters,
            path=(*path, f"{origin.__name__}:item"),
            seen=seen,
        )
    if origin is tuple and arguments:
        if arguments[-1] is Ellipsis:
            tuple_items = (("tuple:item", arguments[0]),)
        else:
            tuple_items = tuple(
                (f"tuple:{index}", argument) for index, argument in enumerate(arguments)
            )
        return _merge_managed_shape_positions(
            *(
                _managed_annotation_shape_positions(
                    argument,
                    type_parameters=type_parameters,
                    path=(*path, component),
                    seen=seen,
                )
                for component, argument in tuple_items
            ),
        )

    if origin is Literal:
        if any(
            isinstance(value, Enum) and isinstance(getattr(value, "_value_", None), Mapping)
            for value in arguments
        ):
            return {path: ["mapping_enum"]}
        return {}
    runtime_type = origin if isinstance(origin, type) else annotation
    if _is_mapping_valued_enum(runtime_type):
        return {path: ["mapping_enum"]}
    typed_dict_target = _typed_dict_origin(annotation)
    if typed_dict_target is not None:
        try:
            typed_dict_fields = get_type_hints(typed_dict_target, include_extras=True)
        except (AttributeError, NameError, TypeError):
            return {path: ["structural"]}
        bindings = _bound_annotation_parameters(
            annotation,
            inherited=type_parameters,
        )
        return _merge_managed_shape_positions(
            {path: ["structural"]},
            *(
                _managed_annotation_shape_positions(
                    field_annotation,
                    type_parameters=bindings,
                    path=(*path, f"typed_dict:{field_name}"),
                    seen=seen,
                )
                for field_name, field_annotation in typed_dict_fields.items()
            ),
        )
    if isinstance(runtime_type, type) and is_dataclass(runtime_type):
        bindings = _bound_annotation_parameters(
            annotation,
            inherited=type_parameters,
        )
        parameterization = tuple(
            _managed_binding_identity(
                bindings.get(parameter, parameter),
                type_parameters=bindings,
                seen=set(),
            )
            for parameter in _annotation_type_parameters(runtime_type)
        )
        return {
            path: [
                "structural",
                ("dataclass", runtime_type, parameterization),
            ],
        }
    if isinstance(runtime_type, type) and issubclass(runtime_type, BaseModel):
        return {path: ["structural"]}
    return {}


def _managed_binding_identity(
    annotation: Any,
    *,
    type_parameters: Mapping[Any, Any],
    seen: set[int],
) -> Any:
    annotation_id = id(annotation)
    if annotation_id in seen:
        return annotation
    visited = {*seen, annotation_id}

    if isinstance(annotation, TypeVar):
        resolved = type_parameters.get(annotation, _MISSING)
        if resolved is _MISSING or resolved is annotation:
            return annotation
        return _managed_binding_identity(
            resolved,
            type_parameters=type_parameters,
            seen=visited,
        )
    if isinstance(annotation, _TYPE_ALIAS_TYPES):
        return _managed_binding_identity(
            annotation.__value__,
            type_parameters=type_parameters,
            seen=visited,
        )
    supertype = _instance_dict(annotation).get("__supertype__")
    if supertype is not None and supertype is not annotation:
        return _managed_binding_identity(
            supertype,
            type_parameters=type_parameters,
            seen=visited,
        )

    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if isinstance(origin, _TYPE_ALIAS_TYPES):
        return _managed_binding_identity(
            origin.__value__,
            type_parameters=_bound_annotation_parameters(
                annotation,
                inherited=type_parameters,
            ),
            seen=visited,
        )
    if origin is Annotated or _is_typed_dict_field_qualifier(origin):
        return _managed_binding_identity(
            arguments[0],
            type_parameters=type_parameters,
            seen=visited,
        )
    if origin is None or not arguments or origin is Literal:
        return annotation
    return (
        origin,
        tuple(
            argument
            if argument is Ellipsis
            else _managed_binding_identity(
                argument,
                type_parameters=type_parameters,
                seen=visited,
            )
            for argument in arguments
        ),
    )


def _merge_managed_shape_positions(
    *sources: Mapping[tuple[str, ...], Sequence[_ManagedAnnotationShape]],
) -> dict[tuple[str, ...], list[_ManagedAnnotationShape]]:
    merged: dict[tuple[str, ...], list[_ManagedAnnotationShape]] = {}
    for source in sources:
        for position, shapes in source.items():
            selected = merged.setdefault(position, [])
            selected.extend(shape for shape in shapes if shape not in selected)
    return merged


def _is_mapping_valued_enum(annotation: Any) -> bool:
    if not isinstance(annotation, type) or not issubclass(annotation, Enum):
        return False
    return any(
        isinstance(getattr(member, "_value_", None), Mapping)
        for member in annotation.__members__.values()
    )


def _validate_explicit_nested_serializer_boundaries(
    family: SchemaFamily[Any],
    projection: _VersionProjection,
    wire_model: type[BaseModel],
    *,
    nested: tuple[_CompiledNestedFamily, ...],
) -> None:
    for route in nested:
        first = cast(str, projection.field(route.path[0]).version_name)
        target_path = (first, *route.path[1:])
        owners = [wire_model]
        for index, field_name in enumerate(target_path):
            next_owners: list[type[BaseModel]] = []
            for owner in owners:
                if owner.model_config.get("json_encoders"):
                    _raise_projection_unsupported(
                        family,
                        projection,
                        f"explicit wire model {_model_display(owner)!r} configures "
                        f"json_encoders along declared nested path {route.path!r}; "
                        "custom encoders can replace child document values",
                    )
                for hook in _CUSTOM_MODEL_HOOKS:
                    hook_owner = _first_defining_class(owner, hook)
                    if hook_owner is not None and hook_owner is not BaseModel:
                        _raise_projection_unsupported(
                            family,
                            projection,
                            f"explicit wire model {_model_display(owner)!r} uses "
                            f"custom model hook {hook} along declared nested path "
                            f"{route.path!r}; custom schemas can relocate child "
                            "document paths",
                        )
                if _model_has_model_serializer(owner):
                    _raise_projection_unsupported(
                        family,
                        projection,
                        f"explicit wire model {_model_display(owner)!r} uses a "
                        "model-level serializer along declared nested path "
                        f"{route.path!r}; serializers can relocate child document paths",
                    )
                field_info = owner.model_fields.get(field_name)
                if field_info is None:
                    _validate_omitted_explicit_managed_path_output(
                        family,
                        projection,
                        owner=owner,
                        field_name=field_name,
                        route_path=route.path,
                    )
                    continue
                if _owner_has_field_serializer(owner, field_name):
                    _raise_projection_unsupported(
                        family,
                        projection,
                        f"explicit wire model {_model_display(owner)!r} serializes field "
                        f"{field_name!r} along declared nested path {route.path!r}; "
                        "serializers can relocate child document paths",
                    )
                if _field_has_serialization_exclusion(field_info):
                    _raise_projection_unsupported(
                        family,
                        projection,
                        f"explicit wire model {_model_display(owner)!r} excludes field "
                        f"{field_name!r} along declared nested path {route.path!r}; "
                        "managed child document paths must always be serialized",
                    )
                if _field_has_functional_serializer(field_info):
                    _raise_projection_unsupported(
                        family,
                        projection,
                        f"explicit wire model {_model_display(owner)!r} uses an "
                        f"annotation-level serializer on field {field_name!r} along "
                        f"declared nested path {route.path!r}; serializers can relocate "
                        "child document paths",
                    )
                _validate_explicit_managed_path_annotation(
                    family,
                    projection,
                    owner=owner,
                    field_name=field_name,
                    annotation=field_info.annotation,
                    route_path=route.path,
                    leaf=index == len(target_path) - 1,
                    represented_family=(route.family if index == len(target_path) - 1 else None),
                    represented_label=(
                        route.child_label(projection.label)
                        if index == len(target_path) - 1
                        else None
                    ),
                )
                if index == len(target_path) - 1:
                    continue
                next_representations: list[tuple[type[Any], Mapping[Any, Any]]] = []
                _collect_immediate_base_model_representations(
                    field_info.annotation,
                    models=next_representations,
                    seen=set(),
                    type_parameters={},
                )
                next_owners.extend(model for model, _parameters in next_representations)
            owners = next_owners


def _validate_omitted_explicit_managed_path_output(
    family: SchemaFamily[Any],
    projection: _VersionProjection,
    *,
    owner: type[BaseModel],
    field_name: str,
    route_path: tuple[str, ...],
) -> None:
    for candidate_name, field_info in owner.model_fields.items():
        if any(
            path and path[0] == field_name
            for path in _field_contract_paths(candidate_name, field_info)
        ):
            _raise_projection_unsupported(
                family,
                projection,
                f"explicit wire model {_model_display(owner)!r} treats declared "
                f"nested path {route_path!r} as omitted, but field "
                f"{candidate_name!r} occupies managed component {field_name!r} "
                "through an alias",
            )
    for candidate_name, field_info in owner.model_computed_fields.items():
        if any(
            path and path[0] == field_name
            for path in _computed_field_output_paths(candidate_name, field_info)
        ):
            _raise_projection_unsupported(
                family,
                projection,
                f"explicit wire model {_model_display(owner)!r} treats declared "
                f"nested path {route_path!r} as omitted, but computed field "
                f"{candidate_name!r} serializes at managed component "
                f"{field_name!r}",
            )


def _validate_structural_representation_nested_boundaries(
    family: SchemaFamily[Any],
    label: str,
    *,
    owner: type[Any],
    type_parameters: Mapping[Any, Any],
) -> None:
    compiled = family._compiled_family()
    nested = compiled.nested
    if not nested:
        return
    projection = compiled.version(label).projection

    for route in nested:
        first = cast(str, projection.field(route.path[0]).version_name)
        target_path = (first, *route.path[1:])
        owners: list[tuple[type[Any], Mapping[Any, Any]]] = [
            (owner, type_parameters),
        ]
        for index, field_name in enumerate(target_path):
            next_owners: list[tuple[type[Any], Mapping[Any, Any]]] = []
            for structural_owner, parameters in owners:
                _validate_structural_owner_serialization(
                    family,
                    projection,
                    owner=structural_owner,
                    route_path=route.path,
                )
                fields = _structural_field_annotations(
                    family,
                    projection,
                    owner=structural_owner,
                    route_path=route.path,
                )
                field = fields.get(field_name)
                if field is None:
                    _validate_omitted_structural_managed_path_output(
                        family,
                        projection,
                        owner=structural_owner,
                        fields=fields,
                        field_name=field_name,
                        route_path=route.path,
                    )
                    continue
                annotation, field_info = field
                if _owner_has_field_serializer(structural_owner, field_name):
                    _raise_projection_unsupported(
                        family,
                        projection,
                        f"explicit wire model {_model_display(structural_owner)!r} "
                        f"serializes field {field_name!r} along declared nested path "
                        f"{route.path!r}; serializers can relocate child document paths",
                    )
                if _field_has_serialization_exclusion(field_info):
                    _raise_projection_unsupported(
                        family,
                        projection,
                        f"explicit wire model {_model_display(structural_owner)!r} "
                        f"excludes field {field_name!r} along declared nested path "
                        f"{route.path!r}; managed child document paths must always "
                        "be serialized",
                    )
                if _annotation_has_functional_serializer(
                    annotation,
                    seen=set(),
                    type_parameters=parameters,
                ) or (
                    field_info is not None
                    and any(
                        _metadata_has_unsafe_nested_serialization(item)
                        for item in getattr(field_info, "metadata", ())
                    )
                ):
                    _raise_projection_unsupported(
                        family,
                        projection,
                        f"explicit wire model {_model_display(structural_owner)!r} uses "
                        f"an annotation-level serializer on field {field_name!r} along "
                        f"declared nested path {route.path!r}; serializers can relocate "
                        "child document paths",
                    )
                leaf = index == len(target_path) - 1
                _validate_explicit_managed_path_annotation(
                    family,
                    projection,
                    owner=structural_owner,
                    field_name=field_name,
                    annotation=annotation,
                    route_path=route.path,
                    leaf=leaf,
                    represented_family=route.family if leaf else None,
                    represented_label=route.child_label(label) if leaf else None,
                    type_parameters=parameters,
                )
                if leaf:
                    continue
                _collect_immediate_base_model_representations(
                    annotation,
                    models=next_owners,
                    seen=set(),
                    type_parameters=parameters,
                )
            owners = next_owners


def _validate_structural_owner_serialization(
    family: SchemaFamily[Any],
    projection: _VersionProjection,
    *,
    owner: type[Any],
    route_path: tuple[str, ...],
) -> None:
    config = getattr(owner, "model_config", None)
    if not isinstance(config, Mapping):
        config = getattr(owner, "__pydantic_config__", None)
    if isinstance(config, Mapping) and config.get("json_encoders"):
        _raise_projection_unsupported(
            family,
            projection,
            f"explicit wire model {_model_display(owner)!r} configures json_encoders "
            f"along declared nested path {route_path!r}; custom encoders can replace "
            "child document values",
        )
    if (
        isinstance(config, Mapping)
        and config.get("alias_generator") is not None
        and not isinstance(getattr(owner, "__pydantic_fields__", None), Mapping)
    ):
        _raise_projection_unsupported(
            family,
            projection,
            f"explicit wire model {_model_display(owner)!r} configures an "
            f"unmaterialized alias_generator along declared nested path "
            f"{route_path!r}; generated aliases cannot be inspected without "
            "executing user code again",
        )


def _structural_field_annotations(
    family: SchemaFamily[Any],
    projection: _VersionProjection,
    *,
    owner: type[Any],
    route_path: tuple[str, ...],
) -> dict[str, tuple[Any, Any | None]]:
    if isinstance(owner, type) and issubclass(owner, BaseModel):
        return {
            name: (field_info.annotation, field_info)
            for name, field_info in owner.model_fields.items()
        }
    try:
        hints = get_type_hints(owner, include_extras=True)
    except (AttributeError, NameError, TypeError):
        _raise_projection_unsupported(
            family,
            projection,
            f"explicit wire model {_model_display(owner)!r} has unresolved field "
            f"annotations along declared nested path {route_path!r}",
        )
    pydantic_fields = getattr(owner, "__pydantic_fields__", None)
    stdlib_dataclass_fields = (
        {field.name: field for field in dataclass_fields(owner)} if is_dataclass(owner) else {}
    )
    return {
        name: (
            annotation,
            (
                pydantic_fields.get(name)
                if isinstance(pydantic_fields, Mapping)
                else _stdlib_dataclass_field_info(
                    stdlib_dataclass_fields.get(name),
                    annotation=annotation,
                )
            ),
        )
        for name, annotation in hints.items()
    }


def _stdlib_dataclass_field_info(field: Any, *, annotation: Any) -> Any | None:
    metadata = getattr(field, "metadata", {}) or {}
    metadata_values = {
        attribute: metadata[attribute]
        for attribute in _MANAGED_FIELD_CONTRACT_ATTRIBUTES
        if attribute in metadata
    }
    candidates = [
        _annotation_field_info(annotation),
        Field(**metadata_values) if metadata_values else None,
        None if field is None else field.default,
    ]
    return _merge_contract_field_infos(*candidates)


def _annotation_field_info(annotation: Any) -> Any | None:
    origin = get_origin(annotation)
    if _is_typed_dict_field_qualifier(origin):
        return _annotation_field_info(get_args(annotation)[0])
    if origin is not Annotated:
        return None
    return _merge_contract_field_infos(*get_args(annotation)[1:])


def _merge_contract_field_infos(*candidates: Any) -> FieldInfo | None:
    selected: dict[str, Any] = {}
    for candidate in candidates:
        if not isinstance(candidate, FieldInfo):
            continue
        explicit = getattr(candidate, "_attributes_set", {})
        for attribute in _MANAGED_FIELD_CONTRACT_ATTRIBUTES:
            if attribute in explicit:
                selected[attribute] = getattr(candidate, attribute)
    if not selected:
        return None
    return Field(**selected)


def _validate_omitted_structural_managed_path_output(
    family: SchemaFamily[Any],
    projection: _VersionProjection,
    *,
    owner: type[Any],
    fields: Mapping[str, tuple[Any, Any | None]],
    field_name: str,
    route_path: tuple[str, ...],
) -> None:
    for candidate_name, (_annotation, field_info) in fields.items():
        paths = (
            ((candidate_name,),)
            if field_info is None
            else _field_contract_paths(candidate_name, field_info)
        )
        if any(path and path[0] == field_name for path in paths):
            _raise_projection_unsupported(
                family,
                projection,
                f"explicit wire model {_model_display(owner)!r} treats declared "
                f"nested path {route_path!r} as omitted, but field "
                f"{candidate_name!r} occupies managed component {field_name!r} "
                "through an alias",
            )
    for candidate_name, field_info in _structured_computed_fields(owner).items():
        if any(
            path and path[0] == field_name
            for path in _computed_field_output_paths(candidate_name, field_info)
        ):
            _raise_projection_unsupported(
                family,
                projection,
                f"explicit wire model {_model_display(owner)!r} treats declared "
                f"nested path {route_path!r} as omitted, but computed field "
                f"{candidate_name!r} serializes at managed component "
                f"{field_name!r}",
            )


def _structured_computed_fields(owner: type[Any]) -> dict[str, Any]:
    computed = getattr(owner, "model_computed_fields", None)
    if isinstance(computed, Mapping):
        return dict(computed)
    decorators = getattr(owner, "__pydantic_decorators__", None)
    decorated = None if decorators is None else getattr(decorators, "computed_fields", None)
    if isinstance(decorated, Mapping):
        return {name: decorator.info for name, decorator in decorated.items()}
    return {
        name: _instance_dict(value)["decorator_info"]
        for name, value in _effective_static_class_items(owner)
        if _pydantic_decorator_info_kind(value) == "ComputedFieldInfo"
    }


def _validate_explicit_managed_path_annotation(
    family: SchemaFamily[Any],
    projection: _VersionProjection,
    *,
    owner: type[Any],
    field_name: str,
    annotation: Any,
    route_path: tuple[str, ...],
    leaf: bool,
    represented_family: SchemaFamily[Any] | None = None,
    represented_label: str | None = None,
    type_parameters: Mapping[Any, Any] | None = None,
    seen: set[tuple[int, bool, tuple[tuple[int, int], ...]]] | None = None,
) -> None:
    parameters = {} if type_parameters is None else type_parameters
    visit_key = (
        id(annotation),
        leaf,
        tuple(sorted((id(key), id(value)) for key, value in parameters.items())),
    )
    visited = set() if seen is None else seen
    if visit_key in visited:
        return
    visited.add(visit_key)

    def reject(detail: str) -> None:
        _raise_projection_unsupported(
            family,
            projection,
            f"explicit wire model {_model_display(owner)!r} field {field_name!r} "
            f"along declared nested path {route_path!r} {detail}",
        )

    if annotation is Any or annotation is object:
        reject(
            "uses a broad annotation; explicit managed paths require a shape-preserving annotation",
        )

    if isinstance(annotation, str | ForwardRef):
        reject("has an unresolved annotation")

    if isinstance(annotation, _TYPE_ALIAS_TYPES):
        _validate_explicit_managed_path_annotation(
            family,
            projection,
            owner=owner,
            field_name=field_name,
            annotation=annotation.__value__,
            route_path=route_path,
            leaf=leaf,
            represented_family=represented_family,
            represented_label=represented_label,
            type_parameters=parameters,
            seen=visited,
        )
        return

    if isinstance(annotation, TypeVar):
        resolved = parameters.get(annotation, _MISSING)
        if resolved is not _MISSING:
            _validate_explicit_managed_path_annotation(
                family,
                projection,
                owner=owner,
                field_name=field_name,
                annotation=resolved,
                route_path=route_path,
                leaf=leaf,
                represented_family=represented_family,
                represented_label=represented_label,
                type_parameters=parameters,
                seen=visited,
            )
            return
        values = _type_parameter_values(annotation)
        if not values:
            reject("uses an unresolved type parameter")
        alternative_shapes = _managed_alternative_shape_positions(
            values,
            type_parameters=parameters,
        )
        if _mapping_enum_structural_ambiguity(alternative_shapes):
            reject(
                "combines an object-shaped Enum scalar with a structural "
                "representation at the same traversal position; runtime "
                "validation cannot identify the authoritative branch",
            )
        if _dataclass_parameterization_ambiguity(alternative_shapes):
            reject(
                "uses multiple parameterizations of the same dataclass origin "
                "at overlapping traversal positions; runtime values cannot "
                "identify the authoritative branch",
            )
        for value in values:
            _validate_explicit_managed_path_annotation(
                family,
                projection,
                owner=owner,
                field_name=field_name,
                annotation=value,
                route_path=route_path,
                leaf=leaf,
                represented_family=represented_family,
                represented_label=represented_label,
                type_parameters=parameters,
                seen=visited,
            )
        return

    supertype = _instance_dict(annotation).get("__supertype__")
    if supertype is not None and supertype is not annotation:
        _validate_explicit_managed_path_annotation(
            family,
            projection,
            owner=owner,
            field_name=field_name,
            annotation=supertype,
            route_path=route_path,
            leaf=leaf,
            represented_family=represented_family,
            represented_label=represented_label,
            type_parameters=parameters,
            seen=visited,
        )
        return

    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if isinstance(origin, _TYPE_ALIAS_TYPES):
        bindings = _bound_annotation_parameters(
            annotation,
            inherited=parameters,
        )
        _validate_explicit_managed_path_annotation(
            family,
            projection,
            owner=owner,
            field_name=field_name,
            annotation=origin.__value__,
            route_path=route_path,
            leaf=leaf,
            represented_family=represented_family,
            represented_label=represented_label,
            type_parameters=bindings,
            seen=visited,
        )
        return

    if origin is Annotated:
        _validate_explicit_managed_path_annotation(
            family,
            projection,
            owner=owner,
            field_name=field_name,
            annotation=arguments[0],
            route_path=route_path,
            leaf=leaf,
            represented_family=represented_family,
            represented_label=represented_label,
            type_parameters=parameters,
            seen=visited,
        )
        return

    if _is_typed_dict_field_qualifier(origin):
        _validate_explicit_managed_path_annotation(
            family,
            projection,
            owner=owner,
            field_name=field_name,
            annotation=arguments[0],
            route_path=route_path,
            leaf=leaf,
            represented_family=represented_family,
            represented_label=represented_label,
            type_parameters=parameters,
            seen=visited,
        )
        return

    if origin in (Union, UnionType):
        alternative_shapes = _managed_alternative_shape_positions(
            arguments,
            type_parameters=parameters,
        )
        if _mapping_enum_structural_ambiguity(alternative_shapes):
            reject(
                "combines an object-shaped Enum scalar with a structural "
                "representation in a union at the same traversal position; "
                "runtime validation cannot identify the authoritative arm",
            )
        if _dataclass_parameterization_ambiguity(alternative_shapes):
            reject(
                "uses multiple parameterizations of the same dataclass origin "
                "at overlapping traversal positions in a union; runtime values "
                "cannot identify the authoritative arm",
            )
        for argument in arguments:
            _validate_explicit_managed_path_annotation(
                family,
                projection,
                owner=owner,
                field_name=field_name,
                annotation=argument,
                route_path=route_path,
                leaf=leaf,
                represented_family=represented_family,
                represented_label=represented_label,
                type_parameters=parameters,
                seen=visited,
            )
        return

    if origin is Literal:
        return

    if origin in (list, tuple, set, frozenset):
        if not arguments:
            if origin is tuple and getattr(annotation, "__args__", _MISSING) == ():
                return
            reject("uses an unparameterized collection")
        if origin is tuple:
            if arguments[-1] is Ellipsis:
                item_annotations = arguments[:1]
            else:
                item_annotations = arguments
        else:
            item_annotations = arguments
        for argument in item_annotations:
            _validate_explicit_managed_path_annotation(
                family,
                projection,
                owner=owner,
                field_name=field_name,
                annotation=argument,
                route_path=route_path,
                leaf=leaf,
                represented_family=represented_family,
                represented_label=represented_label,
                type_parameters=parameters,
                seen=visited,
            )
        return

    if annotation in (list, tuple, set, frozenset):
        reject("uses an unparameterized collection")

    if origin is dict or annotation is dict:
        reject("uses a mapping container")

    typed_dict_target = _typed_dict_origin(annotation)
    if typed_dict_target is not None:
        if not leaf:
            reject("uses a TypedDict at an intermediate path component")
        typed_dict_parameters = _annotation_type_parameters(typed_dict_target)
        typed_dict_bindings = _bound_annotation_parameters(
            annotation,
            inherited=parameters,
        )
        if any(parameter not in typed_dict_bindings for parameter in typed_dict_parameters):
            reject("uses an unparameterized generic TypedDict")
        try:
            get_type_hints(typed_dict_target, include_extras=True)
        except (AttributeError, NameError, TypeError):
            reject("uses a TypedDict with unresolved field annotations")
        config = getattr(typed_dict_target, "__pydantic_config__", None)
        if isinstance(config, Mapping) and config.get("extra") == "allow":
            reject(
                "uses a TypedDict with extra='allow'; managed object fields "
                "must have deterministic ownership",
            )
        extra_items = getattr(typed_dict_target, "__extra_items__", NoExtraItems)
        if not _is_no_extra_items(extra_items):
            reject(
                "uses a TypedDict with extra_items; managed object fields must "
                "have deterministic ownership",
            )
        if represented_family is not None and represented_label is not None:
            _validate_structural_representation_nested_boundaries(
                represented_family,
                represented_label,
                owner=typed_dict_target,
                type_parameters=typed_dict_bindings,
            )
        return

    structured_dataclass = is_dataclass(annotation) or (origin is not None and is_dataclass(origin))
    if structured_dataclass:
        if not leaf:
            reject("uses a dataclass at an intermediate path component")
        schema_target = annotation if origin is None else origin
        if _model_has_model_serializer(schema_target):
            reject(
                "uses a model-level serializer on a managed dataclass leaf; "
                "serializers can relocate child document paths",
            )
        if _has_custom_annotation_schema_hook(schema_target):
            reject("uses an unsupported custom annotation schema hook")
        if represented_family is not None and represented_label is not None:
            _validate_structural_representation_nested_boundaries(
                represented_family,
                represented_label,
                owner=schema_target,
                type_parameters=_bound_annotation_parameters(
                    annotation,
                    inherited=parameters,
                ),
            )
        return

    runtime_type = origin if isinstance(origin, type) else annotation
    if isinstance(runtime_type, type) and issubclass(runtime_type, BaseModel):
        if getattr(runtime_type, "__pydantic_root_model__", False):
            reject("uses a RootModel that is not object-shaped")
        if not getattr(runtime_type, "__pydantic_complete__", False):
            reject(
                "uses an incomplete model; resolve forward references and call "
                "model_rebuild() before compilation",
            )
        generic_metadata = getattr(runtime_type, "__pydantic_generic_metadata__", None)
        if isinstance(generic_metadata, Mapping) and generic_metadata.get("parameters"):
            reject("uses an unparameterized generic model")
        if _model_has_model_serializer(runtime_type):
            reject(
                "uses a model-level serializer on a managed model leaf; "
                "serializers can relocate child document paths",
            )
        if _has_custom_annotation_schema_hook(runtime_type):
            reject("uses an unsupported custom annotation schema hook")
        if represented_family is not None and represented_label is not None:
            _validate_structural_representation_nested_boundaries(
                represented_family,
                represented_label,
                owner=runtime_type,
                type_parameters=_bound_annotation_parameters(
                    annotation,
                    inherited=parameters,
                ),
            )
        return

    if origin is not None:
        if isinstance(runtime_type, type) and (
            _safe_runtime_subclass(runtime_type, Mapping)
            or _safe_runtime_subclass(runtime_type, Iterable)
        ):
            reject("uses an unsupported abstract or custom container")
        reject("uses an unsupported generic annotation")

    if not isinstance(annotation, type):
        reject("uses an unsupported opaque annotation")
    if _has_custom_annotation_schema_hook(annotation):
        reject("uses an unsupported custom annotation schema hook")
    if _safe_runtime_subclass(annotation, Mapping):
        reject("uses a mapping container")
    if annotation not in (str, bytes, bytearray) and _safe_runtime_subclass(
        annotation,
        Iterable,
    ):
        reject("uses an unsupported abstract or custom container")


def _annotation_has_functional_serializer(
    annotation: Any,
    *,
    seen: set[tuple[int, tuple[tuple[int, int], ...]]],
    type_parameters: Mapping[Any, Any] | None = None,
) -> bool:
    parameters = {} if type_parameters is None else type_parameters
    visit_key = (
        id(annotation),
        tuple(sorted((id(key), id(value)) for key, value in parameters.items())),
    )
    if visit_key in seen:
        return False
    seen.add(visit_key)

    if isinstance(annotation, _TYPE_ALIAS_TYPES):
        return _annotation_has_functional_serializer(
            annotation.__value__,
            seen=seen,
            type_parameters=parameters,
        )

    if isinstance(annotation, TypeVar):
        resolved = parameters.get(annotation, _MISSING)
        values = _type_parameter_values(annotation) if resolved is _MISSING else (resolved,)
        return any(
            value is not annotation
            and _annotation_has_functional_serializer(
                value,
                seen=seen,
                type_parameters=parameters,
            )
            for value in values
        )

    supertype = _instance_dict(annotation).get("__supertype__")
    if supertype is not None and supertype is not annotation:
        return _annotation_has_functional_serializer(
            supertype,
            seen=seen,
            type_parameters=parameters,
        )

    arguments = get_args(annotation)
    origin = get_origin(annotation)
    if isinstance(origin, _TYPE_ALIAS_TYPES):
        bindings = _bound_annotation_parameters(
            annotation,
            inherited=parameters,
        )
        return _annotation_has_functional_serializer(
            origin.__value__,
            seen=seen,
            type_parameters=bindings,
        )

    if origin is Annotated:
        base, *metadata = arguments
        if any(_metadata_has_unsafe_nested_serialization(item) for item in metadata):
            return True
        return _annotation_has_functional_serializer(
            base,
            seen=seen,
            type_parameters=parameters,
        )
    return any(
        argument is not Ellipsis
        and _annotation_has_functional_serializer(
            argument,
            seen=seen,
            type_parameters=parameters,
        )
        for argument in arguments
    )


def _field_has_functional_serializer(field_info: Any) -> bool:
    return _annotation_has_functional_serializer(
        field_info.annotation,
        seen=set(),
    ) or any(_metadata_has_unsafe_nested_serialization(item) for item in field_info.metadata)


def _field_has_serialization_exclusion(field_info: Any | None) -> bool:
    if field_info is None:
        return False
    return (
        getattr(field_info, "exclude", None) not in (None, False)
        or getattr(
            field_info,
            "exclude_if",
            None,
        )
        is not None
    )


def _metadata_has_unsafe_nested_serialization(item: Any) -> bool:
    return (
        isinstance(
            item,
            (PlainSerializer, _SERIALIZE_AS_ANY_METADATA_TYPE, WrapSerializer),
        )
        or isinstance(item, GetPydanticSchema)
        or _has_schema_hook(item)
    )


def _owner_has_field_serializer(owner: type[Any], field_name: str) -> bool:
    decorators = getattr(owner, "__pydantic_decorators__", None)
    serializers = None if decorators is None else getattr(decorators, "field_serializers", None)
    if serializers and any(
        field_name in decorator.info.fields or "*" in decorator.info.fields
        for decorator in serializers.values()
    ):
        return True
    for value in _effective_static_class_values(owner):
        if _pydantic_decorator_info_kind(value) != "FieldSerializerDecoratorInfo":
            continue
        info = _instance_dict(value)["decorator_info"]
        fields = getattr(info, "fields", ())
        if field_name in fields or "*" in fields:
            return True
    return False


def _collect_immediate_base_model_representations(
    annotation: Any,
    *,
    models: list[tuple[type[Any], Mapping[Any, Any]]],
    seen: set[tuple[int, tuple[tuple[int, int], ...]]],
    type_parameters: Mapping[Any, Any],
) -> None:
    visit_key = (
        id(annotation),
        tuple(sorted((id(key), id(value)) for key, value in type_parameters.items())),
    )
    if visit_key in seen:
        return
    seen.add(visit_key)
    if isinstance(annotation, _TYPE_ALIAS_TYPES):
        _collect_immediate_base_model_representations(
            annotation.__value__,
            models=models,
            seen=seen,
            type_parameters=type_parameters,
        )
        return
    if isinstance(annotation, TypeVar):
        resolved = type_parameters.get(annotation, _MISSING)
        values = _type_parameter_values(annotation) if resolved is _MISSING else (resolved,)
        for value in values:
            _collect_immediate_base_model_representations(
                value,
                models=models,
                seen=seen,
                type_parameters=type_parameters,
            )
        return
    supertype = _instance_dict(annotation).get("__supertype__")
    if supertype is not None and supertype is not annotation:
        _collect_immediate_base_model_representations(
            supertype,
            models=models,
            seen=seen,
            type_parameters=type_parameters,
        )
        return
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if isinstance(origin, _TYPE_ALIAS_TYPES):
        bindings = _bound_annotation_parameters(
            annotation,
            inherited=type_parameters,
        )
        _collect_immediate_base_model_representations(
            origin.__value__,
            models=models,
            seen=seen,
            type_parameters=bindings,
        )
        return
    if origin is Annotated:
        _collect_immediate_base_model_representations(
            arguments[0],
            models=models,
            seen=seen,
            type_parameters=type_parameters,
        )
        return
    runtime_type = origin if isinstance(origin, type) else annotation
    if isinstance(runtime_type, type) and issubclass(runtime_type, BaseModel):
        models.append(
            (
                runtime_type,
                _bound_annotation_parameters(
                    annotation,
                    inherited=type_parameters,
                ),
            ),
        )
        return
    if origin is Literal:
        return
    for argument in arguments:
        if argument is Ellipsis:
            continue
        _collect_immediate_base_model_representations(
            argument,
            models=models,
            seen=seen,
            type_parameters=type_parameters,
        )
