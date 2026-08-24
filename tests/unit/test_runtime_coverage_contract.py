from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Annotated, Any, Literal, cast

import pytest
from pydantic import (
    AliasPath,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    create_model,
    model_serializer,
)

from pydantic_versions import (
    InvalidMigrationError,
    NestedFamily,
    SchemaFamily,
    SchemaVersion,
    VersionMetadata,
    VersionTransition,
    dump_versioned,
    field_removed,
    field_renamed,
    matching_labels,
    model_for_version,
    schema_version,
    validate_versioned,
    versioned_schema,
)


def test_mapping_render_copies_builtin_containers_and_preserves_canonical_scalars() -> None:
    class OpaqueScalar:
        def __init__(self, value: int) -> None:
            self.value = value

    class Payload(BaseModel):
        model_config = ConfigDict(ser_json_temporal="seconds")

        tuple_values: tuple[int, ...]
        set_values: set[int]
        frozen_values: frozenset[int]
        opaque: Any
        opaque_keys: dict[Any, int]

    family = SchemaFamily(
        model=Payload,
        name="runtime_canonical_containers",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )
    opaque = OpaqueScalar(7)
    source = {
        "tuple_values": (1, 2),
        "set_values": {3, 4},
        "frozen_values": frozenset({5, 6}),
        "opaque": opaque,
        "opaque_keys": {OpaqueScalar(8): 9},
    }

    rendered = family.dump(
        version="1",
        data=source,
        fallback=lambda value: f"opaque:{value.value}",
    )

    assert rendered["tuple_values"] == [1, 2]
    assert set(rendered["set_values"]) == {3, 4}
    assert set(rendered["frozen_values"]) == {5, 6}
    assert rendered["opaque"] == "opaque:7"
    assert rendered["opaque_keys"] == {"opaque:8": 9}
    assert source["tuple_values"] == (1, 2)
    assert source["set_values"] == {3, 4}
    assert source["frozen_values"] == frozenset({5, 6})
    assert source["opaque"] is opaque
    assert next(iter(source["opaque_keys"])).value == 8

    seen_elapsed: list[Any] = []

    class HistoricalDuration(BaseModel):
        model_config = ConfigDict(ser_json_temporal="seconds")

        elapsed: dt.timedelta

    class CurrentDuration(BaseModel):
        elapsed: Any

    def capture_duration(data: dict[str, Any]) -> dict[str, Any]:
        seen_elapsed.append(data["elapsed"])
        return data

    temporal_family = SchemaFamily(
        model=CurrentDuration,
        name="runtime_temporal_scalar",
        versions=(
            SchemaVersion("1", wire_model=HistoricalDuration),
            SchemaVersion("2"),
        ),
        transitions=(VersionTransition("1", "2", upgrade=capture_duration),),
        version_metadata=None,
    )

    temporal_family.validate(
        {"elapsed": dt.timedelta(seconds=2.5)},
        version="1",
    )
    assert seen_elapsed == [dt.timedelta(seconds=2.5)]


def test_historical_removed_nested_routes_are_conditionally_absent() -> None:
    @versioned_schema(name="runtime_removed_decorator_child", versions=("1", "2"), current="2")
    class DecoratorChild(BaseModel):
        value: int = 3

    @versioned_schema(name="runtime_removed_decorator_parent", versions=("1", "2"), current="2")
    @schema_version("1", patches=(field_removed("child"),))
    class DecoratorParent(BaseModel):
        child: DecoratorChild = Field(default_factory=DecoratorChild)

    assert dump_versioned(
        DecoratorParent,
        version="1",
        data=DecoratorParent(),
    ) == {"schema_version": "1"}
    assert (
        validate_versioned(
            DecoratorParent,
            {"schema_version": "1"},
        ).current_model
        == DecoratorParent()
    )


def test_optional_annotated_nested_path_prunes_none_and_present_values() -> None:
    class Child(BaseModel):
        value: int

    child_family = SchemaFamily(
        model=Child,
        name="runtime_optional_path_child",
        versions=(SchemaVersion("1"),),
    )

    class Wrapper(BaseModel):
        child: Child

    class Parent(BaseModel):
        wrapper: Annotated[Wrapper | None, Field(description="optional wrapper")] = None

    parent_family = SchemaFamily(
        model=Parent,
        name="runtime_optional_path_parent",
        versions=(SchemaVersion("1"),),
        nested=(NestedFamily(("wrapper", "child"), child_family, matching_labels()),),
        version_metadata=None,
    )

    assert parent_family.dump(version="1", data={"wrapper": None}) == {"wrapper": None}
    assert parent_family.dump(
        version="1",
        data={"wrapper": {"child": {"value": 8}}},
    ) == {"wrapper": {"child": {"value": 8}}}


def test_parent_callback_can_supply_models_and_builtin_nested_containers() -> None:
    class Child(BaseModel):
        model_config = ConfigDict(frozen=True)

        value: int

    child_family = SchemaFamily(
        model=Child,
        name="runtime_callback_container_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        version_metadata=None,
    )

    class Parent(BaseModel):
        direct: Child
        tupled: tuple[Child, ...]
        set_values: set[Child]
        frozen_values: frozenset[Child]

    def supply_typed_values(data: dict[str, Any]) -> dict[str, Any]:
        return {
            **data,
            "direct": Child(value=10),
            "tupled": (Child(value=11),),
            "set_values": {Child(value=12)},
            "frozen_values": frozenset({Child(value=13)}),
        }

    family = SchemaFamily(
        model=Parent,
        name="runtime_callback_container_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                downgrade=supply_typed_values,
                downgrade_semantics="exact",
            ),
        ),
        nested=(
            NestedFamily("direct", child_family, matching_labels()),
            NestedFamily("tupled", child_family, matching_labels()),
            NestedFamily("set_values", child_family, matching_labels()),
            NestedFamily("frozen_values", child_family, matching_labels()),
        ),
        version_metadata=None,
    )

    rendered = family.dump(
        version="1",
        data=Parent(
            direct=Child(value=0),
            tupled=(Child(value=1),),
            set_values={Child(value=2)},
            frozen_values=frozenset({Child(value=3)}),
        ),
    )

    assert rendered == {
        "direct": {"value": 10},
        "tupled": [{"value": 11}],
        "set_values": [{"value": 12}],
        "frozen_values": [{"value": 13}],
    }


@pytest.mark.parametrize(
    ("operation", "message"),
    [
        ("shrink", "changed decorator nested occurrence cardinality"),
        ("reuse", "reused one decorator nested occurrence"),
        ("unrelated", "typed decorator nested replacement.*no unique family"),
        ("append_raw", "new occurrences must be exact current-model instances"),
    ],
)
def test_parent_callback_reconciliation_rejects_ambiguous_occurrence_changes(
    operation: str,
    message: str,
) -> None:
    @versioned_schema(
        name=f"runtime_reconcile_child_{operation}",
        versions=("1", "2"),
        current="2",
    )
    class Child(BaseModel):
        value: int

    class Unrelated(BaseModel):
        value: int

    def mutate(data: dict[str, Any]) -> dict[str, Any]:
        items = data["items"]
        if operation == "shrink":
            replacement: list[Any] = items[:1]
        elif operation == "reuse":
            replacement = [items[0], items[0]]
        elif operation == "unrelated":
            replacement = [Unrelated(value=items[0]["value"])]
        else:
            replacement = [items[0], {"value": 99}]
        return {**data, "items": replacement}

    @versioned_schema(
        name=f"runtime_reconcile_parent_{operation}",
        versions=("1", "2"),
        current="2",
        transitions=(
            VersionTransition(
                "1",
                "2",
                downgrade=mutate,
                downgrade_semantics="exact",
            ),
        ),
    )
    class Parent(BaseModel):
        items: list[Child]

    values = [Child(value=1), Child(value=2)]
    if operation in {"unrelated", "append_raw"}:
        values = values[:1]
    with pytest.raises(InvalidMigrationError, match=message):
        dump_versioned(Parent, version="1", data=Parent(items=values))


def test_parent_callback_reanchors_copied_homogeneous_occurrences() -> None:
    @versioned_schema(name="runtime_reanchor_child", versions=("1", "2"), current="2")
    class Child(BaseModel):
        value: int

    def copy_items(data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "items": [dict(item) for item in data["items"]]}

    @versioned_schema(
        name="runtime_reanchor_parent",
        versions=("1", "2"),
        current="2",
        transitions=(
            VersionTransition(
                "1",
                "2",
                downgrade=copy_items,
                downgrade_semantics="exact",
            ),
        ),
    )
    class Parent(BaseModel):
        items: list[Child]

    assert dump_versioned(
        Parent,
        version="1",
        data=Parent(items=[Child(value=1), Child(value=2)]),
    )["items"] == [{"value": 1}, {"value": 2}]


def test_non_child_mapping_union_arm_is_not_discovered_as_a_decorator_child() -> None:
    @versioned_schema(name="runtime_raw_union_child", versions=("1", "2"), current="2")
    class Child(BaseModel):
        value: int

    @versioned_schema(
        name="runtime_raw_union_parent",
        versions=("1", "2"),
        current="2",
        transitions=(
            VersionTransition(
                "1",
                "2",
                downgrade=lambda data: dict(data),
                downgrade_semantics="exact",
            ),
        ),
    )
    class Parent(BaseModel):
        items: list[Child | dict[str, int]]

    assert dump_versioned(
        Parent,
        version="1",
        data=Parent(items=[{"raw": 7}]),
    )["items"] == [{"raw": 7}]


def test_untyped_mapping_introduced_into_non_mapping_union_is_rejected() -> None:
    @versioned_schema(name="runtime_non_mapping_union_child", versions=("1", "2"), current="2")
    class Child(BaseModel):
        value: int

    @versioned_schema(
        name="runtime_non_mapping_union_parent",
        versions=("1", "2"),
        current="2",
        transitions=(
            VersionTransition(
                "1",
                "2",
                downgrade=lambda data: {**data, "items": [{"raw": 7}]},
                downgrade_semantics="exact",
            ),
        ),
    )
    class Parent(BaseModel):
        items: list[Child | int]

    with pytest.raises(
        InvalidMigrationError, match="introduced an untyped decorator nested mapping"
    ):
        dump_versioned(Parent, version="1", data=Parent(items=[1]))


def test_model_owned_serializer_rejects_duplicate_metadata_aliases() -> None:
    class Current(BaseModel):
        schema_version: str = Field(
            default="2",
            validation_alias="wire_version",
            serialization_alias="emitted_version",
        )
        value: int = 1

    class Historical(BaseModel):
        schema_version: Literal["1"] = Field(
            default="1",
            validation_alias="wire_version",
            serialization_alias="emitted_version",
        )
        value: int = 1

        @model_serializer
        def serialize_model(self) -> dict[str, Any]:
            return {
                "emitted_version": self.schema_version,
                "wire_version": self.schema_version,
                "value": self.value,
            }

    family = SchemaFamily(
        model=Current,
        name="runtime_duplicate_model_metadata",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
        version_metadata=VersionMetadata("wire_version", owner="model"),
    )

    with pytest.raises(ValueError, match="serialized duplicate version metadata.*wire_version"):
        family.defaults_for(version="1")


@pytest.mark.parametrize("envelope", [[], "not-a-list"])
def test_model_metadata_alias_path_can_be_absent_without_preflight_crashing(
    envelope: Any,
) -> None:
    class Payload(BaseModel):
        model_config = ConfigDict(validate_by_alias=True, validate_by_name=True)

        schema_version: str = Field(default="2", validation_alias=AliasPath("envelope", 1))
        value: int

    family = SchemaFamily(
        model=Payload,
        name=f"runtime_metadata_alias_absent_{type(envelope).__name__}",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        version_metadata=VersionMetadata("schema_version", owner="model"),
    )

    assert family.dump(
        version="1",
        data={"envelope": envelope, "value": 4},
    ) == {"schema_version": "1", "value": 4}


@pytest.mark.parametrize("envelope", [[], "not-a-list"])
def test_nested_alias_path_failure_defers_to_model_validation(envelope: Any) -> None:
    class Child(BaseModel):
        value: int

    child_family = SchemaFamily(
        model=Child,
        name=f"runtime_short_alias_child_{type(envelope).__name__}",
        versions=(SchemaVersion("1"),),
    )

    class Parent(BaseModel):
        child: Child = Field(validation_alias=AliasPath("envelope", 1))

    parent_family = SchemaFamily(
        model=Parent,
        name=f"runtime_short_alias_parent_{type(envelope).__name__}",
        versions=(SchemaVersion("1"),),
        nested=(NestedFamily("child", child_family, matching_labels()),),
    )

    with pytest.raises(ValidationError):
        parent_family.validate({"schema_version": "1", "envelope": envelope})


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("wrapped", 1),
        ("listed", {}),
        ("fixed", {}),
        ("mapped", []),
    ],
)
def test_malformed_raw_decorator_shapes_defer_to_model_validation(
    field_name: str,
    invalid_value: Any,
) -> None:
    @versioned_schema(name=f"runtime_raw_shape_child_{field_name}", versions=("1",), current="1")
    class Child(BaseModel):
        value: int

    class Wrapper(BaseModel):
        child: Child

    @versioned_schema(name=f"runtime_raw_shape_parent_{field_name}", versions=("1",), current="1")
    class Parent(BaseModel):
        wrapped: Wrapper
        listed: list[Child]
        fixed: tuple[Child]
        mapped: dict[str, Child]

    payload: dict[str, Any] = {
        "schema_version": "1",
        "wrapped": {"child": {"value": 1}},
        "listed": [{"value": 1}],
        "fixed": [{"value": 1}],
        "mapped": {"item": {"value": 1}},
    }
    payload[field_name] = invalid_value
    with pytest.raises(ValidationError):
        validate_versioned(Parent, payload)


def test_generated_set_union_render_carrier_preserves_supported_arms() -> None:
    @versioned_schema(name="runtime_set_union_child", versions=("1",), current="1")
    class Child(BaseModel):
        value: int

    @versioned_schema(name="runtime_set_union_parent", versions=("1",), current="1")
    class Parent(BaseModel):
        values: set[Child | int]

    generated = model_for_version(Parent, "1").model_validate(
        {"values": [{"value": 1}, 2]},
    )
    rendered = dump_versioned(
        Parent,
        version="1",
        data=cast(Any, generated),
    )

    assert {item if isinstance(item, int) else item["value"] for item in rendered["values"]} == {
        1,
        2,
    }


def test_nested_family_metadata_path_is_inserted_and_reserved_output_is_rejected() -> None:
    class Current(BaseModel):
        value: int = 1

    class Historical(BaseModel):
        value: int = 1

        @model_serializer
        def serialize_model(self) -> dict[str, Any]:
            payload: dict[str, Any] = {"value": self.value}
            if self.value < 0:
                payload["contract"] = {"version": "stale"}
            return payload

    family = SchemaFamily(
        model=Current,
        name="runtime_nested_family_metadata_output",
        versions=(
            SchemaVersion("1", wire_model=Historical),
            SchemaVersion("2"),
        ),
        version_metadata=VersionMetadata(("contract", "version"), owner="family"),
    )

    assert family.defaults_for(version="1") == {
        "value": 1,
        "contract": {"version": "1"},
    }
    with pytest.raises(ValueError, match="conflicts with version metadata.*contract"):
        family.dump(version="1", data=Current(value=-1))


def test_decorator_child_normalizes_validated_explicit_descendant_shapes() -> None:
    class Grandchild(BaseModel):
        value: int

    grandchild_family = SchemaFamily(
        model=Grandchild,
        name="runtime_normalized_explicit_grandchild",
        versions=(
            SchemaVersion("1", patches=(field_renamed("value", "legacy_value"),)),
            SchemaVersion("2"),
        ),
        transitions=(
            VersionTransition(
                "1",
                "2",
                upgrade=lambda data: dict(data),
                downgrade=lambda data: dict(data),
                downgrade_semantics="exact",
            ),
        ),
        version_metadata=None,
    )

    @versioned_schema(
        name="runtime_normalized_explicit_child",
        versions=("1", "2"),
        current="2",
        nested=(
            NestedFamily("direct", grandchild_family, matching_labels()),
            NestedFamily("optional", grandchild_family, matching_labels()),
        ),
    )
    class Child(BaseModel):
        direct: Grandchild
        optional: Grandchild | None

    @versioned_schema(
        name="runtime_normalized_explicit_parent",
        versions=("1", "2"),
        current="2",
    )
    class Parent(BaseModel):
        child: Child

    result = validate_versioned(
        Parent,
        {
            "schema_version": "1",
            "child": {
                "schema_version": "1",
                "direct": {"legacy_value": 4},
                "optional": None,
            },
        },
    )

    assert result.current_model == Parent(
        child=Child(
            direct=Grandchild(value=4),
            optional=None,
        ),
    )


def test_explicit_nested_conversion_preserves_unchanged_optional_tuple() -> None:
    class Child(BaseModel):
        value: int

    child_family = SchemaFamily(
        model=Child,
        name="runtime_scalar_tuple_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                upgrade=lambda data: dict(data),
                downgrade=lambda data: dict(data),
                downgrade_semantics="exact",
            ),
        ),
        version_metadata=None,
    )

    class Parent(BaseModel):
        optional_tuple: tuple[Child | None, ...]

    family = SchemaFamily(
        model=Parent,
        name="runtime_scalar_tuple_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        nested=(NestedFamily("optional_tuple", child_family, matching_labels()),),
        version_metadata=None,
    )

    assert family.dump(
        version="1",
        data=Parent(optional_tuple=(None,)),
    ) == {"optional_tuple": [None]}


def test_stationary_explicit_nested_label_normalizes_validated_source() -> None:
    class Child(BaseModel):
        value: int

    child_family = SchemaFamily(
        model=Child,
        name="runtime_stationary_child",
        versions=(SchemaVersion("stable"),),
        version_metadata=None,
    )

    class Parent(BaseModel):
        child: Child

    family = SchemaFamily(
        model=Parent,
        name="runtime_stationary_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(VersionTransition("1", "2", upgrade=lambda data: dict(data)),),
        nested=(NestedFamily("child", child_family, {"1": "stable", "2": "stable"}),),
        version_metadata=None,
    )

    result = family.validate({"child": {"value": 5}}, version="1")

    assert result.current_model == Parent(child=Child(value=5))


def test_callback_deep_containers_preserve_declared_paths_and_set_hashability() -> None:
    class Child(BaseModel):
        model_config = ConfigDict(frozen=True)

        value: int

    child_family = SchemaFamily(
        model=Child,
        name="runtime_deep_container_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                upgrade=lambda data: dict(data),
                downgrade=lambda data: dict(data),
                downgrade_semantics="exact",
            ),
        ),
        version_metadata=None,
    )

    class Wrapper(BaseModel):
        model_config = ConfigDict(frozen=True)

        child: Child

    class Parent(BaseModel):
        tupled: tuple[Wrapper, ...]
        set_values: set[Wrapper]
        frozen_values: frozenset[Wrapper]
        unchanged: tuple[Wrapper | None, ...]

    def supply_containers(data: dict[str, Any]) -> dict[str, Any]:
        return {
            **data,
            "tupled": (Wrapper(child=Child(value=10)),),
            "set_values": {Wrapper(child=Child(value=11))},
            "frozen_values": frozenset({Wrapper(child=Child(value=12))}),
            "unchanged": (None,),
        }

    family = SchemaFamily(
        model=Parent,
        name="runtime_deep_container_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                downgrade=supply_containers,
                downgrade_semantics="exact",
            ),
        ),
        nested=(
            NestedFamily(("tupled", "child"), child_family, matching_labels()),
            NestedFamily(("set_values", "child"), child_family, matching_labels()),
            NestedFamily(("frozen_values", "child"), child_family, matching_labels()),
            NestedFamily(("unchanged", "child"), child_family, matching_labels()),
        ),
        version_metadata=None,
    )

    rendered = family.dump(
        version="1",
        data=Parent(
            tupled=(Wrapper(child=Child(value=0)),),
            set_values={Wrapper(child=Child(value=1))},
            frozen_values=frozenset({Wrapper(child=Child(value=2))}),
            unchanged=(None,),
        ),
    )

    assert rendered == {
        "tupled": [{"child": {"value": 10}}],
        "set_values": [{"child": {"value": 11}}],
        "frozen_values": [{"child": {"value": 12}}],
        "unchanged": [None],
    }


def test_mixed_tuple_and_set_nested_projection_keeps_hash_required_variant() -> None:
    class Child(BaseModel):
        model_config = ConfigDict(frozen=True)

        value: int

    child_family = SchemaFamily(
        model=Child,
        name="runtime_mixed_tuple_set_child",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )

    class Wrapper(BaseModel):
        model_config = ConfigDict(frozen=True)

        child: Child

    class Parent(BaseModel):
        values: tuple[Wrapper, set[Wrapper]]

    family = SchemaFamily(
        model=Parent,
        name="runtime_mixed_tuple_set_parent",
        versions=(SchemaVersion("1"),),
        nested=(NestedFamily(("values", "child"), child_family, matching_labels()),),
        version_metadata=None,
    )

    result = family.validate(
        {
            "values": [
                {"child": {"value": 1}},
                [{"child": {"value": 2}}],
            ],
        },
        version="1",
    )

    assert result.current_model == Parent(
        values=(
            Wrapper(child=Child(value=1)),
            {Wrapper(child=Child(value=2))},
        ),
    )


def test_malformed_callback_deep_collection_defers_to_target_validation() -> None:
    class Child(BaseModel):
        value: int

    child_family = SchemaFamily(
        model=Child,
        name="runtime_malformed_deep_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        version_metadata=None,
    )

    class Wrapper(BaseModel):
        child: Child

    class Parent(BaseModel):
        wrappers: list[Wrapper]

    family = SchemaFamily(
        model=Parent,
        name="runtime_malformed_deep_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                downgrade=lambda data: {**data, "wrappers": 1},
                downgrade_semantics="exact",
            ),
        ),
        nested=(NestedFamily(("wrappers", "child"), child_family, matching_labels()),),
        version_metadata=None,
    )

    with pytest.raises(ValidationError):
        family.dump(
            version="1",
            data=Parent(wrappers=[Wrapper(child=Child(value=1))]),
        )


def test_generated_render_carriers_support_common_hash_required_schemas() -> None:
    @versioned_schema(name="runtime_nullable_core_child", versions=("1",), current="1")
    class NullableChild(BaseModel):
        value: int

    @versioned_schema(name="runtime_tagged_core_cat", versions=("1",), current="1")
    class Cat(BaseModel):
        kind: Literal["cat"]
        value: int

    @versioned_schema(name="runtime_tagged_core_dog", versions=("1",), current="1")
    class Dog(BaseModel):
        kind: Literal["dog"]
        value: int

    cases: tuple[tuple[str, Any, list[Any]], ...] = (
        ("nullable", set[NullableChild | None], [{"value": 1}, None]),
        ("tuple", set[tuple[NullableChild, int]], [[{"value": 2}, 3]]),
        (
            "tagged",
            set[Annotated[Cat | Dog, Field(discriminator="kind")]],
            [
                {"kind": "cat", "value": 4},
                {"kind": "dog", "value": 5},
            ],
        ),
    )

    for name, annotation, values in cases:
        parent_model = create_model(
            f"RuntimeCoreSchema{name.title()}Parent",
            values=(annotation, ...),
        )
        decorated_parent = versioned_schema(
            name=f"runtime_core_schema_{name}",
            versions=("1",),
            current="1",
        )(parent_model)
        generated = model_for_version(decorated_parent, "1").model_validate({"values": values})

        rendered = dump_versioned(decorated_parent, version="1", data=cast(Any, generated))

        assert len(rendered["values"]) == len(values)


def test_standard_lax_and_json_core_schemas_render_from_current_wire() -> None:
    @versioned_schema(name="runtime_standard_core_schemas", versions=("1",), current="1")
    class Payload(BaseModel):
        paths: set[Path]

    generated = model_for_version(Payload, "1").model_validate(
        {"paths": ["folder/item"]},
    )

    assert dump_versioned(Payload, version="1", data=cast(Any, generated)) == {
        "paths": [str(Path("folder/item"))],
        "schema_version": "1",
    }


def test_omitted_model_owned_nested_metadata_uses_target_default() -> None:
    class Child(BaseModel):
        schema_version: str = "2"
        value: int

    child_family = SchemaFamily(
        model=Child,
        name="runtime_omitted_model_metadata_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        version_metadata=VersionMetadata("schema_version", owner="model"),
    )

    class Parent(BaseModel):
        child: Child

    parent_family = SchemaFamily(
        model=Parent,
        name="runtime_omitted_model_metadata_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        nested=(NestedFamily("child", child_family, matching_labels()),),
        version_metadata=None,
    )

    result = parent_family.validate({"child": {"value": 9}}, version="1")

    assert result.current_model == Parent(child=Child(schema_version="2", value=9))


@pytest.mark.parametrize(
    ("shape", "annotation", "replacement"),
    [
        ("list", list, 1),
        ("tuple", tuple, {}),
        ("mapping", dict, []),
    ],
)
def test_parent_callback_invalidates_decorator_container_shape(
    shape: str,
    annotation: type,
    replacement: Any,
) -> None:
    @versioned_schema(
        name=f"runtime_invalid_route_child_{shape}",
        versions=("1", "2"),
        current="2",
    )
    class Child(BaseModel):
        value: int

    if annotation is list:
        field_annotation = list[Child]
        initial: Any = [Child(value=1)]
    elif annotation is tuple:
        field_annotation = tuple[Child]
        initial = (Child(value=1),)
    else:
        field_annotation = dict[str, Child]
        initial = {"item": Child(value=1)}

    def invalidate(data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "items": replacement}

    parent_model = create_model(
        f"RuntimeInvalidRoute{shape.title()}Parent",
        items=(field_annotation, ...),
    )
    decorated_parent = versioned_schema(
        name=f"runtime_invalid_route_parent_{shape}",
        versions=("1", "2"),
        current="2",
        transitions=(
            VersionTransition(
                "1",
                "2",
                downgrade=invalidate,
                downgrade_semantics="exact",
            ),
        ),
    )(parent_model)

    with pytest.raises(InvalidMigrationError, match="occurrence cardinality"):
        dump_versioned(
            decorated_parent,
            version="1",
            data=parent_model.model_validate({"items": initial}),
        )


def test_any_union_arm_allows_new_raw_mapping_occurrences() -> None:
    @versioned_schema(name="runtime_any_arm_child", versions=("1", "2"), current="2")
    class Child(BaseModel):
        value: int

    @versioned_schema(
        name="runtime_any_arm_parent",
        versions=("1", "2"),
        current="2",
        transitions=(
            VersionTransition(
                "1",
                "2",
                downgrade=lambda data: {**data, "items": [{"raw": 7}]},
                downgrade_semantics="exact",
            ),
        ),
    )
    class Parent(BaseModel):
        items: list[Child | Any]

    assert dump_versioned(Parent, version="1", data=Parent(items=[1]))["items"] == [{"raw": 7}]


def test_literal_union_arm_rejects_new_raw_mapping_occurrences() -> None:
    @versioned_schema(name="runtime_literal_arm_child", versions=("1", "2"), current="2")
    class Child(BaseModel):
        value: int

    @versioned_schema(
        name="runtime_literal_arm_parent",
        versions=("1", "2"),
        current="2",
        transitions=(
            VersionTransition(
                "1",
                "2",
                downgrade=lambda data: {**data, "items": [{"raw": 7}]},
                downgrade_semantics="exact",
            ),
        ),
    )
    class Parent(BaseModel):
        items: list[Child | Literal["raw"]]

    with pytest.raises(InvalidMigrationError, match="untyped decorator nested mapping"):
        dump_versioned(Parent, version="1", data=Parent(items=["raw"]))


def test_later_parent_callback_tuple_is_preserved_by_next_nested_edge() -> None:
    class Child(BaseModel):
        value: int

    child_family = SchemaFamily(
        model=Child,
        name="runtime_later_tuple_child",
        versions=(SchemaVersion("1"), SchemaVersion("2"), SchemaVersion("3")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                upgrade=lambda data: dict(data),
                downgrade=lambda data: dict(data),
                downgrade_semantics="exact",
            ),
            VersionTransition(
                "2",
                "3",
                upgrade=lambda data: dict(data),
                downgrade=lambda data: dict(data),
                downgrade_semantics="exact",
            ),
        ),
        version_metadata=None,
    )

    class Parent(BaseModel):
        children: tuple[Child | None, ...]

    family = SchemaFamily(
        model=Parent,
        name="runtime_later_tuple_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2"), SchemaVersion("3")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                downgrade=lambda data: dict(data),
                downgrade_semantics="exact",
            ),
            VersionTransition(
                "2",
                "3",
                downgrade=lambda data: {**data, "children": (None,)},
                downgrade_semantics="exact",
            ),
        ),
        nested=(NestedFamily("children", child_family, matching_labels()),),
        version_metadata=None,
    )

    assert family.dump(
        version="1",
        data=Parent(children=(Child(value=1),)),
    ) == {"children": [None]}


def test_later_parent_callback_scalar_nested_value_fails_target_validation() -> None:
    class Child(BaseModel):
        value: int

    child_family = SchemaFamily(
        model=Child,
        name="runtime_later_scalar_child",
        versions=(SchemaVersion("1"), SchemaVersion("2"), SchemaVersion("3")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                upgrade=lambda data: dict(data),
                downgrade=lambda data: dict(data),
                downgrade_semantics="exact",
            ),
            VersionTransition(
                "2",
                "3",
                upgrade=lambda data: dict(data),
                downgrade=lambda data: dict(data),
                downgrade_semantics="exact",
            ),
        ),
        version_metadata=None,
    )

    class Parent(BaseModel):
        child: Child

    family = SchemaFamily(
        model=Parent,
        name="runtime_later_scalar_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2"), SchemaVersion("3")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                downgrade=lambda data: dict(data),
                downgrade_semantics="exact",
            ),
            VersionTransition(
                "2",
                "3",
                downgrade=lambda data: {**data, "child": 7},
                downgrade_semantics="exact",
            ),
        ),
        nested=(NestedFamily("child", child_family, matching_labels()),),
        version_metadata=None,
    )

    with pytest.raises(ValidationError):
        family.dump(version="1", data=Parent(child=Child(value=1)))
