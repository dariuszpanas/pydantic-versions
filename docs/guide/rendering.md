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
still enforced. Revalidation receives a detached copy of the complete
top-level instance state, including excluded declared values and allowed
extras, while nested model and dataclass instances retain their native
Pydantic instance semantics. Only the later transition projection omits those
private values. A model whose canonical bridge would bypass an overridden
`model_validate` or wrapped `__pydantic_validator__` fails closed.

### Transition payload during rendering

Downgrades use the shared [transition payload
contract](migrations.md#transition-payload-contract). Rendering extracts
declared fields into a private Python-value canonical
mapping without flattening allowed extras, accepting subclass-only fields, or
invoking model and field serializers. Validated scalars and mapping keys retain
their Python values, and exact built-in `list`, `tuple`, `set`, and `frozenset`
containers retain their kinds. Caller-owned mappings and containers are copied
recursively, so mutations made by a downgrade cannot reach them. Pydantic
converts values to JSON only when the validated target wire model is finally
serialized. Cycles encountered while extracting caller-owned models cannot
form this detached tree and fail through the authoritative validation boundary
instead of recursing; a migration may still deliberately create a cycle for a
native target arm such as `Any`.

### Enums and application validators

With `use_enum_values=True`, callbacks receive the raw value stored by
Pydantic. A direct enum or enum-valued Literal retries its declared member only
after native validation rejects that raw value. Ordinary unions do not repair
individual arms: Pydantic keeps its native arm and validator selection (so
`Mode | str` retains the raw string arm), and canonical validation fails closed
if a successful selection would instead change an erased enum's Python type or
container shape. A selected application-owned `BeforeValidator`,
`PlainValidator`, `AfterValidator`, or `WrapValidator` keeps its native result,
including deliberate nested value or container edits, and executes once.
Failed and unselected application arms cannot authorize another arm's result.
Ordinary Literal coercion remains Pydantic-owned.

### Sets and mapping keys

Private identity-based mapping carriers keep projected models distinct inside
sets and mapping-key positions while callbacks run. Canonical guards reject a
carrier-backed cardinality collapse in a field before-validator or in the
declared set, frozenset, or mapping-key parser; another non-lossy union arm may
still succeed. An application wrap validator cannot swallow that core-loss
invariant, but its output remains native when no guarded parse loses a carrier.
Callback-supplied iterables and coercive scalar collection edits without
private carriers retain Pydantic's native behavior. Dataclass construction and
collection semantics likewise remain Pydantic-owned rather than gaining a
separate canonical reconstruction contract. A carrier that reaches a non-hash
`Any` position becomes an ordinary `dict` before it can enter a public model.
Opaque hash positions such as `set[Any]` and `dict[Any, ...]` fail closed when
only the private carrier could keep a projected mapping hashable.

### Target validation boundaries

An unrelated `BaseModel` is accepted only when its declared structure validates
as current input. Otherwise Pydantic raises a validation error for the
authoritative current model. Package-generated current wire instances also
remain renderable, including hashable set projections that the original model
annotation cannot construct directly.

If that bridge is required, an enclosing model with a custom `__init__`, a
nonstandard `__new__`, an overridden `model_validate`, or a wrapped
`__pydantic_validator__` fails closed with `UnsupportedWireModelError`.
Pydantic validators and `model_post_init` retain their exact model type and
once-only lifecycle; custom construction and validation entry points cannot be
replayed without bypassing or re-entering field validation.

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

Explicit historical bodies may reshape a managed nested-family value only
within the [declared historical annotation
grammar](generated-wire-contracts.md#explicit-historical-nested-shapes). Use a
`TypedDict`, Pydantic model, or structural dataclass when that historical leaf is
mapping-shaped; a broad `dict`, `Mapping`, `Any`, abstract container, or custom
carrier cannot establish static ownership and is rejected during compilation.
Only exact built-in `list`, `tuple`, `set`, and `frozenset` containers with
supported element annotations compose managed values.

A concrete scalar replacement owns whatever JSON shape its normal Pydantic
serialization produces. In particular, an object-shaped enum value is not
reinterpreted as a child document merely because it contains a key named like
the child version field. Conversely, omitting a managed route reserves that
output location; aliases, computed fields, model serializers, and extras cannot
reintroduce it through another channel.

A mapping-valued enum cannot share an overlapping union position with a
structural model, dataclass, or `TypedDict` arm. Pydantic's collection coercion
and smart-union selection make that branch identity unrecoverable, so the
declaration fails compilation.

Current-model `Field(exclude=True)` and `exclude_if` declarations remain
application-only state and are omitted from generated wire projections. An
explicit historical body must instead leave an intentionally absent managed
route undeclared. Declaring the route and attaching a serialization exclusion
is rejected because validation would retain a value that serialization removes
unconditionally or conditionally.

Before explicit source-body validators execute, family-owned metadata is
preflighted through every structurally viable declared arm and its effective
validation aliases. After validation, the selected managed value must still
conform to a declared branch.

After an explicit target model validates data or constructs defaults, the
family verifies that every managed value still conforms to its declared
annotation before pruning child-owned metadata and serializing the target. The
same check descends through generated parent adapters into instantiated
explicit child targets and independently declared model, dataclass, and
TypedDict representations of those children. Any family-owned metadata inside
those structural representations is verified before removal.
Explicit-source validation applies the same check before migration. Validators
may normalize a value while preserving a declared branch, but a field or model
validator that returns an out-of-contract shape fails with a contextual,
payload-free `ValueError`. Pydantic accepting such an after-validator result is
therefore not permission to change the managed wire shape silently.

Attribute input is available only when the explicit body sets
`ConfigDict(from_attributes=True)`. Per-call `from_attributes=True` cannot
enable attribute input for a body that did not declare it; use a mapping or an
exact body instance instead. Allowed extras remain available, but an extra that
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
