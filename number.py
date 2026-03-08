"""Number entities for writable machine configuration.

These entities change Home Assistant stored options only.
The coordinator then refreshes and recalculates the live derived values.
"""

from __future__ import annotations

from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import CONF_MACHINE_BIN_LIMITS, CONF_MACHINE_RATES, CONF_MACHINES, DOMAIN
from .coordinator import EnvipcoCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: EnvipcoCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    machines_cfg = entry.options.get(CONF_MACHINES, entry.data.get(CONF_MACHINES, [])) or []
    entities: list[NumberEntity] = []
    # Number entities are only used for local configuration values.
    # Nothing is written back to the Envipco portal.
    for machine in machines_cfg:
        machine_id = machine.get("id")
        if not machine_id:
            continue
        entities.append(CanRateConfigNumber(coordinator, entry, machine_id))
        entities.append(PetRateConfigNumber(coordinator, entry, machine_id))
        for bin_no in coordinator.active_bins(machine_id):
            entities.append(BinLimitConfigNumber(coordinator, entry, machine_id, bin_no))
    async_add_entities(entities)


class BaseConfigNumber(CoordinatorEntity[EnvipcoCoordinator], NumberEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = "box"

    def __init__(self, coordinator: EnvipcoCoordinator, entry: ConfigEntry, machine_id: str) -> None:
        super().__init__(coordinator)
        self.entry = entry
        self.machine_id = machine_id

    @property
    def device_info(self):
        return self.coordinator.machine_device_info(self.machine_id)

    @property
    def suggested_object_id(self) -> str | None:
        unique_id = getattr(self, "_attr_unique_id", None)
        if unique_id:
            return slugify(str(unique_id), separator="_")
        return slugify(f"{self.machine_id}_{self.__class__.__name__.lower()}", separator="_")


class BinLimitConfigNumber(BaseConfigNumber):
    _attr_icon = "mdi:tune-vertical"
    _attr_native_min_value = 0
    _attr_native_max_value = 5000
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "st"

    def __init__(self, coordinator: EnvipcoCoordinator, entry: ConfigEntry, machine_id: str, bin_no: int) -> None:
        super().__init__(coordinator, entry, machine_id)
        self.bin_no = bin_no
        self._attr_unique_id = f"{machine_id}_bin_{bin_no}_config_limit"
        self._attr_name = f"Bin {bin_no} limiet"

    @property
    def native_value(self):
        return float(self.coordinator.current_bin_limit(self.machine_id, self.bin_no) or 0)

    async def async_set_native_value(self, value: float) -> None:
        options = dict(self.entry.options)
        all_limits = dict(options.get(CONF_MACHINE_BIN_LIMITS, self.entry.data.get(CONF_MACHINE_BIN_LIMITS, {})) or {})
        machine_limits = dict(all_limits.get(self.machine_id, {}) or {})
        machine_limits[str(self.bin_no)] = int(round(value))
        all_limits[self.machine_id] = machine_limits
        options[CONF_MACHINE_BIN_LIMITS] = all_limits
        domain_data = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id)
        if domain_data is not None:
            domain_data["suppress_reload_once"] = True
        self.hass.config_entries.async_update_entry(self.entry, options=options)
        self.entry = self.hass.config_entries.async_get_entry(self.entry.entry_id) or self.entry
        self.coordinator.entry = self.entry
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()


class MachineRateConfigNumber(BaseConfigNumber):
    _attr_icon = "mdi:currency-eur"
    _attr_native_min_value = 0
    _attr_native_max_value = 5
    _attr_native_step = 0.0001
    _attr_native_unit_of_measurement = "EUR"

    def __init__(self, coordinator: EnvipcoCoordinator, entry: ConfigEntry, machine_id: str, rate_key: str, label: str) -> None:
        super().__init__(coordinator, entry, machine_id)
        self.rate_key = rate_key
        self._attr_unique_id = f"{machine_id}_{rate_key}_rate_config"
        self._attr_name = label

    @property
    def native_value(self):
        rate_can, rate_pet = self.coordinator.machine_rates(self.machine_id)
        value = rate_can if self.rate_key == "can" else rate_pet
        return float(value)

    async def async_set_native_value(self, value: float) -> None:
        options = dict(self.entry.options)
        all_rates = dict(options.get(CONF_MACHINE_RATES, self.entry.data.get(CONF_MACHINE_RATES, {})) or {})
        machine_rates = dict(all_rates.get(self.machine_id, {}) or {})
        machine_rates[self.rate_key] = round(float(value), 4)
        all_rates[self.machine_id] = machine_rates
        options[CONF_MACHINE_RATES] = all_rates
        domain_data = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id)
        if domain_data is not None:
            domain_data["suppress_reload_once"] = True
        self.hass.config_entries.async_update_entry(self.entry, options=options)
        self.entry = self.hass.config_entries.async_get_entry(self.entry.entry_id) or self.entry
        self.coordinator.entry = self.entry
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()


class CanRateConfigNumber(MachineRateConfigNumber):
    def __init__(self, coordinator: EnvipcoCoordinator, entry: ConfigEntry, machine_id: str) -> None:
        super().__init__(coordinator, entry, machine_id, "can", "Blik tarief")


class PetRateConfigNumber(MachineRateConfigNumber):
    def __init__(self, coordinator: EnvipcoCoordinator, entry: ConfigEntry, machine_id: str) -> None:
        super().__init__(coordinator, entry, machine_id, "pet", "PET tarief")
