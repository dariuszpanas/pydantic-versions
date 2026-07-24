# Schema Patches

Historical schemas are derived from the current model with declarative patches.
Patches target declared schema versions, not software versions.

## Defaults

Use `field_default()` when a field still exists but had a different default in a
historical schema:

```python
@schema_version("1", patches=[field_default("timeout", 5.0)])
class AppConfig(BaseModel):
    timeout: float = 10.0
```

Default factories are supported:

```python
@schema_version("1", patches=[field_default("plugins", default_factory=list)])
class AppConfig(BaseModel):
    plugins: list[str]
```

## Removed fields

Use `field_removed()` when a field did not exist in an older schema:

```python
@schema_version("1", patches=[field_removed("new_feature")])
class AppConfig(BaseModel):
    new_feature: bool = False
```

When rendering version `1`, `new_feature` is omitted. When validating version
`1`, the generated historical model does not accept or require that field.

## Renamed fields

Use `field_renamed()` when an older schema used a different field name:

```python
@schema_version("1", patches=[field_renamed("retries", "attempts")])
class AppConfig(BaseModel):
    retries: int = 3
```

Version `1` accepts and renders `attempts`. Upgrade validation maps it back to
`retries` before current-model validation.

## Applying patches to multiple versions

Use `schema_versions()` to avoid repeated decorators when several explicit
versions share the same patches:

```python
@versioned_schema(name="app_config", versions=["1.0", "1.1", "2.0"], current="2.0")
@schema_versions(["1.0", "1.1"], patches=[field_default("timeout", 5.0)])
class AppConfig(BaseModel):
    timeout: float = 10.0
```

Only explicitly listed versions are patched. Pattern or regex matching is not
enabled because schema versions are arbitrary ordered strings, not necessarily
semantic versions.
