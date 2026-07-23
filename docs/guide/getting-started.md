# Getting Started

This guide walks through the smallest useful setup: install the package, keep a
current application model ordinary, declare its history in another module,
validate old input into the current model, and render an old config shape.

## Install

```bash
pip install pydantic-versions
```

With `uv`:

```bash
uv add pydantic-versions
```

`pydantic-versions` supports Python 3.12 through 3.14 and requires Pydantic
2.12.3 or newer in the Pydantic v2 release line. Pydantic v1 models, including
models imported from the `pydantic.v1` compatibility namespace, are not
supported.

## Define the current schema

Start with the model your current application wants to use. It does not need a
version decorator or a `pydantic_versions` import:

```python
# models.py
from pydantic import BaseModel


class AppConfig(BaseModel):
    timeout: float = 10.0
    retries: int = 3
    new_feature: bool = False
```

## Declare schema history

Put the growing history in a dedicated module:

```python
# schema_history.py
from models import AppConfig
from pydantic_versions import (
    SchemaFamily,
    SchemaVersion,
    field_default,
    field_removed,
)


APP_CONFIG_SCHEMA = SchemaFamily(
    model=AppConfig,
    name="app_config",
    versions=(
        SchemaVersion(
            "1",
            patches=(
                field_default("timeout", 5.0),
                field_removed("new_feature"),
            ),
        ),
        SchemaVersion("2"),
    ),
)
```

This says:

- version `2`, the final declared label, is current;
- version `1` had `timeout=5.0`;
- version `1` did not have `new_feature`.

Adding another historical version changes `schema_history.py`, not the
authoritative application model.

## Validate historical input

Users can keep older config files:

```python
old_config = {
    "schema_version": "1",
    "retries": 2,
}

result = APP_CONFIG_SCHEMA.validate(old_config)

assert result.source_version == "1"
assert result.current_version == "2"
assert result.current_model == AppConfig(
    timeout=5.0,
    retries=2,
    new_feature=False,
)
```

Application code can use `result.current_model` and ignore the historical shape
after validation.

## Render a historical config

Use `dump()` when you need defaults or output for a specific schema version:

```python
v1_config = APP_CONFIG_SCHEMA.dump(version="1")

assert v1_config == {
    "timeout": 5.0,
    "retries": 3,
    "schema_version": "1",
}
```

## Add an upgrade when patches are not enough

Patches describe field-level schema differences. Declare adjacent forward
transitions for custom value changes:

```python
from pydantic_versions import VersionTransition


def upgrade_v1(data: dict) -> dict:
    data.setdefault("new_feature", False)
    return data


APP_CONFIG_SCHEMA = SchemaFamily(
    model=AppConfig,
    name="app_config",
    versions=(
        SchemaVersion(
            "1",
            patches=(
                field_default("timeout", 5.0),
                field_removed("new_feature"),
            ),
        ),
        SchemaVersion("2"),
    ),
    transitions=(
        VersionTransition("1", "2", upgrade=upgrade_v1),
    ),
)
```

This replaces the earlier family declaration with the same v1 projection plus
an upgrade. Transitions run after historical validation and before final
current-model validation. Every declared transition must connect adjacent
labels, so accepted upgrade code cannot become an unreachable registration.

## Keep the decorator style when it stays small

The 0.1 decorators remain compatibility and convenience adapters. They build a
default family and delegate to the same compiler:

```python
from pydantic_versions import schema_version, versioned_schema


@versioned_schema(name="small_config", versions=("1", "2"), current="2")
@schema_version("1", patches=(field_default("timeout", 5.0),))
class SmallConfig(BaseModel):
    timeout: float = 10.0
```

Use an external family when the history would obscure the current model, when
two histories share one model, or when configuration must be explicit rather
than import-order dependent.

## Next steps

- Read [External Schema Families](external-families.md) for defaults, isolation, and compilation rules.
- Read [Version Discovery](version-discovery.md) before using `missing_version`.
- Read [Schema Patches](schema-patches.md) for defaults, removed fields, renamed fields, and grouped patches.
- Read [Migrations](migrations.md) for upgrade behavior.
- Read the [Complex Config Example](complex-config-example.md) for nested models and metadata-style version fields.
