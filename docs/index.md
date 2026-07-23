# pydantic-versions

<p align="center">
  <img src="assets/images/pydantic-versions-hero.png" alt="pydantic-versions logo" width="760">
</p>

`pydantic-versions` brings explicit schema versioning to Pydantic models.

It focuses on config and API payloads where schema versions are independent from
software versions. External schema families keep growing history outside the
current application model, derive historical Pydantic models, validate
historical payloads, render historical config shapes, and upgrade inputs through
explicit transitions. Decorators remain available as a compact compatibility
style.

## Install

```bash
pip install pydantic-versions
```

With `uv`:

```bash
uv add pydantic-versions
```

## Quick example

This example declares history without decorating the current model. The input
validates as version `1`, then upgrades to the current model.

```python
from pydantic import BaseModel
from pydantic_versions import SchemaFamily, SchemaVersion, field_default


class AppConfig(BaseModel):
    timeout: float = 10.0


APP_CONFIG_SCHEMA = SchemaFamily(
    model=AppConfig,
    name="app_config",
    versions=(
        SchemaVersion("1", patches=(field_default("timeout", 5.0),)),
        SchemaVersion("2"),
    ),
)

result = APP_CONFIG_SCHEMA.validate({"schema_version": "1"})
assert result.current_model.timeout == 5.0
```

See the user guide for [external families](guide/external-families.md), version
discovery, `missing_version`, schema patches, migrations, rendering historical
configs, and the [complex config example](guide/complex-config-example.md).
