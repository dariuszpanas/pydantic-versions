# Changelog

## Unreleased

- Added Python 3.14 package metadata and raised the supported Pydantic v2 floor
  to 2.12.3.
- Added explicit registration-time errors for Pydantic v1 and other models that
  do not inherit from Pydantic v2's `BaseModel`.
- Updated internal typing for compatibility with current `ty` releases.
- Reworked CI to verify the real Python 3.12-3.14 interpreters and the minimum
  and latest supported Pydantic releases.
- Added release version/changelog validation, isolated wheel and source archive
  tests, and hardened workflow dependencies, credentials, permissions, and gates.
- Refreshed the development lockfile, including the current Django security fixes.
- Fixed the README hero image URL for rendering on PyPI.

## 0.1.0 - 2026-05-18

- Added the initial project scaffold.
- Added the first versioned schema API for decorators, generated historical models, validation, rendering, and upgrade migrations.
- Expanded docs around schema version discovery and legacy unversioned config handling.
- Added nested version-field paths, grouped schema-version patch decorators, and release-oriented guide/reference docs.
- Added an extensive nested config example showing the fragility of plain schema changes and the compatibility workflow.
- Added adoption guidance for schema-version design, compatibility tests, and patch-vs-migration decisions.
- Added Django Ninja compatibility tests and documentation for versioned API schemas.
- Added install and getting-started documentation.
