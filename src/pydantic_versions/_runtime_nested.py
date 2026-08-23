"""Nested-family runtime validation, migration, projection, and pruning."""

from __future__ import annotations

from collections.abc import Mapping
from functools import partial
from types import NoneType, UnionType
from typing import (
    TYPE_CHECKING,
    Any,
    Literal,
    NoReturn,
    TypeVar,
    Union,
    cast,
    get_args,
    get_origin,
)

from pydantic import BaseModel

from pydantic_versions._compiler import (
    _CompiledDecoratorNestedFamily,
    _CompiledFamily,
    _CompiledVersion,
)
from pydantic_versions._runtime_payload import (
    _annotation_contains_model,
    _annotation_declares_path,
    _collection_kind,
    _declared_field_payload_value,
    _declared_payload_occurrences_at_path,
    _declared_runtime_values_at_path,
    _explicit_runtime_body,
    _explicit_runtime_body_model,
    _extract_declared_fields,
    _extract_preflight_fields,
    _is_concrete_runtime_scalar_annotation,
    _is_runtime_base_model_type,
    _matching_declared_annotation,
    _runtime_annotation_value,
    _runtime_nested_structural_occurrences,
    _runtime_structural_field_value,
    _runtime_structural_fields,
    _runtime_structural_serialized_field_name,
    _runtime_structural_validation_alias_paths,
    _runtime_type_parameter_values,
    _runtime_value_matches_annotation,
    _strip_annotated,
    _transform_declared_payload_at_path,
    _unwrap_optional_annotated,
)
from pydantic_versions._runtime_versioning import (
    _MISSING,
    _alias_paths,
    _copy_alias_payload_value,
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
    _verify_serialized_model_metadata,
    _version_field_display,
)
from pydantic_versions.declarations import VersionPath
from pydantic_versions.exceptions import (
    InvalidMigrationError,
    IrreversibleTransitionError,
    SchemaVersionError,
)

if TYPE_CHECKING:
    from pydantic_versions.family import SchemaFamily


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


def _target_nested_path(
    target: _CompiledVersion,
    path: tuple[str, ...],
) -> tuple[str, ...]:
    first = target.projection.field(path[0]).version_name
    assert first is not None
    return (first, *path[1:])
