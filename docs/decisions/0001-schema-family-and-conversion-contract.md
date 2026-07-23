# ADR 0001: Schema-family and conversion contract

- **Status:** Accepted
- **Target:** 0.2.0
- **Decision date:** 2026-07-22
- **Tracking issue:** [#3](https://github.com/dariuszpanas/pydantic-versions/issues/3)
- **Parent roadmap:** [#2](https://github.com/dariuszpanas/pydantic-versions/issues/2)

This record defines the target 0.2.0 contract. It is not a claim that the API is
already implemented in 0.1. Implementation is split across the linked roadmap
issues and must preserve the decisions below.

## Context

The 0.1 API stores schema history behind decorators on the current model and a
private process-global registry. As histories grow, the decorator configuration
can become larger than the model it wraps. The registry also makes model
identity the only public lookup key, so two independent histories cannot safely
reuse one current model.

The current model builder, validator, and renderer derive transformations in
different code paths. Confirmed consequences include dead non-adjacent
migrations, shallow nested conversion, unsafe version-metadata insertion,
alias divergence, generated-model identity collisions, and rendering that does
not reverse value migrations.

The original application that needed a vendored copy is in another repository
and is unavailable. Its exact private changes are therefore not evidence for
this decision. Two requirements are established independently:

1. schema history must be declarable outside the current model, including from
   another module; and
2. applications must be able to inspect declared migrations, planned routes,
   and completed conversion work without logging payload data.

## Decision drivers

- Keep one authoritative current Pydantic application model.
- Let long histories grow outside that model.
- Make declarations and conversion behavior deterministic and inspectable.
- Reject ambiguity and irreversible rendering before processing user data.
- Preserve the useful 0.1 decorator and free-function path as compatibility
  adapters.
- Keep historical models usable for Pydantic, JSON Schema, and Django Ninja
  inspection without pretending they clone current-model behavior.
- Support a bounded explicit historical-model escape hatch instead of exposing
  Pydantic-internal field mutation as a plugin API.
- Make nested ordering, metadata ownership, and trace privacy explicit.

## Terminology

**Current model**

: The authoritative application `BaseModel`. Final validation always runs
  through this type.

**Schema family**

: One named, independently configured history for a current model. A current
  model may participate in more than one family.

**Historical wire model**

: The Pydantic input/output contract for one historical label. It may be
  generated from patches or supplied explicitly. It is not a behavioral clone
  of the current model.

**Projection**

: The structural description of one version relative to the current model,
  including historical names, omissions, defaults, and wire-model selection.
  Projections are snapshots, not cumulative deltas.

**Transition**

: A custom adjacent data transformation. An upgrade and downgrade are separate
  declarations.

**Upgrade**

: Conversion from an earlier version toward the current version.

**Downgrade**

: Conversion from the current version toward an earlier version. It is never
  inferred from an upgrade.

**Plan**

: An immutable, operation-specific ordered set of step templates produced
  before payload execution.

**Trace**

: An immutable record of which planned steps completed, were skipped, or
  failed. Trace metadata never contains payload values.

## Product boundary

The package owns current-model-first evolution of long-lived Pydantic config and
data contracts. Version labels are ordered opaque strings; they are not package
or semantic versions.

0.2.0 does not become a generic model-variant toolkit, API-router versioning
framework, serializer-format package, schema-diff tool, code generator, or file
loader. It does not add a general `FieldInfo` transformer/plugin API.

## Public declaration API

The implementation must provide an equivalent typed surface using these names
and semantics. Normal tuple conversion at construction is allowed, but the
declaration observed by callers is immutable.

~~~python
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Self

from pydantic import BaseModel


type TransitionData = dict[str, Any]
type TransitionFunc = Callable[[TransitionData], TransitionData]
type VersionPath = str | tuple[str, ...]
type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)


@dataclass(frozen=True)
class SchemaVersion:
    label: str
    patches: tuple[VersionPatch, ...] = ()
    wire_model: type[BaseModel] | None = None


@dataclass(frozen=True)
class VersionTransition:
    source: str
    target: str
    upgrade: TransitionFunc | None = None
    downgrade: TransitionFunc | None = None
    downgrade_semantics: Literal["exact", "lossy"] | None = None


@dataclass(frozen=True)
class VersionMetadata:
    path: VersionPath = "schema_version"
    owner: Literal["family", "model"] = "family"

    def to_dict(self) -> dict[str, JsonValue]: ...


@dataclass(frozen=True)
class MatchingLabels:
    pass


@dataclass(frozen=True)
class NestedFamily:
    path: VersionPath
    family: SchemaFamily[Any] | Callable[[], SchemaFamily[Any]]
    versions: Mapping[str, str] | MatchingLabels


def matching_labels() -> MatchingLabels: ...


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

    @property
    def current_version(self) -> str: ...

    def compile(self) -> Self: ...
    def as_default(self) -> Self: ...
    def describe(self) -> SchemaInventory: ...
    def plan_validation(self, source_version: str) -> ConversionPlan: ...
    def plan_render(self, target_version: str) -> ConversionPlan: ...
    def model_for(self, version: str) -> type[BaseModel]: ...

    def validate(
        self,
        data: Any,
        *,
        version: str | None = None,
    ) -> VersionedValidation[T]: ...

    def render(
        self,
        data: T | Mapping[str, Any],
        *,
        version: str,
        include_version: bool = True,
        **dump_kwargs: Any,
    ) -> VersionedRendering[T]: ...

    def defaults_for(
        self,
        *,
        version: str,
        include_version: bool = True,
        **dump_kwargs: Any,
    ) -> dict[str, Any]: ...

    def dump(
        self,
        *,
        version: str,
        data: T | Mapping[str, Any] | None = None,
        include_version: bool = True,
        **dump_kwargs: Any,
    ) -> dict[str, Any]: ...
~~~

`VersionPatch` is the public union of `FieldDefault`, `FieldRemoved`, and
`FieldRenamed` returned by the existing patch helpers. All names in the block
above, plus the result, inventory, plan, trace, and error types defined below,
are exported from `pydantic_versions`. Detail records are public value types so
type checkers and application tooling do not depend on private modules.

The final non-empty `versions` entry is the current version. The external API
does not repeat it as a separate `current=` argument. The compatibility
`versioned_schema(..., current=...)` decorator remains available, but its
`current` value must equal the final declared label.

`compile()` is idempotent. It resolves lazy family references and builds an
immutable family-owned cache. Compiling or using one family must not mutate the
plans, generated models, or caches of another family, even when both use the
same current model.

`SchemaVersion.patches` and `SchemaVersion.wire_model` are mutually exclusive.
The current version must use neither: its body and application contract is the
current model. Every supplied sequence and mapping, including
`NestedFamily.versions`, is defensively copied into an immutable ordered value;
mutating a caller-owned list or dictionary after construction has no effect.

## Family identity and default selection

The family object owns its declarations and compiled state. A process-global
registry must not own runtime configuration or generated-model caches.

Constructing an external family has no global selection side effect. Calls on
that family, or free functions receiving that family as their first argument,
are unambiguous. `as_default()` is the only external-family operation that
enables model-only compatibility calls. A second default for the same model is
rejected as a configuration error; it never silently replaces the first. The
application must make the one `as_default()` call deliberately during its
configuration startup, not as an incidental import side effect.

The 0.1 decorators create one explicit default family. The decorator returns the
original model class, and all decorator state is finalized before the family's
first compilation. Registering a migration after compilation is an error rather
than a mutation of live plans.

The legacy `@migration` builder is retained for compatibility but deprecated
for new 0.2 declarations. Its module must finish registering migrations before
the first runtime use; late registration fails loudly. The deterministic
migration path is `transitions=` on `versioned_schema()` or the external family
constructor. Concurrent calls to `compile()` produce one immutable cache and
the same generated-model identities.

This schematic declaration shows two isolated histories without editing the
model; [Typed examples](#typed-examples) contains the complete imports and model:

~~~python
PUBLIC_CONFIG = SchemaFamily(
    model=AppConfig,
    name="public_config",
    versions=(SchemaVersion("1"), SchemaVersion("2")),
)
INTERNAL_CONFIG = SchemaFamily(
    model=AppConfig,
    name="internal_config",
    versions=(SchemaVersion("legacy"), SchemaVersion("current")),
)

public = PUBLIC_CONFIG.validate({"schema_version": "1"})
internal = INTERNAL_CONFIG.validate({"schema_version": "legacy"})
~~~

`validate_versioned(AppConfig, data)` has no basis for choosing either external
family and raises `SchemaFamilySelectionError`. The caller may pass the family
as the first argument or deliberately call `PUBLIC_CONFIG.as_default()`.

## Ordered timeline and transition topology

- Labels are exact, non-empty strings. Arbitrary objects are not coerced with
  `str()`.
- Labels are unique and their declared order is authoritative.
- The current label is the final label.
- Each historical version is independently projected from the current model.
- Custom transitions connect adjacent labels only. Branches, skip edges, and
  arbitrary historical-to-historical conversion are not public 0.2 features.
- Every adjacent edge is visible. An omitted upgrade callable compiles as an
  `implicit_identity` step instead of disappearing.
- Duplicate, reverse-ordered, and non-adjacent declarations fail compilation.
- A `VersionTransition` must provide at least one callable. A downgrade-only
  declaration leaves validation on that edge as an explicit implicit-identity
  step.
- A custom upgrade without a downgrade makes rendering across that edge
  unavailable.
- A custom downgrade declares whether its result is intended to be exact or
  lossy. The package validates the target wire contract but cannot prove
  semantic equality.
- `downgrade_semantics` is required when a downgrade is present and forbidden
  when it is absent.
- Transition functions receive a fresh dictionary and must return a dictionary.
  Their input is never the caller's mapping or a cached intermediate value.
- If a historical projection removes a current required field that has no
  default, compilation requires an upgrade on the edge that first introduces
  that field. It does not accept a route guaranteed to fail final validation.

Structural rename and default projections can render in reverse. Removing a
field from a historical version is allowed but the render plan is marked lossy.
The package never invents an inverse for custom code.

## Canonical validation pipeline

Validation targets the current model; arbitrary version-to-version conversion
is not public in 0.2. Execution has three phases.

Once at the source boundary:

1. discover the source label, reject conflicts, and select the validation plan;
2. copy the input and synthesize only metadata justified by an explicit version
   or configured missing-version fallback;
3. validate the complete source document wire model;
4. extract declared Python field values without invoking Pydantic serializers,
   computed fields, or serialization exclusions; and
5. remove family-owned metadata from the private working value and normalize
   generated historical names into the selected version's canonical field
   space.

For each adjacent parent edge, in order:

1. advance each mapped embedded child from its source mapping to its target
   mapping; then
2. run the parent upgrade, or record an implicit identity step.

Once after the final edge, validate the authoritative current model.

The serializer-free extractor reads only declared Pydantic fields from the
already validated instance. It preserves nested model and union identity until
child-family dispatch is complete, deliberately excludes computed/private
state, and then builds a fresh transition dictionary. A source model serializer
never determines migration input.

Generated projections normalize historical names to their current field
identities before compatibility migration functions run, preserving the 0.1
contract. An explicit wire model uses its declared Python field names and must
provide the upgrade needed to reach the next version's canonical shape.

Pydantic aliases are handled by the wire/current model at their own validation
boundaries. The conversion engine operates on Python field names and must not
manually inject duplicate alias keys.

## Rendering and historical defaults

`render()` is a real current-to-historical operation and requires current model
data. It returns a structured result with a trace. The complete route is
preflighted before any user downgrade function runs. Render planning is
deliberately data-independent and conservative: every child route reachable
through an optional, union, or container annotation must be reversible. One
unavailable conditional child edge makes `plan_render()` fail with
`IrreversibleTransitionError`, even when a particular payload would skip that
branch. `PlanStep.conditional` controls only whether a proven step executes; it
never makes route availability depend on payload inspection. Execution again
has three phases.

Once at the current boundary:

1. select and preflight the complete render plan;
2. verify any configured metadata present in mapping input identifies the
   family current label, then validate through the authoritative current model,
   or accept an already validated instance of that exact model type;
3. verify model-owned metadata on the validated current instance identifies the
   family current label; and
4. use the serializer-free extractor to create a private current canonical
   value.

A mapping passed to `render()` is current body or current document data, never
historical/target-shaped data. A configured metadata value already present must
equal the family current label or `VersionConflictError` is raised, even when
the application model would ignore that field as extra input. Family-owned
metadata is verified and removed only from the private copy before application
model validation. Model-owned metadata is verified after validation as well,
including on an exact current-model instance; it is never silently normalized
from a historical label at this boundary.

For each reversed parent edge, in order:

1. run the parent downgrade; then
2. downgrade each mapped embedded child.

Once after the final edge:

1. apply the target structural projection;
2. construct the complete target document, including verified metadata;
3. validate the target document wire model; and
4. serialize that model exactly once under its Pydantic serialization contract.

Declarative patches may not overlap a child-managed path. A parent transition
may read current-shaped child data but contractually may not create, delete,
move, or mutate that subtree. A transition may likewise read its family's
model-owned metadata path but may not create, delete, move, or change it. The
engine snapshots both protected regions and checks the returned value; a
violation raises `TransitionExecutionError` without mutating caller data.
Immediately before every custom transition or implicit-identity step, the
private value contains that directional edge's source label. The step observes
that source label; only the engine replaces it with the directional target
label after the step succeeds. This rule also applies to implicit identities
and to embedded child labels selected by the parent's mapping. Final current or
target normalization is the result of those edge advances, not a whole-route
overwrite. A parent-owned shape change must leave that path out of `nested=` and
treat the subtree as opaque. Compilation cannot infer the behavior of arbitrary
Python code and does not claim to do so.

`defaults_for()` constructs target wire defaults. It is not a downgrade and has
no conversion trace. Existing `dump_versioned(..., data=None)` retains its 0.1
meaning by delegating to this operation.

`dump()` and `dump_versioned()` remain dictionary-returning convenience
operations. With data they return `render(...).payload`; without data they
return `defaults_for(...)`. No `with_trace` boolean changes a function's return
type.

The compatibility `**dump_kwargs` accepts non-omitting Pydantic serialization
controls such as `mode`, `by_alias`, `context`, `round_trip`, `warnings`,
`fallback`, and `serialize_as_any`. Options that can remove contract fields,
including `include`, `exclude`, `exclude_unset`, `exclude_defaults`, and
`exclude_none`, are rejected for versioned rendering. The package validates the
target model before serialization and then trusts that model's serialization
schema; it does not incorrectly feed serialization aliases or custom serializer
output back through the input-validation schema.

## Inventory, plans, and traces

The public inventory, description, plan, and trace records are frozen value
objects with stable equality and a deterministic `to_dict()` containing
JSON-safe primitives. Those descriptive records never serialize model classes,
callable objects, object representations, or payload data. The validation and
rendering result records intentionally carry models or payloads and do not
provide this descriptive `to_dict()` contract.

At minimum, the records expose this information:

~~~python
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
class ProjectionDescription:
    kind: Literal["default", "removed", "renamed"]
    current_field: str
    historical_field: str | None
    has_default: bool

    def to_dict(self) -> dict[str, JsonValue]: ...


@dataclass(frozen=True)
class VersionDescription:
    label: str
    wire_model: Literal["current", "generated", "explicit"]
    projections: tuple[ProjectionDescription, ...]

    def to_dict(self) -> dict[str, JsonValue]: ...


@dataclass(frozen=True)
class TransitionDescription:
    source: str
    target: str
    upgrade: Literal["implicit_identity", "custom"]
    downgrade: Literal["implicit_identity", "custom", "unavailable"]
    downgrade_semantics: StepSemantics

    def to_dict(self) -> dict[str, JsonValue]: ...


@dataclass(frozen=True)
class NestedFamilyDescription:
    schema_path: str
    family: str
    versions: tuple[tuple[str, str], ...]

    def to_dict(self) -> dict[str, JsonValue]: ...


@dataclass(frozen=True)
class SchemaInventory:
    family: str
    model: str
    current_version: str
    versions: tuple[VersionDescription, ...]
    transitions: tuple[TransitionDescription, ...]
    nested: tuple[NestedFamilyDescription, ...]
    version_metadata: VersionMetadata | None

    def to_dict(self) -> dict[str, JsonValue]: ...


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

    def to_dict(self) -> dict[str, JsonValue]: ...


@dataclass(frozen=True)
class ConversionPlan:
    family: str
    source_version: str
    target_version: str
    operation: Literal["validate", "render"]
    semantics: StepSemantics
    steps: tuple[PlanStep, ...]

    def to_dict(self) -> dict[str, JsonValue]: ...


@dataclass(frozen=True)
class TraceEvent:
    step_id: str
    status: Literal["completed", "skipped", "failed"]
    family: str
    source_version: str
    target_version: str
    direction: Literal["upgrade", "downgrade"]
    kind: StepKind
    schema_path: str

    def to_dict(self) -> dict[str, JsonValue]: ...


@dataclass(frozen=True)
class ConversionTrace:
    family: str
    source_version: str
    target_version: str
    operation: Literal["validate", "render"]
    events: tuple[TraceEvent, ...]

    def to_dict(self) -> dict[str, JsonValue]: ...


@dataclass(frozen=True)
class VersionedValidation[T: BaseModel]:
    source_version: str
    current_version: str
    source_model: BaseModel
    current_model: T
    migrations_applied: tuple[tuple[str, str], ...]
    trace: ConversionTrace


@dataclass(frozen=True)
class VersionedRendering[T: BaseModel]:
    source_model: T
    target_version: str
    target_model: BaseModel
    payload: dict[str, Any]
    trace: ConversionTrace
~~~

Inventory intentionally records that a default exists without serializing its
value or factory. Stable step IDs derive from declaration identity, not object
identity or callable `repr()`.

Plan paths are safe schema patterns such as `workers[*].retry`. They never
contain actual list indices, mapping keys, union discriminator values, or other
payload-derived values. Container and union steps are conditional templates. A
trace records one aggregate status per template, not one event per payload
element.

A successful trace is an ordered realization of a plan, not a promise that
`trace.events == plan.steps`. For an aggregate conditional/container template,
`completed` means at least one occurrence ran and all succeeded, `skipped`
means no occurrence selected that template, and `failed` means at least one
occurrence failed and dominates the aggregate. Counts are never recorded. The
failed event is last because conversion stops. Events always correlate through
stable step IDs.

`migrations_applied` remains a derived compatibility view of completed
top-level custom upgrade transitions.

## Trace privacy and security

Plans, inventories, traces, and their `to_dict()` representations contain no:

- input, intermediate, or output values;
- mapping keys, concrete container indices, or container sizes;
- callable arguments or return values;
- exception messages or tracebacks;
- timing, host, process, or user identifiers; or
- automatic log records.

The original exception is available only through explicit exception chaining
and typed error attributes. The package does not log it. A later telemetry
adapter may consume safe step metadata, but callbacks and automatic telemetry
are outside 0.2 core scope.

## Nested family graph

Every nested declaration identifies an exact child family and a path expressed
in current-model Python field names, never wire aliases. Container traversal is
derived from Pydantic annotations. The mapping is complete for every parent
label, and the parent current label must map to the child current label. Version
selection is explicit. The following is a schematic declaration;
[Typed examples](#typed-examples) contains complete model definitions and
imports:

~~~python
PIPELINE_SCHEMA = SchemaFamily(
    model=PipelineConfig,
    name="pipeline",
    versions=(SchemaVersion("1"), SchemaVersion("2")),
    nested=(
        NestedFamily(
            path=("workers", "retry"),
            family=RETRY_SCHEMA,
            versions={"1": "legacy", "2": "current"},
        ),
    ),
)
~~~

`matching_labels()` is an explicit shorthand that expands to a complete
parent-to-child mapping. Equal label strings are never assumed to describe the
same evolution event merely because they match.

Compilation traverses direct fields and ordinary unversioned `BaseModel`
wrappers through list, tuple, set, frozenset, mapping values, union/optional,
and `Annotated` annotations. Mapping keys and arbitrary custom containers are
not converted.

Embedded children are controlled by the parent mapping. A family-owned child
envelope discriminator is therefore omitted. A model-owned child discriminator
is a declared body field and remains in the embedded wire model, narrowed to
the child label selected by the parent mapping; an unequal value raises
`VersionConflictError`. It confirms the already selected route and never
performs independent dispatch. Independently discriminated nested documents are
deferred; they require runtime route dispatch rather than one deterministic
root plan.

Self-recursive and mutually recursive schema graphs use placeholders and model
rebuild. If a graph cannot be represented, compilation raises a contextual
package error. A raw `RecursionError` never crosses the public boundary. Cyclic
runtime object identities are not supported.

## Historical wire-model fidelity

`model_for()` and `model_for_version()` return historical wire contracts, not
behavioral subclasses of the current model.

Generated wire models preserve supported field annotations, Pydantic metadata
and constraints, required/default/default-factory state, field aliases,
validation aliases, serialization aliases, and alias generators. The compiler
also preserves `extra`, `strict`, `populate_by_name`, `validate_by_alias`,
`validate_by_name`, `serialize_by_alias`, `loc_by_alias`, `use_enum_values`,
Pydantic's declarative string/number/temporal/bytes coercion settings, and
static title or mapping-based JSON Schema metadata. Lifecycle-only settings
such as assignment validation and frozen instances are not part of a wire
contract.

Generated models do not clone model validators, serializers, computed fields,
private attributes, methods, `model_post_init`, or custom core/JSON Schema
hooks. Ordinary field/model validators may exist on the current model because
that model still performs final validation, but they are not copied.
`RootModel`, unresolved generic models, model-level serializers, overridden
core/JSON Schema hooks, callable schema mutation, and non-object serialization
fail automatic projection with `UnsupportedWireModelError`.

All current and historical 0.2 wire bodies are object-shaped Pydantic v2
`BaseModel` contracts. An explicit wire model does not make a root list or
scalar compatible with the dictionary transition API; wrap that value in a
named object field instead. Pydantic v1 compatibility models are rejected.

For example, automatic field patches do not reinterpret a root model as an
ordinary model:

~~~python
import pytest
from pydantic import RootModel
from pydantic_versions import (
    SchemaFamily,
    SchemaVersion,
    UnsupportedWireModelError,
    field_default,
)


class TokenList(RootModel[list[str]]):
    pass


with pytest.raises(UnsupportedWireModelError):
    SchemaFamily(
        model=TokenList,
        name="tokens",
        versions=(
            SchemaVersion(
                "1",
                patches=(field_default("root", default_factory=list),),
            ),
            SchemaVersion("2"),
        ),
    ).compile()
~~~

An explicit historical model is the bounded escape hatch:

~~~python
from typing import Any

from pydantic import BaseModel
from pydantic_versions import SchemaFamily, SchemaVersion, VersionTransition


class AppConfig(BaseModel):
    timeout: float = 10.0


class ConfigV1(BaseModel):
    timeout: str


def upgrade_timeout(data: dict[str, Any]) -> dict[str, Any]:
    return {**data, "timeout": float(data["timeout"])}


def downgrade_timeout(data: dict[str, Any]) -> dict[str, Any]:
    return {**data, "timeout": str(data["timeout"])}


CONFIG_SCHEMA = SchemaFamily(
    model=AppConfig,
    name="config",
    versions=(
        SchemaVersion("1", wire_model=ConfigV1),
        SchemaVersion("2"),
    ),
    transitions=(
        VersionTransition(
            "1",
            "2",
            upgrade=upgrade_timeout,
            downgrade=downgrade_timeout,
            downgrade_semantics="exact",
        ),
    ),
)
~~~

The explicit model owns its intentionally declared validators, object-shaped
serializers, and JSON Schema. It requires an explicit upgrade. Rendering to it
requires an explicit downgrade. The compiler rejects an explicit model whose
validation or serialization schema is not object-shaped. Factories, arbitrary
field transformers, and requiring a separate model for every version are not
part of this escape hatch.

Generated class and JSON Schema component identities are deterministic and
collision-resistant. Their human-readable prefix contains current-model,
family, and label slugs; a suffix contains the first 12 hexadecimal characters
of SHA-256 over length-prefixed UTF-8 values for the current model's module and
qualified name, the exact family name, and the exact label. Labels `1.0` and
`1-0` therefore cannot collide. A family name is a non-empty stable identifier;
two different declarations with the same model and family identity may not be
combined in one schema graph. Plan-step IDs use the same encoding plus the
operation, direction, kind, source, target, schema path, semantics, and
declaration ordinal, together with safe step-specific declaration details such
as projection, wire-model, and metadata kind. They use a versioned `pv1-`
prefix and the full 64-character SHA-256 digest because they are durable
correlation identifiers; unlike generated Python class-name suffixes, they are
not truncated. Default values, factories, and callable identities are excluded.

## Version metadata ownership

`VersionMetadata.owner` has two modes.

**`family`**

: The discriminator belongs to the family/document envelope. Its path may not
  overlap a body-model field or alias. For every version, the compiler composes
  the body model with a family-owned document adapter whose discriminator is
  `Literal[label]`, including nested envelope wrappers. Public `model_for()`
  returns that complete document wire model, and the current version receives
  an adapter too. Source validation checks the complete document, then the
  engine removes metadata only from its private transition value. Rendering
  validates the complete target document before serialization. The adapter then
  delegates to the body model's Pydantic serializer exactly once, requires its
  result to remain object-shaped, and safely inserts metadata into a fresh
  mapping. This outer serialization boundary preserves an explicit body's
  validators and model serializer without allowing that serializer to omit or
  replace the family-owned discriminator. A non-mapping result or collision
  raises `VersionedRenderingError`. Unsupported composition fails compilation.

**`model`**

: The configured wire path must resolve through declared model fields and
  aliases at one invariant location in every version. For every automatically
  projected version, including current, the compiler creates a distinct
  non-behavioral document wire projection whose metadata field
  annotation/default is `Literal[label]`. `model_for(current)` returns that
  projection; it never mutates or subclasses the authoritative application
  model, which still owns final current validation. An explicit historical wire
  model is not copied or subclassed: to preserve its validators, serializer, and
  JSON Schema, it must itself declare the invariant metadata field as the exact
  `Literal[label]` with a matching default. Compilation rejects an explicit
  model that does not. The discriminator is verified at the operation boundary
  and advanced on a private working copy one edge at a time under the transition
  ownership rule above, ending at the current label during validation and the
  target label during rendering. When an explicit version or fallback supplies
  an otherwise absent value, the engine injects it into the copied document
  before wire validation. It is never silently overwritten. After
  serialization, the engine verifies that the object-shaped result retains the
  exact discriminator at the configured path resolved for the selected alias
  mode; omission or disagreement raises `VersionedRenderingError` without
  revalidating serializer
  output. Model-owned metadata cannot be omitted with `include_version=False`.

`version_metadata=None` means the document carries no embedded discriminator;
validation requires an explicit version or an intentional `missing_version`
fallback.

An explicit version and embedded value must agree. A conflict raises
`VersionConflictError`; the explicit argument no longer silently wins. A
present `None` is invalid rather than missing. Missing fallback applies only
when the configured path is absent.

Insertion never replaces a non-mapping path component or an existing unequal
value. Metadata collisions are compilation or conversion errors, not data
mutation.

With family-owned metadata, `include_version=False` is an explicit body-only
serialization mode: the already validated document is copied and its metadata
path is removed. The returned dictionary is not claimed to be a complete
versioned document. This option is unavailable for model-owned metadata.

## Alias precedence

1. The engine discovers and verifies metadata at its invariant wire path.
2. The selected document wire model applies its Pydantic validation aliases.
3. The serializer-free extractor reads declared Python field values.
4. Structural projection normalizes generated historical names.
5. Transitions operate on a private canonical mapping.
6. The current or target document model performs authoritative validation.
7. Target serialization applies aliases and allowed dump options exactly once.

A renamed field uses its declared historical Python name; the current field's
aliases are not silently attached to a different name. Unrenamed generated
fields preserve supported alias behavior. Complex historical alias behavior
belongs in an explicit wire model.

## Errors and partial traces

All new package errors remain subclasses of `SchemaVersionError`.

| Error | Contract |
| --- | --- |
| `SchemaCompilationError` | A declaration, projection, topology, nested graph, or generated identity cannot be compiled safely. |
| `SchemaFamilySelectionError` | A model-only compatibility call has no explicit default, or attaching another default was attempted. |
| `VersionConflictError` | Explicit, embedded, parent-mapped, or model-owned version information disagrees. |
| `UnsupportedWireModelError` | Automatic generation or an explicit model would violate the object-shaped wire contract. |
| `IrreversibleTransitionError` | A complete render plan cannot be formed before payload execution. |
| `TransitionExecutionError` | User transition code fails or violates its return contract; includes the safe failed step and partial trace. |
| `VersionedValidationError` | Wraps source/current Pydantic validation during `validate()` with `.validation_error`, `.phase`, and the safe partial trace. |
| `VersionedRenderingError` | Wraps current-input or target Pydantic validation and serialization during `render()`; it also wraps target-default validation or serialization during `defaults_for()`. |

The original exception is preserved with `raise ... from exc`. Wrapped Pydantic
errors remain available through `.validation_error`; direct use of a returned
wire model still raises native `pydantic.ValidationError`. Serialized package
errors and traces never include the chained exception message automatically.
Privacy tests must verify that package-error `str`, `repr`, and `to_dict()` do
not traverse `.validation_error`, its input values, or the chained cause.

Existing `MissingSchemaVersionError`, `UnknownSchemaVersionError`,
`DuplicateSchemaVersionError`, and `InvalidMigrationError` remain importable.
`TransitionExecutionError` subclasses `InvalidMigrationError`, so existing
catches still handle a migration callable that raises or returns a non-dict.
Legacy decorator declaration errors continue to raise `InvalidMigrationError`
directly; external-family compilation errors use `SchemaCompilationError`.
The previously unused `VersionedValidationError` receives the defined role
above rather than remaining an empty promise. `VersionedRenderingError.phase`
is one of `current_validation`, `target_validation`, `defaults_validation`, or
`serialization`; `.validation_error` is present for Pydantic failures.
`render()` errors carry the safe partial trace. Because `defaults_for()` does
not perform a conversion, its `VersionedRenderingError.trace` is `None`.

## Compatibility API

The current imports remain available. Runtime free functions widen their first
argument to accept either a model or a family:

~~~python
def model_for_version[T: BaseModel](
    subject: type[T] | SchemaFamily[T],
    version: str,
) -> type[BaseModel]: ...


def validate_versioned[T: BaseModel](
    subject: type[T] | SchemaFamily[T],
    data: Any,
    *,
    version: str | None = None,
) -> VersionedValidation[T]: ...


def render_versioned[T: BaseModel](
    subject: type[T] | SchemaFamily[T],
    data: T | Mapping[str, Any],
    *,
    version: str,
    include_version: bool = True,
    **dump_kwargs: Any,
) -> VersionedRendering[T]: ...


def dump_versioned[T: BaseModel](
    subject: type[T] | SchemaFamily[T],
    *,
    version: str,
    data: T | Mapping[str, Any] | None = None,
    include_version: bool = True,
    **dump_kwargs: Any,
) -> dict[str, Any]: ...


def schema_version[T: BaseModel](
    version: str,
    *,
    patches: Sequence[VersionPatch] = (),
) -> Callable[[type[T]], type[T]]: ...


def schema_versions[T: BaseModel](
    versions: Sequence[str],
    *,
    patches: Sequence[VersionPatch] = (),
) -> Callable[[type[T]], type[T]]: ...


def versioned_schema[T: BaseModel](
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


def migration[T: BaseModel](
    subject: type[T] | SchemaFamily[T],
    from_version: str,
    to_version: str,
    *,
    downgrade: TransitionFunc | None = None,
    downgrade_semantics: Literal["exact", "lossy"] | None = None,
) -> Callable[[TransitionFunc], TransitionFunc]: ...
~~~

`migration()` decorates the forward upgrade and optionally receives the reverse
callable. It accepts a family or resolves the model's explicit default.
External declarations should normally put transitions directly in the family
constructor, and decorator users should prefer the deterministic
`transitions=` argument over late registration.

When `metadata_owner=None`, the decorator adapter infers `model` only if the
configured wire path resolves unambiguously through the current model's fields
and aliases; otherwise it uses `family`. Explicit `metadata_owner` removes that
inference. For 0.1 nested compatibility only, an omitted `nested=` must discover
each embedded field whose type has exactly one decorator-created default child
family. When its ordered labels exactly equal the parent's, the adapter
synthesizes `matching_labels()`. A discovered decorator child with a different
timeline, or an ambiguous field with multiple candidate families, fails
compilation instead of being silently ignored. External families are never
auto-discovered and require explicit mappings. Redundant family-owned child
discriminators that 0.1 generated are intentionally removed from the root
document projection in 0.2; declared model-owned discriminator fields remain
as specified above.

The following intentional correctness changes are part of the 0.2 migration
contract:

| 0.1 behavior | 0.2 behavior |
| --- | --- |
| Any declared label may be `current`. | The final label is the only current version. |
| Labels and sequence members are coerced with `str()`. | Labels must already be non-empty strings; a string is rejected where a sequence is required. |
| Non-adjacent forward migrations are accepted but never executed. | They fail compilation. |
| Explicit version silently overrides conflicting embedded metadata. | Conflicts raise `VersionConflictError`. |
| Metadata insertion may replace existing data. | Collisions and non-mapping paths fail without mutation. |
| Rendering ignores custom upgrades. | It uses an explicit downgrade or fails route planning. |
| Generated models may be mistaken for class clones. | They are documented and enforced as wire contracts. |
| Pydantic conversion errors escape without family/step context. | Conversion APIs wrap them while preserving the native error and cause. |
| Decorated nested children inherit labels and add discriminators implicitly. | Exact-label inheritance is a legacy-adapter special case; redundant family-owned child envelope discriminators are removed, while model-owned fields remain and are verified. |
| Arbitrary `model_dump()` omission options are forwarded. | Contract-field omission options are rejected by rendering. |
| `RootModel` may be converted into an ordinary generated model. | 0.2 wire bodies are object-shaped; root/scalar contracts fail compilation. |
| `dump_versioned(data=None)` creates target defaults. | That behavior remains, explicitly delegated to `defaults_for()`. |
| `dump_versioned()` returns a dictionary. | It continues returning a dictionary; `render_versioned()` exposes the trace. |

The validation wrapper is intentional: a native Pydantic error alone cannot
carry safe partial conversion progress. Existing callers migrate as follows:

~~~python
from typing import Any

from pydantic import BaseModel, ValidationError
from pydantic_versions import (
    ConversionTrace,
    VersionedValidationError,
    validate_versioned,
    versioned_schema,
)


@versioned_schema(name="app_config", versions=("1", "2"), current="2")
class AppConfig(BaseModel):
    timeout: float


payload: dict[str, Any] = {
    "schema_version": "1",
    "timeout": "not-a-number",
}

try:
    validate_versioned(AppConfig, payload)
except VersionedValidationError as exc:
    native_error: ValidationError = exc.validation_error
    safe_trace: ConversionTrace = exc.trace
~~~

The package wrapper does not stringify or serialize `native_error` unless the
application explicitly chooses to inspect it.

## Typed examples

History can live in a separate module without decorating the current model:

~~~python
# models.py
from pydantic import BaseModel


class AppConfig(BaseModel):
    timeout: float = 10.0


# schema_history.py
from typing import assert_type

from models import AppConfig
from pydantic_versions import SchemaFamily, SchemaVersion, field_default


APP_CONFIG_SCHEMA = SchemaFamily(
    model=AppConfig,
    name="app_config",
    versions=(
        SchemaVersion("1", patches=(field_default("timeout", 5.0),)),
        SchemaVersion("2"),
    ),
)

result = APP_CONFIG_SCHEMA.validate({"schema_version": "1"})
assert_type(result.current_model, AppConfig)
~~~

Two external families stay explicit, and a model-only call has no import-order
fallback:

~~~python
import pytest
from typing import assert_type

from models import AppConfig
from pydantic_versions import (
    SchemaFamily,
    SchemaFamilySelectionError,
    SchemaVersion,
    validate_versioned,
)

PUBLIC_CONFIG = SchemaFamily(
    model=AppConfig,
    name="public_config",
    versions=(SchemaVersion("1"), SchemaVersion("2")),
)
INTERNAL_CONFIG = SchemaFamily(
    model=AppConfig,
    name="internal_config",
    versions=(SchemaVersion("legacy"), SchemaVersion("current")),
)

assert_type(
    PUBLIC_CONFIG.validate({"schema_version": "1"}).current_model,
    AppConfig,
)
assert_type(
    INTERNAL_CONFIG.validate({"schema_version": "legacy"}).current_model,
    AppConfig,
)

with pytest.raises(SchemaFamilySelectionError):
    validate_versioned(AppConfig, {"schema_version": "1"})
~~~

The decorator path remains typed and delegates to a default family:

~~~python
from typing import assert_type

from pydantic import BaseModel
from pydantic_versions import (
    VersionedValidation,
    validate_versioned,
    versioned_schema,
)


@versioned_schema(name="app_config", versions=["1", "2"], current="2")
class DecoratedConfig(BaseModel):
    timeout: float = 10.0


result = validate_versioned(DecoratedConfig, {"schema_version": "2"})
assert_type(result, VersionedValidation[DecoratedConfig])
~~~

Trace access is explicit and the legacy dump return is unchanged:

~~~python
from typing import Any, assert_type

from models import AppConfig
from schema_history import APP_CONFIG_SCHEMA
from pydantic_versions import ConversionTrace, dump_versioned


rendered = APP_CONFIG_SCHEMA.render(AppConfig(), version="1")
assert_type(rendered.trace, ConversionTrace)
assert_type(rendered.payload, dict[str, Any])

payload = dump_versioned(APP_CONFIG_SCHEMA, version="1", data=AppConfig())
assert_type(payload, dict[str, Any])
~~~

An upgrade-only edge blocks rendering before user data is transformed:

~~~python
from typing import Any

import pytest

from models import AppConfig
from pydantic_versions import (
    IrreversibleTransitionError,
    SchemaFamily,
    SchemaVersion,
    VersionTransition,
)


def upgrade_v1(data: dict[str, Any]) -> dict[str, Any]:
    return data


UPGRADE_ONLY = SchemaFamily(
    model=AppConfig,
    name="upgrade_only",
    versions=(SchemaVersion("1"), SchemaVersion("2")),
    transitions=(VersionTransition("1", "2", upgrade=upgrade_v1),),
)

with pytest.raises(IrreversibleTransitionError):
    UPGRADE_ONLY.plan_render("1")
~~~

Nested timelines require a complete mapping or an explicit shorthand:

~~~python
import pytest
from pydantic import BaseModel
from pydantic_versions import (
    NestedFamily,
    SchemaCompilationError,
    SchemaFamily,
    SchemaVersion,
)


class RetryConfig(BaseModel):
    attempts: int = 3


class WorkerConfig(BaseModel):
    retry: RetryConfig


class PipelineConfig(BaseModel):
    workers: list[WorkerConfig]


RETRY_SCHEMA = SchemaFamily(
    model=RetryConfig,
    name="retry",
    versions=(SchemaVersion("legacy"), SchemaVersion("current")),
)

PIPELINE_SCHEMA = SchemaFamily(
    model=PipelineConfig,
    name="pipeline",
    versions=(SchemaVersion("1"), SchemaVersion("2")),
    nested=(
        NestedFamily(
            path=("workers", "retry"),
            family=RETRY_SCHEMA,
            versions={"1": "legacy", "2": "current"},
        ),
    ),
).compile()

# Rejected at compile time: parent label "2" has no child selection.
with pytest.raises(SchemaCompilationError):
    SchemaFamily(
        model=PipelineConfig,
        name="incomplete_pipeline",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        nested=(
            NestedFamily(
                path=("workers", "retry"),
                family=RETRY_SCHEMA,
                versions={"1": "legacy"},
            ),
        ),
    ).compile()
~~~

Family-owned metadata cannot overwrite business data:

~~~python
import pytest
from pydantic import BaseModel
from pydantic_versions import (
    SchemaCompilationError,
    SchemaFamily,
    SchemaVersion,
    VersionMetadata,
)


class ConflictingConfig(BaseModel):
    schema_version: str


# Rejected: family-owned metadata overlaps a model field.
with pytest.raises(SchemaCompilationError):
    SchemaFamily(
        model=ConflictingConfig,
        name="conflicting",
        versions=(SchemaVersion("1"),),
        version_metadata=VersionMetadata("schema_version", owner="family"),
    ).compile()
~~~

## Alternatives considered

**Continue adding decorators**

: Rejected because it preserves the recalled model-readability problem and
  model-only identity.

**Wrap the existing global registry with a public family object**

: Rejected because two families would still share mutable plans and caches.

**Automatically choose the only family imported so far**

: Rejected because importing another module could change runtime behavior.

**Infer child mappings from equal strings**

: Rejected because equal labels in independent families do not establish the
  same release event. `matching_labels()` makes that choice explicit.

**Automatically invert upgrade functions**

: Rejected because arbitrary value/shape transformations are not invertible.

**Add `with_trace=True` to `dump_versioned()`**

: Rejected because a boolean-dependent return type silently complicates the
  stable dictionary API. `render_versioned()` is explicit.

**Expose arbitrary historical-to-historical conversion**

: Deferred. Validation-to-current and rendering-from-current are the product
  operations required for 0.2.

**Expose a generic field/model transformer plugin**

: Rejected in favor of explicit historical Pydantic wire models and explicit
  transitions.

## Consequences and delivery

The primary API grows, but configuration, caches, and behavior become local to
one inspectable object. 0.1 users can migrate incrementally because decorators
and dictionary-returning free functions remain adapters. Unsafe declarations
that were previously accepted may fail earlier and more clearly.

Implementation remains split into focused issues:

- [#4](https://github.com/dariuszpanas/pydantic-versions/issues/4): external families and immutable compilation;
- [#5](https://github.com/dariuszpanas/pydantic-versions/issues/5): inventory and operation-specific plans;
- [#6](https://github.com/dariuszpanas/pydantic-versions/issues/6): generated wire-model contract;
- [#7](https://github.com/dariuszpanas/pydantic-versions/issues/7): top-level plan execution, aliases, and metadata;
- [#8](https://github.com/dariuszpanas/pydantic-versions/issues/8): explicit downgrade and render refusal;
- [#9](https://github.com/dariuszpanas/pydantic-versions/issues/9): explicit historical wire models;
- [#10](https://github.com/dariuszpanas/pydantic-versions/issues/10): nested graph compilation and mappings;
- [#11](https://github.com/dariuszpanas/pydantic-versions/issues/11): deterministic nested execution;
- [#12](https://github.com/dariuszpanas/pydantic-versions/issues/12): traces and contextual execution failures; and
- [#13](https://github.com/dariuszpanas/pydantic-versions/issues/13): installed-consumer and prerelease hardening.

Deferred capabilities include independently discriminated nested documents,
mapping-key conversion, arbitrary custom containers, cyclic runtime object
identities, automatic logging/telemetry, OpenAPI migration extensions, generic
file I/O, CLI/code generation, broad framework expansion, Pydantic 3, and
Python 3.15.

No package version, changelog, tag, upload, or release is part of this decision
record.

## Acceptance verification

| Issue #3 requirement | Decision section |
| --- | --- |
| Checked-in architecture record | This ADR and documentation navigation |
| Concrete public signatures and typing | Public declaration API, compatibility API, and typed examples |
| External configuration and two families | Family identity and typed examples |
| Irreversible rendering | Ordered topology, rendering, and upgrade-only example |
| Nested mapping | Nested family graph and mapping examples |
| Wire-model limits and escape hatch | Historical wire-model fidelity |
| Decorator/free-function compatibility | Compatibility API and migration table |
| Trace privacy | Inventory, plans, traces, and trace privacy |
| Deferred scope and non-goals | Product boundary, alternatives, and consequences |
