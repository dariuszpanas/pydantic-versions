"""Decorator route selection, reconciliation, and payload mechanics."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel

from pydantic_versions._compiler import (
    _CompiledDecoratorNestedFamily,
    _CompiledFamily,
    _CompiledVersion,
)
from pydantic_versions._runtime_nested import _normalize_validated_explicit_nested_payloads
from pydantic_versions._runtime_payload import (
    _extract_declared_fields,
    _matching_declared_annotation,
    _strip_annotated,
)
from pydantic_versions._runtime_versioning import (
    _current_validation_input,
    _serialized_field_name,
    _to_current_names,
)
from pydantic_versions.exceptions import InvalidMigrationError

type _DecoratorDispatchSite = tuple[tuple[str, str], ...]
type _DecoratorLocation = tuple[str | int, ...]
type _DecoratorCandidate = tuple[Any, _DecoratorLocation, _DecoratorLocation]
type _DecoratorUnionArmCache = dict[tuple[int, int], Any]
type _DecoratorRouteGroups = dict[
    _DecoratorDispatchSite,
    tuple[_CompiledDecoratorNestedFamily, ...],
]
type _DecoratorRouteGroupCache = dict[int, _DecoratorRouteGroups]


@dataclass
class _DecoratorRouteSelection:
    route: _CompiledDecoratorNestedFamily
    location: _DecoratorLocation
    relative_location: _DecoratorLocation
    site_routes: tuple[_CompiledDecoratorNestedFamily, ...]
    label: str
    parent: _DecoratorRouteSelection | None = None
    value_identity: int | None = None


def _select_decorator_routes(
    value: BaseModel,
    *,
    compiled: _CompiledFamily,
    parent_label: str,
    source_version: _CompiledVersion | None,
    location_prefix: tuple[str | int, ...] = (),
    parent_selection: _DecoratorRouteSelection | None = None,
    _union_arm_cache: _DecoratorUnionArmCache | None = None,
    _route_group_cache: _DecoratorRouteGroupCache | None = None,
) -> tuple[_DecoratorRouteSelection, ...]:
    if not compiled.decorator_nested:
        return ()
    if _union_arm_cache is None:
        _union_arm_cache = {}
    if _route_group_cache is None:
        _route_group_cache = {}
    root_names = {
        route.path[0]: (
            route.path[0]
            if source_version is None
            else source_version.projection.field(route.path[0]).version_name
        )
        for route in compiled.decorator_nested
    }
    selected: list[_DecoratorRouteSelection] = []
    route_groups = _decorator_route_groups(
        compiled,
        cache=_route_group_cache,
    )
    for route in compiled.decorator_nested:
        if root_names[route.path[0]] is None:
            continue
        for nested_value, location in _walk_authoritative_decorator_route(
            value,
            annotation=type(value),
            route=route,
            root_names=root_names,
            union_arm_cache=_union_arm_cache,
        ):
            expected = (route.family.model, route.family.model_for(parent_label))
            if isinstance(nested_value, expected):
                selection = _DecoratorRouteSelection(
                    route=route,
                    location=(*location_prefix, *location),
                    relative_location=location,
                    site_routes=route_groups[_decorator_dispatch_site(route)],
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
                        _union_arm_cache=_union_arm_cache,
                        _route_group_cache=_route_group_cache,
                    )
                )
    return tuple(selected)


def _walk_authoritative_decorator_route(
    value: Any,
    *,
    annotation: Any,
    route: _CompiledDecoratorNestedFamily,
    root_names: Mapping[str, str | None],
    union_arm_cache: _DecoratorUnionArmCache,
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
                cache_key = (id(normalized_annotation), id(current))
                if cache_key not in union_arm_cache:
                    union_arm_cache[cache_key] = _matching_declared_annotation(
                        normalized_annotation,
                        current,
                    )
                selected = union_arm_cache[cache_key]
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
) -> _DecoratorDispatchSite:
    return tuple(
        (step.kind, "*") if step.kind == "union_arm" else (step.kind, step.value)
        for step in route.traversal
    )


def _decorator_route_groups(
    compiled: _CompiledFamily,
    *,
    cache: _DecoratorRouteGroupCache,
) -> _DecoratorRouteGroups:
    cache_key = id(compiled)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    mutable_groups: dict[
        _DecoratorDispatchSite,
        list[_CompiledDecoratorNestedFamily],
    ] = {}
    for route in compiled.decorator_nested:
        site = _decorator_dispatch_site(route)
        mutable_groups.setdefault(site, []).append(route)

    groups = {site: tuple(routes) for site, routes in mutable_groups.items()}
    cache[cache_key] = groups
    return groups


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
        tuple[int, _DecoratorDispatchSite],
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
        candidates = tuple(
            (value, (*location_prefix, *relative_location), relative_location)
            for value, relative_location in _walk_decorator_payload_candidates(
                base_payload,
                declarations[0],
            )
        )
        candidate_identities = tuple(id(value) for value, _, _ in candidates)
        if any(
            identity in identity_sites and identity_sites[identity] != site_key
            for identity in candidate_identities
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

        candidate_indexes_by_identity: dict[int, list[int]] = {}
        for candidate_index, identity in enumerate(candidate_identities):
            candidate_indexes_by_identity.setdefault(identity, []).append(candidate_index)
        claimed_candidate_indexes: set[int] = set()
        available_previous_indexes = set(range(len(previous)))

        # Object identity is the authoritative occurrence token. It permits a
        # heterogeneous list to reorder without rediscovering its union branch.
        for previous_index, selection in enumerate(previous):
            identity = selection.value_identity
            if identity is None:
                continue
            candidate_indexes = candidate_indexes_by_identity.get(identity, ())
            if len(candidate_indexes) > 1:
                msg = (
                    "Parent migration reused one decorator nested occurrence at "
                    f"path {selection.route.path!r}"
                )
                raise InvalidMigrationError(msg)
            if not candidate_indexes:
                continue
            candidate_index = candidate_indexes[0]
            if candidate_index in claimed_candidate_indexes:
                continue
            value, location, relative_location = candidates[candidate_index]
            selection.location = location
            selection.relative_location = relative_location
            selection.value_identity = candidate_identities[candidate_index]
            available_previous_indexes.remove(previous_index)
            claimed_candidate_indexes.add(candidate_index)

        previous_indexes_by_location: dict[
            _DecoratorLocation,
            deque[int],
        ] = {}
        for previous_index in range(len(previous)):
            if previous_index not in available_previous_indexes:
                continue
            selection = previous[previous_index]
            previous_indexes_by_location.setdefault(
                selection.location,
                deque(),
            ).append(previous_index)

        routes_by_exact_model: dict[
            type[BaseModel],
            list[_CompiledDecoratorNestedFamily],
        ] = {}
        for declaration in declarations:
            routes_by_exact_model.setdefault(
                declaration.family.model,
                [],
            ).append(declaration)

        # An exact current-model instance is an explicit, authoritative branch
        # replacement and may establish a new route without validation.
        remaining_candidates: list[_DecoratorCandidate] = []
        fallback_previous_index = 0
        for candidate_index, (value, location, relative_location) in enumerate(candidates):
            if candidate_index in claimed_candidate_indexes:
                continue
            if not available_previous_indexes:
                remaining_candidates.extend(
                    candidates[index]
                    for index in range(candidate_index, len(candidates))
                    if index not in claimed_candidate_indexes
                )
                break
            if not isinstance(value, BaseModel):
                remaining_candidates.append((value, location, relative_location))
                continue
            matching = routes_by_exact_model.get(type(value), ())
            if len(matching) != 1:
                msg = (
                    "Parent migration supplied a typed decorator nested replacement "
                    f"with no unique family at path {declarations[0].path!r}"
                )
                raise InvalidMigrationError(msg)
            route = matching[0]
            location_indexes = previous_indexes_by_location.get(location)
            while location_indexes and location_indexes[0] not in available_previous_indexes:
                location_indexes.popleft()
            if location_indexes:
                previous_index = location_indexes.popleft()
            else:
                while fallback_previous_index not in available_previous_indexes:
                    fallback_previous_index += 1
                previous_index = fallback_previous_index
                fallback_previous_index += 1
            available_previous_indexes.remove(previous_index)
            replaced = previous[previous_index]
            replaced.route = route
            replaced.location = location
            replaced.relative_location = relative_location
            replaced.label = route.family.current_version
            replaced.value_identity = id(value)
            replaced_owner_ids.add(id(replaced))
            claimed_candidate_indexes.add(candidate_index)

        remaining_previous = [
            selection
            for previous_index, selection in enumerate(previous)
            if previous_index in available_previous_indexes
        ]

        if not remaining_previous:
            if remaining_candidates and not all(
                len(routes_by_exact_model.get(type(value), ())) == 1
                for value, _, _ in remaining_candidates
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
                location in anchored for _, location, _ in remaining_candidates
            ):
                for value, location, relative_location in remaining_candidates:
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
            for selection, (value, location, relative_location) in zip(
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
    occupied_locations = {(id(selection.parent), selection.location) for selection in selections}
    locations_by_identity: dict[int, set[tuple[str | int, ...]]] = {}
    children_by_parent: dict[int, list[_DecoratorRouteSelection]] = {}
    for selection in selections:
        children_by_parent.setdefault(id(selection.parent), []).append(selection)
        if selection.value_identity is not None:
            locations_by_identity.setdefault(selection.value_identity, set()).add(
                selection.location
            )
    route_group_cache: _DecoratorRouteGroupCache = {}

    def visit_owner(
        owner_payload: Any,
        owner_compiled: _CompiledFamily,
        *,
        location_prefix: tuple[str | int, ...],
        parent: _DecoratorRouteSelection | None,
    ) -> None:
        parent_identity = id(parent)
        route_groups = _decorator_route_groups(
            owner_compiled,
            cache=route_group_cache,
        )
        for declarations in route_groups.values():
            routes_by_exact_model: dict[
                type[BaseModel],
                list[_CompiledDecoratorNestedFamily],
            ] = {}
            for declaration in declarations:
                routes_by_exact_model.setdefault(
                    declaration.family.model,
                    [],
                ).append(declaration)
            for value, relative_location in _walk_decorator_payload_candidates(
                owner_payload,
                declarations[0],
            ):
                location = (*location_prefix, *relative_location)
                if (parent_identity, location) in occupied_locations:
                    continue
                matches = routes_by_exact_model.get(type(value), ())
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
                value_identity = id(value)
                existing_locations = locations_by_identity.get(value_identity)
                if existing_locations is not None and (
                    len(existing_locations) != 1 or location not in existing_locations
                ):
                    msg = (
                        "Parent migration reused one decorator nested occurrence at "
                        f"path {route.path!r}"
                    )
                    raise InvalidMigrationError(msg)
                selection = _DecoratorRouteSelection(
                    route=route,
                    location=location,
                    relative_location=relative_location,
                    site_routes=declarations,
                    label=route.family.current_version,
                    parent=parent,
                    value_identity=value_identity,
                )
                discovered.append(selection)
                occupied_locations.add((parent_identity, location))
                locations_by_identity.setdefault(value_identity, set()).add(location)
                children_by_parent.setdefault(parent_identity, []).append(selection)

        children = tuple(children_by_parent.get(parent_identity, ()))
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
