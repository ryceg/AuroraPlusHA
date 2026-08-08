"""Services for the Aurora+ integration."""

import asyncio
import datetime
import logging

import voluptuous as vol

from homeassistant.components import persistent_notification
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SERVICE_BACKFILL = "backfill"

SERVICE_BACKFILL_SCHEMA = vol.Schema(
    {
        vol.Required("start_date"): cv.date,
        vol.Optional("end_date"): cv.date,
    }
)

# Aurora's API holds roughly 3 years of daily history; give up a little past
# that rather than hammering the API with requests that can never succeed.
MAX_BACKFILL_DAYS = 1200

# Consecutive per-day fetch failures before aborting the whole run, so a died
# session doesn't turn into hundreds of doomed requests.
MAX_CONSECUTIVE_FAILURES = 5


def async_register_services(hass: HomeAssistant) -> None:
    """Register integration services (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_BACKFILL):
        return

    async def _handle_backfill(call: ServiceCall) -> None:
        start_date = call.data["start_date"]
        end_date = call.data.get("end_date") or (
            dt_util.now().date() - datetime.timedelta(days=1)
        )
        if start_date > end_date:
            raise ServiceValidationError(
                f"start_date {start_date} is after end_date {end_date}"
            )
        if (dt_util.now().date() - start_date).days > MAX_BACKFILL_DAYS:
            raise ServiceValidationError(
                f"start_date {start_date} is more than {MAX_BACKFILL_DAYS} days "
                "ago; Aurora+ does not hold that much history"
            )

        # The fetch can take minutes for multi-year ranges: run it in the
        # background and report completion via a persistent notification.
        hass.async_create_background_task(
            _backfill(hass, start_date, end_date),
            name=f"{DOMAIN} backfill {start_date}..{end_date}",
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_BACKFILL,
        _handle_backfill,
        schema=SERVICE_BACKFILL_SCHEMA,
    )


async def _backfill(
    hass: HomeAssistant,
    start_date: datetime.date,
    end_date: datetime.date,
) -> None:
    summary_lines = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state != ConfigEntryState.LOADED:
            continue
        coordinator = entry.runtime_data
        try:
            line = await _backfill_coordinator(
                hass, coordinator, start_date, end_date
            )
        except Exception:
            _LOGGER.exception(
                f"backfill failed for {coordinator.service_agreement_id}"
            )
            line = (
                f"{coordinator.service_agreement_id}: FAILED, see log "
                "for details"
            )
        summary_lines.append(line)

    persistent_notification.async_create(
        hass,
        f"Backfill of {start_date} to {end_date} finished:\n\n"
        + "\n".join(f"- {line}" for line in summary_lines),
        title="Aurora+ backfill",
    )


async def _backfill_coordinator(
    hass: HomeAssistant,
    coordinator,
    start_date: datetime.date,
    end_date: datetime.date,
) -> str:
    """Fetch all days in the range and rebuild statistics for one account."""
    api = coordinator._api
    today = dt_util.now().date()

    # getday(-N) returns the day N days before today; -1 is yesterday.
    first_index = (start_date - today).days
    last_index = min((end_date - today).days, -1)

    records: dict[str, dict] = {}
    days_with_data = 0
    consecutive_failures = 0
    for index in range(first_index, last_index + 1):
        try:
            day = await hass.async_add_executor_job(_fetch_day, api, index)
            consecutive_failures = 0
        except Exception as e:
            consecutive_failures += 1
            _LOGGER.warning(f"backfill: getday({index}) failed: {e}")
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                raise
            continue

        if day.get("NoDataFlag"):
            continue
        days_with_data += 1
        for record in day.get("MeteredUsageRecords") or []:
            if record and record.get("StartTime"):
                # The daily cost record shares its midnight StartTime with the
                # first hourly energy record, so the key must also encode which
                # fields the record carries or one clobbers the other.
                key = (
                    record["StartTime"],
                    bool(record.get("KilowattHourUsage")),
                    bool(record.get("DollarValueUsage")),
                )
                records[key] = record

        if days_with_data % 50 == 0:
            _LOGGER.info(
                f"backfill: fetched {days_with_data} days with data "
                f"(up to index {index})"
            )
        # Be polite to Aurora's API: this can be over a thousand requests.
        await asyncio.sleep(0.1)

    sorted_records = [records[k] for k in sorted(records)]
    _LOGGER.info(
        f"backfill: {days_with_data} days with data, "
        f"{len(sorted_records)} usage records; importing statistics"
    )

    imported = 0
    for sensor in getattr(coordinator, "historical_sensors", []):
        imported += await _import_sensor_statistics(hass, sensor, sorted_records)

    line = (
        f"{coordinator.service_agreement_id}: {days_with_data} days, "
        f"{imported} statistics rows imported"
    )
    _LOGGER.info(f"backfill: done: {line}")
    return line


def _fetch_day(api, index: int) -> dict:
    api.getday(index)
    return dict(api.day)


async def _import_sensor_statistics(
    hass: HomeAssistant, sensor, records: list[dict]
) -> int:
    """Import a full range of statistics for one historical sensor.

    The ha-historical-sensor library refuses to write anything older than the
    newest existing statistics row (its cumulative sum only ever extends
    forward), so backfilling has to recompute sums itself: anchor on the last
    row *before* the range, accumulate through every fetched state, and
    overwrite the whole tail. Rows from the range's end to "now" are always
    part of the fetch, so the rewritten series stays consistent with future
    library-driven appends.
    """
    hist_states = sensor.historical_states_from_records(records)
    if not hist_states:
        return 0
    hist_states.sort(key=lambda hs: hs.timestamp)

    metadata = sensor.get_statistic_metadata()
    anchor = await get_instance(hass).async_add_executor_job(
        _last_statistic_before,
        hass,
        metadata["statistic_id"],
        hist_states[0].timestamp,
    )

    statistics_data = await sensor.async_calculate_statistic_data(
        hist_states, latest=anchor
    )
    async_add_external_statistics(hass, metadata, statistics_data)
    _LOGGER.info(
        f"backfill: {metadata['statistic_id']}: "
        f"imported {len(statistics_data)} rows "
        f"(anchor sum: {anchor['sum'] if anchor else 0})"
    )
    return len(statistics_data)


def _last_statistic_before(hass: HomeAssistant, statistic_id: str, before_ts: float):
    """Newest statistics row strictly before the given timestamp, if any.

    Runs in the recorder executor.
    """
    rows = statistics_during_period(
        hass,
        datetime.datetime(2000, 1, 1, tzinfo=datetime.UTC),
        datetime.datetime.fromtimestamp(before_ts, datetime.UTC),
        {statistic_id},
        "hour",
        None,
        {"sum"},
    ).get(statistic_id)
    return rows[-1] if rows else None
