from __future__ import annotations

from collections.abc import Mapping
from functools import partial
from typing import (
    TYPE_CHECKING,
    Any,
    Literal,
    get_args,
)

from pydantic import BaseModel

from pydantic_versions._compiler import _CompiledFamily, _CompiledFamilyRuntimeCache
from pydantic_versions._runtime_decorators import (
    _decorator_collection_kind,
    _decorator_selections_child_first,
    _decorator_selections_parent_first,
    _DecoratorRouteSelection,
    _normalize_decorator_value,
    _normalize_selected_decorator_payloads,
    _payload_at_location,
    _reconcile_decorator_selections,
    _refresh_decorator_selection_identities,
    _route_has_non_child_mapping_arm,
    _select_decorator_routes,
    _serialized_decorator_selection_location,
    _transform_payload_at_locations,
)
from pydantic_versions._runtime_nested import (
    _apply_nested_family_migrations,
    _convert_nested_family_payload,
    _declared_nested_version_metadata,
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
    _extract_declared_fields,
    _extract_preflight_fields,
    _preserve_canonical_mapping_copy,
    _strip_annotated,
)
from pydantic_versions._runtime_render import (
    _copy_render_input,
    _current_wire_validation_adapter,
    _validate_base_model_render_metadata,
    _validate_current_render_metadata,
    _without_family_render_metadata,
)
from pydantic_versions._runtime_validation import (
    _contains_hashable_canonical_mapping,
    _revalidate_canonical_model_instance,
    _validate_canonical_adapter_payload,
    _validate_canonical_model,
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
    if isinstance(data, source.model) and _model_instance_requires_revalidation(
        source.model,
        data,
    ):
        source_model = _revalidate_canonical_model_instance(
            source.model,
            data,
            cache=compiled._runtime_cache,
        )
    else:
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
        _extract_declared_fields(source_model, declared_model=source.model),
    )
    nested_payload_is_canonical = source.wire_model_kind != "explicit"
    if nested_payload_is_canonical:
        payload = _normalize_validated_explicit_nested_payloads(
            payload=payload,
            compiled=compiled,
            parent_label=source_version,
        )
    payload = _normalize_selected_decorator_payloads(
        payload=payload,
        selections=decorator_selections,
        parent_label=source_version,
    )

    migrations_applied: list[tuple[str, str]] = []
    source_index = compiled.index(source_version)
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
    nested_container_ids: set[int] = set()
    payload = _project_nested_family_payloads(
        payload=payload,
        compiled=compiled,
        parent_label=compiled.current_version,
        wire_boundary=False,
        guarded_container_ids=nested_container_ids,
    )
    payload = _project_selected_decorator_payloads(
        payload=payload,
        selections=decorator_selections,
        parent_label=compiled.current_version,
        wire_boundary=False,
    )
    payload, materialized_model_ids = _materialize_selected_decorator_wire_models(
        payload=payload,
        selections=decorator_selections,
        compiled=compiled,
        parent_label=compiled.current_version,
        wire_boundary=False,
    )
    current_model = _validate_canonical_model(
        family.model,
        _current_validation_input(
            family.model,
            payload,
            tracked_container_ids=nested_container_ids,
            mapping_copy=_preserve_canonical_mapping_copy,
        ),
        cache=compiled._runtime_cache,
        materialized_model_ids=materialized_model_ids,
        materialized_container_ids=frozenset(nested_container_ids)
        | _materialized_hash_container_ids(payload, materialized_model_ids),
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
    nested_container_ids: set[int] = set()
    payload = _project_nested_family_payloads(
        payload=payload,
        compiled=compiled,
        parent_label=requested,
        wire_boundary=True,
        guarded_container_ids=nested_container_ids,
    )
    payload = _project_selected_decorator_payloads(
        payload=payload,
        selections=decorator_selections,
        parent_label=requested,
        wire_boundary=True,
    )
    payload, materialized_model_ids = _materialize_selected_decorator_wire_models(
        payload=payload,
        selections=decorator_selections,
        compiled=compiled,
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

    target_payload = _to_version_names(
        target,
        payload,
        tracked_container_ids=nested_container_ids,
        mapping_copy=_preserve_canonical_mapping_copy,
    )
    target_model = _validate_canonical_model(
        target.model,
        target_payload,
        cache=compiled._runtime_cache,
        materialized_model_ids=materialized_model_ids,
        materialized_container_ids=frozenset(nested_container_ids)
        | _materialized_hash_container_ids(target_payload, materialized_model_ids),
    )
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
    if not selections:
        return
    source_payload = _extract_declared_fields(source_model)
    source_set_index_cache = {}
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
            msg = (
                f"Target serialization for family {compiled.name!r} and version "
                f"{parent_label!r} omitted managed decorator route "
                f"{selection.route.path!r}"
            )
            raise ValueError(msg)
        source_found, source_value = _payload_at_location(
            source_payload,
            selection.location,
            _set_index_cache=source_set_index_cache,
            # Detached identity-hashed sets may iterate differently; keep the
            # target-model ordinals that address the already serialized payload.
            _update_set_locations=False,
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
        if _model_instance_requires_revalidation(family.model, data):
            current_model = _revalidate_canonical_model_instance(
                family.model,
                data,
                cache=compiled._runtime_cache,
            )
        else:
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
            current_model = _validate_canonical_adapter_payload(
                _current_wire_validation_adapter(
                    compiled,
                    guard_collections=_contains_hashable_canonical_mapping(validation_payload),
                ),
                validation_payload,
            )
        else:
            current_model = family.model.model_validate(validation_payload)
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


def _model_instance_requires_revalidation(
    model: type[BaseModel],
    value: BaseModel,
) -> bool:
    policy = model.model_config.get("revalidate_instances", "never")
    return policy == "always" or (policy == "subclass-instances" and type(value) is not model)


def _apply_selected_decorator_migrations(
    *,
    payload: dict[str, Any],
    selections: tuple[_DecoratorRouteSelection, ...],
    target_label: str,
) -> dict[str, Any]:
    pending: list[tuple[_DecoratorRouteSelection, str]] = []
    transforms = []
    for selection in _decorator_selections_child_first(selections):
        nested_source = selection.label
        nested_target = selection.route.child_label(target_label)
        if nested_source == nested_target:
            continue
        pending.append((selection, nested_target))
        transforms.append(
            (
                selection.location,
                partial(
                    _convert_decorator_value,
                    family=selection.route.family,
                    source_label=nested_source,
                    target_label=nested_target,
                    collection_kind=_decorator_collection_kind(selection.route),
                ),
            ),
        )
    converted = _transform_payload_at_locations(
        payload,
        transforms=tuple(transforms),
        order="child_first",
    )
    for selection, nested_target in pending:
        selection.label = nested_target
    _refresh_decorator_selection_identities(converted, selections)
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
    transforms = []
    for selection in _decorator_selections_child_first(selections):
        target_label = selection.route.child_label(parent_label)
        transforms.append(
            (
                selection.location,
                partial(
                    _project_decorator_value,
                    family=selection.route.family,
                    target_label=target_label,
                    wire_boundary=wire_boundary,
                    collection_kind=_decorator_collection_kind(selection.route),
                ),
            ),
        )
    projected = _transform_payload_at_locations(
        payload,
        transforms=tuple(transforms),
        order="child_first",
    )
    _refresh_decorator_selection_identities(projected, selections)
    return projected


def _materialize_selected_decorator_wire_models(
    *,
    payload: dict[str, Any],
    selections: tuple[_DecoratorRouteSelection, ...],
    compiled: _CompiledFamily,
    parent_label: str,
    wire_boundary: bool,
) -> tuple[dict[str, Any], frozenset[int]]:
    transforms = []
    materialized_model_ids: set[int] = set()
    for selection in _decorator_selections_child_first(selections):
        if not any(step.kind == "union_arm" for step in selection.route.traversal):
            continue
        owner_model = (
            compiled.model if selection.parent is None else selection.parent.route.family.model
        )
        if len(selection.site_routes) == 1 and not _route_has_non_child_mapping_arm(
            owner_model,
            selection.route,
        ):
            continue
        child_compiled = selection.route.family._compiled_family()
        transforms.append(
            (
                selection.location,
                partial(
                    _materialize_selected_decorator_wire_model,
                    model=_selected_decorator_wire_model(
                        compiled=compiled,
                        parent_label=parent_label,
                        selection=selection,
                        wire_boundary=wire_boundary,
                    ),
                    cache=child_compiled._runtime_cache,
                    materialized_model_ids=materialized_model_ids,
                ),
            )
        )
    materialized = _transform_payload_at_locations(
        payload,
        transforms=tuple(transforms),
        order="child_first",
    )
    assert isinstance(materialized, dict)
    _refresh_decorator_selection_identities(materialized, selections)
    return materialized, frozenset(materialized_model_ids)


def _materialize_selected_decorator_wire_model(
    value: Any,
    *,
    model: type[BaseModel],
    cache: _CompiledFamilyRuntimeCache,
    materialized_model_ids: set[int],
) -> Any:
    if not isinstance(value, Mapping):
        return value
    existing_ids = frozenset(materialized_model_ids)
    validated = _validate_canonical_model(
        model,
        dict(value),
        cache=cache,
        materialized_model_ids=existing_ids,
        materialized_container_ids=_materialized_hash_container_ids(value, existing_ids),
    )
    materialized_model_ids.add(id(validated))
    return validated


def _materialized_hash_container_ids(
    value: Any,
    materialized_model_ids: frozenset[int],
) -> frozenset[int]:
    if not materialized_model_ids:
        return frozenset()
    found: set[int] = set()
    seen: set[int] = set()

    def contains_materialized_hash_value(item: Any) -> bool:
        if id(item) in materialized_model_ids:
            return True
        if isinstance(item, tuple | frozenset):
            return any(contains_materialized_hash_value(child) for child in item)
        return False

    def visit(current: Any) -> None:
        if not isinstance(current, Mapping | list | tuple | set | frozenset):
            return
        identity = id(current)
        if identity in seen:
            return
        seen.add(identity)
        if isinstance(current, set | frozenset):
            if any(contains_materialized_hash_value(item) for item in current):
                found.add(identity)
            for item in current:
                visit(item)
            return
        if isinstance(current, Mapping):
            if any(contains_materialized_hash_value(key) for key in current):
                found.add(identity)
            for key, item in current.items():
                visit(key)
                visit(item)
            return
        for item in current:
            visit(item)

    visit(value)
    return frozenset(found)


def _selected_decorator_wire_model(
    *,
    compiled: _CompiledFamily,
    parent_label: str,
    selection: _DecoratorRouteSelection,
    wire_boundary: bool,
) -> type[BaseModel]:
    if selection.parent is None:
        owner_compiled = compiled
        owner_label = parent_label
        owner_model = (
            owner_compiled.version(owner_label).model if wire_boundary else owner_compiled.model
        )
    else:
        owner_compiled = selection.parent.route.family._compiled_family()
        owner_label = selection.parent.label
        owner_model = _selected_decorator_wire_model(
            compiled=compiled,
            parent_label=parent_label,
            selection=selection.parent,
            wire_boundary=wire_boundary,
        )

    owner_target = owner_compiled.version(owner_label)
    annotation: Any = owner_model
    for step_index, step in enumerate(selection.route.traversal):
        normalized = _strip_annotated(annotation)
        if step.kind == "field":
            field_name = step.value
            if wire_boundary and step_index == 0:
                projected_name = owner_target.projection.field(field_name).version_name
                assert projected_name is not None
                field_name = projected_name
            annotation = normalized.model_fields[field_name].annotation
            continue
        arguments = get_args(normalized)
        if step.kind == "union_arm":
            annotation = arguments[int(step.value)]
        elif step.kind == "mapping_values":
            annotation = arguments[1]
        elif step.kind == "tuple_index":
            annotation = arguments[int(step.value)]
        else:
            assert step.kind == "each"
            annotation = arguments[0]

    target_model = _strip_annotated(annotation)
    assert isinstance(target_model, type) and issubclass(target_model, BaseModel)
    return target_model


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
    transforms = []
    for selection in _decorator_selections_parent_first(selections):
        route = selection.route
        expected = route.child_label(parent_label)
        child_compiled = route.family._compiled_family()
        transforms.append(
            (
                selection.location,
                partial(
                    _preflight_decorator_value,
                    route=route,
                    child_compiled=child_compiled,
                    expected=expected,
                    parent_label=parent_label,
                ),
            ),
        )
    _transform_payload_at_locations(
        canonical,
        transforms=tuple(transforms),
        order="parent_first",
    )


def _preflight_decorator_value(
    raw: Any,
    *,
    route: Any,
    child_compiled: _CompiledFamily,
    expected: str,
    parent_label: str,
) -> Any:
    if isinstance(raw, BaseModel):
        raw = _extract_preflight_fields(raw)
    if not isinstance(raw, Mapping):
        return raw
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
    return _normalize_decorator_value(
        raw,
        compiled=child_compiled,
        child_label=expected,
    )
