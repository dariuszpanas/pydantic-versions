# Adoption Guidance

This library is most useful when config files outlive the software release that
created them. The goal is not to make every model change invisible. The goal is
to make compatibility explicit, testable, and documented.

## Recommended Workflow

Start with the current Pydantic model:

```python
@versioned_schema(name="app_config", versions=["2"], current="2")
class AppConfig(BaseModel):
    timeout: float = 10.0
```

When a future change would alter how existing config files validate or behave,
add a new schema version:

```python
@versioned_schema(name="app_config", versions=["1", "2"], current="2")
@schema_version("1", patches=[field_default("timeout", 5.0)])
class AppConfig(BaseModel):
    timeout: float = 10.0
```

Keep schema versions small and intentional. A schema version should change when
the config contract changes, not whenever the package or application version
changes.

## When To Use Patches

Use patches for schema-level differences that can be described declaratively:

- a default changed;
- a field did not exist yet;
- a field had a different name.

Patches are best when the historical payload can still be expressed as a
generated Pydantic model.

```python
@schema_version(
    "1",
    patches=[
        field_default("timeout", 5.0),
        field_removed("new_feature"),
        field_renamed("retries", "attempts"),
    ],
)
```

## When To Use Migrations

Use migrations for data-level transformations:

- one value needs to be split into several fields;
- multiple old fields combine into one current field;
- a default depends on another field's value;
- a nested payload needs custom normalization.

```python
@migration(AppConfig, "1", "2")
def migrate_v1_to_v2(data: dict) -> dict:
    if data.get("mode") == "legacy":
        data["compatibility"] = {"enabled": True}
    return data
```

Migrations should be deterministic and side-effect free. They should not read
files, access the network, or depend on the current time.

## Handling Existing Unversioned Files

If users already have configs without schema metadata, decide what historical
schema those files represent and set `missing_version`:

```python
@versioned_schema(
    name="app_config",
    versions=["1", "2"],
    current="2",
    missing_version="1",
)
class AppConfig(BaseModel):
    ...
```

Do this only for real legacy compatibility. For new formats, prefer requiring an
explicit version field so mistakes fail early.

## Version Field Placement

Use top-level `schema_version` for simple application configs:

```yaml
schema_version: "2"
timeout: 10
```

Use `apiVersion` for CRD-like resources:

```python
@versioned_schema(
    name="resource",
    versions=["example.com/v1", "example.com/v2"],
    current="example.com/v2",
    version_field="apiVersion",
)
```

Use a tuple path when the version belongs under metadata:

```python
@versioned_schema(
    name="document",
    versions=["1", "2"],
    current="2",
    version_field=("metadata", "schema_version"),
)
```

Do not rely on implicit fallback searches across several fields unless the API
explicitly supports that behavior. A single configured version location keeps
validation deterministic.

## What To Test

For each schema family, test these cases:

- current-version payload validates into the current model;
- each supported historical version validates into the expected current model;
- `dump_versioned(..., version=...)` renders the expected historical shape;
- every migration changes only the fields it is meant to change;
- missing or unknown versions raise the expected typed errors;
- legacy unversioned configs only work when `missing_version` is intentionally configured.

These tests become the compatibility contract for users who keep config files in
Git, object storage, databases, or generated deployment manifests.
