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


class InstallationConfig(pydantic.BaseModel):
    """One VRM installation to monitor as a stationary subject."""
    installation_id: int = pydantic.Field(
        ...,
        title="Installation ID",
        description="The VRM installation id, visible in the URL when viewing "
                    "the installation in the VRM dashboard.",
    )
    latitude: float = pydantic.Field(
        ...,
        ge=-90,
        le=90,
        title="Latitude",
        description="Subject location latitude in decimal degrees. The VRM API "
                    "does not provide coordinates, so the location is fixed here.",
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


class PullObservationsConfig(PullActionConfiguration):
    installations: List[InstallationConfig] = FieldWithUIOptions(
        ...,
        title="Installations",
        description="VRM installations to monitor. Each becomes a stationary "
                    "subject in EarthRanger at the configured location.",
        min_items=1,
    )
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
        description="Any other VRM data-attribute codes to include (case-sensitive; "
                    "see https://vrmapi.victronenergy.com/v2/data-attributes).",
    )
    max_data_age_hours: int = FieldWithUIOptions(
        24,
        ge=1,
        le=720,
        title="Maximum data age (hours)",
        description="If an installation has not reported for longer than this, "
                    "no observation is sent until data resumes.",
    )

    ui_global_options: GlobalUISchemaOptions = GlobalUISchemaOptions(
        order=[
            "installations",
            "subject_subtype",
            "sensors_of_interest",
            "additional_sensor_codes",
            "max_data_age_hours",
            "run_on_schedule",  # inherited from PullActionConfiguration
        ],
    )
