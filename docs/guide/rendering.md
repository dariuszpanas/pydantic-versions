# Rendering Configs

Use `defaults_for()` to materialize one version's defaults and `dump()` to
convert current data into a requested schema version. The decorator-compatible
`dump_versioned()` function supports both operations.

```python
defaults = APP_CONFIG_SCHEMA.defaults_for(version="1")
dumped = APP_CONFIG_SCHEMA.dump(version="1", data=current_config)
```

By default, output uses the target model's JSON serialization schema, applies
serialization aliases, and includes the configured version field. Pydantic can
define different validation and serialization aliases, so an intentionally
asymmetric model may require a separately aligned consumer contract.

## Render defaults

The following snippets form one executable example:

<!-- pv-doc-test: rendering -->
```python
from pydantic import BaseModel
from pydantic_versions import (
    dump_versioned,
    field_default,
    field_renamed,
    schema_version,
    versioned_schema,
)


@versioned_schema(name="app_config", versions=["1", "2"], current="2")
@schema_version(
    "1",
    patches=[
        field_default("timeout", 5.0),
        field_renamed("retries", "attempts"),
    ],
)
class AppConfig(BaseModel):
    timeout: float = 10.0
    retries: int = 3


assert dump_versioned(AppConfig, version="1") == {
    "timeout": 5.0,
    "attempts": 3,
    "schema_version": "1",
}
```

`SchemaFamily.defaults_for(version=...)` constructs the selected target wire
model from `{}` and serializes it directly. It does not plan or execute a
downgrade from the current version. Historical defaults therefore remain
available even when a custom upgrade has no reverse operation. Target default
factories, default validation, validators on an explicit wire model, and its
serializer retain normal Pydantic behavior and execute once.

For compatibility, `dump(version=..., data=None)` delegates to
`defaults_for()`. An empty mapping is real current input, not defaults syntax;
required current fields still fail validation.

## Render existing data

Current model instances and mappings can be rendered into historical field names:

<!-- pv-doc-test: rendering -->
```python
dumped = dump_versioned(
    AppConfig,
    version="1",
    data=AppConfig(timeout=12.0, retries=5),
)
assert dumped == {
    "timeout": 12.0,
    "attempts": 5,
    "schema_version": "1",
}
```

Supplied `data` always describes the current schema. The `version` argument is
the output version, not the input version. Mappings cross the authoritative
current-model boundary with their original key shape, so Pydantic's current
validators, alias priority, defaults, and constraints apply before any
downgrade. An already-constructed instance of the authoritative model is
handled according to that model's Pydantic `revalidate_instances` policy, so
the default avoids duplicate validation while an opt-in revalidation policy is
still enforced.

Rendering extracts declared fields into a private JSON-shaped payload without
flattening allowed extras, accepting subclass-only fields, or invoking model
and field serializers. Mutations made by a downgrade therefore cannot reach
caller-owned nested mappings or collections.

An unrelated `BaseModel` is accepted only when its declared structure validates
as current input. Otherwise Pydantic raises a validation error for the
authoritative current model. Package-generated current wire instances also
remain renderable, including hashable set projections that the original model
annotation cannot construct directly.

If that set bridge is required, an enclosing model with a custom `__init__`
fails closed with `UnsupportedWireModelError`. Pydantic validators and
`model_post_init` retain their exact model type and once-only lifecycle; a
custom initializer cannot be replayed without re-entering field validation.

Fields removed in the target version are dropped before historical validation.

If current input includes version metadata, it must name the current version.
Rendering rebases that metadata to the requested target. This makes a mapping
such as `{"schema_version": "1", ...}` invalid current input when the current
version is `"2"`, even if the requested output version is `"1"`. Omit the input
metadata when the application model does not own it.

The converted value is validated by the target wire model before target
serialization. An explicit historical model keeps its own validators and
serializer. With family-owned metadata, `model_for(version)` returns a complete
document adapter around that explicit body model. The adapter invokes the body
serializer once, requires object-shaped output, and rejects any attempt by the
body serializer to emit or replace the reserved metadata path. A nested
family-owned metadata path reserves its complete root envelope; application
data cannot share sibling keys under that root.

The document adapter is final. If an application needs a specialized historical
body, subclass the explicit wire body before passing it to `SchemaVersion`
rather than subclassing `model_for(version)`. The body remains the sole owner of
its aliases, validators, serializer, and JSON Schema callbacks. Custom model
core/JSON Schema hooks are rejected because composing them through the adapter
would not have once-only semantics. Annotation or decorator serializers that
can relocate a declared nested-family path are rejected for the same reason,
as are custom model schema hooks and legacy `json_encoders` on that path.

Attribute input is available only when the explicit body sets
`ConfigDict(from_attributes=True)`. The adapter preflights family-owned metadata
before executing body validators. Per-call `from_attributes=True` cannot enable
attribute input for a body that did not declare it; use a mapping or an exact
body instance instead. Allowed extras remain available, but an extra that
overwrites an active field or computed-field serialization name fails closed.
The facade exposes the declared Pydantic state and standard model operations;
it does not proxy arbitrary body methods. Self-references created inside
body-owned values retain the explicit body's identity, so applications must not
use `value is document` as part of the wire contract.

## Serialization options

Rendering defaults to `mode="json"` and `by_alias=True`. The complete-output
API accepts this bounded set of `model_dump()` options:

- `mode`
- `by_alias`
- `context`
- `fallback`
- `warnings`
- `round_trip=False`
- `serialize_as_any=False`
- `polymorphic_serialization=False` or `None`

Truthy polymorphic options are rejected because subclass-only fields can escape
the declared target contract. `round_trip=True` is rejected because Pydantic may
omit computed target fields in that mode. Unknown options fail closed so a new
Pydantic keyword cannot silently weaken the versioned contract.

The following omission options are rejected whenever they are present, even if
their value would currently be false or empty:

- `include`
- `exclude`
- `exclude_unset`
- `exclude_defaults`
- `exclude_none`
- `exclude_computed_fields`

Use the returned complete dictionary as the versioned document, then derive a
partial view outside this API when an application deliberately needs one.

## Omit version metadata

Set `include_version=False` when family-owned version metadata is stored outside
the rendered payload:

<!-- pv-doc-test: rendering -->
```python
dumped = dump_versioned(AppConfig, version="1", include_version=False)
assert dumped == {"timeout": 5.0, "attempts": 3}
```

This is an explicit body-only mode. Its result is not a complete document for
the family-owned `model_for(version)` adapter, so the consumer must supply the
version separately. Model-owned metadata is part of the body contract and
cannot be omitted; requesting `include_version=False` for that ownership mode
raises `ValueError` before target construction or serialization.

## Nested version metadata

Nested version fields are rendered into the requested path:

<!-- pv-doc-test: rendering -->
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

For a declared nested family, the parent version mapping owns child selection.
Its embedded family-owned discriminator is therefore omitted from the rendered
child value. A child discriminator declared as model-owned remains a real body
field and is retained and verified.
