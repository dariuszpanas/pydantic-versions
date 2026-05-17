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

Fields removed in the target version are dropped before historical validation.

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
