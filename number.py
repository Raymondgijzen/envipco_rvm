from __future__ import annotations

import re
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import (
    CONF_MACHINE_BIN_LIMITS,
    CONF_MACHINES,
    DEFAULT_BIN_CAPACITY_BY_MATERIAL,
    DOMAIN,
)
from .coordinator import EnvipcoCoordinator


def _machine_name_map(entry: ConfigEntry) -> dict[str, str]:
    machines_cfg = entry.options.get(CONF_MACHINES, entry.data.get(CONF_MACHINES, [])) or []
    result: dict[str, str] = {}

    for item in machines_cfg:
        if not isinstance(item, dict):
            continue

        machine_id = str(item.get("id") or "").strip()
        if not machine_id:
            continue

        result[machine_id] = str(item.get("name") or machine_id)

    return result


def _number_unique_id(machine_id: str, bin_no: int) -> str:
    return f"{machine_id}_bin_{bin_no}_limit"


def _extract_bin_number(text: str) -> int | None:
    match = re.search(r"_bin_(\d+)_", text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


async def _async_remove_inactive_bin_limit_numbers(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: EnvipcoCoordinator,
    machine_ids: list[str],
) -> None:
    entity_registry = er.async_get(hass)

    active_by_machine: dict[str, set[int]] = {}
    for machine_id in machine_ids:
        active_by_machine[machine_id] = set(coordinator.active_bins(machine_id))

    for entity_entry in list(entity_registry.entities.values()):
        if entity_entry.config_entry_id != entry.entry_id:
            continue

        unique_id = (entity_entry.unique_id or "").strip()
        entity_id = (entity_entry.entity_id or "").strip()

        target_text = unique_id or entity_id
        if "_bin_" not in target_text:
            continue

        machine_id = None
        for current_machine_id in machine_ids:
            if target_text.startswith(f"{current_machine_id}_bin_"):
                machine_id = current_machine_id
                break

        if machine_id is None:
            continue

        bin_no = _extract_bin_number(target_text)
        if bin_no is None:
            continue

        if bin_no in active_by_machine.get(machine_id, set()):
            continue

        entity_registry.async_remove(entity_entry.entity_id)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: EnvipcoCoordinator = data["coordinator"]

    machine_name_map = _machine_name_map(entry)
    machine_ids = list(machine_name_map.keys())

    await _async_remove_inactive_bin_limit_numbers(hass, entry, coordinator, machine_ids)

    entities: list[NumberEntity] = []

    for machine_id in machine_ids:
        for bin_no in coordinator.active_bins(machine_id):
            entities.append(
                BinLimitNumber(
                    hass=hass,
                    entry=entry,
                    coordinator=coordinator,
                    machine_id=machine_id,
                    machine_name=machine_name_map.get(machine_id, machine_id),
                    bin_no=bin_no,
                )
            )

    async_add_entities(entities)


class BinLimitNumber(CoordinatorEntity[EnvipcoCoordinator], NumberEntity):
    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_max_value = 10000
    _attr_native_step = 1
    _attr_icon = "mdi:trash-can-outline"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: EnvipcoCoordinator,
        machine_id: str,
        machine_name: str,
        bin_no: int,
    ) -> None:
        super().__init__(coordinator)
        self.hass = hass
        self.entry = entry
        self.machine_id = machine_id
        self.machine_name = machine_name
        self.bin_no = bin_no

        self._attr_unique_id = _number_unique_id(machine_id, bin_no)
        self._attr_name = f"Bin {bin_no} limiet"

        material = coordinator.bin_material(machine_id, bin_no)
        fallback = DEFAULT_BIN_CAPACITY_BY_MATERIAL.get(material) if material else None
        api_limit = coordinator.current_bin_limit(machine_id, bin_no)

        suggestions: list[int] = []
        if fallback:
            suggestions.append(int(fallback))
        if api_limit:
            try:
                suggestions.append(int(api_limit))
            except (TypeError, ValueError):
                pass

        if suggestions:
            highest = max(suggestions)
            self._attr_native_max_value = max(10000, highest * 2)

    @property
    def device_info(self):
        return self.coordinator.machine_device_info(self.machine_id)

    @property
    def available(self) -> bool:
        return self.bin_no in self.coordinator.active_bins(self.machine_id)

    @property
    def suggested_object_id(self) -> str | None:
        return slugify(str(self._attr_unique_id), separator="_")

    @property
    def native_value(self) -> float:
        value = self.coordinator.configured_bin_limit(self.machine_id, self.bin_no)
        if value is not None:
            return float(value)

        current = self.coordinator.current_bin_limit(self.machine_id, self.bin_no)
        if current is not None:
            return float(current)

        return 0.0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        material = self.coordinator.bin_material(self.machine_id, self.bin_no)
        fallback = DEFAULT_BIN_CAPACITY_BY_MATERIAL.get(material) if material else None
        api_limit = self.coordinator.safe_int(
            self.coordinator.rvm_data(self.machine_id).get(f"BinInfoLimitBin{self.bin_no}")
        ) or None

        return {
            "machine_id": self.machine_id,
            "machine_name": self.machine_name,
            "bin_nummer": self.bin_no,
            "materiaal": material,
            "api_limiet": api_limit,
            "fallback_limiet": fallback,
            "actieve_limiet": self.coordinator.current_bin_limit(self.machine_id, self.bin_no),
        }

    async def async_set_native_value(self, value: float) -> None:
        new_value = int(round(value))

        all_limits = dict(self.entry.options.get(CONF_MACHINE_BIN_LIMITS, {}) or {})
        machine_limits = dict(all_limits.get(self.machine_id, {}) or {})
        machine_limits[str(self.bin_no)] = new_value
        all_limits[self.machine_id] = machine_limits

        new_options = dict(self.entry.options)
        new_options[CONF_MACHINE_BIN_LIMITS] = all_limits

        domain_data = self.hass.data[DOMAIN][self.entry.entry_id]
        domain_data["suppress_reload_once"] = True

        self.hass.config_entries.async_update_entry(self.entry, options=new_options)

        updated_entry = self.hass.config_entries.async_get_entry(self.entry.entry_id)
        if updated_entry is not None:
            self.entry = updated_entry
            self.coordinator.entry = updated_entry

        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()
