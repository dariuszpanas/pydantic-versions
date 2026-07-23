from __future__ import annotations

from collections.abc import Mapping, Sequence
from threading import RLock
from typing import Any, Self

from pydantic import BaseModel

from pydantic_versions._compiler import (
    _compile_projection,
    _compile_transition,
    _CompiledFamily,
    _CompiledVersion,
    _ensure_pydantic_v2_model,
    _validate_compilation_boundary,
    _validate_family_declarations,
    _validate_required_field_introductions,
)
from pydantic_versions._runtime import (
    _build_model_for_projection,
    _dump_family,
    _runtime_label,
    _validate_family,
)
from pydantic_versions.declarations import (
    _DEFAULT_VERSION_METADATA,
    NestedFamily,
    SchemaVersion,
    TransitionFunc,
    VersionedValidation,
    VersionMetadata,
    VersionTransition,
    _freeze_sequence,
    _require_label,
)
from pydantic_versions.exceptions import (
    DuplicateSchemaVersionError,
    InvalidMigrationError,
    SchemaCompilationError,
    SchemaFamilySelectionError,
    UnknownSchemaVersionError,
)

_DEFAULT_FAMILIES: dict[type[BaseModel], SchemaFamily[Any]] = {}
_FAMILY_LOCK = RLock()


class SchemaFamily[T: BaseModel]:
    __slots__ = (
        "_compiled",
        "_compiling",
        "_decorator_created",
        "_missing_version",
        "_model",
        "_name",
        "_nested",
        "_transitions",
        "_version_metadata",
        "_versions",
    )

    def __init__(
        self,
        *,
        model: type[T],
        name: str,
        versions: Sequence[SchemaVersion],
        transitions: Sequence[VersionTransition] = (),
        nested: Sequence[NestedFamily] = (),
        version_metadata: VersionMetadata | None = _DEFAULT_VERSION_METADATA,
        missing_version: str | None = None,
    ) -> None:
        _ensure_pydantic_v2_model(model)
        self._model = model
        self._name = _require_label(name, parameter="SchemaFamily.name")
        self._versions = _freeze_sequence(versions, parameter="SchemaFamily.versions")
        self._transitions = _freeze_sequence(
            transitions,
            parameter="SchemaFamily.transitions",
        )
        self._nested = _freeze_sequence(nested, parameter="SchemaFamily.nested")
        if version_metadata is not None and not isinstance(version_metadata, VersionMetadata):
            msg = "version_metadata must be VersionMetadata or None"
            raise SchemaCompilationError(msg)
        self._version_metadata = version_metadata
        if missing_version is not None:
            missing_version = _require_label(missing_version, parameter="missing_version")
        self._missing_version = missing_version
        self._decorator_created = False
        self._compiled: _CompiledFamily | None = None
        self._compiling = False
        self._validate_declarations()

    @property
    def model(self) -> type[T]:
        return self._model

    @property
    def name(self) -> str:
        return self._name

    @property
    def versions(self) -> tuple[SchemaVersion, ...]:
        return self._versions

    @property
    def transitions(self) -> tuple[VersionTransition, ...]:
        return self._transitions

    @property
    def nested(self) -> tuple[NestedFamily, ...]:
        return self._nested

    @property
    def version_metadata(self) -> VersionMetadata | None:
        return self._version_metadata

    @property
    def missing_version(self) -> str | None:
        return self._missing_version

    @property
    def current_version(self) -> str:
        return self._versions[-1].label

    def compile(self) -> Self:
        with _FAMILY_LOCK:
            if self._compiled is not None:
                return self
            if self._compiling:
                msg = f"Recursive schema-family compilation is not yet supported for {self.name!r}"
                raise SchemaCompilationError(msg)
            self._compiling = True
            try:
                self._validate_declarations()
                _validate_compilation_boundary(
                    name=self.name,
                    versions=self.versions,
                    transitions=self.transitions,
                    nested=self.nested,
                )
                projections = tuple(
                    _compile_projection(self.model, declaration) for declaration in self.versions
                )
                _validate_required_field_introductions(
                    model=self.model,
                    name=self.name,
                    projections=projections,
                    transitions=self.transitions,
                )
                compiled_versions = tuple(
                    _CompiledVersion(
                        projection=projection,
                        model=_build_model_for_projection(self, projection),
                    )
                    for projection in projections
                )
                explicit = {
                    (transition.source, transition.target): transition
                    for transition in self.transitions
                }
                compiled_transitions = tuple(
                    _compile_transition(source, target, explicit.get((source, target)))
                    for source, target in zip(
                        (version.label for version in self.versions),
                        (version.label for version in self.versions[1:]),
                        strict=False,
                    )
                )
                self._compiled = _CompiledFamily(
                    model=self.model,
                    name=self.name,
                    versions=compiled_versions,
                    transitions=compiled_transitions,
                    version_metadata=self.version_metadata,
                    missing_version=self.missing_version,
                )
            finally:
                self._compiling = False
        return self

    def as_default(self) -> Self:
        with _FAMILY_LOCK:
            existing = _DEFAULT_FAMILIES.get(self.model)
            if existing is None:
                _DEFAULT_FAMILIES[self.model] = self
            elif existing is not self:
                msg = (
                    f"{self.model.__name__!r} already has explicit default family "
                    f"{existing.name!r}; cannot attach {self.name!r}"
                )
                raise SchemaFamilySelectionError(msg)
        return self

    def model_for(self, version: str) -> type[BaseModel]:
        requested = _runtime_label(version, family_name=self.name)
        return self._compiled_family().version(requested).model

    def validate(self, data: Any, *, version: str | None = None) -> VersionedValidation[T]:
        return _validate_family(self, data, version=version)

    def defaults_for(
        self,
        *,
        version: str,
        include_version: bool = True,
        **dump_kwargs: Any,
    ) -> dict[str, Any]:
        return self.dump(
            version=version,
            include_version=include_version,
            **dump_kwargs,
        )

    def dump(
        self,
        *,
        version: str,
        data: T | Mapping[str, Any] | None = None,
        include_version: bool = True,
        **dump_kwargs: Any,
    ) -> dict[str, Any]:
        return _dump_family(
            self,
            version=version,
            data=data,
            include_version=include_version,
            dump_kwargs=dump_kwargs,
        )

    def _compiled_family(self) -> _CompiledFamily:
        self.compile()
        if self._compiled is None:  # pragma: no cover - compile() publishes atomically
            msg = f"Schema family {self.name!r} did not publish compiled state"
            raise SchemaCompilationError(msg)
        return self._compiled

    def _validate_declarations(self) -> None:
        _validate_family_declarations(
            model=self.model,
            name=self.name,
            versions=self.versions,
            transitions=self.transitions,
            nested=self.nested,
            missing_version=self.missing_version,
        )

    def _register_legacy_transition(
        self,
        source: str,
        target: str,
        upgrade: TransitionFunc,
    ) -> None:
        with _FAMILY_LOCK:
            self._ensure_legacy_transition_allowed(source, target)
            candidate = VersionTransition(source=source, target=target, upgrade=upgrade)
            self._transitions = (*self.transitions, candidate)

    def _ensure_legacy_transition_allowed(self, source: str, target: str) -> None:
        with _FAMILY_LOCK:
            if self._compiled is not None or self._compiling:
                msg = f"Cannot register migration after {self.name!r} has been compiled"
                raise InvalidMigrationError(msg)
            labels = tuple(version.label for version in self.versions)
            try:
                source_index = labels.index(source)
                target_index = labels.index(target)
            except ValueError as exc:
                msg = f"Unknown migration edge {source!r} -> {target!r} for {self.name!r}"
                raise UnknownSchemaVersionError(msg) from exc
            if target_index != source_index + 1:
                msg = (
                    f"Migration {source!r} -> {target!r} must connect adjacent "
                    f"forward labels for {self.name!r}"
                )
                raise InvalidMigrationError(msg)
            if any(
                transition.source == source and transition.target == target
                for transition in self.transitions
            ):
                msg = f"Migration {source!r} -> {target!r} is already registered for {self.name!r}"
                raise DuplicateSchemaVersionError(msg)


def _default_family_for_model(model: type[BaseModel]) -> SchemaFamily[Any] | None:
    return _DEFAULT_FAMILIES.get(model)


def _family_for[T: BaseModel](subject: type[T] | SchemaFamily[T]) -> SchemaFamily[T]:
    if isinstance(subject, SchemaFamily):
        return subject
    _ensure_pydantic_v2_model(subject)
    family = _DEFAULT_FAMILIES.get(subject)
    if family is None:
        msg = (
            f"{subject.__name__!r} has no explicit default schema family; pass a "
            "SchemaFamily or call family.as_default() during application configuration"
        )
        raise SchemaFamilySelectionError(msg)
    return family
