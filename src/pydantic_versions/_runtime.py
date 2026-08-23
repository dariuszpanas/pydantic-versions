from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from types import UnionType
from typing import (
    TYPE_CHECKING,
    Any,
    Literal,
    Union,
    get_args,
    get_origin,
)

from pydantic import BaseModel

from pydantic_versions._compiler import (
    _CompiledDecoratorNestedFamily,
    _CompiledFamily,
    _CompiledVersion,
)
from pydantic_versions._runtime_nested import (
    _apply_nested_family_migrations,
    _convert_nested_family_payload,
    _declared_nested_version_metadata,
    _nested_family_collection_kind,
    _normalize_validated_explicit_nested_payloads,
    _preflight_nested_version_metadata,
    _preflight_validation_route,
    _project_nested_family_payload,
    _project_nested_family_payloads,
    _prune_nested_family_metadata_at_path,
    _prune_nested_family_metadata_payload,
    _target_nested_path,
    _validate_explicit_nested_runtime_shapes,
    _verify_validated_family_version_metadata,
)
from pydantic_versions._runtime_payload import (
    _declared_payload_occurrences_at_path,
    _explicit_runtime_body_model,
    _extract_declared_fields,
    _extract_preflight_fields,
    _matching_declared_annotation,
    _runtime_nested_structural_occurrences,
    _strip_annotated,
)
from pydantic_versions._runtime_render import (
    _copy_render_input,
    _current_wire_validation_adapter,
    _validate_base_model_render_metadata,
    _validate_current_render_metadata,
    _without_family_render_metadata,
)
from pydantic_versions._runtime_versioning import (
    _apply_serialized_version_metadata,
    _current_validation_input,
    _detect_version,
    _matches_version_label,
    _model_metadata_field_name,
    _safe_nested_version_display,
    _serialized_field_name,
    _set_version_field,
    _to_current_names,
    _to_version_names,
    _validate_include_version_mode,
)
from pydantic_versions._runtime_versioning import (
    _runtime_label as _runtime_label,
)
from pydantic_versions.declarations import VersionedValidation
from pydantic_versions.exceptions import (
    InvalidMigrationError,
    SchemaVersionError,
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
