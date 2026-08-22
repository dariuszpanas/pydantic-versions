# External Schema Families

An external `SchemaFamily` keeps schema history beside the application model
instead of stacking an ever-growing declaration above it. This is the primary
configuration style for histories that need their own module, tooling, or more
than one interpretation of the same current model.

## Keep the current model ordinary

The model module does not need to import `pydantic_versions`:

<!-- pv-doc-test: external-families -->
```python
# models.py
from pydantic import BaseModel


class AppConfig(BaseModel):
    timeout: float = 10.0
    retries: int = 3
    new_feature: bool = False
```

Declare its history elsewhere:

<!-- pv-doc-test: external-families -->
```python
# schema_history.py
from pydantic_versions import (
    SchemaFamily,
    SchemaVersion,
    field_default,
    field_removed,
)


APP_CONFIG_SCHEMA = SchemaFamily(
    model=AppConfig,
    name="app_config",
    versions=(
        SchemaVersion(
            "1",
            patches=(
                field_default("timeout", 5.0),
                field_removed("new_feature"),
            ),
        ),
        SchemaVersion("2"),
    ),
)
```

When these definitions live in separate files, `schema_history.py` imports
`AppConfig` from `models.py`; the snippets on this page are also one executable
sequence.

The final declared label is current. Historical versions are projected
independently from the current model; patches are not accumulated from one
historical declaration into the next.

## Use the family explicitly

Direct family calls cannot be confused with another history:

<!-- pv-doc-test: external-families -->
```python
result = APP_CONFIG_SCHEMA.validate(
    {"schema_version": "1", "retries": 2},
)

assert result.current_model == AppConfig(
    timeout=5.0,
    retries=2,
    new_feature=False,
)

v1_model = APP_CONFIG_SCHEMA.model_for("1")
v1_defaults = APP_CONFIG_SCHEMA.defaults_for(version="1")

assert v1_model.model_validate(v1_defaults).model_dump() == v1_defaults
assert v1_defaults == {
    "timeout": 5.0,
    "retries": 3,
    "schema_version": "1",
}
```

The compatibility free functions also accept a family as their first argument:

<!-- pv-doc-test: external-families -->
```python
from pydantic_versions import model_for_version, validate_versioned


v1_model = model_for_version(APP_CONFIG_SCHEMA, "1")
result = validate_versioned(
    APP_CONFIG_SCHEMA,
    {"schema_version": "1", "retries": 2},
)

assert model_for_version(APP_CONFIG_SCHEMA, "1") is v1_model
assert result.current_model.retries == 2
```

## Reuse one model in two families

Families own their declarations, compiled projections, transitions, and
generated-model cache. Two families can therefore reuse the same application
model without overwriting each other:

<!-- pv-doc-test: external-families -->
```python
PUBLIC_CONFIG = SchemaFamily(
    model=AppConfig,
    name="public_config",
    versions=(SchemaVersion("1"), SchemaVersion("2")),
)
INTERNAL_CONFIG = SchemaFamily(
    model=AppConfig,
    name="internal_config",
    versions=(SchemaVersion("legacy"), SchemaVersion("current")),
)
```

Constructing either family does not select it globally. A model-only call has no
basis for choosing and raises `SchemaFamilySelectionError`.

## Select one compatibility default deliberately

Applications that still need model-only helper calls can select one family
during configuration startup:

<!-- pv-doc-test: external-families -->
```python
PUBLIC_CONFIG.as_default()
```

After that call, `validate_versioned(AppConfig, data)` delegates to
`PUBLIC_CONFIG`. Repeating `as_default()` on the same family is safe. Selecting a
different second default raises `SchemaFamilySelectionError` and leaves the
first selection unchanged.

The `@versioned_schema` compatibility decorator performs this selection for its
own family automatically. External family construction never does.

## Compilation rules

Compilation happens on first use or through an explicit `compile()` call. It is
thread-safe and idempotent; repeated calls on one family return the same family
and generated model objects.

Declarations are copied into immutable tuples before compilation. Labels must
already be non-empty strings, the final label is current, and custom transitions
must connect adjacent forward labels. Duplicate, reverse, skipped, or otherwise
unreachable transitions fail instead of becoming silent dead code.

The current foundation covers flat patch projections, explicit wire models,
explicit nested-family mappings, upgrades, and downgrades.
