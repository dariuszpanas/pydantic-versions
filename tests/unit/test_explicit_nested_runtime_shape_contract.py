from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from enum import Enum
from typing import Annotated, Any, Literal, NewType, NotRequired, Required, TypedDict, TypeVar, cast

import pytest
from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    create_model,
    field_serializer,
    field_validator,
    model_validator,
    with_config,
)
from pydantic.dataclasses import dataclass as pydantic_dataclass
from typing_extensions import ReadOnly
from typing_extensions import TypeVar as ExtensionsTypeVar  # noqa: UP035

from pydantic_versions import (
    InvalidMigrationError,
    NestedFamily,
    SchemaFamily,
    SchemaVersion,
    SchemaVersionError,
    TransitionFunc,
    VersionMetadata,
    VersionPath,
    VersionTransition,
    matching_labels,
)

type RuntimeItems[RuntimeItem] = list[RuntimeItem]
type RuntimeScalarAlias = str
type RuntimeUnionItems[RuntimeItem] = list[RuntimeItem] | tuple[RuntimeItem, ...]
type RuntimeAnnotatedItems[RuntimeItem] = Annotated[list[RuntimeItem], Field(min_length=1)]
RuntimeNewScalar = NewType("RuntimeNewScalar", str)
RuntimeBoundScalar = TypeVar("RuntimeBoundScalar", bound=str)
RuntimeConstrainedScalar = TypeVar("RuntimeConstrainedScalar", str, bytes)
RuntimeDefaultScalar = ExtensionsTypeVar("RuntimeDefaultScalar", default=str)


class RuntimeHistoricalMappingScalar(Enum):
    legacy = {
        "schema_version": "foreign",
        "value": "mapping-secret",
    }


type RuntimeAliasedHistoricalScalar = Annotated[
    RuntimeHistoricalMappingScalar,
    Field(serialization_alias="legacyGrandchild"),
]


@dataclass
class RuntimeAssignmentAliasRepresentation:
    grandchild: RuntimeHistoricalMappingScalar = Field(
        serialization_alias="legacyGrandchild",
    )


@dataclass
class RuntimeMetadataAliasRepresentation:
    grandchild: RuntimeHistoricalMappingScalar = dataclass_field(
        metadata={"serialization_alias": "legacyGrandchild"},
    )


@dataclass
class RuntimeAliasPrecedenceRepresentation:
    grandchild: Annotated[
        RuntimeHistoricalMappingScalar,
        Field(serialization_alias="annotatedGrandchild"),
    ] = dataclass_field(
        default=Field(serialization_alias="assignedGrandchild"),
        metadata={"serialization_alias": "metadataGrandchild"},
    )


@dataclass
class RuntimeMergedAliasRepresentation:
    grandchild: Annotated[
        RuntimeHistoricalMappingScalar,
        Field(serialization_alias="annotatedGrandchild"),
    ] = dataclass_field(default=Field(exclude=False))


@dataclass
class RuntimeClearedAliasRepresentation:
    grandchild: Annotated[
        RuntimeHistoricalMappingScalar,
        Field(alias="annotatedGrandchild"),
    ] = Field(serialization_alias=None)


class RuntimeBaseModelClearedAliasRepresentation(BaseModel):
    grandchild: Annotated[
        RuntimeHistoricalMappingScalar,
        Field(alias="annotatedGrandchild"),
    ] = Field(serialization_alias=None)


@dataclass
class RuntimeMultipleAnnotatedAliasRepresentation:
    grandchild: Annotated[
        RuntimeHistoricalMappingScalar,
        Field(serialization_alias="firstGrandchild"),
        Field(serialization_alias="secondGrandchild"),
    ]


@dataclass
class RuntimeEmptyAliasRepresentation:
    grandchild: Annotated[
        RuntimeHistoricalMappingScalar,
        Field(alias="lowerGrandchild"),
    ] = dataclass_field(
        metadata={
            "validation_alias": "",
            "serialization_alias": "",
        },
    )


@dataclass
class RuntimeTypeAliasRepresentation:
    grandchild: RuntimeAliasedHistoricalScalar


class RuntimeTypedDictAliasRepresentation[Item](TypedDict):
    grandchild: Annotated[Item, Field(serialization_alias="legacyGrandchild")]


class RuntimeSmartUnionGrandchildPayload(TypedDict):
    value: int
    schema_version: str


class RuntimeSmartUnionExactTypedDictArm(TypedDict):
    grandchild: RuntimeSmartUnionGrandchildPayload
    alternate: RuntimeSmartUnionGrandchildPayload


class RuntimePreflightGrandchildRepresentation(BaseModel):
    value: int


@dataclass
class RuntimePreflightDataclassAliasRepresentation:
    grandchild: Annotated[
        RuntimePreflightGrandchildRepresentation,
        Field(
            validation_alias=AliasChoices(
                "legacyGrandchild",
                AliasPath("legacy", "grandchild"),
            ),
        ),
    ]


class RuntimePreflightTypedDictAliasRepresentation(TypedDict):
    grandchild: Annotated[
        RuntimePreflightGrandchildRepresentation,
        Field(validation_alias=AliasPath("legacy", "grandchild")),
    ]


@dataclass
class RuntimePreflightAliasDisabledDataclassRepresentation:
    __pydantic_config__ = ConfigDict(validate_by_alias=False)

    grandchild: Annotated[
        RuntimePreflightGrandchildRepresentation,
        Field(validation_alias="legacyGrandchild"),
    ]


@with_config(ConfigDict(validate_by_alias=False))
class RuntimePreflightAliasDisabledTypedDictRepresentation(TypedDict):
    grandchild: Annotated[
        RuntimePreflightGrandchildRepresentation,
        Field(validation_alias="legacyGrandchild"),
    ]


@dataclass
class RuntimePreflightPopulateByNameDataclassRepresentation:
    __pydantic_config__ = ConfigDict(populate_by_name=True)

    grandchild: Annotated[
        RuntimePreflightGrandchildRepresentation,
        Field(validation_alias="legacyGrandchild"),
    ]


@with_config(ConfigDict(populate_by_name=True))
class RuntimePreflightPopulateByNameTypedDictRepresentation(TypedDict):
    grandchild: Annotated[
        RuntimePreflightGrandchildRepresentation,
        Field(validation_alias="legacyGrandchild"),
    ]


@dataclass
class RuntimePreflightClearedAliasRepresentation:
    grandchild: Annotated[
        RuntimePreflightGrandchildRepresentation,
        Field(validation_alias="legacyGrandchild"),
    ] = Field(alias=None)


@dataclass
class RuntimePreflightMultipleAnnotatedAliasRepresentation:
    grandchild: Annotated[
        RuntimePreflightGrandchildRepresentation,
        Field(validation_alias="firstGrandchild"),
        Field(validation_alias="secondGrandchild"),
    ]


@dataclass
class RuntimePreflightMetadataAliasRepresentation:
    grandchild: Annotated[
        RuntimePreflightGrandchildRepresentation,
        Field(validation_alias="annotatedGrandchild"),
    ] = dataclass_field(metadata={"validation_alias": "metadataGrandchild"})


@dataclass
class RuntimePreflightAssignedAliasRepresentation:
    grandchild: Annotated[
        RuntimePreflightGrandchildRepresentation,
        Field(validation_alias="annotatedGrandchild"),
    ] = dataclass_field(
        default=Field(validation_alias="assignedGrandchild"),
        metadata={"validation_alias": "metadataGrandchild"},
    )


@dataclass
class RuntimePreflightEmptyAliasRepresentation:
    grandchild: Annotated[
        RuntimePreflightGrandchildRepresentation,
        Field(alias="lowerGrandchild"),
    ] = dataclass_field(
        metadata={
            "validation_alias": "",
            "serialization_alias": "",
        },
    )


_DEFAULT_RUNTIME_VERSION_METADATA = VersionMetadata()


def _runtime_family(
    *,
    model: type[BaseModel],
    name: str,
    wire_model: type[BaseModel] | None = None,
    upgrade: TransitionFunc | None = None,
    downgrade: TransitionFunc | None = None,
    exact_downgrade: bool = False,
    nested: tuple[NestedFamily, ...] = (),
    version_metadata: VersionMetadata | None = _DEFAULT_RUNTIME_VERSION_METADATA,
) -> SchemaFamily[Any]:
    transitions = (
        (
            VersionTransition(
                "1",
                "2",
                upgrade=upgrade,
                downgrade=downgrade,
                downgrade_semantics="exact" if exact_downgrade else None,
            ),
        )
        if upgrade is not None or downgrade is not None
        else ()
    )
    return SchemaFamily(
        model=model,
        name=name,
        versions=(
            SchemaVersion("1", wire_model=wire_model)
            if wire_model is not None
            else SchemaVersion("1"),
            SchemaVersion("2"),
        ),
        transitions=transitions,
        nested=nested,
        version_metadata=version_metadata,
    )


def _nested_runtime_family(
    *,
    model: type[BaseModel],
    name: str,
    child_family: SchemaFamily[Any],
    nested_path: VersionPath,
    wire_model: type[BaseModel] | None = None,
    upgrade: TransitionFunc | None = None,
    downgrade: TransitionFunc | None = None,
    exact_downgrade: bool = False,
    version_metadata: VersionMetadata | None = _DEFAULT_RUNTIME_VERSION_METADATA,
) -> SchemaFamily[Any]:
    return _runtime_family(
        model=model,
        name=name,
        wire_model=wire_model,
        upgrade=upgrade,
        downgrade=downgrade,
        exact_downgrade=exact_downgrade,
        nested=(NestedFamily(nested_path, child_family, matching_labels()),),
        version_metadata=version_metadata,
    )


def test_explicit_source_rejects_shape_break_before_migration_without_payload() -> None:
    migrations: list[str] = []

    class Child(BaseModel):
        value: int

    child_family = _runtime_family(
        model=Child,
        name="runtime_shape_source_child",
        version_metadata=None,
    )

    class Parent(BaseModel):
        children: list[Child]

    class HistoricalParent(BaseModel):
        children: RuntimeItems[str]

        @field_validator("children", mode="after")
        @classmethod
        def break_declared_shape(cls, value: list[str]) -> Any:
            return [{"foreign_version": value[0]}]

    def upgrade(data: dict[str, Any]) -> dict[str, Any]:
        migrations.append("upgrade")
        return {"children": [{"value": int(item)} for item in data["children"]]}

    family = _nested_runtime_family(
        model=Parent,
        name="runtime_shape_source_parent",
        wire_model=HistoricalParent,
        upgrade=upgrade,
        child_family=child_family,
        nested_path="children",
        version_metadata=None,
    )

    secret = "source-secret"
    with pytest.raises(ValueError) as caught:
        family.validate({"children": [secret]}, version="1")

    message = str(caught.value)
    assert "runtime_shape_source_parent" in message
    assert "version '1'" in message
    assert "nested path ('children',)" in message
    assert secret not in message
    assert migrations == []


def test_explicit_source_rejects_shape_break_from_model_validator() -> None:
    migrations: list[str] = []

    class Child(BaseModel):
        value: int

    child_family = _runtime_family(
        model=Child,
        name="runtime_shape_root_child",
        version_metadata=None,
    )

    class Parent(BaseModel):
        child: Child

    class HistoricalParent(BaseModel):
        child: str

        @model_validator(mode="after")
        def break_model_shape(self) -> Any:
            return {"child": self.child}

    def upgrade(data: dict[str, Any]) -> dict[str, Any]:
        migrations.append("upgrade")
        return {"child": {"value": int(data["child"])}}

    family = _nested_runtime_family(
        model=Parent,
        name="runtime_shape_root_parent",
        wire_model=HistoricalParent,
        upgrade=upgrade,
        child_family=child_family,
        nested_path="child",
        version_metadata=None,
    )

    with pytest.raises(ValueError, match=r"nested path \('child',\)"):
        family.validate({"child": "8"}, version="1")

    assert migrations == []


def test_exact_union_subclass_cannot_be_masked_by_a_base_arm() -> None:
    migrations: list[str] = []

    class Child(BaseModel):
        value: int

    child_family = _runtime_family(
        model=Child,
        name="runtime_shape_union_subclass_child",
        version_metadata=None,
    )

    class Wrapper(BaseModel):
        child: Child

    class Parent(BaseModel):
        wrapper: Wrapper

    class HistoricalBase(BaseModel):
        note: str = "base"

    class HistoricalWrapper(HistoricalBase):
        child: str

        @field_validator("child", mode="after")
        @classmethod
        def break_declared_shape(cls, value: str) -> Any:
            return {"payload": value}

    class HistoricalParent(BaseModel):
        wrapper: HistoricalBase | HistoricalWrapper

    def upgrade(_data: dict[str, Any]) -> dict[str, Any]:
        migrations.append("upgrade")
        return {"wrapper": {"child": {"value": 1}}}

    family = _nested_runtime_family(
        model=Parent,
        name="runtime_shape_union_subclass_parent",
        wire_model=HistoricalParent,
        upgrade=upgrade,
        child_family=child_family,
        nested_path=("wrapper", "child"),
        version_metadata=None,
    )

    with pytest.raises(ValueError) as caught:
        family.validate(
            {"wrapper": HistoricalWrapper(child="union-secret")},
            version="1",
        )

    message = str(caught.value)
    assert "runtime_shape_union_subclass_parent" in message
    assert "nested path ('wrapper', 'child')" in message
    assert "union-secret" not in message
    assert migrations == []

    base_arm = family.validate(
        {"wrapper": HistoricalBase(note="whole-subtree")},
        version="1",
    )
    assert base_arm.current_model == Parent(wrapper=Wrapper(child=Child(value=1)))
    assert migrations == ["upgrade"]


def test_fixed_tuple_union_members_preserve_model_and_scalar_arms() -> None:
    class Child(BaseModel):
        value: int

    child_family = _runtime_family(
        model=Child,
        name="runtime_shape_fixed_tuple_child",
        version_metadata=None,
    )

    class Wrapper(BaseModel):
        child: Child

    class Parent(BaseModel):
        wrappers: list[Wrapper]

    class HistoricalWrapper(BaseModel):
        child: str

    class HistoricalParent(BaseModel):
        wrappers: tuple[HistoricalWrapper, str]

    def upgrade(data: dict[str, Any]) -> dict[str, Any]:
        return {
            "wrappers": [
                {
                    "child": {
                        "value": int(data["wrappers"][0]["child"]),
                    },
                },
            ],
        }

    family = _nested_runtime_family(
        model=Parent,
        name="runtime_shape_fixed_tuple_parent",
        wire_model=HistoricalParent,
        upgrade=upgrade,
        child_family=child_family,
        nested_path=("wrappers", "child"),
        version_metadata=None,
    )

    validated = family.validate(
        {
            "wrappers": [
                {"child": "9"},
                "whole-subtree",
            ],
        },
        version="1",
    )

    assert validated.current_model == Parent(wrappers=[Wrapper(child=Child(value=9))])


def test_explicit_source_rejects_a_deleted_declared_managed_field() -> None:
    migrations: list[str] = []

    class Child(BaseModel):
        value: int

    child_family = _runtime_family(
        model=Child,
        name="runtime_shape_deleted_source_child",
        version_metadata=None,
    )

    class Parent(BaseModel):
        child: Child

    class HistoricalParent(BaseModel):
        child: str

        @model_validator(mode="after")
        def delete_child(self) -> HistoricalParent:
            return type(self).model_construct()

    def upgrade(_data: dict[str, Any]) -> dict[str, Any]:
        migrations.append("upgrade")
        return {"child": {"value": 1}}

    family = _nested_runtime_family(
        model=Parent,
        name="runtime_shape_deleted_source_parent",
        wire_model=HistoricalParent,
        upgrade=upgrade,
        child_family=child_family,
        nested_path="child",
        version_metadata=None,
    )

    with pytest.raises(ValueError) as caught:
        family.validate({"child": "deleted-source-secret"}, version="1")

    assert "runtime_shape_deleted_source_parent" in str(caught.value)
    assert "deleted-source-secret" not in str(caught.value)
    assert migrations == []


@pytest.mark.parametrize(
    "operation",
    ["target", "defaults_for", "dump_none"],
    ids=["target", "defaults", "dump-none"],
)
def test_explicit_target_and_defaults_reject_a_deleted_managed_field(
    operation: str,
) -> None:
    events: list[str] = []

    class Child(BaseModel):
        value: int = 1

    child_family = _runtime_family(
        model=Child,
        name="runtime_shape_deleted_target_child",
        version_metadata=None,
    )

    class Parent(BaseModel):
        child: Child = Field(default_factory=Child)

    class HistoricalParent(BaseModel):
        child: str = "deleted-default-secret"
        audit: str = "audit"

        @model_validator(mode="after")
        def delete_child(self) -> HistoricalParent:
            incomplete = type(self).model_construct(audit=self.audit)
            del incomplete.child
            return incomplete

        @field_serializer("audit")
        def serialize_audit(self, value: str) -> str:
            events.append("serializer")
            return value

    def downgrade(data: dict[str, Any]) -> dict[str, Any]:
        events.append("downgrade")
        return {
            "child": f"deleted-target-secret-{data['child']['value']}",
            "audit": "audit",
        }

    family = _nested_runtime_family(
        model=Parent,
        name="runtime_shape_deleted_target_parent",
        wire_model=HistoricalParent,
        downgrade=downgrade,
        exact_downgrade=True,
        child_family=child_family,
        nested_path="child",
        version_metadata=None,
    )

    with pytest.raises(ValueError) as caught:
        if operation == "target":
            family.dump(version="1", data=Parent(child=Child(value=7)))
        elif operation == "defaults_for":
            family.defaults_for(version="1")
        else:
            family.dump(version="1", data=None)

    message = str(caught.value)
    assert "runtime_shape_deleted_target_parent" in message
    assert "deleted-target-secret" not in message
    assert "deleted-default-secret" not in message
    assert events == (["downgrade"] if operation == "target" else [])


def test_explicit_source_rejects_an_extra_at_an_omitted_managed_route() -> None:
    migrations: list[str] = []

    class Child(BaseModel):
        value: int

    child_family = _runtime_family(
        model=Child,
        name="runtime_shape_omitted_source_child",
        version_metadata=None,
    )

    class Parent(BaseModel):
        child: Child

    class HistoricalParent(BaseModel):
        model_config = ConfigDict(extra="allow")

        note: str

    def upgrade(_data: dict[str, Any]) -> dict[str, Any]:
        migrations.append("upgrade")
        return {"child": {"value": 1}}

    family = _nested_runtime_family(
        model=Parent,
        name="runtime_shape_omitted_source_parent",
        wire_model=HistoricalParent,
        upgrade=upgrade,
        child_family=child_family,
        nested_path="child",
        version_metadata=None,
    )

    with pytest.raises(ValueError) as caught:
        family.validate(
            {
                "note": "historical",
                "child": {
                    "value": "omitted-source-secret",
                },
            },
            version="1",
        )

    assert "runtime_shape_omitted_source_parent" in str(caught.value)
    assert "omitted-source-secret" not in str(caught.value)
    assert migrations == []


@pytest.mark.parametrize(
    "mutated",
    [
        "not-an-object",
        {},
        {"value": 1, "foreign": "typed-secret"},
        {"value": "wrong-type"},
    ],
    ids=["not-mapping", "missing-required", "extra-key", "wrong-value-shape"],
)
def test_explicit_source_rejects_typed_dict_after_validator_mutations(
    mutated: Any,
) -> None:
    migrations: list[str] = []

    class Child(BaseModel):
        value: int

    child_family = _runtime_family(
        model=Child,
        name="runtime_shape_typed_dict_child",
        version_metadata=None,
    )

    class HistoricalBody(TypedDict):
        value: int

    class Parent(BaseModel):
        child: Child

    class HistoricalParent(BaseModel):
        child: HistoricalBody

        @field_validator("child", mode="after")
        @classmethod
        def mutate_body(cls, _value: HistoricalBody) -> Any:
            return mutated

    def upgrade(data: dict[str, Any]) -> dict[str, Any]:
        migrations.append("upgrade")
        return {"child": data["child"]}

    family = _nested_runtime_family(
        model=Parent,
        name="runtime_shape_typed_dict_parent",
        wire_model=HistoricalParent,
        upgrade=upgrade,
        child_family=child_family,
        nested_path="child",
        version_metadata=None,
    )

    with pytest.raises(ValueError) as caught:
        family.validate({"child": {"value": 1}}, version="1")

    assert "runtime_shape_typed_dict_parent" in str(caught.value)
    assert "typed-secret" not in str(caught.value)
    assert migrations == []


def test_explicit_target_rejects_shape_break_before_serialization_without_payload() -> None:
    events: list[str] = []

    class Child(BaseModel):
        value: int

    child_family = _runtime_family(
        model=Child,
        name="runtime_shape_target_child",
        version_metadata=None,
    )

    class Parent(BaseModel):
        child: Child
        audit: str

    class HistoricalParent(BaseModel):
        child: str
        audit: str

        @field_validator("child", mode="after")
        @classmethod
        def break_declared_shape(cls, value: str) -> Any:
            return {"foreign_version": value}

        @field_serializer("audit")
        def serialize_audit(self, value: str) -> str:
            events.append("serializer")
            return value

    def downgrade(data: dict[str, Any]) -> dict[str, Any]:
        events.append("downgrade")
        return {"child": str(data["child"]["value"]), "audit": data["audit"]}

    family = _nested_runtime_family(
        model=Parent,
        name="runtime_shape_target_parent",
        wire_model=HistoricalParent,
        downgrade=downgrade,
        exact_downgrade=True,
        child_family=child_family,
        nested_path="child",
        version_metadata=None,
    )

    with pytest.raises(ValueError) as caught:
        family.dump(
            version="1",
            data=Parent(child=Child(value=7), audit="target-secret"),
        )

    message = str(caught.value)
    assert "runtime_shape_target_parent" in message
    assert "version '1'" in message
    assert "nested path ('child',)" in message
    assert "target-secret" not in message
    assert events == ["downgrade"]


def test_explicit_defaults_reject_shape_break_before_serialization() -> None:
    serializers: list[str] = []

    class Child(BaseModel):
        value: int = 1

    child_family = _runtime_family(
        model=Child,
        name="runtime_shape_defaults_child",
        version_metadata=None,
    )

    class Parent(BaseModel):
        child: Child = Field(default_factory=Child)

    class HistoricalParent(BaseModel):
        child: str = Field("default-secret", validate_default=True)
        audit: str = "audit"

        @field_validator("child", mode="after")
        @classmethod
        def break_declared_shape(cls, value: str) -> Any:
            return {"foreign_version": value}

        @field_serializer("audit")
        def serialize_audit(self, value: str) -> str:
            serializers.append("serializer")
            return value

    family = _nested_runtime_family(
        model=Parent,
        name="runtime_shape_defaults_parent",
        wire_model=HistoricalParent,
        child_family=child_family,
        nested_path="child",
        version_metadata=None,
    )

    with pytest.raises(ValueError, match=r"nested path \('child',\)") as caught:
        family.dump(version="1")

    assert "default-secret" not in str(caught.value)
    assert serializers == []


def test_parameterized_alias_shape_preserving_validator_remains_supported() -> None:
    serializers: list[str] = []

    class Child(BaseModel):
        value: int

    child_family = _runtime_family(
        model=Child,
        name="runtime_shape_alias_child",
        version_metadata=None,
    )

    class Parent(BaseModel):
        children: list[Child]
        audit: str

    class HistoricalParent(BaseModel):
        children: RuntimeItems[str] | None
        audit: str

        @field_validator("children", mode="after")
        @classmethod
        def preserve_declared_shape(cls, value: list[str] | None) -> list[str] | None:
            return None if value is None else [item.strip() for item in value]

        @field_serializer("audit")
        def serialize_audit(self, value: str) -> str:
            serializers.append("serializer")
            return value

    def upgrade(data: dict[str, Any]) -> dict[str, Any]:
        children = data["children"]
        return {
            "children": None if children is None else [{"value": int(item)} for item in children],
            "audit": data["audit"],
        }

    def downgrade(data: dict[str, Any]) -> dict[str, Any]:
        children = data["children"]
        return {
            "children": None if children is None else [str(child["value"]) for child in children],
            "audit": data["audit"],
        }

    family = _nested_runtime_family(
        model=Parent,
        name="runtime_shape_alias_parent",
        wire_model=HistoricalParent,
        upgrade=upgrade,
        downgrade=downgrade,
        exact_downgrade=True,
        child_family=child_family,
        nested_path="children",
        version_metadata=None,
    )

    validated = family.validate(
        {"children": [" 7 "], "audit": "source"},
        version="1",
    )
    assert validated.current_model == Parent(
        children=[Child(value=7)],
        audit="source",
    )
    assert family.dump(version="1", data=validated.current_model) == {
        "children": ["7"],
        "audit": "source",
    }
    assert serializers == ["serializer"]


def test_supported_scalar_wrappers_preserve_their_runtime_contract() -> None:
    field_names = (
        "aliased",
        "new_type",
        "bounded",
        "constrained",
        "defaulted",
    )

    class Child(BaseModel):
        value: int

    child_family = _runtime_family(
        model=Child,
        name="runtime_shape_wrappers_child",
        version_metadata=None,
    )

    class Parent(BaseModel):
        aliased: Child
        new_type: Child
        bounded: Child
        constrained: Child
        defaulted: Child
        union_items: list[Child]
        annotated_items: list[Child]

    historical_parent = create_model(
        "RuntimeShapeWrappersHistoricalParent",
        aliased=(RuntimeScalarAlias, ...),
        new_type=(RuntimeNewScalar, ...),
        bounded=(RuntimeBoundScalar, ...),
        constrained=(RuntimeConstrainedScalar, ...),
        defaulted=(RuntimeDefaultScalar, ...),
        union_items=(RuntimeUnionItems[str], ...),
        annotated_items=(RuntimeAnnotatedItems[str], ...),
    )

    def upgrade(data: dict[str, Any]) -> dict[str, Any]:
        upgraded: dict[str, Any] = {name: {"value": int(data[name])} for name in field_names}
        upgraded["union_items"] = [{"value": int(item)} for item in data["union_items"]]
        upgraded["annotated_items"] = [{"value": int(item)} for item in data["annotated_items"]]
        return upgraded

    def downgrade(data: dict[str, Any]) -> dict[str, Any]:
        downgraded: dict[str, Any] = {name: str(data[name]["value"]) for name in field_names}
        downgraded["union_items"] = tuple(str(item["value"]) for item in data["union_items"])
        downgraded["annotated_items"] = [str(item["value"]) for item in data["annotated_items"]]
        return downgraded

    family = _runtime_family(
        model=Parent,
        name="runtime_shape_wrappers_parent",
        wire_model=historical_parent,
        upgrade=upgrade,
        downgrade=downgrade,
        exact_downgrade=True,
        nested=tuple(
            NestedFamily(name, child_family, matching_labels())
            for name in (*field_names, "union_items", "annotated_items")
        ),
        version_metadata=None,
    )
    historical = {
        **{name: f" {index} " for index, name in enumerate(field_names, start=1)},
        "union_items": [" 6 "],
        "annotated_items": [" 7 "],
    }

    validated = family.validate(historical, version="1")
    assert {
        name: validated.current_model.model_dump()[name]["value"] for name in field_names
    } == dict(zip(field_names, range(1, 6), strict=True))
    assert validated.current_model.union_items == [Child(value=6)]
    assert validated.current_model.annotated_items == [Child(value=7)]
    assert family.dump(version="1", data=validated.current_model) == {
        **{name: str(index) for index, name in enumerate(field_names, start=1)},
        "union_items": ["6"],
        "annotated_items": ["7"],
    }


def test_nested_explicit_source_rejects_shape_break_before_child_migration() -> None:
    migrations: list[str] = []

    class Grandchild(BaseModel):
        value: int

    grandchild_family = _runtime_family(
        model=Grandchild,
        name="runtime_shape_nested_grandchild",
        version_metadata=None,
    )

    class Child(BaseModel):
        grandchild: Grandchild

    class HistoricalChild(BaseModel):
        grandchild: str

        @field_validator("grandchild", mode="after")
        @classmethod
        def break_declared_shape(cls, value: str) -> Any:
            return {"foreign_version": value}

    def upgrade(data: dict[str, Any]) -> dict[str, Any]:
        migrations.append("child-upgrade")
        return {"grandchild": {"value": int(data["grandchild"])}}

    child_family = _nested_runtime_family(
        model=Child,
        name="runtime_shape_nested_child",
        wire_model=HistoricalChild,
        upgrade=upgrade,
        child_family=grandchild_family,
        nested_path="grandchild",
        version_metadata=None,
    )

    class Parent(BaseModel):
        child: Child

    @dataclass
    class HistoricalChildRepresentation:
        grandchild: str

    class HistoricalParent(BaseModel):
        child: HistoricalChildRepresentation

    def upgrade_parent(data: dict[str, Any]) -> dict[str, Any]:
        migrations.append("parent-upgrade")
        return data

    parent_family = _nested_runtime_family(
        model=Parent,
        name="runtime_shape_nested_parent",
        wire_model=HistoricalParent,
        upgrade=upgrade_parent,
        child_family=child_family,
        nested_path="child",
        version_metadata=None,
    )

    secret = "nested-secret"
    with pytest.raises(ValueError) as caught:
        parent_family.validate(
            {"child": {"grandchild": secret}},
            version="1",
        )

    message = str(caught.value)
    assert "runtime_shape_nested_child" in message
    assert "nested path ('grandchild',)" in message
    assert secret not in message
    assert migrations == []


def test_applied_alias_collection_prunes_nested_child_metadata() -> None:
    class Child(BaseModel):
        value: int

    child_family = _runtime_family(
        model=Child,
        name="runtime_shape_alias_pruning_child",
    )
    historical_child = child_family.model_for("1")

    class Wrapper(BaseModel):
        child: Child

    class Parent(BaseModel):
        wrappers: list[Wrapper]

    historical_wrapper = create_model(
        "RuntimeShapeAliasPruningHistoricalWrapper",
        child=(historical_child, ...),
    )

    applied_alias = cast(Any, RuntimeItems)[historical_wrapper]
    historical_parent = create_model(
        "RuntimeShapeAliasPruningHistoricalParent",
        wrappers=(applied_alias, ...),
    )
    family = _nested_runtime_family(
        model=Parent,
        name="runtime_shape_alias_pruning_parent",
        wire_model=historical_parent,
        child_family=child_family,
        nested_path=("wrappers", "child"),
    )

    assert family.dump(
        version="1",
        data=Parent(wrappers=[Wrapper(child=Child(value=3))]),
    ) == {
        "wrappers": [{"child": {"value": 3}}],
        "schema_version": "1",
    }


def test_scalar_enum_mapping_preserves_foreign_version_metadata() -> None:
    class Child(BaseModel):
        value: int

    child_family = _runtime_family(
        model=Child,
        name="runtime_shape_scalar_mapping_child",
    )

    class HistoricalScalar(Enum):
        value = {
            "schema_version": "foreign",
            "value": "mapping-secret",
        }

    class Parent(BaseModel):
        child: Child

    class HistoricalParent(BaseModel):
        child: HistoricalScalar

    def upgrade(data: dict[str, Any]) -> dict[str, Any]:
        return {"child": {"value": 1}}

    def downgrade(_data: dict[str, Any]) -> dict[str, Any]:
        return {"child": HistoricalScalar.value}

    family = _nested_runtime_family(
        model=Parent,
        name="runtime_shape_scalar_mapping_parent",
        wire_model=HistoricalParent,
        upgrade=upgrade,
        downgrade=downgrade,
        exact_downgrade=True,
        child_family=child_family,
        nested_path="child",
        version_metadata=None,
    )

    assert family.dump(
        version="1",
        data=Parent(child=Child(value=4)),
    ) == {
        "child": {
            "schema_version": "foreign",
            "value": "mapping-secret",
        },
    }


def test_omitted_nested_route_rejects_target_extra_without_payload() -> None:
    class Child(BaseModel):
        value: int

    child_family = _runtime_family(
        model=Child,
        name="runtime_shape_omitted_extra_child",
    )

    class Parent(BaseModel):
        child: Child

    class HistoricalParent(BaseModel):
        model_config = ConfigDict(extra="allow")

        note: str

    def upgrade(data: dict[str, Any]) -> dict[str, Any]:
        return {"child": {"value": 1}}

    def downgrade(_data: dict[str, Any]) -> dict[str, Any]:
        return {
            "note": "historical",
            "child": {
                "schema_version": "foreign",
                "value": "omitted-secret",
            },
        }

    family = _nested_runtime_family(
        model=Parent,
        name="runtime_shape_omitted_extra_parent",
        wire_model=HistoricalParent,
        upgrade=upgrade,
        downgrade=downgrade,
        exact_downgrade=True,
        child_family=child_family,
        nested_path="child",
        version_metadata=None,
    )

    with pytest.raises(ValueError) as caught:
        family.dump(
            version="1",
            data=Parent(child=Child(value=4)),
        )

    message = str(caught.value)
    assert "runtime_shape_omitted_extra_child" in message
    assert "does not match expected label" in message
    assert "omitted-secret" not in message


def test_empty_tuple_nested_replacement_preserves_zero_cardinality() -> None:
    class Child(BaseModel):
        value: int

    child_family = _runtime_family(
        model=Child,
        name="runtime_shape_empty_tuple_child",
        version_metadata=None,
    )

    class Parent(BaseModel):
        children: list[Child]

    class HistoricalParent(BaseModel):
        children: tuple[()]

    def upgrade(_data: dict[str, Any]) -> dict[str, Any]:
        return {"children": []}

    def downgrade(_data: dict[str, Any]) -> dict[str, Any]:
        return {"children": ()}

    family = _nested_runtime_family(
        model=Parent,
        name="runtime_shape_empty_tuple_parent",
        wire_model=HistoricalParent,
        upgrade=upgrade,
        downgrade=downgrade,
        exact_downgrade=True,
        child_family=child_family,
        nested_path="children",
        version_metadata=None,
    )

    assert family.validate({"children": []}, version="1").current_model == Parent(
        children=[],
    )
    assert family.dump(version="1", data=Parent(children=[])) == {"children": []}


def _recursive_target_family(events: list[str]) -> SchemaFamily[Any]:
    class Grandchild(BaseModel):
        value: int = 1

    grandchild_family = _runtime_family(
        model=Grandchild,
        name="runtime_shape_recursive_target_grandchild",
        version_metadata=None,
    )

    class Child(BaseModel):
        grandchild: Grandchild = Field(default_factory=Grandchild)
        audit: str = "audit"

    class HistoricalChild(BaseModel):
        grandchild: str = Field("recursive-default-secret", validate_default=True)
        audit: str = "audit"

        @field_validator("grandchild", mode="after")
        @classmethod
        def break_declared_shape(cls, value: str) -> Any:
            events.append("validator")
            return {"foreign_version": value}

        @field_serializer("audit")
        def serialize_audit(self, value: str) -> str:
            events.append("serializer")
            return value

    def upgrade(data: dict[str, Any]) -> dict[str, Any]:
        return {
            "grandchild": {"value": int(data["grandchild"])},
            "audit": data["audit"],
        }

    def downgrade(data: dict[str, Any]) -> dict[str, Any]:
        events.append("downgrade")
        return {
            "grandchild": f"grandchild-secret-{data['grandchild']['value']}",
            "audit": data["audit"],
        }

    child_family = _nested_runtime_family(
        model=Child,
        name="runtime_shape_recursive_target_child",
        wire_model=HistoricalChild,
        upgrade=upgrade,
        downgrade=downgrade,
        exact_downgrade=True,
        child_family=grandchild_family,
        nested_path="grandchild",
    )

    class Parent(BaseModel):
        child: Child = Field(default_factory=Child)

    return _nested_runtime_family(
        model=Parent,
        name="runtime_shape_recursive_target_parent",
        child_family=child_family,
        nested_path="child",
        version_metadata=None,
    )


def test_generated_target_recursively_rejects_explicit_child_shape_break() -> None:
    events: list[str] = []
    family = _recursive_target_family(events)

    with pytest.raises(ValueError) as caught:
        family.dump(
            version="1",
            data={
                "child": {
                    "grandchild": {"value": 7},
                    "audit": "dump-secret",
                },
            },
        )

    message = str(caught.value)
    assert "runtime_shape_recursive_target_child" in message
    assert "version '1'" in message
    assert "nested path ('grandchild',)" in message
    assert "grandchild-secret" not in message
    assert "dump-secret" not in message
    assert events == ["downgrade", "validator"]


@pytest.mark.parametrize("operation", ["defaults_for", "dump"], ids=["defaults", "dump-none"])
def test_generated_defaults_recursively_reject_explicit_child_shape_break(
    operation: str,
) -> None:
    events: list[str] = []
    family = _recursive_target_family(events)

    with pytest.raises(ValueError) as caught:
        if operation == "defaults_for":
            family.defaults_for(version="1")
        else:
            family.dump(version="1", data=None)

    message = str(caught.value)
    assert "runtime_shape_recursive_target_child" in message
    assert "nested path ('grandchild',)" in message
    assert "recursive-default-secret" not in message
    assert events == ["validator"]


@pytest.mark.parametrize("representation_kind", ["model", "dataclass"])
def test_independent_structural_child_rejects_transitive_shape_break(
    representation_kind: str,
) -> None:
    events: list[str] = []

    class Grandchild(BaseModel):
        value: int

    grandchild_family = _runtime_family(
        model=Grandchild,
        name=f"runtime_shape_transitive_{representation_kind}_grandchild",
    )

    class Child(BaseModel):
        grandchild: Grandchild

    class HistoricalChild(BaseModel):
        grandchild: str

    def downgrade_child(data: dict[str, Any]) -> dict[str, Any]:
        events.append("child-downgrade")
        return {"grandchild": f"transitive-secret-{data['grandchild']['value']}"}

    child_family = _nested_runtime_family(
        model=Child,
        name=f"runtime_shape_transitive_{representation_kind}_child",
        wire_model=HistoricalChild,
        downgrade=downgrade_child,
        exact_downgrade=True,
        child_family=grandchild_family,
        nested_path="grandchild",
    )

    class Parent(BaseModel):
        child: Child
        audit: str

    if representation_kind == "model":

        class HistoricalChildRepresentation(BaseModel):
            grandchild: str

            @field_validator("grandchild", mode="after")
            @classmethod
            def corrupt_grandchild(cls, value: str) -> Any:
                events.append("validator")
                return {"schema_version": "foreign", "value": value}

    else:

        @pydantic_dataclass
        class HistoricalChildRepresentation:
            grandchild: str

            @field_validator("grandchild", mode="after")
            @classmethod
            def corrupt_grandchild(cls, value: str) -> Any:
                events.append("validator")
                return {"schema_version": "foreign", "value": value}

    class HistoricalParent(BaseModel):
        child: HistoricalChildRepresentation
        audit: str

        @field_serializer("audit")
        def serialize_audit(self, value: str) -> str:
            events.append("serializer")
            return value

    def downgrade_parent(data: dict[str, Any]) -> dict[str, Any]:
        events.append("parent-downgrade")
        return data

    family = _nested_runtime_family(
        model=Parent,
        name=f"runtime_shape_transitive_{representation_kind}_parent",
        wire_model=HistoricalParent,
        downgrade=downgrade_parent,
        exact_downgrade=True,
        child_family=child_family,
        nested_path="child",
    )

    with pytest.raises(ValueError) as caught:
        family.dump(
            version="1",
            data=Parent(
                child=Child(grandchild=Grandchild(value=7)),
                audit="audit-secret",
            ),
        )

    message = str(caught.value)
    assert child_family.name in message
    assert "nested path ('grandchild',)" in message
    assert "transitive-secret" not in message
    assert "audit-secret" not in message
    assert events == ["child-downgrade", "parent-downgrade", "validator"]


def test_alias_union_collection_rejects_transitive_dataclass_shape_break() -> None:
    migrations: list[str] = []

    class Grandchild(BaseModel):
        value: int

    grandchild_family = _runtime_family(
        model=Grandchild,
        name="runtime_shape_alias_union_collection_grandchild",
        version_metadata=None,
    )

    class Child(BaseModel):
        grandchild: Grandchild

    class HistoricalChild(BaseModel):
        grandchild: str

    child_family = _nested_runtime_family(
        model=Child,
        name="runtime_shape_alias_union_collection_child",
        wire_model=HistoricalChild,
        upgrade=lambda data: migrations.append("child-upgrade") or data,
        child_family=grandchild_family,
        nested_path="grandchild",
        version_metadata=None,
    )

    class Parent(BaseModel):
        children: list[Child]

    @pydantic_dataclass
    class HistoricalChildRepresentation:
        grandchild: str

    type HistoricalChildren = list[HistoricalChildRepresentation] | str

    class HistoricalParent(BaseModel):
        children: HistoricalChildren

        @field_validator("children", mode="after")
        @classmethod
        def corrupt_grandchild(cls, value: HistoricalChildren) -> Any:
            if isinstance(value, list):
                value[0].grandchild = cast(
                    Any,
                    {
                        "schema_version": "foreign",
                        "value": "alias-union-collection-secret",
                    },
                )
            return value

    family = _nested_runtime_family(
        model=Parent,
        name="runtime_shape_alias_union_collection_parent",
        wire_model=HistoricalParent,
        upgrade=lambda data: migrations.append("parent-upgrade") or data,
        child_family=child_family,
        nested_path="children",
        version_metadata=None,
    )

    with pytest.raises(ValueError) as caught:
        family.validate(
            {"children": [{"grandchild": "7"}]},
            version="1",
        )

    message = str(caught.value)
    assert child_family.name in message
    assert "nested path ('grandchild',)" in message
    assert "alias-union-collection-secret" not in message
    assert migrations == []


def test_independent_dataclass_default_rejects_transitive_shape_break() -> None:
    events: list[str] = []

    class Grandchild(BaseModel):
        value: int = 1

    grandchild_family = _runtime_family(
        model=Grandchild,
        name="runtime_shape_transitive_default_grandchild",
    )

    class Child(BaseModel):
        grandchild: Grandchild = Field(default_factory=Grandchild)

    class HistoricalChild(BaseModel):
        grandchild: str = "official"

    child_family = _nested_runtime_family(
        model=Child,
        name="runtime_shape_transitive_default_child",
        wire_model=HistoricalChild,
        child_family=grandchild_family,
        nested_path="grandchild",
    )

    class Parent(BaseModel):
        child: Child = Field(default_factory=Child)

    @pydantic_dataclass
    class HistoricalChildRepresentation:
        grandchild: str = Field("transitive-default-secret", validate_default=True)

        @field_validator("grandchild", mode="after")
        @classmethod
        def corrupt_grandchild(cls, value: str) -> Any:
            events.append("validator")
            return {"schema_version": "foreign", "value": value}

    class HistoricalParent(BaseModel):
        child: HistoricalChildRepresentation = Field(
            default_factory=HistoricalChildRepresentation,
        )

    family = _nested_runtime_family(
        model=Parent,
        name="runtime_shape_transitive_default_parent",
        wire_model=HistoricalParent,
        child_family=child_family,
        nested_path="child",
    )

    with pytest.raises(ValueError) as caught:
        family.defaults_for(version="1")

    message = str(caught.value)
    assert child_family.name in message
    assert "transitive-default-secret" not in message
    assert events == ["validator"]


@pytest.mark.filterwarnings("ignore:Item 'readonly'.*ReadOnly")
def test_parameterized_typed_dict_qualifiers_are_checked_after_validation() -> None:
    migrations: list[str] = []

    class Grandchild(BaseModel):
        value: int

    grandchild_family = _runtime_family(
        model=Grandchild,
        name="runtime_shape_generic_typed_dict_grandchild",
        version_metadata=None,
    )

    class Child(BaseModel):
        grandchild: Grandchild

    child_family = _nested_runtime_family(
        model=Child,
        name="runtime_shape_generic_typed_dict_child",
        child_family=grandchild_family,
        nested_path="grandchild",
        version_metadata=None,
    )

    class HistoricalChildRepresentation[Item](TypedDict, total=False):
        grandchild: Required[Item]
        optional: NotRequired[Item]
        readonly: ReadOnly[Item]

    class Parent(BaseModel):
        child: Child

    class HistoricalParent(BaseModel):
        child: HistoricalChildRepresentation[str]

        @field_validator("child", mode="after")
        @classmethod
        def corrupt_grandchild(
            cls,
            value: HistoricalChildRepresentation[str],
        ) -> Any:
            value["grandchild"] = cast(
                Any,
                {
                    "schema_version": "foreign",
                    "value": "typed-dict-secret",
                },
            )
            return value

    def upgrade(data: dict[str, Any]) -> dict[str, Any]:
        migrations.append("upgrade")
        return {"child": {"grandchild": {"value": 1}}}

    family = _nested_runtime_family(
        model=Parent,
        name="runtime_shape_generic_typed_dict_parent",
        wire_model=HistoricalParent,
        upgrade=upgrade,
        child_family=child_family,
        nested_path="child",
        version_metadata=None,
    )

    with pytest.raises(ValueError) as caught:
        family.validate({"child": {"grandchild": "1"}}, version="1")

    message = str(caught.value)
    assert family.name in message
    assert "typed-dict-secret" not in message
    assert migrations == []


@pytest.mark.parametrize(
    "alias_style",
    [
        "assignment",
        "base_model_cleared",
        "cleared",
        "empty",
        "merged",
        "metadata",
        "multiple_annotated",
        "precedence",
        "type_alias",
    ],
)
@pytest.mark.filterwarnings("ignore:The 'serialization_alias'.*no effect")
def test_independent_structural_scalar_alias_preserves_foreign_metadata(
    alias_style: str,
) -> None:
    class Grandchild(BaseModel):
        value: int

    grandchild_family = _runtime_family(
        model=Grandchild,
        name=f"runtime_shape_dataclass_alias_{alias_style}_grandchild",
    )

    class Child(BaseModel):
        grandchild: Grandchild

    class HistoricalChild(BaseModel):
        grandchild: str

    child_family = _nested_runtime_family(
        model=Child,
        name=f"runtime_shape_dataclass_alias_{alias_style}_child",
        wire_model=HistoricalChild,
        child_family=grandchild_family,
        nested_path="grandchild",
    )

    representations = {
        "assignment": RuntimeAssignmentAliasRepresentation,
        "base_model_cleared": RuntimeBaseModelClearedAliasRepresentation,
        "cleared": RuntimeClearedAliasRepresentation,
        "empty": RuntimeEmptyAliasRepresentation,
        "merged": RuntimeMergedAliasRepresentation,
        "metadata": RuntimeMetadataAliasRepresentation,
        "multiple_annotated": RuntimeMultipleAnnotatedAliasRepresentation,
        "precedence": RuntimeAliasPrecedenceRepresentation,
        "type_alias": RuntimeTypeAliasRepresentation,
    }
    historical_child_representation = representations[alias_style]
    output_name = {
        "assignment": "legacyGrandchild",
        "base_model_cleared": "grandchild",
        "cleared": "grandchild",
        "empty": "",
        "merged": "annotatedGrandchild",
        "metadata": "legacyGrandchild",
        "multiple_annotated": "secondGrandchild",
        "precedence": "assignedGrandchild",
        "type_alias": "grandchild",
    }[alias_style]
    input_name = {
        "base_model_cleared": "annotatedGrandchild",
        "cleared": "annotatedGrandchild",
        "empty": "",
    }.get(alias_style, "grandchild")

    class Parent(BaseModel):
        child: Child

    historical_parent = create_model(
        f"RuntimeDataclassAlias{alias_style.title()}HistoricalParent",
        child=(historical_child_representation, ...),
    )

    family = _nested_runtime_family(
        model=Parent,
        name=f"runtime_shape_dataclass_alias_{alias_style}_parent",
        wire_model=historical_parent,
        upgrade=lambda data: data,
        downgrade=lambda _data: {
            "child": {input_name: RuntimeHistoricalMappingScalar.legacy},
        },
        exact_downgrade=True,
        child_family=child_family,
        nested_path="child",
    )

    dumped = family.dump(
        version="1",
        data=Parent(child=Child(grandchild=Grandchild(value=7))),
    )
    assert dumped == {
        "child": {
            output_name: {
                "schema_version": "foreign",
                "value": "mapping-secret",
            },
        },
        "schema_version": "1",
    }
    assert family.dump(
        version="1",
        data=Parent(child=Child(grandchild=Grandchild(value=7))),
        by_alias=False,
    ) == {
        "child": {
            "grandchild": {
                "schema_version": "foreign",
                "value": "mapping-secret",
            },
        },
        "schema_version": "1",
    }


def test_parameterized_typed_dict_scalar_alias_preserves_foreign_metadata() -> None:
    class Grandchild(BaseModel):
        value: int

    grandchild_family = _runtime_family(
        model=Grandchild,
        name="runtime_shape_typed_dict_alias_grandchild",
    )

    class Child(BaseModel):
        grandchild: Grandchild

    class HistoricalChild(BaseModel):
        grandchild: str

    child_family = _nested_runtime_family(
        model=Child,
        name="runtime_shape_typed_dict_alias_child",
        wire_model=HistoricalChild,
        child_family=grandchild_family,
        nested_path="grandchild",
    )

    class Parent(BaseModel):
        child: Child

    class HistoricalParent(BaseModel):
        child: RuntimeTypedDictAliasRepresentation[RuntimeHistoricalMappingScalar]

    family = _nested_runtime_family(
        model=Parent,
        name="runtime_shape_typed_dict_alias_parent",
        wire_model=HistoricalParent,
        upgrade=lambda data: data,
        downgrade=lambda _data: {
            "child": {"grandchild": RuntimeHistoricalMappingScalar.legacy},
        },
        exact_downgrade=True,
        child_family=child_family,
        nested_path="child",
    )

    assert family.dump(
        version="1",
        data=Parent(child=Child(grandchild=Grandchild(value=7))),
    ) == {
        "child": {
            "legacyGrandchild": {
                "schema_version": "foreign",
                "value": "mapping-secret",
            },
        },
        "schema_version": "1",
    }


def test_independent_dataclass_can_omit_an_inner_managed_route() -> None:
    class Grandchild(BaseModel):
        value: int

    grandchild_family = _runtime_family(
        model=Grandchild,
        name="runtime_shape_structural_omission_grandchild",
    )

    class Child(BaseModel):
        grandchild: Grandchild

    child_family = _nested_runtime_family(
        model=Child,
        name="runtime_shape_structural_omission_child",
        child_family=grandchild_family,
        nested_path="grandchild",
    )

    class Parent(BaseModel):
        child: Child

    @dataclass
    class HistoricalChildRepresentation:
        note: str

    class HistoricalParent(BaseModel):
        child: HistoricalChildRepresentation

    family = _nested_runtime_family(
        model=Parent,
        name="runtime_shape_structural_omission_parent",
        wire_model=HistoricalParent,
        upgrade=lambda data: data,
        downgrade=lambda _data: {"child": {"note": "historical"}},
        exact_downgrade=True,
        child_family=child_family,
        nested_path="child",
    )

    assert family.dump(
        version="1",
        data=Parent(child=Child(grandchild=Grandchild(value=7))),
    ) == {
        "child": {"note": "historical"},
        "schema_version": "1",
    }


@pytest.mark.parametrize("injection", ["declared_default", "computed"])
def test_independent_structural_child_cannot_inject_foreign_family_metadata(
    injection: str,
) -> None:
    class Grandchild(BaseModel):
        value: int

    grandchild_family = _runtime_family(
        model=Grandchild,
        name=f"runtime_shape_metadata_{injection}_grandchild",
    )

    class Child(BaseModel):
        grandchild: Grandchild

    class HistoricalChild(BaseModel):
        grandchild: str

    child_family = _nested_runtime_family(
        model=Child,
        name=f"runtime_shape_metadata_{injection}_child",
        wire_model=HistoricalChild,
        child_family=grandchild_family,
        nested_path="grandchild",
    )

    class Parent(BaseModel):
        child: Child

    if injection == "declared_default":

        class HistoricalChildRepresentation(BaseModel):
            grandchild: str
            schema_version: str = "metadata-secret"

    else:

        class HistoricalChildRepresentation(BaseModel):
            grandchild: str

            @computed_field
            @property
            def schema_version(self) -> str:
                return "metadata-secret"

    class HistoricalParent(BaseModel):
        child: HistoricalChildRepresentation

    family = _nested_runtime_family(
        model=Parent,
        name=f"runtime_shape_metadata_{injection}_parent",
        wire_model=HistoricalParent,
        upgrade=lambda data: data,
        downgrade=lambda _data: {"child": {"grandchild": "historical"}},
        exact_downgrade=True,
        child_family=child_family,
        nested_path="child",
    )

    with pytest.raises(ValueError) as caught:
        family.dump(
            version="1",
            data=Parent(child=Child(grandchild=Grandchild(value=7))),
        )

    message = str(caught.value)
    assert child_family.name in message
    assert "does not match expected label" in message
    assert "metadata-secret" not in message


def test_independent_structural_metadata_envelope_rejects_siblings() -> None:
    class Grandchild(BaseModel):
        value: int

    grandchild_family = _runtime_family(
        model=Grandchild,
        name="runtime_shape_metadata_envelope_grandchild",
    )

    class Child(BaseModel):
        grandchild: Grandchild

    class HistoricalChild(BaseModel):
        grandchild: str

    child_family = _nested_runtime_family(
        model=Child,
        name="runtime_shape_metadata_envelope_child",
        wire_model=HistoricalChild,
        child_family=grandchild_family,
        nested_path="grandchild",
        version_metadata=VersionMetadata(("contract", "version"), owner="family"),
    )

    class Parent(BaseModel):
        child: Child

    class HistoricalChildRepresentation(BaseModel):
        grandchild: str
        contract: dict[str, Any] = Field(
            default_factory=lambda: {"version": "1", "sibling": "envelope-secret"},
        )

    class HistoricalParent(BaseModel):
        child: HistoricalChildRepresentation

    family = _nested_runtime_family(
        model=Parent,
        name="runtime_shape_metadata_envelope_parent",
        wire_model=HistoricalParent,
        upgrade=lambda data: data,
        downgrade=lambda _data: {"child": {"grandchild": "historical"}},
        exact_downgrade=True,
        child_family=child_family,
        nested_path="child",
    )

    with pytest.raises(ValueError) as caught:
        family.dump(
            version="1",
            data=Parent(child=Child(grandchild=Grandchild(value=7))),
        )

    message = str(caught.value)
    assert "without siblings" in message
    assert "envelope-secret" not in message


@pytest.mark.parametrize(
    ("contract", "expected_message"),
    [
        pytest.param(
            {"version": "1", "sibling": "source-envelope-secret"},
            "without siblings",
            id="sibling",
        ),
        pytest.param(
            {"sibling": "source-envelope-secret"},
            "without siblings",
            id="incomplete",
        ),
        pytest.param(
            {"version": "source-envelope-secret"},
            "payload declares a different label",
            id="foreign-label",
        ),
    ],
)
def test_source_preflight_rejects_invalid_family_metadata_envelope(
    contract: dict[str, str],
    expected_message: str,
) -> None:
    class Child(BaseModel):
        value: int

    child_family = _runtime_family(
        model=Child,
        name="runtime_shape_source_envelope_child",
        version_metadata=VersionMetadata(("contract", "version"), owner="family"),
    )

    class HistoricalChildRepresentation(BaseModel):
        value: int
        version: str = Field(
            default="1",
            validation_alias=AliasPath("contract", "version"),
            exclude=True,
        )

    class Parent(BaseModel):
        child: Child

    class HistoricalParent(BaseModel):
        child: HistoricalChildRepresentation

    family = _nested_runtime_family(
        model=Parent,
        name="runtime_shape_source_envelope_parent",
        wire_model=HistoricalParent,
        upgrade=lambda data: data,
        child_family=child_family,
        nested_path="child",
        version_metadata=None,
    )

    with pytest.raises(SchemaVersionError) as caught:
        family.validate(
            {
                "child": {
                    "value": 1,
                    "contract": contract,
                },
            },
            version="1",
        )

    message = str(caught.value)
    assert expected_message in message
    assert "source-envelope-secret" not in message


@pytest.mark.parametrize("injection", ["default", "validator"])
def test_validated_source_cannot_inject_excluded_family_metadata(
    injection: str,
) -> None:
    migrations: list[str] = []

    class Child(BaseModel):
        value: int

    child_family = _runtime_family(
        model=Child,
        name=f"runtime_shape_validated_source_{injection}_child",
    )

    if injection == "default":

        class HistoricalChildRepresentation(BaseModel):
            value: int
            schema_version: str = Field(
                default="validated-source-secret",
                exclude=True,
            )

    else:

        class HistoricalChildRepresentation(BaseModel):
            value: int
            schema_version: str = Field(default="1", exclude=True)

            @model_validator(mode="after")
            def inject_metadata(self) -> HistoricalChildRepresentation:
                self.schema_version = "validated-source-secret"
                return self

    class Parent(BaseModel):
        child: Child

    historical_parent = create_model(
        f"RuntimeValidatedSource{injection.title()}Parent",
        child=(HistoricalChildRepresentation, ...),
    )
    family = _nested_runtime_family(
        model=Parent,
        name=f"runtime_shape_validated_source_{injection}_parent",
        wire_model=historical_parent,
        upgrade=lambda data: migrations.append("upgrade") or data,
        child_family=child_family,
        nested_path="child",
        version_metadata=None,
    )

    with pytest.raises(SchemaVersionError) as caught:
        family.validate({"child": {"value": 1}}, version="1")

    assert "validated-source-secret" not in str(caught.value)
    assert migrations == []


def test_exact_nested_source_validation_cannot_inject_transitive_metadata() -> None:
    migrations: list[str] = []

    class Grandchild(BaseModel):
        value: int

    grandchild_family = _runtime_family(
        model=Grandchild,
        name="runtime_shape_exact_source_stage_grandchild",
    )

    class GrandchildRepresentation(BaseModel):
        value: int
        schema_version: str = Field(
            default="exact-source-stage-secret",
            exclude=True,
        )

    class Child(BaseModel):
        grandchild: Grandchild

    class HistoricalChild(BaseModel):
        grandchild: GrandchildRepresentation

    child_family = _nested_runtime_family(
        model=Child,
        name="runtime_shape_exact_source_stage_child",
        wire_model=HistoricalChild,
        upgrade=lambda data: migrations.append("child-upgrade") or data,
        child_family=grandchild_family,
        nested_path="grandchild",
        version_metadata=None,
    )

    @dataclass
    class IndependentGrandchildRepresentation:
        value: int

    @dataclass
    class IndependentChildRepresentation:
        grandchild: IndependentGrandchildRepresentation

    class Parent(BaseModel):
        child: Child

    historical_parent = create_model(
        "RuntimeExactSourceStageHistoricalParent",
        child=(IndependentChildRepresentation, ...),
    )
    family = _nested_runtime_family(
        model=Parent,
        name="runtime_shape_exact_source_stage_parent",
        wire_model=historical_parent,
        upgrade=lambda data: data,
        child_family=child_family,
        nested_path="child",
        version_metadata=None,
    )

    with pytest.raises(SchemaVersionError) as caught:
        family.validate(
            {"child": {"grandchild": {"value": 1}}},
            version="1",
        )

    assert "exact-source-stage-secret" not in str(caught.value)
    assert migrations == []


def test_exact_nested_source_validation_cannot_mutate_own_model_metadata() -> None:
    migrations: list[str] = []

    class Child(BaseModel):
        schema_version: str
        value: int

    class HistoricalChild(BaseModel):
        schema_version: Literal["1"] = "1"
        value: int

        @model_validator(mode="after")
        def inject_metadata(self) -> HistoricalChild:
            self.schema_version = cast(Any, "exact-source-model-secret")
            return self

    child_family = _runtime_family(
        model=Child,
        name="runtime_shape_exact_source_model_child",
        wire_model=HistoricalChild,
        upgrade=lambda data: migrations.append("child-upgrade") or data,
        version_metadata=VersionMetadata("schema_version", owner="model"),
    )

    @dataclass
    class IndependentChildRepresentation:
        value: int

    class Parent(BaseModel):
        child: Child

    historical_parent = create_model(
        "RuntimeExactSourceModelHistoricalParent",
        child=(IndependentChildRepresentation, ...),
    )
    family = _nested_runtime_family(
        model=Parent,
        name="runtime_shape_exact_source_model_parent",
        wire_model=historical_parent,
        upgrade=lambda data: data,
        child_family=child_family,
        nested_path="child",
        version_metadata=None,
    )

    with pytest.raises(SchemaVersionError) as caught:
        family.validate({"child": {"value": 1}}, version="1")

    assert "exact-source-model-secret" not in str(caught.value)
    assert migrations == []


@pytest.mark.parametrize("placement", ["direct", "nested"])
def test_validated_source_cannot_inject_family_owned_root_metadata(
    placement: str,
) -> None:
    migrations: list[str] = []

    class Current(BaseModel):
        value: int

    class Historical(BaseModel):
        model_config = ConfigDict(extra="allow")

        value: int

        @model_validator(mode="after")
        def inject_metadata(self) -> Historical:
            assert self.__pydantic_extra__ is not None
            self.__pydantic_extra__["schema_version"] = "family-root-secret"
            return self

    nested_family = _runtime_family(
        model=Current,
        name=f"runtime_shape_family_root_{placement}_child",
        wire_model=Historical,
        upgrade=lambda data: migrations.append("upgrade") or data,
    )

    with pytest.raises(SchemaVersionError) as caught:
        if placement == "direct":
            nested_family.validate(
                {"value": 1, "schema_version": "1"},
                version="1",
            )
        else:

            @dataclass
            class IndependentRepresentation:
                value: int

            class Parent(BaseModel):
                child: Current

            historical_parent = create_model(
                "RuntimeFamilyRootNestedHistoricalParent",
                child=(IndependentRepresentation, ...),
            )
            family = _nested_runtime_family(
                model=Parent,
                name="runtime_shape_family_root_nested_parent",
                wire_model=historical_parent,
                upgrade=lambda data: data,
                child_family=nested_family,
                nested_path="child",
                version_metadata=None,
            )
            family.validate({"child": {"value": 1}}, version="1")

    assert "family-root-secret" not in str(caught.value)
    assert migrations == []


def test_validated_source_cannot_delete_model_owned_root_metadata() -> None:
    migrations: list[str] = []

    class Current(BaseModel):
        schema_version: str
        value: int

    class Historical(BaseModel):
        schema_version: Literal["1"] = "1"
        value: int

        @model_validator(mode="after")
        def delete_metadata(self) -> Historical:
            incomplete = type(self).model_construct(value=self.value)
            del incomplete.schema_version
            return incomplete

    family = _runtime_family(
        model=Current,
        name="runtime_shape_deleted_model_metadata",
        wire_model=Historical,
        upgrade=lambda data: migrations.append("upgrade") or data,
        version_metadata=VersionMetadata("schema_version", owner="model"),
    )

    with pytest.raises(SchemaVersionError, match="omitted required version metadata"):
        family.validate(
            {"schema_version": "1", "value": 1},
            version="1",
        )

    assert migrations == []


@pytest.mark.parametrize("representation_kind", ["stdlib", "pydantic"])
def test_source_preflight_reads_slots_dataclass_metadata(
    representation_kind: str,
) -> None:
    class Child(BaseModel):
        value: int

    child_family = _runtime_family(
        model=Child,
        name=f"runtime_shape_slots_{representation_kind}_child",
    )

    if representation_kind == "stdlib":

        @dataclass(slots=True)
        class HistoricalChildRepresentation:
            value: int
            schema_version: str = Field(exclude=True)

    else:

        @pydantic_dataclass(slots=True)
        class HistoricalChildRepresentation:
            value: int
            schema_version: str = Field(exclude=True)

    class Parent(BaseModel):
        child: Child

    historical_parent = create_model(
        f"RuntimeSlots{representation_kind.title()}Parent",
        child=(HistoricalChildRepresentation, ...),
    )
    family = _nested_runtime_family(
        model=Parent,
        name=f"runtime_shape_slots_{representation_kind}_parent",
        wire_model=historical_parent,
        upgrade=lambda data: data,
        child_family=child_family,
        nested_path="child",
        version_metadata=None,
    )
    historical = HistoricalChildRepresentation(
        value=1,
        schema_version="slots-metadata-secret",
    )

    with pytest.raises(SchemaVersionError) as caught:
        family.validate({"child": historical}, version="1")

    assert "slots-metadata-secret" not in str(caught.value)


@pytest.mark.parametrize("metadata", ["wrong", "expected"])
def test_prevalidated_alias_path_metadata_is_checked_without_synthetic_siblings(
    metadata: str,
) -> None:
    class Child(BaseModel):
        value: int

    child_family = _runtime_family(
        model=Child,
        name=f"runtime_shape_prevalidated_alias_{metadata}_child",
        version_metadata=VersionMetadata(("contract", "version"), owner="family"),
    )

    class HistoricalChildRepresentation(BaseModel):
        value: int
        version: str = Field(
            validation_alias=AliasChoices(
                AliasPath("contract", "version"),
                AliasPath("contract", "legacy_version"),
            ),
            exclude=True,
        )

    class Parent(BaseModel):
        child: Child

    historical_parent = create_model(
        f"RuntimePrevalidatedAlias{metadata.title()}Parent",
        child=(HistoricalChildRepresentation, ...),
    )
    family = _nested_runtime_family(
        model=Parent,
        name=f"runtime_shape_prevalidated_alias_{metadata}_parent",
        wire_model=historical_parent,
        upgrade=lambda data: data,
        child_family=child_family,
        nested_path="child",
        version_metadata=None,
    )
    declared = "prevalidated-alias-secret" if metadata == "wrong" else "1"
    historical = HistoricalChildRepresentation.model_validate(
        {
            "value": 1,
            "contract": {"version": declared},
        },
    )

    if metadata == "wrong":
        with pytest.raises(SchemaVersionError) as caught:
            family.validate({"child": historical}, version="1")
        assert "prevalidated-alias-secret" not in str(caught.value)
        return

    validated = family.validate({"child": historical}, version="1")
    assert validated.current_model == Parent(child=Child(value=1))


def test_source_preflight_rejects_transitive_collection_metadata_envelope() -> None:
    class Grandchild(BaseModel):
        value: int

    grandchild_family = _runtime_family(
        model=Grandchild,
        name="runtime_shape_transitive_source_envelope_grandchild",
        version_metadata=VersionMetadata(("contract", "version"), owner="family"),
    )

    class HistoricalGrandchildRepresentation(BaseModel):
        value: int
        version: str = Field(
            validation_alias=AliasPath("contract", "version"),
            exclude=True,
        )

    class Child(BaseModel):
        grandchildren: list[Grandchild]

    class HistoricalChild(BaseModel):
        grandchildren: list[HistoricalGrandchildRepresentation]

    child_family = _nested_runtime_family(
        model=Child,
        name="runtime_shape_transitive_source_envelope_child",
        wire_model=HistoricalChild,
        upgrade=lambda data: data,
        child_family=grandchild_family,
        nested_path="grandchildren",
        version_metadata=None,
    )

    class Parent(BaseModel):
        child: Child

    historical_child_representation = create_model(
        "RuntimeShapeTransitiveSourceEnvelopeRepresentation",
        grandchildren=(list[HistoricalGrandchildRepresentation], ...),
    )

    historical_parent = create_model(
        "RuntimeShapeTransitiveSourceEnvelopeParentModel",
        child=(historical_child_representation, ...),
    )

    family = _nested_runtime_family(
        model=Parent,
        name="runtime_shape_transitive_source_envelope_parent",
        wire_model=historical_parent,
        upgrade=lambda data: data,
        child_family=child_family,
        nested_path="child",
        version_metadata=None,
    )

    with pytest.raises(SchemaVersionError) as caught:
        family.validate(
            {
                "child": {
                    "grandchildren": [
                        {
                            "value": 1,
                            "contract": {
                                "version": "1",
                                "sibling": "transitive-envelope-secret",
                            },
                        },
                    ],
                },
            },
            version="1",
        )

    message = str(caught.value)
    assert grandchild_family.name in message
    assert "without siblings" in message
    assert "transitive-envelope-secret" not in message


@pytest.mark.parametrize(
    ("representation", "input_path"),
    [
        pytest.param(
            RuntimePreflightDataclassAliasRepresentation,
            ("legacy", "grandchild"),
            id="dataclass-alias-choices",
        ),
        pytest.param(
            RuntimePreflightTypedDictAliasRepresentation,
            ("legacy", "grandchild"),
            id="typed-dict-alias-path",
        ),
        pytest.param(
            RuntimePreflightAliasDisabledDataclassRepresentation,
            ("grandchild",),
            id="dataclass-alias-disabled-uses-name",
        ),
        pytest.param(
            RuntimePreflightAliasDisabledTypedDictRepresentation,
            ("grandchild",),
            id="typed-dict-alias-disabled-uses-name",
        ),
        pytest.param(
            RuntimePreflightPopulateByNameDataclassRepresentation,
            ("grandchild",),
            id="dataclass-populate-by-name",
        ),
        pytest.param(
            RuntimePreflightPopulateByNameTypedDictRepresentation,
            ("grandchild",),
            id="typed-dict-populate-by-name",
        ),
        pytest.param(
            RuntimePreflightMultipleAnnotatedAliasRepresentation,
            ("secondGrandchild",),
            id="later-annotated",
        ),
        pytest.param(
            RuntimePreflightMetadataAliasRepresentation,
            ("metadataGrandchild",),
            id="dataclass-metadata",
        ),
        pytest.param(
            RuntimePreflightAssignedAliasRepresentation,
            ("assignedGrandchild",),
            id="assigned-field",
        ),
        pytest.param(
            RuntimePreflightClearedAliasRepresentation,
            ("grandchild",),
            id="explicit-none-clears",
        ),
        pytest.param(
            RuntimePreflightEmptyAliasRepresentation,
            ("",),
            id="empty-string-override",
        ),
    ],
)
def test_source_preflight_follows_structural_validation_aliases(
    representation: Any,
    input_path: tuple[str, ...],
) -> None:
    class Grandchild(BaseModel):
        value: int

    grandchild_family = _runtime_family(
        model=Grandchild,
        name="runtime_shape_structural_alias_preflight_grandchild",
    )

    class Child(BaseModel):
        grandchild: Grandchild

    class HistoricalChild(BaseModel):
        grandchild: RuntimePreflightGrandchildRepresentation

    child_family = _nested_runtime_family(
        model=Child,
        name="runtime_shape_structural_alias_preflight_child",
        wire_model=HistoricalChild,
        upgrade=lambda data: data,
        child_family=grandchild_family,
        nested_path="grandchild",
        version_metadata=None,
    )

    class Parent(BaseModel):
        child: Child

    historical_parent = create_model(
        f"RuntimeStructuralAliasPreflight{representation.__name__}",
        child=(representation, ...),
    )
    family = _nested_runtime_family(
        model=Parent,
        name=f"runtime_shape_structural_alias_preflight_{representation.__name__}",
        wire_model=historical_parent,
        upgrade=lambda data: data,
        child_family=child_family,
        nested_path="child",
        version_metadata=None,
    )

    child_payload: dict[str, Any] = {
        "value": 1,
        "schema_version": "alias-preflight-secret",
    }
    for component in reversed(input_path):
        child_payload = {component: child_payload}

    with pytest.raises(SchemaVersionError) as caught:
        family.validate({"child": child_payload}, version="1")

    message = str(caught.value)
    assert grandchild_family.name in message
    assert "payload declares a different label" in message
    assert "alias-preflight-secret" not in message


def test_source_preflight_checks_optional_structural_union_arm() -> None:
    class Child(BaseModel):
        value: int

    child_family = _runtime_family(
        model=Child,
        name="runtime_shape_optional_preflight_child",
    )

    class HistoricalChildRepresentation(BaseModel):
        value: int

    class Parent(BaseModel):
        child: Child

    class HistoricalParent(BaseModel):
        child: HistoricalChildRepresentation | None

    family = _nested_runtime_family(
        model=Parent,
        name="runtime_shape_optional_preflight_parent",
        wire_model=HistoricalParent,
        upgrade=lambda data: data,
        child_family=child_family,
        nested_path="child",
        version_metadata=None,
    )

    with pytest.raises(SchemaVersionError) as caught:
        family.validate(
            {
                "child": {
                    "value": 1,
                    "schema_version": "optional-preflight-secret",
                },
            },
            version="1",
        )

    assert "optional-preflight-secret" not in str(caught.value)


def test_source_preflight_checks_every_smart_union_structural_arm() -> None:
    class Grandchild(BaseModel):
        value: int

    grandchild_family = _runtime_family(
        model=Grandchild,
        name="runtime_shape_smart_union_preflight_grandchild",
    )

    class HistoricalGrandchildRepresentation(BaseModel):
        value: int

    class Child(BaseModel):
        grandchild: Grandchild

    class HistoricalChild(BaseModel):
        grandchild: HistoricalGrandchildRepresentation

    child_family = _nested_runtime_family(
        model=Child,
        name="runtime_shape_smart_union_preflight_child",
        wire_model=HistoricalChild,
        upgrade=lambda data: data,
        child_family=grandchild_family,
        nested_path="grandchild",
        version_metadata=None,
    )

    class AliasScoredModelArm(BaseModel):
        grandchild: HistoricalGrandchildRepresentation = Field(
            validation_alias="alternate",
        )
        first_score: dict[str, Any] = Field(validation_alias="grandchild")
        second_score: dict[str, Any] = Field(validation_alias="grandchild")

    class Parent(BaseModel):
        child: Child

    historical_parent = create_model(
        "RuntimeSmartUnionPreflightHistoricalParent",
        child=(RuntimeSmartUnionExactTypedDictArm | AliasScoredModelArm, ...),
    )

    family = _nested_runtime_family(
        model=Parent,
        name="runtime_shape_smart_union_preflight_parent",
        wire_model=historical_parent,
        upgrade=lambda data: data,
        child_family=child_family,
        nested_path="child",
        version_metadata=None,
    )
    payload = {
        "child": {
            "grandchild": {"value": 1, "schema_version": "1"},
            "alternate": {
                "value": 2,
                "schema_version": "smart-union-preflight-secret",
            },
        },
    }

    selected = cast(Any, historical_parent.model_validate(payload)).child
    assert isinstance(selected, AliasScoredModelArm)
    with pytest.raises(SchemaVersionError) as caught:
        family.validate(payload, version="1")

    message = str(caught.value)
    assert grandchild_family.name in message
    assert "payload declares a different label" in message
    assert "smart-union-preflight-secret" not in message


def test_source_preflight_checks_scalar_first_collection_union_arm() -> None:
    class Child(BaseModel):
        value: int

    child_family = _runtime_family(
        model=Child,
        name="runtime_shape_collection_union_preflight_child",
    )

    class HistoricalChildRepresentation(BaseModel):
        value: int

    class Parent(BaseModel):
        children: list[Child]

    class HistoricalParent(BaseModel):
        children: list[str] | list[HistoricalChildRepresentation]

    family = _nested_runtime_family(
        model=Parent,
        name="runtime_shape_collection_union_preflight_parent",
        wire_model=HistoricalParent,
        upgrade=lambda data: data,
        child_family=child_family,
        nested_path="children",
        version_metadata=None,
    )

    with pytest.raises(SchemaVersionError) as caught:
        family.validate(
            {
                "children": [
                    {
                        "value": 1,
                        "schema_version": "collection-union-preflight-secret",
                    },
                ],
            },
            version="1",
        )

    assert "collection-union-preflight-secret" not in str(caught.value)


@pytest.mark.parametrize(
    ("downgraded_values", "should_collapse"),
    [
        pytest.param([1, "1"], True, id="collapse"),
        pytest.param([1, 2], False, id="preserve"),
    ],
)
def test_independent_dataclass_preserves_recursive_set_cardinality_check(
    downgraded_values: list[int | str],
    should_collapse: bool,
) -> None:
    class Grandchild(BaseModel):
        model_config = ConfigDict(frozen=True)

        value: int | str

    class HistoricalGrandchild(BaseModel):
        model_config = ConfigDict(frozen=True)

        value: int

    grandchild_family = _runtime_family(
        model=Grandchild,
        name="runtime_shape_structural_cardinality_grandchild",
        wire_model=HistoricalGrandchild,
        version_metadata=None,
    )

    class Child(BaseModel):
        grandchildren: set[Grandchild]

    child_family = _nested_runtime_family(
        model=Child,
        name="runtime_shape_structural_cardinality_child",
        child_family=grandchild_family,
        nested_path="grandchildren",
        version_metadata=None,
    )

    class Parent(BaseModel):
        child: Child

    @dataclass
    class HistoricalChildRepresentation:
        grandchildren: set[int]

    class HistoricalParent(BaseModel):
        child: HistoricalChildRepresentation

    family = _nested_runtime_family(
        model=Parent,
        name="runtime_shape_structural_cardinality_parent",
        wire_model=HistoricalParent,
        upgrade=lambda data: data,
        downgrade=lambda _data: {
            "child": {"grandchildren": downgraded_values},
        },
        exact_downgrade=True,
        child_family=child_family,
        nested_path="child",
        version_metadata=None,
    )
    current = Parent(
        child=Child(
            grandchildren={
                Grandchild(value=1),
                Grandchild(value="1"),
            },
        ),
    )

    if should_collapse:
        with pytest.raises(InvalidMigrationError, match="set cardinality"):
            family.dump(version="1", data=current)
        return

    dumped = family.dump(version="1", data=current)
    assert set(dumped["child"]["grandchildren"]) == {1, 2}
