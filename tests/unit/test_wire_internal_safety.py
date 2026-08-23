from __future__ import annotations

from typing import Any, ForwardRef, cast

import pytest
from pydantic import BaseModel

from pydantic_versions import SchemaFamily, SchemaVersion, UnsupportedWireModelError
from pydantic_versions._wire_contract import (
    _is_exact_module_member,
    _is_typing_reflection_owner,
    _validate_type_alias,
    _wire_field_attributes,
)


def _family() -> SchemaFamily[BaseModel]:
    class Payload(BaseModel):
        value: int

    return SchemaFamily(
        model=Payload,
        name="wire_internal_safety",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )


def test_unknown_pydantic_field_attributes_fail_closed() -> None:
    with pytest.raises(UnsupportedWireModelError, match="uses unsupported attributes: unknown"):
        _wire_field_attributes(_family(), "value", {"unknown": True})


def test_unresolved_forward_reference_hidden_in_type_alias_fails_closed() -> None:
    unresolved_alias: Any = ForwardRef("UndefinedAlias")
    type CustomAlias[T] = unresolved_alias

    with pytest.raises(
        UnsupportedWireModelError,
        match="forward reference hidden in a type alias",
    ):
        _validate_type_alias(
            cast(Any, _family()),
            "value",
            cast(Any, CustomAlias),
        )


def test_reflection_allowlist_accepts_only_real_module_members() -> None:
    assert _is_exact_module_member(int, module="builtins") is True
    assert _is_exact_module_member(int, module="typing") is False
    assert _is_typing_reflection_owner(int) is True

    class LocalModel(BaseModel):
        pass

    assert _is_typing_reflection_owner(LocalModel) is False
