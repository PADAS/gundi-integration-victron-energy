import pytest
import respx
from httpx import Response
from unittest.mock import AsyncMock

import app.actions.client as client


VRM = "https://vrmapi.victronenergy.com/v2"


def slept_for(mock_sleep):
    """Durations passed to asyncio.sleep, ignoring the event loop's own
    zero-length sleeps (the patch is module-global)."""
    return [c.args[0] for c in mock_sleep.await_args_list if c.args and c.args[0] > 0]


@pytest.mark.asyncio
@respx.mock
async def test_429_sleeps_retry_after_and_raises_transient(mocker):
    mock_sleep = mocker.patch.object(client.asyncio, "sleep", AsyncMock())
    respx.get(f"{VRM}/installations/100001/diagnostics").mock(
        return_value=Response(429, headers={"Retry-After": "7"})
    )
    async with client.vrm_session("token") as session:
        with pytest.raises(client.VRMTransientError):
            await client.get_diagnostics(session, 100001)
    assert slept_for(mock_sleep) == [7.0]


@pytest.mark.asyncio
@respx.mock
async def test_429_retry_after_is_capped(mocker):
    mock_sleep = mocker.patch.object(client.asyncio, "sleep", AsyncMock())
    respx.get(f"{VRM}/installations/100001/diagnostics").mock(
        return_value=Response(429, headers={"Retry-After": "3600"})
    )
    async with client.vrm_session("token") as session:
        with pytest.raises(client.VRMTransientError):
            await client.get_diagnostics(session, 100001)
    assert slept_for(mock_sleep) == [client.MAX_RETRY_AFTER_SECONDS]


@pytest.mark.asyncio
@respx.mock
async def test_429_without_retry_after_does_not_sleep(mocker):
    mock_sleep = mocker.patch.object(client.asyncio, "sleep", AsyncMock())
    respx.get(f"{VRM}/installations/100001/diagnostics").mock(
        return_value=Response(429)
    )
    async with client.vrm_session("token") as session:
        with pytest.raises(client.VRMTransientError):
            await client.get_diagnostics(session, 100001)
    assert slept_for(mock_sleep) == []


@pytest.mark.asyncio
@respx.mock
async def test_5xx_raises_transient():
    respx.get(f"{VRM}/installations/100001/diagnostics").mock(
        return_value=Response(503)
    )
    async with client.vrm_session("token") as session:
        with pytest.raises(client.VRMTransientError):
            await client.get_diagnostics(session, 100001)


@pytest.mark.asyncio
@respx.mock
async def test_non_transient_4xx_is_not_retryable():
    # A 404 must surface as httpx.HTTPStatusError, which the handlers'
    # retry loops do NOT retry on.
    import httpx

    respx.get(f"{VRM}/installations/100001/diagnostics").mock(
        return_value=Response(404)
    )
    async with client.vrm_session("token") as session:
        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            await client.get_diagnostics(session, 100001)
    assert not isinstance(excinfo.value, client.VRMTransientError)


@pytest.mark.asyncio
@respx.mock
async def test_401_raises_unauthorized():
    respx.get(f"{VRM}/users/me").mock(return_value=Response(401))
    async with client.vrm_session("token") as session:
        with pytest.raises(client.VRMUnauthorizedException):
            await client.get_current_user(session)
