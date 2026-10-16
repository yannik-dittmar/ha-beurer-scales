"""Config flow for adding a Beurer scale."""

from typing import Any

import probatio
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS

from .const import CONF_MODEL, DOMAIN, MANUFACTURER
from .protocol.models import ScaleModel, model_from_advertisement


def _display_name(model: ScaleModel) -> str:
    return f"{MANUFACTURER} {model.name}"


class BeurerScalesConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add one Beurer scale. Adding a scale never connects to it."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._address: str | None = None
        self._model: ScaleModel | None = None
        self._discovered: dict[str, ScaleModel] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a scale found by Bluetooth discovery."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        model = model_from_advertisement(
            discovery_info.advertisement.local_name, discovery_info.service_data
        )
        if model is None:
            return self.async_abort(reason="not_supported")
        self._address = discovery_info.address
        self._model = model
        self.context["title_placeholders"] = {"name": _display_name(model)}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask the user to confirm a discovered scale."""
        assert self._address is not None and self._model is not None
        if user_input is not None:
            return self._create_scale_entry(self._address, self._model)
        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={"name": _display_name(self._model)},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user pick one of the unconfigured scales HA has seen nearby."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self._create_scale_entry(address, self._discovered[address])

        configured = self._async_current_ids(include_ignore=False)
        # Connectable advertisements only: a scale that HA can't connect to can't sync.
        for info in async_discovered_service_info(self.hass):
            if info.address in configured or info.address in self._discovered:
                continue
            if model := model_from_advertisement(
                info.advertisement.local_name, info.service_data
            ):
                self._discovered[info.address] = model
        if not self._discovered:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=probatio.Schema(
                {
                    probatio.Required(CONF_ADDRESS): probatio.In(
                        {
                            address: f"{_display_name(model)} ({address})"
                            for address, model in self._discovered.items()
                        }
                    )
                }
            ),
        )

    def _create_scale_entry(self, address: str, model: ScaleModel) -> ConfigFlowResult:
        return self.async_create_entry(
            title=_display_name(model),
            data={CONF_ADDRESS: address, CONF_MODEL: model.name},
        )
