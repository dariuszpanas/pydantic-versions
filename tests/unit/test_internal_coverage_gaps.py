from __future__ import annotations

# Internal coverage gap unit tests for wire, runtime, and compiler modules.
from collections.abc import Mapping
from typing import Any, ForwardRef, TypeAliasType, TypeVar, cast

import pytest
from pydantic import BaseModel, ConfigDict, Discriminator, Field, create_model

from pydantic_versions import SchemaFamily, SchemaVersion, VersionTransition
from pydantic_versions._compiler import (
    _CompiledField,
    _CompiledNestedFamily,
    _validate_compilation_boundary,
    _validate_family_declarations,
    _validate_transition_declarations,
    _VersionProjection,
)
from pydantic_versions._runtime import (
    _infer_metadata_owner,
    _nested_family_collection_kind,
    _normalize_payload_field_aliases,
    _prune_nested_family_metadata_at_path,
    _runtime_label,
    _set_version_field,
)
from pydantic_versions._wire import (
    _snapshot_wire_metadata,
    _validate_explicit_wire_model_metadata,
    _validate_model_config,
    _validate_type_alias,
    _wire_field_attributes,
)
from pydantic_versions.declarations import (
    NestedFamily,
    VersionMetadata,
    _freeze_sequence,
    _freeze_version_path,
    matching_labels,
)
from pydantic_versions.exceptions import (
    DuplicateSchemaVersionError,
    InvalidMigrationError,
    SchemaCompilationError,
    UnknownSchemaVersionError,
    UnsupportedWireModelError,
)


class CopyError:
    def __deepcopy__(self, _: Any) -> Any:  # pragma: no cover
        msg = "cannot copy"
        raise ValueError(msg)


def test_compiler_private_paths_raise_expected_errors() -> None:
    class Base(BaseModel):
        value: int = 1

    assert (
        _freeze_version_path("schema_version", parameter="VersionMetadata.path") == "schema_version"
    )

    with pytest.raises(SchemaCompilationError, match="must be a non-empty string or tuple"):
        _freeze_version_path((), parameter="NestedFamily.path")

    with pytest.raises(SchemaCompilationError, match="must be a sequence"):
        _freeze_sequence("v1", parameter="SchemaFamily.versions")

    compiled_projection = _VersionProjection(
        label="1.0",
        fields=(
            _CompiledField(
                current_name="value",
                version_name="value",
                default=None,
                patch_ordinal=None,
            ),
        ),
    )
    with pytest.raises(SchemaCompilationError, match="does not contain current field"):
        compiled_projection.field("missing")

    with pytest.raises(SchemaCompilationError, match="has no child label"):
        _CompiledNestedFamily(path=("child",), family=cast(Any, object()), versions=()).child_label(
            "legacy"
        )

    with pytest.raises(SchemaCompilationError, match="must declare at least one version"):
        _validate_family_declarations(
            model=Base,
            name="empty",
            versions=(),
            transitions=(),
            nested=(),
            missing_version=None,
        )

    with pytest.raises(DuplicateSchemaVersionError, match="is declared more than once"):
        _validate_transition_declarations(
            "family",
            ("1", "2"),
            (
                VersionTransition("1", "2", upgrade=lambda payload: payload),
                VersionTransition("1", "2", upgrade=lambda payload: payload),
            ),
        )

    with pytest.raises(SchemaCompilationError, match="must provide at least one callable"):
        _validate_transition_declarations(
            "family",
            ("1", "2"),
            (VersionTransition("1", "2"),),
        )

    with pytest.raises(SchemaCompilationError, match="cannot be patched"):
        _validate_family_declarations(
            model=Base,
            name="current-with-patch",
            versions=(SchemaVersion("1", patches=(cast(Any, object()),)),),
            transitions=(),
            nested=(),
            missing_version=None,
        )


def test_compiler_validation_catches_nested_family_projection_errors() -> None:
    class Parent(BaseModel):
        value: int = 1

    class Child(BaseModel):
        value: int = 1

    child = SchemaFamily(
        model=Child,
        name="child",
        versions=(SchemaVersion("v1"), SchemaVersion("v2")),
    )
    parent = SchemaFamily(
        model=Parent,
        name="parent",
        versions=(SchemaVersion("v1"), SchemaVersion("v2")),
        nested=(
            NestedFamily("value", child, matching_labels()),
            NestedFamily("value", child, matching_labels()),
        ),
    )

    with pytest.raises(SchemaCompilationError, match="Duplicate nested family declaration path"):
        _validate_compilation_boundary(
            name="parent",
            model=Parent,
            labels=("v1", "v2"),
            nested=parent.nested,
        )

    with pytest.raises(SchemaCompilationError, match="must resolve to a SchemaFamily"):
        _validate_compilation_boundary(
            name="parent",
            model=Parent,
            labels=("v1", "v2"),
            nested=(NestedFamily("value", lambda: cast(Any, "not_family"), matching_labels()),),
        )

    compiled_boundary = _validate_compilation_boundary(
        name="parent",
        model=Parent,
        labels=("v1", "v2"),
        nested=(NestedFamily("value", lambda: child, versions={"v1": "v1", "v2": "v2"}),),
    )
    assert len(compiled_boundary) == 1

    comp_family = child._compiled_family()
    assert comp_family.labels == ("v1", "v2")


def test_runtime_private_edges() -> None:
    class Base(BaseModel):
        value: int = 1

    assert _runtime_label("v1", family_name="runtime") == "v1"

    with pytest.raises(UnknownSchemaVersionError, match="must be a non-empty string"):
        _runtime_label(1, family_name="runtime")

    assert _infer_metadata_owner(Base, "value") == "model"
    assert _infer_metadata_owner(Base, ("meta", "schema_version")) == "family"

    payload: dict[str, Any] = {"nested": []}
    with pytest.raises(InvalidMigrationError, match="Cannot set version metadata"):
        _set_version_field(payload, ("nested", "value"), "v1")

    payload = {"nested": {"schema_version": "v2"}, "extra": 1}
    _set_version_field(payload, ("schema_version",), "v1")
    assert payload == {"nested": {"schema_version": "v2"}, "extra": 1, "schema_version": "v1"}

    normalized = _normalize_payload_field_aliases(
        Base,
        {"value": 2, "legacy_value": 3},
        prefer_aliases=False,
    )
    assert normalized["value"] == 2

    parent = SchemaFamily(
        model=Base,
        name="runtime-parent",
        versions=(SchemaVersion("v1"), SchemaVersion("v2")),
    )
    payload = {"outer": {"value": {"schema_version": "v2"}}}
    _prune_nested_family_metadata_at_path(
        payload=payload,
        path=("outer", "value"),
        family=parent,
    )
    assert payload == {"outer": {"value": {}}}

    assert _nested_family_collection_kind(model=Base, path=("value",)) is None


def test_wire_private_paths() -> None:
    bad_config = create_model("BadConfig", value=(int, 1), __config__=cast(ConfigDict, {1: True}))

    bad_family = SchemaFamily(
        model=bad_config,
        name="bad-config",
        versions=(SchemaVersion("1"),),
    )

    with pytest.raises(UnsupportedWireModelError, match="model configuration keys must be strings"):
        _validate_model_config(bad_family)

    with pytest.raises(
        UnsupportedWireModelError,
        match="uses unsupported attributes",
    ):
        _wire_field_attributes(
            cast(Any, bad_family),
            "value",
            {"unknown": True},
        )

    with pytest.raises(UnsupportedWireModelError, match="callable discriminator"):
        _snapshot_wire_metadata(
            cast(Any, bad_family),
            "value",
            (Discriminator(lambda value: str(value)),),
            detail="wire",
        )

    unresolved = ForwardRef("UndefinedAlias")
    unresolved_alias: Any = unresolved
    type custom[T_alias] = unresolved_alias

    with pytest.raises(UnsupportedWireModelError, match="forward reference hidden in a type alias"):
        _validate_type_alias(
            cast(Any, bad_family),
            "value",
            cast(Any, custom),
        )


def test_runtime_and_wire_cross_product_helpers() -> None:
    class ProjectionModel(BaseModel):
        model_config = ConfigDict(populate_by_name=True)

        value: int = Field(validation_alias="payload")
        schema_version: str = "1"

    family = SchemaFamily(
        model=ProjectionModel,
        name="projection-helper",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )
    historical = family.model_for("1")
    projection_alias = _runtime_label("1", family_name="projection-helper")
    assert projection_alias == "1"

    rendered = historical.model_json_schema()["properties"]
    assert isinstance(rendered, Mapping)

    assert _nested_family_collection_kind(model=ProjectionModel, path=("value",)) is None


def test_more_compiler_validation_branches() -> None:
    class Base(BaseModel):
        value: int = 1

    class ExtraFieldModel(BaseModel):
        val: int = 1
        other: int = 2

    # SchemaFamily.versions containing non-SchemaVersion
    with pytest.raises(SchemaCompilationError, match="must contain only SchemaVersion values"):
        _validate_family_declarations(
            model=Base,
            name="invalid-version-item",
            versions=(cast(Any, "1"),),
            transitions=(),
            nested=(),
            missing_version=None,
        )

    # SchemaFamily.transitions containing non-VersionTransition
    with pytest.raises(SchemaCompilationError, match="must contain only VersionTransition values"):
        _validate_family_declarations(
            model=Base,
            name="invalid-transition-item",
            versions=(SchemaVersion("1"),),
            transitions=(cast(Any, "1->2"),),
            nested=(),
            missing_version=None,
        )

    # SchemaFamily.nested containing non-NestedFamily
    with pytest.raises(SchemaCompilationError, match="must contain only NestedFamily values"):
        _validate_family_declarations(
            model=Base,
            name="invalid-nested-item",
            versions=(SchemaVersion("1"),),
            transitions=(),
            nested=(cast(Any, "nested"),),
            missing_version=None,
        )

    # Downgrade not callable error in _validate_transition_declarations
    with pytest.raises(SchemaCompilationError, match="must be callable"):
        _validate_transition_declarations(
            "family",
            ("1", "2"),
            (
                VersionTransition(
                    "1", "2", upgrade=lambda p: p, downgrade=cast(Any, "not_callable")
                ),
            ),
        )

    # downgrade_semantics forbidden without downgrade
    with pytest.raises(SchemaCompilationError, match="forbidden when no downgrade is declared"):
        _validate_transition_declarations(
            "family",
            ("1", "2"),
            (VersionTransition("1", "2", upgrade=lambda p: p, downgrade_semantics="exact"),),
        )

    # downgrade_semantics invalid
    with pytest.raises(SchemaCompilationError, match="must be 'exact' or 'lossy'"):
        _validate_transition_declarations(
            "family",
            ("1", "2"),
            (
                VersionTransition(
                    "1",
                    "2",
                    upgrade=lambda p: p,
                    downgrade=lambda p: p,
                    downgrade_semantics=cast(Any, "invalid"),
                ),
            ),
        )

    # NestedFamily callable provider returning non-SchemaFamily
    with pytest.raises(SchemaCompilationError, match="must resolve to a SchemaFamily"):
        _validate_compilation_boundary(
            name="parent",
            model=Base,
            labels=("v1",),
            nested=(NestedFamily("value", lambda: cast(Any, "not_family"), matching_labels()),),
        )

    # NestedFamily pointing to same model
    parent_family = SchemaFamily(model=Base, name="base", versions=(SchemaVersion("v1"),))
    with pytest.raises(SchemaCompilationError, match="cannot reference the same owning model"):
        _validate_compilation_boundary(
            name="parent",
            model=Base,
            labels=("v1",),
            nested=(NestedFamily("value", parent_family, matching_labels()),),
        )

    # NestedFamily matching labels when child labels != parent labels
    child_family = SchemaFamily(
        model=ExtraFieldModel,
        name="child",
        versions=(SchemaVersion("v1"), SchemaVersion("v2")),
    )
    with pytest.raises(SchemaCompilationError, match="must use matching labels"):
        _validate_compilation_boundary(
            name="parent",
            model=Base,
            labels=("v1",),
            nested=(NestedFamily("val", child_family, matching_labels()),),
        )

    # NestedFamily versions not a Mapping
    with pytest.raises(SchemaCompilationError, match="must be a mapping"):
        _validate_compilation_boundary(
            name="parent",
            model=Base,
            labels=("v1",),
            nested=(NestedFamily("val", child_family, versions=cast(Any, "not_a_mapping")),),
        )

    # NestedFamily versions missing mappings for parent labels
    with pytest.raises(SchemaCompilationError, match="missing mappings"):
        _validate_compilation_boundary(
            name="parent",
            model=Base,
            labels=("v1", "v2"),
            nested=(NestedFamily("val", child_family, versions={"v1": "v1"}),),
        )

    # NestedFamily versions unknown parent label
    with pytest.raises(SchemaCompilationError, match="unknown parent labels"):
        _validate_compilation_boundary(
            name="parent",
            model=Base,
            labels=("v1",),
            nested=(NestedFamily("val", child_family, versions={"v1": "v1", "v_unknown": "v1"}),),
        )

    # NestedFamily versions unknown child label
    with pytest.raises(SchemaCompilationError, match="unknown child labels"):
        _validate_compilation_boundary(
            name="parent",
            model=Base,
            labels=("v1",),
            nested=(NestedFamily("val", child_family, versions={"v1": "v_unknown"}),),
        )


def test_more_runtime_collection_and_migration_branches() -> None:
    class ChildModel(BaseModel):
        val: int = 1

    child_fam = SchemaFamily(
        model=ChildModel,
        name="runtime-child",
        versions=(
            SchemaVersion("v1"),
            SchemaVersion("v2"),
        ),
        version_metadata=VersionMetadata("schema_version"),
        transitions=(
            VersionTransition(
                "v1",
                "v2",
                upgrade=lambda p: {"val": p.get("val", 1) + 1},
                downgrade=lambda p: {"val": p.get("val", 2) - 1},
                downgrade_semantics="exact",
            ),
        ),
    )

    # test _prune_nested_family_metadata_at_path with family having version metadata
    payload_dict = {"schema_version": "v1", "val": 1}
    _prune_nested_family_metadata_at_path(
        payload=payload_dict,
        path=(),
        family=child_fam,
    )
    assert payload_dict == {"val": 1}

    # test upgrade returning non-dict raises InvalidMigrationError
    bad_upgrade_fam = SchemaFamily(
        model=ChildModel,
        name="non-dict-upgrade",
        versions=(
            SchemaVersion("v1"),
            SchemaVersion("v2"),
        ),
        version_metadata=VersionMetadata("schema_version"),
        transitions=(
            VersionTransition(
                "v1",
                "v2",
                upgrade=lambda p: cast(Any, "not_a_dict"),
            ),
        ),
    )
    with pytest.raises(InvalidMigrationError, match="must return a dict"):
        bad_upgrade_fam.validate({"val": 1, "schema_version": "v1"})

    # test downgrade returning non-dict raises InvalidMigrationError
    bad_downgrade_fam = SchemaFamily(
        model=ChildModel,
        name="non-dict-downgrade",
        versions=(
            SchemaVersion("v1"),
            SchemaVersion("v2"),
        ),
        version_metadata=None,
        transitions=(
            VersionTransition(
                "v1",
                "v2",
                upgrade=lambda p: p,
                downgrade=lambda p: cast(Any, "not_a_dict"),
                downgrade_semantics="exact",
            ),
        ),
    )
    with pytest.raises(InvalidMigrationError, match="must return a dict"):
        bad_downgrade_fam.dump(version="v1", data=ChildModel(val=1))


def test_explicit_wire_model_metadata_validation() -> None:
    from typing import Literal

    class WireMissingField(BaseModel):
        val: int

    class ModelWithMeta(BaseModel):
        val: int = 1
        schema_version: str = "v1"

    fam = SchemaFamily(
        model=ModelWithMeta,
        name="meta-wire",
        versions=(SchemaVersion("v1"),),
        version_metadata=VersionMetadata("schema_version", owner="model"),
    )
    comp = fam._compiled_family()
    proj = comp.version("v1").projection

    with pytest.raises(
        UnsupportedWireModelError, match="must declare the same model metadata field"
    ):
        _validate_explicit_wire_model_metadata(fam, proj, WireMissingField)

    class WireWrongType(BaseModel):
        val: int
        schema_version: int = 1

    with pytest.raises(UnsupportedWireModelError, match="must annotate field"):
        _validate_explicit_wire_model_metadata(fam, proj, WireWrongType)

    class WireWrongDefault(BaseModel):
        val: int
        schema_version: Literal["v1"] = cast(Any, "v2")

    with pytest.raises(UnsupportedWireModelError, match="must provide the exact default"):
        _validate_explicit_wire_model_metadata(fam, proj, WireWrongDefault)


def test_declarations_family_and_core_coverage_gaps() -> None:
    from pydantic_versions.core import schema_version, versioned_schema

    with pytest.raises(
        SchemaCompilationError, match="VersionMetadata.owner must be 'family' or 'model'"
    ):
        VersionMetadata(owner=cast(Any, "invalid"))

    class ModelBase(BaseModel):
        val: int = 1

    class Wire1(BaseModel):
        val: int = 1

    class Wire2(BaseModel):
        val: int = 1

    with pytest.raises(
        SchemaCompilationError, match="version_metadata must be VersionMetadata or None"
    ):
        SchemaFamily(
            model=ModelBase,
            name="bad-meta",
            versions=(SchemaVersion("v1"),),
            version_metadata=cast(Any, "invalid_str"),
        )

    # Core decorator duplicate wire model for same version label
    with pytest.raises(DuplicateSchemaVersionError, match="declared more than once"):

        @versioned_schema(name="test-dup-wire", versions=("v1",), current="v1")
        @schema_version("v1", wire_model=Wire1)
        @schema_version("v1", wire_model=Wire2)
        class DupWireModel(BaseModel):
            val: int = 1

    fam = SchemaFamily(
        model=ModelBase,
        name="recursive-check",
        versions=(SchemaVersion("v1"), SchemaVersion("v2")),
    )
    with pytest.raises(UnknownSchemaVersionError, match="Unknown migration edge"):
        fam._ensure_legacy_transition_allowed("v1", "v3")

    fam._compiling = True
    with pytest.raises(
        SchemaCompilationError, match="Recursive schema-family compilation is not yet supported"
    ):
        fam.compile()


def test_nested_family_container_collections_and_conversions() -> None:
    class ChildModel(BaseModel):
        val: int = 1

    child_fam = SchemaFamily(
        model=ChildModel,
        name="container-child",
        versions=(SchemaVersion("v1"), SchemaVersion("v2")),
        version_metadata=VersionMetadata("schema_version"),
        transitions=(
            VersionTransition(
                "v1",
                "v2",
                upgrade=lambda p: {"val": p.get("val", 1) + 10},
                downgrade=lambda p: {"val": p.get("val", 11) - 10},
                downgrade_semantics="exact",
            ),
        ),
    )

    class Wrapper(BaseModel):
        child: ChildModel

    class ParentModel(BaseModel):
        wrappers_list: list[Wrapper]
        wrappers_tuple: tuple[Wrapper, ...]

    parent_fam = SchemaFamily(
        model=ParentModel,
        name="container-parent",
        versions=(SchemaVersion("v1"), SchemaVersion("v2")),
        version_metadata=VersionMetadata("schema_version"),
        nested=(
            NestedFamily(("wrappers_list", "child"), child_fam, matching_labels()),
            NestedFamily(("wrappers_tuple", "child"), child_fam, matching_labels()),
        ),
    )

    # Validate v1 payload containing collections of wrapped v1 children
    payload_v1 = {
        "schema_version": "v1",
        "wrappers_list": [{"child": {"schema_version": "v1", "val": 1}}],
        "wrappers_tuple": ({"child": {"schema_version": "v1", "val": 2}},),
    }

    res = parent_fam.validate(payload_v1)
    assert res.current_model.wrappers_list[0].child.val == 11
    assert res.current_model.wrappers_tuple[0].child.val == 12

    # Dump v1 payload from ParentModel instance
    dumped = parent_fam.dump(version="v1", data=res.current_model)
    assert dumped["wrappers_list"][0]["child"]["val"] == 1


def test_wire_decorators_and_uncopyable_defaults() -> None:
    from pydantic import field_validator

    class ModelWithValidator(BaseModel):
        val: int

        @field_validator("val")
        @classmethod
        def validate_val(cls, v: int) -> int:
            return v

    fam_val = SchemaFamily(
        model=ModelWithValidator,
        name="validator-family",
        versions=(SchemaVersion("v1"),),
    )
    assert fam_val.model_for("v1") is not None

    class UncopyableDefaultModel(BaseModel):
        val: Any = Field(default=CopyError())

    bad_fam = SchemaFamily(
        model=UncopyableDefaultModel,
        name="uncopyable-family",
        versions=(SchemaVersion("v1"),),
    )
    with pytest.raises(UnsupportedWireModelError, match="cannot safely copy"):
        bad_fam.model_for("v1")


def test_more_runtime_alias_and_prune_edge_cases() -> None:
    class Base(BaseModel):
        val: int = Field(default=1, validation_alias="legacy_val")

    normalized = _normalize_payload_field_aliases(
        Base,
        {"legacy_val": 5},
        prefer_aliases=True,
    )
    assert normalized["val"] == 5

    child_fam = SchemaFamily(
        model=Base,
        name="edge-child",
        versions=(SchemaVersion("v1"),),
    )
    # _prune_nested_family_metadata_at_path with missing key in dict
    payload_missing_key = {"other": 1}
    _prune_nested_family_metadata_at_path(
        payload=payload_missing_key,
        path=("missing_key",),
        family=child_fam,
    )
    assert payload_missing_key == {"other": 1}

    # _prune_nested_family_metadata_at_path with list, tuple, set, frozenset
    p_list = [{"schema_version": "v1", "val": 1}]
    _prune_nested_family_metadata_at_path(payload=p_list, path=("item",), family=child_fam)

    p_tuple = ({"schema_version": "v1", "val": 1},)
    _prune_nested_family_metadata_at_path(payload=p_tuple, path=("item",), family=child_fam)

    p_set = set()
    _prune_nested_family_metadata_at_path(payload=p_set, path=("item",), family=child_fam)

    p_frozenset = frozenset()
    _prune_nested_family_metadata_at_path(payload=p_frozenset, path=("item",), family=child_fam)

    # family.defaults_for and dump with mode="json"
    assert child_fam.defaults_for(version="v1") is not None
    assert child_fam.dump(version="v1", data=Base(legacy_val=1), mode="json") is not None

    # top-level dump downgrade returning non-dict
    downgrade_bad_top = SchemaFamily(
        model=Base,
        name="downgrade-bad-top",
        versions=(SchemaVersion("v1"), SchemaVersion("v2")),
        transitions=(
            VersionTransition(
                "v1",
                "v2",
                upgrade=lambda p: p,
                downgrade=lambda p: cast(Any, "not_a_dict"),
                downgrade_semantics="exact",
            ),
        ),
    )
    with pytest.raises(InvalidMigrationError, match="downgrade must return a dict"):
        downgrade_bad_top.dump(version="v1", data=Base(legacy_val=1))


def test_type_alias_unsupported_metadata() -> None:
    from typing import Annotated, Literal

    from pydantic import Discriminator, WithJsonSchema

    class ModelA(BaseModel):
        kind: Literal["a"] = "a"

    class ModelB(BaseModel):
        kind: Literal["b"] = "b"

    disc_alias: Any = Annotated[ModelA | ModelB, Discriminator("kind")]
    AliasDisc = TypeAliasType("AliasDisc", disc_alias)  # noqa: N806, UP040

    class ModelAliasDisc(BaseModel):
        val: AliasDisc

    fam_disc = SchemaFamily(
        model=ModelAliasDisc,
        name="alias-disc",
        versions=(SchemaVersion("v1"),),
    )
    with pytest.raises(UnsupportedWireModelError, match="hidden in a type alias"):
        fam_disc.model_for("v1")

    json_alias: Any = Annotated[int, WithJsonSchema({})]
    AliasJson = TypeAliasType("AliasJson", json_alias)  # noqa: N806, UP040

    class ModelAliasJson(BaseModel):
        val: AliasJson

    fam_json = SchemaFamily(
        model=ModelAliasJson,
        name="alias-json",
        versions=(SchemaVersion("v1"),),
    )
    with pytest.raises(UnsupportedWireModelError, match="hidden in a type alias"):
        fam_json.model_for("v1")


def test_runtime_helper_functions() -> None:
    from typing import Annotated

    from pydantic_versions._runtime import (
        _collection_kind,
        _has_duplicate_payload,
        _strip_annotated,
    )

    assert _collection_kind(list[int]) == "list"
    assert _collection_kind(tuple[int, ...]) == "tuple"
    assert _collection_kind(set[int]) == "set"
    assert _collection_kind(frozenset[int]) == "frozenset"

    assert _strip_annotated(Annotated[int, "meta"]) is int
    assert _has_duplicate_payload([1, 1]) is True
    assert _has_duplicate_payload([1, 2]) is False

    from pydantic_versions._runtime import _nested_family_collection_kind

    class Child(BaseModel):
        val: int

    class Parent(BaseModel):
        items: list[Child]

    assert _nested_family_collection_kind(model=Parent, path=("items", "val")) is None
    assert _nested_family_collection_kind(model=Parent, path=("missing",)) is None
    assert _nested_family_collection_kind(model=cast(Any, int), path=("val",)) is None


def test_runtime_alias_choices_and_payload_paths() -> None:
    from pydantic import AliasChoices, AliasPath

    from pydantic_versions._runtime import (
        _field_alias_paths,
        _remove_payload_path,
        _set_payload_path,
        _to_version_names,
    )

    class ModelAlias(BaseModel):
        f1: int = Field(validation_alias=AliasChoices("a", AliasPath("b", "c")))
        f2: int = Field(validation_alias=AliasPath("d", "e"))

    paths1 = _field_alias_paths(ModelAlias.model_fields["f1"])
    assert paths1 == (("a",), ("b", "c"))

    paths2 = _field_alias_paths(ModelAlias.model_fields["f2"])
    assert paths2 == (("d", "e"),)

    # _remove_payload_path edge cases
    _remove_payload_path({}, ())
    d1 = {"a": 1}
    _remove_payload_path(d1, ("a", "b"))
    assert d1 == {"a": 1}
    _remove_payload_path(cast(Any, "scalar"), ("a",))

    # _set_payload_path edge cases
    _set_payload_path({}, (), 1)
    _set_payload_path(cast(Any, "scalar"), ("a",), 1)
    d2 = {"a": 1}
    _set_payload_path(d2, ("a", "b"), 2)
    assert d2 == {"a": 1}

    # _to_version_names edge cases
    assert _to_version_names(cast(Any, None), "scalar") == "scalar"


def test_runtime_nested_child_and_family_payload_conversions() -> None:
    from pydantic_versions._runtime import (
        _convert_nested_child_family,
        _convert_nested_family_payload,
    )

    class HashableDict(dict[str, Any]):
        def __hash__(self) -> int:
            return hash(
                tuple(
                    sorted(
                        (k, hash(v) if isinstance(v, HashableDict) else v) for k, v in self.items()
                    )
                )
            )

    class ChildModel(BaseModel):
        val: int = 1

    child_fam = SchemaFamily(
        model=ChildModel,
        name="nested-conv-child",
        versions=(SchemaVersion("v1"), SchemaVersion("v2")),
        version_metadata=VersionMetadata("schema_version"),
        transitions=(
            VersionTransition(
                "v1",
                "v2",
                upgrade=lambda p: HashableDict({"val": p.get("val", 1) + 10}),
                downgrade=lambda p: HashableDict({"val": p.get("val", 11) - 10}),
                downgrade_semantics="exact",
            ),
        ),
    )

    # _convert_nested_child_family direct tests with tuple, set, frozenset
    item_v1 = HashableDict({"schema_version": "v1", "val": 1})

    res_tuple = _convert_nested_child_family(
        payload=({"sub": item_v1},),
        path=("sub",),
        family=child_fam,
        source_label="v1",
        target_label="v2",
    )
    assert res_tuple[0]["sub"]["val"] == 11

    res_set = _convert_nested_child_family(
        payload={1, 2},
        path=("sub",),
        family=child_fam,
        source_label="v1",
        target_label="v2",
    )
    assert len(res_set) == 2

    res_frozenset = _convert_nested_child_family(
        payload=frozenset([1, 2]),
        path=("sub",),
        family=child_fam,
        source_label="v1",
        target_label="v2",
    )
    assert len(res_frozenset) == 2

    # _convert_nested_family_payload direct tests with tuple, set, frozenset
    payload_tuple = (item_v1,)
    conv_tuple = _convert_nested_family_payload(
        family=child_fam,
        payload=payload_tuple,
        source_label="v1",
        target_label="v2",
    )
    assert conv_tuple[0]["val"] == 11

    payload_set = {item_v1}
    conv_set = _convert_nested_family_payload(
        family=child_fam,
        payload=payload_set,
        source_label="v1",
        target_label="v2",
    )
    assert len(conv_set) == 1

    payload_frozenset = frozenset([item_v1])
    conv_frozenset = _convert_nested_family_payload(
        family=child_fam,
        payload=payload_frozenset,
        source_label="v1",
        target_label="v2",
    )
    assert len(conv_frozenset) == 1


def test_wire_annotation_behavior_and_type_parameter_values() -> None:
    from typing import Annotated

    from pydantic import Discriminator, WithJsonSchema

    from pydantic_versions._wire import (
        _annotation_contains_runtime_behavior,
        _type_parameter_values,
    )

    class CustomJsonSchema(WithJsonSchema):
        pass

    assert (
        _annotation_contains_runtime_behavior(
            Annotated[int, Discriminator(lambda x: "a")],
            seen=set(),
        )
        is True
    )
    assert (
        _annotation_contains_runtime_behavior(
            Annotated[int, CustomJsonSchema({})],
            seen=set(),
        )
        is True
    )

    T = TypeVar("T", int, str)
    values = _type_parameter_values(T)
    assert int in values
    assert str in values


def test_wire_validate_object_schema_edge_cases() -> None:
    from pydantic_versions._wire import _validate_object_schema

    class DummyModel(BaseModel):
        val: int

    fam = SchemaFamily(model=DummyModel, name="json-schema-test", versions=(SchemaVersion("v1"),))
    proj = fam._compiled_family().version("v1").projection

    # non-serializable schema
    class NonSerializableModel(BaseModel):
        @classmethod
        def model_json_schema(
            cls,
            by_alias: bool = True,
            ref_template: str = "",
            schema_generator: Any = None,
            mode: str = "validation",
            **kwargs: Any,
        ) -> dict[str, Any]:
            return {"bad": cast(Any, object())}

    with pytest.raises(UnsupportedWireModelError, match="non-JSON-serializable"):
        _validate_object_schema(fam, proj, NonSerializableModel, mode="validation")

    # $ref resolution
    class RefModel(BaseModel):
        @classmethod
        def model_json_schema(
            cls,
            by_alias: bool = True,
            ref_template: str = "",
            schema_generator: Any = None,
            mode: str = "validation",
            **kwargs: Any,
        ) -> dict[str, Any]:
            return {
                "$ref": "#/$defs/Target",
                "$defs": {"Target": {"type": "object"}},
            }

    _validate_object_schema(fam, proj, RefModel, mode="validation")

    # non-object schema
    class NonObjectModel(BaseModel):
        @classmethod
        def model_json_schema(
            cls,
            by_alias: bool = True,
            ref_template: str = "",
            schema_generator: Any = None,
            mode: str = "validation",
            **kwargs: Any,
        ) -> dict[str, Any]:
            return {"type": "string"}

    with pytest.raises(UnsupportedWireModelError, match="has a non-object"):
        _validate_object_schema(fam, proj, NonObjectModel, mode="validation")


def test_wire_generic_type_alias() -> None:

    T = TypeVar("T", bound=int)
    GenericAlias = TypeAliasType("GenericAlias", list[T], type_params=(T,))  # noqa: N806, UP040

    class ModelGen(BaseModel):
        val: GenericAlias[int]

    fam = SchemaFamily(model=ModelGen, name="gen-alias", versions=(SchemaVersion("v1"),))
    assert fam.model_for("v1") is not None


def test_wire_metadata_validation_edge_cases() -> None:
    from typing import Annotated, NewType

    from pydantic import Discriminator

    from pydantic_versions._wire import (
        _model_metadata_field,
        _validate_annotation_behavior,
        _validate_family_metadata_collision,
    )

    class DummyModel(BaseModel):
        ver: str

    fam = SchemaFamily(model=DummyModel, name="dummy", versions=(SchemaVersion("v1"),))

    # NewType with behavior
    NT = NewType("NT", Annotated[int, Discriminator(lambda x: "a")])  # noqa: N806
    with pytest.raises(UnsupportedWireModelError, match="behavioral NewType target"):
        _validate_annotation_behavior(fam, "f", NT)

    # model-owned metadata path non-str tuple
    fam_non_str = SchemaFamily(
        model=DummyModel,
        name="non-str-meta",
        versions=(SchemaVersion("v1"),),
        version_metadata=VersionMetadata(("ver", "sub"), owner="model"),
    )
    with pytest.raises(
        UnsupportedWireModelError, match="requires the top-level conversion compiler"
    ):
        _model_metadata_field(fam_non_str)

    # model-owned metadata path matches 0 fields
    fam_no_match = SchemaFamily(
        model=DummyModel,
        name="no-match-meta",
        versions=(SchemaVersion("v1"),),
        version_metadata=VersionMetadata("unknown_field", owner="model"),
    )
    with pytest.raises(UnsupportedWireModelError, match="must resolve to exactly one direct field"):
        _model_metadata_field(fam_no_match)

    # family-owned version metadata collides with model field
    fam_collision = SchemaFamily(
        model=DummyModel,
        name="collision-meta",
        versions=(SchemaVersion("v1"),),
        version_metadata=VersionMetadata("ver", owner="family"),
    )
    with pytest.raises(UnsupportedWireModelError, match="family-owned version metadata collides"):
        _validate_family_metadata_collision(fam_collision)


def test_wire_module_reflection_helpers() -> None:
    from pydantic_versions._wire import _is_exact_module_member, _is_typing_reflection_owner

    assert _is_exact_module_member(int, module="builtins") is True
    assert _is_exact_module_member(int, module="typing") is False
    assert _is_typing_reflection_owner(int) is True

    class LocalModel(BaseModel):
        pass

    assert _is_typing_reflection_owner(LocalModel) is False


def test_runtime_more_nested_family_migration_errors_and_conversions() -> None:
    from pydantic_versions._runtime import (
        _convert_nested_child_family,
        _convert_nested_family_payload,
    )

    class ChildModel(BaseModel):
        val: int = 1

    bad_upgrade_fam = SchemaFamily(
        model=ChildModel,
        name="bad-up-child",
        versions=(SchemaVersion("v1"), SchemaVersion("v2")),
        transitions=(
            VersionTransition(
                "v1",
                "v2",
                upgrade=lambda p: cast(Any, "not_dict"),
                downgrade=lambda p: p,
                downgrade_semantics="exact",
            ),
        ),
    )

    with pytest.raises(InvalidMigrationError, match="must return a dict"):
        _convert_nested_family_payload(
            family=bad_upgrade_fam,
            payload={"schema_version": "v1", "val": 1},
            source_label="v1",
            target_label="v2",
        )

    bad_downgrade_fam = SchemaFamily(
        model=ChildModel,
        name="bad-down-child",
        versions=(SchemaVersion("v1"), SchemaVersion("v2")),
        transitions=(
            VersionTransition(
                "v1",
                "v2",
                upgrade=lambda p: p,
                downgrade=lambda p: cast(Any, "not_dict"),
                downgrade_semantics="exact",
            ),
        ),
    )

    with pytest.raises(InvalidMigrationError, match="must return a dict"):
        _convert_nested_family_payload(
            family=bad_downgrade_fam,
            payload={"schema_version": "v2", "val": 1},
            source_label="v2",
            target_label="v1",
        )

    # _convert_nested_child_family multi-level dict conversion
    item_v1 = {"schema_version": "v1", "val": 1}
    res_dict = _convert_nested_child_family(
        payload={"a": {"sub": item_v1}},
        path=("sub", "val"),
        family=bad_upgrade_fam,
        source_label="v1",
        target_label="v2",
    )
    assert res_dict["a"]["sub"]["val"] == 1


def test_wire_decorator_child_label_mismatch() -> None:
    from pydantic_versions import versioned_schema
    from pydantic_versions.family import _default_family_for_model

    @versioned_schema(name="mismatch-child", versions=("v1", "v2"), current="v2")
    class MismatchChild(BaseModel):
        val: int = 1

    @versioned_schema(name="mismatch-parent", versions=("v1",), current="v1")
    class MismatchParent(BaseModel):
        child: MismatchChild

    fam = _default_family_for_model(MismatchParent)
    assert fam is not None
    with pytest.raises(UnsupportedWireModelError, match="could not be built safely"):
        fam.model_for("v1")


def test_wire_more_annotation_behavior_and_forward_refs() -> None:
    from dataclasses import dataclass

    from pydantic_versions._wire import (
        _annotation_contains_runtime_behavior,
        _has_behavioral_structured_annotation,
        _owner_annotations,
    )

    @dataclass
    class PostInitDataclass:
        val: int

        def __post_init__(self) -> None:
            pass

    assert _has_behavioral_structured_annotation(PostInitDataclass) is True

    UndefinedClass = Any  # noqa: N806

    class UndefinedRefModel(BaseModel):
        val: UndefinedClass

    resolved = _owner_annotations(UndefinedRefModel)
    assert "val" in resolved

    assert _annotation_contains_runtime_behavior("UndefinedClass", seen=set()) is True


def test_runtime_nested_cardinality_and_prune_coverage() -> None:
    from pydantic_versions._runtime import (
        _convert_nested_child_family,
        _convert_nested_family_payload,
        _prune_nested_family_metadata_at_path,
    )

    class HashableDict(dict[str, Any]):
        def __hash__(self) -> int:
            return hash(frozenset(self.items()))

    class ChildModel(BaseModel):
        val: int = 1

    fam = SchemaFamily(
        model=ChildModel,
        name="cardinality-child",
        versions=(SchemaVersion("v1"), SchemaVersion("v2")),
        transitions=(
            VersionTransition(
                "v1",
                "v2",
                upgrade=lambda p: HashableDict({"schema_version": "v2", "val": 0}),
                downgrade=lambda p: p,
                downgrade_semantics="exact",
            ),
        ),
    )

    item1 = HashableDict({"schema_version": "v1", "val": 1})
    item2 = HashableDict({"schema_version": "v1", "val": 2})

    # _convert_nested_child_family set & frozenset
    with pytest.raises(InvalidMigrationError, match="cannot preserve set cardinality"):
        _convert_nested_child_family(
            payload={item1, item2},
            path=(),
            family=fam,
            source_label="v1",
            target_label="v2",
        )

    with pytest.raises(InvalidMigrationError, match="cannot preserve set cardinality"):
        _convert_nested_child_family(
            payload=frozenset({item1, item2}),
            path=(),
            family=fam,
            source_label="v1",
            target_label="v2",
        )

    # _convert_nested_family_payload set & frozenset
    with pytest.raises(InvalidMigrationError, match="cannot preserve set cardinality"):
        _convert_nested_family_payload(
            family=fam,
            payload={item1, item2},
            source_label="v1",
            target_label="v2",
        )

    with pytest.raises(InvalidMigrationError, match="cannot preserve set cardinality"):
        _convert_nested_family_payload(
            family=fam,
            payload=frozenset({item1, item2}),
            source_label="v1",
            target_label="v2",
        )

    # _prune_nested_family_metadata_at_path with tuple, set, frozenset
    item_meta = HashableDict({"schema_version": "v1", "val": 1})
    _prune_nested_family_metadata_at_path(
        payload=(item_meta,),
        path=(),
        family=fam,
    )
    _prune_nested_family_metadata_at_path(
        payload={item_meta},
        path=(),
        family=fam,
    )


def test_runtime_more_pruning_and_nested_collection_kinds() -> None:
    from pydantic_versions._runtime import (
        _apply_nested_family_migrations,
        _nested_family_collection_kind,
        _prune_nested_family_metadata,
        _prune_nested_family_metadata_at_path,
        _prune_nested_family_metadata_payload,
    )

    class HashableDict(dict[str, Any]):
        def __hash__(self) -> int:
            return hash(frozenset(self.items()))

    class ChildModel(BaseModel):
        val: int = 1

    child_fam = SchemaFamily(
        model=ChildModel,
        name="prune-child",
        versions=(SchemaVersion("v1"), SchemaVersion("v2")),
    )

    class ParentModel(BaseModel):
        items: list[ChildModel]

    parent_fam = SchemaFamily(
        model=ParentModel,
        name="prune-parent",
        versions=(SchemaVersion("v1"), SchemaVersion("v2")),
        nested=(NestedFamily("items", lambda: child_fam, matching_labels()),),
    )

    parent_compiled = parent_fam._compiled_family()

    # _apply_nested_family_migrations same source and target label
    res = _apply_nested_family_migrations(
        payload={"items": []},
        compiled=parent_compiled,
        source_label="v1",
        target_label="v1",
    )
    assert res == {"items": []}

    # _nested_family_collection_kind 2-level path
    kind = _nested_family_collection_kind(
        model=ParentModel,
        path=("items", "val"),
    )
    assert kind is None

    # _prune_nested_family_metadata with nested
    payload = {"items": [{"schema_version": "v1", "val": 1}]}
    _prune_nested_family_metadata(payload=payload, compiled=parent_compiled)

    # _prune_nested_family_metadata_payload with nested
    _prune_nested_family_metadata_payload(payload, parent_compiled)

    # _prune_nested_family_metadata_at_path set & frozenset multi-level path
    item = HashableDict({"val": 1})
    _prune_nested_family_metadata_at_path(
        payload={item},
        path=("wrapper", "val"),
        family=child_fam,
    )
    _prune_nested_family_metadata_at_path(
        payload=frozenset({item}),
        path=("wrapper", "val"),
        family=child_fam,
    )


def test_wire_decorators_have_behavior() -> None:
    from pydantic import field_serializer, field_validator, model_validator

    from pydantic_versions._wire import _decorators_have_behavior

    class ValModel(BaseModel):
        val: int

        @field_validator("val")
        @classmethod
        def check_val(cls, v: int) -> int:
            return v

    class SerModel(BaseModel):
        val: int

        @field_serializer("val")
        def ser_val(self, v: int) -> int:
            return v

    class ModValModel(BaseModel):
        val: int

        @model_validator(mode="after")
        def check_model(self) -> Any:
            return self

    assert _decorators_have_behavior(ValModel) is True
    assert _decorators_have_behavior(SerModel) is True
    assert _decorators_have_behavior(ModValModel) is True
    assert _decorators_have_behavior(int) is False


def test_compiler_nested_family_declaration_validation_errors() -> None:

    from pydantic_versions._compiler import _validate_compilation_boundary

    class ModelA(BaseModel):
        val: int = 1

    class ModelB(BaseModel):
        child: ModelA

    fam_a = SchemaFamily(model=ModelA, name="fam-a", versions=(SchemaVersion("v1"),))

    # Invalid provider
    with pytest.raises(
        SchemaCompilationError,
        match="must use a SchemaFamily or callable provider",
    ):
        _validate_compilation_boundary(
            model=ModelB,
            name="fam-b",
            labels=("v1",),
            nested=(NestedFamily("child", cast(Any, "invalid_provider"), matching_labels()),),
        )

    # Invalid versions mapping
    decl = NestedFamily("child", fam_a, matching_labels())
    object.__setattr__(decl, "versions", "not_mapping")
    with pytest.raises(SchemaCompilationError, match="must be a mapping"):
        _validate_compilation_boundary(
            model=ModelB,
            name="fam-b",
            labels=("v1",),
            nested=(decl,),
        )


def test_runtime_list_tuple_unchanged_conversions() -> None:
    from pydantic_versions._runtime import (
        _convert_nested_child_family,
        _convert_nested_family_payload,
    )

    class ChildModel(BaseModel):
        val: int = 1

    fam = SchemaFamily(
        model=ChildModel,
        name="unchanged-child",
        versions=(SchemaVersion("v1"),),
    )

    item = {"schema_version": "v1", "val": 1}

    # list unchanged
    res_list1 = _convert_nested_child_family(
        payload=[item],
        path=(),
        family=fam,
        source_label="v1",
        target_label="v1",
    )
    assert res_list1 == [item]

    res_list2 = _convert_nested_family_payload(
        family=fam,
        payload=[item],
        source_label="v1",
        target_label="v1",
    )
    assert res_list2 == [item]

    # tuple unchanged
    res_tup1 = _convert_nested_child_family(
        payload=(item,),
        path=(),
        family=fam,
        source_label="v1",
        target_label="v1",
    )
    assert res_tup1 == (item,)
    res_tup2 = _convert_nested_family_payload(
        family=fam,
        payload=(item,),
        source_label="v1",
        target_label="v1",
    )
    assert res_tup2 == (item,)


def test_runtime_nested_migration_none_transitions_and_metadata_alias() -> None:
    from pydantic import Field

    from pydantic_versions._runtime import (
        _convert_nested_family_payload,
        _infer_metadata_owner,
    )

    class ModelWithAlias(BaseModel):
        my_field: str = Field(alias="ver")

    assert _infer_metadata_owner(ModelWithAlias, "ver") == "model"

    class ChildModel(BaseModel):
        val: int = 1

    fam = SchemaFamily(
        model=ChildModel,
        name="none-trans-child",
        versions=(SchemaVersion("v1"), SchemaVersion("v2"), SchemaVersion("v3")),
        transitions=(
            VersionTransition(
                "v1",
                "v2",
                upgrade=None,
                downgrade=lambda p: p,
                downgrade_semantics="exact",
            ),
            VersionTransition(
                "v2",
                "v3",
                upgrade=lambda p: p,
                downgrade=None,
                downgrade_semantics=None,
            ),
        ),
    )

    item = {"schema_version": "v1", "val": 1}

    # upgrade v1 -> v3 with None upgrade
    res_up = _convert_nested_family_payload(
        family=fam,
        payload=item,
        source_label="v1",
        target_label="v3",
    )
    assert res_up is not None

    item3 = {"schema_version": "v3", "val": 1}
    # downgrade v3 -> v1 with None downgrade
    res_down = _convert_nested_family_payload(
        family=fam,
        payload=item3,
        source_label="v3",
        target_label="v1",
    )
    assert res_down is not None


def test_compiler_patch_validation_errors() -> None:

    from pydantic_versions._compiler import _snapshot_field_default, _validate_patches
    from pydantic_versions.patches import FieldDefault

    class Base(BaseModel):
        val: int = 1

    with pytest.raises(SchemaCompilationError, match="Unsupported patch declaration"):
        _validate_patches(Base, "v1", (cast(Any, "not_a_patch"),))

    with pytest.raises(SchemaCompilationError, match="Patch field names for version"):
        _validate_patches(Base, "v1", (FieldDefault(name="", default=1),))

    with pytest.raises(SchemaCompilationError, match="cannot be copied into the compiled plan"):
        _snapshot_field_default(FieldDefault(name="val", default=CopyError()), version="v1")


def test_wire_grouped_metadata_and_schema_hook_type_alias() -> None:
    from typing import Annotated

    from annotated_types import GroupedMetadata

    class CustomGroupedMetadata(GroupedMetadata):
        def __iter__(self) -> Any:
            yield self

    GroupedAlias = TypeAliasType(  # noqa: N806, UP040
        "GroupedAlias", Annotated[int, CustomGroupedMetadata()]
    )

    class ModelGrouped(BaseModel):
        val: GroupedAlias

    fam_grouped = SchemaFamily(
        model=ModelGrouped,
        name="grouped-alias",
        versions=(SchemaVersion("v1"),),
    )
    with pytest.raises(
        UnsupportedWireModelError, match="executable metadata hidden in a type alias"
    ):
        fam_grouped.model_for("v1")

    class CustomSchemaHook:
        def __get_pydantic_core_schema__(self, source_type: Any, handler: Any) -> Any:
            return handler(source_type)

    HookAlias = TypeAliasType(  # noqa: N806, UP040
        "HookAlias", Annotated[int, CustomSchemaHook()]
    )

    class ModelHook(BaseModel):
        val: HookAlias

    fam_hook = SchemaFamily(
        model=ModelHook,
        name="hook-alias",
        versions=(SchemaVersion("v1"),),
    )
    with pytest.raises(
        UnsupportedWireModelError, match="custom schema metadata hidden in a type alias"
    ):
        fam_hook.model_for("v1")


def test_wire_decorator_child_mismatch_and_set_element() -> None:
    from pydantic_versions._wire import _set_element_wire_model

    class FrozenModel(BaseModel):
        model_config = ConfigDict(frozen=True)
        val: int

    assert _set_element_wire_model(FrozenModel) is FrozenModel
