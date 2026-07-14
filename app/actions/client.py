import asyncio
import logging

import httpx

from app.actions.configurations import AuthenticateConfig
from app.services.errors import ConfigurationNotFound
from app.services.utils import find_config_for_action


logger = logging.getLogger(__name__)

VRM_API_BASE_URL = "https://vrmapi.victronenergy.com/v2"


class VRMClientException(Exception):
    def __init__(self, message: str, status_code=500):
        self.status_code = status_code
        self.message = message
        super().__init__(f"{self.status_code}: {self.message}")


class VRMUnauthorizedException(VRMClientException):
    """Raised on 401/403 — the access token is invalid or was revoked."""
    def __init__(self, message: str, status_code=401):
        super().__init__(message, status_code=status_code)


class VRMTransientError(VRMClientException):
    """Raised on 429/5xx — safe to retry. Other 4xx (e.g. a 404 for a deleted
    site) raise httpx.HTTPStatusError and are not retried."""


# A 429's Retry-After is honored up to this bound, so a misbehaving header
# can't stall a run; the retry loop's own backoff still applies on top.
MAX_RETRY_AFTER_SECONDS = 60


def get_auth_config(integration) -> AuthenticateConfig:
    auth_config = find_config_for_action(
        configurations=integration.configurations,
        action_id="auth",
    )
    if not auth_config:
        raise ConfigurationNotFound(
            f"Authentication settings for integration {str(integration.id)} "
            f"are missing. Please fix the integration setup in the portal."
        )
    return AuthenticateConfig.parse_obj(auth_config.data)


def vrm_session(token: str) -> httpx.AsyncClient:
    """One client (connection pool) per action run — VRM calls fan out to
    1-2 requests per installation, so per-request clients would redo the
    TCP+TLS handshake dozens of times per pull."""
    return httpx.AsyncClient(
        base_url=VRM_API_BASE_URL,
        headers={"x-authorization": f"Token {token}"},
        timeout=60,
    )


async def _vrm_get(session: httpx.AsyncClient, path: str, params: dict = None) -> dict:
    response = await session.get(path, params=params)
    if response.status_code in (401, 403):
        raise VRMUnauthorizedException(
            "VRM API rejected the access token. Generate a new token in the "
            "VRM portal (Preferences > Integrations > Access tokens) and update "
            "the Authentication settings in the portal.",
            status_code=response.status_code,
        )
    if response.status_code == 429:
        try:
            retry_after = float(response.headers.get("Retry-After") or 0)
        except ValueError:
            retry_after = 0
        if retry_after > 0:
            await asyncio.sleep(min(retry_after, MAX_RETRY_AFTER_SECONDS))
        raise VRMTransientError("VRM rate limit exceeded", status_code=429)
    if response.status_code >= 500:
        raise VRMTransientError(
            f"VRM server error: {response.status_code}",
            status_code=response.status_code,
        )
    response.raise_for_status()
    return response.json()


async def get_current_user(session: httpx.AsyncClient) -> dict:
    """Validate the token and return the VRM user (id, name, email, country)."""
    data = await _vrm_get(session, "/users/me")
    return data["user"]


async def get_installations(session: httpx.AsyncClient, user_id: int) -> list:
    """All installations visible to the account, with extended attributes."""
    data = await _vrm_get(
        session, f"/users/{user_id}/installations", params={"extended": 1}
    )
    return data.get("records", [])


async def get_diagnostics(session: httpx.AsyncClient, id_site: int, count: int = 1000) -> list:
    """Latest value of every data attribute for an installation."""
    data = await _vrm_get(
        session, f"/installations/{id_site}/diagnostics", params={"count": count}
    )
    return data.get("records", [])
