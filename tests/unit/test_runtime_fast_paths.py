"""Behavior and mechanism coverage for bounded runtime fast paths."""

from __future__ import annotations

import datetime as dt
from enum import Enum
from typing import Annotated, Any, NewType
from uuid import UUID

import pytest
from pydantic import BaseModel
from pydantic_core import to_jsonable_python as core_to_jsonable_python

import pydantic_versions._runtime as runtime
import pydantic_versions._runtime_nested as runtime_nested
import pydantic_versions._runtime_payload as runtime_payload
from pydantic_versions import SchemaFamily, SchemaVersion


def test_empty_decorator_selection_skips_declared_tree_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Source(BaseModel):
        value: int

    family = SchemaFamily(
        model=Source,
        name="runtime_fast_path_empty_selection",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )
    compiled = family._compiled_family()
    source = Source(value=1)

    def unexpected_extraction(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("empty selections must not extract the source tree")

    monkeypatch.setattr(runtime, "_extract_declared_fields", unexpected_extraction)

    runtime._prune_serialized_decorator_metadata(
        dumped={},
        source_model=source,
        compiled=compiled,
        parent_label="1",
        selections=(),
        by_alias=False,
    )


def test_exact_class_annotation_skips_annotation_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Ordinary:
        pass

    value = Ordinary()

    def unexpected_normalization(_annotation: Any) -> Any:
        raise AssertionError("exact class matches must not normalize the annotation")

    monkeypatch.setattr(
        runtime_payload,
        "_runtime_annotation_value",
        unexpected_normalization,
    )

    assert runtime_payload._runtime_value_matches_annotation(value, Ordinary)


def test_class_annotation_subclass_retains_runtime_instance_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Ordinary:
        pass

    class Specialized(Ordinary):
        pass

    calls: list[tuple[Any, Any]] = []
    normalized: list[Any] = []
    real_probe = runtime_payload._safe_annotation_instance
    real_normalization = runtime_payload._runtime_annotation_value

    def tracking_probe(value: Any, annotation: Any) -> bool:
        calls.append((value, annotation))
        return real_probe(value, annotation)

    def tracking_normalization(annotation: Any) -> Any:
        normalized.append(annotation)
        return real_normalization(annotation)

    value = Specialized()
    monkeypatch.setattr(runtime_payload, "_safe_annotation_instance", tracking_probe)
    monkeypatch.setattr(
        runtime_payload,
        "_runtime_annotation_value",
        tracking_normalization,
    )

    assert runtime_payload._runtime_value_matches_annotation(value, Ordinary)
    assert calls == [(value, Ordinary)]
    assert normalized == [Ordinary]


def test_class_with_a_supertype_retains_annotation_normalization() -> None:
    class SupertypeCarrier:
        __supertype__ = int

    assert not runtime_payload._runtime_value_matches_annotation(
        SupertypeCarrier(),
        SupertypeCarrier,
    )


def test_aliases_and_new_types_retain_annotation_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Ordinary:
        pass

    type alias = Ordinary
    new_type = NewType("new_type", Ordinary)
    annotated = Annotated[Ordinary, "metadata"]
    normalized: list[Any] = []
    real_normalization = runtime_payload._runtime_annotation_value

    def tracking_normalization(annotation: Any) -> Any:
        normalized.append(annotation)
        return real_normalization(annotation)

    monkeypatch.setattr(
        runtime_payload,
        "_runtime_annotation_value",
        tracking_normalization,
    )
    value = Ordinary()

    assert runtime_payload._runtime_value_matches_annotation(value, alias)
    assert runtime_payload._runtime_value_matches_annotation(value, new_type)
    assert runtime_payload._runtime_value_matches_annotation(value, annotated)
    assert normalized == [alias, new_type, annotated]


def test_exact_json_scalars_skip_general_conversion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_conversion(_value: Any, **_kwargs: Any) -> Any:
        raise AssertionError("exact JSON-native scalars must not enter pydantic-core")

    monkeypatch.setattr(runtime_payload, "to_jsonable_python", unexpected_conversion)

    values = (None, False, True, 0, 1, -1, 1.5, "payload")
    assert (
        tuple(runtime_payload._jsonable_declared_scalar(value, config={}) for value in values)
        == values
    )


def test_non_exact_json_scalars_retain_general_conversion_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Text(str):
        pass

    class Mode(Enum):
        READY = "ready"

    class Opaque:
        pass

    calls: list[Any] = []

    def tracking_conversion(value: Any, **kwargs: Any) -> Any:
        calls.append(value)
        return core_to_jsonable_python(value, **kwargs)

    monkeypatch.setattr(runtime_payload, "to_jsonable_python", tracking_conversion)

    opaque = Opaque()
    values = (
        Text("text"),
        b"bytes",
        dt.datetime(2020, 1, 2, 3, 4, 5, tzinfo=dt.UTC),
        dt.date(2020, 1, 2),
        dt.time(3, 4, 5),
        dt.timedelta(seconds=1.5),
        Mode.READY,
        UUID("12345678-1234-5678-1234-567812345678"),
        opaque,
    )
    converted = tuple(
        runtime_payload._jsonable_declared_scalar(value, config={}) for value in values
    )

    assert calls == list(values)
    assert converted == (
        "text",
        "bytes",
        "2020-01-02T03:04:05Z",
        "2020-01-02",
        "03:04:05",
        "PT1.5S",
        "ready",
        "12345678-1234-5678-1234-567812345678",
        opaque,
    )


def test_duplicate_payload_detection_does_not_slice() -> None:
    class NoSliceList(list[Any]):
        def __getitem__(self, key: Any) -> Any:
            if isinstance(key, slice):
                raise AssertionError("duplicate detection must not allocate a prefix slice")
            return super().__getitem__(key)

    assert runtime_nested._has_duplicate_payload(NoSliceList([{"value": 1}, {"value": 1}]))


def test_duplicate_payload_detection_preserves_membership_equality_semantics() -> None:
    class Earlier:
        def __eq__(self, other: object) -> bool:
            return isinstance(other, Later)

    class Later:
        def __eq__(self, _other: object) -> bool:
            return False

    class NeverEqual:
        def __eq__(self, _other: object) -> bool:
            return False

    repeated = NeverEqual()

    assert runtime_nested._has_duplicate_payload([Earlier(), Later()])
    assert not runtime_nested._has_duplicate_payload([Later(), Earlier()])
    assert runtime_nested._has_duplicate_payload([repeated, repeated])
    assert not runtime_nested._has_duplicate_payload([[1], [2]])
