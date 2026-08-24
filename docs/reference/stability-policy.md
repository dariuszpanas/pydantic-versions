# Stability and Compatibility Policy

`pydantic-versions` uses Semantic Versioning for the deliberate public
contract described here. This policy is the compatibility promise for the 1.x
release series beginning with 1.0.0.

Stable does not mean feature-complete. It means that the behavior the package
claims to support is deliberate, tested, and changed under explicit versioning
rules.

## Public Python surface

The stable Python surface consists of:

- names exported by `pydantic_versions.__all__` and imported from the package
  root;
- their documented call signatures and typing behavior;
- fields of exported frozen records;
- the inheritance relationships of exported exceptions;
- documented `SchemaFamily` methods and read-only properties; and
- the package's `py.typed` marker and supported static-typing examples.

Modules whose names begin with an underscore are private. Other implementation
modules are also not separate import contracts: import public names from
`pydantic_versions`, not from the module that currently defines them.

Exception classes and their inheritance are stable. Exact exception messages,
tracebacks, and private chained-exception details are diagnostic text rather
than machine-readable API.

## Schema and wire behavior

Application schema labels are immutable wire contracts. Reusing a published
label for incompatible fields, aliases, constraints, defaults, metadata, or
transition meaning is an application-level breaking change even if the Python
package version is unchanged.

For a supported declaration, compatible 1.x package releases preserve the
documented behavior of:

- version discovery and explicit `version=` selection;
- generated input and output wire shapes;
- historical defaults, removals, and renames;
- upgrade and downgrade ordering;
- nested-family conversion;
- transition callbacks receiving detached, declared, already-validated Python
  values, including exact built-in container kinds, without invoking source
  serializers or admitting excluded, extra, or subclass-only fields;
- exact, lossy, and unavailable rendering semantics; and
- validation into the authoritative current Pydantic model.

Generated model object identity is stable within one compiled family. Private
attributes and implementation class names are not a cross-release contract.
The supported JSON Schema and serialized payload behavior is the contract.

The [explicit historical nested-shape
grammar](../guide/generated-wire-contracts.md#explicit-historical-nested-shapes)
is part of the supported declaration contract. Compatible 1.x releases preserve
its statically owned scalar, model, dataclass, `TypedDict`, union, and exact
built-in-container behavior, including the post-validation requirement that a
managed value still conform to its annotation. Effective alias normalization,
family-owned metadata preflight and envelope verification, reserved
omitted-route output locations, scalar ownership of recoverable scalar output,
and fail-closed rejection of runtime-unrecoverable overlapping union arms are
part of that boundary. Broad mappings, abstract or custom containers, and other
opaque carriers remain unsupported declarations; support for a new, statically
identifiable shape may be added in a compatible minor release.

## Inventories and plans

Exported inventory and plan records, their frozen fields, `to_dict()` keys and
meanings, canonical ordering, path representation, semantics, and deterministic
step IDs are stable for an equivalent supported declaration.

A later release may add behavior for a newly supported declaration without
changing the output for existing supported declarations. An unexplained change
to a committed golden inventory or plan is treated as a compatibility failure,
not accepted by regenerating the fixture silently.

## Semantic Versioning rules

A new major release is required to:

- remove or rename a public export;
- make a supported call signature incompatible;
- remove or reinterpret an exported record field;
- change documented exception inheritance;
- invalidate persisted payloads that satisfy the stable contract;
- change the meaning of an existing schema declaration; or
- incompatibly change stable inventory or plan serialization for an equivalent
  declaration.

A minor release may add optional API, support a new declaration form, add a new
runtime or dependency version, or deprecate API while keeping it operational.
A patch release may correct behavior to match the existing contract, improve
diagnostics or documentation, update compatible dependencies, and make
internal performance or reliability changes.

If a correctness or security fix necessarily changes observable behavior, the
release notes identify the affected contract and migration path. Security and
data integrity take priority over preserving a defect.

## Deprecation

During 1.x, a public API is not removed merely because a preferred alternative
exists. Deprecation requires:

1. a documented replacement or reason;
2. a changelog entry;
3. an appropriate runtime warning when the deprecated path is executed; and
4. retention until the next major release unless an exceptional security or
   data-integrity issue makes that unsafe.

The decorator and model-first compatibility entry points are part of the
deliberate contract unless they are explicitly deprecated under this policy.

## Python and Pydantic support

The current supported range is authoritative in package metadata and the
[compatibility matrix](../guide/pydantic-compatibility-matrix.md). CI verifies
the lowest supported Pydantic version, the locked environment, the latest
allowed Pydantic version, and every supported Python minor.

Adding support for a new Python or Pydantic version can occur in a minor or
patch release. Dropping an end-of-life Python minor or raising the Pydantic
lower bound is a maintenance compatibility change: it occurs in a minor
release, is called out prominently in the changelog, and must be justified by
upstream support, correctness, security, or standards alignment. The `<3.0`
upper bound remains until Pydantic 3 support is deliberately designed and
tested.

Dependency updates that remain inside the declared support ranges are normal
maintenance and do not by themselves require a feature release.

## Evidence and maintenance

The compatibility promise is enforced through public-surface, typing,
cross-release golden, unit, integration, supported-version, documentation,
security, and isolated-package tests. A green narrow test is not evidence for a
broad compatibility claim; the complete relevant gate must pass.

CI enforces at least 95.00% statement coverage and reports the measured value to
two decimal places. Coverage is supporting evidence rather than a target to
game: the floor advances through reachable public-contract tests and justified
source simplification, not new exclusions or fabricated internal states.

Cross-release fixtures record their exact released-package provenance and have
an explicit regeneration command. Current code must consume the persisted
payloads and reproduce the stable inspection data. Fixture changes require
review and an explanation under this policy; CI never updates them implicitly.

The tag-driven release path repeats the locked dependency and build-backend
audit, creates the environment with the repository's pinned Python, installs
the project non-editably with the audited backend, and disables subsequent
environment synchronization. It binds the distribution build to that same
environment, runs a strict documentation build and distribution metadata
validation, and tests the wheel and source archive in isolation before
publishing. Release tags are immutable, and a tag is rejected unless its commit
is on the default branch, so published artifacts and attestations remain tied
to reviewed source. The workflow can also rehearse every build and package test
from an explicitly selected commit without publishing; TestPyPI upload is a
main-branch-only, separate opt-in action.

Once the package solves its defined problem, work is driven by concrete user
feedback, bug and security reports, dependency/runtime updates, current
ecosystem standards, and bounded polish. These are responses to real needs,
not a commitment to a speculative long-range feature roadmap.
