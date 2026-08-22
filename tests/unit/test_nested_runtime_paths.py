from typing import Any, Self, cast

import pytest
from pydantic import AliasChoices, AliasPath, BaseModel, ConfigDict, Field, model_validator

from pydantic_versions import (
    InvalidMigrationError,
    NestedFamily,
    SchemaFamily,
    SchemaVersion,
    SchemaVersionError,
    VersionTransition,
    field_renamed,
    matching_labels,
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
        exclude_none=True,
    )

    assert rendered["opaque"] == opaque


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
        "child": {"legacy_value": 5, "schema_version": "1"},
        "schema_version": "1",
    }
    assert validation_counts == {"parent": 1, "child": 1}
    assert transition_events == ["child", "parent"]
