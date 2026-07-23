# Migrations

Migrations upgrade already-validated historical data toward the current model.
They are optional: if adjacent versions are compatible, the compiler records an
identity edge.

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
    transitions=(
        VersionTransition("1", "2", upgrade=upgrade_v1),
    ),
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
`validate_versioned()`, or `dump_versioned()` has compiled that family raises
`InvalidMigrationError` instead of mutating live plans.

## Direction and topology

Forward upgrades connect adjacent declared versions only:

```python
from pydantic import BaseModel
from pydantic_versions import versioned_schema


@versioned_schema(name="app_config", versions=("1", "2", "3"), current="3")
class AppConfig(BaseModel):
    ...
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
explicit internal identity step, which is useful when a version changed only
field defaults or when its historical projection is already current-compatible.

`SchemaFamily.transitions` exposes the frozen custom declarations for tooling
and review. After validation, `VersionedValidation.migrations_applied` reports
the custom upgrade edges that actually ran. Compiler-added identity steps are
not presented as user migrations; the public compiled-plan API will provide the
complete topology.

## Return values

Migration functions receive a fresh dictionary using current field names and
must return a dictionary. Returning any other type raises
`InvalidMigrationError`.

Downgrade declarations and historical rendering across value-changing upgrades
are part of the broader 0.2 conversion contract, but this foundation does not
execute them yet. A downgrade declaration is rejected rather than accepted and
silently ignored.
