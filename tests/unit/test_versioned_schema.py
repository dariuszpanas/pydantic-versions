from __future__ import annotations

import warnings
from typing import Any, cast

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pydantic_versions import (
    DuplicateSchemaVersionError,
    InvalidMigrationError,
    MissingSchemaVersionError,
    SchemaVersionError,
    UnknownSchemaVersionError,
    dump_versioned,
    field_default,
    field_removed,
    field_renamed,
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


def test_validate_versioned_uses_explicit_version_over_embedded_version() -> None:
    result = validate_versioned(
        AppConfig,
        {"schema_version": "3", "attempts": 1},
        version="1",
    )

    assert result.source_version == "1"
    assert result.current_model.timeout == 5.0


def test_validate_versioned_uses_missing_version_fallback() -> None:
    result = validate_versioned(AppConfig, {"attempts": 1})

    assert result.source_version == "1"
    assert result.current_model == AppConfig(timeout=5.0, retries=1, new_feature=False)


def test_validate_versioned_handles_current_version() -> None:
    result = validate_versioned(AppConfig, {"schema_version": "3", "timeout": 11.0})

    assert result.current_model == AppConfig(timeout=11.0)
    assert result.migrations_applied == ()


def test_dump_versioned_renders_defaults_for_requested_schema() -> None:
    assert dump_versioned(AppConfig, version="1") == {
        "timeout": 5.0,
        "attempts": 3,
        "schema_version": "1",
    }


def test_dump_versioned_accepts_current_model_data_for_historical_schema() -> None:
    dumped = dump_versioned(
        AppConfig,
        version="1",
        data=AppConfig(timeout=7.5, retries=6, new_feature=True),
        include_version=False,
    )

    assert dumped == {"timeout": 7.5, "attempts": 6}


def test_dump_versioned_accepts_mapping_data_for_historical_schema() -> None:
    dumped = dump_versioned(
        AppConfig,
        version="1",
        data={"timeout": 6.5, "retries": 5, "new_feature": True},
        include_version=False,
    )

    assert dumped == {"timeout": 6.5, "attempts": 5}


def test_dump_versioned_rejects_non_mapping_data() -> None:
    with pytest.raises(ValidationError):
        dump_versioned(AppConfig, version="1", data=["not", "a", "mapping"])


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
    assert result.source_model.model_dump() == {"timeout": 3.5}
    assert result.current_model == MetadataConfig(timeout=3.5)


def test_nested_version_field_can_be_supplied_explicitly_when_missing_from_payload() -> None:
    result = validate_versioned(MetadataConfig, {"timeout": 4.5}, version="v1")

    assert result.source_version == "v1"
    assert result.source_model.model_dump() == {"timeout": 4.5}


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
    @versioned_schema(name="factory_default", versions=["1"], current="1")
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
