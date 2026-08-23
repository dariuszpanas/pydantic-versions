from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from copy import copy, deepcopy
from dataclasses import is_dataclass
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
    is_typeddict,
)
from typing import (
    TypeAliasType as StdlibTypeAliasType,
)

from annotated_types import GroupedMetadata, Not, Predicate
from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    GetCoreSchemaHandler,
    GetJsonSchemaHandler,
    GetPydanticSchema,
    WithJsonSchema,
    create_model,
)
from pydantic.functional_serializers import PlainSerializer, SerializeAsAny, WrapSerializer
from pydantic.functional_validators import (
    AfterValidator,
    BeforeValidator,
    PlainValidator,
    WrapValidator,
)
from pydantic_core import CoreSchema, PydanticUndefined, core_schema
from typing_extensions import TypeAliasType as ExtensionsTypeAliasType  # noqa: UP035

from pydantic_versions._compiler import (
    _CompiledDecoratorNestedFamily,
    _CompiledNestedFamily,
    _DecoratorTraversalStep,
    _generated_model_name,
    _identifier_component,
    _NestedCollectionKind,
    _stable_digest,
    _VersionProjection,
)
from pydantic_versions.exceptions import SchemaCompilationError, UnsupportedWireModelError

if TYPE_CHECKING:
    from pydantic_versions.family import SchemaFamily


_TYPE_ALIAS_TYPES = (StdlibTypeAliasType, ExtensionsTypeAliasType)
_SCHEMA_HOOK_NAMES = (
    "__get_pydantic_core_schema__",
    "__get_pydantic_json_schema__",
    "__get_validators__",
    "__modify_schema__",
)
_MISSING = object()
_DOCUMENT_BODY_SLOT = "_FamilyDocumentAdapterBase__document_body"
_SERIALIZE_AS_ANY_METADATA_TYPE = type(cast(Any, SerializeAsAny)())

_MODEL_SCHEMA_STRUCTURE_KEYS = frozenset(
    {
        "$defs",
        "$dynamicRef",
        "$ref",
        "additionalProperties",
        "allOf",
        "anyOf",
        "const",
        "contains",
        "dependentRequired",
        "dependentSchemas",
        "discriminator",
        "else",
        "enum",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "if",
        "items",
        "maxContains",
        "maxItems",
        "maxLength",
        "maxProperties",
        "maximum",
        "minContains",
        "minItems",
        "minLength",
        "minProperties",
        "minimum",
        "multipleOf",
        "not",
        "oneOf",
        "pattern",
        "patternProperties",
        "prefixItems",
        "properties",
        "propertyNames",
        "required",
        "then",
        "type",
        "unevaluatedProperties",
        "uniqueItems",
    }
)


_WIRE_CONFIG_KEYS = frozenset(
    {
        "alias_generator",
        "allow_inf_nan",
        "coerce_numbers_to_str",
        "extra",
        "json_schema_mode_override",
        "json_schema_serialization_defaults_required",
        "loc_by_alias",
        "populate_by_name",
        "regex_engine",
        "ser_json_bytes",
        "ser_json_inf_nan",
        "ser_json_temporal",
        "ser_json_timedelta",
        "serialize_by_alias",
        "str_max_length",
        "str_min_length",
        "str_strip_whitespace",
        "str_to_lower",
        "str_to_upper",
        "strict",
        "title",
        "url_preserve_empty_path",
        "use_enum_values",
        "val_json_bytes",
        "val_temporal_unit",
        "validate_by_alias",
        "validate_by_name",
        "validate_default",
    }
)
_DROPPED_CONFIG_KEYS = frozenset(
    {
        "cache_strings",
        "defer_build",
        "from_attributes",
        "frozen",
        "hide_input_in_errors",
        "ignored_types",
        "protected_namespaces",
        "revalidate_instances",
        "use_attribute_docstrings",
        "validate_assignment",
        "validate_return",
        "validation_error_cause",
    }
)
_REJECTED_CONFIG_KEYS = frozenset(
    {
        "arbitrary_types_allowed",
        "field_title_generator",
        "json_encoders",
        "model_title_generator",
        "plugin_settings",
        "polymorphic_serialization",
        "schema_generator",
    }
)
_KNOWN_CONFIG_KEYS = (
    _WIRE_CONFIG_KEYS | _DROPPED_CONFIG_KEYS | _REJECTED_CONFIG_KEYS | {"json_schema_extra"}
)

_WIRE_FIELD_ATTRIBUTES = frozenset(
    {
        "alias",
        "alias_priority",
        "default",
        "default_factory",
        "deprecated",
        "description",
        "discriminator",
        "examples",
        "json_schema_extra",
        "serialization_alias",
        "title",
        "validate_default",
        "validation_alias",
    }
)
_DROPPED_FIELD_ATTRIBUTES = frozenset({"frozen", "init", "init_var", "kw_only", "repr"})
_OMITTED_FIELD_ATTRIBUTES = frozenset({"exclude", "exclude_if"})
_REJECTED_FIELD_ATTRIBUTES = frozenset({"field_title_generator"})
_FUNCTIONAL_FIELD_BEHAVIOR = (
    AfterValidator,
    BeforeValidator,
    PlainSerializer,
    PlainValidator,
    _SERIALIZE_AS_ANY_METADATA_TYPE,
    WrapSerializer,
    WrapValidator,
)
_CUSTOM_MODEL_HOOKS = (
    "__get_pydantic_core_schema__",
    "__get_pydantic_json_schema__",
    "model_json_schema",
)
_HASHABLE_MODEL_CACHE: dict[type[BaseModel], type[BaseModel]] = {}


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
    metadata = family.version_metadata
    if metadata is None or metadata.owner != "family":
        msg = f"Schema family {family.name!r} lost family-owned version metadata"
        raise SchemaCompilationError(msg)
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
        if not isinstance(body, body_model):
            msg = (
                f"Family-owned document adapter for {family.name!r} lost its "
                "validated explicit wire body"
            )
            raise ValueError(msg)
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

        def __init__(self, /, **data: Any) -> None:
            validated = type(self).model_validate(data)
            _FamilyDocumentAdapterBase._replace_document_adapter_state(self, validated)

        def __getattribute__(self, name: str) -> Any:
            if name in body_fields:
                try:
                    body = object.__getattribute__(
                        self,
                        "_FamilyDocumentAdapterBase__document_body",
                    )
                except AttributeError:
                    body = None
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
            if not isinstance(body, body_model):
                msg = (
                    f"Family-owned document adapter for {family.name!r} lost its "
                    "validated explicit wire body"
                )
                raise ValueError(msg)
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
            if not isinstance(body, body_model):
                msg = (
                    f"Family-owned document adapter for {family.name!r} lost its "
                    "validated explicit wire body"
                )
                raise ValueError(msg)
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
    adapter.__pydantic_computed_fields__ = dict(body_model.model_computed_fields)
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
        copied.alias_priority = 2
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


def _validate_explicit_wire_model_metadata(
    family: SchemaFamily[Any],
    projection: _VersionProjection,
    wire_model: type[BaseModel],
) -> None:
    metadata = family.version_metadata
    if metadata is None or metadata.owner != "model":
        return

    metadata_field = cast(str, _model_metadata_field(family))
    if metadata_field not in wire_model.model_fields:
        _raise_projection_unsupported(
            family,
            projection,
            "explicit wire model for model-owned metadata must declare the same "
            f"model metadata field {metadata_field!r}",
        )

    field_info = wire_model.model_fields[metadata_field]
    model_label_type = _literal_type(projection.label)
    model_field_type = field_info.annotation

    if model_field_type != model_label_type:
        msg = (
            "explicit wire model for model-owned metadata must annotate "
            f"field {metadata_field!r} as {model_label_type!r}"
        )
        _raise_projection_unsupported(
            family,
            projection,
            msg,
        )
    if field_info.default is PydanticUndefined or field_info.default != projection.label:
        _raise_projection_unsupported(
            family,
            projection,
            "explicit wire model for model-owned metadata must provide "
            f"the exact default {projection.label!r}",
        )


def _build_model_for_projection_unchecked(
    family: SchemaFamily[Any],
    projection: _VersionProjection,
    nested: tuple[_CompiledNestedFamily, ...] = (),
    decorator_nested: tuple[_CompiledDecoratorNestedFamily, ...] = (),
) -> type[BaseModel]:
    model_metadata_field = _model_metadata_field(family)
    used_nested: set[tuple[str, ...]] = set()
    nested_projection_cache: dict[
        tuple[int, tuple[str, ...], str, bool], type[BaseModel] | None
    ] = {}
    nested_projection_stack: set[tuple[int, tuple[str, ...], str, bool]] = set()
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
        annotation = _rewrite_annotation(
            field_dict["annotation"],
            projection.label,
            family,
            nested=nested,
            decorator_nested=decorator_nested,
            field_path=(compiled_field.current_name,),
            used_nested=used_nested,
            field_name=compiled_field.current_name,
            allow_child_projection=True,
            nested_projection_cache=nested_projection_cache,
            nested_projection_stack=nested_projection_stack,
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

        metadata = (
            ()
            if compiled_field.current_name == model_metadata_field
            else _wire_field_metadata(
                family,
                compiled_field.current_name,
                field_dict["metadata"],
            )
        )
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


def _wire_model_config(family: SchemaFamily[Any]) -> ConfigDict:
    config: dict[str, Any] = {
        key: value for key, value in family.model.model_config.items() if key in _WIRE_CONFIG_KEYS
    }
    schema_extra = family.model.model_config.get("json_schema_extra")
    if isinstance(schema_extra, Mapping):
        config["json_schema_extra"] = _safe_deepcopy(
            family,
            dict(schema_extra),
            detail="model JSON Schema metadata",
        )
    return ConfigDict(**config)


def _factory_takes_validated_data(field_info: Any, patched_default: Any) -> bool:
    if patched_default is None:
        return bool(field_info.default_factory_takes_validated_data)
    if patched_default.has_default:
        return False
    patched_field = Field(default_factory=patched_default.default_factory)
    return bool(patched_field.default_factory_takes_validated_data)


def _validate_model_config(family: SchemaFamily[Any]) -> None:
    config = family.model.model_config
    if any(not isinstance(key, str) for key in config):
        _raise_unsupported(family, "model configuration keys must be strings")
    unknown = sorted(set(config) - _KNOWN_CONFIG_KEYS)
    if unknown:
        _raise_unsupported(
            family,
            f"unsupported model configuration keys are set: {', '.join(unknown)}",
        )

    for key in sorted(_REJECTED_CONFIG_KEYS & set(config)):
        if _has_effect(config[key]):
            _raise_unsupported(family, f"model configuration {key!r} is not wire-declarative")

    schema_extra = config.get("json_schema_extra")
    if schema_extra is not None and not isinstance(schema_extra, Mapping):
        _raise_unsupported(
            family,
            "callable model JSON Schema mutation cannot be projected automatically",
        )
    if isinstance(schema_extra, Mapping):
        structural_keys = sorted(_MODEL_SCHEMA_STRUCTURE_KEYS & set(schema_extra))
        if structural_keys:
            _raise_unsupported(
                family,
                "model JSON Schema metadata cannot override generated structure: "
                f"{', '.join(structural_keys)}",
            )


def _validate_typed_extras(family: SchemaFamily[Any]) -> None:
    if family.model.model_config.get("extra") != "allow":
        return
    for owner in family.model.__mro__:
        if owner is BaseModel:
            continue
        annotations = owner.__dict__.get("__annotations__", {})
        if "__pydantic_extra__" in annotations:
            _raise_unsupported(
                family,
                "typed extra values cannot be projected automatically",
            )


def _wire_field_attributes(
    family: SchemaFamily[Any],
    field_name: str,
    source: Mapping[str, Any],
) -> dict[str, Any]:
    unknown = sorted(
        set(source)
        - _WIRE_FIELD_ATTRIBUTES
        - _DROPPED_FIELD_ATTRIBUTES
        - _OMITTED_FIELD_ATTRIBUTES
        - _REJECTED_FIELD_ATTRIBUTES
    )
    if unknown:
        _raise_unsupported(
            family,
            f"field {field_name!r} uses unsupported attributes: {', '.join(unknown)}",
        )
    for key in _REJECTED_FIELD_ATTRIBUTES:
        if key in source and _has_effect(source[key]):
            _raise_unsupported(
                family,
                f"field {field_name!r} uses non-declarative attribute {key!r}",
            )

    discriminator = source.get("discriminator")
    discriminator_value = getattr(discriminator, "discriminator", discriminator)
    if isinstance(discriminator, Discriminator) and type(discriminator) is not Discriminator:
        _raise_unsupported(
            family,
            f"field {field_name!r} uses a custom discriminator subtype",
        )
    if discriminator is not None and not isinstance(discriminator_value, str):
        _raise_unsupported(
            family,
            f"field {field_name!r} uses a callable discriminator",
        )

    schema_extra = source.get("json_schema_extra")
    if schema_extra is not None and not isinstance(schema_extra, Mapping):
        _raise_unsupported(
            family,
            f"field {field_name!r} uses callable JSON Schema mutation",
        )

    attributes: dict[str, Any] = {}
    for key in _WIRE_FIELD_ATTRIBUTES:
        value = source[key]
        if key == "default_factory" or value is PydanticUndefined:
            attributes[key] = value
        else:
            attributes[key] = _safe_deepcopy(
                family,
                value,
                detail=f"attribute {key!r} for field {field_name!r}",
            )
    return attributes


def _wire_field_metadata(
    family: SchemaFamily[Any],
    field_name: str,
    source: list[Any],
) -> tuple[Any, ...]:
    return _snapshot_wire_metadata(
        family,
        field_name,
        source,
        detail="metadata",
    )


def _snapshot_wire_metadata(
    family: SchemaFamily[Any],
    field_name: str,
    source: Iterable[Any],
    *,
    detail: str,
) -> tuple[Any, ...]:
    snapshot: list[Any] = []
    for item in source:
        if isinstance(item, _FUNCTIONAL_FIELD_BEHAVIOR):
            continue
        if isinstance(item, Predicate | Not):
            _raise_unsupported(
                family,
                f"field {field_name!r} uses callable predicate metadata",
            )
        if isinstance(item, GroupedMetadata) and not _is_trusted_declarative_type(
            type(item),
            include_annotated_types=True,
        ):
            _raise_unsupported(
                family,
                f"field {field_name!r} uses custom executable grouped metadata",
            )
        if isinstance(item, Discriminator):
            if type(item) is not Discriminator:
                _raise_unsupported(
                    family,
                    f"field {field_name!r} uses a custom discriminator subtype",
                )
            if not isinstance(item.discriminator, str):
                _raise_unsupported(
                    family,
                    f"field {field_name!r} uses a callable discriminator",
                )
        elif isinstance(item, WithJsonSchema):
            if type(item) is not WithJsonSchema:
                _raise_unsupported(
                    family,
                    f"field {field_name!r} uses a custom schema metadata subtype",
                )
        elif isinstance(item, GetPydanticSchema) or _has_schema_hook(item):
            _raise_unsupported(
                family,
                f"field {field_name!r} uses custom schema or validation metadata",
            )
        snapshot.append(
            _safe_deepcopy(
                family,
                item,
                detail=f"{detail} for field {field_name!r}",
            )
        )
    return tuple(snapshot)


def _protocol_owners(item: Any) -> tuple[type[Any], ...]:
    metaclass = type(item)
    owners = list(_static_mro(metaclass))
    if isinstance(item, type):
        owners.extend(_static_mro(item))
    deduped: list[type[Any]] = []
    for owner in owners:
        if owner not in deduped:
            deduped.append(owner)
    return tuple(deduped)


def _static_type_attr(owner: type[Any], name: str, default: Any = None) -> Any:
    try:
        return type.__getattribute__(owner, name)
    except AttributeError:
        return default


def _static_mro(owner: type[Any]) -> tuple[type[Any], ...]:
    mro = _static_type_attr(owner, "__mro__", ())
    return mro if isinstance(mro, tuple) else ()


def _instance_dict(item: Any) -> Mapping[str, Any]:
    try:
        if isinstance(item, type):
            return type.__getattribute__(item, "__dict__")
        return object.__getattribute__(item, "__dict__")
    except (AttributeError, TypeError):
        return {}


def _owner_has_dynamic_lookup(owner: type[Any]) -> bool:
    if owner in (type, object):
        return False
    owner_dict = _instance_dict(owner)
    return "__getattr__" in owner_dict or "__getattribute__" in owner_dict


def _owner_has_schema_hook(owner: type[Any]) -> bool:
    return any(name in _instance_dict(owner) for name in _SCHEMA_HOOK_NAMES)


def _is_exact_module_member(owner: type[Any], *, module: str) -> bool:
    module_name = _static_type_attr(owner, "__module__", None)
    if not isinstance(module_name, str):
        return False
    if module_name != module and not module_name.startswith(module + "."):
        return False
    qualname = _static_type_attr(owner, "__qualname__", "")
    if not qualname or qualname == "<locals>":
        return False
    current: Any = sys.modules.get(module_name)
    if current is None:
        return False
    for component in qualname.split("."):
        if component == "<locals>":
            return False
        namespace = _instance_dict(current) if isinstance(current, type) else vars(current)
        if not isinstance(namespace, Mapping):
            return False
        value = namespace.get(component, _MISSING)
        if value is _MISSING:
            return False
        current = value
    return current is owner


def _is_typing_reflection_owner(owner: type[Any]) -> bool:
    module_name = _static_type_attr(owner, "__module__", "")
    if not isinstance(module_name, str):
        return False
    if module_name in (
        "typing",
        "typing_extensions",
        "types",
        "collections.abc",
        "builtins",
    ):
        return _is_exact_module_member(owner, module=module_name)
    if module_name.startswith(("typing.", "typing_extensions.", "collections.abc.")):
        return _is_exact_module_member(owner, module=module_name.split(".")[0])
    return False


def _has_schema_hook(item: Any) -> bool:
    if any(name in _instance_dict(item) for name in _SCHEMA_HOOK_NAMES):
        return True
    return any(
        _owner_has_dynamic_lookup(owner) or _owner_has_schema_hook(owner)
        for owner in _protocol_owners(item)
        if not _is_typing_reflection_owner(owner)
    )


def _has_custom_annotation_schema_hook(annotation: Any) -> bool:
    if not isinstance(annotation, type):
        return _has_schema_hook(annotation)
    protocol_owners = tuple(
        owner
        for owner in _protocol_owners(annotation)
        if _owner_has_dynamic_lookup(owner) or _owner_has_schema_hook(owner)
    )
    if not protocol_owners:
        return False
    if issubclass(annotation, BaseModel):
        return any(
            owner is not BaseModel and not _is_trusted_declarative_type(owner)
            for owner in protocol_owners
        )
    return any(
        not (
            _is_trusted_declarative_type(owner, include_annotated_types=True)
            or _is_typing_reflection_owner(owner)
        )
        for owner in protocol_owners
    )


def _is_trusted_declarative_type(
    owner: type[Any],
    *,
    include_annotated_types: bool = False,
) -> bool:
    module_name = _static_type_attr(owner, "__module__", "")
    prefixes = ("pydantic.", "pydantic_core")
    if include_annotated_types:
        prefixes = (*prefixes, "annotated_types")
    if not (module_name == "pydantic" or module_name.startswith(prefixes)):
        return False
    return _is_exact_module_member(owner, module=module_name)


def _decorators_have_behavior(annotation: Any) -> bool:
    decorators = _instance_dict(annotation).get("__pydantic_decorators__", None)
    if decorators is None:
        return False
    for name in (
        "field_serializers",
        "field_validators",
        "model_serializers",
        "model_validators",
        "root_validators",
        "validators",
    ):
        try:
            if object.__getattribute__(decorators, name):
                return True
        except AttributeError:
            continue
    return False


def _is_structured_annotation(annotation: Any) -> bool:
    if not isinstance(annotation, type):
        return False
    if _mro_defines_attribute(annotation, "__dataclass_fields__"):
        return is_dataclass(annotation)
    if _mro_defines_attribute(annotation, "__required_keys__"):
        return _mro_defines_attribute(annotation, "__optional_keys__") and _mro_defines_attribute(
            annotation, "__total__"
        )
    return issubclass(annotation, tuple) and _mro_defines_attribute(annotation, "_fields")


def _mro_defines_attribute(item: Any, attribute: str) -> bool:
    if not isinstance(item, type):
        return False
    return any(attribute in _instance_dict(owner) for owner in _static_mro(item))


def _mro_annotations(item: type[Any]) -> Iterable[tuple[str, Any]]:
    if not isinstance(item, type):
        return ()
    return tuple(
        (name, value)
        for owner in _static_mro(item)
        for name, value in _owner_annotations(owner).items()
    )


def _owner_annotations(owner: type[Any]) -> Mapping[str, Any]:
    module_name = _static_type_attr(owner, "__module__", "")
    module = sys.modules.get(module_name) if isinstance(module_name, str) else None
    globals_dict = dict(_instance_dict(module) if module is not None else {})
    builtins_object = globals_dict.get("__builtins__")
    if not isinstance(builtins_object, Mapping):
        globals_dict["__builtins__"] = vars(__import__("builtins"))
    elif not isinstance(builtins_object, dict):
        globals_dict["__builtins__"] = vars(builtins_object)

    annotations = _instance_dict(owner).get("__annotations__")
    if not isinstance(annotations, Mapping):
        annotations = getattr(owner, "__annotations__", {})
        if not isinstance(annotations, Mapping):
            return {}

    resolved = {}
    localns = dict(_instance_dict(owner))
    for name, value in annotations.items():
        if not isinstance(value, str):
            resolved[name] = value
            continue
        try:
            resolved[name] = _evaluate_owner_forward_ref(
                value,
                globals_dict=globals_dict,
                localns=localns,
            )
        except (AttributeError, NameError, SyntaxError, TypeError):
            resolved[name] = value
    return resolved


def _evaluate_owner_forward_ref(
    value: str,
    *,
    globals_dict: dict[str, Any],
    localns: dict[str, Any],
) -> Any:
    reference = ForwardRef(value)
    evaluate = getattr(reference, "evaluate", None)
    if evaluate is not None:
        return evaluate(globals=globals_dict, locals=localns)
    return reference._evaluate(
        globalns=globals_dict,
        localns=localns,
        recursive_guard=frozenset(),
    )


def _has_behavioral_structured_annotation(
    annotation: Any,
    *,
    seen: set[int] | None = None,
) -> bool:
    if not _is_structured_annotation(annotation):
        return False
    visited = set() if seen is None else seen
    if id(annotation) in visited:
        return False
    visited.add(id(annotation))
    if _mro_defines_attribute(annotation, "__post_init__"):
        return True
    if _decorators_have_behavior(annotation):
        return True
    return any(
        _annotation_contains_runtime_behavior(value, seen=visited)
        for value in (value for _, value in _mro_annotations(annotation))
    )


def _annotation_contains_runtime_behavior(annotation: Any, *, seen: set[int]) -> bool:
    if id(annotation) in seen:
        return False
    if _is_structured_annotation(annotation):
        return _has_behavioral_structured_annotation(annotation, seen=seen)
    seen.add(id(annotation))
    if isinstance(annotation, str | ForwardRef):
        return True
    if isinstance(annotation, _TYPE_ALIAS_TYPES):
        return _annotation_contains_runtime_behavior(annotation.__value__, seen=seen) or any(
            _annotation_contains_runtime_behavior(value, seen=seen)
            for parameter in annotation.__type_params__
            for value in _type_parameter_values(parameter)
        )
    if isinstance(annotation, TypeVar):
        return any(
            _annotation_contains_runtime_behavior(value, seen=seen)
            for value in _type_parameter_values(annotation)
        )
    supertype = _instance_dict(annotation).get("__supertype__")
    if supertype is not None and supertype is not annotation:
        return _annotation_contains_runtime_behavior(supertype, seen=seen)
    if _has_custom_annotation_schema_hook(annotation):
        return True
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if _decorators_have_behavior(annotation):
            return True

    origin = get_origin(annotation)
    if origin is Literal:
        return False
    if origin is not None and _has_custom_annotation_schema_hook(origin):
        return True
    if origin is Annotated:
        base, *metadata = get_args(annotation)
        for item in metadata:
            if isinstance(item, (*_FUNCTIONAL_FIELD_BEHAVIOR, Predicate, Not)):
                return True
            if isinstance(item, Discriminator) and (
                type(item) is not Discriminator or not isinstance(item.discriminator, str)
            ):
                return True
            if isinstance(item, WithJsonSchema) and type(item) is not WithJsonSchema:
                return True
            if isinstance(item, GroupedMetadata) and not _is_trusted_declarative_type(
                type(item),
                include_annotated_types=True,
            ):
                return True
            if isinstance(item, GetPydanticSchema) or _has_schema_hook(item):
                return True
        return _annotation_contains_runtime_behavior(base, seen=seen)
    return any(
        _annotation_contains_runtime_behavior(argument, seen=seen)
        for argument in get_args(annotation)
    )


def _type_parameter_values(parameter: Any) -> tuple[Any, ...]:
    values: list[Any] = []
    bound = getattr(parameter, "__bound__", None)
    if bound is not None:
        values.append(bound)
    values.extend(getattr(parameter, "__constraints__", ()))
    default = getattr(parameter, "__default__", None)
    default_type = type(default)
    if default is not None and not (
        default_type.__module__ in ("typing", "typing_extensions")
        and "NoDefault" in default_type.__name__
    ):
        values.append(default)
    return tuple(values)


def _validate_annotation_behavior(
    family: SchemaFamily[Any],
    field_name: str,
    annotation: Any,
    *,
    hidden_in_alias: bool = False,
) -> None:
    if _has_custom_annotation_schema_hook(annotation):
        location = " hidden in a type alias" if hidden_in_alias else ""
        _raise_unsupported(
            family,
            f"field {field_name!r} uses a custom annotation schema hook{location}",
        )
    if _has_behavioral_structured_annotation(annotation):
        location = " hidden in a type alias" if hidden_in_alias else ""
        _raise_unsupported(
            family,
            f"field {field_name!r} uses a behavioral structured annotation{location}",
        )
    if isinstance(annotation, TypeVar) and any(
        _annotation_contains_runtime_behavior(value, seen=set())
        for value in _type_parameter_values(annotation)
    ):
        location = " hidden in a type alias" if hidden_in_alias else ""
        _raise_unsupported(
            family,
            f"field {field_name!r} uses a behavioral type parameter{location}",
        )
    supertype = _instance_dict(annotation).get("__supertype__")
    if supertype is not None and supertype is not annotation:
        if _annotation_contains_runtime_behavior(supertype, seen=set()):
            location = " hidden in a type alias" if hidden_in_alias else ""
            _raise_unsupported(
                family,
                f"field {field_name!r} uses a behavioral NewType target{location}",
            )


def _model_metadata_field(family: SchemaFamily[Any]) -> str | None:
    metadata = family.version_metadata
    if metadata is None or metadata.owner != "model":
        return None
    if not isinstance(metadata.path, str):
        _raise_unsupported(
            family,
            "nested model-owned version metadata requires the top-level conversion compiler",
        )

    matches = tuple(
        field_name
        for field_name, field_info in family.model.model_fields.items()
        if metadata.path
        in (
            field_name,
            field_info.alias,
            field_info.validation_alias,
            field_info.serialization_alias,
        )
    )
    if len(matches) != 1:
        _raise_unsupported(
            family,
            "model-owned version metadata must resolve to exactly one direct field or alias",
        )
    field_name = matches[0]
    field_info = family.model.model_fields[field_name]
    config = family.model.model_config
    if metadata.path == field_name:
        accepted_by_name = field_info.validation_alias is None or config.get(
            "validate_by_name", False
        )
        accepted_by_alias = (
            field_info.validation_alias == metadata.path
            and config.get("validate_by_alias", True) is not False
        )
        if not (accepted_by_name or accepted_by_alias):
            _raise_unsupported(
                family,
                "model-owned version metadata uses a field name disabled for validation",
            )
        return field_name

    validation_alias = field_info.validation_alias
    validation_path = (
        validation_alias
        if isinstance(validation_alias, str)
        else field_info.alias
        if validation_alias is None
        else None
    )
    if metadata.path != validation_path:
        _raise_unsupported(
            family,
            "model-owned version metadata must use an enabled direct validation location",
        )
    if config.get("validate_by_alias", True) is False:
        _raise_unsupported(
            family,
            "model-owned version metadata uses an alias disabled for validation",
        )
    return field_name


def _validate_family_metadata_collision(family: SchemaFamily[Any]) -> None:
    metadata = family.version_metadata
    if metadata is None or metadata.owner != "family":
        return
    root_name = metadata.path if isinstance(metadata.path, str) else metadata.path[0]
    for field_name, field_info in family.model.model_fields.items():
        attributes = field_info.asdict()["attributes"]
        if any(
            key in attributes and _has_effect(attributes[key]) for key in _OMITTED_FIELD_ATTRIBUTES
        ):
            continue
        if any(
            path and path[0] == root_name for path in _field_contract_paths(field_name, field_info)
        ):
            _raise_unsupported(
                family,
                f"family-owned version metadata collides with body field {field_name!r}",
            )


def _validate_explicit_family_metadata_collision(
    family: SchemaFamily[Any],
    projection: _VersionProjection,
    model: type[BaseModel],
) -> None:
    metadata = family.version_metadata
    if metadata is None or metadata.owner != "family":
        return
    root_name = metadata.path if isinstance(metadata.path, str) else metadata.path[0]
    for field_name, field_info in model.model_fields.items():
        if any(
            path and path[0] == root_name for path in _field_contract_paths(field_name, field_info)
        ):
            _raise_projection_unsupported(
                family,
                projection,
                f"family-owned version metadata collides with explicit wire field {field_name!r}",
            )
    for field_name, field_info in model.model_computed_fields.items():
        if any(
            path and path[0] == root_name
            for path in _computed_field_output_paths(field_name, field_info)
        ):
            _raise_projection_unsupported(
                family,
                projection,
                "family-owned version metadata collides with explicit wire computed "
                f"field {field_name!r}",
            )


def _validate_metadata_field_name_collision(
    family: SchemaFamily[Any],
    projection: _VersionProjection,
    fields: Mapping[str, Any],
) -> None:
    metadata = family.version_metadata
    if metadata is None or metadata.owner != "family":
        return
    root_name = metadata.path if isinstance(metadata.path, str) else metadata.path[0]
    if root_name in fields:
        _raise_projection_unsupported(
            family,
            projection,
            f"family-owned version metadata collides with projected field {root_name!r}",
        )


def _validate_generated_metadata_aliases(
    family: SchemaFamily[Any],
    projection: _VersionProjection,
    model: type[BaseModel],
    *,
    model_metadata_field: str | None,
) -> None:
    metadata = family.version_metadata
    if metadata is None:
        return
    metadata_root = metadata.path if isinstance(metadata.path, str) else metadata.path[0]
    family_metadata_field = metadata_root if metadata.owner == "family" else None
    reserved_roots = {metadata_root}
    if model_metadata_field is not None:
        metadata_field_info = model.model_fields[model_metadata_field]
        metadata_paths = (
            (model_metadata_field,),
            *_alias_paths(metadata_field_info.alias),
            *_alias_paths(metadata_field_info.validation_alias),
            *_alias_paths(metadata_field_info.serialization_alias),
        )
        reserved_roots.update(path[0] for path in metadata_paths if path)

    for field_name, field_info in model.model_fields.items():
        if field_name == family_metadata_field or field_name == model_metadata_field:
            continue
        paths = (
            (field_name,),
            *_alias_paths(field_info.alias),
            *_alias_paths(field_info.validation_alias),
            *_alias_paths(field_info.serialization_alias),
        )
        if any(path and path[0] in reserved_roots for path in paths):
            _raise_projection_unsupported(
                family,
                projection,
                f"version metadata overlaps projected field or alias {field_name!r}",
            )
    for field_name, field_info in model.model_computed_fields.items():
        if any(
            path and path[0] in reserved_roots
            for path in _computed_field_output_paths(field_name, field_info)
        ):
            _raise_projection_unsupported(
                family,
                projection,
                f"version metadata overlaps computed field or alias {field_name!r}",
            )


def _field_contract_paths(field_name: str, field_info: Any) -> tuple[tuple[str | int, ...], ...]:
    return (
        (field_name,),
        *_alias_paths(field_info.alias),
        *_alias_paths(field_info.validation_alias),
        *_alias_paths(field_info.serialization_alias),
    )


def _computed_field_output_paths(
    field_name: str,
    field_info: Any,
) -> tuple[tuple[str | int, ...], ...]:
    return ((field_name,), *_alias_paths(field_info.alias))


def _validate_unique_serialization_names(
    family: SchemaFamily[Any],
    projection: _VersionProjection,
    model: type[BaseModel],
) -> None:
    schema = model.__pydantic_core_schema__
    definitions = _core_schema_definitions(schema)
    root = schema.get("schema") if schema.get("type") == "definitions" else schema
    _validate_core_schema_serialization_names(
        family,
        projection,
        root,
        definitions=definitions,
        seen=set(),
    )


def _raise_duplicate_serialization_name(
    family: SchemaFamily[Any],
    projection: _VersionProjection,
    *,
    model: type[Any],
    output_name: str,
    first: str,
    second: str,
) -> None:
    _raise_projection_unsupported(
        family,
        projection,
        f"wire model {_model_display(model)!r} serializes {first} and {second} "
        f"to duplicate output name {output_name!r}",
    )


def _core_schema_definitions(schema: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    definitions = schema.get("definitions")
    if not isinstance(definitions, list):
        return {}
    return {
        reference: definition
        for definition in definitions
        if isinstance(definition, Mapping) and isinstance(reference := definition.get("ref"), str)
    }


def _validate_core_schema_serialization_names(
    family: SchemaFamily[Any],
    projection: _VersionProjection,
    schema: Any,
    *,
    definitions: Mapping[str, Mapping[str, Any]],
    seen: set[int],
    owner: type[Any] | None = None,
) -> None:
    if id(schema) in seen:
        return
    seen.add(id(schema))
    if isinstance(schema, list | tuple):
        for item in schema:
            _validate_core_schema_serialization_names(
                family,
                projection,
                item,
                definitions=definitions,
                seen=seen,
                owner=owner,
            )
        return
    if not isinstance(schema, Mapping):
        return
    serialization = schema.get("serialization")
    if isinstance(serialization, Mapping) and serialization.get(
        "when_used",
        "always",
    ) in ("always", "unless-none"):
        return

    schema_type = schema.get("type")
    if schema_type == "definition-ref":
        reference = schema.get("schema_ref")
        resolved = definitions.get(reference) if isinstance(reference, str) else None
        if resolved is not None:
            _validate_core_schema_serialization_names(
                family,
                projection,
                resolved,
                definitions=definitions,
                seen=seen,
                owner=owner,
            )
        return

    candidate_owner = schema.get("cls")
    if isinstance(candidate_owner, type):
        owner = candidate_owner
    if schema_type in ("model", "dataclass"):
        _validate_core_schema_serialization_names(
            family,
            projection,
            schema.get("schema"),
            definitions=definitions,
            seen=seen,
            owner=owner,
        )
        return

    if schema_type in ("model-fields", "typed-dict"):
        fields = schema.get("fields")
        if isinstance(fields, Mapping):
            entries = tuple(
                (name, field)
                for name, field in fields.items()
                if isinstance(name, str) and isinstance(field, Mapping)
            )
            _validate_core_schema_object_fields(
                family,
                projection,
                entries,
                computed=schema.get("computed_fields"),
                definitions=definitions,
                seen=seen,
                owner=owner,
            )
        return

    if schema_type == "dataclass-args":
        fields = schema.get("fields")
        if isinstance(fields, list):
            entries = tuple(
                (name, field)
                for field in fields
                if isinstance(field, Mapping) and isinstance(name := field.get("name"), str)
            )
            _validate_core_schema_object_fields(
                family,
                projection,
                entries,
                computed=schema.get("computed_fields"),
                definitions=definitions,
                seen=seen,
                owner=owner,
            )
        return

    for value in schema.values():
        _validate_core_schema_serialization_names(
            family,
            projection,
            value,
            definitions=definitions,
            seen=seen,
            owner=owner,
        )


def _validate_core_schema_object_fields(
    family: SchemaFamily[Any],
    projection: _VersionProjection,
    fields: tuple[tuple[str, Mapping[str, Any]], ...],
    *,
    computed: Any,
    definitions: Mapping[str, Mapping[str, Any]],
    seen: set[int],
    owner: type[Any] | None,
) -> None:
    selected: dict[str, str] = {}
    for field_name, field_schema in fields:
        if field_schema.get("serialization_exclude") is True:
            continue
        serialization_alias = field_schema.get("serialization_alias")
        output_name = field_name if serialization_alias is None else serialization_alias
        description = f"field {field_name!r}"
        _record_core_schema_output_name(
            family,
            projection,
            owner=owner,
            selected=selected,
            output_name=output_name,
            description=description,
        )
        _validate_core_schema_serialization_names(
            family,
            projection,
            field_schema.get("schema"),
            definitions=definitions,
            seen=seen,
        )

    if not isinstance(computed, list):
        return

    for computed_schema in computed:
        if not isinstance(computed_schema, Mapping):
            continue
        field_name = computed_schema.get("property_name")
        if not isinstance(field_name, str):
            continue
        computed_alias = computed_schema.get("alias")
        output_name = field_name if computed_alias is None else computed_alias
        description = f"computed field {field_name!r}"
        _record_core_schema_output_name(
            family,
            projection,
            owner=owner,
            selected=selected,
            output_name=output_name,
            description=description,
        )
        _validate_core_schema_serialization_names(
            family,
            projection,
            computed_schema.get("return_schema"),
            definitions=definitions,
            seen=seen,
        )


def _record_core_schema_output_name(
    family: SchemaFamily[Any],
    projection: _VersionProjection,
    *,
    owner: type[Any] | None,
    selected: dict[str, str],
    output_name: Any,
    description: str,
) -> None:
    if not isinstance(output_name, str):
        return
    previous = selected.get(output_name)
    if previous is not None and previous != description:
        _raise_duplicate_serialization_name(
            family,
            projection,
            model=family.model if owner is None else owner,
            output_name=output_name,
            first=previous,
            second=description,
        )
    selected[output_name] = description


def _model_has_model_serializer(model: type[BaseModel]) -> bool:
    decorators = getattr(model, "__pydantic_decorators__", None)
    return bool(decorators is not None and getattr(decorators, "model_serializers", None))


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
                    continue
                if _model_has_field_serializer(owner, field_name):
                    _raise_projection_unsupported(
                        family,
                        projection,
                        f"explicit wire model {_model_display(owner)!r} serializes field "
                        f"{field_name!r} along declared nested path {route.path!r}; "
                        "serializers can relocate child document paths",
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
                if index == len(target_path) - 1:
                    continue
                _collect_immediate_base_models(
                    field_info.annotation,
                    models=next_owners,
                    seen=set(),
                )
            owners = next_owners


def _annotation_has_functional_serializer(annotation: Any, *, seen: set[int]) -> bool:
    if isinstance(
        annotation,
        (PlainSerializer, _SERIALIZE_AS_ANY_METADATA_TYPE, WrapSerializer),
    ):
        return True
    if id(annotation) in seen:
        return False
    seen.add(id(annotation))
    if isinstance(annotation, _TYPE_ALIAS_TYPES):
        return _annotation_has_functional_serializer(annotation.__value__, seen=seen)
    arguments = get_args(annotation)
    if get_origin(annotation) is Annotated:
        base, *metadata = arguments
        if any(_metadata_has_unsafe_nested_serialization(item) for item in metadata):
            return True
        return _annotation_has_functional_serializer(base, seen=seen)
    return any(
        argument is not Ellipsis and _annotation_has_functional_serializer(argument, seen=seen)
        for argument in arguments
    )


def _field_has_functional_serializer(field_info: Any) -> bool:
    return _annotation_has_functional_serializer(
        field_info.annotation,
        seen=set(),
    ) or any(_metadata_has_unsafe_nested_serialization(item) for item in field_info.metadata)


def _metadata_has_unsafe_nested_serialization(item: Any) -> bool:
    return (
        isinstance(
            item,
            (PlainSerializer, _SERIALIZE_AS_ANY_METADATA_TYPE, WrapSerializer),
        )
        or isinstance(item, GetPydanticSchema)
        or _has_schema_hook(item)
    )


def _model_has_field_serializer(model: type[BaseModel], field_name: str) -> bool:
    decorators = getattr(model, "__pydantic_decorators__", None)
    serializers = None if decorators is None else getattr(decorators, "field_serializers", None)
    if not serializers:
        return False
    return any(
        field_name in decorator.info.fields or "*" in decorator.info.fields
        for decorator in serializers.values()
    )


def _collect_immediate_base_models(
    annotation: Any,
    *,
    models: list[type[BaseModel]],
    seen: set[int],
) -> None:
    if id(annotation) in seen:
        return
    seen.add(id(annotation))
    if isinstance(annotation, _TYPE_ALIAS_TYPES):
        _collect_immediate_base_models(annotation.__value__, models=models, seen=seen)
        return
    if isinstance(annotation, TypeVar):
        for value in _type_parameter_values(annotation):
            _collect_immediate_base_models(value, models=models, seen=seen)
        return
    supertype = _instance_dict(annotation).get("__supertype__")
    if supertype is not None and supertype is not annotation:
        _collect_immediate_base_models(supertype, models=models, seen=seen)
        return
    try:
        is_model = isinstance(annotation, type) and issubclass(annotation, BaseModel)
    except TypeError:
        is_model = False
    if is_model:
        models.append(annotation)
        return
    if get_origin(annotation) is Literal:
        return
    for argument in get_args(annotation):
        if argument is Ellipsis:
            continue
        _collect_immediate_base_models(argument, models=models, seen=seen)


def _alias_paths(alias: Any) -> tuple[tuple[str | int, ...], ...]:
    if isinstance(alias, str):
        return ((alias,),)
    if isinstance(alias, AliasPath):
        return (tuple(alias.path),)
    if isinstance(alias, AliasChoices):
        return tuple(path for choice in alias.choices for path in _alias_paths(choice))
    return ()


def _add_family_metadata_field(
    family: SchemaFamily[Any],
    version: str,
    fields: dict[str, Any],
) -> None:
    metadata = family.version_metadata
    if metadata is None or metadata.owner != "family":
        return
    if not isinstance(metadata.path, str):
        _add_nested_family_metadata_field(family, version, metadata.path, fields)
        return
    annotation = _literal_type(version)
    fields[metadata.path] = Annotated[
        annotation,
        Field(
            default=version,
            alias=metadata.path,
            alias_priority=2,
            validation_alias=metadata.path,
            serialization_alias=metadata.path,
        ),
    ]


def _add_nested_family_metadata_field(
    family: SchemaFamily[Any],
    version: str,
    path: tuple[str, ...],
    fields: dict[str, Any],
) -> None:
    if len(path) == 1:
        field_name = path[0]
        annotation = _literal_type(version)
        fields[field_name] = Annotated[
            annotation,
            Field(
                default=version,
                alias=field_name,
                alias_priority=2,
                validation_alias=field_name,
                serialization_alias=field_name,
            ),
        ]
        return

    child_model: type[BaseModel] | None = None
    for index in range(len(path) - 1, 0, -1):
        field_name = path[index]
        if child_model is None:
            annotation = _literal_type(version)
            field = Field(
                default=version,
                alias=field_name,
                alias_priority=2,
                validation_alias=field_name,
                serialization_alias=field_name,
            )
        else:
            annotation = child_model
            field = Field(
                default_factory=child_model,
                alias=field_name,
                alias_priority=2,
                validation_alias=field_name,
                serialization_alias=field_name,
            )
        field_definitions: dict[str, Any] = {field_name: Annotated[annotation, field]}
        child_model = create_model(
            _metadata_model_name(family, version, path, index),
            __config__=_metadata_wrapper_config(family),
            __module__=family.model.__module__,
            **field_definitions,
        )

    if child_model is None:  # pragma: no cover - paths are non-empty and handled above
        _raise_unsupported(family, "family-owned metadata path cannot be empty")
    root_name = path[0]
    fields[root_name] = Annotated[
        child_model,
        Field(
            default_factory=child_model,
            alias=root_name,
            alias_priority=2,
            validation_alias=root_name,
            serialization_alias=root_name,
        ),
    ]


def _metadata_wrapper_config(family: SchemaFamily[Any]) -> ConfigDict:
    del family
    return ConfigDict(extra="forbid", frozen=True)


def _metadata_model_name(
    family: SchemaFamily[Any],
    version: str,
    path: tuple[str, ...],
    index: int,
) -> str:
    components = (
        family.model.__module__,
        family.model.__qualname__,
        family.name,
        version,
        "version-metadata",
        *path[: index + 1],
    )
    suffix = _stable_digest(components)[:12]
    return (
        f"{_generated_model_name(family.model, family.name, version)}"
        f"_Metadata_{_identifier_component(path[index])}_{suffix}"
    )


def _validate_object_schema(
    family: SchemaFamily[Any],
    projection: _VersionProjection,
    model: type[BaseModel],
    *,
    mode: Literal["validation", "serialization"],
) -> None:
    schema = model.model_json_schema(mode=mode)
    try:
        json.dumps(schema, allow_nan=False)
    except (TypeError, ValueError) as exc:
        msg = (
            f"Automatic wire model for family {family.name!r}, version "
            f"{projection.label!r}, and model {_model_display(family.model)!r} "
            f"has a non-JSON-serializable {mode} schema"
        )
        raise UnsupportedWireModelError(msg) from exc
    root: Any = schema
    seen: set[str] = set()
    while isinstance(root, Mapping) and isinstance(root.get("$ref"), str):
        ref = root["$ref"]
        if ref in seen or not ref.startswith("#/"):
            break
        seen.add(ref)
        root = schema
        for component in ref[2:].split("/"):
            if not isinstance(root, Mapping) or component not in root:
                root = None
                break
            root = root[component]
    if not isinstance(root, Mapping) or root.get("type") != "object":
        msg = (
            f"Automatic wire model for family {family.name!r}, version "
            f"{projection.label!r}, and model {_model_display(family.model)!r} "
            f"has a non-object {mode} schema"
        )
        raise UnsupportedWireModelError(msg)
    properties = root.get("properties")
    if properties is not None and not isinstance(properties, Mapping):
        msg = (
            f"Automatic wire model for family {family.name!r}, version "
            f"{projection.label!r}, and model {_model_display(family.model)!r} "
            f"has malformed {mode} schema: object properties must be a mapping"
        )
        raise UnsupportedWireModelError(msg)


def _first_defining_class(model: type[BaseModel], attribute: str) -> type[Any] | None:
    return next((owner for owner in model.__mro__ if attribute in owner.__dict__), None)


def _literal_type(value: str) -> Any:
    return cast(Any, Literal)[value]


def _has_effect(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, Mapping | tuple | list | set | frozenset) and not value:
        return False
    return True


def _safe_deepcopy(
    family: SchemaFamily[Any],
    value: Any,
    *,
    detail: str,
) -> Any:
    try:
        return deepcopy(value)
    except Exception as exc:
        msg = (
            f"Automatic wire model for family {family.name!r} and model "
            f"{_model_display(family.model)!r} cannot safely copy {detail}"
        )
        raise UnsupportedWireModelError(msg) from exc


def _raise_unsupported(family: SchemaFamily[Any], detail: str) -> None:
    msg = (
        f"Automatic wire model for family {family.name!r} and model "
        f"{_model_display(family.model)!r} is unsupported: {detail}"
    )
    raise UnsupportedWireModelError(msg)


def _safe_runtime_subclass(candidate: type[Any], target: type[Any]) -> bool:
    try:
        return issubclass(candidate, target)
    except TypeError:
        return False


def _raise_projection_unsupported(
    family: SchemaFamily[Any],
    projection: _VersionProjection,
    detail: str,
) -> None:
    msg = (
        f"Automatic wire model for family {family.name!r}, version "
        f"{projection.label!r}, and model {_model_display(family.model)!r} "
        f"is unsupported: {detail}"
    )
    raise UnsupportedWireModelError(msg)


def _model_display(model: type[BaseModel]) -> str:
    return f"{model.__module__}.{model.__qualname__}"


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
        if is_typeddict(annotation):
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
                if any(is_typeddict(argument) for argument in branch_arguments):
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
            if not arguments:
                return
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
    if is_typeddict(annotation):
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


def _set_element_wire_model(model: type[BaseModel]) -> type[BaseModel]:
    if model.model_config.get("frozen"):
        return model
    cached = _HASHABLE_MODEL_CACHE.get(model)
    if cached is not None:
        return cached
    frozen_model = create_model(
        f"{model.__name__}__HashableSetElement",
        __base__=model,
        __module__=model.__module__,
        __config__=ConfigDict(frozen=True),
    )
    frozen_model.model_rebuild(force=True)
    _HASHABLE_MODEL_CACHE[model] = frozen_model
    return frozen_model


def _rewrite_annotation(
    annotation: Any,
    version: str,
    family: SchemaFamily[Any],
    *,
    nested: tuple[_CompiledNestedFamily, ...],
    decorator_nested: tuple[_CompiledDecoratorNestedFamily, ...],
    field_path: tuple[str, ...],
    used_nested: set[tuple[str, ...]],
    field_name: str,
    allow_child_projection: bool,
    nested_projection_cache: dict[tuple[int, tuple[str, ...], str, bool], type[BaseModel] | None],
    nested_projection_stack: set[tuple[int, tuple[str, ...], str, bool]],
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
        return _set_element_wire_model(family_model) if in_set_element else family_model
    decorator_child = (
        _find_decorator_family_for_model(decorator_nested, field_path, annotation)
        if allow_child_projection
        else None
    )
    if decorator_child is not None:
        family_model = decorator_child.family.model_for(version)
        return _set_element_wire_model(family_model) if in_set_element else family_model

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
                field_name=field_name,
                field_path=field_path,
                nested=nested_families,
                decorator_nested=decorator_families,
                in_set_element=in_set_element,
                nested_projection_cache=nested_projection_cache,
                nested_projection_stack=nested_projection_stack,
                used_nested=used_nested,
            )

    origin = get_origin(annotation)
    if isinstance(origin, _TYPE_ALIAS_TYPES):
        _validate_type_alias(family, field_name, origin)
    if origin is not None:
        _validate_annotation_behavior(family, field_name, origin)
    if origin is Annotated:
        base, *source_metadata = get_args(annotation)
        rewritten = _rewrite_annotation(
            base,
            version,
            family,
            nested=nested,
            decorator_nested=decorator_nested,
            field_path=field_path,
            used_nested=used_nested,
            field_name=field_name,
            allow_child_projection=allow_child_projection,
            nested_projection_cache=nested_projection_cache,
            nested_projection_stack=nested_projection_stack,
            in_set_element=in_set_element,
        )
        metadata = _snapshot_wire_metadata(
            family,
            field_name,
            source_metadata,
            detail="nested annotation metadata",
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
            field_name=field_name,
            field_path=field_path,
            allow_child_projection=allow_child_projection and legacy_container,
            nested=nested,
            decorator_nested=decorator_nested,
            used_nested=used_nested,
            nested_projection_cache=nested_projection_cache,
            nested_projection_stack=nested_projection_stack,
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
    copy_with = getattr(annotation, "copy_with", None)
    if callable(copy_with):
        return copy_with(args)
    try:
        return origin[args]
    except UnsupportedWireModelError:
        raise
    except Exception as exc:
        msg = (
            f"Automatic wire model for family {family.name!r} and model "
            f"{_model_display(family.model)!r} cannot safely rewrite annotation "
            f"for field {field_name!r}"
        )
        raise UnsupportedWireModelError(msg) from exc


def _rewrite_nested_model(
    annotation: type[BaseModel],
    version: str,
    owner: SchemaFamily[Any],
    *,
    field_name: str,
    field_path: tuple[str, ...],
    nested: tuple[_CompiledNestedFamily, ...],
    decorator_nested: tuple[_CompiledDecoratorNestedFamily, ...],
    nested_projection_cache: dict[tuple[int, tuple[str, ...], str, bool], type[BaseModel] | None],
    nested_projection_stack: set[tuple[int, tuple[str, ...], str, bool]],
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
    if cache_key in nested_projection_stack:
        msg = (
            f"nested declaration path {field_path!r} on model {annotation.__qualname__!r} "
            "is self-referential and cannot be projected safely"
        )
        _raise_unsupported(owner, msg)

    placeholder = create_model(
        _nested_model_name(owner, annotation, field_path, version),
        __module__=annotation.__module__,
    )
    nested_projection_cache[cache_key] = placeholder
    nested_projection_stack.add(cache_key)
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
            rewritten_annotation = _rewrite_annotation(
                source["annotation"],
                version,
                owner,
                nested=nested,
                decorator_nested=decorator_nested,
                field_path=field_path + (source_name,),
                used_nested=used_nested,
                field_name=source_name,
                allow_child_projection=True,
                in_set_element=in_set_element,
                nested_projection_cache=nested_projection_cache,
                nested_projection_stack=nested_projection_stack,
            )
            metadata = _wire_field_metadata(owner, source_name, source["metadata"])
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
            nested_projection = _set_element_wire_model(nested_projection)
    except UnsupportedWireModelError:
        raise
    except Exception as exc:
        msg = (
            f"Automatic wire model for family {owner.name!r}, version {version!r}, and model "
            f"{_model_display(annotation)!r} cannot safely project nested model for path "
            f"{field_path!r}"
        )
        raise UnsupportedWireModelError(msg) from exc
    finally:
        nested_projection_stack.remove(cache_key)
    nested_projection_cache[cache_key] = nested_projection
    return nested_projection


def _validate_type_alias(
    family: SchemaFamily[Any],
    field_name: str,
    alias: Any,
    *,
    seen: set[int] | None = None,
) -> None:
    visited = set() if seen is None else seen
    if id(alias) in visited:
        return
    visited.add(id(alias))
    _validate_type_alias_value(family, field_name, alias.__value__, seen=visited)
    for parameter in alias.__type_params__:
        for value in _type_parameter_values(parameter):
            _validate_type_alias_value(family, field_name, value, seen=visited)


def _validate_type_alias_value(
    family: SchemaFamily[Any],
    field_name: str,
    value: Any,
    *,
    seen: set[int],
) -> None:
    if isinstance(value, _TYPE_ALIAS_TYPES):
        _validate_type_alias(family, field_name, value, seen=seen)
        return
    origin = get_origin(value)
    if origin is Literal:
        return
    if isinstance(value, str | ForwardRef):
        _raise_unsupported(
            family,
            f"field {field_name!r} has an unresolved forward reference hidden in a type alias",
        )
    if isinstance(origin, _TYPE_ALIAS_TYPES):
        _validate_type_alias(family, field_name, origin, seen=seen)
    if origin is not None:
        _validate_annotation_behavior(
            family,
            field_name,
            origin,
            hidden_in_alias=True,
        )
    _validate_annotation_behavior(
        family,
        field_name,
        value,
        hidden_in_alias=True,
    )
    if origin is Annotated:
        base, *metadata = get_args(value)
        for item in metadata:
            if isinstance(item, (*_FUNCTIONAL_FIELD_BEHAVIOR, Predicate, Not)):
                _raise_unsupported(
                    family,
                    f"field {field_name!r} has runtime behavior hidden in a type alias",
                )
            if isinstance(item, Discriminator | WithJsonSchema):
                _raise_unsupported(
                    family,
                    f"field {field_name!r} has schema metadata hidden in a type alias",
                )
            elif isinstance(item, GroupedMetadata) and not _is_trusted_declarative_type(
                type(item),
                include_annotated_types=True,
            ):
                _raise_unsupported(
                    family,
                    f"field {field_name!r} has executable metadata hidden in a type alias",
                )
            elif isinstance(item, GetPydanticSchema) or (_has_schema_hook(item)):
                _raise_unsupported(
                    family,
                    f"field {field_name!r} has custom schema metadata hidden in a type alias",
                )
        _validate_type_alias_value(family, field_name, base, seen=seen)
        return
    for argument in get_args(value):
        _validate_type_alias_value(family, field_name, argument, seen=seen)


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

    from pydantic_versions._runtime import _remove_version_field, _to_version_names

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
