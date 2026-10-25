"""Compare exact base/head sources in alternating, isolated Python processes."""

from __future__ import annotations

import argparse
import gc
import hashlib
import importlib.metadata
import json
import os
import platform
import random
import subprocess
import sys
import tomllib
from pathlib import Path
from statistics import median
from time import perf_counter_ns
from typing import Any

REPORT_SCHEMA = "pydantic-versions.runtime-comparison.v1"
DEPENDENCIES = ("pydantic", "pydantic-core", "annotated-types", "typing-extensions")


def source_state(source: Path) -> dict[str, Any]:
    def git(*args: str) -> str | None:
        result = subprocess.run(
            ["git", "-C", str(source.parent), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    metadata = tomllib.loads((source.parent / "pyproject.toml").read_text(encoding="utf-8"))
    dirty = git("status", "--porcelain", "--untracked-files=no")
    return {
        "source_root": str(source),
        "revision": git("rev-parse", "HEAD"),
        "tracked_files_dirty": None if dirty is None else bool(dirty),
        "project_version": metadata["project"]["version"],
    }


def worker(source: Path, *, rounds: int, seed: int, quick: bool) -> dict[str, Any]:
    source = source.resolve()
    sys.path.insert(0, str(source))
    import pydantic_versions

    package_path = Path(str(pydantic_versions.__file__)).resolve()
    if not package_path.is_relative_to(source):
        raise RuntimeError(f"Package imported outside selected source: {package_path}")
    # The harness always comes from head; only the library source varies.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from runtime_workloads import build_cases, observe

    cases, contracts = build_cases()
    expected = {case.name: case.expected for case in cases}
    gc.enable()
    warmups = 1 if quick else 5
    for case in cases:
        if observe(case.operation()) != case.expected:
            raise AssertionError(f"Incorrect expected result: {case.name}")
        for _ in range(warmups):
            case.operation()
    samples: dict[str, list[float]] = {case.name: [] for case in cases}
    iterations = {case.name: 1 if quick else case.iterations for case in cases}
    rng = random.Random(seed)
    for _ in range(rounds):
        order = list(cases)
        rng.shuffle(order)
        for case in order:
            gc.collect()
            count = iterations[case.name]
            started = perf_counter_ns()
            result: Any = None
            for _ in range(count):
                result = case.operation()
            samples[case.name].append((perf_counter_ns() - started) / count / 1_000)
            if observe(result) != case.expected:
                raise AssertionError(f"Result changed during measurement: {case.name}")
    return {
        "source": source_state(source),
        "package_path": str(package_path),
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "dependencies": {name: importlib.metadata.version(name) for name in DEPENDENCIES},
            "hash_seed": os.environ.get("PYTHONHASHSEED"),
            "gc_enabled": gc.isenabled(),
        },
        "rounds": rounds,
        "warmups": warmups,
        "iterations_per_sample": iterations,
        "observations": {"outputs": expected, "contracts": contracts},
        "samples_us": samples,
    }


def summarize(runs: list[dict[str, Any]]) -> dict[str, Any]:
    if not runs or len(runs) % 2:
        raise ValueError("A complete base/head pair is required")
    reference = runs[0]
    for run in runs:
        if run["environment"] != reference["environment"]:
            raise ValueError("Base/head environments differ")
        if run["observations"] != reference["observations"]:
            raise ValueError("Base/head outputs, schemas, inventories, or plans differ")
        if run["iterations_per_sample"] != reference["iterations_per_sample"]:
            raise ValueError("Base/head iteration counts differ")
    pairs: dict[int, dict[str, Any]] = {}
    for run in runs:
        pair = pairs.setdefault(run["pair"], {})
        if run["side"] not in ("base", "head") or run["side"] in pair:
            raise ValueError("Invalid or repeated benchmark side")
        pair[run["side"]] = run
    if any(set(pair) != {"base", "head"} for pair in pairs.values()):
        raise ValueError("Incomplete base/head pair")
    result: dict[str, Any] = {}
    for name in reference["samples_us"]:
        grouped = {
            side: [median(pair[side]["samples_us"][name]) for pair in pairs.values()]
            for side in ("base", "head")
        }
        ratios = [head / base for base, head in zip(grouped["base"], grouped["head"], strict=True)]
        result[name] = {
            "base_median_us": median(grouped["base"]),
            "head_median_us": median(grouped["head"]),
            "paired_head_over_base": ratios,
            "median_paired_ratio": median(ratios),
            "process_medians_us": grouped,
        }
    return result


def markdown(report: dict[str, Any]) -> str:
    lines = ["## Runtime benchmark comparison", ""]
    if report["status"] != "passed":
        lines += [
            "**Correctness or harness check failed.** Timings are not a valid comparison.",
            "",
        ]
        return "\n".join(lines)
    env = report["runs"][0]["environment"]
    sources = {run["side"]: run["source"] for run in report["runs"]}
    lines += [
        f"Base `{sources['base']['revision']}` ({sources['base']['project_version']}) → "
        f"head `{sources['head']['revision']}` ({sources['head']['project_version']}).",
        f"Python {env['python']}; Pydantic {env['dependencies']['pydantic']}; {env['platform']}.",
        f"{report['pairs']} alternating process pairs; {report['rounds']} samples per case/process.",
        "One head-locked dependency environment; identical outputs and schema/plan observations.",
        "",
        "| Case | Base µs/op | Head µs/op | Paired head/base | Pair range |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for name, values in report["comparison"].items():
        ratios = values["paired_head_over_base"]
        lines.append(
            f"| {name} | {values['base_median_us']:.2f} | {values['head_median_us']:.2f} | "
            f"{values['median_paired_ratio']:.3f}× | {min(ratios):.3f}–{max(ratios):.3f}× |"
        )
    lines += [
        "",
        "A ratio above 1 means a slower head in this run. Timing differences are informational;",
        "runner noise, workload, and process variation require repeat investigation before attribution.",
        "These are batch-average operation timings, not request tail latency or concurrent throughput.",
        "Compilation creates a fresh family and generated wire models using existing application classes;",
        "imports and application model-class construction are excluded. Runtime cases reuse compiled families.",
        "The JSON artifact contains revisions, versions, raw samples, iteration counts, and observations.",
    ]
    if report["quick"]:
        lines += [
            "",
            "**Quick correctness smoke only: these timings are not performance evidence.**",
        ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path)
    parser.add_argument("--head", type=Path)
    parser.add_argument("--output", type=Path, default=Path("runtime-benchmark.json"))
    parser.add_argument("--summary", type=Path, default=Path("runtime-benchmark.md"))
    parser.add_argument("--pairs", type=int, default=3)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument(
        "--quick", action="store_true", help="Correctness smoke only; not useful timings"
    )
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--seed", type=int, default=2718, help=argparse.SUPPRESS)
    options = parser.parse_args()
    if not 1 <= options.pairs <= 10 or not 1 <= options.rounds <= 10:
        parser.error("pairs and rounds must be between 1 and 10")
    if options.worker:
        print(
            json.dumps(
                worker(
                    options.worker, rounds=options.rounds, seed=options.seed, quick=options.quick
                )
            )
        )
        return 0
    if options.base is None or options.head is None:
        parser.error("--base and --head src directories are required")
    report: dict[str, Any] = {
        "benchmark_schema": REPORT_SCHEMA,
        "status": "failed",
        "pairs": options.pairs,
        "rounds": options.rounds,
        "quick": options.quick,
        "runs": [],
        "harness_sha256": {
            name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in ("runtime_comparison.py", "runtime_workloads.py")
        },
    }
    try:
        for pair in range(options.pairs):
            order = ("base", "head") if pair % 2 == 0 else ("head", "base")
            for side in order:
                command = [
                    sys.executable,
                    "-B",
                    str(Path(__file__).resolve()),
                    "--worker",
                    str(getattr(options, side).resolve()),
                    "--rounds",
                    str(options.rounds),
                    "--seed",
                    str(2718 + pair),
                ]
                if options.quick:
                    command.append("--quick")
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=120,
                    env={
                        **os.environ,
                        "PYTHONPATH": "",
                        "PYTHONHASHSEED": "2718",
                        "PYTHONDONTWRITEBYTECODE": "1",
                    },
                )
                if completed.returncode:
                    raise RuntimeError(f"{side} worker failed: {completed.stderr[-4000:]}")
                run = json.loads(completed.stdout)
                run.update(pair=pair, side=side)
                report["runs"].append(run)
        report["comparison"] = summarize(report["runs"])
        report["status"] = "passed"
    except (AssertionError, OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        report["error"] = str(error)
        print(str(error), file=sys.stderr)
    finally:
        options.output.parent.mkdir(parents=True, exist_ok=True)
        options.summary.parent.mkdir(parents=True, exist_ok=True)
        options.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        options.summary.write_text(markdown(report), encoding="utf-8")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
