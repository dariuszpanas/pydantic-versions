from __future__ import annotations

import gc
import weakref
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from types import GenericAlias
from typing import Any, cast, get_args

import pytest
from pydantic import BaseModel, create_model

import pydantic_versions._runtime_render as runtime_render
import pydantic_versions.family as family_module
from pydantic_versions import NestedFamily, SchemaCompilationError, SchemaFamily, SchemaVersion

type _ModelRef = weakref.ReferenceType[type[BaseModel]]


def _set_element_model(model: type[BaseModel], field_name: str) -> type[BaseModel]:
    arguments = get_args(model.model_fields[field_name].annotation)
    assert len(arguments) == 1
    element = arguments[0]
    assert isinstance(element, type) and issubclass(element, BaseModel)
    return element


def test_hashable_wire_models_are_reused_only_within_one_family() -> None:
    class Child(BaseModel):
        value: int

    child_family = SchemaFamily(
        model=Child,
        name="cache_child",
        versions=(SchemaVersion("stable"),),
        version_metadata=None,
    )
    labels = {"old": "stable", "current": "stable"}

    class Parent(BaseModel):
        children: set[Child]
        frozen_children: frozenset[Child]

    parent_family = SchemaFamily(
        model=Parent,
        name="cache_parent",
        versions=(SchemaVersion("old"), SchemaVersion("current")),
        nested=(
            NestedFamily("children", child_family, labels),
            NestedFamily("frozen_children", child_family, labels),
        ),
        version_metadata=None,
    )
    occurrences = tuple(
        _set_element_model(parent_family.model_for(version), field_name)
        for version in ("old", "current")
        for field_name in ("children", "frozen_children")
    )

    assert len({id(model) for model in occurrences}) == 1

    class OtherParent(BaseModel):
        children: set[Child]

    other_family = SchemaFamily(
        model=OtherParent,
        name="other_cache_parent",
        versions=(SchemaVersion("old"), SchemaVersion("current")),
        nested=(NestedFamily("children", child_family, labels),),
        version_metadata=None,
    )

    assert _set_element_model(other_family.model_for("old"), "children") is not occurrences[0]


def _discarded_hashable_model_refs(
    start: int,
    count: int,
) -> tuple[tuple[_ModelRef, _ModelRef], ...]:
    references = []
    for index in range(start, start + count):
        child_model = create_model(
            f"LifecycleChild{index}",
            __module__=__name__,
            value=(int, ...),
        )
        child_family = SchemaFamily(
            model=child_model,
            name=f"lifecycle_child_{index}",
            versions=(SchemaVersion("stable"),),
            version_metadata=None,
        )
        children_annotation: Any = GenericAlias(set, child_model)
        parent_model = create_model(
            f"LifecycleParent{index}",
            __module__=__name__,
            children=(children_annotation, ...),
        )
        parent_family = SchemaFamily(
            model=parent_model,
            name=f"lifecycle_parent_{index}",
            versions=(SchemaVersion("stable"),),
            nested=(NestedFamily("children", child_family, {"stable": "stable"}),),
            version_metadata=None,
        )
        parent_wire = parent_family.model_for("stable")
        child_wire = child_family.model_for("stable")
        wrapper = _set_element_model(parent_wire, "children")
        references.append((weakref.ref(wrapper), weakref.ref(child_wire)))
    return tuple(references)


def test_discarded_families_do_not_accumulate_in_a_global_model_cache() -> None:
    first_batch = _discarded_hashable_model_refs(0, 128)
    gc.collect()

    # Python's typing machinery keeps a bounded cache of recent parameterized
    # annotations. Churn a second batch so that retention cannot be mistaken for
    # a pydantic-versions process-global cache.
    _discarded_hashable_model_refs(128, 128)
    gc.collect()

    assert all(wrapper() is None and child() is None for wrapper, child in first_batch)


def test_current_wire_validator_is_built_once_under_concurrent_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Payload(BaseModel):
        value: int

    family = SchemaFamily(
        model=Payload,
        name="cached_current_validator",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )
    current_wire = family.model_for("1").model_validate({"value": 7})
    original_builder = runtime_render._build_current_wire_validation_adapter
    builder_lock = Lock()
    builder_calls = 0

    def counting_builder(model: type[BaseModel], *, family_name: str) -> Any:
        nonlocal builder_calls
        with builder_lock:
            builder_calls += 1
        return original_builder(model, family_name=family_name)

    monkeypatch.setattr(
        runtime_render,
        "_build_current_wire_validation_adapter",
        counting_builder,
    )
    workers = 8
    barrier = Barrier(workers)

    def render() -> dict[str, Any]:
        barrier.wait()
        return family.dump(version="1", data=cast(Any, current_wire))

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = tuple(executor.map(lambda _: render(), range(workers)))

    assert results == ({"value": 7},) * workers
    assert family.dump(version="1", data=cast(Any, current_wire)) == {"value": 7}
    assert builder_calls == 1


def test_forced_model_rebuild_invalidates_an_existing_family() -> None:
    class Payload(BaseModel):
        value: int

    family = SchemaFamily(
        model=Payload,
        name="rebuild_boundary",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )
    generated = family.model_for("1")
    original_schema = Payload.__pydantic_core_schema__

    assert Payload.model_rebuild() is None
    assert Payload.__pydantic_core_schema__ is original_schema
    assert family.model_for("1") is generated

    assert Payload.model_rebuild(force=True) is True
    with pytest.raises(SchemaCompilationError, match="rebuilt after compilation"):
        family.compile()
    with pytest.raises(SchemaCompilationError, match="rebuilt after compilation"):
        family.validate({"value": 1}, version="1")

    replacement = SchemaFamily(
        model=Payload,
        name="replacement_after_rebuild",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )
    assert replacement.validate({"value": 2}, version="1").current_model == Payload(value=2)


def test_nested_model_rebuild_invalidates_dependent_families() -> None:
    class Child(BaseModel):
        value: int

    child_family = SchemaFamily(
        model=Child,
        name="rebuilt_child",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )

    class Parent(BaseModel):
        child: Child

    parent_family = SchemaFamily(
        model=Parent,
        name="dependent_parent",
        versions=(SchemaVersion("1"),),
        nested=(NestedFamily("child", child_family, {"1": "1"}),),
        version_metadata=None,
    )
    parent_family.compile()

    assert Child.model_rebuild(force=True) is True
    with pytest.raises(SchemaCompilationError, match="schema family 'rebuilt_child'"):
        parent_family.model_for("1")


def test_rebuild_detected_during_compilation_does_not_publish_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Payload(BaseModel):
        value: int

    family = SchemaFamily(
        model=Payload,
        name="rebuild_during_compilation",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )
    original_validator = family_module._validate_automatic_wire_model

    def rebuild_after_validation(candidate: SchemaFamily[Any]) -> None:
        original_validator(candidate)
        candidate.model.model_rebuild(force=True)

    monkeypatch.setattr(
        family_module,
        "_validate_automatic_wire_model",
        rebuild_after_validation,
    )
    with pytest.raises(SchemaCompilationError, match="rebuilt during compilation"):
        family.compile()

    assert family._compiled is None
    monkeypatch.setattr(
        family_module,
        "_validate_automatic_wire_model",
        original_validator,
    )
    assert family.compile() is family
