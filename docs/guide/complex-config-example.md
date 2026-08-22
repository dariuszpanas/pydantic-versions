# Complex Config Example

This page uses a larger config to show why schema versioning matters. The
example is intentionally config-shaped: nested Pydantic models, lists, defaults,
renamed fields, and version metadata stored outside the core model.

## A Plain Pydantic Config

Imagine a deployment tool that reads YAML and validates it with Pydantic:

<!-- pv-doc-test: complex-plain -->
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

`pydantic-versions` keeps that compatibility policy beside the ordinary current
models instead.

## Define The Current Models

Define each current model exactly once. The rest of this page is one executable
sequence, so every family and transition is registered before validation first
compiles it.

<!-- pv-doc-test: complex-versioned -->
```python
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic_versions import (
    NestedFamily,
    SchemaFamily,
    SchemaVersion,
    VersionMetadata,
    VersionTransition,
    field_default,
    field_renamed,
    matching_labels,
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


class PipelineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    workers: list[WorkerConfig]
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
```

## Declare The Nested History

The retry policy owns its history. Version 1 used `attempts` and a one-second
default; version 2 uses the current names and defaults.

<!-- pv-doc-test: complex-versioned -->
```python
RETRY_SCHEMA = SchemaFamily(
    model=RetryPolicy,
    name="retry_policy",
    versions=(
        SchemaVersion(
            "1",
            patches=(
                field_renamed("max_attempts", "attempts"),
                field_default("backoff_seconds", 1.0),
            ),
        ),
        SchemaVersion("2"),
    ),
)

assert RETRY_SCHEMA.defaults_for(version="1") == {
    "attempts": 3,
    "backoff_seconds": 1.0,
    "schema_version": "1",
}
```

The parent explicitly connects `workers[*].retry` to that family. The
`matching_labels()` mapping selects retry v1 for pipeline v1 and retry v2 for
pipeline v2. Direct and optional children and homogeneous lists, tuples, sets,
and frozen sets are supported; mapping values and heterogeneous branches are
rejected because runtime conversion cannot dispatch them unambiguously.

## Register Value Transitions Before First Use

Patches handle field-level compatibility. Use a transition for value-level or
shape-level changes that need code. Here, disabled telemetry is normalized to
the `"none"` exporter. The downgrade is declared at the same time so historical
rendering remains available; it is lossy because the normalization cannot
recover a prior non-`"none"` exporter.

<!-- pv-doc-test: complex-versioned -->
```python
def upgrade_pipeline_v1(data: dict) -> dict:
    observability = data.setdefault("observability", {})
    if observability.get("enabled") is False:
        observability["exporter"] = "none"
    return data


def downgrade_pipeline_v2(data: dict) -> dict:
    return data


PIPELINE_SCHEMA = SchemaFamily(
    model=PipelineConfig,
    name="pipeline_config",
    versions=(
        SchemaVersion(
            "1",
            patches=(
                field_renamed("name", "service_name"),
                field_renamed("observability", "telemetry"),
            ),
        ),
        SchemaVersion("2"),
    ),
    transitions=(
        VersionTransition(
            "1",
            "2",
            upgrade=upgrade_pipeline_v1,
            downgrade=downgrade_pipeline_v2,
            downgrade_semantics="lossy",
        ),
    ),
    nested=(
        NestedFamily(
            ("workers", "retry"),
            RETRY_SCHEMA,
            matching_labels(),
        ),
    ),
)
```

Construct the complete graph before calling `validate()`, `model_for()`,
`describe()`, or a planning method. Those operations compile the family;
declarations cannot be added afterward.

## Validate Historical Input

The old field names and the nested v1 retry values now reach the current
models:

<!-- pv-doc-test: complex-versioned -->
```python
historical_pipeline = {
    "schema_version": "1",
    "service_name": "invoice-ingest",
    "workers": [
        {
            "name": "parser",
            "image": "ghcr.io/example/parser:1.0",
            "replicas": 2,
            "retry": {"attempts": 5},
        }
    ],
    "telemetry": {"enabled": False, "exporter": "otlp"},
}

result = PIPELINE_SCHEMA.validate(historical_pipeline)

assert result.source_version == "1"
assert result.current_version == "2"
assert result.current_model.name == "invoice-ingest"
assert result.current_model.observability.enabled is False
assert result.current_model.observability.exporter == "none"
assert result.current_model.workers[0].retry == RetryPolicy(
    max_attempts=5,
    backoff_seconds=1.0,
)
```

## Version Metadata Outside The Model

Some formats keep version metadata in wrapper fields. Kubernetes-style
resources often use `apiVersion`; other configs may store version information
under `metadata`. Wrapper families can connect their `spec` field to the
pipeline family with an explicit label mapping.

<!-- pv-doc-test: complex-versioned -->
```python
class PipelineResource(BaseModel):
    kind: Literal["Pipeline"]
    spec: PipelineConfig


PIPELINE_RESOURCE_SCHEMA = SchemaFamily(
    model=PipelineResource,
    name="pipeline_crd",
    versions=(
        SchemaVersion("example.com/v1"),
        SchemaVersion("example.com/v2"),
    ),
    nested=(
        NestedFamily(
            "spec",
            PIPELINE_SCHEMA,
            {
                "example.com/v1": "1",
                "example.com/v2": "2",
            },
        ),
    ),
    version_metadata=VersionMetadata("apiVersion"),
)


class PipelineDocument(BaseModel):
    spec: PipelineConfig


PIPELINE_DOCUMENT_SCHEMA = SchemaFamily(
    model=PipelineDocument,
    name="pipeline_document",
    versions=(SchemaVersion("1"), SchemaVersion("2")),
    nested=(NestedFamily("spec", PIPELINE_SCHEMA, matching_labels()),),
    version_metadata=VersionMetadata(("metadata", "schema_version")),
)

resource = PIPELINE_RESOURCE_SCHEMA.model_for("example.com/v1").model_validate(
    {
        "apiVersion": "example.com/v1",
        "kind": "Pipeline",
        "spec": historical_pipeline,
    }
)
document = PIPELINE_DOCUMENT_SCHEMA.model_for("1").model_validate(
    {
        "metadata": {"schema_version": "1"},
        "spec": historical_pipeline,
    }
)

assert resource.model_dump()["apiVersion"] == "example.com/v1"
assert document.model_dump()["metadata"] == {"schema_version": "1"}
```

For nested metadata paths, the wrapper does not have to be part of the
authoritative application model. The generated wire model validates the
complete document, and family-owned metadata is removed from the private
transition value before final application-model validation.

## Render Older Configs

The paired downgrade makes the historical route available. Rendering applies
the top-level projection and the explicitly connected retry v1 wire contract.
The parent mapping owns the embedded child selection, so the retry payload does
not repeat its standalone `schema_version` discriminator:

<!-- pv-doc-test: complex-versioned -->
```python
v1_example = PIPELINE_SCHEMA.dump(
    version="1",
    data=PipelineConfig(
        name="invoice-ingest",
        workers=[
            WorkerConfig(
                name="parser",
                image="ghcr.io/example/parser:2.0",
                retry=RetryPolicy(
                    max_attempts=3,
                    backoff_seconds=1.0,
                ),
            )
        ],
        observability=ObservabilityConfig(enabled=False),
    ),
)

assert v1_example["service_name"] == "invoice-ingest"
assert v1_example["telemetry"] == {"enabled": False, "exporter": "none"}
assert "name" not in v1_example
assert v1_example["workers"][0]["retry"] == {
    "attempts": 3,
    "backoff_seconds": 1.0,
}
```

This makes the compatibility contract explicit: the latest software can keep
validating and rendering old config schemas without pretending that schema
versions and software versions are the same thing.
