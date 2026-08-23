from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import is_dataclass
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ForwardRef,
    Literal,
    Never,
    TypeVar,
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
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
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
from pydantic_core import PydanticUndefined
from typing_extensions import NoExtraItems
from typing_extensions import TypeAliasType as ExtensionsTypeAliasType  # noqa: UP035
from typing_extensions import is_typeddict as extensions_is_typeddict

from pydantic_versions._compiler import (
    _generated_model_name,
    _identifier_component,
    _model_display,
    _stable_digest,
    _VersionProjection,
)
from pydantic_versions._runtime_versioning import _alias_paths
from pydantic_versions.exceptions import UnsupportedWireModelError

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
    return any(attribute in _instance_dict(owner) for owner in _static_mro(item))


def _mro_annotations(item: type[Any]) -> Iterable[tuple[str, Any]]:
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
    default = _type_parameter_default(parameter)
    if default is not _MISSING:
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


def _model_has_model_serializer(model: type[Any]) -> bool:
    decorators = getattr(model, "__pydantic_decorators__", None)
    if decorators is not None and getattr(decorators, "model_serializers", None):
        return True
    return any(
        _pydantic_decorator_info_kind(value) == "ModelSerializerDecoratorInfo"
        for value in _effective_static_class_values(model)
    )


def _pydantic_decorator_info_kind(value: Any) -> str | None:
    """Return the stable Pydantic decorator-info kind without importing internals."""
    info = _instance_dict(value).get("decorator_info")
    info_type = type(info)
    if not info_type.__module__.startswith("pydantic."):
        return None
    return info_type.__name__


def _effective_static_class_values(owner: type[Any]) -> tuple[Any, ...]:
    return tuple(value for _name, value in _effective_static_class_items(owner))


def _effective_static_class_items(owner: type[Any]) -> tuple[tuple[str, Any], ...]:
    selected: dict[str, Any] = {}
    for candidate in _static_mro(owner):
        for name, value in _instance_dict(candidate).items():
            selected.setdefault(name, value)
    return tuple(selected.items())


def _is_typed_dict(annotation: Any) -> bool:
    return is_typeddict(annotation) or extensions_is_typeddict(annotation)


def _typed_dict_origin(annotation: Any) -> type[Any] | None:
    if _is_typed_dict(annotation):
        return cast(type[Any], annotation)
    origin = get_origin(annotation)
    if _is_typed_dict(origin):
        return cast(type[Any], origin)
    return None


def _annotation_type_parameters(annotation: Any) -> tuple[Any, ...]:
    parameters = getattr(annotation, "__type_params__", None)
    if not parameters:
        parameters = getattr(annotation, "__parameters__", ())
    return tuple(parameters)


def _type_parameter_default(parameter: Any) -> Any:
    default = getattr(parameter, "__default__", _MISSING)
    default_type = type(default)
    no_default = "NoDefault" in default_type.__name__ and repr(default) in (
        "typing.NoDefault",
        "typing_extensions.NoDefault",
    )
    if default is not _MISSING and not no_default:
        return default
    return _MISSING


def _is_typed_dict_field_qualifier(origin: Any) -> bool:
    return getattr(origin, "__module__", None) in ("typing", "typing_extensions") and getattr(
        origin, "_name", None
    ) in ("NotRequired", "ReadOnly", "Required")


def _is_no_extra_items(value: Any) -> bool:
    if value is NoExtraItems:
        return True
    value_type = type(value)
    return (
        value_type.__module__ in ("typing", "typing_extensions")
        and value_type.__name__.lstrip("_") == "NoExtraItemsType"
    )


def _bound_annotation_parameters(
    annotation: Any,
    *,
    inherited: Mapping[Any, Any],
) -> dict[Any, Any]:
    origin = get_origin(annotation)
    target = annotation if origin is None else origin
    parameters = _annotation_type_parameters(target)
    arguments = get_args(annotation)
    bindings = {
        **inherited,
        **dict(zip(parameters, arguments, strict=False)),
    }
    for parameter in parameters[len(arguments) :]:
        default = _type_parameter_default(parameter)
        if default is not _MISSING:
            bindings[parameter] = default
    return bindings


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
            __config__=_metadata_wrapper_config(),
            __module__=family.model.__module__,
            **field_definitions,
        )

    # VersionMetadata rejects empty paths and the single-component case returns
    # above, so the wrapper loop always produces this model. Keep that invariant
    # visible to static analysis without a second runtime error path.
    assert child_model is not None
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


def _metadata_wrapper_config() -> ConfigDict:
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
) -> Never:
    msg = (
        f"Automatic wire model for family {family.name!r}, version "
        f"{projection.label!r}, and model {_model_display(family.model)!r} "
        f"is unsupported: {detail}"
    )
    raise UnsupportedWireModelError(msg)


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
