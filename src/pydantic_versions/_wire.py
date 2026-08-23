from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from copy import copy, deepcopy
from dataclasses import dataclass, is_dataclass
from dataclasses import field as dataclass_field
from dataclasses import fields as dataclass_fields
from enum import Enum
from functools import reduce
from operator import or_
from types import GenericAlias, MemberDescriptorType, UnionType
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ClassVar,
    ForwardRef,
    Literal,
    TypeVar,
    Union,
    cast,
    get_args,
    get_origin,
    get_type_hints,
)

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    GetCoreSchemaHandler,
    GetJsonSchemaHandler,
    GetPydanticSchema,
    create_model,
)
from pydantic.fields import FieldInfo
from pydantic.functional_serializers import PlainSerializer, WrapSerializer
from pydantic_core import CoreSchema, PydanticUndefined, core_schema
from typing_extensions import NoExtraItems

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
    _MISSING,
    _MODEL_SCHEMA_STRUCTURE_KEYS,
    _OMITTED_FIELD_ATTRIBUTES,
    _REJECTED_CONFIG_KEYS,
    _SERIALIZE_AS_ANY_METADATA_TYPE,
    _TYPE_ALIAS_TYPES,
    _WIRE_CONFIG_KEYS,
    _add_family_metadata_field,
    _annotation_type_parameters,
    _bound_annotation_parameters,
    _computed_field_output_paths,
    _effective_static_class_items,
    _effective_static_class_values,
    _factory_takes_validated_data,
    _field_contract_paths,
    _first_defining_class,
    _has_custom_annotation_schema_hook,
    _has_effect,
    _has_schema_hook,
    _instance_dict,
    _is_no_extra_items,
    _is_typed_dict,
    _is_typed_dict_field_qualifier,
    _literal_type,
    _model_display,
    _model_has_model_serializer,
    _model_metadata_field,
    _pydantic_decorator_info_kind,
    _raise_projection_unsupported,
    _raise_unsupported,
    _safe_deepcopy,
    _safe_runtime_subclass,
    _snapshot_wire_metadata,
    _type_parameter_values,
    _typed_dict_origin,
    _validate_annotation_behavior,
    _validate_explicit_family_metadata_collision,
    _validate_explicit_wire_model_metadata,
    _validate_family_metadata_collision,
    _validate_generated_metadata_aliases,
    _validate_metadata_field_name_collision,
    _validate_model_config,
    _validate_object_schema,
    _validate_type_alias,
    _validate_typed_extras,
    _validate_unique_serialization_names,
    _wire_field_attributes,
    _wire_field_metadata,
    _wire_model_config,
)
from pydantic_versions.exceptions import SchemaCompilationError, UnsupportedWireModelError

if TYPE_CHECKING:
    from pydantic_versions.family import SchemaFamily


_DOCUMENT_BODY_SLOT = "_FamilyDocumentAdapterBase__document_body"
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


@dataclass
class _WireCompilationContext:
    hashable_models: dict[type[BaseModel], type[BaseModel]] = dataclass_field(default_factory=dict)


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


def _build_family_document_adapter(
    family: SchemaFamily[Any],
    projection: _VersionProjection,
    body_model: type[BaseModel],
) -> type[BaseModel]:
    # The sole caller checks the owner immediately before entering this builder.
    metadata = cast(Any, family.version_metadata)
    label = projection.label
    metadata_path = metadata.path
    _validate_family_document_adapter_schema_hooks(family, projection, body_model)
    adapter_config = dict(body_model.model_config)
    # The exact body schema remains the sole owner of materialized aliases and
    # JSON Schema callbacks. Replaying these while create_model builds the
    # document facade can change stateful aliases or execute callbacks twice.
    for key in (
        "alias_generator",
        "field_title_generator",
        "json_schema_extra",
        "model_title_generator",
        "schema_generator",
    ):
        adapter_config.pop(key, None)
    body_config = ConfigDict(**adapter_config)
    body_fields = tuple(body_model.model_fields)

    def synchronize_adapter(instance: BaseModel) -> BaseModel:
        body = object.__getattribute__(
            instance,
            "_FamilyDocumentAdapterBase__document_body",
        )
        adapter_instance = cast(Any, instance)
        synchronized = type(adapter_instance)._from_document_body(body)
        _FamilyDocumentAdapterBase._replace_document_adapter_state(
            adapter_instance,
            synchronized,
        )
        return body

    class _FamilyDocumentAdapterBase(BaseModel):
        __slots__ = ("__document_body",)

        model_config = body_config
        _document_body_model: ClassVar[type[BaseModel]] = body_model

        @classmethod
        def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
            super().__pydantic_init_subclass__(**kwargs)
            if _FamilyDocumentAdapterBase in cls.__bases__:
                return
            msg = (
                f"Generated family-owned document model for {family.name!r} is final; "
                "subclass the explicit wire body before declaring the schema version"
            )
            raise TypeError(msg)

        def __init__(self, /, **data: Any) -> None:  # noqa: V103 - Pydantic entry point
            validated = type(self).model_validate(data)
            _FamilyDocumentAdapterBase._replace_document_adapter_state(self, validated)

        def __getattribute__(self, name: str) -> Any:
            if name in body_fields:
                body = object.__getattribute__(
                    self,
                    "_FamilyDocumentAdapterBase__document_body",
                )
                if isinstance(body, body_model):
                    value = getattr(body, name)
                    return self if value is body else value
            # Explicit bodies with extra="allow" may legitimately receive names
            # used only by the generated facade. Keep those values observable in
            # exactly the same way as on the body while internal calls use the
            # unbound helper methods and the slot descriptor directly.
            if name in {
                _DOCUMENT_BODY_SLOT,
                "_document_body_model",
                "_from_document_body",
                "_replace_document_adapter_state",
            }:
                extras = object.__getattribute__(self, "__pydantic_extra__")
                if isinstance(extras, Mapping) and name in extras:
                    return extras[name]
            return super().__getattribute__(name)

        def _replace_document_adapter_state(self, adapter: BaseModel) -> None:
            metadata_root = metadata_path if isinstance(metadata_path, str) else metadata_path[0]
            existing_metadata = self.__dict__.get(metadata_root, _MISSING)
            if existing_metadata is not _MISSING:
                adapter.__dict__[metadata_root] = existing_metadata
            object.__setattr__(self, "__dict__", adapter.__dict__)
            object.__setattr__(
                self,
                "__pydantic_fields_set__",
                adapter.__pydantic_fields_set__,
            )
            object.__setattr__(self, "__pydantic_extra__", adapter.__pydantic_extra__)
            object.__setattr__(self, "__pydantic_private__", adapter.__pydantic_private__)
            body = object.__getattribute__(
                adapter,
                "_FamilyDocumentAdapterBase__document_body",
            )
            object.__setattr__(
                self,
                "_FamilyDocumentAdapterBase__document_body",
                body,
            )

        @classmethod
        def _from_document_body(cls, body: BaseModel) -> BaseModel:
            if not isinstance(body, body_model):
                msg = (
                    f"Explicit wire body validator for family {family.name!r} must "
                    f"return an instance of {_model_display(body_model)!r}"
                )
                raise ValueError(msg)
            values = {name: body.__dict__.get(name, PydanticUndefined) for name in body_fields}
            extras = body.__pydantic_extra__
            if isinstance(extras, Mapping):
                values.update(
                    (name, value)
                    for name, value in extras.items()
                    if name not in body_model.model_fields
                )
            adapter = super().model_construct(
                _fields_set=set(body.model_fields_set),
                **values,
            )
            # model_construct fills facade defaults. Preserve the exact body
            # state after supported deletion rather than resurrecting fields.
            for name in body_fields:
                if name not in body.__dict__:
                    adapter.__dict__.pop(name, None)
            object.__setattr__(adapter, "__pydantic_fields_set__", body.__pydantic_fields_set__)
            object.__setattr__(adapter, "__pydantic_extra__", body.__pydantic_extra__)
            object.__setattr__(adapter, "__pydantic_private__", body.__pydantic_private__)
            object.__setattr__(
                adapter,
                "_FamilyDocumentAdapterBase__document_body",
                body,
            )
            return adapter

        @classmethod
        def model_construct(
            cls,
            _fields_set: set[str] | None = None,
            **values: Any,
        ) -> BaseModel:
            body_values = _copy_without_document_metadata(
                values,
                metadata_path=metadata_path,
                expected=label,
                family_name=family.name,
            )
            body_fields_set = None if _fields_set is None else set(_fields_set)
            if body_fields_set is not None:
                metadata_root = (
                    metadata_path if isinstance(metadata_path, str) else metadata_path[0]
                )
                body_fields_set.discard(metadata_root)
            body = body_model.model_construct(
                _fields_set=body_fields_set,
                **body_values,
            )
            return cls._from_document_body(body)

        @classmethod
        def __get_pydantic_core_schema__(
            cls,
            _source_type: Any,
            handler: GetCoreSchemaHandler,
        ) -> CoreSchema:
            body_schema = handler.generate_schema(body_model)

            def strip_metadata(value: Any) -> Any:
                if isinstance(value, cls):
                    body = object.__getattribute__(
                        value,
                        "_FamilyDocumentAdapterBase__document_body",
                    )
                    if isinstance(body, body_model):
                        return body
                if isinstance(value, body_model):
                    _reject_explicit_body_document_metadata(
                        value,
                        metadata_path=metadata_path,
                        family_name=family.name,
                    )
                    return value
                if not isinstance(value, Mapping):
                    return value
                copied = _copy_without_document_metadata(
                    value,
                    metadata_path=metadata_path,
                    expected=label,
                    family_name=family.name,
                )
                return copied

            def validate_document(value: Any, inner_handler: Any) -> BaseModel:
                revalidation = body_model.model_config.get("revalidate_instances", "never")
                foreign_body = isinstance(value, body_model) and not isinstance(value, cls)
                attribute_source = not isinstance(value, (cls, body_model, Mapping))
                if attribute_source:
                    if body_model.model_config.get("from_attributes") is not True:
                        msg = (
                            f"Explicit wire body for family {family.name!r} does not "
                            "enable attribute validation; use a mapping or body instance"
                        )
                        raise ValueError(msg)
                    _validate_document_metadata_attributes(
                        value,
                        metadata_path=metadata_path,
                        expected=label,
                        family_name=family.name,
                    )
                if isinstance(value, cls):
                    if revalidation != "always":
                        synchronize_adapter(value)
                        return value
                    body = strip_metadata(value)
                    body = inner_handler(body)
                else:
                    body = inner_handler(strip_metadata(value))
                if foreign_body and body is value:
                    body = _model_revalidation_proxy(body, type(body))
                return cls._from_document_body(body)

            def serialize_document(instance: BaseModel, handler: Any, info: Any) -> Any:
                body = synchronize_adapter(instance)
                try:
                    serialized = handler(body)
                finally:
                    synchronize_adapter(instance)
                if not isinstance(serialized, Mapping):
                    msg = (
                        f"Explicit wire body for family {family.name!r} and version "
                        f"{label!r} must serialize to an object"
                    )
                    raise ValueError(msg)
                return _copy_with_document_metadata(
                    serialized,
                    metadata_path=metadata_path,
                    metadata_value=label,
                    family_name=family.name,
                )

            return core_schema.no_info_wrap_validator_function(
                validate_document,
                body_schema,
                serialization=core_schema.wrap_serializer_function_ser_schema(
                    serialize_document,
                    info_arg=True,
                    schema=body_schema,
                    return_schema=core_schema.dict_schema(),
                ),
            )

        @classmethod
        def __get_pydantic_json_schema__(
            cls,
            _core_schema: CoreSchema,
            handler: GetJsonSchemaHandler,
        ) -> dict[str, Any]:
            body_core_schema: Any = _core_schema.get("schema")
            if handler.mode == "serialization":
                serialization = _core_schema.get("serialization")
                if isinstance(serialization, Mapping):
                    body_core_schema = serialization.get("schema", body_core_schema)
            schema = deepcopy(handler(body_core_schema))
            schema = deepcopy(handler.resolve_ref_schema(schema))
            _add_document_metadata_json_schema(
                schema,
                metadata_path=metadata_path,
                label=label,
            )
            return schema

        def __getattr__(self, name: str) -> Any:
            body = object.__getattribute__(
                self,
                "_FamilyDocumentAdapterBase__document_body",
            )
            if isinstance(body, body_model) and (
                name in body_model.__private_attributes__
                or name in body_model.model_computed_fields
            ):
                try:
                    value = getattr(body, name)
                    return self if value is body else value
                finally:
                    synchronize_adapter(self)
            try:
                base_getattr = vars(BaseModel)["__getattr__"]
                value = base_getattr(self, name)
                return self if value is body else value
            except AttributeError:
                raise

        def __setattr__(self, name: str, value: Any) -> None:
            if name == _DOCUMENT_BODY_SLOT:
                msg = f"Internal document state for family {family.name!r} is reserved"
                raise AttributeError(msg)
            body = object.__getattribute__(
                self,
                "_FamilyDocumentAdapterBase__document_body",
            )
            body_private = name in body_model.__private_attributes__
            if isinstance(body, body_model) and (not name.startswith("_") or body_private):
                metadata_root = (
                    metadata_path if isinstance(metadata_path, str) else metadata_path[0]
                )
                if name == metadata_root:
                    _copy_without_document_metadata(
                        {name: value},
                        metadata_path=metadata_path,
                        expected=label,
                        family_name=family.name,
                    )
                    return
                try:
                    setattr(body, name, value)
                finally:
                    synchronize_adapter(self)
                return
            super().__setattr__(name, value)

        def __delattr__(self, name: str) -> None:
            if name == _DOCUMENT_BODY_SLOT:
                msg = f"Internal document state for family {family.name!r} is reserved"
                raise AttributeError(msg)
            body = object.__getattribute__(
                self,
                "_FamilyDocumentAdapterBase__document_body",
            )
            if isinstance(body, body_model):
                metadata_root = (
                    metadata_path if isinstance(metadata_path, str) else metadata_path[0]
                )
                if name == metadata_root:
                    msg = f"Family-owned version metadata for {family.name!r} cannot be deleted"
                    raise AttributeError(msg)
                extras = body.__pydantic_extra__
                body_extra = isinstance(extras, Mapping) and name in extras
                if (
                    name in body_model.model_fields
                    or name in body_model.__private_attributes__
                    or name in body_model.model_computed_fields
                    or body_extra
                ):
                    try:
                        delattr(body, name)
                    finally:
                        synchronize_adapter(self)
                    return
            super().__delattr__(name)

        def model_copy(
            self,
            *,
            update: Mapping[str, Any] | None = None,
            deep: bool = False,
        ) -> BaseModel:
            body = object.__getattribute__(
                self,
                "_FamilyDocumentAdapterBase__document_body",
            )
            body_update = None
            if update is not None:
                body_update = _copy_without_document_metadata(
                    update,
                    metadata_path=metadata_path,
                    expected=label,
                    family_name=family.name,
                )
            copied = body.model_copy(update=body_update, deep=deep)
            adapter = type(self)._from_document_body(copied)
            if not deep:
                metadata_root = (
                    metadata_path if isinstance(metadata_path, str) else metadata_path[0]
                )
                if metadata_root in self.__dict__:
                    adapter.__dict__[metadata_root] = self.__dict__[metadata_root]
            return adapter

        def __copy__(self) -> BaseModel:
            return self.model_copy()

        def __iter__(self) -> Any:
            synchronize_adapter(self)
            return super().__iter__()

        def __repr_args__(self) -> Any:
            synchronize_adapter(self)
            return super().__repr_args__()

        def __eq__(self, other: Any) -> bool:
            for value in (self, other):
                if isinstance(value, _FamilyDocumentAdapterBase):
                    synchronize_adapter(value)
            return super().__eq__(other)

        def __deepcopy__(self, memo: dict[int, Any] | None = None) -> BaseModel:
            if memo is None:
                memo = {}
            existing = memo.get(id(self))
            if isinstance(existing, type(self)):
                return existing
            placeholder = object.__new__(type(self))
            memo[id(self)] = placeholder
            body = object.__getattribute__(
                self,
                "_FamilyDocumentAdapterBase__document_body",
            )
            copied = type(self)._from_document_body(deepcopy(body, memo))
            metadata_root = metadata_path if isinstance(metadata_path, str) else metadata_path[0]
            if metadata_root in self.__dict__:
                copied.__dict__[metadata_root] = deepcopy(
                    self.__dict__[metadata_root],
                    memo,
                )
            _FamilyDocumentAdapterBase._replace_document_adapter_state(
                placeholder,
                copied,
            )
            return placeholder

    fields: dict[str, Any] = {
        name: (field_info.annotation, _copy_document_field_info(field_info))
        for name, field_info in body_model.model_fields.items()
    }
    _add_family_metadata_field(family, label, fields)
    adapter = create_model(
        _generated_model_name(family.model, family.name, label),
        __base__=_FamilyDocumentAdapterBase,
        __module__=family.model.__module__,
        **fields,
    )
    adapter.__pydantic_computed_fields__ = dict(  # noqa: V101 - Pydantic reads this
        body_model.model_computed_fields,
    )
    _validate_object_schema(family, projection, adapter, mode="validation")
    _validate_object_schema(family, projection, adapter, mode="serialization")
    return adapter


def _model_revalidation_proxy(
    instance: BaseModel,
    target_type: type[BaseModel],
) -> BaseModel:
    proxy = object.__new__(target_type)
    object.__setattr__(proxy, "__dict__", dict(instance.__dict__))
    extras = instance.__pydantic_extra__
    object.__setattr__(
        proxy,
        "__pydantic_extra__",
        None if extras is None else dict(extras),
    )
    object.__setattr__(
        proxy,
        "__pydantic_fields_set__",
        set(instance.__pydantic_fields_set__),
    )
    private = instance.__pydantic_private__
    object.__setattr__(
        proxy,
        "__pydantic_private__",
        None if private is None else dict(private),
    )
    standard_slots = {
        "__dict__",
        "__pydantic_extra__",
        "__pydantic_fields_set__",
        "__pydantic_private__",
    }
    for owner in type(instance).__mro__:
        for name, descriptor in vars(owner).items():
            if name in standard_slots or not isinstance(descriptor, MemberDescriptorType):
                continue
            try:
                slot_value = descriptor.__get__(instance, type(instance))
            except AttributeError:
                continue
            descriptor.__set__(proxy, slot_value)
    return proxy


def _copy_document_field_info(field_info: Any) -> Any:
    copied = copy(field_info)
    if any(
        alias is not None
        for alias in (
            field_info.alias,
            field_info.validation_alias,
            field_info.serialization_alias,
        )
    ):
        copied.alias_priority = 2  # noqa: V101 - Pydantic reads copied metadata
    return copied


def _validate_family_document_adapter_schema_hooks(
    family: SchemaFamily[Any],
    projection: _VersionProjection,
    body_model: type[BaseModel],
) -> None:
    for hook in _CUSTOM_MODEL_HOOKS:
        owner = _first_defining_class(body_model, hook)
        if owner is not None and owner is not BaseModel:
            _raise_projection_unsupported(
                family,
                projection,
                f"family-owned metadata cannot safely compose custom model hook {hook} "
                "from an explicit wire body",
            )


def _validate_family_document_adapter_member_collisions(
    family: SchemaFamily[Any],
    projection: _VersionProjection,
    body_model: type[BaseModel],
) -> None:
    reserved = set(vars(BaseModel)) | {
        _DOCUMENT_BODY_SLOT,
        "_document_body_model",
        "_from_document_body",
        "_replace_document_adapter_state",
    }
    for kind, names in (
        ("private attribute", body_model.__private_attributes__),
        ("computed field", body_model.model_computed_fields),
    ):
        collision = next((name for name in names if name in reserved), None)
        if collision is None:
            continue
        _raise_projection_unsupported(
            family,
            projection,
            f"explicit wire body {kind} {collision!r} conflicts with the "
            "family-owned document adapter API",
        )


def _reject_explicit_body_document_metadata(
    body: BaseModel,
    *,
    metadata_path: str | tuple[str, ...],
    family_name: str,
) -> None:
    extras = body.__pydantic_extra__
    if not isinstance(extras, Mapping):
        return
    path = (metadata_path,) if isinstance(metadata_path, str) else metadata_path
    if path[0] not in extras:
        return
    msg = (
        f"Explicit wire body for family {family_name!r} contains the reserved "
        f"family-owned metadata root {path[0]!r}"
    )
    raise ValueError(msg)


def _validate_document_metadata_attributes(
    value: Any,
    *,
    metadata_path: str | tuple[str, ...],
    expected: str,
    family_name: str,
) -> None:
    path = (metadata_path,) if isinstance(metadata_path, str) else metadata_path
    try:
        current = getattr(value, path[0])
    except AttributeError:
        return
    except Exception as exc:
        msg = (
            f"Version metadata for family {family_name!r} could not be read "
            f"from attribute path component {path[0]!r}"
        )
        raise ValueError(msg) from exc
    for part in path[1:]:
        current_mapping = _document_metadata_mapping(current)
        if current_mapping is None:
            try:
                namespace = object.__getattribute__(current, "__dict__")
            except (AttributeError, TypeError):
                namespace = None
            if isinstance(namespace, Mapping):
                current_mapping = namespace
        if current_mapping is None or set(current_mapping) != {part}:
            msg = (
                f"Version metadata for family {family_name!r} reserves the entire "
                f"root {path[0]!r}; the complete metadata path is required without siblings"
            )
            raise ValueError(msg)
        current = current_mapping[part]
    if current != expected:
        msg = f"Version metadata for family {family_name!r} is {current!r}; expected {expected!r}"
        raise ValueError(msg)


def _copy_without_document_metadata(
    value: Mapping[Any, Any],
    *,
    metadata_path: str | tuple[str, ...],
    expected: str,
    family_name: str,
) -> dict[Any, Any]:
    path = (metadata_path,) if isinstance(metadata_path, str) else metadata_path
    copied: dict[Any, Any] = dict(value)
    root = path[0]
    if root not in copied:
        return copied
    if len(path) == 1:
        declared = copied.pop(root)
        if declared != expected:
            msg = (
                f"Version metadata for family {family_name!r} is {declared!r}; "
                f"expected {expected!r}"
            )
            raise ValueError(msg)
        return copied

    current: Any = copied[root]
    for part in path[1:-1]:
        current_mapping = _document_metadata_mapping(current)
        if current_mapping is None:
            msg = (
                f"Version metadata for family {family_name!r} has a non-object "
                f"component at {part!r}"
            )
            raise ValueError(msg)
        if set(current_mapping) != {part}:
            msg = (
                f"Version metadata for family {family_name!r} reserves the entire "
                f"root {root!r}; sibling data cannot share that envelope"
            )
            raise ValueError(msg)
        current = current_mapping[part]
    final = path[-1]
    current_mapping = _document_metadata_mapping(current)
    if current_mapping is None:
        msg = f"Version metadata for family {family_name!r} has a non-object component at {final!r}"
        raise ValueError(msg)
    if set(current_mapping) != {final}:
        msg = (
            f"Version metadata for family {family_name!r} reserves the entire "
            f"root {root!r}; the complete metadata path is required without siblings"
        )
        raise ValueError(msg)
    declared = current_mapping[final]
    if declared != expected:
        msg = f"Version metadata for family {family_name!r} is {declared!r}; expected {expected!r}"
        raise ValueError(msg)
    copied.pop(root)
    return copied


def _copy_with_document_metadata(
    payload: Mapping[Any, Any],
    *,
    metadata_path: str | tuple[str, ...],
    metadata_value: str,
    family_name: str,
) -> dict[Any, Any]:
    path = (metadata_path,) if isinstance(metadata_path, str) else metadata_path
    copied: dict[Any, Any] = dict(payload)
    current = copied
    for part in path[:-1]:
        existing = current.get(part, _MISSING)
        if existing is _MISSING:
            child: dict[Any, Any] = {}
        else:
            msg = (
                f"Explicit wire serializer for family {family_name!r} conflicts with "
                f"version metadata at reserved path component {part!r}"
            )
            raise ValueError(msg)
        current[part] = child
        current = child
    final = path[-1]
    existing = current.get(final, _MISSING)
    if existing is not _MISSING:
        msg = (
            f"Explicit wire serializer for family {family_name!r} emitted conflicting "
            f"version metadata {existing!r}; the family-owned path is reserved"
        )
        raise ValueError(msg)
    current[final] = metadata_value
    return copied


def _document_metadata_mapping(value: Any) -> Mapping[Any, Any] | None:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, BaseModel):
        return None
    mapped = {
        name: value.__dict__[name] for name in type(value).model_fields if name in value.__dict__
    }
    extras = value.__pydantic_extra__
    if isinstance(extras, Mapping):
        mapped.update(extras)
    return mapped


def _add_document_metadata_json_schema(
    schema: dict[str, Any],
    *,
    metadata_path: str | tuple[str, ...],
    label: str,
) -> None:
    path = (metadata_path,) if isinstance(metadata_path, str) else metadata_path
    current = schema
    for index, part in enumerate(path):
        current.setdefault("type", "object")
        properties = current.setdefault("properties", {})
        if not isinstance(properties, dict):
            msg = "Family-owned document metadata requires object-shaped JSON Schema properties"
            raise SchemaCompilationError(msg)
        if index == len(path) - 1:
            properties[part] = {
                "const": label,
                "default": label,
                "title": part.replace("_", " ").title(),
                "type": "string",
            }
            return
        existing_child = properties.get(part)
        child: dict[str, Any]
        if isinstance(existing_child, dict):
            child = cast(dict[str, Any], existing_child)
        else:
            child = {"type": "object", "properties": {}}
            properties[part] = child
        child["additionalProperties"] = False
        child["required"] = [path[index + 1]]
        current = child


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
    nested_projection_cache: dict[tuple[int, tuple[str, ...], str, bool], type[BaseModel] | None],
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
                field_name=field_name,
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
    field_name: str,
    field_path: tuple[str, ...],
    nested: tuple[_CompiledNestedFamily, ...],
    decorator_nested: tuple[_CompiledDecoratorNestedFamily, ...],
    nested_projection_cache: dict[tuple[int, tuple[str, ...], str, bool], type[BaseModel] | None],
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
