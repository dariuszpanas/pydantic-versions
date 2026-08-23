from __future__ import annotations

from collections.abc import Mapping
from functools import partial
from typing import (
    TYPE_CHECKING,
    Any,
    Literal,
)

from pydantic import BaseModel

from pydantic_versions._compiler import _CompiledFamily
from pydantic_versions._runtime_decorators import (
    _decorator_collection_kind,
    _decorator_selections_child_first,
    _decorator_selections_parent_first,
    _decorator_set_cardinalities,
    _DecoratorRouteSelection,
    _materialize_selected_decorator_models,
    _normalize_decorator_value,
    _normalize_selected_decorator_payloads,
    _payload_at_location,
    _reconcile_decorator_selections,
    _refresh_decorator_selection_identities,
    _refresh_decorator_selection_identity,
    _select_decorator_routes,
    _serialized_decorator_selection_location,
    _transform_payload_at_location,
    _walk_decorator_payload_candidates,
)
from pydantic_versions._runtime_nested import (
    _apply_nested_family_migrations,
    _convert_nested_family_payload,
    _declared_nested_version_metadata,
    _nested_family_collection_kind,
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
    _runtime_nested_structural_occurrences,
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
