from __future__ import annotations

import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path
from threading import Barrier
from typing import Any, cast

import pytest
from pydantic import BaseModel, Field

from pydantic_versions import (
    ConversionPlan,
    InvalidMigrationError,
    IrreversibleTransitionError,
    NestedFamilyDescription,
    PlanStep,
    ProjectionDescription,
    SchemaFamily,
    SchemaFamilySelectionError,
    SchemaInventory,
    SchemaVersion,
    TransitionDescription,
    UnknownSchemaVersionError,
    VersionDescription,
    VersionMetadata,
    VersionTransition,
    field_default,
    field_removed,
    field_renamed,
    migration,
    validate_versioned,
)


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
