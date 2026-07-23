from __future__ import annotations

from typing import Any, assert_type

from pydantic import BaseModel

from pydantic_versions import (
    ConversionPlan,
    IrreversibleTransitionError,
    NestedFamilyDescription,
    PlanStep,
    ProjectionDescription,
    SchemaCompilationError,
    SchemaFamily,
    SchemaFamilySelectionError,
    SchemaInventory,
    SchemaVersion,
    StepKind,
    StepSemantics,
    TransitionDescription,
    UnsupportedWireModelError,
    VersionDescription,
    VersionedValidation,
    VersionPatch,
    VersionTransition,
    dump_versioned,
    field_default,
    model_for_version,
    validate_versioned,
)


class AppConfig(BaseModel):
    timeout: float = 10.0


def upgrade_v1(data: dict[str, Any]) -> dict[str, Any]:
    return data


patch: VersionPatch = field_default("timeout", 5.0)
family: SchemaFamily[AppConfig] = SchemaFamily(
    model=AppConfig,
    name="app_config",
    versions=(
        SchemaVersion("1", patches=(patch,)),
        SchemaVersion("2"),
    ),
    transitions=(
        VersionTransition(
            "1", "2", upgrade=upgrade_v1, downgrade=lambda d: d, downgrade_semantics="exact"
        ),
    ),
)

assert_type(family.compile(), SchemaFamily[AppConfig])
assert_type(family.as_default(), SchemaFamily[AppConfig])
inventory: SchemaInventory = family.describe()
assert_type(inventory, SchemaInventory)
assert_type(inventory.versions, tuple[VersionDescription, ...])
assert_type(inventory.versions[0].projections, tuple[ProjectionDescription, ...])
assert_type(inventory.transitions, tuple[TransitionDescription, ...])
assert_type(inventory.nested, tuple[NestedFamilyDescription, ...])
validation_plan: ConversionPlan = family.plan_validation("1")
render_plan: ConversionPlan = family.plan_render("2")
assert_type(validation_plan, ConversionPlan)
assert_type(render_plan, ConversionPlan)
assert_type(validation_plan.steps, tuple[PlanStep, ...])
step_kind: StepKind = validation_plan.steps[0].kind
step_semantics: StepSemantics = validation_plan.steps[0].semantics
assert_type(step_kind, StepKind)
assert_type(step_semantics, StepSemantics)
assert_type(family.model_for("1"), type[BaseModel])
assert_type(family.validate({"schema_version": "1"}), VersionedValidation[AppConfig])
assert_type(model_for_version(family, "1"), type[BaseModel])
assert_type(
    validate_versioned(family, {"schema_version": "1"}),
    VersionedValidation[AppConfig],
)
assert_type(family.dump(version="1"), dict[str, Any])
assert_type(dump_versioned(family, version="1"), dict[str, Any])

compilation_error: type[Exception] = SchemaCompilationError
unsupported_wire_error: type[SchemaCompilationError] = UnsupportedWireModelError
selection_error: type[Exception] = SchemaFamilySelectionError
irreversible_error: type[Exception] = IrreversibleTransitionError
