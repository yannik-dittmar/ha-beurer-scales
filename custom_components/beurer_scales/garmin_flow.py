"""The "Garmin Connect" subentry flow: send a scale user's readings to Garmin.

One link per scale user. Setup asks for the Garmin email and password, and for
the MFA code when Garmin wants one; Home Assistant keeps only the tokens of that
login (ADR 0011). Reconfiguring a link logs in again, which is how a rejected
login is fixed. Garmin's login is slow on purpose, so it runs as a progress step.
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import probatio
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import CONF_SCALE_USER, SUBENTRY_GARMIN, SUBENTRY_SCALE_USER
from .coordinator import BeurerConfigEntry
from .garmin import (
    GarminAuthError,
    GarminError,
    GarminRateLimited,
    PendingLogin,
    finish_mfa,
    login,
)

_LOGGER = logging.getLogger(__name__)

CONF_MFA_CODE = "mfa_code"


class GarminSubentryFlow(ConfigSubentryFlow):
    """Link a scale user to a Garmin Connect account."""

    def __init__(self) -> None:
        """Initialize the flow."""
        self._scale_user_id: str | None = None
        self._credentials: tuple[str, str] | None = None
        self._pending: PendingLogin | None = None
        self._tokens: str | None = None
        self._task: asyncio.Task[Any] | None = None
        self._errors: dict[str, str] = {}

    # --- Add ---

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Choose the scale user and enter the Garmin login."""
        options = self._unlinked_scale_users()
        if not options:
            return self.async_abort(reason="no_scale_user")
        # After a failed login HA hands the form step the input of the call that
        # ended the login: show the error instead of submitting it again.
        if user_input is not None and not self._errors:
            self._scale_user_id = user_input[CONF_SCALE_USER]
            self._credentials = (user_input[CONF_EMAIL], user_input[CONF_PASSWORD])
            return await self.async_step_login()
        return self.async_show_form(
            step_id="user",
            data_schema=probatio.Schema(
                {
                    probatio.Required(CONF_SCALE_USER): SelectSelector(
                        SelectSelectorConfig(
                            options=options, mode=SelectSelectorMode.DROPDOWN
                        )
                    ),
                    **_login_fields(),
                }
            ),
            errors=self._pop_errors(),
        )

    def _unlinked_scale_users(self) -> list[SelectOptionDict]:
        entry = self._get_entry()
        linked = {
            subentry.data[CONF_SCALE_USER]
            for subentry in entry.subentries.values()
            if subentry.subentry_type == SUBENTRY_GARMIN
        }
        return [
            SelectOptionDict(value=subentry.subentry_id, label=subentry.title)
            for subentry in entry.subentries.values()
            if subentry.subentry_type == SUBENTRY_SCALE_USER
            and subentry.subentry_id not in linked
        ]

    # --- Reconfigure: log in again ---

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Log in to Garmin again, e.g. after Garmin rejected the stored login."""
        subentry = self._get_reconfigure_subentry()
        self._scale_user_id = subentry.data[CONF_SCALE_USER]
        if user_input is not None and not self._errors:
            self._credentials = (user_input[CONF_EMAIL], user_input[CONF_PASSWORD])
            return await self.async_step_login()
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=probatio.Schema(_login_fields()),
            errors=self._pop_errors(),
        )

    # --- The login, shared ---

    async def async_step_login(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Log in; Garmin's login takes a while."""
        assert self._credentials is not None
        email, password = self._credentials
        if self._task is None:
            self._task = self.hass.async_create_task(
                self._async_in_executor(login, email, password)
            )
        if not self._task.done():
            return self.async_show_progress(
                step_id="login", progress_action="login", progress_task=self._task
            )
        task, self._task = self._task, None
        # The password isn't needed any more, whatever came out.
        self._credentials = None
        try:
            self._pending, self._tokens = task.result()
        except GarminError as err:
            self._errors = {"base": _error_key(err)}
            return self.async_show_progress_done(next_step_id=self._form_step())
        except Exception:
            _LOGGER.exception("Logging in to Garmin Connect failed")
            self._errors = {"base": "unknown"}
            return self.async_show_progress_done(next_step_id=self._form_step())
        return self.async_show_progress_done(
            next_step_id="mfa" if self._pending else "finish"
        )

    async def _async_in_executor[*Args, T](
        self, func: Callable[[*Args], T], *args: *Args
    ) -> T:
        """Run the blocking Garmin call; a task needs a coroutine, not a future."""
        return await self.hass.async_add_executor_job(func, *args)

    def _form_step(self) -> str:
        return "reconfigure" if self.source == SOURCE_RECONFIGURE else "user"

    async def async_step_mfa(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Ask for the code Garmin sent."""
        if user_input is not None and CONF_MFA_CODE in user_input and not self._errors:
            return await self.async_step_verify(user_input)
        return self.async_show_form(
            step_id="mfa",
            data_schema=probatio.Schema(
                {
                    probatio.Required(CONF_MFA_CODE): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.TEXT)
                    )
                }
            ),
            errors=self._pop_errors(),
        )

    async def async_step_verify(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Check the code with Garmin."""
        assert self._pending is not None
        if self._task is None:
            assert user_input is not None
            code = str(user_input[CONF_MFA_CODE]).strip()
            self._task = self.hass.async_create_task(
                self._async_in_executor(finish_mfa, self._pending, code)
            )
        if not self._task.done():
            return self.async_show_progress(
                step_id="verify", progress_action="verify", progress_task=self._task
            )
        task, self._task = self._task, None
        try:
            self._tokens = task.result()
        except GarminAuthError:
            # The login stays pending: the code may be tried again.
            self._errors = {"base": "invalid_mfa"}
            return self.async_show_progress_done(next_step_id="mfa")
        except GarminError as err:
            self._pending = None
            self._errors = {"base": _error_key(err)}
            return self.async_show_progress_done(next_step_id=self._form_step())
        except Exception:
            _LOGGER.exception("Finishing the Garmin Connect login failed")
            self._pending = None
            self._errors = {"base": "unknown"}
            return self.async_show_progress_done(next_step_id=self._form_step())
        self._pending = None
        return self.async_show_progress_done(next_step_id="finish")

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Keep the tokens and create, or resume, the link."""
        assert self._scale_user_id is not None and self._tokens is not None
        entry: BeurerConfigEntry = self._get_entry()
        await entry.runtime_data.garmin.async_set_tokens(
            self._scale_user_id, self._tokens
        )
        self._tokens = None
        if self.source == SOURCE_RECONFIGURE:
            return self.async_abort(reason="reconfigure_successful")
        scale_user = entry.subentries[self._scale_user_id]
        return self.async_create_entry(
            title=scale_user.title,
            data={CONF_SCALE_USER: self._scale_user_id},
            unique_id=self._scale_user_id,
        )

    def _pop_errors(self) -> dict[str, str]:
        errors, self._errors = self._errors, {}
        return errors


def _login_fields() -> dict[probatio.Marker, Any]:
    return {
        probatio.Required(CONF_EMAIL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        probatio.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(
                type=TextSelectorType.PASSWORD, autocomplete="current-password"
            )
        ),
    }


def _error_key(err: GarminError) -> str:
    if isinstance(err, GarminAuthError):
        return "invalid_auth"
    if isinstance(err, GarminRateLimited):
        return "rate_limited"
    return "cannot_connect"
