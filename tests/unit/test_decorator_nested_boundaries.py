from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Generic, Literal, TypeVar, cast, get_args
from uuid import UUID

import pytest
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

from pydantic_versions import (
    InvalidMigrationError,
    NestedFamily,
    SchemaFamily,
    SchemaVersion,
    SchemaVersionError,
    UnsupportedWireModelError,
    VersionTransition,
    dump_versioned,
    field_renamed,
    matching_labels,
    model_for_version,
    schema_version,
    validate_versioned,
    versioned_schema,
)

_ProbeItem = TypeVar("_ProbeItem")


def test_decorator_child_uses_its_historical_wire_projection() -> None:
    @versioned_schema(name="auto_direct_child", versions=("1", "2"), current="2")
    @schema_version("1", patches=(field_renamed("value", "legacy_value"),))
    class Child(BaseModel):
        value: int

    @versioned_schema(name="auto_direct_parent", versions=("1", "2"), current="2")
    class Parent(BaseModel):
        child: Child

    rendered = dump_versioned(
        Parent,
        version="1",
        data=Parent(child=Child(value=7)),
    )

    assert rendered == {
        "child": {"legacy_value": 7},
        "schema_version": "1",
    }
    validated = validate_versioned(Parent, rendered)
    assert validated.current_model == Parent(child=Child(value=7))


def test_overlapping_union_preserves_authoritative_typed_branch() -> None:
    calls: list[str] = []

    def a_to_one(data: dict[str, Any]) -> dict[str, Any]:
        calls.append("a1")
        return {**data, "value": f"{data['value']}:a1"}

    def a_to_two(data: dict[str, Any]) -> dict[str, Any]:
        calls.append("a2")
        return {**data, "value": f"{data['value']}:a2"}

    def b_to_one(data: dict[str, Any]) -> dict[str, Any]:
        calls.append("b1")
        return {**data, "value": f"{data['value']}:b1"}

    def b_to_two(data: dict[str, Any]) -> dict[str, Any]:
        calls.append("b2")
        return {**data, "value": f"{data['value']}:b2"}

    @versioned_schema(
        name="overlap_a",
        versions=("1", "2", "3"),
        current="3",
        transitions=(
            VersionTransition("1", "2", downgrade=a_to_one, downgrade_semantics="exact"),
            VersionTransition("2", "3", downgrade=a_to_two, downgrade_semantics="exact"),
        ),
    )
    class A(BaseModel):
        value: str

    @versioned_schema(
        name="overlap_b",
        versions=("1", "2", "3"),
        current="3",
        transitions=(
            VersionTransition("1", "2", downgrade=b_to_one, downgrade_semantics="exact"),
            VersionTransition("2", "3", downgrade=b_to_two, downgrade_semantics="exact"),
        ),
    )
    class B(BaseModel):
        value: str

    def reverse_items(data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "items": list(reversed(data["items"]))}

    @versioned_schema(
        name="overlap_parent",
        versions=("1", "2", "3"),
        current="3",
        transitions=(
            VersionTransition(
                "1",
                "2",
                downgrade=lambda data: data,
                downgrade_semantics="exact",
            ),
            VersionTransition(
                "2",
                "3",
                downgrade=reverse_items,
                downgrade_semantics="exact",
            ),
        ),
    )
    class Parent(BaseModel):
        items: list[A | B]

    rendered = dump_versioned(
        Parent,
        version="1",
        data=Parent(items=[A(value="first"), B(value="second")]),
    )

    assert rendered["items"] == [
        {"value": "second:b2:b1"},
        {"value": "first:a2:a1"},
    ]
    assert calls == ["a2", "b2", "a1", "b1"]


def test_decorator_children_traverse_builtin_containers_and_wrappers() -> None:
    @versioned_schema(name="shape_child", versions=("1", "2"), current="2")
    @schema_version("1", patches=(field_renamed("value", "legacy_value"),))
    class Child(BaseModel):
        model_config = ConfigDict(frozen=True)

        value: int

    class Wrapper(BaseModel):
        child: Child

    @versioned_schema(name="shape_parent", versions=("1", "2"), current="2")
    class Parent(BaseModel):
        direct: Child
        listed: list[Child]
        variadic: tuple[Child, ...]
        fixed: tuple[Child, Child]
        mapped: dict[str, Child]
        wrapped: Wrapper
        deep: list[dict[str, Wrapper]]
        set_values: set[Child]
        frozen_values: frozenset[Child]

    rendered = dump_versioned(
        Parent,
        version="1",
        data=Parent(
            direct=Child(value=1),
            listed=[Child(value=2)],
            variadic=(Child(value=3),),
            fixed=(Child(value=4), Child(value=5)),
            mapped={"key": Child(value=6)},
            wrapped=Wrapper(child=Child(value=7)),
            deep=[{"key": Wrapper(child=Child(value=8))}],
            set_values={Child(value=9)},
            frozen_values=frozenset({Child(value=10)}),
        ),
    )

    assert rendered["direct"]["legacy_value"] == 1
    assert rendered["listed"][0]["legacy_value"] == 2
    assert rendered["variadic"][0]["legacy_value"] == 3
    assert [item["legacy_value"] for item in rendered["fixed"]] == [4, 5]
    assert rendered["mapped"]["key"]["legacy_value"] == 6
    assert rendered["wrapped"]["child"]["legacy_value"] == 7
    assert rendered["deep"][0]["key"]["child"]["legacy_value"] == 8
    assert rendered["set_values"][0]["legacy_value"] == 9
    assert rendered["frozen_values"][0]["legacy_value"] == 10
    assert validate_versioned(Parent, rendered).current_model.direct.value == 1


@pytest.mark.parametrize("operation", ["validate", "render"])
@pytest.mark.parametrize("exclusion", ["exclude", "exclude_if"])
def test_decorator_set_rejects_collapse_after_excluded_fields_are_omitted(
    exclusion: str,
    operation: str,
) -> None:
    omitted = (
        Field(0, exclude=True)
        if exclusion == "exclude"
        else Field(0, exclude_if=lambda _value: True)
    )

    @versioned_schema(
        name=f"decorator_excluded_set_child_{exclusion}_{operation}",
        versions=("1", "2"),
        current="2",
    )
    class Child(BaseModel):
        model_config = ConfigDict(frozen=True)

        value: int
        internal: int = omitted

    def collapse(data: dict[str, Any]) -> dict[str, Any]:
        children = list(data["children"]) if operation == "render" else tuple(data["children"])
        for child in children:
            child["value"] = 0
        return {**data, "children": children}

    @versioned_schema(
        name=f"decorator_excluded_set_parent_{exclusion}_{operation}",
        versions=("1", "2"),
        current="2",
        transitions=(
            VersionTransition(
                "1",
                "2",
                upgrade=collapse,
                downgrade=collapse,
                downgrade_semantics="exact",
            ),
        ),
    )
    class Parent(BaseModel):
        children: set[Child]

    source = {
        "children": [
            {"value": 1, "internal": 1},
            {"value": 2, "internal": 2},
        ],
        "schema_version": "1",
    }
    current = Parent(
        children={
            Child(value=1, internal=1),
            Child(value=2, internal=2),
        }
    )
    with pytest.raises(InvalidMigrationError, match="cardinality"):
        if operation == "render":
            dump_versioned(Parent, version="1", data=current)
        else:
            validate_versioned(Parent, source)

    assert {child.value for child in current.children} == {1, 2}
    assert [child["value"] for child in source["children"]] == [1, 2]


def test_decorator_children_keep_identity_through_nested_hash_containers() -> None:
    observed_shapes: list[tuple[type[Any], type[Any]]] = []
    identifiers = {index: UUID(int=index) for index in range(1, 6)}

    @versioned_schema(name="nested_hash_child", versions=("1", "2"), current="2")
    @schema_version("1", patches=(field_renamed("value", "legacy_value"),))
    class Child(BaseModel):
        model_config = ConfigDict(strict=True, frozen=True)

        value: UUID
        internal: str = Field("private", exclude=True)

    def inspect(data: dict[str, Any]) -> dict[str, Any]:
        observed_shapes.append((type(data["tupled"]), type(data["grouped"])))
        assert all(type(item) is tuple for item in data["tupled"])
        assert all(type(item) is frozenset for item in data["grouped"])

        clone_index = 0

        def clone(item: Any) -> Any:
            nonlocal clone_index
            assert isinstance(item, Mapping)
            assert "internal" not in item
            if clone_index == 0:
                result = item.copy()
            elif clone_index == 1:
                result = item | {}
            elif clone_index == 2:
                result = {} | item
            else:
                result = item.copy()
                if clone_index == 3:
                    result |= {}
                else:
                    result.setdefault("probe", True)
                    result.update(probe=True)
                    assert result.pop("probe") is True
            clone_index += 1
            assert result != item and not result == item
            assert len({item, result}) == 2
            assert dict(result) == dict(item)
            return result

        return {
            **data,
            "tupled": {(clone(child), index) for child, index in data["tupled"]},
            "grouped": frozenset(
                frozenset(clone(child) for child in group) for group in data["grouped"]
            ),
        }

    @versioned_schema(
        name="nested_hash_parent",
        versions=("1", "2"),
        current="2",
        transitions=(
            VersionTransition(
                "1",
                "2",
                upgrade=inspect,
                downgrade=inspect,
                downgrade_semantics="exact",
            ),
        ),
    )
    class Parent(BaseModel):
        model_config = ConfigDict(strict=True)

        tupled: set[tuple[Child, int]]
        grouped: frozenset[frozenset[Child]]

    current = Parent(
        tupled={
            (Child(value=identifiers[1]), 10),
            (Child(value=identifiers[2]), 20),
        },
        grouped=frozenset(
            {
                frozenset({Child(value=identifiers[3]), Child(value=identifiers[4])}),
                frozenset({Child(value=identifiers[5])}),
            }
        ),
    )
    rendered = dump_versioned(Parent, version="1", data=current)
    historical_parent = model_for_version(Parent, "1")
    tuple_child = get_args(get_args(historical_parent.model_fields["tupled"].annotation)[0])[0]
    grouped_child = get_args(get_args(historical_parent.model_fields["grouped"].annotation)[0])[0]
    historical = cast(Any, historical_parent)(
        tupled={
            (tuple_child(legacy_value=identifiers[1]), 10),
            (tuple_child(legacy_value=identifiers[2]), 20),
        },
        grouped=frozenset(
            {
                frozenset(
                    {
                        grouped_child(legacy_value=identifiers[3]),
                        grouped_child(legacy_value=identifiers[4]),
                    }
                ),
                frozenset({grouped_child(legacy_value=identifiers[5])}),
            }
        ),
    )
    validated = validate_versioned(Parent, historical, version="1")

    tuple_values = {item[0]["legacy_value"] for item in rendered["tupled"]}
    grouped_values = {item["legacy_value"] for group in rendered["grouped"] for item in group}
    assert tuple_values | grouped_values == {str(value) for value in identifiers.values()}
    assert validated.current_model == current
    assert observed_shapes == [(set, frozenset)] * 2


def test_nested_frozenset_dump_prunes_every_child_metadata_without_reordering() -> None:
    @versioned_schema(
        name="nested_frozenset_metadata_child",
        versions=("1", "2"),
        current="2",
    )
    @schema_version("1", patches=(field_renamed("value", "legacy_value"),))
    class Child(BaseModel):
        model_config = ConfigDict(frozen=True)

        value: int
        internal: int = Field(0, exclude=True)
        conditional: int = Field(0, exclude_if=lambda value: value == 0)

    @versioned_schema(
        name="nested_frozenset_metadata_parent",
        versions=("1", "2"),
        current="2",
    )
    class Parent(BaseModel):
        grouped: frozenset[frozenset[Child]]

    for attempt in range(4):
        expected: set[int] = set()
        groups: list[frozenset[Child]] = []
        for group_size in range(1, 9):
            group = frozenset(
                Child(
                    value=attempt * 1_000 + group_size * 100 + item,
                    internal=item,
                )
                for item in range(group_size)
            )
            groups.append(group)
            expected.update(child.value for child in group)

        rendered = dump_versioned(
            Parent,
            version="1",
            data=Parent(grouped=frozenset(groups)),
        )
        children = [child for group in rendered["grouped"] for child in group]

        assert rendered["schema_version"] == "1"
        assert {child["legacy_value"] for child in children} == expected
        assert all("schema_version" not in child for child in children)
        assert all("internal" not in child for child in children)
        assert all("conditional" not in child for child in children)


def test_decorator_set_sibling_shifts_preserve_all_occurrences() -> None:
    def shift(data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "value": data["value"] + 1}

    @versioned_schema(
        name="batched_set_child",
        versions=("1", "2"),
        current="2",
        transitions=(
            VersionTransition(
                "1",
                "2",
                downgrade=shift,
                downgrade_semantics="exact",
            ),
        ),
    )
    class Child(BaseModel):
        model_config = ConfigDict(frozen=True)

        value: int

    @versioned_schema(name="batched_set_parent", versions=("1", "2"), current="2")
    class Parent(BaseModel):
        children: set[Child]

    rendered = dump_versioned(
        Parent,
        version="1",
        data=Parent(children={Child(value=value) for value in range(32)}),
    )

    assert {item["value"] for item in rendered["children"]} == set(range(1, 33))


def test_discriminated_union_preflights_stale_metadata_and_runs_validator_once() -> None:
    events: list[str] = []
    transitions: list[str] = []

    @versioned_schema(name="tagged_a", versions=("1", "2"), current="2")
    @schema_version("1", patches=(field_renamed("value", "legacy_value"),))
    class A(BaseModel):
        kind: Literal["a"] = "a"
        value: int

    @versioned_schema(name="tagged_b", versions=("1", "2"), current="2")
    @schema_version("1", patches=(field_renamed("value", "legacy_value"),))
    class B(BaseModel):
        kind: Literal["b"] = "b"
        value: int

        @field_validator("value")
        @classmethod
        def record_validation(cls, value: int) -> int:
            events.append("b")
            return value

    def upgrade(data: dict[str, Any]) -> dict[str, Any]:
        transitions.append("parent")
        return data

    @versioned_schema(
        name="tagged_parent",
        versions=("1", "2"),
        current="2",
        transitions=(VersionTransition("1", "2", upgrade=upgrade),),
    )
    class Parent(BaseModel):
        item: A | B = Field(discriminator="kind")

    stale = {
        "schema_version": "1",
        "item": {
            "schema_version": "2",
            "kind": "b",
            "legacy_value": 7,
        },
    }
    with pytest.raises(SchemaVersionError, match="payload declares '2'"):
        validate_versioned(Parent, stale)
    assert transitions == []
    assert events == []

    result = validate_versioned(
        Parent,
        {
            "schema_version": "1",
            "item": {
                "schema_version": "1",
                "kind": "b",
                "legacy_value": 7,
            },
        },
    )
    assert isinstance(result.current_model.item, B)
    assert result.current_model.item.value == 7
    assert events == ["b"]
    assert transitions == ["parent"]


def test_overlapping_union_rejects_ambiguous_raw_copies_before_next_callback() -> None:
    probes: list[str] = []

    @versioned_schema(name="copied_a", versions=("1", "2", "3"), current="3")
    class A(BaseModel):
        value: int

    @versioned_schema(name="copied_b", versions=("1", "2", "3"), current="3")
    class B(BaseModel):
        value: int

    def copy_children(data: dict[str, Any]) -> dict[str, Any]:
        probes.append("copy")
        return {**data, "items": [dict(item) for item in data["items"]]}

    def must_not_run(data: dict[str, Any]) -> dict[str, Any]:
        probes.append("later")
        return data

    @versioned_schema(
        name="copied_parent",
        versions=("1", "2", "3"),
        current="3",
        transitions=(
            VersionTransition("1", "2", upgrade=copy_children),
            VersionTransition("2", "3", upgrade=must_not_run),
        ),
    )
    class Parent(BaseModel):
        items: list[A | B]

    source = model_for_version(Parent, "1").model_validate(
        {
            "items": [
                model_for_version(A, "1").model_validate({"value": 1}),
                model_for_version(B, "1").model_validate({"value": 2}),
            ]
        }
    )

    with pytest.raises(InvalidMigrationError, match="ambiguous raw mappings"):
        validate_versioned(Parent, source, version="1")
    assert probes == ["copy"]


def test_historical_overlapping_union_materializes_the_authoritative_current_branch_once() -> None:
    events: list[tuple[str, str]] = []
    collapse = False

    def upgrade_a(data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "value": f"{data['value']}:a"}

    def upgrade_b(data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "value": f"{data['value']}:b"}

    @versioned_schema(
        name="materialized_a",
        versions=("1", "2"),
        current="2",
        transitions=(VersionTransition("1", "2", upgrade=upgrade_a),),
    )
    class A(BaseModel):
        model_config = ConfigDict(frozen=True)

        value: str

        @field_validator("value")
        @classmethod
        def count_a(cls, value: str) -> str:
            events.append(("model-a", value))
            return value

    @versioned_schema(
        name="materialized_b",
        versions=("1", "2"),
        current="2",
        transitions=(VersionTransition("1", "2", upgrade=upgrade_b),),
    )
    class B(BaseModel):
        model_config = ConfigDict(frozen=True, revalidate_instances="always")

        value: str

        @field_validator("value")
        @classmethod
        def count_b(cls, value: str) -> str:
            events.append(("model", value))
            return value

    def validate_selected_arm(value: B) -> B:
        events.append(("arm", value.value))
        return value.model_copy(update={"value": "same"}) if collapse else value

    @versioned_schema(name="materialized_parent", versions=("1", "2"), current="2")
    class Parent(BaseModel):
        items: set[A | Annotated[B, AfterValidator(validate_selected_arm)]]

    historical_parent_model = model_for_version(Parent, "1")
    historical_union = get_args(
        get_args(historical_parent_model.model_fields["items"].annotation)[0]
    )
    historical_b = historical_union[1]
    historical_parent = historical_parent_model.model_validate(
        {
            "items": {
                historical_b.model_validate({"value": "x"}),
                historical_b.model_validate({"value": "y"}),
            }
        }
    )
    events.clear()
    result = validate_versioned(Parent, historical_parent, version="1")

    assert {type(item) for item in result.current_model.items} == {B}
    assert {item.value for item in result.current_model.items} == {"x:b", "y:b"}
    assert sorted(kind for kind, _value in events) == ["arm", "arm", "model", "model"]

    collapse = True
    events.clear()
    with pytest.raises(InvalidMigrationError, match="set cardinality"):
        validate_versioned(Parent, historical_parent, version="1")
    assert sorted(kind for kind, _value in events) == ["arm", "arm", "model", "model"]


def test_current_render_accepts_subclasses_with_narrower_route_annotations() -> None:
    @versioned_schema(name="narrow_route_child", versions=("1", "2"), current="2")
    class Child(BaseModel):
        value: int

    @versioned_schema(name="narrow_union_parent", versions=("1", "2"), current="2")
    class UnionParent(BaseModel):
        item: Child | int

    class NarrowUnionParent(UnionParent):
        item: Child

    @versioned_schema(name="narrow_list_parent", versions=("1", "2"), current="2")
    class ListParent(BaseModel):
        children: list[Child]

    class BareListParent(ListParent):
        children: list

    assert dump_versioned(
        UnionParent,
        version="2",
        data=NarrowUnionParent(item=Child(value=7)),
    ) == {
        "item": {"value": 7},
        "schema_version": "2",
    }
    assert dump_versioned(
        ListParent,
        version="2",
        data=BareListParent(children=[Child(value=8)]),
    ) == {
        "children": [{"value": 8}],
        "schema_version": "2",
    }


def test_single_family_raw_duplication_raises_typed_cardinality_error() -> None:
    @versioned_schema(name="duplicated_single_child", versions=("1", "2"), current="2")
    class Child(BaseModel):
        value: int

    def duplicate(data: dict[str, Any]) -> dict[str, Any]:
        item = data["items"][0]
        return {**data, "items": [dict(item), dict(item)]}

    @versioned_schema(
        name="duplicated_single_parent",
        versions=("1", "2"),
        current="2",
        transitions=(
            VersionTransition("1", "2", downgrade=duplicate, downgrade_semantics="exact"),
        ),
    )
    class Parent(BaseModel):
        items: list[Child]

    with pytest.raises(InvalidMigrationError, match="one-to-one"):
        dump_versioned(
            Parent,
            version="1",
            data=Parent(items=[Child(value=1)]),
        )


def test_parent_callback_cannot_move_occurrence_identity_across_dispatch_sites() -> None:
    def mark(suffix: str):
        def downgrade(data: dict[str, Any]) -> dict[str, Any]:
            return {**data, "value": f"{data['value']}:{suffix}"}

        return downgrade

    @versioned_schema(
        name="cross_site_a",
        versions=("1", "2", "3"),
        current="3",
        transitions=(
            VersionTransition("1", "2", downgrade=mark("a1"), downgrade_semantics="exact"),
            VersionTransition("2", "3", downgrade=mark("a2"), downgrade_semantics="exact"),
        ),
    )
    class A(BaseModel):
        value: str

    @versioned_schema(
        name="cross_site_b",
        versions=("1", "2", "3"),
        current="3",
        transitions=(
            VersionTransition("1", "2", downgrade=mark("b1"), downgrade_semantics="exact"),
            VersionTransition("2", "3", downgrade=mark("b2"), downgrade_semantics="exact"),
        ),
    )
    class B(BaseModel):
        value: str

    def swap(data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "left": data["right"], "right": data["left"]}

    @versioned_schema(
        name="cross_site_parent",
        versions=("1", "2", "3"),
        current="3",
        transitions=(
            VersionTransition("1", "2", downgrade=lambda data: data, downgrade_semantics="exact"),
            VersionTransition("2", "3", downgrade=swap, downgrade_semantics="exact"),
        ),
    )
    class Parent(BaseModel):
        left: A
        right: B

    with pytest.raises(InvalidMigrationError, match="across dispatch sites"):
        dump_versioned(
            Parent,
            version="1",
            data=Parent(left=A(value="left"), right=B(value="right")),
        )


def test_exact_typed_replacement_on_final_parent_edge_is_fully_downgraded() -> None:
    def downgrade_b(data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "value": f"{data['value']}:b1"}

    @versioned_schema(name="replace_a", versions=("1", "2"), current="2")
    @schema_version("1", patches=(field_renamed("value", "legacy_a"),))
    class A(BaseModel):
        value: str

    @versioned_schema(
        name="replace_b",
        versions=("1", "2"),
        current="2",
        transitions=(
            VersionTransition(
                "1",
                "2",
                downgrade=downgrade_b,
                downgrade_semantics="exact",
            ),
        ),
    )
    @schema_version("1", patches=(field_renamed("value", "legacy_b"),))
    class B(BaseModel):
        value: str

    def replace_with_typed_b(data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "item": B(value="replacement")}

    @versioned_schema(
        name="replace_parent",
        versions=("1", "2"),
        current="2",
        transitions=(
            VersionTransition(
                "1",
                "2",
                downgrade=replace_with_typed_b,
                downgrade_semantics="exact",
            ),
        ),
    )
    class Parent(BaseModel):
        item: A | B

    rendered = dump_versioned(
        Parent,
        version="1",
        data=Parent(item=A(value="original")),
    )

    assert rendered["item"] == {
        "legacy_b": "replacement:b1",
    }


def test_mapping_keys_anchor_copied_overlapping_union_values() -> None:
    def a_to_one(data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "value": f"{data['value']}:a1"}

    def a_to_two(data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "value": f"{data['value']}:a2"}

    def b_to_one(data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "value": f"{data['value']}:b1"}

    def b_to_two(data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "value": f"{data['value']}:b2"}

    @versioned_schema(
        name="mapped_a",
        versions=("1", "2", "3"),
        current="3",
        transitions=(
            VersionTransition("1", "2", downgrade=a_to_one, downgrade_semantics="exact"),
            VersionTransition("2", "3", downgrade=a_to_two, downgrade_semantics="exact"),
        ),
    )
    class MappedA(BaseModel):
        value: str

    @versioned_schema(
        name="mapped_b",
        versions=("1", "2", "3"),
        current="3",
        transitions=(
            VersionTransition("1", "2", downgrade=b_to_one, downgrade_semantics="exact"),
            VersionTransition("2", "3", downgrade=b_to_two, downgrade_semantics="exact"),
        ),
    )
    class MappedB(BaseModel):
        value: str

    def copy_values(data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "items": {key: dict(value) for key, value in data["items"].items()}}

    @versioned_schema(
        name="mapped_union_parent",
        versions=("1", "2", "3"),
        current="3",
        transitions=(
            VersionTransition(
                "1",
                "2",
                downgrade=lambda data: data,
                downgrade_semantics="exact",
            ),
            VersionTransition(
                "2",
                "3",
                downgrade=copy_values,
                downgrade_semantics="exact",
            ),
        ),
    )
    class Parent(BaseModel):
        items: dict[str, MappedA | MappedB]

    rendered = dump_versioned(
        Parent,
        version="1",
        data=Parent(
            items={
                "a": MappedA(value="first"),
                "b": MappedB(value="second"),
            }
        ),
    )

    assert rendered["items"]["a"]["value"] == "first:a2:a1"
    assert rendered["items"]["b"]["value"] == "second:b2:b1"


def test_mapping_keys_do_not_anchor_beneath_a_reordered_dynamic_parent() -> None:
    def mark(suffix: str):
        def downgrade(data: dict[str, Any]) -> dict[str, Any]:
            return {**data, "value": f"{data['value']}:{suffix}"}

        return downgrade

    @versioned_schema(
        name="dynamic_anchor_a",
        versions=("1", "2", "3"),
        current="3",
        transitions=(
            VersionTransition("1", "2", downgrade=mark("a1"), downgrade_semantics="exact"),
            VersionTransition("2", "3", downgrade=mark("a2"), downgrade_semantics="exact"),
        ),
    )
    class A(BaseModel):
        value: str

    @versioned_schema(
        name="dynamic_anchor_b",
        versions=("1", "2", "3"),
        current="3",
        transitions=(
            VersionTransition("1", "2", downgrade=mark("b1"), downgrade_semantics="exact"),
            VersionTransition("2", "3", downgrade=mark("b2"), downgrade_semantics="exact"),
        ),
    )
    class B(BaseModel):
        value: str

    def reorder_copies(data: dict[str, Any]) -> dict[str, Any]:
        return {
            **data,
            "items": [
                {key: dict(value) for key, value in group.items()}
                for group in reversed(data["items"])
            ],
        }

    @versioned_schema(
        name="dynamic_anchor_parent",
        versions=("1", "2", "3"),
        current="3",
        transitions=(
            VersionTransition("1", "2", downgrade=lambda data: data, downgrade_semantics="exact"),
            VersionTransition("2", "3", downgrade=reorder_copies, downgrade_semantics="exact"),
        ),
    )
    class Parent(BaseModel):
        items: list[dict[str, A | B]]

    with pytest.raises(InvalidMigrationError, match="ambiguous raw mappings"):
        dump_versioned(
            Parent,
            version="1",
            data=Parent(
                items=[
                    {"x": A(value="first")},
                    {"x": B(value="second")},
                ]
            ),
        )


def test_unselected_overlapping_union_route_uses_selected_exact_list_arm() -> None:
    @versioned_schema(
        name="all_route_cardinality_grand",
        versions=("1", "2"),
        current="2",
    )
    class Grand(BaseModel):
        model_config = ConfigDict(frozen=True)

        value: int

    @versioned_schema(
        name="all_route_cardinality_set_branch",
        versions=("1", "2"),
        current="2",
    )
    class SetBranch(BaseModel):
        values: set[Grand]

    @versioned_schema(
        name="all_route_cardinality_selected_branch",
        versions=("1", "2"),
        current="2",
    )
    class SelectedBranch(BaseModel):
        values: list[dict[str, int | str]]

    @versioned_schema(
        name="all_route_cardinality_parent",
        versions=("1", "2"),
        current="2",
    )
    class Parent(BaseModel):
        item: SetBranch | SelectedBranch

    source = Parent(
        item=SelectedBranch(
            values=[{"value": 1}, {"value": "1"}],
        ),
    )

    assert type(source.item) is SelectedBranch
    assert dump_versioned(Parent, version="1", data=source) == {
        "item": {"values": [{"value": 1}, {"value": "1"}]},
        "schema_version": "1",
    }


def test_decorator_boundaries_recurse_across_decorated_families() -> None:
    @versioned_schema(name="recursive_grandchild", versions=("1", "2"), current="2")
    @schema_version("1", patches=(field_renamed("value", "legacy_value"),))
    class Grandchild(BaseModel):
        value: int

    @versioned_schema(name="recursive_child", versions=("1", "2"), current="2")
    @schema_version(
        "1",
        patches=(field_renamed("grandchildren", "old_grandchildren"),),
    )
    class Child(BaseModel):
        grandchildren: list[Grandchild]

    @versioned_schema(name="recursive_parent", versions=("1", "2"), current="2")
    class Parent(BaseModel):
        child: Child

    rendered = dump_versioned(
        Parent,
        version="1",
        data=Parent(child=Child(grandchildren=[Grandchild(value=11)])),
    )

    assert rendered["child"]["old_grandchildren"] == [{"legacy_value": 11}]
    assert validate_versioned(Parent, rendered).current_model.child.grandchildren[0].value == 11


def test_decorator_children_project_wrapper_defaults_without_running_opaque_factories() -> None:
    @versioned_schema(name="wrapper_default_child", versions=("1", "2"), current="2")
    @schema_version("1", patches=(field_renamed("value", "legacy_value"),))
    class Child(BaseModel):
        model_config = ConfigDict(extra="allow")

        value: int = 7

    class InstanceWrapper(BaseModel):
        model_config = ConfigDict(extra="allow")

        child: Child = Child()

    @versioned_schema(
        name="wrapper_instance_default_parent",
        versions=("1", "2"),
        current="2",
    )
    class InstanceParent(BaseModel):
        wrapper: InstanceWrapper = InstanceWrapper(secret="must-not-cross-wire")

    class FactoryWrapper(BaseModel):
        child: Child = Field(default_factory=Child)

    @versioned_schema(
        name="wrapper_factory_default_parent",
        versions=("1", "2"),
        current="2",
    )
    class FactoryParent(BaseModel):
        wrapper: FactoryWrapper = Field(default_factory=FactoryWrapper)

    model_expected = {
        "wrapper": {"child": {"legacy_value": 7, "schema_version": "1"}},
        "schema_version": "1",
    }
    dump_expected = {
        "wrapper": {"child": {"legacy_value": 7}},
        "schema_version": "1",
    }
    assert model_for_version(InstanceParent, "1")().model_dump() == model_expected
    assert dump_versioned(InstanceParent, version="1") == dump_expected
    assert model_for_version(FactoryParent, "1")().model_dump() == model_expected
    assert dump_versioned(FactoryParent, version="1") == dump_expected

    @versioned_schema(
        name="direct_extra_default_parent",
        versions=("1", "2"),
        current="2",
    )
    class DirectExtraParent(BaseModel):
        child: Child = Child(secret="must-not-cross-wire")

    direct_expected = {
        "child": {"legacy_value": 7, "schema_version": "1"},
        "schema_version": "1",
    }
    assert model_for_version(DirectExtraParent, "1")().model_dump() == direct_expected
    assert dump_versioned(DirectExtraParent, version="1") == {
        "child": {"legacy_value": 7},
        "schema_version": "1",
    }


def test_decorator_children_project_defaults_through_every_builtin_shape() -> None:
    @versioned_schema(name="container_default_child", versions=("1", "2"), current="2")
    @schema_version("1", patches=(field_renamed("value", "legacy_value"),))
    class Child(BaseModel):
        model_config = ConfigDict(frozen=True)

        value: int

    class Wrapper(BaseModel):
        child: Child

    @versioned_schema(
        name="container_default_parent",
        versions=("1", "2"),
        current="2",
    )
    class Parent(BaseModel):
        listed: list[Child] = [Child(value=1)]
        variadic: tuple[Child, ...] = (Child(value=2),)
        fixed: tuple[Child, str] = (Child(value=3), "fixed")
        mapped: dict[str, Child] = {"x": Child(value=4)}
        optional: Child | None = Child(value=5)
        setted: set[Child] = {Child(value=6)}
        frozen: frozenset[Child] = frozenset({Child(value=7)})
        deep: list[dict[str, Wrapper]] = [{"x": Wrapper(child=Child(value=8))}]

    model_expected_values = {
        "listed": [{"legacy_value": 1, "schema_version": "1"}],
        "variadic": [{"legacy_value": 2, "schema_version": "1"}],
        "fixed": [{"legacy_value": 3, "schema_version": "1"}, "fixed"],
        "mapped": {"x": {"legacy_value": 4, "schema_version": "1"}},
        "optional": {"legacy_value": 5, "schema_version": "1"},
        "setted": [{"legacy_value": 6, "schema_version": "1"}],
        "frozen": [{"legacy_value": 7, "schema_version": "1"}],
        "deep": [{"x": {"child": {"legacy_value": 8, "schema_version": "1"}}}],
        "schema_version": "1",
    }
    dump_expected_values = {
        "listed": [{"legacy_value": 1}],
        "variadic": [{"legacy_value": 2}],
        "fixed": [{"legacy_value": 3}, "fixed"],
        "mapped": {"x": {"legacy_value": 4}},
        "optional": {"legacy_value": 5},
        "setted": [{"legacy_value": 6}],
        "frozen": [{"legacy_value": 7}],
        "deep": [{"x": {"child": {"legacy_value": 8}}}],
        "schema_version": "1",
    }
    assert model_for_version(Parent, "1")().model_dump(mode="json") == model_expected_values
    assert dump_versioned(Parent, version="1") == dump_expected_values

    factory_calls: list[str] = []

    def opaque_children() -> list[Child]:
        factory_calls.append("called")
        return [Child(value=9)]

    @versioned_schema(
        name="opaque_container_default_parent",
        versions=("1", "2"),
        current="2",
    )
    class OpaqueParent(BaseModel):
        children: list[Child] = Field(default_factory=opaque_children)

    with pytest.raises(UnsupportedWireModelError, match="opaque factory"):
        model_for_version(OpaqueParent, "1")
    assert factory_calls == []


def test_parent_callback_can_introduce_exact_typed_decorator_branches() -> None:
    def downgrade_child(data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "value": f"{data['value']}:child"}

    @versioned_schema(
        name="introduced_typed_child",
        versions=("1", "2"),
        current="2",
        transitions=(
            VersionTransition("1", "2", downgrade=downgrade_child, downgrade_semantics="exact"),
        ),
    )
    @schema_version("1", patches=(field_renamed("value", "legacy_value"),))
    class Child(BaseModel):
        value: str

    def introduce(data: dict[str, Any]) -> dict[str, Any]:
        return {
            **data,
            "items": [
                *data["items"],
                Child(value="introduced"),
            ],
        }

    @versioned_schema(
        name="introduced_typed_parent",
        versions=("1", "2"),
        current="2",
        transitions=(
            VersionTransition("1", "2", downgrade=introduce, downgrade_semantics="exact"),
        ),
    )
    class Parent(BaseModel):
        items: list[int | Child]

    rendered = dump_versioned(
        Parent,
        version="1",
        data=Parent(items=[Child(value="existing"), 0]),
    )

    assert rendered == {
        "items": [
            {"legacy_value": "existing:child"},
            0,
            {"legacy_value": "introduced:child"},
        ],
        "schema_version": "1",
    }


def test_parent_callback_rejects_untyped_decorator_branch_introduction() -> None:
    def downgrade_child(data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "value": data["value"] + 10}

    @versioned_schema(
        name="untyped_introduction_child",
        versions=("1", "2"),
        current="2",
        transitions=(
            VersionTransition("1", "2", downgrade=downgrade_child, downgrade_semantics="exact"),
        ),
    )
    class Child(BaseModel):
        value: int

    def introduce_raw(data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "item": {"value": 9}}

    @versioned_schema(
        name="untyped_introduction_parent",
        versions=("1", "2"),
        current="2",
        transitions=(
            VersionTransition("1", "2", downgrade=introduce_raw, downgrade_semantics="exact"),
        ),
    )
    class Parent(BaseModel):
        item: int | Child

    with pytest.raises(InvalidMigrationError, match="untyped decorator nested mapping"):
        dump_versioned(Parent, version="1", data=Parent(item=0))


def test_parent_callback_fails_closed_when_a_mapping_subclass_probe_raises() -> None:
    probe_active = False
    probe_count = 0

    class RaisingMappingSubclassMeta(type):
        def __subclasscheck__(cls, subclass: type[Any]) -> bool:
            nonlocal probe_count
            if probe_active and subclass is dict:
                probe_count += 1
                raise TypeError("custom mapping probe failed")
            return super().__subclasscheck__(subclass)

    @dataclass
    class Envelope(metaclass=RaisingMappingSubclassMeta):
        marker: str

    @versioned_schema(name="mapping_probe_child", versions=("1", "2"), current="2")
    class Child(BaseModel):
        value: int

    def introduce_raw_child(data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "item": {"value": 1}}

    @versioned_schema(
        name="mapping_probe_parent",
        versions=("1", "2"),
        current="2",
        transitions=(VersionTransition("1", "2", upgrade=introduce_raw_child),),
    )
    class Parent(BaseModel):
        item: Envelope | Child

    probe_active = True
    try:
        with pytest.raises(InvalidMigrationError, match="untyped decorator nested mapping"):
            validate_versioned(
                Parent,
                {"schema_version": "1", "item": {"marker": "source"}},
            )
    finally:
        probe_active = False

    assert probe_count > 0


def test_dump_tolerates_generic_annotation_instance_probe_type_errors() -> None:
    probe_active = False
    probe_count = 0

    class RaisingInstanceMeta(type):
        def __instancecheck__(cls, instance: object) -> bool:
            nonlocal probe_count
            if probe_active:
                probe_count += 1
                raise TypeError("custom instance probe failed")
            return super().__instancecheck__(instance)

    @dataclass
    class Envelope(Generic[_ProbeItem], metaclass=RaisingInstanceMeta):
        value: _ProbeItem

    @dataclass
    class IntegerEnvelope(Envelope[int]):
        pass

    @versioned_schema(name="instance_probe_child", versions=("1",), current="1")
    class Child(BaseModel):
        value: int

    class Parent(BaseModel):
        payload: Child | Envelope[int]

    family = SchemaFamily(
        model=Parent,
        name="instance_probe_parent",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )

    value = Parent(payload=IntegerEnvelope(1))
    probe_active = True
    try:
        rendered = family.dump(data=value, version="1")
    finally:
        probe_active = False

    assert probe_count > 0
    assert rendered == {"payload": {"value": 1}}


def test_parent_callback_rejects_reused_exact_typed_introductions() -> None:
    @versioned_schema(name="reused_introduction_child", versions=("1", "2"), current="2")
    class Child(BaseModel):
        value: int

    def introduce_reused(data: dict[str, Any]) -> dict[str, Any]:
        child = Child(value=9)
        return {**data, "items": [child, child]}

    @versioned_schema(
        name="reused_introduction_parent",
        versions=("1", "2"),
        current="2",
        transitions=(
            VersionTransition("1", "2", downgrade=introduce_reused, downgrade_semantics="exact"),
        ),
    )
    class Parent(BaseModel):
        items: list[int | Child]

    with pytest.raises(InvalidMigrationError, match="reused one decorator nested occurrence"):
        dump_versioned(Parent, version="1", data=Parent(items=[0, 1]))


def test_exact_typed_owner_replacement_rebinds_its_recursive_subtree() -> None:
    def mark_b(data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "value": data["value"] + 10}

    @versioned_schema(name="replacement_grand_a", versions=("1", "2"), current="2")
    class GrandA(BaseModel):
        value: int

    @versioned_schema(
        name="replacement_grand_b",
        versions=("1", "2"),
        current="2",
        transitions=(VersionTransition("1", "2", downgrade=mark_b, downgrade_semantics="exact"),),
    )
    @schema_version("1", patches=(field_renamed("value", "legacy_value"),))
    class GrandB(BaseModel):
        value: int

    @versioned_schema(name="replacement_owner_a", versions=("1", "2"), current="2")
    class A(BaseModel):
        grand: GrandA

    @versioned_schema(name="replacement_owner_b", versions=("1", "2"), current="2")
    class B(BaseModel):
        grand: GrandB

    def replace_owner(data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "item": B(grand=GrandB(value=2))}

    @versioned_schema(
        name="replacement_recursive_parent",
        versions=("1", "2"),
        current="2",
        transitions=(
            VersionTransition("1", "2", downgrade=replace_owner, downgrade_semantics="exact"),
        ),
    )
    class Parent(BaseModel):
        item: A | B

    rendered = dump_versioned(
        Parent,
        version="1",
        data=Parent(item=A(grand=GrandA(value=1))),
    )

    assert rendered == {
        "item": {
            "grand": {"legacy_value": 12},
        },
        "schema_version": "1",
    }


def test_recursive_decorator_set_cardinality_is_checked_after_target_coercion() -> None:
    def collapse_by_coercion(data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "value": "1" if data["value"] == 1 else 1}

    @versioned_schema(
        name="recursive_collapse_grand",
        versions=("1", "2"),
        current="2",
        transitions=(
            VersionTransition(
                "1", "2", downgrade=collapse_by_coercion, downgrade_semantics="lossy"
            ),
        ),
    )
    class Grand(BaseModel):
        model_config = ConfigDict(frozen=True)

        value: int

    @versioned_schema(name="recursive_collapse_child", versions=("1", "2"), current="2")
    class Child(BaseModel):
        grandchildren: set[Grand]

    @versioned_schema(name="recursive_collapse_parent", versions=("1", "2"), current="2")
    class Parent(BaseModel):
        child: Child

    with pytest.raises(InvalidMigrationError, match="set cardinality"):
        dump_versioned(
            Parent,
            version="1",
            data=Parent(child=Child(grandchildren={Grand(value=1), Grand(value=2)})),
        )


def test_parent_callback_rejects_non_string_decorator_mapping_keys() -> None:
    @versioned_schema(name="non_string_key_child", versions=("1", "2"), current="2")
    class Child(BaseModel):
        value: str

    def inject_numeric_key(data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "children": {1: {"value": "new"}}}

    @versioned_schema(
        name="non_string_key_parent",
        versions=("1", "2"),
        current="2",
        transitions=(
            VersionTransition("1", "2", downgrade=inject_numeric_key, downgrade_semantics="exact"),
        ),
    )
    class Parent(BaseModel):
        model_config = ConfigDict(coerce_numbers_to_str=True)

        children: dict[str, Child]

    with pytest.raises(InvalidMigrationError, match="non-string"):
        dump_versioned(Parent, version="1", data=Parent(children={}))


def test_decorator_to_explicit_set_cardinality_recurses_after_target_coercion() -> None:
    class Grand(BaseModel):
        model_config = ConfigDict(frozen=True)

        value: int | str

    class GrandV1(BaseModel):
        model_config = ConfigDict(frozen=True)

        value: int

    grand_family = SchemaFamily(
        model=Grand,
        name="decorator_explicit_grand",
        versions=(SchemaVersion("1", wire_model=GrandV1), SchemaVersion("2")),
    )

    @versioned_schema(
        name="decorator_explicit_child",
        versions=("1", "2"),
        current="2",
        nested=(NestedFamily("grandchildren", grand_family, matching_labels()),),
    )
    class Child(BaseModel):
        grandchildren: set[Grand]

    @versioned_schema(name="decorator_explicit_parent", versions=("1", "2"), current="2")
    class Parent(BaseModel):
        child: Child

    with pytest.raises(InvalidMigrationError, match="set cardinality"):
        dump_versioned(
            Parent,
            version="1",
            data=Parent(
                child=Child(
                    grandchildren={Grand(value=1), Grand(value="1")},
                )
            ),
        )


def test_decorator_to_explicit_nested_family_round_trips_historical_names() -> None:
    def upgrade_grand(data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "value": data["value"] + 10}

    def downgrade_grand(data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "value": data["value"] - 10}

    class Grand(BaseModel):
        value: int

    grand_family = SchemaFamily(
        model=Grand,
        name="decorator_explicit_roundtrip_grand",
        versions=(
            SchemaVersion("1", patches=(field_renamed("value", "legacy_value"),)),
            SchemaVersion("2"),
        ),
        transitions=(
            VersionTransition(
                "1",
                "2",
                upgrade=upgrade_grand,
                downgrade=downgrade_grand,
                downgrade_semantics="exact",
            ),
        ),
    )

    @versioned_schema(
        name="decorator_explicit_roundtrip_child",
        versions=("1", "2"),
        current="2",
        nested=(NestedFamily("grand", grand_family, matching_labels()),),
    )
    class Child(BaseModel):
        grand: Grand

    @versioned_schema(
        name="decorator_explicit_roundtrip_parent",
        versions=("1", "2"),
        current="2",
    )
    class Parent(BaseModel):
        child: Child

    current = Parent(child=Child(grand=Grand(value=11)))
    rendered = dump_versioned(Parent, version="1", data=current)

    assert rendered == {
        "child": {
            "grand": {"legacy_value": 1},
        },
        "schema_version": "1",
    }
    assert validate_versioned(Parent, rendered).current_model == current


def test_recursive_union_stale_metadata_fails_before_validation_or_callbacks() -> None:
    events: list[str] = []

    @versioned_schema(name="stale_recursive_grand_a", versions=("1", "2"), current="2")
    class GrandA(BaseModel):
        value: int

        @field_validator("value")
        @classmethod
        def count_grand_a(cls, value: int) -> int:
            events.append("grand-a")
            return value

    @versioned_schema(name="stale_recursive_grand_b", versions=("1", "2"), current="2")
    class GrandB(BaseModel):
        value: int

    @versioned_schema(name="stale_recursive_a", versions=("1", "2"), current="2")
    class A(BaseModel):
        grand: GrandA

    @versioned_schema(name="stale_recursive_b", versions=("1", "2"), current="2")
    class B(BaseModel):
        grand: GrandB

    @versioned_schema(name="stale_recursive_child", versions=("1", "2"), current="2")
    class Child(BaseModel):
        item: A | B

    @versioned_schema(name="stale_recursive_parent", versions=("1", "2"), current="2")
    class Parent(BaseModel):
        child: Child

    historical = {
        "schema_version": "1",
        "child": {
            "schema_version": "1",
            "item": {
                "schema_version": "1",
                "grand": {"schema_version": "2", "value": 1},
            },
        },
    }
    with pytest.raises(SchemaVersionError, match="declares '2'"):
        validate_versioned(Parent, historical, version="1")

    current = {
        "schema_version": "2",
        "child": {
            "schema_version": "2",
            "item": {
                "schema_version": "2",
                "grand": {"schema_version": "1", "value": 1},
            },
        },
    }
    with pytest.raises(SchemaVersionError, match="declares '1'"):
        dump_versioned(Parent, version="1", data=current)
    assert events == []


def test_decorator_child_model_owned_metadata_round_trips() -> None:
    @versioned_schema(name="model_metadata_child", versions=("1", "2"), current="2")
    @schema_version("1", patches=(field_renamed("value", "legacy_value"),))
    class Child(BaseModel):
        schema_version: str = "2"
        value: int

    @versioned_schema(name="model_metadata_parent", versions=("1", "2"), current="2")
    class Parent(BaseModel):
        child: Child

    rendered = dump_versioned(
        Parent,
        version="1",
        data=Parent(child=Child(value=12)),
    )

    assert rendered["child"] == {"schema_version": "1", "legacy_value": 12}
    result = validate_versioned(Parent, rendered)
    assert result.current_model.child.schema_version == "2"
    assert result.current_model.child.value == 12
