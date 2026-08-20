from __future__ import annotations

from typing import Any, Self, cast

import pytest
from pydantic import BaseModel, ValidationError, field_validator, model_validator

from pydantic_versions import SchemaFamily, SchemaVersion


def test_generated_wire_validation_is_separate_from_authoritative_validator_behavior() -> None:
    events: list[str] = []

    class BoundaryPayload(BaseModel):
        values: list[int]
        plain_value: int
        wrapped_value: int
        seed: int
        after_marker: int = 0

        @field_validator("values", mode="before")
        @classmethod
        def split_values(cls, value: Any) -> Any:
            events.append("before")
            if isinstance(value, str):
                return [int(item) for item in value.split(",")]
            return list(reversed(value))

        @field_validator("plain_value", mode="plain")
        @classmethod
        def parse_plain_value(cls, value: Any) -> int:
            events.append("plain")
            if value == "seven":
                return 7
            return int(value) + 1

        @field_validator("wrapped_value", mode="wrap")
        @classmethod
        def parse_wrapped_value(cls, value: Any, handler: Any) -> int:
            events.append("wrap")
            return handler(value) + 1

        @field_validator("after_marker", mode="after")
        @classmethod
        def mark_after_validation(cls, value: int) -> int:
            events.append("after")
            return value + 1

        @model_validator(mode="before")
        @classmethod
        def add_missing_seed(cls, value: Any) -> Any:
            events.append("model-before")
            return value

        @model_validator(mode="after")
        def require_matching_seed(self) -> Self:
            events.append("model-after")
            if self.seed != 42:
                raise ValueError("seed must be 42")
            return self

    family = SchemaFamily(
        model=BoundaryPayload,
        name="validator_boundary",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )
    wire = family.model_for("1")

    schema = wire.model_json_schema()
    assert schema["properties"]["values"] == {
        "items": {"type": "integer"},
        "title": "Values",
        "type": "array",
    }
    assert schema["properties"]["plain_value"]["type"] == "integer"
    assert schema["properties"]["wrapped_value"]["type"] == "integer"
    assert "seed" in schema["required"]

    raw_payload = {
        "values": "1,2",
        "plain_value": "seven",
        "wrapped_value": "eight",
        "seed": 42,
    }
    with pytest.raises(ValidationError):
        wire.model_validate(raw_payload)
    with pytest.raises(ValidationError):
        family.validate(raw_payload, version="1")
    assert events == []

    direct_wire = cast(
        Any,
        wire.model_validate(
            {
                "values": [1, 2],
                "plain_value": 7,
                "wrapped_value": 8,
                "seed": 42,
            }
        ),
    )
    assert direct_wire.values == [1, 2]
    assert direct_wire.plain_value == 7
    assert direct_wire.wrapped_value == 8
    assert direct_wire.after_marker == 0
    assert events == []

    result = family.validate(
        {
            "values": [1, 2],
            "plain_value": 7,
            "wrapped_value": 8,
            "seed": 42,
        },
        version="1",
    )

    source_model = cast(Any, result.source_model)
    current_model = cast(Any, result.current_model)
    assert source_model.values == [1, 2]
    assert source_model.plain_value == 7
    assert source_model.wrapped_value == 8
    assert source_model.seed == 42
    assert source_model.after_marker == 0
    assert current_model.values == [2, 1]
    assert current_model.plain_value == 8
    assert current_model.wrapped_value == 9
    assert current_model.after_marker == 1
    assert events == ["model-before", "before", "plain", "wrap", "after", "model-after"]
