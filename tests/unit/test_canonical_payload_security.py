from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Literal, cast

import pytest
from pydantic import (
    AliasChoices,
    AliasPath,
    AnyUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_serializer,
    model_serializer,
)

from pydantic_versions import (
    NestedFamily,
    SchemaFamily,
    SchemaVersion,
    VersionTransition,
    field_removed,
    matching_labels,
)


class ExcludedPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    public: str
    internal: bool = Field(False, exclude=True)


class ConditionallyExcludedPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    public: str
    internal: bool = Field(False, exclude_if=lambda value: value is False)


@pytest.mark.parametrize(
    "model_cls",
    [ExcludedPayload, ConditionallyExcludedPayload],
    ids=["exclude", "exclude_if"],
)
def test_allowed_extras_cannot_populate_excluded_current_fields(
    model_cls: type[BaseModel],
) -> None:
    family = SchemaFamily(
        model=model_cls,
        name=f"excluded_extra_{model_cls.__name__}",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )
    result = family.validate(
        {
            "public": "visible",
            "internal": True,
            "extension": {"owner": "source"},
        },
        version="1",
    )

    assert result.source_model.__pydantic_extra__ == {
        "internal": True,
        "extension": {"owner": "source"},
    }
    assert cast(Any, result.current_model).internal is False
    assert result.current_model.__pydantic_extra__ == {}


def test_removed_fields_cannot_reenter_through_names_or_validation_aliases() -> None:
    class RemovedAliasPayload(BaseModel):
        model_config = ConfigDict(
            extra="allow",
            validate_by_alias=True,
            validate_by_name=True,
        )

        direct: int = 0
        plain: int = Field(0, alias="plainAlias")
        choice: int = Field(
            0,
            validation_alias=AliasChoices("choiceFirst", "choiceSecond"),
        )
        path: int = Field(0, validation_alias=AliasPath("envelope", "path"))

    family = SchemaFamily(
        model=RemovedAliasPayload,
        name="removed_alias_extras",
        versions=(
            SchemaVersion(
                "1",
                patches=tuple(field_removed(name) for name in RemovedAliasPayload.model_fields),
            ),
            SchemaVersion("2"),
        ),
        version_metadata=None,
    )
    attack = {
        "direct": 1,
        "plain": 2,
        "plainAlias": 3,
        "choice": 4,
        "choiceSecond": 5,
        "path": 6,
        "envelope": {"path": 7},
        "extension": "source-only",
    }

    result = family.validate(attack, version="1")

    assert result.source_model.__pydantic_extra__ == attack
    assert result.current_model.direct == 0
    assert result.current_model.plain == 0
    assert result.current_model.choice == 0
    assert result.current_model.path == 0
    assert result.current_model.__pydantic_extra__ == {}


def test_upgrade_can_deliberately_introduce_a_removed_field() -> None:
    seen_payloads: list[dict[str, Any]] = []

    class UpgradePayload(BaseModel):
        model_config = ConfigDict(extra="allow")

        public: str
        internal: bool = False

    def add_internal(payload: dict[str, Any]) -> dict[str, Any]:
        seen_payloads.append(dict(payload))
        return {**payload, "internal": True}

    family = SchemaFamily(
        model=UpgradePayload,
        name="trusted_removed_field_upgrade",
        versions=(
            SchemaVersion("1", patches=(field_removed("internal"),)),
            SchemaVersion("2"),
        ),
        transitions=(VersionTransition("1", "2", upgrade=add_internal),),
        version_metadata=None,
    )
    result = family.validate(
        {"public": "visible", "internal": False, "extension": "source-only"},
        version="1",
    )

    assert result.source_model.__pydantic_extra__ == {
        "internal": False,
        "extension": "source-only",
    }
    assert seen_payloads == [{"public": "visible"}]
    assert result.current_model.internal is True


def test_nested_automatic_projections_drop_excluded_and_removed_extras() -> None:
    class ChildPayload(BaseModel):
        model_config = ConfigDict(extra="allow")

        value: int
        removed: bool = False

    child_family = SchemaFamily(
        model=ChildPayload,
        name="nested_extra_child",
        versions=(
            SchemaVersion("1", patches=(field_removed("removed"),)),
            SchemaVersion("2"),
        ),
        version_metadata=None,
    )

    class InnerPayload(BaseModel):
        model_config = ConfigDict(extra="allow")

        public: str
        internal: bool = Field(False, exclude=True)
        child: ChildPayload

    class RootPayload(BaseModel):
        inner: InnerPayload

    family = SchemaFamily(
        model=RootPayload,
        name="nested_omitted_extras",
        versions=(SchemaVersion("1"), SchemaVersion("2")),
        nested=(NestedFamily(("inner", "child"), child_family, matching_labels()),),
        version_metadata=None,
    )
    result = family.validate(
        {
            "inner": {
                "public": "visible",
                "internal": True,
                "extension": "inner-source-only",
                "child": {
                    "value": 1,
                    "removed": True,
                    "extension": "child-source-only",
                },
            },
        },
        version="1",
    )

    source_inner = cast(Any, result.source_model).inner
    assert source_inner.__pydantic_extra__ == {
        "internal": True,
        "extension": "inner-source-only",
    }
    assert source_inner.child.__pydantic_extra__ == {
        "removed": True,
        "extension": "child-source-only",
    }
    assert result.current_model.inner.internal is False
    assert result.current_model.inner.child.removed is False
    assert result.current_model.inner.__pydantic_extra__ == {}
    assert result.current_model.inner.child.__pydantic_extra__ == {}


@pytest.mark.parametrize("extra_mode", [None, "ignore"], ids=["default", "ignore"])
def test_ignored_excluded_input_keeps_the_current_default(
    extra_mode: Literal["ignore"] | None,
) -> None:
    config = ConfigDict() if extra_mode is None else ConfigDict(extra=extra_mode)

    class IgnoredPayload(BaseModel):
        model_config = config

        public: str
        internal: bool = Field(False, exclude=True)

    family = SchemaFamily(
        model=IgnoredPayload,
        name=f"ignored_excluded_{extra_mode}",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )

    result = family.validate(
        {"public": "visible", "internal": True},
        version="1",
    )

    assert result.current_model.internal is False


def test_forbidden_excluded_input_remains_rejected() -> None:
    class ForbiddenPayload(BaseModel):
        model_config = ConfigDict(extra="forbid")

        public: str
        internal: bool = Field(False, exclude=True)

    family = SchemaFamily(
        model=ForbiddenPayload,
        name="forbidden_excluded",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        family.validate(
            {"public": "visible", "internal": True},
            version="1",
        )


def test_extra_allow_without_omitted_fields_remains_a_source_wire_policy() -> None:
    class ExtensiblePayload(BaseModel):
        model_config = ConfigDict(extra="allow")

        value: int

    family = SchemaFamily(
        model=ExtensiblePayload,
        name="source_wire_extensions",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )
    result = family.validate(
        {"value": 1, "extension": {"owner": "source"}},
        version="1",
    )

    assert result.source_model.__pydantic_extra__ == {
        "extension": {"owner": "source"},
    }
    assert result.current_model.value == 1
    assert result.current_model.__pydantic_extra__ == {}


def test_source_serializers_do_not_define_the_canonical_transition_payload() -> None:
    field_serializer_calls = 0
    model_serializer_calls = 0
    seen_payloads: list[dict[str, Any]] = []

    class HistoricalWire(BaseModel):
        model_config = ConfigDict(extra="allow")

        value: int

        @field_serializer("value")
        def serialize_value(self, value: int) -> str:
            nonlocal field_serializer_calls
            field_serializer_calls += 1
            return f"serialized:{value}"

        @model_serializer
        def serialize_model(self) -> dict[str, str]:
            nonlocal model_serializer_calls
            model_serializer_calls += 1
            return {"value": f"model-serialized:{self.value}"}

    class CurrentPayload(BaseModel):
        value: int

    def inspect_payload(payload: dict[str, Any]) -> dict[str, Any]:
        seen_payloads.append(dict(payload))
        return payload

    family = SchemaFamily(
        model=CurrentPayload,
        name="serializer_free_source_extraction",
        versions=(
            SchemaVersion("1", wire_model=HistoricalWire),
            SchemaVersion("2"),
        ),
        transitions=(VersionTransition("1", "2", upgrade=inspect_payload),),
        version_metadata=None,
    )
    result = family.validate(
        {"value": 1, "extension": "source-only"},
        version="1",
    )

    assert field_serializer_calls == 0
    assert model_serializer_calls == 0
    assert seen_payloads == [{"value": 1}]
    assert result.current_model.value == 1
    assert result.source_model.__pydantic_extra__ == {"extension": "source-only"}


def test_declared_extraction_preserves_the_previous_json_scalar_shape() -> None:
    class Mode(Enum):
        active = "active"

    seen_payloads: list[dict[str, Any]] = []

    class HistoricalScalarWire(BaseModel):
        model_config = ConfigDict(
            ser_json_bytes="base64",
            ser_json_temporal="milliseconds",
            val_json_bytes="base64",
            val_temporal_unit="milliseconds",
        )

        occurred_at: datetime
        endpoint: AnyUrl
        mode: Mode
        raw: bytes
        amount: Decimal
        secret: SecretStr
        labels: tuple[Mode, ...]
        indexes: set[int]
        schedule: dict[datetime, Mode]

    class CurrentScalarPayload(BaseModel):
        occurred_at: Any
        endpoint: Any
        mode: Any
        raw: Any
        amount: Any
        secret: Any
        labels: Any
        indexes: Any
        schedule: Any

    def inspect_payload(payload: dict[str, Any]) -> dict[str, Any]:
        seen_payloads.append(dict(payload))
        return payload

    family = SchemaFamily(
        model=CurrentScalarPayload,
        name="json_scalar_compatibility",
        versions=(
            SchemaVersion("1", wire_model=HistoricalScalarWire),
            SchemaVersion("2"),
        ),
        transitions=(VersionTransition("1", "2", upgrade=inspect_payload),),
        version_metadata=None,
    )
    source_data = {
        "occurred_at": "2020-01-02T03:04:05Z",
        "endpoint": "https://example.com/config",
        "mode": "active",
        "raw": "_wA=",
        "amount": "1.20",
        "secret": "private",
        "labels": ["active"],
        "indexes": [2, 1],
        "schedule": {"2020-01-02T03:04:05Z": "active"},
    }
    expected = (
        family.model_for("1")
        .model_validate(source_data)
        .model_dump(
            by_alias=False,
            mode="json",
        )
    )

    family.validate(source_data, version="1")

    assert seen_payloads == [expected]
    assert seen_payloads == [
        {
            "occurred_at": 1_577_934_245_000.0,
            "endpoint": "https://example.com/config",
            "mode": "active",
            "raw": "_wA=",
            "amount": "1.20",
            "secret": "**********",
            "labels": ["active"],
            "indexes": [1, 2],
            "schedule": {"1577934245000": "active"},
        },
    ]


def test_legacy_timedelta_mode_does_not_change_other_temporal_scalars() -> None:
    seen_payloads: list[dict[str, Any]] = []

    class HistoricalDurationWire(BaseModel):
        model_config = ConfigDict(ser_json_timedelta="float")

        occurred_at: datetime
        duration: timedelta

    class CurrentDurationPayload(BaseModel):
        occurred_at: Any
        duration: Any

    def inspect_payload(payload: dict[str, Any]) -> dict[str, Any]:
        seen_payloads.append(dict(payload))
        return payload

    family = SchemaFamily(
        model=CurrentDurationPayload,
        name="legacy_timedelta_compatibility",
        versions=(
            SchemaVersion("1", wire_model=HistoricalDurationWire),
            SchemaVersion("2"),
        ),
        transitions=(VersionTransition("1", "2", upgrade=inspect_payload),),
        version_metadata=None,
    )
    source_data = {
        "occurred_at": datetime(2020, 1, 2, 3, 4, 5, tzinfo=UTC),
        "duration": timedelta(seconds=1.5),
    }
    expected = (
        family.model_for("1")
        .model_validate(source_data)
        .model_dump(
            by_alias=False,
            mode="json",
        )
    )

    family.validate(source_data, version="1")

    assert seen_payloads == [expected]
    assert seen_payloads == [
        {
            "occurred_at": "2020-01-02T03:04:05Z",
            "duration": 1.5,
        },
    ]
