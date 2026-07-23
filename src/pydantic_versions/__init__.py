from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from pydantic_versions.core import (
    dump_versioned,
    migration,
    model_for_version,
    schema_version,
    schema_versions,
    validate_versioned,
    versioned_schema,
)
from pydantic_versions.declarations import (
    JsonValue,
    MatchingLabels,
    NestedFamily,
    SchemaVersion,
    TransitionData,
    TransitionFunc,
    VersionedValidation,
    VersionMetadata,
    VersionPath,
    VersionTransition,
    matching_labels,
)
from pydantic_versions.exceptions import (
    DuplicateSchemaVersionError,
    InvalidMigrationError,
    MissingSchemaVersionError,
    SchemaCompilationError,
    SchemaFamilySelectionError,
    SchemaVersionError,
    UnknownSchemaVersionError,
    VersionedValidationError,
)
from pydantic_versions.family import SchemaFamily
from pydantic_versions.patches import (
    FieldDefault,
    FieldRemoved,
    FieldRenamed,
    VersionPatch,
    field_default,
    field_removed,
    field_renamed,
)


def _package_version(distribution: str = "pydantic-versions") -> str:
    try:
        return version(distribution)
    except PackageNotFoundError:
        return "0.0.0"


__version__ = _package_version()

__all__ = [
    "DuplicateSchemaVersionError",
    "FieldDefault",
    "FieldRemoved",
    "FieldRenamed",
    "InvalidMigrationError",
    "JsonValue",
    "MatchingLabels",
    "MissingSchemaVersionError",
    "NestedFamily",
    "SchemaCompilationError",
    "SchemaFamily",
    "SchemaFamilySelectionError",
    "SchemaVersion",
    "SchemaVersionError",
    "TransitionData",
    "TransitionFunc",
    "UnknownSchemaVersionError",
    "VersionMetadata",
    "VersionPatch",
    "VersionPath",
    "VersionTransition",
    "VersionedValidation",
    "VersionedValidationError",
    "__version__",
    "dump_versioned",
    "field_default",
    "field_removed",
    "field_renamed",
    "matching_labels",
    "migration",
    "model_for_version",
    "schema_version",
    "schema_versions",
    "validate_versioned",
    "versioned_schema",
]
