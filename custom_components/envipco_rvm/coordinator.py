from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import EnvipcoApiError, EnvipcoRvmApiClient, EnvipcoThrottleError
from .const import (
    ACCEPT_FIELDS_PREFIX,
    BIN_LIMIT_PREFIX,
    BIN_MATERIAL_PREFIX,
    CONF_MACHINE_BIN_LIMITS,
    CONF_MACHINE_META,
    CONF_MACHINE_RATES,
    CONF_MACHINES,
    CONF_REJECTS_INTERVAL,
    DEFAULT_BIN_CAPACITY_BY_MATERIAL,
    DEFAULT_RATE_CAN,
    DEFAULT_RATE_PET,
    DEFAULT_REJECTS_INTERVAL,
    KEY_ACCEPTED_CANS,
    KEY_ACCEPTED_GLASS,
    KEY_ACCEPTED_PET,
    MATERIAL_MAP,
    NAME,
    REJECT_KEYS,
)

_LOGGER = logging.getLogger(__name__)

INACTIVE_MATERIAL_VALUES = {
    "",
    "UNKNOWN",
    "UNBEKEND",
    "NONE",
    "NULL",
    "N/A",
    "NA",
    "-",
    "--",
    "EMPTY",
    "NOT_USED",
    "NOTUSED",
    "UNUSED",
    "INACTIVE",
    "0",
}


@dataclass(slots=True)
class MachineDef:
    id: str
    name: str


def normalize_material(raw: Any) -> str | None:
    """Normalize material.

    Unused bins often come back as 'Unknown'.
    Those must count as inactive.
    """
    if raw is None:
        return None

    text = str(raw).strip().upper()
    if not text:
        return None

    if text in INACTIVE_MATERIAL_VALUES:
        return None

    mapped = MATERIAL_MAP.get(text, text)
    mapped_text = str(mapped).strip().upper()

    if not mapped_text or mapped_text in INACTIVE_MATERIAL_VALUES:
        return None

    return mapped_text


class EnvipcoCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Central place for all API polling and derived calculations."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: EnvipcoRvmApiClient,
        entry: ConfigEntry,
        update_interval,
    ) -> None:
        super().__init__(hass, logger=_LOGGER, name=NAME, update_interval=update_interval)
        self.client = client
        self.entry = entry
        self._machine_meta_cache: dict[str, dict[str, Any]] = dict(
            entry.options.get(CONF_MACHINE_META, entry.data.get(CONF_MACHINE_META, {})) or {}
        )
        self._live_machine_rates: dict[str, dict[str, float]] = {}
        self._live_machine_bin_limits: dict[str, dict[str, int]] = {}
        self._local_revision = 0
        self.refresh_local_options_from_entry()

        self._last_rejects_fetch: datetime | None = None
        self._rejects_cache: list[dict[str, str]] = []
        self._stats_throttle_until: datetime | None = None
        self._rejects_throttle_until: datetime | None = None
        self._last_successful_update: datetime | None = None
        self._last_error: str | None = None

        # Platform-wide diagnostic timestamps
        self._last_platform_contact: datetime | None = None
        self._last_stats_fetch: datetime | None = None
        self._last_rejects_successful_fetch: datetime | None = None

        self.data = {
            "stats": {},
            "rejects": {},
            "accepted": {},
            "totals": {},
            "machine_meta": self._machine_meta_cache,
            "local_revision": self._local_revision,
        }

    @property
    def last_platform_contact(self) -> datetime | None:
        return self._last_platform_contact

    @property
    def last_stats_fetch(self) -> datetime | None:
        return self._last_stats_fetch

    @property
    def last_rejects_successful_fetch(self) -> datetime | None:
        return self._last_rejects_successful_fetch

    @property
    def last_successful_update(self) -> datetime | None:
        return self._last_successful_update

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def stats_throttle_remaining(self) -> int:
        """Seconds remaining for rvmStats throttling."""
        if not self._stats_throttle_until:
            return 0
        remaining = int((self._stats_throttle_until - datetime.utcnow()).total_seconds())
        return max(0, remaining)

    @property
    def rejects_throttle_remaining(self) -> int:
        """Seconds remaining for rejects throttling."""
        if not self._rejects_throttle_until:
            return 0
        remaining = int((self._rejects_throttle_until - datetime.utcnow()).total_seconds())
        return max(0, remaining)

    @property
    def stats_throttled(self) -> bool:
        return self.stats_throttle_remaining > 0

    @property
    def rejects_throttled(self) -> bool:
        return self.rejects_throttle_remaining > 0

    @property
    def throttle_status_text(self) -> str:
        stats = self.stats_throttle_remaining
        rejects = self.rejects_throttle_remaining

        if stats > 0 and rejects > 0:
            return f"rvmStats + rejects geremd ({max(stats, rejects)}s)"
        if stats > 0:
            return f"rvmStats geremd ({stats}s)"
        if rejects > 0:
            return f"rejects geremd ({rejects}s)"
        return "Geen throttling"

    def mark_platform_contact(self) -> None:
        self._last_platform_contact = datetime.utcnow()

    @staticmethod
    def _row_get_case_insensitive(row: dict[str, Any], *candidates: str) -> Any:
        lowered = {str(k).lower(): v for k, v in row.items()}
        for candidate in candidates:
            value = lowered.get(str(candidate).lower())
            if value is not None:
                return value
        return None

    def refresh_local_options_from_entry(self) -> None:
        """Refresh cached config values from entry data/options."""
        rates_raw = self.entry.options.get(
            CONF_MACHINE_RATES,
            self.entry.data.get(CONF_MACHINE_RATES, {}),
        ) or {}
        limits_raw = self.entry.options.get(
            CONF_MACHINE_BIN_LIMITS,
            self.entry.data.get(CONF_MACHINE_BIN_LIMITS, {}),
        ) or {}

        clean_rates: dict[str, dict[str, float]] = {}
        for machine_id, values in dict(rates_raw).items():
            item = dict(values or {})
            clean_rates[str(machine_id)] = {
                "can": float(item.get("can", DEFAULT_RATE_CAN)),
                "pet": float(item.get("pet", DEFAULT_RATE_PET)),
            }

        clean_limits: dict[str, dict[str, int]] = {}
        for machine_id, values in dict(limits_raw).items():
            machine_limits: dict[str, int] = {}
            for key, value in dict(values or {}).items():
                try:
                    machine_limits[str(key)] = int(float(value))
                except (TypeError, ValueError):
                    continue
            clean_limits[str(machine_id)] = machine_limits

        self._live_machine_rates = clean_rates
        self._live_machine_bin_limits = clean_limits

    def push_local_change(self) -> None:
        """Force coordinator entities to refresh immediately for local config changes."""
        self._local_revision += 1
        current = dict(self.data or {})
        current["local_revision"] = self._local_revision
        self.async_set_updated_data(current)

    def set_live_machine_rate(self, rvm_id: str, rate_key: str, value: float) -> None:
        machine_rates = dict(
            self._live_machine_rates.get(
                rvm_id,
                {"can": DEFAULT_RATE_CAN, "pet": DEFAULT_RATE_PET},
            ) or {}
        )
        machine_rates[rate_key] = round(float(value), 4)
        self._live_machine_rates[rvm_id] = machine_rates
        self.push_local_change()

    def set_live_bin_limit(self, rvm_id: str, bin_no: int, value: int) -> None:
        machine_limits = dict(self._live_machine_bin_limits.get(rvm_id, {}) or {})
        machine_limits[str(bin_no)] = int(value)
        self._live_machine_bin_limits[rvm_id] = machine_limits
        self.push_local_change()

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
        item = self._live_machine_rates.get(rvm_id, {}) or {}
        return float(item.get("can", DEFAULT_RATE_CAN)), float(item.get("pet", DEFAULT_RATE_PET))

    def machine_bin_limits(self, rvm_id: str) -> dict[str, int]:
        return dict(self._live_machine_bin_limits.get(rvm_id, {}) or {})

    def configured_bin_limit(self, rvm_id: str, bin_no: int) -> int | None:
        return self.machine_bin_limits(rvm_id).get(str(bin_no))

    @staticmethod
    def safe_int(value: Any) -> int:
        try:
            return int(float(str(value).strip()))
        except Exception:
            return 0

    def rvm_data(self, rvm_id: str) -> dict[str, Any]:
        data = self.data or {}
        return (data.get("stats", {}) or {}).get(rvm_id, {}) or {}

    def raw_bin_material(self, rvm_id: str, bin_no: int) -> Any:
        return self.rvm_data(rvm_id).get(f"{BIN_MATERIAL_PREFIX}{bin_no}")

    def machine_meta(self, rvm_id: str) -> dict[str, Any]:
        return self._machine_meta_cache.get(rvm_id, {}) or {}

    def machine_type(self, rvm_id: str) -> str:
        meta = self.machine_meta(rvm_id)
        value = str(meta.get("machine_type") or "").strip()
        if value:
            return value
        rvm = self.rvm_data(rvm_id)
        for key in ("machineType", "RvmType", "RVMType", "MachineType", "Type", "Model"):
            value = str(rvm.get(key) or "").strip()
            if value:
                return value.title()
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
        value = str(
            meta.get("site_id")
            or self.rvm_data(rvm_id).get("SiteId")
            or self.rvm_data(rvm_id).get("SiteInfoSiteId")
            or ""
        ).strip()
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

    def integration_device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {("envipco_rvm", f"{self.entry.entry_id}_platform")},
            "name": "Envipco Platform",
            "manufacturer": "Envipco",
            "model": "ePortal API",
            "entry_type": "service",
        }

    def active_bins(self, rvm_id: str) -> list[int]:
        """Return active bins based only on material.

        Unknown/unset material counts as inactive.
        """
        active: list[int] = []
        for bin_no in range(1, 13):
            material = normalize_material(self.raw_bin_material(rvm_id, bin_no))
            if material is not None:
                active.append(bin_no)
        return active

    def current_bin_limit(self, rvm_id: str, bin_no: int) -> int | None:
        configured = self.configured_bin_limit(rvm_id, bin_no)
        if configured is not None:
            return configured

        api_value = self.safe_int(self.rvm_data(rvm_id).get(f"{BIN_LIMIT_PREFIX}{bin_no}"))
        if api_value > 0:
            return api_value

        material = normalize_material(self.raw_bin_material(rvm_id, bin_no))
        if material:
            return DEFAULT_BIN_CAPACITY_BY_MATERIAL.get(material)

        return None

    def bin_material(self, rvm_id: str, bin_no: int) -> str | None:
        return normalize_material(self.raw_bin_material(rvm_id, bin_no))

    def machine_total_value(self, rvm_id: str, key: str) -> int:
        if key == KEY_ACCEPTED_CANS:
            return self.accepted_cans(rvm_id)
        if key == KEY_ACCEPTED_PET:
            return self.accepted_pet(rvm_id)
        if key == KEY_ACCEPTED_GLASS:
            return self.accepted_glass(rvm_id)
        if key == "accepted_total":
            return self.accepted_total(rvm_id)

        data = self.data or {}
        totals = (data.get("totals", {}) or {}).get(rvm_id, {}) or {}
        return self.safe_int(totals.get(key))

    def accepted_cans(self, rvm_id: str) -> int:
        return self.safe_int(self.rvm_data(rvm_id).get(KEY_ACCEPTED_CANS))

    def accepted_pet(self, rvm_id: str) -> int:
        return self.safe_int(self.rvm_data(rvm_id).get(KEY_ACCEPTED_PET))

    def accepted_glass(self, rvm_id: str) -> int:
        return self.safe_int(self.rvm_data(rvm_id).get(KEY_ACCEPTED_GLASS))

    def accepted_total(self, rvm_id: str) -> int:
        return self.accepted_cans(rvm_id) + self.accepted_pet(rvm_id) + self.accepted_glass(rvm_id)

    async def async_refresh_machine_meta_once(self, force: bool = False) -> None:
        site_ids: set[str] = set()
        stats = (self.data or {}).get("stats", {}) or {}

        for rvm_id in self.rvm_ids():
            if not force and self.machine_meta(rvm_id).get("machine_type") and self.machine_meta(rvm_id).get("add_date"):
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
                self.mark_platform_contact()
            except EnvipcoThrottleError as err:
                _LOGGER.warning(
                    "siteData tijdelijk geremd door API (%s sec); metadata-update wordt later opnieuw geprobeerd",
                    err.seconds,
                )
                return
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
                    "machine_type": str(machine.get("machineType") or existing.get("machine_type") or "").strip()
                    or existing.get("machine_type"),
                    "add_date": str(machine.get("addDate") or existing.get("add_date") or "").strip()
                    or existing.get("add_date"),
                }
                if merged != existing:
                    new_meta[serial] = merged
                    changed = True

        if changed:
            self._machine_meta_cache = new_meta
            options = dict(self.entry.options)
            options[CONF_MACHINE_META] = new_meta
            self.hass.config_entries.async_update_entry(self.entry, options=options)
            self.entry = self.hass.config_entries.async_get_entry(self.entry.entry_id) or self.entry
            self.refresh_local_options_from_entry()
            self.push_local_change()

    def _rejects_interval_seconds(self) -> int:
        return int(
            self.entry.options.get(
                CONF_REJECTS_INTERVAL,
                self.entry.data.get(CONF_REJECTS_INTERVAL, DEFAULT_REJECTS_INTERVAL),
            )
        )

    async def _get_rejects_rows(self) -> list[dict[str, str]]:
        now = datetime.utcnow()

        if self._last_rejects_fetch and (now - self._last_rejects_fetch).total_seconds() < self._rejects_interval_seconds():
            return self._rejects_cache

        if self._rejects_throttle_until and now < self._rejects_throttle_until:
            return self._rejects_cache

        try:
            rows = await self.client.rejects(self.rvm_ids(), date.today(), date.today(), include_acceptance=True)
            self.mark_platform_contact()
            self._rejects_cache = rows
            self._last_rejects_fetch = now
            self._last_rejects_successful_fetch = now
            self._rejects_throttle_until = None
            return rows
        except EnvipcoThrottleError as err:
            self._rejects_throttle_until = now + timedelta(seconds=err.seconds)
            _LOGGER.warning(
                "Rejects tijdelijk geremd door API; cached rejects blijven actief voor nog ongeveer %s seconden",
                err.seconds,
            )
            return self._rejects_cache

    async def _async_update_data(self) -> dict[str, Any]:
        now = datetime.utcnow()

        if self._stats_throttle_until and now < self._stats_throttle_until:
            wait = int((self._stats_throttle_until - now).total_seconds())
            self._last_error = f"HTTP 429: Request was throttled. Expected available in {wait} seconds."
            raise UpdateFailed(self._last_error)

        try:
            stats = await self.client.rvm_stats(self.rvm_ids(), date.today())
            self.mark_platform_contact()
            self._last_stats_fetch = now
            self._stats_throttle_until = None
            rejects_rows = await self._get_rejects_rows()
        except EnvipcoThrottleError as err:
            self._stats_throttle_until = now + timedelta(seconds=err.seconds)
            self._last_error = f"HTTP 429: Request was throttled. Expected available in {err.seconds} seconds."
            raise UpdateFailed(self._last_error) from err
        except EnvipcoApiError as err:
            self._last_error = str(err)
            raise UpdateFailed(self._last_error) from err
        except Exception as err:
            self._last_error = f"Unexpected error: {err}"
            raise UpdateFailed(self._last_error) from err

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
            accepted_cans = self.safe_int((stats.get(rvm_id, {}) or {}).get(KEY_ACCEPTED_CANS))
            accepted_pet = self.safe_int((stats.get(rvm_id, {}) or {}).get(KEY_ACCEPTED_PET))
            accepted_glass = self.safe_int((stats.get(rvm_id, {}) or {}).get(KEY_ACCEPTED_GLASS))
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

        self._last_successful_update = now
        self._last_error = None

        return {
            "stats": stats,
            "rejects": {k: dict(v) for k, v in rejects_by_machine.items()},
            "accepted": {k: dict(v) for k, v in accepted_by_machine.items()},
            "totals": totals,
            "machine_meta": self._machine_meta_cache,
            "local_revision": self._local_revision,
        }