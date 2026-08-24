from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum, IntEnum
from typing import Annotated, Any, Literal, Self, cast
from uuid import UUID

import pytest
from pydantic import (
    AfterValidator,
    AliasChoices,
    AliasPath,
    AnyUrl,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainValidator,
    SecretBytes,
    SecretStr,
    ValidationError,
    WrapValidator,
    computed_field,
    create_model,
    field_serializer,
    model_serializer,
    model_validator,
)

from pydantic_versions import (
    InvalidMigrationError,
    NestedFamily,
    SchemaFamily,
    SchemaVersion,
    UnsupportedWireModelError,
    VersionTransition,
    field_removed,
    matching_labels,
)


def _transition_family(
    model: type[BaseModel],
    name: str,
    callback: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    semantics: Literal["exact", "lossy"] = "exact",
    wire_model: type[BaseModel] | None = None,
) -> SchemaFamily[Any]:
    return SchemaFamily(
        model=model,
        name=name,
        versions=(SchemaVersion("1", wire_model=wire_model), SchemaVersion("2")),
        transitions=(
            VersionTransition(
                "1",
                "2",
                upgrade=callback,
                downgrade=callback,
                downgrade_semantics=semantics,
            ),
        ),
        version_metadata=None,
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


def test_adapted_source_revalidation_preserves_native_state_before_projection() -> None:
    class Mode(Enum):
        active = "active"

    mutate_revalidated_state = False
    revalidation_events: list[str] = []

    class Child(BaseModel):
        value: int

    class ExtendedChild(Child):
        extension: str

    class HistoricalPayload(BaseModel):
        model_config = ConfigDict(
            extra="allow",
            revalidate_instances="always",
            strict=True,
            use_enum_values=True,
        )

        public: int
        defaulted: int = 2
        mode: Mode
        child: Child
        state: dict[str, list[int]]
        internal: int = Field(0, exclude=True)
        conditional: int = Field(0, exclude_if=lambda value: value == 3)

        @model_validator(mode="before")
        @classmethod
        def mutate_nested_state(cls, value: Any) -> Any:
            if mutate_revalidated_state:
                revalidation_events.append("before")
                assert isinstance(value, dict)
                value["state"]["items"].append(2)
            return value

    class CurrentPayload(BaseModel):
        model_config = ConfigDict(strict=True, use_enum_values=True)

        public: int
        defaulted: int
        mode: Mode
        child: Child
        state: dict[str, list[int]]

    seen: list[dict[str, Any]] = []

    def inspect(data: dict[str, Any]) -> dict[str, Any]:
        seen.append(dict(data))
        return data

    family = _transition_family(
        CurrentPayload,
        "adapted_source_full_state",
        inspect,
        wire_model=HistoricalPayload,
    )
    child = ExtendedChild(value=1, extension="child-only")
    source = HistoricalPayload.model_validate(
        {
            "public": 1,
            "mode": Mode.active,
            "child": child,
            "state": {"items": [1]},
            "internal": 2,
            "conditional": 3,
            "extension": "source-only",
        },
    )
    original_fields_set = set(source.model_fields_set)
    mutate_revalidated_state = True

    result = family.validate(source, version="1")
    source_model = cast(HistoricalPayload, result.source_model)

    assert source_model.internal == 2
    assert source_model.conditional == 3
    assert source_model.__pydantic_extra__ == {"extension": "source-only"}
    assert source_model.child is child
    assert source_model.state == {"items": [1, 2]}
    assert source_model.model_fields_set == original_fields_set
    assert seen == [
        {
            "public": 1,
            "defaulted": 2,
            "mode": "active",
            "child": {"value": 1},
            "state": {"items": [1, 2]},
        },
    ]
    assert revalidation_events == ["before"]
    assert source.internal == 2
    assert source.conditional == 3
    assert source.child is child
    assert source.state == {"items": [1]}


@pytest.mark.filterwarnings("ignore:Default value .* is not JSON serializable")
def test_schema_only_serializer_computed_and_json_input_types_do_not_adapt_validation() -> None:
    class Mode(Enum):
        active = "active"

    class Rendered(BaseModel):
        model_config = ConfigDict(frozen=True)

        children: tuple[Self, ...] = ()

    events: list[str] = []

    class Payload(BaseModel):
        value: Annotated[
            int,
            PlainValidator(int, json_schema_input_type=set[Rendered]),
        ]
        payload: Any = {
            "type": "set",
            "items_schema": Rendered.__pydantic_core_schema__,
        }

        def __new__(cls, *args: Any, **kwargs: Any) -> Self:
            events.append("new")
            return super().__new__(cls)

        @field_serializer("value", return_type=Mode)
        def serialize_value(self, _value: int) -> Mode:
            events.append("serializer")
            return Mode.active

        @computed_field(return_type=set[Rendered])
        @property
        def rendered(self) -> set[Rendered]:
            return set()

    family = SchemaFamily(
        model=Payload,
        name="validation_ignores_schema_only_children",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )

    result = family.validate({"value": 1}, version="1")

    assert result.current_model.value == 1
    assert result.current_model.payload["type"] == "set"
    assert events == ["new"]


@pytest.mark.parametrize("operation", ["validate", "render"])
@pytest.mark.parametrize("override_kind", ["model_validate", "validator_proxy"])
def test_adapted_validation_rejects_runtime_validator_overrides_after_cache_warm(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    override_kind: str,
) -> None:
    class Mode(Enum):
        active = "active"

    class HistoricalPayload(BaseModel):
        model_config = ConfigDict(strict=True, use_enum_values=True)

        item: Mode

    class CurrentPayload(BaseModel):
        model_config = ConfigDict(
            revalidate_instances="always",
            strict=True,
            use_enum_values=True,
        )

        item: Mode

    family = _transition_family(
        CurrentPayload,
        f"runtime_validator_override_{operation}_{override_kind}",
        lambda data: data,
        wire_model=HistoricalPayload,
    )
    historical = HistoricalPayload(item=Mode.active)
    current = CurrentPayload(item=Mode.active)
    family.validate(historical, version="1")
    family.dump(version="2", data=current)
    events: list[str] = []

    if override_kind == "model_validate":
        original = CurrentPayload.model_validate

        def overridden(_cls: type[CurrentPayload], value: Any, **kwargs: Any) -> Any:
            events.append("model_validate")
            return original(value, **kwargs)

        monkeypatch.setattr(CurrentPayload, "model_validate", classmethod(overridden))
        CurrentPayload.model_validate({"item": Mode.active})
        match = "overridden model_validate"
    else:
        delegate = CurrentPayload.__pydantic_validator__

        class ValidatorProxy:
            def validate_python(self, *args: Any, **kwargs: Any) -> Any:
                events.append("validator_proxy")
                return delegate.validate_python(*args, **kwargs)

            def __getattr__(self, name: str) -> Any:
                return getattr(delegate, name)

        monkeypatch.setattr(CurrentPayload, "__pydantic_validator__", ValidatorProxy())
        CurrentPayload.model_validate({"item": Mode.active})
        match = "wrapped __pydantic_validator__"
    assert len(events) == 1
    events.clear()

    with pytest.raises(UnsupportedWireModelError, match=match):
        if operation == "validate":
            family.validate(historical, version="1")
        else:
            family.dump(version="2", data=current)
    assert events == []

    if override_kind == "model_validate":
        monkeypatch.setattr(
            CurrentPayload,
            "model_validate",
            BaseModel.__dict__["model_validate"],
        )
    else:
        monkeypatch.setattr(CurrentPayload, "__pydantic_validator__", delegate)
    if operation == "validate":
        assert family.validate(historical, version="1").current_model.item == "active"
    else:
        assert family.dump(version="2", data=current) == {"item": "active"}


@pytest.mark.parametrize("wire_kind", ["explicit", "generated"])
def test_historical_subclass_fields_stay_outside_canonical_payload(wire_kind: str) -> None:
    class HistoricalPayload(BaseModel):
        model_config = ConfigDict(extra="allow")

        value: int

    class CurrentPayload(BaseModel):
        model_config = ConfigDict(extra="allow")

        value: int

    seen: list[dict[str, Any]] = []

    def inspect(data: dict[str, Any]) -> dict[str, Any]:
        seen.append(dict(data))
        return data

    family = _transition_family(
        CurrentPayload,
        f"historical_subclass_boundary_{wire_kind}",
        inspect,
        wire_model=HistoricalPayload if wire_kind == "explicit" else None,
    )
    declared_source = HistoricalPayload if wire_kind == "explicit" else family.model_for("1")
    extended_source = create_model(
        f"ExtendedHistoricalPayload_{wire_kind}",
        __base__=declared_source,
        secret=(str, ...),
    )
    source: Any = extended_source.model_validate(
        {"value": 1, "secret": "caller", "extension": "source-only"}
    )

    result = family.validate(source, version="1")

    assert seen == [{"value": 1}]
    assert source.secret == "caller"
    assert source.__pydantic_extra__ == {"extension": "source-only"}
    assert result.source_model is source
    assert result.current_model.__pydantic_extra__ == {}


def test_declared_extraction_preserves_validated_python_values() -> None:
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
        secret_bytes: SecretBytes
        labels: tuple[Mode, ...]
        indexes: set[int]
        schedule: dict[datetime, Mode]
        lookup: dict[UUID, UUID]

    class CurrentScalarPayload(BaseModel):
        model_config = ConfigDict(strict=True)

        occurred_at: Any
        endpoint: Any
        mode: Mode
        raw: bytes
        amount: Any
        secret: SecretStr
        secret_bytes: SecretBytes
        labels: tuple[Mode, ...]
        indexes: set[int]
        schedule: Any
        lookup: dict[UUID, UUID]

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
        transitions=(
            VersionTransition(
                "1",
                "2",
                upgrade=inspect_payload,
                downgrade=inspect_payload,
                downgrade_semantics="exact",
            ),
        ),
        version_metadata=None,
    )
    source_data = {
        "occurred_at": "2020-01-02T03:04:05Z",
        "endpoint": "https://example.com/config",
        "mode": "active",
        "raw": "_wA=",
        "amount": "1.20",
        "secret": "private",
        "secret_bytes": b"private-bytes",
        "labels": ["active"],
        "indexes": [2, 1],
        "schedule": {"2020-01-02T03:04:05Z": "active"},
        "lookup": {"12345678-1234-5678-1234-567812345678": "12345678-1234-5678-1234-567812345678"},
    }
    validated = family.validate(source_data, version="1")
    rendered = family.dump(version="1", data=validated.current_model)

    assert len(seen_payloads) == 2
    for payload in seen_payloads:
        assert payload["occurred_at"] == datetime(2020, 1, 2, 3, 4, 5, tzinfo=UTC)
        assert str(payload["endpoint"]) == "https://example.com/config"
        assert payload["mode"] is Mode.active
        assert payload["raw"] == b"\xff\x00"
        assert payload["amount"] == Decimal("1.20")
        assert payload["secret"].get_secret_value() == "private"
        assert payload["secret_bytes"].get_secret_value() == b"private-bytes"
        assert payload["labels"] == (Mode.active,)
        assert payload["indexes"] == {1, 2}
        assert payload["schedule"] == {
            datetime(2020, 1, 2, 3, 4, 5, tzinfo=UTC): Mode.active,
        }
        assert payload["lookup"] == {
            UUID("12345678-1234-5678-1234-567812345678"): UUID(
                "12345678-1234-5678-1234-567812345678"
            ),
        }
    assert rendered["secret"] == "**********"
    assert rendered["secret_bytes"] == "**********"


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
    family.validate(source_data, version="1")

    assert seen_payloads == [source_data]


def test_use_enum_values_keeps_raw_values_when_a_strict_declared_arm_accepts_them() -> None:
    class Mode(Enum):
        active = "active"

    class Number(IntEnum):
        one = 1

    validator_events: list[str] = []
    reject_strings = False

    def enum_after(value: Mode) -> Mode:
        validator_events.append("enum")
        return value

    def string_after(value: str) -> str:
        validator_events.append("string")
        if reject_strings:
            raise ValueError("string arm rejected")
        return value

    class Payload(BaseModel):
        model_config = ConfigDict(strict=True, use_enum_values=True)

        direct: (
            Annotated[Mode, AfterValidator(enum_after)]
            | Annotated[str, AfterValidator(string_after)]
        )
        listed: list[Mode] | list[str]
        literal: Literal[Mode.active, "active"]
        literal_list: list[Literal[Mode.active, "active"]]
        literal_key: dict[Literal[Mode.active, "active"], int]
        number: Number | int
        boolean: Number | bool

    seen: list[dict[str, Any]] = []

    def inspect(data: dict[str, Any]) -> dict[str, Any]:
        seen.append(dict(data))
        return data

    family = _transition_family(
        Payload,
        "raw_enum_union_arms",
        inspect,
    )
    source = {
        "direct": Mode.active,
        "listed": [Mode.active],
        "literal": Mode.active,
        "literal_list": [Mode.active],
        "literal_key": {Mode.active: 1},
        "number": Number.one,
        "boolean": True,
    }

    validated = family.validate(source, version="1").current_model
    family.dump(version="1", data=Payload.model_validate(source))

    assert validated.model_dump() == {
        "direct": "active",
        "listed": ["active"],
        "literal": "active",
        "literal_list": ["active"],
        "literal_key": {"active": 1},
        "number": 1,
        "boolean": True,
    }
    assert seen == [validated.model_dump(), validated.model_dump()]
    assert validator_events == ["string", "enum"]

    reject_strings = True
    validator_events.clear()
    with pytest.raises(ValidationError, match="string arm rejected"):
        family.validate(source, version="1")
    assert validator_events == ["string"]


def test_strict_enum_unions_keep_application_validator_outputs_once() -> None:
    class Mode(Enum):
        active = "active"

    class HistoricalPayload(BaseModel):
        model_config = ConfigDict(strict=True, use_enum_values=True)

        scalar: Mode
        wrapped: Mode
        listed: list[Mode]
        mapped: dict[str, Mode]

    events: list[str] = []

    def transform_string(value: str) -> str:
        events.append("scalar")
        return f"{value}!"

    def transform_list(value: list[str]) -> list[str]:
        events.append("list")
        return [*value, "extra"]

    def transform_mapping(value: dict[str, str]) -> dict[str, str]:
        events.append("mapping")
        return {**value, "extra": "added"}

    def transform_wrap(value: Any, handler: Any) -> str:
        events.append("wrap")
        return f"{handler(value)}?"

    class CurrentPayload(BaseModel):
        model_config = ConfigDict(strict=True, use_enum_values=True)

        scalar: Mode | Annotated[str, AfterValidator(transform_string)]
        wrapped: Mode | Annotated[str, WrapValidator(transform_wrap)]
        listed: list[Mode] | Annotated[list[str], AfterValidator(transform_list)]
        mapped: (
            dict[str, Mode]
            | Annotated[
                dict[str, str],
                AfterValidator(transform_mapping),
            ]
        )

    family = _transition_family(
        CurrentPayload,
        "strict_enum_union_application_output",
        lambda data: data,
        wire_model=HistoricalPayload,
    )
    source = HistoricalPayload(
        scalar=Mode.active,
        wrapped=Mode.active,
        listed=[Mode.active],
        mapped={"value": Mode.active},
    )
    events.clear()

    validated = family.validate(source, version="1").current_model

    assert validated.scalar == "active!"
    assert validated.wrapped == "active?"
    assert validated.listed == ["active", "extra"]
    assert validated.mapped == {"value": "active", "extra": "added"}
    assert events == ["scalar", "wrap", "list", "mapping"]


def test_strict_enum_union_keeps_cyclic_application_output_once() -> None:
    class Mode(Enum):
        active = "active"

    class EnumArm(BaseModel):
        model_config = ConfigDict(strict=True, use_enum_values=True)

        value: Mode

    class HistoricalPayload(BaseModel):
        item: EnumArm

    events: list[str] = []

    def make_cycle(value: Any) -> Any:
        events.append("cycle")
        assert isinstance(value, dict)
        value["self"] = value
        return value

    class CurrentPayload(BaseModel):
        item: EnumArm | Annotated[Any, AfterValidator(make_cycle)]

    family = _transition_family(
        CurrentPayload,
        "strict_enum_union_cyclic_application_output",
        lambda data: data,
        wire_model=HistoricalPayload,
    )
    source = HistoricalPayload(item=EnumArm(value=Mode.active))
    events.clear()

    validated = family.validate(source, version="1").current_model

    assert isinstance(validated.item, dict)
    assert validated.item["self"] is validated.item
    assert events == ["cycle"]
    assert source.item.value == "active"


def test_strict_enum_union_keeps_nested_model_application_output_once() -> None:
    class Mode(Enum):
        active = "active"

    class HistoricalArm(BaseModel):
        model_config = ConfigDict(strict=True, use_enum_values=True)

        value: Mode

    class HistoricalPayload(BaseModel):
        item: HistoricalArm

    events: list[str] = []

    def to_bytes(value: str) -> bytes:
        events.append("after")
        return value.encode()

    class EnumArm(BaseModel):
        model_config = ConfigDict(strict=True, use_enum_values=True)

        value: Mode

    class StringArm(BaseModel):
        model_config = ConfigDict(strict=True)

        value: Annotated[str, AfterValidator(to_bytes)]

    class CurrentPayload(BaseModel):
        item: EnumArm | StringArm

    raw = {"item": {"value": "active"}}
    native = CurrentPayload.model_validate(raw)
    assert isinstance(native.item, StringArm)
    assert native.item.value == b"active"
    events.clear()

    family = _transition_family(
        CurrentPayload,
        "strict_enum_union_nested_model_application_output",
        lambda data: data,
        wire_model=HistoricalPayload,
    )
    source = HistoricalPayload(item=HistoricalArm(value=Mode.active))

    validated = family.validate(source, version="1").current_model

    assert isinstance(validated.item, StringArm)
    assert validated.item.value == b"active"
    assert events == ["after"]
    assert source.item.value == "active"


def test_callback_created_cycle_uses_native_any_union_arm() -> None:
    class Mode(Enum):
        active = "active"

    class EnumArm(BaseModel):
        model_config = ConfigDict(strict=True, use_enum_values=True)

        value: Mode

    class HistoricalPayload(BaseModel):
        item: EnumArm

    class CurrentPayload(BaseModel):
        item: EnumArm | Any

    def add_cycle(data: dict[str, Any]) -> dict[str, Any]:
        item = data["item"]
        item["self"] = item
        return data

    family = _transition_family(
        CurrentPayload,
        "strict_enum_union_callback_cycle",
        add_cycle,
        wire_model=HistoricalPayload,
    )
    source = HistoricalPayload(item=EnumArm(value=Mode.active))

    validated = family.validate(source, version="1").current_model

    assert isinstance(validated.item, dict)
    assert validated.item["self"] is validated.item
    assert source.item.value == "active"


def test_strict_enum_unions_keep_before_and_plain_validator_results_once() -> None:
    class Mode(Enum):
        active = "active"

    class HistoricalPayload(BaseModel):
        model_config = ConfigDict(strict=True, use_enum_values=True)

        before: Mode
        plain: Mode
        nested_after: list[Mode]
        nested_before: list[Mode]
        nested_plain: list[Mode]
        unselected: Mode
        nested_unselected: list[Mode]

    events: list[str] = []

    def select_before(_value: Any) -> int:
        events.append("before")
        return 7

    def select_plain(_value: Any) -> int:
        events.append("plain")
        return 8

    def select_nested_after(value: str) -> bytes:
        events.append("nested_after")
        return value.encode()

    def select_nested_before(_value: Any) -> int:
        events.append("nested_before")
        return 10

    def select_nested_plain(_value: Any) -> int:
        events.append("nested_plain")
        return 11

    def lose_to_exact_string(_value: Any) -> str:
        events.append("unselected")
        return "not-an-integer"

    def lose_nested_to_exact_strings(_value: Any) -> str:
        events.append("nested_unselected")
        return "not-an-integer"

    class CurrentPayload(BaseModel):
        model_config = ConfigDict(strict=True, use_enum_values=True)

        before: Mode | Annotated[int, BeforeValidator(select_before)]
        plain: Mode | Annotated[int, PlainValidator(select_plain)]
        nested_after: list[Mode] | list[Annotated[str, AfterValidator(select_nested_after)]]
        nested_before: list[Mode] | list[Annotated[int, BeforeValidator(select_nested_before)]]
        nested_plain: list[Mode] | list[Annotated[int, PlainValidator(select_nested_plain)]]
        unselected: Mode | Annotated[int, BeforeValidator(lose_to_exact_string)] | str
        nested_unselected: (
            list[Mode]
            | list[Annotated[int, BeforeValidator(lose_nested_to_exact_strings)]]
            | list[str]
        )

    raw_values = {
        "before": "active",
        "plain": "active",
        "nested_after": ["active"],
        "nested_before": ["active"],
        "nested_plain": ["active"],
        "unselected": "active",
        "nested_unselected": ["active"],
    }
    assert CurrentPayload.model_validate(raw_values).model_dump() == {
        "before": 7,
        "plain": 8,
        "nested_after": [b"active"],
        "nested_before": [10],
        "nested_plain": [11],
        "unselected": "active",
        "nested_unselected": ["active"],
    }
    events.clear()
    family = _transition_family(
        CurrentPayload,
        "strict_enum_union_before_plain_output",
        lambda data: data,
        wire_model=HistoricalPayload,
    )
    source = HistoricalPayload(
        before=Mode.active,
        plain=Mode.active,
        nested_after=[Mode.active],
        nested_before=[Mode.active],
        nested_plain=[Mode.active],
        unselected=Mode.active,
        nested_unselected=[Mode.active],
    )

    validated = family.validate(source, version="1").current_model

    assert validated.model_dump() == {
        "before": 7,
        "plain": 8,
        "nested_after": [b"active"],
        "nested_before": [10],
        "nested_plain": [11],
        "unselected": "active",
        "nested_unselected": ["active"],
    }
    assert events == [
        "before",
        "plain",
        "nested_after",
        "nested_before",
        "nested_plain",
        "unselected",
        "nested_unselected",
    ]


def test_wrap_cannot_swallow_carrier_cardinality_violation() -> None:
    class Child(BaseModel):
        model_config = ConfigDict(frozen=True)

        value: int

    class HistoricalPayload(BaseModel):
        items: set[Child]

    events: list[str] = []

    def waive_rejected_set(value: Any, handler: Any) -> Any:
        events.append("wrap")
        try:
            handler(value)
        except ValidationError:
            return {"waived"}
        raise ValueError("reject the set arm after parsing")

    class DirectPayload(BaseModel):
        items: Annotated[
            set[Child],
            WrapValidator(waive_rejected_set),
            WrapValidator(waive_rejected_set),
        ]

    class UnionPayload(BaseModel):
        items: Annotated[set[Child], WrapValidator(waive_rejected_set)] | tuple[Child, ...]

    def collapse(data: dict[str, Any]) -> dict[str, Any]:
        items = list(data["items"])
        for item in items:
            item["value"] = 0
        return {**data, "items": items}

    direct_family = _transition_family(
        DirectPayload,
        "direct_wrap_cardinality_failure",
        collapse,
        wire_model=HistoricalPayload,
    )
    union_family = _transition_family(
        UnionPayload,
        "union_wrap_cardinality_fallback",
        collapse,
        wire_model=HistoricalPayload,
    )
    source = HistoricalPayload(items={Child(value=1), Child(value=2)})

    with pytest.raises(InvalidMigrationError, match="set cardinality"):
        direct_family.validate(source, version="1")
    assert events == ["wrap", "wrap"]

    events.clear()
    validated = union_family.validate(source, version="1").current_model

    assert isinstance(validated.items, tuple)
    assert [item.value for item in validated.items] == [0, 0]
    assert events == ["wrap"]
    assert {item.value for item in source.items} == {1, 2}


def test_reentrant_canonical_validation_isolates_application_frames() -> None:
    class Child(BaseModel):
        model_config = ConfigDict(frozen=True)

        value: int

    class InnerPayload(BaseModel):
        items: set[Child]

    def collapse(data: dict[str, Any]) -> dict[str, Any]:
        items = list(data["items"])
        for item in items:
            item["value"] = 0
        return {**data, "items": items}

    inner_family = _transition_family(
        InnerPayload,
        "reentrant_inner_cardinality",
        collapse,
        wire_model=InnerPayload,
    )
    inner_source = InnerPayload(items={Child(value=1), Child(value=2)})

    class Mode(Enum):
        active = "active"

    class HistoricalPayload(BaseModel):
        model_config = ConfigDict(strict=True, use_enum_values=True)

        item: Mode

    events: list[str] = []

    def run_inner(value: str) -> str:
        events.append("outer")
        with pytest.raises(InvalidMigrationError, match="cardinality"):
            inner_family.validate(inner_source, version="1")
        events.append("caught")
        return f"{value}!"

    class CurrentPayload(BaseModel):
        model_config = ConfigDict(strict=True, use_enum_values=True)

        item: Mode | Annotated[str, AfterValidator(run_inner)]

    family = _transition_family(
        CurrentPayload,
        "reentrant_outer_application",
        lambda data: data,
        wire_model=HistoricalPayload,
    )

    validated = family.validate(
        HistoricalPayload(item=Mode.active),
        version="1",
    ).current_model

    assert validated.item == "active!"
    assert events == ["outer", "caught"]
    assert {item.value for item in inner_source.items} == {1, 2}


def test_adapted_validation_preserves_authoritative_error_config_and_union_locations() -> None:
    class Mode(Enum):
        active = "active"

    class HistoricalPayload(BaseModel):
        item: Any
        count: int

    class CurrentPayload(BaseModel):
        model_config = ConfigDict(
            hide_input_in_errors=True,
            strict=True,
            title="Public Current Payload",
            use_enum_values=True,
        )

        item: Mode | int
        count: int = Field(gt=0)

    family = _transition_family(
        CurrentPayload,
        "canonical_error_contract",
        lambda data: data,
        wire_model=HistoricalPayload,
    )
    raw = {"item": "bad", "count": -1}

    with pytest.raises(ValidationError) as native_error:
        CurrentPayload.model_validate(raw)
    with pytest.raises(ValidationError) as family_error:
        family.validate(raw, version="1")

    native = native_error.value
    adapted = family_error.value
    assert adapted.title == native.title == "Public Current Payload"
    assert [error["loc"] for error in adapted.errors()] == [
        error["loc"] for error in native.errors()
    ]
    assert "input_value" not in str(adapted)
    assert "input_type" not in str(adapted)


@pytest.mark.parametrize("operation", ["validate", "render"])
def test_strict_enum_unions_fail_closed_before_changing_stored_python_types(
    operation: str,
) -> None:
    class Number(IntEnum):
        one = 1

    class Payload(BaseModel):
        model_config = ConfigDict(strict=True, use_enum_values=True)

        number: Number | float
        reversed_literal: Literal[1.0, Number.one]  # ty: ignore[invalid-type-form]
        boolean: Literal[1] | bool
        listed: list[Number | float]
        keyed: dict[Number | float, str]

    class HistoricalPayload(Payload):
        pass

    seen: list[dict[str, Any]] = []

    def inspect(data: dict[str, Any]) -> dict[str, Any]:
        seen.append(dict(data))
        return data

    family = _transition_family(
        Payload,
        "strict_enum_python_type_preservation",
        inspect,
        wire_model=HistoricalPayload,
    )
    canonical_values = {
        "number": Number.one,
        "reversed_literal": Number.one,
        "boolean": True,
        "listed": [Number.one],
        "keyed": {Number.one: "value"},
    }
    # These are the authoritative values Pydantic stores after validating the
    # enum-bearing inputs. model_construct avoids a second native Literal pass
    # changing bool to equal int before the canonical seam is exercised.
    HistoricalPayload.model_validate(canonical_values)
    stored_values: dict[str, Any] = {
        "number": 1,
        "reversed_literal": 1,
        "boolean": True,
        "listed": [1],
        "keyed": {1: "value"},
    }
    with pytest.raises(InvalidMigrationError, match="stored enum value"):
        if operation == "validate":
            family.validate(
                HistoricalPayload.model_construct(**stored_values),
                version="1",
            )
        else:
            family.dump(
                version="1",
                data=Payload.model_construct(**stored_values),
            )

    assert len(seen) == 1
    payload = seen[0]
    assert type(payload["number"]) is int
    assert type(payload["reversed_literal"]) is int
    assert type(payload["boolean"]) is bool
    assert type(payload["listed"][0]) is int
    assert type(next(iter(payload["keyed"]))) is int


@pytest.mark.parametrize("operation", ["validate", "render"])
def test_callback_wrong_fixed_tuple_arity_uses_pydantic_validation_error(
    operation: str,
) -> None:
    class Payload(BaseModel):
        values: tuple[int, int]

    def remove_item(data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "values": (data["values"][0],)}

    family = _transition_family(
        Payload,
        f"fixed_tuple_arity_{operation}",
        remove_item,
        semantics="lossy",
    )

    with pytest.raises(ValidationError, match="Field required"):
        if operation == "validate":
            family.validate({"values": (1, 2)}, version="1")
        else:
            family.dump(version="1", data=Payload(values=(1, 2)))


@pytest.mark.parametrize("operation", ["validate", "render"])
@pytest.mark.parametrize("values", [(1,), (1, 2, 3)])
def test_malformed_fixed_tuple_instance_uses_authoritative_validation(
    operation: str,
    values: tuple[int, ...],
) -> None:
    class Payload(BaseModel):
        model_config = ConfigDict(revalidate_instances="always")

        value: tuple[int, int]

    family = SchemaFamily(
        model=Payload,
        name=f"malformed_fixed_tuple_{operation}_{len(values)}",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )
    source_model = family.model_for("1") if operation == "validate" else Payload
    source: Any = source_model.model_construct(value=values)

    with pytest.raises(ValidationError) as exc_info:
        if operation == "validate":
            family.validate(source, version="1")
        else:
            family.dump(version="1", data=source)

    assert exc_info.value.errors()[0]["type"] == ("missing" if len(values) == 1 else "too_long")
    assert source.value == values


def test_cyclic_revalidated_model_fails_closed_without_recursing() -> None:
    class Node(BaseModel):
        model_config = ConfigDict(revalidate_instances="always")

        value: int
        child: Self | None = None

    family = SchemaFamily(
        model=Node,
        name="cyclic_revalidated_model",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )
    source = Node(value=1)
    source.child = source

    with pytest.raises(ValidationError) as native_error:
        Node.model_validate(source)
    assert native_error.value.errors()[0]["type"] == "recursion_loop"

    with pytest.raises(ValidationError) as family_error:
        family.dump(version="1", data=source)
    assert family_error.value.errors()[0]["type"] == "recursion_loop"

    assert source.child is source


@pytest.mark.parametrize("operation", ["validate", "render"])
def test_callback_mutation_cannot_hide_set_or_mapping_key_collapse(operation: str) -> None:
    class Child(BaseModel):
        model_config = ConfigDict(frozen=True)

        value: int

    collapse_carriers = False

    def collapse_hash_positions(data: dict[str, Any]) -> dict[str, Any]:
        if collapse_carriers:
            children = list(data["children"]) if operation == "render" else tuple(data["children"])
            for child in children:
                child["value"] = 0
            data["children"] = children
            for child in data["lookup"]:
                child["value"] = 0
            del data["required"]
        data["ordinary"] = (item for item in (3,))
        return data

    class Payload(BaseModel):
        children: set[Child]
        lookup: dict[Child, int]
        ordinary: set[int]
        required: int

    family = _transition_family(
        Payload,
        f"callback_hash_collapse_{operation}",
        collapse_hash_positions,
        semantics="lossy",
    )
    current = Payload(
        children={Child(value=1), Child(value=2)},
        lookup={Child(value=3): 1, Child(value=4): 2},
        ordinary={1, 2},
        required=1,
    )
    source = family.model_for("1").model_construct(
        children=current.children,
        lookup=current.lookup,
        ordinary=current.ordinary,
        required=current.required,
    )
    if operation == "render":
        assert family.dump(version="1", data=current)["ordinary"] == [3]
    else:
        assert family.validate(source, version="1").current_model.ordinary == {3}

    collapse_carriers = True
    with pytest.raises(InvalidMigrationError, match="cardinality") as exc_info:
        if operation == "render":
            family.dump(version="1", data=current)
        else:
            family.validate(source, version="1")

    errors = cast(ValidationError, exc_info.value.__cause__).errors()
    assert {error["loc"] for error in errors} == {
        ("children",),
        ("lookup",),
        ("required",),
    }
    assert {child.value for child in current.children} == {1, 2}
    assert {child.value for child in current.lookup} == {3, 4}


def test_hash_carriers_unwrap_at_non_hash_any_targets_without_leaking() -> None:
    class Child(BaseModel):
        model_config = ConfigDict(frozen=True)

        value: int

    class HistoricalPayload(BaseModel):
        items: set[Child]
        nested: set[Child]
        choice: set[Child]
        opaque: set[Child]

    class CurrentPayload(BaseModel):
        items: list[Any]
        nested: dict[str, list[Any]]
        choice: list[Any] | tuple[Any, ...]
        opaque: Any

    def reshape(data: dict[str, Any]) -> dict[str, Any]:
        opaque_item = next(iter(data["opaque"]))
        cycle: list[Any] = []
        cycle.append(cycle)
        return {
            "items": list(data["items"]),
            "nested": {"values": list(data["nested"])},
            "choice": list(data["choice"]),
            "opaque": {
                "list": [opaque_item],
                "tuple": (opaque_item,),
                "cycle": cycle,
            },
        }

    family = _transition_family(
        CurrentPayload,
        "non_hash_any_carrier_unwrap",
        reshape,
        wire_model=HistoricalPayload,
    )
    source = HistoricalPayload(
        items={Child(value=1)},
        nested={Child(value=2)},
        choice={Child(value=3)},
        opaque={Child(value=4)},
    )

    current = family.validate(source, version="1").current_model

    assert current.items == [{"value": 1}]
    assert current.nested == {"values": [{"value": 2}]}
    assert isinstance(current.choice, list)
    assert current.choice == [{"value": 3}]
    assert current.opaque["list"] == [{"value": 4}]
    assert current.opaque["tuple"] == ({"value": 4},)
    assert current.opaque["cycle"][0] is current.opaque["cycle"]
    assert all(type(item) is dict for item in current.items)
    assert type(current.nested["values"][0]) is dict
    assert type(current.choice[0]) is dict
    assert type(current.opaque["list"][0]) is dict
    assert type(current.opaque["tuple"][0]) is dict


def test_carrier_unwrap_preserves_sibling_sets_and_unrelated_hash_values() -> None:
    class Child(BaseModel):
        model_config = ConfigDict(frozen=True)

        value: int

    class HistoricalPayload(BaseModel):
        items: set[Child]
        tags: set[int]
        opaque: set[str]

    class CurrentPayload(BaseModel):
        payload: Any
        opaque: set[Any]

    def reshape(data: dict[str, Any]) -> dict[str, Any]:
        return {
            "payload": {
                "projected": next(iter(data["items"])),
                "tags": data["tags"],
            },
            "opaque": data["opaque"],
        }

    family = _transition_family(
        CurrentPayload,
        "carrier_unwrap_sibling_sets",
        reshape,
        wire_model=HistoricalPayload,
    )
    source = HistoricalPayload(
        items={Child(value=1)},
        tags={2, 3},
        opaque={"keep"},
    )

    current = family.validate(source, version="1").current_model

    assert current.payload == {
        "projected": {"value": 1},
        "tags": {2, 3},
    }
    assert type(current.payload["projected"]) is dict
    assert type(current.payload["tags"]) is set
    assert current.opaque == {"keep"}
    assert type(current.opaque) is set
    assert source.items == {Child(value=1)}
    assert source.tags == {2, 3}
    assert source.opaque == {"keep"}


def test_nested_mapping_key_carrier_fails_before_unhashable_any_materialization() -> None:
    class Child(BaseModel):
        model_config = ConfigDict(frozen=True)

        value: int

    class HistoricalPayload(BaseModel):
        keyed: dict[Child, int]

    class CurrentPayload(BaseModel):
        items: list[Any]

    family = _transition_family(
        CurrentPayload,
        "nested_mapping_key_carrier_unwrap",
        lambda data: {"items": [data["keyed"]]},
        wire_model=HistoricalPayload,
    )
    source = HistoricalPayload(keyed={Child(value=1): 2})

    with pytest.raises(InvalidMigrationError, match="private hash carrier"):
        family.validate(source, version="1")

    assert source.keyed == {Child(value=1): 2}


def test_hash_carriers_fail_closed_at_opaque_hash_positions() -> None:
    class Child(BaseModel):
        model_config = ConfigDict(frozen=True)

        value: int

    class HistoricalPayload(BaseModel):
        items: set[Child]
        opaque: set[Child]

    class CurrentPayload(BaseModel):
        items: set[Any]
        opaque: Any

    family = _transition_family(
        CurrentPayload,
        "opaque_hash_carrier_boundary",
        lambda data: data,
        wire_model=HistoricalPayload,
    )
    source = HistoricalPayload(
        items={Child(value=1)},
        opaque={Child(value=2)},
    )

    with pytest.raises(InvalidMigrationError, match="private hash carrier"):
        family.validate(source, version="1")
    assert {item.value for item in source.items} == {1}
    assert {item.value for item in source.opaque} == {2}


@pytest.mark.parametrize("position", ["set", "mapping_key"])
@pytest.mark.parametrize("validator_kind", ["before", "plain", "wrap"])
def test_opaque_hash_positions_reject_carriers_before_application_validators(
    position: str,
    validator_kind: str,
) -> None:
    class Child(BaseModel):
        model_config = ConfigDict(frozen=True)

        value: int

    seen: list[Any] = []

    def observe(value: Any) -> Any:
        seen.append(value)
        return value

    def observe_wrap(value: Any, handler: Any) -> Any:
        seen.append(value)
        return handler(value)

    if validator_kind == "before":
        opaque = Annotated[Any, BeforeValidator(observe)]
    elif validator_kind == "plain":
        opaque = Annotated[Any, PlainValidator(observe)]
    else:
        opaque = Annotated[Any, WrapValidator(observe_wrap)]
    historical_annotation = set[Child] if position == "set" else dict[Child, int]
    current_annotation = set[opaque] if position == "set" else dict[opaque, int]
    historical_payload = create_model(
        f"HistoricalOpaqueHash{position}{validator_kind}",
        value=(historical_annotation, ...),
    )
    current_payload = create_model(
        f"CurrentOpaqueHash{position}{validator_kind}",
        value=(current_annotation, ...),
    )
    family = _transition_family(
        current_payload,
        f"opaque_hash_{position}_{validator_kind}",
        lambda data: data,
        wire_model=historical_payload,
    )
    source_value = {Child(value=1)} if position == "set" else {Child(value=1): 1}
    source = historical_payload.model_validate({"value": source_value})

    with pytest.raises(InvalidMigrationError, match="private hash carrier"):
        family.validate(source, version="1")
    assert seen == []


@pytest.mark.parametrize("operation", ["validate", "render"])
@pytest.mark.parametrize("position", ["set", "mapping_key"])
@pytest.mark.parametrize("validator_kind", ["before", "wrap"])
def test_outer_collection_validators_cannot_observe_or_swallow_private_carriers(
    operation: str,
    position: str,
    validator_kind: str,
) -> None:
    class Child(BaseModel):
        model_config = ConfigDict(frozen=True)

        value: int

    seen: list[Any] = []

    def observe(value: Any) -> Any:
        seen.append(value)
        return value

    def swallow(value: Any, handler: Any) -> Any:
        seen.append(value)
        try:
            return handler(value)
        except ValidationError:
            return set() if position == "set" else {}

    typed_annotation = set[Child] if position == "set" else dict[Child, int]
    opaque_container = set[Any] if position == "set" else dict[Any, int]
    outer_validator = (
        BeforeValidator(observe) if validator_kind == "before" else WrapValidator(swallow)
    )
    opaque_annotation = Annotated[opaque_container, outer_validator]
    historical_annotation = opaque_annotation if operation == "render" else typed_annotation
    current_annotation = typed_annotation if operation == "render" else opaque_annotation
    historical_payload = create_model(
        f"HistoricalOuterOpaque{operation}{position}{validator_kind}",
        value=(historical_annotation, ...),
    )
    current_payload = create_model(
        f"CurrentOuterOpaque{operation}{position}{validator_kind}",
        value=(current_annotation, ...),
    )
    family = _transition_family(
        current_payload,
        f"outer_opaque_{operation}_{position}_{validator_kind}",
        lambda data: data,
        wire_model=historical_payload,
    )
    source_value = {Child(value=1)} if position == "set" else {Child(value=1): 1}

    with pytest.raises(InvalidMigrationError, match="private hash carrier"):
        if operation == "render":
            family.dump(
                version="1",
                data=current_payload.model_validate({"value": source_value}),
            )
        else:
            family.validate(
                historical_payload.model_validate({"value": source_value}),
                version="1",
            )
    assert seen == []
