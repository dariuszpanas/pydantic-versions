import pytest
from pydantic import BaseModel

from pydantic_versions import (
    SchemaFamily,
    SchemaVersion,
    VersionTransition,
    dump_versioned,
    field_default,
    validate_versioned,
)


class AppConfig(BaseModel):
    timeout: float = 10.0


def upgrade_v1_to_v2(data: dict) -> dict:
    data.setdefault("timeout", 5.0)
    return data


def downgrade_v2_to_v1(data: dict) -> dict:
    return data


APP_CONFIG_SCHEMA = SchemaFamily(
    model=AppConfig,
    name="app_config",
    versions=(
        SchemaVersion("1", patches=(field_default("timeout", 5.0),)),
        SchemaVersion("2"),
    ),
    transitions=(
        VersionTransition(
            "1",
            "2",
            upgrade=upgrade_v1_to_v2,
            downgrade=downgrade_v2_to_v1,
            downgrade_semantics="exact",
        ),
    ),
)


def test_consumer_scenario_validate_current_version() -> None:
    data = {"schema_version": "2", "timeout": 15.5}
    validated = validate_versioned(APP_CONFIG_SCHEMA, data)
    assert validated.current_model.timeout == 15.5
    assert validated.source_version == "2"


def test_consumer_scenario_validate_historical_version() -> None:
    # Notice that v1 is missing "timeout" and we expect our upgrade_v1_to_v2
    # to add a default of 5.0 if it's missing (though Pydantic does that too
    # due to field_default). Let's test the explicit upgrade logic.
    data = {"schema_version": "1"}
    validated = validate_versioned(APP_CONFIG_SCHEMA, data)
    assert validated.current_model.timeout == 5.0
    assert validated.source_version == "1"


def test_consumer_scenario_dump_historical_version() -> None:
    current_config = AppConfig(timeout=25.0)
    rendered = dump_versioned(APP_CONFIG_SCHEMA, version="1", data=current_config)
    assert rendered == {"schema_version": "1", "timeout": 25.0}


def test_consumer_scenario_missing_version_raises_error() -> None:
    # If the payload lacks schema_version, it should raise
    from pydantic_versions.exceptions import MissingSchemaVersionError

    with pytest.raises(MissingSchemaVersionError):
        validate_versioned(APP_CONFIG_SCHEMA, {"timeout": 10.0})
