# Rendering Configs

Use `dump_versioned()` to render a config in a requested schema version.

```python
dumped = dump_versioned(AppConfig, version="1")
```

The output validates against the generated historical model and includes the
configured version field by default.

## Render defaults

```python
@versioned_schema(name="app_config", versions=["1", "2"], current="2")
@schema_version("1", patches=[field_default("timeout", 5.0)])
class AppConfig(BaseModel):
    timeout: float = 10.0


assert dump_versioned(AppConfig, version="1") == {
    "timeout": 5.0,
    "schema_version": "1",
}
```

## Render existing data

Current model instances and mappings can be rendered into historical field names:

```python
@schema_version("1", patches=[field_renamed("retries", "attempts")])
class AppConfig(BaseModel):
    retries: int = 3


dumped = dump_versioned(AppConfig, version="1", data=AppConfig(retries=5))
assert dumped["attempts"] == 5
```

Supplied `data` always describes the current schema. The `version` argument is
the output version, not the input version. Mappings cross the authoritative
current-model boundary with their original key shape, so Pydantic's current
validators, alias priority, defaults, and constraints apply before any
downgrade. An already-constructed instance of the authoritative model is
handled according to that model's Pydantic `revalidate_instances` policy, so
the default avoids duplicate validation while an opt-in revalidation policy is
still enforced.

Rendering extracts declared fields into a private JSON-shaped payload without
flattening allowed extras, accepting subclass-only fields, or invoking model
and field serializers. Mutations made by a downgrade therefore cannot reach
caller-owned nested mappings or collections.

An unrelated `BaseModel` is accepted only when its declared structure validates
as current input. Otherwise Pydantic raises a validation error for the
authoritative current model. Package-generated current wire instances also
remain renderable, including hashable set projections that the original model
annotation cannot construct directly.

If that set bridge is required, an enclosing model with a custom `__init__`
fails closed with `UnsupportedWireModelError`. Pydantic validators and
`model_post_init` retain their exact model type and once-only lifecycle; a
custom initializer cannot be replayed without re-entering field validation.

Fields removed in the target version are dropped before historical validation.

If current input includes version metadata, it must name the current version.
Rendering rebases that metadata to the requested target. This makes a mapping
such as `{"schema_version": "1", ...}` invalid current input when the current
version is `"2"`, even if the requested output version is `"1"`. Omit the input
metadata when the application model does not own it.

## Omit version metadata

Set `include_version=False` when version metadata is stored outside the rendered
payload:

```python
dumped = dump_versioned(AppConfig, version="1", include_version=False)
```

## Nested version metadata

Nested version fields are rendered into the requested path:

```python
@versioned_schema(
    name="metadata_config",
    versions=["1", "2"],
    current="2",
    version_field=("metadata", "schema_version"),
)
class MetadataConfig(BaseModel):
    timeout: float = 10.0


assert dump_versioned(MetadataConfig, version="1") == {
    "timeout": 10.0,
    "metadata": {"schema_version": "1"},
}
```
