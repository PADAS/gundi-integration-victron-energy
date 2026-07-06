from enum import Enum
from typing import List, Optional

import pydantic

from app.services.utils import FieldWithUIOptions, GlobalUISchemaOptions, UIOptions
from .core import AuthActionConfiguration, PullActionConfiguration, ExecutableActionMixin


class SensorCode(str, Enum):
    """Curated VRM data-attribute codes (case-sensitive, global across accounts).

    The human-readable name and unit for each reading come from the VRM API
    itself (record `description` / `formattedValue`), so this enum only curates
    which attributes are worth showing — it never renames them.
    """
    # Battery Monitor
    VOLTAGE = "V"                    # Voltage
    BATTERY_TEMPERATURE = "BT"       # Battery temperature
    CONSUMED_AMPHOURS = "CE"         # Consumed Amphours
    CURRENT = "I"                    # Current
    MAX_CELL_VOLTAGE = "McV"         # Maximum cell voltage
    MIN_CELL_VOLTAGE = "mcV"         # Minimum cell voltage
    MAX_CELL_TEMPERATURE = "McT"     # Maximum cell temperature
    MIN_CELL_TEMPERATURE = "mcT"     # Minimum cell temperature
    STATE_OF_CHARGE = "SOC"          # State of charge
    TIME_TO_GO = "TTG"               # Time to go
    # Solar Charger
    PV_POWER = "PVP"                 # PV power
    PV_VOLTAGE = "PVV"               # PV voltage
    YIELD_TODAY = "YT"               # Yield today
    YIELD_YESTERDAY = "YY"           # Yield yesterday
    CHARGE_STATE = "ScS"             # Charge state
    CHARGER_ERROR_CODE = "ScERR"     # Error code
    # System overview (fallbacks for sites without a Battery Monitor)
    SYSTEM_VOLTAGE = "bv"            # Voltage
    SYSTEM_CURRENT = "bc"            # Current
    SYSTEM_BATTERY_SOC = "bs"        # Battery SOC
    SYSTEM_BATTERY_POWER = "bp"      # Battery Power
    PV_DC_COUPLED = "Pdc"            # PV - DC-coupled
    DC_SYSTEM = "dc"                 # DC System


# Human labels shown next to each checkbox in the portal ("enumNames").
SENSOR_LABELS = {
    SensorCode.VOLTAGE: "Voltage (V) — Battery Monitor",
    SensorCode.BATTERY_TEMPERATURE: "Battery temperature (BT)",
    SensorCode.CONSUMED_AMPHOURS: "Consumed Amphours (CE)",
    SensorCode.CURRENT: "Current (I) — Battery Monitor",
    SensorCode.MAX_CELL_VOLTAGE: "Maximum cell voltage (McV)",
    SensorCode.MIN_CELL_VOLTAGE: "Minimum cell voltage (mcV)",
    SensorCode.MAX_CELL_TEMPERATURE: "Maximum cell temperature (McT)",
    SensorCode.MIN_CELL_TEMPERATURE: "Minimum cell temperature (mcT)",
    SensorCode.STATE_OF_CHARGE: "State of charge (SOC)",
    SensorCode.TIME_TO_GO: "Time to go (TTG)",
    SensorCode.PV_POWER: "PV power (PVP)",
    SensorCode.PV_VOLTAGE: "PV voltage (PVV)",
    SensorCode.YIELD_TODAY: "Yield today (YT)",
    SensorCode.YIELD_YESTERDAY: "Yield yesterday (YY)",
    SensorCode.CHARGE_STATE: "Charge state (ScS)",
    SensorCode.CHARGER_ERROR_CODE: "Solar charger error code (ScERR)",
    SensorCode.SYSTEM_VOLTAGE: "Voltage (bv) — System overview fallback",
    SensorCode.SYSTEM_CURRENT: "Current (bc) — System overview fallback",
    SensorCode.SYSTEM_BATTERY_SOC: "Battery SOC (bs) — System overview fallback",
    SensorCode.SYSTEM_BATTERY_POWER: "Battery power (bp) — System overview fallback",
    SensorCode.PV_DC_COUPLED: "PV - DC-coupled (Pdc)",
    SensorCode.DC_SYSTEM: "DC System (dc)",
}

DEFAULT_SENSORS_OF_INTEREST = [
    SensorCode.VOLTAGE,
    SensorCode.BATTERY_TEMPERATURE,
    SensorCode.CONSUMED_AMPHOURS,
    SensorCode.CURRENT,
    SensorCode.MAX_CELL_VOLTAGE,
    SensorCode.MIN_CELL_VOLTAGE,
    SensorCode.STATE_OF_CHARGE,
    SensorCode.TIME_TO_GO,
    SensorCode.PV_POWER,
    SensorCode.YIELD_TODAY,
    SensorCode.CHARGE_STATE,
    SensorCode.CHARGER_ERROR_CODE,
    SensorCode.SYSTEM_VOLTAGE,
    SensorCode.SYSTEM_CURRENT,
    SensorCode.SYSTEM_BATTERY_SOC,
    SensorCode.PV_DC_COUPLED,
    SensorCode.DC_SYSTEM,
]


class LocationOverride(pydantic.BaseModel):
    """Optional placement/naming for one auto-discovered installation."""
    installation_id: int = pydantic.Field(
        ...,
        title="Installation ID",
        description="The VRM installation id. Run 'Test Connection' on the "
                    "authentication section to list all installations "
                    "available to your token, with their ids and names.",
    )
    latitude: float = pydantic.Field(
        ...,
        ge=-90,
        le=90,
        title="Latitude",
        description="Subject location latitude in decimal degrees.",
    )
    longitude: float = pydantic.Field(
        ...,
        ge=-180,
        le=180,
        title="Longitude",
        description="Subject location longitude in decimal degrees.",
    )
    subject_name: Optional[str] = pydantic.Field(
        None,
        title="Subject name override",
        description="Optional name for the subject in EarthRanger. "
                    "Defaults to the VRM site name.",
    )


class AuthenticateConfig(AuthActionConfiguration, ExecutableActionMixin):
    token: pydantic.SecretStr = FieldWithUIOptions(
        ...,
        format="password",
        title="VRM access token",
        description="Access token generated in the VRM portal under "
                    "Preferences > Integrations > Access tokens. "
                    "Username/password login is not supported.",
        ui_options=UIOptions(
            widget="password",
        ),
    )

    ui_global_options: GlobalUISchemaOptions = GlobalUISchemaOptions(
        order=["token"],
    )

    class Config:
        title = "Authentication"


class PullObservationsConfig(PullActionConfiguration, ExecutableActionMixin):
    """All installations visible to the VRM token are synced automatically —
    no per-installation setup is required. Each becomes a stationary subject
    in EarthRanger."""
    subject_subtype: str = FieldWithUIOptions(
        "sensor",
        title="Subject subtype",
        description="EarthRanger subject subtype applied to all subjects "
                    "created by this connection.",
    )
    sensors_of_interest: List[SensorCode] = FieldWithUIOptions(
        DEFAULT_SENSORS_OF_INTEREST,
        title="Sensors of interest",
        description="Readings to include as subject details in EarthRanger. "
                    "Names and units come from VRM automatically.",
        ui_options=UIOptions(
            widget="checkboxes",
        ),
    )
    additional_sensor_codes: List[str] = FieldWithUIOptions(
        [],
        title="Additional sensor codes",
        description="Optional, for advanced users: any other VRM data-attribute "
                    "codes to include beyond the list above. Codes are "
                    "case-sensitive, e.g. 'PVV' (PV voltage), 'gs' (generator "
                    "state), 'mcT' (minimum cell temperature). Most setups can "
                    "leave this empty.",
    )
    max_data_age_hours: int = FieldWithUIOptions(
        24,
        ge=1,
        le=720,
        title="Maximum data age (hours)",
        description="If an installation has not reported for longer than this, "
                    "no observation is sent until data resumes.",
    )
    location_overrides: List[LocationOverride] = FieldWithUIOptions(
        [],
        title="Location overrides",
        description="Optional: place specific installations on the map. The VRM "
                    "API does not provide coordinates, so installations without "
                    "an override here are created at latitude 0, longitude 0 "
                    "and can be repositioned on the EarthRanger side.",
    )
    excluded_installations: List[int] = FieldWithUIOptions(
        [],
        title="Excluded installations",
        description="Optional: VRM installation ids that should NOT be synced "
                    "to EarthRanger.",
    )
    # Inherited from PullActionConfiguration; hidden from the portal form —
    # all our connections run on the schedule.
    run_on_schedule: bool = FieldWithUIOptions(
        True,
        title="Run On Schedule",
        ui_options=UIOptions(
            widget="hidden",
        ),
    )

    ui_global_options: GlobalUISchemaOptions = GlobalUISchemaOptions(
        order=[
            "subject_subtype",
            "sensors_of_interest",
            "additional_sensor_codes",
            "max_data_age_hours",
            "location_overrides",
            "excluded_installations",
            "run_on_schedule",  # inherited from PullActionConfiguration
        ],
    )

    class Config:
        title = "Configuration Settings"

        @staticmethod
        def schema_extra(schema: dict, model) -> None:
            # Inline the sensor options with human-readable labels: the portal's
            # form renderer doesn't resolve $ref inside array items. Labels use
            # the standard oneOf/const/title form — NOT "enumNames", which is an
            # rjsf extension that ajv (strict mode) rejects as an unknown keyword.
            sensors = schema.get("properties", {}).get("sensors_of_interest")
            if sensors is not None:
                sensors["items"] = {
                    "type": "string",
                    "oneOf": [
                        {"const": code.value, "title": label}
                        for code, label in SENSOR_LABELS.items()
                    ],
                }
                sensors["uniqueItems"] = True
            schema.get("definitions", {}).pop("SensorCode", None)
