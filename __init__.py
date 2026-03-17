"""Home Assistant entry setup for Envipco RVM."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
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

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "client": client,
        "coordinator": coordinator,
        "loaded_platforms": [],
        "suppress_reload_once": False,
    }

    try:
        await coordinator.async_config_entry_first_refresh()
        await coordinator.async_refresh_machine_meta_once(force=True)

        device_registry = async_get_device_registry(hass)
        for machine in coordinator.machines():
            device_registry.async_get_or_create(
                config_entry_id=entry.entry_id,
                **coordinator.machine_device_info(machine.id),
            )

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        hass.data[DOMAIN][entry.entry_id]["loaded_platforms"] = list(PLATFORMS)

        await _async_apply_registry_naming(hass, entry, coordinator)

        entry.async_on_unload(entry.add_update_listener(async_reload_entry))
        return True

    except Exception:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        if not hass.data.get(DOMAIN):
            hass.data.pop(DOMAIN, None)
        raise


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    domain_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    loaded_platforms = domain_data.get("loaded_platforms", [])

    if loaded_platforms:
        unload_ok = await hass.config_entries.async_unload_platforms(
            entry, loaded_platforms
        )
    else:
        unload_ok = True

    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        if not hass.data.get(DOMAIN):
            hass.data.pop(DOMAIN, None)

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    domain_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)

    if domain_data and domain_data.get("suppress_reload_once"):
        domain_data["suppress_reload_once"] = False
        coordinator = domain_data.get("coordinator")
        if coordinator is not None:
            coordinator.entry = entry
            coordinator.update_interval = timedelta(
                seconds=entry.options.get(
                    CONF_RVMSTATS_INTERVAL,
                    entry.data.get(
                        CONF_RVMSTATS_INTERVAL,
                        DEFAULT_RVMSTATS_INTERVAL,
                    ),
                )
            )
            await coordinator.async_request_refresh()
        return

    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
