from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, make_dataclass
from typing import Annotated, Any, ClassVar, Literal, NewType, NotRequired, TypedDict, cast

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
    WithJsonSchema,
    computed_field,
    create_model,
    field_validator,
    model_serializer,
)
from pydantic.dataclasses import dataclass as pydantic_dataclass
from pydantic.json_schema import DEFAULT_REF_TEMPLATE, GenerateJsonSchema, JsonSchemaMode
from typing_extensions import TypeVar

from pydantic_versions import (
    NestedFamily,
    SchemaCompilationError,
    SchemaFamily,
    SchemaVersion,
    UnsupportedWireModelError,
    VersionMetadata,
    VersionTransition,
    field_removed,
    field_renamed,
    matching_labels,
    model_for_version,
    schema_version,
    versioned_schema,
)


@dataclass
class _SafeRecursiveEnvelope:
    recursive: _SafeRecursiveEnvelope | None = None


def test_explicit_recursive_wire_model_resolves_its_root_schema_reference() -> None:
    class CurrentNode(BaseModel):
        value: int
        child: CurrentNode | None = None

    class HistoricalNode(BaseModel):
        value: int
        child: HistoricalNode | None = None

    schema = HistoricalNode.model_json_schema()
    assert schema["$ref"].startswith("#/$defs/")

    family = SchemaFamily(
        model=CurrentNode,
        name="recursive_explicit_wire_model",
        versions=(
            SchemaVersion("1", wire_model=HistoricalNode),
            SchemaVersion("2"),
        ),
        version_metadata=None,
    )

    historical = family.model_for("1")
    validated = historical.model_validate(
        {"value": 1, "child": {"value": 2, "child": None}},
    )
    assert validated.model_dump() == {
        "value": 1,
        "child": {"value": 2, "child": None},
    }


def test_decorator_union_tolerates_runtime_subclass_probe_type_errors() -> None:
    probe_active = False
    probed_subclasses: list[type[Any]] = []

    class RaisingSubclassMeta(type):
        def __subclasscheck__(cls, subclass: type[Any]) -> bool:
            if probe_active:
                probed_subclasses.append(subclass)
                raise TypeError("custom subclass probe failed")
            return super().__subclasscheck__(subclass)

    @dataclass
    class OtherPayload(metaclass=RaisingSubclassMeta):
        value: int

    @versioned_schema(name="subclass_probe_child", versions=("1", "2"), current="2")
    @schema_version("1", patches=(field_renamed("value", "legacy_value"),))
    class Child(BaseModel):
        value: int

    @versioned_schema(name="subclass_probe_parent", versions=("1", "2"), current="2")
    class Parent(BaseModel):
        payload: Child | OtherPayload

    probe_active = True
    try:
        historical = model_for_version(Parent, "1")
    finally:
        probe_active = False

    assert probed_subclasses
    document = cast(
        Any,
        historical.model_validate(
            {"schema_version": "1", "payload": {"value": 7}},
        ),
    )
    assert isinstance(document.payload, OtherPayload)
    assert document.payload.value == 7


def test_explicit_wire_model_rejects_unresolvable_root_schema_references() -> None:
    class Current(BaseModel):
        value: int

    class ExternalReference(BaseModel):
        value: int

        @classmethod
        def model_json_schema(
            cls,
            by_alias: bool = True,
            ref_template: str = DEFAULT_REF_TEMPLATE,
            schema_generator: type[GenerateJsonSchema] = GenerateJsonSchema,
            mode: JsonSchemaMode = "validation",
            *,
            union_format: Literal["any_of", "primitive_type_array"] = "any_of",
        ) -> dict[str, Any]:
            del cls, by_alias, ref_template, schema_generator, mode, union_format
            return {"$ref": "https://example.invalid/schema"}

    class MissingLocalReference(BaseModel):
        value: int

        @classmethod
        def model_json_schema(
            cls,
            by_alias: bool = True,
            ref_template: str = DEFAULT_REF_TEMPLATE,
            schema_generator: type[GenerateJsonSchema] = GenerateJsonSchema,
            mode: JsonSchemaMode = "validation",
            *,
            union_format: Literal["any_of", "primitive_type_array"] = "any_of",
        ) -> dict[str, Any]:
            del cls, by_alias, ref_template, schema_generator, mode, union_format
            return {"$ref": "#/$defs/Missing", "$defs": {}}

    for index, wire_model in enumerate((ExternalReference, MissingLocalReference)):
        family = SchemaFamily(
            model=Current,
            name=f"unresolvable_explicit_schema_reference_{index}",
            versions=(
                SchemaVersion("1", wire_model=wire_model),
                SchemaVersion("2"),
            ),
            version_metadata=None,
        )

        with pytest.raises(UnsupportedWireModelError, match="non-object validation schema"):
            family.compile()


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


def test_family_document_adapter_rejects_late_malformed_properties_schema() -> None:
    schema_calls = 0

    def mutate_schema(schema: dict[str, Any]) -> None:
        nonlocal schema_calls
        schema_calls += 1
        if schema_calls >= 3:
            schema["properties"] = "not-an-object"

    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        model_config = ConfigDict(json_schema_extra=mutate_schema)

        value: int = 1

    family = SchemaFamily(
        model=Current,
        name="late_malformed_explicit_properties",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
    )

    with pytest.raises(
        SchemaCompilationError,
        match="requires object-shaped JSON Schema properties",
    ):
        family.model_for("1")

    assert schema_calls >= 3


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
    callable_discriminator = Annotated[
        Annotated[Literal["cat"], Tag("cat")] | Annotated[Literal["dog"], Tag("dog")],
        Discriminator(lambda value: value),
    ]

    class CustomWithJsonSchema(WithJsonSchema):
        pass

    custom_schema_metadata = Annotated[int, CustomWithJsonSchema({"type": "integer"})]

    class CustomSchemaType:
        @classmethod
        def __get_pydantic_core_schema__(cls, source_type: Any, handler: Any) -> Any:
            del cls, source_type
            return handler(int)

    defaulted_type = TypeVar("defaulted_type", default=behavioral)
    bounded_type = TypeVar("bounded_type", bound=behavioral)
    constrained_type = TypeVar("constrained_type", behavioral, str)
    discriminator_type = TypeVar("discriminator_type", bound=callable_discriminator)
    custom_schema_type = TypeVar("custom_schema_type", bound=custom_schema_metadata)
    custom_hook_type = TypeVar("custom_hook_type", bound=CustomSchemaType)
    parameters = (
        defaulted_type,
        bounded_type,
        constrained_type,
        discriminator_type,
        custom_schema_type,
        custom_hook_type,
    )

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
        child: Child = Field(default_factory=Child)

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
    validated = family.validate({"schema_version": "1", "note": "historical"})
    assert validated.current_model.child == Child()
    assert family.dump(version="1", data=Parent(child=Child(value=5))) == {
        "note": "historical",
        "schema_version": "1",
    }


def test_explicit_wire_model_reports_a_missing_declared_projection_path() -> None:
    class Child(BaseModel):
        value: int

    child_family = SchemaFamily(
        model=Child,
        name="missing_projection_path_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    )

    class Parent(BaseModel):
        value: int

    class HistoricalParent(BaseModel):
        value: int

    family = SchemaFamily(
        model=Parent,
        name="missing_projection_path_parent",
        versions=(
            SchemaVersion("1", wire_model=HistoricalParent),
            SchemaVersion("2"),
        ),
        nested=(NestedFamily("missing", child_family, matching_labels()),),
        version_metadata=None,
    )

    with pytest.raises(
        SchemaCompilationError,
        match="Compiled projection does not contain current field 'missing'",
    ):
        family.model_for("1")


def test_explicit_wire_model_may_replace_a_nested_path_with_a_scalar() -> None:
    class Child(BaseModel):
        value: int

    child_family = SchemaFamily(
        model=Child,
        name="scalar_explicit_path_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        version_metadata=None,
    )

    class Wrapper(BaseModel):
        child: Child

    class Parent(BaseModel):
        wrapper: Wrapper

    class HistoricalParent(BaseModel):
        wrapper: str

    def upgrade(data: dict[str, Any]) -> dict[str, Any]:
        return {"wrapper": {"child": {"value": int(data["wrapper"])}}}

    def downgrade(data: dict[str, Any]) -> dict[str, Any]:
        return {"wrapper": str(data["wrapper"]["child"]["value"])}

    family = SchemaFamily(
        model=Parent,
        name="scalar_explicit_path_parent",
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
        nested=(NestedFamily(("wrapper", "child"), child_family, matching_labels()),),
        version_metadata=None,
    )

    validated = family.validate({"wrapper": "7"}, version="1")
    assert validated.current_model.wrapper.child.value == 7
    assert family.dump(
        version="1",
        data=Parent(wrapper=Wrapper(child=Child(value=8))),
    ) == {"wrapper": "8"}


@pytest.mark.parametrize("union_arm", [False, True], ids=["scalar", "model-or-scalar"])
def test_explicit_wire_model_may_replace_a_nested_leaf_with_a_scalar(
    union_arm: bool,
) -> None:
    case = "union" if union_arm else "scalar"

    class Child(BaseModel):
        value: int

    child_family = SchemaFamily(
        model=Child,
        name=f"explicit_{case}_leaf_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    )
    historical_child = child_family.model_for("1")

    class Parent(BaseModel):
        child: Child

    historical_annotation = historical_child | str if union_arm else str
    historical_parent = create_model(
        f"Explicit{case.title()}LeafHistoricalParent",
        child=(historical_annotation, ...),
    )

    def upgrade(data: dict[str, Any]) -> dict[str, Any]:
        return {"child": {"value": int(data["child"])}}

    def downgrade(data: dict[str, Any]) -> dict[str, Any]:
        return {"child": str(data["child"]["value"])}

    family = SchemaFamily(
        model=Parent,
        name=f"explicit_{case}_leaf_parent",
        versions=(
            SchemaVersion("1", wire_model=historical_parent),
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
        nested=(NestedFamily("child", child_family, matching_labels()),),
    )

    validated = family.validate({"child": "7", "schema_version": "1"})
    assert validated.current_model.child.value == 7
    assert family.dump(version="1", data=Parent(child=Child(value=8))) == {
        "child": "8",
        "schema_version": "1",
    }


def test_explicit_wire_model_prunes_an_independent_nested_body_model() -> None:
    class Child(BaseModel):
        value: int

    child_family = SchemaFamily(
        model=Child,
        name="independent_explicit_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    )

    class Parent(BaseModel):
        child: Child

    class HistoricalChildRepresentation(BaseModel):
        value: int
        schema_version: Literal["1"] = "1"

    class HistoricalParent(BaseModel):
        child: HistoricalChildRepresentation

    family = SchemaFamily(
        model=Parent,
        name="independent_explicit_child_parent",
        versions=(
            SchemaVersion("1", wire_model=HistoricalParent),
            SchemaVersion("2"),
        ),
        nested=(NestedFamily("child", child_family, matching_labels()),),
    )

    validated = family.validate(
        {
            "child": {"value": 7, "schema_version": "1"},
            "schema_version": "1",
        },
    )
    assert validated.current_model.child.value == 7
    assert family.dump(version="1", data=Parent(child=Child(value=8))) == {
        "child": {"value": 8},
        "schema_version": "1",
    }


def test_explicit_wire_model_prunes_a_structural_dataclass_body() -> None:
    class Child(BaseModel):
        value: int

    child_family = SchemaFamily(
        model=Child,
        name="dataclass_explicit_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    )

    class Parent(BaseModel):
        child: Child

    @dataclass
    class HistoricalChildRepresentation:
        value: int
        schema_version: Literal["1"] = "1"

    class HistoricalParent(BaseModel):
        child: HistoricalChildRepresentation

    family = SchemaFamily(
        model=Parent,
        name="dataclass_explicit_child_parent",
        versions=(
            SchemaVersion("1", wire_model=HistoricalParent),
            SchemaVersion("2"),
        ),
        nested=(NestedFamily("child", child_family, matching_labels()),),
    )

    validated = family.validate(
        {
            "child": {"value": 7, "schema_version": "1"},
            "schema_version": "1",
        },
    )
    assert validated.current_model.child.value == 7
    assert family.dump(version="1", data=Parent(child=Child(value=8))) == {
        "child": {"value": 8},
        "schema_version": "1",
    }


def test_explicit_wire_model_rejects_a_relocated_dataclass_body() -> None:
    serializer_calls = 0

    class Child(BaseModel):
        value: int

    child_family = SchemaFamily(
        model=Child,
        name="relocated_dataclass_explicit_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    )

    class Parent(BaseModel):
        child: Child

    @pydantic_dataclass
    class HistoricalChildRepresentation:
        value: int
        schema_version: Literal["1"] = "1"

        @model_serializer
        def serialize(self) -> str:
            nonlocal serializer_calls
            serializer_calls += 1
            return "relocated"

    class HistoricalParent(BaseModel):
        child: HistoricalChildRepresentation

    family = SchemaFamily(
        model=Parent,
        name="relocated_dataclass_explicit_child_parent",
        versions=(
            SchemaVersion("1", wire_model=HistoricalParent),
            SchemaVersion("2"),
        ),
        nested=(NestedFamily("child", child_family, matching_labels()),),
    )

    with pytest.raises(
        UnsupportedWireModelError,
        match="model-level serializer on a managed dataclass leaf",
    ):
        family.compile()

    assert serializer_calls == 0


def test_explicit_wire_model_prunes_a_typed_dict_union_arm() -> None:
    class Child(BaseModel):
        value: int

    child_family = SchemaFamily(
        model=Child,
        name="typed_dict_explicit_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    )

    class Parent(BaseModel):
        child: Child

    class HistoricalChildRepresentation(TypedDict):
        value: int
        schema_version: NotRequired[Annotated[Literal["1"], Field(default="1")]]

    class HistoricalParent(BaseModel):
        child: HistoricalChildRepresentation | str

    def upgrade(data: dict[str, Any]) -> dict[str, Any]:
        return {"child": {"value": data["child"]["value"]}}

    def downgrade(data: dict[str, Any]) -> dict[str, Any]:
        return {"child": {**data["child"], "schema_version": "1"}}

    family = SchemaFamily(
        model=Parent,
        name="typed_dict_explicit_child_parent",
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
        nested=(NestedFamily("child", child_family, matching_labels()),),
    )

    validated = family.validate(
        {
            "child": {"value": 7, "schema_version": "1"},
            "schema_version": "1",
        },
    )
    assert validated.current_model.child.value == 7
    assert family.dump(version="1", data=Parent(child=Child(value=8))) == {
        "child": {"value": 8},
        "schema_version": "1",
    }


@pytest.mark.parametrize("model_first", [True, False], ids=["model-first", "scalar-first"])
def test_explicit_wire_model_uses_scalar_items_in_generic_union_arms(
    model_first: bool,
) -> None:
    case = "model_first" if model_first else "scalar_first"

    class Child(BaseModel):
        value: int

    child_family = SchemaFamily(
        model=Child,
        name=f"generic_scalar_union_{case}_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    )

    class Parent(BaseModel):
        children: list[Child]

    class HistoricalChildRepresentation(BaseModel):
        value: int
        schema_version: Literal["1"] = "1"

    model_arm = list[HistoricalChildRepresentation]
    scalar_arm = list[str]
    historical_annotation = model_arm | scalar_arm if model_first else scalar_arm | model_arm
    historical_parent = create_model(
        f"GenericScalarUnion{case.title()}HistoricalParent",
        children=(historical_annotation, ...),
    )

    def upgrade(data: dict[str, Any]) -> dict[str, Any]:
        return {"children": [{"value": int(value)} for value in data["children"]]}

    def downgrade(data: dict[str, Any]) -> dict[str, Any]:
        return {
            "children": [str(child["value"]) for child in data["children"]],
        }

    family = SchemaFamily(
        model=Parent,
        name=f"generic_scalar_union_{case}_parent",
        versions=(
            SchemaVersion("1", wire_model=historical_parent),
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
        nested=(NestedFamily("children", child_family, matching_labels()),),
    )

    validated = family.validate(
        {"children": ["7"], "schema_version": "1"},
    )
    assert [child.value for child in validated.current_model.children] == [7]
    assert family.dump(version="1", data=Parent(children=[Child(value=8)])) == {
        "children": ["8"],
        "schema_version": "1",
    }


def test_explicit_wire_model_uses_scalar_items_in_an_intermediate_generic_union() -> None:
    class Child(BaseModel):
        value: int

    child_family = SchemaFamily(
        model=Child,
        name="intermediate_generic_scalar_union_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    )

    class Wrapper(BaseModel):
        child: Child

    class Parent(BaseModel):
        wrappers: list[Wrapper]

    class HistoricalChildRepresentation(BaseModel):
        value: int
        schema_version: Literal["1"] = "1"

    class HistoricalWrapper(BaseModel):
        child: HistoricalChildRepresentation

    historical_parent = create_model(
        "IntermediateGenericScalarUnionHistoricalParent",
        wrappers=(list[HistoricalWrapper] | list[str], ...),
    )

    def upgrade(data: dict[str, Any]) -> dict[str, Any]:
        return {
            "wrappers": [{"child": {"value": int(value)}} for value in data["wrappers"]],
        }

    def downgrade(data: dict[str, Any]) -> dict[str, Any]:
        return {
            "wrappers": [str(wrapper["child"]["value"]) for wrapper in data["wrappers"]],
        }

    family = SchemaFamily(
        model=Parent,
        name="intermediate_generic_scalar_union_parent",
        versions=(
            SchemaVersion("1", wire_model=historical_parent),
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
        nested=(NestedFamily(("wrappers", "child"), child_family, matching_labels()),),
    )

    validated = family.validate(
        {"wrappers": ["7"], "schema_version": "1"},
    )
    assert validated.current_model.wrappers[0].child.value == 7
    assert family.dump(
        version="1",
        data=Parent(wrappers=[Wrapper(child=Child(value=8))]),
    ) == {
        "wrappers": ["8"],
        "schema_version": "1",
    }


def test_explicit_union_wrapper_prunes_nested_family_metadata() -> None:
    class Child(BaseModel):
        value: int

    child_family = SchemaFamily(
        model=Child,
        name="explicit_union_metadata_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    )
    historical_child = child_family.model_for("1")

    class Wrapper(BaseModel):
        child: Child

    class Parent(BaseModel):
        wrapper: Wrapper

    historical_wrapper = create_model(
        "ExplicitUnionMetadataHistoricalWrapper",
        child=(historical_child, ...),
    )
    historical_parent = create_model(
        "ExplicitUnionMetadataHistoricalParent",
        wrapper=(historical_wrapper | str, ...),
    )
    family = SchemaFamily(
        model=Parent,
        name="explicit_union_metadata_parent",
        versions=(
            SchemaVersion("1", wire_model=historical_parent),
            SchemaVersion("2"),
        ),
        nested=(NestedFamily(("wrapper", "child"), child_family, matching_labels()),),
    )

    assert family.dump(
        version="1",
        data=Parent(wrapper=Wrapper(child=Child(value=3))),
    ) == {
        "wrapper": {"child": {"value": 3}},
        "schema_version": "1",
    }


def test_explicit_union_wrapper_uses_its_validated_scalar_arm() -> None:
    class Child(BaseModel):
        value: int

    child_family = SchemaFamily(
        model=Child,
        name="explicit_scalar_union_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    )
    historical_child = child_family.model_for("1")

    class Wrapper(BaseModel):
        child: Child

    class Parent(BaseModel):
        wrapper: Wrapper

    historical_wrapper = create_model(
        "ExplicitScalarUnionHistoricalWrapper",
        child=(historical_child, ...),
    )
    historical_parent = create_model(
        "ExplicitScalarUnionHistoricalParent",
        wrapper=(historical_wrapper | str, ...),
    )

    def upgrade(data: dict[str, Any]) -> dict[str, Any]:
        return {"wrapper": {"child": {"value": int(data["wrapper"])}}}

    def downgrade(data: dict[str, Any]) -> dict[str, Any]:
        return {"wrapper": str(data["wrapper"]["child"]["value"])}

    family = SchemaFamily(
        model=Parent,
        name="explicit_scalar_union_parent",
        versions=(
            SchemaVersion("1", wire_model=historical_parent),
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
        nested=(NestedFamily(("wrapper", "child"), child_family, matching_labels()),),
    )

    validated = family.validate(
        {"wrapper": "7", "schema_version": "1"},
    )
    assert validated.current_model.wrapper.child.value == 7
    assert family.dump(
        version="1",
        data=Parent(wrapper=Wrapper(child=Child(value=8))),
    ) == {
        "wrapper": "8",
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
            ("recursive", _SafeRecursiveEnvelope),
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


def test_behavioral_newtype_target_has_a_specific_diagnostic() -> None:
    behavioral_value = NewType(
        "behavioral_value",
        Annotated[int, AfterValidator(lambda value: value)],
    )

    class Payload(BaseModel):
        value: behavioral_value

    family = SchemaFamily(
        model=Payload,
        name="behavioral_newtype_target",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )

    with pytest.raises(UnsupportedWireModelError, match="behavioral NewType target"):
        family.compile()


def test_safe_recursive_nested_and_generic_aliases_compile() -> None:
    type literal_alias = Literal["ok"]
    type nested_alias = list[literal_alias]
    type generic_alias[value_type: int] = list[value_type]
    type nested_generic_alias = generic_alias[int]
    type recursive_alias = int | list[recursive_alias]
    type annotated_alias = Annotated[int, Field(description="safe declarative metadata")]

    class Payload(BaseModel):
        nested: nested_alias
        applied: generic_alias[int]
        nested_applied: nested_generic_alias
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
            "nested_applied": [3, 4],
            "recursive": [1, [2]],
            "annotated": 3,
        }
    ).model_dump() == {
        "nested": ["ok"],
        "applied": [1, 2],
        "nested_applied": [3, 4],
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
