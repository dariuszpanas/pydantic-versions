from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Literal,
    NamedTuple,
    NewType,
    Self,
    TypedDict,
    cast,
    get_args,
    get_origin,
)
from typing import (
    TypeAliasType as StdlibTypeAliasType,
)

import pytest
from pydantic import (
    AfterValidator,
    AliasChoices,
    AliasPath,
    AnyUrl,
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    GetPydanticSchema,
    PlainSerializer,
    RootModel,
    SecretStr,
    Tag,
    TypeAdapter,
    ValidateAs,
    ValidationError,
    WithJsonSchema,
    computed_field,
    create_model,
    field_serializer,
    field_validator,
    model_serializer,
    model_validator,
)
from typing_extensions import TypeAliasType as ExtensionsTypeAliasType  # noqa: UP035

from pydantic_versions import (
    NestedFamily,
    SchemaCompilationError,
    SchemaFamily,
    SchemaVersion,
    UnsupportedWireModelError,
    VersionMetadata,
    VersionTransition,
    field_default,
    field_renamed,
    matching_labels,
)


def _assert_exact_version_field(
    model: type[BaseModel],
    *,
    field_name: str,
    label: str,
) -> None:
    field = model.model_fields[field_name]
    assert get_origin(field.annotation) is Literal
    assert get_args(field.annotation) == (label,)
    assert field.default == label

    property_schema = model.model_json_schema()["properties"][field_name]
    assert property_schema["const"] == label
    assert property_schema["default"] == label


def _assert_unsupported(model: type[BaseModel], *, family_name: str) -> None:
    family = SchemaFamily(
        model=model,
        name=family_name,
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    )

    with pytest.raises(UnsupportedWireModelError):
        family.compile()

    assert family._compiled is None

    with pytest.raises(UnsupportedWireModelError):
        family.compile()

    assert family._compiled is None


def test_unsupported_wire_model_error_is_a_public_compilation_error() -> None:
    assert issubclass(UnsupportedWireModelError, SchemaCompilationError)


def test_unsupported_wire_model_error_has_safe_context_and_chained_cause() -> None:
    sensitive_value = "never-render-this-schema-metadata"

    class NonJsonMetadata:
        def __repr__(self) -> str:
            return sensitive_value

    class NonJsonSchemaPayload(BaseModel):
        model_config = ConfigDict(
            json_schema_extra={"x-private": cast(Any, NonJsonMetadata())},
        )

        value: int = 1

    family = SchemaFamily(
        model=NonJsonSchemaPayload,
        name="safe_error_context",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        version_metadata=None,
    )

    with pytest.raises(UnsupportedWireModelError) as exc_info:
        family.compile()

    message = str(exc_info.value)
    model_name = f"{NonJsonSchemaPayload.__module__}.{NonJsonSchemaPayload.__qualname__}"
    assert "safe_error_context" in message
    assert "version '1'" in message
    assert model_name in message
    assert "non-JSON-serializable validation schema" in message
    assert sensitive_value not in message
    assert isinstance(exc_info.value.__cause__, TypeError)
    assert family._compiled is None


def test_current_behavior_runs_only_at_the_final_application_boundary() -> None:
    events: list[str] = []

    def validate_annotated(value: int) -> int:
        events.append("annotated-validator")
        if value != 10:
            msg = "current annotated value must be migrated first"
            raise ValueError(msg)
        return value

    def serialize_annotated(value: int) -> str:
        events.append("annotated-serializer")
        return f"annotated:{value}"

    class BehavioralPayload(BaseModel):
        annotated_value: Annotated[
            int,
            Field(gt=0),
            AfterValidator(validate_annotated),
            PlainSerializer(serialize_annotated, return_type=str),
        ]
        decorated_value: Annotated[int, Field(gt=0)]

        @field_validator("decorated_value")
        @classmethod
        def validate_decorated(cls, value: int) -> int:
            events.append("decorator-validator")
            if value != 10:
                msg = "current decorated value must be migrated first"
                raise ValueError(msg)
            return value

        @model_validator(mode="after")
        def validate_model(self) -> Self:
            events.append("model-validator")
            if self.annotated_value != self.decorated_value:
                msg = "current values must agree"
                raise ValueError(msg)
            return self

        @field_serializer("decorated_value")
        def serialize_decorated(self, value: int) -> str:
            events.append("decorator-serializer")
            return f"decorated:{value}"

        @computed_field
        @property
        def total(self) -> int:
            return self.annotated_value + self.decorated_value

        def model_post_init(self, context: Any, /) -> None:
            events.append("model-post-init")

    def upgrade(data: dict[str, Any]) -> dict[str, Any]:
        return {
            **data,
            "annotated_value": 10,
            "decorated_value": 10,
        }

    family = SchemaFamily(
        model=BehavioralPayload,
        name="behavioral_boundary",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(VersionTransition("1", "2", upgrade=upgrade),),
        version_metadata=None,
    )
    historical = family.model_for("1")
    current_wire = family.model_for("2")

    assert historical is not BehavioralPayload
    assert current_wire is not BehavioralPayload
    assert not issubclass(historical, BehavioralPayload)
    assert not issubclass(current_wire, BehavioralPayload)

    source = historical.model_validate({"annotated_value": 1, "decorated_value": 1})
    assert source.model_dump() == {"annotated_value": 1, "decorated_value": 1}
    assert "total" not in source.model_dump()
    assert (
        historical.model_json_schema(mode="serialization")["properties"]["annotated_value"]["type"]
        == "integer"
    )
    assert events == []

    with pytest.raises(ValidationError):
        historical.model_validate({"annotated_value": 0, "decorated_value": 1})

    result = family.validate(
        {"annotated_value": 1, "decorated_value": 1},
        version="1",
    )

    assert result.source_model.model_dump()["annotated_value"] == 1
    assert result.current_model.annotated_value == 10
    assert result.current_model.decorated_value == 10
    assert Counter(events) == Counter(
        {
            "annotated-validator": 1,
            "decorator-validator": 1,
            "model-validator": 1,
            "model-post-init": 1,
        }
    )

    assert result.current_model.model_dump() == {
        "annotated_value": "annotated:10",
        "decorated_value": "decorated:10",
        "total": 20,
    }
    assert events.count("annotated-serializer") == 1
    assert events.count("decorator-serializer") == 1

    events.clear()
    current_result = family.validate(
        {"annotated_value": 10, "decorated_value": 10},
        version="2",
    )

    assert current_result.current_model.annotated_value == 10
    assert Counter(events) == Counter(
        {
            "annotated-validator": 1,
            "decorator-validator": 1,
            "model-validator": 1,
            "model-post-init": 1,
        }
    )


def test_nested_annotated_behavior_runs_only_on_the_current_model() -> None:
    events: list[str] = []

    def validate_nested(value: int) -> int:
        events.append("validator")
        return value + 1

    def serialize_nested(value: int) -> str:
        events.append("serializer")
        return str(value)

    behavior = (
        AfterValidator(validate_nested),
        PlainSerializer(serialize_nested, return_type=str),
    )

    class NestedBehaviorPayload(BaseModel):
        values: list[Annotated[int, *behavior]]
        optional: Annotated[int, *behavior] | None = None
        mapping: Mapping[str, Annotated[int, *behavior]]
        sequence: Sequence[Annotated[int, *behavior]]

    family = SchemaFamily(
        model=NestedBehaviorPayload,
        name="nested_behavior_boundary",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )
    wire = family.model_for("1")
    payload = {
        "values": [1],
        "optional": 2,
        "mapping": {"value": 3},
        "sequence": [4],
    }
    source = wire.model_validate(payload)

    assert source.model_dump() == payload
    assert wire.model_json_schema(mode="serialization")["properties"]["values"]["items"] == {
        "type": "integer"
    }
    assert events == []

    result = family.validate(payload, version="1")

    assert result.current_model.values == [2]
    assert result.current_model.optional == 3
    assert result.current_model.mapping == {"value": 4}
    assert result.current_model.sequence == [5]
    assert events == ["validator"] * 4
    assert result.current_model.model_dump() == {
        "values": ["2"],
        "optional": "3",
        "mapping": {"value": "4"},
        "sequence": ["5"],
    }
    assert events == ["validator"] * 4 + ["serializer"] * 4


@pytest.mark.parametrize(
    "alias_type",
    [StdlibTypeAliasType, ExtensionsTypeAliasType],
    ids=["typing", "typing_extensions"],
)
def test_type_aliases_cannot_hide_runtime_field_behavior(alias_type: Any) -> None:
    calls: Counter[str] = Counter()

    def validate_alias(value: int) -> int:
        calls["validator"] += 1
        return value

    def serialize_alias(value: int) -> str:
        calls["serializer"] += 1
        return str(value)

    behavior_alias = alias_type(
        "BehaviorAlias",
        Annotated[
            int,
            AfterValidator(validate_alias),
            PlainSerializer(serialize_alias, return_type=str),
        ],
    )
    payload = create_model(
        "AliasBehaviorPayload",
        values=(list[behavior_alias], ...),
    )

    baseline = calls.copy()
    _assert_unsupported(payload, family_name="unsupported_alias_behavior")
    assert calls == baseline


@pytest.mark.parametrize(
    "alias_type",
    [StdlibTypeAliasType, ExtensionsTypeAliasType],
    ids=["typing", "typing_extensions"],
)
def test_type_aliases_cannot_hide_mutable_schema_metadata(alias_type: Any) -> None:
    schema_extra: dict[str, Any] = {"type": "integer", "x-static": ["source"]}
    schema_alias = alias_type("SchemaAlias", Annotated[int, WithJsonSchema(schema_extra)])
    payload = create_model(
        "AliasSchemaPayload",
        value=(schema_alias, ...),
    )

    _assert_unsupported(payload, family_name="unsupported_alias_schema")
    schema_extra["x-static"].append("caller")


def test_generated_fields_preserve_constraints_defaults_factories_and_static_schema() -> None:
    factory_calls = 0

    def make_tags() -> list[str]:
        nonlocal factory_calls
        factory_calls += 1
        return []

    class RichFields(BaseModel):
        required_value: Annotated[
            int,
            Field(
                gt=0,
                title="Positive value",
                description="Must remain positive on every wire contract.",
                examples=[3],
                json_schema_extra={"x-wire-field": "preserved"},
            ),
        ]
        threshold: Annotated[int, Field(ge=1, le=20)] = 10
        optional_note: str | None = None
        mutable_default: list[int] = [1]
        tags: list[str] = Field(default_factory=make_tags)

    family = SchemaFamily(
        model=RichFields,
        name="rich_fields",
        versions=(
            SchemaVersion("1", patches=(field_default("threshold", 5),)),
            SchemaVersion("2"),
        ),
        version_metadata=None,
    )
    historical = family.model_for("1")

    assert factory_calls == 0
    assert historical.model_fields["required_value"].is_required()
    assert historical.model_fields["threshold"].default == 5
    assert historical.model_fields["optional_note"].default is None

    schema = historical.model_json_schema()
    required_schema = schema["properties"]["required_value"]
    assert required_schema["exclusiveMinimum"] == 0
    assert required_schema["title"] == "Positive value"
    assert required_schema["description"] == "Must remain positive on every wire contract."
    assert required_schema["examples"] == [3]
    assert required_schema["x-wire-field"] == "preserved"
    assert schema["properties"]["threshold"]["minimum"] == 1
    assert schema["properties"]["threshold"]["maximum"] == 20
    assert schema["properties"]["threshold"]["default"] == 5
    assert "required_value" in schema["required"]

    first = historical.model_validate({"required_value": 1})
    second = historical.model_validate({"required_value": 2})

    assert factory_calls == 2
    first.tags.append("first")
    first.mutable_default.append(2)
    assert second.tags == []
    assert second.mutable_default == [1]

    with pytest.raises(ValidationError):
        historical.model_validate({"required_value": 0})
    with pytest.raises(ValidationError):
        historical.model_validate({"required_value": 1, "threshold": 21})


def test_validated_data_factory_rejects_a_changed_preceding_namespace() -> None:
    def from_value(data: dict[str, Any]) -> int:
        return data["value"] + 1

    class DependentFactoryPayload(BaseModel):
        value: int
        derived: int = Field(default_factory=from_value)

    family = SchemaFamily(
        model=DependentFactoryPayload,
        name="dependent_factory",
        versions=(
            SchemaVersion("1", patches=(field_renamed("value", "old_value"),)),
            SchemaVersion("2"),
        ),
        version_metadata=None,
    )

    with pytest.raises(UnsupportedWireModelError, match="validated-data default factory"):
        family.compile()


def test_validated_data_factory_is_rejected_when_preceding_behavior_is_stripped() -> None:
    calls: Counter[str] = Counter()

    def validate_value(value: int) -> int:
        calls["validator"] += 1
        return value + 1

    def from_value(data: dict[str, Any]) -> int:
        calls["factory"] += 1
        return data["value"] + 1

    class BehavioralFactoryPayload(BaseModel):
        value: Annotated[int, AfterValidator(validate_value)]
        derived: int = Field(default_factory=from_value)

    family = SchemaFamily(
        model=BehavioralFactoryPayload,
        name="behavioral_validated_data_factory",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        version_metadata=None,
    )

    with pytest.raises(UnsupportedWireModelError, match="validated-data default factory"):
        family.compile()

    assert calls == Counter()
    assert family._compiled is None


def test_zero_argument_factory_survives_a_changed_preceding_namespace() -> None:
    class IndependentFactoryPayload(BaseModel):
        value: int
        tags: list[str] = Field(default_factory=list)

    family = SchemaFamily(
        model=IndependentFactoryPayload,
        name="independent_factory",
        versions=(
            SchemaVersion("1", patches=(field_renamed("value", "old_value"),)),
            SchemaVersion("2"),
        ),
        version_metadata=None,
    )

    assert family.model_for("1").model_validate({"old_value": 1}).model_dump() == {
        "old_value": 1,
        "tags": [],
    }


def test_static_schema_mappings_are_deeply_snapshotted_at_compilation() -> None:
    model_schema_extra: dict[str, Any] = {"x-model": {"owners": ["source"]}}
    field_schema_extra: dict[str, Any] = {"x-field": {"owners": ["source"]}}

    class SnapshotPayload(BaseModel):
        model_config = ConfigDict(json_schema_extra=model_schema_extra)

        value: int = Field(json_schema_extra=field_schema_extra)

    family = SchemaFamily(
        model=SnapshotPayload,
        name="schema_mapping_snapshot",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    ).compile()
    wire = family.model_for("1")

    assert (
        wire.model_config["json_schema_extra"]
        is not SnapshotPayload.model_config["json_schema_extra"]
    )
    assert (
        wire.model_fields["value"].json_schema_extra
        is not SnapshotPayload.model_fields["value"].json_schema_extra
    )

    model_schema_extra["x-model"]["owners"].append("caller")
    field_schema_extra["x-field"]["owners"].append("caller")
    source_model_extra = SnapshotPayload.model_config["json_schema_extra"]
    source_field_extra = SnapshotPayload.model_fields["value"].json_schema_extra
    assert isinstance(source_model_extra, dict)
    assert isinstance(source_field_extra, dict)
    cast(dict[str, Any], source_model_extra)["x-model"]["owners"].append("model")
    cast(dict[str, Any], source_field_extra)["x-field"]["owners"].append("field")

    schema = wire.model_json_schema()
    assert schema["x-model"] == {"owners": ["source"]}
    assert schema["properties"]["value"]["x-field"] == {"owners": ["source"]}


@pytest.mark.parametrize(
    "schema_extra",
    [
        {"type": "string"},
        {"properties": {"fake": {"type": "string"}}},
        {"required": []},
        {"allOf": []},
        {"discriminator": {"propertyName": "wrong"}},
    ],
    ids=["type", "properties", "required", "composition", "discriminator"],
)
def test_model_schema_metadata_cannot_override_generated_structure(
    schema_extra: dict[str, Any],
) -> None:
    class StructuralSchemaPayload(BaseModel):
        model_config = ConfigDict(json_schema_extra=schema_extra)

        value: int

    _assert_unsupported(
        StructuralSchemaPayload,
        family_name="unsupported_structural_model_schema",
    )


def test_static_with_json_schema_metadata_is_preserved() -> None:
    class StaticSchemaPayload(BaseModel):
        value: Annotated[int, WithJsonSchema({"type": "integer", "x-static": True})]

    family = SchemaFamily(
        model=StaticSchemaPayload,
        name="static_schema_metadata",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )

    assert family.model_for("1").model_json_schema()["properties"]["value"]["x-static"] is True


def test_custom_with_json_schema_subtype_fails_without_hook_invocation() -> None:
    calls: Counter[str] = Counter()

    class CustomWithJsonSchema(WithJsonSchema):
        def __get_pydantic_json_schema__(self, core_schema: Any, handler: Any) -> Any:
            calls["schema"] += 1
            return super().__get_pydantic_json_schema__(core_schema, handler)

    class CustomSchemaPayload(BaseModel):
        value: Annotated[
            int,
            CustomWithJsonSchema({"type": "integer", "x-custom": True}),
        ]

    baseline = calls.copy()
    _assert_unsupported(
        CustomSchemaPayload,
        family_name="unsupported_custom_schema_metadata_subtype",
    )
    assert calls == baseline


def test_declarative_annotated_discriminator_is_preserved() -> None:
    class Cat(BaseModel):
        kind: Literal["cat"]
        lives: int

    class Dog(BaseModel):
        kind: Literal["dog"]
        bark: bool

    class PetPayload(BaseModel):
        pet: Annotated[Cat | Dog, Discriminator("kind")]

    family = SchemaFamily(
        model=PetPayload,
        name="declarative_discriminator",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )
    wire = family.model_for("1")

    value = wire.model_validate({"pet": {"kind": "cat", "lives": 9}})
    assert value.model_dump()["pet"] == {"kind": "cat", "lives": 9}
    assert wire.model_json_schema()["properties"]["pet"]["discriminator"]["propertyName"] == "kind"


def test_generated_fields_preserve_alias_modes_and_apply_generators_after_rename() -> None:
    def to_camel(field_name: str) -> str:
        head, *tail = field_name.split("_")
        return head + "".join(part.title() for part in tail)

    class AliasedPayload(BaseModel):
        model_config = ConfigDict(
            alias_generator=to_camel,
            populate_by_name=True,
            validate_by_alias=True,
            validate_by_name=True,
            serialize_by_alias=True,
        )

        value: int = Field(
            validation_alias=AliasChoices("valueIn", AliasPath("envelope", "value")),
            serialization_alias="valueOut",
        )
        current_name: bool = Field(alias="currentAlias")

    family = SchemaFamily(
        model=AliasedPayload,
        name="aliased_fields",
        versions=(
            SchemaVersion("1", patches=(field_renamed("current_name", "legacy_name"),)),
            SchemaVersion("2"),
        ),
        version_metadata=None,
    )
    historical = family.model_for("1")
    current = family.model_for("2")

    first = historical.model_validate({"valueIn": 1, "legacyName": True})
    second = historical.model_validate(
        {"envelope": {"value": 2}, "legacyName": False},
    )

    assert first.model_dump() == {"valueOut": 1, "legacyName": True}
    assert second.model_dump()["valueOut"] == 2
    assert historical.model_json_schema(mode="validation")["properties"].keys() >= {
        "valueIn",
        "legacyName",
    }
    assert historical.model_json_schema(mode="serialization")["properties"].keys() >= {
        "valueOut",
        "legacyName",
    }
    assert "currentAlias" not in historical.model_json_schema()["properties"]

    current_value = current.model_validate({"valueIn": 3, "currentAlias": True})
    assert current_value.model_dump() == {"valueOut": 3, "currentAlias": True}


def test_alias_handoff_uses_canonical_names_without_creating_forbidden_extras() -> None:
    class DisabledAliasPayload(BaseModel):
        model_config = ConfigDict(
            validate_by_alias=False,
            validate_by_name=True,
            extra="forbid",
        )

        value: int = Field(alias="wireValue")

    class SplitAliasPayload(BaseModel):
        model_config = ConfigDict(extra="forbid")

        value: int = Field(alias="wireOut", validation_alias="wireIn")

    class ComplexAliasPayload(BaseModel):
        model_config = ConfigDict(extra="forbid")

        choice: int = Field(validation_alias=AliasChoices("choiceIn", "legacyChoice"))
        path: int = Field(validation_alias=AliasPath("envelope", "path"))

    disabled = SchemaFamily(
        model=DisabledAliasPayload,
        name="disabled_alias_handoff",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )
    split = SchemaFamily(
        model=SplitAliasPayload,
        name="split_alias_handoff",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )
    complex_alias = SchemaFamily(
        model=ComplexAliasPayload,
        name="complex_alias_handoff",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )

    assert disabled.validate({"value": 1}, version="1").current_model.value == 1
    assert split.validate({"wireIn": 2}, version="1").current_model.value == 2
    complex_result = complex_alias.validate(
        {"choiceIn": 3, "envelope": {"path": 4}},
        version="1",
    )
    assert complex_result.current_model.choice == 3
    assert complex_result.current_model.path == 4


def test_generated_config_preserves_wire_settings_but_omits_lifecycle_settings() -> None:
    class Mode(StrEnum):
        FAST = "fast"

    class ConfiguredPayload(BaseModel):
        model_config = ConfigDict(
            title="Configured wire payload",
            json_schema_extra={"x-wire-contract": True},
            extra="forbid",
            strict=True,
            populate_by_name=True,
            validate_by_alias=True,
            validate_by_name=True,
            serialize_by_alias=True,
            loc_by_alias=True,
            use_enum_values=True,
            str_strip_whitespace=True,
            str_to_lower=True,
            coerce_numbers_to_str=True,
            val_temporal_unit="seconds",
            ser_json_temporal="milliseconds",
            ser_json_bytes="base64",
            val_json_bytes="base64",
            frozen=True,
            validate_assignment=True,
        )

        name: str
        count: int = Field(alias="Count")
        mode: Mode
        payload: bytes

    family = SchemaFamily(
        model=ConfiguredPayload,
        name="configured_payload",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )
    wire = family.model_for("1")

    preserved_keys = (
        "extra",
        "strict",
        "populate_by_name",
        "validate_by_alias",
        "validate_by_name",
        "serialize_by_alias",
        "loc_by_alias",
        "use_enum_values",
        "str_strip_whitespace",
        "str_to_lower",
        "coerce_numbers_to_str",
        "val_temporal_unit",
        "ser_json_temporal",
        "ser_json_bytes",
        "val_json_bytes",
    )
    for key in preserved_keys:
        assert wire.model_config[key] == ConfiguredPayload.model_config[key]

    assert wire.model_config.get("frozen") is None
    assert wire.model_config.get("validate_assignment") is None

    schema = wire.model_json_schema()
    assert schema["title"] == "Configured wire payload"
    assert schema["x-wire-contract"] is True

    value = wire.model_validate(
        {
            "name": " FAST ",
            "Count": 1,
            "mode": Mode.FAST,
            "payload": b"data",
        }
    )
    assert value.model_dump()["name"] == "fast"
    assert value.model_dump()["mode"] == "fast"
    assert value.model_dump()["Count"] == 1

    dynamic_value = cast(Any, value)
    dynamic_value.count = "not validated"
    assert dynamic_value.count == "not validated"

    with pytest.raises(ValidationError) as strict_error:
        wire.model_validate(
            {
                "name": "fast",
                "Count": "1",
                "mode": Mode.FAST,
                "payload": b"data",
            }
        )
    assert strict_error.value.errors()[0]["loc"] == ("Count",)

    with pytest.raises(ValidationError):
        wire.model_validate(
            {
                "name": "fast",
                "Count": 1,
                "mode": Mode.FAST,
                "payload": b"data",
                "unexpected": True,
            }
        )


def test_non_string_model_config_keys_raise_a_contextual_wire_error() -> None:
    class NonStringConfigPayload(BaseModel):
        value: int = 1

    config = cast(dict[Any, Any], NonStringConfigPayload.model_config)
    config[1] = True
    family = SchemaFamily(
        model=NonStringConfigPayload,
        name="non_string_config_key",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )

    with pytest.raises(UnsupportedWireModelError) as exc_info:
        family.compile()

    message = str(exc_info.value)
    assert "non_string_config_key" in message
    assert NonStringConfigPayload.__qualname__ in message
    assert "model configuration keys must be strings" in message
    assert family._compiled is None


def test_typed_extra_values_fail_instead_of_losing_their_wire_type() -> None:
    class TypedExtraPayload(BaseModel):
        model_config = ConfigDict(extra="allow")

        __pydantic_extra__: dict[str, int] = Field(init=False)
        value: int = 1

    _assert_unsupported(TypedExtraPayload, family_name="unsupported_typed_extras")


def test_typed_extra_values_in_a_mixin_after_base_model_are_rejected() -> None:
    class TypedExtraMixin:
        __pydantic_extra__: dict[str, int]

    class InheritedTypedExtraPayload(BaseModel, TypedExtraMixin):
        model_config = ConfigDict(extra="allow")

        value: int = 1

    assert InheritedTypedExtraPayload.__mro__.index(TypedExtraMixin) > (
        InheritedTypedExtraPayload.__mro__.index(BaseModel)
    )
    _assert_unsupported(
        InheritedTypedExtraPayload,
        family_name="unsupported_inherited_typed_extras",
    )


def test_nested_metadata_wrappers_preserve_the_body_extra_mode() -> None:
    class IgnoredExtraPayload(BaseModel):
        value: int = 1

    class AllowedExtraPayload(BaseModel):
        model_config = ConfigDict(extra="allow")

        value: int = 1

    class ForbiddenExtraPayload(BaseModel):
        model_config = ConfigDict(extra="forbid")

        value: int = 1

    metadata = VersionMetadata(("meta", "version"), owner="family")
    ignored = SchemaFamily(
        model=IgnoredExtraPayload,
        name="ignored_envelope_extra",
        versions=(SchemaVersion("1"),),
        version_metadata=metadata,
    ).model_for("1")
    allowed = SchemaFamily(
        model=AllowedExtraPayload,
        name="allowed_envelope_extra",
        versions=(SchemaVersion("1"),),
        version_metadata=metadata,
    ).model_for("1")
    forbidden = SchemaFamily(
        model=ForbiddenExtraPayload,
        name="forbidden_envelope_extra",
        versions=(SchemaVersion("1"),),
        version_metadata=metadata,
    ).model_for("1")
    payload = {"meta": {"version": "1", "note": "keep"}}

    assert ignored.model_validate(payload).model_dump()["meta"] == {"version": "1"}
    assert allowed.model_validate(payload).model_dump()["meta"] == {
        "version": "1",
        "note": "keep",
    }
    with pytest.raises(ValidationError):
        forbidden.model_validate(payload)


def test_every_family_owned_direct_discriminator_is_an_exact_literal() -> None:
    class FamilyOwnedPayload(BaseModel):
        value: int = 1

    family = SchemaFamily(
        model=FamilyOwnedPayload,
        name="family_owned_metadata",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    )

    for label in ("1", "2"):
        wire = family.model_for(label)
        _assert_exact_version_field(wire, field_name="schema_version", label=label)
        assert wire is not FamilyOwnedPayload
        assert not issubclass(wire, FamilyOwnedPayload)
        assert wire.model_validate({}).model_dump()["schema_version"] == label
        with pytest.raises(ValidationError):
            wire.model_validate({"schema_version": "wrong"})


def test_every_model_owned_direct_discriminator_is_an_exact_literal() -> None:
    class ModelOwnedPayload(BaseModel):
        schema_version: str = "2"
        value: int = 1

    family = SchemaFamily(
        model=ModelOwnedPayload,
        name="model_owned_metadata",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        version_metadata=VersionMetadata("schema_version", owner="model"),
    )

    for label in ("1", "2"):
        wire = family.model_for(label)
        _assert_exact_version_field(wire, field_name="schema_version", label=label)
        assert wire is not ModelOwnedPayload
        assert not issubclass(wire, ModelOwnedPayload)
        assert (
            wire.model_validate({"schema_version": label}).model_dump()["schema_version"] == label
        )
        with pytest.raises(ValidationError):
            wire.model_validate({"schema_version": "wrong"})


def test_model_owned_metadata_alias_modes_end_at_the_current_label() -> None:
    class AliasMetadataPayload(BaseModel):
        schema_version: str = Field(default="2", alias="wire_version")
        value: int = 1

    class ValidationAliasMetadataPayload(BaseModel):
        schema_version: str = Field(default="2", validation_alias="wire_version")
        value: int = 1

    class IdentityAliasMetadataPayload(BaseModel):
        model_config = ConfigDict(alias_generator=lambda name: name)

        schema_version: str = "2"
        value: int = 1

    cases = (
        ("alias", AliasMetadataPayload, "wire_version", "wire_version", "wire_version"),
        (
            "validation_alias",
            ValidationAliasMetadataPayload,
            "wire_version",
            "wire_version",
            "schema_version",
        ),
        (
            "identity_alias_generator",
            IdentityAliasMetadataPayload,
            "schema_version",
            "schema_version",
            "schema_version",
        ),
    )
    for suffix, model, metadata_path, validation_name, serialization_name in cases:
        family = SchemaFamily(
            model=model,
            name=f"model_metadata_{suffix}",
            versions=(SchemaVersion("1"), SchemaVersion("2")),
            version_metadata=VersionMetadata(metadata_path, owner="model"),
        )
        historical = family.model_for("1")

        validation_schema = historical.model_json_schema(mode="validation")
        serialization_schema = historical.model_json_schema(mode="serialization")
        assert validation_schema["properties"][validation_name]["const"] == "1"
        assert serialization_schema["properties"][serialization_name]["const"] == "1"

        result = family.validate({metadata_path: "1"})
        assert result.source_version == "1"
        assert result.source_model.model_dump()["schema_version"] == "1"
        assert result.current_model.model_dump()["schema_version"] == "2"


def test_model_owned_metadata_rejects_disabled_or_output_only_locations() -> None:
    class SerializationAliasPayload(BaseModel):
        schema_version: str = Field(default="2", serialization_alias="wire_version")

    class DisabledAliasPayload(BaseModel):
        model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)

        schema_version: str = Field(default="2", alias="wire_version")

    class DisabledNamePayload(BaseModel):
        schema_version: str = Field(default="2", validation_alias="wire_version")

    cases = (
        ("serialization_alias", SerializationAliasPayload, "wire_version"),
        ("disabled_alias", DisabledAliasPayload, "wire_version"),
        ("disabled_name", DisabledNamePayload, "schema_version"),
    )
    for suffix, model, path in cases:
        family = SchemaFamily(
            model=model,
            name=f"unsupported_model_metadata_{suffix}",
            versions=(SchemaVersion("1"), SchemaVersion("2")),
            version_metadata=VersionMetadata(path, owner="model"),
        )
        with pytest.raises(UnsupportedWireModelError, match="model-owned version metadata"):
            family.compile()
        assert family._compiled is None


def test_model_owned_metadata_rejects_a_historical_default_patch() -> None:
    class PatchedMetadataPayload(BaseModel):
        schema_version: str = "2"
        value: int = 1

    family = SchemaFamily(
        model=PatchedMetadataPayload,
        name="patched_model_metadata",
        versions=(
            SchemaVersion(
                "1",
                patches=(field_default("schema_version", "legacy"),),
            ),
            SchemaVersion("2"),
        ),
        version_metadata=VersionMetadata("schema_version", owner="model"),
    )

    with pytest.raises(UnsupportedWireModelError, match="historical default patch"):
        family.compile()

    assert family._compiled is None


def test_exact_model_metadata_replaces_incompatible_historical_constraints() -> None:
    class ConstrainedMetadataPayload(BaseModel):
        schema_version: Annotated[str, Field(pattern=r"^v\d+$")] = "v2"
        value: int = 1

    family = SchemaFamily(
        model=ConstrainedMetadataPayload,
        name="constrained_model_metadata",
        versions=(SchemaVersion("legacy"), SchemaVersion("v2")),
        version_metadata=VersionMetadata("schema_version", owner="model"),
    )
    historical = family.model_for("legacy")

    assert (
        historical.model_validate({"schema_version": "legacy"}).model_dump()["schema_version"]
        == "legacy"
    )
    schema = historical.model_json_schema()["properties"]["schema_version"]
    assert schema["const"] == "legacy"
    assert "pattern" not in schema
    assert family.validate({"schema_version": "legacy"}).current_model.schema_version == "v2"


def test_nested_family_owned_discriminator_is_an_exact_literal_document_wrapper() -> None:
    class NestedMetadataPayload(BaseModel):
        value: int = 1

    family = SchemaFamily(
        model=NestedMetadataPayload,
        name="nested_family_metadata",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        version_metadata=VersionMetadata(("meta", "details", "version"), owner="family"),
    )

    for label in ("1", "2"):
        wire = family.model_for(label)
        meta_model = wire.model_fields["meta"].annotation
        details_model = meta_model.model_fields["details"].annotation

        _assert_exact_version_field(details_model, field_name="version", label=label)
        assert wire.model_validate({}).model_dump()["meta"] == {"details": {"version": label}}
        with pytest.raises(ValidationError):
            wire.model_validate(
                {"meta": {"details": {"version": "wrong"}}},
            )


def test_unused_nested_declarations_are_rejected() -> None:
    class NestedPayload(BaseModel):
        value: int = 1

    nested_family = SchemaFamily(
        model=NestedPayload,
        name="unused_nested_payload",
        versions=(SchemaVersion("1"),),
    )

    class RootPayload(BaseModel):
        value: int = 1

    family = SchemaFamily(
        model=RootPayload,
        name="unused_nested_root",
        versions=(SchemaVersion("1"),),
        nested=(
            NestedFamily("unused", nested_family, matching_labels()),
            NestedFamily(("value", "also_unused"), nested_family, matching_labels()),
        ),
    )

    with pytest.raises(UnsupportedWireModelError, match="2 nested declarations do not match"):
        family.model_for("1")


def test_nested_projection_rewrites_deeply_nested_models() -> None:
    class InnerPayload(BaseModel):
        label: int

    inner_family = SchemaFamily(
        model=InnerPayload,
        name="nested_projection_inner_family",
        versions=(
            SchemaVersion("legacy", patches=(field_renamed("label", "value"),)),
            SchemaVersion("current"),
        ),
        missing_version="legacy",
    )

    class NestedPayload(BaseModel):
        inner: InnerPayload

    class RootPayload(BaseModel):
        nested: NestedPayload

    family = SchemaFamily(
        model=RootPayload,
        name="nested_projection_rewrite",
        versions=(SchemaVersion("legacy"), SchemaVersion("current")),
        nested=(NestedFamily(("nested", "inner"), inner_family, matching_labels()),),
        missing_version="legacy",
    )

    legacy_wire = family.model_for("legacy")
    current_wire = family.model_for("current")

    legacy = legacy_wire.model_validate({"nested": {"inner": {"value": 1}}})
    current = current_wire.model_validate({"nested": {"inner": {"label": 1}}})

    assert legacy.model_dump()["nested"]["inner"]["value"] == 1
    assert current.model_dump()["nested"]["inner"]["label"] == 1


def test_single_segment_tuple_metadata_keeps_tuple_path_semantics() -> None:
    class TupleMetadataPayload(BaseModel):
        value: int = 1

    family = SchemaFamily(
        model=TupleMetadataPayload,
        name="tuple_family_metadata",
        versions=(SchemaVersion("1"),),
        version_metadata=VersionMetadata(("version",), owner="family"),
    )
    wire = family.model_for("1")

    _assert_exact_version_field(wire, field_name="version", label="1")
    assert wire.model_validate({}).model_dump() == {"value": 1, "version": "1"}


def test_version_metadata_rejects_projected_name_and_alias_collisions() -> None:
    class RenamedCollisionPayload(BaseModel):
        value: int

    renamed = SchemaFamily(
        model=RenamedCollisionPayload,
        name="renamed_metadata_collision",
        versions=(
            SchemaVersion("1", patches=(field_renamed("value", "schema_version"),)),
            SchemaVersion("2"),
        ),
    )

    class ChoiceCollisionPayload(BaseModel):
        value: int = Field(validation_alias=AliasChoices("schema_version", "value"))

    choice = SchemaFamily(
        model=ChoiceCollisionPayload,
        name="choice_metadata_collision",
        versions=(SchemaVersion("1"),),
    )

    class PathCollisionPayload(BaseModel):
        value: int = Field(validation_alias=AliasPath("meta", "value"))

    alias_path = SchemaFamily(
        model=PathCollisionPayload,
        name="path_metadata_collision",
        versions=(SchemaVersion("1"),),
        version_metadata=VersionMetadata(("meta", "version"), owner="family"),
    )

    class ModelOwnedAliasCollisionPayload(BaseModel):
        model_config = ConfigDict(populate_by_name=True)

        schema_version: str = Field(default="1", alias="wire")
        value: int = Field(default=1, serialization_alias="wire")

    model_owned = SchemaFamily(
        model=ModelOwnedAliasCollisionPayload,
        name="model_owned_alias_collision",
        versions=(SchemaVersion("1"),),
        version_metadata=VersionMetadata("schema_version", owner="model"),
    )

    for family in (renamed, choice, alias_path, model_owned):
        with pytest.raises(UnsupportedWireModelError):
            family.compile()
        assert family._compiled is None


def test_generated_schema_refs_remain_distinct_after_label_sanitization() -> None:
    class CollisionPayload(BaseModel):
        value: int = 1

    family = SchemaFamily(
        model=CollisionPayload,
        name="wire_collision",
        versions=(SchemaVersion("1.0"), SchemaVersion("1-0"), SchemaVersion("2")),
    )
    dotted = family.model_for("1.0")
    dashed = family.model_for("1-0")

    schema = TypeAdapter(dotted | dashed).json_schema()
    definitions = schema["$defs"]
    version_constants = {
        definition["properties"]["schema_version"]["const"] for definition in definitions.values()
    }

    assert dotted.__name__ != dashed.__name__
    assert len(definitions) == 2
    assert version_constants == {"1.0", "1-0"}
    assert len({branch["$ref"] for branch in schema["anyOf"]}) == 2


def test_generated_schema_refs_resist_sanitized_family_name_collisions() -> None:
    class FamilyCollisionPayload(BaseModel):
        value: int = 1

    dotted = SchemaFamily(
        model=FamilyCollisionPayload,
        name="family.one",
        versions=(SchemaVersion("1"),),
    ).model_for("1")
    dashed = SchemaFamily(
        model=FamilyCollisionPayload,
        name="family-one",
        versions=(SchemaVersion("1"),),
    ).model_for("1")

    schema = TypeAdapter(dotted | dashed).json_schema()

    assert dotted.__name__ != dashed.__name__
    assert len(schema["$defs"]) == 2
    assert len({branch["$ref"] for branch in schema["anyOf"]}) == 2


def test_generated_schema_refs_resist_sanitized_dynamic_model_name_collisions() -> None:
    dotted_model = create_model("Payload.One", value=(int, 1))
    dashed_model = create_model("Payload-One", value=(int, 2))
    dotted = SchemaFamily(
        model=dotted_model,
        name="dynamic_model_collision",
        versions=(SchemaVersion("1"),),
    ).model_for("1")
    dashed = SchemaFamily(
        model=dashed_model,
        name="dynamic_model_collision",
        versions=(SchemaVersion("1"),),
    ).model_for("1")

    schema = TypeAdapter(dotted | dashed).json_schema()
    defaults = {
        definition["properties"]["value"]["default"] for definition in schema["$defs"].values()
    }

    assert dotted.__name__ != dashed.__name__
    assert len(schema["$defs"]) == 2
    assert defaults == {1, 2}
    assert len({branch["$ref"] for branch in schema["anyOf"]}) == 2


def test_root_models_fail_automatic_projection_without_partial_cache() -> None:
    class RootPayload(RootModel[list[str]]):
        pass

    _assert_unsupported(RootPayload, family_name="unsupported_root")


def test_unresolved_generics_fail_but_concrete_generic_models_are_supported() -> None:
    class GenericPayload[T](BaseModel):
        value: T

    _assert_unsupported(GenericPayload, family_name="unsupported_generic")

    concrete = SchemaFamily(
        model=GenericPayload[int],
        name="concrete_generic",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )
    wire = concrete.model_for("1")

    assert wire.model_validate({"value": 3}).model_dump()["value"] == 3
    with pytest.raises(ValidationError):
        wire.model_validate({"value": "not-an-int"})


def test_model_serializers_and_custom_model_schema_hooks_fail_projection() -> None:
    class SerializedPayload(BaseModel):
        value: int

        @model_serializer
        def serialize_model(self) -> dict[str, int]:
            return {"renamed": self.value}

    class CoreHookPayload(BaseModel):
        value: int

        @classmethod
        def __get_pydantic_core_schema__(
            cls,
            source_type: Any,
            handler: Any,
        ) -> Any:
            return handler(source_type)

    class JsonHookBase(BaseModel):
        @classmethod
        def __get_pydantic_json_schema__(
            cls,
            core_schema: Any,
            handler: Any,
        ) -> Any:
            schema = handler(core_schema)
            schema["x-custom-hook"] = True
            return schema

    class InheritedJsonHookPayload(JsonHookBase):
        value: int

    unsupported = (
        ("serializer", SerializedPayload),
        ("core_hook", CoreHookPayload),
        ("inherited_json_hook", InheritedJsonHookPayload),
    )
    for suffix, model in unsupported:
        _assert_unsupported(model, family_name=f"unsupported_{suffix}")


def test_legacy_json_encoders_fail_automatic_projection() -> None:
    encoder_calls = 0

    def encode_datetime(value: datetime) -> str:
        nonlocal encoder_calls
        encoder_calls += 1
        return value.isoformat()

    with pytest.warns(DeprecationWarning, match="json_encoders"):

        class EncodedPayload(BaseModel):
            model_config = ConfigDict(json_encoders={datetime: encode_datetime})

            created_at: datetime

    _assert_unsupported(EncodedPayload, family_name="unsupported_json_encoders")
    assert encoder_calls == 0


def test_serialization_exclusions_fail_automatic_projection() -> None:
    class ExcludedPayload(BaseModel):
        value: int = Field(default=1, exclude=True)

    class ConditionalExclusionPayload(BaseModel):
        value: int = Field(default=1, exclude_if=lambda value: value == 0)

    unsupported = (
        ("exclude", ExcludedPayload),
        ("exclude_if", ConditionalExclusionPayload),
    )
    for suffix, model in unsupported:
        _assert_unsupported(model, family_name=f"unsupported_{suffix}")


def test_callable_discriminators_fail_in_field_and_annotated_forms() -> None:
    def discriminator(value: Any) -> str | None:
        candidate = (
            value.get("kind") if isinstance(value, Mapping) else getattr(value, "kind", None)
        )
        return candidate if isinstance(candidate, str) else None

    class Cat(BaseModel):
        kind: Literal["cat"]

    class Dog(BaseModel):
        kind: Literal["dog"]

    class FieldDiscriminatorPayload(BaseModel):
        pet: Annotated[Cat, Tag("cat")] | Annotated[Dog, Tag("dog")] = Field(
            discriminator=Discriminator(discriminator),
        )

    class AnnotatedDiscriminatorPayload(BaseModel):
        pet: Annotated[
            Annotated[Cat, Tag("cat")] | Annotated[Dog, Tag("dog")],
            Discriminator(discriminator),
        ]

    unsupported = (
        ("field_callable_discriminator", FieldDiscriminatorPayload),
        ("annotated_callable_discriminator", AnnotatedDiscriminatorPayload),
    )
    for suffix, model in unsupported:
        _assert_unsupported(model, family_name=f"unsupported_{suffix}")


def test_custom_annotation_hooks_fail_without_generated_hook_invocation() -> None:
    calls: Counter[str] = Counter()

    class HookBase:
        @classmethod
        def __get_pydantic_core_schema__(cls, source_type: Any, handler: Any) -> Any:
            calls[cls.__name__] += 1
            return handler(int)

    class InheritedHook(HookBase):
        pass

    class GenericHook[T]:
        @classmethod
        def __get_pydantic_core_schema__(cls, source_type: Any, handler: Any) -> Any:
            calls[cls.__name__] += 1
            return handler(int)

    class SpoofedHook:
        __module__ = "pydantic.types"

        @classmethod
        def __get_pydantic_core_schema__(cls, source_type: Any, handler: Any) -> Any:
            calls[cls.__name__] += 1
            return handler(int)

    type HiddenHook = InheritedHook

    class DirectHookPayload(BaseModel):
        value: InheritedHook

    class GenericHookPayload(BaseModel):
        value: GenericHook[int]

    class AliasedHookPayload(BaseModel):
        value: HiddenHook

    class SpoofedHookPayload(BaseModel):
        value: SpoofedHook

    baseline = calls.copy()
    unsupported = (
        ("direct_annotation_hook", DirectHookPayload),
        ("generic_annotation_hook", GenericHookPayload),
        ("aliased_annotation_hook", AliasedHookPayload),
        ("spoofed_annotation_hook", SpoofedHookPayload),
    )
    for suffix, model in unsupported:
        _assert_unsupported(model, family_name=f"unsupported_{suffix}")
        assert calls == baseline


AliasValue = NewType("AliasValue", int)


def test_builtin_annotations_continue_to_work_with_compiled_wire_models() -> None:
    class BuiltinAliasPayload(BaseModel):
        value_int: int
        value_bool: bool
        alias: AliasValue

    family = SchemaFamily(
        model=BuiltinAliasPayload,
        name="builtin_annotations_supported",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )
    wire = family.model_for("1")
    value = wire.model_validate({"value_int": 1, "value_bool": True, "alias": 3})
    assert value.model_dump() == {"value_int": 1, "value_bool": True, "alias": 3}


def test_declarative_pydantic_annotation_hooks_remain_supported() -> None:
    class PydanticTypesPayload(BaseModel):
        secret: SecretStr
        url: AnyUrl

    family = SchemaFamily(
        model=PydanticTypesPayload,
        name="pydantic_annotation_types",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )
    wire = family.model_for("1")
    value = wire.model_validate({"secret": "hidden", "url": "https://example.com/path"})

    assert isinstance(value.model_dump()["secret"], SecretStr)
    assert isinstance(value.model_dump()["url"], AnyUrl)
    assert wire.model_json_schema()["properties"]["secret"]["format"] == "password"


def test_behavioral_dataclass_annotations_fail_but_plain_dataclasses_remain_supported() -> None:
    post_init_calls: list[int] = []

    @dataclass
    class PlainValue:
        value: int

    @dataclass
    class BehavioralValue:
        value: int

        def __post_init__(self) -> None:
            post_init_calls.append(self.value)
            self.value += 1

    class PlainDataclassPayload(BaseModel):
        item: PlainValue

    class BehavioralDataclassPayload(BaseModel):
        item: BehavioralValue

    plain_family = SchemaFamily(
        model=PlainDataclassPayload,
        name="plain_dataclass_annotation",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )
    assert plain_family.model_for("1").model_validate({"item": {"value": 1}}).model_dump() == {
        "item": {"value": 1}
    }

    baseline = list(post_init_calls)
    _assert_unsupported(
        BehavioralDataclassPayload,
        family_name="unsupported_behavioral_dataclass",
    )
    assert post_init_calls == baseline


def test_structured_annotations_with_behavior_in_nested_dataclass_are_rejected() -> None:
    @dataclass
    class InnerValue:
        value: Annotated[int, AfterValidator(lambda value: value if value > 0 else 0)]

    class NestedBehavioralPayload(BaseModel):
        value: InnerValue

    _assert_unsupported(
        NestedBehavioralPayload,
        family_name="unsupported_nested_behavioral_dataclass",
    )


def test_structured_annotations_with_behavior_in_nested_typed_dict_are_rejected() -> None:
    class InnerTypedDict(TypedDict):
        value: Annotated[int, AfterValidator(lambda value: value if value > 0 else 0)]

    class NestedBehavioralTypedDictPayload(BaseModel):
        value: InnerTypedDict

    _assert_unsupported(
        NestedBehavioralTypedDictPayload,
        family_name="unsupported_nested_behavioral_typeddict",
    )


def test_structured_annotations_with_behavior_in_nested_named_tuple_are_rejected() -> None:
    class InnerNamedTuple(NamedTuple):
        value: Annotated[int, AfterValidator(lambda value: value if value > 0 else 0)]

    class NestedBehavioralNamedTuplePayload(BaseModel):
        value: InnerNamedTuple

    _assert_unsupported(
        NestedBehavioralNamedTuplePayload,
        family_name="unsupported_nested_behavioral_namedtuple",
    )


if TYPE_CHECKING:

    class NotDefinedYet:
        value: int


if "NotDefinedYet" in globals():
    del NotDefinedYet


def test_structured_annotations_with_unresolved_forward_refs_are_rejected() -> None:
    @dataclass
    class ResolvingFuture:
        value: NotDefinedYet

    class ForwardRefStructuredPayload(BaseModel):
        value: ResolvingFuture

    _assert_unsupported(
        ForwardRefStructuredPayload,
        family_name="unsupported_structured_forward_ref",
    )


def test_custom_field_schema_metadata_fails_without_compiler_invocation() -> None:
    calls: Counter[str] = Counter()

    class ValidatedValue:
        def __init__(self, value: int) -> None:
            self.value = value

    class ValidationShape(BaseModel):
        value: int

    def instantiate(value: ValidationShape) -> ValidatedValue:
        calls["instantiate"] += 1
        return ValidatedValue(value.value)

    def generate_schema(source_type: Any, handler: Any) -> Any:
        calls["schema"] += 1
        return handler(int)

    class ValidateAsPayload(BaseModel):
        value: Annotated[ValidatedValue, ValidateAs(ValidationShape, instantiate)]

    class SchemaMetadataPayload(BaseModel):
        value: Annotated[int, GetPydanticSchema(generate_schema)]

    baseline = calls.copy()
    _assert_unsupported(ValidateAsPayload, family_name="unsupported_validate_as")
    _assert_unsupported(SchemaMetadataPayload, family_name="unsupported_field_schema")
    assert calls == baseline


def test_callable_schema_and_title_mutation_fails_without_compiler_invocation() -> None:
    calls: Counter[str] = Counter()

    def mutate_model_schema(schema: dict[str, Any]) -> None:
        calls["model_schema"] += 1
        schema["x-mutated"] = True

    def mutate_field_schema(schema: dict[str, Any]) -> None:
        calls["field_schema"] += 1
        schema["x-mutated"] = True

    def generate_model_title(model: type) -> str:
        calls["model_title"] += 1
        return model.__name__

    def generate_field_title(field_name: str, field_info: Any) -> str:
        calls["field_title"] += 1
        return field_name

    class CallableModelSchemaPayload(BaseModel):
        model_config = ConfigDict(json_schema_extra=mutate_model_schema)

        value: int

    class CallableFieldSchemaPayload(BaseModel):
        value: int = Field(json_schema_extra=mutate_field_schema)

    class CallableModelTitlePayload(BaseModel):
        model_config = ConfigDict(model_title_generator=generate_model_title)

        value: int

    class CallableFieldTitlePayload(BaseModel):
        model_config = ConfigDict(field_title_generator=generate_field_title)

        value: int

    baseline = calls.copy()
    unsupported = (
        ("model_schema_callable", CallableModelSchemaPayload),
        ("field_schema_callable", CallableFieldSchemaPayload),
        ("model_title_callable", CallableModelTitlePayload),
        ("field_title_callable", CallableFieldTitlePayload),
    )
    for suffix, model in unsupported:
        _assert_unsupported(model, family_name=f"unsupported_{suffix}")
        assert calls == baseline
