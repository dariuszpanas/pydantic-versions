"""Fixed public-API workloads shared by base and head benchmark workers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel, Field

from pydantic_versions import (
    NestedFamily,
    SchemaFamily,
    SchemaVersion,
    VersionedValidation,
    VersionTransition,
    field_default,
    field_removed,
    field_renamed,
    matching_labels,
)


class FlatConfig(BaseModel):
    host: str = "localhost"
    port: int = 8080
    timeout: float = 5.0
    retries: int = 3
    tls_enabled: bool = True
    region: str = "us-east-1"


class RichEvent(BaseModel):
    event_id: UUID = Field(alias="eventId")
    correlation_id: UUID = Field(alias="correlationId")
    amount: float = Field(alias="totalAmount")
    tags: set[str] = Field(default_factory=set)
    metrics: dict[str, float] = Field(default_factory=dict)


class RetryPolicy(BaseModel):
    max_attempts: int = 3
    backoff_ms: int = 100
    jitter: bool = True


class WorkerNode(BaseModel):
    name: str
    concurrency: int = 4
    retry: RetryPolicy


class ClusterConfig(BaseModel):
    cluster_name: str
    workers: list[WorkerNode]


@dataclass(frozen=True)
class Case:
    name: str
    operation: Callable[[], Any]
    iterations: int
    expected: Any


def observe(value: Any) -> Any:
    """Canonicalize observations, preserving ordered collections and scalar types."""
    if isinstance(value, VersionedValidation):
        return observe(
            {
                "current_model": value.current_model.model_dump(mode="json"),
                "source_version": value.source_version,
                "current_version": value.current_version,
                "migrations_applied": value.migrations_applied,
            }
        )
    if isinstance(value, SchemaFamily):
        return observe(value.describe().to_dict())
    if isinstance(value, dict):
        # RichEvent.tags is a set; JSON array order is not part of that contract.
        return {
            key: sorted(item) if key == "tags" and isinstance(item, list) else observe(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [observe(item) for item in value]
    return value


def fresh_nested_family() -> SchemaFamily[ClusterConfig]:
    retry = SchemaFamily(
        model=RetryPolicy,
        name="retry_policy",
        versions=(
            SchemaVersion(
                "1", patches=(field_renamed("max_attempts", "attempts"), field_removed("jitter"))
            ),
            SchemaVersion("2", patches=(field_default("jitter", False),)),
            SchemaVersion("3"),
        ),
        transitions=(
            VersionTransition(
                "1",
                "2",
                upgrade=lambda data: {**data, "jitter": False},
                downgrade=lambda data: {
                    key: value for key, value in data.items() if key != "jitter"
                },
                downgrade_semantics="lossy",
            ),
        ),
    )
    return SchemaFamily(
        model=ClusterConfig,
        name="cluster_config",
        versions=(
            SchemaVersion("1", patches=(field_renamed("cluster_name", "name"),)),
            SchemaVersion("2"),
            SchemaVersion("3"),
        ),
        nested=(NestedFamily(("workers", "retry"), retry, matching_labels()),),
    ).compile()


def build_cases() -> tuple[list[Case], dict[str, Any]]:
    flat = SchemaFamily(
        model=FlatConfig,
        name="flat_config",
        versions=(
            SchemaVersion(
                "1",
                patches=(
                    field_renamed("host", "server_host"),
                    field_removed("retries"),
                    field_removed("region"),
                ),
            ),
            SchemaVersion("2", patches=(field_removed("region"),)),
            SchemaVersion("3"),
        ),
        transitions=(
            VersionTransition(
                "1",
                "2",
                upgrade=lambda data: {"retries": 3, **data},
                downgrade=lambda data: {
                    key: value for key, value in data.items() if key != "retries"
                },
                downgrade_semantics="lossy",
            ),
            VersionTransition(
                "2",
                "3",
                upgrade=lambda data: {"region": "us-east-1", **data},
                downgrade=lambda data: {
                    key: value for key, value in data.items() if key != "region"
                },
                downgrade_semantics="lossy",
            ),
        ),
    ).compile()
    rich = SchemaFamily(
        model=RichEvent,
        name="rich_event",
        versions=(
            SchemaVersion("1", patches=(field_default("tags", default_factory=set),)),
            SchemaVersion("2"),
        ),
    ).compile()
    nested = fresh_nested_family()
    flat_raw = {
        "host": "db.internal",
        "port": 5432,
        "timeout": 2.5,
        "retries": 5,
        "tls_enabled": True,
        "region": "eu-west-1",
    }
    flat_old = {"server_host": "db.internal", "port": 5432, "timeout": 2.5, "tls_enabled": True}
    rich_raw = {
        "eventId": "12345678-1234-5678-1234-567812345678",
        "correlationId": "87654321-4321-8765-4321-876543218765",
        "totalAmount": 1234.56,
        "tags": ["alpha", "beta", "gamma", "delta"],
        "metrics": {"p50": 1.2, "p95": 4.5, "p99": 9.8},
    }
    cluster_raw: dict[str, Any] = {
        "cluster_name": "prod-us",
        "workers": [
            {
                "name": f"worker-{index}",
                "concurrency": 8,
                "retry": {"max_attempts": 5, "backoff_ms": 200, "jitter": True},
            }
            for index in range(10)
        ],
    }
    cluster_old = {
        "name": "prod-us",
        "workers": [
            {"name": worker["name"], "concurrency": 8, "retry": {"attempts": 5, "backoff_ms": 200}}
            for worker in cluster_raw["workers"]
        ],
    }
    cluster_upgraded = {
        "cluster_name": "prod-us",
        "workers": [
            {
                "name": f"worker-{index}",
                "concurrency": 8,
                "retry": {"max_attempts": 5, "backoff_ms": 200, "jitter": False},
            }
            for index in range(10)
        ],
    }
    cases: list[Case] = []
    contracts: dict[str, Any] = {}
    workloads: tuple[
        tuple[str, SchemaFamily[Any], dict[str, Any], dict[str, Any], dict[str, Any], int], ...
    ] = (
        ("flat", flat, flat_raw, flat_old, {**flat_raw, "retries": 3, "region": "us-east-1"}, 500),
        ("rich", rich, rich_raw, rich_raw, rich_raw, 250),
        ("nested", nested, cluster_raw, cluster_old, cluster_upgraded, 30),
    )
    for name, declared_family, raw, historical, upgraded, iterations in workloads:
        family = cast(SchemaFamily[Any], declared_family)
        instance = family.model.model_validate(raw)
        current = family.current_version
        contracts[name] = {"inventory": family.describe().to_dict()}
        for label in ("1", current):
            contracts[name][label] = {
                "schema": family.model_for(label).model_json_schema(),
                "validation_plan": family.plan_validation(label).to_dict(),
                "render_plan": family.plan_render(label).to_dict(),
            }
            route = "current" if label == current else "historical"
            payload = {**(raw if route == "current" else historical), "schema_version": label}
            expected_model = family.model.model_validate(raw if route == "current" else upgraded)
            cases.append(
                Case(
                    f"{name}.validate.{route}",
                    partial(family.validate, payload),
                    iterations,
                    observe(
                        {
                            "current_model": expected_model.model_dump(mode="json"),
                            "source_version": label,
                            "current_version": current,
                            "migrations_applied": [["1", "2"], ["2", "3"]]
                            if name == "flat" and route == "historical"
                            else [],
                        }
                    ),
                )
            )
            cases.append(
                Case(
                    f"{name}.dump.{route}",
                    partial(family.dump, version=label, data=instance),
                    iterations,
                    observe(payload),
                )
            )
    cases.append(Case("compile.nested", fresh_nested_family, 3, observe(nested)))
    return cases, observe(contracts)
