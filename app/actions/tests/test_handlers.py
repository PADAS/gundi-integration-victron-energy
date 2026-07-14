import datetime

import pytest
import respx
from httpx import Response
from unittest.mock import AsyncMock

import app.actions.handlers as handlers
from app.actions.handlers import (
    action_auth,
    action_pull_observations,
    build_readings,
)
from app.actions.configurations import (
    AuthenticateConfig,
    LocationOverride,
    PullObservationsConfig,
)


VRM = "https://vrmapi.victronenergy.com/v2"
NOW = datetime.datetime.now(datetime.timezone.utc).timestamp()


def diag_record(code, description, value, device="Battery Monitor", ts=None):
    return {
        "code": code,
        "description": description,
        "formattedValue": value,
        "Device": device,
        "instance": 278,
        "timestamp": int(ts if ts is not None else NOW),
    }


@pytest.fixture
def override():
    return LocationOverride(
        installation_id="100001", latitude="-12.345678", longitude="34.567890"
    )


@pytest.fixture
def pull_config(override):
    return PullObservationsConfig(location_overrides=[override])


@pytest.fixture
def mock_integration(mocker):
    integration = mocker.Mock()
    integration.id = "test-integration-id"
    return integration


@pytest.fixture
def patch_pull_dependencies(mocker):
    mocker.patch("app.services.activity_logger.publish_event", AsyncMock())
    mocker.patch.object(
        handlers.client, "get_auth_config",
        return_value=AuthenticateConfig(token="test-token"),
    )
    mocker.patch.object(
        handlers.state_manager, "set_if_absent", AsyncMock(return_value=True)
    )
    mock_log_activity = mocker.patch.object(handlers, "log_action_activity", AsyncMock())
    mock_send = mocker.patch.object(handlers, "send_observations_to_gundi", AsyncMock())
    return mock_send, mock_log_activity


def mock_vrm_account(sites):
    respx.get(f"{VRM}/users/me").mock(
        return_value=Response(200, json={"success": True, "user": {"id": 66846, "name": "Test"}})
    )
    respx.get(f"{VRM}/users/66846/installations").mock(
        return_value=Response(200, json={"success": True, "records": sites})
    )


class TestBuildReadings:

    def test_filters_to_whitelist(self):
        diagnostics = [
            diag_record("V", "Voltage", "53.26 V"),
            diag_record("ip", "Local ip address", "192.168.9.24", device="Gateway"),
        ]
        readings, _ = build_readings(diagnostics, {"V"})
        assert readings == {"Voltage": "53.26 V"}

    def test_prefers_battery_monitor_over_system_overview(self):
        diagnostics = [
            diag_record("bv", "Voltage", "53.00 V", device="System overview"),
            diag_record("V", "Voltage", "53.26 V", device="Battery Monitor"),
        ]
        readings, _ = build_readings(diagnostics, {"V", "bv"})
        assert readings == {"Voltage": "53.26 V"}

    def test_drops_records_far_behind_newest(self):
        old_ts = NOW - 5 * 365 * 24 * 3600
        diagnostics = [
            diag_record("V", "Voltage", "26.54 V"),
            diag_record("PVP", "PV power", "294 W", device="Solar Charger", ts=old_ts),
        ]
        readings, newest_ts = build_readings(diagnostics, {"V", "PVP"})
        assert readings == {"Voltage": "26.54 V"}
        assert newest_ts == int(NOW)

    def test_skips_empty_values(self):
        diagnostics = [diag_record("V", "Voltage", "")]
        readings, _ = build_readings(diagnostics, {"V"})
        assert readings == {}

    def test_suppresses_system_overview_fallback_when_battery_monitor_present(self):
        diagnostics = [
            diag_record("SOC", "State of charge", "95.0 %"),
            diag_record("bs", "Battery SOC", "95.0 %", device="System overview"),
        ]
        readings, _ = build_readings(diagnostics, {"SOC", "bs"})
        assert readings == {"State of charge": "95.0 %"}

    def test_keeps_fallback_when_battery_monitor_absent(self):
        diagnostics = [
            diag_record("bs", "Battery SOC", "95.0 %", device="System overview"),
        ]
        readings, _ = build_readings(diagnostics, {"SOC", "bs"})
        assert readings == {"Battery SOC": "95.0 %"}


@pytest.mark.asyncio
@respx.mock
async def test_action_auth_success(mocker, mock_integration):
    mocker.patch("app.services.activity_logger.publish_event", AsyncMock())
    mock_vrm_account([
        {"idSite": 100001, "name": "Baobab Camp", "last_timestamp": int(NOW - 300)},
    ])
    result = await action_auth(mock_integration, AuthenticateConfig(token="good"))
    assert result["valid_credentials"] is True
    assert result["user_id"] == 66846
    assert result["installations_found"] == 1
    assert result["installations"][0]["installation_id"] == 100001
    assert result["installations"][0]["name"] == "Baobab Camp"


@pytest.mark.asyncio
@respx.mock
async def test_action_auth_bad_token(mocker, mock_integration):
    mocker.patch("app.services.activity_logger.publish_event", AsyncMock())
    respx.get(f"{VRM}/users/me").mock(
        return_value=Response(401, json={"success": False, "errors": "Unauthorized"})
    )
    result = await action_auth(mock_integration, AuthenticateConfig(token="bad"))
    assert result == {"valid_credentials": False}


@pytest.mark.asyncio
@respx.mock
async def test_pull_observations_happy_path(
    mock_integration, pull_config, patch_pull_dependencies
):
    mock_send, _ = patch_pull_dependencies
    mock_vrm_account([
        {"idSite": 100001, "name": "Baobab Camp", "last_timestamp": int(NOW - 300)},
    ])
    respx.get(f"{VRM}/installations/100001/diagnostics").mock(
        return_value=Response(200, json={"success": True, "records": [
            diag_record("V", "Voltage", "53.26 V"),
            diag_record("SOC", "State of charge", "95.0 %"),
            diag_record("us", "Update status", "Idle", device="Gateway"),
        ]})
    )

    result = await action_pull_observations(mock_integration, pull_config)

    assert result["observations_extracted"] == 1
    assert result["installations_skipped"] == 0
    observations = mock_send.call_args.kwargs["observations"]
    obs = observations[0]
    assert obs["source"] == "100001"
    assert obs["source_name"] == "Baobab Camp"
    assert obs["type"] == "stationary-object"
    assert obs["subject_type"] == "static-sensor"
    assert obs["location"] == {"lat": -12.345678, "lon": 34.567890}
    assert obs["additional"] == {"Voltage": "53.26 V", "State of charge": "95.0 %"}


@pytest.mark.asyncio
@respx.mock
async def test_pull_observations_warns_on_unmatched_override(
    mock_integration, pull_config, patch_pull_dependencies
):
    mock_send, mock_log_activity = patch_pull_dependencies
    mock_vrm_account([])  # account has no visible sites

    result = await action_pull_observations(mock_integration, pull_config)

    assert result["observations_extracted"] == 0
    assert result["installations_found"] == 0
    mock_send.assert_not_called()
    mock_log_activity.assert_awaited_once()
    assert "matches no installation" in mock_log_activity.call_args.kwargs["title"]


@pytest.mark.asyncio
@respx.mock
async def test_pull_observations_auto_discovers_without_config(
    mock_integration, patch_pull_dependencies
):
    mock_send, _ = patch_pull_dependencies
    config = PullObservationsConfig()  # zero installation setup
    mock_vrm_account([
        {"idSite": 100001, "name": "Baobab Camp", "last_timestamp": int(NOW - 300)},
    ])
    respx.get(f"{VRM}/installations/100001/diagnostics").mock(
        return_value=Response(200, json={"success": True, "records": [
            diag_record("V", "Voltage", "53.26 V"),
        ]})
    )

    result = await action_pull_observations(mock_integration, config)

    assert result["observations_extracted"] == 1
    obs = mock_send.call_args.kwargs["observations"][0]
    assert obs["source"] == "100001"
    assert obs["location"] == {"lat": 0.0, "lon": 0.0}


@pytest.mark.asyncio
@respx.mock
async def test_pull_observations_excludes_installations(
    mock_integration, patch_pull_dependencies
):
    mock_send, _ = patch_pull_dependencies
    config = PullObservationsConfig(excluded_installations=["100002"])
    mock_vrm_account([
        {"idSite": 100001, "name": "Baobab Camp", "last_timestamp": int(NOW - 300)},
        {"idSite": 100002, "name": "Hilltop Repeater", "last_timestamp": int(NOW - 300)},
    ])
    respx.get(f"{VRM}/installations/100001/diagnostics").mock(
        return_value=Response(200, json={"success": True, "records": [
            diag_record("V", "Voltage", "53.26 V"),
        ]})
    )

    result = await action_pull_observations(mock_integration, config)

    assert result["observations_extracted"] == 1
    assert result["installations_excluded"] == 1
    sources = [o["source"] for o in mock_send.call_args.kwargs["observations"]]
    assert sources == ["100001"]


@pytest.mark.asyncio
@respx.mock
async def test_pull_observations_skips_stale_site(
    mock_integration, pull_config, patch_pull_dependencies
):
    mock_send, mock_log_activity = patch_pull_dependencies
    mock_vrm_account([
        {"idSite": 100001, "name": "Baobab Camp", "last_timestamp": int(NOW - 3 * 24 * 3600)},
    ])

    result = await action_pull_observations(mock_integration, pull_config)

    assert result["observations_extracted"] == 0
    assert result["installations_skipped"] == 1
    mock_send.assert_not_called()
    assert "has not reported" in mock_log_activity.call_args.kwargs["title"]


@pytest.mark.asyncio
@respx.mock
async def test_pull_observations_skips_site_that_never_reported(
    mock_integration, patch_pull_dependencies
):
    mock_send, mock_log_activity = patch_pull_dependencies
    mock_vrm_account([
        {"idSite": 100001, "name": "Baobab Camp"},  # no last_timestamp at all
    ])

    result = await action_pull_observations(mock_integration, PullObservationsConfig())

    assert result["observations_extracted"] == 0
    assert result["installations_skipped"] == 1
    mock_send.assert_not_called()
    title = mock_log_activity.call_args.kwargs["title"]
    assert "has not reported data ever" in title
    assert "1970" not in title


@pytest.mark.asyncio
@respx.mock
async def test_pull_observations_continues_after_site_failure(
    mock_integration, patch_pull_dependencies
):
    mock_send, _ = patch_pull_dependencies
    config = PullObservationsConfig()
    mock_vrm_account([
        {"idSite": 100001, "name": "Baobab Camp", "last_timestamp": int(NOW - 300)},
        {"idSite": 100002, "name": "Hilltop Repeater", "last_timestamp": int(NOW - 300)},
    ])
    broken_site = respx.get(f"{VRM}/installations/100001/diagnostics").mock(
        return_value=Response(404, json={"success": False})
    )
    respx.get(f"{VRM}/installations/100002/diagnostics").mock(
        return_value=Response(200, json={"success": True, "records": [
            diag_record("V", "Voltage", "26.56 V"),
        ]})
    )

    result = await action_pull_observations(mock_integration, config)

    assert result["observations_extracted"] == 1
    assert result["installations_failed"][0]["installation_id"] == 100001
    # Non-transient 4xx must fail the site immediately, not burn retries
    assert broken_site.call_count == 1
    observations = mock_send.call_args.kwargs["observations"]
    assert observations[0]["source"] == "100002"


@pytest.mark.asyncio
@respx.mock
async def test_pull_observations_warning_throttled(
    mocker, mock_integration, pull_config, patch_pull_dependencies
):
    mock_send, mock_log_activity = patch_pull_dependencies
    # State says we already warned about this override recently
    mocker.patch.object(
        handlers.state_manager, "set_if_absent", AsyncMock(return_value=False)
    )
    mock_vrm_account([])  # override 100001 matches no visible site

    result = await action_pull_observations(mock_integration, pull_config)

    assert result["installations_found"] == 0
    mock_log_activity.assert_not_awaited()  # throttled, not re-logged


@pytest.mark.asyncio
@respx.mock
async def test_pull_observations_warns_when_throttle_state_unavailable(
    mocker, mock_integration, pull_config, patch_pull_dependencies
):
    # The throttle is best-effort: if Redis is down, the warning is published
    # and the pull run continues instead of failing.
    mock_send, mock_log_activity = patch_pull_dependencies
    mocker.patch.object(
        handlers.state_manager, "set_if_absent",
        AsyncMock(side_effect=ConnectionError("redis down")),
    )
    mock_vrm_account([])  # override 100001 matches no visible site

    result = await action_pull_observations(mock_integration, pull_config)

    assert result["installations_found"] == 0
    mock_log_activity.assert_awaited_once()  # fail-open: warning still published


@pytest.mark.asyncio
@respx.mock
async def test_pull_observations_unauthorized_mid_pull_fails_run(
    mock_integration, patch_pull_dependencies
):
    # A revoked token mid-pull must fail the whole run (so the portal surfaces
    # the auth error), not be swallowed like a per-site fetch failure.
    mock_send, _ = patch_pull_dependencies
    mock_vrm_account([
        {"idSite": 100001, "name": "Baobab Camp", "last_timestamp": int(NOW - 300)},
    ])
    respx.get(f"{VRM}/installations/100001/diagnostics").mock(
        return_value=Response(401, json={"success": False, "errors": "Unauthorized"})
    )

    with pytest.raises(handlers.client.VRMUnauthorizedException):
        await action_pull_observations(mock_integration, PullObservationsConfig())

    mock_send.assert_not_called()


@pytest.mark.asyncio
@respx.mock
async def test_pull_observations_additional_sensor_codes(
    mock_integration, patch_pull_dependencies
):
    mock_send, _ = patch_pull_dependencies
    config = PullObservationsConfig(
        sensors_of_interest=[],
        additional_sensor_codes=["gs"],
    )
    mock_vrm_account([
        {"idSite": 100001, "name": "Baobab Camp", "last_timestamp": int(NOW - 300)},
    ])
    respx.get(f"{VRM}/installations/100001/diagnostics").mock(
        return_value=Response(200, json={"success": True, "records": [
            diag_record("V", "Voltage", "53.26 V"),
            diag_record("gs", "Genset state", "Stopped", device="Gateway"),
        ]})
    )

    result = await action_pull_observations(mock_integration, config)

    observations = mock_send.call_args.kwargs["observations"]
    assert observations[0]["additional"] == {"Genset state": "Stopped"}
