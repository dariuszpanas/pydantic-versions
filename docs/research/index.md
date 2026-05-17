# Research

This section is a working area for notes on existing packages, Pydantic internals, schema migration patterns, and the design tradeoffs that should shape this project.

Initial research target:

- `pydantic-variants`: clarify what it solves, what assumptions it makes, and where this project should differ.

## Initial direction

The first implementation keeps the core data-only and decorator-first:

- schema versions are explicit strings with declared ordering;
- historical models are derived from the current model with declarative patches;
- rendered configs include schema version metadata by default;
- validation can upgrade historical payloads to the current model with registered migrations;
- `missing_version` is reserved for legacy unversioned configs, not as an implicit current-version default;
- YAML loading and dumping remain the caller's responsibility.
