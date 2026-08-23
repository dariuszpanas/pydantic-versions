# API Reference

## Package metadata

### `__version__`

`__version__: str` reports the version of the installed
`pydantic-versions` distribution. It is diagnostic library metadata, not an
application schema label. Use a family's declared version labels and configured
version metadata for application payloads.

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
- `defaults_for(*, version, include_version=True, **dump_kwargs)`: construct and
  serialize the selected target wire model's defaults without planning or
  executing a downgrade.
- `dump(*, version, data=None, include_version=True, **dump_kwargs)`: convert
  current data to a target-version dictionary; `data=None` delegates to
  `defaults_for()`, while `{}` remains real current input.

Compilation is idempotent and thread-safe. A family owns its generated-model
identities and cache, so two families can reuse one current model without
sharing state. Rebuild incomplete authoritative models before compilation. A
forced `model_rebuild()` after compilation invalidates the compiled family;
dependent parent families are invalidated transitively. Discard the affected
family graph and recreate its declarations from fully rebuilt models rather
than mixing stale projections with replacement Pydantic core schemas.
Forced rebuilds must not race family operations. A decorator-managed default
cannot be rebound on the same model class; redeclare and reinitialize those
models, or use a new explicit family graph.

### `SchemaVersion`

```python
@dataclass(frozen=True)
class SchemaVersion:
    label: str
    patches: tuple[VersionPatch, ...] = ()
    wire_model: type[BaseModel] | None = None
```

Labels are exact non-empty strings. The final label is current and cannot carry
historical patches or an explicit wire model. `wire_model` is the bounded
historical escape hatch for behavior that automatic projection cannot model.

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
declared transition must contain at least one callable. Upgrades are executed
during validation, and downgrades are executed during historical rendering.

### `VersionMetadata`

```python
@dataclass(frozen=True)
class VersionMetadata:
    path: str | tuple[str, ...] = "schema_version"
    owner: Literal["family", "model"] = "family"
```

Describes the version-discriminator path and its ownership. Full collision and
alias semantics are part of the runtime contract: family-owned metadata is
validated on the generated document and removed before authoritative model
validation, while a model-owned top-level field or direct alias remains part of
the model payload. See [Rendering Configs](../guide/rendering.md) and
[Version Discovery](../guide/version-discovery.md) for the supported paths and
conflict behavior.

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
validation. `model_for(...).model_validate(...)` is the direct source-wire
check; `SchemaFamily.validate(...)` performs that wire check first and then
validates the migrated payload with the authoritative current model. Neither
API treats a validator-only raw input shape as part of the generated wire
contract.

When version metadata is family-owned, the complete generated document adapter
has an exact `Literal[label]` discriminator for every version, including
current, with default `label`. With a supported validation-capable direct
model-owned field or alias, every generated document projection, including
current, declares its metadata field with exact annotation `Literal[label]` and
default `label`. Output-only or disabled validation locations are rejected.
That location must remain invariant; nested model-owned paths are rejected
until the top-level conversion compiler can resolve them safely. No
discriminator is added when `version_metadata=None`.

For an explicit historical wire body, that family-owned document adapter is a
final facade: subclass the explicit body before registering the version, not the
class returned by `model_for()`. The explicit body owns its materialized aliases,
validators, serializer, and JSON Schema callbacks exactly once. Custom model
core/JSON Schema hooks and serializer behavior that can relocate a managed
nested-family path are unsupported; this includes annotation/decorator
serializers, custom schema hooks, and legacy `json_encoders` along that path.
Nested family-owned metadata reserves its
entire root envelope, and allowed extras cannot overwrite an active declared
serialization name.

Attribute validation through this facade follows the explicit body's declared
`ConfigDict(from_attributes=True)` policy. Family metadata is checked before
body validators execute. A per-call flag cannot enable attribute input when the
body configuration disables it; callers must instead provide a mapping or an
instance of the explicit body. The facade does not proxy arbitrary body methods,
and self-references stored inside body-owned values keep body identity rather
than being rewritten as facade identity.

Automatic projection raises `UnsupportedWireModelError` for a `RootModel`,
unresolved generic, model-level serializer, overridden model core/JSON Schema
hook, application-defined annotation hook, behavioral dataclass, callable or
non-JSON schema mutation, structural model schema override, validated-data
factory, legacy `json_encoders`, arbitrary-type escape hatch, or non-object
validation or serialization shape. Pydantic v1 models
instead fail registration with `SchemaVersionError`.
See
[generated wire contracts](../guide/generated-wire-contracts.md) for the full
supported preserve, omit, and reject boundary.

### Behavior contract

The generated plan inventory and plans are the preferred compatibility artifacts:

- validation and rendering compatibility helpers are required to use a declared
  `SchemaFamily` rather than hidden model-first heuristics;
- downgrade declaration of a route that cannot be executed as a reverse plan still
  compiles in declaration-time validation, while `plan_render` rejects the
  operation with `IrreversibleTransitionError` and callers must remain
  conservative;
- unsafe declarations fail at registration and keep payloads unchanged by never
  running custom transitions before full validation;
- exceptions raised by user transition callables propagate with their original
  type and traceback. A transition that violates the dictionary return contract
  raises `InvalidMigrationError`; discovery, compilation, and rendering failures
  use the documented `SchemaVersionError` subclasses. Exception messages are
  diagnostic text rather than structured API.

### Nested declarations

```python
@dataclass(frozen=True)
class MatchingLabels:
    pass


@dataclass(frozen=True)
class NestedFamily:
    path: VersionPath
    family: SchemaFamily[Any] | Callable[[], SchemaFamily[Any]]
    versions: Mapping[str, str] | MatchingLabels
```

<!-- pv-api-signature: matching_labels -->
```python
def matching_labels() -> MatchingLabels: ...
```

`NestedFamily` declares the path to a child family and the child label selected
for each parent label. `family` accepts either the family itself or a
zero-argument callable that resolves it lazily. An explicit `versions` mapping
may select child labels independently for historical parent versions;
`matching_labels()` returns the fieldless `MatchingLabels` sentinel for the
common case where parent and child labels match exactly. The current parent
label must map to the child's current label. Compilation rejects a different
current mapping because application models hold authoritative current child
data.

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
`NestedFamilyDescription` is part of the stable output contract.

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
field projections, each adjacent upgrade or identity edge, nested-family
conversions immediately before their owning parent edges, and current-model
validation. Its overall semantics are `not_applicable`.

`plan_render(target_version)` exposes current validation, reverse edges, target
projections and metadata, target wire validation, and serialization. Exact
structural changes produce an `exact` plan; removing a current field produces a
`lossy` plan. A custom upgrade without a declared downgrade makes the route
unavailable, so the method raises `IrreversibleTransitionError` instead of
returning that candidate.

Every parent edge whose child-version mapping changes has one `nested` step per
declared path, in declaration order and immediately before the owning parent
step. The child labels appear as that step's source and target versions, while
the parent family continues to own the step and its schema path. Nested steps
are conditional because a payload may omit an optional branch or contain an
empty collection, but planning is deliberately conservative: child loss makes
the parent render plan lossy, and an unavailable child route makes the complete
parent route unavailable before any user downgrade callable runs.

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
operation; it is not an execution trace. The package does not currently expose
structured per-payload traces; `VersionedValidation.migrations_applied` remains
the compatibility view of completed top-level custom upgrades.

Calling `describe()`, `plan_validation()`, or `plan_render()` performs the
family's first compilation when needed. A later legacy `@migration`
registration therefore fails instead of mutating the published inventory and
plans.

Validation and dictionary-dumping are fully driven by these public plans. A rejected
render plan means that no safe reverse transition is declared.

## Decorator compatibility

<!-- pv-api-signature: versioned_schema -->
```python
def versioned_schema(
    *,
    name: str,
    versions: Sequence[str],
    current: str,
    version_field: VersionPath = "schema_version",
    missing_version: str | None = None,
    metadata_owner: Literal["family", "model"] | None = None,
    transitions: Sequence[VersionTransition] = (),
    nested: Sequence[NestedFamily] = (),
) -> Callable[[type[T]], type[T]]: ...
```

Builds a default family for a Pydantic model and returns the original model
class. `current` must equal the final label. The deterministic `transitions=`
argument uses `VersionTransition` records.

<!-- pv-api-signature: schema_version -->
```python
def schema_version(
    version: str,
    *,
    patches: Sequence[VersionPatch] = (),
    wire_model: type[BaseModel] | None = None,
) -> Callable[[type[T]], type[T]]: ...
```

Applies patches to one declared historical version.

<!-- pv-api-signature: schema_versions -->
```python
def schema_versions(
    versions: Sequence[str],
    *,
    patches: Sequence[VersionPatch] = (),
    wire_model: type[BaseModel] | None = None,
) -> Callable[[type[T]], type[T]]: ...
```

Applies the same patches to multiple explicitly declared historical versions.

<!-- pv-api-signature: migration -->
```python
def migration(
    subject: type[T] | SchemaFamily[T],
    from_version: str,
    to_version: str,
) -> Callable[[F], F]: ...
```

Registers a legacy forward upgrade before first compilation. `subject` may be a
family or a model with an explicit default family. Late, reverse, skipped, and
duplicate registrations fail.

## Patch helpers and records

```python
@dataclass(frozen=True)
class FieldDefault:
    name: str
    default: Any = None
    default_factory: Callable[[], Any] | None = None
    has_default: bool = True


@dataclass(frozen=True)
class FieldRemoved:
    name: str


@dataclass(frozen=True)
class FieldRenamed:
    current_name: str
    version_name: str
```

<!-- pv-api-signature: field_default -->
```python
def field_default(
    name: str,
    default: Any = ...,
    *,
    default_factory: Callable[[], Any] | None = None,
) -> FieldDefault: ...
```

Changes a field default for a historical version and returns `FieldDefault`.
Exactly one of `default` or `default_factory` is required. The ellipsis above
represents the helper's private missing-value sentinel, not a supported default
value.

<!-- pv-api-signature: field_removed -->
```python
def field_removed(name: str) -> FieldRemoved: ...
```

Removes a field from a historical version and returns `FieldRemoved`.

<!-- pv-api-signature: field_renamed -->
```python
def field_renamed(current_name: str, version_name: str) -> FieldRenamed: ...
```

Uses `version_name` in the historical schema and maps it back to `current_name`
during upgrade validation. Returns `FieldRenamed`.

`VersionPatch` is the public union of those three frozen record types.

## Runtime compatibility helpers

<!-- pv-api-signature: model_for_version -->
```python
def model_for_version(
    subject: type[T] | SchemaFamily[T],
    version: str,
) -> type[BaseModel]: ...
```

Returns the generated object-shaped Pydantic wire contract for a declared
version. `subject` may be a family or a model with an explicit default family.

<!-- pv-api-signature: validate_versioned -->
```python
def validate_versioned(
    subject: type[T] | SchemaFamily[T],
    data: Any,
    *,
    version: str | None = None,
) -> VersionedValidation[T]: ...
```

Validates `data` against the discovered source version, applies adjacent
forward upgrades, and validates the current model.

<!-- pv-api-signature: dump_versioned -->
```python
def dump_versioned(
    subject: type[T] | SchemaFamily[T],
    *,
    version: str,
    data: T | Mapping[str, Any] | None = None,
    include_version: bool = True,
    **dump_kwargs: Any,
) -> dict[str, Any]: ...
```

With `data=None`, delegates to target-direct `defaults_for()` and does not
require a render route. With model or mapping data, converts current data using
the requested render plan and returns the target serialization dictionary. An
empty mapping is data, not defaults syntax.

Mapping and unrelated model data is validated as the authoritative current
model before reverse transitions run; authoritative model instances follow
their Pydantic `revalidate_instances` policy. Embedded version metadata at any
accepted input name must match the current version. Rendering extracts
authoritative declared fields into detached canonical data, excluding allowed
extras, subclass-only fields, and serializers, so transition mutation cannot
affect caller-owned containers. Invalid unrelated models raise the current
model's `ValidationError`.

Target serialization defaults to `mode="json"` and `by_alias=True`. Supported
keyword arguments are `mode`, `by_alias`, `context`, `fallback`, `warnings`,
false `round_trip`, false `serialize_as_any`, and false or `None`
`polymorphic_serialization`. Truthy polymorphic modes, `round_trip=True`, unknown
options, and the omission options `include`, `exclude`, `exclude_unset`,
`exclude_defaults`, `exclude_none`, and `exclude_computed_fields` raise
`ValueError`. Omission options are rejected by presence.

Family-owned `include_version=False` returns a body-only dictionary for a
transport that carries the label separately. Model-owned metadata is part of
the body contract, so that option is unavailable. Family-owned metadata on
embedded child families is omitted because the parent mapping owns child
selection; model-owned child metadata remains.

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

`UnsupportedWireModelError` reports that automatic projection cannot safely
produce the required object-shaped Pydantic v2 wire contract. It is raised
during compilation and includes safe family, model, and unsupported-reason
context, plus the version for projection-specific failures. Direct validation
of a successfully generated wire model still raises Pydantic's native
`ValidationError`.

See the [stability and compatibility policy](stability-policy.md) for the exact
public/private boundary and the Semantic Versioning rules applied to this API.
