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

    def machines(self) -> list[MachineDef]:
        raw = self.entry.options.get(CONF_MACHINES, self.entry.data.get(CONF_MACHINES, [])) or []
        return [MachineDef(id=item["id"], name=item.get("name") or item["id"]) for item in raw if item.get("id")]

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

    def rvm_data(self, rvm_id: str) -> dict[str, Any]:
        return (self.data.get("stats", {}) or {}).get(rvm_id, {}) or {}

    def machine_device_name(self, rvm_id: str) -> str:
        machine = next((m for m in self.machines() if m.id == rvm_id), None)
        rvm = self.rvm_data(rvm_id)
        configured = (machine.name if machine else rvm_id).strip()
        if configured and configured != rvm_id:
            return configured
        account = str(rvm.get("SiteInfoAccount") or "").strip()
        if account:
            return account
        location = str(rvm.get("SiteInfoLocationID") or "").strip()
        if location:
            return location
        return rvm_id

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

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            stats = await self.client.rvm_stats(self.rvm_ids(), date.today())
            rejects_rows = await self.client.rejects(self.rvm_ids(), date.today(), date.today(), include_acceptance=True)
        except EnvipcoApiError as err:
            raise UpdateFailed(str(err)) from err
        except Exception as err:
            raise UpdateFailed(f"Unexpected error: {err}") from err

        rejects_by_machine: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        accepted_by_machine: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

        for row in rejects_rows:
            machine_id = str(row.get("MachineSerialNumber") or row.get("Machine") or row.get("RVM") or row.get("MachineId") or "").strip()
            if not machine_id:
                continue

            for key in REJECT_KEYS:
                rejects_by_machine[machine_id][key] += self.safe_int(row.get(key))

            for key, value in row.items():
                if not key.startswith(ACCEPT_FIELDS_PREFIX):
                    continue
                lowered = key.lower()
                if "can" in lowered or "alu" in lowered:
                    accepted_by_machine[machine_id][KEY_ACCEPTED_CANS] += self.safe_int(value)
                elif "pet" in lowered:
                    accepted_by_machine[machine_id][KEY_ACCEPTED_PET] += self.safe_int(value)
                elif "glass" in lowered or "gls" in lowered:
                    accepted_by_machine[machine_id][KEY_ACCEPTED_GLASS] += self.safe_int(value)

        totals: dict[str, dict[str, Any]] = {}
        for rvm_id in self.rvm_ids():
            accepted_cans = accepted_by_machine[rvm_id].get(KEY_ACCEPTED_CANS, 0)
            accepted_pet = accepted_by_machine[rvm_id].get(KEY_ACCEPTED_PET, 0)
            accepted_glass = accepted_by_machine[rvm_id].get(KEY_ACCEPTED_GLASS, 0)
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
        }
