"""Exercise existing BSB entries and lazy startup against real Home Assistant."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry

from custom_components import elco_aerotop
from custom_components.elco_aerotop.api import ElcoApiClient, ElcoConnectionError, ElcoResponseError
from custom_components.elco_aerotop.diagnostics import async_get_config_entry_diagnostics
from custom_components.elco_aerotop.models import ElcoData, PlantState, ZoneState

DOMAIN = "elco_aerotop"
GATEWAY_ID = "TESTGATEWAY"
FIXTURE = json.loads(
    (Path(__file__).parents[1] / "tests/fixtures/aerotop_one_zone.json").read_text()
)


def fake_gateway(monkeypatch, *, gate=None, fail_first=False):
    api = AsyncMock(spec=ElcoApiClient)
    api.gateway_id = GATEWAY_ID
    api.last_features_response = {"data": {"plant": {"systemType": 5}}}
    api.async_get_features.return_value = FIXTURE["features"]
    api.async_get_zone_numbers.return_value = [1]
    api.async_get_system_items.return_value = FIXTURE["system_items"]
    api.async_get_plant_metadata.return_value = FIXTURE["plant_metadata"]
    api.async_get_plant_header.return_value = FIXTURE["plant_header"]
    api.async_get_plant_user_data.return_value = FIXTURE["plant_user_data"]
    api.async_get_bsb_boiler_data.return_value = {}
    api.async_get_bus_errors.return_value = FIXTURE["bus_errors"]
    api.async_get_bsb_points.return_value = {}
    api.async_get_schedule.return_value = {}
    api.async_get_maintenance.return_value = {}
    api.async_get_automated_monitoring.return_value = {}
    api.async_get_bsb_plant_data.return_value = {}
    raw = FIXTURE["get_data"]
    first_attempt = asyncio.Event()

    async def get_data(*args, **kwargs):
        first_attempt.set()
        if gate is not None:
            await gate.wait()
        if fail_first and api.async_get_data.await_count == 1:
            raise ElcoConnectionError("offline for first poll")
        return ElcoData(
            GATEWAY_ID, PlantState.parse(raw["plantData"]), {1: ZoneState.parse(1, raw["zoneData"])}
        )

    api.async_get_data.side_effect = get_data
    monkeypatch.setattr(elco_aerotop, "ElcoApiClient", lambda *args: api)
    return api, first_attempt


def existing_entry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Existing BSB",
        data={
            "username": "synthetic@example.invalid",
            "password": "synthetic",
            "gateway_id": GATEWAY_ID,
            "base_url": "https://example.invalid",
        },
        unique_id=GATEWAY_ID,
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    climate = registry.async_get_or_create(
        "climate",
        DOMAIN,
        f"{GATEWAY_ID}_zone_1_thermostat",
        suggested_object_id="existing_thermostat",
        config_entry=entry,
    )
    disabled = registry.async_get_or_create(
        "number",
        DOMAIN,
        f"{GATEWAY_ID}_zone_1_comfort_temperature",
        suggested_object_id="existing_comfort_number",
        disabled_by=er.RegistryEntryDisabler.USER,
        config_entry=entry,
    )
    return entry, climate.entity_id, disabled.entity_id


async def test_existing_bsb_entry_on_real_ha(hass, monkeypatch):
    api, _ = fake_gateway(monkeypatch)
    entry, thermostat, disabled = existing_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get(thermostat).state in {"auto", "heat", "cool"}
    assert er.async_get(hass).async_get(disabled).disabled_by is er.RegistryEntryDisabler.USER
    assert hass.states.get(disabled) is None
    assert er.async_get(hass).async_get_entity_id(
        "water_heater", DOMAIN, f"{GATEWAY_ID}_domestic_hot_water"
    )
    assert er.async_get(hass).async_get_entity_id(
        "calendar", DOMAIN, f"{GATEWAY_ID}_zone_1_holidays"
    )
    assert api.async_get_data.await_count == 1
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": thermostat, "temperature": 22}, blocking=True
    )
    api.async_set_zone_temperatures.assert_awaited_once()
    assert api.async_set_zone_temperatures.await_args.kwargs["comfort"] == 22
    api.async_get_galevo_data.assert_not_awaited()
    registry = er.async_get(hass)
    reduced = registry.async_get_entity_id(
        "number", DOMAIN, f"{GATEWAY_ID}_zone_1_reduced_temperature"
    )
    assert hass.states.get(reduced).attributes["mode"] == "box"
    assert (
        registry.async_get_entity_id("sensor", DOMAIN, f"{GATEWAY_ID}_gateway_signal_level") is None
    )
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_existing_bsb_flow_setpoint_when_first_poll_omits_item(hass, monkeypatch):
    api, _ = fake_gateway(monkeypatch)
    entry, _, _ = existing_entry(hass)
    registry = er.async_get(hass)
    flow = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{GATEWAY_ID}_heating_circuit_flow_setpoint_temperature",
        suggested_object_id="existing_flow_setpoint",
        config_entry=entry,
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    api.async_get_system_items.return_value = {
        **FIXTURE["system_items"],
        "ChFlowSetpointTemp:0": {"value": 41},
    }
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data.data.discovery.system_value("ChFlowSetpointTemp") == 41
    assert float(hass.states.get(flow.entity_id).state) == 41


async def test_pending_bsb_diagnostics(hass, monkeypatch):
    gate = asyncio.Event()
    _, attempt = fake_gateway(monkeypatch, gate=gate)
    entry, _, _ = existing_entry(hass)
    try:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await attempt.wait()
        assert entry.state is ConfigEntryState.LOADED
        assert entry.runtime_data.data is None
        exported = await async_get_config_entry_diagnostics(hass, entry)
        assert exported["status"] == "waiting_for_first_snapshot"
        assert exported["data"] is None
    finally:
        gate.set()
        await hass.async_block_till_done(wait_background_tasks=True)


async def test_bsb_recovers_entities_and_optional_discovery_on_first_valid_poll(hass, monkeypatch):
    api, _ = fake_gateway(monkeypatch, fail_first=True)
    entry, thermostat, _ = existing_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = entry.runtime_data
    assert coordinator.data is None
    assert api.async_get_bsb_points.await_count == 0
    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(thermostat).state in {"auto", "heat", "cool"}
    assert api.async_get_bsb_points.await_count > 0


async def test_unload_cancels_pending_snapshot_before_platforms(hass, monkeypatch):
    from homeassistant.components import switch

    can_poll = asyncio.Event()
    switch_unloading = asyncio.Event()
    can_unload = asyncio.Event()
    original = switch.async_unload_entry

    async def delayed_unload(hass, entry):
        switch_unloading.set()
        await can_unload.wait()
        return await original(hass, entry)

    monkeypatch.setattr(switch, "async_unload_entry", delayed_unload)
    _, polling = fake_gateway(monkeypatch, gate=can_poll)
    entry, thermostat, _ = existing_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await polling.wait()
    coordinator = entry.runtime_data
    unload = asyncio.create_task(hass.config_entries.async_unload(entry.entry_id))
    try:
        await asyncio.wait_for(switch_unloading.wait(), 5)
        assert coordinator.initial_refresh_task.cancelled()
        can_poll.set()
        assert coordinator.data is None
    finally:
        can_unload.set()
        assert await unload
    state = hass.states.get(thermostat)
    assert state is None or state.state == "unavailable"


async def test_refresh_button_stays_inside_server_backoff(hass, monkeypatch):
    api, _ = fake_gateway(monkeypatch)
    entry, _, _ = existing_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = entry.runtime_data
    last_success = coordinator.last_successful_update
    api.async_get_data.side_effect = ElcoResponseError("Rate limited", status=429, retry_after=300)
    await coordinator.async_refresh()
    refresh = er.async_get(hass).async_get_entity_id("button", DOMAIN, f"{GATEWAY_ID}_refresh")
    await hass.services.async_call("button", "press", {"entity_id": refresh}, blocking=True)
    assert api.async_get_data.await_count == 2
    assert coordinator.last_successful_update == last_success
