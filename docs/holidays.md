# Holiday periods in Home Assistant

BSB and Galevo expose different holiday controls in Home Assistant. BSB shows returned holiday
periods in a read-only zone calendar and lets you start, extend, or cancel a current holiday.
Galevo lets you enter start and end dates for a plant-wide period and manage it with a switch.

## Galevo holiday controls

1. Set **Pending holiday start** and **Pending holiday end** on the device page.
2. Turn on **Holiday** to send the dates to the gateway.
3. Turn off **Holiday** to request cancellation of the controller's holiday.

The date fields are a local draft and reset on reload. The switch reflects the controller's
reported flag. Readback validates the end date when the gateway returns it. For a holiday
starting today, the switch can confirm a transition to active without a reported end date.
Some gateways may give too little information to confirm a future schedule. In that case,
Home Assistant reports the unconfirmed result and the official application can show the
schedule. The integration sends local date-times using the Home Assistant configuration.

## BSB holiday entities

| Entity | Purpose | Access |
|---|---|---|
| `calendar.<device>_zone_<zone>_holidays` | Shows every valid holiday returned for the zone | Read-only |
| `select.<device>_zone_1_holiday_operating_level` | Chooses Reduced or Frost protection for heating-circuit 1 holidays | Read/write |
| `date.<device>_zone_<zone>_holiday_until` | Starts a holiday now or changes the current holiday's inclusive final day | Read/write |
| `button.<device>_zone_<zone>_cancel_holiday` | Cancels the current holiday | Write |
| `climate.<device>_zone_<zone>_thermostat` | Shows the resulting zone mode; starting a holiday selects Automatic | Read/write |

The exact entity IDs contain the Home Assistant device name. Find them on the ELCO device page or
under **Developer Tools → States** rather than guessing them.

### Why the BSB calendar is read-only

Home Assistant's generic calendar editor supports fields such as arbitrary start and end times,
summary, description, location, recurrence, and independent event deletion. ELCO does not expose
that general event model. Its controller uses a fixed internal holiday table and applies heating
side effects that a generic calendar form cannot explain.

Making the calendar writable would therefore advertise operations that are not safely supported:

- selecting and reusing any of the controller's eight internal slots;
- creating arbitrary future start dates;
- resolving overlaps or choosing which slot to edit;
- storing calendar text, recurrence, or location fields; and
- explaining that starting a holiday changes the zone to Automatic while cancellation does not
  restore its previous mode.

The calendar's intention is consequently **visibility and automation input**. It lets dashboards
and automations see the controller's valid holiday periods without pretending that ELCO is a
general-purpose calendar. Writes use dedicated entities whose meaning and side effects are
explicit.

### BSB current-holiday workflow

The supported workflow mirrors Remocon's immediate holiday control:

1. Choose **Reduced** or **Frost protection** with the holiday operating-level select.
2. Set **Holiday until** to the final holiday day.
3. The integration starts a new period at the current Home Assistant local time, or changes the
   final day of the first usable current period.
4. The selected final day is inclusive. For example, selecting 10 September keeps the holiday in
   effect through 10 September.
5. Starting the holiday changes the zone to **Automatic**, which is the controller mode Remocon
   requires while the holiday period applies its Reduced or Frost-protection level.
6. **Cancel holiday** marks the current period deleted. It deliberately leaves the zone in its
   current mode and does not restore the mode that was active before the holiday.

ELCO uses an inclusive final day, while Home Assistant calendar events use an exclusive end. The
integration adds one day only when presenting a period in the calendar, so the displayed all-day
event covers the correct dates without changing the controller value.

Each command reads a fresh plant and zone snapshot, preserves all companion values required by
Remocon, sends one serialized write, reconciles ambiguous responses with a fresh read, and refreshes
Home Assistant immediately. It never blindly replays a write after a timeout or disconnect.

### Reduced versus Frost protection

The operating level decides which heating reference is used while a holiday is active:

- **Reduced** uses the heating circuit's reduced setpoint, BSB line 712. This keeps the building at
  the normal reduced level rather than the comfort level.
- **Frost protection** uses the frost-protection setpoint, BSB line 714. Heating remains available
  only as required to protect the building and installation; it is not the same as switching the
  heat pump off.

For example, a controller configured with a reduced setpoint of 18 °C and frost protection of 8 °C
would target the lower 8 °C reference during a Frost-protection holiday. These are controller
reference setpoints. On weather-compensated systems without a room sensor, they influence the
heating curve and are not guaranteed measured room temperatures.

Changing the operating level does not itself create a holiday. It configures how the controller
handles a holiday when one is active.

### BSB availability and the eight-slot boundary

Holiday entities are created only when the native zone response includes a holiday list. The list
may be empty. A temporary failure makes the corresponding values unavailable instead of inventing
periods or reusing stale data.

- **Cancel holiday** is available only while a usable, non-deleted, in-service period exists.
- **Holiday until** has no date value when there is no current period, so its state is `unknown`.
  Some Home Assistant frontend versions disable the native `date-set` tile feature in that empty
  state. The `date.set_value` action remains the integration's supported first-holiday command;
  after a current period exists, the tile shows and edits its inclusive final day normally.
- Creating a new current period appends a new record only when the returned table is safe to extend.
  The integration refuses to guess how an unverified out-of-service (`osv`) slot should be reused.

The BSB calendar displays all valid periods returned from the controller's eight records. Writes
apply to the first usable current holiday. Use Remocon to manage future starts and individual slots.

### Starting the first holiday from Developer Tools

If the native date tile is disabled because the entity is `unknown`, use **Developer Tools →
Actions**, choose `date.set_value`, target the zone's **Holiday until** entity, and provide an
inclusive final date:

```yaml
action: date.set_value
target:
  entity_id: date.your_elco_device_zone_1_holiday_until
data:
  date: "2026-09-10"
```

This is an action example, not configuration for `configuration.yaml`. Replace the sample entity ID
and date before running it. The same controller validation and write-safety policy applies.

## BSB dashboard example

The following view reproduces the test dashboard with built-in Home Assistant cards only. It uses
a Sections view, Tile cards, a Button card with confirmation, and the standard Calendar card. It
does not require HACS cards, templates, CSS, or custom JavaScript.

Create a new dashboard or view in the Home Assistant dashboard editor, then reproduce these cards
in the visual editor or use this as a raw-configuration model. Replace every
`your_elco_device` entity ID with the IDs from your installation.

```yaml
title: Holiday
path: holiday
icon: mdi:calendar-range
type: sections
max_columns: 2
sections:
  - type: grid
    cards:
      - type: heading
        heading: Current holiday
        heading_style: title
        icon: mdi:beach

      - type: markdown
        content: >-
          Choose the operating level, then choose the inclusive final holiday
          day. Starting a holiday changes the zone to Automatic. Cancellation
          leaves the operating mode unchanged.
        grid_options:
          columns: 12
          rows: auto

      - type: tile
        entity: select.your_elco_device_zone_1_holiday_operating_level
        name: Holiday operating level
        icon: mdi:thermostat-cog
        features:
          - type: select-options
            style: dropdown
        features_position: bottom
        grid_options:
          columns: 12
          rows: auto

      - type: tile
        entity: date.your_elco_device_zone_1_holiday_until
        name: Holiday until (inclusive)
        icon: mdi:calendar-end
        features:
          - type: date-set
        features_position: bottom
        grid_options:
          columns: 12
          rows: auto

      - type: button
        entity: button.your_elco_device_zone_1_cancel_holiday
        name: Cancel holiday
        icon: mdi:calendar-remove
        show_state: true
        tap_action:
          action: perform-action
          perform_action: button.press
          target:
            entity_id: button.your_elco_device_zone_1_cancel_holiday
          confirmation:
            text: >-
              Cancel the active ELCO holiday? The controller will remain in
              its current operating mode.
        grid_options:
          columns: 6
          rows: 3

      - type: tile
        entity: climate.your_elco_device_zone_1_thermostat
        name: Zone 1 operating mode
        icon: mdi:radiator
        features:
          - type: climate-hvac-modes
            style: dropdown
        features_position: bottom
        grid_options:
          columns: 6
          rows: auto

  - type: grid
    cards:
      - type: heading
        heading: Holiday calendar
        heading_style: title
        icon: mdi:calendar-month

      - type: calendar
        entities:
          - calendar.your_elco_device_zone_1_holidays
        initial_view: listWeek
        grid_options:
          columns: full
          rows: 8

      - type: markdown
        content: >-
          The calendar is the read-only controller view. Dedicated controls
          manage the immediate/current holiday; the full eight-slot planner is
          intentionally not exposed.
        grid_options:
          columns: 12
          rows: auto
```

The calendar card may show multiple periods because it reflects every valid record returned by the
controller. The date and cancel controls still address only the first usable current period.
