# Performance

Versioned schemas add compatibility work around Pydantic validation and
serialization. Expect additional CPU work and temporary allocations compared
with validating or dumping only the current, unversioned model. The cost
depends on the model, payload, and conversion route; there is no universal
overhead percentage.

The runtime improvements in package releases 1.0.3 and 1.0.4 are comparisons
with earlier **pydantic-versions** releases, including 1.0.2. They do not show
that versioning is faster than plain Pydantic. Historical migration also has
no equivalent in a plain current-model validation call.

## Compile once, reuse the family

The first family operation compiles its declarations and generated wire
models. Calling `compile()` at startup moves that work out of the first
request. It is idempotent and thread-safe, and later calls reuse the compiled
family. Some runtime adapters and field plans are initialized on first use,
so eager compilation alone is not a fully warmed validation or dump benchmark.

Keep the model classes and family in a module or another application-lifetime
owner. Recreating them for each request repeats setup and loses reuse:

<!-- pv-doc-test: performance-reuse -->
```python
from pydantic import BaseModel
from pydantic_versions import SchemaFamily, SchemaVersion, field_renamed


class Config(BaseModel):
    retries: int = 3


CONFIG_SCHEMA = SchemaFamily(
    model=Config,
    name="config",
    versions=(
        SchemaVersion("1", patches=(field_renamed("retries", "attempts"),)),
        SchemaVersion("2"),
    ),
).compile()


def load_config(payload: dict) -> Config:
    return CONFIG_SCHEMA.validate(payload).current_model


current = load_config({"schema_version": "1", "attempts": 5})
assert current == Config(retries=5)
assert CONFIG_SCHEMA.dump(version="2", data=current) == {
    "schema_version": "2",
    "retries": 5,
}
```

Caches store schema plans and adapters, not validated results for repeated
payloads. Each call still enforces its validation and conversion contracts.
Keeping many distinct model classes or families alive also keeps their owned
plans and generated models alive. Discarding temporary families permits
collection, subject to Python and Pydantic's own references and caches.

Finish model definitions and forward-reference rebuilding before compilation.
A forced `model_rebuild()` invalidates an already compiled family and its
dependent parent families: recreate the affected family graph before reuse.
Do not race rebuilds with runtime calls. See
[family lifecycle rules](external-families.md#select-one-compatibility-default-deliberately).

## What changes the runtime cost

| Workload | Work performed and practical implication |
| --- | --- |
| Current-version validation | Selects and validates the wire version, extracts declared canonical values, and validates the authoritative current model. Current-version shortcuts avoid migration traversal, but retain these boundaries. |
| Historical validation | Also follows the required transitions toward current, including nested child conversions and application callbacks. The number and kind of transitions matter, not just the number of declared labels. |
| Current or historical dump | Establishes authoritative current input, extracts canonical values, projects to the requested wire version, validates it, and serializes it. Historical output can require downgrade callbacks and nested conversion. |
| Nested or large payloads | More fields, collection elements, nesting depth, and managed child occurrences require more traversal and allocation. Nested decorator unions can add route selection and reconciliation. A small flat example does not predict a large configuration's cost. |
| Aliases and metadata | Alias precedence, nested alias paths, version discovery, and metadata normalization add work. Cached field plans reduce repeated schema inspection; they do not remove payload traversal. |
| Application code | Migration callbacks and validators contribute their own cost. Generated wire models omit current-model validators; authoritative validation retains them. Explicit wire models may add their own validators. Measure the configuration you actually use. |

`dump()` on a current model instance follows its `revalidate_instances` policy;
dumping a mapping must first validate current input. Do not compare those as
if they were the same operation, or change validation policies merely to
improve a benchmark. See [rendering boundaries](rendering.md).

Canonical extraction detaches caller-owned built-in mappings and containers
and preserves declared Python values for callbacks. It avoids invoking source
serializers at this intermediate step. Copies and temporary structures cost
time and memory, but protect caller data and conversion semantics. This is
not a zero-copy or general-purpose deep-copy API; opaque values retain the
[documented transition contract](migrations.md#transition-payload-contract).

## Measure the boundary your application uses

The repository's [runtime comparison harness](https://github.com/dariuszpanas/pydantic-versions/blob/main/benchmarks/README.md)
compares base and head sources in one dependency environment. PR checks save
raw samples and a summary for flat, rich/aliased, and nested current/historical
validation and dumping. Fresh-family compilation is measured separately using
already defined application model classes; import time and application model
construction are excluded. The report checks outputs, schemas, inventories,
and operation plans before accepting a comparison.

For application decisions:

1. Separate process startup, model/family construction and compilation, first
   use, and warmed repeated operations. Include cold work if the application
   is short-lived; do not put it inside a claimed warm benchmark.
2. Use representative payload sizes, version distributions, aliases, nested
   routes, callbacks and validators. Keep input type and dump options identical.
   Check results before timing, including migration effects and metadata.
3. For a plain-Pydantic baseline, compare current-model operations with the
   same validation settings and output mode. Account explicitly for version
   metadata and the additional versioned boundary. Measure historical
   conversion separately; directly validating already upgraded data omits its
   work. Do not substitute `model_construct()` for validation.
4. Pin interpreter and dependencies, warm both sides, alternate repeated runs
   on the same idle host, and retain raw samples, revisions and environment
   details. Investigate repeatable differences across realistic workloads;
   shared CI runner timings are signals, not a fixed performance gate.

The CI report's microseconds per operation are batch-average timings. Their
reciprocal describes sequential benchmark throughput, not concurrent server
capacity. They are not request p95/p99 latency measurements and exclude I/O,
queueing and application scheduling. Single-host results do not establish a
cross-platform guarantee or replace end-to-end latency and memory profiling.
