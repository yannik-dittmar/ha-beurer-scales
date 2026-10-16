"""The check after an unrecognised weigh-in (ADR 0008, design §5.3).

A wake that reaches ~25 s without a managed slot pending makes HA connect, read
the user list and disconnect: a factory reset or a deletion on the scale would
otherwise go unnoticed, because the scale never lists those slots again.
"""

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .fake_scale import FakeScale
from .test_scale_user_repairs import anna_id, scale_issues, setup_anna
from .conftest import stay_awake, wake_scale


async def test_a_factory_reset_is_found_after_an_unrecognised_weigh_in(
    hass: HomeAssistant,
    fake_scale: FakeScale,
    freezer: FrozenDateTimeFactory,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Nobody is pending; at ~25 s HA reads the empty user list and raises a repair."""
    entry = await setup_anna(hass)
    fake_scale.factory_reset()

    await wake_scale(hass, fake_scale)
    await stay_awake(hass, freezer, fake_scale, 26)

    assert f"factory_reset_{anna_id(entry)}" in scale_issues(issue_registry)
    assert fake_scale.connection_attempts == 1
    assert fake_scale.rebonds == 1
    assert not fake_scale.connected


async def test_the_check_waits_until_the_weigh_in_is_final(
    hass: HomeAssistant, fake_scale: FakeScale, freezer: FrozenDateTimeFactory
) -> None:
    """Connecting earlier would cut the weigh-in short (the scale sleeps ~1 s later)."""
    await setup_anna(hass)
    fake_scale.add_user(slot=2, consent_code=4321)

    await wake_scale(hass, fake_scale)
    await stay_awake(hass, freezer, fake_scale, 22)
    assert fake_scale.connection_attempts == 0

    await stay_awake(hass, freezer, fake_scale, 4)
    assert fake_scale.connection_attempts == 1
    assert not fake_scale.connected


async def test_a_deleted_scale_user_is_found_after_an_unrecognised_weigh_in(
    hass: HomeAssistant,
    fake_scale: FakeScale,
    freezer: FrozenDateTimeFactory,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Others remain on the scale, Anna's slot is gone: the same repair as at consent."""
    fake_scale.add_user(slot=1, consent_code=1111)
    entry = await setup_anna(hass)

    await wake_scale(hass, fake_scale)
    await stay_awake(hass, freezer, fake_scale, 26)

    assert set(scale_issues(issue_registry)) == {f"scale_user_deleted_{anna_id(entry)}"}
    assert fake_scale.rebonds == 0
