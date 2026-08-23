# Decorator runtime benchmarks

`decorator_runtime.py` measures the two scaling shapes tracked by issue #100:

- reconciliation after a callback reorders 100, 500, and 1,000 decorator
  occurrences;
- authoritative route selection for a 16-arm decorator-child union.

The harness uses fixed inputs, verifies every selected location and family, and
emits raw nanosecond samples plus their median as JSON. It does not enforce a
speed threshold, because timings vary by machine and ordinary CI remains a
correctness gate.

Run the candidate from the repository root:

```console
uv run python benchmarks/decorator_runtime.py --label candidate
```

To compare another checkout without installing it, point at that checkout's
`src` directory. Use the same Python environment, repeat count, and command for
both runs:

```console
uv run python benchmarks/decorator_runtime.py \
  --source-root ../pydantic-versions-baseline/src \
  --label origin-main
```

The report records the imported source root, Git revision and dirty state,
Python, Pydantic, platform, warm-up count, repeat count, and every raw sample.
Compare the three `reconcile_reordered_occurrences` medians as both absolute
times and ratios, then compare `select_multi_route_union` on the same machine.
