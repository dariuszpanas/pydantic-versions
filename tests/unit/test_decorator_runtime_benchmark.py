from __future__ import annotations

import json
from pathlib import Path

import pydantic_versions
from benchmarks.decorator_runtime import REPORT_SCHEMA, run_benchmarks


def test_decorator_runtime_benchmark_proves_correctness_without_speed_thresholds() -> None:
    assert pydantic_versions.__file__ is not None
    package_file = Path(pydantic_versions.__file__).resolve()
    source_root = package_file.parent.parent
    report = run_benchmarks(
        counts=(7,),
        union_routes=4,
        union_values_per_route=2,
        repeats=1,
        warmups=0,
        label="unit-test",
        source_root=source_root,
    )

    assert report["benchmark_schema"] == REPORT_SCHEMA
    assert report["environment"]["pydantic_versions_module"] == str(package_file)
    assert report["environment"]["source_root"] == str(source_root)
    assert report["configuration"] == {
        "counts": [7],
        "repeats": 1,
        "union_routes": 4,
        "union_values_per_route": 2,
        "warmups": 0,
    }
    assert [result["case"] for result in report["results"]] == [
        "reconcile_reordered_occurrences",
        "select_multi_route_union",
    ]
    assert report["results"][0]["correctness"] == {
        "locations": 7,
        "payload_first": 6,
        "payload_last": 0,
        "unique_value_identities": 7,
    }
    assert report["results"][1]["correctness"] == {
        "locations": 8,
        "selected_families": 4,
        "site_routes": 4,
    }
    assert len(report["results"][0]["samples_ns"]) == 1
    assert len(report["results"][1]["samples_ns"]) == 1
    assert json.loads(json.dumps(report)) == report
