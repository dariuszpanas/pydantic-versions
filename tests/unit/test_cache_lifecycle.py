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
from pydantic_versions import (
    NestedFamily,
    SchemaCompilationError,
    SchemaFamily,
    SchemaFamilySelectionError,
    SchemaVersion,
    model_for_version,
    versioned_schema,
)

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


def _discarded_default_model_refs(count: int) -> tuple[_ModelRef, ...]:
    references = []
    for index in range(count):
        model = create_model(
            f"DefaultLifecycleModel{index}",
            __module__=__name__,
            value=(int, ...),
        )
        family = SchemaFamily(
            model=model,
            name=f"default_lifecycle_{index}",
            versions=(SchemaVersion("stable"),),
            version_metadata=None,
        ).as_default()
        family.compile()
        references.append(weakref.ref(model))
    return tuple(references)


def test_discarded_model_owned_default_families_are_collectable() -> None:
    references = _discarded_default_model_refs(32)

    gc.collect()

    assert all(reference() is None for reference in references)


def test_default_family_selection_is_isolated_to_the_exact_model_class() -> None:
    class Parent(BaseModel):
        value: int

    parent_family = SchemaFamily(
        model=Parent,
        name="exact_parent_default",
        versions=(SchemaVersion("parent"),),
        version_metadata=None,
    ).as_default()

    class Child(Parent):
        pass

    with pytest.raises(SchemaFamilySelectionError, match="no explicit default"):
        model_for_version(Child, "parent")

    child_family = SchemaFamily(
        model=Child,
        name="exact_child_default",
        versions=(SchemaVersion("child"),),
        version_metadata=None,
    ).as_default()

    assert model_for_version(Parent, "parent") is parent_family.model_for("parent")
    assert model_for_version(Child, "child") is child_family.model_for("child")


@pytest.mark.parametrize("collision", [None, "application-owned", property(lambda _: None)])
def test_reserved_default_family_attribute_collision_fails_closed(collision: object) -> None:
    class Payload(BaseModel):
        value: int

    setattr(Payload, family_module._DEFAULT_FAMILY_ATTRIBUTE, collision)
    candidate = SchemaFamily(
        model=Payload,
        name="reserved_attribute_collision",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )

    with pytest.raises(SchemaFamilySelectionError, match="reserved default-family attribute"):
        candidate.as_default()
    with pytest.raises(SchemaFamilySelectionError, match="rename or remove"):
        model_for_version(Payload, "1")

    assert Payload.__dict__[family_module._DEFAULT_FAMILY_ATTRIBUTE] is collision


def test_foreign_model_default_family_attribute_collision_fails_closed() -> None:
    class Foreign(BaseModel):
        value: int

    foreign_family = SchemaFamily(
        model=Foreign,
        name="foreign_reserved_attribute_owner",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )

    class Payload(BaseModel):
        value: int

    setattr(Payload, family_module._DEFAULT_FAMILY_ATTRIBUTE, foreign_family)
    candidate = SchemaFamily(
        model=Payload,
        name="wrong_reserved_attribute_owner",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )

    with pytest.raises(SchemaFamilySelectionError, match="owned by that exact model"):
        model_for_version(Payload, "1")
    with pytest.raises(SchemaFamilySelectionError, match="owned by that exact model"):
        candidate.as_default()

    assert Payload.__dict__[family_module._DEFAULT_FAMILY_ATTRIBUTE] is foreign_family


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


def test_forced_model_rebuild_allows_default_family_replacement() -> None:
    class Payload(BaseModel):
        value: int

    selected = SchemaFamily(
        model=Payload,
        name="selected_before_rebuild",
        versions=(SchemaVersion("before"),),
        version_metadata=None,
    ).as_default()
    selected.compile()
    premature = SchemaFamily(
        model=Payload,
        name="premature_replacement",
        versions=(SchemaVersion("premature"),),
        version_metadata=None,
    )

    with pytest.raises(SchemaFamilySelectionError, match="already has explicit default"):
        premature.as_default()

    assert Payload.model_rebuild(force=True) is True
    with pytest.raises(SchemaCompilationError, match=r"call as_default\(\)"):
        model_for_version(Payload, "before")

    replacement = SchemaFamily(
        model=Payload,
        name="selected_after_rebuild",
        versions=(SchemaVersion("after"),),
        version_metadata=None,
    ).as_default()

    assert model_for_version(Payload, "after") is replacement.model_for("after")
    with pytest.raises(SchemaFamilySelectionError, match="already has explicit default"):
        selected.as_default()


def test_forced_model_rebuild_allows_decorator_default_replacement() -> None:
    @versioned_schema(
        name="decorator_selected_before_rebuild",
        versions=("before",),
        current="before",
        metadata_owner="family",
    )
    class Payload(BaseModel):
        value: int

    selected = family_module._default_family_for_model(Payload)
    assert selected is not None
    selected.compile()

    assert Payload.model_rebuild(force=True) is True
    replacement_model = versioned_schema(
        name="decorator_selected_after_rebuild",
        versions=("after",),
        current="after",
        metadata_owner="family",
    )(Payload)

    replacement = family_module._default_family_for_model(Payload)
    assert replacement_model is Payload
    assert replacement is not None and replacement is not selected
    assert model_for_version(Payload, "after") is replacement.model_for("after")


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


def test_nested_model_rebuild_allows_dependent_default_family_replacement() -> None:
    class Child(BaseModel):
        value: int

    child_family = SchemaFamily(
        model=Child,
        name="selected_nested_child",
        versions=(SchemaVersion("before"),),
        version_metadata=None,
    )

    class Parent(BaseModel):
        child: Child

    parent_family = SchemaFamily(
        model=Parent,
        name="selected_nested_parent",
        versions=(SchemaVersion("before"),),
        nested=(NestedFamily("child", child_family, {"before": "before"}),),
        version_metadata=None,
    ).as_default()
    parent_family.compile()

    assert Child.model_rebuild(force=True) is True
    with pytest.raises(SchemaCompilationError, match="schema family 'selected_nested_child'"):
        model_for_version(Parent, "before")

    replacement_child = SchemaFamily(
        model=Child,
        name="replacement_nested_child",
        versions=(SchemaVersion("after"),),
        version_metadata=None,
    )
    replacement_parent = SchemaFamily(
        model=Parent,
        name="replacement_nested_parent",
        versions=(SchemaVersion("after"),),
        nested=(NestedFamily("child", replacement_child, {"after": "after"}),),
        version_metadata=None,
    ).as_default()

    assert model_for_version(Parent, "after") is replacement_parent.model_for("after")


def test_compiled_nested_rebuild_allows_uncompiled_default_parent_replacement() -> None:
    class Child(BaseModel):
        value: int

    child_family = SchemaFamily(
        model=Child,
        name="compiled_nested_child",
        versions=(SchemaVersion("before"),),
        version_metadata=None,
    )
    child_family.compile()

    class Parent(BaseModel):
        child: Child

    selected_parent = SchemaFamily(
        model=Parent,
        name="uncompiled_selected_parent",
        versions=(SchemaVersion("before"),),
        nested=(NestedFamily("child", child_family, {"before": "before"}),),
        version_metadata=None,
    ).as_default()
    replacement_child = SchemaFamily(
        model=Child,
        name="replacement_compiled_nested_child",
        versions=(SchemaVersion("after"),),
        version_metadata=None,
    )
    replacement_parent = SchemaFamily(
        model=Parent,
        name="replacement_uncompiled_parent",
        versions=(SchemaVersion("after"),),
        nested=(NestedFamily("child", replacement_child, {"after": "after"}),),
        version_metadata=None,
    )

    assert selected_parent._compiled is None
    with pytest.raises(SchemaFamilySelectionError, match="already has explicit default"):
        replacement_parent.as_default()

    assert Child.model_rebuild(force=True) is True
    with pytest.raises(SchemaCompilationError, match="schema family 'compiled_nested_child'"):
        model_for_version(Parent, "before")

    assert replacement_parent.as_default() is replacement_parent
    assert model_for_version(Parent, "after") is replacement_parent.model_for("after")


def _race_default_selection(
    candidates: tuple[SchemaFamily[Any], ...],
) -> tuple[bool, ...]:
    barrier = Barrier(len(candidates))

    def select(family: SchemaFamily[Any]) -> bool:
        barrier.wait()
        try:
            family.as_default()
        except SchemaFamilySelectionError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=len(candidates)) as executor:
        return tuple(executor.map(select, candidates))


def test_concurrent_default_selection_and_rebuild_replacement_choose_one_family() -> None:
    class Payload(BaseModel):
        value: int

    initial = tuple(
        SchemaFamily(
            model=Payload,
            name=f"concurrent_initial_{index}",
            versions=(SchemaVersion(f"initial-{index}"),),
            version_metadata=None,
        )
        for index in range(8)
    )
    initial_results = _race_default_selection(initial)

    assert sum(initial_results) == 1
    initial_winner = initial[initial_results.index(True)]
    assert family_module._default_family_for_model(Payload) is initial_winner
    initial_winner.compile()

    assert Payload.model_rebuild(force=True) is True
    replacements = tuple(
        SchemaFamily(
            model=Payload,
            name=f"concurrent_replacement_{index}",
            versions=(SchemaVersion(f"replacement-{index}"),),
            version_metadata=None,
        )
        for index in range(8)
    )
    replacement_results = _race_default_selection(replacements)

    assert sum(replacement_results) == 1
    replacement_winner = replacements[replacement_results.index(True)]
    assert family_module._default_family_for_model(Payload) is replacement_winner
    assert model_for_version(Payload, replacement_winner.current_version) is (
        replacement_winner.model_for(replacement_winner.current_version)
    )


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
