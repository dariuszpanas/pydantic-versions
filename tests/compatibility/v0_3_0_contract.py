from __future__ import annotations

import argparse
import difflib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from pydantic_versions import (
    NestedFamily,
    SchemaFamily,
    SchemaVersion,
    VersionTransition,
    __version__,
    field_default,
    field_removed,
    field_renamed,
)

BASELINE_VERSION = "0.3.0"
BASELINE_TAG = "v0.3.0"
BASELINE_COMMIT = "a03e67e5adc83e0087773383c3d55ed9e8da9bde"
SCHEMA_NAME = "com.example.golden-service-config"
FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "compatibility" / "v0.3.0.json"

_REVIEWED_NESTED_PLAN_OVERLAY: tuple[tuple[str, str, dict[str, Any]], ...] = (
    (
        "validate_v1",
        "pv1-4e531e5e4ad4f54c2971167c7da787f2d33e96f8713ee5fb9d9f3af51514b32c",
        {
            "id": "pv1-0f03ddf9ebd7693766aa6232192adaeb02b20634cb1f5bf871ad92d9d93e833d",
            "family": SCHEMA_NAME,
            "source_version": "1",
            "target_version": "2",
            "operation": "validate",
            "direction": "upgrade",
            "kind": "nested",
            "schema_path": "$.credentials",
            "semantics": "not_applicable",
            "conditional": True,
        },
    ),
    (
        "render_v1_lossy",
        "pv1-e4054d80fff357a46244b427c480e943b37a1481c17c471d50296f94ae7f5374",
        {
            "id": "pv1-dd8410f673330912f3b42b32b6722102dea8ba7e9d3b04d8b121d93f44aa214a",
            "family": SCHEMA_NAME,
            "source_version": "2",
            "target_version": "1",
            "operation": "render",
            "direction": "downgrade",
            "kind": "nested",
            "schema_path": "$.credentials",
            "semantics": "exact",
            "conditional": True,
        },
    ),
)


def _upgrade_endpoint(data: dict[str, Any]) -> dict[str, Any]:
    upgraded = dict(data)
    endpoint = upgraded.get("endpoint")
    if isinstance(endpoint, str) and "://" not in endpoint:
        upgraded["endpoint"] = f"https://{endpoint}"
    return upgraded


def _downgrade_endpoint(data: dict[str, Any]) -> dict[str, Any]:
    downgraded = dict(data)
    endpoint = downgraded.get("endpoint")
    if isinstance(endpoint, str) and endpoint.startswith("https://"):
        downgraded["endpoint"] = endpoint.removeprefix("https://")
    return downgraded


class ProducerCredentials(BaseModel):
    token: str
    region: str = "us-east-1"


class ProducerConfig(BaseModel):
    endpoint: str
    timeout: int = 30
    retry_limit: int = 3
    telemetry: bool = True
    credentials: ProducerCredentials


class ConsumerCredentials(BaseModel):
    token: str
    region: str = "us-east-1"


class ConsumerConfig(BaseModel):
    endpoint: str
    timeout: int = 10
    retries: int = 3
    telemetry: bool = True
    credentials: ConsumerCredentials


# Keep inventory model names independent of whether this file is imported or
# executed directly during baseline regeneration.
for _model in (
    ProducerCredentials,
    ProducerConfig,
    ConsumerCredentials,
    ConsumerConfig,
):
    _model.__module__ = "pydantic_versions_compatibility_v0_3_0"


PRODUCER_CREDENTIALS = SchemaFamily(
    model=ProducerCredentials,
    name=f"{SCHEMA_NAME}.credentials",
    versions=(
        SchemaVersion("1"),
        SchemaVersion("2"),
    ),
)

PRODUCER_SCHEMA = SchemaFamily(
    model=ProducerConfig,
    name=SCHEMA_NAME,
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
    transitions=(
        VersionTransition(
            "1",
            "2",
            upgrade=_upgrade_endpoint,
            downgrade=_downgrade_endpoint,
            downgrade_semantics="exact",
        ),
    ),
    nested=(
        NestedFamily(
            "credentials",
            PRODUCER_CREDENTIALS,
            {"1": "1", "2": "2", "3": "2"},
        ),
    ),
)

CONSUMER_CREDENTIALS = SchemaFamily(
    model=ConsumerCredentials,
    name=f"{SCHEMA_NAME}.credentials",
    versions=(
        SchemaVersion("1"),
        SchemaVersion("2"),
    ),
)

CONSUMER_SCHEMA = SchemaFamily(
    model=ConsumerConfig,
    name=SCHEMA_NAME,
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
    transitions=(
        VersionTransition(
            "1",
            "2",
            upgrade=_upgrade_endpoint,
            downgrade=_downgrade_endpoint,
            downgrade_semantics="exact",
        ),
    ),
    nested=(
        NestedFamily(
            "credentials",
            CONSUMER_CREDENTIALS,
            {"1": "1", "2": "2"},
        ),
    ),
)


def build_artifact() -> dict[str, Any]:
    historical_embedded = {
        "schema_version": "1",
        "endpoint": "worker.example",
        "attempts": 4,
        "credentials": {
            "schema_version": "1",
            "token": "fixture-token",
            "region": "eu-west-1",
        },
    }
    normalized = PRODUCER_SCHEMA.validate(historical_embedded).current_model
    current_with_loss = ProducerConfig(
        endpoint="https://worker.example",
        timeout=12,
        retry_limit=6,
        telemetry=False,
        credentials=ProducerCredentials(
            token="fixture-token",
            region="eu-west-1",
        ),
    )
    exact_embedded = PRODUCER_SCHEMA.dump(version="2", data=normalized)
    exact_transport = PRODUCER_SCHEMA.dump(
        version="2",
        data=normalized,
        include_version=False,
    )
    lossy_embedded = PRODUCER_SCHEMA.dump(version="1", data=current_with_loss)

    exact_received = CONSUMER_SCHEMA.validate(exact_embedded).current_model
    transport_received = CONSUMER_SCHEMA.validate(
        exact_transport,
        version="2",
    ).current_model
    lossy_received = CONSUMER_SCHEMA.validate(lossy_embedded).current_model

    return {
        "provenance": {
            "package": "pydantic-versions",
            "version": BASELINE_VERSION,
            "git_tag": BASELINE_TAG,
            "git_commit": BASELINE_COMMIT,
            "artifact_format": 1,
        },
        "payloads": {
            "historical_embedded_v1": historical_embedded,
            "normalized_from_v1": normalized.model_dump(mode="json"),
            "exact_embedded_v2": exact_embedded,
            "exact_transport_v2": exact_transport,
            "lossy_source_v3": current_with_loss.model_dump(mode="json"),
            "lossy_embedded_v1": lossy_embedded,
        },
        "consumer_results": {
            "exact_embedded_v2": exact_received.model_dump(mode="json"),
            "exact_transport_v2": transport_received.model_dump(mode="json"),
            "lossy_embedded_v1": lossy_received.model_dump(mode="json"),
        },
        "inspection": {
            "producer_inventory": PRODUCER_SCHEMA.describe().to_dict(),
            "credentials_inventory": PRODUCER_CREDENTIALS.describe().to_dict(),
            "validate_v1": PRODUCER_SCHEMA.plan_validation("1").to_dict(),
            "render_v2_exact": PRODUCER_SCHEMA.plan_render("2").to_dict(),
            "render_v1_lossy": PRODUCER_SCHEMA.plan_render("1").to_dict(),
        },
    }


def render_artifact() -> str:
    return f"{json.dumps(build_artifact(), indent=2)}\n"


def _expected_current_artifact(baseline: dict[str, Any]) -> dict[str, Any]:
    expected = deepcopy(baseline)
    inspection = expected["inspection"]
    for plan_name, owning_step_id, nested_step in _REVIEWED_NESTED_PLAN_OVERLAY:
        steps = inspection[plan_name]["steps"]
        owning_indexes = [index for index, step in enumerate(steps) if step["id"] == owning_step_id]
        if len(owning_indexes) != 1:
            msg = (
                f"Frozen v0.3.0 plan {plan_name!r} must contain exactly one owning "
                f"step {owning_step_id!r}"
            )
            raise RuntimeError(msg)
        steps.insert(owning_indexes[0], deepcopy(nested_step))
    return expected


def _render_expected_current_artifact(baseline: str) -> str:
    expected = _expected_current_artifact(json.loads(baseline))
    return f"{json.dumps(expected, indent=2)}\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true")
    action.add_argument("--write", action="store_true")
    args = parser.parse_args()
    if args.write:
        if __version__ != BASELINE_VERSION:
            parser.error(
                f"--write requires pydantic-versions=={BASELINE_VERSION}; found {__version__}"
            )
        FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE_PATH.write_text(render_artifact(), encoding="utf-8")
        return 0

    committed = FIXTURE_PATH.read_text(encoding="utf-8")
    expected_rendered = _render_expected_current_artifact(committed)
    current_rendered = render_artifact()
    if current_rendered == expected_rendered:
        return 0
    print(
        "".join(
            difflib.unified_diff(
                expected_rendered.splitlines(keepends=True),
                current_rendered.splitlines(keepends=True),
                fromfile="reviewed v0.3.0 contract plus nested-plan overlay",
                tofile="current contract",
            )
        )
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
