from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import EnvipcoApiError, EnvipcoRvmApiClient
from .const import (
    ACCEPT_FIELDS_PREFIX,
    BIN_COUNT_PREFIX,
    BIN_FULL_PREFIX,
    BIN_LIMIT_PREFIX,
    BIN_MATERIAL_PREFIX,
    CONF_MACHINE_BIN_LIMITS,
    CONF_MACHINE_META,
    CONF_MACHINE_RATES,
    CONF_MACHINES,
    DEFAULT_BIN_CAPACITY_BY_MATERIAL,
    DEFAULT_RATE_CAN,
    DEFAULT_RATE_PET,
    KEY_ACCEPTED_CANS,
    KEY_ACCEPTED_GLASS,
    KEY_ACCEPTED_PET,
    MATERIAL_MAP,
    NAME,
    REJECT_KEYS,
)


@dataclass(slots=True)
class MachineDef:
    id: str
    name: str


def normalize_material(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip().upper()
    if not text:
        return None
    return MATERIAL_MAP.get(text, text)


class EnvipcoCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(self, hass: HomeAssistant, client: EnvipcoRvmApiClient, entry: ConfigEntry, update_interval) -> None:
        super().__init__(hass, logger=__import__("logging").getLogger(__name__), name=NAME, update_interval=update_interval)
        self.client = client
        self.entry = entry
        self._machine_meta_cache: dict[str, dict[str, Any]] = dict(
            entry.options.get(CONF_MACHINE_META, entry.data.get(CONF_MACHINE_META, {})) or {}
        )
        self.data = {"stats": {}, "rejects": {}, "accepted": {}, "totals": {}, "machine_meta": self._machine_meta_cache}

    def machines(self) -> list[MachineDef]:
        raw = self.entry.options.get(CONF_MACHINES, self.entry.data.get(CONF_MACHINES, [])) or []
        machines: list[MachineDef] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            machine_id = str(item.get("id") or "").strip()
            if not machine_id:
                continue
            machines.append(MachineDef(id=machine_id, name=str(item.get("name") or machine_id)))
        return machines

    def rvm_ids(self) -> list[str]:
        return [machine.id for machine in self.machines()]

    def machine_rates(self, rvm_id: str) -> tuple[float, float]:
        rates = self.entry.options.get(CONF_MACHINE_RATES, {}) or {}
        item = rates.get(rvm_id, {}) or {}
        return float(item.get("can", DEFAULT_RATE_CAN)), float(item.get("pet", DEFAULT_RATE_PET))

    def machine_bin_limits(self, rvm_id: str) -> dict[str, int]:
        all_limits = self.entry.options.get(CONF_MACHINE_BIN_LIMITS, {}) or {}
        machine_limits = all_limits.get(rvm_id, {}) or {}
        cleaned: dict[str, int] = {}
        for key, value in machine_limits.items():
            try:
                cleaned[str(key)] = int(float(value))
            except (TypeError, ValueError):
                continue
        return cleaned

    def configured_bin_limit(self, rvm_id: str, bin_no: int) -> int | None:
        return self.machine_bin_limits(rvm_id).get(str(bin_no))

    @staticmethod
    def safe_int(value: Any) -> int:
        try:
            return int(float(str(value).strip()))
        except Exception:
            return 0

    @staticmethod
    def _row_get_case_insensitive(row: dict[str, Any], *keys: str) -> Any:
        if not isinstance(row, dict):
            return None
        lowered = {str(k).strip().lower(): v for k, v in row.items()}
        for key in keys:
            value = lowered.get(str(key).strip().lower())
            if value not in (None, ""):
                return value
        return None

    def rvm_data(self, rvm_id: str) -> dict[str, Any]:
        data = self.data or {}
        return (data.get("stats", {}) or {}).get(rvm_id, {}) or {}

    def machine_meta(self, rvm_id: str) -> dict[str, Any]:
        return self._machine_meta_cache.get(rvm_id, {}) or {}

    def machine_type(self, rvm_id: str) -> str:
        meta = self.machine_meta(rvm_id)
        value = str(meta.get("machine_type") or "").strip()
        if value:
            return value
        rvm = self.rvm_data(rvm_id)
        for key in ("RvmType", "RVMType", "MachineType", "Type", "Model"):
            value = str(rvm.get(key) or "").strip()
            if value:
                return value.title()

        material_1 = normalize_material(rvm.get(f"{BIN_MATERIAL_PREFIX}1"))
        material_2 = normalize_material(rvm.get(f"{BIN_MATERIAL_PREFIX}2"))
        material_3 = normalize_material(rvm.get(f"{BIN_MATERIAL_PREFIX}3"))
        material_4 = normalize_material(rvm.get(f"{BIN_MATERIAL_PREFIX}4"))
        active = set(self.active_bins(rvm_id))

        if material_1 == "CAN" and material_2 == "PET" and 3 not in active and 4 not in active:
            return "Quantum"
        if material_1 == "CAN" and material_3 == "PET" and material_4 == "CAN":
            return "Optima"
        return "RVM"

    def machine_device_name(self, rvm_id: str) -> str:
        return f"{rvm_id}-{self.machine_type(rvm_id)}"

    def machine_address(self, rvm_id: str) -> str | None:
        meta = self.machine_meta(rvm_id)
        return str(meta.get("address") or self.rvm_data(rvm_id).get("SiteInfoAddress") or "").strip() or None

    def machine_postal_code(self, rvm_id: str) -> str | None:
        meta = self.machine_meta(rvm_id)
        return str(meta.get("postal_code") or self.rvm_data(rvm_id).get("SiteInfoPostalCode") or "").strip() or None

    def machine_city(self, rvm_id: str) -> str | None:
        meta = self.machine_meta(rvm_id)
        return str(meta.get("city") or self.rvm_data(rvm_id).get("SiteInfoCity") or "").strip() or None

    def machine_country(self, rvm_id: str) -> str | None:
        meta = self.machine_meta(rvm_id)
        return str(meta.get("country") or self.rvm_data(rvm_id).get("SiteInfoCountry") or "").strip() or None

    def machine_add_date(self, rvm_id: str) -> str | None:
        meta = self.machine_meta(rvm_id)
        return str(meta.get("add_date") or "").strip() or None

    def machine_site_id(self, rvm_id: str) -> str | None:
        meta = self.machine_meta(rvm_id)
        value = str(meta.get("site_id") or self.rvm_data(rvm_id).get("SiteId") or self.rvm_data(rvm_id).get("SiteInfoSiteId") or "").strip()
        return value or None

    def machine_site_name(self, rvm_id: str) -> str | None:
        meta = self.machine_meta(rvm_id)
        return str(meta.get("account_name") or "").strip() or None

    def machine_device_info(self, rvm_id: str) -> dict[str, Any]:
        rvm = self.rvm_data(rvm_id)
        info: dict[str, Any] = {
            "identifiers": {("envipco_rvm", rvm_id)},
            "name": self.machine_device_name(rvm_id),
            "manufacturer": "Envipco",
            "model": self.machine_type(rvm_id),
            "serial_number": rvm_id,
            "sw_version": str(rvm.get("VersionREL") or "").strip() or None,
            "hw_version": str(rvm.get("VersionMCX") or "").strip() or None,
        }
        city = self.machine_city(rvm_id)
        if city:
            info["suggested_area"] = city
        return info

    def active_bins(self, rvm_id: str) -> list[int]:
        rvm = self.rvm_data(rvm_id)
        active: list[int] = []
        for bin_no in range(1, 13):
            material = normalize_material(rvm.get(f"{BIN_MATERIAL_PREFIX}{bin_no}"))
            count = rvm.get(f"{BIN_COUNT_PREFIX}{bin_no}")
            full = rvm.get(f"{BIN_FULL_PREFIX}{bin_no}")
            if material:
                active.append(bin_no)
                continue
            if count not in (None, "", 0, "0"):
                active.append(bin_no)
                continue
            if full in (True, "true", "True", 1, "1"):
                active.append(bin_no)
        return active

    def current_bin_limit(self, rvm_id: str, bin_no: int) -> int | None:
        configured = self.configured_bin_limit(rvm_id, bin_no)
        if configured is not None:
            return configured
        api_value = self.safe_int(self.rvm_data(rvm_id).get(f"{BIN_LIMIT_PREFIX}{bin_no}"))
        if api_value > 0:
            return api_value
        material = normalize_material(self.rvm_data(rvm_id).get(f"{BIN_MATERIAL_PREFIX}{bin_no}"))
        if material:
            return DEFAULT_BIN_CAPACITY_BY_MATERIAL.get(material)
        return None

    def bin_material(self, rvm_id: str, bin_no: int) -> str | None:
        return normalize_material(self.rvm_data(rvm_id).get(f"{BIN_MATERIAL_PREFIX}{bin_no}"))

    def machine_total_value(self, rvm_id: str, key: str) -> int:
        data = self.data or {}
        totals = (data.get("totals", {}) or {}).get(rvm_id, {}) or {}
        return self.safe_int(totals.get(key))

    async def _async_update_machine_meta(self, stats: dict[str, Any]) -> None:
        site_ids: set[str] = set()
        for rvm_id in self.rvm_ids():
            if self.machine_meta(rvm_id).get("machine_type") and self.machine_meta(rvm_id).get("add_date"):
                continue
            rvm = stats.get(rvm_id, {}) or {}
            site_id = str(rvm.get("SiteId") or rvm.get("SiteInfoSiteId") or "").strip()
            if site_id:
                site_ids.add(site_id)

        if not site_ids:
            return

        new_meta = dict(self._machine_meta_cache)
        changed = False
        for site_id in sorted(site_ids):
            try:
                site_data = await self.client.site_data(site_id)
            except Exception:
                continue

            site_common = {
                "site_id": str(site_data.get("siteId") or site_id).strip() or site_id,
                "account_name": str(site_data.get("accountName") or "").strip() or None,
                "address": str(site_data.get("address") or "").strip() or None,
                "postal_code": str(site_data.get("postalCode") or "").strip() or None,
                "city": str(site_data.get("city") or "").strip() or None,
                "country": str(site_data.get("country") or "").strip() or None,
            }

            for machine in site_data.get("currentRVMs", []) or []:
                if not isinstance(machine, dict):
                    continue
                serial = str(machine.get("machineSerialNumber") or "").strip()
                if not serial:
                    continue
                remove_date = str(machine.get("removeDate") or "").strip()
                if remove_date:
                    continue
                existing = dict(new_meta.get(serial, {}) or {})
                merged = {
                    **existing,
                    **{k: v for k, v in site_common.items() if v},
                    "machine_type": str(machine.get("machineType") or existing.get("machine_type") or "").strip() or existing.get("machine_type"),
                    "add_date": str(machine.get("addDate") or existing.get("add_date") or "").strip() or existing.get("add_date"),
                }
                if merged != existing:
                    new_meta[serial] = merged
                    changed = True

        if changed:
            self._machine_meta_cache = new_meta
            options = dict(self.entry.options)
            options[CONF_MACHINE_META] = new_meta
            self.hass.config_entries.async_update_entry(self.entry, options=options)

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            stats = await self.client.rvm_stats(self.rvm_ids(), date.today())
            await self._async_update_machine_meta(stats)
            rejects_rows = await self.client.rejects(self.rvm_ids(), date.today(), date.today(), include_acceptance=True)
        except EnvipcoApiError as err:
            raise UpdateFailed(str(err)) from err
        except Exception as err:
            raise UpdateFailed(f"Unexpected error: {err}") from err

        rejects_by_machine: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        accepted_by_machine: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

        for row in rejects_rows:
            machine_id = str(
                self._row_get_case_insensitive(
                    row,
                    "MachineSerialNumber",
                    "machineSerialNumber",
                    "Machine",
                    "machine",
                    "RVM",
                    "rvm",
                    "MachineId",
                    "machineId",
                )
                or ""
            ).strip()
            if not machine_id:
                continue

            for key in REJECT_KEYS:
                rejects_by_machine[machine_id][key] += self.safe_int(self._row_get_case_insensitive(row, key))

            for key, value in row.items():
                key_text = str(key or "").strip()
                if not key_text.lower().startswith(ACCEPT_FIELDS_PREFIX.lower()):
                    continue
                lowered = key_text.lower()
                if "can" in lowered or "alu" in lowered or "steel" in lowered:
                    accepted_by_machine[machine_id][KEY_ACCEPTED_CANS] += self.safe_int(value)
                elif "pet" in lowered:
                    accepted_by_machine[machine_id][KEY_ACCEPTED_PET] += self.safe_int(value)
                elif "glass" in lowered or "gls" in lowered:
                    accepted_by_machine[machine_id][KEY_ACCEPTED_GLASS] += self.safe_int(value)

        totals: dict[str, dict[str, Any]] = {}
        for rvm_id in self.rvm_ids():
            rvm_stats = stats.get(rvm_id, {}) or {}

            accepted_cans = self.safe_int(rvm_stats.get(KEY_ACCEPTED_CANS))
            accepted_pet = self.safe_int(rvm_stats.get(KEY_ACCEPTED_PET))
            accepted_glass = self.safe_int(rvm_stats.get(KEY_ACCEPTED_GLASS))
            accepted_total = accepted_cans + accepted_pet + accepted_glass
            rejects_total = sum(rejects_by_machine[rvm_id].get(key, 0) for key in REJECT_KEYS)
            denominator = accepted_total + rejects_total
            reject_rate = round((rejects_total / denominator) * 100, 1) if denominator else 0.0
            rate_can, rate_pet = self.machine_rates(rvm_id)
            totals[rvm_id] = {
                "accepted_cans": accepted_cans,
                "accepted_pet": accepted_pet,
                "accepted_glass": accepted_glass,
                "accepted_total": accepted_total,
                "rejects_total": rejects_total,
                "reject_rate": reject_rate,
                "revenue_can_today": round(accepted_cans * rate_can, 4),
                "revenue_pet_today": round(accepted_pet * rate_pet, 4),
                "revenue_today": round((accepted_cans * rate_can) + (accepted_pet * rate_pet), 4),
            }

        return {
            "stats": stats,
            "rejects": {k: dict(v) for k, v in rejects_by_machine.items()},
            "accepted": {k: dict(v) for k, v in accepted_by_machine.items()},
            "totals": totals,
            "machine_meta": self._machine_meta_cache,
        }
