from __future__ import annotations

from collections.abc import Mapping
from copy import copy, deepcopy
from types import MemberDescriptorType
from typing import TYPE_CHECKING, Any, ClassVar, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    GetCoreSchemaHandler,
    GetJsonSchemaHandler,
    create_model,
)
from pydantic_core import CoreSchema, PydanticUndefined, core_schema

from pydantic_versions._compiler import _generated_model_name, _VersionProjection
from pydantic_versions._wire_contract import (
    _CUSTOM_MODEL_HOOKS,
    _MISSING,
    _add_family_metadata_field,
    _first_defining_class,
    _model_display,
    _raise_projection_unsupported,
    _validate_object_schema,
)
from pydantic_versions.exceptions import SchemaCompilationError

if TYPE_CHECKING:
    from pydantic_versions.family import SchemaFamily

_DOCUMENT_BODY_SLOT = "_FamilyDocumentAdapterBase__document_body"


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
