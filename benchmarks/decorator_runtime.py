"""Reproduce the decorator-runtime scaling cases tracked by issue #100.

The script emits only JSON on stdout so results can be retained or compared by
another tool.  It intentionally records timings without enforcing thresholds;
ordinary CI should prove correctness, not make machine-speed claims.

Run against the current checkout:

    uv run python benchmarks/decorator_runtime.py --label candidate

Run the same harness against an isolated checkout:

    uv run python benchmarks/decorator_runtime.py \
        --source-root ../pydantic-versions-baseline/src \
        --label origin-main
"""

from __future__ import annotations

import argparse
import gc
import json
import platform
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import reduce
from operator import or_
from pathlib import Path
from statistics import median
from time import perf_counter_ns
from types import GenericAlias
from typing import Any

DEFAULT_COUNTS = (100, 500, 1_000)
DEFAULT_UNION_ROUTES = 16
DEFAULT_UNION_VALUES_PER_ROUTE = 8
REPORT_SCHEMA = "pydantic-versions.decorator-runtime.v1"


@dataclass(frozen=True)
class _BenchmarkCase:
    name: str
    parameters: dict[str, int]
    operation: Callable[[], Any]
    verify: Callable[[Any], dict[str, Any]]


def _activate_source_root(source_root: Path | None) -> Path:
    selected = (
        Path(__file__).resolve().parents[1] / "src"
        if source_root is None
        else source_root.resolve()
    )
    if not (selected / "pydantic_versions").is_dir():
        msg = f"Source root does not contain pydantic_versions: {selected}"
        raise ValueError(msg)
    if any(
        name == "pydantic_versions" or name.startswith("pydantic_versions.") for name in sys.modules
    ):
        msg = "Select --source-root before importing pydantic_versions"
        raise RuntimeError(msg)
    sys.path.insert(0, str(selected))
    return selected


def _repository_state(source_root: Path) -> dict[str, Any]:
    repository = source_root.parent if source_root.name == "src" else source_root

    def git(*arguments: str) -> str | None:
        completed = subprocess.run(
            ("git", "-C", str(repository), *arguments),
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            return None
        return completed.stdout.strip()

    revision = git("rev-parse", "HEAD")
    status = git("status", "--porcelain", "--untracked-files=no")
    return {
        "revision": revision,
        "tracked_files_dirty": None if status is None else bool(status),
    }


def _build_occurrence_case(count: int) -> _BenchmarkCase:
    from pydantic import create_model

    from pydantic_versions import VersionTransition, versioned_schema
    from pydantic_versions._runtime import _validated_current_render_payload
    from pydantic_versions._runtime_decorators import _reconcile_decorator_selections
    from pydantic_versions.family import _family_for

    child = create_model("BenchmarkOccurrenceChild", value=(int, ...))
    child = versioned_schema(
        name="benchmark_occurrence_child",
        versions=("1", "2"),
        current="2",
    )(child)

    def reverse_items(data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "items": list(reversed(data["items"]))}

    parent = create_model(
        "BenchmarkOccurrenceParent",
        items=(GenericAlias(list, child), ...),
    )
    parent = versioned_schema(
        name="benchmark_occurrence_parent",
        versions=("1", "2"),
        current="2",
        transitions=(
            VersionTransition(
                "1",
                "2",
                downgrade=reverse_items,
                downgrade_semantics="exact",
            ),
        ),
    )(parent)
    family = _family_for(parent)
    compiled = family._compiled_family()
    current_model = parent.model_validate(
        {
            "items": [child.model_validate({"value": index}) for index in range(count)],
        }
    )
    payload, selections = _validated_current_render_payload(
        family=family,
        compiled=compiled,
        data=current_model,
    )
    payload = reverse_items(payload)

    def operation() -> Any:
        nonlocal selections
        selections = _reconcile_decorator_selections(
            payload=payload,
            selections=selections,
            compiled=compiled,
            discover_new=True,
        )
        return selections

    def verify(result: Any) -> dict[str, Any]:
        if len(result) != count:
            msg = f"Expected {count} selections, received {len(result)}"
            raise AssertionError(msg)
        items = payload["items"]
        expected_locations = {("items", index) for index in range(count)}
        locations = {selection.location for selection in result}
        if locations != expected_locations:
            raise AssertionError("Reconciliation did not preserve every occurrence location")
        for selection in result:
            _, index = selection.location
            if selection.value_identity != id(items[index]):
                raise AssertionError("Reconciliation attached an occurrence to the wrong value")
        values = [item["value"] for item in items]
        expected_values = list(reversed(range(count)))
        if values != expected_values:
            raise AssertionError("The benchmark callback did not preserve reversed values")
        return {
            "locations": len(locations),
            "payload_first": values[0],
            "payload_last": values[-1],
            "unique_value_identities": len({selection.value_identity for selection in result}),
        }

    return _BenchmarkCase(
        name="reconcile_reordered_occurrences",
        parameters={"occurrences": count},
        operation=operation,
        verify=verify,
    )


def _build_union_case(route_count: int, values_per_route: int) -> _BenchmarkCase:
    from pydantic import create_model

    from pydantic_versions import versioned_schema
    from pydantic_versions._runtime_decorators import _select_decorator_routes
    from pydantic_versions.family import _family_for

    children: list[type[Any]] = []
    for route_index in range(route_count):
        child = create_model(
            f"BenchmarkUnionChild{route_index}",
            value=(int, ...),
        )
        child = versioned_schema(
            name=f"benchmark_union_child_{route_index}",
            versions=("1",),
            current="1",
        )(child)
        children.append(child)

    union_annotation = reduce(or_, children)
    parent = create_model(
        "BenchmarkUnionParent",
        items=(GenericAlias(list, union_annotation), ...),
    )
    parent = versioned_schema(
        name="benchmark_union_parent",
        versions=("1",),
        current="1",
    )(parent)
    family = _family_for(parent)
    compiled = family._compiled_family()
    values = [
        child.model_validate({"value": route_index * values_per_route + ordinal})
        for route_index, child in enumerate(children)
        for ordinal in range(values_per_route)
    ]
    current_model = parent.model_validate({"items": values})

    def operation() -> Any:
        return _select_decorator_routes(
            current_model,
            compiled=compiled,
            parent_label=compiled.current_version,
            source_version=None,
        )

    def verify(result: Any) -> dict[str, Any]:
        occurrence_count = route_count * values_per_route
        if len(result) != occurrence_count:
            msg = f"Expected {occurrence_count} selections, received {len(result)}"
            raise AssertionError(msg)
        locations = {selection.location for selection in result}
        expected_locations = {("items", index) for index in range(occurrence_count)}
        if locations != expected_locations:
            raise AssertionError("Union selection did not visit every occurrence exactly once")
        selected_families: set[type[Any]] = set()
        for selection in result:
            _, index = selection.location
            value = values[index]
            if selection.route.family.model is not type(value):
                raise AssertionError("Union selection resolved an occurrence to the wrong route")
            if len(selection.site_routes) != route_count:
                raise AssertionError("Union dispatch site does not retain all route choices")
            selected_families.add(selection.route.family.model)
        if selected_families != set(children):
            raise AssertionError("Union selection omitted one or more child families")
        return {
            "locations": len(locations),
            "selected_families": len(selected_families),
            "site_routes": route_count,
        }

    return _BenchmarkCase(
        name="select_multi_route_union",
        parameters={
            "occurrences": route_count * values_per_route,
            "routes": route_count,
            "values_per_route": values_per_route,
        },
        operation=operation,
        verify=verify,
    )


def _measure(case: _BenchmarkCase, *, repeats: int, warmups: int) -> dict[str, Any]:
    first = case.operation()
    correctness = case.verify(first)
    for _ in range(warmups):
        case.operation()

    samples: list[int] = []
    final: Any = first
    for _ in range(repeats):
        gc.collect()
        gc_was_enabled = gc.isenabled()
        gc.disable()
        try:
            started = perf_counter_ns()
            final = case.operation()
            samples.append(perf_counter_ns() - started)
        finally:
            if gc_was_enabled:
                gc.enable()

    if case.verify(final) != correctness:
        raise AssertionError("Benchmark correctness observations changed between runs")
    middle = int(median(samples))
    return {
        "case": case.name,
        "correctness": correctness,
        "max_ns": max(samples),
        "median_ns": middle,
        "min_ns": min(samples),
        "parameters": case.parameters,
        "samples_ns": samples,
    }


def run_benchmarks(
    *,
    counts: Sequence[int] = DEFAULT_COUNTS,
    union_routes: int = DEFAULT_UNION_ROUTES,
    union_values_per_route: int = DEFAULT_UNION_VALUES_PER_ROUTE,
    repeats: int = 7,
    warmups: int = 1,
    label: str = "working-tree",
    source_root: Path,
) -> dict[str, Any]:
    if not counts or any(count < 1 for count in counts):
        raise ValueError("counts must contain positive integers")
    if union_routes < 2:
        raise ValueError("union_routes must be at least 2")
    if union_values_per_route < 1:
        raise ValueError("union_values_per_route must be positive")
    if repeats < 1:
        raise ValueError("repeats must be positive")
    if warmups < 0:
        raise ValueError("warmups cannot be negative")

    cases = [*(_build_occurrence_case(count) for count in counts)]
    cases.append(_build_union_case(union_routes, union_values_per_route))
    results = [_measure(case, repeats=repeats, warmups=warmups) for case in cases]

    from pydantic import __version__ as pydantic_version

    import pydantic_versions

    package_file = pydantic_versions.__file__
    if package_file is None:
        raise RuntimeError("Imported pydantic_versions package has no source file")
    package_path = Path(package_file).resolve()
    if not package_path.is_relative_to(source_root.resolve()):
        msg = (
            f"Imported pydantic_versions from {package_path}, outside requested "
            f"source root {source_root.resolve()}"
        )
        raise RuntimeError(msg)

    return {
        "benchmark_schema": REPORT_SCHEMA,
        "configuration": {
            "counts": list(counts),
            "repeats": repeats,
            "union_routes": union_routes,
            "union_values_per_route": union_values_per_route,
            "warmups": warmups,
        },
        "environment": {
            "executable": sys.executable,
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "pydantic": pydantic_version,
            "pydantic_versions": pydantic_versions.__version__,
            "pydantic_versions_module": str(package_path),
            "python": platform.python_version(),
            "source_root": str(source_root),
            "timer": "perf_counter_ns",
        },
        "label": label,
        "repository": _repository_state(source_root),
        "results": results,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--counts",
        nargs="+",
        type=int,
        default=list(DEFAULT_COUNTS),
        help="Decorator occurrence counts (default: 100 500 1000)",
    )
    parser.add_argument("--label", default="working-tree")
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument(
        "--source-root",
        type=Path,
        help="Checkout src directory to import instead of this checkout",
    )
    parser.add_argument("--union-routes", type=int, default=DEFAULT_UNION_ROUTES)
    parser.add_argument(
        "--union-values-per-route",
        type=int,
        default=DEFAULT_UNION_VALUES_PER_ROUTE,
    )
    parser.add_argument("--warmups", type=int, default=1)
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    options = _parser().parse_args(arguments)
    source_root = _activate_source_root(options.source_root)
    report = run_benchmarks(
        counts=options.counts,
        union_routes=options.union_routes,
        union_values_per_route=options.union_values_per_route,
        repeats=options.repeats,
        warmups=options.warmups,
        label=options.label,
        source_root=source_root,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
