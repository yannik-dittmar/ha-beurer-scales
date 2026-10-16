"""The "Add scale user" subentry flow: create a new scale user or adopt a slot."""

import asyncio
from typing import Any

import probatio
from homeassistant.config_entries import ConfigSubentryFlow, SubentryFlowResult
from homeassistant.const import CONF_ADDRESS
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
)
from homeassistant.util import dt as dt_util

from .connection import WakeTimeout, async_open_session, async_wait_for_wake
from .const import (
    CONF_CONSENT_CODE,
    CONF_MODEL,
    CONF_NAME,
    CONF_PERSON,
    CONF_PROFILE,
    CONF_SLOT,
    PROFILE_ACTIVITY_LEVEL,
    PROFILE_BIRTH_DATE,
    PROFILE_GENDER,
    PROFILE_HEIGHT_CM,
    slot_unique_id,
)
from .protocol.errors import ConsentRejected, PairingUnsupported, ScaleError
from .protocol.frames import UserListEntry
from .protocol.models import ScaleModel, model_by_name


class SlotNotFound(ScaleError):
    """The scale's user list has no scale user in the slot."""


def profile_data(profile: UserListEntry) -> dict[str, Any]:
    """Return a profile as stored in the scale user subentry."""
    return {
        PROFILE_BIRTH_DATE: (
            profile.birth_date.isoformat() if profile.birth_date else None
        ),
        PROFILE_HEIGHT_CM: profile.height_cm,
        PROFILE_GENDER: str(profile.gender),
        PROFILE_ACTIVITY_LEVEL: profile.activity_level,
    }


class ScaleUserSubentryFlow(ConfigSubentryFlow):
    """Add a scale user to a scale."""

    def __init__(self) -> None:
        """Initialize the flow."""
        self._input: dict[str, Any] = {}
        self._name = ""
        self._person_id: str | None = None
        # Errors to show when the progress step returns to the form.
        self._errors: dict[str, str] = {}
        self._abort_reason = ""
        self._task: asyncio.Task[UserListEntry] | None = None
        self._profile: UserListEntry | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Choose how to add the scale user."""
        return self.async_show_menu(step_id="user", menu_options=["create", "adopt"])

    async def async_step_create(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Create a new scale user on the scale (not built yet)."""
        return self.async_abort(reason="create_not_available")

    async def async_step_adopt(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Ask for the person or name, and the slot with its consent code."""
        errors, self._errors = self._errors, {}
        if user_input is not None:
            self._input = user_input
            errors = self._validate_identity(user_input)
            if not errors:
                return await self.async_step_wake()

        schema = probatio.Schema(
            {
                probatio.Optional(CONF_PERSON): EntitySelector(
                    EntitySelectorConfig(domain="person")
                ),
                probatio.Optional(CONF_NAME): TextSelector(),
                probatio.Required(CONF_SLOT): NumberSelector(
                    NumberSelectorConfig(
                        min=1,
                        max=self._model().slot_count,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                probatio.Required(CONF_CONSENT_CODE): NumberSelector(
                    NumberSelectorConfig(
                        min=0, max=9999, step=1, mode=NumberSelectorMode.BOX
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="adopt",
            data_schema=self.add_suggested_values_to_schema(schema, self._input),
            errors=errors,
        )

    async def async_step_wake(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Wait for someone to step on the scale, then verify the slot."""
        if self._task is None:
            self._task = self.hass.async_create_task(self._async_adopt())
        if not self._task.done():
            return self.async_show_progress(
                step_id="wake", progress_action="wake", progress_task=self._task
            )

        task, self._task = self._task, None
        try:
            self._profile = task.result()
        except WakeTimeout:
            return self._progress_failed("wake_timeout")
        except SlotNotFound:
            return self._progress_error({CONF_SLOT: "slot_not_found"})
        except ConsentRejected:
            return self._progress_error({CONF_CONSENT_CODE: "invalid_consent_code"})
        except PairingUnsupported:
            return self._progress_failed("pairing_unsupported")
        except ScaleError:
            return self._progress_failed("cannot_connect")
        return self.async_show_progress_done(next_step_id="finish")

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Create the scale user subentry."""
        assert self._profile is not None
        return self.async_create_entry(
            title=self._name,
            unique_id=slot_unique_id(self._profile.slot),
            data={
                CONF_SLOT: self._profile.slot,
                CONF_CONSENT_CODE: int(self._input[CONF_CONSENT_CODE]),
                CONF_NAME: self._name,
                CONF_PERSON: self._person_id,
                CONF_PROFILE: profile_data(self._profile),
            },
        )

    async def async_step_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """End the flow after the scale couldn't be woken or reached."""
        return self.async_abort(reason=self._abort_reason)

    def _progress_error(self, errors: dict[str, str]) -> SubentryFlowResult:
        self._errors = errors
        return self.async_show_progress_done(next_step_id="adopt")

    def _progress_failed(self, reason: str) -> SubentryFlowResult:
        self._abort_reason = reason
        return self.async_show_progress_done(next_step_id="failed")

    def _model(self) -> ScaleModel:
        return model_by_name(self._get_entry().data[CONF_MODEL])

    def _validate_identity(self, user_input: dict[str, Any]) -> dict[str, str]:
        """Resolve the name and the person, and check the slot; return form errors."""
        person = user_input.get(CONF_PERSON) or None
        name = (user_input.get(CONF_NAME) or "").strip()
        if not name and person and (state := self.hass.states.get(person)):
            name = state.name
        if not name:
            return {"base": "name_required"}

        subentries = self._get_entry().subentries.values()
        if any(s.title.casefold() == name.casefold() for s in subentries):
            return {CONF_NAME: "name_in_use"}
        slot = int(user_input[CONF_SLOT])
        if any(s.unique_id == slot_unique_id(slot) for s in subentries):
            return {CONF_SLOT: "slot_in_use"}

        self._name = name
        registry_entry = er.async_get(self.hass).async_get(person) if person else None
        self._person_id = registry_entry.id if registry_entry else None
        return {}

    async def _async_adopt(self) -> UserListEntry:
        """Wait for the wake, then check that the slot exists and the code works."""
        address: str = self._get_entry().data[CONF_ADDRESS]
        slot = int(self._input[CONF_SLOT])
        await async_wait_for_wake(self.hass, address)
        async with async_open_session(self.hass, address, self._model()) as session:
            await session.set_time(dt_util.now())
            # The scale answers a wrong code and a missing slot alike (§9.4b);
            # the user list, which needs no consent, tells them apart.
            users = await session.read_user_list()
            profile = next((u for u in users if u.slot == slot), None)
            if profile is None:
                raise SlotNotFound(f"slot {slot}")
            await session.consent(slot, int(self._input[CONF_CONSENT_CODE]))
        return profile
