from __future__ import annotations

from collections.abc import Mapping
from copy import copy, deepcopy
from dataclasses import dataclass
from functools import cached_property
from inspect import signature
from typing import Annotated, Any, Literal, TypedDict, cast

import pytest
from pydantic import (
    AliasChoices,
    AliasGenerator,
    AliasPath,
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    computed_field,
    create_model,
    field_serializer,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic.functional_serializers import PlainSerializer, SerializeAsAny
from pydantic_core import core_schema

from pydantic_versions import (
    InvalidMigrationError,
    NestedFamily,
    SchemaFamily,
    SchemaVersion,
    UnsupportedWireModelError,
    VersionMetadata,
    VersionTransition,
    field_default,
    field_renamed,
    matching_labels,
)


def _to_dict(data: Mapping[str, Any]) -> dict[str, Any]:
    return dict(data)


def test_defaults_construct_the_target_without_planning_or_downgrading() -> None:
    factory_calls: list[str] = []

    def historical_factory() -> list[str]:
        factory_calls.append("historical")
        return ["legacy"]

    def unexpected_downgrade(data: dict[str, Any]) -> dict[str, Any]:
        pytest.fail(f"defaults unexpectedly executed a downgrade with {data!r}")

    class Config(BaseModel):
        items: list[str] = Field(default_factory=lambda: ["current"])

    family = SchemaFamily(
        model=Config,
        name="target_defaults_without_downgrades",
        versions=(
            SchemaVersion(
                "1",
                patches=(field_default("items", default_factory=historical_factory),),
            ),
            SchemaVersion("2"),
        ),
        transitions=(
            VersionTransition(
                "1",
                "2",
                upgrade=_to_dict,
                downgrade=unexpected_downgrade,
                downgrade_semantics="exact",
            ),
        ),
    )

    assert family.defaults_for(version="1") == {
        "items": ["legacy"],
        "schema_version": "1",
    }
    assert family.dump(version="1") == {
        "items": ["legacy"],
        "schema_version": "1",
    }
    assert factory_calls == ["historical", "historical"]


def test_empty_mapping_remains_conversion_input_and_is_not_defaults_syntax() -> None:
    class Config(BaseModel):
        required: str

    family = SchemaFamily(
        model=Config,
        name="empty_mapping_is_data",
        versions=(SchemaVersion("1"),),
    )

    with pytest.raises(ValidationError):
        family.dump(version="1", data={})
    with pytest.raises(ValidationError):
        family.defaults_for(version="1")


def test_target_default_factory_and_serializer_execute_once() -> None:
    events: list[str] = []

    def factory() -> int:
        events.append("factory")
        return 7

    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        value: int = Field(default_factory=factory)

        @model_serializer
        def serialize_model(self) -> dict[str, Any]:
            events.append("serializer")
            return {"value": self.value}

    family = SchemaFamily(
        model=Current,
        name="target_default_once",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
        version_metadata=None,
    )

    assert family.defaults_for(version="1") == {"value": 7}
    assert events == ["factory", "serializer"]


def test_complete_dump_defaults_to_json_and_target_serialization_aliases() -> None:
    class Config(BaseModel):
        model_config = ConfigDict(validate_by_alias=True, validate_by_name=False)

        worker_name: str = Field(
            validation_alias="inputWorker",
            serialization_alias="outputWorker",
        )

    family = SchemaFamily(
        model=Config,
        name="target_alias_contract",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )
    current = Config.model_validate({"inputWorker": "alpha"})

    assert family.dump(version="1", data=current) == {"outputWorker": "alpha"}
    assert family.dump(version="1", data=current, by_alias=False) == {"worker_name": "alpha"}


def test_explicit_wire_serializer_defines_target_output_without_revalidation() -> None:
    serializer_calls: list[int] = []

    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        value: int = 1

        @field_serializer("value")
        def serialize_value(self, value: int) -> str:
            serializer_calls.append(value)
            return f"legacy:{value}"

    family = SchemaFamily(
        model=Current,
        name="explicit_serializer_contract",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
        transitions=(
            VersionTransition(
                "1",
                "2",
                upgrade=_to_dict,
                downgrade=_to_dict,
                downgrade_semantics="exact",
            ),
        ),
        version_metadata=None,
    )

    assert family.defaults_for(version="1") == {"value": "legacy:1"}
    assert family.dump(version="1", data=Current(value=2)) == {"value": "legacy:2"}
    assert serializer_calls == [1, 2]


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("include", None),
        ("exclude", None),
        ("exclude_unset", False),
        ("exclude_defaults", False),
        ("exclude_none", False),
        ("exclude_computed_fields", False),
    ],
)
def test_complete_rendering_rejects_omission_options_by_presence(
    option: str,
    value: object,
) -> None:
    class Config(BaseModel):
        value: int = 1

    family = SchemaFamily(
        model=Config,
        name=f"reject_omission_{option}",
        versions=(SchemaVersion("1"),),
    )
    kwargs: dict[str, Any] = {option: value}

    with pytest.raises(ValueError, match=rf"unsupported model_dump option.*'{option}'"):
        family.dump(version="1", data=Config(), **kwargs)
    with pytest.raises(ValueError, match=rf"unsupported model_dump option.*'{option}'"):
        family.defaults_for(version="1", **kwargs)


def test_rendering_rejects_unknown_model_dump_options() -> None:
    class Config(BaseModel):
        value: int = 1

    family = SchemaFamily(
        model=Config,
        name="reject_future_dump_option",
        versions=(SchemaVersion("1"),),
    )

    with pytest.raises(ValueError, match="unsupported model_dump option.*'future_option'"):
        family.defaults_for(version="1", future_option=True)


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("serialize_as_any", True),
        ("polymorphic_serialization", True),
    ],
)
def test_rendering_rejects_polymorphic_subclass_leak_options(
    option: str,
    value: object,
) -> None:
    class PublicCredential(BaseModel):
        username: str

    class SecretCredential(PublicCredential):
        secret: str

    class Current(BaseModel):
        credential: PublicCredential

    class Historical(BaseModel):
        credential: PublicCredential = Field(
            default_factory=lambda: SecretCredential(username="worker", secret="token")
        )

    family = SchemaFamily(
        model=Current,
        name=f"reject_polymorphic_{option}",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
        version_metadata=None,
    )
    kwargs: dict[str, Any] = {option: value}

    with pytest.raises(ValueError, match="outside the target wire contract"):
        family.defaults_for(version="1", **kwargs)


def test_false_polymorphic_options_preserve_declared_target_shape() -> None:
    class PublicCredential(BaseModel):
        username: str

    class SecretCredential(PublicCredential):
        secret: str

    class Current(BaseModel):
        credential: PublicCredential

    class Historical(BaseModel):
        credential: PublicCredential = Field(
            default_factory=lambda: SecretCredential(username="worker", secret="token")
        )

    family = SchemaFamily(
        model=Current,
        name="false_polymorphic_options",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
        version_metadata=None,
    )

    assert family.defaults_for(
        version="1",
        serialize_as_any=False,
        polymorphic_serialization=None,
    ) == {"credential": {"username": "worker"}}
    assert family.defaults_for(
        version="1",
        polymorphic_serialization=False,
    ) == {"credential": {"username": "worker"}}


def test_round_trip_mode_cannot_omit_computed_target_fields() -> None:
    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        value: int = 1

        @computed_field
        @property
        def doubled(self) -> int:
            return self.value * 2

    family = SchemaFamily(
        model=Current,
        name="round_trip_computed_omission",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
        version_metadata=None,
    )

    assert family.defaults_for(version="1", round_trip=False) == {
        "value": 1,
        "doubled": 2,
    }
    with pytest.raises(ValueError, match="round_trip=True.*omit computed"):
        family.defaults_for(version="1", round_trip=True)


def test_explicit_target_wraps_family_owned_metadata_after_serialization() -> None:
    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        model_config = ConfigDict(extra="forbid")

        value: int = 1

    family = SchemaFamily(
        model=Current,
        name="explicit_family_metadata_wrapper",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
    )

    defaults = family.defaults_for(version="1")
    assert defaults == {
        "value": 1,
        "schema_version": "1",
    }
    dumped = family.dump(version="1", data=Current(value=2))
    assert dumped == {
        "value": 2,
        "schema_version": "1",
    }
    target = family.model_for("1")
    assert target.model_validate(dumped).model_dump(mode="json") == dumped
    assert family.validate(dumped).current_model == Current(value=2)
    assert family.defaults_for(version="1", include_version=False) == {"value": 1}
    assert target.model_json_schema(mode="validation")["properties"]["schema_version"] == {
        "const": "1",
        "default": "1",
        "title": "Schema Version",
        "type": "string",
    }


def test_model_owned_metadata_uses_selected_serialization_location() -> None:
    class Config(BaseModel):
        schema_version: str = Field(
            default="2",
            validation_alias="wire_version",
            serialization_alias="emitted_version",
        )
        value: int = 1

    family = SchemaFamily(
        model=Config,
        name="model_metadata_serialization_location",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        version_metadata=VersionMetadata("wire_version", owner="model"),
    )

    assert family.defaults_for(version="1") == {
        "emitted_version": "1",
        "value": 1,
    }
    assert family.defaults_for(version="1", by_alias=False) == {
        "schema_version": "1",
        "value": 1,
    }
    with pytest.raises(ValueError, match="model-owned.*include_version=False is unavailable"):
        family.defaults_for(version="1", include_version=False)


def test_runtime_rejects_conflicting_family_owned_serializer_metadata() -> None:
    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        value: int = 1

        @model_serializer
        def serialize_model(self) -> dict[str, Any]:
            return {"value": self.value, "schema_version": "wrong"}

    family = SchemaFamily(
        model=Current,
        name="conflicting_family_serializer_metadata",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
    )

    with pytest.raises(ValueError, match="conflicting version metadata.*wrong"):
        family.defaults_for(version="1")
    with pytest.raises(ValueError, match="conflicting version metadata.*wrong"):
        family.defaults_for(version="1", include_version=False)


def test_runtime_rejects_omitted_model_owned_serializer_metadata() -> None:
    class Current(BaseModel):
        schema_version: str = Field(default="2", alias="wire_version")
        value: int = 1

    class Historical(BaseModel):
        schema_version: Literal["1"] = Field(default="1", alias="wire_version")
        value: int = 1

        @model_serializer
        def serialize_model(self) -> dict[str, Any]:
            return {"value": self.value}

    family = SchemaFamily(
        model=Current,
        name="omitted_model_serializer_metadata",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
        version_metadata=VersionMetadata("wire_version", owner="model"),
    )

    with pytest.raises(ValueError, match="omitted model-owned version metadata"):
        family.defaults_for(version="1")
    with pytest.raises(ValueError, match="model-owned.*include_version=False is unavailable"):
        family.defaults_for(version="1", include_version=False)


def test_target_model_must_serialize_to_an_object() -> None:
    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        value: int = 1

        @model_serializer
        def serialize_model(self) -> dict[str, Any]:
            return cast(Any, "not-an-object")

    family = SchemaFamily(
        model=Current,
        name="object_shaped_target_output",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
        version_metadata=None,
    )

    with pytest.raises(ValueError, match="must serialize to an object"):
        family.defaults_for(version="1", warnings=False)


def test_nested_metadata_is_pruned_for_all_builtin_collection_shapes() -> None:
    class Child(BaseModel):
        model_config = ConfigDict(frozen=True)

        value: int = 1

    child_family = SchemaFamily(
        model=Child,
        name="pruned_collection_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    )

    class Parent(BaseModel):
        direct: Child
        listed: list[Child]
        variadic: tuple[Child, ...]
        fixed: tuple[Child, Child]
        set_values: set[Child]
        frozen_values: frozenset[Child]

    parent_family = SchemaFamily(
        model=Parent,
        name="pruned_collection_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        nested=(
            NestedFamily("direct", child_family, matching_labels()),
            NestedFamily("listed", child_family, matching_labels()),
            NestedFamily("variadic", child_family, matching_labels()),
            NestedFamily("fixed", child_family, matching_labels()),
            NestedFamily("set_values", child_family, matching_labels()),
            NestedFamily("frozen_values", child_family, matching_labels()),
        ),
    )

    rendered = parent_family.dump(
        version="1",
        data=Parent(
            direct=Child(value=1),
            listed=[Child(value=2)],
            variadic=(Child(value=3),),
            fixed=(Child(value=4), Child(value=5)),
            set_values={Child(value=6)},
            frozen_values=frozenset({Child(value=7)}),
        ),
    )

    assert rendered == {
        "direct": {"value": 1},
        "listed": [{"value": 2}],
        "variadic": [{"value": 3}],
        "fixed": [{"value": 4}, {"value": 5}],
        "set_values": [{"value": 6}],
        "frozen_values": [{"value": 7}],
        "schema_version": "1",
    }


def test_nested_metadata_pruning_translates_historical_names_before_aliases() -> None:
    class Child(BaseModel):
        value: int = 1

    child_family = SchemaFamily(
        model=Child,
        name="renamed_nested_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    )

    class Wrapper(BaseModel):
        child: Child = Field(serialization_alias="childWire")

    class Parent(BaseModel):
        wrapper: Wrapper

    parent_family = SchemaFamily(
        model=Parent,
        name="renamed_nested_parent",
        versions=(
            SchemaVersion("1", patches=(field_renamed("wrapper", "legacy_wrapper"),)),
            SchemaVersion("2"),
        ),
        transitions=(
            VersionTransition(
                "1",
                "2",
                upgrade=_to_dict,
                downgrade=_to_dict,
                downgrade_semantics="exact",
            ),
        ),
        nested=(NestedFamily(("wrapper", "child"), child_family, matching_labels()),),
    )
    current = Parent(wrapper=Wrapper(child=Child(value=4)))

    assert parent_family.dump(version="1", data=current) == {
        "legacy_wrapper": {"childWire": {"value": 4}},
        "schema_version": "1",
    }
    assert parent_family.dump(version="1", data=current, by_alias=False) == {
        "legacy_wrapper": {"child": {"value": 4}},
        "schema_version": "1",
    }


def test_nested_model_owned_metadata_is_verified_after_parent_serialization() -> None:
    class Child(BaseModel):
        schema_version: str = "2"
        value: int = 1

    class HistoricalChild(BaseModel):
        schema_version: Literal["1"] = "1"
        value: int = 1

        @model_serializer
        def serialize_model(self) -> dict[str, Any]:
            return {"schema_version": "wrong", "value": self.value}

    child_family = SchemaFamily(
        model=Child,
        name="nested_model_metadata_verification_child",
        versions=(
            SchemaVersion("1", wire_model=HistoricalChild),
            SchemaVersion("2"),
        ),
        transitions=(
            VersionTransition(
                "1",
                "2",
                upgrade=_to_dict,
                downgrade=_to_dict,
                downgrade_semantics="exact",
            ),
        ),
        version_metadata=VersionMetadata("schema_version", owner="model"),
    )

    class Parent(BaseModel):
        child: Child

    parent_family = SchemaFamily(
        model=Parent,
        name="nested_model_metadata_verification_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        nested=(NestedFamily("child", child_family, matching_labels()),),
    )

    with pytest.raises(ValueError, match="serialized version metadata.*wrong.*expected '1'"):
        parent_family.dump(
            version="1",
            data=Parent(child=Child(schema_version="2", value=7)),
        )


def test_nested_target_serializer_must_remain_object_shaped() -> None:
    class Child(BaseModel):
        value: int = 1

    class HistoricalChild(BaseModel):
        value: int = 1

        @model_serializer
        def serialize_model(self) -> dict[str, Any]:
            return cast(Any, [self.value])

    child_family = SchemaFamily(
        model=Child,
        name="nested_object_shape_child",
        versions=(
            SchemaVersion("1", wire_model=HistoricalChild),
            SchemaVersion("2"),
        ),
        transitions=(
            VersionTransition(
                "1",
                "2",
                upgrade=_to_dict,
                downgrade=_to_dict,
                downgrade_semantics="exact",
            ),
        ),
        version_metadata=None,
    )

    class Parent(BaseModel):
        child: Child

    parent_family = SchemaFamily(
        model=Parent,
        name="nested_object_shape_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        nested=(NestedFamily("child", child_family, matching_labels()),),
    )

    with pytest.raises(ValueError, match="Nested target wire model.*must serialize to an object"):
        parent_family.dump(version="1", data=Parent(child=Child(value=7)), warnings=False)


def test_validate_default_policy_is_preserved_for_target_defaults() -> None:
    class Current(BaseModel):
        value: int = 1

    class AcceptedHistorical(BaseModel):
        value: int = Field(default=cast(Any, "not-an-int"), validate_default=False)

    class RejectedHistorical(BaseModel):
        value: int = Field(default="not-an-int", validate_default=True)

    accepted = SchemaFamily(
        model=Current,
        name="accepted_unvalidated_target_default",
        versions=(
            SchemaVersion("1", wire_model=AcceptedHistorical),
            SchemaVersion("2"),
        ),
        version_metadata=None,
    )
    rejected = SchemaFamily(
        model=Current,
        name="rejected_invalid_target_default",
        versions=(
            SchemaVersion("1", wire_model=RejectedHistorical),
            SchemaVersion("2"),
        ),
        version_metadata=None,
    )

    assert accepted.defaults_for(version="1", warnings=False) == {"value": "not-an-int"}
    with pytest.raises(ValidationError):
        rejected.defaults_for(version="1")


def test_validate_default_validator_runs_once() -> None:
    events: list[int] = []

    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        value: int = Field(default=2, validate_default=True)

        @field_validator("value")
        @classmethod
        def record(cls, value: int) -> int:
            events.append(value)
            return value

    family = SchemaFamily(
        model=Current,
        name="target_default_validator_once",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
        version_metadata=None,
    )

    assert family.defaults_for(version="1") == {"value": 2}
    assert events == [2]


@pytest.mark.parametrize("collision", ["field", "choice", "path", "computed"])
def test_explicit_wire_body_cannot_collide_with_family_metadata(collision: str) -> None:
    class Current(BaseModel):
        value: int = 1

    if collision == "field":

        class Historical(BaseModel):
            schema_version: str = "body"

    elif collision == "choice":

        class Historical(BaseModel):
            value: int = Field(default=1, validation_alias=AliasChoices("schema_version", "value"))

    elif collision == "path":

        class Historical(BaseModel):
            value: int = Field(default=1, validation_alias=AliasPath("schema_version", "value"))

    else:

        class Historical(BaseModel):
            value: int = 1

            @computed_field(alias="schema_version")
            @property
            def marker(self) -> str:
                return "body"

    family = SchemaFamily(
        model=Current,
        name=f"explicit_metadata_collision_{collision}",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
    )

    with pytest.raises(UnsupportedWireModelError, match="version metadata collides"):
        family.compile()


@pytest.mark.parametrize("collision", ["fields", "computed", "nested"])
def test_duplicate_serialization_output_names_fail_compilation(collision: str) -> None:
    class Current(BaseModel):
        value: int = 1

    if collision == "fields":

        class Payload(BaseModel):
            first: int = Field(default=1, serialization_alias="shared")
            second: int = Field(default=2, serialization_alias="shared")

    elif collision == "computed":

        class Payload(BaseModel):
            first: int = Field(default=1, serialization_alias="shared")

            @computed_field(alias="shared")
            @property
            def second(self) -> int:
                return 2

    else:

        class Inner(BaseModel):
            first: int = Field(default=1, serialization_alias="shared")
            second: int = Field(default=2, serialization_alias="shared")

        class Payload(BaseModel):
            inner: Inner = Field(default_factory=Inner)

    family = SchemaFamily(
        model=Current,
        name=f"duplicate_serialization_{collision}",
        versions=(
            SchemaVersion("1", wire_model=Payload),
            SchemaVersion("2"),
        ),
        version_metadata=None,
    )

    with pytest.raises(UnsupportedWireModelError, match="duplicate output name 'shared'"):
        family.compile()


def test_family_document_adapter_preserves_public_model_operations() -> None:
    validated: list[int] = []

    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        model_config = ConfigDict(extra="forbid")

        value: int = 1

        @model_validator(mode="after")
        def require_exact_body_type(self) -> Historical:
            assert type(self) is Historical
            validated.append(self.value)
            return self

    family = SchemaFamily(
        model=Current,
        name="public_family_document_adapter",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
    )
    target = family.model_for("1")
    construct_target = cast(Any, target)

    direct = construct_target(value=3, schema_version="1")
    assert direct.model_dump(mode="json") == {"value": 3, "schema_version": "1"}
    assert validated == [3]

    constructed = target.model_construct(value=4, schema_version="1")
    assert constructed.model_dump(mode="json") == {"value": 4, "schema_version": "1"}
    assert validated == [3]

    copied = direct.model_copy(update={"value": 9})
    assert copied.model_dump(mode="json") == {"value": 9, "schema_version": "1"}
    assert direct.model_dump(mode="json") == {"value": 3, "schema_version": "1"}
    assert validated == [3]

    with pytest.raises(ValueError, match="expected '1'"):
        direct.model_copy(update={"schema_version": "wrong"})


def test_family_document_adapter_uses_active_json_schema_definitions() -> None:
    class Inner(BaseModel):
        name: str

    class Current(BaseModel):
        inner: Inner

    class Historical(BaseModel):
        inner: Inner

    family = SchemaFamily(
        model=Current,
        name="adapter_nested_json_schema",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
    )
    target = family.model_for("1")

    for mode in ("validation", "serialization"):
        schema = target.model_json_schema(mode=mode)
        assert schema["properties"]["inner"]["$ref"].startswith("#/$defs/")
        assert schema["properties"]["schema_version"]["const"] == "1"
        assert "Inner" in schema["$defs"]

    payload = {"inner": {"name": "worker"}, "schema_version": "1"}
    assert target.model_validate(payload).model_dump(mode="json") == payload


def test_family_document_adapter_supports_nested_metadata_paths() -> None:
    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        model_config = ConfigDict(extra="forbid")

        value: int = 1

    family = SchemaFamily(
        model=Current,
        name="adapter_nested_metadata_path",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
        version_metadata=VersionMetadata(("contract", "version"), owner="family"),
    )
    target = family.model_for("1")
    payload = {"value": 2, "contract": {"version": "1"}}

    assert target.model_validate(payload).model_dump(mode="json") == payload
    assert family.validate(payload).current_model == Current(value=2)
    assert family.dump(version="1", data=Current(value=2)) == payload
    schema = target.model_json_schema()
    contract_schema = schema["properties"]["contract"]
    assert contract_schema["additionalProperties"] is False
    assert contract_schema["required"] == ["version"]
    assert contract_schema["properties"]["version"]["const"] == "1"

    document = cast(Any, target).model_validate(payload)
    with pytest.raises(ValidationError, match="frozen"):
        document.contract.version = "wrong"
    assert document.model_dump(mode="json") == payload


def test_family_document_adapter_does_not_rerun_alias_generators() -> None:
    aliases: list[str] = []

    def stateful_alias(field_name: str) -> str:
        alias = f"{field_name}_{len(aliases) + 1}"
        aliases.append(alias)
        return alias

    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        model_config = ConfigDict(alias_generator=stateful_alias, populate_by_name=True)

        value: int = 1

    body_alias = Historical.model_fields["value"].alias
    calls_after_body = tuple(aliases)
    family = SchemaFamily(
        model=Current,
        name="adapter_stateful_alias_generator",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
    )
    target = family.model_for("1")

    assert tuple(aliases) == calls_after_body
    assert target.model_fields["value"].alias == body_alias
    assert body_alias in signature(target).parameters
    document = cast(Any, target)(**{cast(str, body_alias): 3}, schema_version="1")
    assert document.model_dump(mode="json", by_alias=True) == {
        cast(str, body_alias): 3,
        "schema_version": "1",
    }


def test_family_document_adapter_forwards_serialization_context_once() -> None:
    serializer_calls: list[int] = []

    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        value: int = 1

        @field_serializer("value")
        def serialize_value(self, value: int, info: Any) -> str:
            serializer_calls.append(value)
            return f"{info.context['prefix']}:{value}"

    family = SchemaFamily(
        model=Current,
        name="adapter_serialization_context",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
    )

    assert family.defaults_for(version="1", context={"prefix": "legacy"}) == {
        "value": "legacy:1",
        "schema_version": "1",
    }
    assert serializer_calls == [1]


def test_family_document_adapter_synchronizes_assignment_side_effects_and_extras() -> None:
    class Current(BaseModel):
        value: int = 1
        doubled: int = 2

    class Historical(BaseModel):
        model_config = ConfigDict(extra="allow", validate_assignment=True)

        value: int = 1
        doubled: int = 2

        @model_validator(mode="after")
        def synchronize_doubled(self) -> Historical:
            object.__setattr__(self, "doubled", self.value * 2)
            return self

    family = SchemaFamily(
        model=Current,
        name="adapter_assignment_synchronization",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
    )
    target = family.model_for("1")
    document = cast(Any, target)(
        value=2,
        doubled=0,
        schema_version="1",
        note="initial",
    )

    document.value = 4
    document.note = "updated"
    assert document.model_extra is not None
    document.model_extra["added"] = "in-place"
    del document.model_extra["note"]

    assert document.doubled == 8
    assert document.added == "in-place"
    with pytest.raises(AttributeError):
        _ = document.note
    assert document.model_dump(mode="json") == {
        "value": 4,
        "doubled": 8,
        "added": "in-place",
        "schema_version": "1",
    }


def test_family_document_adapter_delegates_declared_private_state_only() -> None:
    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        value: int = 1
        _marker: str = PrivateAttr(default="initial")

        def bump(self) -> Historical:
            self.value += 1
            return self

        @model_serializer
        def serialize_model(self) -> dict[str, Any]:
            return {"value": self.value, "marker": self._marker}

    family = SchemaFamily(
        model=Current,
        name="adapter_private_state",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
    )
    target = family.model_for("1")
    document = cast(Any, target)(value=2, schema_version="1")

    document._marker = "updated"

    assert document._marker == "updated"
    assert document.model_dump(mode="json") == {
        "value": 2,
        "marker": "updated",
        "schema_version": "1",
    }
    with pytest.raises(AttributeError):
        document.bump()


def test_family_document_adapter_exposes_declared_computed_fields() -> None:
    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        value: int = 1

        @computed_field
        @property
        def doubled(self) -> int:
            return self.value * 2

    family = SchemaFamily(
        model=Current,
        name="adapter_computed_field",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
    )
    target = family.model_for("1")
    document = cast(Any, target)(value=3, schema_version="1")

    assert target.model_computed_fields == Historical.model_computed_fields
    assert document.doubled == 6
    assert "doubled=6" in repr(document)
    assert document.model_dump(mode="json") == {
        "value": 3,
        "doubled": 6,
        "schema_version": "1",
    }


def test_family_document_adapter_synchronizes_field_and_extra_deletion() -> None:
    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        model_config = ConfigDict(extra="allow")

        value: int = 1

    family = SchemaFamily(
        model=Current,
        name="adapter_deletion_synchronization",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
    )
    target = family.model_for("1")
    document = cast(Any, target)(value=2, note="temporary", schema_version="1")

    del document.note
    assert document.model_dump(mode="json") == {"value": 2, "schema_version": "1"}

    del document.value
    assert document.model_dump(mode="json", warnings=False) == {"schema_version": "1"}

    with pytest.raises(AttributeError, match="cannot be deleted"):
        del document.schema_version


def test_family_document_adapter_shares_the_public_fields_set() -> None:
    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        value: int = 1

    family = SchemaFamily(
        model=Current,
        name="adapter_fields_set_synchronization",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
    )
    document = cast(Any, family.model_for("1"))(schema_version="1")

    assert document.model_dump(mode="json", exclude_unset=True) == {"schema_version": "1"}
    document.model_fields_set.add("value")
    assert document.model_dump(mode="json", exclude_unset=True) == {
        "value": 1,
        "schema_version": "1",
    }


def test_family_document_adapter_copy_protocol_does_not_share_the_body() -> None:
    class Nested(BaseModel):
        value: int = 1

    class Current(BaseModel):
        nested: Nested = Field(default_factory=Nested)

    class Historical(BaseModel):
        nested: Nested = Field(default_factory=Nested)
        _marker: str = PrivateAttr(default="original")

        @model_serializer
        def serialize_model(self) -> dict[str, Any]:
            return {"nested": self.nested, "marker": self._marker}

    family = SchemaFamily(
        model=Current,
        name="adapter_copy_protocol",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
    )
    document = cast(Any, family.model_for("1"))(schema_version="1")

    shallow = copy(document)
    shallow._marker = "shallow"
    shallow.nested.value = 2

    assert document.model_dump(mode="json") == {
        "nested": {"value": 2},
        "marker": "original",
        "schema_version": "1",
    }
    assert shallow.model_dump(mode="json") == {
        "nested": {"value": 2},
        "marker": "shallow",
        "schema_version": "1",
    }

    deep = deepcopy(document)
    deep._marker = "deep"
    deep.nested.value = 3

    assert document.model_dump(mode="json") == {
        "nested": {"value": 2},
        "marker": "original",
        "schema_version": "1",
    }
    assert deep.model_dump(mode="json") == {
        "nested": {"value": 3},
        "marker": "deep",
        "schema_version": "1",
    }


def test_family_document_adapter_rejects_serializer_owned_metadata_even_when_equal() -> None:
    serializer_calls: list[int] = []

    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        value: int = 1

        @model_serializer
        def serialize_model(self) -> dict[str, Any]:
            serializer_calls.append(self.value)
            return {"value": self.value, "schema_version": "1"}

    family = SchemaFamily(
        model=Current,
        name="adapter_equal_metadata_collision",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
    )

    with pytest.raises(ValueError, match="conflicting version metadata '1'.*reserved"):
        family.defaults_for(version="1")
    assert serializer_calls == [1]


def test_family_document_adapter_rejects_serializer_owned_metadata_root() -> None:
    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        value: int = 1

        @model_serializer
        def serialize_model(self) -> dict[str, Any]:
            return {"value": self.value, "contract": {"secret": "body"}}

    family = SchemaFamily(
        model=Current,
        name="adapter_nested_metadata_root_collision",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
        version_metadata=VersionMetadata(("contract", "version"), owner="family"),
    )

    with pytest.raises(ValueError, match="reserved path component 'contract'"):
        family.defaults_for(version="1")


def test_automatic_projection_ignores_non_wire_metadata_collisions() -> None:
    class Current(BaseModel):
        hidden: str = Field(default="private", alias="schema_version", exclude=True)
        value: int = 1

        @computed_field(alias="schema_version")
        @property
        def computed_marker(self) -> str:
            return "computed"

    family = SchemaFamily(
        model=Current,
        name="non_wire_metadata_collisions",
        versions=(SchemaVersion("1"),),
        version_metadata=VersionMetadata("schema_version", owner="family"),
    )

    assert family.defaults_for(version="1") == {
        "value": 1,
        "schema_version": "1",
    }


def test_excluded_field_does_not_create_duplicate_serialization_name() -> None:
    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        hidden: int = Field(default=1, serialization_alias="shared", exclude=True)
        visible: int = Field(default=2, serialization_alias="shared")

    family = SchemaFamily(
        model=Current,
        name="excluded_duplicate_serialization_name",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
        version_metadata=None,
    )

    assert family.defaults_for(version="1") == {"shared": 2}


def test_model_serializer_controls_duplicate_field_output_names() -> None:
    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        first: int = Field(default=1, serialization_alias="shared")
        second: int = Field(default=2, serialization_alias="shared")

        @model_serializer
        def serialize_model(self) -> dict[str, int]:
            return {"first": self.first, "second": self.second}

    family = SchemaFamily(
        model=Current,
        name="serializer_controls_output_names",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
        version_metadata=None,
    )

    assert family.defaults_for(version="1") == {"first": 1, "second": 2}


def test_field_serializer_controls_nested_duplicate_output_names() -> None:
    class Inner(BaseModel):
        first: int = Field(default=1, serialization_alias="shared")
        second: int = Field(default=2, serialization_alias="shared")

    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        inner: Inner = Field(default_factory=Inner)

        @field_serializer("inner")
        def serialize_inner(self, inner: Inner) -> dict[str, int]:
            return {"first": inner.first, "second": inner.second}

    family = SchemaFamily(
        model=Current,
        name="field_serializer_controls_nested_output_names",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
        version_metadata=None,
    )

    assert family.defaults_for(version="1") == {"inner": {"first": 1, "second": 2}}


def test_duplicate_serialization_names_inside_typed_dict_fail_compilation() -> None:
    class Inner(BaseModel):
        first: int = Field(default=1, serialization_alias="shared")
        second: int = Field(default=2, serialization_alias="shared")

    class Payload(TypedDict):
        inner: Inner

    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        payload: Payload

    family = SchemaFamily(
        model=Current,
        name="typed_dict_duplicate_serialization_name",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
        version_metadata=None,
    )

    with pytest.raises(UnsupportedWireModelError, match="duplicate output name 'shared'"):
        family.compile()


def test_nested_explicit_model_serializer_fails_closed() -> None:
    class Child(BaseModel):
        value: int = 1

    child_family = SchemaFamily(
        model=Child,
        name="serializer_nested_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    )

    class Current(BaseModel):
        child: Child

    class Historical(BaseModel):
        child: Child

        @model_serializer
        def relocate_child(self) -> dict[str, Any]:
            return {"relocated": self.child}

    parent_family = SchemaFamily(
        model=Current,
        name="serializer_nested_parent",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
        nested=(NestedFamily("child", child_family, matching_labels()),),
    )

    with pytest.raises(
        UnsupportedWireModelError,
        match="model-level serializer.*relocate child document paths",
    ):
        parent_family.compile()


def test_nested_explicit_field_serializer_fails_closed() -> None:
    class Child(BaseModel):
        value: int = 1

    child_family = SchemaFamily(
        model=Child,
        name="field_serializer_nested_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    )
    child_document = child_family.model_for("1")

    class Current(BaseModel):
        child: Child

    class RelocatingHistoricalBase(BaseModel):
        @field_serializer("child", check_fields=False)
        def relocate_child(self, child: BaseModel) -> dict[str, Any]:
            return {"payload": child.model_dump(mode="json")}

    historical = create_model(
        "FieldSerializerHistorical",
        __base__=RelocatingHistoricalBase,
        child=(child_document, ...),
    )

    parent_family = SchemaFamily(
        model=Current,
        name="field_serializer_nested_parent",
        versions=(
            SchemaVersion("1", wire_model=historical),
            SchemaVersion("2"),
        ),
        nested=(NestedFamily("child", child_family, matching_labels()),),
    )

    with pytest.raises(
        UnsupportedWireModelError,
        match="serializes field 'child'.*relocate child document paths",
    ):
        parent_family.compile()


@pytest.mark.parametrize("serializer_kind", ["model", "field"])
def test_nested_explicit_wrapper_serializer_fails_closed(serializer_kind: str) -> None:
    class Child(BaseModel):
        value: int = 1

    child_family = SchemaFamily(
        model=Child,
        name=f"wrapper_serializer_child_{serializer_kind}",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    )
    child_document = child_family.model_for("1")

    if serializer_kind == "model":

        class RelocatingWrapperBase(BaseModel):
            @model_serializer
            def relocate_child(self) -> dict[str, Any]:
                return {"payload": self.__dict__["child"]}

    else:

        class RelocatingWrapperBase(BaseModel):
            @field_serializer("child", check_fields=False)
            def relocate_child(self, child: BaseModel) -> dict[str, Any]:
                return {"payload": child.model_dump(mode="json")}

    historical_wrapper = create_model(
        f"HistoricalWrapper{serializer_kind.title()}",
        __base__=RelocatingWrapperBase,
        child=(child_document, ...),
    )

    class CurrentWrapper(BaseModel):
        child: Child

    class Current(BaseModel):
        wrapper: CurrentWrapper

    historical = create_model(
        f"WrapperSerializerHistorical{serializer_kind.title()}",
        wrapper=(historical_wrapper, ...),
    )

    parent_family = SchemaFamily(
        model=Current,
        name=f"wrapper_serializer_parent_{serializer_kind}",
        versions=(
            SchemaVersion("1", wire_model=historical),
            SchemaVersion("2"),
        ),
        nested=(NestedFamily(("wrapper", "child"), child_family, matching_labels()),),
    )

    with pytest.raises(UnsupportedWireModelError, match="relocate child document paths"):
        parent_family.compile()


def test_duplicate_serialization_names_on_typed_dict_fields_fail_compilation() -> None:
    class Payload(TypedDict):
        first: Annotated[int, Field(serialization_alias="shared")]
        second: Annotated[int, Field(serialization_alias="shared")]

    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        payload: Payload

    family = SchemaFamily(
        model=Current,
        name="typed_dict_field_alias_collision",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
        version_metadata=None,
    )

    with pytest.raises(UnsupportedWireModelError, match="duplicate output name 'shared'"):
        family.compile()


def test_duplicate_serialization_names_on_dataclass_fields_fail_compilation() -> None:
    @dataclass
    class Payload:
        first: Annotated[int, Field(serialization_alias="shared")] = 1
        second: Annotated[int, Field(serialization_alias="shared")] = 2

    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        payload: Payload = Field(default_factory=Payload)

    family = SchemaFamily(
        model=Current,
        name="dataclass_field_alias_collision",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
        version_metadata=None,
    )

    with pytest.raises(UnsupportedWireModelError, match="duplicate output name 'shared'"):
        family.compile()


def test_unconditional_computed_field_serializer_owns_its_output_shape() -> None:
    class Inner(BaseModel):
        first: int = Field(default=1, serialization_alias="shared")
        second: int = Field(default=2, serialization_alias="shared")

    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        @computed_field
        @property
        def inner(self) -> Inner:
            return Inner()

        @field_serializer("inner")
        def serialize_inner(self, inner: Inner) -> dict[str, int]:
            return {"first": inner.first, "second": inner.second}

    family = SchemaFamily(
        model=Current,
        name="computed_serializer_output_boundary",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
        version_metadata=None,
    )

    assert family.defaults_for(version="1") == {"inner": {"first": 1, "second": 2}}


def test_unconditional_plain_serializer_owns_its_output_shape() -> None:
    class Inner(BaseModel):
        first: int = Field(default=1, serialization_alias="shared")
        second: int = Field(default=2, serialization_alias="shared")

    safe_inner = Annotated[
        Inner,
        PlainSerializer(
            lambda value: {"first": value.first, "second": value.second},
            return_type=dict[str, int],
        ),
    ]

    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        inner: safe_inner = Field(default_factory=Inner)

    family = SchemaFamily(
        model=Current,
        name="plain_serializer_output_boundary",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
        version_metadata=None,
    )

    assert family.defaults_for(version="1") == {"inner": {"first": 1, "second": 2}}


@pytest.mark.parametrize("serializer_kind", ["field", "model", "plain"])
def test_json_only_serializers_do_not_hide_python_mode_alias_collisions(
    serializer_kind: str,
) -> None:
    class Inner(BaseModel):
        first: int = Field(default=1, serialization_alias="shared")
        second: int = Field(default=2, serialization_alias="shared")

    class Current(BaseModel):
        value: int = 1

    if serializer_kind == "field":

        class Historical(BaseModel):
            inner: Inner = Field(default_factory=Inner)

            @field_serializer("inner", when_used="json")
            def serialize_inner(self, inner: Inner) -> dict[str, int]:
                return {"first": inner.first, "second": inner.second}

    elif serializer_kind == "model":

        class Historical(BaseModel):
            inner: Inner = Field(default_factory=Inner)

            @model_serializer(when_used="json")
            def serialize_model(self) -> dict[str, Any]:
                return {"inner": {"first": self.inner.first, "second": self.inner.second}}

    else:
        json_inner = Annotated[
            Inner,
            PlainSerializer(
                lambda value: {"first": value.first, "second": value.second},
                return_type=dict[str, int],
                when_used="json",
            ),
        ]

        class Historical(BaseModel):
            inner: json_inner = Field(default_factory=Inner)

    family = SchemaFamily(
        model=Current,
        name=f"json_only_serializer_collision_{serializer_kind}",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
        version_metadata=None,
    )

    with pytest.raises(UnsupportedWireModelError, match="duplicate output name 'shared'"):
        family.compile()


def test_empty_serialization_alias_collisions_fail_compilation() -> None:
    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        first: int = Field(default=1, serialization_alias="")
        second: int = Field(default=2, serialization_alias="")

    family = SchemaFamily(
        model=Current,
        name="empty_serialization_alias_collision",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
        version_metadata=None,
    )

    with pytest.raises(UnsupportedWireModelError, match="duplicate output name ''"):
        family.compile()


def test_empty_model_metadata_serialization_alias_is_verified() -> None:
    class Config(BaseModel):
        schema_version: Literal["1"] = Field(
            default="1",
            validation_alias="wire_version",
            serialization_alias="",
        )
        value: int = 1

    family = SchemaFamily(
        model=Config,
        name="empty_model_metadata_alias",
        versions=(SchemaVersion("1"),),
        version_metadata=VersionMetadata("wire_version", owner="model"),
    )

    assert family.defaults_for(version="1") == {"": "1", "value": 1}


@pytest.mark.parametrize("declared", ["1", "wrong"])
def test_family_document_adapter_rejects_reserved_metadata_on_exact_body(
    declared: str,
) -> None:
    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        model_config = ConfigDict(extra="allow")

        value: int = 1

    family = SchemaFamily(
        model=Current,
        name="exact_body_reserved_metadata",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
    )
    target = family.model_for("1")
    body = Historical(value=2, schema_version=declared)

    with pytest.raises(ValidationError, match="reserved family-owned metadata root"):
        target.model_validate(body)


def test_family_document_adapter_checks_attribute_owned_metadata() -> None:
    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        model_config = ConfigDict(from_attributes=True)

        value: int = 1

    class Source:
        value = 2
        schema_version = "wrong"

    class MissingMetadata:
        value = 3

    class BrokenMetadata:
        value = 4

        @property
        def schema_version(self) -> str:
            msg = "metadata getter failed"
            raise RuntimeError(msg)

    family = SchemaFamily(
        model=Current,
        name="attribute_metadata_validation",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
    )
    target = family.model_for("1")

    with pytest.raises(ValidationError, match="expected '1'"):
        target.model_validate(Source(), from_attributes=True)
    with pytest.raises(ValidationError, match="could not be read"):
        target.model_validate(BrokenMetadata(), from_attributes=True)
    assert target.model_validate(MissingMetadata(), from_attributes=True).model_dump() == {
        "value": 3,
        "schema_version": "1",
    }


@pytest.mark.parametrize(
    "contract",
    [
        {"version": "1", "other": "body"},
        {"other": "body"},
    ],
)
def test_nested_family_metadata_reserves_its_complete_root(
    contract: dict[str, str],
) -> None:
    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        model_config = ConfigDict(extra="allow")

        value: int = 1

    family = SchemaFamily(
        model=Current,
        name="reserved_nested_metadata_root",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
        version_metadata=VersionMetadata(("contract", "version"), owner="family"),
    )

    with pytest.raises(ValidationError, match="reserves the entire root"):
        family.model_for("1").model_validate({"value": 2, "contract": contract})


def test_family_document_adapter_schema_callbacks_execute_once() -> None:
    schema_extra_calls: list[str] = []
    title_calls: list[str] = []

    def add_schema_extra(schema: dict[str, Any], model: type[BaseModel]) -> None:
        schema_extra_calls.append(model.__name__)
        schema["x-schema-extra"] = len(schema_extra_calls)
        properties = schema.setdefault("properties", {})
        properties["contract"] = {"description": "Reserved version envelope"}

    def title_for(model: type) -> str:
        title_calls.append(model.__name__)
        return f"Title-{model.__name__}"

    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        model_config = ConfigDict(
            json_schema_extra=add_schema_extra,
            model_title_generator=title_for,
        )

        value: int = 1

    family = SchemaFamily(
        model=Current,
        name="adapter_schema_callback_ownership",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
        version_metadata=VersionMetadata(("contract", "version"), owner="family"),
    )
    target = family.model_for("1")
    schema_extra_calls.clear()
    title_calls.clear()

    schema = target.model_json_schema()

    assert schema["x-schema-extra"] == 1
    assert schema["title"] == "Title-Historical"
    assert schema["properties"]["contract"] == {
        "additionalProperties": False,
        "description": "Reserved version envelope",
        "properties": {
            "version": {
                "const": "1",
                "default": "1",
                "title": "Version",
                "type": "string",
            }
        },
        "required": ["version"],
        "type": "object",
    }
    assert schema_extra_calls == ["Historical"]
    assert title_calls == ["Historical"]


def test_family_document_adapter_rejects_custom_schema_hook_before_execution() -> None:
    calls: list[str] = []

    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        value: int = 1

        @classmethod
        def __get_pydantic_json_schema__(cls, core_schema: Any, handler: Any) -> Any:
            calls.append(cls.__name__)
            return handler(core_schema)

    family = SchemaFamily(
        model=Current,
        name="adapter_custom_schema_hook",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
    )

    with pytest.raises(UnsupportedWireModelError, match="cannot safely compose custom model hook"):
        family.compile()
    assert calls == []


def test_family_document_adapter_does_not_replay_none_alias_generators() -> None:
    calls: list[str] = []

    def no_alias(field_name: str) -> None:
        calls.append(field_name)
        return None

    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        model_config = ConfigDict(
            alias_generator=AliasGenerator(
                alias=cast(Any, no_alias),
                validation_alias=cast(Any, no_alias),
                serialization_alias=cast(Any, no_alias),
            ),
        )

        value: int = 1

    after_body = tuple(calls)
    family = SchemaFamily(
        model=Current,
        name="adapter_none_alias_generator",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
    )
    target = family.model_for("1")

    assert tuple(calls) == after_body
    assert target.model_fields["value"].alias is None
    assert target.model_fields["value"].validation_alias is None
    assert target.model_fields["value"].serialization_alias is None


def test_family_document_adapter_preserves_default_field_deletion() -> None:
    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        value: int = 1

    family = SchemaFamily(
        model=Current,
        name="adapter_default_deletion",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
    )
    document = cast(Any, family.model_for("1"))(schema_version="1")

    del document.value

    with pytest.raises(AttributeError):
        _ = document.value
    assert "value" not in document.__dict__
    assert document.model_dump(warnings=False) == {"schema_version": "1"}


def test_family_document_adapter_deepcopy_uses_the_callers_memo() -> None:
    class Current(BaseModel):
        items: list[int] = Field(default_factory=list)

    class Historical(BaseModel):
        items: list[int] = Field(default_factory=list)

    family = SchemaFamily(
        model=Current,
        name="adapter_deepcopy_memo",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
    )
    document = cast(Any, family.model_for("1"))(items=[1], schema_version="1")

    copied = deepcopy([document, document.items])

    assert copied[0].items is copied[1]
    assert copied[0] is not document
    existing = document.model_copy()
    assert document.__deepcopy__({id(document): existing}) is existing


def test_family_document_adapter_private_state_participates_in_equality() -> None:
    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        value: int = 1
        _marker: str = PrivateAttr(default="a")

        @model_serializer
        def serialize_model(self) -> dict[str, Any]:
            return {"value": self.value, "marker": self._marker}

    family = SchemaFamily(
        model=Current,
        name="adapter_private_equality",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
    )
    target = cast(Any, family.model_for("1"))
    first = target(schema_version="1")
    second = target(schema_version="1")

    second._marker = "b"

    assert first != second
    assert first.model_dump() != second.model_dump()


def test_family_document_adapter_declared_state_wins_over_extras() -> None:
    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        model_config = ConfigDict(extra="allow")

        value: int = 1
        _marker: str = PrivateAttr(default="private")

        @computed_field
        @cached_property
        def doubled(self) -> int:
            return self.value * 2

    family = SchemaFamily(
        model=Current,
        name="adapter_declared_state_precedence",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
    )
    document = cast(Any, family.model_for("1")).model_validate(
        {
            "value": 2,
            "doubled": 999,
            "_marker": "extra",
            "schema_version": "1",
        },
    )

    assert document.doubled == 4
    assert document._marker == "private"
    del document.doubled
    assert document.doubled == 4


def test_family_document_adapter_resynchronizes_failed_assignment() -> None:
    class Current(BaseModel):
        value: int = 1
        side: int = 2

    class Historical(BaseModel):
        model_config = ConfigDict(validate_assignment=True)

        value: int = 1
        side: int = 2

        @model_validator(mode="after")
        def synchronize(self) -> Historical:
            object.__setattr__(self, "side", self.value * 2)
            if self.value == 13:
                msg = "unlucky value"
                raise ValueError(msg)
            return self

    family = SchemaFamily(
        model=Current,
        name="adapter_failed_assignment_sync",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
    )
    document = cast(Any, family.model_for("1"))(value=2, schema_version="1")

    with pytest.raises(ValidationError, match="unlucky value"):
        document.value = 13

    assert document.value == 13
    assert document.side == 26
    assert document.model_dump() == {"value": 13, "side": 26, "schema_version": "1"}


def test_family_document_adapter_honors_revalidation_policies() -> None:
    calls: list[int] = []

    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        model_config = ConfigDict(revalidate_instances="always")

        value: int = 1

        @model_validator(mode="after")
        def record(self) -> Historical:
            calls.append(self.value)
            return self

    family = SchemaFamily(
        model=Current,
        name="adapter_always_revalidation",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
    )
    target = cast(Any, family.model_for("1"))
    document = target(value=2, schema_version="1")
    calls.clear()

    revalidated = target.model_validate(document)

    assert revalidated is not document
    assert calls == [2]


def test_family_document_adapter_is_final() -> None:
    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        model_config = ConfigDict(revalidate_instances="subclass-instances")

        value: int = 1

    family = SchemaFamily(
        model=Current,
        name="adapter_subclass_revalidation",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
    )
    target = family.model_for("1")

    with pytest.raises(TypeError, match="document model.*is final"):
        type("SubTarget", (target,), {})


def test_optional_nested_child_serializer_cannot_impersonate_absence() -> None:
    class Child(BaseModel):
        value: int = 1

    class HistoricalChild(BaseModel):
        value: int = 1

        @model_serializer
        def serialize_model(self) -> dict[str, Any]:
            return cast(Any, None)

    child_family = SchemaFamily(
        model=Child,
        name="optional_none_serializer_child",
        versions=(
            SchemaVersion("1", wire_model=HistoricalChild),
            SchemaVersion("2"),
        ),
        transitions=(
            VersionTransition(
                "1",
                "2",
                upgrade=_to_dict,
                downgrade=_to_dict,
                downgrade_semantics="exact",
            ),
        ),
        version_metadata=None,
    )

    class Parent(BaseModel):
        child: Child | None = None

    parent_family = SchemaFamily(
        model=Parent,
        name="optional_none_serializer_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        nested=(NestedFamily("child", child_family, matching_labels()),),
    )

    assert parent_family.defaults_for(version="1") == {
        "child": None,
        "schema_version": "1",
    }
    with pytest.raises(ValueError, match="must serialize to an object"):
        parent_family.dump(version="1", data=Parent(child=Child(value=2)))


@pytest.mark.parametrize("collection", [False, True])
def test_annotation_serializer_cannot_rewrite_declared_nested_path(
    collection: bool,
) -> None:
    class Child(BaseModel):
        value: int = 1

    child_family = SchemaFamily(
        model=Child,
        name=f"functional_nested_child_{collection}",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        version_metadata=None,
    )
    annotation = list[Child] if collection else Child
    serialized_annotation = Annotated[
        annotation,
        PlainSerializer(
            lambda _value: [] if collection else {},
            return_type=list[dict[str, Any]] if collection else dict[str, Any],
        ),
    ]

    current_parent = create_model(
        f"CurrentFunctionalNestedParent{collection}",
        children=(annotation, ...),
    )

    historical_parent = create_model(
        f"FunctionalNestedParent{collection}",
        children=(serialized_annotation, ...),
    )
    path = "children"
    parent_family = SchemaFamily(
        model=current_parent,
        name=f"functional_nested_parent_{collection}",
        versions=(
            SchemaVersion("1", wire_model=historical_parent),
            SchemaVersion("2"),
        ),
        nested=(NestedFamily(path, child_family, matching_labels()),),
    )

    with pytest.raises(UnsupportedWireModelError, match="annotation-level serializer"):
        parent_family.compile()


def test_nested_collection_visitor_preserves_nested_optional_shapes() -> None:
    class Child(BaseModel):
        value: int = 1

    child_family = SchemaFamily(
        model=Child,
        name="deep_collection_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    )

    class Parent(BaseModel):
        children: list[list[Child | None]]

    parent_family = SchemaFamily(
        model=Parent,
        name="deep_collection_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        nested=(NestedFamily("children", child_family, matching_labels()),),
    )

    assert parent_family.dump(
        version="1",
        data=Parent(children=[[Child(value=2), None], [Child(value=3)]]),
    ) == {
        "children": [[{"value": 2}, None], [{"value": 3}]],
        "schema_version": "1",
    }


@pytest.mark.parametrize("collection_kind", ["set", "frozenset"])
def test_nested_serialization_preserves_set_cardinality(collection_kind: str) -> None:
    class Child(BaseModel):
        model_config = ConfigDict(frozen=True)

        value: int

    class HistoricalChild(BaseModel):
        model_config = ConfigDict(frozen=True)

        value: int

        @model_serializer
        def serialize_model(self) -> dict[str, int]:
            return {"value": 0}

    child_family = SchemaFamily(
        model=Child,
        name=f"serialized_{collection_kind}_child",
        versions=(
            SchemaVersion("1", wire_model=HistoricalChild),
            SchemaVersion("2"),
        ),
        transitions=(
            VersionTransition(
                "1",
                "2",
                upgrade=_to_dict,
                downgrade=_to_dict,
                downgrade_semantics="exact",
            ),
        ),
        version_metadata=None,
    )
    annotation = set[Child] if collection_kind == "set" else frozenset[Child]
    parent_model = create_model(
        f"Serialized{collection_kind.title()}Parent",
        children=(annotation, ...),
    )
    parent_family = SchemaFamily(
        model=parent_model,
        name=f"serialized_{collection_kind}_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        nested=(NestedFamily("children", child_family, matching_labels()),),
    )
    values = {Child(value=1), Child(value=2)}
    container = values if collection_kind == "set" else frozenset(values)

    with pytest.raises(InvalidMigrationError, match="cannot preserve set cardinality"):
        parent_family.dump(
            version="1",
            data=cast(Any, parent_model)(children=container),
        )


@pytest.mark.parametrize(
    ("extra_key", "message"),
    [
        ("child", "duplicate locations"),
        ("childWire", "overwrites the declared location"),
    ],
)
def test_serialized_nested_alias_collision_fails_closed(
    extra_key: str,
    message: str,
) -> None:
    class Child(BaseModel):
        value: int = 1

    child_family = SchemaFamily(
        model=Child,
        name="serialized_alias_collision_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    )
    child_document = child_family.model_for("1")

    class CurrentParent(BaseModel):
        child: Child

    class HistoricalParentBase(BaseModel):
        model_config = ConfigDict(extra="allow", validate_by_name=True)

        @model_validator(mode="after")
        def inject_colliding_extra(self) -> HistoricalParentBase:
            if self.__pydantic_extra__ is not None:
                self.__pydantic_extra__[extra_key] = {"fake": True}
            return self

    historical_parent = create_model(
        "HistoricalAliasCollisionParent",
        __base__=HistoricalParentBase,
        child=(child_document, Field(..., serialization_alias="childWire")),
    )
    parent_family = SchemaFamily(
        model=CurrentParent,
        name=f"serialized_alias_collision_parent_{extra_key}",
        versions=(
            SchemaVersion("1", wire_model=historical_parent),
            SchemaVersion("2"),
        ),
        nested=(NestedFamily("child", child_family, matching_labels()),),
    )

    with pytest.raises(ValueError, match=message):
        parent_family.dump(version="1", data=CurrentParent(child=Child()))


def test_automatic_nested_metadata_wrapper_rejects_siblings() -> None:
    class Config(BaseModel):
        model_config = ConfigDict(extra="allow")

        value: int = 1

    family = SchemaFamily(
        model=Config,
        name="automatic_reserved_metadata_root",
        versions=(SchemaVersion("1"),),
        version_metadata=VersionMetadata(("contract", "version"), owner="family"),
    )

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        family.model_for("1").model_validate(
            {"value": 2, "contract": {"version": "1", "tenant": "evil"}},
        )


def test_family_document_adapter_shallow_copy_preserves_metadata_identity() -> None:
    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        value: int = 1

    family = SchemaFamily(
        model=Current,
        name="adapter_metadata_copy_identity",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
        version_metadata=VersionMetadata(("contract", "version"), owner="family"),
    )
    document = cast(Any, family.model_for("1"))(contract={"version": "1"})

    assert copy(document).contract is document.contract
    assert document.model_copy().contract is document.contract

    copied_graph = deepcopy([document, document.contract])

    assert copied_graph[0].contract is copied_graph[1]


def test_nested_serialize_as_any_cannot_leak_subclass_fields() -> None:
    class Child(BaseModel):
        value: int = 1

    child_family = SchemaFamily(
        model=Child,
        name="serialize_as_any_nested_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        version_metadata=None,
    )

    class CurrentParent(BaseModel):
        children: list[Child]

    class HistoricalParent(BaseModel):
        children: list[SerializeAsAny[Child]]

    family = SchemaFamily(
        model=CurrentParent,
        name="serialize_as_any_nested_parent",
        versions=(
            SchemaVersion("1", wire_model=HistoricalParent),
            SchemaVersion("2"),
        ),
        nested=(NestedFamily("children", child_family, matching_labels()),),
    )

    with pytest.raises(UnsupportedWireModelError, match="annotation-level serializer"):
        family.compile()


def test_custom_annotation_schema_cannot_serialize_a_managed_path() -> None:
    calls: list[str] = []

    class EmptySerializer:
        def __get_pydantic_core_schema__(self, source: Any, handler: Any) -> Any:
            calls.append("schema")
            return core_schema.no_info_after_validator_function(
                lambda value: value,
                handler(source),
                serialization=core_schema.plain_serializer_function_ser_schema(
                    lambda _value: {},
                    return_schema=core_schema.dict_schema(),
                ),
            )

    class Child(BaseModel):
        value: int = 1

    child_family = SchemaFamily(
        model=Child,
        name="custom_schema_nested_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        version_metadata=None,
    )

    class CurrentParent(BaseModel):
        child: Child

    class HistoricalParent(BaseModel):
        child: Annotated[Child, EmptySerializer()]

    calls.clear()
    family = SchemaFamily(
        model=CurrentParent,
        name="custom_schema_nested_parent",
        versions=(
            SchemaVersion("1", wire_model=HistoricalParent),
            SchemaVersion("2"),
        ),
        nested=(NestedFamily("child", child_family, matching_labels()),),
    )

    with pytest.raises(UnsupportedWireModelError, match="annotation-level serializer"):
        family.compile()
    assert calls == []


def test_custom_model_schema_cannot_serialize_a_managed_path() -> None:
    calls: list[str] = []

    class Child(BaseModel):
        value: int = 1

    child_family = SchemaFamily(
        model=Child,
        name="custom_model_schema_nested_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        version_metadata=None,
    )

    class CurrentParent(BaseModel):
        child: Child

    class HistoricalParent(BaseModel):
        child: Child

        @classmethod
        def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> Any:
            calls.append("schema")
            return core_schema.no_info_after_validator_function(
                lambda value: value,
                handler(source),
                serialization=core_schema.plain_serializer_function_ser_schema(
                    lambda _value: {"child": {}},
                    return_schema=core_schema.dict_schema(),
                ),
            )

    calls.clear()
    family = SchemaFamily(
        model=CurrentParent,
        name="custom_model_schema_nested_parent",
        versions=(
            SchemaVersion("1", wire_model=HistoricalParent),
            SchemaVersion("2"),
        ),
        nested=(NestedFamily("child", child_family, matching_labels()),),
        version_metadata=None,
    )

    with pytest.raises(UnsupportedWireModelError, match="custom model hook"):
        family.compile()
    assert calls == []


def test_json_encoder_cannot_serialize_a_managed_path() -> None:
    class Child(BaseModel):
        value: int = 1

    child_family = SchemaFamily(
        model=Child,
        name="json_encoder_nested_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        version_metadata=None,
    )

    class CurrentParent(BaseModel):
        child: Child

    with pytest.warns(DeprecationWarning, match="json_encoders"):

        class HistoricalParent(BaseModel):
            model_config = ConfigDict(json_encoders={Child: lambda _value: {}})

            child: Child

    family = SchemaFamily(
        model=CurrentParent,
        name="json_encoder_nested_parent",
        versions=(
            SchemaVersion("1", wire_model=HistoricalParent),
            SchemaVersion("2"),
        ),
        nested=(NestedFamily("child", child_family, matching_labels()),),
        version_metadata=None,
    )

    with pytest.raises(UnsupportedWireModelError, match="json_encoders"):
        family.compile()


def test_attribute_metadata_rejects_nested_envelope_siblings() -> None:
    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        model_config = ConfigDict(from_attributes=True)

        value: int = 1

    class Source:
        value = 2
        contract = {"version": "1", "tenant": "unexpected"}

    family = SchemaFamily(
        model=Current,
        name="attribute_nested_metadata_envelope",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
        version_metadata=VersionMetadata(("contract", "version"), owner="family"),
    )

    with pytest.raises(ValidationError, match="reserves the entire root"):
        family.model_for("1").model_validate(Source(), from_attributes=True)


def test_invalid_non_attribute_input_does_not_probe_metadata_properties() -> None:
    reads: list[str] = []

    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        value: int = 1

    class Source:
        value = 2

        @property
        def schema_version(self) -> str:
            reads.append("schema_version")
            return "wrong"

    family = SchemaFamily(
        model=Current,
        name="metadata_attribute_probe_order",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
    )

    with pytest.raises(ValidationError, match="does not enable attribute validation"):
        family.model_for("1").model_validate(Source())
    assert reads == []


def test_wrong_attribute_metadata_is_rejected_before_body_validators() -> None:
    events: list[str] = []

    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        model_config = ConfigDict(from_attributes=True)

        value: int = 1

        @model_validator(mode="before")
        @classmethod
        def record_validation(cls, value: Any) -> Any:
            events.append("body-validator")
            return value

    class Source:
        value = 2
        schema_version = "wrong"

    family = SchemaFamily(
        model=Current,
        name="attribute_metadata_preflight_order",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
    )

    with pytest.raises(ValidationError, match="expected '1'"):
        family.model_for("1").model_validate(Source(), from_attributes=True)
    assert events == []


def test_plain_attribute_metadata_envelope_is_verified_exactly() -> None:
    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        model_config = ConfigDict(from_attributes=True)

        value: int = 1

    class Contract:
        def __init__(self) -> None:
            self.version = "1"

    class Source:
        value = 2
        contract = Contract()

    family = SchemaFamily(
        model=Current,
        name="plain_attribute_metadata_envelope",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
        version_metadata=VersionMetadata(("contract", "version"), owner="family"),
    )

    assert family.model_for("1").model_validate(Source()).model_dump() == {
        "value": 2,
        "contract": {"version": "1"},
    }

    class ContractModel(BaseModel):
        version: Literal["1"] = "1"

    class ModelSource:
        value = 3
        contract = ContractModel()

    assert family.model_for("1").model_validate(ModelSource()).model_dump() == {
        "value": 3,
        "contract": {"version": "1"},
    }

    class ContractWithExtra(BaseModel):
        model_config = ConfigDict(extra="allow")

        version: Literal["1"] = "1"

    class ExtraSource:
        value = 3
        contract = ContractWithExtra(tenant="unexpected")

    with pytest.raises(ValidationError, match="reserves the entire root"):
        family.model_for("1").model_validate(ExtraSource())

    class SlotsContract:
        __slots__ = ("version",)

        def __init__(self) -> None:
            self.version = "1"

    class SlotsSource:
        value = 4
        contract = SlotsContract()

    with pytest.raises(ValidationError, match="reserves the entire root"):
        family.model_for("1").model_validate(SlotsSource())


@pytest.mark.parametrize(
    ("contract", "message"),
    [
        (1, "non-object component"),
        ({"envelope": {"version": "1"}, "sibling": True}, "reserves the entire root"),
        ({"envelope": 1}, "non-object component"),
        ({"envelope": {"version": "wrong"}}, "expected '1'"),
    ],
)
def test_deep_family_metadata_envelope_fails_closed(
    contract: Any,
    message: str,
) -> None:
    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        value: int = 1

    family = SchemaFamily(
        model=Current,
        name=f"deep_metadata_envelope_{message}_{type(contract).__name__}",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
        version_metadata=VersionMetadata(
            ("contract", "envelope", "version"),
            owner="family",
        ),
    )

    with pytest.raises(ValidationError, match=message):
        family.model_for("1").model_validate({"value": 2, "contract": contract})


def test_family_document_adapter_rejects_private_api_collisions() -> None:
    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        value: int = 1
        _replace_document_adapter_state: str = PrivateAttr(default="collision")

    family = SchemaFamily(
        model=Current,
        name="adapter_private_api_collision",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
    )

    with pytest.raises(UnsupportedWireModelError, match="conflicts with.*adapter API"):
        family.compile()


def test_family_document_adapter_construct_removes_metadata_from_body_fields_set() -> None:
    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        value: int = 1

    family = SchemaFamily(
        model=Current,
        name="adapter_construct_fields_set",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
        version_metadata=VersionMetadata(("contract", "version"), owner="family"),
    )
    document = family.model_for("1").model_construct(
        _fields_set={"value", "contract"},
        value=2,
        contract={"version": "1"},
    )

    assert document.model_fields_set == {"value"}
    assert document.model_dump() == {"value": 2, "contract": {"version": "1"}}


def test_target_extras_cannot_overwrite_declared_serialization_aliases() -> None:
    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        model_config = ConfigDict(extra="allow")

        value: int = Field(default=1, serialization_alias="wireValue")

        @model_validator(mode="after")
        def inject_collision(self) -> Historical:
            assert self.__pydantic_extra__ is not None
            self.__pydantic_extra__["wireValue"] = "not-an-integer"
            return self

    family = SchemaFamily(
        model=Current,
        name="top_level_extra_output_collision",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
        version_metadata=None,
    )

    with pytest.raises(ValueError, match="extras overwrite declared serialization"):
        family.defaults_for(version="1")


def test_nested_target_extras_cannot_overwrite_declared_serialization_aliases() -> None:
    class HistoricalInner(BaseModel):
        model_config = ConfigDict(extra="allow")

        value: int = Field(default=1, serialization_alias="wireValue")

        @model_validator(mode="after")
        def inject_collision(self) -> HistoricalInner:
            assert self.__pydantic_extra__ is not None
            self.__pydantic_extra__["wireValue"] = "not-an-integer"
            return self

    class Current(BaseModel):
        inner: dict[str, Any] = Field(default_factory=dict)

    class Historical(BaseModel):
        inner: HistoricalInner = Field(default_factory=HistoricalInner)

    family = SchemaFamily(
        model=Current,
        name="recursive_extra_output_collision",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
        version_metadata=None,
    )

    with pytest.raises(ValueError, match="extras overwrite declared serialization"):
        family.defaults_for(version="1")


def test_typed_target_extras_recurse_into_nested_model_collisions() -> None:
    class Inner(BaseModel):
        model_config = ConfigDict(extra="allow")

        __pydantic_extra__: dict[str, Any] = Field(init=False)
        value: int = Field(default=1, serialization_alias="wireValue")

        @model_validator(mode="after")
        def add_collision(self) -> Inner:
            assert self.__pydantic_extra__ is not None
            self.__pydantic_extra__["wireValue"] = "CORRUPTED"
            return self

    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        model_config = ConfigDict(extra="allow")

        __pydantic_extra__: dict[str, Inner] = Field(init=False)
        value: int = 1

        @model_validator(mode="after")
        def add_inner(self) -> Historical:
            assert self.__pydantic_extra__ is not None
            self.__pydantic_extra__["holder"] = Inner()
            return self

    family = SchemaFamily(
        model=Current,
        name="typed_extra_nested_collision",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
        version_metadata=None,
    )

    with pytest.raises(ValueError, match="extras overwrite declared serialization.*wireValue"):
        family.defaults_for(version="1")


def test_target_extras_cannot_overwrite_computed_serialization_aliases() -> None:
    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        model_config = ConfigDict(extra="allow")

        value: int = 1

        @computed_field(alias="wireComputed")
        @property
        def computed(self) -> int:
            return self.value * 2

        @model_validator(mode="after")
        def inject_collision(self) -> Historical:
            assert self.__pydantic_extra__ is not None
            self.__pydantic_extra__["wireComputed"] = "not-an-integer"
            return self

    family = SchemaFamily(
        model=Current,
        name="computed_extra_output_collision",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
        version_metadata=None,
    )

    with pytest.raises(ValueError, match="extras overwrite declared serialization"):
        family.defaults_for(version="1")


def test_nested_fixed_tuple_shape_is_preserved_during_metadata_pruning() -> None:
    class Child(BaseModel):
        value: int = 1

    child_family = SchemaFamily(
        model=Child,
        name="fixed_tuple_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    )

    class Parent(BaseModel):
        children: tuple[Child, Child]

    family = SchemaFamily(
        model=Parent,
        name="fixed_tuple_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        nested=(NestedFamily("children", child_family, matching_labels()),),
    )

    assert family.dump(
        version="1",
        data=Parent(children=(Child(value=2), Child(value=3))),
    ) == {
        "children": [{"value": 2}, {"value": 3}],
        "schema_version": "1",
    }


def test_family_document_adapter_preserves_declared_values_when_extras_reuse_names() -> None:
    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        model_config = ConfigDict(extra="allow")

        value: int = 1

        @model_validator(mode="after")
        def inject_collision(self) -> Historical:
            assert self.__pydantic_extra__ is not None
            self.__pydantic_extra__["value"] = "extra"
            return self

    family = SchemaFamily(
        model=Current,
        name="adapter_declared_extra_collision",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
    )
    document = cast(Any, family.model_for("1"))(value=2, schema_version="1")

    assert document.value == 2
    assert document.model_extra == {"value": "extra"}


def test_family_document_adapter_preserves_internal_name_extras() -> None:
    internal_names = (
        "_FamilyDocumentAdapterBase__document_body",
        "_document_body_model",
        "_from_document_body",
        "_replace_document_adapter_state",
    )

    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        model_config = ConfigDict(extra="allow")

        value: int = 1

    family = SchemaFamily(
        model=Current,
        name="adapter_internal_name_extras",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
    )
    body = Historical.model_validate(
        {"value": 2, **{name: f"wire-{index}" for index, name in enumerate(internal_names)}},
    )
    document = cast(Any, family.model_for("1")).model_validate(body)

    for index, name in enumerate(internal_names):
        assert getattr(document, name) == f"wire-{index}"
    dumped = document.model_dump()
    assert all(dumped[name] == f"wire-{index}" for index, name in enumerate(internal_names))

    with pytest.raises(AttributeError, match="Internal document state"):
        setattr(document, internal_names[0], "replacement")
    with pytest.raises(AttributeError, match="Internal document state"):
        delattr(document, internal_names[0])

    object.__setattr__(document, internal_names[0], object())
    with pytest.raises(ValueError, match="lost its validated explicit wire body"):
        document.model_dump()


def test_family_document_adapter_snapshots_foreign_body_scalar_state() -> None:
    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        value: int = 1

    family = SchemaFamily(
        model=Current,
        name="adapter_foreign_body_snapshot",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
    )
    body = Historical(value=2)
    document = cast(Any, family.model_for("1")).model_validate(body)

    body.value = 9

    assert document.value == 2
    assert document.model_dump() == {"value": 2, "schema_version": "1"}


def test_family_document_adapter_does_not_replay_deleted_default_factories() -> None:
    calls: list[str] = []

    def factory() -> list[int]:
        calls.append("factory")
        return [1]

    class Current(BaseModel):
        items: list[int] = Field(default_factory=list)
        marker: int = 0

    class Historical(BaseModel):
        items: list[int] = Field(default_factory=factory)
        marker: int = 0

    family = SchemaFamily(
        model=Current,
        name="adapter_deleted_factory_state",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
    )
    document = cast(Any, family.model_for("1"))(schema_version="1")

    del document.items
    document.marker = 2
    document.model_dump(warnings=False)

    assert calls == ["factory"]


def test_family_document_adapter_refreshes_after_body_back_reference_mutation() -> None:
    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        value: int = 1
        _references: list[Any] = PrivateAttr(default_factory=list)

        @model_validator(mode="after")
        def retain_self(self) -> Historical:
            self._references = [self]
            return self

    family = SchemaFamily(
        model=Current,
        name="adapter_body_back_reference",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
    )
    document = cast(Any, family.model_for("1"))(value=2, schema_version="1")

    references = document._references
    references[0].value = 7

    assert document.value == 7
    assert dict(document)["value"] == 7
    assert "value=7" in repr(document)
    assert document == document.model_copy()
    assert family.validate(document, version="1").current_model.value == 7
