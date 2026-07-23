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
| Rejected | Serialization exclusions such as `exclude`/`exclude_if`, callable discriminators, and unknown behavior-changing model or field settings | The automatic compiler fails closed instead of silently changing the wire document. |
| Rejected at registration | Pydantic v1 compatibility models | The family raises `SchemaVersionError` before automatic projection; wire models must inherit from Pydantic v2's `BaseModel`. |

Historical patches are applied to this preserved declaration state. A removed
field is absent, a renamed field uses its historical Python name, and a default
patch replaces the projected field's required/default/factory state.

Zero-argument factories remain safe when the field annotation is unchanged. A
decorator-owned child annotation can replace the child class itself as a
factory, and a direct child instance is projected without running its
serializers. Opaque factories for a projected child are rejected because they
could construct the authoritative child and run its behavior on historical
input. Any factory that consumes already validated field data is also rejected:
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

## Current-Model Validation

The current application model remains authoritative. Its validators, methods,
and other application behavior can run during final current-model validation
without being copied into a historical or current wire projection.

Directly validating a generated model exercises only that wire contract and
raises Pydantic's native `ValidationError` on invalid input. Use the family
validation API when the desired result is an instance of the authoritative
current model.

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
model objects, while separate family identities do not share them.
