"""Sensor platform for Envipco RVM.

This file intentionally keeps entity classes thin.
Complex API logic and derived calculations live in the coordinator,
so sensors stay predictable and easier to debug.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from .const import (
    BIN_COUNT_PREFIX,
    BIN_FULL_PREFIX,
    BIN_MATERIAL_PREFIX,
    CONF_MACHINES,
    DEFAULT_BIN_CAPACITY_BY_MATERIAL,
    DOMAIN,
    KEY_ACCEPTED_CANS,
    KEY_ACCEPTED_PET,
    MATERIAL_LABELS_NL,
    REJECT_KEYS,
    REJECT_LABELS_NL,
    STATUS_LAST_REPORT_FALLBACK_KEYS,
    STATUS_LAST_REPORT_PRIMARY_KEY,
    STATUS_STATE_KEY,
)
from .coordinator import EnvipcoCoordinator, normalize_material


@dataclass(slots=True)
class SensorMachineDef:
    id: str
    name: str


def parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=dt_util.UTC)
        return dt_util.as_utc(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        parsed = dt_util.parse_datetime(text)
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt_util.UTC)
        return dt_util.as_utc(parsed)
    return None


def format_local(dt_value: datetime | None) -> str | None:
    if dt_value is None:
        return None
    return dt_util.as_local(dt_value).strftime("%Y-%m-%d %H:%M:%S")


def get_last_report_raw(rvm: dict[str, Any]) -> Any:
    raw = rvm.get(STATUS_LAST_REPORT_PRIMARY_KEY)
    if raw is None:
        for key in STATUS_LAST_REPORT_FALLBACK_KEYS:
            raw = rvm.get(key)
            if raw is not None:
                break
    return raw


def material_label(material: str | None) -> str | None:
    if not material:
        return None
    return MATERIAL_LABELS_NL.get(material, material)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: EnvipcoCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    machines_cfg = entry.options.get(CONF_MACHINES, entry.data.get(CONF_MACHINES, [])) or []
    machines: list[SensorMachineDef] = []
    for item in machines_cfg:
        if not isinstance(item, dict):
            continue
        machine_id = str(item.get("id") or "").strip()
        if not machine_id:
            continue
        machines.append(SensorMachineDef(id=machine_id, name=str(item.get("name") or machine_id)))
    entities: list[SensorEntity] = []

    # Each RVM gets its own machine-oriented entity set.
    # The coordinator does the heavy lifting; the entities mostly expose values cleanly.
    for machine in machines:
        entities.extend([
            StatusSensor(coordinator, machine),
            LastReportSensor(coordinator, machine),
            LastReportTextSensor(coordinator, machine),
            LastSuccessfulUpdateSensor(coordinator, machine),
            ApiThrottleStatusSensor(coordinator, machine),
            ApiThrottleSecondsSensor(coordinator, machine),
            AcceptedTotalSensor(coordinator, machine),
            AcceptedCansSensor(coordinator, machine),
            AcceptedPetSensor(coordinator, machine),
            RejectTotalSensor(coordinator, machine),
            RejectRateSensor(coordinator, machine),
            RevenueTodaySensor(coordinator, machine),
            RevenueCanTodaySensor(coordinator, machine),
            RevenuePetTodaySensor(coordinator, machine),
            LocationInfoSensor(coordinator, machine),
        ])
        for reject_key in REJECT_KEYS:
            entities.append(RejectTypeSensor(coordinator, machine, reject_key))
        for bin_no in coordinator.active_bins(machine.id):
            entities.extend([
                BinCountSensor(coordinator, machine, bin_no),
                BinLimitSensor(coordinator, machine, bin_no),
                BinPercentageSensor(coordinator, machine, bin_no),
            ])

    async_add_entities(entities)


class BaseSensor(CoordinatorEntity[EnvipcoCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: EnvipcoCoordinator, machine: SensorMachineDef) -> None:
        super().__init__(coordinator)
        self.machine = machine

    @property
    def device_info(self):
        return self.coordinator.machine_device_info(self.machine.id)

    @property
    def suggested_object_id(self) -> str | None:
        unique_id = getattr(self, "_attr_unique_id", None)
        if unique_id:
            return slugify(str(unique_id), separator="_")
        return slugify(f"{self.machine.id}_{self.__class__.__name__.lower()}", separator="_")

    def _rvm(self) -> dict[str, Any]:
        return self.coordinator.rvm_data(self.machine.id)


class StatusSensor(BaseSensor):
    _attr_name = "Status"
    _attr_icon = "mdi:robot"

    def __init__(self, coordinator, machine):
        super().__init__(coordinator, machine)
        self._attr_unique_id = f"{machine.id}_status"

    @property
    def native_value(self):
        return self._rvm().get(STATUS_STATE_KEY)


class LastReportSensor(BaseSensor):
    _attr_name = "Laatste rapport"
    _attr_icon = "mdi:clock-outline"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, machine):
        super().__init__(coordinator, machine)
        self._attr_unique_id = f"{machine.id}_last_report"

    @property
    def native_value(self):
        return parse_timestamp(get_last_report_raw(self._rvm()))


class LastReportTextSensor(BaseSensor):
    _attr_name = "Laatste rapport tekst"
    _attr_icon = "mdi:calendar-clock"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, machine):
        super().__init__(coordinator, machine)
        self._attr_unique_id = f"{machine.id}_last_report_text"

    @property
    def native_value(self):
        return format_local(parse_timestamp(get_last_report_raw(self._rvm())))


class LastSuccessfulUpdateSensor(BaseSensor):
    _attr_name = "Laatste succesvolle update"
    _attr_icon = "mdi:update"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, machine):
        super().__init__(coordinator, machine)
        self._attr_unique_id = f"{machine.id}_last_successful_update"

    @property
    def native_value(self):
        return parse_timestamp(self.coordinator.last_successful_update)


class ApiThrottleStatusSensor(BaseSensor):
    _attr_name = "API throttling"
    _attr_icon = "mdi:speedometer-slow"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, machine):
        super().__init__(coordinator, machine)
        self._attr_unique_id = f"{machine.id}_api_throttle_status"

    @property
    def native_value(self):
        return self.coordinator.throttle_status_text

    @property
    def extra_state_attributes(self):
        return {
            "rvmstats_geremd": self.coordinator.stats_throttled,
            "rvmstats_resterend_seconden": self.coordinator.stats_throttle_remaining,
            "rejects_geremd": self.coordinator.rejects_throttled,
            "rejects_resterend_seconden": self.coordinator.rejects_throttle_remaining,
            "laatste_fout": self.coordinator.last_error,
        }


class ApiThrottleSecondsSensor(BaseSensor):
    _attr_name = "API throttle resterend"
    _attr_icon = "mdi:timer-sand"
    _attr_native_unit_of_measurement = "s"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, machine):
        super().__init__(coordinator, machine)
        self._attr_unique_id = f"{machine.id}_api_throttle_remaining"

    @property
    def native_value(self):
        return max(self.coordinator.stats_throttle_remaining, self.coordinator.rejects_throttle_remaining)


class AcceptedTotalSensor(BaseSensor):
    _attr_name = "Totaal ingenomen"
    _attr_icon = "mdi:counter"
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator, machine):
        super().__init__(coordinator, machine)
        self._attr_unique_id = f"{machine.id}_accepted_total"

    @property
    def native_value(self):
        return self.coordinator.machine_total_value(self.machine.id, "accepted_total")


class AcceptedCansSensor(BaseSensor):
    _attr_name = "Blik totaal"
    _attr_icon = "mdi:beer"
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator, machine):
        super().__init__(coordinator, machine)
        self._attr_unique_id = f"{machine.id}_accepted_cans"

    @property
    def native_value(self):
        return self.coordinator.machine_total_value(self.machine.id, KEY_ACCEPTED_CANS)


class AcceptedPetSensor(BaseSensor):
    _attr_name = "PET totaal"
    _attr_icon = "mdi:bottle-soda"
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator, machine):
        super().__init__(coordinator, machine)
        self._attr_unique_id = f"{machine.id}_accepted_pet"

    @property
    def native_value(self):
        return self.coordinator.machine_total_value(self.machine.id, KEY_ACCEPTED_PET)


class RejectTotalSensor(BaseSensor):
    _attr_name = "Afkeur totaal"
    _attr_icon = "mdi:close-circle-outline"
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator, machine):
        super().__init__(coordinator, machine)
        self._attr_unique_id = f"{machine.id}_reject_total"

    @property
    def native_value(self):
        return (self.coordinator.data.get("totals", {}) or {}).get(self.machine.id, {}).get("rejects_total", 0)


class RejectRateSensor(BaseSensor):
    _attr_name = "Afkeurpercentage"
    _attr_icon = "mdi:percent"
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, machine):
        super().__init__(coordinator, machine)
        self._attr_unique_id = f"{machine.id}_reject_rate"

    @property
    def native_value(self):
        return (self.coordinator.data.get("totals", {}) or {}).get(self.machine.id, {}).get("reject_rate", 0.0)


class RevenueTodaySensor(BaseSensor):
    _attr_name = "Opbrengst Totaal"
    _attr_icon = "mdi:currency-eur"
    _attr_native_unit_of_measurement = "EUR"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator, machine):
        super().__init__(coordinator, machine)
        self._attr_unique_id = f"{machine.id}_revenue_today"

    @property
    def native_value(self):
        rate_can, rate_pet = self.coordinator.machine_rates(self.machine.id)
        return round(
            (self.coordinator.machine_total_value(self.machine.id, KEY_ACCEPTED_CANS) * rate_can)
            + (self.coordinator.machine_total_value(self.machine.id, KEY_ACCEPTED_PET) * rate_pet),
            4,
        )


class RevenueCanTodaySensor(BaseSensor):
    _attr_name = "Opbrengst Blik"
    _attr_icon = "mdi:currency-eur"
    _attr_native_unit_of_measurement = "EUR"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator, machine):
        super().__init__(coordinator, machine)
        self._attr_unique_id = f"{machine.id}_revenue_can_today"

    @property
    def native_value(self):
        rate_can, _ = self.coordinator.machine_rates(self.machine.id)
        return round(self.coordinator.machine_total_value(self.machine.id, KEY_ACCEPTED_CANS) * rate_can, 4)


class RevenuePetTodaySensor(BaseSensor):
    _attr_name = "Opbrengst PET"
    _attr_icon = "mdi:currency-eur"
    _attr_native_unit_of_measurement = "EUR"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator, machine):
        super().__init__(coordinator, machine)
        self._attr_unique_id = f"{machine.id}_revenue_pet_today"

    @property
    def native_value(self):
        _, rate_pet = self.coordinator.machine_rates(self.machine.id)
        return round(self.coordinator.machine_total_value(self.machine.id, KEY_ACCEPTED_PET) * rate_pet, 4)


class LocationInfoSensor(BaseSensor):
    _attr_icon = "mdi:map-marker"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, machine):
        super().__init__(coordinator, machine)
        self._attr_unique_id = f"{machine.id}_location_info"
        self._attr_name = "Locatie"

    @property
    def native_value(self):
        address = self.coordinator.machine_address(self.machine.id) or ""
        postal = self.coordinator.machine_postal_code(self.machine.id) or ""
        city = self.coordinator.machine_city(self.machine.id) or ""
        city_line = " ".join(part for part in [postal, city] if part).strip()
        return ", ".join(part for part in [address, city_line] if part) or None

    @property
    def extra_state_attributes(self):
        return {
            "machine_naam": self.coordinator.machine_device_name(self.machine.id),
            "machine_id": self.machine.id,
            "machine_type": self.coordinator.machine_type(self.machine.id),
            "adres": self.coordinator.machine_address(self.machine.id),
            "postcode": self.coordinator.machine_postal_code(self.machine.id),
            "plaats": self.coordinator.machine_city(self.machine.id),
            "land": self.coordinator.machine_country(self.machine.id),
            "add_date": self.coordinator.machine_add_date(self.machine.id),
            "site_id": self.coordinator.machine_site_id(self.machine.id),
            "account_name": self.coordinator.machine_site_name(self.machine.id),
        }


class RejectTypeSensor(BaseSensor):
    _attr_icon = "mdi:alert-circle-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, machine, reject_key: str):
        super().__init__(coordinator, machine)
        self.reject_key = reject_key
        self._attr_unique_id = f"{machine.id}_reject_{reject_key}"
        self._attr_name = REJECT_LABELS_NL.get(reject_key, f"Reject {reject_key}")

    @property
    def native_value(self):
        return (self.coordinator.data.get("rejects", {}) or {}).get(self.machine.id, {}).get(self.reject_key, 0)


class BinBaseSensor(BaseSensor):
    def __init__(self, coordinator, machine, bin_no: int):
        super().__init__(coordinator, machine)
        self.bin_no = bin_no

    def _material(self) -> str | None:
        return normalize_material(self._rvm().get(f"{BIN_MATERIAL_PREFIX}{self.bin_no}"))

    def _material_label(self) -> str | None:
        return material_label(self._material())

    def _count(self) -> int | None:
        value = self._rvm().get(f"{BIN_COUNT_PREFIX}{self.bin_no}")
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    def _full(self):
        return self._rvm().get(f"{BIN_FULL_PREFIX}{self.bin_no}")


class BinCountSensor(BinBaseSensor):
    _attr_icon = "mdi:counter"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, machine, bin_no: int):
        super().__init__(coordinator, machine, bin_no)
        self._attr_unique_id = f"{machine.id}_bin_{bin_no}_count"
        material = self._material_label()
        self._attr_name = f"Bin {bin_no} aantal ({material})" if material else f"Bin {bin_no} aantal"

    @property
    def native_value(self):
        return self._count()

    @property
    def extra_state_attributes(self):
        return {
            "materiaal": self._material_label(),
            "bin_full": self._full(),
            "actieve_limiet": self.coordinator.current_bin_limit(self.machine.id, self.bin_no),
        }


class BinLimitSensor(BinBaseSensor):
    _attr_icon = "mdi:tune-vertical"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, machine, bin_no: int):
        super().__init__(coordinator, machine, bin_no)
        self._attr_unique_id = f"{machine.id}_bin_{bin_no}_active_limit"
        self._attr_name = f"Bin {bin_no} actieve limiet"

    @property
    def native_value(self):
        return self.coordinator.current_bin_limit(self.machine.id, self.bin_no)

    @property
    def extra_state_attributes(self):
        return {
            "materiaal": self._material_label(),
            "api_limiet": self.coordinator.safe_int(self._rvm().get(f"BinInfoLimitBin{self.bin_no}")) or None,
            "ingestelde_limiet": self.coordinator.configured_bin_limit(self.machine.id, self.bin_no),
        }


class BinPercentageSensor(BinBaseSensor):
    _attr_icon = "mdi:percent"
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, machine, bin_no: int):
        super().__init__(coordinator, machine, bin_no)
        self._attr_unique_id = f"{machine.id}_bin_{bin_no}_percentage"
        self._attr_name = f"Bin {bin_no} vulling"

    @property
    def native_value(self):
        count = self._count()
        limit_value = self.coordinator.current_bin_limit(self.machine.id, self.bin_no)
        if count is None or not limit_value:
            return None
        return round(min(100.0, max(0.0, (count / limit_value) * 100)), 1)

    @property
    def extra_state_attributes(self):
        material = self._material()
        return {
            "materiaal": self._material_label(),
            "aantal": self._count(),
            "actieve_limiet": self.coordinator.current_bin_limit(self.machine.id, self.bin_no),
            "fallback_materiaal_limiet": DEFAULT_BIN_CAPACITY_BY_MATERIAL.get(material) if material else None,
        }
