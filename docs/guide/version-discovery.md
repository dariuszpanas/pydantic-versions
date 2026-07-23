# Version Discovery

`validate_versioned()` chooses the source schema version in a fixed order:

1. explicit `version=`;
2. configured `version_field` in mapping input;
3. configured `missing_version`;
4. otherwise `MissingSchemaVersionError`.

## Explicit version

An explicit version selects the expected wire contract when the version is
known outside the payload:

```python
result = validate_versioned(
    AppConfig,
    {"timeout": 5.0},
    version="1",
)

assert result.source_version == "1"
```

Use this when the version is known from a filename, database column, API route,
or another source outside the config body. If the payload also carries version
metadata, that discriminator must match the selected contract; the package does
not overwrite a conflicting value.

## Top-level version fields

By default, configs use a top-level `schema_version` field:

```yaml
schema_version: "2"
timeout: 10
```

You can use a different field name:

```python
@versioned_schema(
    name="crd_config",
    versions=["example.com/v1", "example.com/v2"],
    current="example.com/v2",
    version_field="apiVersion",
)
class ResourceConfig(BaseModel):
    replicas: int = 1
```

This works for Kubernetes-style `apiVersion` values:

```yaml
apiVersion: example.com/v1
replicas: 3
```

## Nested version fields

Use a tuple path when version metadata lives inside a wrapper object:

```python
@versioned_schema(
    name="metadata_config",
    versions=["1", "2"],
    current="2",
    version_field=("metadata", "schema_version"),
)
class MetadataConfig(BaseModel):
    timeout: float = 10.0
```

This reads:

```yaml
metadata:
  schema_version: "1"
timeout: 5
```

The metadata wrapper does not have to be part of the authoritative application
model. The generated wire model includes and validates the complete wrapper,
then family-owned metadata is removed only from the private transition value.
Strict application models can therefore still reject unrelated extra fields.

## Legacy unversioned configs

`missing_version` is only for legacy config files that do not contain version
metadata. It means "when the version field is missing, assume this schema
version."

```python
@versioned_schema(
    name="app_config",
    versions=["1", "2"],
    current="2",
    missing_version="1",
)
class AppConfig(BaseModel):
    timeout: float = 10.0
```

Then this unversioned payload is treated as schema version `1`:

```python
result = validate_versioned(AppConfig, {"timeout": 5.0})
assert result.source_version == "1"
```

If you do not set `missing_version`, unversioned input raises
`MissingSchemaVersionError`. That is the safer default for new config formats.
