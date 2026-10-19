"""Linking a scale user to Garmin Connect: the subentry flow (ADR 0011)."""

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.beurer_scales.const import (
    CONF_SCALE_USER,
    SUBENTRY_GARMIN,
)
from custom_components.beurer_scales.garmin import (
    GarminAuthError,
    GarminRateLimited,
    PendingLogin,
)

from .test_garmin_push import ANNA_ID, STORE_KEY, scale_with_link
from .test_sync import anna_scale

LOGIN = "custom_components.beurer_scales.garmin_flow.login"
FINISH_MFA = "custom_components.beurer_scales.garmin_flow.finish_mfa"
CREDENTIALS = {"email": "anna@example.com", "password": "hunter2"}


@pytest.fixture
async def scale(hass: HomeAssistant) -> MockConfigEntry:
    """A loaded scale with Anna and no Garmin link."""
    entry = anna_scale()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def scale_user_id(entry: MockConfigEntry) -> str:
    [subentry] = entry.subentries.values()
    return subentry.subentry_id


async def start(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_GARMIN), context={"source": SOURCE_USER}
    )


async def submit(
    hass: HomeAssistant, flow_id: str, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Submit a form and let a progress step finish."""
    result = await hass.config_entries.subentries.async_configure(flow_id, user_input)
    return await settle(hass, result)


async def settle(hass: HomeAssistant, result: dict[str, Any]) -> dict[str, Any]:
    while result["type"] is FlowResultType.SHOW_PROGRESS:
        await hass.async_block_till_done()
        result = await hass.config_entries.subentries.async_configure(result["flow_id"])
    return result


async def test_a_scale_user_is_linked_with_the_garmin_login(
    hass: HomeAssistant, scale: MockConfigEntry, hass_storage: dict[str, Any]
) -> None:
    """Email and password create the link; only the tokens are kept."""
    user_id = scale_user_id(scale)
    result = await start(hass, scale)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    with patch(LOGIN, return_value=(None, "the-tokens")) as login:
        result = await submit(
            hass, result["flow_id"], {CONF_SCALE_USER: user_id, **CREDENTIALS}
        )

    login.assert_called_once_with("anna@example.com", "hunter2")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_SCALE_USER: user_id}
    assert scale.runtime_data.garmin.has_link(user_id)
    await hass.async_block_till_done()
    stored = hass_storage[f"beurer_scales.{scale.entry_id}.garmin"]["data"]
    assert stored[user_id]["tokens"] == "the-tokens"
    # Neither the password nor the email is kept anywhere.
    assert "hunter2" not in str(hass_storage)
    assert "hunter2" not in str(scale.as_dict())


async def test_garmin_asks_for_a_code_and_the_link_is_made_with_it(
    hass: HomeAssistant, scale: MockConfigEntry
) -> None:
    """With MFA the flow asks for the code and finishes the login with it."""
    user_id = scale_user_id(scale)
    result = await start(hass, scale)
    pending = PendingLogin(client=object())

    with patch(LOGIN, return_value=(pending, None)):
        result = await submit(
            hass, result["flow_id"], {CONF_SCALE_USER: user_id, **CREDENTIALS}
        )
    assert result["step_id"] == "mfa"

    with patch(FINISH_MFA, return_value="mfa-tokens") as finish:
        result = await submit(hass, result["flow_id"], {"mfa_code": " 123456 "})

    finish.assert_called_once_with(pending, "123456")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert scale.runtime_data.garmin.has_link(user_id)


async def test_a_wrong_code_can_be_corrected(
    hass: HomeAssistant, scale: MockConfigEntry
) -> None:
    """The login stays pending after a rejected code."""
    user_id = scale_user_id(scale)
    result = await start(hass, scale)
    pending = PendingLogin(client=object())
    with patch(LOGIN, return_value=(pending, None)):
        result = await submit(
            hass, result["flow_id"], {CONF_SCALE_USER: user_id, **CREDENTIALS}
        )

    with patch(FINISH_MFA, side_effect=GarminAuthError("bad code")):
        result = await submit(hass, result["flow_id"], {"mfa_code": "000000"})
    assert result["step_id"] == "mfa"
    assert result["errors"] == {"base": "invalid_mfa"}

    with patch(FINISH_MFA, return_value="tokens"):
        result = await submit(hass, result["flow_id"], {"mfa_code": "123456"})
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (GarminAuthError("no"), "invalid_auth"),
        (GarminRateLimited("429"), "rate_limited"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_a_failed_login_shows_why(
    hass: HomeAssistant, scale: MockConfigEntry, error: Exception, key: str
) -> None:
    """The form returns with the reason; nothing is created."""
    user_id = scale_user_id(scale)
    result = await start(hass, scale)

    with patch(LOGIN, side_effect=error):
        result = await submit(
            hass, result["flow_id"], {CONF_SCALE_USER: user_id, **CREDENTIALS}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": key}
    assert not scale.runtime_data.garmin.has_link(user_id)


async def test_a_scale_user_with_a_link_is_not_offered_again(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """One link per scale user: with every one linked, the flow aborts."""
    hass_storage[STORE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORE_KEY,
        "data": {ANNA_ID: {"tokens": "t"}},
    }
    entry = scale_with_link()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await start(hass, entry)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_scale_user"


async def test_reconfiguring_logs_in_again_and_resumes_pushing(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """A new login replaces the tokens of the link."""
    hass_storage[STORE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORE_KEY,
        "data": {ANNA_ID: {"tokens": "old", "paused": True}},
    }
    entry = scale_with_link()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.garmin.is_paused(ANNA_ID)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_GARMIN),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": "garmin-link"},
    )
    assert result["step_id"] == "reconfigure"
    with patch(LOGIN, return_value=(None, "new")):
        result = await submit(hass, result["flow_id"], CREDENTIALS)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert not entry.runtime_data.garmin.is_paused(ANNA_ID)
    await hass.async_block_till_done()
    assert hass_storage[STORE_KEY]["data"][ANNA_ID]["tokens"] == "new"
