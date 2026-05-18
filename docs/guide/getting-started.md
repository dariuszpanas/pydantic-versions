# Getting Started

This guide walks through the smallest useful setup: install the package, declare
a current model, describe one historical schema version, validate old input into
the current model, and render an old config shape.

## Install

```bash
pip install pydantic-versions
```

With `uv`:

```bash
uv add pydantic-versions
```

`pydantic-versions` depends on Pydantic v2 and supports Python 3.12+.

## Define the current schema

Start with the model your current application wants to use:

```python
from pydantic import BaseModel


class AppConfig(BaseModel):
    timeout: float = 10.0
    retries: int = 3
    new_feature: bool = False
```

In current application code, this is still the shape you want to work with.

## Register schema versions

Decorate the current model with the schema versions you support:

```python
from pydantic_versions import field_default, field_removed, schema_version, versioned_schema


@versioned_schema(
    name="app_config",
    versions=["1", "2"],
    current="2",
)
@schema_version(
    "1",
    patches=[
        field_default("timeout", 5.0),
        field_removed("new_feature"),
    ],
)
class AppConfig(BaseModel):
    timeout: float = 10.0
    retries: int = 3
    new_feature: bool = False
```

This says:

- version `2` is the current schema;
- version `1` had `timeout=5.0`;
- version `1` did not have `new_feature`.

## Validate historical input

Users can keep older config files:

```python
from pydantic_versions import validate_versioned


old_config = {
    "schema_version": "1",
    "retries": 2,
}

result = validate_versioned(AppConfig, old_config)

assert result.source_version == "1"
assert result.current_version == "2"
assert result.current_model == AppConfig(timeout=5.0, retries=2, new_feature=False)
```

Application code can use `result.current_model` and ignore the historical shape
after validation.

## Render a historical config

Use `dump_versioned()` when you need to generate examples or output for a
specific schema version:

```python
from pydantic_versions import dump_versioned


v1_config = dump_versioned(AppConfig, version="1")

assert v1_config == {
    "timeout": 5.0,
    "retries": 3,
    "schema_version": "1",
}
```

## Add a migration when patches are not enough

Patches describe field-level schema differences. Use a migration for custom data
changes:

```python
from pydantic_versions import migration


@migration(AppConfig, "1", "2")
def migrate_v1_to_v2(data: dict) -> dict:
    data.setdefault("new_feature", False)
    return data
```

Migrations run after historical validation and before final current-model
validation.

## Next steps

- Read [Version Discovery](version-discovery.md) before using `missing_version`.
- Read [Schema Patches](schema-patches.md) for defaults, removed fields, renamed fields, and grouped patches.
- Read [Migrations](migrations.md) for upgrade behavior.
- Read the [Complex Config Example](complex-config-example.md) for nested models and metadata-style version fields.
