from __future__ import annotations

from typing import Any, assert_type

from pydantic import BaseModel

from pydantic_versions import (
    ConversionPlan,
    DuplicateSchemaVersionError,
    FieldDefault,
    FieldRemoved,
    FieldRenamed,
    InvalidMigrationError,
    IrreversibleTransitionError,
    JsonValue,
    MatchingLabels,
    MissingSchemaVersionError,
    NestedFamily,
    NestedFamilyDescription,
    PlanStep,
    ProjectionDescription,
    SchemaCompilationError,
    SchemaFamily,
    SchemaFamilySelectionError,
    SchemaInventory,
    SchemaVersion,
    SchemaVersionError,
    StepKind,
    StepSemantics,
    TransitionData,
    TransitionDescription,
    TransitionFunc,
    UnknownSchemaVersionError,
    UnsupportedWireModelError,
    VersionDescription,
    VersionedValidation,
    VersionMetadata,
    VersionPatch,
    VersionPath,
    VersionTransition,
    __version__,
    dump_versioned,
    field_default,
    field_removed,
    field_renamed,
    matching_labels,
    migration,
    model_for_version,
    schema_version,
    schema_versions,
    validate_versioned,
    versioned_schema,
)


class AppConfig(BaseModel):
    timeout: float = 10.0


def upgrade_v1(data: dict[str, Any]) -> dict[str, Any]:
    return data


transition_data: TransitionData = {"version": "1"}
transition_func: TransitionFunc = upgrade_v1
version_path: VersionPath = ("metadata", "schema_version")
assert_type(transition_data, dict[str, Any])
assert_type(__version__, str)


class SingleVersionDecoratorConfig(BaseModel):
    timeout: float = 10.0


single_version_decorated = schema_version("1")(SingleVersionDecoratorConfig)
assert_type(single_version_decorated, type[SingleVersionDecoratorConfig])


class MultipleVersionDecoratorConfig(BaseModel):
    timeout: float = 10.0


multiple_versions_decorated = schema_versions(("1", "2"))(MultipleVersionDecoratorConfig)
assert_type(multiple_versions_decorated, type[MultipleVersionDecoratorConfig])


class VersionedDecoratorConfig(BaseModel):
    timeout: float = 10.0


versioned_decorated = versioned_schema(
    name="typed_decorator_config",
    versions=("1",),
    current="1",
)(VersionedDecoratorConfig)
assert_type(versioned_decorated, type[VersionedDecoratorConfig])


migration_family: SchemaFamily[AppConfig] = SchemaFamily(
    model=AppConfig,
    name="typed_migration_family",
    versions=(SchemaVersion("1"), SchemaVersion("2")),
)
preserved_transition = migration(migration_family, "1", "2")(transition_func)
assert_type(preserved_transition(transition_data), dict[str, Any])


patch: VersionPatch = field_default("timeout", 5.0)
default_patch: FieldDefault = field_default("timeout", 5.0)
removed_patch: FieldRemoved = field_removed("timeout")
renamed_patch: FieldRenamed = field_renamed("timeout", "request_timeout")
labels: MatchingLabels = matching_labels()
metadata = VersionMetadata(path=version_path, owner="family")
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
nested: NestedFamily = NestedFamily(path="child", family=family, versions=labels)
json_value: JsonValue = {"version": "1"}
assert_type(default_patch, FieldDefault)
assert_type(removed_patch, FieldRemoved)
assert_type(renamed_patch, FieldRenamed)
assert_type(nested, NestedFamily)
assert_type(metadata, VersionMetadata)

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
schema_error: type[Exception] = SchemaVersionError
unsupported_wire_error: type[SchemaCompilationError] = UnsupportedWireModelError
selection_error: type[Exception] = SchemaFamilySelectionError
irreversible_error: type[Exception] = IrreversibleTransitionError
missing_error: type[Exception] = MissingSchemaVersionError
unknown_error: type[Exception] = UnknownSchemaVersionError
duplicate_error: type[Exception] = DuplicateSchemaVersionError
invalid_migration_error: type[Exception] = InvalidMigrationError
