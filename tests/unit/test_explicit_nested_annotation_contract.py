from __future__ import annotations

import typing
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from enum import Enum
from typing import (
    Annotated,
    Any,
    Generic,
    Literal,
    Never,
    NewType,
    NotRequired,
    TypedDict,
    TypeVar,
)

import pytest
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    computed_field,
    create_model,
    field_serializer,
    model_serializer,
    with_config,
)
from pydantic.dataclasses import dataclass as pydantic_dataclass
from pydantic.functional_serializers import PlainSerializer
from pydantic_core import core_schema
from typing_extensions import TypedDict as ExtensionsTypedDict  # noqa: UP035
from typing_extensions import TypeVar as ExtensionsTypeVar  # noqa: UP035

from pydantic_versions import (
    NestedFamily,
    SchemaFamily,
    SchemaVersion,
    UnsupportedWireModelError,
    VersionTransition,
    matching_labels,
)


class _Child(BaseModel):
    value: int


class _Parent(BaseModel):
    child: _Child


_CHILD_FAMILY = SchemaFamily(
    model=_Child,
    name="explicit_annotation_contract_child",
    versions=(SchemaVersion("1"), SchemaVersion("2")),
)


class _DictionarySchemaCarrier:
    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: Any,
        handler: Any,
    ) -> core_schema.CoreSchema:
        del cls, source_type, handler
        return core_schema.dict_schema(
            core_schema.str_schema(),
            core_schema.any_schema(),
        )


class _CustomList(list[str]):
    pass


class _HistoricalChildModel(BaseModel):
    value: int


class _HistoricalChildWithSchemaHook(BaseModel):
    value: int

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        schema: core_schema.CoreSchema,
        handler: Any,
    ) -> dict[str, Any]:
        del cls
        return handler(schema)


@dataclass
class _HistoricalChildDataclass:
    value: int


@dataclass
class _HistoricalDataclassSchemaCarrier:
    value: int

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: Any,
        handler: Any,
    ) -> core_schema.CoreSchema:
        del cls, source_type, handler
        return core_schema.dict_schema(
            core_schema.str_schema(),
            core_schema.any_schema(),
        )


class _MappingValuedHistoricalChildEnum(Enum):
    OBJECT = {"value": 1}
    TEXT = "legacy"


type _mapping_enum_items[Item] = list[Item]
type _mapping_enum_structural_items = (
    list[_MappingValuedHistoricalChildEnum] | list[_HistoricalChildDataclass]
)
_mapping_enum_alias = NewType(
    "_mapping_enum_alias",
    _MappingValuedHistoricalChildEnum,
)
_mapping_enum_structural_leaf = TypeVar(
    "_mapping_enum_structural_leaf",
    _MappingValuedHistoricalChildEnum,
    _HistoricalChildModel,
)


class _ExtensionsHistoricalChildTypedDict(ExtensionsTypedDict):
    value: int


class _GenericHistoricalChildTypedDict[Item](TypedDict):
    value: Item


_LegacyTypedDictItem = TypeVar("_LegacyTypedDictItem")


class _LegacyGenericHistoricalChildTypedDict(
    TypedDict,
    Generic[_LegacyTypedDictItem],  # noqa: UP046 - exercise legacy generic metadata
):
    value: _LegacyTypedDictItem


_DefaultTypedDictItem = ExtensionsTypeVar("_DefaultTypedDictItem", default=int)


class _DefaultGenericHistoricalChildTypedDict(
    TypedDict,
    Generic[_DefaultTypedDictItem],  # noqa: UP046 - exercise defaulted legacy metadata
):
    value: _DefaultTypedDictItem


@with_config(ConfigDict(extra="allow"))
class _ExtraAllowHistoricalChildTypedDict(TypedDict):
    value: int


class _ExtraItemsHistoricalChildTypedDict(ExtensionsTypedDict, extra_items=str):
    value: int


class _HistoricalChildRoot(RootModel[dict[str, int]]):
    pass


class _HistoricalGenericChild[item](BaseModel):
    value: item


_IncompleteHistoricalChild = create_model(
    "_IncompleteHistoricalChild",
    value=("_NeverDefinedHistoricalChild", ...),
)


type _safe_items[_safe_item] = list[_safe_item]
type _safe_annotated_alias = Annotated[str, Field(description="legacy scalar")]
type _safe_recursive_alias = str | list[_safe_recursive_alias]
_safe_identifier = NewType("_safe_identifier", str)
_safe_bound = TypeVar("_safe_bound", bound=str)
_unbound = TypeVar("_unbound")
_unsafe_constraint = TypeVar("_unsafe_constraint", str, Any)
type _unsafe_alias = list[Any]
type _unsafe_annotated_alias = Annotated[Any, Field(description="broad")]
_unsafe_mapping = NewType("_unsafe_mapping", dict[str, int])
_relocating_serializer = PlainSerializer(
    lambda value: {"relocated": value, "schema_version": "secret"},
    return_type=dict[str, Any],
)
type _serialized_direct_alias = Annotated[str, _relocating_serializer]
type _serialized_generic_alias[_serialized_item] = Annotated[
    _serialized_item,
    _relocating_serializer,
]
type _serializer_passthrough[_serialized_item] = list[_serialized_item]
_serialized_new_type = NewType(
    "_serialized_new_type",
    _serialized_direct_alias,  # ty: ignore[invalid-newtype]
)
_serialized_bound = TypeVar("_serialized_bound", bound=_serialized_direct_alias)


class _SerializedHistoricalChildModel(BaseModel):
    value: int

    @model_serializer
    def serialize_model(self) -> dict[str, Any]:
        return {"relocated": self.value, "schema_version": "secret"}


@dataclass
class _SerializedStdlibHistoricalChildDataclass:
    value: int

    @model_serializer
    def serialize_model(self) -> dict[str, Any]:
        return {"relocated": self.value, "schema_version": "secret"}


@dataclass
class _InheritedSerializedStdlibDataclassBase:
    @model_serializer
    def serialize_model(self) -> dict[str, Any]:
        return {"relocated": 1, "schema_version": "secret"}


@dataclass
class _InheritedSerializedStdlibHistoricalChildDataclass(
    _InheritedSerializedStdlibDataclassBase,
):
    value: int


def _leaf_family(
    annotation: Any,
    *,
    case: str,
    arbitrary_types_allowed: bool = False,
) -> SchemaFamily[_Parent]:
    config = ConfigDict(arbitrary_types_allowed=True) if arbitrary_types_allowed else None
    historical_parent = create_model(
        f"ExplicitAnnotation{case.title().replace('_', '')}HistoricalParent",
        __config__=config,
        child=(annotation, ...),
    )
    return SchemaFamily(
        model=_Parent,
        name=f"explicit_annotation_{case}",
        versions=(
            SchemaVersion("1", wire_model=historical_parent),
            SchemaVersion("2"),
        ),
        nested=(NestedFamily("child", _CHILD_FAMILY, matching_labels()),),
    )


@pytest.mark.parametrize(
    ("case", "annotation", "diagnostic"),
    [
        ("any", Any, "broad annotation"),
        ("object", object, "broad annotation"),
        ("bare_dict", dict, "mapping container"),
        ("typed_dict_container", dict[str, int], "mapping container"),
        ("bare_mapping", Mapping, "mapping container"),
        ("mapping", Mapping[str, int], "abstract or custom container"),
        ("bare_list", list, "unparameterized collection"),
        ("typing_tuple", typing.Tuple, "unparameterized collection"),  # noqa: UP006
        ("list_of_any", list[Any], "broad annotation"),
        ("tuple_of_any", tuple[Any, ...], "broad annotation"),
        ("sequence", Sequence[str], "abstract or custom container"),
        (
            "schema_carrier",
            _DictionarySchemaCarrier,
            "unsupported custom annotation schema hook",
        ),
        (
            "model_schema_hook",
            _HistoricalChildWithSchemaHook,
            "unsupported custom annotation schema hook",
        ),
        (
            "dataclass_schema_hook",
            _HistoricalDataclassSchemaCarrier,
            "unsupported custom annotation schema hook",
        ),
        ("root_model", _HistoricalChildRoot, "RootModel that is not object-shaped"),
        ("generic_model", _HistoricalGenericChild, "unparameterized generic model"),
        ("incomplete_model", _IncompleteHistoricalChild, "incomplete model"),
        ("unsafe_alias", _unsafe_alias, "broad annotation"),
        ("unsafe_annotated_alias", _unsafe_annotated_alias, "broad annotation"),
        ("unsafe_new_type", _unsafe_mapping, "mapping container"),
        ("unbound_type_var", _unbound, "unresolved type parameter"),
        (
            "unbound_type_var_union",
            _unbound | _HistoricalChildDataclass,
            "unresolved type parameter",
        ),
        ("unsafe_constraint", _unsafe_constraint, "broad annotation"),
        ("broad_union_arm", str | Any, "broad annotation"),
        ("unresolved_forward", "_NeverDefinedExplicitLeaf", "unresolved annotation"),
        ("unsupported_generic", type[int], "unsupported generic annotation"),
    ],
)
def test_explicit_managed_leaf_rejects_shape_erasing_annotations(
    case: str,
    annotation: Any,
    diagnostic: str,
) -> None:
    family = _leaf_family(annotation, case=case)

    with pytest.raises(UnsupportedWireModelError, match=diagnostic) as exc_info:
        family.model_for("1")

    message = str(exc_info.value)
    assert f"explicit_annotation_{case}" in message
    assert "version '1'" in message
    assert "declared nested path ('child',)" in message


@pytest.mark.parametrize(
    ("case", "annotation"),
    [
        ("optional_scalar", str | None),
        ("empty_tuple", tuple[()]),
        ("frozenset", frozenset[str]),
        ("extensions_typed_dict", _ExtensionsHistoricalChildTypedDict),
        (
            "legacy_generic_typed_dict",
            _LegacyGenericHistoricalChildTypedDict[int],
        ),
        ("defaulted_generic_typed_dict", _DefaultGenericHistoricalChildTypedDict),
        ("new_type", _safe_identifier),
        ("annotated_alias", _safe_annotated_alias),
        ("generic_alias", _safe_items[str]),
        ("recursive_alias", _safe_recursive_alias),
        ("bounded_type_var", _safe_bound),
        ("model_or_scalar", _HistoricalChildModel | str),
        ("model_or_literal", _HistoricalChildModel | Literal["legacy"]),
        ("mapping_valued_enum", _MappingValuedHistoricalChildEnum),
        (
            "mapping_valued_enum_literal",
            Literal[_MappingValuedHistoricalChildEnum.OBJECT],
        ),
        (
            "mapping_valued_enum_or_scalar",
            _MappingValuedHistoricalChildEnum | str,
        ),
        (
            "mapping_enum_distinct_depth_union",
            _MappingValuedHistoricalChildEnum | list[_HistoricalChildModel],
        ),
        (
            "mapping_enum_distinct_fixed_tuple_positions",
            tuple[_MappingValuedHistoricalChildEnum, str] | tuple[str, _HistoricalChildModel],
        ),
    ],
)
def test_explicit_managed_leaf_accepts_shape_preserving_annotations(
    case: str,
    annotation: Any,
) -> None:
    family = _leaf_family(annotation, case=f"safe_{case}")

    historical = family.model_for("1")

    assert "child" in historical.model_fields


@pytest.mark.parametrize(
    ("case", "annotation"),
    [
        (
            "direct",
            _MappingValuedHistoricalChildEnum | _HistoricalChildModel,
        ),
        (
            "collection",
            list[_MappingValuedHistoricalChildEnum] | list[_HistoricalChildDataclass],
        ),
        (
            "fixed_tuple",
            tuple[_MappingValuedHistoricalChildEnum, str] | tuple[_HistoricalChildDataclass, str],
        ),
        (
            "fixed_and_variadic_tuple",
            tuple[str, _MappingValuedHistoricalChildEnum] | tuple[_HistoricalChildDataclass, ...],
        ),
        (
            "coercible_collection_reversed",
            tuple[_HistoricalChildModel, ...] | list[_MappingValuedHistoricalChildEnum],
        ),
        (
            "coercible_collection_kinds",
            frozenset[_MappingValuedHistoricalChildEnum] | list[_HistoricalChildModel],
        ),
        ("generic_alias", _mapping_enum_structural_items),
        (
            "applied_generic_alias",
            _mapping_enum_items[_MappingValuedHistoricalChildEnum]
            | _mapping_enum_items[_HistoricalChildDataclass],
        ),
        (
            "annotated",
            Annotated[
                _MappingValuedHistoricalChildEnum,
                Field(description="legacy object-shaped scalar"),
            ]
            | _HistoricalChildModel,
        ),
        ("new_type", _mapping_enum_alias | _HistoricalChildModel),
        (
            "literal",
            Literal[_MappingValuedHistoricalChildEnum.OBJECT] | _HistoricalChildModel,
        ),
        ("type_parameter", list[_mapping_enum_structural_leaf]),
    ],
)
def test_managed_union_rejects_mapping_enum_and_structural_ambiguity(
    case: str,
    annotation: Any,
) -> None:
    family = _leaf_family(annotation, case=f"ambiguous_mapping_enum_{case}")

    with pytest.raises(
        UnsupportedWireModelError,
        match=("object-shaped Enum scalar.*structural representation.*same traversal position"),
    ):
        family.model_for("1")


@pytest.mark.parametrize(
    ("case", "annotation"),
    [
        ("direct_alias", _serialized_direct_alias),
        ("generic_alias", _serialized_generic_alias[str]),
        ("generic_alias_argument", _serializer_passthrough[_serialized_direct_alias]),
        ("new_type", _serialized_new_type),
        ("bounded_type_var", _serialized_bound),
    ],
)
def test_explicit_managed_leaf_rejects_functional_serializers_hidden_by_wrappers(
    case: str,
    annotation: Any,
) -> None:
    family = _leaf_family(annotation, case=f"serialized_{case}")

    with pytest.raises(
        UnsupportedWireModelError,
        match="annotation-level serializer",
    ):
        family.model_for("1")


@pytest.mark.parametrize(
    ("case", "annotation", "diagnostic"),
    [
        (
            "model_serializer",
            _SerializedHistoricalChildModel,
            "model-level serializer on a managed model leaf",
        ),
        (
            "stdlib_dataclass_serializer",
            _SerializedStdlibHistoricalChildDataclass,
            "model-level serializer on a managed dataclass leaf",
        ),
        (
            "inherited_stdlib_dataclass_serializer",
            _InheritedSerializedStdlibHistoricalChildDataclass,
            "model-level serializer on a managed dataclass leaf",
        ),
    ],
)
def test_explicit_managed_leaf_rejects_model_level_serializers(
    case: str,
    annotation: Any,
    diagnostic: str,
) -> None:
    family = _leaf_family(annotation, case=case)

    with pytest.raises(UnsupportedWireModelError, match=diagnostic):
        family.model_for("1")


def test_explicit_managed_leaf_rejects_unresolved_typed_dict_fields() -> None:
    class LocalTypedDict(TypedDict):
        value: _OnlyVisibleInThisTest

    class _OnlyVisibleInThisTest(BaseModel):
        value: int

    family = _leaf_family(LocalTypedDict, case="local_typed_dict_forward")

    with pytest.raises(
        UnsupportedWireModelError,
        match="TypedDict with unresolved field annotations",
    ):
        family.model_for("1")


@pytest.mark.parametrize(
    ("case", "annotation", "diagnostic"),
    [
        (
            "generic_typed_dict",
            _GenericHistoricalChildTypedDict,
            "unparameterized generic TypedDict",
        ),
        (
            "legacy_generic_typed_dict",
            _LegacyGenericHistoricalChildTypedDict,
            "unparameterized generic TypedDict",
        ),
        (
            "extra_allow_typed_dict",
            _ExtraAllowHistoricalChildTypedDict,
            "TypedDict with extra='allow'",
        ),
        (
            "extra_items_typed_dict",
            _ExtraItemsHistoricalChildTypedDict,
            "TypedDict with extra_items",
        ),
    ],
)
def test_explicit_managed_leaf_rejects_ambiguous_typed_dict_contracts(
    case: str,
    annotation: Any,
    diagnostic: str,
) -> None:
    family = _leaf_family(annotation, case=case)

    with pytest.raises(UnsupportedWireModelError, match=diagnostic):
        family.model_for("1")


@pytest.mark.parametrize(
    ("case", "field"),
    [
        ("exclude", Field(exclude=True)),
        ("exclude_if", Field(exclude_if=lambda value: value is not None)),
    ],
)
def test_explicit_historical_managed_path_rejects_serialization_exclusion(
    case: str,
    field: Any,
) -> None:
    historical_parent = create_model(
        f"ExplicitAnnotationExcluded{case.title()}HistoricalParent",
        child=(_HistoricalChildModel, field),
    )
    family = SchemaFamily(
        model=_Parent,
        name=f"explicit_annotation_excluded_{case}",
        versions=(
            SchemaVersion("1", wire_model=historical_parent),
            SchemaVersion("2"),
        ),
        nested=(NestedFamily("child", _CHILD_FAMILY, matching_labels()),),
    )

    with pytest.raises(UnsupportedWireModelError, match="excludes field 'child'"):
        family.model_for("1")


class _TransitiveGrandchild(BaseModel):
    value: int


_TRANSITIVE_GRANDCHILD_FAMILY = SchemaFamily(
    model=_TransitiveGrandchild,
    name="explicit_annotation_transitive_grandchild",
    versions=(SchemaVersion("1"), SchemaVersion("2")),
)


class _TransitiveChild(BaseModel):
    grandchild: _TransitiveGrandchild


_TRANSITIVE_CHILD_FAMILY = SchemaFamily(
    model=_TransitiveChild,
    name="explicit_annotation_transitive_child",
    versions=(SchemaVersion("1"), SchemaVersion("2")),
    nested=(
        NestedFamily(
            "grandchild",
            _TRANSITIVE_GRANDCHILD_FAMILY,
            matching_labels(),
        ),
    ),
)


class _TransitiveParent(BaseModel):
    child: _TransitiveChild


class _BroadTransitiveChildModel(BaseModel):
    grandchild: Any


class _DeepTransitiveWrapper(BaseModel):
    grandchild: _TransitiveGrandchild


class _DeepTransitiveChild(BaseModel):
    wrapper: _DeepTransitiveWrapper


_DEEP_TRANSITIVE_CHILD_FAMILY = SchemaFamily(
    model=_DeepTransitiveChild,
    name="explicit_annotation_deep_transitive_child",
    versions=(SchemaVersion("1"), SchemaVersion("2")),
    nested=(
        NestedFamily(
            ("wrapper", "grandchild"),
            _TRANSITIVE_GRANDCHILD_FAMILY,
            matching_labels(),
        ),
    ),
)


class _DeepTransitiveParent(BaseModel):
    child: _DeepTransitiveChild


_DeepWrapperNewType = NewType("_DeepWrapperNewType", _BroadTransitiveChildModel)
type _DeepWrapperAlias = _DeepWrapperNewType
type _deep_wrapper_representations[Item] = Annotated[
    tuple[Item, ...] | Literal["omitted"],
    Field(description="historical wrapper representation"),
]
type _recursive_deep_wrapper = _BroadTransitiveChildModel | list[_recursive_deep_wrapper]


@dataclass
class _BroadTransitiveChildDataclass:
    grandchild: Any


class _BroadTransitiveChildTypedDict(TypedDict):
    grandchild: Any


class _GenericTransitiveChildTypedDict[Item](TypedDict):
    grandchild: Item


class _QualifiedGenericTransitiveChildTypedDict[Item](TypedDict):
    grandchild: NotRequired[Item]


class _DefaultGenericTransitiveChildTypedDict(
    TypedDict,
    Generic[_DefaultTypedDictItem],  # noqa: UP046 - exercise defaulted legacy metadata
):
    grandchild: _DefaultTypedDictItem


@dataclass
class _GenericTransitiveChildDataclass[Item]:
    grandchild: Item


@pydantic_dataclass
class _GenericTransitiveChildPydanticDataclass[Item]:
    grandchild: Item


class _GenericDataclassTransitiveTypedDict[Item](TypedDict):
    grandchild: NotRequired[_GenericTransitiveChildDataclass[Item]]


type _StringTransitiveChildDataclass = _GenericTransitiveChildDataclass[str]
type _ModelTransitiveChildDataclass = _GenericTransitiveChildDataclass[_HistoricalChildModel]
type _transitive_dataclass_union[Item] = Item | _GenericTransitiveChildDataclass[str]
type _transitive_dataclass_identity[Item] = Item
_ambiguous_transitive_dataclass_constraint = TypeVar(
    "_ambiguous_transitive_dataclass_constraint",
    _GenericTransitiveChildDataclass[str],
    _GenericTransitiveChildDataclass[_HistoricalChildModel],
)
_StringTransitiveChildDataclassNewType = NewType(
    "_StringTransitiveChildDataclassNewType",
    _GenericTransitiveChildDataclass[str],
)


@dataclass
class _SafeTransitiveChildDataclass:
    grandchild: str


@dataclass
class _DefaultOverridesAnnotatedManagedChildDataclass:
    grandchild: Annotated[str, Field(exclude=True)] = Field(exclude=False)


class _LaterAnnotatedFieldInfoWinsManagedChildTypedDict(TypedDict):
    grandchild: Annotated[
        str,
        Field(exclude=True, serialization_alias="old-grandchild"),
        Field(exclude=False, serialization_alias="grandchild"),
    ]


@dataclass
class _OmittedTransitiveChildDataclass:
    note: str


class _ComputedOmittedTransitiveChildModel(BaseModel):
    note: str

    @computed_field(alias="grandchild")
    @property
    def legacy_grandchild(self) -> dict[str, int]:
        return {"value": 1}


@pydantic_dataclass
class _ComputedOmittedTransitiveChildPydanticDataclass:
    note: str

    @computed_field(alias="grandchild")
    @property
    def legacy_grandchild(self) -> dict[str, int]:
        return {"value": 1}


@dataclass
class _SerializedTransitiveChildDataclass:
    grandchild: str

    @field_serializer("grandchild")
    def serialize_grandchild(self, value: str) -> dict[str, str]:
        return {"relocated": value}


@pydantic_dataclass
class _PydanticSerializedTransitiveChildDataclass:
    grandchild: str

    @field_serializer("grandchild")
    def serialize_grandchild(self, value: str) -> dict[str, str]:
        return {"relocated": value}


@dataclass
class _AnnotatedSerializedTransitiveChildDataclass:
    grandchild: _serialized_direct_alias


class _ExcludedTransitiveChildModel(BaseModel):
    grandchild: str = Field(exclude=True)


@dataclass
class _ExcludedTransitiveChildDataclass:
    grandchild: Annotated[str, Field(exclude=True)]


class _ExcludedTransitiveChildTypedDict(TypedDict):
    grandchild: Annotated[str, Field(exclude=True)]


@dataclass
class _MetadataExcludedTransitiveChildDataclass:
    grandchild: str = dataclass_field(metadata={"exclude": True})


@dataclass
class _InheritedSerializedTransitiveDataclassBase:
    @field_serializer("grandchild", check_fields=False)
    def serialize_grandchild(self, value: str) -> dict[str, str]:
        return {"relocated": value}


@dataclass
class _InheritedSerializedTransitiveChildDataclass(
    _InheritedSerializedTransitiveDataclassBase,
):
    grandchild: str


@dataclass
class _AliasedOmittedTransitiveChildDataclass:
    legacy_grandchild: Annotated[
        str,
        Field(serialization_alias="grandchild"),
    ]


@dataclass
class _DefaultAliasedOmittedTransitiveChildDataclass:
    legacy_grandchild: str = Field(serialization_alias="grandchild")


@dataclass
class _MetadataAliasedOmittedTransitiveChildDataclass:
    legacy_grandchild: str = dataclass_field(
        metadata={
            "validation_alias": "grandchild",
            "serialization_alias": "grandchild",
        },
    )


@dataclass
class _DefaultOverridesAnnotatedOmittedTransitiveChildDataclass:
    legacy_grandchild: Annotated[
        str,
        Field(serialization_alias="grandchild"),
    ] = Field(serialization_alias="legacy_grandchild")


@dataclass
class _EmptyAliasOverridesAnnotatedOmittedTransitiveChildDataclass:
    legacy_grandchild: Annotated[
        str,
        Field(
            validation_alias="grandchild",
            serialization_alias="grandchild",
        ),
    ] = Field(validation_alias="", serialization_alias="")


@dataclass
class _ClearedSerializationAliasOmittedTransitiveChildDataclass:
    legacy_grandchild: Annotated[
        str,
        Field(
            alias="legacy-wire",
            serialization_alias="grandchild",
        ),
        Field(serialization_alias=None),
    ]


@dataclass
class _DefaultOccupiesAnnotatedOmittedTransitiveChildDataclass:
    legacy_grandchild: Annotated[
        str,
        Field(serialization_alias="legacy_grandchild"),
    ] = Field(serialization_alias="grandchild")


@dataclass
class _MetadataOccupiesAnnotatedOmittedTransitiveChildDataclass:
    legacy_grandchild: Annotated[
        str,
        Field(serialization_alias="legacy_grandchild"),
    ] = dataclass_field(metadata={"serialization_alias": "grandchild"})


class _LaterAnnotatedAliasOmittedTransitiveChildTypedDict(TypedDict):
    legacy_grandchild: Annotated[
        str,
        Field(serialization_alias="legacy_grandchild"),
        Field(serialization_alias="grandchild"),
    ]


def _generated_transitive_alias(field_name: str) -> str:
    return f"wire_{field_name}"


@with_config(ConfigDict(alias_generator=_generated_transitive_alias))
@dataclass
class _GeneratedAliasTransitiveChildDataclass:
    grandchild: str


@with_config(ConfigDict(alias_generator=_generated_transitive_alias))
class _GeneratedAliasTransitiveChildTypedDict(TypedDict):
    grandchild: str


class _AliasedOmittedTransitiveChildTypedDict(TypedDict):
    legacy_grandchild: Annotated[
        str,
        Field(serialization_alias="grandchild"),
    ]


type _transitive_representations[Item] = list[Item] | str


def _managed_child_family(
    *,
    model: type[BaseModel],
    child_family: SchemaFamily[Any],
    annotation: Any,
    case: str,
    prefix: str,
    defer_build: bool = False,
) -> SchemaFamily[Any]:
    historical_parent = create_model(
        f"Explicit{prefix.title().replace('_', '')}{case.title().replace('_', '')}HistoricalParent",
        __config__=ConfigDict(defer_build=True) if defer_build else None,
        child=(annotation, ...),
    )
    return SchemaFamily(
        model=model,
        name=f"explicit_annotation_{prefix}_{case}",
        versions=(
            SchemaVersion("1", wire_model=historical_parent),
            SchemaVersion("2"),
        ),
        nested=(NestedFamily("child", child_family, matching_labels()),),
    )


def _transitive_family(
    annotation: Any,
    *,
    case: str,
    defer_build: bool = False,
) -> SchemaFamily[Any]:
    return _managed_child_family(
        model=_TransitiveParent,
        child_family=_TRANSITIVE_CHILD_FAMILY,
        annotation=annotation,
        case=case,
        prefix="transitive_parent",
        defer_build=defer_build,
    )


def _deep_transitive_family(
    wrapper_annotation: Any,
    *,
    case: str,
) -> SchemaFamily[Any]:
    historical_child = create_model(
        f"ExplicitDeepTransitive{case.title().replace('_', '')}HistoricalChild",
        wrapper=(wrapper_annotation, ...),
    )
    return _managed_child_family(
        model=_DeepTransitiveParent,
        child_family=_DEEP_TRANSITIVE_CHILD_FAMILY,
        annotation=historical_child,
        case=case,
        prefix="deep_transitive_parent",
    )


@pytest.mark.parametrize(
    ("case", "annotation"),
    [
        ("model", _BroadTransitiveChildModel),
        ("dataclass", _BroadTransitiveChildDataclass),
        ("typed_dict", _BroadTransitiveChildTypedDict),
        ("bound_generic_typed_dict", _GenericTransitiveChildTypedDict[Any]),
        (
            "qualified_generic_typed_dict",
            _QualifiedGenericTransitiveChildTypedDict[Any],
        ),
        (
            "generic_typed_dict_union",
            _GenericTransitiveChildTypedDict[int] | _GenericTransitiveChildTypedDict[Any],
        ),
    ],
)
def test_structural_child_representation_recurses_into_nested_route_annotations(
    case: str,
    annotation: Any,
) -> None:
    family = _transitive_family(annotation, case=case)

    with pytest.raises(UnsupportedWireModelError, match="broad annotation") as exc_info:
        family.model_for("1")

    message = str(exc_info.value)
    assert "explicit_annotation_transitive_child" in message
    assert "nested path ('grandchild',)" in message


@pytest.mark.parametrize(
    ("case", "annotation"),
    [
        (
            "wrapped",
            _deep_wrapper_representations[_DeepWrapperAlias],
        ),
        ("recursive", _recursive_deep_wrapper),
    ],
)
def test_structural_child_representation_recurses_through_multicomponent_routes(
    case: str,
    annotation: Any,
) -> None:
    family = _deep_transitive_family(annotation, case=case)

    with pytest.raises(UnsupportedWireModelError, match="broad annotation") as exc_info:
        family.model_for("1")

    assert "nested path ('wrapper', 'grandchild')" in str(exc_info.value)


@pytest.mark.parametrize(
    ("case", "annotation"),
    [
        (
            "stdlib",
            _GenericTransitiveChildDataclass[str]
            | _GenericTransitiveChildDataclass[_HistoricalChildModel],
        ),
        (
            "pydantic",
            _GenericTransitiveChildPydanticDataclass[str]
            | _GenericTransitiveChildPydanticDataclass[_HistoricalChildModel],
        ),
        (
            "plain_alias",
            _StringTransitiveChildDataclass | _ModelTransitiveChildDataclass,
        ),
        (
            "bound_type_parameter",
            _transitive_dataclass_union[_GenericTransitiveChildDataclass[_HistoricalChildModel]],
        ),
        (
            "constrained_type_parameter",
            _ambiguous_transitive_dataclass_constraint,
        ),
        (
            "applied_alias_arms",
            _transitive_dataclass_identity[_GenericTransitiveChildDataclass[str]]
            | _transitive_dataclass_identity[
                _GenericTransitiveChildDataclass[_HistoricalChildModel]
            ],
        ),
        (
            "new_type",
            _StringTransitiveChildDataclassNewType
            | _GenericTransitiveChildDataclass[_HistoricalChildModel],
        ),
        (
            "annotated",
            Annotated[
                _GenericTransitiveChildDataclass[str],
                Field(description="legacy child"),
            ]
            | _GenericTransitiveChildDataclass[_HistoricalChildModel],
        ),
        (
            "list",
            list[_GenericTransitiveChildDataclass[str]]
            | list[_GenericTransitiveChildDataclass[_HistoricalChildModel]],
        ),
        (
            "coercible_list_tuple",
            list[_GenericTransitiveChildDataclass[str]]
            | tuple[_GenericTransitiveChildDataclass[_HistoricalChildModel], ...],
        ),
        (
            "coercible_tuple_list_reversed",
            tuple[_GenericTransitiveChildDataclass[_HistoricalChildModel], ...]
            | list[_GenericTransitiveChildDataclass[str]],
        ),
        (
            "same_fixed_tuple_position",
            tuple[_GenericTransitiveChildDataclass[str], str]
            | tuple[_GenericTransitiveChildDataclass[_HistoricalChildModel], str],
        ),
        (
            "typed_dict_erased_origin",
            _GenericDataclassTransitiveTypedDict[str]
            | _GenericDataclassTransitiveTypedDict[_HistoricalChildModel],
        ),
    ],
)
def test_managed_union_rejects_distinct_generic_dataclass_parameterizations(
    case: str,
    annotation: Any,
) -> None:
    family = _transitive_family(
        annotation,
        case=f"ambiguous_generic_dataclass_union_{case}",
    )

    with pytest.raises(
        UnsupportedWireModelError,
        match="multiple parameterizations of the same dataclass origin",
    ):
        family.model_for("1")


@pytest.mark.parametrize(
    ("case", "annotation"),
    [
        ("dataclass", _SafeTransitiveChildDataclass),
        (
            "dataclass_default_overrides_exclusion",
            _DefaultOverridesAnnotatedManagedChildDataclass,
        ),
        (
            "typed_dict_later_annotated_field_info",
            _LaterAnnotatedFieldInfoWinsManagedChildTypedDict,
        ),
        ("generic_typed_dict", _GenericTransitiveChildTypedDict[int]),
        (
            "qualified_generic_typed_dict",
            _QualifiedGenericTransitiveChildTypedDict[int],
        ),
        ("defaulted_generic_typed_dict", _DefaultGenericTransitiveChildTypedDict),
        ("omitted", _OmittedTransitiveChildDataclass),
        (
            "omitted_default_overrides_annotated_alias",
            _DefaultOverridesAnnotatedOmittedTransitiveChildDataclass,
        ),
        (
            "omitted_empty_alias_overrides_annotated_alias",
            _EmptyAliasOverridesAnnotatedOmittedTransitiveChildDataclass,
        ),
        (
            "omitted_explicitly_cleared_serialization_alias",
            _ClearedSerializationAliasOmittedTransitiveChildDataclass,
        ),
        (
            "alias_collection_union",
            _transitive_representations[_SafeTransitiveChildDataclass],
        ),
        (
            "generic_dataclass_distinct_fixed_tuple_positions",
            tuple[_GenericTransitiveChildDataclass[str], str]
            | tuple[str, _GenericTransitiveChildDataclass[_HistoricalChildModel]],
        ),
    ],
)
def test_structural_child_representation_preserves_safe_and_omitted_semantics(
    case: str,
    annotation: Any,
) -> None:
    family = _transitive_family(annotation, case=f"safe_{case}")

    historical = family.model_for("1")

    assert "child" in historical.model_fields


@pytest.mark.parametrize(
    ("case", "annotation", "diagnostic"),
    [
        (
            "direct",
            _SerializedTransitiveChildDataclass,
            "serializes field 'grandchild'",
        ),
        (
            "inherited",
            _InheritedSerializedTransitiveChildDataclass,
            "serializes field 'grandchild'",
        ),
        (
            "pydantic",
            _PydanticSerializedTransitiveChildDataclass,
            "serializes field 'grandchild'",
        ),
        (
            "annotation",
            _AnnotatedSerializedTransitiveChildDataclass,
            "annotation-level serializer on field 'grandchild'",
        ),
    ],
)
def test_structural_child_representation_rejects_inner_serializer(
    case: str,
    annotation: Any,
    diagnostic: str,
) -> None:
    family = _transitive_family(annotation, case=f"inner_serializer_{case}")

    with pytest.raises(UnsupportedWireModelError, match=diagnostic):
        family.model_for("1")


@pytest.mark.parametrize(
    ("case", "annotation"),
    [
        ("model", _ExcludedTransitiveChildModel),
        ("dataclass", _ExcludedTransitiveChildDataclass),
        ("dataclass_metadata", _MetadataExcludedTransitiveChildDataclass),
        ("typed_dict", _ExcludedTransitiveChildTypedDict),
    ],
)
def test_transitive_explicit_historical_managed_path_rejects_serialization_exclusion(
    case: str,
    annotation: Any,
) -> None:
    family = _transitive_family(annotation, case=f"inner_exclusion_{case}")

    with pytest.raises(UnsupportedWireModelError, match="excludes field 'grandchild'"):
        family.model_for("1")


@pytest.mark.parametrize(
    ("case", "annotation"),
    [
        ("dataclass", _GeneratedAliasTransitiveChildDataclass),
        ("typed_dict", _GeneratedAliasTransitiveChildTypedDict),
    ],
)
def test_structural_child_representation_rejects_unmaterialized_alias_generator(
    case: str,
    annotation: Any,
) -> None:
    family = _transitive_family(annotation, case=f"alias_generator_{case}")

    with pytest.raises(
        UnsupportedWireModelError,
        match="unmaterialized alias_generator",
    ):
        family.model_for("1")


def test_structural_child_representation_rejects_json_encoders() -> None:
    with pytest.warns(DeprecationWarning, match="json_encoders"):

        class JsonEncodedChild(BaseModel):
            model_config = ConfigDict(
                json_encoders={str: lambda value: {"relocated": value}},
            )

            grandchild: str

    family = _transitive_family(JsonEncodedChild, case="json_encoders")

    with pytest.raises(UnsupportedWireModelError, match="configures json_encoders"):
        family.model_for("1")


def test_structural_child_representation_rejects_unresolved_field_hints() -> None:
    @dataclass
    class LocalChild:
        grandchild: _OnlyVisibleInThisTest

    class _OnlyVisibleInThisTest(BaseModel):
        value: int

    family = _transitive_family(
        LocalChild,
        case="unresolved_field_hints",
        defer_build=True,
    )

    with pytest.raises(
        UnsupportedWireModelError,
        match="unresolved field annotations",
    ):
        family.model_for("1")


@pytest.mark.parametrize(
    ("case", "annotation"),
    [
        ("model", _ComputedOmittedTransitiveChildModel),
        (
            "pydantic_dataclass",
            _ComputedOmittedTransitiveChildPydanticDataclass,
        ),
    ],
)
def test_structural_child_representation_reserves_omitted_computed_output(
    case: str,
    annotation: Any,
) -> None:
    family = _transitive_family(annotation, case=f"omitted_computed_{case}")

    with pytest.raises(
        UnsupportedWireModelError,
        match="computed field 'legacy_grandchild'.*managed component 'grandchild'",
    ):
        family.model_for("1")


@pytest.mark.parametrize(
    ("case", "annotation"),
    [
        ("dataclass", _AliasedOmittedTransitiveChildDataclass),
        ("dataclass_default", _DefaultAliasedOmittedTransitiveChildDataclass),
        ("dataclass_metadata", _MetadataAliasedOmittedTransitiveChildDataclass),
        (
            "dataclass_default_precedence",
            _DefaultOccupiesAnnotatedOmittedTransitiveChildDataclass,
        ),
        (
            "dataclass_metadata_precedence",
            _MetadataOccupiesAnnotatedOmittedTransitiveChildDataclass,
        ),
        ("typed_dict", _AliasedOmittedTransitiveChildTypedDict),
        (
            "typed_dict_later_annotated_alias",
            _LaterAnnotatedAliasOmittedTransitiveChildTypedDict,
        ),
    ],
)
def test_structural_child_representation_reserves_omitted_nested_route_alias(
    case: str,
    annotation: Any,
) -> None:
    family = _transitive_family(annotation, case=f"omitted_alias_{case}")

    with pytest.raises(
        UnsupportedWireModelError,
        match="occupies managed component 'grandchild'.*alias",
    ):
        family.model_for("1")


def test_omitted_managed_path_rejects_field_alias_occupancy() -> None:
    class HistoricalParent(BaseModel):
        legacy_child: _HistoricalChildModel = Field(
            validation_alias="child",
            serialization_alias="child",
        )

    family = SchemaFamily(
        model=_Parent,
        name="explicit_annotation_omitted_alias",
        versions=(
            SchemaVersion("1", wire_model=HistoricalParent),
            SchemaVersion("2"),
        ),
        nested=(NestedFamily("child", _CHILD_FAMILY, matching_labels()),),
    )

    with pytest.raises(
        UnsupportedWireModelError,
        match="field 'legacy_child'.*managed component 'child'.*alias",
    ):
        family.model_for("1")


def test_omitted_managed_path_rejects_computed_field_output_occupancy() -> None:
    class ExactHistoricalParent(BaseModel):
        @computed_field
        @property
        def child(self) -> dict[str, Any]:
            return {"value": 1, "schema_version": "secret"}

    class AliasedHistoricalParent(BaseModel):
        @computed_field(alias="child")
        @property
        def derived_child(self) -> dict[str, Any]:
            return {"value": 1, "schema_version": "secret"}

    for case, historical_parent, computed_name in (
        ("exact", ExactHistoricalParent, "child"),
        ("alias", AliasedHistoricalParent, "derived_child"),
    ):
        family = SchemaFamily(
            model=_Parent,
            name=f"explicit_annotation_omitted_computed_{case}",
            versions=(
                SchemaVersion("1", wire_model=historical_parent),
                SchemaVersion("2"),
            ),
            nested=(NestedFamily("child", _CHILD_FAMILY, matching_labels()),),
        )

        with pytest.raises(
            UnsupportedWireModelError,
            match=rf"computed field '{computed_name}'.*managed component 'child'",
        ):
            family.model_for("1")


def test_unrelated_broad_fields_do_not_affect_the_managed_path_contract() -> None:
    class HistoricalParent(BaseModel):
        child: str
        unrelated_payload: Any = "visible"
        unrelated_mapping: Mapping[str, Any] = Field(default_factory=dict)
        unrelated_excluded: str = Field("excluded", exclude=True)
        unrelated_conditionally_excluded: str = Field(
            "conditionally-excluded",
            exclude_if=lambda value: value == "conditionally-excluded",
        )

    def upgrade(data: dict[str, Any]) -> dict[str, Any]:
        return {"child": {"value": int(data["child"])}}

    def downgrade(data: dict[str, Any]) -> dict[str, Any]:
        return {"child": str(data["child"]["value"])}

    family = SchemaFamily(
        model=_Parent,
        name="explicit_annotation_unrelated_broad_fields",
        versions=(
            SchemaVersion("1", wire_model=HistoricalParent),
            SchemaVersion("2"),
        ),
        transitions=(
            VersionTransition(
                "1",
                "2",
                upgrade=upgrade,
                downgrade=downgrade,
                downgrade_semantics="exact",
            ),
        ),
        nested=(NestedFamily("child", _CHILD_FAMILY, matching_labels()),),
    )

    historical = family.model_for("1")

    assert {
        "child",
        "unrelated_payload",
        "unrelated_mapping",
        "unrelated_excluded",
        "unrelated_conditionally_excluded",
        "schema_version",
    } == set(historical.model_fields)
    assert family.dump(version="1", data=_Parent(child=_Child(value=7))) == {
        "child": "7",
        "unrelated_payload": "visible",
        "unrelated_mapping": {},
        "schema_version": "1",
    }


@pytest.mark.parametrize(
    ("case", "annotation", "diagnostic"),
    [
        ("custom_container", _CustomList, "abstract or custom container"),
        ("opaque_annotation", Never, "unsupported opaque annotation"),
    ],
)
def test_explicit_managed_leaf_rejects_arbitrary_shape_erasing_types(
    case: str,
    annotation: Any,
    diagnostic: str,
) -> None:
    if annotation is Never:
        with pytest.warns(UserWarning, match="not a Python type"):
            family = _leaf_family(
                annotation,
                case=case,
                arbitrary_types_allowed=True,
            )
    else:
        family = _leaf_family(
            annotation,
            case=case,
            arbitrary_types_allowed=True,
        )

    with pytest.raises(UnsupportedWireModelError, match=diagnostic):
        family.model_for("1")


class _Wrapper(BaseModel):
    child: _Child


class _WrappedParent(BaseModel):
    wrapper: _Wrapper


class _HistoricalWrapperModel(BaseModel):
    child: str


@dataclass
class _HistoricalWrapperDataclass:
    child: str


class _HistoricalWrapperTypedDict(TypedDict):
    child: str


def _intermediate_family(annotation: Any, *, case: str) -> SchemaFamily[_WrappedParent]:
    historical_parent = create_model(
        f"ExplicitIntermediate{case.title().replace('_', '')}HistoricalParent",
        wrapper=(annotation, ...),
    )
    return SchemaFamily(
        model=_WrappedParent,
        name=f"explicit_intermediate_annotation_{case}",
        versions=(
            SchemaVersion("1", wire_model=historical_parent),
            SchemaVersion("2"),
        ),
        nested=(NestedFamily(("wrapper", "child"), _CHILD_FAMILY, matching_labels()),),
    )


@pytest.mark.parametrize(
    ("case", "annotation", "diagnostic"),
    [
        ("dataclass", _HistoricalWrapperDataclass, "dataclass at an intermediate"),
        ("typed_dict", _HistoricalWrapperTypedDict, "TypedDict at an intermediate"),
        ("broad", Any, "broad annotation"),
    ],
)
def test_explicit_managed_path_rejects_untraversed_structured_intermediates(
    case: str,
    annotation: Any,
    diagnostic: str,
) -> None:
    family = _intermediate_family(annotation, case=case)

    with pytest.raises(UnsupportedWireModelError, match=diagnostic):
        family.model_for("1")


@pytest.mark.parametrize(
    ("case", "annotation"),
    [
        ("model_collection", list[_HistoricalWrapperModel]),
        ("model_or_scalar", _HistoricalWrapperModel | str),
    ],
)
def test_explicit_managed_path_accepts_traversable_or_scalar_intermediates(
    case: str,
    annotation: Any,
) -> None:
    family = _intermediate_family(annotation, case=f"safe_{case}")

    historical = family.model_for("1")

    assert "wrapper" in historical.model_fields
