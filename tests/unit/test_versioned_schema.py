from __future__ import annotations

import warnings
from collections.abc import Mapping
from typing import Any, cast

import pytest
from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_serializer,
)

from pydantic_versions import (
    DuplicateSchemaVersionError,
    InvalidMigrationError,
    IrreversibleTransitionError,
    MissingSchemaVersionError,
    NestedFamily,
    SchemaFamily,
    SchemaVersion,
    SchemaVersionError,
    UnknownSchemaVersionError,
    UnsupportedWireModelError,
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


@versioned_schema(
    name="app_config",
    versions=["1", "2", "3"],
    current="3",
    version_field="schema_version",
    missing_version="1",
)
@schema_version(
    "1",
    patches=[
        field_default("timeout", 5.0),
        field_removed("new_feature"),
        field_renamed("retries", "attempts"),
    ],
)
@schema_version("2", patches=[field_default("timeout", 8.0)])
class AppConfig(BaseModel):
    timeout: float = Field(default=10.0, gt=0)
    retries: int = 3
    new_feature: bool = False


@migration(AppConfig, "1", "2")
def migrate_v1_to_v2(data: dict) -> dict:
    data["new_feature"] = data["retries"] > 1
    return data


def _to_dict(data: Mapping[str, Any]) -> dict[str, Any]:
    return dict(data)


class PlainModel(BaseModel):
    value: str


def test_package_requires_registered_models() -> None:
    with pytest.raises(SchemaVersionError):
        model_for_version(PlainModel, "1")


def test_versioned_schema_rejects_pydantic_v1_models_at_registration() -> None:
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="Core Pydantic V1 functionality isn't compatible with Python 3.14 or greater.",
        )
        from pydantic.v1 import BaseModel as PydanticV1BaseModel

    class LegacyModel(PydanticV1BaseModel):
        value: str

    decorator = versioned_schema(name="legacy", versions=["1"], current="1")

    with pytest.raises(SchemaVersionError, match="Pydantic v2"):
        decorator(cast(Any, LegacyModel))


def test_schema_version_rejects_non_pydantic_models_at_registration() -> None:
    class NotPydantic:
        pass

    with pytest.raises(SchemaVersionError, match="Pydantic v2"):
        schema_version("1")(cast(Any, NotPydantic))


def test_model_for_version_applies_defaults_removals_renames_and_version_field() -> None:
    model_v1 = model_for_version(AppConfig, "1")

    config = model_v1.model_validate({"attempts": 4})

    assert config.model_dump() == {
        "timeout": 5.0,
        "attempts": 4,
        "schema_version": "1",
    }
    assert "new_feature" not in model_v1.model_fields
    assert "retries" not in model_v1.model_fields


def test_generated_models_keep_field_constraints() -> None:
    model_v1 = model_for_version(AppConfig, "1")

    with pytest.raises(ValidationError):
        model_v1.model_validate({"timeout": -1, "attempts": 1})


def test_validate_versioned_uses_embedded_version_and_applies_migrations() -> None:
    result = validate_versioned(AppConfig, {"schema_version": "1", "attempts": 2})

    assert result.source_version == "1"
    assert result.current_version == "3"
    assert result.source_model.model_dump()["timeout"] == 5.0
    assert result.current_model == AppConfig(timeout=5.0, retries=2, new_feature=True)
    assert result.migrations_applied == (("1", "2"),)


def test_validate_versioned_does_not_overwrite_a_conflicting_embedded_version() -> None:
    with pytest.raises(ValidationError):
        validate_versioned(
            AppConfig,
            {"schema_version": "3", "attempts": 1},
            version="1",
        )


def test_validate_versioned_uses_missing_version_fallback() -> None:
    result = validate_versioned(AppConfig, {"attempts": 1})

    assert result.source_version == "1"
    assert result.current_model == AppConfig(timeout=5.0, retries=1, new_feature=False)


def test_validate_versioned_handles_current_version() -> None:
    result = validate_versioned(AppConfig, {"schema_version": "3", "timeout": 11.0})

    assert result.current_model == AppConfig(timeout=11.0)
    assert result.migrations_applied == ()


def test_validate_versioned_normalizes_alias_paths_in_upgrade_output() -> None:
    @versioned_schema(
        name="alias_path_migration_target",
        versions=["1", "2"],
        current="2",
        missing_version="1",
    )
    @schema_version("1")
    class AliasPathMigrationTarget(BaseModel):
        value: int = Field(validation_alias=AliasPath("payload", "value"))

    @migration(AliasPathMigrationTarget, "1", "2")
    def migrate_alias_path(data: dict) -> dict:
        return {"payload": {"value": data["value"]}}

    result = validate_versioned(
        AliasPathMigrationTarget,
        {"schema_version": "1", "value": 4},
    )

    assert result.current_model == AliasPathMigrationTarget.model_validate(
        {"payload": {"value": 4}}
    )


def test_dump_versioned_renders_defaults_for_requested_schema() -> None:
    with pytest.raises(IrreversibleTransitionError):
        dump_versioned(AppConfig, version="1")


def test_dump_versioned_executes_explicit_downgrades_in_reverse_edge_order() -> None:
    order: list[str] = []

    def downgrade_to_one(data: dict[str, Any]) -> dict[str, Any]:
        order.append("3->2")
        return {"value": data["value"], "marker": data["marker"]}

    def downgrade_to_two(data: dict[str, Any]) -> dict[str, Any]:
        order.append("2->1")
        return {"value": data["value"] * 2, "marker": data["marker"]}

    @versioned_schema(
        name="downgrade_chain_render",
        versions=["1", "2", "3"],
        current="3",
        transitions=(
            VersionTransition(
                "1",
                "2",
                downgrade=downgrade_to_one,
                downgrade_semantics="lossy",
            ),
            VersionTransition(
                "2",
                "3",
                downgrade=downgrade_to_two,
                downgrade_semantics="exact",
            ),
        ),
    )
    @schema_version("1", patches=[field_renamed("value", "legacy_value")])
    class DowngradeChainRender(BaseModel):
        value: int = 10
        marker: int = 4

    result = dump_versioned(
        DowngradeChainRender,
        version="1",
        data=DowngradeChainRender(value=2, marker=5),
    )

    assert result["legacy_value"] == 4
    assert result["marker"] == 5
    assert result["schema_version"] == "1"
    assert order == ["2->1", "3->2"]


def test_dump_versioned_rejects_irreversible_render_routes() -> None:
    @versioned_schema(
        name="irreversible_chain_render",
        versions=["1", "2"],
        current="2",
    )
    class IrreversibleRenderConfig(BaseModel):
        value: int

    @migration(IrreversibleRenderConfig, "1", "2")
    def migrate_value_up(data: dict[str, Any]) -> dict[str, Any]:
        return {"value": data["value"]}

    with pytest.raises(IrreversibleTransitionError):
        dump_versioned(
            IrreversibleRenderConfig,
            version="1",
            data=IrreversibleRenderConfig(value=1),
        )


def test_explicit_wire_model_enables_typeful_historical_rendering_and_validation() -> None:
    class HistoricalTimeoutConfig(BaseModel):
        timeout: str

    def upgrade_timeout(data: dict[str, Any]) -> dict[str, Any]:
        return {"timeout": float(data["timeout"])}

    def downgrade_timeout(data: dict[str, Any]) -> dict[str, Any]:
        return {"timeout": str(data["timeout"])}

    @versioned_schema(
        name="historical_wire_type_change",
        versions=("1", "2"),
        current="2",
        transitions=(
            VersionTransition(
                "1",
                "2",
                upgrade=upgrade_timeout,
                downgrade=downgrade_timeout,
                downgrade_semantics="exact",
            ),
        ),
    )
    @schema_version("1", wire_model=HistoricalTimeoutConfig)
    class HistoricalTypeConfig(BaseModel):
        timeout: float = 2.5

    validated = validate_versioned(
        HistoricalTypeConfig,
        {"schema_version": "1", "timeout": "12.75"},
    )
    assert validated.source_model.model_dump()["timeout"] == "12.75"
    assert validated.current_model.timeout == 12.75

    rendered = dump_versioned(
        HistoricalTypeConfig,
        version="1",
        data=HistoricalTypeConfig(timeout=9.5),
    )

    assert rendered == {"timeout": "9.5", "schema_version": "1"}
    assert model_for_version(HistoricalTypeConfig, "1") is HistoricalTimeoutConfig


def test_dump_versioned_accepts_current_model_data_for_historical_schema() -> None:
    @versioned_schema(
        name="historical_dump_accepts_current_model_data",
        versions=["1", "2"],
        current="2",
        transitions=(
            VersionTransition(
                "1",
                "2",
                downgrade=_to_dict,
                downgrade_semantics="exact",
            ),
        ),
    )
    @schema_version("1")
    class ReversibleHistorical(BaseModel):
        value: int = 10

    dumped = dump_versioned(
        ReversibleHistorical,
        version="1",
        data=ReversibleHistorical(value=7),
        include_version=False,
    )

    assert dumped == {"value": 7}


def test_dump_versioned_accepts_mapping_data_for_historical_schema() -> None:
    @versioned_schema(
        name="historical_dump_accepts_mapping_data",
        versions=["1", "2"],
        current="2",
        transitions=(
            VersionTransition(
                "1",
                "2",
                downgrade=_to_dict,
                downgrade_semantics="exact",
            ),
        ),
    )
    @schema_version("1")
    class ReversibleHistorical(BaseModel):
        value: int = 10

    dumped = dump_versioned(
        ReversibleHistorical,
        version="1",
        data={"value": 6},
        include_version=False,
    )

    assert dumped == {"value": 6}


def test_dump_versioned_normalizes_alias_paths_and_choices_in_input() -> None:
    @versioned_schema(
        name="aliased_dump_input",
        versions=["1", "2"],
        current="2",
        missing_version="1",
    )
    class AliasRuntimeDumpInput(BaseModel):
        value: int = Field(validation_alias=AliasChoices("legacy", AliasPath("payload", "value")))

    dumped = dump_versioned(
        AliasRuntimeDumpInput,
        version="1",
        data={"legacy": 1, "value": 3, "payload": {"value": 2}},
        include_version=False,
    )

    assert dumped == {"value": 3}


def test_dump_versioned_rejects_non_mapping_data() -> None:
    @versioned_schema(
        name="historical_dump_rejects_non_mapping",
        versions=["1", "2"],
        current="2",
        transitions=(
            VersionTransition(
                "1",
                "2",
                downgrade=_to_dict,
                downgrade_semantics="exact",
            ),
        ),
    )
    @schema_version("1")
    class ReversibleHistorical(BaseModel):
        value: int = 10

    with pytest.raises((TypeError, ValueError)):
        dump_versioned(
            ReversibleHistorical,
            version="1",
            data=cast(Any, ["not", "a", "mapping"]),
        )


def test_dump_versioned_removes_historical_fields_before_validation() -> None:
    @versioned_schema(name="strict_config", versions=["1", "2"], current="2")
    @schema_version("1", patches=[field_removed("added")])
    class StrictConfig(BaseModel):
        model_config = ConfigDict(extra="forbid")

        name: str
        added: bool = False

    dumped = dump_versioned(
        StrictConfig,
        version="1",
        data=StrictConfig(name="app", added=True),
        include_version=False,
    )

    assert dumped == {"name": "app"}


@versioned_schema(
    name="metadata_config",
    versions=["v1", "v2"],
    current="v2",
    version_field=("metadata", "schema_version"),
)
@schema_versions(["v1"], patches=[field_default("timeout", 2.5)])
class MetadataConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout: float = 7.5


def test_nested_version_field_can_live_outside_model_payload() -> None:
    result = validate_versioned(
        MetadataConfig,
        {"metadata": {"schema_version": "v1"}, "timeout": 3.5},
    )

    assert result.source_version == "v1"
    assert result.source_model.model_dump() == {
        "timeout": 3.5,
        "metadata": {"schema_version": "v1"},
    }
    assert result.current_model == MetadataConfig(timeout=3.5)


def test_nested_version_field_can_be_supplied_explicitly_when_missing_from_payload() -> None:
    result = validate_versioned(MetadataConfig, {"timeout": 4.5}, version="v1")

    assert result.source_version == "v1"
    assert result.source_model.model_dump() == {
        "timeout": 4.5,
        "metadata": {"schema_version": "v1"},
    }


def test_nested_version_field_is_rendered_and_removed_on_request() -> None:
    assert dump_versioned(MetadataConfig, version="v1") == {
        "timeout": 2.5,
        "metadata": {"schema_version": "v1"},
    }
    assert dump_versioned(MetadataConfig, version="v1", include_version=False) == {"timeout": 2.5}


def test_unknown_and_missing_versions_raise_typed_errors() -> None:
    with pytest.raises(UnknownSchemaVersionError):
        validate_versioned(AppConfig, {"schema_version": "9"})

    @versioned_schema(name="no_fallback", versions=["1"], current="1")
    class NoFallback(BaseModel):
        value: str

    with pytest.raises(MissingSchemaVersionError):
        validate_versioned(NoFallback, {"value": "ok"})


def test_missing_nested_version_error_names_path() -> None:
    @versioned_schema(
        name="nested_missing",
        versions=["1"],
        current="1",
        version_field=("metadata", "schema_version"),
    )
    class NestedMissing(BaseModel):
        value: str

    with pytest.raises(MissingSchemaVersionError, match="metadata.schema_version"):
        validate_versioned(NestedMissing, {"value": "ok"})


def test_duplicate_version_registration_raises_typed_error() -> None:
    with pytest.raises(DuplicateSchemaVersionError):

        @versioned_schema(name="dup", versions=["1", "1"], current="1")
        class DuplicateVersion(BaseModel):
            value: str


def test_schema_versions_applies_patch_to_multiple_versions() -> None:
    @versioned_schema(name="grouped", versions=["1.0", "1.1", "2.0"], current="2.0")
    @schema_versions(["1.0", "1.1"], patches=[field_default("timeout", 4.0)])
    class GroupedConfig(BaseModel):
        timeout: float = 9.0

    assert model_for_version(GroupedConfig, "1.0")().model_dump()["timeout"] == 4.0
    assert model_for_version(GroupedConfig, "1.1")().model_dump()["timeout"] == 4.0
    assert model_for_version(GroupedConfig, "2.0")().model_dump()["timeout"] == 9.0


def test_unknown_current_and_missing_versions_raise_typed_errors() -> None:
    with pytest.raises(UnknownSchemaVersionError):

        @versioned_schema(name="bad_current", versions=["1"], current="2")
        class BadCurrent(BaseModel):
            value: str

    with pytest.raises(UnknownSchemaVersionError):

        @versioned_schema(name="bad_missing", versions=["1"], current="1", missing_version="2")
        class BadMissing(BaseModel):
            value: str


def test_invalid_version_field_configuration_raises_typed_error() -> None:
    with pytest.raises(SchemaVersionError):

        @versioned_schema(name="empty_field", versions=["1"], current="1", version_field="")
        class EmptyVersionField(BaseModel):
            value: str

    with pytest.raises(SchemaVersionError):

        @versioned_schema(
            name="empty_path", versions=["1"], current="1", version_field=("metadata", "")
        )
        class EmptyVersionPath(BaseModel):
            value: str


def test_patch_for_undeclared_version_raises_typed_error() -> None:
    with pytest.raises(UnknownSchemaVersionError):

        @versioned_schema(name="undeclared_patch", versions=["1"], current="1")
        @schema_version("2", patches=[field_default("value", "legacy")])
        class UndeclaredPatch(BaseModel):
            value: str


def test_duplicate_schema_version_patch_registration_raises_typed_error() -> None:
    with pytest.raises(DuplicateSchemaVersionError):

        @versioned_schema(name="dup_patch", versions=["1"], current="1")
        @schema_version("1", patches=[field_default("value", "a")])
        @schema_version("1", patches=[field_default("value", "b")])
        class DuplicatePatch(BaseModel):
            value: str


def test_invalid_patch_field_raises_typed_error() -> None:
    with pytest.raises(SchemaVersionError):

        @versioned_schema(name="bad_patch", versions=["1"], current="1")
        @schema_version("1", patches=[field_removed("missing")])
        class BadPatch(BaseModel):
            value: str


def test_invalid_rename_conflict_raises_typed_error() -> None:
    with pytest.raises(SchemaVersionError):

        @versioned_schema(name="bad_rename", versions=["1"], current="1")
        @schema_version("1", patches=[field_renamed("value", "other")])
        class BadRename(BaseModel):
            value: str
            other: str


def test_invalid_rename_source_raises_typed_error() -> None:
    with pytest.raises(SchemaVersionError):

        @versioned_schema(name="bad_rename_source", versions=["1"], current="1")
        @schema_version("1", patches=[field_renamed("missing", "old_name")])
        class BadRenameSource(BaseModel):
            value: str


def test_patch_helpers_validate_default_arguments() -> None:
    with pytest.raises(ValueError):
        field_default("value")
    with pytest.raises(ValueError):
        field_default("value", "a", default_factory=lambda: "b")


def test_field_default_supports_default_factory() -> None:
    @versioned_schema(name="factory_default", versions=["1", "2"], current="2")
    @schema_version("1", patches=[field_default("items", default_factory=list)])
    class FactoryDefault(BaseModel):
        items: list[str]

    assert model_for_version(FactoryDefault, "1")().model_dump() == {
        "items": [],
        "schema_version": "1",
    }


def test_invalid_migration_registration_and_return_value_raise_typed_errors() -> None:
    with pytest.raises(InvalidMigrationError):
        migration(AppConfig, "3", "1")

    @versioned_schema(name="bad_migration", versions=["1", "2"], current="2", missing_version="1")
    class BadMigrationModel(BaseModel):
        value: int

    @migration(BadMigrationModel, "1", "2")
    def bad_migration(data: dict) -> dict:
        return cast(Any, [])

    with pytest.raises(InvalidMigrationError):
        validate_versioned(BadMigrationModel, {"value": 1})


def test_duplicate_migration_registration_raises_typed_error() -> None:
    @versioned_schema(name="dup_migration", versions=["1", "2"], current="2")
    class DuplicateMigrationModel(BaseModel):
        value: int

    @migration(DuplicateMigrationModel, "1", "2")
    def first(data: dict) -> dict:
        return data

    with pytest.raises(DuplicateSchemaVersionError):

        @migration(DuplicateMigrationModel, "1", "2")
        def second(data: dict) -> dict:
            return data


def test_user_owned_top_level_version_field_is_not_redeclared() -> None:
    @versioned_schema(name="owned_version", versions=["1"], current="1")
    class OwnedVersion(BaseModel):
        schema_version: str
        value: int = 1

    assert dump_versioned(OwnedVersion, version="1", data={"schema_version": "1"}) == {
        "schema_version": "1",
        "value": 1,
    }


@versioned_schema(name="generic_child", versions=["1", "2"], current="2", missing_version="1")
@schema_version("1", patches=[field_default("value", 1)])
class GenericChild(BaseModel):
    value: int = 2


@versioned_schema(name="generic_parent", versions=["1", "2"], current="2", missing_version="1")
class GenericParent(BaseModel):
    children: list[GenericChild] = Field(default_factory=list)
    child_tuple: tuple[GenericChild, ...] = ()
    child_set: set[int] = Field(default_factory=set)
    frozen_tags: frozenset[str] = frozenset()
    child_map: dict[str, GenericChild] = Field(default_factory=dict)
    maybe_child: GenericChild | None = None


def test_container_annotations_are_rewritten_for_nested_versioned_models() -> None:
    parent_v1 = model_for_version(GenericParent, "1")
    parent = parent_v1.model_validate(
        {
            "children": [{}],
            "child_tuple": [{}],
            "child_set": {1, 2},
            "frozen_tags": {"a"},
            "child_map": {"primary": {}},
            "maybe_child": {},
        }
    )

    assert parent.model_dump() == {
        "children": [{"value": 1, "schema_version": "1"}],
        "child_tuple": ({"value": 1, "schema_version": "1"},),
        "child_set": {1, 2},
        "frozen_tags": frozenset({"a"}),
        "child_map": {"primary": {"value": 1, "schema_version": "1"}},
        "maybe_child": {"value": 1, "schema_version": "1"},
        "schema_version": "1",
    }


@versioned_schema(name="database", versions=["1", "2"], current="2", missing_version="1")
@schema_version("1", patches=[field_default("port", 5432)])
class DatabaseConfig(BaseModel):
    host: str = "localhost"
    port: int = 6432


@versioned_schema(name="service", versions=["1", "2"], current="2", missing_version="1")
class ServiceConfig(BaseModel):
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)


def test_nested_registered_models_use_matching_version_models() -> None:
    service_v1 = model_for_version(ServiceConfig, "1")
    service = service_v1()

    assert service.model_dump() == {
        "database": {"host": "localhost", "port": 5432, "schema_version": "1"},
        "schema_version": "1",
    }


@versioned_schema(
    name="service_with_default", versions=["1", "2"], current="2", missing_version="1"
)
class ServiceWithDefault(BaseModel):
    database: DatabaseConfig = DatabaseConfig()


def test_nested_default_instances_use_matching_version_models() -> None:
    service_v1 = model_for_version(ServiceWithDefault, "1")

    assert service_v1().model_dump() == {
        "database": {"host": "localhost", "port": 5432, "schema_version": "1"},
        "schema_version": "1",
    }


def test_nested_patched_instance_and_factory_defaults_use_the_historical_child() -> None:
    @versioned_schema(
        name="patched_default_child",
        versions=["1", "2"],
        current="2",
        missing_version="1",
    )
    @schema_version("1", patches=[field_default("port", 5432)])
    class PatchedDefaultChild(BaseModel):
        port: int = 6432

    @versioned_schema(
        name="patched_default_parent",
        versions=["1", "2"],
        current="2",
        missing_version="1",
    )
    @schema_version(
        "1",
        patches=[
            field_default("instance_child", PatchedDefaultChild(port=6432)),
            field_default("factory_child", default_factory=PatchedDefaultChild),
        ],
    )
    class PatchedDefaultParent(BaseModel):
        instance_child: PatchedDefaultChild = Field(default_factory=PatchedDefaultChild)
        factory_child: PatchedDefaultChild = Field(default_factory=PatchedDefaultChild)

    parent_v1 = model_for_version(PatchedDefaultParent, "1")()
    typed_parent_v1 = cast(Any, parent_v1)

    assert not isinstance(typed_parent_v1.instance_child, PatchedDefaultChild)
    assert not isinstance(typed_parent_v1.factory_child, PatchedDefaultChild)
    assert parent_v1.model_dump() == {
        "instance_child": {"port": 6432, "schema_version": "1"},
        "factory_child": {"port": 5432, "schema_version": "1"},
        "schema_version": "1",
    }


def test_opaque_nested_child_factory_is_rejected_without_execution() -> None:
    factory_events: list[str] = []

    @versioned_schema(
        name="opaque_factory_child",
        versions=["1", "2"],
        current="2",
        missing_version="1",
    )
    class OpaqueFactoryChild(BaseModel):
        value: int = 1

    def make_child() -> OpaqueFactoryChild:
        factory_events.append("factory")
        return OpaqueFactoryChild()

    @versioned_schema(
        name="opaque_factory_parent",
        versions=["1", "2"],
        current="2",
        missing_version="1",
    )
    class OpaqueFactoryParent(BaseModel):
        child: OpaqueFactoryChild = Field(default_factory=make_child)

    with pytest.raises(UnsupportedWireModelError, match="opaque factory"):
        model_for_version(OpaqueFactoryParent, "1")

    assert factory_events == []


def test_nested_default_projection_does_not_run_current_child_serializers() -> None:
    serialization_events: list[int] = []

    @versioned_schema(
        name="serializer_default_child",
        versions=["1", "2"],
        current="2",
        missing_version="1",
    )
    class SerializerDefaultChild(BaseModel):
        value: int = 1

        @field_serializer("value")
        def serialize_value(self, value: int) -> int:
            serialization_events.append(value)
            return value

    child_default = SerializerDefaultChild()

    @versioned_schema(
        name="serializer_default_parent",
        versions=["1", "2"],
        current="2",
        missing_version="1",
    )
    class SerializerDefaultParent(BaseModel):
        child: SerializerDefaultChild = child_default

    baseline = list(serialization_events)
    parent_v1 = model_for_version(SerializerDefaultParent, "1")

    assert serialization_events == baseline
    assert parent_v1().model_dump() == {
        "child": {"value": 1, "schema_version": "1"},
        "schema_version": "1",
    }
    assert serialization_events == baseline


def test_nested_model_owned_metadata_default_is_normalized_to_the_target_version() -> None:
    @versioned_schema(
        name="model_owned_default_child",
        versions=["1", "2"],
        current="2",
        missing_version="1",
    )
    class ModelOwnedDefaultChild(BaseModel):
        schema_version: str = "2"
        value: int = 1

    @versioned_schema(
        name="model_owned_default_parent",
        versions=["1", "2"],
        current="2",
        missing_version="1",
    )
    class ModelOwnedDefaultParent(BaseModel):
        child: ModelOwnedDefaultChild = ModelOwnedDefaultChild(schema_version="2")

    parent_v1 = model_for_version(ModelOwnedDefaultParent, "1")()

    assert parent_v1.model_dump() == {
        "child": {"schema_version": "1", "value": 1},
        "schema_version": "1",
    }


def test_nested_family_metadata_defaults_are_normalized_without_user_factories() -> None:
    @versioned_schema(
        name="family_default_child",
        versions=["1", "2"],
        current="2",
        missing_version="1",
    )
    class FamilyDefaultChild(BaseModel):
        model_config = ConfigDict(extra="allow")

        value: int = 1

    @versioned_schema(
        name="family_default_parent",
        versions=["1", "2"],
        current="2",
        missing_version="1",
    )
    class FamilyDefaultParent(BaseModel):
        child: FamilyDefaultChild = FamilyDefaultChild.model_validate({"schema_version": "2"})

    direct_v1 = model_for_version(FamilyDefaultParent, "1")()
    assert direct_v1.model_dump()["child"]["schema_version"] == "1"

    @versioned_schema(
        name="nested_metadata_default_child",
        versions=["1", "2"],
        current="2",
        version_field=("meta", "version"),
        missing_version="1",
    )
    class NestedMetadataDefaultChild(BaseModel):
        value: int = 1

    @versioned_schema(
        name="nested_metadata_default_parent",
        versions=["1", "2"],
        current="2",
        missing_version="1",
    )
    class NestedMetadataDefaultParent(BaseModel):
        child: NestedMetadataDefaultChild = NestedMetadataDefaultChild()

    nested_v1 = model_for_version(NestedMetadataDefaultParent, "1")()
    assert nested_v1.model_dump() == {
        "child": {"value": 1, "meta": {"version": "1"}},
        "schema_version": "1",
    }


def test_nested_runtime_migrations_execute_before_parent_migrations_for_validation() -> None:
    events: list[str] = []

    def child_migrate_up_from_one_to_two(data: dict[str, Any]) -> dict[str, Any]:
        events.append("child 1->2 0")
        return {"value": data["value"] + 1}

    def child_migrate_up_from_two_to_three(data: dict[str, Any]) -> dict[str, Any]:
        events.append("child 2->3 0")
        return {"value": data["value"] + 10}

    def parent_migrate_up_from_one_to_two(data: dict[str, Any]) -> dict[str, Any]:
        events.append("parent 1->2")
        return {**data, "value": data["value"] + 100}

    def parent_migrate_up_from_two_to_three(data: dict[str, Any]) -> dict[str, Any]:
        events.append("parent 2->3")
        return {**data, "value": data["value"] + 1000}

    class NestedChild(BaseModel):
        value: int

    child_family = SchemaFamily(
        model=NestedChild,
        name="nested_issue11_child",
        versions=(SchemaVersion("1"), SchemaVersion("2"), SchemaVersion("3")),
        transitions=(
            VersionTransition("1", "2", upgrade=child_migrate_up_from_one_to_two),
            VersionTransition("2", "3", upgrade=child_migrate_up_from_two_to_three),
        ),
        missing_version="1",
    )

    class NestedParent(BaseModel):
        value: int
        children: list[NestedChild]

    parent_family = SchemaFamily(
        model=NestedParent,
        name="nested_issue11_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2"), SchemaVersion("3")),
        transitions=(
            VersionTransition("1", "2", upgrade=parent_migrate_up_from_one_to_two),
            VersionTransition("2", "3", upgrade=parent_migrate_up_from_two_to_three),
        ),
        nested=(NestedFamily("children", child_family, matching_labels()),),
        missing_version="1",
    )

    result = validate_versioned(
        parent_family,
        {"schema_version": "1", "value": 1, "children": [{"value": 1}, {"value": 2}]},
    )

    assert result.current_model.value == 1101
    assert [child.value for child in result.current_model.children] == [12, 13]
    assert events == [
        "child 1->2 0",
        "child 1->2 0",
        "parent 1->2",
        "child 2->3 0",
        "child 2->3 0",
        "parent 2->3",
    ]


def test_nested_runtime_migrations_execute_before_parent_migrations_for_rendering() -> None:
    events: list[str] = []

    def child_migrate_up_from_one_to_two(data: dict[str, Any]) -> dict[str, Any]:
        return {"value": data["value"] + 1}

    def child_migrate_up_from_two_to_three(data: dict[str, Any]) -> dict[str, Any]:
        return {"value": data["value"] + 10}

    def parent_migrate_up_from_one_to_two(data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "value": data["value"] + 100}

    def parent_migrate_up_from_two_to_three(data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "value": data["value"] + 1000}

    def child_migrate_down_from_two_to_one(data: dict[str, Any]) -> dict[str, Any]:
        events.append("child 2->1 0")
        return {"value": data["value"] - 1}

    def child_migrate_down_from_three_to_two(data: dict[str, Any]) -> dict[str, Any]:
        events.append("child 3->2 0")
        return {"value": data["value"] - 10}

    def parent_migrate_down_from_two_to_one(data: dict[str, Any]) -> dict[str, Any]:
        events.append("parent 2->1")
        return {**data, "value": data["value"] - 100}

    def parent_migrate_down_from_three_to_two(data: dict[str, Any]) -> dict[str, Any]:
        events.append("parent 3->2")
        return {**data, "value": data["value"] - 1000}

    class NestedChild(BaseModel):
        value: int

    child_family = SchemaFamily(
        model=NestedChild,
        name="nested_issue11_child_downgrade",
        versions=(SchemaVersion("1"), SchemaVersion("2"), SchemaVersion("3")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                upgrade=child_migrate_up_from_one_to_two,
                downgrade=child_migrate_down_from_two_to_one,
                downgrade_semantics="exact",
            ),
            VersionTransition(
                "2",
                "3",
                upgrade=child_migrate_up_from_two_to_three,
                downgrade=child_migrate_down_from_three_to_two,
                downgrade_semantics="exact",
            ),
        ),
        missing_version="1",
    )

    class NestedParent(BaseModel):
        value: int
        children: list[NestedChild]

    parent_family = SchemaFamily(
        model=NestedParent,
        name="nested_issue11_parent_downgrade",
        versions=(SchemaVersion("1"), SchemaVersion("2"), SchemaVersion("3")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                upgrade=parent_migrate_up_from_one_to_two,
                downgrade=parent_migrate_down_from_two_to_one,
                downgrade_semantics="exact",
            ),
            VersionTransition(
                "2",
                "3",
                upgrade=parent_migrate_up_from_two_to_three,
                downgrade=parent_migrate_down_from_three_to_two,
                downgrade_semantics="exact",
            ),
        ),
        nested=(NestedFamily("children", child_family, matching_labels()),),
        missing_version="1",
    )
    current_payload = parent_family.model_for("3").model_validate(
        {"value": 111, "children": [{"value": 12}, {"value": 13}]},
    )
    rendered = dump_versioned(cast(Any, parent_family), version="1", data=current_payload)

    assert rendered == {
        "value": -989,
        "children": [{"value": 1}, {"value": 2}],
        "schema_version": "1",
    }
    assert events == [
        "child 3->2 0",
        "child 3->2 0",
        "parent 3->2",
        "child 2->1 0",
        "child 2->1 0",
        "parent 2->1",
    ]


def test_nested_runtime_handles_set_tuple_and_frozenset_payloads() -> None:
    def child_migrate_to_current(data: dict[str, Any]) -> dict[str, Any]:
        return {"value": data["value"] + 1}

    def child_migrate_to_historical(data: dict[str, Any]) -> dict[str, Any]:
        return {"value": data["value"] - 1}

    class NestedCollectionChild(BaseModel):
        value: int

    child_family = SchemaFamily(
        model=NestedCollectionChild,
        name="nested_collection_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                upgrade=child_migrate_to_current,
                downgrade=child_migrate_to_historical,
                downgrade_semantics="exact",
            ),
        ),
        missing_version="1",
    )

    class NestedCollectionParent(BaseModel):
        values: set[NestedCollectionChild]
        tuple_values: tuple[NestedCollectionChild, ...]
        frozenset_values: frozenset[NestedCollectionChild]
        value: int = 2

    parent_family = SchemaFamily(
        model=NestedCollectionParent,
        name="nested_collection_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                upgrade=lambda data: {**data, "value": data["value"] + 10},
                downgrade=lambda data: {**data, "value": data["value"] - 10},
                downgrade_semantics="exact",
            ),
        ),
        nested=(
            NestedFamily("values", child_family, matching_labels()),
            NestedFamily("tuple_values", child_family, matching_labels()),
            NestedFamily("frozenset_values", child_family, matching_labels()),
        ),
        missing_version="1",
    )

    current_payload = parent_family.model_for("2").model_validate(
        {
            "schema_version": "2",
            "values": [
                {"schema_version": "2", "value": 2},
                {"schema_version": "2", "value": 3},
            ],
            "tuple_values": [
                {"schema_version": "2", "value": 4},
                {"schema_version": "2", "value": 5},
            ],
            "frozenset_values": [
                {"schema_version": "2", "value": 6},
            ],
            "value": 20,
        },
    )

    rendered = dump_versioned(cast(Any, parent_family), version="1", data=current_payload)

    assert rendered["value"] == 10
    assert len(rendered["values"]) == 2
    assert len(rendered["tuple_values"]) == 2
    assert len(rendered["frozenset_values"]) == 1
    assert all(item["schema_version"] == "1" for item in rendered["values"])
    assert all(item["schema_version"] == "1" for item in rendered["tuple_values"])
    assert all(item["schema_version"] == "1" for item in rendered["frozenset_values"])


def test_nested_runtime_set_conversion_preserves_collection_membership_or_raises() -> None:
    def child_migrate_down(data: dict[str, Any]) -> dict[str, Any]:
        return {"value": 1}

    def parent_migrate_down(data: dict[str, Any]) -> dict[str, Any]:
        return data

    class DuplicateNestedChild(BaseModel):
        value: int

    child_family = SchemaFamily(
        model=DuplicateNestedChild,
        name="nested_set_collision_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(
            VersionTransition("1", "2", downgrade=child_migrate_down, downgrade_semantics="exact"),
        ),
        missing_version="1",
    )

    class DuplicateNestedParent(BaseModel):
        items: set[DuplicateNestedChild]

    parent_family = SchemaFamily(
        model=DuplicateNestedParent,
        name="nested_set_collision_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(
            VersionTransition("1", "2", downgrade=parent_migrate_down, downgrade_semantics="exact"),
        ),
        nested=(NestedFamily("items", child_family, matching_labels()),),
        missing_version="1",
    )

    current_payload = parent_family.model_for("2").model_validate(
        {"schema_version": "2", "items": [{"value": 3}, {"value": 4}]},
    )
    with pytest.raises(InvalidMigrationError, match="cannot preserve set cardinality"):
        dump_versioned(cast(Any, parent_family), version="1", data=current_payload)
