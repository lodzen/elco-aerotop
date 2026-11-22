"""Galevo beta protocol, authentication, and capability regressions."""

from __future__ import annotations

import logging

import pytest
from test_api import FakeResponse, FakeSession, login_responses

from custom_components.elco_aerotop import api as api_module
from custom_components.elco_aerotop.api import ElcoApiClient, ElcoResponseError
from custom_components.elco_aerotop.control_mapping import (
    dhw_operation,
    zone_hvac_mode,
    zone_hvac_modes,
    zone_mode_for_hvac,
)
from custom_components.elco_aerotop.models import PlantState, ZoneState


def _client(*responses, family=3):
    session = FakeSession(list(responses))
    client = ElcoApiClient(session, "user", "pass", "SYNTHETIC_GW", "https://example.test")
    client._authenticated = True
    client._mobile_token = "stale-token"
    client.last_features_response = {"data": {"plant": {"systemType": family}}}
    return client, session


async def test_galevo_renews_cookie_and_mobile_token_independently():
    client, session = _client(
        FakeResponse(status=401),
        *login_responses(),
        FakeResponse(json_data={"data": {"items": [{"id": "OutsideTemp", "value": 4}]}}),
        FakeResponse(status=401),
        FakeResponse(json_data={"token": "renewed-token"}),
        FakeResponse(json_data={"data": {"mod": {"value": 3}}}),
    )
    data, items = await client.async_get_galevo_data({}, [1])
    assert data.zones[1].mode.value == 1
    assert items["OutsideTemp:0"]["value"] == 4
    assert session.calls[-1][2]["headers"]["ar.authToken"] == "renewed-token"
    assert sum("/accounts/login" in url for _, url, _ in session.calls) == 1


@pytest.mark.parametrize(
    "payload",
    [{"success": False}, {"data": {}}, {"data": {"items": []}}, {"data": None}],
)
async def test_malformed_core_item_response_is_not_a_snapshot(payload):
    client, _ = _client(FakeResponse(json_data=payload))
    with pytest.raises(ElcoResponseError):
        await client.async_get_galevo_data({}, [1])


async def test_malformed_zone_cannot_publish_a_partial_snapshot():
    client, _ = _client(
        FakeResponse(json_data={"data": {"items": [{"id": "OutsideTemp", "value": 4}]}}),
        FakeResponse(json_data={"data": {"room": 20}}),
        FakeResponse(json_data={"data": {"message": "offline"}}),
    )
    with pytest.raises(ElcoResponseError, match="unexpected payload"):
        await client.async_get_galevo_data({}, [1, 2])
    assert client.last_system_items == {}


async def test_bsb_keeps_optional_probe_language_and_does_not_retry_auth():
    client, session = _client(FakeResponse(status=401), family=5)
    from custom_components.elco_aerotop.api import ElcoAuthenticationError

    with pytest.raises(ElcoAuthenticationError):
        await client.async_get_system_items({}, [1], culture="de")
    assert len(session.calls) == 1
    body = session.calls[0][2]["json"]
    assert body["culture"] == "en"
    assert {"id": "ZoneComfortCoolingTemp", "zn": 1} not in body["items"]


async def test_debug_log_does_not_include_gateway_or_response_details(caplog):
    client, _ = _client(FakeResponse(json_data={"data": {"plantName": "PRIVATE_SAMPLE_NAME"}}))
    with caplog.at_level(logging.DEBUG, logger=api_module.__name__):
        await client._request_payload("GET", "/sample/SYNTHETIC_GW")
    assert "<gateway>" in caplog.text
    assert "SYNTHETIC_GW" not in caplog.text
    assert "PRIVATE_SAMPLE_NAME" not in caplog.text


async def test_zone_off_and_current_mode_have_distinct_wire_meanings(monkeypatch):
    monkeypatch.setattr(api_module, "GALEVO_WRITES_ENABLED", True)
    client, session = _client(FakeResponse(json_data={"success": True}))
    items = {"ZoneMode:1": {"value": 0, "options": [0, 2, 3]}}
    zone = ZoneState.from_galevo(1, {}, items)
    assert zone_hvac_mode(zone.mode) == "off"
    assert zone_hvac_modes(zone.mode) == ("off", "auto", "heat")
    await client.async_set_zone_mode(
        PlantState.from_galevo({}), zone, mode=zone_mode_for_hvac(zone.mode, "heat")
    )
    assert session.calls[-1][2]["json"] == {"new": 2, "old": 0}


async def test_always_active_target_is_separate_from_scheduled_and_measured(monkeypatch):
    monkeypatch.setattr(api_module, "GALEVO_WRITES_ENABLED", True)
    client, session = _client(FakeResponse(json_data={"success": True}))
    plant = PlantState.from_galevo(
        {
            "DhwMode:0": {"value": 2, "options": [0, 1, 2], "optTexts": ["Aus", "Zeit", "Immer"]},
            "DhwTemp:0": {"value": 55},
            "DhwTimeProgComfortTemp:0": {"value": 48},
        }
    )
    assert plant.dhw_current_temperature is None
    assert plant.dhw_target_temperature.value == 55
    assert dhw_operation(plant.dhw_mode) == "performance"
    await client.async_set_galevo_dhw_settings(plant, comfort=54, reduced=None, mode=None)
    assert session.calls[-1][1].endswith("/dhwTemp")
    assert session.calls[-1][2]["json"] == {"new": 54, "old": 55}


async def test_dhw_mode_can_change_without_temperature_and_combined_changes_are_rejected(
    monkeypatch,
):
    monkeypatch.setattr(api_module, "GALEVO_WRITES_ENABLED", True)
    client, session = _client(FakeResponse(json_data={"success": True}))
    plant = PlantState.from_galevo({"DhwMode:0": {"value": 2, "options": [0, 2]}})
    with pytest.raises(ValueError, match="separately"):
        await client.async_set_galevo_dhw_settings(plant, comfort=50, reduced=None, mode=0)
    assert session.calls == []
    await client.async_set_galevo_dhw_settings(plant, comfort=None, reduced=None, mode=0)
    assert session.calls[-1][2]["json"] == {"new": 0, "old": 2}


async def test_individual_comfort_gate_still_allows_only_unchanged_economy(monkeypatch):
    monkeypatch.setattr(api_module, "GALEVO_WRITES_ENABLED", False)
    client, session = _client(FakeResponse(json_data={"success": True}))
    with pytest.raises(ValueError, match="disabled"):
        await client.async_set_galevo_dhw_timeprog_temperatures(
            comfort=52, economy=41, old_comfort=53, old_economy=40
        )
    assert session.calls == []
    await client.async_set_galevo_dhw_timeprog_temperatures(
        comfort=52, economy=40, old_comfort=53, old_economy=40
    )
    _, url, options = session.calls[-1]
    assert url.endswith("/dhwTimeProgTemperatures")
    assert options["json"] == {
        "new": {"comf": 52, "econ": 40},
        "old": {"comf": 53, "econ": 40},
    }


async def test_writable_boolean_data_item_keeps_the_gateway_wire_type():
    client, session = _client(FakeResponse(json_data={"success": True}))
    item = {"id": "IsQuite", "zone": 0, "value": False}
    await client.async_set_galevo_data_item(item, 1.0)
    _, _, options = session.calls[-1]
    sent = options["json"]["items"][0]
    assert sent["value"] is True
    assert sent["prevValue"] is False


async def test_galevo_cooling_setpoint_writes_only_the_changed_item():
    client, session = _client(FakeResponse(json_data={"success": True}))
    items = {
        "PlantMode:0": {"value": 3},
        "ZoneComfortCoolingTemp:1": {
            "id": "ZoneComfortCoolingTemp",
            "zone": 1,
            "value": 22,
            "min": 17,
            "max": 27,
        },
        "ZoneEconomyCoolingTemp:1": {
            "id": "ZoneEconomyCoolingTemp",
            "zone": 1,
            "value": 27,
            "min": 22,
            "max": 30,
        },
    }
    client.last_system_items = items
    zone = ZoneState.from_galevo(1, {}, items)
    assert zone.uses_cooling_setpoints and zone.cooling_active is None
    await client.async_set_zone_temperatures(zone, comfort=23, reduced=27, cooling=True)
    _, url, options = session.calls[-1]
    assert url.endswith("/dataItems/SYNTHETIC_GW/set")
    assert len(options["json"]["items"]) == 1
    assert options["json"]["items"][0] == {
        **items["ZoneComfortCoolingTemp:1"],
        "value": 23,
        "prevValue": 22,
    }


@pytest.mark.parametrize(
    ("overrides", "value"),
    [({"readOnly": True}, 21), ({"invalid": True}, 21), ({}, 21.25), ({}, 45), ({}, float("nan"))],
)
async def test_data_item_validation_prevents_requests(monkeypatch, overrides, value):
    monkeypatch.setattr(api_module, "GALEVO_WRITES_ENABLED", True)
    client, session = _client()
    item = {
        "id": "HeatingFlowTemp",
        "value": 20,
        "readOnly": False,
        "min": 10,
        "max": 35,
        "step": 0.5,
        **overrides,
    }
    with pytest.raises(ValueError):
        await client.async_set_galevo_data_item(item, value)
    assert session.calls == []
