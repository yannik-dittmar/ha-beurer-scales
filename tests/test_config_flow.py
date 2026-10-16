"""Tests for adding a scale."""

from unittest.mock import patch

import pytest
from homeassistant.components.bluetooth.match import ble_device_matches
from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_USER
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.loader import async_get_integration
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.beurer_scales.const import DOMAIN

from .conftest import (
    SCALE_ADDRESS,
    SWAPPED_WEIGHT_SCALE_UUID,
    WEIGHT_SCALE_UUID,
    inject_advertisement,
    scale_advertisement,
)


async def test_bf450_advertisement_starts_discovery(hass: HomeAssistant) -> None:
    """A waking BF450 is offered as a discovered scale."""
    await inject_advertisement(hass, scale_advertisement())

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == SOURCE_BLUETOOTH
    assert flows[0]["context"]["unique_id"] == SCALE_ADDRESS


@pytest.mark.parametrize("uuid", [WEIGHT_SCALE_UUID, SWAPPED_WEIGHT_SCALE_UUID])
async def test_weight_scale_service_data_in_either_byte_order_is_matched(
    hass: HomeAssistant, uuid: str
) -> None:
    """Discovery listens for 0x181D service data in both byte orders, even unnamed."""
    integration = await async_get_integration(hass, DOMAIN)
    advertisement = scale_advertisement(local_name=None, service_data={uuid: b""})

    assert integration.bluetooth
    assert any(ble_device_matches(m, advertisement) for m in integration.bluetooth)


async def test_unnamed_bf450_advertisement_is_offered_as_bf450(
    hass: HomeAssistant,
) -> None:
    """Without a local name, the byte-swapped pending-slot data identifies a BF450."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_BLUETOOTH},
        data=scale_advertisement(local_name=None),
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "bluetooth_confirm"
    assert result["description_placeholders"] == {"name": "Beurer BF450"}


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


async def test_adding_a_scale_never_connects_to_it(hass: HomeAssistant) -> None:
    """Discovery, confirmation and setup work while the scale is asleep."""
    with patch(
        "habluetooth.wrappers.HaBleakClientWrapper.connect",
        side_effect=AssertionError("connected to the scale"),
    ) as connect:
        await inject_advertisement(hass, scale_advertisement())
        [flow] = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        result = await hass.config_entries.flow.async_configure(flow["flow_id"], {})
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    connect.assert_not_called()


async def test_discovering_a_configured_scale_again_aborts(
    hass: HomeAssistant, configured_scale: MockConfigEntry
) -> None:
    """A scale that is already set up is not offered a second time."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=scale_advertisement()
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.parametrize(
    ("local_name", "service_data"),
    [
        (None, {WEIGHT_SCALE_UUID: b""}),
        ("Smart Scale", {WEIGHT_SCALE_UUID: b""}),
        ("BF999", {SWAPPED_WEIGHT_SCALE_UUID: b""}),
    ],
    ids=["unnamed-standard-weight-scale", "other-brand", "unknown-beurer-name"],
)
async def test_discovery_of_an_unknown_model_aborts(
    hass: HomeAssistant, local_name: str | None, service_data: dict[str, bytes]
) -> None:
    """An advertisement that doesn't identify a supported Beurer model is not set up."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_BLUETOOTH},
        data=scale_advertisement(local_name=local_name, service_data=service_data),
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


async def test_manual_add_skips_configured_scales_and_other_devices(
    hass: HomeAssistant, configured_scale: MockConfigEntry
) -> None:
    """Only unconfigured Beurer scales are offered; otherwise there is nothing to add."""
    await inject_advertisement(hass, scale_advertisement())
    await inject_advertisement(
        hass,
        scale_advertisement(
            address="AA:BB:CC:DD:EE:02",
            local_name="Smart Scale",
            # Plain 0x181D service data would also wake core's xiaomi_ble discovery.
            service_data={},
        ),
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"
