"""The "Add scale user" subentry flow: create a new scale user or adopt a slot."""

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from datetime import date, datetime
from types import MappingProxyType
from typing import Any

import probatio
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigSubentry,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
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

from .connection import (
    WakeTimeout,
    async_hold_scale,
    async_open_session,
    async_wait_for_wake,
)
from .const import (
    CONF_CONSENT_CODE,
    CONF_MODEL,
    CONF_NAME,
    CONF_PERSON,
    CONF_PROFILE,
    CONF_SLOT,
    DOMAIN,
    PROFILE_ACTIVITY_LEVEL,
    PROFILE_BIRTH_DATE,
    PROFILE_GENDER,
    PROFILE_HEIGHT_CM,
    SOURCE_REPAIR_CREATE,
    SUBENTRY_SCALE_USER,
    scale_user_id,
    slot_unique_id,
)
from .coordinator import BeurerConfigEntry, async_hand_over_readings
from .protocol import session as protocol_session
from .protocol import teaching as protocol_teaching
from .pending_deletions import PendingDeletion
from .protocol.errors import (
    ConnectionLost,
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
from .protocol.teaching import Teaching
from .repairs import async_clear_scale_user_issues

# Beurer's supported ranges for body-composition scales.
MIN_HEIGHT_CM = 100
MAX_HEIGHT_CM = 220
MIN_AGE = 10
MAX_AGE = 100


class SlotNotFound(ScaleError):
    """The scale's user list has no scale user in the slot."""


class SlotHeldByOtherScaleUser(ScaleError):
    """The scale put a new scale user into a slot another scale user holds in HA."""


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


def profile_from_data(data: Mapping[str, Any]) -> Profile:
    """Return the profile of a form's input or a subentry's stored profile."""
    return Profile(
        birth_date=date.fromisoformat(data[PROFILE_BIRTH_DATE]),
        height_cm=int(data[PROFILE_HEIGHT_CM]),
        gender=Gender(data[PROFILE_GENDER]),
        activity_level=int(data[PROFILE_ACTIVITY_LEVEL]),
    )


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
        # The adopt, repair adopt or reconfigure task the wake step waits for.
        self._task: asyncio.Task[Any] | None = None
        self._profile: UserListEntry | None = None
        # The create path.
        self._new_profile: Profile | None = None
        # Holds the connection from the wake through teaching.
        self._create_task: asyncio.Task[CreatedScaleUser] | None = None
        self._created: CreatedScaleUser | None = None
        self._connected_event = asyncio.Event()
        self._created_event = asyncio.Event()
        self._teaching = Teaching()
        # When the create task connected, for the backlog rule.
        self._sync_start: datetime | None = None
        # What the progress step shown right now waits for.
        self._milestone: asyncio.Task[Any] | None = None
        self._entry_created = False
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

        schema = probatio.Schema({**_identity_fields(), **_profile_fields()})
        return self.async_show_form(
            step_id="create",
            data_schema=self.add_suggested_values_to_schema(schema, self._input),
            errors=errors,
        )

    async def async_step_create_wake(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Progress 1: wait for the step-on that wakes the scale; connect."""
        if self._create_task is None:
            # A background task: it outlives the progress steps.
            self._create_task = self._get_entry().async_create_background_task(
                self.hass, self._async_create(), "beurer_scales create scale user"
            )
        if result := self._progress_until(
            "create_wake",
            "create_wake",
            lambda: self._until_create_ends(self._connected_event.wait(), "connect"),
        ):
            return result
        return self._after_stage("create_user")

    async def async_step_create_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Progress 2: the scale user is being created on the scale."""
        if result := self._progress_until(
            "create_user",
            "create_user",
            lambda: self._until_create_ends(self._created_event.wait(), "create"),
        ):
            return result
        return self._after_stage("create_measure")

    async def async_step_create_measure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Progress 3: the scale measures; its reading teaches it the scale user."""
        if result := self._progress_until(
            "create_measure",
            "create_measure",
            lambda: self._until_create_ends(None, "measure"),
        ):
            return result
        return self._after_stage("create_finish")

    def _after_stage(self, next_step_id: str) -> SubentryFlowResult:
        """Go on to the next stage, or end the flow if the create task failed."""
        assert self._create_task is not None
        if self._create_task.done() and (reason := self._create_failure()):
            return self._progress_failed(reason)
        return self.async_show_progress_done(next_step_id=next_step_id)

    async def async_step_create_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Create the subentry of the new scale user, naming the measured weight."""
        placeholders = {"weight": self._measured_weight()}
        if self.source == SOURCE_REPAIR_CREATE:
            self._async_update_recreated_subentry()
            return self.async_abort(
                reason="scale_user_recreated", description_placeholders=placeholders
            )
        self._keep_teaching_readings()
        self._entry_created = True
        return self.async_create_entry(
            title=self._name,
            unique_id=self._new_unique_id(),
            data=self._new_data(),
            description="measured",
            description_placeholders=placeholders,
        )

    def _measured_weight(self) -> str:
        """The teaching reading's weight, for the confirmation."""
        reading = self._teaching.reading
        return "the weight" if reading is None else f"{reading.weight_kg:.1f} kg"

    @callback
    def async_remove(self) -> None:
        """Clean up after the flow closed, finished or not.

        Before the teaching reading, the scale user is deleted on the scale again.
        One that got its reading stays on the scale, so it gets its subentry even
        when the flow is closed without finishing.
        """
        if self._milestone is not None:
            self._milestone.cancel()
        task = self._create_task
        if task is None or self._entry_created:
            return
        if not task.done():
            task.cancel()
            return
        if self._create_failure() is None and self.source == SOURCE_REPAIR_CREATE:
            self._async_update_recreated_subentry()
        elif self._create_failure() is None:
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
                **_identity_fields(),
                probatio.Required(CONF_SLOT): NumberSelector(
                    NumberSelectorConfig(
                        min=1,
                        max=self._model().slot_count,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                **_consent_code_field(),
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
        if result := self._progress_until_task("wake", "wake", self._async_adopt):
            return result
        try:
            self._profile = self._finished_task().result()
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
            data=self._subentry_data(
                self._profile.slot,
                int(self._input[CONF_CONSENT_CODE]),
                self._profile,
            ),
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

        schema = probatio.Schema({**_identity_fields(), **_profile_fields()})
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
        if result := self._progress_until_task(
            "reconfigure_wake", "reconfigure", self._async_write_profile
        ):
            return result
        try:
            self._finished_task().result()
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
            data_updates=self._identity_data(self._new_profile),
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
        async with async_hold_scale(self.hass, address):
            await async_wait_for_wake(self.hass, address)
            async with async_open_session(self.hass, address, self._model()) as session:
                # Consent releases the slot's stored readings (§3).
                result = await session.sync(
                    slot, int(subentry.data[CONF_CONSENT_CODE]), dt_util.now()
                )
                # The scale user's entities exist: process them now, so a failed
                # write doesn't lose them.
                entry.runtime_data.async_process_readings(slot, result.readings)
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

        self._new_profile = profile_from_data(user_input)
        return {}

    async def _async_adopt(self) -> UserListEntry:
        """Wait for the wake, then check that the slot exists and the code works."""
        address: str = self._get_entry().data[CONF_ADDRESS]
        slot = int(self._input[CONF_SLOT])
        async with async_hold_scale(self.hass, address):
            await async_wait_for_wake(self.hass, address)
            async with async_open_session(self.hass, address, self._model()) as session:
                # The scale answers a wrong code and a missing slot alike (§9.4b);
                # the user list, which needs no consent, tells them apart.
                users = await session.read_user_list()
                profile = next((u for u in users if u.slot == slot), None)
                if profile is None:
                    raise SlotNotFound(f"slot {slot}")
                # Consent releases the slot's stored readings (§3).
                result = await session.sync(
                    slot, int(self._input[CONF_CONSENT_CODE]), dt_util.now()
                )
                self._readings = result.readings
        return profile

    async def _async_create(self) -> CreatedScaleUser:
        """Wait for the wake, create the scale user, write its profile and teach.

        On any failure or cancellation before teaching ended, `create_scale_user`
        deletes the scale user on the scale again before the session disconnects.
        """
        assert self._new_profile is not None
        address: str = self._get_entry().data[CONF_ADDRESS]
        async with async_hold_scale(self.hass, address):
            await async_wait_for_wake(self.hass, address)
            self._sync_start = dt_util.now()
            async with async_open_session(
                self.hass,
                address,
                self._model(),
                session_timeout=_create_session_timeout(),
            ) as session:
                self._connected_event.set()
                await session.set_time(dt_util.now())
                async with create_scale_user(session, self._new_profile) as created:
                    self._check_slot_free(created.slot)
                    self._created = created
                    self._created_event.set()
                    await session.subscribe_readings()
                    try:
                        await self._teaching.run(session)
                    except ConnectionLost:
                        # Deleting it needs a link: do it at the next wake.
                        await (
                            self._get_entry().runtime_data.pending_deletions.async_add(
                                PendingDeletion(created.slot, created.consent_code)
                            )
                        )
                        raise
        return created

    def _progress_until(
        self,
        step_id: str,
        progress_action: str,
        milestone: Callable[[], asyncio.Task[Any]],
        description_placeholders: Mapping[str, str] | None = None,
    ) -> SubentryFlowResult | None:
        """Show a progress step until the task `milestone` starts is done.

        Returns None once it is done; the step then decides where to go.
        """
        if self._milestone is None:
            self._milestone = milestone()
        elif self._milestone.done():
            self._milestone = None
            return None
        return self.async_show_progress(
            step_id=step_id,
            progress_action=progress_action,
            progress_task=self._milestone,
            description_placeholders=description_placeholders,
        )

    def _progress_until_task(
        self,
        step_id: str,
        progress_action: str,
        work: Callable[[], Coroutine[Any, Any, Any]],
    ) -> SubentryFlowResult | None:
        """Start `work` once and show a progress step until it is done.

        Returns None once it is done; the step then takes `_finished_task`.
        """
        if self._task is None:
            self._task = self.hass.async_create_task(work())
        task = self._task
        return self._progress_until(step_id, progress_action, lambda: task)

    def _finished_task(self) -> asyncio.Task[Any]:
        """The task `_progress_until_task` waited for; the next wake starts anew."""
        task, self._task = self._task, None
        assert task is not None
        assert task.done()
        return task

    def _until_create_ends(
        self, milestone: Awaitable[Any] | None, name: str
    ) -> asyncio.Task[None]:
        """A task that ends at `milestone` or with the create task, if earlier.

        Without a milestone it ends with the create task. A background task:
        teaching holds the create task open, and tests that wait for HA's tasks
        must not wait for teaching.
        """
        return self.hass.async_create_background_task(
            self._async_until_either(milestone), f"beurer_scales {name}"
        )

    async def _async_until_either(self, milestone: Awaitable[Any] | None) -> None:
        """Return at `milestone` or when the create task ends, whichever is first."""
        assert self._create_task is not None
        waiting = {self._create_task}
        if milestone is not None:
            waiting.add(asyncio.ensure_future(milestone))
        try:
            await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for future in waiting:
                if future is not self._create_task:
                    future.cancel()

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
        except ConnectionLost as err:
            if self._created is not None:
                return "teaching_connection_lost"
            return _failure_reason(err)
        except NoFreeSlot:
            return "no_free_slot"
        except SlotHeldByOtherScaleUser:
            return "slot_held_by_other_scale_user"
        except (WakeTimeout, ScaleError) as err:
            return _failure_reason(err)
        return None

    def _check_slot_free(self, slot: int) -> None:
        """Raise if another scale user of this scale holds `slot` in HA.

        The scale picks the slot. A scale user that was deleted on the scale (or
        a factory reset) frees its slot on the scale while its subentry remains.
        """
        own = self.context.get("subentry_id")
        if any(
            s.unique_id == slot_unique_id(slot) and s.subentry_id != own
            for s in self._get_entry().subentries.values()
        ):
            raise SlotHeldByOtherScaleUser(f"slot {slot}")

    def _new_unique_id(self) -> str:
        assert self._created is not None
        return slot_unique_id(self._created.slot)

    def _new_data(self) -> dict[str, Any]:
        """The subentry data of the created scale user."""
        assert self._created is not None
        assert self._new_profile is not None
        return self._subentry_data(
            self._created.slot, self._created.consent_code, self._new_profile
        )

    def _subentry_data(
        self, slot: int, consent_code: int, profile: UserListEntry | Profile
    ) -> dict[str, Any]:
        """The data of a new scale user subentry."""
        return {
            CONF_SLOT: slot,
            CONF_CONSENT_CODE: consent_code,
            **self._identity_data(profile),
        }

    def _identity_data(self, profile: UserListEntry | Profile) -> dict[str, Any]:
        """Who the scale user is: the subentry data the forms change."""
        return {
            CONF_NAME: self._name,
            CONF_PERSON: self._person_id,
            CONF_PROFILE: profile_data(profile),
        }

    def _keep_teaching_readings(self) -> None:
        """Hand the teaching readings to the sync pipeline, like fresh readings."""
        assert self._created is not None
        async_hand_over_readings(
            self.hass,
            self._get_entry().entry_id,
            self._created.slot,
            [] if self._teaching.reading is None else [self._teaching.reading],
            self._sync_start,
        )

    # --- Repair fix flows (#12): enter the code again, or create again ---
    # Started by the repair issue's fix flow with context["subentry_id"]; they
    # update that scale user's subentry and close its repair issue.

    async def async_step_repair_adopt(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Ask for the scale user's consent code again."""
        subentry = self._repair_subentry()
        errors, self._errors = self._errors, {}
        if user_input is not None:
            self._input = {
                CONF_SLOT: subentry.data[CONF_SLOT],
                CONF_CONSENT_CODE: user_input[CONF_CONSENT_CODE],
            }
            return await self.async_step_repair_adopt_wake()

        schema = probatio.Schema(_consent_code_field())
        return self.async_show_form(
            step_id="repair_adopt",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "name": subentry.title,
                "slot": str(subentry.data[CONF_SLOT]),
            },
        )

    async def async_step_repair_adopt_wake(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Wait for the wake, then verify the code like adopt does."""
        if result := self._progress_until_task(
            "repair_adopt_wake", "wake", self._async_adopt
        ):
            return result
        try:
            self._profile = self._finished_task().result()
        except SlotNotFound:
            return self._progress_failed("slot_now_empty")
        except ConsentRejected:
            self._errors = {CONF_CONSENT_CODE: "invalid_consent_code"}
            return self.async_show_progress_done(next_step_id="repair_adopt")
        except (WakeTimeout, ScaleError) as err:
            return self._progress_failed(_failure_reason(err))
        return self.async_show_progress_done(next_step_id="repair_adopt_finish")

    async def async_step_repair_adopt_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Store the working code and the scale's profile; close the repair."""
        assert self._profile is not None
        entry, subentry = self._get_entry(), self._repair_subentry()
        async_hand_over_readings(
            self.hass, entry.entry_id, self._profile.slot, self._readings
        )
        async_clear_scale_user_issues(self.hass, subentry.subentry_id)
        return self.async_update_and_abort(
            entry,
            subentry,
            data_updates={
                CONF_CONSENT_CODE: int(self._input[CONF_CONSENT_CODE]),
                CONF_PROFILE: profile_data(self._profile),
            },
            reason="scale_user_repaired",
        )

    async def async_step_repair_create(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Confirm creating the scale user again with its stored profile."""
        subentry = self._repair_subentry()
        stored = subentry.data[CONF_PROFILE]
        if not stored.get(PROFILE_BIRTH_DATE) or not stored.get(PROFILE_HEIGHT_CM):
            return self.async_abort(reason="profile_unknown")
        if user_input is not None:
            self._new_profile = profile_from_data(stored)
            self._name = subentry.title
            self._person_id = subentry.data.get(CONF_PERSON)
            return await self.async_step_create_wake()
        return self.async_show_form(
            step_id="repair_create",
            data_schema=probatio.Schema({}),
            description_placeholders={"name": subentry.title},
        )

    @callback
    def _async_update_recreated_subentry(self) -> ConfigSubentry:
        """Move the recreated scale user to its new slot and code; close the repair."""
        assert self._created is not None
        entry, subentry = self._get_entry(), self._repair_subentry()
        self._keep_teaching_readings()
        self._entry_created = True
        async_move_scale_user(self.hass, entry, subentry, self._created.slot)
        async_clear_scale_user_issues(self.hass, subentry.subentry_id)
        self.hass.config_entries.async_update_subentry(
            entry,
            subentry,
            unique_id=self._new_unique_id(),
            data=subentry.data
            | {
                CONF_SLOT: self._created.slot,
                CONF_CONSENT_CODE: self._created.consent_code,
            },
        )
        return subentry

    def _repair_subentry(self) -> ConfigSubentry:
        return self._get_entry().subentries[self.context["subentry_id"]]


@callback
def async_move_scale_user(
    hass: HomeAssistant, entry: ConfigEntry, subentry: ConfigSubentry, slot: int
) -> None:
    """Move a scale user's device and entities to a new slot, keeping their history.

    Device identifiers and entity unique ids carry the slot (`scale_user_id`).
    """
    address: str = entry.data[CONF_ADDRESS]
    old = scale_user_id(address, subentry.data[CONF_SLOT])
    new = scale_user_id(address, slot)
    if old == new:
        return
    device_registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
        if (DOMAIN, old) in device.identifiers:
            device_registry.async_update_device(
                device.id, new_identifiers={(DOMAIN, new)}
            )
    entity_registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        if entity.config_subentry_id == subentry.subentry_id and (
            entity.unique_id.startswith(f"{old}_")
        ):
            entity_registry.async_update_entity(
                entity.entity_id,
                new_unique_id=new + entity.unique_id.removeprefix(old),
            )


def _create_session_timeout() -> float:
    """The time budget of a create session.

    Teaching keeps the scale awake (V3), so the session may outlast the wake
    window; the teaching reading has its own timeout on top.
    """
    return protocol_session.SESSION_TIMEOUT + protocol_teaching.TEACHING_TIMEOUT


def _identity_fields() -> dict[probatio.Marker, Any]:
    """The person and name fields of the create, adopt and reconfigure forms."""
    return {
        probatio.Optional(CONF_PERSON): EntitySelector(
            EntitySelectorConfig(domain="person")
        ),
        probatio.Optional(CONF_NAME): TextSelector(),
    }


def _consent_code_field() -> dict[probatio.Marker, Any]:
    """The consent code field of the adopt and repair adopt forms."""
    return {
        probatio.Required(CONF_CONSENT_CODE): NumberSelector(
            NumberSelectorConfig(min=0, max=9999, step=1, mode=NumberSelectorMode.BOX)
        ),
    }


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
