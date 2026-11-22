"""Load two controller families in one Home Assistant instance."""

from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from test_lifecycle import DOMAIN, GATEWAY_ID, existing_entry, fake_gateway

from custom_components import elco_aerotop
from custom_components.elco_aerotop import config_flow
from custom_components.elco_aerotop.api import ElcoApiClient
from custom_components.elco_aerotop.models import ElcoData, PlantState, ZoneState

GALEVO_ID = "TESTGALEVO"


@pytest.mark.parametrize("same_account", [True, False])
async def test_mixed_controller_gateways_work_together(hass, monkeypatch, same_account):
    bsb_api, _ = fake_gateway(monkeypatch)
    bsb_entry, bsb_thermostat, _ = existing_entry(hass)

    galevo_api = AsyncMock(spec=ElcoApiClient)
    galevo_api.gateway_id = GALEVO_ID
    galevo_api.system_type = 3
    galevo_api.last_features_response = {"data": {"plant": {"systemType": 3}}}
    galevo_api.async_get_features.return_value = {"zones": [{"num": 1, "roomSens": True}]}
    galevo_api.async_get_zone_numbers.return_value = [1]
    galevo_api.async_get_plant_metadata.return_value = {"gwSerial": "GALEVO-SERIAL"}
    galevo_api.async_get_plant_header.return_value = {"applianceModel": "Galevo", "gwOnline": True}
    galevo_api.async_get_bus_errors.return_value = []
    galevo_api.async_get_galevo_time_prog.return_value = {}
    galevo_api.async_get_galevo_bus_devices.return_value = []
    galevo_api.async_get_galevo_consumption_sequences.return_value = []
    galevo_api.async_get_menu_items.return_value = {"119": {"value": 42}}
    items = {
        "PlantMode:0": {
            "value": 1,
            "options": [0, 1],
            "optTexts": ["Summer", "Winter"],
            "readOnly": False,
        }
    }
    zone = ZoneState.from_galevo(
        1, {"mod": {"value": 3, "allowedOptions": [2, 3]}, "comf": 21, "econ": 17}, {}
    )

    def snapshot(*args, **kwargs):
        snapshot_items = {key: dict(value) for key, value in items.items()}
        return ElcoData(
            GALEVO_ID, PlantState.from_galevo(snapshot_items), {1: zone}
        ), snapshot_items

    def set_mode(mode, *, old_mode):
        assert items["PlantMode:0"]["value"] == old_mode
        items["PlantMode:0"]["value"] = mode

    galevo_api.async_get_galevo_data.side_effect = snapshot
    galevo_api.async_set_galevo_plant_mode.side_effect = set_mode

    account_api = Mock(async_login=AsyncMock(), async_list_plants=AsyncMock())
    account_api.async_list_plants.return_value = [
        {"gwId": GALEVO_ID, "plantName": "Galevo fixture"}
    ]
    if same_account:
        account_api.async_list_plants.return_value.append(
            {"gwId": GATEWAY_ID, "plantName": "BSB fixture"}
        )
    monkeypatch.setattr(
        config_flow,
        "ElcoApiClient",
        lambda session, username, password, gateway_id, base_url: (
            galevo_api if gateway_id else account_api
        ),
    )

    sessions = {}

    def runtime_api(session, username, password, gateway_id, base_url):
        sessions[gateway_id] = session
        return {GATEWAY_ID: bsb_api, GALEVO_ID: galevo_api}[gateway_id]

    monkeypatch.setattr(elco_aerotop, "ElcoApiClient", runtime_api)
    assert await hass.config_entries.async_setup(bsb_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    user = bsb_entry.data["username"] if same_account else "other@example.invalid"
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={"username": user, "password": "synthetic", "base_url": "https://example.invalid"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    galevo_entry = result["result"]
    await hass.async_block_till_done(wait_background_tasks=True)

    assert bsb_entry.state is ConfigEntryState.LOADED
    assert galevo_entry.state is ConfigEntryState.LOADED
    assert bsb_entry.runtime_data is not galevo_entry.runtime_data
    assert sessions[GATEWAY_ID] is not sessions[GALEVO_ID]
    assert sessions[GATEWAY_ID].cookie_jar is not sessions[GALEVO_ID].cookie_jar

    device_registry = dr.async_get(hass)
    bsb_device = device_registry.async_get_device_by_identifier(
        (DOMAIN, GATEWAY_ID), bsb_entry.entry_id
    )
    galevo_device = device_registry.async_get_device_by_identifier(
        (DOMAIN, GALEVO_ID), galevo_entry.entry_id
    )
    assert bsb_device.id != galevo_device.id
    registry = er.async_get(hass)
    plant_mode = registry.async_get_entity_id("select", DOMAIN, f"{GALEVO_ID}_plant_mode")
    assert registry.async_get(plant_mode).device_id == galevo_device.id
    assert registry.async_get(bsb_thermostat).device_id == bsb_device.id
    assert registry.async_get_entity_id("select", DOMAIN, f"{GATEWAY_ID}_plant_mode") is None
    assert registry.async_get_entity_id("calendar", DOMAIN, f"{GALEVO_ID}_zone_1_holidays") is None

    assert bsb_api.async_get_data.await_count == 1
    bsb_api.async_get_galevo_data.assert_not_awaited()
    galevo_api.async_get_data.assert_not_awaited()
    assert galevo_api.async_get_galevo_data.await_count == 1

    await hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": bsb_thermostat, "temperature": 22},
        blocking=True,
    )
    await hass.services.async_call(
        "select", "select_option", {"entity_id": plant_mode, "option": "Summer"}, blocking=True
    )
    bsb_api.async_set_zone_temperatures.assert_awaited_once()
    galevo_api.async_set_zone_temperatures.assert_not_awaited()
    galevo_api.async_set_galevo_plant_mode.assert_awaited_once_with(0, old_mode=1)
    bsb_api.async_set_galevo_plant_mode.assert_not_awaited()

    assert await hass.config_entries.async_unload(bsb_entry.entry_id)
    assert galevo_entry.state is ConfigEntryState.LOADED
    assert hass.states.get(plant_mode).state == "Summer"
    assert sessions[GATEWAY_ID].closed and not sessions[GALEVO_ID].closed
