from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import logging
import re
from typing import Any

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


@dataclass(slots=True)
class MachineDef:
    id: str
    name: str


def normalize_material(raw: Any) -> str | None:
    """Normalize Envipco material values."""
    if raw is None:
        return None

    text = str(raw).strip().upper()
    if not text:
        return None

    return MATERIAL_MAP.get(text, text)


class EnvipcoCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Central coordinator for Envipco API data."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: EnvipcoRvmApiClient,
        entry: ConfigEntry,
        update_interval,
    ) -> None:
        super().__init__(
            hass,
            logger=_LOGGER,
            name=NAME,
            update_interval=update_interval,
        )
        self.client = client
        self.entry = entry
        self._machine_meta_cache: dict[str, dict[str, Any]] = dict(
            entry.options.get(CONF_MACHINE_META, entry.data.get(CONF_MACHINE_META, {})) or {}
        )
        self._last_rejects_fetch: datetime | None = None
        self._rejects_cache: list[dict[str, str]] = []
        self._stats_throttle_until: datetime | None = None
        self._rejects_throttle_until: datetime | None = None
        self._last_successful_update: datetime | None = None
        self._last_error: str | None = None
        self.data = {
            "stats": {},
            "rejects": {},
            "accepted": {},
            "totals": {},
            "machine_meta": self._machine_meta_cache,
        }

    @staticmethod
    def _row_get_case_insensitive(row: dict[str, Any], *candidates: str) -> Any:
        """Get a dict value ignoring key casing."""
        lowered = {str(key).lower(): value for key, value in row.items()}
        for candidate in candidates:
            value = lowered.get(str(candidate).lower())
            if value is not None:
                return value
        return None

    def _entry_value(self, key: str, default: Any) -> Any:
        """Read value from options first, then entry data."""
        return self.entry.options.get(key, self.entry.data.get(key, default))

    def machines(self) -> list[MachineDef]:
        """Return configured machines only."""
        raw = self._entry_value(CONF_MACHINES, []) or []
        machines: list[MachineDef] = []

        for item in raw:
            if not isinstance(item, dict):
                continue

            machine_id = str(item.get("id") or "").strip()
            if not machine_id:
                continue

            machines.append(
                MachineDef(
                    id=machine_id,
                    name=str(item.get("name") or machine_id),
                )
            )

        return machines

    def rvm_ids(self) -> list[str]:
        """Return configured machine ids."""
        return [machine.id for machine in self.machines()]

    def machine_rates(self, rvm_id: str) -> tuple[float, float]:
        """Return configured deposit rates for a machine."""
        rates = self._entry_value(CONF_MACHINE_RATES, {}) or {}
        item = rates.get(rvm_id, {}) or {}
        return float(item.get("can", DEFAULT_RATE_CAN)), float(item.get("pet", DEFAULT_RATE_PET))

    def machine_bin_limits(self, rvm_id: str) -> dict[str, int]:
        """Return manually configured bin limits."""
        all_limits = self._entry_value(CONF_MACHINE_BIN_LIMITS, {}) or {}
        machine_limits = all_limits.get(rvm_id, {}) or {}
        cleaned: dict[str, int] = {}

        for key, value in machine_limits.items():
            try:
                cleaned[str(key)] = int(float(value))
            except (TypeError, ValueError):
                continue

        return cleaned

    def configured_bin_limit(self, rvm_id: str, bin_no: int) -> int | None:
        """Return manually configured bin limit if present."""
        return self.machine_bin_limits(rvm_id).get(str(bin_no))

    @staticmethod
    def safe_int(value: Any) -> int:
        """Convert value to int safely."""
        try:
            return int(float(str(value).strip()))
        except Exception:
            return 0

    def rvm_data(self, rvm_id: str) -> dict[str, Any]:
        """Return raw stats row for one machine."""
        data = self.data or {}
        return (data.get("stats", {}) or {}).get(rvm_id, {}) or {}

    def machine_meta(self, rvm_id: str) -> dict[str, Any]:
        """Return cached machine metadata."""
        return self._machine_meta_cache.get(rvm_id, {}) or {}

    def machine_type(self, rvm_id: str) -> str:
        """Return machine type."""
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
        """Return configured machine name or fallback."""
        configured_names = {machine.id: machine.name for machine in self.machines()}
        configured_name = str(configured_names.get(rvm_id) or "").strip()
        if configured_name:
            return configured_name
        return f"{rvm_id}-{self.machine_type(rvm_id)}"

    def machine_address(self, rvm_id: str) -> str | None:
        """Return address."""
        meta = self.machine_meta(rvm_id)
        return str(meta.get("address") or self.rvm_data(rvm_id).get("SiteInfoAddress") or "").strip() or None

    def machine_postal_code(self, rvm_id: str) -> str | None:
        """Return postal code."""
        meta = self.machine_meta(rvm_id)
        return str(meta.get("postal_code") or self.rvm_data(rvm_id).get("SiteInfoPostalCode") or "").strip() or None

    def machine_city(self, rvm_id: str) -> str | None:
        """Return city."""
        meta = self.machine_meta(rvm_id)
        return str(meta.get("city") or self.rvm_data(rvm_id).get("SiteInfoCity") or "").strip() or None

    def machine_country(self, rvm_id: str) -> str | None:
        """Return country."""
        meta = self.machine_meta(rvm_id)
        return str(meta.get("country") or self.rvm_data(rvm_id).get("SiteInfoCountry") or "").strip() or None

    def machine_add_date(self, rvm_id: str) -> str | None:
        """Return add date."""
        meta = self.machine_meta(rvm_id)
        return str(meta.get("add_date") or "").strip() or None

    def machine_site_id(self, rvm_id: str) -> str | None:
        """Return site id."""
        meta = self.machine_meta(rvm_id)
        value = str(
            meta.get("site_id")
            or self.rvm_data(rvm_id).get("SiteId")
            or self.rvm_data(rvm_id).get("SiteInfoSiteId")
            or ""
        ).strip()
        return value or None

    def machine_site_name(self, rvm_id: str) -> str | None:
        """Return site name."""
        meta = self.machine_meta(rvm_id)
        return str(meta.get("account_name") or "").strip() or None

    def machine_device_info(self, rvm_id: str) -> dict[str, Any]:
        """Return HA device info for one machine."""
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
        """Return bins that are actually active/configured.

        Een bin kan leeg zijn omdat hij net is geleegd.
        Daarom kijken we niet naar actuele inhoud als hoofdbron.
        We gebruiken:
        - materiaal aanwezig
        - API limit > 0
        - handmatige limit > 0
        - API full > 0 als extra hint
        """
        rvm = self.rvm_data(rvm_id)
        active: list[int] = []

        for bin_no in range(1, 13):
            material = normalize_material(rvm.get(f"{BIN_MATERIAL_PREFIX}{bin_no}"))
            api_limit = self.safe_int(rvm.get(f"BinInfoLimitBin{bin_no}"))
            configured_limit = self.configured_bin_limit(rvm_id, bin_no)
            api_full = self.safe_int(rvm.get(f"BinInfoFullBin{bin_no}"))

            if material:
                active.append(bin_no)
                continue

            if api_limit > 0:
                active.append(bin_no)
                continue

            if configured_limit is not None and configured_limit > 0:
                active.append(bin_no)
                continue

            if api_full > 0:
                active.append(bin_no)

        return active

    def current_bin_limit(self, rvm_id: str, bin_no: int) -> int | None:
        """Return best available bin capacity/limit."""
        configured = self.configured_bin_limit(rvm_id, bin_no)
        if configured is not None:
            return configured

        api_value = self.safe_int(self.rvm_data(rvm_id).get(f"BinInfoLimitBin{bin_no}"))
        if api_value > 0:
            return api_value

        material = normalize_material(self.rvm_data(rvm_id).get(f"{BIN_MATERIAL_PREFIX}{bin_no}"))
        if material:
            return DEFAULT_BIN_CAPACITY_BY_MATERIAL.get(material)

        return None

    def bin_material(self, rvm_id: str, bin_no: int) -> str | None:
        """Return normalized material for one bin."""
        return normalize_material(self.rvm_data(rvm_id).get(f"{BIN_MATERIAL_PREFIX}{bin_no}"))

    def bin_count(self, rvm_id: str, bin_no: int) -> int:
        """Return bin count from API.

        Envipco gebruikt in rvmStats:
        BinInfoCountBinX
        """
        return self.safe_int(self.rvm_data(rvm_id).get(f"BinInfoCountBin{bin_no}"))

    def bin_full_percent(self, rvm_id: str, bin_no: int) -> float | None:
        """Return bin fill percentage.

        Hoofdbron:
        - BinInfoFullBinX uit de API, omdat dit overeenkomt met de portal.

        Fallback:
        - count / configured_limit als BinInfoFullBinX ontbreekt
        """
        rvm = self.rvm_data(rvm_id)

        api_full = rvm.get(f"BinInfoFullBin{bin_no}")
        if api_full is not None:
            try:
                value = float(api_full)
                if value < 0:
                    return 0.0
                if value > 100:
                    return 100.0
                return round(value, 1)
            except (TypeError, ValueError):
                pass

        count = self.bin_count(rvm_id, bin_no)
        limit = self.current_bin_limit(rvm_id, bin_no)
        if limit and limit > 0:
            return round((count / limit) * 100, 1)

        return None

    def machine_total_value(self, rvm_id: str, key: str) -> int:
        """Return derived totals."""
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
        """Return accepted cans."""
        return self.safe_int(self.rvm_data(rvm_id).get(KEY_ACCEPTED_CANS))

    def accepted_pet(self, rvm_id: str) -> int:
        """Return accepted PET."""
        return self.safe_int(self.rvm_data(rvm_id).get(KEY_ACCEPTED_PET))

    def accepted_glass(self, rvm_id: str) -> int:
        """Return accepted glass."""
        return self.safe_int(self.rvm_data(rvm_id).get(KEY_ACCEPTED_GLASS))

    def accepted_total(self, rvm_id: str) -> int:
        """Return accepted total."""
        return self.accepted_cans(rvm_id) + self.accepted_pet(rvm_id) + self.accepted_glass(rvm_id)

    @property
    def last_successful_update(self) -> datetime | None:
        """Return last successful update time."""
        return self._last_successful_update

    @property
    def last_error(self) -> str | None:
        """Return last coordinator error."""
        return self._last_error

    @property
    def stats_throttled(self) -> bool:
        """Return whether stats are throttled."""
        return self.stats_throttle_remaining > 0

    @property
    def rejects_throttled(self) -> bool:
        """Return whether rejects are throttled."""
        return self.rejects_throttle_remaining > 0

    @property
    def stats_throttle_remaining(self) -> int:
        """Return remaining stats throttle time in seconds."""
        if not self._stats_throttle_until:
            return 0
        seconds = int((self._stats_throttle_until - datetime.utcnow()).total_seconds())
        return max(0, seconds)

    @property
    def rejects_throttle_remaining(self) -> int:
        """Return remaining rejects throttle time in seconds."""
        if not self._rejects_throttle_until:
            return 0
        seconds = int((self._rejects_throttle_until - datetime.utcnow()).total_seconds())
        return max(0, seconds)

    @property
    def throttle_status_text(self) -> str:
        """Return readable throttle state."""
        if self.stats_throttled and self.rejects_throttled:
            return "Stats en rejects geremd"
        if self.stats_throttled:
            return "Stats geremd"
        if self.rejects_throttled:
            return "Rejects geremd"
        return "OK"

    def _extract_remaining_seconds(self, text: str) -> int | None:
        """Extract throttle duration from API text."""
        match = re.search(r"(\d+)\s*seconds?", text or "", re.IGNORECASE)
        if not match:
            return None

        try:
            return int(match.group(1))
        except (TypeError, ValueError):
            return None

    async def async_refresh_machine_meta_once(self, force: bool = False) -> None:
        """Refresh machine metadata from site data."""
        site_ids: set[str] = set()
        stats = (self.data or {}).get("stats", {}) or {}

        for rvm_id in self.rvm_ids():
            if (
                not force
                and self.machine_meta(rvm_id).get("machine_type")
                and self.machine_meta(rvm_id).get("add_date")
            ):
                continue

            rvm = stats.get(rvm_id, {}) or {}
            site_id = str(rvm.get("SiteId") or rvm.get("SiteInfoSiteId") or "").strip()
            if site_id:
                site_ids.add(site_id)

        if not site_ids:
            return

        selected_ids = set(self.rvm_ids())
        new_meta = dict(self._machine_meta_cache)
        changed = False

        for site_id in sorted(site_ids):
            try:
                site_data = await self.client.site_data(site_id)
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
                if not serial or serial not in selected_ids:
                    continue

                remove_date = str(machine.get("removeDate") or "").strip()
                if remove_date:
                    continue

                existing = dict(new_meta.get(serial, {}) or {})
                merged = {
                    **existing,
                    **{key: value for key, value in site_common.items() if value},
                    "machine_type": str(
                        machine.get("machineType") or existing.get("machine_type") or ""
                    ).strip() or existing.get("machine_type"),
                    "add_date": str(
                        machine.get("addDate") or existing.get("add_date") or ""
                    ).strip() or existing.get("add_date"),
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

    def _rejects_interval_seconds(self) -> int:
        """Return rejects interval in seconds."""
        return int(self._entry_value(CONF_REJECTS_INTERVAL, DEFAULT_REJECTS_INTERVAL))

    async def _get_rejects_rows(self) -> list[dict[str, str]]:
        """Return cached or fresh rejects rows."""
        now = datetime.utcnow()

        if (
            self._last_rejects_fetch
            and (now - self._last_rejects_fetch).total_seconds() < self._rejects_interval_seconds()
        ):
            return self._rejects_cache

        if self._rejects_throttle_until and now < self._rejects_throttle_until:
            return self._rejects_cache

        try:
            rows = await self.client.rejects(
                self.rvm_ids(),
                date.today(),
                date.today(),
                include_acceptance=True,
            )
            self._rejects_cache = rows
            self._last_rejects_fetch = now
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
        """Fetch new data from API."""
        now = datetime.utcnow()

        if self._stats_throttle_until and now < self._stats_throttle_until:
            wait = int((self._stats_throttle_until - now).total_seconds())
            self._last_error = f"HTTP 429: Request was throttled. Expected available in {wait} seconds."
            raise UpdateFailed(self._last_error)

        machine_ids = self.rvm_ids()
        if not machine_ids:
            return {
                "stats": {},
                "rejects": {},
                "accepted": {},
                "totals": {},
                "machine_meta": self._machine_meta_cache,
            }

        try:
            stats = await self.client.rvm_stats(machine_ids, date.today())
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

        selected_ids = set(machine_ids)
        filtered_stats = {
            machine_id: (row or {})
            for machine_id, row in (stats or {}).items()
            if machine_id in selected_ids
        }

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

            if not machine_id or machine_id not in selected_ids:
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
        for rvm_id in machine_ids:
            stat_row = filtered_stats.get(rvm_id, {}) or {}
            accepted_cans = self.safe_int(stat_row.get(KEY_ACCEPTED_CANS))
            accepted_pet = self.safe_int(stat_row.get(KEY_ACCEPTED_PET))
            accepted_glass = self.safe_int(stat_row.get(KEY_ACCEPTED_GLASS))
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

        self._last_successful_update = datetime.utcnow()
        self._last_error = None

        return {
            "stats": filtered_stats,
            "rejects": {machine_id: dict(values) for machine_id, values in rejects_by_machine.items()},
            "accepted": {machine_id: dict(values) for machine_id, values in accepted_by_machine.items()},
            "totals": totals,
            "machine_meta": self._machine_meta_cache,
        }
