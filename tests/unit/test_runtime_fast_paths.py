"""Behavior and mechanism coverage for bounded runtime fast paths."""

from __future__ import annotations

import gc
import weakref
from enum import Enum
from typing import Annotated, Any, Literal, NewType

import pytest
from pydantic import (
    AfterValidator,
    AliasPath,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    Strict,
    ValidationError,
    create_model,
)

import pydantic_versions._runtime as runtime
import pydantic_versions._runtime_nested as runtime_nested
import pydantic_versions._runtime_payload as runtime_payload
import pydantic_versions._runtime_validation as runtime_validation
import pydantic_versions._runtime_versioning as runtime_versioning
from pydantic_versions import (
    InvalidMigrationError,
    NestedFamily,
    SchemaCompilationError,
    SchemaFamily,
    SchemaVersion,
    UnknownSchemaVersionError,
    VersionMetadata,
    field_removed,
    field_renamed,
)


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


def test_hash_required_extraction_does_not_compare_adversarial_field_values() -> None:
    class Opaque:
        compare = False

        def __init__(self, value: int) -> None:
            self.value = value

        def __hash__(self) -> int:
            return 0

        def __eq__(self, other: object) -> bool:
            if self.compare:
                raise AssertionError("canonical carrier insertion compared field content")
            return isinstance(other, Opaque) and self.value == other.value

    class Child(BaseModel):
        model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

        value: Opaque

    values = {Child(value=Opaque(index)) for index in range(64)}
    Opaque.compare = True
    try:
        extracted = runtime_payload._extract_declared_value(
            values,
            annotation=set[Child],
        )
    finally:
        Opaque.compare = False

    assert len(extracted) == len(values)


def test_field_level_strict_enum_values_round_trip_from_canonical_storage() -> None:
    class Mode(Enum):
        active = "active"

    class FieldStrictPayload(BaseModel):
        model_config = ConfigDict(use_enum_values=True)

        mode: Mode = Field(strict=True)

    class AnnotatedStrictPayload(BaseModel):
        model_config = ConfigDict(use_enum_values=True)

        mode: Annotated[Mode, Strict()]

    class LiteralPayload(BaseModel):
        model_config = ConfigDict(strict=True, use_enum_values=True)

        mode: Literal[Mode.active]

    for model in (FieldStrictPayload, AnnotatedStrictPayload, LiteralPayload):
        source = model(mode=Mode.active)
        validated = runtime_validation._validate_canonical_model(
            model,
            {"mode": source.mode},
        )

        assert validated.mode == "active"
        assert type(validated.mode) is str

    class NativeLiteralPayload(BaseModel):
        mode: Literal[Mode.active]

    with pytest.raises(ValidationError):
        runtime_validation._validate_canonical_model(
            NativeLiteralPayload,
            {"mode": "active"},
        )

    nan = float("nan")

    class NonReflexive(Enum):
        value = nan

    class NonReflexivePayload(BaseModel):
        model_config = ConfigDict(strict=True, use_enum_values=True)

        value: NonReflexive

    source = NonReflexivePayload(value=NonReflexive.value)
    assert (
        runtime_validation._validate_canonical_model(
            NonReflexivePayload,
            {"value": source.value},
        ).value
        is nan
    )


def test_model_unions_keep_native_smart_and_tagged_arm_selection() -> None:
    events: list[str] = []

    class Mode(Enum):
        active = "active"

    def enum_after(value: Mode) -> Mode:
        events.append("enum")
        return value

    def string_after(value: str) -> str:
        events.append("string")
        return value

    class EnumArm(BaseModel):
        model_config = ConfigDict(strict=True, use_enum_values=True)

        value: Annotated[Mode, AfterValidator(enum_after)]

    class StringArm(BaseModel):
        value: Annotated[str, AfterValidator(string_after)]

    class Payload(BaseModel):
        item: EnumArm | StringArm

    validated = runtime_validation._validate_canonical_model(
        Payload,
        {"item": {"value": "active"}},
    )

    assert isinstance(validated.item, StringArm)
    assert events == ["string"]

    class Cat(BaseModel):
        model_config = ConfigDict(strict=True, use_enum_values=True)

        kind: Literal["cat"] = "cat"
        mode: Mode

    class Dog(BaseModel):
        kind: Literal["dog"] = "dog"

    class TaggedPayload(BaseModel):
        item: Annotated[Cat | Dog, Field(discriminator="kind")]

    source = TaggedPayload(item=Cat(mode=Mode.active))
    assert isinstance(source.item, Cat)
    tagged = runtime_validation._validate_canonical_model(
        TaggedPayload,
        {"item": {"kind": "cat", "mode": source.item.mode}},
    )
    assert type(tagged.item) is Cat
    assert tagged.item.mode == "active"


def test_shared_enum_refs_and_noncarrier_values_keep_custom_init_native() -> None:
    events: list[dict[str, Any]] = []

    class Mode(Enum):
        active = "active"

    type SharedModes = list[Mode]

    class CustomPayload(BaseModel):
        model_config = ConfigDict(use_enum_values=True)

        modes: SharedModes
        values: set[int]

        def __init__(self, **data: Any) -> None:
            events.append(dict(data))
            super().__init__(**data)

    class Payload(BaseModel):
        model_config = ConfigDict(strict=True, use_enum_values=True)

        modes: SharedModes
        custom: CustomPayload

    validated = runtime_validation._validate_canonical_model(
        Payload,
        {
            "modes": ["active"],
            "custom": {"modes": ["active"], "values": {1, 2}},
        },
    )

    assert validated.modes == ["active"]
    assert validated.custom.modes == ["active"]
    assert validated.custom.values == {1, 2}
    assert events == [{"modes": ["active"], "values": {1, 2}}]


def test_collection_guard_preserves_native_strict_errors_and_after_validators() -> None:
    class Child(BaseModel):
        model_config = ConfigDict(frozen=True)

        value: int

    class ValidatedPayload(BaseModel):
        children: Annotated[set[Child], AfterValidator(lambda value: {next(iter(value))})]

    class BeforePayload(BaseModel):
        children: Annotated[set[Child], BeforeValidator(list)]

    source = ValidatedPayload.model_construct(
        children={Child(value=1), Child(value=2)},
    )
    canonical = runtime_payload._extract_declared_fields(source, declared_model=ValidatedPayload)
    assert (
        len(runtime_validation._validate_canonical_model(ValidatedPayload, canonical).children) == 1
    )

    for model, container in ((BeforePayload, set), (ValidatedPayload, list)):
        source = model.model_construct(children={Child(value=1), Child(value=2)})
        canonical = runtime_payload._extract_declared_fields(source, declared_model=model)
        canonical["children"] = container(canonical["children"])
        for child in canonical["children"]:
            child["value"] = 0
        with pytest.raises(InvalidMigrationError, match="set cardinality"):
            runtime_validation._validate_canonical_model(model, canonical)


def test_recursive_schema_refs_keep_canonical_guards() -> None:
    class ModelNode(BaseModel):
        model_config = ConfigDict(frozen=True)

        value: int
        children: frozenset[ModelNode] = frozenset()

    ModelNode.model_rebuild()
    source = ModelNode(
        value=0,
        children=frozenset({ModelNode(value=1), ModelNode(value=2)}),
    )
    canonical = runtime_payload._extract_declared_fields(
        source,
        declared_model=ModelNode,
    )

    assert runtime_validation._validate_canonical_model(ModelNode, canonical) == source
    for child in canonical["children"]:
        child["value"] = 0
    with pytest.raises(InvalidMigrationError, match="set cardinality"):
        runtime_validation._validate_canonical_model(ModelNode, canonical)


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


def test_compiled_family_fast_path_skips_compile_lock_and_precompiles_lookups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Child(BaseModel):
        value: int = 1

    child_family = SchemaFamily(
        model=Child,
        name="fast_path_lookup_child",
        versions=(SchemaVersion("v1"), SchemaVersion("v2")),
        version_metadata=None,
    )

    class Parent(BaseModel):
        schema_version: str = Field(default="2", validation_alias="wire_version")
        renamed: int = 1
        removed: str = "active"
        child: Child = Field(default_factory=Child)
        second_child: Child = Field(default_factory=Child)

    family = SchemaFamily(
        model=Parent,
        name="fast_path_lookup_parent",
        versions=(
            SchemaVersion(
                "1",
                patches=(
                    field_renamed("renamed", "legacy_renamed"),
                    field_removed("removed"),
                ),
            ),
            SchemaVersion("2"),
        ),
        nested=(
            NestedFamily("child", child_family, {"1": "v1", "2": "v2"}),
            NestedFamily("second_child", child_family, {"1": "v1", "2": "v2"}),
        ),
        version_metadata=VersionMetadata("wire_version", owner="model"),
    )
    compiled = family._compiled_family()

    def unexpected_compile(_self: Any) -> Any:
        raise AssertionError("already-compiled family must not re-enter compile()")

    monkeypatch.setattr(SchemaFamily, "compile", unexpected_compile)

    assert family._compiled_family() is compiled
    assert compiled.model_metadata_field == "schema_version"
    assert runtime_versioning._model_metadata_field_name(compiled) == "schema_version"
    assert tuple(dep[0] for dep in compiled._schema_dependencies) == (family, child_family)
    assert compiled.index("1") == 0
    assert compiled.index("2") == 1
    assert compiled.version("1").projection.renamed_fields == (("renamed", "legacy_renamed"),)
    assert compiled.version("1").projection.removed_fields == ("removed",)
    assert compiled.version("2").projection.renamed_fields == ()
    assert compiled.version("2").projection.removed_fields == ()
    assert compiled.nested[0].child_label("1") == "v1"
    assert compiled.nested[0].child_label("2") == "v2"
    with pytest.raises(UnknownSchemaVersionError):
        compiled.version("unknown")
    with pytest.raises(SchemaCompilationError, match="does not contain current field"):
        compiled.version("1").projection.field("unknown")


def test_model_alias_plans_cache_and_rebuild_invalidation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Unaliased(BaseModel):
        first: int
        second: str

    class Aliased(BaseModel):
        direct: int
        nested: str = Field(validation_alias=AliasPath("envelope", "nested"))

    assert runtime_versioning._model_alias_plans(Unaliased) == ()
    cached = runtime_versioning._model_alias_plans(Aliased)
    assert len(cached) == 1
    assert cached[0].name == "nested"
    assert runtime_versioning._model_alias_plans(Aliased) is cached

    def unexpected_copy(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("atomic alias payload values must not invoke deep copy helper")

    monkeypatch.setattr(runtime_versioning, "_copy_alias_payload_value", unexpected_copy)
    assert runtime_versioning._normalize_payload_field_aliases(
        Unaliased,
        {"first": 1, "second": "two"},
    ) == {"first": 1, "second": "two"}

    Aliased.model_rebuild(force=True)
    rebuilt = runtime_versioning._model_alias_plans(Aliased)
    assert rebuilt == cached
    assert rebuilt is not cached


def test_discarded_models_in_alias_plan_cache_are_collectable() -> None:
    references: list[weakref.ReferenceType[type[BaseModel]]] = []
    for index in range(32):
        dynamic_model = create_model(
            f"DynamicAliasModel{index}",
            __module__=__name__,
            aliased=(int, Field(validation_alias=f"wire_{index}")),
        )
        runtime_versioning._model_alias_plans(dynamic_model)
        references.append(weakref.ref(dynamic_model))
        del dynamic_model

    gc.collect()

    assert all(reference() is None for reference in references)
