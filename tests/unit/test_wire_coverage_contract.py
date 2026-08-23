from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, make_dataclass
from typing import Annotated, Any, ClassVar, Literal, NewType, TypedDict, cast

import pytest
from annotated_types import GroupedMetadata, Predicate
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    GetPydanticSchema,
    PlainSerializer,
    Tag,
    computed_field,
    create_model,
    field_validator,
    model_serializer,
)
from pydantic.dataclasses import dataclass as pydantic_dataclass
from typing_extensions import TypeVar

from pydantic_versions import (
    NestedFamily,
    SchemaFamily,
    SchemaVersion,
    UnsupportedWireModelError,
    VersionMetadata,
    field_removed,
    field_renamed,
    matching_labels,
    model_for_version,
    schema_version,
    versioned_schema,
)


def test_explicit_document_rejects_malformed_object_properties_schema() -> None:
    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        model_config = ConfigDict(json_schema_extra={"properties": "not-an-object"})

        value: int = 1

    family = SchemaFamily(
        model=Current,
        name="malformed_explicit_properties",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
    )

    with pytest.raises(
        UnsupportedWireModelError,
        match="malformed validation schema: object properties must be a mapping",
    ):
        family.model_for("1")


def test_nested_wrapper_schema_metadata_is_snapshotted() -> None:
    schema_extra = {"x-contract": {"state": "before"}}

    class Child(BaseModel):
        value: int = 1

    child_family = SchemaFamily(
        model=Child,
        name="nested_schema_snapshot_child",
        versions=(
            SchemaVersion("1", patches=(field_renamed("value", "legacy_value"),)),
            SchemaVersion("2"),
        ),
    )

    class Wrapper(BaseModel):
        model_config = ConfigDict(json_schema_extra=schema_extra)

        child: Child

    class Parent(BaseModel):
        wrapper: Wrapper

    family = SchemaFamily(
        model=Parent,
        name="nested_schema_snapshot_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        nested=(NestedFamily(("wrapper", "child"), child_family, matching_labels()),),
    )

    wire = family.model_for("1")
    wrapper_wire = cast(type[BaseModel], wire.model_fields["wrapper"].annotation)
    wire_extra = cast(dict[str, Any], wrapper_wire.model_config["json_schema_extra"])

    assert wire_extra is not schema_extra
    assert wire_extra["x-contract"] is not schema_extra["x-contract"]
    schema_extra["x-contract"]["state"] = "after"
    assert wrapper_wire.model_json_schema()["x-contract"] == {"state": "before"}


def test_nested_wrapper_rejects_structural_schema_metadata() -> None:
    class Child(BaseModel):
        value: int = 1

    child_family = SchemaFamily(
        model=Child,
        name="nested_structural_schema_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    )

    class Wrapper(BaseModel):
        model_config = ConfigDict(
            json_schema_extra={"properties": {"fabricated": {"type": "string"}}},
        )

        child: Child

    class Parent(BaseModel):
        wrapper: Wrapper

    family = SchemaFamily(
        model=Parent,
        name="nested_structural_schema_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        nested=(NestedFamily(("wrapper", "child"), child_family, matching_labels()),),
    )

    with pytest.raises(
        UnsupportedWireModelError,
        match="nested wrapper .* cannot override generated structure: properties",
    ):
        family.model_for("1")


def test_family_document_adapter_rejects_non_object_serializer_output() -> None:
    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        value: int = 1

        @model_serializer
        def serialize_model(self) -> dict[str, Any]:
            return cast(Any, "not-an-object")

    family = SchemaFamily(
        model=Current,
        name="family_document_scalar_serializer",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
    )

    with pytest.raises(ValueError, match="must serialize to an object"):
        family.defaults_for(version="1", warnings=False)


def test_family_document_adapter_metadata_assignment_and_fallback_attributes() -> None:
    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        value: int = 1

    family = SchemaFamily(
        model=Current,
        name="family_document_assignment_contract",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
    )
    target = cast(Any, family.model_for("1"))
    document = target(value=3, schema_version="1")

    document.schema_version = "1"
    assert document.model_dump() == {"value": 3, "schema_version": "1"}
    with pytest.raises(ValueError, match="expected '1'"):
        document.schema_version = "wrong"

    document._transient = "temporary"
    assert document._transient == "temporary"
    del document._transient
    with pytest.raises(AttributeError):
        _ = document._transient


@pytest.mark.parametrize(
    ("patch", "message"),
    (
        (field_removed("schema_version"), "cannot be removed"),
        (
            field_renamed("schema_version", "legacy_schema_version"),
            "must keep one invariant wire location",
        ),
    ),
)
def test_model_owned_metadata_cannot_be_removed_or_renamed(
    patch: Any,
    message: str,
) -> None:
    class Payload(BaseModel):
        schema_version: Literal["2"] = "2"
        value: int = 1

    family = SchemaFamily(
        model=Payload,
        name=f"model_metadata_patch_{type(patch).__name__}",
        versions=(
            SchemaVersion("1", patches=(patch,)),
            SchemaVersion("2"),
        ),
        version_metadata=VersionMetadata("schema_version", owner="model"),
    )

    with pytest.raises(UnsupportedWireModelError, match=message):
        family.model_for("1")


def test_public_field_and_model_safety_rejections_have_specific_diagnostics() -> None:
    class Cat(BaseModel):
        kind: Literal["cat"]

    class Dog(BaseModel):
        kind: Literal["dog"]

    class CustomDiscriminator(Discriminator):
        pass

    class UnknownConfigPayload(BaseModel):
        model_config = cast(ConfigDict, {"unknown_wire_option": True})

        value: int

    class FieldTitlePayload(BaseModel):
        value: int = Field(field_title_generator=lambda name, _info: name)

    class CustomDiscriminatorPayload(BaseModel):
        pet: Cat | Dog = Field(discriminator=CustomDiscriminator("kind"))

    class PredicatePayload(BaseModel):
        value: Annotated[int, Predicate(lambda value: value > 0)]

    class CustomGroup(GroupedMetadata):
        def __iter__(self) -> Iterator[Any]:
            yield Predicate(lambda value: value > 0)

    class GroupedMetadataPayload(BaseModel):
        value: Annotated[int, CustomGroup()]

    tagged = Annotated[Cat, Tag("cat")] | Annotated[Dog, Tag("dog")]

    class CallableDiscriminatorPayload(BaseModel):
        pet: tagged = Field(
            discriminator=Discriminator(
                lambda value: (
                    value.get("kind")
                    if isinstance(value, Mapping)
                    else getattr(value, "kind", None)
                ),
            ),
        )

    cases = (
        (UnknownConfigPayload, "unsupported model configuration keys.*unknown_wire_option"),
        (FieldTitlePayload, "non-declarative attribute 'field_title_generator'"),
        (CustomDiscriminatorPayload, "custom discriminator subtype"),
        (PredicatePayload, "callable predicate metadata"),
        (GroupedMetadataPayload, "custom executable grouped metadata"),
        (CallableDiscriminatorPayload, "callable discriminator"),
    )
    for index, (model, message) in enumerate(cases):
        family = SchemaFamily(
            model=model,
            name=f"specific_wire_diagnostic_{index}",
            versions=(SchemaVersion("1"),),
            version_metadata=None,
        )
        with pytest.raises(UnsupportedWireModelError, match=message):
            family.model_for("1")


def test_behavioral_type_parameters_are_rejected() -> None:
    behavioral = Annotated[int, AfterValidator(lambda value: value)]
    defaulted_type = TypeVar("defaulted_type", default=behavioral)
    bounded_type = TypeVar("bounded_type", bound=behavioral)
    constrained_type = TypeVar("constrained_type", behavioral, str)
    parameters = (defaulted_type, bounded_type, constrained_type)

    for index, value_type in enumerate(parameters):
        payload_model = create_model(
            f"BehavioralTypeParameterPayload{index}",
            value=(value_type, ...),
        )
        family = SchemaFamily(
            model=payload_model,
            name=f"behavioral_type_parameter_{index}",
            versions=(SchemaVersion("1"),),
            version_metadata=None,
        )

        with pytest.raises(UnsupportedWireModelError, match="behavioral type parameter"):
            family.model_for("1")


def test_decorator_children_hidden_by_alias_and_typed_dict_fail_closed() -> None:
    @versioned_schema(name="hidden_decorator_child", versions=("1", "2"), current="2")
    class Child(BaseModel):
        value: int

    type child_alias = Annotated[Child, Field(description="hidden child")]

    @versioned_schema(name="aliased_decorator_parent", versions=("1", "2"), current="2")
    class AliasParent(BaseModel):
        child: child_alias

    class ChildEnvelope(TypedDict):
        child: Child

    @versioned_schema(name="typed_dict_decorator_parent", versions=("1", "2"), current="2")
    class TypedDictParent(BaseModel):
        envelope: ChildEnvelope

    with pytest.raises(UnsupportedWireModelError, match="hidden in a type alias"):
        model_for_version(AliasParent, "1")
    with pytest.raises(
        UnsupportedWireModelError,
        match="uses a behavioral structured annotation",
    ):
        model_for_version(TypedDictParent, "1")


def test_decorator_child_cannot_be_a_mapping_key() -> None:
    @versioned_schema(name="mapping_key_decorator_child", versions=("1", "2"), current="2")
    class Child(BaseModel):
        value: int

    @versioned_schema(name="mapping_key_decorator_parent", versions=("1", "2"), current="2")
    class Parent(BaseModel):
        values: dict[Child, str]

    with pytest.raises(UnsupportedWireModelError, match="cannot be used as a mapping key"):
        model_for_version(Parent, "1")


def test_explicit_wire_parent_rejects_implicit_decorator_child() -> None:
    @versioned_schema(name="explicit_parent_decorator_child", versions=("1", "2"), current="2")
    class Child(BaseModel):
        value: int

    class HistoricalParent(BaseModel):
        child: Child

    @versioned_schema(name="explicit_parent_with_decorator_child", versions=("1", "2"), current="2")
    @schema_version("1", wire_model=HistoricalParent)
    class Parent(BaseModel):
        child: Child

    with pytest.raises(
        UnsupportedWireModelError,
        match="explicit wire models cannot contain decorator-discovered child families",
    ):
        model_for_version(Parent, "1")


def test_single_unused_nested_declaration_names_its_path() -> None:
    class Child(BaseModel):
        value: int

    child_family = SchemaFamily(
        model=Child,
        name="single_unused_child",
        versions=(SchemaVersion("1"),),
    )

    class Parent(BaseModel):
        value: int

    family = SchemaFamily(
        model=Parent,
        name="single_unused_parent",
        versions=(SchemaVersion("1"),),
        nested=(NestedFamily("missing", child_family, matching_labels()),),
    )

    with pytest.raises(
        UnsupportedWireModelError,
        match=r"nested declaration path \('missing',\) does not match",
    ):
        family.model_for("1")


def test_model_metadata_rejects_computed_output_collision() -> None:
    class Current(BaseModel):
        schema_version: Literal["2"] = Field("2", alias="wire_version")
        value: int = 1

    class Historical(BaseModel):
        schema_version: Literal["1"] = Field("1", alias="wire_version")
        value: int = 1

        @computed_field(alias="wire_version")
        @property
        def repeated_version(self) -> str:
            return "1"

    family = SchemaFamily(
        model=Current,
        name="computed_model_metadata_collision",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
        version_metadata=VersionMetadata("wire_version", owner="model"),
    )

    with pytest.raises(UnsupportedWireModelError, match="overlaps computed field"):
        family.model_for("1")


def test_explicit_wire_model_may_omit_declared_nested_path() -> None:
    class Child(BaseModel):
        value: int = 1

    child_family = SchemaFamily(
        model=Child,
        name="omitted_explicit_nested_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    )

    class Parent(BaseModel):
        child: Child

    class HistoricalParent(BaseModel):
        note: str = "historical"

    family = SchemaFamily(
        model=Parent,
        name="omitted_explicit_nested_parent",
        versions=(
            SchemaVersion("1", wire_model=HistoricalParent),
            SchemaVersion("2"),
        ),
        nested=(NestedFamily("child", child_family, matching_labels()),),
    )

    historical = family.model_for("1")
    assert "child" not in historical.model_fields
    assert historical.model_validate({"schema_version": "1"}).model_dump() == {
        "note": "historical",
        "schema_version": "1",
    }


def test_projected_wrapper_rejects_a_wrongly_typed_default() -> None:
    class Child(BaseModel):
        value: int = 1

    child_family = SchemaFamily(
        model=Child,
        name="wrong_wrapper_default_child",
        versions=(
            SchemaVersion("1", patches=(field_renamed("value", "legacy_value"),)),
            SchemaVersion("2"),
        ),
    )

    class Wrapper(BaseModel):
        child: Child

    class Parent(BaseModel):
        wrapper: Wrapper = cast(Any, {"child": {"value": 1}})

    family = SchemaFamily(
        model=Parent,
        name="wrong_wrapper_default_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        nested=(NestedFamily(("wrapper", "child"), child_family, matching_labels()),),
    )

    with pytest.raises(UnsupportedWireModelError, match="default changed type unexpectedly"):
        family.model_for("1")


def test_nested_child_default_does_not_execute_new_target_factories() -> None:
    class Child(BaseModel):
        value: int = 1

    class HistoricalChild(BaseModel):
        value: int = 1
        generated: list[int] = Field(default_factory=list)

    child_family = SchemaFamily(
        model=Child,
        name="target_factory_default_child",
        versions=(
            SchemaVersion("1", wire_model=HistoricalChild),
            SchemaVersion("2"),
        ),
        version_metadata=None,
    )

    class Parent(BaseModel):
        child: Child = Child()

    family = SchemaFamily(
        model=Parent,
        name="target_factory_default_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        nested=(NestedFamily("child", child_family, matching_labels()),),
    )

    with pytest.raises(
        UnsupportedWireModelError,
        match="nested model default would execute default factories.*generated",
    ):
        family.model_for("1")


def test_nested_family_metadata_validates_attribute_sources() -> None:
    class Current(BaseModel):
        value: int

    class Historical(BaseModel):
        model_config = ConfigDict(from_attributes=True)

        value: int

    family = SchemaFamily(
        model=Current,
        name="nested_attribute_metadata",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
        version_metadata=VersionMetadata(("contract", "version"), owner="family"),
    )
    historical = family.model_for("1")

    class Contract:
        def __init__(self, version: str, *, sibling: bool = False) -> None:
            self.version = version
            if sibling:
                self.note = "not reserved metadata"

    class Source:
        def __init__(self, contract: Any) -> None:
            self.value = 3
            self.contract = contract

    validated = historical.model_validate(Source(Contract("1")))
    assert validated.model_dump() == {
        "value": 3,
        "contract": {"version": "1"},
    }

    with pytest.raises(ValueError, match="complete metadata path.*without siblings"):
        historical.model_validate(Source(Contract("1", sibling=True)))
    with pytest.raises(ValueError, match="is 'wrong'; expected '1'"):
        historical.model_validate(Source({"version": "wrong"}))

    class UnreadableSource:
        value = 3

        @property
        def contract(self) -> Any:
            msg = "unavailable"
            raise RuntimeError(msg)

    with pytest.raises(ValueError, match="could not be read.*'contract'"):
        historical.model_validate(UnreadableSource())


def test_behavioral_structured_annotation_hidden_in_nested_alias_is_rejected() -> None:
    class BehavioralChild(BaseModel):
        value: int

        @field_validator("value")
        @classmethod
        def validate_value(cls, value: int) -> int:
            return value

    @dataclass
    class Envelope:
        child: BehavioralChild

    type envelope_alias = Envelope
    type envelopes_alias = list[envelope_alias]

    class Payload(BaseModel):
        envelopes: envelopes_alias

    family = SchemaFamily(
        model=Payload,
        name="nested_alias_behavioral_structure",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )

    with pytest.raises(
        UnsupportedWireModelError,
        match="behavioral structured annotation hidden in a type alias",
    ):
        family.model_for("1")


def test_explicit_nested_serializer_hidden_by_alias_fails_closed() -> None:
    class Child(BaseModel):
        value: int

    child_family = SchemaFamily(
        model=Child,
        name="aliased_serializer_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        version_metadata=None,
    )
    type serialized_child = Annotated[
        Child,
        PlainSerializer(lambda _value: {}, return_type=dict[str, Any]),
    ]

    class HistoricalWrapper(BaseModel):
        child: serialized_child

    type historical_wrapper_alias = Annotated[
        tuple[HistoricalWrapper, ...] | Literal["absent"],
        Field(description="historical wrapper traversal"),
    ]

    class CurrentWrapper(BaseModel):
        child: Child

    class CurrentParent(BaseModel):
        wrapper: CurrentWrapper

    class HistoricalParent(BaseModel):
        wrapper: historical_wrapper_alias

    family = SchemaFamily(
        model=CurrentParent,
        name="aliased_nested_serializer_parent",
        versions=(
            SchemaVersion("1", wire_model=HistoricalParent),
            SchemaVersion("2"),
        ),
        nested=(NestedFamily(("wrapper", "child"), child_family, matching_labels()),),
    )

    with pytest.raises(UnsupportedWireModelError, match="annotation-level serializer"):
        family.compile()


def test_decorator_union_rejects_incompatible_metadata_contracts() -> None:
    @versioned_schema(name="union_family_metadata_child", versions=("1", "2"), current="2")
    class FamilyChild(BaseModel):
        value: int

    @versioned_schema(
        name="union_model_metadata_child",
        versions=("1", "2"),
        current="2",
        metadata_owner="model",
    )
    class ModelChild(BaseModel):
        schema_version: Literal["2"] = "2"
        value: int

    @versioned_schema(name="incompatible_union_parent", versions=("1", "2"), current="2")
    class Parent(BaseModel):
        child: FamilyChild | ModelChild

    with pytest.raises(
        UnsupportedWireModelError,
        match="incompatible version-metadata contracts",
    ):
        model_for_version(Parent, "1")


def test_decorator_union_and_collection_defaults_project_positive_paths() -> None:
    @versioned_schema(name="positive_default_child", versions=("1", "2"), current="2")
    @schema_version("1", patches=(field_renamed("value", "legacy_value"),))
    class Child(BaseModel):
        value: int

    class SpecializedChild(Child):
        pass

    @versioned_schema(name="positive_default_parent", versions=("1", "2"), current="2")
    class Parent(BaseModel):
        selected: Annotated[Child, Field(description="selected child")] | str = SpecializedChild(
            value=3
        )
        children: list[Child] = [Child(value=4)]
        optional_children: list[Child | None] = [None, Child(value=5)]

    historical = model_for_version(Parent, "1")

    assert historical().model_dump(mode="json") == {
        "selected": {"legacy_value": 3, "schema_version": "1"},
        "children": [{"legacy_value": 4, "schema_version": "1"}],
        "optional_children": [None, {"legacy_value": 5, "schema_version": "1"}],
        "schema_version": "1",
    }


def test_projected_wrapper_default_omits_non_wire_fields() -> None:
    class Child(BaseModel):
        value: int

    child_family = SchemaFamily(
        model=Child,
        name="positive_wrapper_default_child",
        versions=(
            SchemaVersion("1", patches=(field_renamed("value", "legacy_value"),)),
            SchemaVersion("2"),
        ),
        version_metadata=None,
    )

    class Wrapper(BaseModel):
        child: Child
        cache: str = Field("application-only", exclude=True)

    class Parent(BaseModel):
        wrapper: Wrapper = Wrapper(child=Child(value=5))

    family = SchemaFamily(
        model=Parent,
        name="positive_wrapper_default_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        nested=(NestedFamily(("wrapper", "child"), child_family, matching_labels()),),
        version_metadata=None,
    )
    historical = family.model_for("1")

    assert historical().model_dump() == {"wrapper": {"child": {"legacy_value": 5}}}


def test_projected_wrapper_subtype_does_not_execute_a_missing_base_factory() -> None:
    factory_calls = 0

    def generated_value() -> list[int]:
        nonlocal factory_calls
        factory_calls += 1
        return [1]

    class Child(BaseModel):
        value: int

    child_family = SchemaFamily(
        model=Child,
        name="missing_wrapper_factory_child",
        versions=(
            SchemaVersion("1", patches=(field_renamed("value", "legacy_value"),)),
            SchemaVersion("2"),
        ),
        version_metadata=None,
    )

    class Wrapper(BaseModel):
        child: Child
        generated: list[int] = Field(default_factory=generated_value)

    wrapper_without_generated = create_model(
        "WrapperWithoutGenerated",
        __base__=Wrapper,
        generated=(ClassVar[list[int]], []),
    )

    class Parent(BaseModel):
        wrapper: Wrapper = wrapper_without_generated(child=Child(value=5))

    family = SchemaFamily(
        model=Parent,
        name="missing_wrapper_factory_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        nested=(NestedFamily(("wrapper", "child"), child_family, matching_labels()),),
        version_metadata=None,
    )

    assert factory_calls == 0
    with pytest.raises(
        UnsupportedWireModelError,
        match="projected wrapper default would execute default factories.*generated",
    ):
        family.model_for("1")
    assert factory_calls == 0


def test_structured_annotations_detect_nested_runtime_behavior() -> None:
    behavioral = Annotated[int, AfterValidator(lambda value: value)]

    class BehavioralModel(BaseModel):
        value: int

        @field_validator("value")
        @classmethod
        def validate_value(cls, value: int) -> int:
            return value

    @pydantic_dataclass
    class BehavioralDataclass:
        value: int

        @field_validator("value")
        @classmethod
        def validate_value(cls, value: int) -> int:
            return value

    type behavioral_alias = Annotated[int, AfterValidator(lambda value: value)]
    behavioral_type = TypeVar("behavioral_type", bound=behavioral)
    behavioral_new_type = NewType("behavioral_new_type", BehavioralModel)

    class CustomGroup(GroupedMetadata):
        def __iter__(self) -> Iterator[Any]:
            yield Predicate(lambda value: value > 0)

    grouped_behavior = Annotated[int, CustomGroup()]
    custom_schema_behavior = Annotated[
        int,
        GetPydanticSchema(lambda source, handler: handler(source)),
    ]
    post_init_type = make_dataclass(
        "PostInitStructuredValue",
        [("value", int)],
        namespace={"__post_init__": lambda self: None},
    )
    cases = (
        BehavioralModel,
        BehavioralDataclass,
        behavioral_alias,
        behavioral_type,
        behavioral_new_type,
        grouped_behavior,
        custom_schema_behavior,
        post_init_type,
    )

    for index, annotation in enumerate(cases):
        structured = make_dataclass(
            f"RuntimeBehaviorEnvelope{index}",
            [("value", annotation)],
        )
        payload_model = create_model(
            f"RuntimeBehaviorPayload{index}",
            structured=(structured, ...),
        )
        family = SchemaFamily(
            model=payload_model,
            name=f"runtime_behavior_structure_{index}",
            versions=(SchemaVersion("1"),),
            version_metadata=None,
        )

        with pytest.raises(UnsupportedWireModelError, match="behavioral structured annotation"):
            family.model_for("1")

    safe_structured = make_dataclass(
        "SafeDeclarativeEnvelope",
        [
            ("literal", Literal["safe"]),
            ("annotated", Annotated[int, Field(description="safe")]),
        ],
    )
    safe_payload = create_model(
        "SafeDeclarativeStructuredPayload",
        structured=(safe_structured, ...),
    )
    safe_family = SchemaFamily(
        model=safe_payload,
        name="safe_declarative_structure",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )

    safe_family.model_for("1")


def test_safe_recursive_nested_and_generic_aliases_compile() -> None:
    type literal_alias = Literal["ok"]
    type nested_alias = list[literal_alias]
    type generic_alias[value_type] = list[value_type]
    type applied_alias = generic_alias[int]
    type recursive_alias = int | list[recursive_alias]
    type annotated_alias = Annotated[int, Field(description="safe declarative metadata")]

    class Payload(BaseModel):
        nested: nested_alias
        applied: applied_alias
        recursive: recursive_alias
        annotated: annotated_alias

    family = SchemaFamily(
        model=Payload,
        name="safe_recursive_nested_aliases",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )
    wire = family.model_for("1")

    assert wire.model_validate(
        {
            "nested": ["ok"],
            "applied": [1, 2],
            "recursive": [1, [2]],
            "annotated": 3,
        }
    ).model_dump() == {
        "nested": ["ok"],
        "applied": [1, 2],
        "recursive": [1, [2]],
        "annotated": 3,
    }


def test_ambiguous_decorator_union_default_fails_closed() -> None:
    @versioned_schema(name="ambiguous_default_child", versions=("1", "2"), current="2")
    @schema_version("1", patches=(field_renamed("value", "legacy_value"),))
    class Child(BaseModel):
        value: int

    class LeftWrapper(BaseModel):
        child: Child

    class RightWrapper(BaseModel):
        child: Child

    class AmbiguousWrapper(LeftWrapper, RightWrapper):
        pass

    @versioned_schema(name="ambiguous_default_parent", versions=("1", "2"), current="2")
    class Parent(BaseModel):
        wrapper: LeftWrapper | RightWrapper = AmbiguousWrapper(child=Child(value=1))

    with pytest.raises(UnsupportedWireModelError, match="ambiguous union arm"):
        model_for_version(Parent, "1")


def test_ordinary_wrapper_configuration_safety_is_fail_closed() -> None:
    @versioned_schema(name="wrapper_configuration_child", versions=("1", "2"), current="2")
    class Child(BaseModel):
        value: int

    class UnknownConfigWrapper(BaseModel):
        model_config = cast(ConfigDict, {"unknown_wrapper_option": True})

        child: Child

    class RejectedConfigWrapper(BaseModel):
        model_config = ConfigDict(arbitrary_types_allowed=True)

        child: Child

    class CallableSchemaWrapper(BaseModel):
        model_config = ConfigDict(json_schema_extra=lambda _schema: None)

        child: Child

    class CustomHookWrapper(BaseModel):
        child: Child

        @classmethod
        def __get_pydantic_json_schema__(cls, core_schema: Any, handler: Any) -> Any:
            return handler(core_schema)

    cases = (
        (UnknownConfigWrapper, "unsupported model configuration.*unknown_wrapper_option"),
        (RejectedConfigWrapper, "uses model configuration 'arbitrary_types_allowed'"),
        (CallableSchemaWrapper, "uses callable model JSON Schema mutation"),
        (CustomHookWrapper, "uses custom model hook __get_pydantic_json_schema__"),
    )

    for index, (wrapper, message) in enumerate(cases):
        parent = create_model(
            f"UnsafeConfiguredWrapperParent{index}",
            wrapper=(wrapper, ...),
        )
        decorated = versioned_schema(
            name=f"unsafe_configured_wrapper_parent_{index}",
            versions=("1", "2"),
            current="2",
        )(parent)

        with pytest.raises(UnsupportedWireModelError, match=message):
            model_for_version(decorated, "1")


def test_empty_behavioral_configuration_is_wire_inert() -> None:
    class Payload(BaseModel):
        model_config = ConfigDict(plugin_settings={})

        value: int = 1

    family = SchemaFamily(
        model=Payload,
        name="empty_behavioral_configuration",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )

    assert family.model_for("1")().model_dump() == {"value": 1}


def test_decorator_topology_handles_safe_and_unsafe_indirect_types() -> None:
    @versioned_schema(name="indirect_topology_child", versions=("1", "2"), current="2")
    class Child(BaseModel):
        value: int

    child_type = TypeVar("child_type", bound=Child)
    safe_type = TypeVar("safe_type", bound=int)
    type safe_alias = Literal["safe"]

    class SafeEnvelope(TypedDict):
        value: int

    class RecursiveWrapper(BaseModel):
        ignored: int = Field(0, exclude=True)
        next_wrapper: RecursiveWrapper | None = None

    parent = create_model(
        "IndirectTopologyParent",
        safe=(safe_alias, "safe"),
        safe_type_value=(safe_type, 1),
        envelope=(SafeEnvelope, ...),
        recursive=(RecursiveWrapper, ...),
        ignored=(Child, Field(default=Child(value=0), exclude=True)),
        any_first=(Any | Child, ...),
        any_last=(Child | Any, ...),
    )
    decorated = versioned_schema(
        name="indirect_topology_parent",
        versions=("1", "2"),
        current="2",
    )(parent)

    with pytest.raises(UnsupportedWireModelError, match="behavioral structured annotation"):
        model_for_version(decorated, "1")

    typevar_parent = create_model(
        "DecoratorTypeVarTopologyParent",
        child=(child_type, ...),
    )
    decorated_typevar_parent = versioned_schema(
        name="decorator_typevar_topology_parent",
        versions=("1", "2"),
        current="2",
    )(typevar_parent)
    with pytest.raises(UnsupportedWireModelError, match="unresolved type parameter"):
        model_for_version(decorated_typevar_parent, "1")

    typed_dict_factory = cast(Any, TypedDict)
    child_envelope = typed_dict_factory("DecoratorChildEnvelope", {"child": Child})
    typed_dict_parent = create_model(
        "DecoratorTypedDictTopologyParent",
        envelope=(child_envelope, ...),
    )
    decorated_typed_dict_parent = versioned_schema(
        name="decorator_typed_dict_topology_parent",
        versions=("1", "2"),
        current="2",
    )(typed_dict_parent)
    with pytest.raises(UnsupportedWireModelError, match="uses a TypedDict boundary"):
        model_for_version(decorated_typed_dict_parent, "1")
