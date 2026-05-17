# API Reference

## Decorators

`versioned_schema(name, versions, current, version_field="schema_version", missing_version=None)`

Registers a Pydantic model as a versioned schema family.

- `versions`: explicit ordered schema version strings.
- `current`: the current schema version.
- `version_field`: a top-level field name or tuple path used to read/write version metadata.
- `missing_version`: optional legacy fallback for unversioned input.

`schema_version(version, patches=())`

Applies patches to one declared version.

`schema_versions(versions, patches=())`

Applies the same patches to multiple explicitly declared versions.

`migration(model_cls, from_version, to_version)`

Registers a forward upgrade migration. Migration functions receive and return
`dict` values using current field names.

## Patch Helpers

`field_default(name, default)` or `field_default(name, default_factory=callable)`

Changes a field default for a historical version.

`field_removed(name)`

Removes a field from a historical version.

`field_renamed(current_name, version_name)`

Uses `version_name` in the historical schema and maps it back to `current_name`
during upgrade validation.

## Runtime Helpers

`model_for_version(model_cls, version)`

Returns the generated Pydantic model for a declared schema version.

`validate_versioned(model_cls, data, version=None)`

Validates `data` against the discovered source version, applies forward
migrations, and validates the current model.

`dump_versioned(model_cls, version, data=None, include_version=True, **dump_kwargs)`

Renders defaults, model data, or mapping data using a requested schema version.
Extra keyword arguments are passed to Pydantic `model_dump()`.

## Result

`VersionedValidation`

```python
@dataclass(frozen=True)
class VersionedValidation:
    source_version: str
    current_version: str
    source_model: BaseModel
    current_model: BaseModel
    migrations_applied: tuple[tuple[str, str], ...]
```

## Exceptions

- `SchemaVersionError`
- `MissingSchemaVersionError`
- `UnknownSchemaVersionError`
- `DuplicateSchemaVersionError`
- `InvalidMigrationError`
- `VersionedValidationError`
