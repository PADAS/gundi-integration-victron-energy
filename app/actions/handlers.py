import datetime
import logging

import httpx
import stamina
from gundi_core.events import LogLevel

import app.actions.client as client

from app.services.activity_logger import activity_logger, log_action_activity
from app.services.action_scheduler import crontab_schedule
from app.services.gundi import send_observations_to_gundi
from app.services.state import IntegrationStateManager
from app.actions.configurations import AuthenticateConfig, PullObservationsConfig


logger = logging.getLogger(__name__)
state_manager = IntegrationStateManager()

# A record more than this far behind the site's newest record is a leftover
# from replaced/dead hardware and must not be presented as current.
MAX_RECORD_LAG_SECONDS = 3600

# Same reading can come from several devices; highest priority wins the dedup.
DEVICE_PRIORITY = {
    "Battery Monitor": 0,
    "Solar Charger": 1,
    "System overview": 2,
}

# System-overview attributes that duplicate a Battery Monitor reading under a
# different description ("Battery SOC" vs "State of charge"). The fallback is
# suppressed when the preferred code is present and fresh.
FALLBACK_CODES = {
    "bv": "V",    # Voltage
    "bc": "I",    # Current
    "bs": "SOC",  # Battery SOC vs State of charge
    "bp": None,   # Battery Power (no BM equivalent, never suppressed)
}

WARNING_THROTTLE_SECONDS = 24 * 3600


def build_readings(diagnostics: list, sensor_codes: set) -> tuple:
    """Filter diagnostics records to the whitelisted, fresh, deduped readings.

    Returns ({description: formattedValue}, newest_timestamp).
    """
    candidates = [
        r for r in diagnostics
        if r.get("code") in sensor_codes and r.get("formattedValue") not in ("", None)
    ]
    if not candidates:
        return {}, 0
    newest_ts = max(r["timestamp"] for r in candidates)
    fresh_codes = {
        r["code"] for r in candidates
        if newest_ts - r["timestamp"] <= MAX_RECORD_LAG_SECONDS
    }
    candidates = [
        r for r in candidates
        if FALLBACK_CODES.get(r["code"]) not in fresh_codes
    ]
    # Prefer higher-priority devices; sort so they land first, then keep first per name
    candidates.sort(key=lambda r: DEVICE_PRIORITY.get(r.get("Device"), 99))
    readings = {}
    for record in candidates:
        if newest_ts - record["timestamp"] > MAX_RECORD_LAG_SECONDS:
            continue
        name = record["description"]
        if name in readings:
            continue
        readings[name] = record["formattedValue"]
    return readings, newest_ts


def build_observation(site, override, readings: dict, newest_ts: int, subject_subtype: str) -> dict:
    return {
        # idSite, not the GX device identifier: survives gateway hardware swaps
        "source": str(site["idSite"]),
        "source_name": (override.subject_name if override else None) or site.get("name") or str(site["idSite"]),
        "type": "stationary-object",
        "subject_type": subject_subtype,
        "recorded_at": datetime.datetime.fromtimestamp(
            newest_ts, tz=datetime.timezone.utc
        ).isoformat(),
        "location": {
            # VRM has no coordinates; without an override the subject lands at
            # 0,0 and is repositioned on the EarthRanger side.
            "lat": override.latitude if override else 0.0,
            "lon": override.longitude if override else 0.0,
        },
        "additional": readings,
    }


async def warn_throttled(integration_id: str, key: str, title: str, data: dict = None):
    """Emit a portal WARNING at most once per day per key."""
    state = await state_manager.get_state(integration_id, "pull_observations", f"warn.{key}")
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    if state and now - state.get("warned_at", 0) < WARNING_THROTTLE_SECONDS:
        logger.warning(f"{title} (portal warning throttled)")
        return
    await log_action_activity(
        integration_id=integration_id,
        action_id="pull_observations",
        level=LogLevel.WARNING,
        title=title,
        data=data or {},
    )
    await state_manager.set_state(
        integration_id, "pull_observations", {"warned_at": now}, f"warn.{key}"
    )


@activity_logger()
async def action_auth(integration, action_config: AuthenticateConfig):
    logger.info(f"Executing auth action with integration {integration}...")
    token = action_config.token.get_secret_value()
    try:
        user = await client.get_current_user(token)
    except client.VRMUnauthorizedException:
        return {"valid_credentials": False}
    # List the visible installations so the user can copy the IDs straight
    # from the Test Connection result when filling the pull_observations form.
    installations = await client.get_installations(token, user["id"])
    return {
        "valid_credentials": True,
        "user_id": user.get("id"),
        "user_name": user.get("name"),
        "installations_found": len(installations),
        "installations": [
            {
                "installation_id": s["idSite"],
                "name": s.get("name"),
                "last_data_at": datetime.datetime.fromtimestamp(
                    s["last_timestamp"], tz=datetime.timezone.utc
                ).isoformat() if s.get("last_timestamp") else None,
            }
            for s in installations
        ],
    }


@crontab_schedule("*/10 * * * *")
@activity_logger()
async def action_pull_observations(integration, action_config: PullObservationsConfig):
    logger.info(
        f"Executing pull_observations action with integration {integration}..."
    )
    integration_id = str(integration.id)
    auth_config = client.get_auth_config(integration)
    token = auth_config.token.get_secret_value()

    sensor_codes = {c.value for c in action_config.sensors_of_interest}
    sensor_codes |= set(action_config.additional_sensor_codes)
    max_age_seconds = action_config.max_data_age_hours * 3600
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()

    user = await client.get_current_user(token)
    sites_by_id = {}
    async for attempt in stamina.retry_context(on=httpx.HTTPError, attempts=3):
        with attempt:
            sites_by_id = {
                s["idSite"]: s
                for s in await client.get_installations(token, user["id"])
            }

    overrides_by_id = {o.installation_id: o for o in action_config.location_overrides}
    excluded = set(action_config.excluded_installations)
    for id_site in overrides_by_id.keys() - sites_by_id.keys():
        await warn_throttled(
            integration_id,
            f"invisible.{id_site}",
            f"Location override for installation {id_site} matches no installation "
            f"visible to this VRM account. Check the installation ID.",
        )

    observations = []
    skipped = []
    failed = []
    for id_site, site in sites_by_id.items():
        if id_site in excluded:
            continue
        if now - site.get("last_timestamp", 0) > max_age_seconds:
            skipped.append(id_site)
            await warn_throttled(
                integration_id,
                f"stale.{id_site}",
                f"Installation {id_site} ({site.get('name')}) has not reported "
                f"since {datetime.datetime.fromtimestamp(site.get('last_timestamp', 0), tz=datetime.timezone.utc).isoformat()}. "
                f"Skipping until data resumes.",
            )
            continue
        try:
            diagnostics = []
            async for attempt in stamina.retry_context(on=httpx.HTTPError, attempts=3):
                with attempt:
                    diagnostics = await client.get_diagnostics(token, id_site)
        except client.VRMUnauthorizedException:
            raise
        except httpx.HTTPError as e:
            logger.exception(f"Failed to fetch diagnostics for installation {id_site}")
            failed.append({"installation_id": id_site, "error": str(e)})
            continue

        readings, newest_ts = build_readings(diagnostics, sensor_codes)
        if not readings:
            skipped.append(id_site)
            logger.info(f"Installation {id_site}: no matching fresh readings, skipping.")
            continue
        observations.append(
            build_observation(
                site, overrides_by_id.get(id_site), readings, newest_ts,
                action_config.subject_subtype,
            )
        )

    if observations:
        async for attempt in stamina.retry_context(on=httpx.HTTPError, attempts=3):
            with attempt:
                await send_observations_to_gundi(
                    observations=observations, integration_id=integration_id
                )

    result = {
        "observations_extracted": len(observations),
        "installations_found": len(sites_by_id),
        "installations_excluded": len(excluded & sites_by_id.keys()),
        "installations_skipped": len(skipped),
    }
    if failed:
        result["installations_failed"] = failed
    return result
