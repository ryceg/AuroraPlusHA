"""The auroraplus sensor integration."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryNotReady,
    PlatformNotReady,
)


from .api import aurora_init
from .const import CONF_SERVICE_AGREEMENT_ID, CONF_TOKEN
from .coordinator import AuroraPlusCoordinator
from .services import async_register_services

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up entry."""
    token = entry.data.get(CONF_TOKEN)

    try:
        api = await hass.async_add_executor_job(aurora_init, token)
    except OSError as err:
        raise PlatformNotReady("Connection to Aurora+ failed") from err

    # get_info() picks the *last* Active premise, which is a coin flip on
    # accounts with two active service agreements — pin to the agreement this
    # entry was set up with so sensors and statistics stay bound to it.
    stored_said = entry.data.get(CONF_SERVICE_AGREEMENT_ID)
    if stored_said and api.serviceAgreementID != stored_said:
        _LOGGER.warning(
            f"get_info picked service agreement {api.serviceAgreementID}; "
            f"pinning to this entry's stored agreement {stored_said}"
        )
        api.serviceAgreementID = stored_said

    entry.runtime_data = AuroraPlusCoordinator(hass, entry, api)

    # If init had to roll the OAuth token, persist it NOW: the old grant is
    # already consumed, and the coordinator's own persist is gated on the
    # entry being LOADED — a crash before then would strand a dead token on
    # disk and force a reauth.
    if api.token != entry.data.get(CONF_TOKEN):
        hass.config_entries.async_update_entry(
            entry,
            data={
                CONF_SERVICE_AGREEMENT_ID: api.serviceAgreementID,
                CONF_TOKEN: api.token.copy(),
            },
        )

    if not (
        hasattr(entry.runtime_data, "week")
        and entry.runtime_data.week.get("TariffTypes")
    ):
        raise ConfigEntryNotReady("No tariffs in returned data, yet")

    await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])

    async_register_services(hass)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    await AuroraPlusCoordinator.update_config_entry_token(hass, entry)
    return True
