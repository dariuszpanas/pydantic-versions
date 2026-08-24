# Application-to-Application Exchange

`pydantic-versions` can sit at the boundary between applications that share a
configuration contract but deploy different software releases. The schema
version describes the exchanged document; it is independent from either
application's package version.

Consider a producer that understands schema versions `1`, `2`, and `3`, while a
consumer still accepts only `1` and `2`. The producer can validate old input
into its current model and render version `2` for the consumer. The applications
exchange serialized data, not Python models or schema-family objects.

## Give the shared contract an identity

Both applications need an agreed protocol identity and immutable version
labels:

```python
PIPELINE_CONFIG_SCHEMA = "com.example.pipeline-config"
```

`SchemaFamily.name` can carry that value, but the library does not register or
resolve names globally. The applications establish the identity through their
own API, deployment configuration, message metadata, or file convention.

Once published, a label such as `2` must keep the same fields, aliases,
constraints, and defaults. An incompatible contract change receives a new
schema label.

## Declare independent application models

The producer owns its current application model and history:

```python
from pydantic import BaseModel
from pydantic_versions import (
    SchemaFamily,
    SchemaVersion,
    field_default,
    field_removed,
    field_renamed,
)


class ProducerConfig(BaseModel):
    endpoint: str
    timeout: int = 30
    retry_limit: int = 3
    telemetry: bool = True


PRODUCER_SCHEMA = SchemaFamily(
    model=ProducerConfig,
    name="com.example.pipeline-config",
    versions=(
        SchemaVersion(
            "1",
            patches=(
                field_default("timeout", 5),
                field_renamed("retry_limit", "attempts"),
                field_removed("telemetry"),
            ),
        ),
        SchemaVersion(
            "2",
            patches=(
                field_default("timeout", 10),
                field_renamed("retry_limit", "retries"),
            ),
        ),
        SchemaVersion("3"),
    ),
)
```

The consumer declares the same shared wire versions against its own model. It
does not import `ProducerConfig` or `PRODUCER_SCHEMA`:

```python
class ConsumerConfig(BaseModel):
    endpoint: str
    timeout: int = 10
    retries: int = 3
    telemetry: bool = True


CONSUMER_SCHEMA = SchemaFamily(
    model=ConsumerConfig,
    name="com.example.pipeline-config",
    versions=(
        SchemaVersion(
            "1",
            patches=(
                field_default("timeout", 5),
                field_renamed("retries", "attempts"),
                field_removed("telemetry"),
            ),
        ),
        SchemaVersion("2"),
    ),
)
```

The declarations for each shared label must describe the same wire contract,
even though the applications' current Python field names can differ.

Serialization and validation aliases can also differ in Pydantic. Versioned
output defaults to `by_alias=True`, so a producer emits each target field's
serialization alias (or ordinary alias). The consumer must accept that exact
location as a field name, alias, or `AliasChoices` entry. Do not assume the
producer's validation-only alias is its output name. Use `by_alias=False` only
when both sides deliberately share Python field names, and cover that mode with
an exchange test.

## Advertise accepted versions

The consumer can publish a small application-owned capability document:

```json
{
  "schema": "com.example.pipeline-config",
  "accepts": ["1", "2"]
}
```

This is deliberately not a `pydantic-versions` transport API. It can be an HTTP
response, message header, sidecar file, command-line setting, or deployment
contract.

## Select an exact common version

The producer already has the primitives needed to select a target. Declared
versions give its preference order, and `plan_render()` reports whether a
target is exact, lossy, or unavailable:

```python
from collections.abc import Iterable

from pydantic_versions import IrreversibleTransitionError, SchemaFamily


class NoCompatibleSchemaVersionError(RuntimeError):
    pass


def select_target_version(
    producer: SchemaFamily,
    accepted: Iterable[str],
    *,
    allow_lossy: bool = False,
) -> str:
    accepted_labels = set(accepted)
    for declaration in reversed(producer.versions):
        if declaration.label not in accepted_labels:
            continue
        try:
            plan = producer.plan_render(declaration.label)
        except IrreversibleTransitionError:
            continue
        if plan.semantics == "exact" or allow_lossy:
            return declaration.label
    raise NoCompatibleSchemaVersionError
```

For the example above, version `2` is the newest exact common version. Version
`1` is lossy because it omits `telemetry`. A deployment may permit that loss,
but it must opt in rather than discovering it after data has been discarded.

The same decision includes declared nested families. If a child schema loses a
field on the selected route, the parent plan is lossy and the selector above
requires `allow_lossy=True`. If a child route has no required downgrade,
`plan_render()` rejects the parent route and the selector continues searching.
This preflight is payload-independent, so an optional child or an empty child
collection cannot make an otherwise unavailable contract safe.

Version labels are opaque strings. “Newest” here means the producer's declared
order, not lexical or numeric sorting.

## Normalize before sending

Do not forward the original dictionary directly. Validate it into the
producer's authoritative current model, then render the selected target:

```python
historical_input = {
    "schema_version": "1",
    "endpoint": "https://worker.example",
    "attempts": 4,
}

normalized = PRODUCER_SCHEMA.validate(historical_input).current_model
target = select_target_version(PRODUCER_SCHEMA, ("1", "2"))
outgoing = PRODUCER_SCHEMA.dump(version=target, data=normalized)
```

The outgoing version `2` document contains the effective historical timeout of
`5`, the `retries` wire name, and the current value of `telemetry`. Validation
followed by rendering makes defaults explicit under the selected contract
instead of letting two applications independently guess what an omitted value
means.

The consumer reads only serialized data:

```python
received = CONSUMER_SCHEMA.validate(outgoing)
config = received.current_model

assert config.timeout == 5
assert config.retries == 4
assert config.telemetry is True
```

## Carry version metadata in one place

The default document includes `schema_version`. If the transport already owns
that metadata, render without it and supply the selected version explicitly at
the consumer boundary:

```python
payload = PRODUCER_SCHEMA.dump(
    version=target,
    data=normalized,
    include_version=False,
)
received = CONSUMER_SCHEMA.validate(payload, version=target)
```

When both transport and document metadata are present, they must agree. The
library validates the selected wire contract rather than silently replacing a
conflicting embedded label.

Transport-owned metadata requires family-owned version metadata. A
model-owned discriminator is a declared body field and cannot be removed with
`include_version=False`.

## Fail when there is no safe overlap

A producer must not guess when the applications share no usable version.
Return a compatibility error, stop the deployment, or require an explicit
upgrade. Do not substitute the producer's current version or use
`missing_version` as negotiation fallback.

`missing_version` is only for a known legacy document population whose absent
metadata has one deliberate meaning.

## Decide who owns unknown fields

Version conversion does not define a multi-writer merge protocol. A consumer
that ignores unknown fields can destroy another application's data if it reads,
modifies, and rewrites a larger shared document.

Choose an explicit ownership model:

- one application owns the complete configuration and others consume it;
- each application owns a namespaced section;
- every partial consumer uses and tests suitable Pydantic extra-field behavior;
  or
- a separate envelope preserves opaque application extensions.

Do not claim transparent relay behavior without a round-trip test for the exact
models and Pydantic configuration in use.

`extra="allow"` on a wire model retains unknown values on the returned
`source_model` for inspection, but those extras are not migration input and are
not promoted into the authoritative current model. Family conversion extracts
declared fields recursively so omitted application state cannot be restored by
an alias-shaped extra. Use a declared namespaced extension mapping or a separate
envelope when opaque values must cross that boundary.

## Useful operating modes

The same primitives support several deployments:

- **File import:** a new application validates and upgrades an old user file.
- **Compatibility output:** a new producer renders a format accepted by an old
  application.
- **Translator:** a gateway validates any supported input and renders a chosen
  target version.
- **Fan-out:** one current model is rendered separately for consumers with
  different supported windows.
- **Rolling deployment:** old and new instances exchange the newest exact
  version shared during the rollout.

The integration suite exercises the independent producer/consumer file exchange
as executable documentation. Capability advertisement and negotiation remain
application-owned; this workflow requires no library transport API.
