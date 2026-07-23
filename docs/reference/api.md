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
- `describe()`: return the frozen compiled `SchemaInventory`.
- `plan_validation(source_version)`: return the cached source-to-current `ConversionPlan`.
- `plan_render(target_version)`: return the cached current-to-target `ConversionPlan`, or raise `IrreversibleTransitionError` if no complete reverse route exists.
- `model_for(version)`: return the family-local, object-shaped generated wire
  contract for that declared version.
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

### Generated wire models

`SchemaFamily.model_for()` and `model_for_version()` return generated Pydantic
v2 wire contracts, not behavioral subclasses of the current model. Generated
current and historical projections are object-shaped and preserve supported
field annotations, constraints, defaults, factories, aliases, declarative model
configuration, and static non-structural model schema metadata. Model metadata
cannot replace generated object properties, requirements, or composition.

They do not copy model or field validators, field serializers, computed fields,
private attributes, methods, `model_post_init`, or lifecycle-only configuration.
The authoritative current model remains responsible for final application
validation.

When version metadata is family-owned, the complete generated document adapter
has an exact `Literal[label]` discriminator for every version, including
current, with default `label`. With a supported validation-capable direct
model-owned field or alias, every generated document projection, including
current, declares its metadata field with exact annotation `Literal[label]` and
default `label`. Output-only or disabled validation locations are rejected.
That location must remain invariant; nested model-owned paths are rejected
until the top-level conversion compiler can resolve them safely. No
discriminator is added when `version_metadata=None`.

Automatic projection raises `UnsupportedWireModelError` for a `RootModel`,
unresolved generic, model-level serializer, overridden model core/JSON Schema
hook, application-defined annotation hook, behavioral dataclass, callable or
non-JSON schema mutation, structural model schema override, validated-data
factory, legacy `json_encoders`, arbitrary-type escape hatch, serialization
exclusion, or non-object validation or serialization shape. Pydantic v1 models
instead fail registration with `SchemaVersionError`.
See
[generated wire contracts](../guide/generated-wire-contracts.md) for the full
supported preserve, omit, and reject boundary.

### Reserved nested declarations

`NestedFamily`, `MatchingLabels`, and `matching_labels()` are exported as frozen
declaration types so the final constructor remains stable. Non-empty explicit
nested mappings currently fail compilation instead of being ignored; graph
compilation and nested execution land in their dedicated 0.2 changes.

## Compiled inventory and plans

The public inspection records are frozen value objects. Their `to_dict()`
methods return fresh deterministic dictionaries containing JSON-safe
primitives.

### Inventory records

```python
@dataclass(frozen=True)
class ProjectionDescription:
    kind: Literal["default", "removed", "renamed"]
    current_field: str
    historical_field: str | None
    has_default: bool


@dataclass(frozen=True)
class VersionDescription:
    label: str
    wire_model: Literal["current", "generated", "explicit"]
    projections: tuple[ProjectionDescription, ...]


@dataclass(frozen=True)
class TransitionDescription:
    source: str
    target: str
    upgrade: Literal["implicit_identity", "custom"]
    downgrade: Literal["implicit_identity", "custom", "unavailable"]
    downgrade_semantics: StepSemantics


@dataclass(frozen=True)
class NestedFamilyDescription:
    schema_path: str
    family: str
    versions: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class SchemaInventory:
    family: str
    model: str
    current_version: str
    versions: tuple[VersionDescription, ...]
    transitions: tuple[TransitionDescription, ...]
    nested: tuple[NestedFamilyDescription, ...]
    version_metadata: VersionMetadata | None
```

`SchemaFamily.describe()` compiles the family if necessary and returns its
cached inventory. Versions and transitions retain canonical declared order, and
every adjacent edge is present even when its upgrade is an implicit identity.
The inventory value `wire_model="current"` identifies the current version's
semantic role; `model_for(current_version)` still returns a generated wire
projection rather than the authoritative application class.
Projection descriptions reveal whether a historical version changes a default,
removes a field, or renames it, but never reveal a default value or factory.

The model is represented by its qualified name rather than a class object.
`NestedFamilyDescription` is part of the stable output contract, but inventories
remain flat while explicit nested compilation is unsupported.

### Plan records

```python
type StepKind = Literal[
    "wire_validation",
    "projection",
    "implicit_identity",
    "custom_transition",
    "nested",
    "current_validation",
    "serialization",
    "metadata",
]
type StepSemantics = Literal[
    "not_applicable",
    "exact",
    "lossy",
    "unavailable",
]


@dataclass(frozen=True)
class PlanStep:
    id: str
    family: str
    source_version: str
    target_version: str
    operation: Literal["validate", "render"]
    direction: Literal["upgrade", "downgrade"]
    kind: StepKind
    schema_path: str
    semantics: StepSemantics
    conditional: bool


@dataclass(frozen=True)
class ConversionPlan:
    family: str
    source_version: str
    target_version: str
    operation: Literal["validate", "render"]
    semantics: StepSemantics
    steps: tuple[PlanStep, ...]
```

`plan_validation(source_version)` exposes source metadata and wire validation,
field projections, each adjacent upgrade or identity edge, and current-model
validation. Its overall semantics are `not_applicable`.

`plan_render(target_version)` exposes current validation, reverse edges, target
projections and metadata, target wire validation, and serialization. Exact
structural changes produce an `exact` plan; removing a current field produces a
`lossy` plan. A custom upgrade without a declared downgrade makes the route
unavailable, so the method raises `IrreversibleTransitionError` instead of
returning that candidate.

Plan construction is data-independent and does not execute transition
callables or default factories. Step IDs use `pv1-` plus a full 64-character
SHA-256 digest and do not depend on object identity, callable representations,
or Python's randomized hash. Root-level steps use `$`; a plain metadata field
uses its field name, while tuple paths use an unambiguous `$.*` schema pattern
and literal special characters use JSON-style bracket quoting. Paths never
contain payload-derived indices or keys.

Inventories and plans never contain payloads, model objects, callable objects,
default values, exception messages, tracebacks, timing, or host/user
identifiers, and creating them does not log. A plan describes a possible
operation; it is not an execution trace. Structured per-payload traces are a
separate API.

Calling `describe()`, `plan_validation()`, or `plan_render()` performs the
family's first compilation when needed. A later legacy `@migration`
registration therefore fails instead of mutating the published inventory and
plans.

The current validation and dictionary-dump compatibility paths are not yet
driven by these public plans. In particular, a rejected render plan means that
no safe reverse transition is declared even if legacy dumping can still apply a
structural target projection.

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

Returns the generated object-shaped Pydantic wire contract for a declared
version. `subject` may be a family or a model with an explicit default family.

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
- `StepKind`: supported inventory/plan operation-step kinds
- `StepSemantics`: `not_applicable`, `exact`, `lossy`, or `unavailable`

## Exceptions

- `SchemaVersionError`
  - `SchemaCompilationError`
    - `UnsupportedWireModelError`
  - `SchemaFamilySelectionError`
  - `IrreversibleTransitionError`
  - `MissingSchemaVersionError`
  - `UnknownSchemaVersionError`
  - `DuplicateSchemaVersionError`
  - `InvalidMigrationError`
  - `VersionedValidationError`

`UnsupportedWireModelError` reports that automatic projection cannot safely
produce the required object-shaped Pydantic v2 wire contract. It is raised
during compilation and includes safe family, model, and unsupported-reason
context, plus the version for projection-specific failures. Direct validation
of a successfully generated wire model still raises Pydantic's native
`ValidationError`.
