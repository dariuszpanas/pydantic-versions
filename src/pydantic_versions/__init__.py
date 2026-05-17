from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from pydantic_versions.core import (
    VersionedValidation,
    dump_versioned,
    migration,
    model_for_version,
    schema_version,
    schema_versions,
    validate_versioned,
    versioned_schema,
)
from pydantic_versions.exceptions import (
    DuplicateSchemaVersionError,
    InvalidMigrationError,
    MissingSchemaVersionError,
    SchemaVersionError,
    UnknownSchemaVersionError,
    VersionedValidationError,
)
from pydantic_versions.patches import field_default, field_removed, field_renamed


def _package_version(distribution: str = "pydantic-versions") -> str:
    try:
        return version(distribution)
    except PackageNotFoundError:
        return "0.0.0"


__version__ = _package_version()

__all__ = [
    "DuplicateSchemaVersionError",
    "InvalidMigrationError",
    "MissingSchemaVersionError",
    "SchemaVersionError",
    "UnknownSchemaVersionError",
    "VersionedValidation",
    "VersionedValidationError",
    "__version__",
    "dump_versioned",
    "field_default",
    "field_removed",
    "field_renamed",
    "migration",
    "model_for_version",
    "schema_version",
    "schema_versions",
    "validate_versioned",
    "versioned_schema",
]
