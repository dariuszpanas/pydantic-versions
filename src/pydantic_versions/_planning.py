from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic_versions._compiler import (
    _CompiledField,
    _CompiledTransition,
    _CompiledVersion,
    _stable_digest,
)
from pydantic_versions.declarations import VersionPath
from pydantic_versions.exceptions import SchemaCompilationError
from pydantic_versions.inspection import (
    ConversionPlan,
    PlanStep,
    ProjectionDescription,
    SchemaInventory,
    StepKind,
    StepSemantics,
    TransitionDescription,
    VersionDescription,
)

if TYPE_CHECKING:
    from pydantic_versions.family import SchemaFamily

_ROOT_PATH = "$"


@dataclass(frozen=True)
class _PlanningCatalog:
    inventory: SchemaInventory
    validation_plans: tuple[ConversionPlan, ...]
    render_plans: tuple[ConversionPlan, ...]


def _build_planning_catalog(
    family: SchemaFamily[Any],
    versions: tuple[_CompiledVersion, ...],
    transitions: tuple[_CompiledTransition, ...],
) -> _PlanningCatalog:
    version_descriptions = tuple(_describe_version(version) for version in versions)
    transition_descriptions = tuple(_describe_transition(transition) for transition in transitions)
    inventory = SchemaInventory(
        family=family.name,
        model=f"{family.model.__module__}.{family.model.__qualname__}",
        current_version=family.current_version,
        versions=version_descriptions,
        transitions=transition_descriptions,
        nested=(),
        version_metadata=family.version_metadata,
    )
    validation_plans = tuple(
        _build_validation_plan(
            family,
            versions,
            transitions,
            source_index=source_index,
        )
        for source_index in range(len(versions))
    )
    render_plans = tuple(
        _build_render_plan(
            family,
            versions,
            transitions,
            target_index=target_index,
        )
        for target_index in range(len(versions))
    )
    return _PlanningCatalog(
        inventory=inventory,
        validation_plans=validation_plans,
        render_plans=render_plans,
    )


def _describe_version(version: _CompiledVersion) -> VersionDescription:
    ordered: list[tuple[int, ProjectionDescription]] = []
    for field in version.projection.fields:
        description = _describe_projection(field)
        if description is None:
            continue
        if field.patch_ordinal is None:  # pragma: no cover - compiled invariant
            msg = (
                f"Compiled projection {version.projection.label!r} has no declaration "
                f"ordinal for field {field.current_name!r}"
            )
            raise SchemaCompilationError(msg)
        ordered.append((field.patch_ordinal, description))
    ordered.sort(key=lambda item: item[0])
    return VersionDescription(
        label=version.projection.label,
        wire_model=version.wire_model_kind,
        projections=tuple(description for _, description in ordered),
    )


def _describe_projection(field: _CompiledField) -> ProjectionDescription | None:
    if field.version_name is None:
        return ProjectionDescription(
            kind="removed",
            current_field=field.current_name,
            historical_field=None,
            has_default=False,
        )
    if field.version_name != field.current_name:
        return ProjectionDescription(
            kind="renamed",
            current_field=field.current_name,
            historical_field=field.version_name,
            has_default=False,
        )
    if field.default is not None:
        return ProjectionDescription(
            kind="default",
            current_field=field.current_name,
            historical_field=field.version_name,
            has_default=True,
        )
    return None


def _describe_transition(transition: _CompiledTransition) -> TransitionDescription:
    upgrade: Literal["implicit_identity", "custom"] = (
        "implicit_identity" if transition.upgrade_kind == "implicit_identity" else "custom"
    )
    if transition.downgrade_kind == "custom_transition":
        downgrade: Literal["implicit_identity", "custom", "unavailable"] = "custom"
    else:
        downgrade = transition.downgrade_kind
    return TransitionDescription(
        source=transition.source,
        target=transition.target,
        upgrade=upgrade,
        downgrade=downgrade,
        downgrade_semantics=transition.downgrade_semantics,
    )


def _build_validation_plan(
    family: SchemaFamily[Any],
    versions: tuple[_CompiledVersion, ...],
    transitions: tuple[_CompiledTransition, ...],
    *,
    source_index: int,
) -> ConversionPlan:
    source = versions[source_index]
    source_label = source.projection.label
    current_label = versions[-1].projection.label
    steps: list[PlanStep] = []

    if family.version_metadata is not None:
        metadata_identity = _version_path_identity(family.version_metadata.path)
        steps.append(
            _step(
                family,
                operation="validate",
                direction="upgrade",
                kind="metadata",
                source_version=source_label,
                target_version=source_label,
                schema_path=_schema_path(family.version_metadata.path),
                semantics="not_applicable",
                ordinal=0,
                identity_details=(family.version_metadata.owner, *metadata_identity),
            )
        )
    steps.append(
        _step(
            family,
            operation="validate",
            direction="upgrade",
            kind="wire_validation",
            source_version=source_label,
            target_version=source_label,
            schema_path=_ROOT_PATH,
            semantics="not_applicable",
            ordinal=source_index,
            identity_details=(source.wire_model_kind,),
        )
    )
    steps.extend(
        _projection_steps(
            family,
            operation="validate",
            direction="upgrade",
            source_version=source_label,
            target_version=source_label,
            descriptions=_describe_version(source).projections,
            render=False,
        )
    )
    for edge_index, transition in enumerate(transitions[source_index:], start=source_index):
        kind: StepKind = transition.upgrade_kind
        semantics: StepSemantics = "exact" if kind == "implicit_identity" else "not_applicable"
        steps.append(
            _step(
                family,
                operation="validate",
                direction="upgrade",
                kind=kind,
                source_version=transition.source,
                target_version=transition.target,
                schema_path=_ROOT_PATH,
                semantics=semantics,
                ordinal=edge_index,
                identity_details=(transition.upgrade_kind,),
            )
        )
    steps.append(
        _step(
            family,
            operation="validate",
            direction="upgrade",
            kind="current_validation",
            source_version=current_label,
            target_version=current_label,
            schema_path=_ROOT_PATH,
            semantics="not_applicable",
            ordinal=0,
        )
    )
    return ConversionPlan(
        family=family.name,
        source_version=source_label,
        target_version=current_label,
        operation="validate",
        semantics="not_applicable",
        steps=tuple(steps),
    )


def _build_render_plan(
    family: SchemaFamily[Any],
    versions: tuple[_CompiledVersion, ...],
    transitions: tuple[_CompiledTransition, ...],
    *,
    target_index: int,
) -> ConversionPlan:
    current_label = versions[-1].projection.label
    target = versions[target_index]
    target_label = target.projection.label
    steps: list[PlanStep] = [
        _step(
            family,
            operation="render",
            direction="downgrade",
            kind="current_validation",
            source_version=current_label,
            target_version=current_label,
            schema_path=_ROOT_PATH,
            semantics="not_applicable",
            ordinal=0,
        )
    ]

    route = tuple(enumerate(transitions[target_index:], start=target_index))
    for edge_index, transition in reversed(route):
        kind: StepKind = (
            "custom_transition"
            if transition.downgrade_kind == "unavailable"
            else transition.downgrade_kind
        )
        steps.append(
            _step(
                family,
                operation="render",
                direction="downgrade",
                kind=kind,
                source_version=transition.target,
                target_version=transition.source,
                schema_path=_ROOT_PATH,
                semantics=transition.downgrade_semantics,
                ordinal=edge_index,
                identity_details=(
                    transition.downgrade_kind,
                    transition.downgrade_semantics,
                ),
            )
        )
    steps.extend(
        _projection_steps(
            family,
            operation="render",
            direction="downgrade",
            source_version=target_label,
            target_version=target_label,
            descriptions=_describe_version(target).projections,
            render=True,
        )
    )
    if family.version_metadata is not None:
        metadata_identity = _version_path_identity(family.version_metadata.path)
        steps.append(
            _step(
                family,
                operation="render",
                direction="downgrade",
                kind="metadata",
                source_version=target_label,
                target_version=target_label,
                schema_path=_schema_path(family.version_metadata.path),
                semantics="not_applicable",
                ordinal=0,
                identity_details=(family.version_metadata.owner, *metadata_identity),
            )
        )
    steps.extend(
        (
            _step(
                family,
                operation="render",
                direction="downgrade",
                kind="wire_validation",
                source_version=target_label,
                target_version=target_label,
                schema_path=_ROOT_PATH,
                semantics="not_applicable",
                ordinal=target_index,
                identity_details=(target.wire_model_kind,),
            ),
            _step(
                family,
                operation="render",
                direction="downgrade",
                kind="serialization",
                source_version=target_label,
                target_version=target_label,
                schema_path=_ROOT_PATH,
                semantics="not_applicable",
                ordinal=0,
            ),
        )
    )
    semantics: StepSemantics = "exact"
    if any(step.semantics == "unavailable" for step in steps):
        semantics = "unavailable"
    elif any(step.semantics == "lossy" for step in steps):
        semantics = "lossy"
    return ConversionPlan(
        family=family.name,
        source_version=current_label,
        target_version=target_label,
        operation="render",
        semantics=semantics,
        steps=tuple(steps),
    )


def _projection_steps(
    family: SchemaFamily[Any],
    *,
    operation: Literal["validate", "render"],
    direction: Literal["upgrade", "downgrade"],
    source_version: str,
    target_version: str,
    descriptions: tuple[ProjectionDescription, ...],
    render: bool,
) -> tuple[PlanStep, ...]:
    steps: list[PlanStep] = []
    for ordinal, description in enumerate(descriptions):
        semantics: StepSemantics = "not_applicable"
        if render:
            semantics = "lossy" if description.kind == "removed" else "exact"
        steps.append(
            _step(
                family,
                operation=operation,
                direction=direction,
                kind="projection",
                source_version=source_version,
                target_version=target_version,
                schema_path=description.current_field,
                semantics=semantics,
                ordinal=ordinal,
                identity_details=(
                    description.kind,
                    description.historical_field or "",
                    "default" if description.has_default else "required",
                ),
            )
        )
    return tuple(steps)


def _step(
    family: SchemaFamily[Any],
    *,
    operation: Literal["validate", "render"],
    direction: Literal["upgrade", "downgrade"],
    kind: StepKind,
    source_version: str,
    target_version: str,
    schema_path: str,
    semantics: StepSemantics,
    ordinal: int,
    identity_details: tuple[str, ...] = (),
) -> PlanStep:
    components = (
        family.model.__module__,
        family.model.__qualname__,
        family.name,
        operation,
        direction,
        kind,
        source_version,
        target_version,
        schema_path,
        semantics,
        str(ordinal),
        *identity_details,
    )
    return PlanStep(
        id=f"pv1-{_stable_digest(components)}",
        family=family.name,
        source_version=source_version,
        target_version=target_version,
        operation=operation,
        direction=direction,
        kind=kind,
        schema_path=schema_path,
        semantics=semantics,
        conditional=False,
    )


def _schema_path(path: VersionPath) -> str:
    if isinstance(path, str):
        return path if path.isidentifier() else f"$[{json.dumps(path, ensure_ascii=False)}]"
    return "$" + "".join(
        f".{part}" if part.isidentifier() else f"[{json.dumps(part, ensure_ascii=False)}]"
        for part in path
    )


def _version_path_identity(path: VersionPath) -> tuple[str, ...]:
    if isinstance(path, str):
        return ("field", path)
    return ("nested", *path)
