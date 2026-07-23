# Concepts

`pydantic-versions` separates schema versions from software versions.

A project can release software version `4.2.0` while still accepting config schema
version `1`, `2`, or `2024-10`. The schema version describes the shape and
defaults of a config file. The software version describes the application that is
loading it.

## Current model

The current model is the authoritative application schema. It can remain an
ordinary model while an external family in another module owns its history.
Application code should usually use this model after validation:

```python
from pydantic import BaseModel
from pydantic_versions import SchemaFamily, SchemaVersion


class AppConfig(BaseModel):
    timeout: float = 10.0


APP_CONFIG_SCHEMA = SchemaFamily(
    model=AppConfig,
    name="app_config",
    versions=(SchemaVersion("1"), SchemaVersion("2")),
)

result = APP_CONFIG_SCHEMA.validate({"schema_version": "2"})
config = result.current_model
```

## Generated wire schemas

Current and historical wire schemas are object-shaped Pydantic models generated
from the current model's supported declarative field contract. They are not
behavioral subclasses of the current model. Historical patches keep
default-only versions compact while still allowing validation against an older
shape:

```python
from pydantic_versions import SchemaFamily, SchemaVersion, field_default


class AppConfig(BaseModel):
    timeout: float = 10.0


APP_CONFIG_SCHEMA = SchemaFamily(
    model=AppConfig,
    name="app_config",
    versions=(
        SchemaVersion("1", patches=(field_default("timeout", 5.0),)),
        SchemaVersion("2"),
    ),
)
```

Generated models preserve supported field constraints, defaults, factories,
aliases, and declarative model configuration. Application validators, methods,
computed fields, private attributes, and lifecycle configuration are not copied.
The authoritative current model still performs final application validation.

See [generated wire contracts](generated-wire-contracts.md) for the supported
preserve, omit, and reject boundary.

For a larger nested example that shows why this matters in practice, see the
[complex config example](complex-config-example.md).

## Validation flow

`SchemaFamily.validate()` and `validate_versioned()`:

1. discovers the source schema version;
2. validates input against that source version;
3. maps historical field names back to current names;
4. applies registered upgrade migrations;
5. validates the final payload against the current model.

The result preserves both sides:

```python
result = APP_CONFIG_SCHEMA.validate({"schema_version": "1"})

assert result.source_version == "1"
assert result.current_version == "2"
assert result.source_model.timeout == 5.0
assert result.current_model.timeout == 5.0
```

## Rendering flow

`SchemaFamily.dump()` and `dump_versioned()` render a payload in a requested
schema version. This is for
writing example configs, preserving legacy output formats, or showing users what
a specific schema version looks like.

```python
dumped = APP_CONFIG_SCHEMA.dump(version="1")
assert dumped == {"timeout": 5.0, "schema_version": "1"}
```
