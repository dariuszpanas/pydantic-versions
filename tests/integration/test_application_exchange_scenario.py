from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass

import pytest
from pydantic import BaseModel

from pydantic_versions import (
    IrreversibleTransitionError,
    SchemaFamily,
    SchemaVersion,
    field_default,
    field_removed,
    field_renamed,
)

SCHEMA_ID = "com.example.pipeline-config"


class ProducerConfig(BaseModel):
    endpoint: str
    timeout: int = 30
    retry_limit: int = 3
    telemetry: bool = True


PRODUCER_SCHEMA = SchemaFamily(
    model=ProducerConfig,
    name=SCHEMA_ID,
    versions=(
        SchemaVersion(
            "1",
            patches=(
                field_default("timeout", 5),
                field_renamed("retry_limit", "attempts"),
                field_removed("telemetry"),
            ),
        ),
        SchemaVersion(
            "2",
            patches=(
                field_default("timeout", 10),
                field_renamed("retry_limit", "retries"),
            ),
        ),
        SchemaVersion("3"),
    ),
)


class ConsumerConfig(BaseModel):
    endpoint: str
    timeout: int = 10
    retries: int = 3
    telemetry: bool = True


CONSUMER_SCHEMA = SchemaFamily(
    model=ConsumerConfig,
    name=SCHEMA_ID,
    versions=(
        SchemaVersion(
            "1",
            patches=(
                field_default("timeout", 5),
                field_renamed("retries", "attempts"),
                field_removed("telemetry"),
            ),
        ),
        SchemaVersion("2"),
    ),
)


@dataclass(frozen=True)
class ConsumerCapabilities:
    schema: str
    accepts: tuple[str, ...]


class NoCompatibleSchemaVersionError(RuntimeError):
    pass


def _select_target_version(
    producer: SchemaFamily[ProducerConfig],
    consumer: ConsumerCapabilities,
    *,
    allow_lossy: bool = False,
) -> str:
    if consumer.schema != producer.name:
        raise NoCompatibleSchemaVersionError

    accepted = set(consumer.accepts)
    for declaration in reversed(producer.versions):
        if declaration.label not in accepted:
            continue
        try:
            plan = producer.plan_render(declaration.label)
        except IrreversibleTransitionError:
            continue
        if plan.semantics == "exact" or allow_lossy:
            return declaration.label
    raise NoCompatibleSchemaVersionError


def _consumer_capabilities(accepts: Iterable[str] = ("1", "2")) -> ConsumerCapabilities:
    return ConsumerCapabilities(schema=CONSUMER_SCHEMA.name, accepts=tuple(accepts))


def test_independent_applications_exchange_the_newest_exact_common_version(tmp_path) -> None:
    historical_input = {
        "schema_version": "1",
        "endpoint": "https://worker.example",
        "attempts": 4,
    }
    normalized = PRODUCER_SCHEMA.validate(historical_input).current_model
    target = _select_target_version(PRODUCER_SCHEMA, _consumer_capabilities())

    assert target == "2"
    assert PRODUCER_SCHEMA.plan_render(target).semantics == "exact"
    assert normalized.timeout == 5
    assert normalized.retry_limit == 4
    assert normalized.telemetry is True

    outgoing = PRODUCER_SCHEMA.dump(version=target, data=normalized)
    exchange_path = tmp_path / "pipeline-config.json"
    exchange_path.write_text(json.dumps(outgoing), encoding="utf-8")

    serialized = json.loads(exchange_path.read_text(encoding="utf-8"))
    received = CONSUMER_SCHEMA.validate(serialized)

    assert serialized == {
        "endpoint": "https://worker.example",
        "timeout": 5,
        "retries": 4,
        "telemetry": True,
        "schema_version": "2",
    }
    assert type(received.current_model) is ConsumerConfig
    assert received.current_model == ConsumerConfig(
        endpoint="https://worker.example",
        timeout=5,
        retries=4,
        telemetry=True,
    )


def test_exchange_refuses_no_overlap_and_requires_opt_in_for_lossy_output() -> None:
    with pytest.raises(NoCompatibleSchemaVersionError):
        _select_target_version(PRODUCER_SCHEMA, _consumer_capabilities(("legacy",)))

    version_one_only = _consumer_capabilities(("1",))
    assert PRODUCER_SCHEMA.plan_render("1").semantics == "lossy"

    with pytest.raises(NoCompatibleSchemaVersionError):
        _select_target_version(PRODUCER_SCHEMA, version_one_only)

    assert _select_target_version(PRODUCER_SCHEMA, version_one_only, allow_lossy=True) == "1"


def test_exchange_supports_transport_owned_version_metadata() -> None:
    normalized = ProducerConfig(
        endpoint="https://worker.example",
        timeout=12,
        retry_limit=6,
    )
    payload = PRODUCER_SCHEMA.dump(
        version="2",
        data=normalized,
        include_version=False,
    )

    assert "schema_version" not in payload
    received = CONSUMER_SCHEMA.validate(payload, version="2")
    assert received.current_model.retries == 6
    assert received.current_model.timeout == 12
