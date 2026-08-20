from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from pydantic_versions import SchemaFamily, SchemaVersion


class CpuTarget(BaseModel):
    test_type: Literal["cpu"]
    workers: int


class GpuTarget(BaseModel):
    test_type: Literal["gpu"]
    device: str


class BenchmarkPayload(BaseModel):
    target: CpuTarget | GpuTarget = Field(discriminator="test_type")


BENCHMARK_SCHEMA = SchemaFamily(
    model=BenchmarkPayload,
    name="content_discriminator_benchmark",
    versions=(SchemaVersion("v1"), SchemaVersion("v2")),
    version_metadata=None,
)


def test_content_discriminator_survives_each_wire_projection() -> None:
    for version in ("v1", "v2"):
        wire = BENCHMARK_SCHEMA.model_for(version)
        schema = wire.model_json_schema()
        target_schema = schema["properties"]["target"]

        assert target_schema["discriminator"] == {
            "mapping": {
                "cpu": "#/$defs/CpuTarget",
                "gpu": "#/$defs/GpuTarget",
            },
            "propertyName": "test_type",
        }
        assert wire.model_validate(
            {"target": {"test_type": "cpu", "workers": 8}},
        ).model_dump() == {"target": {"test_type": "cpu", "workers": 8}}
        assert wire.model_validate(
            {"target": {"test_type": "gpu", "device": "cuda:0"}},
        ).model_dump() == {"target": {"test_type": "gpu", "device": "cuda:0"}}
