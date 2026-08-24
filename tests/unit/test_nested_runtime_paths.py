from enum import Enum
from typing import Annotated, Any, Self, TypedDict, cast, get_args
from uuid import UUID

import pytest
from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from pydantic_versions import (
    InvalidMigrationError,
    NestedFamily,
    SchemaFamily,
    SchemaVersion,
    SchemaVersionError,
    VersionMetadata,
    VersionTransition,
    dump_versioned,
    field_renamed,
    matching_labels,
    validate_versioned,
    versioned_schema,
)


@pytest.mark.parametrize("opaque_version", ["1", "2"])
def test_nested_runtime_does_not_search_unrelated_mapping_values(
    opaque_version: str,
) -> None:
    class Child(BaseModel):
        value: int

    child_family = SchemaFamily(
        model=Child,
        name="exact_path_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                upgrade=lambda data: {**data, "value": data["value"] + 10},
            ),
        ),
    )

    class Parent(BaseModel):
        child: Child | None = None
        opaque: dict[str, Any]

    parent_family = SchemaFamily(
        model=Parent,
        name="exact_path_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        nested=(NestedFamily("child", child_family, matching_labels()),),
    )
    opaque = {"child": {"schema_version": opaque_version, "value": 1}}

    result = parent_family.validate(
        {"schema_version": "1", "opaque": opaque},
    )

    assert result.current_model.child is None
    assert result.current_model.opaque == opaque


def test_generated_nested_enum_values_are_trusted_but_explicit_structures_are_not() -> None:
    class Mode(Enum):
        active = "active"

    class Child(BaseModel):
        model_config = ConfigDict(strict=True, use_enum_values=True)

        mode: Mode

    child_family = SchemaFamily(
        model=Child,
        name="nested_enum_trust_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(VersionTransition("1", "2", upgrade=lambda data: data),),
        version_metadata=None,
    )

    class Parent(BaseModel):
        child: Child

    generated_parent = SchemaFamily(
        model=Parent,
        name="generated_nested_enum_trust_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(VersionTransition("1", "2", upgrade=lambda data: data),),
        nested=(NestedFamily("child", child_family, matching_labels()),),
        version_metadata=None,
    )

    assert (
        generated_parent.validate(
            {"child": {"mode": Mode.active}},
            version="1",
        ).current_model.child.mode
        == "active"
    )

    class HistoricalChild(TypedDict):
        mode: str

    class HistoricalParent(BaseModel):
        child: HistoricalChild

    explicit_parent = SchemaFamily(
        model=Parent,
        name="explicit_nested_enum_trust_parent",
        versions=(
            SchemaVersion("1", wire_model=HistoricalParent),
            SchemaVersion("2"),
        ),
        transitions=(VersionTransition("1", "2", upgrade=lambda data: data),),
        nested=(NestedFamily("child", child_family, matching_labels()),),
        version_metadata=None,
    )

    with pytest.raises(ValidationError, match="instance of .*Mode"):
        explicit_parent.validate(
            {"child": {"mode": "active"}},
            version="1",
        )


def test_nested_projection_and_pruning_ignore_an_opaque_sibling() -> None:
    class Child(BaseModel):
        value: int

    child_family = SchemaFamily(
        model=Child,
        name="exact_projection_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    )

    class Parent(BaseModel):
        children: list[Child] | None = None
        opaque: dict[str, Any]

    parent_family = SchemaFamily(
        model=Parent,
        name="exact_projection_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        nested=(NestedFamily("children", child_family, matching_labels()),),
    )
    opaque = {"children": [{"schema_version": "2", "value": 1}]}

    rendered = parent_family.dump(
        version="2",
        data={"opaque": opaque},
    )

    assert rendered["children"] is None
    assert rendered["opaque"] == opaque


def test_nested_paths_cross_multiple_homogeneous_collection_layers() -> None:
    class Child(BaseModel):
        value: int

    child_family = SchemaFamily(
        model=Child,
        name="multi_collection_layer_child",
        versions=(
            SchemaVersion("1", patches=(field_renamed("value", "legacy_value"),)),
            SchemaVersion("2"),
        ),
        version_metadata=None,
    )

    class Wrapper(BaseModel):
        child: Child

    class Parent(BaseModel):
        wrappers: list[list[Wrapper]]

    parent_family = SchemaFamily(
        model=Parent,
        name="multi_collection_layer_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        nested=(NestedFamily(("wrappers", "child"), child_family, matching_labels()),),
        version_metadata=None,
    )
    historical_payload = {
        "wrappers": [[{"child": {"legacy_value": 7}}]],
    }

    result = parent_family.validate(historical_payload, version="1")

    assert result.current_model.wrappers[0][0].child.value == 7
    assert parent_family.dump(version="1", data=result.current_model) == historical_payload


def test_nested_metadata_preflight_uses_second_alias_choice() -> None:
    class Child(BaseModel):
        value: int

    child_family = SchemaFamily(
        model=Child,
        name="alias_choice_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    )

    class Parent(BaseModel):
        child: Child = Field(validation_alias=AliasChoices("preferred", "fallback"))

    parent_family = SchemaFamily(
        model=Parent,
        name="alias_choice_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        nested=(NestedFamily("child", child_family, matching_labels()),),
    )

    with pytest.raises(SchemaVersionError, match="expects version '1'"):
        parent_family.validate(
            {
                "schema_version": "1",
                "fallback": {"schema_version": "2", "value": 1},
            },
        )


def test_nested_metadata_preflight_follows_alias_path_list_index() -> None:
    class Child(BaseModel):
        value: int

    child_family = SchemaFamily(
        model=Child,
        name="alias_path_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    )

    class Parent(BaseModel):
        child: Child = Field(validation_alias=AliasPath("envelope", 1))

    parent_family = SchemaFamily(
        model=Parent,
        name="alias_path_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        nested=(NestedFamily("child", child_family, matching_labels()),),
    )

    with pytest.raises(SchemaVersionError, match="expects version '1'"):
        parent_family.validate(
            {
                "schema_version": "1",
                "envelope": [None, {"schema_version": "2", "value": 1}],
            },
        )


@pytest.mark.parametrize(
    ("model_config", "child_payloads", "expected_value"),
    [
        (
            ConfigDict(validate_by_name=True, extra="ignore"),
            {
                "child": {"schema_version": "1", "value": 7},
                "serialized": {"schema_version": "2", "value": 99},
            },
            7,
        ),
        (
            ConfigDict(
                validate_by_alias=False,
                validate_by_name=True,
                extra="ignore",
            ),
            {
                "child": {"schema_version": "1", "value": 7},
                "accepted": {"schema_version": "2", "value": 98},
                "serialized": {"schema_version": "2", "value": 99},
            },
            7,
        ),
        (
            ConfigDict(
                validate_by_alias=True,
                validate_by_name=False,
                extra="ignore",
            ),
            {
                "accepted": {"schema_version": "1", "value": 7},
                "child": {"schema_version": "2", "value": 98},
                "serialized": {"schema_version": "2", "value": 99},
            },
            7,
        ),
        (
            ConfigDict(
                validate_by_alias=True,
                validate_by_name=True,
                extra="ignore",
            ),
            {
                "child": {"schema_version": "1", "value": 7},
                "accepted": {"schema_version": "2", "value": 99},
            },
            None,
        ),
    ],
    ids=["serialization-ignored", "name-only", "alias-only", "alias-precedence"],
)
def test_nested_metadata_preflight_honors_validation_alias_policy(
    model_config: ConfigDict,
    child_payloads: dict[str, dict[str, int | str]],
    expected_value: int | None,
) -> None:
    class Child(BaseModel):
        value: int

    child_family = SchemaFamily(
        model=Child,
        name="validation_alias_policy_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    )
    parent_config = model_config

    class Parent(BaseModel):
        model_config = parent_config

        child: Child = Field(alias="serialized", validation_alias="accepted")

    parent_family = SchemaFamily(
        model=Parent,
        name="validation_alias_policy_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        nested=(NestedFamily("child", child_family, matching_labels()),),
    )
    payload: dict[str, Any] = {"schema_version": "1", **child_payloads}
    if expected_value is None:
        with pytest.raises(SchemaVersionError, match="expects version '1'"):
            parent_family.validate(payload)
        return

    result = parent_family.validate(payload)
    assert result.current_model.child.value == expected_value


@pytest.mark.parametrize("model_input", [False, True], ids=["mapping", "unrelated-model"])
def test_nested_render_metadata_preflight_preserves_structural_alias_input(
    model_input: bool,
) -> None:
    class Child(BaseModel):
        value: int

    child_family = SchemaFamily(
        model=Child,
        name="structural_preflight_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
    )

    class Parent(BaseModel):
        child: Child = Field(validation_alias="accepted")

    parent_family = SchemaFamily(
        model=Parent,
        name="structural_preflight_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        nested=(NestedFamily("child", child_family, matching_labels()),),
    )

    class Envelope(BaseModel):
        accepted: dict[str, Any]

    payload = {"accepted": {"schema_version": "1", "value": 7}}
    data: dict[str, Any] | Envelope = Envelope.model_validate(payload) if model_input else payload

    with pytest.raises(SchemaVersionError, match="expects version '2'"):
        parent_family.dump(version="1", data=cast(Any, data))


@pytest.mark.parametrize("direction", ["render", "validate"])
@pytest.mark.parametrize("injected_kind", ["declared-subclass", "unrelated"])
def test_nested_route_handles_parent_injected_child_model(
    direction: str,
    injected_kind: str,
) -> None:
    class Child(BaseModel):
        model_config = ConfigDict(extra="allow")

        value: int

    class InjectedChild(Child):
        callback_only: bool = True

    class UnrelatedChild(BaseModel):
        value: int

    child_family = SchemaFamily(
        model=Child,
        name=f"injected_{direction}_child",
        versions=(SchemaVersion("1"), SchemaVersion("2"), SchemaVersion("3")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                upgrade=lambda data: {**data, "value": data["value"] + 1},
                downgrade=lambda data: {**data, "value": data["value"] - 1},
                downgrade_semantics="exact",
            ),
            VersionTransition(
                "2",
                "3",
                upgrade=lambda data: {**data, "value": data["value"] + 10},
                downgrade=lambda data: {**data, "value": data["value"] - 10},
                downgrade_semantics="exact",
            ),
        ),
        version_metadata=None,
    )

    class Parent(BaseModel):
        child: Child

    def inject_child(data: dict[str, Any]) -> dict[str, Any]:
        injected = (
            InjectedChild(value=100)
            if injected_kind == "declared-subclass"
            else UnrelatedChild(value=100)
        )
        return {**data, "child": injected}

    def identity(data: dict[str, Any]) -> dict[str, Any]:
        return data

    first_upgrade = inject_child if direction == "validate" else identity
    second_downgrade = inject_child if direction == "render" else identity
    parent_family = SchemaFamily(
        model=Parent,
        name=f"injected_{direction}_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2"), SchemaVersion("3")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                upgrade=first_upgrade,
                downgrade=identity,
                downgrade_semantics="exact",
            ),
            VersionTransition(
                "2",
                "3",
                upgrade=identity,
                downgrade=second_downgrade,
                downgrade_semantics="exact",
            ),
        ),
        nested=(NestedFamily("child", child_family, matching_labels()),),
        version_metadata=None,
    )

    if injected_kind == "unrelated":
        with pytest.raises(InvalidMigrationError, match="expected current model 'Child'"):
            if direction == "render":
                parent_family.dump(
                    version="1",
                    data=Parent(child=Child(value=50)),
                )
            else:
                parent_family.validate(
                    {"child": {"value": 50}},
                    version="1",
                )
        return

    if direction == "render":
        rendered = parent_family.dump(
            version="1",
            data=Parent(child=Child(value=50)),
        )
        assert rendered == {"child": {"value": 99}}
        return

    result = parent_family.validate(
        {"child": {"value": 50}},
        version="1",
    )

    assert result.current_model.child.value == 110
    assert result.current_model.child.__pydantic_extra__ == {}


@pytest.mark.parametrize("model_input", [False, True], ids=["mapping", "model"])
def test_nested_render_validates_parent_and_child_only_once(model_input: bool) -> None:
    validation_counts = {"parent": 0, "child": 0}
    transition_events: list[str] = []

    class Child(BaseModel):
        model_config = ConfigDict(revalidate_instances="always")

        current_value: int

        @model_validator(mode="after")
        def count_validation(self) -> Self:
            validation_counts["child"] += 1
            return self

    def downgrade_child(data: dict[str, Any]) -> dict[str, Any]:
        assert data == {"current_value": 5}
        transition_events.append("child")
        return data

    child_family = SchemaFamily(
        model=Child,
        name="once_only_child",
        versions=(
            SchemaVersion("1", patches=(field_renamed("current_value", "legacy_value"),)),
            SchemaVersion("2"),
        ),
        transitions=(
            VersionTransition(
                "1",
                "2",
                downgrade=downgrade_child,
                downgrade_semantics="exact",
            ),
        ),
    )

    class Parent(BaseModel):
        model_config = ConfigDict(revalidate_instances="always")

        child: Child

        @model_validator(mode="after")
        def count_validation(self) -> Self:
            validation_counts["parent"] += 1
            return self

    def downgrade_parent(data: dict[str, Any]) -> dict[str, Any]:
        assert data == {"child": {"current_value": 5}}
        transition_events.append("parent")
        return data

    parent_family = SchemaFamily(
        model=Parent,
        name="once_only_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                downgrade=downgrade_parent,
                downgrade_semantics="exact",
            ),
        ),
        nested=(NestedFamily("child", child_family, matching_labels()),),
    )
    mapping = {"child": {"current_value": 5}}
    if model_input:
        data: Parent | dict[str, Any] = Parent.model_validate(mapping)
        validation_counts.update(parent=0, child=0)
    else:
        data = mapping

    rendered = parent_family.dump(version="1", data=data)

    assert rendered == {
        "child": {"legacy_value": 5},
        "schema_version": "1",
    }
    assert validation_counts == {"parent": 1, "child": 1}
    assert transition_events == ["child", "parent"]


def test_nested_family_metadata_rejects_a_scalar_envelope_from_a_migration() -> None:
    class Payload(BaseModel):
        value: int

    family = SchemaFamily(
        model=Payload,
        name="scalar_metadata_envelope",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                upgrade=lambda data: data,
                downgrade=lambda data: {**data, "meta": "not-an-object"},
                downgrade_semantics="exact",
            ),
        ),
        version_metadata=VersionMetadata(("meta", "version"), owner="family"),
    )

    with pytest.raises(
        InvalidMigrationError,
        match=r"Cannot set version metadata at \('meta', 'version'\).*'meta'.*not an object",
    ):
        family.dump(version="1", data=Payload(value=7))


def test_alias_path_does_not_overwrite_a_scalar_intermediate_field() -> None:
    class Payload(BaseModel):
        model_config = ConfigDict(validate_by_name=True)

        envelope: str
        value: int = Field(validation_alias=AliasPath("envelope", "value"))

    family = SchemaFamily(
        model=Payload,
        name="scalar_alias_envelope",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )
    raw = {"envelope": "preserve-me", "value": 7}

    with pytest.raises(ValidationError) as exc_info:
        family.validate(raw, version="1")

    assert [(error["type"], error["loc"]) for error in exc_info.value.errors()] == [
        ("missing", ("envelope", "value")),
    ]
    assert raw == {"envelope": "preserve-me", "value": 7}


@pytest.mark.parametrize("exclusion", ["exclude", "exclude_if"])
def test_nested_families_preserve_strict_container_kinds_and_exclusions(
    exclusion: str,
) -> None:
    identifier = UUID("12345678-1234-5678-1234-567812345678")
    omitted = (
        Field("private", exclude=True)
        if exclusion == "exclude"
        else Field("private", exclude_if=lambda _value: True)
    )

    class Child(BaseModel):
        model_config = ConfigDict(strict=True, frozen=True)

        value: UUID
        secret: str = omitted

    child_family = SchemaFamily(
        model=Child,
        name=f"strict_python_collection_child_{exclusion}",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                upgrade=lambda data: data,
                downgrade=lambda data: data,
                downgrade_semantics="exact",
            ),
        ),
        version_metadata=None,
    )

    class Parent(BaseModel):
        model_config = ConfigDict(strict=True)

        tupled: tuple[Child, ...]
        set_values: set[Child]
        frozen_values: frozenset[Child]

    seen_payloads: list[dict[str, Any]] = []

    def inspect_payload(payload: dict[str, Any]) -> dict[str, Any]:
        seen_payloads.append(dict(payload))
        return payload

    family = SchemaFamily(
        model=Parent,
        name=f"strict_python_collection_parent_{exclusion}",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                upgrade=inspect_payload,
                downgrade=inspect_payload,
                downgrade_semantics="exact",
            ),
        ),
        nested=(
            NestedFamily("tupled", child_family, matching_labels()),
            NestedFamily("set_values", child_family, matching_labels()),
            NestedFamily("frozen_values", child_family, matching_labels()),
        ),
        version_metadata=None,
    )

    historical_parent = family.model_for("1")
    tuple_child = get_args(historical_parent.model_fields["tupled"].annotation)[0]
    set_child = get_args(historical_parent.model_fields["set_values"].annotation)[0]
    frozen_child = get_args(historical_parent.model_fields["frozen_values"].annotation)[0]
    historical = cast(Any, historical_parent)(
        tupled=(tuple_child(value=identifier),),
        set_values={set_child(value=identifier)},
        frozen_values=frozenset({frozen_child(value=identifier)}),
    )
    current = Parent(
        tupled=(Child(value=identifier),),
        set_values={Child(value=identifier)},
        frozen_values=frozenset({Child(value=identifier)}),
    )

    validated = family.validate(historical, version="1")
    rendered = family.dump(version="1", data=current)

    assert validated.current_model == current
    assert rendered == {
        "tupled": [{"value": str(identifier)}],
        "set_values": [{"value": str(identifier)}],
        "frozen_values": [{"value": str(identifier)}],
    }
    assert len(seen_payloads) == 2
    for payload in seen_payloads:
        assert type(payload["tupled"]) is tuple
        assert type(payload["set_values"]) is set
        assert type(payload["frozen_values"]) is frozenset
        for field_name in ("tupled", "set_values", "frozen_values"):
            item = next(iter(payload[field_name]))
            assert dict(item) == {"value": identifier}
            assert "secret" not in item

    with pytest.raises(InvalidMigrationError, match="cardinality"):
        family.dump(
            version="1",
            data=Parent(
                tupled=current.tupled,
                set_values={
                    Child(value=identifier, secret="first"),
                    Child(value=identifier, secret="second"),
                },
                frozen_values=current.frozen_values,
            ),
        )


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_invalid_nested_child_callback_result_names_the_failed_route(direction: str) -> None:
    class Child(BaseModel):
        value: int

    def invalid(_data: dict[str, Any]) -> dict[str, Any]:
        return cast(Any, "not-a-mapping")

    child_family = SchemaFamily(
        model=Child,
        name=f"invalid_{direction}_child",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                upgrade=invalid if direction == "upgrade" else lambda data: data,
                downgrade=invalid if direction == "downgrade" else lambda data: data,
                downgrade_semantics="exact",
            ),
        ),
    )

    class Parent(BaseModel):
        child: Child

    parent_family = SchemaFamily(
        model=Parent,
        name=f"invalid_{direction}_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        nested=(NestedFamily("child", child_family, matching_labels()),),
    )
    source, target = ("1", "2") if direction == "upgrade" else ("2", "1")

    with pytest.raises(
        InvalidMigrationError,
        match=rf"Nested migration '{source}' -> '{target}'.*'invalid_{direction}_child'.*dict",
    ):
        if direction == "upgrade":
            parent_family.validate(
                {"schema_version": "1", "child": {"schema_version": "1", "value": 1}},
            )
        else:
            parent_family.dump(version="1", data=Parent(child=Child(value=1)))


def test_decorator_infers_aliased_model_metadata_and_runs_identity_migration() -> None:
    @versioned_schema(
        name="aliased_model_metadata_owner",
        versions=("1", "2"),
        current="2",
        version_field="version",
    )
    class Payload(BaseModel):
        model_config = ConfigDict(validate_by_name=True)

        schema_version: str = Field("2", alias="version")
        value: int

    result = validate_versioned(Payload, {"version": "1", "value": 7})

    assert result.source_model.model_dump()["schema_version"] == "1"
    assert result.current_model == Payload(schema_version="2", value=7)
    assert result.migrations_applied == ()
    assert dump_versioned(Payload, version="1", data=result.current_model) == {
        "version": "1",
        "value": 7,
    }


def test_current_dump_inserts_nested_family_metadata() -> None:
    class Payload(BaseModel):
        value: int

    family = SchemaFamily(
        model=Payload,
        name="current_nested_metadata",
        versions=(SchemaVersion("1"),),
        version_metadata=VersionMetadata(("meta", "version"), owner="family"),
    )

    assert family.dump(version="1", data=Payload(value=7)) == {
        "value": 7,
        "meta": {"version": "1"},
    }


def test_invalid_top_level_downgrade_result_names_the_failed_route() -> None:
    class Payload(BaseModel):
        value: int

    family = SchemaFamily(
        model=Payload,
        name="invalid_top_level_downgrade",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                downgrade=lambda _data: cast(Any, "not-a-mapping"),
                downgrade_semantics="exact",
            ),
        ),
    )

    with pytest.raises(
        InvalidMigrationError,
        match=r"Migration '1' -> '2' downgrade must return a dict",
    ):
        family.dump(version="1", data=Payload(value=7))


def test_parent_migration_can_inject_python_nested_collections() -> None:
    class Child(BaseModel):
        model_config = ConfigDict(frozen=True)

        value: int

    child_family = SchemaFamily(
        model=Child,
        name="injected_collection_child",
        versions=(SchemaVersion("1"), SchemaVersion("2"), SchemaVersion("3")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                upgrade=lambda data: {"value": data["value"] + 10},
            ),
            VersionTransition(
                "2",
                "3",
                upgrade=lambda data: {"value": data["value"] + 100},
            ),
        ),
        version_metadata=None,
    )

    class Parent(BaseModel):
        tupled: Annotated[tuple[Child, ...], Field(description="tuple children")]
        set_values: Annotated[set[Child], Field(description="set children")]
        frozen_values: Annotated[
            frozenset[Child],
            Field(description="frozen children"),
        ]

    def inject_collections(data: dict[str, Any]) -> dict[str, Any]:
        return {
            **data,
            "tupled": (Child(value=20),),
            "set_values": {Child(value=30)},
            "frozen_values": frozenset({Child(value=40)}),
        }

    parent_family = SchemaFamily(
        model=Parent,
        name="injected_collection_parent",
        versions=(SchemaVersion("1"), SchemaVersion("2"), SchemaVersion("3")),
        transitions=(VersionTransition("1", "2", upgrade=inject_collections),),
        nested=(
            NestedFamily("tupled", child_family, matching_labels()),
            NestedFamily("set_values", child_family, matching_labels()),
            NestedFamily("frozen_values", child_family, matching_labels()),
        ),
        version_metadata=None,
    )

    result = parent_family.validate(
        {
            "tupled": [{"value": 1}],
            "set_values": [{"value": 2}],
            "frozen_values": [{"value": 3}],
        },
        version="1",
    )

    assert result.current_model.tupled == (Child(value=120),)
    assert result.current_model.set_values == {Child(value=130)}
    assert result.current_model.frozen_values == frozenset({Child(value=140)})
