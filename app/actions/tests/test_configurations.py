"""Guards for portal form-rendering constraints.

Each test here pins down a schema property that, when violated, breaks the
Gundi portal form in a way unit-testing the handlers would not catch:
- ajv strict mode rejects unknown keywords (e.g. enumNames)
- rjsf requires ui:order to list every property
- the portal does not coerce numbers nested inside array items
"""
import json

import pytest

from app.actions.configurations import (
    AuthenticateConfig,
    LocationOverride,
    PullObservationsConfig,
    SENSOR_LABELS,
)


def test_ui_order_lists_every_property():
    # rjsf breaks the whole form if ui:order omits any property.
    schema = json.loads(PullObservationsConfig.schema_json())
    order = set(PullObservationsConfig.ui_schema()["ui:order"])
    assert set(schema["properties"]) == order


def test_sensor_options_use_oneof_not_enumnames():
    # enumNames is an rjsf extension that ajv strict mode rejects.
    schema = json.loads(PullObservationsConfig.schema_json())
    items = schema["properties"]["sensors_of_interest"]["items"]
    assert "enumNames" not in items
    assert {opt["const"] for opt in items["oneOf"]} == {c.value for c in SENSOR_LABELS}
    assert all(opt.get("title") for opt in items["oneOf"])


def test_array_item_fields_are_strings():
    # The portal submits numbers nested in array items as strings, so every
    # LocationOverride field and the excluded-ids list must be typed string.
    schema = json.loads(PullObservationsConfig.schema_json())
    override = schema["definitions"]["LocationOverride"]["properties"]
    assert override["installation_id"]["type"] == "string"
    assert override["latitude"]["type"] == "string"
    assert override["longitude"]["type"] == "string"
    assert schema["properties"]["excluded_installations"]["items"]["type"] == "string"


def test_top_level_numeric_field_stays_native():
    # Top-level numbers ARE coerced by the portal, so keep them native-typed.
    schema = json.loads(PullObservationsConfig.schema_json())
    assert schema["properties"]["max_data_age_hours"]["type"] == "integer"


def test_location_override_validates_coordinate_ranges():
    LocationOverride(installation_id="1", latitude="-13.1", longitude="31.8")
    with pytest.raises(ValueError):
        LocationOverride(installation_id="1", latitude="200", longitude="0")
    with pytest.raises(ValueError):
        LocationOverride(installation_id="1", latitude="0", longitude="-190")


def test_actions_are_executable():
    # ExecutableActionMixin drives the portal "trigger" button via is_executable.
    for model in (AuthenticateConfig, PullObservationsConfig):
        from app.actions.core import ExecutableActionMixin
        assert issubclass(model, ExecutableActionMixin)


def test_default_sensors_are_a_subset_of_known_codes():
    from app.actions.configurations import DEFAULT_SENSORS_OF_INTEREST, SensorCode
    assert set(DEFAULT_SENSORS_OF_INTEREST).issubset(set(SensorCode))
