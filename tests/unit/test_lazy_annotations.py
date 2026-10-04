# Deliberately omit the future annotations import to exercise deferred annotations
# on Python 3.14+ and ordinary evaluated annotations on earlier versions.
import sys
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field, create_model

from pydantic_versions import (
    SchemaFamily,
    SchemaVersion,
    UnsupportedWireModelError,
    model_for_version,
    versioned_schema,
)


class _DirectTypedExtra(BaseModel):
    model_config = ConfigDict(extra="allow")

    __pydantic_extra__: dict[str, int] = Field(init=False)
    value: int = 1


class _TypedExtraMixin:
    __pydantic_extra__: dict[str, int]


class _InheritedTypedExtra(BaseModel, _TypedExtraMixin):
    model_config = ConfigDict(extra="allow")

    value: int = 1


@pytest.mark.parametrize("model", [_DirectTypedExtra, _InheritedTypedExtra])
def test_typed_extras_without_future_annotations_are_rejected(model: type[BaseModel]) -> None:
    family = SchemaFamily(
        model=model,
        name=f"deferred_extras_{model.__name__}",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )

    with pytest.raises(UnsupportedWireModelError, match="typed extra values"):
        family.compile()


@pytest.mark.parametrize("model", [_DirectTypedExtra, _InheritedTypedExtra])
def test_ordinary_wrapper_typed_extras_without_future_annotations_are_rejected(
    model: type[BaseModel],
) -> None:
    @versioned_schema(
        name=f"deferred_wrapper_child_{model.__name__}",
        versions=("1", "2"),
        current="2",
    )
    class Child(BaseModel):
        value: int

    wrapper = create_model("DeferredExtraWrapper", __base__=model, child=(Child, ...))
    parent = create_model("DeferredExtraParent", wrapper=(wrapper, ...))
    decorated = versioned_schema(
        name=f"deferred_wrapper_parent_{model.__name__}",
        versions=("1", "2"),
        current="2",
    )(parent)

    with pytest.raises(UnsupportedWireModelError, match="ordinary wrapper.*typed extra values"):
        model_for_version(decorated, "1")


@pytest.mark.skipif(sys.version_info < (3, 14), reason="requires deferred annotations")
def test_typed_extra_detection_does_not_evaluate_unrelated_mixin_annotations() -> None:
    namespace: dict[str, Any] = {
        "__name__": __name__,
        "BaseModel": BaseModel,
        "ConfigDict": ConfigDict,
    }
    source = """
class TypedExtraMixin:
    __pydantic_extra__: dict[str, int]
    _unrelated: UndefinedMixinAnnotation

class Payload(BaseModel, TypedExtraMixin):
    model_config = ConfigDict(extra="allow")
    value: int = 1
"""
    exec(compile(source, "<deferred-extra-mixin>", "exec", dont_inherit=True), namespace)
    family = SchemaFamily(
        model=namespace["Payload"],
        name="deferred_extras_unresolved_mixin",
        versions=(SchemaVersion("1"),),
        version_metadata=None,
    )

    with pytest.raises(UnsupportedWireModelError, match="typed extra values"):
        family.compile()
