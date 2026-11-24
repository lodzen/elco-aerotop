# Heat pump dashboard

The [dashboard YAML](examples/heat-pump-dashboard.yaml) adapts the layout shown in the project
screenshot. It uses Home Assistant's built-in Sections, Thermostat, Tile, History graph, and
Button cards. The view puts heating and hot-water controls next to the tank-temperature
history, heating activity, gateway status, and manual refresh. It fits three sections across
on wide screens and stacks them on narrower screens.

## Add the dashboard

1. In Home Assistant, go to **Settings → Dashboards → Add dashboard** and create an empty
   dashboard. Open it and select **Edit dashboard**.
2. Open the three-dot menu and choose **Raw configuration editor**. Paste the
   [complete YAML](examples/heat-pump-dashboard.yaml) into the new dashboard. To add the
   view to a dashboard you already use, copy just the entry beginning with
   `- title: Heat pump` under that dashboard's existing `views:` list. Keep the indentation
   and choose a unique `path` for the view.
3. Replace every `elco_example_…` reference with the corresponding **Entity ID** from
   **Settings → Devices & services → ELCO Aerotop → Devices**. Replace repeated references
   in the graph and refresh action too. For a different heating zone, use that zone's
   thermostat and heat-request entities.
4. Remove any card or graph series your gateway lacks. Save and check that the temperature,
   activity, and last-update cards show values. Use the refresh button to request a poll.
   History graphs need Home Assistant Recorder history for the selected entities.

| Placeholder in the example | Entity to select on your ELCO device |
|---|---|
| `water_heater.elco_example_domestic_hot_water` | Domestic hot water control |
| `climate.elco_example_zone_1_thermostat` | Thermostat for the chosen heating zone |
| `sensor.elco_example_domestic_hot_water_temperature` | Measured domestic hot water temperature |
| `binary_sensor.elco_example_heat_pump_running` | Heat pump running |
| `binary_sensor.elco_example_zone_1_heat_request` | Heat request for the chosen zone |
| `binary_sensor.elco_example_gateway_online` | Gateway online |
| `sensor.elco_example_last_successful_update` | Last successful update |
| `button.elco_example_refresh` | Refresh |

Home Assistant may generate different entity IDs from the original language or device name,
and an ID can also be customized. Copy the actual ID from the entity settings. The
[entity reference](entities.md) lists the English names and the conditions for each entity.
On a cooling-capable system the request flag may cover both heating and cooling. The
thermostat's activity shows the current action when the controller provides it.

## Add target temperatures to the hot-water graph

On BSB, add the comfort and reduced hot-water number entities to the graph's `entities`
list if they are present and enabled. For Galevo, add **Domestic hot water target temperature**
to show the current mode's target. Use **Current** for the tank reading and **Target** for the
setpoint so users can tell them apart.

```yaml
              - entity: sensor.elco_example_domestic_hot_water_target_temperature
                name: Target
```

## Add Galevo mode and energy

Paste this optional section alongside the other entries under `sections:`. Keep the cards
for entities your device exposes. Add the JAZ tile after the sensor appears on your device.
The heating and hot-water totals in the example are for a rolling window of
12 complete months.

```yaml
      - type: grid
        cards:
          - type: heading
            heading: Mode and energy
            icon: mdi:lightning-bolt
          - type: entities
            entities:
              - entity: select.elco_example_plant_mode
              - entity: sensor.elco_example_heating_energy_last_12_complete_months
              - entity: sensor.elco_example_domestic_hot_water_energy_last_12_complete_months
          - type: tile
            name: Estimated JAZ · previous calendar year
            entity: sensor.elco_example_estimated_jaz_previous_calendar_year
```

## Add BSB maintenance and annual history

Add the section below if the device supplies these diagnostics. Open the annual performance
factor entity in the device's entity settings and enable it when you want it in the dashboard.
Enable the matching **Fixed day** sensor to see the date attached to that annual record.
Slot 1 is the newest record.

```yaml
      - type: grid
        cards:
          - type: heading
            heading: Maintenance and performance
            icon: mdi:wrench
          - type: entities
            entities:
              - entity: sensor.elco_example_maintenance_code_1
              - entity: sensor.elco_example_maintenance_code_2
              - entity: sensor.elco_example_maintenance_priority_1
              - entity: sensor.elco_example_maintenance_priority_2
              - entity: sensor.elco_example_annual_energy_record_1_yearly_performance_factor
```

For holiday controls and the BSB calendar, see the [holiday dashboard examples](holidays.md).
