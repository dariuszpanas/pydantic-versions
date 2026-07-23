from __future__ import annotations

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
    DuplicateSchemaVersionError,
    FieldDefault,
    InvalidMigrationError,
    NestedFamily,
    SchemaCompilationError,
    SchemaFamily,
    SchemaFamilySelectionError,
    SchemaVersion,
    SchemaVersionError,
    UnknownSchemaVersionError,
    VersionMetadata,
    VersionTransition,
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


def _identity(data: dict[str, Any]) -> dict[str, Any]:
    return data


def test_external_family_versions_model_from_separate_module() -> None:
    from tests.fixtures.external_family.models import ExternalConfig

    schema_before = ExternalConfig.model_json_schema()

    from tests.fixtures.external_family.schema_history import EXTERNAL_CONFIG_SCHEMA

    historical = EXTERNAL_CONFIG_SCHEMA.model_for("1")
    result = EXTERNAL_CONFIG_SCHEMA.validate({"schema_version": "1", "retries": 4})

    assert result.source_model.__class__ is historical
    assert result.source_model.model_dump() == {
        "timeout": 5.0,
        "retries": 4,
        "schema_version": "1",
    }
    assert result.current_model == ExternalConfig(timeout=5.0, retries=4)
    assert model_for_version(EXTERNAL_CONFIG_SCHEMA, "1") is historical
    assert (
        validate_versioned(
            EXTERNAL_CONFIG_SCHEMA,
            {"schema_version": "1", "retries": 4},
        ).current_model
        == result.current_model
    )
    assert dump_versioned(EXTERNAL_CONFIG_SCHEMA, version="1") == {
        "timeout": 5.0,
        "retries": 3,
        "schema_version": "1",
    }
    assert ExternalConfig.model_json_schema() == schema_before
    assert "__pydantic_versions_pending__" not in ExternalConfig.__dict__


@pytest.mark.parametrize("history_first", [False, True])
def test_external_history_import_order_does_not_change_model(history_first: bool) -> None:
    repository = Path(__file__).resolve().parents[2]
    if history_first:
        imports = (
            "from tests.fixtures.external_family.schema_history "
            "import EXTERNAL_CONFIG_SCHEMA\n"
            "from tests.fixtures.external_family.models import ExternalConfig\n"
            "before = ExternalConfig.model_json_schema()\n"
        )
    else:
        imports = (
            "from tests.fixtures.external_family.models import ExternalConfig\n"
            "before = ExternalConfig.model_json_schema()\n"
            "from tests.fixtures.external_family.schema_history "
            "import EXTERNAL_CONFIG_SCHEMA\n"
        )
    script = (
        imports
        + "after = ExternalConfig.model_json_schema()\n"
        + "assert before == after\n"
        + "assert EXTERNAL_CONFIG_SCHEMA.validate("
        + "{'schema_version': '1'}).current_model.timeout == 5.0\n"
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_external_family_is_not_an_implicit_default() -> None:
    class NoDefaultConfig(BaseModel):
        value: int = 1

    family = SchemaFamily(
        model=NoDefaultConfig,
        name="no_default",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    )

    assert family.model_for("1") is model_for_version(family, "1")
    with pytest.raises(SchemaFamilySelectionError, match="no explicit default"):
        model_for_version(NoDefaultConfig, "1")


def test_explicit_default_enables_model_only_calls() -> None:
    class DefaultConfig(BaseModel):
        value: int = 2

    family = SchemaFamily(
        model=DefaultConfig,
        name="selected",
        versions=(
            SchemaVersion("1", patches=(field_default("value", 1),)),
            SchemaVersion("2"),
        ),
    )

    assert family.as_default() is family
    assert family.as_default() is family
    assert model_for_version(DefaultConfig, "1") is family.model_for("1")
    assert validate_versioned(DefaultConfig, {"schema_version": "1"}).current_model == (
        DefaultConfig(value=1)
    )


def test_second_default_is_rejected_without_replacing_first() -> None:
    class SharedDefaultConfig(BaseModel):
        value: int = 3

    first = SchemaFamily(
        model=SharedDefaultConfig,
        name="first",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    ).as_default()
    second = SchemaFamily(
        model=SharedDefaultConfig,
        name="second",
        versions=(SchemaVersion("legacy"), SchemaVersion("current")),
    )

    with pytest.raises(SchemaFamilySelectionError, match="already has explicit default"):
        second.as_default()

    assert model_for_version(SharedDefaultConfig, "1") is first.model_for("1")
    with pytest.raises(UnknownSchemaVersionError):
        model_for_version(SharedDefaultConfig, "legacy")


def test_two_families_share_a_model_without_sharing_state() -> None:
    class SharedConfig(BaseModel):
        value: int = 10
        selected_by: str = "current"

    def select_public(data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "selected_by": "public"}

    def select_internal(data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "selected_by": "internal"}

    public = SchemaFamily(
        model=SharedConfig,
        name="public",
        versions=(
            SchemaVersion("1", patches=(field_default("value", 1),)),
            SchemaVersion("2"),
        ),
        transitions=(VersionTransition("1", "2", upgrade=select_public),),
    )
    internal = SchemaFamily(
        model=SharedConfig,
        name="internal",
        versions=(
            SchemaVersion("1", patches=(field_default("value", 2),)),
            SchemaVersion("2"),
        ),
        transitions=(VersionTransition("1", "2", upgrade=select_internal),),
    )

    public_model = public.model_for("1")
    internal_model = internal.model_for("1")

    assert public_model is public.model_for("1")
    assert internal_model is internal.model_for("1")
    assert public_model is not internal_model
    assert public_model.__name__ != internal_model.__name__
    assert public_model().model_dump()["value"] == 1
    assert internal_model().model_dump()["value"] == 2
    assert public.validate({"schema_version": "1"}).current_model.selected_by == "public"
    assert internal.validate({"schema_version": "1"}).current_model.selected_by == "internal"


def test_declarations_are_frozen_and_defensively_copied() -> None:
    class CopiedConfig(BaseModel):
        value: int = 10

    patch_declarations = [field_default("value", 1)]
    version_one = SchemaVersion("1", patches=cast(Any, patch_declarations))
    versions = [version_one, SchemaVersion("2")]
    transition_declarations = [VersionTransition("1", "2", upgrade=_identity)]
    family = SchemaFamily(
        model=CopiedConfig,
        name="copied",
        versions=versions,
        transitions=transition_declarations,
    )

    patch_declarations.append(field_removed("value"))
    versions.append(SchemaVersion("3"))
    transition_declarations.clear()

    assert tuple(version.label for version in family.versions) == ("1", "2")
    assert len(family.transitions) == 1
    assert family.model_for("1")().model_dump()["value"] == 1
    with pytest.raises(FrozenInstanceError):
        cast(Any, version_one).label = "changed"
    with pytest.raises(FrozenInstanceError):
        cast(Any, family.transitions[0]).source = "changed"


def test_compilation_snapshots_mutable_field_defaults() -> None:
    class MutableDefaultConfig(BaseModel):
        values: list[int] = Field(default_factory=list)

    caller_default: list[int] = []
    family = SchemaFamily(
        model=MutableDefaultConfig,
        name="mutable_default",
        versions=(
            SchemaVersion("1", patches=(field_default("values", caller_default),)),
            SchemaVersion("2"),
        ),
    ).compile()

    caller_default.append(9)

    assert family.model_for("1")().model_dump()["values"] == []
    compiled_default = family._compiled_family().versions[0].projection.fields[0].default
    assert compiled_default is not None
    assert compiled_default.default == []


def test_nested_mapping_declaration_copies_the_caller_mapping() -> None:
    class ParentConfig(BaseModel):
        value: int = 1

    class ChildConfig(BaseModel):
        value: int = 1

    child = SchemaFamily(
        model=ChildConfig,
        name="child",
        versions=(SchemaVersion("legacy"), SchemaVersion("current")),
    )
    mapping = {"1": "legacy", "2": "current"}
    declaration = NestedFamily(path="child", family=child, versions=mapping)

    mapping["2"] = "legacy"

    assert dict(cast(Any, declaration.versions)) == {
        "1": "legacy",
        "2": "current",
    }
    assert isinstance(matching_labels(), type(matching_labels()))
    assert VersionMetadata(("meta", "version")).to_dict() == {
        "path": ["meta", "version"],
        "owner": "family",
    }


def test_compile_is_lazy_idempotent_and_cache_stable() -> None:
    class LazyConfig(BaseModel):
        value: int = 1

    family = SchemaFamily(
        model=LazyConfig,
        name="lazy",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    )

    assert family._compiled is None
    assert family.compile() is family
    first = family.model_for("1")

    assert family.compile() is family
    assert family.model_for("1") is first


def test_lazy_compilation_allows_a_later_forward_reference_rebuild() -> None:
    class ForwardConfig(BaseModel):
        child: ForwardValue

    family = SchemaFamily(
        model=ForwardConfig,
        name="forward_reference",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    )

    assert family._compiled is None

    class ForwardValue(BaseModel):
        value: int

    ForwardConfig.model_rebuild(_types_namespace={"ForwardValue": ForwardValue})

    result = family.validate(
        {"schema_version": "1", "child": {"value": 3}},
    )

    assert result.current_model.child == ForwardValue(value=3)


def test_compile_is_thread_safe_and_publishes_one_model_identity() -> None:
    class ConcurrentConfig(BaseModel):
        value: int = 1

    family = SchemaFamily(
        model=ConcurrentConfig,
        name="concurrent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    )
    workers = 8
    barrier = Barrier(workers)

    def compile_model() -> type[BaseModel]:
        barrier.wait()
        return family.compile().model_for("1")

    with ThreadPoolExecutor(max_workers=workers) as executor:
        models = tuple(executor.map(lambda _: compile_model(), range(workers)))

    assert len({id(model) for model in models}) == 1


def test_private_compiled_state_is_frozen() -> None:
    class FrozenConfig(BaseModel):
        value: int = 1

    family = SchemaFamily(
        model=FrozenConfig,
        name="frozen",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    )
    compiled = family._compiled_family()

    assert isinstance(compiled.versions, tuple)
    assert isinstance(compiled.transitions, tuple)
    with pytest.raises(FrozenInstanceError):
        cast(Any, compiled).name = "changed"
    with pytest.raises(FrozenInstanceError):
        cast(Any, compiled.versions[0]).model = FrozenConfig


def test_sanitized_label_collisions_have_distinct_generated_identities() -> None:
    class CollisionConfig(BaseModel):
        value: int = 1

    family = SchemaFamily(
        model=CollisionConfig,
        name="collision",
        versions=(
            SchemaVersion("1.0"),
            SchemaVersion("1-0"),
            SchemaVersion("2"),
        ),
    )

    dotted = family.model_for("1.0")
    dashed = family.model_for("1-0")

    assert dotted is not dashed
    assert dotted.__name__ != dashed.__name__
    assert dotted().model_dump()["schema_version"] == "1.0"
    assert dashed().model_dump()["schema_version"] == "1-0"


def test_schema_family_rejects_malformed_names_labels_and_sequences() -> None:
    class StrictConfig(BaseModel):
        value: int = 1

    versions = (SchemaVersion("1"), SchemaVersion("2"))

    with pytest.raises(SchemaCompilationError, match="at least one version"):
        SchemaFamily(model=StrictConfig, name="strict", versions=())
    with pytest.raises(SchemaCompilationError, match="non-empty string"):
        SchemaFamily(model=StrictConfig, name="", versions=versions)
    with pytest.raises(SchemaCompilationError, match="must be a sequence"):
        SchemaFamily(model=StrictConfig, name="strict", versions=cast(Any, "12"))
    with pytest.raises(SchemaCompilationError, match="non-empty string"):
        SchemaFamily(
            model=StrictConfig,
            name="strict",
            versions=(SchemaVersion(cast(Any, 1)), SchemaVersion("2")),
        )
    with pytest.raises(DuplicateSchemaVersionError):
        SchemaFamily(
            model=StrictConfig,
            name="strict",
            versions=(SchemaVersion("1"), SchemaVersion("1")),
        )
    with pytest.raises(UnknownSchemaVersionError):
        SchemaFamily(
            model=StrictConfig,
            name="strict",
            versions=versions,
            missing_version="legacy",
        )
    with pytest.raises(SchemaCompilationError, match="non-empty string"):
        SchemaFamily(
            model=StrictConfig,
            name="strict",
            versions=versions,
            missing_version=cast(Any, 1),
        )


def test_compatibility_declarations_reject_coercion_and_nonterminal_current() -> None:
    with pytest.raises(SchemaCompilationError, match="must be a sequence"):
        versioned_schema(name="string", versions=cast(Any, "12"), current="2")
    with pytest.raises(SchemaCompilationError, match="must be a sequence"):
        schema_versions(cast(Any, "12"))
    with pytest.raises(SchemaCompilationError, match="non-empty string"):
        schema_version(cast(Any, 1))
    with pytest.raises(SchemaCompilationError, match="final declared label"):
        versioned_schema(name="nonterminal", versions=("1", "2"), current="1")


@pytest.mark.parametrize(
    "transition, error",
    [
        (VersionTransition("0", "1", upgrade=_identity), SchemaCompilationError),
        (VersionTransition("2", "1", upgrade=_identity), SchemaCompilationError),
        (VersionTransition("1", "3", upgrade=_identity), SchemaCompilationError),
        (VersionTransition("1", "2"), SchemaCompilationError),
        (
            VersionTransition("1", "2", upgrade=cast(Any, "not-callable")),
            SchemaCompilationError,
        ),
        (
            VersionTransition("1", "2", upgrade=_identity, downgrade_semantics="exact"),
            SchemaCompilationError,
        ),
        (
            VersionTransition("1", "2", downgrade=_identity),
            SchemaCompilationError,
        ),
    ],
)
def test_transition_topology_is_validated(
    transition: VersionTransition,
    error: type[Exception],
) -> None:
    class TransitionConfig(BaseModel):
        value: int = 1

    with pytest.raises(error):
        SchemaFamily(
            model=TransitionConfig,
            name="transitions",
            versions=(SchemaVersion("1"), SchemaVersion("2"), SchemaVersion("3")),
            transitions=(transition,),
        )


def test_duplicate_transition_edge_is_rejected() -> None:
    class DuplicateTransitionConfig(BaseModel):
        value: int = 1

    with pytest.raises(DuplicateSchemaVersionError):
        SchemaFamily(
            model=DuplicateTransitionConfig,
            name="duplicate_transitions",
            versions=(SchemaVersion("1"), SchemaVersion("2")),
            transitions=(
                VersionTransition("1", "2", upgrade=_identity),
                VersionTransition("1", "2", upgrade=_identity),
            ),
        )


def test_future_declarations_are_rejected_instead_of_ignored() -> None:
    class FutureConfig(BaseModel):
        value: int = 1

    class HistoricalConfig(BaseModel):
        value: str

    wire_family = SchemaFamily(
        model=FutureConfig,
        name="wire_future",
        versions=(SchemaVersion("1", wire_model=HistoricalConfig), SchemaVersion("2")),
    )
    nested_family = SchemaFamily(
        model=FutureConfig,
        name="nested_future",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        nested=(
            NestedFamily(
                path="value",
                family=wire_family,
                versions={"1": "1", "2": "2"},
            ),
        ),
    )
    downgrade_family = SchemaFamily(
        model=FutureConfig,
        name="downgrade_future",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                downgrade=_identity,
                downgrade_semantics="exact",
            ),
        ),
    )

    with pytest.raises(SchemaCompilationError, match="Explicit wire models"):
        wire_family.compile()
    with pytest.raises(SchemaCompilationError, match="nested family"):
        nested_family.compile()
    with pytest.raises(SchemaCompilationError, match="Downgrade execution"):
        downgrade_family.compile()


def test_current_and_mutually_exclusive_wire_declarations_are_rejected() -> None:
    class CurrentConfig(BaseModel):
        value: int = 1

    class HistoricalConfig(BaseModel):
        value: int = 1

    with pytest.raises(SchemaCompilationError, match="cannot be patched"):
        SchemaFamily(
            model=CurrentConfig,
            name="patched_current",
            versions=(SchemaVersion("1", patches=(field_default("value", 2),)),),
        )
    with pytest.raises(SchemaCompilationError, match="cannot be patched"):
        SchemaFamily(
            model=CurrentConfig,
            name="wire_current",
            versions=(SchemaVersion("1", wire_model=HistoricalConfig),),
        )
    with pytest.raises(SchemaCompilationError, match="combine patches"):
        SchemaFamily(
            model=CurrentConfig,
            name="mixed_wire",
            versions=(
                SchemaVersion(
                    "1",
                    patches=(field_default("value", 2),),
                    wire_model=HistoricalConfig,
                ),
                SchemaVersion("2"),
            ),
        )


def test_conflicting_patch_declarations_are_rejected() -> None:
    class PatchedConfig(BaseModel):
        value: int = 1
        other: int = 2

    with pytest.raises(SchemaCompilationError, match="conflicting patches"):
        SchemaFamily(
            model=PatchedConfig,
            name="conflicting",
            versions=(
                SchemaVersion(
                    "1",
                    patches=(field_default("value", 3), field_removed("value")),
                ),
                SchemaVersion("2"),
            ),
        )
    with pytest.raises(SchemaVersionError, match="Rename target"):
        SchemaFamily(
            model=PatchedConfig,
            name="rename_collision",
            versions=(
                SchemaVersion("1", patches=(field_renamed("value", "other"),)),
                SchemaVersion("2"),
            ),
        )
    with pytest.raises(SchemaVersionError, match="unknown field"):
        SchemaFamily(
            model=PatchedConfig,
            name="unknown_field",
            versions=(
                SchemaVersion("1", patches=(field_removed("missing"),)),
                SchemaVersion("2"),
            ),
        )


@pytest.mark.parametrize(
    "patches",
    [
        (field_renamed("a", "c"), field_renamed("b", "a")),
        (field_renamed("b", "a"), field_renamed("a", "c")),
    ],
)
def test_snapshot_renames_are_order_independent(
    patches: tuple[Any, ...],
) -> None:
    class RenamedConfig(BaseModel):
        a: int
        b: int

    family = SchemaFamily(
        model=RenamedConfig,
        name="rename_snapshot",
        versions=(SchemaVersion("1", patches=patches), SchemaVersion("2")),
    )

    result = family.validate({"schema_version": "1", "c": 10, "a": 20})

    assert result.current_model == RenamedConfig(a=10, b=20)
    assert family.dump(
        version="1",
        data=RenamedConfig(a=30, b=40),
    ) == {"c": 30, "a": 40, "schema_version": "1"}


def test_rename_can_reuse_a_removed_field_name() -> None:
    class ReusedNameConfig(BaseModel):
        a: int
        b: int = 2

    family = SchemaFamily(
        model=ReusedNameConfig,
        name="reuse_removed_name",
        versions=(
            SchemaVersion(
                "1",
                patches=(field_removed("b"), field_renamed("a", "b")),
            ),
            SchemaVersion("2"),
        ),
    )

    assert family.validate({"schema_version": "1", "b": 5}).current_model == (
        ReusedNameConfig(a=5, b=2)
    )
    assert family.dump(
        version="1",
        data=ReusedNameConfig(a=7, b=9),
    ) == {"b": 7, "schema_version": "1"}


@pytest.mark.parametrize(
    "patch",
    [
        FieldDefault("values", default_factory=cast(Any, 42), has_default=False),
        FieldDefault("values", default=[1], default_factory=list),
        FieldDefault("values", default=[1], default_factory=list, has_default=False),
        FieldDefault("values", has_default=cast(Any, 1)),
    ],
)
def test_malformed_field_default_records_fail_during_declaration(
    patch: FieldDefault,
) -> None:
    class DefaultRecordConfig(BaseModel):
        values: list[int] = Field(default_factory=list)

    with pytest.raises(SchemaCompilationError, match="Field default"):
        SchemaFamily(
            model=DefaultRecordConfig,
            name="invalid_default_record",
            versions=(SchemaVersion("1", patches=(patch,)), SchemaVersion("2")),
        )


def test_required_field_introduction_requires_upgrade_on_that_edge() -> None:
    class RequiredConfig(BaseModel):
        required: str

    invalid = SchemaFamily(
        model=RequiredConfig,
        name="required_invalid",
        versions=(
            SchemaVersion("1", patches=(field_removed("required"),)),
            SchemaVersion("2"),
        ),
    )

    with pytest.raises(SchemaCompilationError, match="without an upgrade"):
        invalid.compile()

    def add_required(data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "required": "created"}

    valid = SchemaFamily(
        model=RequiredConfig,
        name="required_valid",
        versions=(
            SchemaVersion("1", patches=(field_removed("required"),)),
            SchemaVersion("2"),
        ),
        transitions=(VersionTransition("1", "2", upgrade=add_required),),
    )

    assert valid.validate({"schema_version": "1"}).current_model.required == "created"


def test_every_accepted_upgrade_executes_once_and_in_order() -> None:
    class OrderedConfig(BaseModel):
        events: list[str] = Field(default_factory=list)

    def first(data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "events": [*data["events"], "1-2"]}

    def second(data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "events": [*data["events"], "2-3"]}

    family = SchemaFamily(
        model=OrderedConfig,
        name="ordered",
        versions=(SchemaVersion("1"), SchemaVersion("2"), SchemaVersion("3")),
        transitions=(
            VersionTransition("1", "2", upgrade=first),
            VersionTransition("2", "3", upgrade=second),
        ),
    )

    result = family.validate({"schema_version": "1", "events": []})

    assert result.current_model.events == ["1-2", "2-3"]
    assert result.migrations_applied == (("1", "2"), ("2", "3"))


def test_decorator_transitions_delegate_to_the_family_compiler() -> None:
    class DecoratedConfig(BaseModel):
        value: int = 1

    def double(data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "value": data["value"] * 2}

    decorator = versioned_schema(
        name="decorated_transition",
        versions=("1", "2"),
        current="2",
        transitions=(VersionTransition("1", "2", upgrade=double),),
    )
    returned = decorator(DecoratedConfig)

    assert returned is DecoratedConfig
    assert (
        validate_versioned(
            DecoratedConfig,
            {"schema_version": "1", "value": 3},
        ).current_model.value
        == 6
    )


def test_legacy_migration_must_be_adjacent_and_registered_before_compilation() -> None:
    @versioned_schema(name="legacy_builder", versions=("1", "2", "3"), current="3")
    class LegacyBuilderConfig(BaseModel):
        value: int = 1

    with pytest.raises(InvalidMigrationError, match="adjacent"):
        migration(LegacyBuilderConfig, "1", "3")

    @migration(LegacyBuilderConfig, "1", "2")
    def first(data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "value": data["value"] + 1}

    assert (
        validate_versioned(
            LegacyBuilderConfig,
            {"schema_version": "1", "value": 1},
        ).current_model.value
        == 2
    )

    with pytest.raises(InvalidMigrationError, match="after.*compiled"):
        migration(LegacyBuilderConfig, "2", "3")


def test_decorator_and_external_flat_declarations_are_equivalent() -> None:
    @versioned_schema(name="decorator_equivalent", versions=("1", "2"), current="2")
    @schema_version("1", patches=(field_default("value", 1),))
    class EquivalentConfig(BaseModel):
        value: int = 2

    external = SchemaFamily(
        model=EquivalentConfig,
        name="external_equivalent",
        versions=(
            SchemaVersion("1", patches=(field_default("value", 1),)),
            SchemaVersion("2"),
        ),
    )

    decorated_result = validate_versioned(EquivalentConfig, {"schema_version": "1"})
    external_result = external.validate({"schema_version": "1"})

    assert decorated_result.current_model == external_result.current_model
    assert model_for_version(EquivalentConfig, "1")().model_dump() == (
        external.model_for("1")().model_dump()
    )


def test_runtime_version_arguments_are_not_coerced() -> None:
    class RuntimeLabelConfig(BaseModel):
        value: int = 1

    family = SchemaFamily(
        model=RuntimeLabelConfig,
        name="runtime_labels",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    )

    with pytest.raises(UnknownSchemaVersionError, match="non-empty string"):
        family.model_for(cast(Any, 1))
    with pytest.raises(UnknownSchemaVersionError, match="non-empty string"):
        family.validate({"schema_version": 1})


def test_public_patch_records_are_exported() -> None:
    patch = field_default("value", 1)

    assert isinstance(patch, FieldDefault)
