"""Home Assistant entry setup for Envipco RVM.

This version force-removes inactive bin entities from the entity registry.
That avoids old bin sensors lingering after the active bin layout changed.
"""

from __future__ import annotations

from datetime import timedelta

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


BIN_ENTITY_SUFFIXES = (
    "count",
    "active_limit",
    "percentage",
    "config_limit",
)


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

        identifiers = set(device.identifiers or set())
        if ("envipco_rvm", f"{entry.entry_id}_platform") in identifiers:
            desired_name = "Envipco Platform"
            if device.name != desired_name and device.name_by_user is None:
                try:
                    device_registry.async_update_device(device.id, name_by_user=desired_name)
                except Exception:
                    pass
            continue

        rvm_id = None
        for domain, identifier in device.identifiers:
            if domain == DOMAIN:
                ident = str(identifier)
                if ident == f"{entry.entry_id}_platform":
                    continue
                rvm_id = ident
                break

        if not rvm_id:
            continue

        desired_name = coordinator.machine_device_name(rvm_id)
        if desired_name and device.name != desired_name and device.name_by_user is None:
            try:
                device_registry.async_update_device(device.id, name_by_user=desired_name)
            except Exception:
                pass


async def _async_force_remove_inactive_bin_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: EnvipcoCoordinator,
) -> bool:
    """Force remove all inactive bin entities by exact unique_id/entity_id patterns."""
    entity_registry = er.async_get(hass)
    active_map = _current_active_bins_map(coordinator)
    changed = False

    active_unique_ids = set()
    for machine_id, active_bins in active_map.items():
        for bin_no in active_bins:
            for suffix in BIN_ENTITY_SUFFIXES:
                active_unique_ids.add(f"{machine_id}_bin_{bin_no}_{suffix}")

    machine_ids = [machine.id for machine in coordinator.machines()]

    for machine_id in machine_ids:
        for bin_no in range(1, 13):
            for suffix in BIN_ENTITY_SUFFIXES:
                unique_id = f"{machine_id}_bin_{bin_no}_{suffix}"
                if unique_id in active_unique_ids:
                    continue

                entity_id = entity_registry.async_get_entity_id("sensor", DOMAIN, unique_id)
                if entity_id is None:
                    entity_id = entity_registry.async_get_entity_id("number", DOMAIN, unique_id)

                if entity_id is not None:
                    try:
                        entity_registry.async_remove(entity_id)
                        changed = True
                    except Exception:
                        pass

    for entity_entry in list(entity_registry.entities.values()):
        if entity_entry.config_entry_id != entry.entry_id:
            continue

        unique_id = (entity_entry.unique_id or "").strip()
        if "_bin_" not in unique_id:
            continue
        if unique_id in active_unique_ids:
            continue
        if not any(unique_id.endswith(f"_{suffix}") for suffix in BIN_ENTITY_SUFFIXES):
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
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        **coordinator.integration_device_info(),
    )

    for machine in coordinator.machines():
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            **coordinator.machine_device_info(machine.id),
        )

    await _async_force_remove_inactive_bin_entities(hass, entry, coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    await _async_force_remove_inactive_bin_entities(hass, entry, coordinator)
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
            await _async_force_remove_inactive_bin_entities(hass, entry, coordinator)
        return

    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
