"""Home Assistant entry setup for Envipco RVM.

This module wires together the API client, coordinator and platforms.
It also keeps bin entities in sync with the active bins reported by the API.

Behaviour:
- active bins are present
- inactive bins are removed from the entity registry
- when the active bin layout changes, the integration reloads once
  so entities are added/removed cleanly
"""

from __future__ import annotations

from datetime import timedelta
import re

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import async_get as async_get_device_registry
from homeassistant.util import slugify

from .const import (
    CONF_PASSWORD,
    CONF_RVMSTATS_INTERVAL,
    CONF_USERNAME,
    DEFAULT_RVMSTATS_INTERVAL,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import EnvipcoCoordinator

_BIN_UNIQUE_ID_RE = re.compile(r"^(?P<machine>.+?)_bin_(?P<bin_no>\d+)_.+$")


def _current_active_bins_map(coordinator: EnvipcoCoordinator) -> dict[str, tuple[int, ...]]:
    result: dict[str, tuple[int, ...]] = {}
    for machine in coordinator.machines():
        result[machine.id] = tuple(sorted(coordinator.active_bins(machine.id)))
    return result


async def _async_apply_registry_naming(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: EnvipcoCoordinator,
) -> None:
    entity_registry = er.async_get(hass)
    for entity_id, entity_entry in list(entity_registry.entities.items()):
        if entity_entry.config_entry_id != entry.entry_id:
            continue

        unique_id = (entity_entry.unique_id or "").strip()
        if not unique_id:
            continue

        desired_entity_id = f"{entity_entry.domain}.{slugify(unique_id, separator='_')}"
        if (
            entity_entry.entity_id != desired_entity_id
            and desired_entity_id not in entity_registry.entities
        ):
            try:
                entity_registry.async_update_entity(
                    entity_entry.entity_id,
                    new_entity_id=desired_entity_id,
                )
            except Exception:
                pass

    device_registry = dr.async_get(hass)
    for device in list(device_registry.devices.values()):
        if entry.entry_id not in device.config_entries:
            continue

        rvm_id = None
        for domain, identifier in device.identifiers:
            if domain == DOMAIN:
                rvm_id = str(identifier)
                break

        if not rvm_id:
            continue

        desired_name = coordinator.machine_device_name(rvm_id)
        if desired_name and device.name != desired_name and device.name_by_user is None:
            try:
                device_registry.async_update_device(device.id, name_by_user=desired_name)
            except Exception:
                pass


async def _async_remove_inactive_bin_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: EnvipcoCoordinator,
) -> bool:
    """Remove stale bin entities that do not belong to active bins anymore.

    Returns True when registry changed.
    """
    entity_registry = er.async_get(hass)
    active_map = _current_active_bins_map(coordinator)
    changed = False

    for entity_entry in list(entity_registry.entities.values()):
        if entity_entry.config_entry_id != entry.entry_id:
            continue

        unique_id = (entity_entry.unique_id or "").strip()
        match = _BIN_UNIQUE_ID_RE.match(unique_id)
        if not match:
            continue

        machine_id = match.group("machine")
        bin_no = int(match.group("bin_no"))
        is_active = bin_no in active_map.get(machine_id, ())

        if is_active:
            continue

        try:
            entity_registry.async_remove(entity_entry.entity_id)
            changed = True
        except Exception:
            pass

    return changed


async def _async_process_coordinator_update(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    domain_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not domain_data:
        return

    coordinator: EnvipcoCoordinator | None = domain_data.get("coordinator")
    if coordinator is None:
        return

    previous_map = domain_data.get("active_bins_map", {})
    current_map = _current_active_bins_map(coordinator)

    if current_map == previous_map:
        return

    domain_data["active_bins_map"] = current_map

    if domain_data.get("reload_scheduled"):
        return

    domain_data["reload_scheduled"] = True
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    from .api import EnvipcoRvmApiClient

    session = async_get_clientsession(hass)
    client = EnvipcoRvmApiClient(
        session=session,
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
    )

    coordinator = EnvipcoCoordinator(
        hass=hass,
        client=client,
        entry=entry,
        update_interval=timedelta(
            seconds=entry.options.get(
                CONF_RVMSTATS_INTERVAL,
                entry.data.get(CONF_RVMSTATS_INTERVAL, DEFAULT_RVMSTATS_INTERVAL),
            )
        ),
    )

    await coordinator.async_config_entry_first_refresh()
    await coordinator.async_refresh_machine_meta_once(force=True)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "client": client,
        "coordinator": coordinator,
        "suppress_reload_once": False,
        "reload_scheduled": False,
        "active_bins_map": _current_active_bins_map(coordinator),
    }

    device_registry = async_get_device_registry(hass)
    for machine in coordinator.machines():
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            **coordinator.machine_device_info(machine.id),
        )

    # Remove stale bin entities before platform setup.
    await _async_remove_inactive_bin_entities(hass, entry, coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Run again after setup to remove anything that was left from older versions.
    await _async_remove_inactive_bin_entities(hass, entry, coordinator)
    await _async_apply_registry_naming(hass, entry, coordinator)

    @callback
    def _handle_coordinator_update() -> None:
        hass.async_create_task(_async_process_coordinator_update(hass, entry))

    entry.async_on_unload(coordinator.async_add_listener(_handle_coordinator_update))
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        if not hass.data.get(DOMAIN):
            hass.data.pop(DOMAIN, None)
    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    domain_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)

    if domain_data:
        domain_data["reload_scheduled"] = False

    if domain_data and domain_data.get("suppress_reload_once"):
        domain_data["suppress_reload_once"] = False
        coordinator = domain_data.get("coordinator")
        if coordinator is not None:
            coordinator.entry = entry
            coordinator.update_interval = timedelta(
                seconds=entry.options.get(
                    CONF_RVMSTATS_INTERVAL,
                    entry.data.get(CONF_RVMSTATS_INTERVAL, DEFAULT_RVMSTATS_INTERVAL),
                )
            )
            coordinator.refresh_local_options_from_entry()
            await coordinator.async_request_refresh()
            await _async_remove_inactive_bin_entities(hass, entry, coordinator)
        return

    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
