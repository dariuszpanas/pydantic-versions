from __future__ import annotations

import json
import runpy
from pathlib import Path
from typing import Any

FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "compatibility" / "v0.3.0.json"
CONTRACT_PATH = Path(__file__).parents[1] / "compatibility" / "v0_3_0_contract.py"
CONTRACT = runpy.run_path(str(CONTRACT_PATH), run_name="compatibility_v0_3_0_contract")
CONSUMER_SCHEMA = CONTRACT["CONSUMER_SCHEMA"]
PRODUCER_SCHEMA = CONTRACT["PRODUCER_SCHEMA"]
render_artifact = CONTRACT["render_artifact"]
render_expected_current_artifact = CONTRACT["_render_expected_current_artifact"]


def _fixture() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_current_code_matches_the_reviewed_v0_3_0_additive_overlay() -> None:
    baseline = FIXTURE_PATH.read_text(encoding="utf-8")
    assert render_artifact() == render_expected_current_artifact(baseline)


def test_current_code_consumes_persisted_embedded_and_transport_payloads() -> None:
    fixture = _fixture()
    payloads = fixture["payloads"]
    expected = fixture["consumer_results"]

    historical = PRODUCER_SCHEMA.validate(payloads["historical_embedded_v1"])
    assert historical.current_model.model_dump(mode="json") == payloads["normalized_from_v1"]

    exact = CONSUMER_SCHEMA.validate(payloads["exact_embedded_v2"])
    assert exact.current_model.model_dump(mode="json") == expected["exact_embedded_v2"]
    assert exact.current_model.endpoint == historical.current_model.endpoint
    assert exact.current_model.telemetry == historical.current_model.telemetry
    assert exact.current_model.credentials.region == historical.current_model.credentials.region

    transported = CONSUMER_SCHEMA.validate(payloads["exact_transport_v2"], version="2")
    assert transported.current_model.model_dump(mode="json") == expected["exact_transport_v2"]


def test_exact_and_lossy_baseline_routes_remain_visible() -> None:
    fixture = _fixture()
    payloads = fixture["payloads"]
    inspection = fixture["inspection"]

    assert inspection["render_v2_exact"]["semantics"] == "exact"
    assert inspection["render_v1_lossy"]["semantics"] == "lossy"
    assert payloads["lossy_source_v3"]["telemetry"] is False
    assert "telemetry" not in payloads["lossy_embedded_v1"]

    lossy = CONSUMER_SCHEMA.validate(payloads["lossy_embedded_v1"])
    assert (
        lossy.current_model.model_dump(mode="json")
        == fixture["consumer_results"]["lossy_embedded_v1"]
    )
    assert lossy.current_model.telemetry is True
