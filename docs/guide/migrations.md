# Migrations

Migrations upgrade already-validated historical data toward the current model.
They are optional: if adjacent versions are compatible, the compiler records an
identity edge. The compiled inventory and operation-specific plans make both
custom and identity edges visible without running a payload.

## Declare transitions with the family

External families keep transition topology beside their version declarations:

```python
from models import AppConfig
from pydantic_versions import SchemaFamily, SchemaVersion, VersionTransition


def upgrade_v1(data: dict) -> dict:
    data.setdefault("new_feature", False)
    return data


APP_CONFIG_SCHEMA = SchemaFamily(
    model=AppConfig,
    name="app_config",
    versions=(SchemaVersion("1"), SchemaVersion("2")),
    transitions=(VersionTransition("1", "2", upgrade=upgrade_v1),),
)
```

Constructor declarations are deterministic and complete before first use. The
legacy `@migration` builder remains available for decorator-based families:

```python
from pydantic import BaseModel
from pydantic_versions import migration, versioned_schema


@versioned_schema(name="legacy_app_config", versions=("1", "2"), current="2")
class LegacyAppConfig(BaseModel):
    new_feature: bool = False


@migration(LegacyAppConfig, "1", "2")
def migrate_v1_to_v2(data: dict) -> dict:
    data.setdefault("new_feature", False)
    return data
```

Legacy registrations must finish before the family's first compilation.
Registering another migration after `compile()`, `model_for_version()`,
`describe()`, `plan_validation()`, `plan_render()`, `validate_versioned()`, or
`dump_versioned()` has compiled that family raises `InvalidMigrationError`
instead of mutating live plans. Put legacy decorators before any inspection or
runtime call; new code should prefer constructor transitions.

## Direction and topology

Forward upgrades connect adjacent declared versions only:

```python
from pydantic import BaseModel
from pydantic_versions import versioned_schema


@versioned_schema(name="app_config", versions=("1", "2", "3"), current="3")
class AppConfig(BaseModel): ...
```

Valid edges are `1 -> 2` and `2 -> 3`. A direct `1 -> 3` registration, a reverse
edge, an unknown endpoint, or a duplicate edge fails during declaration. This
guarantees that every accepted custom upgrade has an execution path.

## Chained upgrades

When validating version `1` against current version `3`, the immutable compiled
topology is:

```text
1 -> 2 -> 3
```

Custom upgrades run in that order. An edge with no upgrade callable remains an
explicit identity step, which is useful when a version changed only field
defaults or when its historical projection is already current-compatible.

`SchemaFamily.transitions` exposes the frozen custom declarations for tooling
and review. After validation, `VersionedValidation.migrations_applied` reports
the custom upgrade edges that actually ran. Use the inventory or a validation
plan when tooling needs the complete topology, including compiler-added
identity steps.

## Inspect the migration inventory

`describe()` returns the family-owned compiled inventory:

```python
inventory = APP_CONFIG_SCHEMA.describe()

transition = inventory.transitions[0]
assert transition.source == "1"
assert transition.target == "2"
assert transition.upgrade == "custom"
assert transition.downgrade == "unavailable"
assert transition.downgrade_semantics == "unavailable"
```

That record can drive a migration table without inspecting decorator state or
Python callables:

| Edge | Upgrade | Downgrade | Downgrade semantics |
| --- | --- | --- | --- |
| `1 -> 2` | `custom` | `unavailable` | `unavailable` |

Every adjacent pair appears in declared version order. An omitted transition is
reported as `implicit_identity` in both directions with exact downgrade
semantics. Version descriptions also list generated/current wire-model kinds
and version-specific default, removed, and renamed field projections.

The inventory and its nested records are frozen values. `to_dict()` returns a
fresh deterministic JSON-safe representation and deliberately omits model
classes, callable objects, default values and factories, and payload data.

## Plan validation

`plan_validation()` describes the selected source-to-current route without
executing a migration:

```python
plan = APP_CONFIG_SCHEMA.plan_validation("1")

assert plan.operation == "validate"
assert plan.source_version == "1"
assert plan.target_version == "2"
assert [step.kind for step in plan.steps] == [
    "metadata",
    "wire_validation",
    "custom_transition",
    "current_validation",
]
```

Validation plans contain source metadata and wire-validation boundaries,
version-specific projections, every adjacent upgrade or identity edge, and the
final current-model validation boundary. Their overall semantics are
`not_applicable`; individual identity steps are marked `exact`.

When a parent edge changes the version selected for a declared nested family,
the plan places a `nested` step immediately before that parent edge. Its schema
path and child source/target labels describe the complete child conversion
without inspecting a payload. The step is conditional because an optional or
collection-valued path may have no value to convert for one document.
Its direction follows the child labels rather than the owning parent edge, so an
explicit historical mapping may require a child downgrade during parent
validation or a child upgrade during rendering. Validation preflights the whole
route and rejects an unavailable child downgrade before source validation or any
parent or child transition callable runs.

Runtime execution keeps nested transition dictionaries in canonical current
field names. Each child value migration runs before its owning parent
transition, so the parent callable can read and update current child keys even
when that edge selects a historical child label. Target child field names are
applied recursively only after the parent route completes, immediately before
the final wire-validation boundary. Model-owned child metadata is rebased to
the mapped child label at each edge. If raw nested metadata declares a different
label than the parent mapping, validation and rendering reject the complete
document before source/current validation or any transition callable runs.

For nested `set` and `frozenset` fields, cardinality is checked both after value
migrations and after target wire coercion. Distinct current values therefore
cannot silently collapse because an explicit historical wire model normalizes
them to the same set element.

Each step has a deterministic `pv1-` ID followed by the full 64-character
SHA-256 digest of safe schema identity. IDs never depend on Python's
process-randomized hash, callable `repr()`, object identity, or payload values.

## Preflight rendering

`plan_render()` describes a current-to-target route. It is conservative: a
custom upgrade with no declared downgrade makes the complete reverse route
unavailable.

```python
import pytest

from pydantic_versions import IrreversibleTransitionError


with pytest.raises(IrreversibleTransitionError):
    APP_CONFIG_SCHEMA.plan_render("1")
```

The method takes no payload and raises before any user transition can run.
Structural rename and default projections are exact. A target projection that
removes a current field is available but marks its step and the complete render
plan as `lossy`.

Nested render semantics roll up into the parent plan. A lossy child route makes
the parent route lossy. An unavailable child route rejects the complete parent
plan before any parent or child downgrade callable runs, even if the particular
payload would omit that optional path or use an empty collection. `conditional`
describes execution, not a way to weaken compatibility preflight.

Custom downgrade declarations are fully supported. When an explicit downgrade is
declared on a `VersionTransition`, rendering a historical schema will execute the
downgrade logic in reverse edge order.

Planning ensures that missing downgrades appropriately refuse rendering for
lossy migrations. The `dump_versioned()` function executes these public plans
to safely downgrade and project historical dictionaries.

## Plans are not traces

A plan is a static, payload-free description of what an operation would need to
do. It contains safe schema paths and conditional templates, never actual list
indices, mapping keys, values, exception messages, or timing data. Creating or
serializing a plan does not emit logs.

An execution trace records what actually happened for one payload. Structured
per-step traces are separate later work. Until then,
`VersionedValidation.migrations_applied` remains the compatibility view of
top-level custom upgrades that completed; it intentionally excludes implicit
identity steps.

## Return values

Migration functions receive a fresh dictionary using current field names and
must return a dictionary. Returning any other type raises
`InvalidMigrationError`.

Downgrade declarations and historical rendering across value-changing upgrades
are executed natively by the active conversion contract. A missing downgrade
across a value-changing upgrade makes historical rendering irreversible and
raises an error.
