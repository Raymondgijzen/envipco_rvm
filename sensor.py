from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EnvipcoCoordinator


BIN_MAX = 10


def _clean_material(value: Any) -> str | None:
    """Maak materiaalwaarde schoon."""
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    lowered = text.lower()
    if lowered in {"unknown", "none", "null", "-", "n/a"}:
        return None

    return text


def _get_bin_material(machine_data: dict[str, Any], bin_number: int) -> str | None:
    """Zoek materiaal voor een bin via meerdere mogelijke key-namen."""
    possible_keys = [
        f"BinInfoMaterialBin{bin_number}",
        f"BinInfoMatriaalBin{bin_number}",   # voor het geval de bron of eerdere code typo bevat
        f"BinMaterial{bin_number}",
        f"MaterialBin{bin_number}",
    ]

    for key in possible_keys:
        value = _clean_material(machine_data.get(key))
        if value:
            return value

    return None


def _get_bin_limit(machine_data: dict[str, Any], bin_number: int) -> int | None:
    """Lees bin limiet veilig uit."""
    possible_keys = [
        f"BinInfoLimitBin{bin_number}",
        f"BinLimit{bin_number}",
    ]

    for key in possible_keys:
        value = machine_data.get(key)
        if value is None:
            continue

        try:
            limit = int(float(value))
            if limit > 0:
                return limit
        except (TypeError, ValueError):
            continue

    return None


def _get_bin_count(machine_data: dict[str, Any], bin_number: int) -> int:
    """Lees bin count veilig uit."""
    possible_keys = [
        f"Bin{bin_number}Count",
        f"BinCount{bin_number}",
    ]

    for key in possible_keys:
        value = machine_data.get(key)
        if value is None:
            continue

        try:
            return int(float(value))
        except (TypeError, ValueError):
            continue

    return 0


def _material_to_name(material: str | None, fallback_bin_number: int) -> str:
    """Zet materiaal om naar nette naam."""
    if not material:
        return f"Bin {fallback_bin_number}"

    normalized = material.strip().lower()

    mapping = {
        "pet": "PET",
        "can": "Blik",
        "blik": "Blik",
        "plastic": "Plastic",
        "glas": "Glas",
    }

    return mapping.get(normalized, material.strip())


def _is_active_bin(machine_data: dict[str, Any], bin_number: int) -> tuple[bool, str | None, int | None]:
    """
    Bepaal of bin actief is.

    Regels:
    - Als materiaal aanwezig is -> actief
    - Anders als limiet > 0 -> actief
    - Anders niet actief
    """
    material = _get_bin_material(machine_data, bin_number)
    limit = _get_bin_limit(machine_data, bin_number)

    if material:
        return True, material, limit

    if limit is not None and limit > 0:
        return True, None, limit

    return False, None, None


@dataclass
class BinDefinition:
    """Beschrijving van een actieve bin."""
    bin_number: int
    material: str | None
    limit: int | None


class EnvipcoBaseBinSensor(CoordinatorEntity[EnvipcoCoordinator], SensorEntity):
    """Basis bin sensor."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EnvipcoCoordinator,
        entry_id: str,
        machine_serial: str,
        machine_name: str,
        bin_definition: BinDefinition,
    ) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._machine_serial = machine_serial
        self._machine_name = machine_name
        self._bin_definition = bin_definition

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._machine_serial)},
            "name": self._machine_name,
            "manufacturer": "Envipco",
            "model": "RVM",
        }

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    def _machine_data(self) -> dict[str, Any]:
        return self.coordinator.get_machine_data(self._machine_serial) or {}

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "machine_serial": self._machine_serial,
            "machine_name": self._machine_name,
            "bin_number": self._bin_definition.bin_number,
            "materiaal": self._bin_definition.material,
            "limiet": self._bin_definition.limit,
        }


class EnvipcoBinCountSensor(EnvipcoBaseBinSensor):
    """Sensor voor aantal in bin."""

    _attr_icon = "mdi:counter"

    def __init__(
        self,
        coordinator: EnvipcoCoordinator,
        entry_id: str,
        machine_serial: str,
        machine_name: str,
        bin_definition: BinDefinition,
    ) -> None:
        super().__init__(coordinator, entry_id, machine_serial, machine_name, bin_definition)

        material_name = _material_to_name(bin_definition.material, bin_definition.bin_number)

        self._attr_unique_id = (
            f"{entry_id}_{machine_serial}_bin_{bin_definition.bin_number}_count"
        )
        self._attr_name = f"{material_name} aantal"

    @property
    def native_value(self) -> int:
        machine_data = self._machine_data()
        return _get_bin_count(machine_data, self._bin_definition.bin_number)

    @property
    def state_class(self) -> str:
        return SensorStateClass.MEASUREMENT


class EnvipcoBinFillSensor(EnvipcoBaseBinSensor):
    """Sensor voor bin vulling."""

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_icon = "mdi:percent"

    def __init__(
        self,
        coordinator: EnvipcoCoordinator,
        entry_id: str,
        machine_serial: str,
        machine_name: str,
        bin_definition: BinDefinition,
    ) -> None:
        super().__init__(coordinator, entry_id, machine_serial, machine_name, bin_definition)

        material_name = _material_to_name(bin_definition.material, bin_definition.bin_number)

        self._attr_unique_id = (
            f"{entry_id}_{machine_serial}_bin_{bin_definition.bin_number}_fill"
        )
        self._attr_name = f"{material_name} vulling"

    @property
    def native_value(self) -> float | None:
        machine_data = self._machine_data()
        count = _get_bin_count(machine_data, self._bin_definition.bin_number)
        limit = _get_bin_limit(machine_data, self._bin_definition.bin_number)

        if limit is None or limit <= 0:
            return None

        return round((count / limit) * 100, 1)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Zet sensoren op voor een config entry."""
    coordinator: EnvipcoCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = []

    for machine in coordinator.get_all_machines():
        machine_serial = machine.get("MachineSerialNumber")
        machine_name = machine.get("MachineName") or machine_serial or "Envipco machine"

        if not machine_serial:
            continue

        for bin_number in range(1, BIN_MAX + 1):
            active, material, limit = _is_active_bin(machine, bin_number)

            if not active:
                continue

            bin_definition = BinDefinition(
                bin_number=bin_number,
                material=material,
                limit=limit,
            )

            entities.append(
                EnvipcoBinCountSensor(
                    coordinator=coordinator,
                    entry_id=entry.entry_id,
                    machine_serial=machine_serial,
                    machine_name=machine_name,
                    bin_definition=bin_definition,
                )
            )

            entities.append(
                EnvipcoBinFillSensor(
                    coordinator=coordinator,
                    entry_id=entry.entry_id,
                    machine_serial=machine_serial,
                    machine_name=machine_name,
                    bin_definition=bin_definition,
                )
            )

    async_add_entities(entities)
