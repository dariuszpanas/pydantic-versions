from __future__ import annotations

import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path
from threading import Barrier
from typing import Annotated, Any, cast

import pytest
from pydantic import AliasPath, BaseModel, ConfigDict, Field, create_model

from pydantic_versions import (
    ConversionPlan,
    InvalidMigrationError,
    IrreversibleTransitionError,
    NestedFamily,
    NestedFamilyDescription,
    PlanStep,
    ProjectionDescription,
    SchemaFamily,
    SchemaFamilySelectionError,
    SchemaInventory,
    SchemaVersion,
    SchemaVersionError,
    TransitionDescription,
    UnknownSchemaVersionError,
    VersionDescription,
    VersionMetadata,
    VersionTransition,
    field_default,
    field_removed,
    field_renamed,
    matching_labels,
    migration,
    validate_versioned,
)
from pydantic_versions._runtime import _nested_family_collection_kind


class InventoryConfig(BaseModel):
    timeout: float = 10.0
    retries: int = 3
    feature: bool = False


class RenderConfig(BaseModel):
    renamed: int = 1
    removed: str = "current"


class PrivacyConfig(BaseModel):
    token: str = "current"
    values: list[str] = Field(default_factory=list)


class CollisionConfig(BaseModel):
    value: int = 1


class NestedPlanChild(BaseModel):
    value: int = 1


class NestedPlanParent(BaseModel):
    child: NestedPlanChild


class NestedRouteChild(BaseModel):
    model_config = ConfigDict(frozen=True)

    value: int = 1
    internal: str = "current"


def _identity(data: dict[str, Any]) -> dict[str, Any]:
    return data


class _CallableProbe:
    def __init__(self, marker: str) -> None:
        self.marker = marker
        self.calls = 0

    def __call__(self, data: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        return data

    def __repr__(self) -> str:
        return f"<callable:{self.marker}>"


class _FactoryProbe:
    def __init__(self, marker: str) -> None:
        self.marker = marker
        self.calls = 0

    def __call__(self) -> list[str]:
        self.calls += 1
        return [self.marker]

    def __repr__(self) -> str:
        return f"<factory:{self.marker}>"


def _inventory_family(
    *,
    upgrade: Any = _identity,
    name: str = "inventory",
) -> SchemaFamily[InventoryConfig]:
    return SchemaFamily(
        model=InventoryConfig,
        name=name,
        versions=(
            SchemaVersion(
                "1",
                patches=(
                    field_default("timeout", 5.0),
                    field_renamed("retries", "attempts"),
                    field_removed("feature"),
                ),
            ),
            SchemaVersion("2"),
            SchemaVersion("3"),
        ),
        transitions=(VersionTransition("1", "2", upgrade=upgrade),),
    )


def _structural_family(
    *,
    name: str = "structural",
    remove_field: bool = True,
) -> SchemaFamily[RenderConfig]:
    patches = [field_renamed("renamed", "legacy_name")]
    if remove_field:
        patches.append(field_removed("removed"))
    return SchemaFamily(
        model=RenderConfig,
        name=name,
        versions=(
            SchemaVersion("1", patches=tuple(patches)),
            SchemaVersion("2"),
        ),
    )


def _deterministic_nested_family() -> SchemaFamily[NestedPlanParent]:
    child = SchemaFamily(
        model=NestedPlanChild,
        name="deterministic_nested_child",
        versions=(SchemaVersion("1"), SchemaVersion("2"), SchemaVersion("3")),
    )
    return SchemaFamily(
        model=NestedPlanParent,
        name="deterministic_nested_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2"), SchemaVersion("3")),
        nested=(NestedFamily("child", child, matching_labels()),),
    )


def _nested_shape_value(shape: str, child: BaseModel) -> Any:
    if shape == "direct":
        return child
    if shape == "list":
        return [child]
    if shape == "tuple":
        return (child,)
    if shape == "set":
        return {child}
    if shape == "frozenset":
        return frozenset((child,))
    raise AssertionError(f"Unexpected nested test shape: {shape}")


def _nested_shape_annotation(shape: str) -> Any:
    if shape == "direct":
        return NestedRouteChild
    if shape == "list":
        return list[NestedRouteChild]
    if shape == "tuple":
        return tuple[NestedRouteChild, ...]
    if shape == "set":
        return set[NestedRouteChild]
    if shape == "frozenset":
        return frozenset[NestedRouteChild]
    raise AssertionError(f"Unexpected nested test shape: {shape}")


def _step_signature(
    step: PlanStep,
) -> tuple[str, str, str, str, str]:
    return (
        step.kind,
        step.source_version,
        step.target_version,
        step.schema_path,
        step.semantics,
    )


def _assert_json_safe(value: object) -> None:
    if value is None or isinstance(value, bool | int | float | str):
        return
    if isinstance(value, list):
        for item in value:
            _assert_json_safe(item)
        return
    if isinstance(value, dict):
        assert all(isinstance(key, str) for key in value)
        for item in value.values():
            _assert_json_safe(item)
        return
    pytest.fail(f"Non-JSON-safe value: {type(value).__name__}")


def test_describe_returns_a_complete_frozen_inventory() -> None:
    family = _inventory_family()

    inventory = family.describe()

    assert inventory == SchemaInventory(
        family="inventory",
        model=f"{InventoryConfig.__module__}.{InventoryConfig.__qualname__}",
        current_version="3",
        versions=(
            VersionDescription(
                label="1",
                wire_model="generated",
                projections=(
                    ProjectionDescription(
                        kind="default",
                        current_field="timeout",
                        historical_field="timeout",
                        has_default=True,
                    ),
                    ProjectionDescription(
                        kind="renamed",
                        current_field="retries",
                        historical_field="attempts",
                        has_default=False,
                    ),
                    ProjectionDescription(
                        kind="removed",
                        current_field="feature",
                        historical_field=None,
                        has_default=False,
                    ),
                ),
            ),
            VersionDescription(label="2", wire_model="generated", projections=()),
            VersionDescription(label="3", wire_model="current", projections=()),
        ),
        transitions=(
            TransitionDescription(
                source="1",
                target="2",
                upgrade="custom",
                downgrade="unavailable",
                downgrade_semantics="unavailable",
            ),
            TransitionDescription(
                source="2",
                target="3",
                upgrade="implicit_identity",
                downgrade="implicit_identity",
                downgrade_semantics="exact",
            ),
        ),
        nested=(),
        version_metadata=VersionMetadata(),
    )
    assert family.describe() is inventory
    assert tuple(
        (
            f"{transition.source} -> {transition.target}",
            transition.upgrade,
            transition.downgrade,
            transition.downgrade_semantics,
        )
        for transition in inventory.transitions
    ) == (
        ("1 -> 2", "custom", "unavailable", "unavailable"),
        ("2 -> 3", "implicit_identity", "implicit_identity", "exact"),
    )

    with pytest.raises(FrozenInstanceError):
        cast(Any, inventory).family = "changed"
    with pytest.raises(FrozenInstanceError):
        cast(Any, inventory.versions[0].projections[0]).kind = "removed"


def test_public_nested_description_serializes_pairs_as_json_arrays() -> None:
    description = NestedFamilyDescription(
        schema_path="workers[*].retry",
        family="retry",
        versions=(("1", "legacy"), ("2", "current")),
    )

    assert description.to_dict() == {
        "schema_path": "workers[*].retry",
        "family": "retry",
        "versions": [["1", "legacy"], ["2", "current"]],
    }


def test_public_records_defensively_freeze_caller_owned_sequences() -> None:
    projections = [ProjectionDescription("removed", "value", None, False)]
    versions = [VersionDescription("1", "generated", cast(Any, projections))]
    transitions = [
        TransitionDescription("1", "2", "implicit_identity", "implicit_identity", "exact")
    ]
    nested_versions = [["1", "legacy"]]
    nested = [
        NestedFamilyDescription(
            "child",
            "child_family",
            cast(Any, nested_versions),
        )
    ]
    inventory = SchemaInventory(
        family="frozen",
        model="tests.Config",
        current_version="2",
        versions=cast(Any, versions),
        transitions=cast(Any, transitions),
        nested=cast(Any, nested),
        version_metadata=None,
    )
    steps = [
        PlanStep(
            id="pv1-test",
            family="frozen",
            source_version="1",
            target_version="2",
            operation="validate",
            direction="upgrade",
            kind="implicit_identity",
            schema_path="$",
            semantics="exact",
            conditional=False,
        )
    ]
    plan = ConversionPlan(
        family="frozen",
        source_version="1",
        target_version="2",
        operation="validate",
        semantics="not_applicable",
        steps=cast(Any, steps),
    )

    projections.clear()
    versions.clear()
    transitions.clear()
    nested_versions[0][1] = "mutated"
    nested.clear()
    steps.clear()

    assert inventory.versions[0].projections[0].current_field == "value"
    assert inventory.transitions[0].source == "1"
    assert inventory.nested[0].versions == (("1", "legacy"),)
    assert plan.steps[0].kind == "implicit_identity"


def test_projection_inventory_and_plan_order_follow_patch_declarations() -> None:
    class OrderedProjectionConfig(BaseModel):
        first: int = 1
        second: int = 2

    family = SchemaFamily(
        model=OrderedProjectionConfig,
        name="ordered_projections",
        versions=(
            SchemaVersion(
                "1",
                patches=(field_removed("second"), field_removed("first")),
            ),
            SchemaVersion("2"),
        ),
    )

    assert tuple(
        projection.current_field for projection in family.describe().versions[0].projections
    ) == ("second", "first")
    assert tuple(
        step.schema_path for step in family.plan_validation("1").steps if step.kind == "projection"
    ) == ("second", "first")


def test_validation_plan_exposes_structural_custom_and_identity_steps_in_order() -> None:
    family = _inventory_family()

    plan = family.plan_validation("1")

    assert plan == ConversionPlan(
        family="inventory",
        source_version="1",
        target_version="3",
        operation="validate",
        semantics="not_applicable",
        steps=plan.steps,
    )
    assert tuple(map(_step_signature, plan.steps)) == (
        ("metadata", "1", "1", "schema_version", "not_applicable"),
        ("wire_validation", "1", "1", "$", "not_applicable"),
        ("projection", "1", "1", "timeout", "not_applicable"),
        ("projection", "1", "1", "retries", "not_applicable"),
        ("projection", "1", "1", "feature", "not_applicable"),
        ("custom_transition", "1", "2", "$", "not_applicable"),
        ("implicit_identity", "2", "3", "$", "exact"),
        ("current_validation", "3", "3", "$", "not_applicable"),
    )
    assert all(step.operation == "validate" for step in plan.steps)
    assert all(step.direction == "upgrade" for step in plan.steps)
    assert all(not step.conditional for step in plan.steps)
    assert len({step.id for step in plan.steps}) == len(plan.steps)
    assert all(re.fullmatch(r"pv1-[0-9a-f]{64}", step.id) for step in plan.steps)
    assert family.plan_validation("1") is plan


def test_validation_plans_are_scoped_to_the_requested_source() -> None:
    family = _inventory_family()

    middle = family.plan_validation("2")
    current = family.plan_validation("3")

    assert tuple(step.kind for step in middle.steps) == (
        "metadata",
        "wire_validation",
        "implicit_identity",
        "current_validation",
    )
    assert tuple(step.kind for step in current.steps) == (
        "metadata",
        "wire_validation",
        "current_validation",
    )
    assert all(
        not (step.source_version == "1" and step.target_version == "2")
        for step in (*middle.steps, *current.steps)
    )


def test_family_without_version_metadata_omits_metadata_plan_steps() -> None:
    family = SchemaFamily(
        model=CollisionConfig,
        name="unversioned_body",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        version_metadata=None,
    )

    assert family.describe().version_metadata is None
    assert tuple(step.kind for step in family.plan_validation("1").steps) == (
        "wire_validation",
        "implicit_identity",
        "current_validation",
    )
    assert tuple(step.kind for step in family.plan_render("1").steps) == (
        "current_validation",
        "implicit_identity",
        "wire_validation",
        "serialization",
    )


def test_render_plan_reverses_edges_then_projects_and_marks_removal_lossy() -> None:
    family = _structural_family()

    plan = family.plan_render("1")

    assert plan.source_version == "2"
    assert plan.target_version == "1"
    assert plan.operation == "render"
    assert plan.semantics == "lossy"
    assert tuple(map(_step_signature, plan.steps)) == (
        ("current_validation", "2", "2", "$", "not_applicable"),
        ("implicit_identity", "2", "1", "$", "exact"),
        ("projection", "1", "1", "renamed", "exact"),
        ("projection", "1", "1", "removed", "lossy"),
        ("metadata", "1", "1", "schema_version", "not_applicable"),
        ("wire_validation", "1", "1", "$", "not_applicable"),
        ("serialization", "1", "1", "$", "not_applicable"),
    )
    assert all(step.operation == "render" for step in plan.steps)
    assert all(step.direction == "downgrade" for step in plan.steps)
    assert all(not step.conditional for step in plan.steps)


def test_render_plan_is_exact_for_rename_only_and_current_targets() -> None:
    family = _structural_family(name="exact_structural", remove_field=False)

    historical = family.plan_render("1")
    current = family.plan_render("2")

    assert historical.semantics == "exact"
    assert tuple(step.kind for step in historical.steps) == (
        "current_validation",
        "implicit_identity",
        "projection",
        "metadata",
        "wire_validation",
        "serialization",
    )
    assert current.semantics == "exact"
    assert tuple(step.kind for step in current.steps) == (
        "current_validation",
        "metadata",
        "wire_validation",
        "serialization",
    )
    assert {step.id for step in family.plan_validation("1").steps}.isdisjoint(
        step.id for step in historical.steps
    )


@pytest.mark.parametrize(
    "shape",
    ["direct", "list", "tuple", "set", "frozenset"],
)
@pytest.mark.parametrize("child_semantics", ["exact", "lossy", "unavailable"])
def test_nested_plans_aggregate_child_routes_for_supported_shapes(
    shape: str,
    child_semantics: str,
) -> None:
    child_v1 = SchemaVersion(
        "1",
        patches=(field_removed("internal"),) if child_semantics == "lossy" else (),
    )
    child_transitions = (
        (VersionTransition("1", "2", upgrade=_identity),)
        if child_semantics == "unavailable"
        else ()
    )
    child_family = SchemaFamily(
        model=NestedRouteChild,
        name=f"nested_plan_child_{shape}_{child_semantics}",
        versions=(child_v1, SchemaVersion("2")),
        transitions=child_transitions,
    )

    annotation = _nested_shape_annotation(shape)
    parent_model = create_model(
        f"NestedPlanParent_{shape}_{child_semantics}",
        __module__=__name__,
        child=(annotation, ...),
    )
    parent_downgrade = _CallableProbe(f"parent-{shape}-{child_semantics}")
    parent_family = SchemaFamily(
        model=parent_model,
        name=f"nested_plan_parent_{shape}_{child_semantics}",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                downgrade=parent_downgrade,
                downgrade_semantics="exact",
            ),
        ),
        nested=(NestedFamily("child", child_family, matching_labels()),),
    )

    validation = parent_family.plan_validation("1")
    validation_nested = tuple(step for step in validation.steps if step.kind == "nested")
    assert tuple(map(_step_signature, validation_nested)) == (
        ("nested", "1", "2", "$.child", "not_applicable"),
    )
    assert validation.steps.index(validation_nested[0]) < next(
        index for index, step in enumerate(validation.steps) if step.kind == "implicit_identity"
    )
    assert validation_nested[0].conditional is True

    compiled = parent_family._compiled_family()
    render_candidate = compiled.catalog.render_plans[compiled.index("1")]
    render_nested = tuple(step for step in render_candidate.steps if step.kind == "nested")
    assert tuple(map(_step_signature, render_nested)) == (
        ("nested", "2", "1", "$.child", child_semantics),
    )
    assert render_candidate.steps.index(render_nested[0]) < next(
        index
        for index, step in enumerate(render_candidate.steps)
        if step.kind == "custom_transition"
    )
    assert render_nested[0].conditional is True
    assert render_candidate.semantics == child_semantics
    assert len({step.id for step in render_candidate.steps}) == len(render_candidate.steps)

    child = NestedRouteChild(value=7, internal="private")
    data = parent_model.model_validate({"child": _nested_shape_value(shape, child)})
    if child_semantics == "unavailable":
        with pytest.raises(IrreversibleTransitionError, match="nested family"):
            parent_family.plan_render("1")
        with pytest.raises(IrreversibleTransitionError, match="nested family"):
            parent_family.dump(version="1", data=data)
        assert parent_downgrade.calls == 0
    else:
        assert parent_family.plan_render("1") is render_candidate
        rendered = parent_family.dump(version="1", data=data, include_version=False)
        assert "child" in rendered
        assert parent_downgrade.calls == 1


@pytest.mark.parametrize("grandchild_semantics", ["lossy", "unavailable"])
def test_nested_plan_semantics_propagate_through_child_families(
    grandchild_semantics: str,
) -> None:
    grandchild_model = create_model(
        f"NestedPlanGrandchild_{grandchild_semantics}",
        __module__=__name__,
        value=(int, 1),
        internal=(str, "current"),
    )
    grandchild = SchemaFamily(
        model=grandchild_model,
        name=f"nested_plan_grandchild_{grandchild_semantics}",
        versions=(
            SchemaVersion(
                "1",
                patches=((field_removed("internal"),) if grandchild_semantics == "lossy" else ()),
            ),
            SchemaVersion("2"),
        ),
        transitions=(
            (VersionTransition("1", "2", upgrade=_identity),)
            if grandchild_semantics == "unavailable"
            else ()
        ),
    )
    child_model = create_model(
        f"NestedPlanChild_{grandchild_semantics}",
        __module__=__name__,
        grandchild=(grandchild_model, ...),
    )
    child = SchemaFamily(
        model=child_model,
        name=f"nested_plan_child_recursive_{grandchild_semantics}",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        nested=(NestedFamily("grandchild", grandchild, matching_labels()),),
    )
    parent_model = create_model(
        f"NestedPlanRoot_{grandchild_semantics}",
        __module__=__name__,
        child=(child_model, ...),
    )
    parent = SchemaFamily(
        model=parent_model,
        name=f"nested_plan_root_{grandchild_semantics}",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        nested=(NestedFamily("child", child, matching_labels()),),
    )

    compiled = parent._compiled_family()
    candidate = compiled.catalog.render_plans[compiled.index("1")]
    parent_nested = next(step for step in candidate.steps if step.kind == "nested")
    assert parent_nested.semantics == grandchild_semantics
    assert candidate.semantics == grandchild_semantics
    if grandchild_semantics == "unavailable":
        with pytest.raises(IrreversibleTransitionError, match="nested family"):
            parent.plan_render("1")
    else:
        assert parent.plan_render("1") is candidate


def test_three_level_nested_runtime_executes_each_owned_transition_in_order() -> None:
    events: list[str] = []

    def grandchild_upgrade(data: dict[str, Any]) -> dict[str, Any]:
        events.append("grandchild 1->2")
        return {**data, "value": data["value"] + 1}

    def grandchild_downgrade(data: dict[str, Any]) -> dict[str, Any]:
        events.append("grandchild 2->1")
        return {**data, "value": data["value"] - 1}

    class GrandchildPayload(BaseModel):
        value: int

    grandchild = SchemaFamily(
        model=GrandchildPayload,
        name="three_level_runtime_grandchild",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                upgrade=grandchild_upgrade,
                downgrade=grandchild_downgrade,
                downgrade_semantics="exact",
            ),
        ),
        version_metadata=None,
    )

    def child_upgrade(data: dict[str, Any]) -> dict[str, Any]:
        events.append("child 1->2")
        return {**data, "value": data["value"] + 10}

    def child_downgrade(data: dict[str, Any]) -> dict[str, Any]:
        events.append("child 2->1")
        return {**data, "value": data["value"] - 10}

    class ChildPayload(BaseModel):
        value: int
        grandchild: GrandchildPayload

    child = SchemaFamily(
        model=ChildPayload,
        name="three_level_runtime_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                upgrade=child_upgrade,
                downgrade=child_downgrade,
                downgrade_semantics="exact",
            ),
        ),
        nested=(NestedFamily("grandchild", grandchild, matching_labels()),),
        version_metadata=None,
    )

    def parent_upgrade(data: dict[str, Any]) -> dict[str, Any]:
        events.append("parent 1->2")
        return {**data, "value": data["value"] + 100}

    def parent_downgrade(data: dict[str, Any]) -> dict[str, Any]:
        events.append("parent 2->1")
        return {**data, "value": data["value"] - 100}

    class ParentPayload(BaseModel):
        value: int
        child: ChildPayload

    parent = SchemaFamily(
        model=ParentPayload,
        name="three_level_runtime_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                upgrade=parent_upgrade,
                downgrade=parent_downgrade,
                downgrade_semantics="exact",
            ),
        ),
        nested=(NestedFamily("child", child, matching_labels()),),
        version_metadata=None,
    )

    validated = parent.validate(
        {
            "value": 1,
            "child": {"value": 10, "grandchild": {"value": 100}},
        },
        version="1",
    )

    assert validated.current_model == ParentPayload(
        value=101,
        child=ChildPayload(
            value=20,
            grandchild=GrandchildPayload(value=101),
        ),
    )
    assert events == ["grandchild 1->2", "child 1->2", "parent 1->2"]

    events.clear()
    rendered = parent.dump(
        version="1",
        data=validated.current_model,
        include_version=False,
    )

    assert rendered == {
        "value": 1,
        "child": {"value": 10, "grandchild": {"value": 100}},
    }
    assert events == ["grandchild 2->1", "child 2->1", "parent 2->1"]


def test_nested_render_projects_renamed_child_fields_to_the_target_wire() -> None:
    class ChildPayload(BaseModel):
        max_attempts: int = 3

    child = SchemaFamily(
        model=ChildPayload,
        name="renamed_nested_render_child",
        versions=(
            SchemaVersion(
                "1",
                patches=(field_renamed("max_attempts", "attempts"),),
            ),
            SchemaVersion("2"),
        ),
        version_metadata=None,
    )

    class ParentPayload(BaseModel):
        child: ChildPayload

    parent = SchemaFamily(
        model=ParentPayload,
        name="renamed_nested_render_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        nested=(NestedFamily("child", child, matching_labels()),),
        version_metadata=None,
    )

    rendered = parent.dump(
        version="1",
        data=ParentPayload(child=ChildPayload(max_attempts=5)),
        include_version=False,
    )

    assert rendered == {"child": {"attempts": 5}}
    assert parent.validate(rendered, version="1").current_model.child.max_attempts == 5


def test_parent_downgrade_receives_canonical_renamed_child_fields() -> None:
    seen_children: list[dict[str, Any]] = []

    class ChildPayload(BaseModel):
        max_attempts: int = 3

    child = SchemaFamily(
        model=ChildPayload,
        name="canonical_parent_downgrade_child",
        versions=(
            SchemaVersion(
                "1",
                patches=(field_renamed("max_attempts", "attempts"),),
            ),
            SchemaVersion("2"),
        ),
        version_metadata=None,
    )

    def parent_downgrade(data: dict[str, Any]) -> dict[str, Any]:
        seen_children.append(dict(data["child"]))
        child_payload = dict(data["child"])
        child_payload["max_attempts"] += 2
        return {**data, "child": child_payload}

    class ParentPayload(BaseModel):
        child: ChildPayload

    parent = SchemaFamily(
        model=ParentPayload,
        name="canonical_parent_downgrade",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                downgrade=parent_downgrade,
                downgrade_semantics="exact",
            ),
        ),
        nested=(NestedFamily("child", child, matching_labels()),),
        version_metadata=None,
    )

    rendered = parent.dump(
        version="1",
        data=ParentPayload(child=ChildPayload(max_attempts=5)),
        include_version=False,
    )

    assert seen_children == [{"max_attempts": 5}]
    assert rendered == {"child": {"attempts": 7}}


def test_non_monotonic_parent_upgrades_receive_canonical_child_fields() -> None:
    seen_children: list[tuple[str, dict[str, Any]]] = []

    class ChildPayload(BaseModel):
        max_attempts: int = 3

    child = SchemaFamily(
        model=ChildPayload,
        name="canonical_non_monotonic_child",
        versions=(
            SchemaVersion(
                "1",
                patches=(field_renamed("max_attempts", "attempts"),),
            ),
            SchemaVersion(
                "2",
                patches=(field_renamed("max_attempts", "retry_limit"),),
            ),
            SchemaVersion("3"),
        ),
        version_metadata=None,
    )

    def first_parent_upgrade(data: dict[str, Any]) -> dict[str, Any]:
        seen_children.append(("a->b", dict(data["child"])))
        child_payload = dict(data["child"])
        child_payload["max_attempts"] += 1
        return {**data, "child": child_payload}

    def second_parent_upgrade(data: dict[str, Any]) -> dict[str, Any]:
        seen_children.append(("b->c", dict(data["child"])))
        child_payload = dict(data["child"])
        child_payload["max_attempts"] += 1
        return {**data, "child": child_payload}

    class ParentPayload(BaseModel):
        child: ChildPayload

    parent = SchemaFamily(
        model=ParentPayload,
        name="canonical_non_monotonic_parent",
        versions=(SchemaVersion("a"), SchemaVersion("b"), SchemaVersion("c")),
        transitions=(
            VersionTransition("a", "b", upgrade=first_parent_upgrade),
            VersionTransition("b", "c", upgrade=second_parent_upgrade),
        ),
        nested=(
            NestedFamily(
                "child",
                child,
                {"a": "2", "b": "1", "c": "3"},
            ),
        ),
        version_metadata=None,
    )

    validated = parent.validate({"child": {"retry_limit": 5}}, version="a")

    assert seen_children == [
        ("a->b", {"max_attempts": 5}),
        ("b->c", {"max_attempts": 6}),
    ]
    assert validated.current_model.child.max_attempts == 7


def test_nested_model_owned_metadata_rebases_direct_and_collection_values() -> None:
    class ChildPayload(BaseModel):
        model_config = ConfigDict(frozen=True)

        schema_version: str = "2"
        value: int

    child = SchemaFamily(
        model=ChildPayload,
        name="runtime_model_metadata_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        version_metadata=VersionMetadata("schema_version", owner="model"),
    )

    class ParentPayload(BaseModel):
        direct: ChildPayload
        listed: list[ChildPayload]
        tupled: tuple[ChildPayload, ...]
        set_values: set[ChildPayload]
        frozen_values: frozenset[ChildPayload]

    parent = SchemaFamily(
        model=ParentPayload,
        name="runtime_model_metadata_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        nested=(
            NestedFamily("direct", child, matching_labels()),
            NestedFamily("listed", child, matching_labels()),
            NestedFamily("tupled", child, matching_labels()),
            NestedFamily("set_values", child, matching_labels()),
            NestedFamily("frozen_values", child, matching_labels()),
        ),
        version_metadata=None,
    )
    current = ParentPayload(
        direct=ChildPayload(value=1),
        listed=[ChildPayload(value=2)],
        tupled=(ChildPayload(value=3),),
        set_values={ChildPayload(value=4)},
        frozen_values=frozenset((ChildPayload(value=5),)),
    )

    rendered = parent.dump(version="1", data=current, include_version=False)

    assert rendered["direct"]["schema_version"] == "1"
    for field_name in ("listed", "tupled", "set_values", "frozen_values"):
        assert rendered[field_name][0]["schema_version"] == "1"

    round_tripped = parent.validate(rendered, version="1").current_model
    assert round_tripped.direct.schema_version == "2"
    assert round_tripped.listed[0].schema_version == "2"
    assert round_tripped.tupled[0].schema_version == "2"
    assert next(iter(round_tripped.set_values)).schema_version == "2"
    assert next(iter(round_tripped.frozen_values)).schema_version == "2"


def test_optional_annotated_nested_collections_keep_container_metadata_rules() -> None:
    class ChildPayload(BaseModel):
        model_config = ConfigDict(frozen=True)

        value: int

    child = SchemaFamily(
        model=ChildPayload,
        name="optional_collection_metadata_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    )

    class ParentPayload(BaseModel):
        listed: Annotated[list[ChildPayload] | None, Field(description="listed")]
        tupled: Annotated[tuple[ChildPayload, ...] | None, Field(description="tupled")]
        set_values: Annotated[set[ChildPayload] | None, Field(description="set")]
        frozen_values: Annotated[
            frozenset[ChildPayload] | None,
            Field(description="frozen"),
        ]

    parent = SchemaFamily(
        model=ParentPayload,
        name="optional_collection_metadata_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        nested=(
            NestedFamily("listed", child, matching_labels()),
            NestedFamily("tupled", child, matching_labels()),
            NestedFamily("set_values", child, matching_labels()),
            NestedFamily("frozen_values", child, matching_labels()),
        ),
        version_metadata=None,
    )

    assert [
        _nested_family_collection_kind(model=ParentPayload, path=(field_name,))
        for field_name in ("listed", "tupled", "set_values", "frozen_values")
    ] == ["list", "tuple", "set", "frozenset"]

    rendered = parent.dump(
        version="1",
        data=ParentPayload(
            listed=[ChildPayload(value=1)],
            tupled=(ChildPayload(value=2),),
            set_values={ChildPayload(value=3)},
            frozen_values=frozenset((ChildPayload(value=4),)),
        ),
        include_version=False,
    )

    assert rendered["listed"] == [{"value": 1}]
    for field_name, value in (("tupled", 2), ("set_values", 3), ("frozen_values", 4)):
        assert rendered[field_name] == [{"value": value}]


def test_optional_annotated_set_detects_nested_migration_collapse() -> None:
    class ChildPayload(BaseModel):
        model_config = ConfigDict(frozen=True)

        value: int

    child = SchemaFamily(
        model=ChildPayload,
        name="optional_set_collision_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                downgrade=lambda data: {**data, "value": 0},
                downgrade_semantics="exact",
            ),
        ),
        version_metadata=None,
    )

    class ParentPayload(BaseModel):
        children: Annotated[
            set[ChildPayload] | None,
            Field(description="optional set"),
        ]

    parent = SchemaFamily(
        model=ParentPayload,
        name="optional_set_collision_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        nested=(NestedFamily("children", child, matching_labels()),),
        version_metadata=None,
    )

    with pytest.raises(InvalidMigrationError, match="cannot preserve set cardinality"):
        parent.dump(
            version="1",
            data=ParentPayload(
                children={ChildPayload(value=1), ChildPayload(value=2)},
            ),
            include_version=False,
        )


@pytest.mark.parametrize("shape", ["direct", "list", "tuple", "set", "frozenset"])
def test_nested_metadata_conflicts_preflight_before_source_validation_and_callables(
    shape: str,
) -> None:
    class HashablePayload(dict[str, Any]):
        __hash__ = object.__hash__

    def nested_value(item: dict[str, Any]) -> Any:
        if shape == "direct":
            return item
        if shape == "list":
            return [item]
        if shape == "tuple":
            return (item,)
        hashable = HashablePayload(item)
        if shape == "set":
            return {hashable}
        return frozenset((hashable,))

    class ChildPayload(BaseModel):
        model_config = ConfigDict(frozen=True)

        value: int = 1

    child = SchemaFamily(
        model=ChildPayload,
        name=f"metadata_preflight_{shape}_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    )
    annotations: dict[str, Any] = {
        "direct": ChildPayload,
        "list": list[ChildPayload],
        "tuple": tuple[ChildPayload, ...],
        "set": set[ChildPayload],
        "frozenset": frozenset[ChildPayload],
    }
    parent_model = create_model(
        f"MetadataPreflight{shape.title()}Parent",
        child=(annotations[shape], ...),
        required=(int, ...),
    )
    parent_upgrade = _CallableProbe(f"metadata-preflight-upgrade-{shape}")
    parent_downgrade = _CallableProbe(f"metadata-preflight-downgrade-{shape}")
    parent = SchemaFamily(
        model=parent_model,
        name=f"metadata_preflight_{shape}_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                upgrade=parent_upgrade,
                downgrade=parent_downgrade,
                downgrade_semantics="exact",
            ),
        ),
        nested=(NestedFamily("child", child, matching_labels()),),
        version_metadata=None,
    )
    stale_child = {"schema_version": "2", "value": 1}

    with pytest.raises(SchemaVersionError, match="payload declares '2'"):
        parent.validate({"child": nested_value(stale_child)}, version="1")

    assert parent_upgrade.calls == 0

    stale_current_child = {"schema_version": "1", "value": 1}
    with pytest.raises(SchemaVersionError, match="payload declares '1'"):
        parent.dump(
            version="1",
            data={"child": nested_value(stale_current_child)},
            include_version=False,
        )

    assert parent_downgrade.calls == 0


def test_nested_metadata_preflight_preserves_alias_path_input() -> None:
    class ChildPayload(BaseModel):
        value: int

    child = SchemaFamily(
        model=ChildPayload,
        name="metadata_alias_path_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    )

    class ParentPayload(BaseModel):
        child: ChildPayload = Field(validation_alias=AliasPath("payload", "child"))

    parent = SchemaFamily(
        model=ParentPayload,
        name="metadata_alias_path_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        nested=(NestedFamily("child", child, matching_labels()),),
        version_metadata=None,
    )
    raw = {
        "payload": {
            "child": {
                "schema_version": "1",
                "value": 7,
            },
        },
    }
    expected = {
        "payload": {
            "child": {
                "schema_version": "1",
                "value": 7,
            },
        },
    }

    validated = parent.validate(raw, version="1")

    assert raw == expected
    assert validated.current_model.child.value == 7


def test_nested_metadata_preflight_reads_family_metadata_from_model_extras() -> None:
    class ChildPayload(BaseModel):
        model_config = ConfigDict(extra="allow")

        value: int

    child = SchemaFamily(
        model=ChildPayload,
        name="metadata_extra_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    )

    class ParentPayload(BaseModel):
        child: ChildPayload

    parent_upgrade = _CallableProbe("metadata-extra-upgrade")
    parent_downgrade = _CallableProbe("metadata-extra-downgrade")
    parent = SchemaFamily(
        model=ParentPayload,
        name="metadata_extra_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                upgrade=parent_upgrade,
                downgrade=parent_downgrade,
                downgrade_semantics="exact",
            ),
        ),
        nested=(NestedFamily("child", child, matching_labels()),),
        version_metadata=None,
    )
    stale_source = ChildPayload.model_validate(
        {"schema_version": "2", "value": 1},
    )
    stale_current = ChildPayload.model_validate(
        {"schema_version": "1", "value": 1},
    )

    with pytest.raises(SchemaVersionError, match="payload declares '2'"):
        parent.validate({"child": stale_source}, version="1")
    with pytest.raises(SchemaVersionError, match="payload declares '1'"):
        parent.dump(
            version="1",
            data={"child": stale_current},
            include_version=False,
        )

    assert parent_upgrade.calls == 0
    assert parent_downgrade.calls == 0


@pytest.mark.parametrize("collection_kind", ["set", "frozenset"])
def test_target_wire_coercion_cannot_collapse_nested_set_cardinality(
    collection_kind: str,
) -> None:
    class ChildPayload(BaseModel):
        model_config = ConfigDict(frozen=True)

        value: int | str

    class HistoricalChildWire(BaseModel):
        model_config = ConfigDict(frozen=True)

        value: int

    child = SchemaFamily(
        model=ChildPayload,
        name=f"wire_coercion_{collection_kind}_child",
        versions=(
            SchemaVersion("1", wire_model=HistoricalChildWire),
            SchemaVersion("2"),
        ),
        version_metadata=None,
    )
    annotation = set[ChildPayload] if collection_kind == "set" else frozenset[ChildPayload]
    parent_model = create_model(
        f"WireCoercion{collection_kind.title()}Parent",
        children=(annotation, ...),
    )
    parent = SchemaFamily(
        model=parent_model,
        name=f"wire_coercion_{collection_kind}_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        nested=(NestedFamily("children", child, matching_labels()),),
        version_metadata=None,
    )
    values = (ChildPayload(value=1), ChildPayload(value="1"))
    current_collection = set(values) if collection_kind == "set" else frozenset(values)

    with pytest.raises(InvalidMigrationError, match="cannot preserve set cardinality"):
        parent.dump(
            version="1",
            data=parent_model.model_validate({"children": current_collection}),
            include_version=False,
        )


def test_target_wire_cardinality_check_recurses_through_nested_families() -> None:
    class GrandchildPayload(BaseModel):
        model_config = ConfigDict(frozen=True)

        value: int | str

    class HistoricalGrandchildWire(BaseModel):
        model_config = ConfigDict(frozen=True)

        value: int

    grandchild = SchemaFamily(
        model=GrandchildPayload,
        name="recursive_wire_cardinality_grandchild",
        versions=(
            SchemaVersion("1", wire_model=HistoricalGrandchildWire),
            SchemaVersion("2"),
        ),
        version_metadata=None,
    )

    class ChildPayload(BaseModel):
        grandchildren: set[GrandchildPayload]

    child = SchemaFamily(
        model=ChildPayload,
        name="recursive_wire_cardinality_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        nested=(NestedFamily("grandchildren", grandchild, matching_labels()),),
        version_metadata=None,
    )

    class ParentPayload(BaseModel):
        child: ChildPayload

    parent = SchemaFamily(
        model=ParentPayload,
        name="recursive_wire_cardinality_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        nested=(NestedFamily("child", child, matching_labels()),),
        version_metadata=None,
    )
    current = ParentPayload(
        child=ChildPayload(
            grandchildren={
                GrandchildPayload(value=1),
                GrandchildPayload(value="1"),
            },
        ),
    )

    with pytest.raises(InvalidMigrationError, match="cannot preserve set cardinality"):
        parent.dump(version="1", data=current, include_version=False)


def test_nested_step_direction_follows_non_monotonic_child_mappings() -> None:
    child = SchemaFamily(
        model=NestedPlanChild,
        name="non_monotonic_direction_child",
        versions=(SchemaVersion("1"), SchemaVersion("2"), SchemaVersion("3")),
        version_metadata=None,
    )
    parent = SchemaFamily(
        model=NestedPlanParent,
        name="non_monotonic_direction_parent",
        versions=(SchemaVersion("a"), SchemaVersion("b"), SchemaVersion("c")),
        nested=(
            NestedFamily(
                "child",
                child,
                {"a": "2", "b": "1", "c": "3"},
            ),
        ),
        version_metadata=None,
    )

    validation_steps = tuple(
        step for step in parent.plan_validation("a").steps if step.kind == "nested"
    )
    assert [
        (step.source_version, step.target_version, step.direction, step.semantics)
        for step in validation_steps
    ] == [
        ("2", "1", "downgrade", "exact"),
        ("1", "3", "upgrade", "not_applicable"),
    ]

    render = parent.plan_render("a")
    render_steps = tuple(step for step in render.steps if step.kind == "nested")
    assert [
        (step.source_version, step.target_version, step.direction, step.semantics)
        for step in render_steps
    ] == [
        ("3", "1", "downgrade", "exact"),
        ("1", "2", "upgrade", "not_applicable"),
    ]
    assert render.semantics == "exact"


def test_validation_preflights_every_non_monotonic_child_route() -> None:
    available_upgrade = _CallableProbe("available-upgrade")
    available_downgrade = _CallableProbe("available-downgrade")
    unavailable_upgrade = _CallableProbe("unavailable-upgrade")

    class AvailableChild(BaseModel):
        value: int

    available = SchemaFamily(
        model=AvailableChild,
        name="validation_preflight_available_child",
        versions=(SchemaVersion("1"), SchemaVersion("2"), SchemaVersion("3")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                upgrade=available_upgrade,
                downgrade=available_downgrade,
                downgrade_semantics="exact",
            ),
        ),
        version_metadata=None,
    )

    class UnavailableChild(BaseModel):
        value: int

    unavailable = SchemaFamily(
        model=UnavailableChild,
        name="validation_preflight_unavailable_child",
        versions=(SchemaVersion("1"), SchemaVersion("2"), SchemaVersion("3")),
        transitions=(VersionTransition("1", "2", upgrade=unavailable_upgrade),),
        version_metadata=None,
    )

    class ParentPayload(BaseModel):
        available: AvailableChild
        unavailable: UnavailableChild

    first_parent_upgrade = _CallableProbe("first-parent-upgrade")
    second_parent_upgrade = _CallableProbe("second-parent-upgrade")
    parent = SchemaFamily(
        model=ParentPayload,
        name="validation_preflight_non_monotonic_parent",
        versions=(SchemaVersion("a"), SchemaVersion("b"), SchemaVersion("c")),
        transitions=(
            VersionTransition("a", "b", upgrade=first_parent_upgrade),
            VersionTransition("b", "c", upgrade=second_parent_upgrade),
        ),
        nested=(
            NestedFamily(
                "available",
                available,
                {"a": "2", "b": "1", "c": "3"},
            ),
            NestedFamily(
                "unavailable",
                unavailable,
                {"a": "2", "b": "1", "c": "3"},
            ),
        ),
        version_metadata=None,
    )

    plan = parent.plan_validation("a")
    nested_steps = tuple(step for step in plan.steps if step.kind == "nested")
    assert [
        (step.schema_path, step.source_version, step.target_version, step.direction, step.semantics)
        for step in nested_steps[:2]
    ] == [
        ("$.available", "2", "1", "downgrade", "exact"),
        ("$.unavailable", "2", "1", "downgrade", "unavailable"),
    ]

    with pytest.raises(IrreversibleTransitionError, match="has no complete route"):
        parent.validate(
            {
                "available": {"value": 1},
                "unavailable": {"value": 2},
            },
            version="a",
        )

    assert available_upgrade.calls == 0
    assert available_downgrade.calls == 0
    assert unavailable_upgrade.calls == 0
    assert first_parent_upgrade.calls == 0
    assert second_parent_upgrade.calls == 0


def test_impossible_render_route_fails_only_when_it_crosses_the_one_way_edge() -> None:
    upgrade = _CallableProbe("private-upgrade")
    family = _inventory_family(upgrade=upgrade, name="one_way")

    inventory = family.describe()
    validation = family.plan_validation("1")
    reachable_render = family.plan_render("2")

    assert inventory.transitions[0].downgrade == "unavailable"
    assert "custom_transition" in {step.kind for step in validation.steps}
    assert tuple(step.kind for step in reachable_render.steps) == (
        "current_validation",
        "implicit_identity",
        "metadata",
        "wire_validation",
        "serialization",
    )
    with pytest.raises(IrreversibleTransitionError) as error:
        family.plan_render("1")

    message = str(error.value)
    assert "one_way" in message
    assert "'2' -> '1'" in message
    assert "private-upgrade" not in message
    assert upgrade.calls == 0


@pytest.mark.parametrize("method", ["plan_validation", "plan_render"])
def test_plan_version_arguments_remain_strict_and_typed(method: str) -> None:
    family = _structural_family(name=f"strict_{method}")
    planner = getattr(family, method)

    with pytest.raises(UnknownSchemaVersionError):
        planner(cast(Any, 1))
    with pytest.raises(UnknownSchemaVersionError):
        planner("unknown")


def test_inventory_and_plans_are_json_safe_and_do_not_leak_private_objects() -> None:
    upgrade = _CallableProbe("CALLABLE_SECRET")
    factory = _FactoryProbe("FACTORY_SECRET")
    family = SchemaFamily(
        model=PrivacyConfig,
        name="privacy",
        versions=(
            SchemaVersion(
                "1",
                patches=(
                    field_default("token", "DEFAULT_SECRET"),
                    field_default("values", default_factory=factory),
                ),
            ),
            SchemaVersion("2"),
        ),
        transitions=(VersionTransition("1", "2", upgrade=upgrade),),
        version_metadata=VersionMetadata(("private", "version")),
    )

    inventory = family.describe()
    plan = family.plan_validation("1")
    serialized_records = (
        json.dumps(inventory.to_dict(), allow_nan=False),
        json.dumps(plan.to_dict(), allow_nan=False),
        repr(inventory),
        repr(plan),
    )

    _assert_json_safe(inventory.to_dict())
    _assert_json_safe(plan.to_dict())
    assert all(
        marker not in rendered
        for rendered in serialized_records
        for marker in ("DEFAULT_SECRET", "FACTORY_SECRET", "CALLABLE_SECRET", "0x")
    )
    assert upgrade.calls == 0
    assert factory.calls == 0
    assert inventory.versions[0].projections == (
        ProjectionDescription("default", "token", "token", True),
        ProjectionDescription("default", "values", "values", True),
    )

    mutable_copy = inventory.to_dict()
    versions = cast(list[dict[str, Any]], mutable_copy["versions"])
    versions[0]["label"] = "mutated"
    fresh_versions = cast(list[dict[str, Any]], inventory.to_dict()["versions"])
    assert fresh_versions[0]["label"] == "1"


def test_equivalent_declarations_have_stable_ids_without_callable_identity() -> None:
    first_callable = _CallableProbe("first")
    second_callable = _CallableProbe("second")
    first = _inventory_family(upgrade=first_callable)
    second = _inventory_family(upgrade=second_callable)

    first_plan = first.plan_validation("1")
    second_plan = second.plan_validation("1")

    assert first.describe() == second.describe()
    assert first_plan == second_plan
    assert tuple(step.id for step in first_plan.steps) == tuple(
        step.id for step in second_plan.steps
    )
    assert (
        first_plan.steps[5].id
        == "pv1-debe514195ae9a040548007eaea33abcd83a7c84ec8a9b24653bdfb28b15740d"
    )
    assert first_callable.calls == 0
    assert second_callable.calls == 0


def test_plan_json_is_deterministic_across_processes() -> None:
    repository = Path(__file__).resolve().parents[2]
    local = json.dumps(
        _inventory_family().plan_validation("1").to_dict(),
        separators=(",", ":"),
    )
    script = (
        "import json\n"
        "from tests.unit.test_inventory_and_plans import _inventory_family\n"
        "print(json.dumps("
        "_inventory_family().plan_validation('1').to_dict(),"
        "separators=(',', ':')))\n"
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == local


def test_nested_plan_json_is_deterministic_across_processes() -> None:
    repository = Path(__file__).resolve().parents[2]
    family = _deterministic_nested_family()
    local = json.dumps(
        {
            "validation": family.plan_validation("1").to_dict(),
            "render": family.plan_render("1").to_dict(),
        },
        separators=(",", ":"),
    )
    script = (
        "import json\n"
        "from tests.unit.test_inventory_and_plans import _deterministic_nested_family\n"
        "family = _deterministic_nested_family()\n"
        "print(json.dumps({"
        "'validation': family.plan_validation('1').to_dict(),"
        "'render': family.plan_render('1').to_dict(),"
        "}, separators=(',', ':')))\n"
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == local
    assert any(step.kind == "nested" for step in family.plan_validation("1").steps)
    assert any(step.kind == "nested" for step in family.plan_render("1").steps)


def test_step_ids_resist_sanitized_family_and_label_collisions() -> None:
    dotted = SchemaFamily(
        model=CollisionConfig,
        name="plan.family",
        versions=(SchemaVersion("1.0"), SchemaVersion("1-0")),
    )
    dashed = SchemaFamily(
        model=CollisionConfig,
        name="plan-family",
        versions=(SchemaVersion("1.0"), SchemaVersion("1-0")),
    )

    dotted_ids = tuple(step.id for step in dotted.plan_validation("1.0").steps)
    dashed_ids = tuple(step.id for step in dashed.plan_validation("1.0").steps)

    assert len(set(dotted_ids)) == len(dotted_ids)
    assert len(set(dashed_ids)) == len(dashed_ids)
    assert set(dotted_ids).isdisjoint(dashed_ids)


def test_metadata_schema_paths_distinguish_literal_and_nested_fields() -> None:
    literal = SchemaFamily(
        model=CollisionConfig,
        name="metadata_path",
        versions=(SchemaVersion("1"),),
        version_metadata=VersionMetadata("meta.version", owner="family"),
    )
    nested = SchemaFamily(
        model=CollisionConfig,
        name="metadata_path",
        versions=(SchemaVersion("1"),),
        version_metadata=VersionMetadata(("meta", "version"), owner="family"),
    )

    literal_step = literal.plan_validation("1").steps[0]
    nested_step = nested.plan_validation("1").steps[0]

    assert literal_step.schema_path == '$["meta.version"]'
    assert nested_step.schema_path == "$.meta.version"
    assert literal_step.id != nested_step.id


def test_inspection_freezes_legacy_migration_registration() -> None:
    family = SchemaFamily(
        model=CollisionConfig,
        name="legacy_inspection",
        versions=(SchemaVersion("1"), SchemaVersion("2"), SchemaVersion("3")),
    )
    first_upgrade = _CallableProbe("first-upgrade")
    migration(family, "1", "2")(first_upgrade)

    assert family.describe().transitions[0].upgrade == "custom"
    assert first_upgrade.calls == 0

    with pytest.raises(InvalidMigrationError, match="after.*compiled"):
        migration(family, "2", "3")


def test_concurrent_inspection_publishes_one_cached_side_effect_free_catalog() -> None:
    class ConcurrentConfig(BaseModel):
        value: int = 1

    family = SchemaFamily(
        model=ConcurrentConfig,
        name="concurrent_inspection",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    )
    schema_before = ConcurrentConfig.model_json_schema()
    barrier = Barrier(8)

    with pytest.raises(SchemaFamilySelectionError):
        validate_versioned(ConcurrentConfig, {"schema_version": "1"})

    def inspect_family(_: int) -> tuple[SchemaInventory, ConversionPlan, ConversionPlan]:
        barrier.wait()
        return (
            family.describe(),
            family.plan_validation("1"),
            family.plan_render("1"),
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(inspect_family, range(8)))

    first = results[0]
    assert all(
        inventory is first[0] and validation is first[1] and render is first[2]
        for inventory, validation, render in results
    )
    assert ConcurrentConfig.model_json_schema() == schema_before
    with pytest.raises(SchemaFamilySelectionError):
        validate_versioned(ConcurrentConfig, {"schema_version": "1"})
