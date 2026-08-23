from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic_versions._compiler import (
    _CompiledDecoratorNestedFamily,
    _CompiledFamily,
    _CompiledField,
    _CompiledNestedFamily,
    _CompiledTransition,
    _CompiledVersion,
    _stable_digest,
)
from pydantic_versions.declarations import VersionPath
from pydantic_versions.exceptions import SchemaCompilationError
from pydantic_versions.inspection import (
    ConversionPlan,
    NestedFamilyDescription,
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
    nested: tuple[_CompiledNestedFamily, ...] = (),
    decorator_nested: tuple[_CompiledDecoratorNestedFamily, ...] = (),
) -> _PlanningCatalog:
    version_descriptions = tuple(_describe_version(version) for version in versions)
    transition_descriptions = tuple(_describe_transition(transition) for transition in transitions)
    inventory = SchemaInventory(
        family=family.name,
        model=f"{family.model.__module__}.{family.model.__qualname__}",
        current_version=family.current_version,
        versions=version_descriptions,
        transitions=transition_descriptions,
        nested=tuple(
            _describe_nested_family(nested_family=family_entry) for family_entry in nested
        ),
        version_metadata=family.version_metadata,
    )
    validation_plans = tuple(
        _build_validation_plan(
            family,
            versions,
            transitions,
            nested,
            decorator_nested,
            source_index=source_index,
        )
        for source_index in range(len(versions))
    )
    render_plans = tuple(
        _build_render_plan(
            family,
            versions,
            transitions,
            nested,
            decorator_nested,
            target_index=target_index,
        )
        for target_index in range(len(versions))
    )
    return _PlanningCatalog(
        inventory=inventory,
        validation_plans=validation_plans,
        render_plans=render_plans,
    )


def _describe_nested_family(
    nested_family: _CompiledNestedFamily,
) -> NestedFamilyDescription:
    return NestedFamilyDescription(
        schema_path=_schema_path(nested_family.path),
        family=nested_family.family.name,
        versions=nested_family.versions,
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
    nested: tuple[_CompiledNestedFamily, ...],
    decorator_nested: tuple[_CompiledDecoratorNestedFamily, ...],
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
        steps.extend(
            _nested_steps(
                family,
                nested,
                operation="validate",
                parent_source_version=transition.source,
                parent_target_version=transition.target,
                edge_ordinal=edge_index,
            )
        )
        steps.extend(
            _decorator_nested_steps(
                family,
                decorator_nested,
                operation="validate",
                parent_source_version=transition.source,
                parent_target_version=transition.target,
                edge_ordinal=edge_index,
            )
        )
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
    nested: tuple[_CompiledNestedFamily, ...],
    decorator_nested: tuple[_CompiledDecoratorNestedFamily, ...],
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
        steps.extend(
            _nested_steps(
                family,
                nested,
                operation="render",
                parent_source_version=transition.target,
                parent_target_version=transition.source,
                edge_ordinal=edge_index,
            )
        )
        steps.extend(
            _decorator_nested_steps(
                family,
                decorator_nested,
                operation="render",
                parent_source_version=transition.target,
                parent_target_version=transition.source,
                edge_ordinal=edge_index,
            )
        )
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


def _nested_steps(
    family: SchemaFamily[Any],
    nested: tuple[_CompiledNestedFamily, ...],
    *,
    operation: Literal["validate", "render"],
    parent_source_version: str,
    parent_target_version: str,
    edge_ordinal: int,
) -> tuple[PlanStep, ...]:
    steps: list[PlanStep] = []
    for nested_ordinal, declaration in enumerate(nested):
        source_version = declaration.child_label(parent_source_version)
        target_version = declaration.child_label(parent_target_version)
        if source_version == target_version:
            continue
        child = declaration.family
        child_compiled = child._compiled_family()
        direction: Literal["upgrade", "downgrade"] = (
            "upgrade"
            if child_compiled.index(source_version) < child_compiled.index(target_version)
            else "downgrade"
        )
        steps.append(
            _step(
                family,
                operation=operation,
                direction=direction,
                kind="nested",
                source_version=source_version,
                target_version=target_version,
                schema_path=_schema_path(declaration.path),
                semantics=_nested_route_semantics(
                    child_compiled,
                    source_version=source_version,
                    target_version=target_version,
                ),
                ordinal=edge_ordinal,
                identity_details=(
                    child.model.__module__,
                    child.model.__qualname__,
                    child.name,
                    parent_source_version,
                    parent_target_version,
                    str(nested_ordinal),
                    "conditional",
                ),
                conditional=True,
            )
        )
    return tuple(steps)


def _decorator_nested_steps(
    family: SchemaFamily[Any],
    nested: tuple[_CompiledDecoratorNestedFamily, ...],
    *,
    operation: Literal["validate", "render"],
    parent_source_version: str,
    parent_target_version: str,
    edge_ordinal: int,
) -> tuple[PlanStep, ...]:
    steps: list[PlanStep] = []
    for site_ordinal, declarations in enumerate(_decorator_nested_sites(nested)):
        first = declarations[0]
        source_version = first.child_label(parent_source_version)
        target_version = first.child_label(parent_target_version)
        semantics = _aggregate_semantics(
            tuple(
                _nested_route_semantics(
                    declaration.family._compiled_family(),
                    source_version=source_version,
                    target_version=target_version,
                )
                for declaration in declarations
            )
        )
        child_compiled = first.family._compiled_family()
        direction: Literal["upgrade", "downgrade"] = (
            "upgrade"
            if child_compiled.index(source_version) < child_compiled.index(target_version)
            else "downgrade"
        )
        identity = tuple(
            component
            for declaration in declarations
            for component in (
                "branch",
                *declaration.identity,
            )
        )
        steps.append(
            _step(
                family,
                operation=operation,
                direction=direction,
                kind="nested",
                source_version=source_version,
                target_version=target_version,
                schema_path=_schema_path(first.path),
                semantics=semantics,
                ordinal=edge_ordinal,
                identity_details=(
                    "decorator",
                    parent_source_version,
                    parent_target_version,
                    str(site_ordinal),
                    *identity,
                    "conditional",
                ),
                conditional=True,
            )
        )
    return tuple(steps)


def _decorator_nested_sites(
    nested: tuple[_CompiledDecoratorNestedFamily, ...],
) -> tuple[tuple[_CompiledDecoratorNestedFamily, ...], ...]:
    paths: list[tuple[str, ...]] = []
    grouped: list[list[_CompiledDecoratorNestedFamily]] = []
    for declaration in nested:
        if declaration.path not in paths:
            paths.append(declaration.path)
            grouped.append([])
        grouped[paths.index(declaration.path)].append(declaration)
    return tuple(tuple(site) for site in grouped)


def _aggregate_semantics(values: tuple[StepSemantics, ...]) -> StepSemantics:
    if "unavailable" in values:
        return "unavailable"
    if "lossy" in values:
        return "lossy"
    if "exact" in values:
        return "exact"
    return "not_applicable"


def _nested_route_semantics(
    compiled: _CompiledFamily,
    *,
    source_version: str,
    target_version: str,
) -> StepSemantics:
    source_index = compiled.index(source_version)
    target_index = compiled.index(target_version)

    risks: list[StepSemantics] = []
    if source_index < target_index:
        route = compiled.transitions[source_index:target_index]
        for transition in route:
            risks.extend(
                _nested_route_risks(
                    compiled.nested,
                    compiled.decorator_nested,
                    parent_source_version=transition.source,
                    parent_target_version=transition.target,
                )
            )
        default: StepSemantics = "not_applicable"
    else:
        route = tuple(
            compiled.transitions[edge_index]
            for edge_index in range(source_index - 1, target_index - 1, -1)
        )
        for transition in route:
            risks.extend(
                _nested_route_risks(
                    compiled.nested,
                    compiled.decorator_nested,
                    parent_source_version=transition.target,
                    parent_target_version=transition.source,
                )
            )
            risks.append(transition.downgrade_semantics)
        if any(
            description.kind == "removed"
            for description in _describe_version(compiled.versions[target_index]).projections
        ):
            risks.append("lossy")
        default = "exact"

    if "unavailable" in risks:
        return "unavailable"
    if "lossy" in risks:
        return "lossy"
    return default


def _nested_route_risks(
    nested: tuple[_CompiledNestedFamily, ...],
    decorator_nested: tuple[_CompiledDecoratorNestedFamily, ...],
    *,
    parent_source_version: str,
    parent_target_version: str,
) -> tuple[StepSemantics, ...]:
    risks: list[StepSemantics] = []
    for declaration in nested:
        source_version = declaration.child_label(parent_source_version)
        target_version = declaration.child_label(parent_target_version)
        if source_version == target_version:
            continue
        semantics = _nested_route_semantics(
            declaration.family._compiled_family(),
            source_version=source_version,
            target_version=target_version,
        )
        if semantics in ("lossy", "unavailable"):
            risks.append(semantics)
    for declarations in _decorator_nested_sites(decorator_nested):
        semantics = _aggregate_semantics(
            tuple(
                _nested_route_semantics(
                    declaration.family._compiled_family(),
                    source_version=declaration.child_label(parent_source_version),
                    target_version=declaration.child_label(parent_target_version),
                )
                for declaration in declarations
                if declaration.child_label(parent_source_version)
                != declaration.child_label(parent_target_version)
            )
        )
        if semantics in ("lossy", "unavailable"):
            risks.append(semantics)
    return tuple(risks)


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
    conditional: bool = False,
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
        conditional=conditional,
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
