# Django Ninja

Django Ninja schemas are Pydantic models under the hood, so
`pydantic-versions` can work with `ninja.Schema` classes for request and
response payload compatibility.

This is an integration target, not a runtime dependency for the core package.
Django Ninja is useful when API schema versions need to be decoupled from
application releases in the same way config schema versions are.

The compatibility tests cover both schema-level behavior and a minimal real
`NinjaAPI`:

- generated historical schemas work as request body types;
- current generated schemas work as request body types;
- generated historical schemas work as response body types;
- route handlers can upgrade historical payloads into the current schema;
- generated historical models appear in the OpenAPI schema with historical field names.
- `ModelSchema` classes preserve Django-derived field metadata such as string length constraints.

## Versioned Ninja Schemas

```python
from ninja import Schema
from pydantic_versions import (
    field_default,
    field_renamed,
    schema_version,
    validate_versioned,
    versioned_schema,
)


@versioned_schema(name="task_payload", versions=["v1", "v2"], current="v2")
@schema_version(
    "v1",
    patches=[
        field_default("timeout", 5.0),
        field_renamed("completed", "is_completed"),
    ],
)
class TaskPayload(Schema):
    title: str
    completed: bool = False
    timeout: float = 10.0
```

Historical input validates and upgrades into the current Ninja schema:

```python
result = validate_versioned(
    TaskPayload,
    {"schema_version": "v1", "title": "Import", "is_completed": True},
)

assert result.current_model == TaskPayload(
    title="Import",
    completed=True,
    timeout=5.0,
)
```

## Generated Wire Schemas

`model_for_version()` returns an object-shaped Pydantic wire contract that
Django Ninja can inspect for JSON Schema/OpenAPI generation:

```python
from pydantic_versions import model_for_version


TaskPayloadV1 = model_for_version(TaskPayload, "v1")
schema = TaskPayloadV1.model_json_schema()

assert "is_completed" in schema["properties"]
assert "completed" not in schema["properties"]
```

The generated model preserves supported fields, constraints, defaults, and
aliases, but it is not a behavioral subclass of `TaskPayload`. Model validators,
methods, computed fields, and private attributes are not copied. Family
validation still finishes with the authoritative current `TaskPayload` model.
See [generated wire contracts](generated-wire-contracts.md) for the supported
boundary.

This supports explicit route-level schemas:

```python
@api.post("/v1/tasks")
def create_task_v1(request, payload: TaskPayloadV1):
    result = validate_versioned(TaskPayload, payload.model_dump(), version="v1")
    task = result.current_model
    ...
```

## Aliases

Django Ninja supports Pydantic aliases and additional dotted aliases for response
schemas. Version renames intentionally take precedence for generated historical
models:

```python
from ninja import Field, Schema


@versioned_schema(name="task_alias", versions=["v1", "v2"], current="v2")
@schema_version("v1", patches=[field_renamed("done", "completed")])
class TaskAlias(Schema):
    title: str
    done: bool = Field(False, alias="is_done")
```

The current schema still exposes `is_done`, while the generated v1 schema exposes
`completed`.

## ModelSchema

`ninja.ModelSchema` is also supported for the tested request-schema path:

```python
from django.db import models
from ninja import ModelSchema


class Task(models.Model):
    title = models.CharField(max_length=100)
    is_done = models.BooleanField(default=False)
    timeout = models.FloatField(default=10.0)


@versioned_schema(name="task_model", versions=["v1", "v2"], current="v2")
@schema_version(
    "v1",
    patches=[
        field_default("timeout", 5.0),
        field_renamed("is_done", "completed"),
    ],
)
class TaskPayload(ModelSchema):
    class Meta:
        model = Task
        fields = ["title", "is_done", "timeout"]
```

The generated v1 schema exposes `completed`, keeps the historical timeout
default, and preserves the `maxLength` constraint derived from the Django model's
`CharField`. A model that uses an unsupported automatic-projection feature
raises `UnsupportedWireModelError` during family compilation instead of
producing a misleading OpenAPI schema.

## What To Test In Applications

For Django Ninja projects, add application-level tests for:

- generated historical request schemas used in route signatures;
- OpenAPI output for every public API version;
- aliases and dotted aliases on response schemas;
- `ModelSchema` or `create_schema()` generated classes before decorating them;
- validation from historical request payloads into current application models.

The current library tests cover plain `ninja.Schema` compatibility, generated
historical model JSON Schema, aliases, version rename precedence, minimal route
handling, `ModelSchema`, and OpenAPI request-body output. Larger applications
should still test their real routers, auth, response schemas, `create_schema()`
usage, and generated client contracts.
