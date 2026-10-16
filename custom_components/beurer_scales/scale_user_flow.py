"""The "Add scale user" subentry flow: create a new scale user or adopt a slot."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import date, datetime
from types import MappingProxyType
from typing import Any

import probatio
from homeassistant.config_entries import (
    ConfigSubentry,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    DateSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
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
    SUBENTRY_SCALE_USER,
    slot_unique_id,
)
from .coordinator import BeurerConfigEntry, async_hand_over_readings
from .protocol import session as protocol_session
from .protocol import teaching as protocol_teaching
from .protocol.errors import (
    ConsentRejected,
    NoFreeSlot,
    PairingFailed,
    PairingUnsupported,
    ScaleError,
    TeachingTimeout,
)
from .protocol.frames import Gender, UserListEntry
from .protocol.models import ScaleModel, model_by_name
from .protocol.profile import Profile
from .protocol.readings import Reading
from .protocol.scale_user import CreatedScaleUser, create_scale_user
from .protocol.teaching import TEACHING_READINGS, Teaching

# Beurer's supported ranges for body-composition scales.
MIN_HEIGHT_CM = 100
MAX_HEIGHT_CM = 220
MIN_AGE = 10
MAX_AGE = 100


class SlotNotFound(ScaleError):
    """The scale's user list has no scale user in the slot."""


def profile_data(profile: UserListEntry | Profile) -> dict[str, Any]:
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
        # The create path.
        self._new_profile: Profile | None = None
        # Holds the connection from the wake through teaching.
        self._create_task: asyncio.Task[CreatedScaleUser] | None = None
        self._created: CreatedScaleUser | None = None
        self._created_event = asyncio.Event()
        self._teaching = Teaching()
        # When the create task connected, for the backlog rule.
        self._sync_start: datetime | None = None
        # What the progress step shown right now waits for.
        self._milestone: asyncio.Task[None] | None = None
        self._entry_created = False
        # The reconfigure path.
        self._reconfigure_task: asyncio.Task[None] | None = None
        # Readings the scale delivered when the flow consented.
        self._readings: list[Reading] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Choose how to add the scale user."""
        return self.async_show_menu(step_id="user", menu_options=["create", "adopt"])

    async def async_step_create(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Ask for the person or name and the profile of the new scale user."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._input = user_input
            errors = self._validate_identity(user_input) or self._validate_profile(
                user_input
            )
            if not errors:
                return await self.async_step_create_wake()

        schema = probatio.Schema(
            {
                probatio.Optional(CONF_PERSON): EntitySelector(
                    EntitySelectorConfig(domain="person")
                ),
                probatio.Optional(CONF_NAME): TextSelector(),
                **_profile_fields(),
            }
        )
        return self.async_show_form(
            step_id="create",
            data_schema=self.add_suggested_values_to_schema(schema, self._input),
            errors=errors,
        )

    async def async_step_create_wake(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Wait for the wake, then create the scale user on the scale."""
        if self._create_task is None:
            # A background task: it outlives the progress steps and the menu.
            self._create_task = self._get_entry().async_create_background_task(
                self.hass, self._async_create(), "beurer_scales create scale user"
            )
        if result := self._progress_until(
            "create_wake", "create", self._created_event.wait
        ):
            return result
        if self._create_task.done() and (reason := self._create_failure()):
            return self._progress_failed(reason)
        return self.async_show_progress_done(next_step_id="teach")

    async def async_step_teach(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Teaching: wait for the next live reading of the new scale user."""
        taken = len(self._teaching.readings)
        if result := self._progress_until(
            "teach",
            "teach",
            lambda: self._async_until_reading(taken + 1),
            {"reading": str(taken + 1), "readings": str(TEACHING_READINGS)},
        ):
            return result
        assert self._create_task is not None
        if not self._create_task.done():
            return self.async_show_progress_done(next_step_id="teach_more")
        if reason := self._create_failure():
            return self._progress_failed(reason)
        return self.async_show_progress_done(next_step_id="create_finish")

    async def async_step_teach_more(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Offer another teaching reading or finishing now."""
        return self.async_show_menu(
            step_id="teach_more",
            menu_options=["teach", "teach_done"],
            description_placeholders={
                "reading": str(len(self._teaching.readings)),
                "readings": str(TEACHING_READINGS),
            },
        )

    async def async_step_teach_done(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Finish teaching early: disconnect, then create the subentry."""
        assert self._create_task is not None
        self._teaching.finish()
        await asyncio.wait({self._create_task})
        if reason := self._create_failure():
            return self.async_abort(reason=reason)
        return await self.async_step_create_finish()

    async def async_step_create_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Create the subentry of the new scale user."""
        self._keep_teaching_readings()
        self._entry_created = True
        return self.async_create_entry(
            title=self._name,
            unique_id=self._new_unique_id(),
            data=self._new_data(),
            description="teaching_timeout" if self._teaching.timed_out else None,
        )

    @callback
    def async_remove(self) -> None:
        """Clean up after the flow closed, finished or not.

        Before teaching ended, the scale user is deleted on the scale again. One
        whose teaching ended with a reading stays on the scale, so it gets its
        subentry even when the flow is closed without finishing.
        """
        if self._milestone is not None:
            self._milestone.cancel()
        task = self._create_task
        if task is None or self._entry_created:
            return
        if not task.done():
            task.cancel()
            return
        if self._create_failure() is None:
            self._keep_teaching_readings()
            self.hass.config_entries.async_add_subentry(
                self._get_entry(),
                ConfigSubentry(
                    data=MappingProxyType(self._new_data()),
                    subentry_type=SUBENTRY_SCALE_USER,
                    title=self._name,
                    unique_id=self._new_unique_id(),
                ),
            )

    async def async_step_adopt(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Ask for the person or name, and the slot with its consent code."""
        errors, self._errors = self._errors, {}
        if user_input is not None:
            self._input = user_input
            errors = self._validate_identity(user_input) or self._validate_slot(
                user_input
            )
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
        except SlotNotFound:
            return self._progress_error({CONF_SLOT: "slot_not_found"})
        except ConsentRejected:
            return self._progress_error({CONF_CONSENT_CODE: "invalid_consent_code"})
        except (WakeTimeout, ScaleError) as err:
            return self._progress_failed(_failure_reason(err))
        return self.async_show_progress_done(next_step_id="finish")

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Create the scale user subentry."""
        assert self._profile is not None
        # The scale clears the slot's pending flag once it delivered them, so
        # they would be lost; the sync pipeline takes them at the next setup.
        async_hand_over_readings(
            self.hass, self._get_entry().entry_id, self._profile.slot, self._readings
        )
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

    # --- Reconfigure: change the name, person or profile (design §4.2) ---

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Change the scale user; a profile change needs the scale, a rename doesn't."""
        subentry = self._get_reconfigure_subentry()
        errors: dict[str, str] = {}
        if user_input is not None:
            self._input = user_input
            errors = self._validate_identity(
                user_input, own=subentry
            ) or self._validate_profile(user_input)
            if not errors:
                assert self._new_profile is not None
                if profile_data(self._new_profile) == subentry.data[CONF_PROFILE]:
                    return self._async_save_reconfigure()
                return await self.async_step_reconfigure_wake()

        schema = probatio.Schema(
            {
                probatio.Optional(CONF_PERSON): EntitySelector(
                    EntitySelectorConfig(domain="person")
                ),
                probatio.Optional(CONF_NAME): TextSelector(),
                **_profile_fields(),
            }
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                schema, self._input or self._reconfigure_form_values(subentry)
            ),
            errors=errors,
        )

    async def async_step_reconfigure_wake(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Wait for the wake, then write the new profile to the scale."""
        if self._reconfigure_task is None:
            self._reconfigure_task = self.hass.async_create_task(
                self._async_write_profile()
            )
        if not self._reconfigure_task.done():
            return self.async_show_progress(
                step_id="reconfigure_wake",
                progress_action="reconfigure",
                progress_task=self._reconfigure_task,
            )

        task, self._reconfigure_task = self._reconfigure_task, None
        try:
            task.result()
        except ConsentRejected:
            return self._progress_failed("consent_rejected")
        except (WakeTimeout, ScaleError) as err:
            return self._progress_failed(_failure_reason(err))
        return self.async_show_progress_done(next_step_id="reconfigure_finish")

    async def async_step_reconfigure_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Save the changes once the scale has the new profile."""
        return self._async_save_reconfigure()

    def _async_save_reconfigure(self) -> SubentryFlowResult:
        """Update the subentry; the reload renames the scale user's device."""
        assert self._new_profile is not None
        return self.async_update_and_abort(
            self._get_entry(),
            self._get_reconfigure_subentry(),
            title=self._name,
            data_updates={
                CONF_NAME: self._name,
                CONF_PERSON: self._person_id,
                CONF_PROFILE: profile_data(self._new_profile),
            },
        )

    def _reconfigure_form_values(self, subentry: ConfigSubentry) -> dict[str, Any]:
        """The form's starting values: what the subentry holds."""
        profile = subentry.data[CONF_PROFILE]
        values: dict[str, Any] = {
            CONF_NAME: subentry.data[CONF_NAME],
            PROFILE_BIRTH_DATE: profile[PROFILE_BIRTH_DATE],
            PROFILE_HEIGHT_CM: profile[PROFILE_HEIGHT_CM],
            PROFILE_GENDER: profile[PROFILE_GENDER],
            PROFILE_ACTIVITY_LEVEL: str(profile[PROFILE_ACTIVITY_LEVEL]),
        }
        person_id = subentry.data.get(CONF_PERSON)
        if person_id and (person := er.async_get(self.hass).async_get(person_id)):
            values[CONF_PERSON] = person.entity_id
        # A profile adopted from an unset scale user has no birth date.
        return {key: value for key, value in values.items() if value is not None}

    async def _async_write_profile(self) -> None:
        """Wait for the wake, consent as the scale user and write the profile."""
        assert self._new_profile is not None
        entry: BeurerConfigEntry = self._get_entry()
        subentry = self._get_reconfigure_subentry()
        slot = int(subentry.data[CONF_SLOT])
        address: str = entry.data[CONF_ADDRESS]
        await async_wait_for_wake(self.hass, address)
        async with async_open_session(self.hass, address, self._model()) as session:
            await session.set_time(dt_util.now())
            # Consent releases the slot's stored readings (§3).
            await session.subscribe_readings()
            await session.consent(slot, int(subentry.data[CONF_CONSENT_CODE]))
            # The scale user's entities exist: process them now, so a failed
            # write doesn't lose them.
            entry.runtime_data.async_process_readings(
                slot, await session.collect_readings()
            )
            await session.write_profile(self._new_profile)

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

    def _validate_identity(
        self, user_input: dict[str, Any], own: ConfigSubentry | None = None
    ) -> dict[str, str]:
        """Resolve the name and the person; return form errors.

        `own` is the scale user being reconfigured: its name is no conflict.
        """
        person = user_input.get(CONF_PERSON) or None
        name = (user_input.get(CONF_NAME) or "").strip()
        if not name and person and (state := self.hass.states.get(person)):
            name = state.name
        if not name:
            return {"base": "name_required"}

        others = [
            s
            for s in self._get_entry().subentries.values()
            if own is None or s.subentry_id != own.subentry_id
        ]
        if any(s.title.casefold() == name.casefold() for s in others):
            return {CONF_NAME: "name_in_use"}

        self._name = name
        registry_entry = er.async_get(self.hass).async_get(person) if person else None
        self._person_id = registry_entry.id if registry_entry else None
        return {}

    def _validate_slot(self, user_input: dict[str, Any]) -> dict[str, str]:
        """Check that the slot isn't a scale user of this scale yet."""
        slot = int(user_input[CONF_SLOT])
        subentries = self._get_entry().subentries.values()
        if any(s.unique_id == slot_unique_id(slot) for s in subentries):
            return {CONF_SLOT: "slot_in_use"}
        return {}

    def _validate_profile(self, user_input: dict[str, Any]) -> dict[str, str]:
        """Check the entered profile and keep it; return form errors."""
        try:
            birth_date = date.fromisoformat(user_input[PROFILE_BIRTH_DATE])
        except ValueError:
            return {PROFILE_BIRTH_DATE: "invalid_birth_date"}
        today = dt_util.now().date()
        age = today.year - birth_date.year
        if (today.month, today.day) < (birth_date.month, birth_date.day):
            age -= 1
        if not MIN_AGE <= age <= MAX_AGE:
            return {PROFILE_BIRTH_DATE: "invalid_birth_date"}

        self._new_profile = Profile(
            birth_date=birth_date,
            height_cm=int(user_input[PROFILE_HEIGHT_CM]),
            gender=Gender(user_input[PROFILE_GENDER]),
            activity_level=int(user_input[PROFILE_ACTIVITY_LEVEL]),
        )
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
            # Consent releases the slot's stored readings (§3).
            await session.subscribe_readings()
            await session.consent(slot, int(self._input[CONF_CONSENT_CODE]))
            self._readings = await session.collect_readings()
        return profile

    async def _async_create(self) -> CreatedScaleUser:
        """Wait for the wake, create the scale user, write its profile and teach.

        On any failure or cancellation before teaching ended, `create_scale_user`
        deletes the scale user on the scale again before the session disconnects.
        """
        assert self._new_profile is not None
        address: str = self._get_entry().data[CONF_ADDRESS]
        await async_wait_for_wake(self.hass, address)
        self._sync_start = dt_util.now()
        async with async_open_session(
            self.hass, address, self._model(), session_timeout=_create_session_timeout()
        ) as session:
            await session.set_time(dt_util.now())
            async with create_scale_user(session, self._new_profile) as created:
                self._created = created
                self._created_event.set()
                await session.subscribe_readings()
                await self._teaching.run(session)
        return created

    def _progress_until(
        self,
        step_id: str,
        progress_action: str,
        milestone: Callable[[], Awaitable[Any]],
        description_placeholders: Mapping[str, str] | None = None,
    ) -> SubentryFlowResult | None:
        """Show a progress step until `milestone` or the create task is done.

        Returns None once it is done; the step then decides where to go.
        """
        if self._milestone is None:
            self._milestone = self.hass.async_create_background_task(
                self._async_until_either(milestone()), f"beurer_scales {step_id}"
            )
        elif self._milestone.done():
            self._milestone = None
            return None
        return self.async_show_progress(
            step_id=step_id,
            progress_action=progress_action,
            progress_task=self._milestone,
            description_placeholders=description_placeholders,
        )

    async def _async_until_either(self, milestone: Awaitable[Any]) -> None:
        """Return at `milestone` or when the create task ends, whichever is first."""
        assert self._create_task is not None
        waiting = asyncio.ensure_future(milestone)
        try:
            await asyncio.wait(
                {waiting, self._create_task}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            waiting.cancel()

    async def _async_until_reading(self, count: int) -> None:
        """Return once teaching took `count` readings, except for the last one.

        Teaching ends right after the last reading; the step waits for the create
        task to disconnect instead.
        """
        if count >= TEACHING_READINGS:
            await asyncio.get_running_loop().create_future()
        await self._teaching.wait_for_readings(count)

    def _create_failure(self) -> str | None:
        """The abort reason of a failed create task; None if it succeeded."""
        assert self._create_task is not None
        if self._create_task.cancelled():
            # The scale's entry was unloaded meanwhile.
            return "cannot_connect"
        try:
            self._create_task.result()
        except TeachingTimeout:
            return "teaching_timeout"
        except NoFreeSlot:
            return "no_free_slot"
        except (WakeTimeout, ScaleError) as err:
            return _failure_reason(err)
        return None

    def _new_unique_id(self) -> str:
        assert self._created is not None
        return slot_unique_id(self._created.slot)

    def _new_data(self) -> dict[str, Any]:
        """The subentry data of the created scale user."""
        assert self._created is not None
        assert self._new_profile is not None
        return {
            CONF_SLOT: self._created.slot,
            CONF_CONSENT_CODE: self._created.consent_code,
            CONF_NAME: self._name,
            CONF_PERSON: self._person_id,
            CONF_PROFILE: profile_data(self._new_profile),
        }

    def _keep_teaching_readings(self) -> None:
        """Hand the teaching readings to the sync pipeline, like fresh readings."""
        assert self._created is not None
        async_hand_over_readings(
            self.hass,
            self._get_entry().entry_id,
            self._created.slot,
            self._teaching.readings,
            self._sync_start,
        )


def _create_session_timeout() -> float:
    """The time budget of a create session.

    Teaching keeps the scale awake (V3), so the session may outlast the wake
    window; each teaching reading has its own timeout on top.
    """
    return (
        protocol_session.SESSION_TIMEOUT
        + TEACHING_READINGS * protocol_teaching.TEACHING_TIMEOUT
    )


def _profile_fields() -> dict[probatio.Marker, Any]:
    """The profile fields of the create and reconfigure forms."""
    return {
        probatio.Required(PROFILE_BIRTH_DATE): DateSelector(),
        probatio.Required(PROFILE_HEIGHT_CM): NumberSelector(
            NumberSelectorConfig(
                min=MIN_HEIGHT_CM,
                max=MAX_HEIGHT_CM,
                step=1,
                unit_of_measurement="cm",
                mode=NumberSelectorMode.BOX,
            )
        ),
        probatio.Required(PROFILE_GENDER): SelectSelector(
            SelectSelectorConfig(
                options=[str(g) for g in Gender],
                translation_key=PROFILE_GENDER,
                mode=SelectSelectorMode.LIST,
            )
        ),
        probatio.Required(PROFILE_ACTIVITY_LEVEL): SelectSelector(
            SelectSelectorConfig(
                options=[str(level) for level in range(1, 6)],
                translation_key=PROFILE_ACTIVITY_LEVEL,
                mode=SelectSelectorMode.DROPDOWN,
            )
        ),
    }


def _failure_reason(err: Exception) -> str:
    """The abort reason for a failure that ends the flow."""
    if isinstance(err, WakeTimeout):
        return "wake_timeout"
    if isinstance(err, PairingUnsupported | PairingFailed):
        return "pairing_unsupported"
    return "cannot_connect"
