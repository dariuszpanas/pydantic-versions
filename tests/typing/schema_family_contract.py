from __future__ import annotations

from typing import Any, assert_type

from pydantic import BaseModel

from pydantic_versions import (
    SchemaCompilationError,
    SchemaFamily,
    SchemaFamilySelectionError,
    SchemaVersion,
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
    transitions=(VersionTransition("1", "2", upgrade=upgrade_v1),),
)

assert_type(family.compile(), SchemaFamily[AppConfig])
assert_type(family.as_default(), SchemaFamily[AppConfig])
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
selection_error: type[Exception] = SchemaFamilySelectionError
