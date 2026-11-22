"""Verify Galevo beta behavior through the real Home Assistant lifecycle."""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components.climate.const import ClimateEntityFeature
from homeassistant.components.water_heater import WaterHeaterEntityFeature
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from test_lifecycle import DOMAIN, GATEWAY_ID, existing_entry, fake_gateway, make_entry

from custom_components.elco_aerotop import config_flow
from custom_components.elco_aerotop.api import ElcoConnectionError, ElcoResponseError
from custom_components.elco_aerotop.diagnostics import async_get_config_entry_diagnostics
from custom_components.elco_aerotop.models import (
    GALEVO_DHW_MODE_TO_RAW,
    GALEVO_ZONE_MODE_TO_RAW,
    ElcoData,
    PlantState,
    ZoneState,
)
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
    return api, make_entry(hass), items


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


def _existing_scheduled_comfort(hass, entry):
    return (
        er.async_get(hass)
        .async_get_or_create(
            "number",
            DOMAIN,
            f"{GATEWAY_ID}_dhw_scheduled_comfort_temperature",
            suggested_object_id="existing_scheduled_comfort",
            config_entry=entry,
        )
        .entity_id
    )


@pytest.mark.parametrize("readback", ["confirmed", "unchanged", "economy_changed"])
async def test_beta_scheduled_hot_water_number_verifies_both_setpoints(
    hass, galevo_fixture, readback
):
    api, entry, items = galevo_fixture
    items.update(_scheduled_dhw_items())
    number = _existing_scheduled_comfort(hass, entry)

    def write(*, comfort, economy, old_comfort, old_economy):
        if readback != "unchanged":
            items["DhwTimeProgComfortTemp:0"]["value"] = comfort
        if readback == "economy_changed":
            items["DhwTimeProgEconomyTemp:0"]["value"] = economy + 1

    api.async_set_galevo_dhw_timeprog_temperatures.side_effect = write
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    registry = er.async_get(hass)
    assert hass.states.get(number).attributes["mode"] == "box"
    assert registry.async_get_entity_id("select", DOMAIN, f"{GATEWAY_ID}_dhw_mode") is None
    assert (
        registry.async_get_entity_id("number", DOMAIN, f"{GATEWAY_ID}_dhw_reduced_temperature")
        is not None
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
    number = _existing_scheduled_comfort(hass, entry)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = entry.runtime_data
    items.update(_scheduled_dhw_items())
    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
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


@pytest.mark.parametrize("missing", ["target", "options"])
async def test_native_controls_recover_from_partial_discovery(hass, galevo_fixture, missing):
    api, entry, items = galevo_fixture
    full = _scheduled_dhw_items() | {
        "PlantMode:0": {"value": 1},
        "ZoneMode:1": {"value": 3, "options": [0, 1, 2, 3]},
        "ZoneComfortTemp:1": {"value": 21, "min": 10, "max": 30},
        "ZoneEconomyTemp:1": {"value": 17},
    }
    items.update(full)
    if missing == "target":
        del items["ZoneComfortTemp:1"], items["DhwTimeProgComfortTemp:0"]
    else:
        items["ZoneMode:1"] = {"value": 3}
        items["DhwMode:0"] = {"value": 1}

    def read(*args, **kwargs):
        snapshot = {key: dict(item) for key, item in items.items()}
        return ElcoData(
            GATEWAY_ID,
            PlantState.from_galevo(snapshot),
            {1: ZoneState.from_galevo(1, {}, snapshot)},
        ), snapshot

    api.async_get_galevo_data.side_effect = read
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    registry = er.async_get(hass)
    thermostat = registry.async_get_entity_id("climate", DOMAIN, f"{GATEWAY_ID}_zone_1_thermostat")
    heater = registry.async_get_entity_id(
        "water_heater", DOMAIN, f"{GATEWAY_ID}_domestic_hot_water"
    )
    climate_features = hass.states.get(thermostat).attributes["supported_features"]
    heater_features = hass.states.get(heater).attributes["supported_features"]
    assert bool(climate_features & ClimateEntityFeature.TARGET_TEMPERATURE) == (
        missing == "options"
    )
    assert bool(heater_features & WaterHeaterEntityFeature.TARGET_TEMPERATURE) == (
        missing == "options"
    )
    assert bool(climate_features & ClimateEntityFeature.PRESET_MODE) == (missing == "target")
    assert bool(heater_features & WaterHeaterEntityFeature.OPERATION_MODE) == (missing == "target")

    if missing == "target":

        def set_operation(plant, *, comfort, reduced, mode):
            items["DhwMode:0"] = {**items["DhwMode:0"], "value": GALEVO_DHW_MODE_TO_RAW[mode]}

        api.async_set_galevo_dhw_settings.side_effect = set_operation
        await hass.services.async_call(
            "water_heater",
            "set_operation_mode",
            {"entity_id": heater, "operation_mode": "off"},
            blocking=True,
        )
        assert hass.states.get(heater).state == "off"

    items.update(full)
    await entry.runtime_data.async_refresh()
    assert hass.states.get(thermostat).attributes["temperature"] == 21
    assert hass.states.get(thermostat).attributes["preset_modes"] == ["eco", "comfort"]
    assert hass.states.get(heater).attributes["temperature"] == 53
    assert hass.states.get(heater).attributes["operation_list"] == [
        "off",
        "heat_pump",
        "performance",
    ]

    items.clear()
    await entry.runtime_data.async_refresh()
    assert hass.states.get(thermostat).state == "unavailable"
    assert hass.states.get(heater).state == "unavailable"
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(thermostat).state == "unavailable"
    assert hass.states.get(heater).state == "unavailable"
    items.update(full)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(thermostat).attributes["temperature"] == 21
    assert hass.states.get(heater).attributes["temperature"] == 53
    for domain, key in (
        ("select", "dhw_mode"),
        ("select", "zone_1_mode"),
        ("number", "dhw_comfort_temperature"),
        ("number", "dhw_scheduled_comfort_temperature"),
        ("number", "zone_1_comfort_temperature"),
    ):
        assert registry.async_get_entity_id(domain, DOMAIN, f"{GATEWAY_ID}_{key}") is None


async def test_late_capability_energy_and_privacy(hass, galevo_fixture):
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
    assert registry.async_get_entity_id("switch", DOMAIN, f"{GATEWAY_ID}_holiday") is not None
    assert (
        registry.async_get_entity_id("water_heater", DOMAIN, f"{GATEWAY_ID}_domestic_hot_water")
        is not None
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


async def test_holiday_uses_explicit_draft_and_checks_readback(hass, galevo_fixture, monkeypatch):
    api, entry, items = galevo_fixture
    items["Holiday:0"] = {"value": 0, "readOnly": False}
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = entry.runtime_data
    monkeypatch.setattr(
        coordinator, "_local_now", lambda: datetime.fromisoformat("2026-11-22T12:34:56+01:00")
    )
    with pytest.raises(HomeAssistantError, match="both holiday draft dates"):
        await coordinator.async_set_galevo_holiday()
    coordinator.set_pending_holiday(start=date(2026, 11, 24), end=date(2026, 11, 23))
    with pytest.raises(HomeAssistantError, match="ordered holiday window"):
        await coordinator.async_set_galevo_holiday()
    api.async_set_galevo_holiday.assert_not_awaited()
    registry = er.async_get(hass)
    for boundary, value in (("start", "2026-11-24"), ("end", "2026-11-26")):
        draft = registry.async_get_entity_id("date", DOMAIN, f"{GATEWAY_ID}_holiday_{boundary}")
        await hass.services.async_call(
            "date", "set_value", {"entity_id": draft, "date": value}, blocking=True
        )
    holiday = registry.async_get_entity_id("switch", DOMAIN, f"{GATEWAY_ID}_holiday")
    assert hass.states.get(holiday).attributes["pending_holiday_end"] == "2026-11-26"

    def schedule(*, start, end):
        assert start == "2026-11-24T00:00:00"
        items["Holiday:0"] = {"value": 1, "expiresOn": end}

    def cancel(*, now):
        assert now == "2026-11-22T12:34:56"
        items["Holiday:0"] = {"value": 0}

    api.async_set_galevo_holiday.side_effect = schedule
    api.async_reset_galevo_holiday.side_effect = cancel
    await hass.services.async_call("switch", "turn_on", {"entity_id": holiday}, blocking=True)
    api.async_set_galevo_holiday.assert_awaited_once_with(
        start="2026-11-24T00:00:00", end="2026-11-26T23:59:59"
    )
    await hass.services.async_call("switch", "turn_off", {"entity_id": holiday}, blocking=True)
    api.async_reset_galevo_holiday.assert_awaited_once()


@pytest.mark.parametrize("days_ahead", [0, 2])
async def test_holiday_without_expiry_only_confirms_same_day_activation(
    hass, galevo_fixture, monkeypatch, days_ahead
):
    from datetime import timedelta

    api, entry, items = galevo_fixture
    items["Holiday:0"] = {"value": False}
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = entry.runtime_data
    today = datetime.fromisoformat("2026-11-22T12:34:56+01:00")
    monkeypatch.setattr(coordinator, "_local_now", lambda: today)
    coordinator.set_pending_holiday(
        start=today.date() + timedelta(days=days_ahead),
        end=today.date() + timedelta(days=4),
    )

    def activate(**kwargs):
        items["Holiday:0"] = {"value": True}

    api.async_set_galevo_holiday.side_effect = activate
    if days_ahead:
        with pytest.raises(HomeAssistantError, match="not confirmed"):
            await coordinator.async_set_galevo_holiday()
    else:
        await coordinator.async_set_galevo_holiday()
    api.async_set_galevo_holiday.assert_awaited_once()


@pytest.mark.parametrize(
    ("domain", "key", "item_key", "initial", "value"),
    [
        ("switch", "quiet_mode", "IsQuite:0", False, 1),
        ("switch", "domestic_hot_water_boost", "IsDhwBoost:0", 0, 1),
        ("switch", "automatic_thermoregulation", "AutomaticThermoregulation:0", 0, 1),
        ("number", "zone_1_heating_flow_temperature", "HeatingFlowTemp:1", 35, 36),
        ("number", "zone_1_heating_flow_offset", "HeatingFlowOffset:1", 0, -1),
        ("number", "zone_1_cooling_flow_temperature", "CoolingFlowTemp:1", 19, 20),
        ("number", "zone_1_cooling_flow_offset", "CoolingFlowOffset:1", 0, 1),
    ],
)
async def test_galevo_data_item_controls_register_and_write(
    hass, galevo_fixture, domain, key, item_key, initial, value
):
    api, entry, items = galevo_fixture
    item_id, zone = item_key.split(":")
    item = {"id": item_id, "zone": int(zone), "value": initial, "min": -7, "max": 60}
    items[item_key] = item

    def write(before, target):
        assert before["value"] == initial
        items[item_key]["value"] = target

    api.async_set_galevo_data_item.side_effect = write
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    entity = er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"{GATEWAY_ID}_{key}")
    assert entity
    if domain == "switch":
        await hass.services.async_call(domain, "turn_on", {"entity_id": entity}, blocking=True)
        assert hass.states.get(entity).state == "on"
    else:
        assert hass.states.get(entity).attributes["mode"] == (
            "slider" if key.endswith("offset") else "box"
        )
        await hass.services.async_call(
            domain, "set_value", {"entity_id": entity, "value": value}, blocking=True
        )
        assert float(hass.states.get(entity).state) == value
    assert api.async_set_galevo_data_item.await_args.args[1] == value


@pytest.mark.parametrize("cooling", [False, True])
async def test_galevo_thermostat_and_zone_mode_follow_selected_plant_mode(
    hass, galevo_fixture, cooling
):
    api, entry, items = galevo_fixture
    items.update(
        {
            "PlantMode:0": {"value": 3 if cooling else 1, "options": [1, 3]},
            "ZoneMode:1": {"value": 3, "options": [0, 1, 2, 3]},
            "ZoneComfortTemp:1": {"value": 21, "min": 10, "max": 30},
            "ZoneEconomyTemp:1": {"value": 17},
            "ZoneComfortCoolingTemp:1": {"value": 22, "min": 16, "max": 30},
            "ZoneEconomyCoolingTemp:1": {"value": 27},
        }
    )

    def read(*args, **kwargs):
        snapshot = {key: dict(item) for key, item in items.items()}
        zone = ZoneState.from_galevo(1, {"heat": False}, snapshot)
        return ElcoData(GATEWAY_ID, PlantState.from_galevo(snapshot), {1: zone}), snapshot

    def write_zone_mode(plant, zone, *, mode):
        items["ZoneMode:1"]["value"] = GALEVO_ZONE_MODE_TO_RAW[mode]

    def write_temperature(zone, *, comfort, reduced, cooling):
        key = "ZoneComfortCoolingTemp:1" if cooling else "ZoneComfortTemp:1"
        items[key]["value"] = comfort

    api.async_get_galevo_data.side_effect = read
    api.async_set_zone_mode.side_effect = write_zone_mode
    api.async_set_zone_temperatures.side_effect = write_temperature
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    registry = er.async_get(hass)
    thermostat = registry.async_get_entity_id("climate", DOMAIN, f"{GATEWAY_ID}_zone_1_thermostat")
    select = registry.async_get_entity_id("select", DOMAIN, f"{GATEWAY_ID}_zone_1_mode")
    hvac_mode = "cool" if cooling else "heat"
    assert hvac_mode in hass.states.get(thermostat).attributes["hvac_modes"]
    assert hass.states.get(thermostat).attributes["hvac_action"] == "idle"
    await hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": thermostat, "hvac_mode": hvac_mode}, blocking=True
    )
    assert hass.states.get(thermostat).state == hvac_mode
    assert select is None
    for key in ("comfort_temperature", "cooling_comfort_temperature"):
        assert registry.async_get_entity_id("number", DOMAIN, f"{GATEWAY_ID}_zone_1_{key}") is None
    for key in ("reduced_temperature", "cooling_reduced_temperature"):
        assert (
            registry.async_get_entity_id("number", DOMAIN, f"{GATEWAY_ID}_zone_1_{key}") is not None
        )
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": thermostat, "temperature": 23}, blocking=True
    )
    assert api.async_set_zone_temperatures.await_args.kwargs["cooling"] is cooling
    assert hass.states.get(thermostat).attributes["temperature"] == 23

    # The other season can lack its current target while still offering modes.
    items["PlantMode:0"]["value"] = 1 if cooling else 3
    new_target = "ZoneComfortTemp:1" if cooling else "ZoneComfortCoolingTemp:1"
    saved_target = items.pop(new_target)
    await entry.runtime_data.async_refresh()
    assert ("heat" if cooling else "cool") in hass.states.get(thermostat).attributes["hvac_modes"]
    assert not hass.states.get(thermostat).attributes["supported_features"] & (
        ClimateEntityFeature.TARGET_TEMPERATURE
    )
    assert registry.async_get_entity_id("select", DOMAIN, f"{GATEWAY_ID}_zone_1_mode") is None
    items[new_target] = saved_target
    await entry.runtime_data.async_refresh()
    assert hass.states.get(thermostat).attributes["temperature"] == saved_target["value"]


@pytest.mark.parametrize(
    ("changed_item", "new_item", "error"),
    [
        ("PlantMode:0", None, "current plant season"),
        ("PlantMode:0", {"value": 1}, "only be changed when cooling is selected"),
        ("ZoneEconomyCoolingTemp:1", None, "both zone temperatures"),
    ],
)
async def test_zone_temperature_requires_fresh_season_and_both_targets(
    hass, galevo_fixture, changed_item, new_item, error
):
    api, entry, items = galevo_fixture
    items.update(
        {
            "PlantMode:0": {"value": 3, "options": [1, 3]},
            "ZoneMode:1": {"value": 2, "options": [0, 1, 2, 3]},
            "ZoneComfortTemp:1": {"value": 21},
            "ZoneEconomyTemp:1": {"value": 17},
            "ZoneComfortCoolingTemp:1": {"value": 23},
            "ZoneEconomyCoolingTemp:1": {"value": 27},
        }
    )

    def read(*args, **kwargs):
        snapshot = {key: dict(item) for key, item in items.items()}
        zone = ZoneState.from_galevo(1, {"heat": False, "isCoolingActive": False}, snapshot)
        return ElcoData(GATEWAY_ID, PlantState.from_galevo(snapshot), {1: zone}), snapshot

    def write(zone, *, comfort, reduced, cooling):
        assert cooling and reduced == 27
        items["ZoneComfortCoolingTemp:1"]["value"] = comfort

    api.async_get_galevo_data.side_effect = read
    api.async_set_zone_temperatures.side_effect = write
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    registry = er.async_get(hass)
    thermostat = registry.async_get_entity_id("climate", DOMAIN, f"{GATEWAY_ID}_zone_1_thermostat")
    service_data = {"entity_id": thermostat, "temperature": 24}
    assert hass.states.get(thermostat).attributes["temperature"] == 23

    saved_item = items.pop(changed_item)
    if new_item is not None:
        items[changed_item] = new_item
    with pytest.raises(HomeAssistantError, match=error):
        await hass.services.async_call("climate", "set_temperature", service_data, blocking=True)
    api.async_set_zone_temperatures.assert_not_awaited()

    await entry.runtime_data.async_refresh()
    state = hass.states.get(thermostat)
    if new_item is not None:
        assert state.attributes["temperature"] == 21
        assert "heat" in state.attributes["hvac_modes"]
    else:
        assert not state.attributes["supported_features"] & ClimateEntityFeature.TARGET_TEMPERATURE
    if changed_item == "PlantMode:0" and new_item is None:
        assert state.attributes["hvac_modes"] == ["off", "auto"]
        assert state.attributes.get("temperature") is None
    assert state.attributes["hvac_action"] == "idle"

    items[changed_item] = saved_item
    await entry.runtime_data.async_refresh()
    await hass.services.async_call("climate", "set_temperature", service_data, blocking=True)
    assert hass.states.get(thermostat).attributes["temperature"] == 24
    api.async_set_zone_temperatures.assert_awaited_once()


@pytest.mark.parametrize("plant_mode", [1, 3])
@pytest.mark.parametrize("upgrading", [False, True])
async def test_galevo_blocks_off_season_writes_on_mirrored_gateway(
    hass, galevo_fixture, plant_mode, upgrading
):
    api, entry, items = galevo_fixture
    cooling_active = plant_mode == 3
    reduced = 24 if cooling_active else 17
    items.update(
        {
            "PlantMode:0": {"value": plant_mode, "options": [1, 3]},
            "ZoneMode:1": {"value": 2, "options": [0, 1, 2, 3]},
            "ZoneComfortTemp:1": {"value": 20},
            "ZoneComfortCoolingTemp:1": {"value": 20},
            "ZoneDesiredTemp:1": {"value": 20},
            "ZoneEconomyTemp:1": {"value": reduced},
            "ZoneEconomyCoolingTemp:1": {"value": reduced},
        }
    )

    def read(*args, **kwargs):
        snapshot = {key: dict(item) for key, item in items.items()}
        zone = ZoneState.from_galevo(1, {"heat": not cooling_active}, snapshot)
        return ElcoData(GATEWAY_ID, PlantState.from_galevo(snapshot), {1: zone}), snapshot

    def write(zone, *, comfort, reduced, cooling):
        # The reported controller mirrors a write into both seasons and the live target.
        for key in ("ZoneComfortTemp:1", "ZoneComfortCoolingTemp:1", "ZoneDesiredTemp:1"):
            items[key]["value"] = comfort

    api.async_get_galevo_data.side_effect = read
    api.async_set_zone_temperatures.side_effect = write
    registry = er.async_get(hass)
    prefix = "" if cooling_active else "cooling_"
    preserved_suffixes = (
        "zone_1_off_season_heating_comfort_temperature",
        "zone_1_off_season_cooling_comfort_temperature",
        f"zone_1_{prefix}comfort_temperature",
    )
    existing_ids = {}
    if upgrading:
        for index, suffix in enumerate(preserved_suffixes):
            registered = registry.async_get_or_create(
                "number",
                DOMAIN,
                f"{GATEWAY_ID}_{suffix}",
                suggested_object_id=f"custom_temperature_{index}",
                config_entry=entry,
            )
            existing_ids[suffix] = registered.entity_id

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    inactive_numbers = [
        registry.async_get_entity_id(
            "number", DOMAIN, f"{GATEWAY_ID}_zone_1_{prefix}reduced_temperature"
        )
    ]
    for suffix in preserved_suffixes:
        entity_id = registry.async_get_entity_id("number", DOMAIN, f"{GATEWAY_ID}_{suffix}")
        assert entity_id == existing_ids.get(suffix)
        if entity_id is not None:
            inactive_numbers.append(entity_id)

    for entity_id in inactive_numbers:
        assert hass.states.get(entity_id).state == "unavailable"
        await hass.services.async_call(
            "number", "set_value", {"entity_id": entity_id, "value": 16.5}, blocking=True
        )
    for kind in (f"{prefix}comfort", f"{prefix}reduced"):
        with pytest.raises(HomeAssistantError, match="temperatures can only be changed"):
            await entry.runtime_data.async_set_zone_temperature(1, kind, 16.5)
    api.async_set_zone_temperatures.assert_not_awaited()
    assert items["ZoneComfortTemp:1"]["value"] == 20
    assert items["ZoneComfortCoolingTemp:1"]["value"] == 20
    assert items["ZoneDesiredTemp:1"]["value"] == 20

    thermostat = registry.async_get_entity_id("climate", DOMAIN, f"{GATEWAY_ID}_zone_1_thermostat")
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": thermostat, "temperature": 20.5}, blocking=True
    )
    assert api.async_set_zone_temperatures.await_args.kwargs == {
        "comfort": 20.5,
        "reduced": reduced,
        "cooling": cooling_active,
    }
    assert hass.states.get(thermostat).attributes["temperature"] == 20.5
    assert items["PlantMode:0"]["value"] == plant_mode


async def test_water_heater_rechecks_read_only_target_before_writing(hass, galevo_fixture):
    api, entry, items = galevo_fixture
    items.update(_scheduled_dhw_items())
    items["DhwTemp:0"] = {"value": 55}
    comfort = items["DhwTimeProgComfortTemp:0"]
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    registry = er.async_get(hass)
    heater = registry.async_get_entity_id(
        "water_heater", DOMAIN, f"{GATEWAY_ID}_domestic_hot_water"
    )
    service_data = {"entity_id": heater, "temperature": 52}
    assert hass.states.get(heater).attributes["temperature"] == 53

    comfort["readOnly"] = True
    with pytest.raises(HomeAssistantError, match="scheduled DHW comfort setpoint is read-only"):
        await hass.services.async_call(
            "water_heater", "set_temperature", service_data, blocking=True
        )
    api.async_set_galevo_dhw_settings.assert_not_awaited()

    await entry.runtime_data.async_refresh()
    features = hass.states.get(heater).attributes["supported_features"]
    assert not features & WaterHeaterEntityFeature.TARGET_TEMPERATURE
    assert features & WaterHeaterEntityFeature.OPERATION_MODE

    comfort["readOnly"] = False
    await entry.runtime_data.async_refresh()
    assert (
        hass.states.get(heater).attributes["supported_features"]
        & WaterHeaterEntityFeature.TARGET_TEMPERATURE
    )


@pytest.mark.parametrize(
    "target_result", ["confirmed", "rejected", "read_only", "becomes_read_only"]
)
async def test_native_water_heater_combines_mode_and_target_with_partial_result(
    hass, galevo_fixture, target_result
):
    api, entry, items = galevo_fixture
    items.update(_scheduled_dhw_items())
    items["DhwTemp:0"] = {
        "value": 55,
        "min": 42,
        "max": 65,
        "readOnly": target_result == "read_only",
    }

    def write(plant, *, comfort, reduced, mode):
        if mode is not None:
            items["DhwMode:0"]["value"] = GALEVO_DHW_MODE_TO_RAW[mode]
            if target_result == "becomes_read_only":
                items["DhwTemp:0"]["readOnly"] = True
        if comfort is not None:
            assert plant.dhw_mode.value == 3
            if target_result == "rejected":
                raise ElcoResponseError("Rejected")
            items["DhwTemp:0"]["value"] = comfort

    api.async_set_galevo_dhw_settings.side_effect = write
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    registry = er.async_get(hass)
    heater = registry.async_get_entity_id(
        "water_heater", DOMAIN, f"{GATEWAY_ID}_domestic_hot_water"
    )
    assert registry.async_get_entity_id("select", DOMAIN, f"{GATEWAY_ID}_dhw_mode") is None
    for key in ("dhw_comfort_temperature", "dhw_scheduled_comfort_temperature"):
        assert registry.async_get_entity_id("number", DOMAIN, f"{GATEWAY_ID}_{key}") is None
    assert (
        registry.async_get_entity_id("number", DOMAIN, f"{GATEWAY_ID}_dhw_reduced_temperature")
        is not None
    )
    call = hass.services.async_call(
        "water_heater",
        "set_temperature",
        {"entity_id": heater, "temperature": 56, "operation_mode": "performance"},
        blocking=True,
    )
    if target_result == "read_only":
        with pytest.raises(HomeAssistantError, match="always-active DHW setpoint is read-only"):
            await call
        api.async_set_galevo_dhw_settings.assert_not_awaited()
        assert hass.states.get(heater).state == "heat_pump"
        return
    if target_result in {"rejected", "becomes_read_only"}:
        with pytest.raises(HomeAssistantError, match="mode changed, but the temperature"):
            await call
    else:
        await call
    submitted_targets = [
        call.kwargs["comfort"] for call in api.async_set_galevo_dhw_settings.await_args_list
    ]
    assert submitted_targets == ([None] if target_result == "becomes_read_only" else [None, 56])
    assert hass.states.get(heater).state == "performance"
    assert hass.states.get(heater).attributes["temperature"] == (
        56 if target_result == "confirmed" else 55
    )
    assert hass.states.get(heater).attributes["min_temp"] == 42


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
