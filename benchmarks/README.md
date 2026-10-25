# Runtime benchmarks

## PR base/head comparison

`runtime_comparison.py` runs the same public-API workloads against two source
checkouts. It checks expected results and compares outputs, JSON schemas,
inventories, and operation plans. `runtime_workloads.py` defines flat models,
rich UUID/set/mapping values with aliases, and ten nested workers across three
schema versions. Both current and historical validation/dump paths run; flat
and nested historical routes include callbacks. These fixed examples are not
a complete model or validator benchmark suite.

From the candidate checkout, with a separate base checkout beside it:

```console
uv sync --frozen --python 3.12
uv run --no-sync python benchmarks/runtime_comparison.py \
  --base ../pydantic-versions-base/src --head src \
  --output ../runtime-benchmark.json --summary ../runtime-benchmark.md
```

Use the **same candidate harness and environment** for both sides. No base
dependency resolution occurs: both import their own source under the head's
locked dependencies. The JSON records source paths, Git revisions and tracked
dirty state, project versions from each checkout, actual dependency versions,
harness hashes, interpreter/platform, GC/hash-seed settings, loop counts, and
every sample. This measures a source change under common dependencies, not the
combined effect of changing dependency locks. Incompatible source/dependency
combinations fail explicitly and require a deliberate compatible environment.

Defaults are three alternating base/head process pairs, three randomized case
rounds per process, and five warm-up operations per case. Iterations per sample
are 500 for flat, 250 for rich, 30 for nested runtime, and three for compilation.
GC remains enabled; collection runs outside each timed batch. The report shows
median per-process timings and paired head/base ratios with their range.
`--quick --pairs 1 --rounds 1` is a correctness smoke test, not timing evidence.

`compile.nested` creates fresh family declarations and generated wire models
using existing application model classes. It excludes interpreter startup,
imports, and construction of those application classes. Runtime cases reuse
compiled families; result checking and schema/plan inspection are outside the
timed batches. Rendering uses preconstructed current instances, not mappings.

The **Runtime Benchmark Report** job runs on ordinary `pull_request` events
with the existing read-only permissions. It checks out exact base/head SHAs
without persisting credentials, emits an Actions step summary, and uploads
`runtime-benchmark` JSON/Markdown artifacts. It does not post PR comments or
run privileged PR code. A worker timeout, incorrect result, unequal output or
environment, or harness failure fails the check. Timing changes never fail it:
shared runners are noisy, and there is no justified universal speed threshold.
Repeat meaningful signals on an idle host with representative application data.

Microseconds per operation are batch averages, not request tail latency.
Sequential operation throughput is not concurrent application capacity.
Neither this release comparison nor earlier optimization results establish an
advantage over unversioned Pydantic. See the
[performance guide](../docs/guide/performance.md) for application tradeoffs.

## Decorator scaling details

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
