from __future__ import annotations


class SchemaVersionError(Exception):
    """Base exception for schema version configuration and runtime errors."""


class SchemaCompilationError(SchemaVersionError):
    """Raised when a schema-family declaration cannot be compiled safely."""


class UnsupportedWireModelError(SchemaCompilationError):
    """Raised when a model cannot be represented as an automatic wire contract."""


class SchemaFamilySelectionError(SchemaVersionError):
    """Raised when a model-only call has no unambiguous explicit default family."""


class IrreversibleTransitionError(SchemaVersionError):
    """Raised when a complete render route cannot be planned safely."""


class MissingSchemaVersionError(SchemaVersionError):
    """Raised when a schema version cannot be discovered for input data."""


class UnknownSchemaVersionError(SchemaVersionError):
    """Raised when a requested schema version is not registered."""


class DuplicateSchemaVersionError(SchemaVersionError):
    """Raised when a schema version or migration is registered more than once."""


class InvalidMigrationError(SchemaVersionError):
    """Raised when a migration is invalid or returns an invalid value."""


class VersionedValidationError(SchemaVersionError):
    """Raised when versioned validation cannot be completed."""
