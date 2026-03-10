# /config/custom_components/envipco_rvm/number.py

"""Number platform for Envipco RVM."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import CONF_MACHINES, DOMAIN, MATERIAL_LABELS_NL
from .coordinator import EnvipcoCoordinator


@dataclass(slots=True)
class NumberMachineDef:
    """Configured machine definition."""
    id: str
    name: str


def material_label(material: str | None) -> str | None:
    """Return Dutch material label."""
    if not material:
        return None
    return MATERIAL_LABELS_NL.get(material, material)


def bin_label(material: str | None, bin_no: int) -> str:
    """Return friendly bin label."""
    label = material_label(material)
    if label:
        return label
    return f"Bin {bin_no}"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Envipco RVM number entities."""
    coordinator: EnvipcoCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    machines_cfg = entry.options.get(CONF_MACHINES, entry.data.get(CONF_MACHINES, [])) or []
    machines: list[NumberMachineDef] = []

    for item in machines_cfg:
        if not isinstance(item, dict):
            continue

        machine_id = str(item.get("id") or "").strip()
        if not machine_id:
            continue

        machines.append(
            NumberMachineDef(
                id=machine_id,
                name=str(item.get("name") or machine_id),
            )
        )

    entities: list[NumberEntity] = []

    for machine in machines:
        for bin_no in coordinator.active_bins(machine.id):
            entities.append(BinLimitNumber(coordinator, entry, machine, bin_no))

    async_add_entities(entities)


class BaseNumber(CoordinatorEntity[EnvipcoCoordinator], NumberEntity):
    """Base number entity."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        coordinator: EnvipcoCoordinator,
        entry: ConfigEntry,
        machine: NumberMachineDef,
    ) -> None:
        """Init base number."""
        super().__init__(coordinator)
        self.entry = entry
        self.machine = machine

    @property
    def device_info(self):
        """Attach entity to the machine device."""
        return self.coordinator.machine_device_info(self.machine.id)

    @property
    def suggested_object_id(self) -> str | None:
        """Stable suggested object id."""
        unique_id = getattr(self, "_attr_unique_id", None)
        if unique_id:
            return slugify(str(unique_id), separator="_")
        return slugify(f"{self.machine.id}_{self.__class__.__name__.lower()}", separator="_")

    def _machine_options(self) -> dict[str, Any]:
        """Return current options dict."""
        return dict(self.entry.options)

    def _configured_limits(self) -> dict[str, Any]:
        """Return all configured machine bin limits."""
        options = self._machine_options()
        return dict(options.get("machine_bin_limits", self.entry.data.get("machine_bin_limits", {})) or {})

    def _machine_limit_map(self) -> dict[str, Any]:
        """Return bin-limit map for current machine."""
        return dict(self._configured_limits().get(self.machine.id, {}) or {})


class BinLimitNumber(BaseNumber):
    """Editable bin capacity/limit for one active bin."""

    _attr_icon = "mdi:tune-vertical"
    _attr_native_min_value = 0
    _attr_native_max_value = 500000
    _attr_native_step = 1

    def __init__(
        self,
        coordinator: EnvipcoCoordinator,
        entry: ConfigEntry,
        machine: NumberMachineDef,
        bin_no: int,
    ) -> None:
        """Init bin limit entity."""
        super().__init__(coordinator, entry, machine)
        self.bin_no = bin_no
        self._attr_unique_id = f"{machine.id}_bin_{bin_no}_limit_number"

    def _material(self) -> str | None:
        """Return normalized material."""
        return self.coordinator.bin_material(self.machine.id, self.bin_no)

    def _label(self) -> str:
        """Return friendly entity label."""
        return bin_label(self._material(), self.bin_no)

    @property
    def name(self) -> str:
        """Return entity name."""
        return f"{self._label()} limiet"

    @property
    def native_value(self) -> float:
        """Return current active limit."""
        value = self.coordinator.current_bin_limit(self.machine.id, self.bin_no)
        if value is None:
            return 0.0
        return float(value)

    @property
    def extra_state_attributes(self):
        """Extra attributes."""
        return {
            "bin_nummer": self.bin_no,
            "materiaal": material_label(self._material()),
            "api_limiet": self.coordinator.safe_int(
                self.coordinator.rvm_data(self.machine.id).get(f"BinInfoLimitBin{self.bin_no}")
            ),
            "ingestelde_limiet": self.coordinator.configured_bin_limit(self.machine.id, self.bin_no),
            "api_vulling_percentage": self.coordinator.bin_full_percent(self.machine.id, self.bin_no),
            "api_aantal": self.coordinator.bin_count(self.machine.id, self.bin_no),
        }

    async def async_set_native_value(self, value: float) -> None:
        """Persist new bin limit to config entry options."""
        new_value = int(round(value))

        options = dict(self.entry.options)
        all_limits = dict(
            options.get(
                "machine_bin_limits",
                self.entry.data.get("machine_bin_limits", {}),
            )
            or {}
        )

        machine_limits = dict(all_limits.get(self.machine.id, {}) or {})
        machine_limits[str(self.bin_no)] = new_value
        all_limits[self.machine.id] = machine_limits

        options["machine_bin_limits"] = all_limits

        self.hass.config_entries.async_update_entry(self.entry, options=options)

        updated_entry = self.hass.config_entries.async_get_entry(self.entry.entry_id)
        if updated_entry is not None:
            self.entry = updated_entry

        await self.coordinator.async_request_refresh()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from coordinator."""
        self.async_write_ha_state()
