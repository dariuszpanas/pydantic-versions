# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-07-23

### Added
- Defined generated current and historical models as object-shaped Pydantic v2
  wire contracts, with explicit preservation and behavior boundaries, exact
  `Literal` version discriminators, deterministic identities, and contextual
  `UnsupportedWireModelError` failures for unsafe automatic projections.
- Added frozen schema inventories and deterministic validation/render plans with
  stable step IDs, visible implicit migrations, JSON-safe serialization, lossy
  projection semantics, and payload-free irreversible-route preflight.
- Added immutable external `SchemaFamily`, `SchemaVersion`, and
  `VersionTransition` declarations so schema history can live outside the
  current model and two isolated families can safely reuse one model.
- Added Python 3.14 package metadata and raised the supported Pydantic v2 floor
  to 2.12.3.
- Added explicit registration-time errors for Pydantic v1 and other models that
  do not inherit from Pydantic v2's `BaseModel`.
- Added release version/changelog validation, isolated wheel and source archive
  tests, and hardened workflow dependencies, credentials, permissions, and gates.
- Expanded commit and PR guidance so every retained logical commit provides a
  portable change record, exact validation evidence, and useful investigation
  context, with a tracked commit-message template for local checkouts.

### Changed
- Made family compilation lazy, thread-safe, collision-resistant, and
  authoritative for generated models, field projections, validation upgrades,
  and rendering projections; unreachable transition declarations now fail.
- Kept decorators and model-first free functions as explicit-default adapters
  to the same compiler, with contextual errors for missing or conflicting
  default-family selection.
- Updated internal typing for compatibility with current `ty` releases.
- Reworked CI to verify the real Python 3.12-3.14 interpreters and the minimum
  and latest supported Pydantic releases.

### Fixed
- Fixed the README hero image URL for rendering on PyPI.

### Security
- Refreshed the development lockfile, including the current Django security fixes.

## [0.1.0] - 2026-05-18

### Added
- Added the initial project scaffold.
- Added the first versioned schema API for decorators, generated historical models, validation, rendering, and upgrade migrations.
- Expanded docs around schema version discovery and legacy unversioned config handling.
- Added nested version-field paths, grouped schema-version patch decorators, and release-oriented guide/reference docs.
- Added an extensive nested config example showing the fragility of plain schema changes and the compatibility workflow.
- Added adoption guidance for schema-version design, compatibility tests, and patch-vs-migration decisions.
- Added Django Ninja compatibility tests and documentation for versioned API schemas.
- Added install and getting-started documentation.

[0.2.0]: https://github.com/dariuszpanas/pydantic-versions/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/dariuszpanas/pydantic-versions/releases/tag/v0.1.0
