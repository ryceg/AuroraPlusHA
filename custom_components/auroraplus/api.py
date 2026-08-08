import logging
from typing import Any

import requests as plain_requests
from auroraplus import AuroraPlusApi, AuroraPlusAuthenticationError
from requests.exceptions import HTTPError

from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
)


_LOGGER = logging.getLogger(__name__)

# Aurora's WAF rejects the default python-requests User-Agent on some
# endpoints, so always present as a browser.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


class OAuthRefreshExpired(Exception):
    """The stored OAuth refresh_token grant is expired or revoked.

    Only user re-authentication (pasting a fresh token JSON) can recover.
    """


def oauth_refresh_token(token: dict[str, Any]) -> dict[str, Any] | None:
    """Use the OAuth B2C refresh_token to obtain a fresh id_token and refresh_token.

    Aurora's API no longer returns a RefreshToken cookie from the LoginToken
    endpoint, so the library's built-in refresh mechanism fails. This function
    uses the OAuth refresh_token (from the original auth) to get a new id_token,
    which can then be exchanged for a new access_token via LoginToken.

    Returns the new token dict on success, None on a transient failure
    (network, WAF, server error), and raises OAuthRefreshExpired when the
    grant itself is rejected — the B2C refresh_token only lives a few hours,
    so callers must keep it rolled (see proactive_token_roll).
    """
    refresh_token = token.get("refresh_token")
    if not refresh_token:
        raise OAuthRefreshExpired("no OAuth refresh_token stored")

    try:
        r = plain_requests.post(
            AuroraPlusApi.TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": AuroraPlusApi.CLIENT_ID,
                "scope": "openid offline_access",
            },
            headers={"User-Agent": BROWSER_UA},
            timeout=30,
        )
    except Exception as e:
        _LOGGER.warning(f"OAuth refresh_token exchange failed (transient): {e}")
        return None

    if r.status_code == 400 and "invalid_grant" in r.text:
        _LOGGER.warning(f"OAuth refresh_token rejected: {r.text[:300]}")
        raise OAuthRefreshExpired("refresh_token grant expired or revoked")

    try:
        r.raise_for_status()
        new_token = r.json()
    except Exception as e:
        _LOGGER.warning(
            f"OAuth refresh_token exchange failed: {e}; "
            f"body: {getattr(r, 'text', '')[:300]}"
        )
        return None

    _LOGGER.debug("OAuth refresh_token exchange succeeded")
    return new_token


def proactive_token_roll(api: AuroraPlusApi) -> bool:
    """Roll the OAuth refresh_token before it expires.

    Aurora's B2C refresh_token only lives ~7 hours, and normal data polling
    never exercises it (the library keeps the session alive via its
    RefreshToken cookie instead) — so without this, the grant silently dies
    and the next core restart lands in a reauth flow. Called every update
    cycle; the caller persists api.token afterwards. Leaves the live
    session/access_token untouched. Returns True if the token was rolled.
    """
    new_token = oauth_refresh_token(api.token)
    if not new_token:
        return False

    api.token["id_token"] = new_token["id_token"]
    if new_token.get("refresh_token"):
        api.token["refresh_token"] = new_token["refresh_token"]
    if new_token.get("refresh_token_expires_in"):
        api.token["refresh_token_expires_in"] = new_token["refresh_token_expires_in"]
    _LOGGER.debug("Proactively rolled OAuth refresh_token")
    return True


def aurora_reinit(api: AuroraPlusApi, token: dict[str, Any]) -> bool:
    """Re-initialise an existing API object with a refreshed OAuth token.

    Returns True on success, False on transient failure; raises
    OAuthRefreshExpired when the stored grant is dead (reauth needed).
    """
    new_token = oauth_refresh_token(token)
    if not new_token:
        return False

    # Merge the new OAuth fields into the existing token, preserving any extra keys
    token.update({
        "id_token": new_token["id_token"],
        "refresh_token": new_token.get("refresh_token", token.get("refresh_token")),
    })
    # Clear the stale access_token so the library re-obtains it via id_token
    token.pop("access_token", None)
    token.pop("cookie_RefreshToken", None)

    try:
        new_api = AuroraPlusApi(token=token.copy())
        new_api.get_info()

        # Ensure the OAuth refresh_token is preserved in the api token dict
        # so it survives into the config entry and can be used on future refreshes
        new_refresh = token.get("refresh_token")
        if new_refresh:
            new_api.token["refresh_token"] = new_refresh

        # Copy the refreshed auth state back onto the existing api object —
        # but NOT serviceAgreementID/premiseAddress: get_info() keeps the
        # *last* Active premise, and accounts with two active service
        # agreements get a coin-flip result. Re-auth must never change which
        # agreement the entry is bound to.
        api.session = new_api.session
        api.token = new_api.token
        api.customerId = new_api.customerId
        api.Active = new_api.Active
        api.Error = new_api.Error
        _LOGGER.info("Successfully re-authenticated via OAuth refresh_token")
        return True
    except Exception as e:
        _LOGGER.warning(f"Re-init after OAuth refresh failed: {e}")
        return False


def _new_api(token: dict[str, Any]) -> AuroraPlusApi:
    # We need to copy the token, otherwise the AuroraPlusApi will use and update
    # the reference that it's been passed. If the reference comes from the
    # ConfigEntry, both will always hold the same value in memory. If they are
    # the same, HA will not persist the updated value.
    api = AuroraPlusApi(token=token.copy())

    # Preserve the OAuth refresh_token so it can be used for re-auth
    if "refresh_token" in token and "refresh_token" not in api.token:
        api.token["refresh_token"] = token["refresh_token"]

    # We need this data in AuroraPlusCoordinator.__init__so we have the
    # serviceAgreementID, preiseAddress, and tariffs over the previous
    # week however HomeAssistant is not happy if the calls are made there.

    api.get_info()
    api.getweek()

    return api


def _is_auth_error(e: Exception) -> bool:
    if isinstance(e, AuroraPlusAuthenticationError):
        return True
    return isinstance(e, HTTPError) and e.response.status_code in [401, 403]


def aurora_init(
    token: dict[str, Any] = {},
) -> AuroraPlusApi:
    _LOGGER.debug(f"aurora_init {token=}")
    try:
        return _new_api(token)
    except (AuroraPlusAuthenticationError, HTTPError) as e:
        if not _is_auth_error(e):
            raise

    # The stored id_token/access_token has expired and the library's
    # cookie-based refresh no longer works (Aurora killed that endpoint).
    # Fall back to the OAuth refresh_token before forcing a reauth flow —
    # after a restart the stored refresh_token is usually still valid.
    _LOGGER.info("Stored token rejected on init; trying OAuth refresh_token")
    try:
        new_token = oauth_refresh_token(token)
    except OAuthRefreshExpired as e:
        raise ConfigEntryAuthFailed("authentication failure on init") from e
    if not new_token:
        # Transient failure (Aurora down, network blip during boot) — retry
        # setup later rather than tearing the entry down into a reauth flow.
        raise ConfigEntryNotReady("Aurora token endpoint unreachable")

    merged = token.copy()
    merged["id_token"] = new_token["id_token"]
    merged["refresh_token"] = new_token.get(
        "refresh_token", merged.get("refresh_token")
    )
    # Clear the stale access_token so the library re-obtains it via id_token
    merged.pop("access_token", None)
    merged.pop("cookie_RefreshToken", None)

    try:
        api = _new_api(merged)
    except (AuroraPlusAuthenticationError, HTTPError) as e:
        if not _is_auth_error(e):
            raise
        raise ConfigEntryAuthFailed("authentication failure on init") from e

    _LOGGER.info("Successfully authenticated on init via OAuth refresh_token")
    return api
