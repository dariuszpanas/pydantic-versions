from __future__ import annotations

from typing import Any, Self, cast

import pytest
from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    ConfigDict,
    Field,
    ModelWrapValidatorHandler,
    PrivateAttr,
    ValidationError,
    create_model,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from pydantic_versions import (
    SchemaFamily,
    SchemaVersion,
    SchemaVersionError,
    UnsupportedWireModelError,
    VersionMetadata,
    VersionTransition,
    dump_versioned,
    model_for_version,
    versioned_schema,
)


def test_mapping_render_validates_and_detaches_before_downgrade() -> None:
    seen_values: list[int] = []

    class NestedPayload(BaseModel):
        values: list[int]

    class CurrentPayload(BaseModel):
        value: int
        nested: NestedPayload

        @field_validator("value", mode="before")
        @classmethod
        def normalize_value(cls, value: Any) -> Any:
            return int(value) + 1

    def downgrade(data: dict[str, Any]) -> dict[str, Any]:
        seen_values.append(data["value"])
        data["nested"]["values"].append(9)
        return data

    family = SchemaFamily(
        model=CurrentPayload,
        name="validated_render_input",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                downgrade=downgrade,
                downgrade_semantics="exact",
            ),
        ),
    )
    mapping_input = {"value": 1, "nested": {"values": [1]}}
    model_input = CurrentPayload.model_validate(mapping_input)

    from_mapping = family.dump(
        version="1",
        data=mapping_input,
        include_version=False,
    )
    from_model = family.dump(
        version="1",
        data=model_input,
        include_version=False,
    )

    expected = {"value": 2, "nested": {"values": [1, 9]}}
    assert from_mapping == expected
    assert from_model == expected
    assert seen_values == [2, 2]
    assert mapping_input == {"value": 1, "nested": {"values": [1]}}
    assert model_input.nested.values == [1]


def test_mapping_render_preserves_authoritative_alias_input() -> None:
    seen_inputs: list[Any] = []

    class AliasPayload(BaseModel):
        value: int = Field(
            validation_alias=AliasChoices(
                "legacy_value",
                AliasPath("payload", "value"),
            ),
        )

        @model_validator(mode="before")
        @classmethod
        def capture_input(cls, data: Any) -> Any:
            seen_inputs.append(data)
            return data

    family = SchemaFamily(
        model=AliasPayload,
        name="current_alias_render",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )

    assert family.dump(
        version="1",
        data={
            "value": "3",
            "legacy_value": "1",
            "payload": {"value": "2"},
        },
    ) == {"value": 1}
    assert family.dump(
        version="1",
        data={"payload": {"value": "4"}},
    ) == {"value": 4}
    assert seen_inputs == [
        {
            "value": "3",
            "legacy_value": "1",
            "payload": {"value": "2"},
        },
        {"payload": {"value": "4"}},
    ]


def test_render_canonical_payload_excludes_allowed_current_extras() -> None:
    seen_payloads: list[dict[str, Any]] = []

    class ExtensiblePayload(BaseModel):
        model_config = ConfigDict(extra="allow")

        value: int

    def downgrade(data: dict[str, Any]) -> dict[str, Any]:
        seen_payloads.append(dict(data))
        return data

    family = SchemaFamily(
        model=ExtensiblePayload,
        name="render_extra_boundary",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                downgrade=downgrade,
                downgrade_semantics="exact",
            ),
        ),
        version_metadata=None,
    )
    mapping_input = {"value": 1, "extension": "source-only"}
    model_input = ExtensiblePayload.model_validate(mapping_input)

    assert family.dump(version="1", data=mapping_input) == {"value": 1}
    assert family.dump(version="1", data=model_input) == {"value": 1}
    assert seen_payloads == [{"value": 1}, {"value": 1}]
    assert model_input.__pydantic_extra__ == {"extension": "source-only"}


def test_family_owned_render_metadata_must_describe_current_input() -> None:
    class MetadataPayload(BaseModel):
        value: int

    family = SchemaFamily(
        model=MetadataPayload,
        name="family_metadata_render",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        version_metadata=VersionMetadata(("meta", "version"), owner="family"),
    )
    current_input = {"meta": {"version": "2"}, "value": "5"}

    assert family.dump(version="1", data=current_input) == {
        "value": 5,
        "meta": {"version": "1"},
    }
    assert current_input == {"meta": {"version": "2"}, "value": "5"}

    with pytest.raises(SchemaVersionError, match="current-model input"):
        family.dump(
            version="1",
            data={"meta": {"version": "1"}, "value": 5},
        )


def test_model_owned_render_metadata_is_rebased_without_alias_leaks() -> None:
    class ModelMetadataPayload(BaseModel):
        schema_version: str = Field(default="2", validation_alias="wire_version")
        value: int

    family = SchemaFamily(
        model=ModelMetadataPayload,
        name="model_metadata_render",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        version_metadata=VersionMetadata("wire_version", owner="model"),
    )

    assert family.dump(
        version="1",
        data={"wire_version": "2", "value": "7"},
    ) == {"schema_version": "1", "value": 7}
    with pytest.raises(ValueError, match="model-owned.*include_version=False is unavailable"):
        family.dump(
            version="1",
            data={"schema_version": "2", "value": 7},
            include_version=False,
        )

    with pytest.raises(SchemaVersionError, match="current-model input"):
        family.dump(
            version="1",
            data={"wire_version": "1", "value": 7},
        )
    with pytest.raises(SchemaVersionError, match="current-model input"):
        family.dump(
            version="1",
            data={"schema_version": "1", "value": 7},
        )


def test_model_owned_metadata_rejects_conflicts_at_any_accepted_name() -> None:
    class CanonicalMetadataPayload(BaseModel):
        model_config = ConfigDict(validate_by_alias=True, validate_by_name=True)

        schema_version: str = Field(default="2", validation_alias="wire_version")
        value: int

    family = SchemaFamily(
        model=CanonicalMetadataPayload,
        name="canonical_model_metadata_render",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        version_metadata=VersionMetadata("schema_version", owner="model"),
    )

    assert family.dump(
        version="1",
        data={"wire_version": "2", "value": 7},
    ) == {"schema_version": "1", "value": 7}
    with pytest.raises(SchemaVersionError, match="current-model input"):
        family.dump(
            version="1",
            data={"wire_version": "1", "value": 7},
        )


def test_unrelated_models_are_structurally_validated_as_current_input() -> None:
    class CurrentPayload(BaseModel):
        value: int

    class CompatiblePayload(BaseModel):
        value: str

    class IncompatiblePayload(BaseModel):
        other: str

    family = SchemaFamily(
        model=CurrentPayload,
        name="unrelated_model_render",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )

    assert family.dump(version="1", data=cast(Any, CompatiblePayload(value="8"))) == {
        "value": 8,
    }
    with pytest.raises(ValidationError, match="CurrentPayload"):
        family.dump(
            version="1",
            data=cast(Any, IncompatiblePayload(other="missing")),
        )
    with pytest.raises(TypeError, match="current model instance or mapping"):
        family.dump(version="1", data=cast(Any, ["not", "render", "data"]))


def test_generated_current_wire_does_not_bypass_unrelated_current_errors() -> None:
    @versioned_schema(
        name="rejecting_set_child",
        versions=("1",),
        current="1",
    )
    class RejectingChild(BaseModel):
        value: int

        @model_validator(mode="after")
        def reject_model(self) -> Self:
            raise PydanticCustomError(
                "set_item_not_hashable",
                "Set items should be hashable",
            )

    @versioned_schema(
        name="rejecting_set_parent",
        versions=("1",),
        current="1",
    )
    class RejectingParent(BaseModel):
        children: set[RejectingChild]

    generated_current = model_for_version(RejectingParent, "1").model_validate(
        {"children": [{"value": 1}]},
    )

    with pytest.raises(ValidationError, match="Set items should be hashable"):
        dump_versioned(
            RejectingParent,
            version="1",
            data=cast(Any, generated_current),
        )


def test_generated_set_projection_runs_mutating_after_validators_once() -> None:
    events: list[str] = []

    @versioned_schema(
        name="mutating_after_set_child",
        versions=("1",),
        current="1",
    )
    class MutatingChild(BaseModel):
        model_config = ConfigDict(revalidate_instances="always")

        value: int

        @model_validator(mode="after")
        def normalize_value(self) -> Self:
            events.append("child after")
            self.value += 1
            return self

    @versioned_schema(
        name="mutating_after_set_parent",
        versions=("1",),
        current="1",
    )
    class MutatingParent(BaseModel):
        children: set[MutatingChild]
        parent_runs: int = 0

        @model_validator(mode="after")
        def record_parent_validation(self) -> Self:
            events.append("parent after")
            self.parent_runs += 1
            return self

    generated_current = model_for_version(MutatingParent, "1").model_validate(
        {"children": [{"value": 1}]},
    )

    rendered = dump_versioned(
        MutatingParent,
        version="1",
        data=cast(Any, generated_current),
    )

    assert rendered == {
        "children": [{"value": 2}],
        "parent_runs": 1,
        "schema_version": "1",
    }
    assert events == ["child after", "parent after"]


def test_nested_frozenset_projection_runs_model_post_init_once() -> None:
    events: list[str] = []

    @versioned_schema(
        name="post_init_frozenset_child",
        versions=("1",),
        current="1",
    )
    class PostInitChild(BaseModel):
        model_config = ConfigDict(revalidate_instances="always")

        value: int

        def model_post_init(self, _context: Any) -> None:
            events.append("child post init")
            self.value += 10

    @versioned_schema(
        name="post_init_frozenset_parent",
        versions=("1",),
        current="1",
    )
    class PostInitParent(BaseModel):
        groups: dict[str, list[frozenset[PostInitChild]]]
        parent_runs: int = 0

        @model_validator(mode="after")
        def record_parent_validation(self) -> Self:
            events.append("parent after")
            self.parent_runs += 1
            return self

    generated_current = model_for_version(PostInitParent, "1").model_validate(
        {
            "groups": {
                "primary": [
                    [{"value": 1}],
                ],
            },
        },
    )

    rendered = dump_versioned(
        PostInitParent,
        version="1",
        data=cast(Any, generated_current),
    )

    assert rendered == {
        "groups": {
            "primary": [
                [{"value": 11}],
            ],
        },
        "parent_runs": 1,
        "schema_version": "1",
    }
    assert events == ["child post init", "parent after"]


def test_generated_set_projection_runs_parent_hooks_on_authoritative_type_once() -> None:
    events: list[str] = []

    @versioned_schema(
        name="authoritative_type_set_child",
        versions=("1",),
        current="1",
    )
    class HashableChild(BaseModel):
        value: int

        def __hash__(self) -> int:
            return hash(self.value)

    @versioned_schema(
        name="authoritative_type_set_parent",
        versions=("1",),
        current="1",
    )
    class ExactParent(BaseModel):
        children: set[HashableChild]

        @model_validator(mode="before")
        @classmethod
        def record_before(cls, value: Any) -> Any:
            assert cls is ExactParent
            events.append("parent before")
            return value

        @model_validator(mode="wrap")
        @classmethod
        def record_wrap(
            cls,
            value: Any,
            handler: ModelWrapValidatorHandler[Self],
        ) -> Self:
            assert cls is ExactParent
            events.append("parent wrap before")
            result = handler(value)
            assert type(result) is ExactParent
            events.append("parent wrap after")
            return result

        @field_validator("children", mode="after")
        @classmethod
        def record_field(cls, value: set[HashableChild]) -> set[HashableChild]:
            assert cls is ExactParent
            events.append("parent field")
            return value

        def model_post_init(self, _context: Any) -> None:
            assert type(self) is ExactParent
            events.append("parent post init")

        @model_validator(mode="after")
        def record_after(self) -> Self:
            assert type(self) is ExactParent
            events.append("parent after")
            return self

    expected = {
        "children": [{"value": 1}],
        "schema_version": "1",
    }
    assert (
        dump_versioned(
            ExactParent,
            version="1",
            data={"children": [{"value": 1}]},
        )
        == expected
    )
    assert events == [
        "parent wrap before",
        "parent before",
        "parent field",
        "parent post init",
        "parent wrap after",
        "parent after",
    ]

    events.clear()
    generated_current = model_for_version(ExactParent, "1").model_validate(
        {"children": [{"value": 1}]},
    )
    assert (
        dump_versioned(
            ExactParent,
            version="1",
            data=cast(Any, generated_current),
        )
        == expected
    )
    assert events == [
        "parent wrap before",
        "parent before",
        "parent field",
        "parent post init",
        "parent wrap after",
        "parent after",
    ]


def test_generated_set_carrier_preserves_exact_init_and_private_lifecycle() -> None:
    events: list[Any] = []

    def private_state() -> list[str]:
        events.append("parent private")
        return ["ready"]

    @versioned_schema(
        name="carrier_lifecycle_set_child",
        versions=("1",),
        current="1",
    )
    class CarrierChild(BaseModel):
        value: int

        def __init__(self, **data: Any) -> None:
            events.append(("child init", type(self)))
            super().__init__(**data)

    @versioned_schema(
        name="carrier_lifecycle_set_parent",
        versions=("1",),
        current="1",
    )
    class CarrierParent(BaseModel):
        children: set[CarrierChild]
        _state: list[str] = PrivateAttr(default_factory=private_state)

        def model_post_init(self, _context: Any) -> None:
            assert type(self) is CarrierParent
            assert self._state == ["ready"]
            events.append(("parent post init", type(self)))

    assert CarrierChild.__hash__ is None
    generated_current = model_for_version(CarrierParent, "1").model_validate(
        {"children": [{"value": 1}]},
    )
    assert events == []

    assert dump_versioned(
        CarrierParent,
        version="1",
        data=cast(Any, generated_current),
    ) == {
        "children": [{"value": 1}],
        "schema_version": "1",
    }
    assert events == [
        ("child init", CarrierChild),
        "parent private",
        ("parent post init", CarrierParent),
    ]


def test_nested_carrier_hooks_use_authoritative_wrapper_types_once() -> None:
    events: list[str] = []

    @versioned_schema(
        name="authoritative_type_nested_leaf",
        versions=("1",),
        current="1",
    )
    class NestedLeaf(BaseModel):
        model_config = ConfigDict(revalidate_instances="always")

        value: int

        def model_post_init(self, _context: Any) -> None:
            assert type(self) is NestedLeaf
            events.append("leaf post init")

        @model_validator(mode="after")
        def normalize_value(self) -> Self:
            assert type(self) is NestedLeaf
            events.append("leaf after")
            self.value += 1
            return self

    @versioned_schema(
        name="authoritative_type_nested_wrapper",
        versions=("1",),
        current="1",
    )
    class NestedWrapper(BaseModel):
        model_config = ConfigDict(revalidate_instances="always")

        leaves: frozenset[NestedLeaf]
        wrapper_runs: int = 0

        def model_post_init(self, _context: Any) -> None:
            assert type(self) is NestedWrapper
            events.append("wrapper post init")

        @model_validator(mode="after")
        def record_validation(self) -> Self:
            assert type(self) is NestedWrapper
            events.append("wrapper after")
            self.wrapper_runs += 1
            return self

    @versioned_schema(
        name="authoritative_type_nested_parent",
        versions=("1",),
        current="1",
    )
    class NestedParent(BaseModel):
        wrapper: NestedWrapper
        parent_runs: int = 0

        def model_post_init(self, _context: Any) -> None:
            assert type(self) is NestedParent
            events.append("parent post init")

        @model_validator(mode="after")
        def record_validation(self) -> Self:
            assert type(self) is NestedParent
            events.append("parent after")
            self.parent_runs += 1
            return self

    generated_current = model_for_version(NestedParent, "1").model_validate(
        {"wrapper": {"leaves": [{"value": 1}]}},
    )

    assert dump_versioned(
        NestedParent,
        version="1",
        data=cast(Any, generated_current),
    ) == {
        "wrapper": {
            "leaves": [{"value": 2}],
            "wrapper_runs": 1,
        },
        "parent_runs": 1,
        "schema_version": "1",
    }
    assert events == [
        "leaf post init",
        "leaf after",
        "wrapper post init",
        "wrapper after",
        "parent post init",
        "parent after",
    ]


def test_generated_set_projection_rejects_parent_custom_init_before_execution() -> None:
    init_types: list[type[BaseModel]] = []

    @versioned_schema(
        name="custom_init_set_child",
        versions=("1",),
        current="1",
    )
    class CustomInitChild(BaseModel):
        value: int

    @versioned_schema(
        name="custom_init_set_parent",
        versions=("1",),
        current="1",
    )
    class CustomInitParent(BaseModel):
        children: set[CustomInitChild]

        def __init__(self, **data: Any) -> None:
            init_types.append(type(self))
            super().__init__(**data)

    generated_current = model_for_version(CustomInitParent, "1").model_validate(
        {"children": [{"value": 1}]},
    )

    with pytest.raises(
        UnsupportedWireModelError,
        match="custom __init__.*CustomInitParent",
    ):
        dump_versioned(
            CustomInitParent,
            version="1",
            data=cast(Any, generated_current),
        )
    assert init_types == []


def test_current_model_instances_follow_configured_revalidation_policy() -> None:
    class RevalidatedPayload(BaseModel):
        model_config = ConfigDict(revalidate_instances="always")

        value: int = Field(gt=0)

    family = SchemaFamily(
        model=RevalidatedPayload,
        name="revalidated_current_instance",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )
    current = RevalidatedPayload(value=1)
    current.value = -1

    with pytest.raises(ValidationError, match="greater than 0"):
        family.dump(version="1", data=current)


def test_current_model_subclass_fields_stay_outside_canonical_payload() -> None:
    seen_payloads: list[dict[str, Any]] = []

    class CurrentPayload(BaseModel):
        value: int

    class ExtendedPayload(CurrentPayload):
        extension: str

    def downgrade(data: dict[str, Any]) -> dict[str, Any]:
        seen_payloads.append(dict(data))
        return data

    family = SchemaFamily(
        model=CurrentPayload,
        name="current_subclass_boundary",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                downgrade=downgrade,
                downgrade_semantics="exact",
            ),
        ),
        version_metadata=None,
    )

    assert family.dump(
        version="1",
        data=ExtendedPayload(value=1, extension="source-only"),
    ) == {"value": 1}
    assert seen_payloads == [{"value": 1}]


def test_declared_model_config_controls_top_level_canonical_scalars() -> None:
    seen_payloads: list[dict[str, Any]] = []

    class CurrentPayload(BaseModel):
        model_config = ConfigDict(ser_json_bytes="utf8")

        value: bytes

    class ExtendedPayload(CurrentPayload):
        model_config = ConfigDict(ser_json_bytes="base64")

    def downgrade(data: dict[str, Any]) -> dict[str, Any]:
        seen_payloads.append(dict(data))
        return data

    family = SchemaFamily(
        model=CurrentPayload,
        name="declared_config_top_level",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                downgrade=downgrade,
                downgrade_semantics="exact",
            ),
        ),
        version_metadata=None,
    )

    assert family.dump(version="1", data={"value": b"abc"}) == {"value": "abc"}
    assert family.dump(version="1", data=ExtendedPayload(value=b"abc")) == {
        "value": "abc",
    }
    assert seen_payloads == [{"value": "abc"}, {"value": "abc"}]


def test_nested_subclass_fields_stay_outside_canonical_payload() -> None:
    seen_payloads: list[dict[str, Any]] = []

    class ChildPayload(BaseModel):
        value: int

    class ExtendedChild(ChildPayload):
        secret: str

    class ParentPayload(BaseModel):
        direct: ChildPayload
        items: list[ChildPayload]
        grouped: dict[str, ChildPayload]

    def downgrade(data: dict[str, Any]) -> dict[str, Any]:
        seen_payloads.append(data)
        return data

    family = SchemaFamily(
        model=ParentPayload,
        name="nested_subclass_boundary",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                downgrade=downgrade,
                downgrade_semantics="exact",
            ),
        ),
        version_metadata=None,
    )
    child = ExtendedChild(value=1, secret="never-cross-the-boundary")

    rendered = family.dump(
        version="1",
        data=ParentPayload(direct=child, items=[child], grouped={"key": child}),
    )

    assert rendered == {
        "direct": {"value": 1},
        "items": [{"value": 1}],
        "grouped": {"key": {"value": 1}},
    }
    assert seen_payloads == [rendered]


def test_declared_model_config_controls_nested_canonical_scalars() -> None:
    seen_payloads: list[dict[str, Any]] = []

    class ChildPayload(BaseModel):
        model_config = ConfigDict(ser_json_bytes="utf8")

        value: bytes

    class ExtendedChild(ChildPayload):
        model_config = ConfigDict(ser_json_bytes="base64")

    class ParentPayload(BaseModel):
        child: ChildPayload
        items: list[ChildPayload]

    def downgrade(data: dict[str, Any]) -> dict[str, Any]:
        seen_payloads.append(data)
        return data

    family = SchemaFamily(
        model=ParentPayload,
        name="declared_config_nested",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                downgrade=downgrade,
                downgrade_semantics="exact",
            ),
        ),
        version_metadata=None,
    )
    child = ExtendedChild(value=b"abc")

    expected = {
        "child": {"value": "abc"},
        "items": [{"value": "abc"}],
    }
    assert (
        family.dump(
            version="1",
            data={
                "child": {"value": b"abc"},
                "items": [{"value": b"abc"}],
            },
        )
        == expected
    )
    assert (
        family.dump(
            version="1",
            data=ParentPayload(child=child, items=[child]),
        )
        == expected
    )
    assert seen_payloads == [expected, expected]


@pytest.mark.parametrize("subclass_first", [False, True])
def test_declared_union_keeps_fields_from_the_selected_subclass(
    subclass_first: bool,
) -> None:
    seen_payloads: list[dict[str, Any]] = []

    class BaseChild(BaseModel):
        value: int

    class DeclaredChild(BaseChild):
        declared_detail: str

    union = DeclaredChild | BaseChild if subclass_first else BaseChild | DeclaredChild

    parent_model = create_model(
        f"DeclaredUnionParent_{subclass_first}",
        child=(union, ...),
    )

    def downgrade(data: dict[str, Any]) -> dict[str, Any]:
        seen_payloads.append(data)
        return data

    family = SchemaFamily(
        model=parent_model,
        name=f"declared_union_boundary_{subclass_first}",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                downgrade=downgrade,
                downgrade_semantics="exact",
            ),
        ),
        version_metadata=None,
    )

    family.dump(
        version="1",
        data=parent_model.model_validate(
            {"child": DeclaredChild(value=1, declared_detail="kept")},
        ),
    )

    assert seen_payloads == [
        {"child": {"value": 1, "declared_detail": "kept"}},
    ]


def test_any_union_member_does_not_hide_the_declared_model_boundary() -> None:
    seen_payloads: list[dict[str, Any]] = []

    class BaseChild(BaseModel):
        value: int

    class ExtendedChild(BaseChild):
        secret: str

    class ParentPayload(BaseModel):
        child: Any | BaseChild

    def downgrade(data: dict[str, Any]) -> dict[str, Any]:
        seen_payloads.append(data)
        return data

    family = SchemaFamily(
        model=ParentPayload,
        name="any_union_declared_boundary",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                downgrade=downgrade,
                downgrade_semantics="exact",
            ),
        ),
        version_metadata=None,
    )

    rendered = family.dump(
        version="1",
        data=ParentPayload(child=ExtendedChild(value=1, secret="excluded")),
    )

    assert rendered == {"child": {"value": 1}}
    assert seen_payloads == [rendered]


@pytest.mark.parametrize("field_kind", ["exclude", "exclude_if"])
def test_excluded_current_fields_do_not_cross_the_transition_boundary(
    field_kind: str,
) -> None:
    seen_payloads: list[dict[str, Any]] = []
    excluded = (
        Field(default="private", exclude=True)
        if field_kind == "exclude"
        else Field(default="private", exclude_if=lambda value: value == "private")
    )

    class CurrentPayload(BaseModel):
        public: str
        internal: str = excluded

    def downgrade(data: dict[str, Any]) -> dict[str, Any]:
        seen_payloads.append(data)
        return data

    family = SchemaFamily(
        model=CurrentPayload,
        name=f"excluded_render_boundary_{field_kind}",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                downgrade=downgrade,
                downgrade_semantics="exact",
            ),
        ),
        version_metadata=None,
    )

    assert family.dump(
        version="1",
        data=CurrentPayload(public="visible", internal="private"),
    ) == {"public": "visible"}
    assert seen_payloads == [{"public": "visible"}]


@pytest.mark.parametrize("field_kind", ["exclude", "exclude_if"])
def test_generated_current_wire_extras_cannot_restore_excluded_fields(
    field_kind: str,
) -> None:
    seen_payloads: list[dict[str, Any]] = []
    excluded = (
        Field(default="private", exclude=True)
        if field_kind == "exclude"
        else Field(default="private", exclude_if=lambda value: value == "private")
    )

    class CurrentPayload(BaseModel):
        model_config = ConfigDict(extra="allow")

        public: str
        internal: str = excluded

    def downgrade(data: dict[str, Any]) -> dict[str, Any]:
        seen_payloads.append(data)
        return data

    family = SchemaFamily(
        model=CurrentPayload,
        name=f"excluded_current_wire_extra_{field_kind}",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                downgrade=downgrade,
                downgrade_semantics="exact",
            ),
        ),
        version_metadata=None,
    )
    current_wire = family.model_for("2").model_validate(
        {"public": "visible", "internal": "injected"},
    )

    assert current_wire.__pydantic_extra__ == {"internal": "injected"}
    assert family.dump(version="1", data=cast(Any, current_wire)) == {
        "public": "visible",
    }
    assert seen_payloads == [{"public": "visible"}]


def test_mapping_render_honors_disabled_name_validation() -> None:
    class AliasOnlyPayload(BaseModel):
        model_config = ConfigDict(validate_by_alias=True, validate_by_name=False)

        value: int = Field(alias="wire")

    family = SchemaFamily(
        model=AliasOnlyPayload,
        name="mapping_alias_policy",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )

    with pytest.raises(ValidationError, match="wire"):
        family.dump(version="1", data={"value": 1})
    assert family.dump(version="1", data={"wire": 1}) == {"wire": 1}
    assert family.dump(version="1", data={"wire": 1}, by_alias=False) == {"value": 1}


def test_render_metadata_checks_list_alias_paths() -> None:
    class MetadataPayload(BaseModel):
        model_config = ConfigDict(validate_by_alias=True, validate_by_name=True)

        schema_version: str = Field(
            default="2",
            validation_alias=AliasPath("meta", 0, "version"),
        )
        value: int

    family = SchemaFamily(
        model=MetadataPayload,
        name="list_alias_metadata_render",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        version_metadata=VersionMetadata("schema_version", owner="model"),
    )

    with pytest.raises(SchemaVersionError, match="current-model input"):
        family.dump(
            version="1",
            data={"meta": [{"version": "1"}], "value": 7},
        )
    assert family.dump(
        version="1",
        data={"meta": [{"version": "2"}], "value": 7},
    ) == {"schema_version": "1", "value": 7}


def test_model_instance_family_metadata_extras_cannot_bypass_conflict_check() -> None:
    class ExtensiblePayload(BaseModel):
        model_config = ConfigDict(extra="allow")

        value: int

    family = SchemaFamily(
        model=ExtensiblePayload,
        name="model_extra_metadata_render",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    )
    stale = ExtensiblePayload.model_validate({"schema_version": "1", "value": 7})

    with pytest.raises(SchemaVersionError, match="current-model input"):
        family.dump(version="1", data=stale)


def test_validated_model_metadata_must_still_name_the_current_version() -> None:
    class DefaultedMetadataPayload(BaseModel):
        schema_version: str = "1"
        value: int

    family = SchemaFamily(
        model=DefaultedMetadataPayload,
        name="validated_metadata_render",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        version_metadata=VersionMetadata("schema_version", owner="model"),
    )

    with pytest.raises(SchemaVersionError, match="current-model input"):
        family.dump(version="1", data={"value": 7})


def test_decorator_discovered_current_wire_set_projection_is_renderable() -> None:
    @versioned_schema(
        name="decorator_set_child",
        versions=("1", "2"),
        current="2",
    )
    class DecoratorChild(BaseModel):
        value: int

    @versioned_schema(
        name="decorator_set_parent",
        versions=("1", "2"),
        current="2",
    )
    class DecoratorParent(BaseModel):
        children: set[DecoratorChild]

    current_wire = model_for_version(DecoratorParent, "2").model_validate(
        {"children": [{"value": 1}]},
    )

    rendered = dump_versioned(
        DecoratorParent,
        version="1",
        data=cast(Any, current_wire),
    )

    assert rendered["schema_version"] == "1"
    assert len(rendered["children"]) == 1
    assert {item["value"] for item in rendered["children"]} == {1}
    assert all("schema_version" not in item for item in rendered["children"])


def test_decorator_set_projection_beneath_mapping_is_renderable() -> None:
    @versioned_schema(
        name="mapped_set_leaf",
        versions=("1", "2"),
        current="2",
    )
    class MappedLeaf(BaseModel):
        value: int

    @versioned_schema(
        name="mapped_set_container",
        versions=("1", "2"),
        current="2",
    )
    class MappedContainer(BaseModel):
        leaves: set[MappedLeaf]

    @versioned_schema(
        name="mapped_set_parent",
        versions=("1", "2"),
        current="2",
    )
    class MappedParent(BaseModel):
        groups: dict[str, MappedContainer]

    current_wire = model_for_version(MappedParent, "2").model_validate(
        {
            "groups": {
                "primary": {
                    "leaves": [{"value": 1}],
                },
            },
        },
    )

    rendered = dump_versioned(
        MappedParent,
        version="1",
        data=cast(Any, current_wire),
    )

    assert rendered["schema_version"] == "1"
    primary = rendered["groups"]["primary"]
    assert primary == {"leaves": [{"value": 1}]}
