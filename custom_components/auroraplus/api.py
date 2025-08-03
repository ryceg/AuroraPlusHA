import logging

import auroraplus
from requests.exceptions import HTTPError

from homeassistant.const import (
    CONF_ACCESS_TOKEN,
)
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.util import Throttle

from .const import (
    CONF_ROUNDING,
    CONF_SERVICE_AGREEMENT_ID,
    DEFAULT_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

def aurora_init(access_token: str):
    try:
        session = auroraplus.api(None, access_token)
        session.getmonth()
    except HTTPError as e: 
        status_code = e.response.status_code
        if status_code in [401, 403]:
            raise ConfigEntryAuthFailed(e) from e
        raise e
    return session


async def aurora_oauth_authorize(hass, username: str, password: str):
    """Start OAuth authorization flow and return auth URL."""
    try:
        def _oauth_authorize():
            session = auroraplus.api(username, password)
            auth_url = session.oauth_authorize()
            return auth_url, session
        
        auth_url, session = await hass.async_add_executor_job(_oauth_authorize)
        return auth_url, session
    except Exception as e:
        _LOGGER.error(f"OAuth authorization failed: {e}")
        raise ConfigEntryAuthFailed(f"OAuth authorization failed: {e}") from e


async def aurora_oauth_complete(hass, session, redirect_url: str):
    """Complete OAuth flow with redirect URL and return token info."""
    try:
        def _oauth_complete():
            token_info = session.oauth_redirect(redirect_url)
            return token_info
        
        token_info = await hass.async_add_executor_job(_oauth_complete, session, redirect_url)
        return token_info
    except Exception as e:
        _LOGGER.error(f"OAuth completion failed: {e}")
        raise ConfigEntryAuthFailed(f"OAuth completion failed: {e}") from e


async def aurora_validate_oauth_token(hass, access_token: str):
    """Validate an OAuth token by testing API access."""
    try:
        session = await hass.async_add_executor_job(aurora_init, access_token)
        return session
    except ConfigEntryAuthFailed:
        raise
    except Exception as e:
        _LOGGER.error(f"Token validation failed: {e}")
        raise ConfigEntryAuthFailed(f"Token validation failed: {e}") from e

class AuroraApi():
    """Asynchronously-updating wrapper for the Aurora API. """
    _hass = None
    _session = None

    _instances = {}

    def __init__(self, hass, session):
        self._hass = hass
        self._session = session
        self.service_agreement_id = session.serviceAgreementID
        self.service_address = session.month['ServiceAgreements'][session.serviceAgreementID]['PremiseName']
        self.__class__._instances[self.service_agreement_id] = self
        _LOGGER.debug(f'AuroraApi ready with {self._session}')

    @Throttle(min_time=DEFAULT_SCAN_INTERVAL)  # XXX: should be configurable
    async def async_update(self):
        await self._hass.async_add_executor_job(self._api_update)

    def _api_update(self):
        try:
            self._session.gettoken()
            self._session.getcurrent()
            for i in range(-1, - 10, - 1):
                self._session.getday(i)
                if not self._session.day['NoDataFlag']:
                    self._session.getsummary(i)
                    break
                _LOGGER.debug(f'No data at index {i}')
            _LOGGER.info('Successfully obtained data from '
                         + self._session.day['StartDate'])
        except HTTPError as e:
            status_code = e.response.status_code
            if status_code in [401, 403]:
                raise ConfigEntryAuthFailed(e) from e
            raise e
        except Exception as e:
            _LOGGER.warn(f'Error updating data: {e}')

    @classmethod
    async def update_listener(cls, hass, config_entry):
        """
        XXX: find the api object for the entitie, and update its session token
        """
        service_agreement_id = config_entry.data.get(CONF_SERVICE_AGREEMENT_ID)
        access_token = config_entry.data.get(CONF_ACCESS_TOKEN)
        session = await hass.async_add_executor_job(
            aurora_init,
            access_token
        )
        api = cls._instances[service_agreement_id].update_session(session)

    def update_session(self, session):
        self._session = session

    def __getattr__(self, attr):
        """Forward any attribute access to the session, or handle error """
        if attr == '_throttle':
            raise AttributeError()
        _LOGGER.debug(f'Accessing data for {attr}')
        try:
            data = getattr(self._session, attr)
        except AttributeError as err:
            _LOGGER.debug(
                f'Data for {attr} not yet available'
            )
            return {}  # empty with a get
        _LOGGER.debug(f'... returning {data}')
        return data


