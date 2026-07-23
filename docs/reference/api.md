# API Reference

## External declarations

### `SchemaFamily`

```python
class SchemaFamily[T: BaseModel]:
    def __init__(
        self,
        *,
        model: type[T],
        name: str,
        versions: Sequence[SchemaVersion],
        transitions: Sequence[VersionTransition] = (),
        nested: Sequence[NestedFamily] = (),
        version_metadata: VersionMetadata | None = VersionMetadata(),
        missing_version: str | None = None,
    ) -> None: ...
```

Owns one named history for a current Pydantic model. Construction copies every
declaration sequence and has no default-selection side effect.

- `model`, `name`, `versions`, `transitions`, `nested`, `version_metadata`, and
  `missing_version`: read-only copies of the family declarations.
- `current_version`: the final declared version label.
- `compile()`: lazily and atomically compile the immutable family state; returns the family.
- `as_default()`: deliberately select this family for model-only compatibility calls; returns the family.
- `model_for(version)`: return the family-local generated wire model.
- `validate(data, *, version=None)`: validate historical input and upgrade it to the current model.
- `defaults_for(*, version, include_version=True, **dump_kwargs)`: render target defaults.
- `dump(*, version, data=None, include_version=True, **dump_kwargs)`: return a target-version dictionary.

Compilation is idempotent and thread-safe. A family owns its generated-model
identities and cache, so two families can reuse one current model without
sharing state.

### `SchemaVersion`

```python
@dataclass(frozen=True)
class SchemaVersion:
    label: str
    patches: tuple[VersionPatch, ...] = ()
    wire_model: type[BaseModel] | None = None
```

Labels are exact non-empty strings. The final label is current and cannot carry
historical patches or an explicit wire model. `wire_model` is reserved by the
0.2 contract; the current foundation rejects non-empty use until explicit
historical-model support lands.

### `VersionTransition`

```python
@dataclass(frozen=True)
class VersionTransition:
    source: str
    target: str
    upgrade: TransitionFunc | None = None
    downgrade: TransitionFunc | None = None
    downgrade_semantics: Literal["exact", "lossy"] | None = None
```

Custom transitions must connect adjacent forward labels. An adjacent pair with
no `VersionTransition` declaration is compiled as an identity edge; every
declared transition must contain at least one callable. The current foundation
executes forward upgrades; downgrade execution lands with the
historical-rendering work and non-empty downgrade declarations are rejected in
the meantime.

### `VersionMetadata`

```python
@dataclass(frozen=True)
class VersionMetadata:
    path: str | tuple[str, ...] = "schema_version"
    owner: Literal["family", "model"] = "family"
```

Describes the version-discriminator path and its ownership. Full collision and
alias semantics are defined by the 0.2 architecture decision and implemented by
the later top-level conversion work.

### Reserved nested declarations

`NestedFamily`, `MatchingLabels`, and `matching_labels()` are exported as frozen
declaration types so the final constructor remains stable. Non-empty explicit
nested mappings currently fail compilation instead of being ignored; graph
compilation and nested execution land in their dedicated 0.2 changes.

## Decorator compatibility

`versioned_schema(name, versions, current, version_field="schema_version", missing_version=None, transitions=(), ...)`

Builds a default family for a Pydantic model and returns the original model
class. `current` must equal the final label. The deterministic `transitions=`
argument uses `VersionTransition` records.

`schema_version(version, patches=())`

Applies patches to one declared historical version.

`schema_versions(versions, patches=())`

Applies the same patches to multiple explicitly declared historical versions.

`migration(subject, from_version, to_version)`

Registers a legacy forward upgrade before first compilation. `subject` may be a
family or a model with an explicit default family. Late, reverse, skipped, and
duplicate registrations fail.

## Patch helpers and records

`field_default(name, default)` or `field_default(name, default_factory=callable)`

Changes a field default for a historical version and returns `FieldDefault`.

`field_removed(name)`

Removes a field from a historical version and returns `FieldRemoved`.

`field_renamed(current_name, version_name)`

Uses `version_name` in the historical schema and maps it back to `current_name`
during upgrade validation. Returns `FieldRenamed`.

`VersionPatch` is the public union of those three frozen record types.

## Runtime compatibility helpers

`model_for_version(subject, version)`

Returns the generated Pydantic model for a declared version. `subject` may be a
family or a model with an explicit default family.

`validate_versioned(subject, data, *, version=None)`

Validates `data` against the discovered source version, applies adjacent
forward upgrades, and validates the current model.

`dump_versioned(subject, *, version, data=None, include_version=True, **dump_kwargs)`

Renders defaults, model data, or mapping data using a requested version and
returns a dictionary. Extra keyword arguments are passed to Pydantic
`model_dump()`.

## Result

`VersionedValidation[T]`

```python
@dataclass(frozen=True)
class VersionedValidation[T: BaseModel]:
    source_version: str
    current_version: str
    source_model: BaseModel
    current_model: T
    migrations_applied: tuple[tuple[str, str], ...]
```

## Public type aliases

- `TransitionData`: `dict[str, Any]`
- `TransitionFunc`: `Callable[[TransitionData], TransitionData]`
- `VersionPath`: `str | tuple[str, ...]`
- `JsonValue`: recursive JSON-safe primitive type

## Exceptions

- `SchemaVersionError`
  - `SchemaCompilationError`
  - `SchemaFamilySelectionError`
  - `MissingSchemaVersionError`
  - `UnknownSchemaVersionError`
  - `DuplicateSchemaVersionError`
  - `InvalidMigrationError`
  - `VersionedValidationError`
