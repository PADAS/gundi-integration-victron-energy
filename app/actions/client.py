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


async def _vrm_get(path: str, token: str, params: dict = None) -> dict:
    url = f"{VRM_API_BASE_URL}{path}"
    headers = {"x-authorization": f"Token {token}"}
    async with httpx.AsyncClient(timeout=60) as session:
        response = await session.get(url, headers=headers, params=params)
    if response.status_code in (401, 403):
        raise VRMUnauthorizedException(
            "VRM API rejected the access token. Generate a new token in the "
            "VRM portal (Preferences > Integrations > Access tokens) and update "
            "the Authentication settings in the portal.",
            status_code=response.status_code,
        )
    response.raise_for_status()
    return response.json()


async def get_current_user(token: str) -> dict:
    """Validate the token and return the VRM user (id, name, email, country)."""
    data = await _vrm_get("/users/me", token)
    return data["user"]


async def get_installations(token: str, user_id: int) -> list:
    """All installations visible to the account, with extended attributes."""
    data = await _vrm_get(
        f"/users/{user_id}/installations", token, params={"extended": 1}
    )
    return data.get("records", [])


async def get_diagnostics(token: str, id_site: int, count: int = 1000) -> list:
    """Latest value of every data attribute for an installation."""
    data = await _vrm_get(
        f"/installations/{id_site}/diagnostics", token, params={"count": count}
    )
    return data.get("records", [])
