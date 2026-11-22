"""Verify Galevo beta behavior through the real Home Assistant lifecycle."""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from test_lifecycle import DOMAIN, GATEWAY_ID, existing_entry, fake_gateway

from custom_components.elco_aerotop import config_flow
from custom_components.elco_aerotop import coordinator as coordinator_module
from custom_components.elco_aerotop.api import ElcoConnectionError, ElcoResponseError
from custom_components.elco_aerotop.diagnostics import async_get_config_entry_diagnostics
from custom_components.elco_aerotop.models import ElcoData, PlantState, ZoneState
from custom_components.elco_aerotop.sensor import _energy_value


@pytest.fixture
def galevo_fixture(hass, monkeypatch):
    api, _ = fake_gateway(monkeypatch)
    api.last_features_response = {"data": {"plant": {"systemType": 3}}}
    api.async_get_galevo_time_prog.return_value = {}
    api.async_get_galevo_bus_devices.return_value = [{"name": "Modem", "sn": "SYNTHETIC-MODEM-ID"}]
    api.async_get_galevo_consumption_sequences.return_value = [
        {"k": 1, "p": 4, "v": [1] * 12 + [99]},
        {"k": 2, "p": 4, "v": [2] * 11 + [None, 99]},
    ]
    api.async_get_menu_items.return_value = {"119": {"value": 4}}
    items = {}
    zone = ZoneState.from_galevo(1, {"mod": {"value": 3, "allowedOptions": [2, 3]}}, {})

    def read(*args, **kwargs):
        snapshot_items = {key: dict(value) for key, value in items.items()}
        return ElcoData(
            GATEWAY_ID, PlantState.from_galevo(snapshot_items), {1: zone}
        ), snapshot_items

    api.async_get_galevo_data.side_effect = read
    entry, _, _ = existing_entry(hass)
    return api, entry, items


def _plant_mode(value=1, *, read_only=False):
    return {
        "value": value,
        "options": [0, 1],
        "optTexts": ["Summer", "Winter"],
        "readOnly": read_only,
    }


def _scheduled_dhw_items(mode=1):
    return {
        "DhwMode:0": {"value": mode, "options": [0, 1, 2]},
        "DhwTimeProgComfortTemp:0": {
            "value": 53,
            "min": 35,
            "max": 60,
            "step": 1,
            "readOnly": False,
        },
        "DhwTimeProgEconomyTemp:0": {"value": 40},
    }


@pytest.mark.parametrize("readback", ["confirmed", "unchanged", "economy_changed"])
async def test_beta_scheduled_hot_water_number_verifies_both_setpoints(
    hass, galevo_fixture, readback
):
    api, entry, items = galevo_fixture
    items.update(_scheduled_dhw_items())

    def write(*, comfort, economy, old_comfort, old_economy):
        if readback != "unchanged":
            items["DhwTimeProgComfortTemp:0"]["value"] = comfort
        if readback == "economy_changed":
            items["DhwTimeProgEconomyTemp:0"]["value"] = economy + 1

    api.async_set_galevo_dhw_timeprog_temperatures.side_effect = write
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    registry = er.async_get(hass)
    number = registry.async_get_entity_id(
        "number", DOMAIN, f"{GATEWAY_ID}_dhw_scheduled_comfort_temperature"
    )
    assert hass.states.get(number).attributes["mode"] == "box"
    assert registry.async_get_entity_id("select", DOMAIN, f"{GATEWAY_ID}_dhw_mode") is None
    assert (
        registry.async_get_entity_id("number", DOMAIN, f"{GATEWAY_ID}_dhw_reduced_temperature")
        is None
    )
    call = hass.services.async_call(
        "number", "set_value", {"entity_id": number, "value": 52}, blocking=True
    )
    if readback == "confirmed":
        await call
        assert float(hass.states.get(number).state) == 52
    else:
        with pytest.raises(HomeAssistantError, match="not confirmed"):
            await call
    api.async_set_galevo_dhw_timeprog_temperatures.assert_awaited_once_with(
        comfort=52, economy=40, old_comfort=53, old_economy=40
    )
    api.async_set_galevo_dhw_temp.assert_not_awaited()
    api.async_set_galevo_dhw_mode.assert_not_awaited()


async def test_scheduled_comfort_checks_fresh_mode_availability_and_bounds(hass, galevo_fixture):
    api, entry, items = galevo_fixture
    items.update(_scheduled_dhw_items(mode=2))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    registry = er.async_get(hass)
    unique_id = f"{GATEWAY_ID}_dhw_scheduled_comfort_temperature"
    assert registry.async_get_entity_id("number", DOMAIN, unique_id) is None
    coordinator = entry.runtime_data
    items.update(_scheduled_dhw_items())
    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    number = registry.async_get_entity_id("number", DOMAIN, unique_id)
    assert float(hass.states.get(number).state) == 53

    for value, error in [(39, "below economy"), (61, "above the maximum"), (52.5, "step")]:
        with pytest.raises(HomeAssistantError, match=error):
            await coordinator.async_set_scheduled_dhw_comfort_temperature(value)
    await coordinator.async_set_scheduled_dhw_comfort_temperature(53)
    items["DhwMode:0"]["value"] = 2
    with pytest.raises(HomeAssistantError, match="Time Based"):
        await coordinator.async_set_scheduled_dhw_comfort_temperature(52)
    await coordinator.async_refresh()
    assert hass.states.get(number).state == "unavailable"
    api.async_set_galevo_dhw_timeprog_temperatures.assert_not_awaited()


async def test_late_capability_energy_privacy_and_beta_gates(hass, galevo_fixture):
    api, entry, items = galevo_fixture
    items["DhwTemp:0"] = {"value": 55}
    items["DhwMode:0"] = {"value": 2, "options": [0, 2]}
    items["Holiday:0"] = {"value": 1, "readOnly": False}
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    registry = er.async_get(hass)
    coordinator = entry.runtime_data
    assert coordinator.data.discovery.system_type == 3
    assert registry.async_get_entity_id("select", DOMAIN, f"{GATEWAY_ID}_plant_mode") is None
    assert registry.async_get_entity_id("switch", DOMAIN, f"{GATEWAY_ID}_holiday") is None
    assert (
        registry.async_get_entity_id("water_heater", DOMAIN, f"{GATEWAY_ID}_domestic_hot_water")
        is None
    )
    assert (
        registry.async_get_entity_id(
            "sensor", DOMAIN, f"{GATEWAY_ID}_annual_energy_input_heating_1"
        )
        is None
    )
    energy = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{GATEWAY_ID}_energy_heating_last_12_complete_months"
    )
    assert float(hass.states.get(energy).state) == 12
    data = coordinator.data
    assert _energy_value(data, 2, 4, "last_month") is None
    assert _energy_value(data, 2, 4, "last_12_complete_months") is None
    ready = await async_get_config_entry_diagnostics(hass, entry)
    assert ready["data"]["discovery"]["bus_devices"][0]["sn"] == "<redacted>"

    items["PlantMode:0"] = _plant_mode(read_only=True)
    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert registry.async_get_entity_id("select", DOMAIN, f"{GATEWAY_ID}_plant_mode") is None
    items["PlantMode:0"] = _plant_mode()
    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    select_id = registry.async_get_entity_id("select", DOMAIN, f"{GATEWAY_ID}_plant_mode")
    assert hass.states.get(select_id).state == "Winter"
    api.async_set_galevo_holiday.assert_not_awaited()


@pytest.mark.parametrize("error", [None, ElcoResponseError("Rejected")])
async def test_plant_mode_locks_and_reports_unconfirmed_changes(hass, galevo_fixture, error):
    api, entry, items = galevo_fixture
    items["PlantMode:0"] = _plant_mode()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = entry.runtime_data
    api.async_set_galevo_plant_mode.side_effect = error
    async with coordinator._gateway_lock:
        command = asyncio.create_task(coordinator.async_set_plant_mode(0))
        await asyncio.sleep(0)
        api.async_set_galevo_plant_mode.assert_not_awaited()
    with pytest.raises(HomeAssistantError, match="not confirmed" if error is None else "Rejected"):
        await command
    api.async_set_galevo_plant_mode.assert_awaited_once_with(0, old_mode=1)
    assert coordinator.data.discovery.system_value("PlantMode") == 1


async def test_ambiguous_plant_mode_write_can_be_verified_without_replay(hass, galevo_fixture):
    api, entry, items = galevo_fixture
    items["PlantMode:0"] = _plant_mode()

    def apply_then_disconnect(mode, *, old_mode):
        assert mode == 0 and old_mode == 1
        items["PlantMode:0"] = _plant_mode(value=mode)
        raise ElcoConnectionError("Connection lost after command")

    api.async_set_galevo_plant_mode.side_effect = apply_then_disconnect
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = entry.runtime_data
    await coordinator.async_set_plant_mode(0)
    api.async_set_galevo_plant_mode.assert_awaited_once()
    assert coordinator.data.discovery.system_value("PlantMode") == 0


async def test_reauthentication_checks_gateway_membership(hass, monkeypatch):
    entry, _, _ = existing_entry(hass)
    client = Mock(
        gateway_id=GATEWAY_ID,
        async_login=AsyncMock(),
        async_list_plants=AsyncMock(return_value=[{"gwId": "DIFFERENT-GATEWAY"}]),
        async_get_features=AsyncMock(),
    )
    monkeypatch.setattr(config_flow, "ElcoApiClient", lambda *args: client)
    flow = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reauth", "entry_id": entry.entry_id}, data=dict(entry.data)
    )
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"username": "different@example.invalid", "password": "synthetic"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "gateway_access_denied"}
    assert entry.data["username"] == "synthetic@example.invalid"
    client.async_get_features.assert_not_awaited()


async def test_bad_snapshot_keeps_last_good_read(hass, galevo_fixture):
    api, entry, items = galevo_fixture
    items["PlantMode:0"] = _plant_mode()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = entry.runtime_data
    old_data = coordinator.data
    api.async_get_galevo_data.side_effect = ElcoResponseError("Invalid response")
    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    assert coordinator.data is old_data
    assert coordinator.last_successful_update == old_data.captured_at


async def test_experimental_holiday_uses_explicit_draft_and_checks_readback(
    hass, galevo_fixture, monkeypatch
):
    api, entry, items = galevo_fixture
    items["Holiday:0"] = {"value": 0, "readOnly": False}
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = entry.runtime_data
    monkeypatch.setattr(coordinator_module, "GALEVO_WRITES_ENABLED", True)
    monkeypatch.setattr(
        coordinator, "_local_now", lambda: datetime.fromisoformat("2026-11-22T12:34:56+01:00")
    )
    with pytest.raises(HomeAssistantError, match="both holiday draft dates"):
        await coordinator.async_set_galevo_holiday()
    coordinator.set_pending_holiday(start=date(2026, 11, 24), end=date(2026, 11, 23))
    with pytest.raises(HomeAssistantError, match="ordered holiday window"):
        await coordinator.async_set_galevo_holiday()
    api.async_set_galevo_holiday.assert_not_awaited()
    coordinator.set_pending_holiday(start=None, end=date(2026, 11, 26))

    def schedule(*, start, end):
        assert start == "2026-11-24T00:00:00"
        items["Holiday:0"] = {"value": 1, "expiresOn": end}

    def cancel(*, now):
        assert now == "2026-11-22T12:34:56"
        items["Holiday:0"] = {"value": 0}

    api.async_set_galevo_holiday.side_effect = schedule
    api.async_reset_galevo_holiday.side_effect = cancel
    await coordinator.async_set_galevo_holiday()
    api.async_set_galevo_holiday.assert_awaited_once_with(
        start="2026-11-24T00:00:00", end="2026-11-26T23:59:59"
    )
    await coordinator.async_reset_galevo_holiday()
    api.async_reset_galevo_holiday.assert_awaited_once()


@pytest.mark.parametrize("family", [5, 4])
async def test_manual_gateway_validates_a_bsb_snapshot_and_rejects_unsupported_families(
    hass, monkeypatch, family
):
    from test_lifecycle import FIXTURE

    api, _ = fake_gateway(monkeypatch)
    api.system_type = family
    api.async_get_features.return_value = FIXTURE["features"]
    monkeypatch.setattr(config_flow, "ElcoApiClient", lambda *args: api)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={
            "username": "synthetic@example.invalid",
            "password": "synthetic",
            "gateway_id": GATEWAY_ID.lower(),
        },
    )
    if family == 5:
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["result"].unique_id == GATEWAY_ID
        await hass.async_block_till_done(wait_background_tasks=True)
        assert api.async_get_data.await_args_list[0].kwargs == {"use_cache": False}
    else:
        assert result["errors"] == {"base": "unsupported_controller"}
        api.async_get_data.assert_not_awaited()
    api.async_list_plants.assert_not_awaited()
