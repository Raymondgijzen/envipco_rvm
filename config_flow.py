"""Config and options flow for the Envipco RVM integration.

Ronde 1:
- gebruiker kiest zelf welke machines worden toegevoegd
- die selectie is later opnieuw te wijzigen via opties
- bestaande tarieven en bin-limieten van gekozen machines blijven behouden
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_MACHINE_BIN_LIMITS,
    CONF_MACHINE_META,
    CONF_MACHINE_RATES,
    CONF_MACHINES,
    CONF_PASSWORD,
    CONF_REJECTS_INTERVAL,
    CONF_RVMSTATS_INTERVAL,
    CONF_USERNAME,
    DEFAULT_RATE_CAN,
    DEFAULT_RATE_PET,
    DEFAULT_REJECTS_INTERVAL,
    DEFAULT_RVMSTATS_INTERVAL,
    DOMAIN,
    NAME,
)


SELECTED_MACHINES_KEY = "selected_machines"
MANAGE_MACHINES_KEY = "manage_machines"


def _machine_selector_options(machine_ids: list[str]) -> list[selector.SelectOptionDict]:
    """Build dropdown options for machine selection."""
    return [
        selector.SelectOptionDict(value=machine_id, label=machine_id)
        for machine_id in sorted(machine_ids)
    ]


class EnvipcoRvmConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Envipco RVM."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize flow state."""
        self._login_input: dict[str, Any] = {}
        self._discovered_ids: list[str] = []
        self._selected_ids: list[str] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Validate login and discover available machines."""
        if user_input is None:
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_USERNAME): str,
                        vol.Required(CONF_PASSWORD): str,
                        vol.Optional(
                            CONF_RVMSTATS_INTERVAL,
                            default=DEFAULT_RVMSTATS_INTERVAL,
                        ): vol.All(int, vol.Range(min=60, max=3600)),
                        vol.Optional(
                            CONF_REJECTS_INTERVAL,
                            default=DEFAULT_REJECTS_INTERVAL,
                        ): vol.All(int, vol.Range(min=300, max=86400)),
                    }
                ),
            )

        await self.async_set_unique_id(f"{DOMAIN}_{user_input[CONF_USERNAME]}")
        self._abort_if_unique_id_configured()

        from .api import EnvipcoRvmApiClient

        session = async_get_clientsession(self.hass)
        client = EnvipcoRvmApiClient(
            session=session,
            username=user_input[CONF_USERNAME],
            password=user_input[CONF_PASSWORD],
        )

        try:
            discovered_ids = sorted(await client.rvms())
        except Exception:
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema(
                    {
                        vol.Required(
                            CONF_USERNAME,
                            default=user_input[CONF_USERNAME],
                        ): str,
                        vol.Required(
                            CONF_PASSWORD,
                            default=user_input[CONF_PASSWORD],
                        ): str,
                        vol.Optional(
                            CONF_RVMSTATS_INTERVAL,
                            default=user_input.get(
                                CONF_RVMSTATS_INTERVAL,
                                DEFAULT_RVMSTATS_INTERVAL,
                            ),
                        ): vol.All(int, vol.Range(min=60, max=3600)),
                        vol.Optional(
                            CONF_REJECTS_INTERVAL,
                            default=user_input.get(
                                CONF_REJECTS_INTERVAL,
                                DEFAULT_REJECTS_INTERVAL,
                            ),
                        ): vol.All(int, vol.Range(min=300, max=86400)),
                    }
                ),
                errors={"base": "cannot_connect"},
            )

        if not discovered_ids:
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema(
                    {
                        vol.Required(
                            CONF_USERNAME,
                            default=user_input[CONF_USERNAME],
                        ): str,
                        vol.Required(
                            CONF_PASSWORD,
                            default=user_input[CONF_PASSWORD],
                        ): str,
                        vol.Optional(
                            CONF_RVMSTATS_INTERVAL,
                            default=user_input.get(
                                CONF_RVMSTATS_INTERVAL,
                                DEFAULT_RVMSTATS_INTERVAL,
                            ),
                        ): vol.All(int, vol.Range(min=60, max=3600)),
                        vol.Optional(
                            CONF_REJECTS_INTERVAL,
                            default=user_input.get(
                                CONF_REJECTS_INTERVAL,
                                DEFAULT_REJECTS_INTERVAL,
                            ),
                        ): vol.All(int, vol.Range(min=300, max=86400)),
                    }
                ),
                errors={"base": "no_machines_found"},
            )

        self._login_input = dict(user_input)
        self._discovered_ids = discovered_ids
        self._selected_ids = list(discovered_ids)
        return await self.async_step_select_machines()

    async def async_step_select_machines(
        self,
        user_input: dict[str, Any] | None = None,
    ):
        """Let the user choose which machines should be added."""
        if user_input is None:
            return self.async_show_form(
                step_id="select_machines",
                data_schema=vol.Schema(
                    {
                        vol.Required(
                            SELECTED_MACHINES_KEY,
                            default=self._selected_ids,
                        ): selector.SelectSelector(
                            selector.SelectSelectorConfig(
                                options=_machine_selector_options(self._discovered_ids),
                                multiple=True,
                                mode=selector.SelectSelectorMode.DROPDOWN,
                            )
                        )
                    }
                ),
            )

        self._selected_ids = sorted(user_input.get(SELECTED_MACHINES_KEY, []) or [])
        if not self._selected_ids:
            return self.async_show_form(
                step_id="select_machines",
                data_schema=vol.Schema(
                    {
                        vol.Required(
                            SELECTED_MACHINES_KEY,
                            default=[],
                        ): selector.SelectSelector(
                            selector.SelectSelectorConfig(
                                options=_machine_selector_options(self._discovered_ids),
                                multiple=True,
                                mode=selector.SelectSelectorMode.DROPDOWN,
                            )
                        )
                    }
                ),
                errors={"base": "select_at_least_one_machine"},
            )

        return await self.async_step_name_machines()

    async def async_step_name_machines(
        self,
        user_input: dict[str, Any] | None = None,
    ):
        """Allow friendly names for the selected machines."""
        if user_input is None:
            schema = {
                vol.Optional(f"name_{machine_id}", default=machine_id): str
                for machine_id in self._selected_ids
            }
            return self.async_show_form(
                step_id="name_machines",
                data_schema=vol.Schema(schema),
            )

        machines: list[dict[str, str]] = []
        for machine_id in self._selected_ids:
            name = (user_input.get(f"name_{machine_id}", machine_id) or machine_id).strip() or machine_id
            machines.append({"id": machine_id, "name": name})

        machine_rates = {
            machine_id: {"can": DEFAULT_RATE_CAN, "pet": DEFAULT_RATE_PET}
            for machine_id in self._selected_ids
        }
        machine_bin_limits = {machine_id: {} for machine_id in self._selected_ids}

        data = {
            CONF_MACHINE_META: {},
            CONF_USERNAME: self._login_input[CONF_USERNAME],
            CONF_PASSWORD: self._login_input[CONF_PASSWORD],
            CONF_RVMSTATS_INTERVAL: self._login_input.get(
                CONF_RVMSTATS_INTERVAL,
                DEFAULT_RVMSTATS_INTERVAL,
            ),
            CONF_REJECTS_INTERVAL: self._login_input.get(
                CONF_REJECTS_INTERVAL,
                DEFAULT_REJECTS_INTERVAL,
            ),
            CONF_MACHINES: machines,
            CONF_MACHINE_RATES: machine_rates,
            CONF_MACHINE_BIN_LIMITS: machine_bin_limits,
        }
        return self.async_create_entry(title=NAME, data=data)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Return the options flow."""
        return EnvipcoRvmOptionsFlow(config_entry)


class EnvipcoRvmOptionsFlow(config_entries.OptionsFlow):
    """Handle options for Envipco RVM."""

    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow state."""
        self.entry = entry
        self._pending_opts: dict[str, Any] = {}
        self._discovered_ids: list[str] = []
        self._selected_ids: list[str] = []

    def _machines(self) -> list[dict[str, Any]]:
        """Return configured machines."""
        raw = self._pending_opts.get(
            CONF_MACHINES,
            self.entry.options.get(
                CONF_MACHINES,
                self.entry.data.get(CONF_MACHINES, []),
            ),
        ) or []
        return [item for item in raw if isinstance(item, dict) and item.get("id")]

    def _rates(self) -> dict[str, Any]:
        """Return configured rates."""
        return self._pending_opts.get(
            CONF_MACHINE_RATES,
            self.entry.options.get(
                CONF_MACHINE_RATES,
                self.entry.data.get(CONF_MACHINE_RATES, {}),
            ),
        ) or {}

    def _machine_meta(self) -> dict[str, Any]:
        """Return machine metadata."""
        return self._pending_opts.get(
            CONF_MACHINE_META,
            self.entry.options.get(
                CONF_MACHINE_META,
                self.entry.data.get(CONF_MACHINE_META, {}),
            ),
        ) or {}

    def _bin_limits(self) -> dict[str, Any]:
        """Return machine bin limits."""
        return self._pending_opts.get(
            CONF_MACHINE_BIN_LIMITS,
            self.entry.options.get(
                CONF_MACHINE_BIN_LIMITS,
                self.entry.data.get(CONF_MACHINE_BIN_LIMITS, {}),
            ),
        ) or {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        """Initial options step."""
        stats_default = self.entry.options.get(
            CONF_RVMSTATS_INTERVAL,
            self.entry.data.get(CONF_RVMSTATS_INTERVAL, DEFAULT_RVMSTATS_INTERVAL),
        )
        rejects_default = self.entry.options.get(
            CONF_REJECTS_INTERVAL,
            self.entry.data.get(CONF_REJECTS_INTERVAL, DEFAULT_REJECTS_INTERVAL),
        )

        if user_input is None:
            return self.async_show_form(
                step_id="init",
                data_schema=vol.Schema(
                    {
                        vol.Optional(
                            CONF_RVMSTATS_INTERVAL,
                            default=stats_default,
                        ): vol.All(int, vol.Range(min=60, max=3600)),
                        vol.Optional(
                            CONF_REJECTS_INTERVAL,
                            default=rejects_default,
                        ): vol.All(int, vol.Range(min=300, max=86400)),
                        vol.Optional(MANAGE_MACHINES_KEY, default=True): bool,
                    }
                ),
            )

        self._pending_opts = dict(self.entry.options)
        self._pending_opts[CONF_RVMSTATS_INTERVAL] = user_input[CONF_RVMSTATS_INTERVAL]
        self._pending_opts[CONF_REJECTS_INTERVAL] = user_input[CONF_REJECTS_INTERVAL]

        if user_input.get(MANAGE_MACHINES_KEY):
            return await self.async_step_manage_machines()

        return await self.async_step_rates()

    async def async_step_manage_machines(
        self,
        user_input: dict[str, Any] | None = None,
    ):
        """Rediscover machines and let the user choose the active set."""
        from .api import EnvipcoRvmApiClient

        if not self._discovered_ids:
            session = async_get_clientsession(self.hass)
            client = EnvipcoRvmApiClient(
                session=session,
                username=self.entry.data[CONF_USERNAME],
                password=self.entry.data[CONF_PASSWORD],
            )
            try:
                self._discovered_ids = sorted(await client.rvms())
            except Exception:
                return self.async_show_form(
                    step_id="init",
                    data_schema=vol.Schema(
                        {
                            vol.Optional(
                                CONF_RVMSTATS_INTERVAL,
                                default=self._pending_opts.get(
                                    CONF_RVMSTATS_INTERVAL,
                                    DEFAULT_RVMSTATS_INTERVAL,
                                ),
                            ): vol.All(int, vol.Range(min=60, max=3600)),
                            vol.Optional(
                                CONF_REJECTS_INTERVAL,
                                default=self._pending_opts.get(
                                    CONF_REJECTS_INTERVAL,
                                    DEFAULT_REJECTS_INTERVAL,
                                ),
                            ): vol.All(int, vol.Range(min=300, max=86400)),
                            vol.Optional(MANAGE_MACHINES_KEY, default=True): bool,
                        }
                    ),
                    errors={"base": "cannot_connect"},
                )

            current_ids = [item["id"] for item in self._machines()]
            self._selected_ids = [
                machine_id for machine_id in self._discovered_ids if machine_id in current_ids
            ]
            if not self._selected_ids:
                self._selected_ids = list(self._discovered_ids)

        if user_input is None:
            return self.async_show_form(
                step_id="manage_machines",
                data_schema=vol.Schema(
                    {
                        vol.Required(
                            SELECTED_MACHINES_KEY,
                            default=self._selected_ids,
                        ): selector.SelectSelector(
                            selector.SelectSelectorConfig(
                                options=_machine_selector_options(self._discovered_ids),
                                multiple=True,
                                mode=selector.SelectSelectorMode.DROPDOWN,
                            )
                        )
                    }
                ),
            )

        self._selected_ids = sorted(user_input.get(SELECTED_MACHINES_KEY, []) or [])
        if not self._selected_ids:
            return self.async_show_form(
                step_id="manage_machines",
                data_schema=vol.Schema(
                    {
                        vol.Required(
                            SELECTED_MACHINES_KEY,
                            default=[],
                        ): selector.SelectSelector(
                            selector.SelectSelectorConfig(
                                options=_machine_selector_options(self._discovered_ids),
                                multiple=True,
                                mode=selector.SelectSelectorMode.DROPDOWN,
                            )
                        )
                    }
                ),
                errors={"base": "select_at_least_one_machine"},
            )

        return await self.async_step_name_machines()

    async def async_step_name_machines(
        self,
        user_input: dict[str, Any] | None = None,
    ):
        """Rename selected machines while preserving existing config where possible."""
        current_names = {
            item["id"]: item.get("name", item["id"])
            for item in self._machines()
            if item.get("id")
        }

        if user_input is None:
            schema = {
                vol.Optional(
                    f"name_{machine_id}",
                    default=current_names.get(machine_id, machine_id),
                ): str
                for machine_id in self._selected_ids
            }
            return self.async_show_form(
                step_id="name_machines",
                data_schema=vol.Schema(schema),
            )

        old_rates = dict(self._rates())
        old_meta = dict(self._machine_meta())
        old_bin_limits = dict(self._bin_limits())

        machines: list[dict[str, str]] = []
        new_rates: dict[str, dict[str, float]] = {}
        new_meta: dict[str, Any] = {}
        new_bin_limits: dict[str, Any] = {}

        for machine_id in self._selected_ids:
            name = (user_input.get(f"name_{machine_id}", machine_id) or machine_id).strip() or machine_id
            machines.append({"id": machine_id, "name": name})
            new_rates[machine_id] = old_rates.get(
                machine_id,
                {"can": DEFAULT_RATE_CAN, "pet": DEFAULT_RATE_PET},
            )
            if machine_id in old_meta:
                new_meta[machine_id] = old_meta[machine_id]
            new_bin_limits[machine_id] = old_bin_limits.get(machine_id, {})

        self._pending_opts[CONF_MACHINES] = machines
        self._pending_opts[CONF_MACHINE_RATES] = new_rates
        self._pending_opts[CONF_MACHINE_META] = new_meta
        self._pending_opts[CONF_MACHINE_BIN_LIMITS] = new_bin_limits
        return await self.async_step_rates()

    async def async_step_rates(self, user_input: dict[str, Any] | None = None):
        """Configure per-machine rates for the active selection."""
        machines = self._pending_opts.get(CONF_MACHINES, self._machines())
        rates = self._pending_opts.get(CONF_MACHINE_RATES, self._rates())

        if user_input is None:
            schema_dict: dict[Any, Any] = {}
            for machine in machines:
                machine_id = machine["id"]
                current = rates.get(machine_id, {}) or {}
                schema_dict[
                    vol.Optional(
                        f"can_{machine_id}",
                        default=float(current.get("can", DEFAULT_RATE_CAN)),
                    )
                ] = vol.All(vol.Coerce(float), vol.Range(min=0, max=5))
                schema_dict[
                    vol.Optional(
                        f"pet_{machine_id}",
                        default=float(current.get("pet", DEFAULT_RATE_PET)),
                    )
                ] = vol.All(vol.Coerce(float), vol.Range(min=0, max=5))

            return self.async_show_form(
                step_id="rates",
                data_schema=vol.Schema(schema_dict),
            )

        new_rates = dict(rates)
        for machine in machines:
            machine_id = machine["id"]
            new_rates[machine_id] = {
                "can": round(float(user_input.get(f"can_{machine_id}", DEFAULT_RATE_CAN)), 4),
                "pet": round(float(user_input.get(f"pet_{machine_id}", DEFAULT_RATE_PET)), 4),
            }

        self._pending_opts[CONF_MACHINES] = machines
        self._pending_opts[CONF_MACHINE_RATES] = new_rates
        self._pending_opts.setdefault(CONF_MACHINE_META, self._machine_meta())
        self._pending_opts.setdefault(CONF_MACHINE_BIN_LIMITS, self._bin_limits())
        return self.async_create_entry(title="", data=self._pending_opts)
