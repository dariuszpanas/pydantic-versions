# Complex Config Example

This page uses a larger config to show why schema versioning matters. The
example is intentionally config-shaped: nested Pydantic models, lists, defaults,
renamed fields, removed fields, and version metadata stored outside the core
model.

## A Plain Pydantic Config

Imagine a deployment tool that reads YAML and validates it with Pydantic:

```python
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ResourceLimits(BaseModel):
    cpu: str = "500m"
    memory: str = "512Mi"


class RetryPolicy(BaseModel):
    attempts: int = 3
    backoff_seconds: float = 1.0


class WorkerConfig(BaseModel):
    name: str
    image: str
    replicas: int = 1
    resources: ResourceLimits = Field(default_factory=ResourceLimits)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)


class TelemetryConfig(BaseModel):
    enabled: bool = True
    exporter: Literal["none", "otlp", "prometheus"] = "none"


class PipelineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_name: str
    workers: list[WorkerConfig]
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
```

A user might have this config checked into Git:

```yaml
schema_version: "1"
service_name: invoice-ingest
workers:
  - name: parser
    image: ghcr.io/example/parser:1.0
    replicas: 2
    retry:
      attempts: 5
telemetry:
  enabled: false
```

This works until the application evolves.

## Why Plain Model Changes Are Fragile

Now imagine the software changes:

- `service_name` is renamed to `name`;
- `retry.attempts` is renamed to `retry.max_attempts`;
- default retry backoff changes from `1.0` to `2.0`;
- telemetry is moved under a new `observability` field;
- each worker gets a new `queue` field with default `"default"`.

If you simply edit the Pydantic models, the old YAML may fail validation or load
with subtly different behavior. That creates a bad operational choice:

- pin the old software version forever for old configs;
- update every config immediately during a software rollout;
- add one-off compatibility code around each loader.

`pydantic-versions` is meant to keep that compatibility policy next to the model
definition instead.

## A Versioned Current Model

The decorated model is the current schema. Historical versions are generated
from it with patches:

```python
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic_versions import (
    field_default,
    field_removed,
    field_renamed,
    migration,
    schema_version,
    validate_versioned,
    versioned_schema,
)


class ResourceLimits(BaseModel):
    cpu: str = "500m"
    memory: str = "512Mi"


class RetryPolicy(BaseModel):
    max_attempts: int = 3
    backoff_seconds: float = 2.0


class WorkerConfig(BaseModel):
    name: str
    image: str
    replicas: int = 1
    queue: str = "default"
    resources: ResourceLimits = Field(default_factory=ResourceLimits)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)


class ObservabilityConfig(BaseModel):
    enabled: bool = True
    exporter: Literal["none", "otlp", "prometheus"] = "none"


@versioned_schema(
    name="pipeline_config",
    versions=["1", "2"],
    current="2",
    version_field="schema_version",
)
@schema_version(
    "1",
    patches=[
        field_renamed("name", "service_name"),
        field_renamed("observability", "telemetry"),
    ],
)
class PipelineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    workers: list[WorkerConfig]
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
```

The top-level v1 field names are now understood:

```python
result = validate_versioned(
    PipelineConfig,
    {
        "schema_version": "1",
        "service_name": "invoice-ingest",
        "workers": [
            {
                "name": "parser",
                "image": "ghcr.io/example/parser:1.0",
                "replicas": 2,
            }
        ],
        "telemetry": {"enabled": False},
    },
)

assert result.source_version == "1"
assert result.current_version == "2"
assert result.current_model.name == "invoice-ingest"
assert result.current_model.observability.enabled is False
```

## Nested Schema Changes

Nested models can be versioned too. If `RetryPolicy` changed between schema
versions, decorate it separately:

```python
@versioned_schema(name="retry_policy", versions=["1", "2"], current="2")
@schema_version(
    "1",
    patches=[
        field_renamed("max_attempts", "attempts"),
        field_default("backoff_seconds", 1.0),
    ],
)
class RetryPolicy(BaseModel):
    max_attempts: int = 3
    backoff_seconds: float = 2.0
```

Then any versioned model that references `RetryPolicy` can generate a matching
historical nested schema for `list[WorkerConfig]`, `dict[str, RetryPolicy]`,
tuples, sets, and optional nested model fields where applicable.

## When Patches Are Not Enough

Patches handle field-level compatibility. Use migrations for value-level or
shape-level changes that need code.

In this example, v1 had a `telemetry.enabled: false` value. In v2, the product
decides that disabled telemetry should also force the exporter to `"none"`:

```python
@migration(PipelineConfig, "1", "2")
def migrate_pipeline_v1_to_v2(data: dict) -> dict:
    observability = data.setdefault("observability", {})
    if observability.get("enabled") is False:
        observability["exporter"] = "none"
    return data
```

Migration functions receive dictionaries using current field names. They run
after source-version validation and historical rename mapping, then the result
is validated again with the current model.

## Version Metadata Outside The Model

Some formats keep version metadata in wrapper fields. Kubernetes-style resources
often use `apiVersion`; other configs may store version information under
`metadata`.

Use a top-level custom field:

```python
@versioned_schema(
    name="pipeline_crd",
    versions=["example.com/v1", "example.com/v2"],
    current="example.com/v2",
    version_field="apiVersion",
)
class PipelineResource(BaseModel):
    kind: Literal["Pipeline"]
    spec: PipelineConfig
```

Or a nested path:

```python
@versioned_schema(
    name="pipeline_document",
    versions=["1", "2"],
    current="2",
    version_field=("metadata", "schema_version"),
)
class PipelineDocument(BaseModel):
    spec: PipelineConfig
```

For nested paths, the metadata wrapper does not have to be part of the Pydantic
model. The version field is read before validation and removed before the source
model is validated.

## Rendering Older Configs

You can render the old shape for examples, generated starter configs, or
compatibility output:

```python
from pydantic_versions import dump_versioned


v1_example = dump_versioned(
    PipelineConfig,
    version="1",
    data=PipelineConfig(
        name="invoice-ingest",
        workers=[
            WorkerConfig(
                name="parser",
                image="ghcr.io/example/parser:2.0",
                retry=RetryPolicy(max_attempts=5),
            )
        ],
    ),
)

assert "service_name" in v1_example
assert "telemetry" in v1_example
assert "name" not in v1_example
```

This makes the compatibility contract explicit: the latest software can keep
validating and rendering old config schemas without pretending that schema
versions and software versions are the same thing.
