from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from functools import reduce
from operator import or_
from types import GenericAlias, UnionType
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    TypeVar,
    Union,
    cast,
    get_args,
    get_origin,
)

from pydantic import BaseModel, ConfigDict, Field, create_model
from pydantic_core import PydanticUndefined

from pydantic_versions._compiler import (
    _CompiledDecoratorNestedFamily,
    _CompiledNestedFamily,
    _DecoratorTraversalStep,
    _generated_model_name,
    _NestedCollectionKind,
    _stable_digest,
    _VersionProjection,
)
from pydantic_versions._runtime_versioning import _remove_version_field, _to_version_names
from pydantic_versions._wire_contract import (
    _CUSTOM_MODEL_HOOKS,
    _KNOWN_CONFIG_KEYS,
    _MODEL_SCHEMA_STRUCTURE_KEYS,
    _OMITTED_FIELD_ATTRIBUTES,
    _REJECTED_CONFIG_KEYS,
    _TYPE_ALIAS_TYPES,
    _WIRE_CONFIG_KEYS,
    _first_defining_class,
    _has_effect,
    _is_typed_dict,
    _model_display,
    _model_metadata_field,
    _raise_projection_unsupported,
    _raise_unsupported,
    _safe_deepcopy,
    _safe_runtime_subclass,
    _snapshot_wire_metadata,
    _type_parameter_values,
    _validate_annotation_behavior,
    _validate_type_alias,
    _wire_field_attributes,
    _wire_field_metadata,
)
from pydantic_versions.exceptions import SchemaCompilationError, UnsupportedWireModelError

if TYPE_CHECKING:
    from pydantic_versions.family import SchemaFamily


@dataclass
class _WireCompilationContext:
    hashable_models: dict[type[BaseModel], type[BaseModel]] = dataclass_field(default_factory=dict)


def _find_nested_family_for_path(
    nested: tuple[_CompiledNestedFamily, ...],
    field_path: tuple[str, ...],
) -> _CompiledNestedFamily | None:
    for family in nested:
        if family.path == field_path:
            return family
    return None


def _find_nested_families_under_path(
    nested: tuple[_CompiledNestedFamily, ...],
    field_path: tuple[str, ...],
) -> tuple[_CompiledNestedFamily, ...]:
    if not nested:
        return ()
    return tuple(
        family
        for family in nested
        if len(family.path) > len(field_path) and family.path[: len(field_path)] == field_path
    )


def _find_decorator_family_for_model(
    decorator_nested: tuple[_CompiledDecoratorNestedFamily, ...],
    field_path: tuple[str, ...],
    annotation: Any,
) -> _CompiledDecoratorNestedFamily | None:
    if not isinstance(annotation, type) or not issubclass(annotation, BaseModel):
        return None
    for route in decorator_nested:
        if route.path == field_path and route.family.model is annotation:
            return route
    return None


def _find_decorator_families_under_path(
    decorator_nested: tuple[_CompiledDecoratorNestedFamily, ...],
    field_path: tuple[str, ...],
) -> tuple[_CompiledDecoratorNestedFamily, ...]:
    return tuple(
        route
        for route in decorator_nested
        if len(route.path) > len(field_path) and route.path[: len(field_path)] == field_path
    )


def _nested_projection_cache_key(
    nested_model: type[BaseModel],
    field_path: tuple[str, ...],
    version: str,
    *,
    hash_required: bool,
) -> tuple[int, tuple[str, ...], str, bool]:
    return (id(nested_model), field_path, version, hash_required)


def _nested_model_name(
    parent: SchemaFamily[Any],
    nested_model: type[BaseModel],
    field_path: tuple[str, ...],
    version: str,
) -> str:
    components = (
        parent.model.__module__,
        parent.model.__qualname__,
        parent.name,
        version,
        "nested",
        nested_model.__qualname__,
        *field_path,
    )
    suffix = _stable_digest(components)[:10]
    return f"{_generated_model_name(parent.model, parent.name, version)}_Nested_{suffix}"


def _nested_wire_model_config(
    family: SchemaFamily[Any],
    model: type[BaseModel],
) -> ConfigDict:
    config: dict[str, Any] = {
        key: value for key, value in model.model_config.items() if key in _WIRE_CONFIG_KEYS
    }
    schema_extra = model.model_config.get("json_schema_extra")
    if isinstance(schema_extra, Mapping):
        structural_keys = sorted(_MODEL_SCHEMA_STRUCTURE_KEYS & set(schema_extra))
        if structural_keys:
            _raise_unsupported(
                family,
                f"nested wrapper {_model_display(model)!r} JSON Schema metadata cannot "
                "override generated structure: "
                f"{', '.join(structural_keys)}",
            )
        config["json_schema_extra"] = _safe_deepcopy(
            family,
            dict(schema_extra),
            detail=f"JSON Schema metadata for nested wrapper {_model_display(model)!r}",
        )
    return ConfigDict(**config)


def _validate_nested_projection_coverage(
    family: SchemaFamily[Any],
    projection: _VersionProjection,
    nested: tuple[_CompiledNestedFamily, ...],
    used_nested: set[tuple[str, ...]],
) -> None:
    unused = tuple(family_path for family_path in nested if family_path.path not in used_nested)
    if unused:
        first = unused[0].path
        if len(unused) == 1:
            msg = f"nested declaration path {first!r} does not match any rewritable field path"
        else:
            msg = f"{len(unused)} nested declarations do not match any rewritable field path"
        _raise_projection_unsupported(family, projection, msg)


def _compile_decorator_nested_families(
    owner: SchemaFamily[Any],
    explicit: tuple[_CompiledNestedFamily, ...],
) -> tuple[_CompiledDecoratorNestedFamily, ...]:
    """Discover the compatibility decorator's implicit child-family boundaries."""
    if not owner._decorator_created:
        return ()

    explicit_paths = frozenset(declaration.path for declaration in explicit)
    discovered: list[_CompiledDecoratorNestedFamily] = []

    def visit(
        annotation: Any,
        *,
        field_path: tuple[str, ...],
        traversal: tuple[_DecoratorTraversalStep, ...],
        collection_kind: _NestedCollectionKind | None,
        model_stack: tuple[type[BaseModel], ...],
    ) -> None:
        if field_path in explicit_paths:
            return
        if isinstance(annotation, _TYPE_ALIAS_TYPES):
            if _annotation_has_decorator_child(owner, annotation):
                _raise_unsupported(
                    owner,
                    f"decorator child at path {field_path!r} is hidden in a type alias",
                )
            return
        if isinstance(annotation, TypeVar):
            if _annotation_has_decorator_child(owner, annotation):
                _raise_unsupported(
                    owner,
                    f"decorator child at path {field_path!r} is hidden behind an "
                    "unresolved type parameter",
                )
            return
        if _is_typed_dict(annotation):
            if _annotation_has_decorator_child(owner, annotation):
                _raise_unsupported(
                    owner,
                    f"decorator child at path {field_path!r} uses a TypedDict boundary",
                )
            return

        origin = get_origin(annotation)
        if origin is Annotated:
            arguments = get_args(annotation)
            visit(
                arguments[0],
                field_path=field_path,
                traversal=traversal,
                collection_kind=collection_kind,
                model_stack=model_stack,
            )
            return

        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            hidden = next(
                (
                    path
                    for path in explicit_paths
                    if len(path) > len(field_path) and path[: len(field_path)] == field_path
                ),
                None,
            )
            if hidden is not None:
                from pydantic_versions.family import _default_family_for_model

                registered = _default_family_for_model(annotation)
                if registered is not None and registered._decorator_created:
                    _raise_unsupported(
                        owner,
                        f"explicit NestedFamily path {hidden!r} descends beneath decorator "
                        f"child boundary {field_path!r}",
                    )
            child = _compat_child_family(owner, annotation)
            if child is not None:
                discovered.append(
                    _CompiledDecoratorNestedFamily(
                        path=field_path,
                        family=child,
                        traversal=traversal,
                        collection_kind=collection_kind,
                    )
                )
                return
            has_decorator_child = _annotation_has_decorator_child(owner, annotation)
            # An incomplete wrapper can conceal a decorator child behind an
            # unresolved ForwardRef, so it must fail closed even before route
            # discovery can prove that a projection is required.
            if has_decorator_child or not getattr(annotation, "__pydantic_complete__", False):
                _validate_ordinary_wrapper_model(owner, annotation, field_path=field_path)
            if annotation in model_stack:
                if has_decorator_child:
                    _raise_unsupported(
                        owner,
                        f"decorator child beneath recursive model path {field_path!r} "
                        "cannot be projected safely",
                    )
                return
            for nested_name, field_info in annotation.model_fields.items():
                if not _decorator_field_crosses_wire_boundary(field_info):
                    continue
                visit(
                    field_info.annotation,
                    field_path=(*field_path, nested_name),
                    traversal=(
                        *traversal,
                        _DecoratorTraversalStep("field", nested_name),
                    ),
                    collection_kind=collection_kind,
                    model_stack=(*model_stack, annotation),
                )
            return

        arguments = get_args(annotation)
        if origin in (Union, UnionType):
            branch_shapes: list[tuple[tuple[tuple[str, str], ...], ...]] = []
            branch_routes: list[tuple[_CompiledDecoratorNestedFamily, ...]] = []
            branch_arguments: list[Any] = []
            for ordinal, argument in enumerate(arguments):
                if argument is type(None):
                    continue
                branch_arguments.append(argument)
                start = len(discovered)
                visit(
                    argument,
                    field_path=field_path,
                    traversal=(
                        *traversal,
                        _DecoratorTraversalStep("union_arm", str(ordinal)),
                    ),
                    collection_kind=collection_kind,
                    model_stack=model_stack,
                )
                branch_routes.append(tuple(discovered[start:]))
                branch_shapes.append(
                    tuple(
                        sorted(
                            tuple(
                                (step.kind, "*")
                                if step.kind == "union_arm"
                                else (step.kind, step.value)
                                for step in route.traversal[len(traversal) + 1 :]
                            )
                            for route in discovered[start:]
                        )
                    )
                )
            populated = tuple(shape for shape in branch_shapes if shape)
            if len(populated) > 1 and any(shape != populated[0] for shape in populated[1:]):
                _raise_unsupported(
                    owner,
                    f"decorator children beneath union path {field_path!r} use "
                    "non-isomorphic traversal shapes",
                )
            union_routes = tuple(route for routes in branch_routes for route in routes)
            if union_routes:
                if any(_is_typed_dict(argument) for argument in branch_arguments):
                    _raise_unsupported(
                        owner,
                        f"decorator children beneath union path {field_path!r} use a "
                        "runtime-unrecoverable TypedDict arm",
                    )
                runtime_container_arms: list[tuple[type[Any] | None, bool]] = []
                for argument, routes in zip(branch_arguments, branch_routes, strict=True):
                    normalized = argument
                    while get_origin(normalized) is Annotated:
                        normalized = get_args(normalized)[0]
                    runtime_origin = get_origin(normalized)
                    runtime_container = (
                        runtime_origin
                        if isinstance(runtime_origin, type)
                        else normalized
                        if isinstance(normalized, type) and normalized is not Any
                        else None
                    )
                    runtime_container_arms.append((runtime_container, bool(routes)))
                for left_index, (left, left_has_routes) in enumerate(runtime_container_arms):
                    if left is None:
                        continue
                    for right, right_has_routes in runtime_container_arms[left_index + 1 :]:
                        if right is None or not (left_has_routes or right_has_routes):
                            continue
                        if not (
                            _safe_runtime_subclass(left, right)
                            or _safe_runtime_subclass(right, left)
                        ):
                            continue
                        _raise_unsupported(
                            owner,
                            f"decorator children beneath union path {field_path!r} use "
                            "runtime-indistinguishable container arms",
                        )
                routes_by_site: dict[
                    tuple[tuple[str, str], ...],
                    list[_CompiledDecoratorNestedFamily],
                ] = {}
                for route in union_routes:
                    site = tuple(
                        (step.kind, "*") if step.kind == "union_arm" else (step.kind, step.value)
                        for step in route.traversal
                    )
                    routes_by_site.setdefault(site, []).append(route)
                for routes in routes_by_site.values():
                    first_contract = _decorator_metadata_contract(routes[0].family)
                    if any(
                        _decorator_metadata_contract(route.family) != first_contract
                        for route in routes[1:]
                    ):
                        _raise_unsupported(
                            owner,
                            f"decorator children beneath union path {field_path!r} use "
                            "incompatible version-metadata contracts",
                        )
            return
        if origin in (list, set, frozenset):
            if len(arguments) != 1:
                return
            kind = cast(_NestedCollectionKind, origin.__name__)
            visit(
                arguments[0],
                field_path=field_path,
                traversal=(*traversal, _DecoratorTraversalStep("each", kind)),
                collection_kind=kind,
                model_stack=model_stack,
            )
            return
        if origin is tuple:
            if len(arguments) == 2 and arguments[1] is Ellipsis:
                visit(
                    arguments[0],
                    field_path=field_path,
                    traversal=(
                        *traversal,
                        _DecoratorTraversalStep("each", "tuple"),
                    ),
                    collection_kind="tuple",
                    model_stack=model_stack,
                )
                return
            for ordinal, argument in enumerate(arguments):
                visit(
                    argument,
                    field_path=field_path,
                    traversal=(
                        *traversal,
                        _DecoratorTraversalStep("tuple_index", str(ordinal)),
                    ),
                    collection_kind="tuple",
                    model_stack=model_stack,
                )
            return
        if origin is dict:
            if len(arguments) != 2:
                return
            key_annotation, value_annotation = arguments
            if _annotation_has_decorator_child(owner, key_annotation):
                _raise_unsupported(
                    owner,
                    f"decorator child at path {field_path!r} cannot be used as a mapping key",
                )
            if (
                _annotation_has_decorator_child(owner, value_annotation)
                and key_annotation is not str
            ):
                _raise_unsupported(
                    owner,
                    f"decorator child at path {field_path!r} requires exact string mapping keys",
                )
            visit(
                value_annotation,
                field_path=field_path,
                traversal=(
                    *traversal,
                    _DecoratorTraversalStep("mapping_values", "dict"),
                ),
                collection_kind="mapping",
                model_stack=model_stack,
            )
            return
        if (
            origin is not None
            and isinstance(origin, type)
            and (
                issubclass(origin, Mapping)
                or issubclass(origin, Sequence)
                or issubclass(origin, Iterable)
            )
        ):
            if _annotation_has_decorator_child(owner, annotation):
                _raise_unsupported(
                    owner,
                    f"decorator child at path {field_path!r} uses unsupported abstract "
                    f"container {origin.__module__}.{origin.__qualname__}",
                )
            return
        if arguments and _annotation_has_decorator_child(owner, annotation):
            _raise_unsupported(
                owner,
                f"decorator child at path {field_path!r} uses an unsupported generic wrapper",
            )

    for field_name, field_info in owner.model.model_fields.items():
        if not _decorator_field_crosses_wire_boundary(field_info):
            continue
        visit(
            field_info.annotation,
            field_path=(field_name,),
            traversal=(_DecoratorTraversalStep("field", field_name),),
            collection_kind=None,
            model_stack=(owner.model,),
        )
    return tuple(discovered)


def _decorator_field_crosses_wire_boundary(field_info: Any) -> bool:
    attributes = field_info.asdict()["attributes"]
    return not any(
        key in attributes and _has_effect(attributes[key]) for key in _OMITTED_FIELD_ATTRIBUTES
    )


def _decorator_metadata_contract(family: SchemaFamily[Any]) -> tuple[Any, ...]:
    metadata = family.version_metadata
    if metadata is None or metadata.owner == "family":
        return (metadata,)
    if not isinstance(metadata.path, str):  # pragma: no cover - declaration invariant
        return (metadata,)
    field_info = family.model.model_fields.get(metadata.path)
    if field_info is None:  # pragma: no cover - inferred owner invariant
        return (metadata,)
    return (
        metadata,
        field_info.alias,
        field_info.validation_alias,
        field_info.serialization_alias,
    )


def _annotation_has_decorator_child(
    owner: SchemaFamily[Any],
    annotation: Any,
    *,
    seen: set[int] | None = None,
) -> bool:
    visited = set() if seen is None else seen
    if id(annotation) in visited:
        return False
    visited.add(id(annotation))
    if isinstance(annotation, _TYPE_ALIAS_TYPES):
        return _annotation_has_decorator_child(owner, annotation.__value__, seen=visited)
    if isinstance(annotation, TypeVar):
        return any(
            _annotation_has_decorator_child(owner, value, seen=visited)
            for value in _type_parameter_values(annotation)
        )
    if _is_typed_dict(annotation):
        return any(
            _annotation_has_decorator_child(owner, value, seen=visited)
            for value in annotation.__annotations__.values()
        )
    origin = get_origin(annotation)
    if origin is Annotated:
        arguments = get_args(annotation)
        return bool(arguments) and _annotation_has_decorator_child(
            owner,
            arguments[0],
            seen=visited,
        )
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if _compat_child_family(owner, annotation) is not None:
            return True
        return any(
            _annotation_has_decorator_child(owner, field_info.annotation, seen=visited)
            for field_info in annotation.model_fields.values()
        )
    return any(
        _annotation_has_decorator_child(owner, argument, seen=visited)
        for argument in get_args(annotation)
    )


def _validate_ordinary_wrapper_model(
    owner: SchemaFamily[Any],
    model: type[BaseModel],
    *,
    field_path: tuple[str, ...],
) -> None:
    context = f"ordinary wrapper {_model_display(model)!r} at path {field_path!r}"
    if getattr(model, "__pydantic_root_model__", False):
        _raise_unsupported(owner, f"{context} is a RootModel and is not object-shaped")
    if not getattr(model, "__pydantic_complete__", False):
        _raise_unsupported(
            owner,
            f"{context} is incomplete; resolve forward references and call "
            "model_rebuild() before compilation",
        )
    generic_metadata = getattr(model, "__pydantic_generic_metadata__", None)
    if isinstance(generic_metadata, Mapping) and generic_metadata.get("parameters"):
        _raise_unsupported(owner, f"{context} has unresolved generic parameters")
    decorators = getattr(model, "__pydantic_decorators__", None)
    if decorators is not None and getattr(decorators, "model_serializers", None):
        _raise_unsupported(owner, f"{context} uses a model-level serializer")
    for hook in _CUSTOM_MODEL_HOOKS:
        hook_owner = _first_defining_class(model, hook)
        if hook_owner is not None and hook_owner is not BaseModel:
            _raise_unsupported(owner, f"{context} uses custom model hook {hook}")

    config: Mapping[str, Any] = model.model_config
    unknown = sorted(set(config) - _KNOWN_CONFIG_KEYS)
    if unknown:
        _raise_unsupported(
            owner,
            f"{context} sets unsupported model configuration: {', '.join(unknown)}",
        )
    for key, value in config.items():
        if key in _REJECTED_CONFIG_KEYS and _has_effect(value):
            _raise_unsupported(owner, f"{context} uses model configuration {key!r}")
    schema_extra = config.get("json_schema_extra")
    if schema_extra is not None and not isinstance(schema_extra, Mapping):
        _raise_unsupported(owner, f"{context} uses callable model JSON Schema mutation")

    if config.get("extra") == "allow":
        for base in model.__mro__:
            if base is BaseModel:
                continue
            if "__pydantic_extra__" in base.__dict__.get("__annotations__", {}):
                _raise_unsupported(owner, f"{context} declares typed extra values")


def _compat_child_family(
    owner: SchemaFamily[Any],
    annotation: type[BaseModel],
) -> SchemaFamily[Any] | None:
    from pydantic_versions.family import _default_family_for_model

    child = _default_family_for_model(annotation)
    if child is None or not child._decorator_created:
        return None
    owner_labels = tuple(version.label for version in owner.versions)
    child_labels = tuple(version.label for version in child.versions)
    if child_labels != owner_labels:
        msg = (
            f"Decorator child family {child.name!r} must use the exact labels of "
            f"parent {owner.name!r}; declare an explicit nested mapping instead"
        )
        raise SchemaCompilationError(msg)
    return child


def _set_element_wire_model(
    model: type[BaseModel],
    *,
    compilation: _WireCompilationContext,
) -> type[BaseModel]:
    if model.model_config.get("frozen"):
        return model
    cached = compilation.hashable_models.get(model)
    if cached is not None:
        return cached
    frozen_model = create_model(
        f"{model.__name__}__HashableSetElement",
        __base__=model,
        __module__=model.__module__,
        __config__=ConfigDict(frozen=True),
    )
    frozen_model.model_rebuild(force=True)
    compilation.hashable_models[model] = frozen_model
    return frozen_model


def _rewrite_annotation(
    annotation: Any,
    version: str,
    family: SchemaFamily[Any],
    *,
    compilation: _WireCompilationContext,
    nested: tuple[_CompiledNestedFamily, ...],
    decorator_nested: tuple[_CompiledDecoratorNestedFamily, ...],
    field_path: tuple[str, ...],
    used_nested: set[tuple[str, ...]],
    field_name: str,
    allow_child_projection: bool,
    nested_projection_cache: dict[tuple[int, tuple[str, ...], str, bool], type[BaseModel]],
    in_set_element: bool = False,
) -> Any:
    child = _find_nested_family_for_path(nested, field_path)
    if (
        child is not None
        and isinstance(annotation, type)
        and issubclass(
            annotation,
            BaseModel,
        )
    ):
        used_nested.add(child.path)
        family_model = child.family.model_for(child.child_label(version))
        return (
            _set_element_wire_model(family_model, compilation=compilation)
            if in_set_element
            else family_model
        )
    decorator_child = (
        _find_decorator_family_for_model(decorator_nested, field_path, annotation)
        if allow_child_projection
        else None
    )
    if decorator_child is not None:
        family_model = decorator_child.family.model_for(version)
        return (
            _set_element_wire_model(family_model, compilation=compilation)
            if in_set_element
            else family_model
        )

    if isinstance(annotation, _TYPE_ALIAS_TYPES):
        _validate_type_alias(family, field_name, annotation)
        return annotation
    _validate_annotation_behavior(family, field_name, annotation)
    if (
        allow_child_projection
        and isinstance(annotation, type)
        and issubclass(annotation, BaseModel)
    ):
        nested_families = _find_nested_families_under_path(nested, field_path)
        decorator_families = _find_decorator_families_under_path(
            decorator_nested,
            field_path,
        )
        if nested_families or decorator_families:
            return _rewrite_nested_model(
                annotation,
                version,
                family,
                compilation=compilation,
                field_path=field_path,
                nested=nested_families,
                decorator_nested=decorator_families,
                in_set_element=in_set_element,
                nested_projection_cache=nested_projection_cache,
                used_nested=used_nested,
            )

    origin = get_origin(annotation)
    if isinstance(origin, _TYPE_ALIAS_TYPES):
        _validate_type_alias(family, field_name, origin)
    if origin is not None:
        _validate_annotation_behavior(family, field_name, origin)
    if origin is Annotated:
        base, *source_metadata = get_args(annotation)
        metadata = _snapshot_wire_metadata(
            family,
            field_name,
            source_metadata,
            detail="nested annotation metadata",
        )
        rewritten = _rewrite_annotation(
            base,
            version,
            family,
            compilation=compilation,
            nested=nested,
            decorator_nested=decorator_nested,
            field_path=field_path,
            used_nested=used_nested,
            field_name=field_name,
            allow_child_projection=allow_child_projection,
            nested_projection_cache=nested_projection_cache,
            in_set_element=in_set_element,
        )
        if not metadata:
            return rewritten
        return Annotated[rewritten, *metadata]

    source_args = get_args(annotation)
    if not source_args:
        return annotation
    legacy_container = origin in (
        list,
        tuple,
        set,
        frozenset,
        dict,
        Union,
        UnionType,
    )
    set_context = in_set_element or origin in (set, frozenset)
    args = tuple(
        _rewrite_annotation(
            arg,
            version,
            family,
            compilation=compilation,
            field_name=field_name,
            field_path=field_path,
            allow_child_projection=allow_child_projection and legacy_container,
            nested=nested,
            decorator_nested=decorator_nested,
            used_nested=used_nested,
            nested_projection_cache=nested_projection_cache,
            in_set_element=set_context,
        )
        for arg in source_args
    )
    if all(rewritten is source for rewritten, source in zip(args, source_args, strict=True)):
        return annotation
    if origin in (Union, UnionType):
        return reduce(or_, args)
    if isinstance(annotation, GenericAlias):
        return GenericAlias(origin, args)
    # Changed arguments only arise from supported ``typing`` aliases here;
    # builtin generics and unions are handled above.
    return annotation.copy_with(args)


def _rewrite_nested_model(
    annotation: type[BaseModel],
    version: str,
    owner: SchemaFamily[Any],
    *,
    compilation: _WireCompilationContext,
    field_path: tuple[str, ...],
    nested: tuple[_CompiledNestedFamily, ...],
    decorator_nested: tuple[_CompiledDecoratorNestedFamily, ...],
    nested_projection_cache: dict[tuple[int, tuple[str, ...], str, bool], type[BaseModel]],
    used_nested: set[tuple[str, ...]],
    in_set_element: bool = False,
) -> type[BaseModel]:
    cache_key = _nested_projection_cache_key(
        annotation,
        field_path,
        version,
        hash_required=in_set_element,
    )
    nested_projection = nested_projection_cache.get(cache_key)
    if nested_projection is not None:
        return nested_projection

    placeholder = create_model(
        _nested_model_name(owner, annotation, field_path, version),
        __module__=annotation.__module__,
    )
    nested_projection_cache[cache_key] = placeholder
    try:
        fields: dict[str, Any] = {}
        for source_name, source_field_info in annotation.model_fields.items():
            source = source_field_info.asdict()
            excluded = any(
                key in source["attributes"] and _has_effect(source["attributes"][key])
                for key in _OMITTED_FIELD_ATTRIBUTES
            )
            if excluded:
                continue
            attributes = _wire_field_attributes(owner, source_name, source["attributes"])
            metadata = _wire_field_metadata(owner, source_name, source["metadata"])
            rewritten_annotation = _rewrite_annotation(
                source["annotation"],
                version,
                owner,
                compilation=compilation,
                nested=nested,
                decorator_nested=decorator_nested,
                field_path=field_path + (source_name,),
                used_nested=used_nested,
                field_name=source_name,
                allow_child_projection=True,
                in_set_element=in_set_element,
                nested_projection_cache=nested_projection_cache,
            )
            _rewrite_nested_default(
                attributes,
                source["annotation"],
                rewritten_annotation,
                owner,
                nested=nested,
                decorator_nested=decorator_nested,
                field_path=field_path + (source_name,),
                used_nested=used_nested,
                field_name=source_name,
                version=version,
            )
            fields[source_name] = Annotated[
                rewritten_annotation,
                *metadata,
                Field(**attributes),
            ]
        nested_projection = create_model(
            _nested_model_name(owner, annotation, field_path, version),
            __module__=annotation.__module__,
            __config__=_nested_wire_model_config(owner, annotation),
            **fields,
        )
        nested_projection.model_rebuild(force=True)
        if in_set_element:
            nested_projection = _set_element_wire_model(
                nested_projection,
                compilation=compilation,
            )
    except UnsupportedWireModelError:
        raise
    except Exception as exc:
        msg = (
            f"Automatic wire model for family {owner.name!r}, version {version!r}, and model "
            f"{_model_display(annotation)!r} cannot safely project nested model for path "
            f"{field_path!r}"
        )
        raise UnsupportedWireModelError(msg) from exc
    nested_projection_cache[cache_key] = nested_projection
    return nested_projection


def _rewrite_nested_default(
    attributes: dict[str, Any],
    original_annotation: Any,
    version_annotation: Any,
    family: SchemaFamily[Any],
    *,
    nested: tuple[_CompiledNestedFamily, ...],
    decorator_nested: tuple[_CompiledDecoratorNestedFamily, ...],
    field_path: tuple[str, ...],
    used_nested: set[tuple[str, ...]],
    field_name: str,
    version: str,
) -> None:
    if original_annotation == version_annotation:
        return
    original_model = _default_model_annotation(original_annotation)
    target_model = _default_model_annotation(version_annotation)
    default_factory = attributes.get("default_factory")
    original_base = original_annotation
    original_origin = get_origin(original_base)
    if (
        original_model is not None
        and target_model is not None
        and default_factory is original_model
    ):
        attributes["default_factory"] = target_model
    elif callable(default_factory) and not (
        original_origin in (list, tuple, set, frozenset, dict)
        and default_factory is original_origin
    ):
        _raise_unsupported(
            family,
            f"field {field_name!r} uses an opaque factory for a projected nested value",
        )
    default = attributes.get("default", PydanticUndefined)
    if default is PydanticUndefined or default is None:
        return
    attributes["default"] = _project_nested_default_member(
        default,
        original_annotation=original_annotation,
        version_annotation=version_annotation,
        owner=family,
        nested=nested,
        decorator_nested=decorator_nested,
        field_path=field_path,
        used_nested=used_nested,
        version=version,
    )


def _default_model_annotation(annotation: Any) -> type[BaseModel] | None:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    return None


def _project_nested_model_default_value(
    value: Any,
    *,
    source_model: type[BaseModel],
    target_model: type[BaseModel],
    owner: SchemaFamily[Any],
    nested: tuple[_CompiledNestedFamily, ...],
    decorator_nested: tuple[_CompiledDecoratorNestedFamily, ...],
    field_path: tuple[str, ...],
    used_nested: set[tuple[str, ...]],
    version: str,
) -> BaseModel:
    if not isinstance(value, source_model):
        _raise_unsupported(owner, "a projected wrapper default changed type unexpectedly")

    projected: dict[str, Any] = {}
    for name, source_field in source_model.model_fields.items():
        if name not in value.__dict__:
            continue
        target_field = target_model.model_fields.get(name)
        if target_field is None:
            continue
        projected[name] = _project_nested_default_member(
            value.__dict__[name],
            original_annotation=source_field.annotation,
            version_annotation=target_field.annotation,
            owner=owner,
            nested=nested,
            decorator_nested=decorator_nested,
            field_path=(*field_path, name),
            used_nested=used_nested,
            version=version,
        )

    factory_fields = sorted(
        name
        for name, field_info in target_model.model_fields.items()
        if name not in projected and field_info.default_factory is not None
    )
    if factory_fields:
        _raise_unsupported(
            owner,
            "a projected wrapper default would execute default factories during "
            f"compilation: {', '.join(factory_fields)}",
        )
    target_fields = set(target_model.model_fields)
    fields_set = {name for name in value.model_fields_set if name in target_fields}
    return target_model.model_construct(_fields_set=fields_set, **projected)


def _project_nested_default_member(
    value: Any,
    *,
    original_annotation: Any,
    version_annotation: Any,
    owner: SchemaFamily[Any],
    nested: tuple[_CompiledNestedFamily, ...],
    decorator_nested: tuple[_CompiledDecoratorNestedFamily, ...],
    field_path: tuple[str, ...],
    used_nested: set[tuple[str, ...]],
    version: str,
) -> Any:
    original = original_annotation
    while get_origin(original) is Annotated:
        original = get_args(original)[0]
    target = version_annotation
    while get_origin(target) is Annotated:
        target = get_args(target)[0]
    if value is None:
        return None

    source_model = _default_model_annotation(original)
    target_model = _default_model_annotation(target)
    declaration = _find_nested_family_for_path(nested, field_path)
    if declaration is not None and source_model is not None:
        used_nested.add(declaration.path)
        target_label = declaration.child_label(version)
        return _project_child_default_value(
            value,
            source_model=source_model,
            target_model=(
                target_model
                if target_model is not None
                else declaration.family.model_for(target_label)
            ),
            child=declaration.family,
            owner=owner,
            version=target_label,
        )
    decorator_child = _find_decorator_family_for_model(
        decorator_nested,
        field_path,
        source_model,
    )
    if decorator_child is not None and source_model is not None:
        target_label = decorator_child.child_label(version)
        return _project_child_default_value(
            value,
            source_model=source_model,
            target_model=(
                target_model
                if target_model is not None
                else decorator_child.family.model_for(target_label)
            ),
            child=decorator_child.family,
            owner=owner,
            version=target_label,
        )
    if source_model is not None and target_model is not None and source_model is not target_model:
        return _project_nested_model_default_value(
            value,
            source_model=source_model,
            target_model=target_model,
            owner=owner,
            nested=nested,
            decorator_nested=decorator_nested,
            field_path=field_path,
            used_nested=used_nested,
            version=version,
        )

    original_origin = get_origin(original)
    target_origin = get_origin(target)
    if original_origin in (Union, UnionType) and target_origin in (Union, UnionType):
        original_arguments = get_args(original)
        target_arguments = get_args(target)
        selected = _default_union_ordinal(value, original_arguments)
        if selected is None or selected >= len(target_arguments):
            _raise_unsupported(owner, "a projected nested default has an ambiguous union arm")
        assert selected is not None
        return _project_nested_default_member(
            value,
            original_annotation=original_arguments[selected],
            version_annotation=target_arguments[selected],
            owner=owner,
            nested=nested,
            decorator_nested=decorator_nested,
            field_path=field_path,
            used_nested=used_nested,
            version=version,
        )
    if original_origin in (list, set, frozenset) and target_origin is original_origin:
        source_item = get_args(original)[0]
        target_item = get_args(target)[0]
        items = [
            _project_nested_default_member(
                item,
                original_annotation=source_item,
                version_annotation=target_item,
                owner=owner,
                nested=nested,
                decorator_nested=decorator_nested,
                field_path=field_path,
                used_nested=used_nested,
                version=version,
            )
            for item in value
        ]
        return original_origin(items)
    if original_origin is tuple and target_origin is tuple:
        source_arguments = get_args(original)
        target_arguments = get_args(target)
        if len(source_arguments) == 2 and source_arguments[1] is Ellipsis:
            source_arguments = (source_arguments[0],) * len(value)
            target_arguments = (target_arguments[0],) * len(value)
        return tuple(
            _project_nested_default_member(
                item,
                original_annotation=source_item,
                version_annotation=target_item,
                owner=owner,
                nested=nested,
                decorator_nested=decorator_nested,
                field_path=field_path,
                used_nested=used_nested,
                version=version,
            )
            for item, source_item, target_item in zip(
                value,
                source_arguments,
                target_arguments,
                strict=True,
            )
        )
    if original_origin is dict and target_origin is dict:
        source_arguments = get_args(original)
        target_arguments = get_args(target)
        return {
            key: _project_nested_default_member(
                item,
                original_annotation=source_arguments[1],
                version_annotation=target_arguments[1],
                owner=owner,
                nested=nested,
                decorator_nested=decorator_nested,
                field_path=field_path,
                used_nested=used_nested,
                version=version,
            )
            for key, item in value.items()
        }
    return value


def _default_union_ordinal(value: Any, arguments: tuple[Any, ...]) -> int | None:
    for ordinal, argument in enumerate(arguments):
        candidate = argument
        while get_origin(candidate) is Annotated:
            candidate = get_args(candidate)[0]
        if isinstance(candidate, type) and type(value) is candidate:
            return ordinal
    matches: list[int] = []
    for ordinal, argument in enumerate(arguments):
        candidate = argument
        while get_origin(candidate) is Annotated:
            candidate = get_args(candidate)[0]
        origin = get_origin(candidate)
        runtime_type = origin if isinstance(origin, type) else candidate
        if isinstance(runtime_type, type) and isinstance(value, runtime_type):
            matches.append(ordinal)
    return matches[0] if len(matches) == 1 else None


def _project_child_default_value(
    value: Any,
    *,
    source_model: type[BaseModel],
    target_model: type[BaseModel],
    child: SchemaFamily[Any],
    owner: SchemaFamily[Any],
    version: str,
) -> BaseModel:
    if not isinstance(value, source_model):  # pragma: no cover - narrowed by the caller
        _raise_unsupported(owner, "a projected nested default changed type unexpectedly")
    fields_set = value.model_fields_set
    payload = {
        name: value.__dict__[name]
        for name in fields_set
        if name in source_model.model_fields and name in value.__dict__
    }

    target_version = child._compiled_family().version(version)
    projected = _to_version_names(target_version, payload)
    projected = {
        name: item for name, item in projected.items() if name in target_model.model_fields
    }
    safe_factory_fields: set[str] = set()
    metadata = child.version_metadata
    if metadata is not None and metadata.owner == "model":
        metadata_field = _model_metadata_field(child)
        if metadata_field is not None:  # pragma: no branch - owner='model' resolves a field
            projected[metadata_field] = version
    elif metadata is not None:
        _remove_version_field(projected, metadata.path)
        if not isinstance(metadata.path, str) and len(metadata.path) > 1:
            metadata_root = metadata.path[0]
            metadata_field_info = target_model.model_fields[metadata_root]
            factory = metadata_field_info.default_factory
            if (
                isinstance(factory, type)
                and issubclass(factory, BaseModel)
                and factory is metadata_field_info.annotation
            ):
                safe_factory_fields.add(metadata_root)
    factory_fields = sorted(
        name
        for name, field_info in target_model.model_fields.items()
        if name not in projected
        and name not in safe_factory_fields
        and field_info.default_factory is not None
    )
    if factory_fields:
        _raise_unsupported(
            owner,
            "a nested model default would execute default factories during compilation: "
            f"{', '.join(factory_fields)}",
        )
    return target_model.model_construct(_fields_set=set(projected), **projected)
