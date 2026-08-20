from __future__ import annotations

from typing import Literal

import pytest
from pydantic import BaseModel

from pydantic_versions import (
    NestedFamily,
    SchemaFamily,
    SchemaVersion,
    UnsupportedWireModelError,
    VersionMetadata,
    field_default,
    field_renamed,
    matching_labels,
    validate_versioned,
)


class GeneralConfig(BaseModel):
    workers: int = 2


class ResultsConfig(BaseModel):
    score: float = 0.0


class BenchmarkConfig[T: GeneralConfig](BaseModel):
    general: T
    results: ResultsConfig
    test_type: Literal["cpu", "gpu"] = "cpu"


GENERAL_SCHEMA = SchemaFamily(
    model=GeneralConfig,
    name="benchmark_general",
    versions=(
        SchemaVersion(
            "v1",
            patches=(field_renamed("workers", "threads"),),
        ),
        SchemaVersion("v2"),
    ),
    version_metadata=VersionMetadata("schema_version", owner="family"),
)

RESULTS_SCHEMA = SchemaFamily(
    model=ResultsConfig,
    name="benchmark_results",
    versions=(
        SchemaVersion("v1", patches=(field_default("score", -1.0),)),
        SchemaVersion("v2"),
    ),
    version_metadata=VersionMetadata("schema_version", owner="family"),
)

BENCHMARK_SCHEMA = SchemaFamily(
    model=BenchmarkConfig[GeneralConfig],
    name="benchmark_config",
    versions=(SchemaVersion("v1"), SchemaVersion("v2")),
    nested=(
        NestedFamily("general", GENERAL_SCHEMA, matching_labels()),
        NestedFamily("results", RESULTS_SCHEMA, matching_labels()),
    ),
    version_metadata=VersionMetadata("schema_version", owner="family"),
)


def test_nested_concrete_generic_consumer_contract() -> None:
    legacy_payload = {
        "schema_version": "v1",
        "general": {"schema_version": "v1", "threads": 8},
        "results": {"schema_version": "v1"},
        "test_type": "cpu",
    }

    validated = validate_versioned(BENCHMARK_SCHEMA, legacy_payload)

    assert isinstance(validated.current_model, BenchmarkConfig)
    assert validated.current_model.general.workers == 8
    assert validated.current_model.results.score == -1.0
    assert validated.current_model.test_type == "cpu"
    assert validated.source_version == "v1"


def test_unresolved_generic_consumer_base_is_rejected() -> None:
    unresolved = SchemaFamily(
        model=BenchmarkConfig,
        name="unresolved_benchmark_config",
        versions=(SchemaVersion("v1"),),
        version_metadata=None,
    )

    with pytest.raises(UnsupportedWireModelError, match="unresolved generic"):
        unresolved.compile()


def test_nested_concrete_generic_wire_projection_uses_child_versions() -> None:
    legacy_wire = BENCHMARK_SCHEMA.model_for("v1")
    current_wire = BENCHMARK_SCHEMA.model_for("v2")

    legacy = legacy_wire.model_validate(
        {
            "general": {"schema_version": "v1", "threads": 4},
            "results": {"schema_version": "v1"},
        },
    )
    current = current_wire.model_validate(
        {
            "general": {"schema_version": "v2", "workers": 4},
            "results": {"schema_version": "v2", "score": 0.75},
        },
    )

    assert legacy.model_dump()["general"] == {
        "schema_version": "v1",
        "threads": 4,
    }
    assert legacy.model_dump()["results"] == {
        "schema_version": "v1",
        "score": -1.0,
    }
    assert current.model_dump()["general"] == {
        "schema_version": "v2",
        "workers": 4,
    }
    assert current.model_dump()["results"] == {
        "schema_version": "v2",
        "score": 0.75,
    }
