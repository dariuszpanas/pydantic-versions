from __future__ import annotations

from pydantic_versions import SchemaFamily, SchemaVersion, field_default
from tests.fixtures.external_family.models import ExternalConfig

EXTERNAL_CONFIG_SCHEMA = SchemaFamily(
    model=ExternalConfig,
    name="external_config",
    versions=(
        SchemaVersion("1", patches=(field_default("timeout", 5.0),)),
        SchemaVersion("2"),
    ),
)
