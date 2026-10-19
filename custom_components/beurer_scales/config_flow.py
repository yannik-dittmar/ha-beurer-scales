"""Config flow for adding a Beurer scale."""

import asyncio
from typing import Any

import probatio
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.helpers.selector import BooleanSelector

from .connection import WakeTimeout
from .const import (
    CONF_MODEL,
    CONF_NOTIFY_UNRECOGNISED,
    DEFAULT_NOTIFY_UNRECOGNISED,
    DOMAIN,
    MANUFACTURER,
    SUBENTRY_SCALE_USER,
)
from .factory_reset import async_factory_reset_scale, async_forget_scale_users
from .protocol.errors import PairingFailed, PairingUnsupported
from .protocol.models import ScaleModel, model_by_name, model_from_advertisement
from .scale_user_flow import ScaleUserSubentryFlow


def _display_name(model: ScaleModel) -> str:
    return f"{MANUFACTURER} {model.name}"


class BeurerScalesConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add one Beurer scale. Adding a scale never connects to it."""

    VERSION = 1

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Scale users are subentries of their scale (ADR 0004)."""
        return {SUBENTRY_SCALE_USER: ScaleUserSubentryFlow}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """The scale's options."""
        return BeurerScalesOptionsFlow()

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
        if not model.verified:
            return await self.async_step_untested_model()
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
            picked = self._discovered[address]
            if not picked.verified:
                self._address = address
                self._model = picked
                return await self.async_step_untested_model()
            return self._create_scale_entry(address, picked)

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

    async def async_step_untested_model(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Warn that the model is untested; confirming adds it all the same."""
        assert self._address is not None and self._model is not None
        if user_input is not None:
            return self._create_scale_entry(self._address, self._model)
        self._set_confirm_only()
        return self.async_show_form(
            step_id="untested_model",
            description_placeholders={"name": _display_name(self._model)},
        )

    def _create_scale_entry(self, address: str, model: ScaleModel) -> ConfigFlowResult:
        return self.async_create_entry(
            title=_display_name(model),
            data={CONF_ADDRESS: address, CONF_MODEL: model.name},
        )


class BeurerScalesOptionsFlow(OptionsFlow):
    """The scale's configure menu: its options, and a factory reset."""

    def __init__(self) -> None:
        """Initialize the flow."""
        self._reset_task: asyncio.Task[None] | None = None
        self._reset_failure = "cannot_connect"

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose between the scale's options and a factory reset."""
        options = ["options"]
        if model_by_name(self.config_entry.data[CONF_MODEL]).factory_reset_uuid:
            options.append("factory_reset")
        return self.async_show_menu(step_id="init", menu_options=options)

    async def async_step_options(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose whether HA notifies about unrecognised weigh-ins.

        The coordinator reads the options when needed, so there is no reload.
        """
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        notify = self.config_entry.options.get(
            CONF_NOTIFY_UNRECOGNISED, DEFAULT_NOTIFY_UNRECOGNISED
        )
        return self.async_show_form(
            step_id="options",
            data_schema=probatio.Schema(
                {
                    probatio.Required(
                        CONF_NOTIFY_UNRECOGNISED, default=notify
                    ): BooleanSelector()
                }
            ),
        )

    async def async_step_factory_reset(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Warn what a factory reset deletes and ask for confirmation."""
        if user_input is not None:
            return await self.async_step_factory_reset_wake()
        return self.async_show_form(step_id="factory_reset")

    async def async_step_factory_reset_wake(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Progress: wait for the step-on that wakes the scale; reset it."""
        if self._reset_task is None:
            self._reset_task = self.hass.async_create_task(
                async_factory_reset_scale(self.hass, self.config_entry)
            )
        if not self._reset_task.done():
            return self.async_show_progress(
                step_id="factory_reset_wake",
                progress_action="factory_reset_wake",
                progress_task=self._reset_task,
            )
        task, self._reset_task = self._reset_task, None
        err = None if task.cancelled() else task.exception()
        if task.cancelled() or err is not None:
            self._reset_failure = (
                _reset_failure_reason(err) if err else "cannot_connect"
            )
            return self.async_show_progress_done(next_step_id="factory_reset_failed")
        return self.async_show_progress_done(next_step_id="factory_reset_done")

    async def async_step_factory_reset_done(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Forget the scale users, which the reset deleted, and offer to add one."""
        await async_forget_scale_users(
            self.hass, self.config_entry, self.config_entry.runtime_data
        )
        return self.async_abort(reason="factory_reset_done")

    async def async_step_factory_reset_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The reset didn't happen, or the scale is in an unknown state."""
        return self.async_abort(reason=self._reset_failure)

    @callback
    def async_remove(self) -> None:
        """Stop waiting for the wake when the flow is closed."""
        if self._reset_task is not None:
            self._reset_task.cancel()


def _reset_failure_reason(err: BaseException) -> str:
    if isinstance(err, WakeTimeout):
        return "wake_timeout"
    if isinstance(err, PairingUnsupported | PairingFailed):
        return "pairing_unsupported"
    return "cannot_connect"
