"""Public compatibility regressions from the 1.0.3 runtime optimizations."""

from __future__ import annotations

import gc
import typing
import weakref
from typing import Annotated, Any, Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field, create_model

from pydantic_versions import (
    NestedFamily,
    SchemaFamily,
    SchemaVersion,
    VersionMetadata,
    VersionTransition,
)

_AnnotatedNumber = Annotated[int, {"description": "unhashable annotation metadata"}]


@pytest.mark.parametrize("operation", ["validate", "dump"])
@pytest.mark.parametrize("kind", ["list", "dict", "tuple", "set"])
def test_collections_support_unhashable_annotation_metadata(kind: str, operation: str) -> None:
    annotations: dict[str, Any] = {
        "list": list[_AnnotatedNumber],
        "dict": dict[str, _AnnotatedNumber],
        "tuple": tuple[_AnnotatedNumber, ...],
        "set": set[_AnnotatedNumber],
    }
    values: dict[str, Any] = {
        "list": [1, 2],
        "dict": {"a": 1},
        "tuple": (1, 2),
        "set": {1, 2},
    }
    model = create_model("AnnotatedCollection", value=(annotations[kind], ...))
    family = SchemaFamily(
        model=model,
        name="annotated_collection",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )
    value = values[kind]
    if operation == "validate":
        result = family.validate({"value": value}, version="1")
        assert result.current_model.model_dump()["value"] == value
    else:
        result = family.dump(version="1", data=model.model_validate({"value": value}))
        expected = {"a": 1} if kind == "dict" else [1, 2]
        assert result["value"] == expected


@pytest.mark.parametrize("owner", ["family", "model"])
@pytest.mark.parametrize("callback_location", ["parent", "child"])
def test_nested_callbacks_preserve_canonical_metadata(
    owner: Literal["family", "model"], callback_location: str
) -> None:
    fields: dict[str, Any] = {"value": (int, 1)}
    if owner == "model":
        fields["schema_version"] = (str, "2")
    child_model = create_model("MetadataChild", **fields)
    seen: list[dict[str, Any]] = []

    def inspect_child(payload: dict[str, Any]) -> dict[str, Any]:
        seen.append(dict(payload))
        return payload

    child = SchemaFamily(
        model=child_model,
        name="metadata_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        version_metadata=VersionMetadata("schema_version", owner=owner),
        transitions=(VersionTransition("1", "2", upgrade=inspect_child),)
        if callback_location == "child"
        else (),
    )
    parent_model = create_model("MetadataParent", child=(child_model, ...), decision=(str, ""))

    def parent_upgrade(payload: dict[str, Any]) -> dict[str, Any]:
        inspect_child(payload["child"])
        payload["decision"] = str(payload["child"].get("schema_version", "absent"))
        return payload

    family = SchemaFamily(
        model=parent_model,
        name="metadata_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2"), SchemaVersion("3")),
        nested=(NestedFamily("child", child, {"1": "1", "2": "1", "3": "2"}),),
        transitions=(VersionTransition("1", "2", upgrade=parent_upgrade),)
        if callback_location == "parent"
        else (),
    )
    result = family.validate({"schema_version": "1", "child": {"schema_version": "1", "value": 3}})
    expected: dict[str, Any] = {"value": 3}
    if owner == "model":
        expected["schema_version"] = "2"
    assert seen == [expected]
    if callback_location == "parent":
        assert result.current_model.model_dump()["decision"] == (
            "2" if owner == "model" else "absent"
        )


def test_nested_upgrade_can_revalidate_a_payload_that_forbids_extra_fields() -> None:
    class Child(BaseModel):
        model_config = ConfigDict(extra="forbid")
        value: int

    def upgrade(payload: dict[str, Any]) -> dict[str, Any]:
        return Child.model_validate(payload).model_dump()

    child = SchemaFamily(
        model=Child,
        name="strict_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(VersionTransition("1", "2", upgrade=upgrade),),
    )

    class Parent(BaseModel):
        child: Child

    parent = SchemaFamily(
        model=Parent,
        name="strict_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        nested=(NestedFamily("child", child, {"1": "1", "2": "2"}),),
    )
    result = parent.validate({"schema_version": "1", "child": {"value": 7}})
    assert result.current_model == Parent(child=Child(value=7))


def _temporary_recursive_model(
    index: int, *, use_family: bool, operation: str
) -> weakref.ReferenceType[type[BaseModel]]:
    class Node(BaseModel):
        value: int = 0
        children: list[Node] = Field(default_factory=list)

    Node.model_rebuild()
    payload = {"value": index, "children": [{"value": index + 1}]}
    if use_family:
        family = SchemaFamily(
            model=Node,
            name=f"temporary_recursive_{index}",
            versions=(SchemaVersion("1"),),
            version_metadata=None,
        )
        if operation == "validate":
            family.validate(payload, version="1")
        else:
            family.dump(version="1", data=Node.model_validate(payload))
    elif operation == "validate":
        Node.model_validate(payload)
    else:
        Node.model_validate(payload).model_dump()
    return weakref.ref(Node)


@pytest.mark.parametrize("operation", ["validate", "dump"])
def test_extraction_plans_do_not_add_recursive_model_retention(operation: str) -> None:
    controls = [
        _temporary_recursive_model(index, use_family=False, operation=operation)
        for index in range(16)
    ]
    references = [
        _temporary_recursive_model(index, use_family=True, operation=operation)
        for index in range(16)
    ]
    # Family compilation also uses typing aliases. Clear their bounded caches
    # so the comparison isolates persistent model retention, not recent aliases.
    for cleanup in getattr(typing, "_cleanups", ()):
        cleanup()
    gc.collect()

    # Older supported Pydantic releases themselves retain recursive models.
    # Compare against native Pydantic so this tests retention added by our cache,
    # rather than imposing newer pydantic-core collection behavior on the floor.
    assert sum(reference() is not None for reference in references) <= sum(
        reference() is not None for reference in controls
    )


def test_subclass_extraction_plan_preserves_its_additional_fields() -> None:
    class Parent(BaseModel):
        value: int = 1

    parent = SchemaFamily(
        model=Parent, name="plan_parent", versions=(SchemaVersion("1"),), version_metadata=None
    )
    parent.dump(version="1", data=Parent())

    class Child(Parent):
        label: str = "child"

    family = SchemaFamily(
        model=Child, name="plan_child", versions=(SchemaVersion("1"),), version_metadata=None
    )
    before = Child.model_json_schema()
    assert family.dump(version="1", data=Child()) == {"value": 1, "label": "child"}
    assert Child.model_json_schema() == before
