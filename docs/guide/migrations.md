# Migrations

Migrations upgrade already-validated historical data toward the current model.
They are optional: if adjacent versions are compatible, identity steps are used.

```python
from pydantic_versions import migration


@migration(AppConfig, "1", "2")
def migrate_v1_to_v2(data: dict) -> dict:
    data.setdefault("new_feature", False)
    return data
```

## Direction

The first release supports forward migrations only. A migration must move from an
earlier declared version to a later declared version.

```python
@versioned_schema(name="app_config", versions=["1", "2", "3"], current="3")
class AppConfig(BaseModel):
    ...
```

Valid:

```python
@migration(AppConfig, "1", "2")
def migrate_v1_to_v2(data: dict) -> dict:
    return data
```

Invalid:

```python
@migration(AppConfig, "3", "1")
def downgrade(data: dict) -> dict:
    return data
```

## Chained upgrades

When validating version `1` against current version `3`, migrations are checked
between adjacent declared versions:

```text
1 -> 2 -> 3
```

Registered migrations run in that order. Missing migration steps are treated as
identity steps, which is useful when a version only changed defaults or when the
historical model already renders a current-compatible payload.

## Return values

Migration functions receive and return dictionaries using current field names.
Returning anything other than `dict` raises `InvalidMigrationError`.
