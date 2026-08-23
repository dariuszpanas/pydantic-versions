from __future__ import annotations

from collections.abc import Mapping
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
)

from pydantic import (
    BaseModel,
    Field,
    create_model,
)
from pydantic_core import PydanticUndefined

from pydantic_versions._compiler import (
    _CompiledDecoratorNestedFamily,
    _CompiledNestedFamily,
    _generated_model_name,
    _VersionProjection,
)
from pydantic_versions._wire_contract import (
    _CUSTOM_MODEL_HOOKS,
    _OMITTED_FIELD_ATTRIBUTES,
    _add_family_metadata_field,
    _factory_takes_validated_data,
    _first_defining_class,
    _has_effect,
    _literal_type,
    _model_display,
    _model_metadata_field,
    _raise_projection_unsupported,
    _raise_unsupported,
    _safe_deepcopy,
    _validate_explicit_family_metadata_collision,
    _validate_explicit_wire_model_metadata,
    _validate_family_metadata_collision,
    _validate_generated_metadata_aliases,
    _validate_metadata_field_name_collision,
    _validate_model_config,
    _validate_object_schema,
    _validate_typed_extras,
    _validate_unique_serialization_names,
    _wire_field_attributes,
    _wire_field_metadata,
    _wire_model_config,
)
from pydantic_versions._wire_document import (
    _build_family_document_adapter,
    _validate_family_document_adapter_member_collisions,
    _validate_family_document_adapter_schema_hooks,
)
from pydantic_versions._wire_nested import (
    _compile_decorator_nested_families as _compile_decorator_nested_families,
)
from pydantic_versions._wire_nested import (
    _rewrite_annotation,
    _rewrite_nested_default,
    _validate_nested_projection_coverage,
)
from pydantic_versions._wire_nested import (
    _WireCompilationContext as _WireCompilationContext,
)
from pydantic_versions._wire_shape_contract import (
    _validate_explicit_nested_serializer_boundaries,
)
from pydantic_versions.exceptions import UnsupportedWireModelError

if TYPE_CHECKING:
    from pydantic_versions.family import SchemaFamily


def _validate_automatic_wire_model(family: SchemaFamily[Any]) -> None:
    model = family.model
    if getattr(model, "__pydantic_root_model__", False):
        _raise_unsupported(family, "RootModel is not an object-shaped wire body")
    if not getattr(model, "__pydantic_complete__", False):
        _raise_unsupported(
            family,
            "the model is incomplete; resolve forward references and rebuild it first",
        )

    generic_metadata = getattr(model, "__pydantic_generic_metadata__", None)
    if isinstance(generic_metadata, Mapping) and generic_metadata.get("parameters"):
        _raise_unsupported(family, "unresolved generic parameters cannot define a wire body")

    decorators = getattr(model, "__pydantic_decorators__", None)
    if decorators is not None and getattr(decorators, "model_serializers", None):
        _raise_unsupported(family, "model-level serializers cannot be projected automatically")

    for hook in _CUSTOM_MODEL_HOOKS:
        owner = _first_defining_class(model, hook)
        if owner is not None and owner is not BaseModel:
            _raise_unsupported(family, f"custom model hook {hook} cannot be projected")

    _validate_model_config(family)
    _validate_typed_extras(family)
    _model_metadata_field(family)
    _validate_family_metadata_collision(family)


def _build_model_for_projection(
    family: SchemaFamily[Any],
    projection: _VersionProjection,
    wire_model: type[BaseModel] | None,
    *,
    compilation: _WireCompilationContext,
    nested: tuple[_CompiledNestedFamily, ...] = (),
    decorator_nested: tuple[_CompiledDecoratorNestedFamily, ...] = (),
) -> type[BaseModel]:
    if wire_model is not None:
        if decorator_nested:
            _raise_projection_unsupported(
                family,
                projection,
                "explicit wire models cannot contain decorator-discovered child families; "
                "declare explicit NestedFamily boundaries instead",
            )
        _validate_explicit_nested_serializer_boundaries(
            family,
            projection,
            wire_model,
            nested=nested,
        )
        metadata = family.version_metadata
        if metadata is not None and metadata.owner == "family":
            _validate_family_document_adapter_schema_hooks(
                family,
                projection,
                wire_model,
            )
            _validate_family_document_adapter_member_collisions(
                family,
                projection,
                wire_model,
            )
        validated = _validate_explicit_wire_model(family, projection, wire_model)
        if metadata is not None and metadata.owner == "family":
            return _build_family_document_adapter(
                family,
                projection,
                validated,
            )
        return validated
    try:
        return _build_model_for_projection_unchecked(
            family,
            projection,
            compilation=compilation,
            nested=nested,
            decorator_nested=decorator_nested,
        )
    except UnsupportedWireModelError:
        raise
    except Exception as exc:
        msg = (
            f"Automatic wire model for family {family.name!r}, version "
            f"{projection.label!r}, and model {_model_display(family.model)!r} "
            "could not be built safely"
        )
        raise UnsupportedWireModelError(msg) from exc


def _validate_explicit_wire_model(
    family: SchemaFamily[Any],
    projection: _VersionProjection,
    wire_model: type[BaseModel],
) -> type[BaseModel]:
    _validate_explicit_wire_model_metadata(family, projection, wire_model)
    _validate_explicit_family_metadata_collision(family, projection, wire_model)
    if family.version_metadata is not None and family.version_metadata.owner == "model":
        _validate_generated_metadata_aliases(
            family,
            projection,
            wire_model,
            model_metadata_field=_model_metadata_field(family),
        )
    _validate_unique_serialization_names(family, projection, wire_model)
    _validate_object_schema(
        family,
        projection,
        wire_model,
        mode="validation",
    )
    _validate_object_schema(
        family,
        projection,
        wire_model,
        mode="serialization",
    )
    return wire_model


def _build_model_for_projection_unchecked(
    family: SchemaFamily[Any],
    projection: _VersionProjection,
    *,
    compilation: _WireCompilationContext,
    nested: tuple[_CompiledNestedFamily, ...] = (),
    decorator_nested: tuple[_CompiledDecoratorNestedFamily, ...] = (),
) -> type[BaseModel]:
    model_metadata_field = _model_metadata_field(family)
    used_nested: set[tuple[str, ...]] = set()
    nested_projection_cache: dict[
        tuple[int, tuple[str, ...], str, bool], type[BaseModel] | None
    ] = {}
    fields: dict[str, Any] = {}
    for compiled_field in projection.fields:
        if compiled_field.version_name is None:
            if compiled_field.current_name == model_metadata_field:
                _raise_unsupported(
                    family,
                    "model-owned version metadata cannot be removed from a wire version",
                )
            continue

        field_info = family.model.model_fields[compiled_field.current_name]
        field_dict = field_info.asdict()
        excluded = any(
            key in field_dict["attributes"] and _has_effect(field_dict["attributes"][key])
            for key in _OMITTED_FIELD_ATTRIBUTES
        )
        if excluded:
            if compiled_field.current_name == model_metadata_field:
                _raise_projection_unsupported(
                    family,
                    projection,
                    "model-owned version metadata cannot be excluded from the wire model",
                )
            # These fields are application/server state, not part of the document
            # contract.  Omit them from every generated projection rather than
            # pretending that Pydantic's serialization-only exclusion is wire-safe.
            continue
        if (
            compiled_field.current_name == model_metadata_field
            and compiled_field.default is not None
        ):
            _raise_projection_unsupported(
                family,
                projection,
                "model-owned version metadata cannot have a historical default patch",
            )
        if compiled_field.current_name != model_metadata_field and _factory_takes_validated_data(
            field_info, compiled_field.default
        ):
            _raise_projection_unsupported(
                family,
                projection,
                f"validated-data default factory for field {compiled_field.current_name!r} "
                "cannot be projected without materializing current-model behavior",
            )
        attributes = _wire_field_attributes(
            family,
            compiled_field.current_name,
            field_dict["attributes"],
        )
        metadata = (
            ()
            if compiled_field.current_name == model_metadata_field
            else _wire_field_metadata(
                family,
                compiled_field.current_name,
                field_dict["metadata"],
            )
        )
        annotation = _rewrite_annotation(
            field_dict["annotation"],
            projection.label,
            family,
            compilation=compilation,
            nested=nested,
            decorator_nested=decorator_nested,
            field_path=(compiled_field.current_name,),
            used_nested=used_nested,
            field_name=compiled_field.current_name,
            allow_child_projection=True,
            nested_projection_cache=nested_projection_cache,
        )
        if compiled_field.version_name != compiled_field.current_name:
            if compiled_field.current_name == model_metadata_field:
                _raise_unsupported(
                    family,
                    "model-owned version metadata must keep one invariant wire location",
                )
            attributes["alias"] = None
            attributes["alias_priority"] = None
            attributes["validation_alias"] = None
            attributes["serialization_alias"] = None
        if compiled_field.default is not None:
            if compiled_field.default.has_default:
                attributes["default"] = _safe_deepcopy(
                    family,
                    compiled_field.default.default,
                    detail=f"default for field {compiled_field.current_name!r}",
                )
                attributes["default_factory"] = None
            else:
                attributes["default"] = PydanticUndefined
                attributes["default_factory"] = compiled_field.default.default_factory
        _rewrite_nested_default(
            attributes,
            field_dict["annotation"],
            annotation,
            family,
            nested=nested,
            decorator_nested=decorator_nested,
            field_path=(compiled_field.current_name,),
            used_nested=used_nested,
            field_name=compiled_field.current_name,
            version=projection.label,
        )
        if compiled_field.current_name == model_metadata_field:
            annotation = _literal_type(projection.label)
            attributes["default"] = projection.label
            attributes["default_factory"] = None
            attributes["json_schema_extra"] = None

        fields[compiled_field.version_name] = Annotated[
            annotation,
            *metadata,
            Field(**attributes),
        ]

    _validate_metadata_field_name_collision(family, projection, fields)
    _validate_nested_projection_coverage(family, projection, nested, used_nested)
    _add_family_metadata_field(family, projection.label, fields)

    generated = create_model(
        _generated_model_name(family.model, family.name, projection.label),
        __config__=_wire_model_config(family),
        __module__=family.model.__module__,
        **fields,
    )
    _validate_generated_metadata_aliases(
        family,
        projection,
        generated,
        model_metadata_field=model_metadata_field,
    )
    _validate_unique_serialization_names(family, projection, generated)
    _validate_object_schema(family, projection, generated, mode="validation")
    _validate_object_schema(family, projection, generated, mode="serialization")
    return generated
