# Concepts

`pydantic-versions` separates schema versions from software versions.

A project can release software version `4.2.0` while still accepting config schema
version `1`, `2`, or `2024-10`. The schema version describes the shape and
defaults of a config file. The software version describes the application that is
loading it.

## Current model

The model you decorate is the current schema. Application code should usually
use this model after validation:

```python
from pydantic import BaseModel
from pydantic_versions import validate_versioned, versioned_schema


@versioned_schema(name="app_config", versions=["1", "2"], current="2")
class AppConfig(BaseModel):
    timeout: float = 10.0


result = validate_versioned(AppConfig, {"schema_version": "2"})
config = result.current_model
```

## Historical schemas

Historical schemas are derived from the current model with patches. This keeps
simple default-only versions compact while still allowing validation against the
older shape:

```python
from pydantic_versions import field_default, schema_version


@versioned_schema(name="app_config", versions=["1", "2"], current="2")
@schema_version("1", patches=[field_default("timeout", 5.0)])
class AppConfig(BaseModel):
    timeout: float = 10.0
```

For a larger nested example that shows why this matters in practice, see the
[complex config example](complex-config-example.md).

## Validation flow

`validate_versioned()`:

1. discovers the source schema version;
2. validates input against that source version;
3. maps historical field names back to current names;
4. applies registered upgrade migrations;
5. validates the final payload against the current model.

The result preserves both sides:

```python
result = validate_versioned(AppConfig, {"schema_version": "1"})

assert result.source_version == "1"
assert result.current_version == "2"
assert result.source_model.timeout == 5.0
assert result.current_model.timeout == 5.0
```

## Rendering flow

`dump_versioned()` renders a payload in a requested schema version. This is for
writing example configs, preserving legacy output formats, or showing users what
a specific schema version looks like.

```python
from pydantic_versions import dump_versioned


dumped = dump_versioned(AppConfig, version="1")
assert dumped == {"timeout": 5.0, "schema_version": "1"}
```
