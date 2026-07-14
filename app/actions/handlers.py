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
            "lat": float(override.latitude) if override else 0.0,
            "lon": float(override.longitude) if override else 0.0,
        },
        "additional": readings,
    }


async def warn_throttled(integration_id: str, key: str, title: str, data: dict = None):
    """Emit a portal WARNING at most once per day per key."""
    try:
        first_in_window = await state_manager.set_if_absent(
            integration_id=integration_id,
            action_id="pull_observations",
            source_id=f"warn.{key}",
            ttl_seconds=WARNING_THROTTLE_SECONDS,
        )
    except Exception as throttle_error:
        # The throttle is best-effort noise control: if the state store is
        # unavailable, publish the warning rather than failing the pull run.
        logger.warning(f"Warning throttle unavailable ({throttle_error}). Publishing the warning.")
        first_in_window = True
    if not first_in_window:
        logger.warning(f"{title} (portal warning throttled)")
        return
    await log_action_activity(
        integration_id=integration_id,
        action_id="pull_observations",
        level=LogLevel.WARNING,
        title=title,
        data=data or {},
    )


@activity_logger()
async def action_auth(integration, action_config: AuthenticateConfig):
    # Log the id only: the integration object carries the auth config,
    # including the VRM access token in plaintext.
    logger.info(f"Executing auth action for integration {integration.id}...")
    token = action_config.token.get_secret_value()
    async with client.vrm_session(token) as session:
        try:
            user = await client.get_current_user(session)
        except client.VRMUnauthorizedException:
            return {"valid_credentials": False}
        # List the visible installations so the user can copy the IDs straight
        # from the Test Connection result when filling the pull_observations form.
        installations = await client.get_installations(session, user["id"])
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
        f"Executing pull_observations action for integration {integration.id}..."
    )
    integration_id = str(integration.id)
    auth_config = client.get_auth_config(integration)
    token = auth_config.token.get_secret_value()

    sensor_codes = {c.value for c in action_config.sensors_of_interest}
    sensor_codes |= set(action_config.additional_sensor_codes)
    max_age_seconds = action_config.max_data_age_hours * 3600
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()

    observations = []
    skipped = []
    failed = []
    async with client.vrm_session(token) as session:
        sites_by_id = {}
        async for attempt in stamina.retry_context(
            on=httpx.HTTPError, attempts=3, wait_initial=2.0, wait_max=30.0
        ):
            with attempt:
                user = await client.get_current_user(session)
                sites_by_id = {
                    s["idSite"]: s
                    for s in await client.get_installations(session, user["id"])
                }

        # Config ids arrive as strings from the portal form; VRM idSite is an int.
        # Normalize on strings for all comparisons.
        overrides_by_id = {str(o.installation_id): o for o in action_config.location_overrides}
        excluded = {str(i) for i in action_config.excluded_installations}
        site_ids = {str(id_site) for id_site in sites_by_id}
        for id_site in overrides_by_id.keys() - site_ids:
            await warn_throttled(
                integration_id,
                f"invisible.{id_site}",
                f"Location override for installation {id_site} matches no installation "
                f"visible to this VRM account. Check the installation ID.",
            )

        for id_site, site in sites_by_id.items():
            site_key = str(id_site)
            if site_key in excluded:
                continue
            last_timestamp = site.get("last_timestamp")
            if not last_timestamp or now - last_timestamp > max_age_seconds:
                skipped.append(id_site)
                last_seen = (
                    f"since {datetime.datetime.fromtimestamp(last_timestamp, tz=datetime.timezone.utc).isoformat()}"
                    if last_timestamp else "ever"
                )
                await warn_throttled(
                    integration_id,
                    f"stale.{id_site}",
                    f"Installation {id_site} ({site.get('name')}) has not reported "
                    f"data {last_seen}. Skipping until data resumes.",
                )
                continue
            try:
                diagnostics = []
                async for attempt in stamina.retry_context(
                    on=httpx.HTTPError, attempts=3, wait_initial=2.0, wait_max=30.0
                ):
                    with attempt:
                        diagnostics = await client.get_diagnostics(session, id_site)
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
                    site, overrides_by_id.get(site_key), readings, newest_ts,
                    action_config.subject_subtype,
                )
            )

    if observations:
        # No retry wrapper here: send_observations_to_gundi retries internally,
        # and re-sending a batch whose POST succeeded but whose response was
        # lost would deliver duplicate observations.
        await send_observations_to_gundi(
            observations=observations, integration_id=integration_id
        )

    result = {
        "observations_extracted": len(observations),
        "installations_found": len(sites_by_id),
        "installations_excluded": len(excluded & site_ids),
        "installations_skipped": len(skipped),
    }
    if failed:
        result["installations_failed"] = failed
    return result
