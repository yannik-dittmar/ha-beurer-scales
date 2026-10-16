"""Tests for adding a scale."""

import pytest
from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_USER
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import SCALE_ADDRESS, inject_advertisement, scale_advertisement

DOMAIN = "beurer_scales"


async def test_bf450_advertisement_starts_discovery(hass: HomeAssistant) -> None:
    """A waking BF450 is offered as a discovered device."""
    await inject_advertisement(hass, scale_advertisement())

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == SOURCE_BLUETOOTH
    assert flows[0]["context"]["unique_id"] == SCALE_ADDRESS


async def test_confirming_discovery_adds_the_scale(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry
) -> None:
    """Confirming a discovered BF450 creates the scale entry and the scale device."""
    await inject_advertisement(hass, scale_advertisement())
    [flow] = hass.config_entries.flow.async_progress_by_handler(DOMAIN)

    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Beurer BF450"
    assert result["data"][CONF_ADDRESS] == SCALE_ADDRESS
    entry = result["result"]
    assert entry.unique_id == SCALE_ADDRESS
    [device] = device_registry.async_get_devices(
        connections={(dr.CONNECTION_BLUETOOTH, SCALE_ADDRESS)}
    )
    assert device.config_entry_id == entry.entry_id
    assert device.manufacturer == "Beurer"
    assert device.model == "BF450"


async def test_discovering_a_configured_scale_again_aborts(hass: HomeAssistant) -> None:
    """A scale that is already set up is not offered a second time."""
    MockConfigEntry(
        domain=DOMAIN,
        unique_id=SCALE_ADDRESS,
        data={CONF_ADDRESS: SCALE_ADDRESS, "model": "BF450"},
    ).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=scale_advertisement()
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.parametrize("local_name", [None, "BF999", "Smart Scale"])
async def test_discovery_of_an_unknown_model_aborts(
    hass: HomeAssistant, local_name: str | None
) -> None:
    """An advertisement without a known Beurer model name is not set up."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_BLUETOOTH},
        data=scale_advertisement(local_name=local_name),
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_supported"


async def test_manual_add_lists_seen_scales(hass: HomeAssistant) -> None:
    """The manual add step offers scales HA has seen and creates the entry."""
    await inject_advertisement(hass, scale_advertisement())

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: SCALE_ADDRESS}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == SCALE_ADDRESS
    assert result["data"][CONF_ADDRESS] == SCALE_ADDRESS


async def test_manual_add_skips_configured_and_unknown_devices(
    hass: HomeAssistant,
) -> None:
    """Only unconfigured Beurer scales are offered; otherwise there is nothing to add."""
    MockConfigEntry(
        domain=DOMAIN,
        unique_id=SCALE_ADDRESS,
        data={CONF_ADDRESS: SCALE_ADDRESS, "model": "BF450"},
    ).add_to_hass(hass)
    await inject_advertisement(hass, scale_advertisement())
    await inject_advertisement(
        hass, scale_advertisement(address="AA:BB:CC:DD:EE:02", local_name="Smart Scale")
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"
