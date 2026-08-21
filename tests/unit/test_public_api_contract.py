from __future__ import annotations

import inspect
from dataclasses import fields, is_dataclass
from types import FunctionType
from typing import Any

import pytest
from pydantic import BaseModel

import pydantic_versions as public_api
from pydantic_versions import (
    DuplicateSchemaVersionError,
    InvalidMigrationError,
    IrreversibleTransitionError,
    MissingSchemaVersionError,
    SchemaCompilationError,
    SchemaFamily,
    SchemaFamilySelectionError,
    SchemaVersion,
    SchemaVersionError,
    UnknownSchemaVersionError,
    UnsupportedWireModelError,
    VersionTransition,
)

EXPECTED_EXPORTS = (
    "ConversionPlan",
    "DuplicateSchemaVersionError",
    "FieldDefault",
    "FieldRemoved",
    "FieldRenamed",
    "InvalidMigrationError",
    "IrreversibleTransitionError",
    "JsonValue",
    "MatchingLabels",
    "MissingSchemaVersionError",
    "NestedFamily",
    "NestedFamilyDescription",
    "PlanStep",
    "ProjectionDescription",
    "SchemaCompilationError",
    "SchemaFamily",
    "SchemaFamilySelectionError",
    "SchemaInventory",
    "SchemaVersion",
    "SchemaVersionError",
    "StepKind",
    "StepSemantics",
    "TransitionData",
    "TransitionFunc",
    "TransitionDescription",
    "UnsupportedWireModelError",
    "UnknownSchemaVersionError",
    "VersionMetadata",
    "VersionPatch",
    "VersionPath",
    "VersionTransition",
    "VersionedValidation",
    "VersionDescription",
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
)

EXPECTED_RECORD_FIELDS = {
    "ConversionPlan": (
        "family",
        "source_version",
        "target_version",
        "operation",
        "semantics",
        "steps",
    ),
    "FieldDefault": ("name", "default", "default_factory", "has_default"),
    "FieldRemoved": ("name",),
    "FieldRenamed": ("current_name", "version_name"),
    "MatchingLabels": (),
    "NestedFamily": ("path", "family", "versions"),
    "NestedFamilyDescription": ("schema_path", "family", "versions"),
    "PlanStep": (
        "id",
        "family",
        "source_version",
        "target_version",
        "operation",
        "direction",
        "kind",
        "schema_path",
        "semantics",
        "conditional",
    ),
    "ProjectionDescription": ("kind", "current_field", "historical_field", "has_default"),
    "SchemaInventory": (
        "family",
        "model",
        "current_version",
        "versions",
        "transitions",
        "nested",
        "version_metadata",
    ),
    "SchemaVersion": ("label", "patches", "wire_model"),
    "TransitionDescription": ("source", "target", "upgrade", "downgrade", "downgrade_semantics"),
    "VersionDescription": ("label", "wire_model", "projections"),
    "VersionMetadata": ("path", "owner"),
    "VersionTransition": ("source", "target", "upgrade", "downgrade", "downgrade_semantics"),
    "VersionedValidation": (
        "source_version",
        "current_version",
        "source_model",
        "current_model",
        "migrations_applied",
    ),
}


def _signature_shape(callable_: Any) -> tuple[tuple[str, str, bool], ...]:
    return tuple(
        (
            name,
            parameter.kind.name,
            parameter.default is not inspect.Parameter.empty,
        )
        for name, parameter in inspect.signature(callable_).parameters.items()
    )


EXPECTED_FUNCTION_SIGNATURES = {
    "dump_versioned": (
        ("subject", "POSITIONAL_OR_KEYWORD", False),
        ("version", "KEYWORD_ONLY", False),
        ("data", "KEYWORD_ONLY", True),
        ("include_version", "KEYWORD_ONLY", True),
        ("dump_kwargs", "VAR_KEYWORD", False),
    ),
    "field_default": (
        ("name", "POSITIONAL_OR_KEYWORD", False),
        ("default", "POSITIONAL_OR_KEYWORD", True),
        ("default_factory", "KEYWORD_ONLY", True),
    ),
    "field_removed": (("name", "POSITIONAL_OR_KEYWORD", False),),
    "field_renamed": (
        ("current_name", "POSITIONAL_OR_KEYWORD", False),
        ("version_name", "POSITIONAL_OR_KEYWORD", False),
    ),
    "matching_labels": (),
    "migration": (
        ("subject", "POSITIONAL_OR_KEYWORD", False),
        ("from_version", "POSITIONAL_OR_KEYWORD", False),
        ("to_version", "POSITIONAL_OR_KEYWORD", False),
    ),
    "model_for_version": (
        ("subject", "POSITIONAL_OR_KEYWORD", False),
        ("version", "POSITIONAL_OR_KEYWORD", False),
    ),
    "schema_version": (
        ("version", "POSITIONAL_OR_KEYWORD", False),
        ("patches", "KEYWORD_ONLY", True),
        ("wire_model", "KEYWORD_ONLY", True),
    ),
    "schema_versions": (
        ("versions", "POSITIONAL_OR_KEYWORD", False),
        ("patches", "KEYWORD_ONLY", True),
        ("wire_model", "KEYWORD_ONLY", True),
    ),
    "validate_versioned": (
        ("subject", "POSITIONAL_OR_KEYWORD", False),
        ("data", "POSITIONAL_OR_KEYWORD", False),
        ("version", "KEYWORD_ONLY", True),
    ),
    "versioned_schema": (
        ("name", "KEYWORD_ONLY", False),
        ("versions", "KEYWORD_ONLY", False),
        ("current", "KEYWORD_ONLY", False),
        ("version_field", "KEYWORD_ONLY", True),
        ("missing_version", "KEYWORD_ONLY", True),
        ("metadata_owner", "KEYWORD_ONLY", True),
        ("transitions", "KEYWORD_ONLY", True),
        ("nested", "KEYWORD_ONLY", True),
    ),
}

EXPECTED_FAMILY_SIGNATURES = {
    "__init__": (
        ("self", "POSITIONAL_OR_KEYWORD", False),
        ("model", "KEYWORD_ONLY", False),
        ("name", "KEYWORD_ONLY", False),
        ("versions", "KEYWORD_ONLY", False),
        ("transitions", "KEYWORD_ONLY", True),
        ("nested", "KEYWORD_ONLY", True),
        ("version_metadata", "KEYWORD_ONLY", True),
        ("missing_version", "KEYWORD_ONLY", True),
    ),
    "compile": (("self", "POSITIONAL_OR_KEYWORD", False),),
    "as_default": (("self", "POSITIONAL_OR_KEYWORD", False),),
    "describe": (("self", "POSITIONAL_OR_KEYWORD", False),),
    "plan_validation": (
        ("self", "POSITIONAL_OR_KEYWORD", False),
        ("source_version", "POSITIONAL_OR_KEYWORD", False),
    ),
    "plan_render": (
        ("self", "POSITIONAL_OR_KEYWORD", False),
        ("target_version", "POSITIONAL_OR_KEYWORD", False),
    ),
    "model_for": (
        ("self", "POSITIONAL_OR_KEYWORD", False),
        ("version", "POSITIONAL_OR_KEYWORD", False),
    ),
    "validate": (
        ("self", "POSITIONAL_OR_KEYWORD", False),
        ("data", "POSITIONAL_OR_KEYWORD", False),
        ("version", "KEYWORD_ONLY", True),
    ),
    "defaults_for": (
        ("self", "POSITIONAL_OR_KEYWORD", False),
        ("version", "KEYWORD_ONLY", False),
        ("include_version", "KEYWORD_ONLY", True),
        ("dump_kwargs", "VAR_KEYWORD", False),
    ),
    "dump": (
        ("self", "POSITIONAL_OR_KEYWORD", False),
        ("version", "KEYWORD_ONLY", False),
        ("data", "KEYWORD_ONLY", True),
        ("include_version", "KEYWORD_ONLY", True),
        ("dump_kwargs", "VAR_KEYWORD", False),
    ),
}


def test_package_root_exports_are_an_explicit_contract() -> None:
    assert tuple(public_api.__all__) == EXPECTED_EXPORTS
    assert all(hasattr(public_api, name) for name in EXPECTED_EXPORTS)


def test_exported_record_fields_and_immutability_are_an_explicit_contract() -> None:
    for name, expected_fields in EXPECTED_RECORD_FIELDS.items():
        record = getattr(public_api, name)
        assert is_dataclass(record), name
        assert record.__dataclass_params__.frozen, name
        assert tuple(field.name for field in fields(record)) == expected_fields


def test_public_call_signatures_are_an_explicit_contract() -> None:
    actual_functions = {
        name: _signature_shape(getattr(public_api, name))
        for name in EXPECTED_EXPORTS
        if isinstance(getattr(public_api, name), FunctionType)
    }
    assert actual_functions == EXPECTED_FUNCTION_SIGNATURES
    assert {
        name: _signature_shape(getattr(SchemaFamily, name)) for name in EXPECTED_FAMILY_SIGNATURES
    } == EXPECTED_FAMILY_SIGNATURES
    assert {
        name
        for name, value in vars(SchemaFamily).items()
        if isinstance(value, property) and not name.startswith("_")
    } == {
        "model",
        "name",
        "versions",
        "transitions",
        "nested",
        "version_metadata",
        "missing_version",
        "current_version",
    }


def test_exception_hierarchy_is_an_explicit_contract() -> None:
    assert SchemaVersionError.__bases__ == (Exception,)
    assert SchemaCompilationError.__bases__ == (SchemaVersionError,)
    assert UnsupportedWireModelError.__bases__ == (SchemaCompilationError,)
    for exception in (
        SchemaFamilySelectionError,
        IrreversibleTransitionError,
        MissingSchemaVersionError,
        UnknownSchemaVersionError,
        DuplicateSchemaVersionError,
        InvalidMigrationError,
    ):
        assert exception.__bases__ == (SchemaVersionError,)


def test_user_transition_exceptions_propagate_unchanged() -> None:
    class CurrentConfig(BaseModel):
        value: int

    class ApplicationTransitionError(RuntimeError):
        pass

    error = ApplicationTransitionError("application-owned failure")

    def fail(_data: dict[str, Any]) -> dict[str, Any]:
        raise error

    family = SchemaFamily(
        model=CurrentConfig,
        name="exception_contract",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(VersionTransition("1", "2", upgrade=fail),),
    )

    with pytest.raises(ApplicationTransitionError) as raised:
        family.validate({"schema_version": "1", "value": 1})
    assert raised.value is error
