# Victron Energy connector — design

Jira: [GUNDI-5379](https://allenai.atlassian.net/browse/GUNDI-5379) · Detailed discovery notes: [CONNECTORS-544](https://allenai.atlassian.net/browse/CONNECTORS-544)
First customers: Ol Pejeta Conservancy, CSL Zambia.

## Overview

Victron Energy powers remote field infrastructure (radio repeaters, gates, camps) with solar/battery systems monitored through the [VRM cloud platform](https://vrm.victronenergy.com). This connector pulls from the VRM API and delivers to EarthRanger via Gundi:

- **Sensor readings → observations.** Each VRM installation becomes a **stationary subject** (fixed, configured location). The latest readings (battery voltage, state of charge, current, temperature, solar charger status…) travel in the observation `additional` and show up in the ER subject popup.
- **Alarms → events.** VRM alarms (low battery, device errors, no-data/communication loss, geofence, user-defined threshold alarms) become ER events at the installation's location.

Target UX (validated with a live demo site): subject group "Victron Energy", one subject per installation, popup listing the sensor readings, ~15-minute refresh.

## VRM API summary

- Base: `https://vrmapi.victronenergy.com/v2` — [docs](https://vrm-api-docs.victronenergy.com/#/) (OpenAPI spec at `/docs/docs/openapi.yaml`)
- **Auth:** user-generated access token in header `x-authorization: Token <token>`. Username/password login is rejected for third-party API use (Bearer flow removed June 2026) — the connector must never ask for credentials.
- **Rate limit:** rolling window of 200 requests (~3 req/s sustained); 429 responses carry `Retry-After`.

Endpoints used:

| Endpoint | Purpose |
|---|---|
| `GET /users/me` | Validate token; obtain user id |
| `GET /users/{idUser}/installations?extended=1` | Site names, `last_timestamp`, active alarms (inline) |
| `GET /installations/{idSite}/diagnostics` | Latest value of every data attribute (sensor readings) |
| `GET /installations/{idSite}/alarm-log?start=&end=` | Historical alarm events with `started`/`cleared` |

### Why alarm-log rather than the diagnostics snapshot

The original prototype filtered alarm attributes out of `/diagnostics`. That is a point-in-time snapshot: an alarm that triggers and clears between two polls is never seen. `/alarm-log` keeps history with `started`/`cleared` timestamps, so a cursor persisted in the integration state guarantees no missed events, and lets us report both the alarm start and (optionally) its resolution.

## Actions

### `auth`
`AuthActionConfiguration` + `ExecutableActionMixin`. Single field: `token: pydantic.SecretStr` (password widget). Executes `GET /users/me`; stores the VRM user id in state for reuse.

### `pull_observations`
`PullActionConfiguration`, `@crontab_schedule` every 15 minutes. Per run:

1. `GET /users/{id}/installations?extended=1` — resolve site names, check freshness.
2. For each configured installation:
   - `GET /installations/{id}/diagnostics` → filter by the sensors-of-interest whitelist → build **one observation**: configured lat/lon, `recorded_at` = newest record timestamp, `additional` = `{description: formattedValue}`.
   - `GET /installations/{id}/alarm-log` from the state cursor → map new entries to **events** (`title` from `description` + `nameEnum`, event time = `started`, location = configured lat/lon) → advance cursor.
3. `send_observations_to_gundi(...)` + `send_events_to_gundi(...)`; return counts.

Requests are throttled/retried (`stamina` on 429/5xx honoring `Retry-After`) to respect the shared rate window — Ol Pejeta alone has 37 installations.

## Configuration

```python
class InstallationConfig(pydantic.BaseModel):
    installation_id: int          # visible in the VRM dashboard URL
    latitude: float               # ER subject location (VRM has no coordinates)
    longitude: float
    subject_name: Optional[str]   # defaults to the VRM site name

class PullObservationsConfig(PullActionConfiguration):
    installations: List[InstallationConfig]
    subject_subtype: str = "stationary-object"
    sensors_of_interest: List[str] = [...]   # default whitelist below
    alarm_lookback_days: int = 7             # first-run alarm-log window
```

EarthRanger credentials/base_url are **never** stored here — they live on the destination integration and are resolved at runtime.

Portal form mock:

![Config form mock](images/config-form-mock.svg)

## Field mapping strategy

VRM diagnostics records are self-describing — the connector needs no hardcoded name/unit table:

| Record field | Used as |
|---|---|
| `code` | selection key (whitelist) |
| `description` | field name shown in ER ("Voltage", "State of charge") |
| `formattedValue` | field value shown in ER ("53.26 V", "95.0 %") |
| `Device`, `instance` | disambiguation / dedup |
| `timestamp` | staleness filter, observation `recorded_at` |

Default whitelist (matches the validated demo UX):

| Device | Codes |
|---|---|
| Battery Monitor | `V`, `BT`, `CE`, `I`, `McV`, `mcV`, `SOC`, `TTG` |
| Solar Charger | `PVP`, `YT`, `ScS`, `ScERR` |
| System overview (fallback) | `bv`, `bc`, `bs`, `Pdc`, `dc` |

Verified against both customer accounts: attribute codes and `idDataAttribute` are **global VRM definitions** — identical everywhere (`V`=47, `SOC`=51, `PVP`=442…). Availability varies only by hardware (cell voltages/temperatures require a lithium BMS). Adding a code in the portal makes the new reading appear in ER with its proper name and unit — no code change, no deploy.

## Edge cases (all observed in real customer data)

- **Codes are case-sensitive:** `BT` (battery temperature) ≠ `bt`; `McV` (max cell voltage) ≠ `mcV` (min).
- **Stale per-record timestamps:** one live site returns solar-charger records frozen at **2021** (replaced hardware) while its system-overview records are current. Records older than a threshold (default: a few poll intervals) are dropped, otherwise ER would present years-old readings as current.
- **Dead sites:** several installations have sent nothing for years, some with permanent no-data alarms. Skipped without erroring; no observation emitted.
- **Missing attributes:** whitelisted codes absent on a site (no BMS, no temp sensor) are skipped silently — each subject shows its own subset.
- **Duplicate readings:** the same quantity exists on multiple devices (Battery Monitor `V` vs System overview `bv`). Battery Monitor wins; System overview is the fallback.
- **Rate limiting:** many-site accounts fan out to 1–2 requests per site per poll; throttle and honor `Retry-After` on 429.

## Validation so far

- Both customer accounts exercised live (read-only) — token auth, installation listing, diagnostics, alarm-log.
- The demo site's ER popup (8 fields) was reproduced exactly by the whitelist strategy.
- A real active alarm was read from the alarm-log (`Solar Charger — Error code — #38 PV Input shutdown`, started 2025-12-30, still active).
- Reproduce with [`local/vrm_readings.py`](../local/vrm_readings.py): `VRM_TOKEN=<token> python3 local/vrm_readings.py [idSite ...]`

## Open questions for the team

1. **One action or two?** Alarms could move to a separate `pull_events` action with its own schedule. Current lean: one action, both outputs — simpler config, one failure domain.
2. **Per-installation `subject_subtype`?** Currently one per connection. Needed if e.g. repeaters and solar plants should be different subject types.
3. **Alarms toggle?** A boolean to disable event emission for customers that only want readings.
4. **Alarm-cleared events:** emit a second "resolved" event when `cleared` is set, or only alarm-start events?
