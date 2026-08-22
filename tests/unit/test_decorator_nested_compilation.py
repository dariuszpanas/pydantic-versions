from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Generic, TypedDict, TypeVar

import pytest
from pydantic import BaseModel, ConfigDict, Field, RootModel, create_model, model_serializer

from pydantic_versions import (
    IrreversibleTransitionError,
    NestedFamily,
    SchemaCompilationError,
    SchemaFamily,
    SchemaVersion,
    UnsupportedWireModelError,
    VersionTransition,
    dump_versioned,
    field_renamed,
    matching_labels,
    model_for_version,
    schema_version,
    versioned_schema,
)


def _decorated_family(model: type[BaseModel]) -> SchemaFamily[Any]:
    # The decorator's model-oriented public API deliberately hides its default family;
    # unit-level planning assertions need the exact family that backs those public calls.
    from pydantic_versions.family import _default_family_for_model

    family = _default_family_for_model(model)
    assert family is not None
    return family


def test_exact_explicit_nested_sibling_does_not_suppress_decorator_route() -> None:
    @versioned_schema(
        name="compilation_sibling_decorator_child_77",
        versions=("1", "2"),
        current="2",
    )
    @schema_version("1", patches=(field_renamed("value", "legacy_value"),))
    class CompilationSiblingDecoratorChild77(BaseModel):
        value: int

    class CompilationSiblingExplicitChild77(BaseModel):
        value: int

    explicit_child = SchemaFamily(
        model=CompilationSiblingExplicitChild77,
        name="compilation_sibling_explicit_child_77",
        versions=(
            SchemaVersion(
                "1",
                patches=(field_renamed("value", "legacy_explicit_value"),),
            ),
            SchemaVersion("2"),
        ),
    )

    class CompilationSiblingWrapper77(BaseModel):
        decorator_child: CompilationSiblingDecoratorChild77
        explicit_child: CompilationSiblingExplicitChild77

    @versioned_schema(
        name="compilation_sibling_parent_77",
        versions=("1", "2"),
        current="2",
        nested=(
            NestedFamily(
                ("wrapper", "explicit_child"),
                explicit_child,
                matching_labels(),
            ),
        ),
    )
    class CompilationSiblingParent77(BaseModel):
        wrapper: CompilationSiblingWrapper77

    model_for_version(CompilationSiblingParent77, "1")
    family = _decorated_family(CompilationSiblingParent77)

    assert tuple((nested.schema_path, nested.family) for nested in family.describe().nested) == (
        ("$.wrapper.explicit_child", "compilation_sibling_explicit_child_77"),
    )

    plan = family.plan_validation("1")
    repeated = family.plan_validation("1")
    step_ids = tuple(step.id for step in plan.steps)
    assert repeated is plan
    assert tuple(step.id for step in repeated.steps) == step_ids
    assert len(set(step_ids)) == len(step_ids)

    decorator_index = next(
        index
        for index, step in enumerate(plan.steps)
        if step.kind == "nested" and step.schema_path == "$.wrapper.decorator_child"
    )
    parent_index = next(
        index
        for index, step in enumerate(plan.steps)
        if step.schema_path == "$"
        and step.source_version == "1"
        and step.target_version == "2"
        and step.kind == "implicit_identity"
    )
    assert decorator_index < parent_index
    decorator_step = plan.steps[decorator_index]
    assert decorator_step.conditional is True
    assert decorator_step.semantics == "not_applicable"
    render_step = next(
        step
        for step in family.plan_render("1").steps
        if step.kind == "nested" and step.schema_path == "$.wrapper.decorator_child"
    )
    assert render_step.conditional is True
    assert render_step.semantics == "exact"

    rendered = dump_versioned(
        CompilationSiblingParent77,
        version="1",
        data=CompilationSiblingParent77(
            wrapper=CompilationSiblingWrapper77(
                decorator_child=CompilationSiblingDecoratorChild77(value=1),
                explicit_child=CompilationSiblingExplicitChild77(value=2),
            )
        ),
    )
    assert rendered["wrapper"]["decorator_child"]["legacy_value"] == 1
    assert rendered["wrapper"]["explicit_child"]["legacy_explicit_value"] == 2


def test_builtin_string_key_dict_discovers_decorator_child() -> None:
    @versioned_schema(
        name="compilation_dict_child_77",
        versions=("1", "2"),
        current="2",
    )
    class CompilationDictChild77(BaseModel):
        value: int

    @versioned_schema(
        name="compilation_dict_parent_77",
        versions=("1", "2"),
        current="2",
    )
    class CompilationDictParent77(BaseModel):
        children: dict[str, CompilationDictChild77]

    model_for_version(CompilationDictParent77, "1")
    plan = _decorated_family(CompilationDictParent77).plan_validation("1")

    assert any(step.kind == "nested" and step.schema_path == "$.children" for step in plan.steps)


@pytest.mark.parametrize(
    ("case", "annotation_factory", "message"),
    (
        (
            "mapping",
            lambda child: Mapping[str, child],
            "unsupported abstract container collections.abc.Mapping",
        ),
        (
            "sequence",
            lambda child: Sequence[child],
            "unsupported abstract container collections.abc.Sequence",
        ),
        (
            "non_string_dict",
            lambda child: dict[int, child],
            "requires exact string mapping keys",
        ),
    ),
)
def test_unsupported_decorator_child_containers_fail_closed(
    case: str,
    annotation_factory: Any,
    message: str,
) -> None:
    @versioned_schema(
        name=f"compilation_unsupported_{case}_child_77",
        versions=("1", "2"),
        current="2",
    )
    class CompilationUnsupportedContainerChild77(BaseModel):
        value: int

    annotation = annotation_factory(CompilationUnsupportedContainerChild77)
    parent = create_model(
        f"CompilationUnsupported{case.title().replace('_', '')}Parent77",
        children=(annotation, ...),
    )
    parent = versioned_schema(
        name=f"compilation_unsupported_{case}_parent_77",
        versions=("1", "2"),
        current="2",
    )(parent)

    with pytest.raises(SchemaCompilationError, match=message) as error:
        model_for_version(parent, "1")

    assert f"compilation_unsupported_{case}_parent_77" in str(error.value)
    assert "('children',)" in str(error.value)


def test_non_isomorphic_union_topology_fails_closed() -> None:
    @versioned_schema(
        name="compilation_union_topology_child_77",
        versions=("1", "2"),
        current="2",
    )
    class CompilationUnionTopologyChild77(BaseModel):
        value: int

    class CompilationUnionDirectWrapper77(BaseModel):
        child: CompilationUnionTopologyChild77

    class CompilationUnionListWrapper77(BaseModel):
        child: list[CompilationUnionTopologyChild77]

    @versioned_schema(
        name="compilation_union_topology_parent_77",
        versions=("1", "2"),
        current="2",
    )
    class CompilationUnionTopologyParent77(BaseModel):
        payload: CompilationUnionDirectWrapper77 | CompilationUnionListWrapper77

    with pytest.raises(
        SchemaCompilationError,
        match="non-isomorphic traversal shapes",
    ) as error:
        model_for_version(CompilationUnionTopologyParent77, "1")

    assert "compilation_union_topology_parent_77" in str(error.value)
    assert "('payload',)" in str(error.value)


def test_recursive_wrapper_containing_decorator_child_fails_closed() -> None:
    @versioned_schema(
        name="compilation_recursive_child_77",
        versions=("1", "2"),
        current="2",
    )
    class CompilationRecursiveChild77(BaseModel):
        value: int

    class CompilationRecursiveWrapper77(BaseModel):
        child: CompilationRecursiveChild77 | None = None
        next_wrapper: CompilationRecursiveWrapper77 | None = None

    CompilationRecursiveWrapper77.model_rebuild(
        _types_namespace={
            "CompilationRecursiveChild77": CompilationRecursiveChild77,
            "CompilationRecursiveWrapper77": CompilationRecursiveWrapper77,
        },
    )

    @versioned_schema(
        name="compilation_recursive_parent_77",
        versions=("1", "2"),
        current="2",
    )
    class CompilationRecursiveParent77(BaseModel):
        wrapper: CompilationRecursiveWrapper77

    with pytest.raises(
        SchemaCompilationError,
        match="decorator child beneath recursive model path",
    ) as error:
        model_for_version(CompilationRecursiveParent77, "1")

    assert "compilation_recursive_parent_77" in str(error.value)
    assert "next_wrapper" in str(error.value)


def test_explicit_descendant_beneath_decorator_child_fails_closed() -> None:
    class CompilationExplicitDescendantLeaf77(BaseModel):
        value: int

    leaf_family = SchemaFamily(
        model=CompilationExplicitDescendantLeaf77,
        name="compilation_explicit_descendant_leaf_77",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    )

    @versioned_schema(
        name="compilation_explicit_descendant_child_77",
        versions=("1", "2"),
        current="2",
    )
    class CompilationExplicitDescendantChild77(BaseModel):
        leaf: CompilationExplicitDescendantLeaf77

    @versioned_schema(
        name="compilation_explicit_descendant_parent_77",
        versions=("1", "2"),
        current="2",
        nested=(
            NestedFamily(
                ("child", "leaf"),
                leaf_family,
                matching_labels(),
            ),
        ),
    )
    class CompilationExplicitDescendantParent77(BaseModel):
        child: CompilationExplicitDescendantChild77

    with pytest.raises(
        SchemaCompilationError,
        match="descends beneath decorator child boundary",
    ) as error:
        model_for_version(CompilationExplicitDescendantParent77, "1")

    assert "compilation_explicit_descendant_parent_77" in str(error.value)
    assert "('child', 'leaf')" in str(error.value)


def test_unavailable_decorator_child_downgrade_names_the_child_family() -> None:
    @versioned_schema(
        name="compilation_unavailable_child_77",
        versions=("1", "2"),
        current="2",
        transitions=(VersionTransition("1", "2", upgrade=lambda data: data),),
    )
    class CompilationUnavailableChild77(BaseModel):
        value: int

    @versioned_schema(
        name="compilation_unavailable_parent_77",
        versions=("1", "2"),
        current="2",
    )
    class CompilationUnavailableParent77(BaseModel):
        child: CompilationUnavailableChild77

    with pytest.raises(IrreversibleTransitionError) as error:
        dump_versioned(CompilationUnavailableParent77, version="1")

    message = str(error.value)
    assert "compilation_unavailable_child_77" in message
    assert "$.child" in message
    assert "has no complete route '2' -> '1'" in message


def test_runtime_indistinguishable_container_union_arms_fail_compilation() -> None:
    @versioned_schema(
        name="ambiguous_container_union_a_77",
        versions=("1", "2"),
        current="2",
    )
    class A(BaseModel):
        value: str

    @versioned_schema(
        name="ambiguous_container_union_b_77",
        versions=("1", "2"),
        current="2",
    )
    class B(BaseModel):
        value: str

    @versioned_schema(
        name="ambiguous_container_union_parent_77",
        versions=("1", "2"),
        current="2",
    )
    class Parent(BaseModel):
        items: list[A] | list[B]

    with pytest.raises(
        UnsupportedWireModelError,
        match="runtime-indistinguishable container arms",
    ):
        model_for_version(Parent, "1")


def test_abstract_and_concrete_container_union_arms_fail_compilation() -> None:
    @versioned_schema(
        name="overlapping_abstract_child_77",
        versions=("1", "2"),
        current="2",
    )
    class Child(BaseModel):
        value: int

    @versioned_schema(
        name="overlapping_mapping_parent_77",
        versions=("1", "2"),
        current="2",
    )
    class MappingParent(BaseModel):
        payload: Mapping[str, int] | dict[str, Child]

    @versioned_schema(
        name="overlapping_sequence_parent_77",
        versions=("1", "2"),
        current="2",
    )
    class SequenceParent(BaseModel):
        payload: Sequence[int] | list[Child]

    @versioned_schema(
        name="overlapping_raw_parent_77",
        versions=("1", "2"),
        current="2",
    )
    class RawParent(BaseModel):
        payload: list | list[Child]

    @versioned_schema(
        name="overlapping_object_parent_77",
        versions=("1", "2"),
        current="2",
    )
    class ObjectParent(BaseModel):
        payload: object | list[Child]

    for parent in (MappingParent, SequenceParent, RawParent, ObjectParent):
        with pytest.raises(
            UnsupportedWireModelError,
            match="runtime-indistinguishable container arms",
        ):
            model_for_version(parent, "1")


def test_typed_dict_union_arm_fails_closed_without_builtin_type_error() -> None:
    @versioned_schema(name="typed_dict_union_child_77", versions=("1", "2"), current="2")
    class Child(BaseModel):
        value: int

    class Payload(TypedDict):
        other: int

    @versioned_schema(name="typed_dict_union_parent_77", versions=("1", "2"), current="2")
    class Parent(BaseModel):
        payload: Child | Payload

    with pytest.raises(UnsupportedWireModelError, match="TypedDict arm"):
        model_for_version(Parent, "1")


def test_unparameterized_generic_wrapper_with_decorator_bound_fails_closed() -> None:
    @versioned_schema(
        name="generic_bound_child_77",
        versions=("1", "2"),
        current="2",
    )
    class Child(BaseModel):
        value: int

    child_type = TypeVar("child_type", bound=Child)

    class Box(BaseModel, Generic[child_type]):
        item: child_type

    @versioned_schema(
        name="generic_bound_parent_77",
        versions=("1", "2"),
        current="2",
    )
    class Parent(BaseModel):
        box: Box

    with pytest.raises(
        UnsupportedWireModelError,
        match="unresolved generic parameters",
    ):
        model_for_version(Parent, "1")


def test_specialized_generic_wrapper_discovers_decorator_child() -> None:
    @versioned_schema(
        name="specialized_generic_child_77",
        versions=("1", "2"),
        current="2",
    )
    @schema_version("1", patches=(field_renamed("value", "legacy_value"),))
    class Child(BaseModel):
        value: int

    child_type = TypeVar("child_type")

    class Box(BaseModel, Generic[child_type]):
        item: child_type

    @versioned_schema(
        name="specialized_generic_parent_77",
        versions=("1", "2"),
        current="2",
    )
    class Parent(BaseModel):
        box: Box[Child]

    assert dump_versioned(
        Parent,
        version="1",
        data=Parent(box=Box[Child](item=Child(value=4))),
    ) == {
        "box": {"item": {"legacy_value": 4, "schema_version": "1"}},
        "schema_version": "1",
    }


def test_explicit_parent_rejects_child_with_remaining_decorator_routes() -> None:
    @versioned_schema(name="explicit_auto_grand_77", versions=("1", "2"), current="2")
    class Grand(BaseModel):
        value: int

    @versioned_schema(name="explicit_auto_child_77", versions=("1", "2"), current="2")
    class Child(BaseModel):
        grand: Grand

    @versioned_schema(
        name="explicit_auto_parent_77",
        versions=("1", "2"),
        current="2",
        nested=(NestedFamily("child", _decorated_family(Child), matching_labels()),),
    )
    class Parent(BaseModel):
        child: Child

    with pytest.raises(
        UnsupportedWireModelError,
        match="still contains decorator-discovered nested routes",
    ):
        model_for_version(Parent, "1")


def test_incomplete_ordinary_wrapper_fails_closed() -> None:
    class Wrapper(BaseModel):
        child: "Later"  # noqa: UP037

    @versioned_schema(name="late_wrapper_child_77", versions=("1", "2"), current="2")
    class Later(BaseModel):
        value: int

    @versioned_schema(name="late_wrapper_parent_77", versions=("1", "2"), current="2")
    class Parent(BaseModel):
        wrapper: Wrapper

    with pytest.raises(UnsupportedWireModelError, match=r"model_rebuild\(\)"):
        model_for_version(Parent, "1")


def test_unsafe_ordinary_wrapper_models_fail_closed() -> None:
    @versioned_schema(name="unsafe_wrapper_child_77", versions=("1", "2"), current="2")
    class Child(BaseModel):
        value: int

    class TypedExtraWrapper(BaseModel):
        model_config = ConfigDict(extra="allow")

        __pydantic_extra__: dict[str, int] = Field(init=False)
        child: Child

    class RootWrapper(RootModel[Child]):
        pass

    class SerializedWrapper(BaseModel):
        child: Child

        @model_serializer
        def serialize(self) -> dict[str, Any]:
            return {"child": self.child}

    wrappers = (TypedExtraWrapper, RootWrapper, SerializedWrapper)
    matches = ("typed extra", "RootModel", "model-level serializer")
    for ordinal, (wrapper, match) in enumerate(zip(wrappers, matches, strict=True)):
        parent = create_model(
            f"UnsafeWrapperParent{ordinal}",
            __module__=__name__,
            wrapper=(wrapper, ...),
        )
        decorated = versioned_schema(
            name=f"unsafe_wrapper_parent_{ordinal}_77",
            versions=("1", "2"),
            current="2",
        )(parent)
        with pytest.raises(UnsupportedWireModelError, match=match):
            model_for_version(decorated, "1")


def test_unrelated_unsafe_wrappers_remain_supported_without_projection() -> None:
    class RootPayload(RootModel[int]):
        pass

    class SerializedPayload(BaseModel):
        value: int

        @model_serializer
        def serialize(self) -> dict[str, int]:
            return {"serialized": self.value}

    @versioned_schema(
        name="unrelated_unsafe_wrapper_parent_77",
        versions=("1", "2"),
        current="2",
    )
    class Parent(BaseModel):
        rooted: RootPayload
        serialized: SerializedPayload

    historical = model_for_version(Parent, "1")

    assert historical.model_fields["rooted"].annotation is RootPayload
    assert historical.model_fields["serialized"].annotation is SerializedPayload


def test_optional_wrapper_siblings_compare_metadata_contracts_per_site() -> None:
    @versioned_schema(name="mixed_metadata_family_child_77", versions=("1", "2"), current="2")
    class FamilyChild(BaseModel):
        value: int

    @versioned_schema(name="mixed_metadata_model_child_77", versions=("1", "2"), current="2")
    class ModelChild(BaseModel):
        schema_version: str = "2"
        value: int

    class Wrapper(BaseModel):
        family_child: FamilyChild
        model_child: ModelChild

    @versioned_schema(name="mixed_metadata_parent_77", versions=("1", "2"), current="2")
    class Parent(BaseModel):
        wrapper: Wrapper | None

    rendered = dump_versioned(
        Parent,
        version="1",
        data=Parent(
            wrapper=Wrapper(
                family_child=FamilyChild(value=1),
                model_child=ModelChild(value=2),
            )
        ),
    )
    assert rendered["wrapper"] == {
        "family_child": {"value": 1, "schema_version": "1"},
        "model_child": {"schema_version": "1", "value": 2},
    }


def _deterministic_decorator_family() -> SchemaFamily[Any]:
    @versioned_schema(
        name="deterministic_decorator_child_77",
        versions=("1", "2"),
        current="2",
    )
    class DeterministicDecoratorChild77(BaseModel):
        value: int

    @versioned_schema(
        name="deterministic_decorator_parent_77",
        versions=("1", "2"),
        current="2",
    )
    class DeterministicDecoratorParent77(BaseModel):
        children: list[dict[str, DeterministicDecoratorChild77]]

    return _decorated_family(DeterministicDecoratorParent77)


def test_decorator_plan_json_is_deterministic_across_processes() -> None:
    repository = Path(__file__).resolve().parents[2]
    family = _deterministic_decorator_family()
    local = json.dumps(
        {
            "validation": family.plan_validation("1").to_dict(),
            "render": family.plan_render("1").to_dict(),
        },
        separators=(",", ":"),
    )
    script = (
        "import json\n"
        "from tests.unit.test_decorator_nested_compilation import "
        "_deterministic_decorator_family\n"
        "family = _deterministic_decorator_family()\n"
        "print(json.dumps({"
        "'validation': family.plan_validation('1').to_dict(),"
        "'render': family.plan_render('1').to_dict(),"
        "}, separators=(',', ':')))\n"
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == local
