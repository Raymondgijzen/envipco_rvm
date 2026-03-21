"""Config and options flow for the Envipco RVM integration.

Setup is intentionally kept simple:
- validate login
- discover machines
- store base machine list
- keep editable machine values (rates / bin limits) on entity level
"""

from __future__ import annotations

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


class EnvipcoRvmConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Envipco RVM."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Initial setup step."""
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
            rvms = await client.rvms()
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

        machines = [{"id": rid, "name": rid} for rid in rvms]

        # Stored for entity-based editing later on device pages.
        machine_rates = {
            rid: {"can": DEFAULT_RATE_CAN, "pet": DEFAULT_RATE_PET} for rid in rvms
        }
        machine_bin_limits = {rid: {} for rid in rvms}

        data = {
            CONF_MACHINE_META: {},
            CONF_USERNAME: user_input[CONF_USERNAME],
            CONF_PASSWORD: user_input[CONF_PASSWORD],
            CONF_RVMSTATS_INTERVAL: user_input.get(
                CONF_RVMSTATS_INTERVAL,
                DEFAULT_RVMSTATS_INTERVAL,
            ),
            CONF_REJECTS_INTERVAL: user_input.get(
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
    """Handle Envipco RVM options."""

    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        self.entry = entry
        self._new_ids: list[str] = []
        self._selected_new: list[str] = []
        self._pending_opts: dict = {}

    def _machines(self) -> list[dict]:
        raw = (
            self._pending_opts.get(
                CONF_MACHINES,
                self.entry.options.get(
                    CONF_MACHINES,
                    self.entry.data.get(CONF_MACHINES, []),
                ),
            )
            or []
        )
        return [item for item in raw if isinstance(item, dict)]

    def _rates(self) -> dict:
        return (
            self._pending_opts.get(
                CONF_MACHINE_RATES,
                self.entry.options.get(
                    CONF_MACHINE_RATES,
                    self.entry.data.get(CONF_MACHINE_RATES, {}),
                ),
            )
            or {}
        )

    def _machine_meta(self) -> dict:
        return (
            self._pending_opts.get(
                CONF_MACHINE_META,
                self.entry.options.get(
                    CONF_MACHINE_META,
                    self.entry.data.get(CONF_MACHINE_META, {}),
                ),
            )
            or {}
        )

    def _bin_limits(self) -> dict:
        return (
            self._pending_opts.get(
                CONF_MACHINE_BIN_LIMITS,
                self.entry.options.get(
                    CONF_MACHINE_BIN_LIMITS,
                    self.entry.data.get(CONF_MACHINE_BIN_LIMITS, {}),
                ),
            )
            or {}
        )

    async def async_step_init(self, user_input=None):
        """Options step: intervals + optional scan for new machines."""
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
                        vol.Optional("scan_for_new", default=False): bool,
                    }
                ),
            )

        self._pending_opts = dict(self.entry.options)
        self._pending_opts[CONF_RVMSTATS_INTERVAL] = user_input[
            CONF_RVMSTATS_INTERVAL
        ]
        self._pending_opts[CONF_REJECTS_INTERVAL] = user_input[
            CONF_REJECTS_INTERVAL
        ]

        if user_input.get("scan_for_new"):
            from .api import EnvipcoRvmApiClient

            session = async_get_clientsession(self.hass)
            client = EnvipcoRvmApiClient(
                session=session,
                username=self.entry.data[CONF_USERNAME],
                password=self.entry.data[CONF_PASSWORD],
            )

            try:
                all_ids = await client.rvms()
            except Exception:
                return self.async_show_form(
                    step_id="init",
                    data_schema=vol.Schema(
                        {
                            vol.Optional(
                                CONF_RVMSTATS_INTERVAL,
                                default=user_input[CONF_RVMSTATS_INTERVAL],
                            ): vol.All(int, vol.Range(min=60, max=3600)),
                            vol.Optional(
                                CONF_REJECTS_INTERVAL,
                                default=user_input[CONF_REJECTS_INTERVAL],
                            ): vol.All(int, vol.Range(min=300, max=86400)),
                            vol.Optional("scan_for_new", default=True): bool,
                        }
                    ),
                    errors={"base": "cannot_connect"},
                )

            existing = {item.get("id") for item in self._machines() if item.get("id")}
            self._new_ids = [rid for rid in all_ids if rid not in existing]

            if self._new_ids:
                return await self.async_step_select_new()

        return self._finish_options_flow()

    async def async_step_select_new(self, user_input=None):
        """Select newly discovered machines."""
        if user_input is None:
            return self.async_show_form(
                step_id="select_new",
                data_schema=vol.Schema(
                    {
                        vol.Optional("new_machines", default=[]): selector.SelectSelector(
                            selector.SelectSelectorConfig(
                                options=self._new_ids,
                                multiple=True,
                                mode=selector.SelectSelectorMode.DROPDOWN,
                            )
                        )
                    }
                ),
            )

        self._selected_new = user_input.get("new_machines", []) or []

        if not self._selected_new:
            return self._finish_options_flow()

        return await self.async_step_name_new()

    async def async_step_name_new(self, user_input=None):
        """Name newly discovered machines."""
        if user_input is None:
            schema = {
                vol.Optional(f"name_{rid}", default=rid): str
                for rid in self._selected_new
            }
            return self.async_show_form(
                step_id="name_new",
                data_schema=vol.Schema(schema),
            )

        machines = list(self._machines())
        rates = dict(self._rates())
        bin_limits = dict(self._bin_limits())

        for rid in self._selected_new:
            name = (user_input.get(f"name_{rid}", rid) or rid).strip() or rid
            machines.append({"id": rid, "name": name})
            rates.setdefault(rid, {"can": DEFAULT_RATE_CAN, "pet": DEFAULT_RATE_PET})
            bin_limits.setdefault(rid, {})

        self._pending_opts[CONF_MACHINES] = machines
        self._pending_opts[CONF_MACHINE_RATES] = rates
        self._pending_opts[CONF_MACHINE_BIN_LIMITS] = bin_limits

        return self._finish_options_flow()

    def _finish_options_flow(self):
        """Store options without rate/bin editing in the flow itself."""
        self._pending_opts.setdefault(CONF_MACHINES, self._machines())
        self._pending_opts.setdefault(CONF_MACHINE_RATES, self._rates())
        self._pending_opts.setdefault(CONF_MACHINE_BIN_LIMITS, self._bin_limits())
        self._pending_opts.setdefault(CONF_MACHINE_META, self._machine_meta())

        return self.async_create_entry(title="", data=self._pending_opts)
