# Generated Wire Contracts

`SchemaFamily.model_for()` and `model_for_version()` return Pydantic v2 models
for declared schema versions. These generated models describe the data on the
wire: they are suitable for direct Pydantic validation and serialization, JSON
Schema generation, and framework inspection.

Generated current and historical models are object-shaped wire contracts. They
are not behavioral subclasses or complete copies of the authoritative current
application model.

## What Generation Preserves

The compiler preserves declarations that define the supported wire shape and
Pydantic's declarative handling of that shape:

| Result | Current-model declaration | Generated wire contract |
| --- | --- | --- |
| Preserved | Field annotations, `Annotated` metadata, `Field` constraints, and JSON-serializable static field schema metadata | The generated field keeps the supported validation and JSON Schema contract. Pydantic's declarative built-in annotation types remain supported. |
| Preserved | Required fields, direct defaults, and zero-argument default factories on unchanged annotations | Each version keeps its projected required/default/factory state. Compilation does not call default factories. |
| Preserved | `alias`, `validation_alias`, `serialization_alias`, and alias generators | Unrenamed fields keep supported validation and serialization aliases. A historical rename defines a new Python field name instead of attaching the current field's explicit aliases to that different name. |
| Preserved | Declarative model configuration | `extra`, `strict`, `populate_by_name`, `validate_by_alias`, `validate_by_name`, `serialize_by_alias`, `loc_by_alias`, `use_enum_values`, alias generators, supported string/number/temporal/bytes settings, a static title, and non-structural mapping-based JSON Schema metadata remain part of the wire contract. |
| Omitted | Field and model validators, and field serializers | They remain behavior of the authoritative current model and are not copied onto generated models. Constraints carried by field annotations and metadata are still preserved. |
| Omitted | Computed fields, private attributes, methods, and `model_post_init` | These application behaviors are not part of the wire contract. |
| Omitted | Lifecycle-only configuration such as assignment validation and frozen instances | Generated models describe documents rather than application-object lifecycle behavior. |
| Rejected | `RootModel`, an incomplete or unresolved generic model, or another non-object validation or serialization shape | Compilation raises `UnsupportedWireModelError`; resolve and rebuild an incomplete model, or wrap a root or scalar value in a named object field. |
| Rejected | A model-level serializer, application-defined annotation or model schema hooks, behavioral dataclasses, callable schema/title mutation, non-JSON schema metadata, model schema metadata that replaces generated structure, legacy `json_encoders`, or arbitrary-type escape hatches | Automatic projection cannot safely reproduce that custom behavior and raises `UnsupportedWireModelError`. |
| Omitted | Server-internal fields marked with `Field(exclude=True)` or `Field(exclude_if=...)` | The field is absent from every generated wire projection; it remains available on the authoritative application model. |
| Rejected | Callable discriminators and unknown behavior-changing model or field settings | The automatic compiler fails closed instead of silently changing the wire document. |
| Rejected at registration | Pydantic v1 compatibility models | The family raises `SchemaVersionError` before automatic projection; wire models must inherit from Pydantic v2's `BaseModel`. |
| Rejected | A `NestedFamily` path through a mapping, a heterogeneous union or collection, or a field owned by a different model than the declared child family | Runtime conversion cannot dispatch those shapes unambiguously, so compilation raises `UnsupportedWireModelError`. |

Historical patches are applied to this preserved declaration state. A removed
field is absent, a renamed field uses its historical Python name, and a default
patch replaces the projected field's required/default/factory state.

Explicit `NestedFamily` declarations support direct and optional child models,
plus homogeneous `list`, `tuple`, `set`, and `frozenset` boundaries. The same
boundaries can appear while traversing a multi-segment path. Mapping values and
heterogeneous branches are intentionally rejected during compilation rather
than accepted with payload-dependent behavior.

The `@versioned_schema` compatibility decorator also discovers child models
that have their own decorator-created family with the exact same labels. These
implicit boundaries are separate from explicit `NestedFamily` declarations:
they do not appear in `SchemaInventory.nested`, but conversion plans include
conditional child-before-parent steps for them. Discovery traverses ordinary
Pydantic model wrappers, `Annotated` and optional annotations, general or
discriminated unions whose selected arm remains recoverable, built-in `list`,
`tuple`, `set`, and `frozenset` containers, fixed tuple positions, and
`dict[str, value]`, including nested combinations of those shapes. An explicit
declaration suppresses discovery at
that exact leaf only, so an ordinary wrapper can contain an explicitly mapped
child and a decorator-discovered sibling.

A decorator-discovered child may itself use explicit `NestedFamily`
declarations. The inverse composition fails closed: an explicit
`NestedFamily` child may not retain implicit decorator-discovered descendants.
Declare those descendant routes explicitly on the child with
`versioned_schema(..., nested=...)` so the complete conversion boundary is
visible to the compiler.

Union selection comes from the already validated parent model and is carried
through conversion without validating the child a second time. A parent
transition may reorder surviving child mappings within one dispatch site
because their identities move with them. Moving or swapping an existing child
identity across fields or dispatch sites is rejected. Dictionary keys are
stable occurrence anchors when no dynamic collection occurs above or below
that mapping boundary. An exact current
child-model instance may deliberately replace an occurrence and establish a
new branch. If a transition loses, duplicates, reuses, or replaces overlapping
union occurrences with untyped copied mappings, conversion raises
`InvalidMigrationError` before the next transition runs. Set and frozenset
cardinality is checked again after target-wire validation.

Decorator discovery fails closed when it encounters an abstract or custom
container, a mapping whose declared or runtime key is not exactly `str`, a
child hidden in a type alias or unresolved generic, an unsafe recursive
wrapper, non-isomorphic union traversal shapes, or runtime-unrecoverable union
arms.
Examples of unrecoverable arms include `list[A] | list[B]`, an abstract
`Mapping` competing with `dict[str, Child]`, and a `TypedDict` competing with a
decorator child. Incompatible union metadata contracts and an explicit path
below a decorator-owned child boundary are also rejected.
Different parent and child labels still require an explicit
`NestedFamily` mapping. An explicit historical parent wire model must likewise
declare these child boundaries explicitly; the compiler does not partially
rewrite a user-supplied wire class.

An ordinary wrapper that must be projected for a decorator child must be a
complete, resolved, object-shaped Pydantic model. Rebuild forward references
with `model_rebuild()` before compilation. Projected wrappers may not be a
`RootModel` or carry typed extras, model-level serializers, unsafe
configuration, or custom schema hooks that automatic generation would
otherwise weaken or discard. Those models remain supported as unchanged field
annotations when no decorator route requires their projection.

Compilation snapshots the authoritative model's Pydantic core schema. A
subsequent forced `model_rebuild()` invalidates that family, even when the
rebuilt schema is semantically equivalent, because its generated projections
and conversion plans still describe the original schema object. Runtime family
operations then raise `SchemaCompilationError`. Dependent parent families are
invalidated transitively because their wire models embed the child projection.
Discard the affected family graph and recreate its declarations from fully
rebuilt models. An ordinary no-op `model_rebuild()` remains safe.
Do not race a forced rebuild with compilation or runtime family operations.
For decorator-managed default families, redeclare the affected model classes
and reinitialize the application, or switch callers to a newly constructed
explicit `SchemaFamily` graph.

An excluded field is also absent from the generated wire model. This is the
supported way to keep server-internal state on the authoritative model while
using that model as the source for a document contract. The exclusion is not
copied as a Pydantic serialization option because doing so would leave the
field present during wire validation. Conditional exclusions are treated the
same way: the field is omitted unconditionally from generated projections.
The configured model-owned version field may not be excluded.

`extra="allow"` remains a source wire-validation policy. The returned
`source_model` retains untyped extras in `__pydantic_extra__`, but family
conversion does not flatten those values into migration input. The private
canonical mapping is built recursively from declared, validated Python fields
without invoking source serializers. Consequently, an excluded or historically
removed field cannot re-enter through its Python name, an alias, `AliasChoices`,
an `AliasPath`, or an automatically projected nested model. A user upgrade may
still introduce that canonical field deliberately. Model opaque extensions as a
declared mapping or envelope when they must cross the conversion boundary.

Zero-argument factories remain safe when the field annotation is unchanged. A
decorator-owned child annotation can replace the child class itself as a
factory, and a direct child instance is projected without running its
serializers. Projected direct-child, container, and ordinary-wrapper defaults
are rebuilt from declared fields only; extras and subclass-only state never
cross the wire through a default. Opaque factories for a projected route are
rejected because they could construct authoritative models and run their
behavior on historical input. Any factory that consumes already validated
field data is also rejected:
automatic wire models intentionally omit current-model validators, so copying
its materialized result could prevent the authoritative current factory from
seeing the final values. Typed `__pydantic_extra__` values and schema or runtime
behavior hidden inside either standard-library or `typing_extensions` type
aliases are likewise rejected instead of being weakened or executed. Untyped
`extra` behavior and JSON-serializable static schema metadata declared directly
on a field remain supported.

## Version Discriminators

When a family declares version metadata, the generated document contract uses
the exact version label rather than an unrestricted string:

- With family-owned metadata, `model_for()` returns a complete document adapter
  for every version, including the current version. The discriminator at the
  configured path has annotation `Literal[label]` and default `label`.
- With a supported validation-capable direct model-owned metadata field or
  alias, every automatically
  generated version, including the current version, is a distinct document
  projection. Its declared metadata field has annotation `Literal[label]` and
  default `label`. Output-only serialization aliases and validation locations
  disabled by model configuration are rejected. The field or direct alias must
  resolve unambiguously and keep one invariant location; nested model-owned
  paths are not projected by this top-level wire compiler. The exact label
  replaces other validation constraints on the generated discriminator while
  keeping its aliases and descriptive field metadata; the authoritative current
  field still validates the current label at the final application boundary.
- With `version_metadata=None`, generation does not add a discriminator.

This is a statement about the generated Pydantic and JSON Schema shape.
Conversion-time version discovery, conflict handling, and metadata mutation are
separate runtime concerns.

Content discriminators are separate from schema-version metadata. A supported
string discriminator declared on a field remains part of the generated wire
contract, and its discriminator mapping and literal branch values are preserved
across version projections. Callable discriminators remain unsupported because
their runtime selection behavior cannot be reproduced safely by automatic
projection.

## Current-Model Validation

The current application model remains authoritative. Its validators, methods,
and other application behavior can run during final current-model validation
without being copied into a historical or current wire projection.

Directly validating a generated model exercises only that wire contract and
raises Pydantic's native `ValidationError` on invalid input. Use the family
validation API when the desired result is an instance of the authoritative
current model.

Field and model validators follow the same boundary. They are intentionally not
copied to generated wire models, so a `mode="before"` validator may coerce or
reshape input for the authoritative model while the generated JSON Schema
describes the post-coercion wire shape. Consumers should validate against the
generated model for the document contract and use the family API when they need
the authoritative model's runtime behavior.

Family validation has two deliberate stages: it validates the source document
against the generated wire model first, then runs the authoritative current
model after migrations. Consequently, raw input accepted only by an omitted
validator is not automatically accepted as a wire document. Once the payload
has the generated wire shape, authoritative validators can still normalize it
or enforce application invariants at the final boundary.

The boundary applies consistently to every Pydantic validator mode:

| Validator behavior | Authoritative model | Generated wire model and schema |
| --- | --- | --- |
| `mode="before"` | May coerce or reshape raw input before annotation validation once the wire document has passed. | Accepts the declared annotation shape only; the schema does not advertise raw values accepted only by the validator. |
| `mode="after"` | May normalize or enforce invariants after annotation validation. | Keeps the declarative annotation validation but does not run the normalization or invariant check. |
| `mode="plain"` | Replaces Pydantic's normal annotation validation at the authoritative boundary. | Retains the annotation's generated wire schema and core validation; inputs accepted only by the plain validator are not wire inputs. |
| `mode="wrap"` | May preprocess input, delegate to core validation, or replace its result after wire validation. | Does not copy the wrapper; direct validation follows the generated annotation and constraints. |

Model-level `before`, `after`, and `wrap` validators follow the same rule. They
are authoritative application behavior, not generated wire behavior. Field and
model serializers are also omitted: a generated `model_dump()` represents the
wire contract, while serialization of the authoritative model may still apply
the application's serializer. `model_post_init` is omitted for the same reason.

For example, a `mode="before"` validator that turns `"1,2"` into `[1, 2]`
does not make the generated model accept the string, and `SchemaFamily.validate`
also rejects it at the source wire boundary. A wire-shaped `[1, 2]` payload can
then be normalized by the authoritative validator before the final current
model is returned. This keeps framework/OpenAPI consumers on the wire contract
while preserving application behavior at the final boundary.

## Unsupported Models

`UnsupportedWireModelError` is a `SchemaCompilationError`. It is raised during
family compilation when automatic generation cannot guarantee an object-shaped
wire contract. The error identifies the family, model, and unsupported reason,
plus the version when a failure is projection-specific, without rendering
payloads, defaults, or callable representations.

If Pydantic reports the underlying generation failure, that exception remains
available as the chained cause.

## Stable Generated Identities

Generated classes have deterministic, collision-resistant names. The readable
prefix contains sanitized current-model, family, and version-label components.
The suffix is the first 12 hexadecimal characters of SHA-256 over
length-prefixed UTF-8 values for the model module and qualified name, exact
family name, and exact label.

Consequently, labels whose readable forms sanitize to the same text, such as
`1.0` and `1-0`, still receive distinct Python class names and JSON Schema
components. Repeated compilation of one family reuses its cached generated
model objects, while separate family identities do not share them. Temporary
families do not enter a process-global generated-model cache, so their private
set-element wrappers can be collected with the family that owns them.
