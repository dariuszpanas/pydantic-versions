# pydantic-versions

Bring version control and history to your Pydantic schemas.

<p align="center">
  <img src="https://raw.githubusercontent.com/dariuszpanas/pydantic-versions/main/docs/assets/images/pydantic-versions-hero.png" alt="pydantic-versions logo" width="760">
</p>

`pydantic-versions` lets projects register ordered schema versions, derive historical Pydantic models from a current model, validate historical payloads, render historical config shapes, and upgrade data to the current model.

## Install

```bash
pip install pydantic-versions
```

With `uv`:

```bash
uv add pydantic-versions
```

## Example

Schema versions are independent from software versions. A config payload can declare
the schema it uses, and the latest software can still validate and upgrade it:

```python
from pydantic import BaseModel
from pydantic_versions import (
    SchemaFamily,
    SchemaVersion,
    VersionTransition,
    field_default,
    field_removed,
)


class AppConfig(BaseModel):
    timeout: float = 10.0
    retries: int = 3
    new_feature: bool = False


def upgrade_v1(data: dict) -> dict:
    data.setdefault("new_feature", False)
    return data


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
    transitions=(
        VersionTransition("1", "2", upgrade=upgrade_v1),
    ),
    missing_version="1",
)


result = APP_CONFIG_SCHEMA.validate({"schema_version": "1", "retries": 2})
assert result.current_model == AppConfig(timeout=5.0, retries=2, new_feature=False)

v1_config = APP_CONFIG_SCHEMA.dump(version="1")
assert v1_config == {"timeout": 5.0, "retries": 3, "schema_version": "1"}
```

`missing_version` is only for legacy config files that do not contain a schema
version field. For example, `missing_version="1"` means "if a payload has no
`schema_version`, treat it as an old v1 config." If you do not set it,
unversioned input raises `MissingSchemaVersionError`.

The current model remains ordinary; its growing history can live in another
module. The decorators remain available as a compact compatibility style. The
docs include external-family guidance, a larger nested config example, and
adoption guidance for choosing patches, migrations, metadata, and legacy
unversioned fallbacks.

## Development

Install dependencies:

```bash
uv sync
```

Run the main checks:

```bash
make ci
```

Useful commands:

- `make format`: format with Ruff.
- `make lint`: lint and auto-fix with Ruff.
- `make typecheck`: run `ty`.
- `make test`: run pytest.
- `make docs-build`: build the docs site.
