# pydantic-versions

`pydantic-versions` is an early-stage package for versioned Pydantic schemas.

It focuses on config-style data where schema versions are independent from software versions. The core API lets projects register ordered schema versions, derive historical Pydantic models from a current model, validate historical payloads, render historical config shapes, and upgrade inputs to the current model through explicit migrations.

## Quick example

This example uses an explicit historical schema version. The input validates as
version `1`, then upgrades to the current model.

```python
from pydantic import BaseModel
from pydantic_versions import field_default, schema_version, validate_versioned, versioned_schema


@versioned_schema(name="app_config", versions=["1", "2"], current="2")
@schema_version("1", patches=[field_default("timeout", 5.0)])
class AppConfig(BaseModel):
    timeout: float = 10.0


result = validate_versioned(AppConfig, {"schema_version": "1"})
assert result.current_model.timeout == 5.0
```

See the user guide for the important details around version discovery,
`missing_version`, schema patches, migrations, rendering historical configs, and
the [complex config example](guide/complex-config-example.md).
