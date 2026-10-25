from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from benchmarks.runtime_comparison import REPORT_SCHEMA, markdown, summarize

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "benchmarks" / "runtime_comparison.py"


@pytest.fixture(scope="module")
def benchmark_report(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    directory = tmp_path_factory.mktemp("runtime-benchmark")
    output = directory / "report.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--base",
            str(ROOT / "src"),
            "--head",
            str(ROOT / "src"),
            "--quick",
            "--pairs",
            "1",
            "--rounds",
            "1",
            "--output",
            str(output),
            "--summary",
            str(directory / "summary.md"),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(output.read_text(encoding="utf-8"))


def test_comparison_checks_public_results_and_records_reproducible_samples(
    benchmark_report: dict[str, Any],
) -> None:
    assert benchmark_report["benchmark_schema"] == REPORT_SCHEMA
    assert benchmark_report["status"] == "passed"
    assert len(benchmark_report["comparison"]) == 13
    assert "compile.nested" in benchmark_report["comparison"]
    for run in benchmark_report["runs"]:
        assert Path(run["package_path"]).is_relative_to(ROOT / "src")
        assert run["environment"]["gc_enabled"] is True
        assert len(run["observations"]["outputs"]) == 13
        assert len(run["observations"]["contracts"]) == 3
        assert all(len(samples) == 1 for samples in run["samples_us"].values())


@pytest.mark.parametrize("key", ["observations", "environment", "iterations_per_sample"])
def test_comparison_rejects_incomparable_runs(
    benchmark_report: dict[str, Any],
    key: str,
) -> None:
    runs = copy.deepcopy(benchmark_report["runs"])
    runs[1][key] = {}
    with pytest.raises(ValueError, match="differ"):
        summarize(runs)


def test_timing_changes_are_reported_without_a_speed_gate(benchmark_report: dict[str, Any]) -> None:
    runs = copy.deepcopy(benchmark_report["runs"])
    for name in runs[0]["samples_us"]:
        runs[0]["samples_us"][name] = [1.0]
        runs[1]["samples_us"][name] = [100.0]
    comparison = summarize(runs)
    assert all(value["median_paired_ratio"] == 100 for value in comparison.values())
    rendered = markdown({**benchmark_report, "comparison": comparison})
    assert "100.000×" in rendered
    assert "informational" in rendered


def test_worker_failure_returns_nonzero_and_writes_failure_artifact(tmp_path: Path) -> None:
    output = tmp_path / "report.json"
    summary = tmp_path / "summary.md"
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--base",
            str(tmp_path / "missing-source"),
            "--head",
            str(ROOT / "src"),
            "--quick",
            "--pairs",
            "1",
            "--rounds",
            "1",
            "--output",
            str(output),
            "--summary",
            str(summary),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 1
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert "worker failed" in report["error"]
    assert "not a valid comparison" in summary.read_text(encoding="utf-8")
