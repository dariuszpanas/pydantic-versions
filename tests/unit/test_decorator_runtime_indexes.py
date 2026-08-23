from __future__ import annotations

import builtins
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel

from pydantic_versions import _runtime_decorators as runtime_decorators
from pydantic_versions import versioned_schema
from pydantic_versions.family import _family_for


@versioned_schema(name="runtime_index_child_a", versions=("1",), current="1")
class _IndexedChildA(BaseModel):
    value: int


@versioned_schema(name="runtime_index_child_b", versions=("1",), current="1")
class _IndexedChildB(BaseModel):
    value: int


@versioned_schema(name="runtime_index_union_parent", versions=("1",), current="1")
class _IndexedUnionParent(BaseModel):
    items: list[_IndexedChildA | _IndexedChildB]


@versioned_schema(name="runtime_index_parent", versions=("1",), current="1")
class _IndexedParent(BaseModel):
    items: list[_IndexedChildA]


@versioned_schema(name="runtime_index_mapping_parent", versions=("1",), current="1")
class _IndexedMappingParent(BaseModel):
    items: dict[str, _IndexedChildA]


class _TrackedLocationPart(str):
    comparisons: ClassVar[int] = 0

    def __eq__(self, other: object) -> bool:
        type(self).comparisons += 1
        return super().__eq__(other)

    __hash__ = str.__hash__


def test_selection_resolves_each_union_value_once_and_shares_site_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiled = _family_for(_IndexedUnionParent)._compiled_family()
    values = [
        _IndexedChildA(value=1),
        _IndexedChildB(value=2),
        _IndexedChildA(value=3),
        _IndexedChildB(value=4),
    ]
    original_matcher = runtime_decorators._matching_declared_annotation
    matcher_calls = 0

    def counted_matcher(annotation: Any, value: Any) -> Any:
        nonlocal matcher_calls
        matcher_calls += 1
        return original_matcher(annotation, value)

    monkeypatch.setattr(
        runtime_decorators,
        "_matching_declared_annotation",
        counted_matcher,
    )
    selections = runtime_decorators._select_decorator_routes(
        _IndexedUnionParent(items=values),
        compiled=compiled,
        parent_label="1",
        source_version=None,
    )

    assert matcher_calls == len(values)
    assert {selection.location for selection in selections} == {
        ("items", index) for index in range(len(values))
    }
    shared_routes = selections[0].site_routes
    assert len(shared_routes) == 2
    assert all(selection.site_routes is shared_routes for selection in selections)


def test_reconciliation_identity_lookups_scale_linearly_for_reorders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiled = _family_for(_IndexedParent)._compiled_family()
    identity_count = 64
    items = [_IndexedChildA(value=index) for index in range(identity_count)]
    selections = runtime_decorators._select_decorator_routes(
        _IndexedParent(items=items),
        compiled=compiled,
        parent_label="1",
        source_version=None,
    )
    runtime_decorators._refresh_decorator_selection_identities(
        {"items": items},
        selections,
    )
    identity_calls = 0

    def counted_identity(value: object) -> int:
        nonlocal identity_calls
        identity_calls += 1
        return builtins.id(value)

    payload: dict[str, Any] = {"items": list(reversed(items))}
    with monkeypatch.context() as context:
        context.setattr(runtime_decorators, "id", counted_identity, raising=False)
        reconciled = runtime_decorators._reconcile_decorator_selections(
            payload=payload,
            selections=selections,
            compiled=compiled,
            discover_new=True,
        )

    assert [selection.location[-1] for selection in reconciled] == list(
        reversed(range(identity_count))
    )
    assert identity_calls <= 7 * identity_count


def test_typed_replacements_use_location_indexes_and_preserve_anchors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiled = _family_for(_IndexedParent)._compiled_family()
    replacement_count = 64
    original = [_IndexedChildA(value=index) for index in range(replacement_count)]
    selections = runtime_decorators._select_decorator_routes(
        _IndexedParent(items=original),
        compiled=compiled,
        parent_label="1",
        source_version=None,
    )
    for index, selection in enumerate(selections):
        location = (_TrackedLocationPart("items"), index)
        selection.location = location
        selection.relative_location = location
        selection.value_identity = builtins.id(original[index])

    replacements = [_IndexedChildA(value=1_000 + index) for index in range(replacement_count)]
    candidates = tuple(
        (replacements[index], (_TrackedLocationPart("items"), index))
        for index in reversed(range(replacement_count))
    )

    def walk_candidates(
        payload: Any,
        route: Any,
    ) -> tuple[tuple[Any, tuple[str | int, ...]], ...]:
        del payload, route
        return candidates

    _TrackedLocationPart.comparisons = 0
    with monkeypatch.context() as context:
        context.setattr(
            runtime_decorators,
            "_walk_decorator_payload_candidates",
            walk_candidates,
        )
        reconciled = runtime_decorators._reconcile_decorator_selections(
            payload={"items": replacements},
            selections=selections,
            compiled=compiled,
            discover_new=True,
        )

    assert [selection.location[-1] for selection in reconciled] == list(range(replacement_count))
    assert [selection.value_identity for selection in reconciled] == [
        builtins.id(value) for value in replacements
    ]
    assert _TrackedLocationPart.comparisons <= 4 * replacement_count


def test_typed_mapping_replacements_preserve_location_and_fallback_order() -> None:
    compiled = _family_for(_IndexedMappingParent)._compiled_family()
    original = {key: _IndexedChildA(value=index) for index, key in enumerate(("a", "b", "c", "d"))}
    selections = runtime_decorators._select_decorator_routes(
        _IndexedMappingParent(items=original),
        compiled=compiled,
        parent_label="1",
        source_version=None,
    )
    runtime_decorators._refresh_decorator_selection_identities(
        {"items": original},
        selections,
    )
    replacements = {
        key: _IndexedChildA(value=1_000 + index) for index, key in enumerate(("a", "x", "b", "y"))
    }
    payload: dict[str, Any] = {"items": replacements}

    reconciled = runtime_decorators._reconcile_decorator_selections(
        payload=payload,
        selections=selections,
        compiled=compiled,
        discover_new=True,
    )

    expected_locations = [("items", key) for key in replacements]
    assert [selection.location for selection in reconciled] == expected_locations
    assert [selection.value_identity for selection in reconciled] == [
        id(replacements[key]) for key in replacements
    ]
