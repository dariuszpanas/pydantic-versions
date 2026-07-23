from __future__ import annotations

import json

from django.conf import settings
from django.db import models

if not settings.configured:
    settings.configure(
        SECRET_KEY="test",
        INSTALLED_APPS=[],
        ROOT_URLCONF=__name__,
        ALLOWED_HOSTS=["testserver", "*"],
        NINJA_PAGINATION_CLASS="ninja.pagination.LimitOffsetPagination",
        NINJA_PAGINATION_PER_PAGE=100,
        NINJA_MAX_PER_PAGE_SIZE=100,
        NINJA_PAGINATION_MAX_LIMIT=1000,
        NINJA_NUM_PROXIES=0,
        NINJA_DEFAULT_THROTTLE_RATES={},
        NINJA_FIX_REQUEST_FILES_METHODS={"POST", "PUT", "PATCH"},
    )

import django  # noqa: E402
from django.test import Client
from django.urls import path
from ninja import Field, ModelSchema, NinjaAPI, Schema
from pydantic import BaseModel

django.setup()

from pydantic_versions import (  # noqa: E402
    dump_versioned,
    field_default,
    field_renamed,
    model_for_version,
    schema_version,
    validate_versioned,
    versioned_schema,
)


@versioned_schema(name="ninja_payload", versions=["v1", "v2"], current="v2")
@schema_version(
    "v1",
    patches=[
        field_default("timeout", 5.0),
        field_renamed("completed", "is_completed"),
    ],
)
class NinjaPayload(Schema):
    name: str
    completed: bool = False
    timeout: float = 10.0


def test_ninja_schema_is_supported_as_a_pydantic_model_subclass() -> None:
    assert issubclass(NinjaPayload, BaseModel)

    result = validate_versioned(
        NinjaPayload,
        {"schema_version": "v1", "name": "job", "is_completed": True},
    )

    assert result.current_model == NinjaPayload(name="job", completed=True, timeout=5.0)


def test_generated_historical_model_has_openapi_json_schema_shape() -> None:
    payload_v1 = model_for_version(NinjaPayload, "v1")
    schema = payload_v1.model_json_schema()

    assert schema["properties"]["is_completed"]["default"] is False
    assert schema["properties"]["timeout"]["default"] == 5.0
    assert schema["properties"]["schema_version"]["const"] == "v1"
    assert schema["properties"]["schema_version"]["default"] == "v1"
    assert "completed" not in schema["properties"]


def test_dump_versioned_renders_ninja_schema_historical_aliases() -> None:
    dumped = dump_versioned(
        NinjaPayload,
        version="v1",
        data=NinjaPayload(name="job", completed=True, timeout=7.5),
    )

    assert dumped == {
        "name": "job",
        "is_completed": True,
        "timeout": 7.5,
        "schema_version": "v1",
    }


@versioned_schema(name="ninja_alias_payload", versions=["v1", "v2"], current="v2")
@schema_version("v1", patches=[field_renamed("done", "completed")])
class NinjaAliasPayload(Schema):
    title: str
    done: bool = Field(False, alias="is_done")


def test_ninja_field_alias_metadata_is_preserved_for_current_field_names() -> None:
    payload_v2 = model_for_version(NinjaAliasPayload, "v2")
    schema = payload_v2.model_json_schema()

    assert "is_done" in schema["properties"]
    assert "done" not in schema["properties"]


def test_version_renames_take_precedence_over_ninja_aliases() -> None:
    payload_v1 = model_for_version(NinjaAliasPayload, "v1")
    schema = payload_v1.model_json_schema()

    assert "completed" in schema["properties"]
    assert "is_done" not in schema["properties"]

    result = validate_versioned(
        NinjaAliasPayload,
        {"schema_version": "v1", "title": "Task", "completed": True},
    )
    assert result.current_model.done is True


TaskPayloadV1 = model_for_version(NinjaPayload, "v1")
TaskPayloadV2 = model_for_version(NinjaPayload, "v2")


@versioned_schema(
    name="ninja_openapi_collision",
    versions=["1.0", "1-0", "2"],
    current="2",
)
class NinjaCollisionPayload(Schema):
    value: int


CollisionPayloadDotted = model_for_version(NinjaCollisionPayload, "1.0")
CollisionPayloadDashed = model_for_version(NinjaCollisionPayload, "1-0")

api = NinjaAPI(title="Versioned Test API", urls_namespace="test_ninja_compat")


def _create_task(request, payload, *, version: str):
    result = validate_versioned(NinjaPayload, payload.model_dump(), version=version)
    return {
        "version": result.source_version,
        "name": result.current_model.name,
        "completed": result.current_model.completed,
        "timeout": result.current_model.timeout,
    }


def create_task_v1(request, payload):
    return _create_task(request, payload, version="v1")


def create_task_v2(request, payload):
    return _create_task(request, payload, version="v2")


create_task_v1.__annotations__["payload"] = TaskPayloadV1
create_task_v2.__annotations__["payload"] = TaskPayloadV2
api.post("/v1/tasks")(create_task_v1)
api.post("/v2/tasks")(create_task_v2)


def read_task_v1(request):
    return {
        "schema_version": "v1",
        "name": "job",
        "is_completed": True,
        "timeout": 5.0,
    }


api.get("/v1/task-response", response=TaskPayloadV1)(read_task_v1)


def accept_collision_dotted(request, payload):
    return payload.model_dump()


def accept_collision_dashed(request, payload):
    return payload.model_dump()


accept_collision_dotted.__annotations__["payload"] = CollisionPayloadDotted
accept_collision_dashed.__annotations__["payload"] = CollisionPayloadDashed
api.post("/collision/dotted")(accept_collision_dotted)
api.post("/collision/dashed")(accept_collision_dashed)


def test_generated_historical_schema_works_as_real_ninja_request_body() -> None:
    response = Client().post(
        "/api/v1/tasks",
        data=json.dumps({"name": "job", "is_completed": True}),
        content_type="application/json",
    )

    assert response.status_code == 200
    assert response.json() == {
        "version": "v1",
        "name": "job",
        "completed": True,
        "timeout": 5.0,
    }


def test_generated_current_schema_works_as_real_ninja_request_body() -> None:
    response = Client().post(
        "/api/v2/tasks",
        data=json.dumps({"name": "job", "completed": True, "timeout": 12.0}),
        content_type="application/json",
    )

    assert response.status_code == 200
    assert response.json() == {
        "version": "v2",
        "name": "job",
        "completed": True,
        "timeout": 12.0,
    }


def test_generated_historical_schema_works_as_real_ninja_response_body() -> None:
    response = Client().get("/api/v1/task-response")

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": "v1",
        "name": "job",
        "is_completed": True,
        "timeout": 5.0,
    }

    openapi = api.get_openapi_schema()
    response_schema = openapi["paths"]["/api/v1/task-response"]["get"]["responses"][200]["content"][
        "application/json"
    ]["schema"]
    ref_name = response_schema["$ref"].split("/")[-1]
    payload_schema = openapi["components"]["schemas"][ref_name]
    assert "is_completed" in payload_schema["properties"]
    assert payload_schema["properties"]["schema_version"]["const"] == "v1"


def test_generated_ninja_schema_rejects_a_wrong_discriminator_before_the_handler() -> None:
    response = Client().post(
        "/api/v1/tasks",
        data=json.dumps(
            {
                "schema_version": "v2",
                "name": "job",
                "is_completed": True,
            }
        ),
        content_type="application/json",
    )

    assert response.status_code == 422


def test_ninja_openapi_uses_generated_historical_schema() -> None:
    openapi = api.get_openapi_schema()
    request_schema = openapi["paths"]["/api/v1/tasks"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    ref_name = request_schema["$ref"].split("/")[-1]
    payload_schema = openapi["components"]["schemas"][ref_name]

    assert "is_completed" in payload_schema["properties"]
    assert "completed" not in payload_schema["properties"]
    assert payload_schema["properties"]["timeout"]["default"] == 5.0
    assert payload_schema["properties"]["schema_version"]["const"] == "v1"
    assert payload_schema["properties"]["schema_version"]["default"] == "v1"

    current_request_schema = openapi["paths"]["/api/v2/tasks"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    current_ref_name = current_request_schema["$ref"].split("/")[-1]
    current_payload_schema = openapi["components"]["schemas"][current_ref_name]

    assert current_ref_name != ref_name
    assert current_payload_schema["properties"]["schema_version"]["const"] == "v2"
    assert current_payload_schema["properties"]["schema_version"]["default"] == "v2"


def test_ninja_openapi_keeps_sanitized_label_collisions_in_distinct_components() -> None:
    openapi = api.get_openapi_schema()
    dotted_request = openapi["paths"]["/api/collision/dotted"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    dashed_request = openapi["paths"]["/api/collision/dashed"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    dotted_ref = dotted_request["$ref"].split("/")[-1]
    dashed_ref = dashed_request["$ref"].split("/")[-1]
    components = openapi["components"]["schemas"]

    assert dotted_ref != dashed_ref
    assert components[dotted_ref]["properties"]["schema_version"]["const"] == "1.0"
    assert components[dashed_ref]["properties"]["schema_version"]["const"] == "1-0"


class TaskModel(models.Model):
    title = models.CharField(max_length=100)
    is_done = models.BooleanField(default=False)
    timeout = models.FloatField(default=10.0)

    class Meta:
        app_label = "pydantic_versions_tests"


@versioned_schema(name="ninja_model_payload", versions=["v1", "v2"], current="v2")
@schema_version(
    "v1",
    patches=[
        field_default("timeout", 5.0),
        field_renamed("is_done", "completed"),
    ],
)
class TaskModelPayload(ModelSchema):
    class Meta:
        model = TaskModel
        fields = ["title", "is_done", "timeout"]


def test_ninja_model_schema_is_supported_as_a_pydantic_model_subclass() -> None:
    assert issubclass(TaskModelPayload, BaseModel)

    result = validate_versioned(
        TaskModelPayload,
        {"schema_version": "v1", "title": "Import", "completed": True},
    )

    assert result.current_model.model_dump() == {
        "title": "Import",
        "is_done": True,
        "timeout": 5.0,
    }


def test_generated_model_schema_preserves_django_field_constraints() -> None:
    payload_v1 = model_for_version(TaskModelPayload, "v1")
    schema = payload_v1.model_json_schema()

    assert schema["properties"]["title"]["maxLength"] == 100
    assert schema["properties"]["completed"]["default"] is False
    assert schema["properties"]["timeout"]["default"] == 5.0
    assert "is_done" not in schema["properties"]


TaskModelPayloadV1 = model_for_version(TaskModelPayload, "v1")


def create_model_task_v1(request, payload):
    result = validate_versioned(TaskModelPayload, payload.model_dump(), version="v1")
    current = result.current_model.model_dump()
    return {
        "version": result.source_version,
        "title": current["title"],
        "is_done": current["is_done"],
        "timeout": current["timeout"],
    }


create_model_task_v1.__annotations__["payload"] = TaskModelPayloadV1
api.post("/v1/model-tasks")(create_model_task_v1)


urlpatterns = [path("api/", api.urls)]


def test_generated_model_schema_works_as_real_ninja_request_body() -> None:
    response = Client().post(
        "/api/v1/model-tasks",
        data=json.dumps({"title": "Import", "completed": True}),
        content_type="application/json",
    )

    assert response.status_code == 200
    assert response.json() == {
        "version": "v1",
        "title": "Import",
        "is_done": True,
        "timeout": 5.0,
    }


def test_ninja_openapi_uses_generated_historical_model_schema() -> None:
    openapi = api.get_openapi_schema()
    request_schema = openapi["paths"]["/api/v1/model-tasks"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    ref_name = request_schema["$ref"].split("/")[-1]
    payload_schema = openapi["components"]["schemas"][ref_name]

    assert payload_schema["properties"]["title"]["maxLength"] == 100
    assert "completed" in payload_schema["properties"]
    assert "is_done" not in payload_schema["properties"]
