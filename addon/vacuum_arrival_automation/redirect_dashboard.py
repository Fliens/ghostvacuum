#!/usr/bin/env python3
"""Ingress dashboard for the vacuum automation add-on."""

from __future__ import annotations

import json
import os
import html
from datetime import datetime, timedelta
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


OPTIONS_PATH = Path("/data/options.json")
SUPERVISOR_CORE_API = "http://supervisor/core/api"
EVENT_START_ROOM = "vacuum_automation_start_room"
EVENT_SET_ROOM_DUE = "vacuum_automation_set_room_due"
LOCAL_DEV_ENV = "VACUUM_DASHBOARD_DEV"
LOCAL_OPTIONS_ENV = "VACUUM_DASHBOARD_OPTIONS"
LOCAL_CONFIG_PATH = Path(__file__).with_name("config.yaml")
LOCAL_DASHBOARD_PATH = Path(__file__).with_name("dashboard.html")
MOCK_STATES: dict[str, dict] | None = None
ADDON_NAME = "Vacuum Arrival Automation"


def load_dashboard_template() -> str:
    if LOCAL_DASHBOARD_PATH.exists():
        return LOCAL_DASHBOARD_PATH.read_text(encoding="utf-8")
    return HTML


def local_dev_enabled() -> bool:
    return os.environ.get(LOCAL_DEV_ENV, "").lower() in {"1", "true", "yes", "on"}


def load_options() -> dict:
    if local_dev_enabled():
        override_path = os.environ.get(LOCAL_OPTIONS_ENV)
        if override_path:
            try:
                raw = Path(override_path).read_text()
                if override_path.endswith(".json"):
                    loaded = json.loads(raw)
                else:
                    loaded = yaml.safe_load(raw) or {}
                if isinstance(loaded, dict) and isinstance(loaded.get("options"), dict):
                    return loaded["options"]
                return loaded
            except Exception:
                return {}

        try:
            config = yaml.safe_load(LOCAL_CONFIG_PATH.read_text()) or {}
            return config.get("options") or {}
        except Exception:
            return {}

    try:
        return json.loads(OPTIONS_PATH.read_text())
    except Exception:
        return {}


def addon_name() -> str:
    if local_dev_enabled() and LOCAL_CONFIG_PATH.exists():
        try:
            config = yaml.safe_load(LOCAL_CONFIG_PATH.read_text()) or {}
            return str(config.get("name") or ADDON_NAME)
        except Exception:
            return ADDON_NAME
    return ADDON_NAME


def parse_rooms(raw_rooms: Any) -> List[dict]:
    if isinstance(raw_rooms, list):
        parsed = raw_rooms
    else:
        try:
            parsed = yaml.safe_load(raw_rooms or "[]")
        except Exception:
            parsed = []

    rooms = []
    for item in parsed or []:
        if not isinstance(item, dict):
            continue
        room_id = str(item.get("id") or item.get("slug") or "").strip()
        room_name = str(item.get("name") or room_id or "Room").strip()
        if not room_id:
            continue
        rooms.append(
            {
                "id": room_id,
                "name": room_name,
                "icon": str(item.get("icon") or "mdi:floor-plan"),
                "segment_id": item.get("segment_id"),
            }
        )
    return rooms


def api_request(path: str, method: str = "GET", payload: dict | None = None):
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(
        f"{SUPERVISOR_CORE_API}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read().decode("utf-8")
        return json.loads(raw) if raw else None


def state_for(entity_id: str):
    if local_dev_enabled():
        return mock_states().get(entity_id)

    try:
        return api_request(f"/states/{entity_id}")
    except Exception:
        return None


def all_states():
    if local_dev_enabled():
        return list(mock_states().values())

    try:
        states = api_request("/states")
        return states if isinstance(states, list) else []
    except Exception:
        return []


def service_call(domain: str, service: str, entity_id: str, data: dict | None = None):
    if local_dev_enabled():
        states = mock_states()
        entity = states.get(entity_id)
        if entity is None:
            raise ValueError(f"unknown mock entity: {entity_id}")
        if domain == "input_boolean" and service == "toggle":
            entity["state"] = "off" if entity.get("state") == "on" else "on"
            return [{"entity_id": entity_id, "state": entity["state"]}]
        if domain == "input_number" and service == "set_value":
            entity["state"] = str(data.get("value") if data else "")
            return [{"entity_id": entity_id, "state": entity["state"]}]
        if domain == "input_text" and service == "set_value":
            entity["state"] = str(data.get("value") if data else "")
            return [{"entity_id": entity_id, "state": entity["state"]}]
        raise ValueError(f"unsupported mock service: {domain}.{service}")

    payload = {"entity_id": entity_id}
    if data:
        payload.update(data)
    return api_request(
        f"/services/{domain}/{service}",
        method="POST",
        payload=payload,
    )


def fire_event(event_type: str, payload: dict | None = None):
    if local_dev_enabled():
        apply_mock_event(event_type, payload or {})
        return {"ok": True}

    return api_request(
        f"/events/{event_type}",
        method="POST",
        payload=payload or {},
    )


def helper_entities(options: dict, rooms: List[dict]) -> Dict[str, Any]:
    prefix = options.get("dashboard_prefix", "vacuum_automation")
    helper_prefix = options.get("helper_prefix", "vacuum_automation")
    room_entities = []
    for room in rooms:
        room_id = room["id"]
        room_entities.append(
            {
                "id": room_id,
                "name": room["name"],
                "icon": room["icon"],
                "enabled": f"input_boolean.{helper_prefix}_{room_id}_enabled",
                "weight": f"input_number.{helper_prefix}_{room_id}_weight",
                "interval_h": f"input_number.{helper_prefix}_{room_id}_interval_h",
                "duration_min": f"input_number.{helper_prefix}_{room_id}_duration_min",
            }
        )

    return {
        "sensors": {
            "status": f"sensor.{prefix}_status",
            "active_room": f"sensor.{prefix}_active_room",
            "next_room": f"sensor.{prefix}_next_room",
            "travel_time": f"sensor.{prefix}_travel_time",
            "return_window": f"sensor.{prefix}_return_window",
            "distance_to_home": f"sensor.{prefix}_distance_to_home",
            "weekly_runs": f"sensor.{prefix}_weekly_runs",
            "weekly_minutes": f"sensor.{prefix}_weekly_minutes",
            "history": f"sensor.{prefix}_history",
        },
        "global": {
            "enabled": options.get("enabled_entity", f"input_boolean.{helper_prefix}_enabled"),
            "learning": options.get(
                "learning_enabled_entity",
                f"input_boolean.{helper_prefix}_learning_enabled",
            ),
            "travel_logic": options.get(
                "travel_mode_enabled_entity",
                f"input_boolean.{helper_prefix}_travel_mode_enabled",
            ),
            "start_push": options.get(
                "start_notifications_enabled_entity",
                f"input_boolean.{helper_prefix}_start_notifications_enabled",
            ),
            "return_push": options.get(
                "return_summary_enabled_entity",
                f"input_boolean.{helper_prefix}_return_summary_enabled",
            ),
            "custom_home": options.get(
                "home_override_enabled_entity",
                f"input_boolean.{helper_prefix}_home_override_enabled",
            ),
            "one_time_room_override": options.get(
                "one_time_room_override_entity",
                f"input_text.{helper_prefix}_one_time_room_override",
            ),
            "start_hour": options.get(
                "start_hour_entity", f"input_number.{helper_prefix}_start_hour"
            ),
            "end_hour": options.get(
                "end_hour_entity", f"input_number.{helper_prefix}_end_hour"
            ),
            "return_buffer": options.get(
                "return_buffer_entity",
                f"input_number.{helper_prefix}_return_buffer",
            ),
            "fallback_speed": options.get(
                "fallback_speed_entity",
                f"input_number.{helper_prefix}_fallback_speed",
            ),
            "default_travel_time": options.get(
                "default_travel_time_entity",
                f"input_number.{helper_prefix}_default_travel_time",
            ),
            "home_latitude": options.get(
                "home_latitude_entity",
                f"input_number.{helper_prefix}_home_latitude",
            ),
            "home_longitude": options.get(
                "home_longitude_entity",
                f"input_number.{helper_prefix}_home_longitude",
            ),
            "travel_pause_radius": options.get(
                "travel_pause_radius_entity",
                f"input_number.{helper_prefix}_travel_pause_radius",
            ),
            "max_distance_km": options.get(
                "max_distance_km_entity",
                f"input_number.{helper_prefix}_max_distance_km",
            ),
        },
        "rooms": room_entities,
    }


def mock_state(entity_id: str, state: Any, attributes: dict | None = None) -> dict:
    return {
        "entity_id": entity_id,
        "state": str(state),
        "attributes": attributes or {},
    }


def mock_states() -> dict[str, dict]:
    global MOCK_STATES
    if MOCK_STATES is not None:
        return MOCK_STATES

    options = load_options()
    rooms = parse_rooms(options.get("rooms"))
    entity_map = helper_entities(options, rooms)
    states: dict[str, dict] = {}

    for key, entity_id in entity_map["global"].items():
        if key in {"enabled", "learning", "travel_logic", "return_push"}:
            value = "on"
        elif key in {"start_push", "custom_home"}:
            value = "off"
        elif key == "start_hour":
            value = options.get("start_hour", 8)
        elif key == "end_hour":
            value = options.get("end_hour", 22)
        elif key == "return_buffer":
            value = options.get("return_buffer_min", 5)
        elif key == "fallback_speed":
            value = options.get("fallback_speed_kmh", 30)
        elif key == "default_travel_time":
            value = options.get("default_travel_time_min", 60)
        elif key == "home_latitude":
            value = options.get("home_latitude", 52.52)
        elif key == "home_longitude":
            value = options.get("home_longitude", 13.405)
        elif key == "travel_pause_radius":
            value = options.get("travel_pause_radius_km", 25)
        elif key == "max_distance_km":
            value = options.get("max_distance_km", 0)
        else:
            value = "unknown"
        states[entity_id] = mock_state(entity_id, value)
    states[entity_map["global"]["one_time_room_override"]] = mock_state(
        entity_map["global"]["one_time_room_override"], ""
    )

    room_queue = []
    room_stats = []
    for index, room in enumerate(entity_map["rooms"], start=1):
        room_id = room["id"]
        enabled = "off" if index == len(entity_map["rooms"]) else "on"
        configured_duration = 10 + index * 4
        learned_duration = configured_duration + (index % 3) - 1
        room_queue.append(
            {
                "room": room["name"],
                "room_key": room_id,
                "enabled": enabled == "on",
                "priority": round(1.8 - index * 0.18, 2),
                "interval_h": 24 + index * 12,
                "configured_duration_min": configured_duration,
                "learned_duration_min": learned_duration,
                "effective_duration_min": learned_duration,
                "fits_now": learned_duration <= 34,
                "forecast_score": round(0.86 - index * 0.08, 2),
            }
        )
        room_stats.append(
            {
                "room_key": room_id,
                "room": room["name"],
                "configured_duration_min": configured_duration,
                "learned_duration_min": learned_duration,
                "completed_runs": 3 + index,
                "average_actual_duration_min": learned_duration + 1,
                "last_cleaned": f"2026-04-{26 - index:02d}T10:20:00",
            }
        )
        states[room["enabled"]] = mock_state(room["enabled"], enabled)
        states[room["weight"]] = mock_state(room["weight"], round(1 + index * 0.15, 2))
        states[room["interval_h"]] = mock_state(room["interval_h"], 24 + index * 12)
        states[room["duration_min"]] = mock_state(room["duration_min"], configured_duration)

    sensors = entity_map["sensors"]
    weekly_stats = [
        {"week": "2026-W17", "runs": 5, "minutes": 82, "rooms": ["Bad", "Kueche"]},
        {"week": "2026-W16", "runs": 4, "minutes": 69, "rooms": ["Wohnzimmer"]},
        {"week": "2026-W15", "runs": 6, "minutes": 95, "rooms": ["Bad", "Schlafzimmer"]},
    ]
    recent_runs = [
        {
            "finished_at": "2026-04-26 12:42",
            "room": "Bad",
            "outcome": "completed",
            "actual_duration_min": 14,
        },
        {
            "finished_at": "2026-04-25 18:10",
            "room": "Kueche",
            "outcome": "completed",
            "actual_duration_min": 13,
        },
        {
            "finished_at": "2026-04-24 09:31",
            "room": "Wohnzimmer",
            "outcome": "stopped",
            "actual_duration_min": 8,
        },
    ]
    status_attrs = {
        "reason": "everyone away, enough travel window",
        "presence_summary": [
            {
                "entity_id": "person.resident_1",
                "state": "not_home",
                "distance_km": 11.8,
                "travel_time_min": 42,
            },
            {
                "entity_id": "person.resident_2",
                "state": "not_home",
                "distance_km": 32.4,
                "travel_time_min": 97,
            },
        ],
        "cleaned_during_absence": ["Bad", "Kueche"],
        "away_since": (datetime.now() - timedelta(hours=4)).isoformat(timespec="minutes"),
        "travel_mode_reason": "inside local radius",
        "distance_km": 11.8,
        "travel_pause_radius_km": options.get("travel_pause_radius_km", 25),
        "max_distance_km": options.get("max_distance_km", 0),
        "travel_home_zone": options.get("home_zone", "zone.home"),
        "travel_home_zone_distance_km": 11.8,
        "time_window": {"start_hour": 8, "end_hour": 22},
        "room_queue": room_queue,
        "room_stats": room_stats,
        "recent_runs": recent_runs,
        "weekly_stats": weekly_stats,
        "history_entries": 38,
    }
    states[sensors["status"]] = mock_state(sensors["status"], "Ready", status_attrs)
    states[sensors["active_room"]] = mock_state(
        sensors["active_room"],
        "Bad",
        {"remaining_min": 9, "planned_duration_min": 14, "travel_time_min_at_start": 42},
    )
    states[sensors["next_room"]] = mock_state(sensors["next_room"], "Kueche")
    states[sensors["travel_time"]] = mock_state(sensors["travel_time"], 42)
    states[sensors["return_window"]] = mock_state(sensors["return_window"], 37)
    states[sensors["distance_to_home"]] = mock_state(sensors["distance_to_home"], 11.8)
    states[sensors["weekly_runs"]] = mock_state(sensors["weekly_runs"], 5)
    states[sensors["weekly_minutes"]] = mock_state(sensors["weekly_minutes"], 82)
    states[sensors["history"]] = mock_state(
        sensors["history"],
        "38 entries",
        {"weekly_stats": weekly_stats, "recent_runs": recent_runs},
    )
    vacuum_entity = options.get("vacuum_entity", "vacuum.robot_vacuum")
    states[vacuum_entity] = mock_state(
        vacuum_entity,
        "cleaning",
        {
            "battery_level": 78,
            "friendly_name": "Robot Vacuum",
            "bin_full": False,
            "water_tank_empty": False,
            "mop_attached": True,
        },
    )
    vacuum_base = vacuum_base_id(vacuum_entity)
    states[f"sensor.{vacuum_base}_battery_level"] = mock_state(
        f"sensor.{vacuum_base}_battery_level",
        78,
        {"unit_of_measurement": "%"},
    )
    states[f"binary_sensor.{vacuum_base}_charging_state"] = mock_state(
        f"binary_sensor.{vacuum_base}_charging_state",
        "off",
    )
    states[f"sensor.{vacuum_base}_error"] = mock_state(
        f"sensor.{vacuum_base}_error",
        "no_error",
        {"value": "no_error"},
    )
    states[f"sensor.{vacuum_base}_water_tank"] = mock_state(
        f"sensor.{vacuum_base}_water_tank",
        "mop_installed",
    )
    states[f"sensor.{vacuum_base}_low_water_warning"] = mock_state(
        f"sensor.{vacuum_base}_low_water_warning",
        "no_warning",
    )
    states[f"sensor.{vacuum_base}_mop_pad"] = mock_state(
        f"sensor.{vacuum_base}_mop_pad",
        "installed",
    )
    states[f"sensor.{vacuum_base}_self_wash_base_status"] = mock_state(
        f"sensor.{vacuum_base}_self_wash_base_status",
        "idle",
    )
    states[f"sensor.{vacuum_base}_clean_water_tank_status"] = mock_state(
        f"sensor.{vacuum_base}_clean_water_tank_status",
        "installed",
    )
    states[f"sensor.{vacuum_base}_dirty_water_tank_status"] = mock_state(
        f"sensor.{vacuum_base}_dirty_water_tank_status",
        "installed",
    )
    states[f"sensor.{vacuum_base}_dust_bag_status"] = mock_state(
        f"sensor.{vacuum_base}_dust_bag_status",
        "installed",
    )
    states[f"sensor.{vacuum_base}_detergent_status"] = mock_state(
        f"sensor.{vacuum_base}_detergent_status",
        "low_detergent",
    )

    MOCK_STATES = states
    return MOCK_STATES


def _mock_status_sensor(states: dict[str, dict]) -> Optional[dict]:
    return next(
        (state for entity_id, state in states.items() if entity_id.endswith("_status")),
        None,
    )


def _mock_active_room_sensor(states: dict[str, dict]) -> Optional[dict]:
    return next(
        (state for entity_id, state in states.items() if entity_id.endswith("_active_room")),
        None,
    )


def apply_mock_event(event_type: str, payload: dict):
    states = mock_states()
    room_key = str(payload.get("room_key") or "").strip()
    status_sensor = _mock_status_sensor(states)
    active_room_sensor = _mock_active_room_sensor(states)
    if not status_sensor or not active_room_sensor:
        return

    status_attrs = status_sensor.setdefault("attributes", {})
    queue = status_attrs.get("room_queue", []) or []
    room_stats = status_attrs.get("room_stats", []) or []
    room_item = next((item for item in queue if item.get("room_key") == room_key), None)
    stats_item = next((item for item in room_stats if item.get("room_key") == room_key), None)

    if event_type == EVENT_SET_ROOM_DUE and room_item:
        due = bool(payload.get("due"))
        room_item["forecast_score"] = 1.1 if due else 0.7
        room_item["score"] = 1.0 if due else 0.5
        room_item["priority"] = round((room_item.get("priority") or 1.0) + (0.4 if due else -0.2), 2)
        if stats_item:
            stats_item["last_cleaned"] = "2026-04-20T10:20:00" if due else "2026-04-26T10:20:00"
        return

    if event_type == EVENT_START_ROOM and room_item and active_room_sensor:
        active_room_sensor["state"] = str(room_item.get("room") or room_key)
        active_room_sensor["attributes"] = {
            "remaining_min": room_item.get("effective_duration_min", 10),
            "planned_duration_min": room_item.get("effective_duration_min", 10),
            "travel_time_min_at_start": 42,
        }


def collect_states(entity_map: Dict[str, Any]) -> Dict[str, Any]:
    data: Dict[str, Any] = {"sensors": {}, "global": {}, "rooms": []}
    for key, entity_id in entity_map["sensors"].items():
        data["sensors"][key] = state_for(entity_id)
    for key, entity_id in entity_map["global"].items():
        data["global"][key] = state_for(entity_id)
    for room in entity_map["rooms"]:
        room_state = dict(room)
        for key in ["enabled", "weight", "interval_h", "duration_min"]:
            room_state[f"{key}_state"] = state_for(room[key])
        data["rooms"].append(room_state)
    return data


ROBOT_SIGNAL_SUFFIXES = {
    "battery_level": "sensor.{base}_battery_level",
    "charging_state": "binary_sensor.{base}_charging_state",
    "state": "sensor.{base}_state",
    "status": "sensor.{base}_status",
    "task_status": "sensor.{base}_task_status",
    "error": "sensor.{base}_error",
    "water_tank": "sensor.{base}_water_tank",
    "low_water_warning": "sensor.{base}_low_water_warning",
    "mop_pad": "sensor.{base}_mop_pad",
    "dust_collection": "sensor.{base}_dust_collection",
    "auto_empty_status": "sensor.{base}_auto_empty_status",
    "self_wash_base_status": "sensor.{base}_self_wash_base_status",
    "clean_water_tank_status": "sensor.{base}_clean_water_tank_status",
    "dirty_water_tank_status": "sensor.{base}_dirty_water_tank_status",
    "dust_bag_status": "sensor.{base}_dust_bag_status",
    "detergent_status": "sensor.{base}_detergent_status",
    "station_drainage_status": "sensor.{base}_station_drainage_status",
    "dust_bag_drying_status": "sensor.{base}_dust_bag_drying_status",
    "drainage_status": "sensor.{base}_drainage_status",
    "drying_left": "sensor.{base}_drying_left",
}


def vacuum_base_id(vacuum_entity: str) -> str:
    if "." not in str(vacuum_entity):
        return str(vacuum_entity or "").strip()
    return str(vacuum_entity).split(".", 1)[1].strip()


def configured_robot_entities(options: dict) -> dict:
    configured = options.get("robot_status_entities") or options.get("dreame_status_entities") or {}
    return configured if isinstance(configured, dict) else {}


def collect_robot_states(options: dict, vacuum_state: Any) -> dict:
    vacuum_entity = str(options.get("vacuum_entity") or "")
    base = vacuum_base_id(vacuum_entity)
    configured = configured_robot_entities(options)
    signals = {}

    for key, template in ROBOT_SIGNAL_SUFFIXES.items():
        entity_id = str(configured.get(key) or template.format(base=base)).strip()
        signals[key] = state_for(entity_id) if entity_id else None

    # Some Home Assistant setups rename the Dreame entities. If the inferred
    # names miss, scan entities with the same object-id prefix as a soft fallback.
    missing = {key for key, value in signals.items() if not isinstance(value, dict)}
    if missing and base:
        for state in all_states():
            entity_id = str(state.get("entity_id") or "")
            if not entity_id.startswith(("sensor.", "binary_sensor.")):
                continue
            object_id = entity_id.split(".", 1)[1]
            if not object_id.startswith(f"{base}_"):
                continue
            suffix = object_id[len(base) + 1 :]
            if suffix in missing:
                signals[suffix] = state
                missing.remove(suffix)
                if not missing:
                    break

    return {
        "entity_id": vacuum_entity,
        "base": base,
        "vacuum": vacuum_state,
        "signals": signals,
    }


def build_summary() -> dict:
    options = load_options()
    rooms = parse_rooms(options.get("rooms"))
    entity_map = helper_entities(options, rooms)
    states = collect_states(entity_map)
    status_state = states["sensors"].get("status") or {}
    history_state = states["sensors"].get("history") or {}
    vacuum_state = state_for(options.get("vacuum_entity", ""))
    robot_states = collect_robot_states(options, vacuum_state)
    status_attrs = status_state.get("attributes", {}) if isinstance(status_state, dict) else {}
    history_attrs = history_state.get("attributes", {}) if isinstance(history_state, dict) else {}
    return {
        "options": {
            "vacuum_entity": options.get("vacuum_entity"),
            "notify_service": options.get("notify_service"),
            "presence_entities": options.get("presence_entities"),
            "person_entity": options.get("person_entity"),
            "travel_person_entity": options.get("travel_person_entity"),
            "waze_entity": options.get("waze_entity"),
            "distance_entity": options.get("distance_entity"),
            "home_zone": options.get("home_zone"),
            "travel_pause_zone": options.get("travel_pause_zone"),
            "travel_pause_after_hours": options.get("travel_pause_after_hours"),
            "history_weeks": options.get("history_weeks"),
            "learning_window": options.get("learning_window"),
        },
        "entities": entity_map,
        "states": {**states, "vacuum": vacuum_state, "robot": robot_states},
        "status": {
            "reason": status_attrs.get("reason"),
            "presence_summary": status_attrs.get("presence_summary", []),
            "cleaned_during_absence": status_attrs.get("cleaned_during_absence", []),
            "away_since": status_attrs.get("away_since"),
            "travel_mode_active": status_attrs.get("travel_mode_active"),
            "travel_mode_enabled": status_attrs.get("travel_mode_enabled"),
            "travel_mode_reason": status_attrs.get("travel_mode_reason"),
            "distance_km": status_attrs.get("distance_km"),
            "travel_pause_radius_km": status_attrs.get("travel_pause_radius_km"),
            "max_distance_km": status_attrs.get("max_distance_km"),
            "travel_home_zone": status_attrs.get("travel_home_zone"),
            "travel_home_zone_distance_km": status_attrs.get("travel_home_zone_distance_km"),
            "time_window": status_attrs.get("time_window", {}),
            "room_queue": status_attrs.get("room_queue", []),
            "room_stats": status_attrs.get("room_stats", []),
            "recent_runs": status_attrs.get("recent_runs", []),
            "weekly_stats": status_attrs.get("weekly_stats", []),
            "history_entries": status_attrs.get("history_entries", 0),
        },
        "history": {
            "weekly_stats": history_attrs.get("weekly_stats", []),
            "recent_runs": history_attrs.get("recent_runs", []),
        },
    }


def escape(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def state_value(state: Any, fallback: str = "-") -> str:
    if not isinstance(state, dict):
        return fallback
    value = state.get("state")
    if value in {None, "", "unknown", "unavailable"}:
        return fallback
    return str(value)


def state_attrs(state: Any) -> dict:
    if isinstance(state, dict) and isinstance(state.get("attributes"), dict):
        return state["attributes"]
    return {}


def bool_on(state: Any) -> bool:
    return state_value(state, "off").lower() in {"on", "true", "yes", "1"}


def number_value(value: Any, fallback: Optional[float] = None) -> Optional[float]:
    try:
        if value is None:
            return fallback
        if isinstance(value, str) and value.lower() in {"", "unknown", "unavailable"}:
            return fallback
        return float(value)
    except (TypeError, ValueError):
        return fallback


def format_number(value: Any, suffix: str = "", fallback: str = "-") -> str:
    number = number_value(value)
    if number is None:
        return fallback
    if number.is_integer():
        return f"{int(number)}{suffix}"
    return f"{number:.1f}{suffix}"


def person_name(person: dict) -> str:
    raw = str(person.get("name") or person.get("entity_id") or "Person")
    if "." in raw:
        raw = raw.split(".", 1)[1]
    return raw.replace("_", " ").title()


def person_is_home(person: dict) -> bool:
    return str(person.get("state", "")).lower() in {"home", "zuhause"}


def person_travel_time(person: dict) -> Optional[float]:
    for key in ["travel_time_min", "travel_min", "duration_min"]:
        value = number_value(person.get(key))
        if value is not None:
            return value
    return None


def person_distance(person: dict) -> Optional[float]:
    for key in ["distance_km", "distance"]:
        value = number_value(person.get(key))
        if value is not None:
            return value
    return None


def format_last_cleaned(value: Any) -> str:
    if not value:
        return "noch nie"
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        now = datetime.now(parsed.tzinfo) if parsed.tzinfo else datetime.now()
        hours = max(0, round((now - parsed).total_seconds() / 3600))
        if hours < 24:
            return f"vor {hours} h"
        return f"vor {round(hours / 24)} d"
    except ValueError:
        return text


def parse_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def format_duration_hours(hours: float) -> str:
    if hours <= 0:
        return "jetzt"
    if hours < 1:
        return f"{max(1, round(hours * 60))} min"
    if hours < 24:
        rounded = round(hours, 1)
        return f"{int(round(rounded))} h" if rounded.is_integer() else f"{rounded:.1f} h"
    days = hours / 24
    return f"{days:.1f} d" if days % 1 else f"{int(days)} d"


def travel_reason_label(reason: Any) -> str:
    reason_text = str(reason or "").lower()
    if not reason_text:
        return "Kein Pausengrund aktiv"
    if "max" in reason_text or "distance" in reason_text or "entfernung" in reason_text:
        return "Maximale Entfernung überschritten"
    if "long" in reason_text or "pause" in reason_text or "radius" in reason_text:
        return "Lange Abwesenheit außerhalb des Radius"
    return str(reason)

    weekdays = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
    return f"{weekdays[parsed.weekday()]}, {parsed.strftime('%d.%m.%Y')}"


def room_due_text(room: dict) -> str:
    forecast = number_value(room.get("forecast_score"), 0) or 0
    score = number_value(room.get("score"), 0) or 0
    if forecast >= 1:
        return "heute dran"
    if score >= 0.8:
        return "bald dran"
    return "noch nicht fällig"


def room_reason_text(room: dict) -> str:
    bits = [room_due_text(room)]
    priority = number_value(room.get("priority"))
    if priority is not None:
        bits.append(f"Priorität {priority:.2f}")
    if room.get("fits_now"):
        bits.append("passt ins Fenster")
    else:
        bits.append("zu lang für jetzt")
    return " · ".join(bits)


def localize_vacuum_state(value: str) -> str:
    labels = {
        "cleaning": "Reinigt",
        "docked": "Angedockt",
        "returning": "Kehrt zurück",
        "paused": "Pausiert",
        "idle": "Bereit",
        "unavailable": "Nicht erreichbar",
        "unknown": "Unbekannt",
    }
    return labels.get(value.lower(), value.title())


def normalized_signal_state(state: Any) -> Optional[str]:
    value = state_value(state, "")
    if not value:
        return None
    return value.lower()


def signal_attr(state: Any, key: str) -> Any:
    return state_attrs(state).get(key)


def first_number(*values: Any) -> Optional[float]:
    for value in values:
        number = number_value(value)
        if number is not None:
            return number
    return None


def value_from_signals(signals: dict, key: str) -> Optional[str]:
    return normalized_signal_state(signals.get(key))


def localize_status_value(value: str) -> str:
    labels = {
        "active": "Aktiv",
        "adding_water": "Füllt Wasser",
        "available": "Verfügbar",
        "check": "Prüfen",
        "charging": "Lädt",
        "charging_completed": "Voll geladen",
        "clean_add_water": "Wasser nachfüllen",
        "cleaning": "Reinigt",
        "disabled": "Aus",
        "docked": "Angedockt",
        "draining": "Entleert",
        "draining_failed": "Entleerung fehlgeschlagen",
        "draining_successful": "Entleert",
        "drying": "Trocknet",
        "error": "Fehler",
        "idle": "Ruhig",
        "installed": "OK",
        "low_detergent": "Niedrig",
        "low_water": "Niedrig",
        "mop_in_station": "Mop in Station",
        "mop_installed": "Mop eingesetzt",
        "no_error": "OK",
        "no_warning": "OK",
        "no_water_for_clean": "Zu wenig Wasser",
        "no_water_left": "Leer",
        "no_water_left_after_clean": "Leer",
        "not_available": "Nicht vorhanden",
        "not_installed": "Fehlt",
        "not_installed_or_full": "Fehlt oder voll",
        "not_performed": "Nicht geleert",
        "paused": "Pausiert",
        "returning": "Kehrt zurück",
        "returning_to_wash": "Zur Wäsche",
        "tank_not_installed": "Tank fehlt",
        "unavailable": "Nicht erreichbar",
        "unknown": "Unbekannt",
        "washing": "Wäscht Mop",
    }
    return labels.get(str(value).lower(), str(value).replace("_", " ").title())


def status_class(value: str, warning_values: set[str] | None = None, error_values: set[str] | None = None) -> str:
    normalized = str(value or "").lower()
    if error_values and normalized in error_values:
        return "error"
    if warning_values and normalized in warning_values:
        return "warning"
    if normalized in {"unknown", "unavailable", "not_available"}:
        return "unavailable"
    return ""


def hero_metric(icon: str, label: str, value: str, css_class: str = "", percent: Optional[float] = None) -> dict:
    return {
        "icon": icon,
        "label": label,
        "value": value,
        "class": css_class,
        "percent": None if percent is None else max(0, min(100, percent)),
    }


def render_hero_metric_rows(metrics: List[dict], empty_text: str) -> str:
    if not metrics:
        return f'<div class="hero-level-empty">{escape(empty_text)}</div>'

    rows = []
    for metric in metrics:
        bar = ""
        percent = metric.get("percent")
        if percent is not None:
            bar = (
                '<div class="hero-level-bar">'
                f'<div class="hero-level-fill {escape(metric.get("class", ""))}" style="width: {escape(format_number(percent, "%", "0%"))}"></div>'
                "</div>"
            )
        rows.append(
            f"""
            <div class="hero-level-item">
              <span class="hero-level-icon">{escape(metric.get("icon", ""))}</span>
              <span class="hero-level-label">{escape(metric.get("label", ""))}</span>
              {bar}
              <span class="hero-level-value {escape(metric.get("class", ""))}">{escape(metric.get("value", ""))}</span>
            </div>
            """
        )
    return "".join(rows)


def build_hero_metrics(summary: dict) -> tuple[List[dict], List[dict]]:
    robot = summary.get("states", {}).get("robot", {}) or {}
    signals = robot.get("signals", {}) or {}
    vacuum = robot.get("vacuum") or summary.get("states", {}).get("vacuum") or {}
    attrs = state_attrs(vacuum)

    robot_metrics = []
    station_metrics = []

    battery = first_number(
        attrs.get("battery_level"),
        attrs.get("battery"),
        state_value(signals.get("battery_level"), ""),
    )
    if battery is not None:
        battery_class = "error" if battery < 20 else "warning" if battery < 40 else ""
        charging = normalized_signal_state(signals.get("charging_state")) in {"on", "true"} or bool(
            attrs.get("battery_charging") or attrs.get("charging")
        )
        battery_text = f"{int(battery)}% lädt" if charging else f"{int(battery)}%"
        robot_metrics.append(hero_metric("⚡" if charging else "🔋", "Akku", battery_text, battery_class, battery))

    error_state = value_from_signals(signals, "error")
    error_attr = signal_attr(signals.get("error"), "value")
    error_value = str(error_attr or error_state or attrs.get("error") or "").lower()

    bin_full = bool(attrs.get("bin_full") or attrs.get("dustbin_full")) or error_value in {"bin_full", "box_full"}
    bin_almost_full = bool(attrs.get("bin_almost_full"))
    if bin_full or bin_almost_full or error_state in {"no_error", "unknown"} or "bin_full" in attrs or "dustbin_full" in attrs:
        if bin_full:
            robot_metrics.append(hero_metric("🗑️", "Staubbehälter", "Voll", "error"))
        elif bin_almost_full:
            robot_metrics.append(hero_metric("🗑️", "Staubbehälter", "Fast voll", "warning"))
        else:
            robot_metrics.append(hero_metric("🗑️", "Staubbehälter", "OK"))

    water_state = value_from_signals(signals, "water_tank")
    low_water_state = value_from_signals(signals, "low_water_warning")
    water_empty = bool(attrs.get("water_tank_empty")) or error_value in {
        "water_box_empty",
        "water_tank_dry",
        "onboard_water_tank_empty",
    }
    water_low = bool(attrs.get("water_tank_low")) or low_water_state in {"low_water", "no_water_for_clean"}
    if water_state or low_water_state or water_empty or water_low or "water_tank_empty" in attrs:
        if water_empty or low_water_state in {"no_water_left", "no_water_left_after_clean"}:
            robot_metrics.append(hero_metric("💧", "Wassertank", "Leer", "error"))
        elif water_low:
            robot_metrics.append(hero_metric("💧", "Wassertank", localize_status_value(low_water_state or "low_water"), "warning"))
        elif water_state in {"not_installed", "tank_not_installed"}:
            robot_metrics.append(hero_metric("💧", "Wassertank", "Fehlt", "warning"))
        else:
            robot_metrics.append(hero_metric("💧", "Wassertank", localize_status_value(low_water_state or water_state or "installed")))

    mop_state = value_from_signals(signals, "mop_pad")
    if mop_state:
        robot_metrics.append(hero_metric("🧽", "Mop", localize_status_value(mop_state), status_class(mop_state, {"unknown"})))
    elif "mop_attached" in attrs:
        robot_metrics.append(hero_metric("🧽", "Mop", "Eingesetzt" if attrs.get("mop_attached") else "Nicht eingesetzt"))

    station_state = value_from_signals(signals, "self_wash_base_status")
    if station_state and station_state not in {"unknown"}:
        station_metrics.append(
            hero_metric(
                "🏠",
                "Basis",
                localize_status_value(station_state),
                status_class(station_state, {"paused", "clean_add_water"}, {"error"}),
            )
        )

    dust_bag_state = value_from_signals(signals, "dust_bag_status")
    dust_bag_error = error_value == "dust_bag_full"
    if dust_bag_state or dust_bag_error:
        if dust_bag_error:
            station_metrics.append(hero_metric("🗑️", "Staubbeutel", "Voll", "error"))
        else:
            station_metrics.append(
                hero_metric(
                    "🗑️",
                    "Staubbeutel",
                    localize_status_value(dust_bag_state),
                    status_class(dust_bag_state, {"check"}, {"not_installed"}),
                )
            )

    clean_water_state = value_from_signals(signals, "clean_water_tank_status")
    if clean_water_state:
        station_metrics.append(
            hero_metric(
                "💧",
                "Frischwasser",
                localize_status_value(clean_water_state),
                status_class(clean_water_state, {"low_water"}, {"not_installed"}),
            )
        )

    dirty_water_state = value_from_signals(signals, "dirty_water_tank_status")
    if dirty_water_state:
        station_metrics.append(
            hero_metric(
                "🚿",
                "Abwasser",
                localize_status_value(dirty_water_state),
                status_class(dirty_water_state, {"not_installed_or_full"}),
            )
        )

    detergent_state = value_from_signals(signals, "detergent_status")
    if detergent_state:
        station_metrics.append(
            hero_metric(
                "🫧",
                "Reiniger",
                localize_status_value(detergent_state),
                status_class(detergent_state, {"low_detergent", "disabled"}),
            )
        )

    drainage_state = value_from_signals(signals, "station_drainage_status") or value_from_signals(signals, "drainage_status")
    if drainage_state and drainage_state not in {"idle", "unknown"}:
        station_metrics.append(
            hero_metric(
                "↧",
                "Entleerung",
                localize_status_value(drainage_state),
                status_class(drainage_state, {"draining"}, {"draining_failed"}),
            )
        )

    dust_bag_drying_state = value_from_signals(signals, "dust_bag_drying_status")
    if dust_bag_drying_state and dust_bag_drying_state not in {"idle", "unknown"}:
        station_metrics.append(
            hero_metric("♨", "Beuteltrocknung", localize_status_value(dust_bag_drying_state), status_class(dust_bag_drying_state, {"paused"}))
        )

    return robot_metrics, station_metrics


def room_stats_for(summary: dict, room: dict) -> dict:
    for stats in summary.get("status", {}).get("room_stats", []) or []:
        if stats.get("room_key") == room.get("room_key") or stats.get("room") == room.get("room"):
            return stats
    return {}


def render_presence_rows(summary: dict) -> str:
    status = summary.get("status", {})
    people = summary.get("status", {}).get("presence_summary", []) or []
    if not people:
        return '<div class="empty">Keine Personen konfiguriert</div>'

    radius = number_value(status.get("travel_pause_radius_km"), 0) or 0
    travel_active = bool(status.get("travel_mode_active"))
    after_hours = number_value(summary.get("options", {}).get("travel_pause_after_hours"), 0) or 0
    away_since = parse_datetime(status.get("away_since"))
    remaining_label = None
    if away_since and after_hours > 0:
        now = datetime.now(away_since.tzinfo) if away_since.tzinfo else datetime.now()
        elapsed_hours = max(0, (now - away_since).total_seconds() / 3600)
        remaining_label = format_duration_hours(max(0, after_hours - elapsed_hours))

    rows = []
    for person in people:
        name = person_name(person)
        initials = "".join(part[:1] for part in name.split())[:2].upper() or "P"
        home = person_is_home(person)
        details = []
        distance = person_distance(person)
        travel_time = person_travel_time(person)
        if distance is not None:
            details.append(format_number(distance, " km"))
        if travel_time is not None:
            details.append(format_number(travel_time, " min"))
        outside_radius = not home and radius > 0 and distance is not None and distance > radius
        if outside_radius:
            details = ["außerhalb Radius"]
            if travel_active:
                details.append("Reisemodus aktiv")
            elif remaining_label:
                details.append(f"aktiv in {remaining_label}")
        detail_text = " · ".join(details)
        if home:
            badge = "zuhause"
            badge_class = "home"
        elif outside_radius and travel_active:
            badge = "reisend"
            badge_class = "travel"
        elif outside_radius:
            badge = "außerhalb"
            badge_class = "warning"
        else:
            badge = "abwesend"
            badge_class = "away"
        rows.append(
            f"""
            <div class="person-row">
              <div class="person-info">
                <div class="person-avatar">{escape(initials)}</div>
                <div class="person-copy">
                  <span class="person-name">{escape(name)}</span>
                  <span class="person-status">
                    <span>{escape(detail_text)}</span>
                  </span>
                </div>
              </div>
              <span class="pill {badge_class}">{escape(badge)}</span>
            </div>
            """
        )
    return "".join(rows)


def render_travel_guard(summary: dict) -> str:
    status = summary.get("status", {})
    people = status.get("presence_summary", []) or []
    radius = number_value(status.get("travel_pause_radius_km"), 0) or 0
    max_distance = number_value(status.get("max_distance_km"), 0) or 0
    active = bool(status.get("travel_mode_active"))
    enabled_value = status.get("travel_mode_enabled")
    enabled = True if enabled_value is None else bool(enabled_value)
    away_people = [person for person in people if not person_is_home(person)]
    known_distances = [
        person_distance(person)
        for person in away_people
        if person_distance(person) is not None
    ]
    outside_radius = [
        person for person in away_people
        if radius > 0 and (person_distance(person) or 0) > radius
    ]
    all_outside_radius = bool(people) and len(away_people) == len(people) and len(outside_radius) == len(people)

    if not enabled:
        state_class = "muted"
        state_label = "Reiseschutz aus"
        state_value_text = "Aus"
    elif active:
        state_class = "blocked"
        state_label = "Reisemodus"
        state_value_text = "Aktiv"
    elif all_outside_radius:
        after_hours = number_value(summary.get("options", {}).get("travel_pause_after_hours"), 0) or 0
        away_since = parse_datetime(status.get("away_since"))
        remaining_label = "-"
        if away_since and after_hours > 0:
            now = datetime.now(away_since.tzinfo) if away_since.tzinfo else datetime.now()
            elapsed_hours = max(0, (now - away_since).total_seconds() / 3600)
            remaining_label = format_duration_hours(max(0, after_hours - elapsed_hours))
        state_class = "warning"
        state_label = "Reisemodus"
        state_value_text = f"in {remaining_label}"
    elif people:
        state_class = "clear"
        state_label = "Reisemodus"
        state_value_text = "Inaktiv"
    else:
        state_class = "muted"
        state_label = "Keine Personen"
        state_value_text = "Keine Daten"

    max_parts = []
    if known_distances:
        max_parts.append(f"weiteste Person {format_number(max(known_distances), ' km')}")
    if max_distance > 0:
        max_parts.append(f"Limit {format_number(max_distance, ' km')}")
    distance_detail = " · ".join(max_parts) if max_parts else "Keine Distanzdaten"
    outside_detail = f"{len(outside_radius)}/{len(people)} draußen" if people else "-"
    reason_detail = travel_reason_label(status.get("travel_mode_reason")) if active else outside_detail
    after_hours = number_value(summary.get("options", {}).get("travel_pause_after_hours"), 0) or 0
    duration_label = f"{int(after_hours)} h" if after_hours and after_hours.is_integer() else format_duration_hours(after_hours) if after_hours else "-"

    return f"""
      <div class="travel-guard {state_class}">
        <div class="travel-guard-head">
          <span class="travel-guard-label">{escape(state_label)}</span>
          <strong>{escape(state_value_text)}</strong>
        </div>
        <div class="travel-guard-meta" aria-label="Reisemodus Details">
          <span>{escape(reason_detail)}</span>
          <span>Radius {escape(format_number(radius, " km"))}</span>
          <span>nach {escape(duration_label)}</span>
        </div>
      </div>
    """


def render_history_card(summary: dict) -> str:
    status = summary.get("status", {})
    history = summary.get("history", {})
    weekly = status.get("weekly_stats") or history.get("weekly_stats") or []
    recent = status.get("recent_runs") or history.get("recent_runs") or []
    room_stats = status.get("room_stats", []) or []
    current_week = weekly[0] if weekly else {}

    def run_row(item: dict) -> str:
        return f"""
          <div class="history-row">
            <div>
              <strong>{escape(item.get("room") or "-")}</strong>
              <span>{escape(format_last_cleaned(item.get("finished_at") or item.get("started_at")))}</span>
            </div>
            <div class="history-row-meta">
              <span>{escape(item.get("outcome") or "-")}</span>
              <strong>{escape(format_number(item.get("actual_duration_min"), " min", "0 min"))}</strong>
            </div>
          </div>
        """

    def room_row(item: dict) -> str:
        learned = item.get("learned_duration_min")
        effective = item.get("effective_duration_min")
        plan_value = effective if effective is not None else learned
        runs = item.get("completed_runs") or 0
        average_label = format_number(learned, " min", "unbekannt")
        plan_label = format_number(plan_value, " min", "offen")
        return f"""
          <div class="history-room">
            <div class="history-room-title">
              <strong>{escape(item.get("room") or item.get("room_key") or "-")}</strong>
              <span>{escape(runs)} Läufe</span>
            </div>
            <div class="history-room-metrics">
              <span class="metric-chip">Ø {escape(average_label)}</span>
              <span class="metric-chip">Plan {escape(plan_label)}</span>
            </div>
          </div>
        """

    recent_html = "".join(run_row(item) for item in recent[:5]) or '<div class="empty">Noch keine Läufe gespeichert.</div>'
    room_html = "".join(room_row(item) for item in room_stats[:4]) or '<div class="empty">Noch keine Raumstatistiken verfügbar.</div>'

    return f"""
      <div class="history-summary">
        <div>
          <span>Diese Woche</span>
          <strong>{escape(current_week.get("runs", 0))} Läufe</strong>
        </div>
        <div>
          <span>Minuten</span>
          <strong>{escape(current_week.get("minutes", 0))} min</strong>
        </div>
        <div>
          <span>Einträge</span>
          <strong>{escape(status.get("history_entries", 0))}</strong>
        </div>
      </div>
      <div class="history-block">
        <h3>Letzte Läufe</h3>
        <div class="history-list">{recent_html}</div>
      </div>
      <div class="history-block">
        <h3>Reinigungsdauer</h3>
        <div class="history-room-grid">{room_html}</div>
      </div>
    """


def render_room_row(summary: dict, room: dict, index: int, section_kind: str) -> str:
    stats = room_stats_for(summary, room)
    fits = bool(room.get("fits_now", True))
    room_key = str(room.get("room_key") or room.get("room") or "")
    active_room = active_room_label(summary)

    # Build action button (neben dem Namen)
    action_btn = ""
    if not active_room and section_kind == "due" and fits:
        action_btn = f'<button class="room-btn primary" type="button" data-room-action="start" data-room-key="{escape(room_key)}">Starten</button>'
    elif section_kind == "later":
        action_btn = f'<button class="room-btn" type="button" data-room-action="due" data-room-key="{escape(room_key)}">Fällig machen</button>'

    # Build due tag based on last_cleaned + interval
    from datetime import datetime, timedelta
    last_cleaned_str = stats.get("last_cleaned")
    interval_h = number_value(room.get("interval_h"), 24) or 24
    due_tag = ''

    if last_cleaned_str:
        try:
            last_cleaned_dt = datetime.fromisoformat(last_cleaned_str.replace("Z", "+00:00"))
            due_dt = last_cleaned_dt + timedelta(hours=interval_h)
            now = datetime.now(due_dt.tzinfo) if due_dt.tzinfo else datetime.now()
            hours_until_due = (due_dt - now).total_seconds() / 3600

            if hours_until_due <= 0:
                due_tag = '<span class="room-tag overdue"><span class="icon">⏰</span> überfällig</span>'
            elif hours_until_due <= 24:
                due_tag = '<span class="room-tag due"><span class="icon">⏰</span> heute</span>'
            else:
                days_until = int(hours_until_due / 24)
                due_tag = f'<span class="room-tag due"><span class="icon">⏰</span> in {days_until}d</span>'
        except (ValueError, TypeError):
            pass

    # Build interval tag
    interval_days = interval_h / 24
    if interval_days <= 1:
        interval_tag = '<span class="room-tag interval">täglich</span>'
    elif interval_days == 2:
        interval_tag = '<span class="room-tag interval">alle 2 Tage</span>'
    elif interval_days == 7:
        interval_tag = '<span class="room-tag interval">wöchentlich</span>'
    elif interval_days == 14:
        interval_tag = '<span class="room-tag interval">alle 2 Wochen</span>'
    else:
        interval_tag = f'<span class="room-tag interval">alle {int(interval_days)} Tage</span>'

    buttons_html = f'<div class="room-buttons">{action_btn}</div>' if action_btn else ''

    # CSS classes for row styling
    row_classes = ["room-row"]
    if section_kind == "due" and index == 1:
        row_classes.append("next")
    elif fits:
        row_classes.append("fits")

    duration_min = number_value(room.get("effective_duration_min"), 0) or 0
    return f"""
        <div class="{' '.join(row_classes)}" draggable="true" data-room-key="{escape(room_key)}" data-room-section-kind="{escape(section_kind)}" data-duration="{int(duration_min)}">
          <div class="room-info">
            <div class="room-header">
              <span class="room-name"><span class="room-inline-rank" aria-hidden="true">{index}.</span>{escape(room.get("room", "Raum"))}</span>
              <span class="room-duration">{escape(format_number(room.get("effective_duration_min"), " min"))}</span>
            </div>
            <div class="room-tags">
              <span class="room-tag last"><span class="icon">✓</span> {escape(format_last_cleaned(stats.get("last_cleaned")))}</span>
              {due_tag}
              {interval_tag}
            </div>
            {buttons_html}
          </div>
        </div>
        """


def render_active_room_row(summary: dict) -> str:
    active_state = summary.get("states", {}).get("sensors", {}).get("active_room")
    active_room = state_value(active_state, "")
    if active_room.lower() in {"", "-", "keine", "keiner", "none"}:
        return '<div class="room-empty" data-room-section-kind="active">Gerade wird kein Raum gereinigt.</div>'

    attrs = state_attrs(active_state)
    remaining_value = number_value(attrs.get("remaining_min"), 0) or 0
    planned_value = number_value(attrs.get("planned_duration_min"), 0) or 0
    planned = format_number(planned_value, " min")
    progress_value = 0
    if planned_value > 0:
        progress_value = max(0, min(100, ((planned_value - remaining_value) / planned_value) * 100))

    # Find room in queue to get interval
    queue = summary.get("status", {}).get("room_queue", []) or []
    room_data = next((r for r in queue if r.get("room") == active_room), {})
    interval_h = number_value(room_data.get("interval_h"), 24) or 24
    interval_days = interval_h / 24

    if interval_days <= 1:
        interval_tag = '<span class="room-tag interval">täglich</span>'
    elif interval_days == 2:
        interval_tag = '<span class="room-tag interval">alle 2 Tage</span>'
    elif interval_days == 7:
        interval_tag = '<span class="room-tag interval">wöchentlich</span>'
    elif interval_days == 14:
        interval_tag = '<span class="room-tag interval">alle 2 Wochen</span>'
    else:
        interval_tag = f'<span class="room-tag interval">alle {int(interval_days)} Tage</span>'

    return f"""
      <div class="room-row next" data-duration="{int(remaining_value)}" data-room-section-kind="active">
        <div class="room-info">
          <div class="room-header">
            <span class="room-name">{escape(active_room)}</span>
            <span class="room-duration">{escape(planned)}</span>
          </div>
          <div class="room-tags">
            <span class="room-tag last"><span class="icon">🧹</span> läuft gerade</span>
            {interval_tag}
          </div>
          <div class="room-progress">
            <div class="room-progress-value">{progress_value:.0f}%</div>
            <div class="room-progress-bar" aria-hidden="true">
              <span style="width: {progress_value:.0f}%"></span>
            </div>
          </div>
          <div class="room-buttons">
            <button class="room-btn danger" type="button" data-room-action="stop">Abbrechen</button>
          </div>
        </div>
      </div>
    """


def render_room_sections(summary: dict) -> str:
    queue = visible_room_queue(summary)
    ordered = ordered_room_queue(summary, queue) if queue else []
    due_rooms = [
        room for room in ordered if (number_value(room.get("forecast_score"), 0) or 0) >= 1
    ]
    later_rooms = [
        room for room in ordered if (number_value(room.get("forecast_score"), 0) or 0) < 1
    ]

    sections = ['<div class="room-section" data-room-section-kind="active">In Reinigung</div>', render_active_room_row(summary)]

    sections.append('<div class="room-section" data-room-section-kind="due">Fällig</div>')
    if due_rooms:
        for index, room in enumerate(due_rooms, start=1):
            sections.append(render_room_row(summary, room, index, "due"))
    else:
        sections.append('<div class="room-empty" data-room-section-kind="due">Aktuell ist kein Raum fällig.</div>')

    sections.append('<div class="room-section" data-room-section-kind="later">Noch nicht fällig</div>')
    if later_rooms:
        for index, room in enumerate(later_rooms, start=max(1, len(due_rooms) + 1)):
            sections.append(render_room_row(summary, room, index, "later"))
    else:
        sections.append('<div class="room-empty" data-room-section-kind="later">Alle sichtbaren Räume sind bereits fällig.</div>')

    return "".join(sections)


def rooms_fit_summary(summary: dict) -> str:
    queue = visible_room_queue(summary)
    due_today = [
        room
        for room in queue
        if (number_value(room.get("forecast_score"), 0) or 0) >= 1
        and bool(room.get("enabled", True))
    ]
    fitting = [room for room in due_today if room.get("fits_now")]
    if not due_today:
        return "Kein Raum ist heute fällig"
    if len(fitting) == 1:
        return f"1 von {len(due_today)} fälligen Räumen passt jetzt"
    return f"{len(fitting)} von {len(due_today)} fälligen Räumen passen jetzt"


def active_room_label(summary: dict) -> str:
    active_state = summary.get("states", {}).get("sensors", {}).get("active_room")
    active_room = state_value(active_state, "")
    return "" if active_room.lower() in {"", "-", "keine", "keiner", "none"} else active_room


def visible_room_queue(summary: dict) -> List[dict]:
    active_room = active_room_label(summary)
    queue = summary.get("status", {}).get("room_queue", []) or []
    if not active_room:
        return queue
    return [room for room in queue if str(room.get("room", "")) != active_room]


def ordered_room_queue(summary: dict, queue: Optional[List[dict]] = None) -> List[dict]:
    queue = queue if queue is not None else visible_room_queue(summary)
    ordered = sorted(
        queue,
        key=lambda item: (
            not bool(item.get("enabled", True)),
            not bool(item.get("fits_now", True)),
            -number_value(item.get("priority"), 0),
        ),
    )
    override = state_value(
        summary.get("states", {}).get("global", {}).get("one_time_room_override"), ""
    )
    if override:
        ordered.sort(key=lambda item: 0 if item.get("room_key") == override else 1)
    return ordered


def render_robot_alerts(summary: dict) -> str:
    vacuum = summary.get("states", {}).get("vacuum") or {}
    attrs = state_attrs(vacuum)
    state = state_value(vacuum, "unbekannt")
    battery = number_value(attrs.get("battery_level") or attrs.get("battery"))
    alerts = []

    error = attrs.get("error") or attrs.get("error_message")
    if state.lower() in {"unavailable", "unknown"}:
        alerts.append(("error", "Nicht erreichbar"))
    if error:
        alerts.append(("error", error))
    if battery is not None and battery < 20:
        alerts.append(("warning", f"Batterie niedrig: {format_number(battery, '%')}"))
    if attrs.get("bin_full") or attrs.get("dustbin_full"):
        alerts.append(("warning", "Staubbehälter voll"))
    if attrs.get("water_tank_empty"):
        alerts.append(("warning", "Wassertank leer"))

    if not alerts:
        return ""
    return "".join(
        f'<span class="robot-alert {escape(level)}">{escape(text)}</span>'
        for level, text in alerts
    )


def render_dashboard_html() -> str:
    summary = build_summary()
    states = summary.get("states", {}).get("sensors", {})
    status = summary.get("status", {})
    active_room = state_value(states.get("active_room"), "")
    active_attrs = state_attrs(states.get("active_room"))
    status_text = state_value(states.get("status"), "Bereit")
    is_cleaning = active_room.lower() not in {"", "-", "keine", "keiner", "none"}
    remaining = number_value(active_attrs.get("remaining_min"), 0) or 0

    # Count due rooms
    queue = visible_room_queue(summary)
    due_rooms = [
        room for room in queue
        if (number_value(room.get("forecast_score"), 0) or 0) >= 1
        and bool(room.get("enabled", True))
    ]
    due_count = len(due_rooms)

    # Determine presence state
    people = status.get("presence_summary", []) or []
    home_people = [person for person in people if person_is_home(person)]
    anyone_home = len(home_people) > 0

    # Hero card values will be computed after we have all the data

    planned = number_value(active_attrs.get("planned_duration_min"), 0) or 0
    progress = 0
    if planned > 0:
        progress = max(0, min(100, ((planned - remaining) / planned) * 100))

    # people and home_people already calculated above for hero card
    presence_summary = f"{len(home_people)} zuhause" if home_people else "Niemand zuhause"
    presence_class = "blocked" if home_people else "clear"
    configured_return_buffer = number_value(
        state_value(summary.get("states", {}).get("global", {}).get("return_buffer"), ""),
        0,
    ) or 0

    travel_times = [person_travel_time(person) for person in people]
    travel_times = [value for value in travel_times if value is not None]
    closest_travel_time = min(travel_times) if travel_times else number_value(state_value(states.get("travel_time"), ""))
    return_window = number_value(state_value(states.get("return_window"), ""), 0) or 0
    buffer_min = max(0, (closest_travel_time or 0) - return_window)
    next_rooms = ordered_room_queue(summary)
    next_room_duration = (
        number_value(next_rooms[0].get("effective_duration_min")) if next_rooms else None
    )
    relevant_duration = remaining if is_cleaning else next_room_duration
    travel_metric_class = "primary"
    if is_cleaning and relevant_duration is not None:
        if return_window < relevant_duration + configured_return_buffer:
            travel_metric_class = "blocked"
    if relevant_duration is None:
        window_metric_class = ""
    elif return_window > relevant_duration:
        window_metric_class = "primary"
    else:
        window_metric_class = "blocked"

    vacuum = summary.get("states", {}).get("vacuum") or {}
    vacuum_attrs = state_attrs(vacuum)
    vacuum_state = state_value(vacuum, "unbekannt")
    robot_signals = summary.get("states", {}).get("robot", {}).get("signals", {}) or {}
    battery = first_number(
        vacuum_attrs.get("battery_level"),
        vacuum_attrs.get("battery"),
        state_value(robot_signals.get("battery_level"), ""),
    )
    battery_text = format_number(battery, "%", "")
    robot_summary = localize_vacuum_state(vacuum_state)

    # Hero card: Robot status header
    hero_status_title = addon_name()
    if is_cleaning:
        hero_status_icon = "🧹"
        hero_status_class = "cleaning"
        hero_activity = f"Reinigt {active_room}"
    else:
        hero_status_icon = "🤖"
        hero_status_class = "idle"
        hero_activity = robot_summary
    hero_status_sub = " · ".join(part for part in [hero_activity, battery_text] if part)

    robot_metrics, station_metrics = build_hero_metrics(summary)

    # Hero card: Settings toggles
    global_states = summary.get("states", {}).get("global", {})

    def is_on(entity_state):
        return state_value(entity_state, "off").lower() in {"on", "true", "1"}

    start_push_on = is_on(global_states.get("start_push"))
    return_push_on = is_on(global_states.get("return_push"))
    learning_on = is_on(global_states.get("learning"))
    travel_on = is_on(global_states.get("travel_logic"))
    return_guard_on = not home_people

    hero_start_push_class = "on" if start_push_on else ""
    hero_start_push_text = "Aktiv" if start_push_on else "Inaktiv"
    hero_return_push_class = "on" if return_push_on else ""
    hero_return_push_text = "Aktiv" if return_push_on else "Inaktiv"
    hero_learning_class = "on" if learning_on else ""
    hero_learning_text = "Aktiv" if learning_on else "Inaktiv"
    hero_travel_class = "on" if travel_on else ""
    hero_travel_text = "Aktiv" if travel_on else "Inaktiv"
    hero_return_guard_class = "on" if return_guard_on else ""
    hero_return_guard_text = "Aktiv" if return_guard_on else "Inaktiv"

    replacements = {
        "hero_status_icon": hero_status_icon,
        "hero_status_class": hero_status_class,
        "hero_status_title": hero_status_title,
        "hero_status_sub": hero_status_sub,
        "hero_robot_items": render_hero_metric_rows(robot_metrics, "Keine Robotersensoren verfügbar"),
        "hero_station_items": render_hero_metric_rows(station_metrics, "Keine Stationssensoren verfügbar"),
        "hero_start_push_class": hero_start_push_class,
        "hero_start_push_text": hero_start_push_text,
        "hero_return_push_class": hero_return_push_class,
        "hero_return_push_text": hero_return_push_text,
        "hero_learning_class": hero_learning_class,
        "hero_learning_text": hero_learning_text,
        "hero_travel_class": hero_travel_class,
        "hero_travel_text": hero_travel_text,
        "hero_return_guard_class": hero_return_guard_class,
        "hero_return_guard_text": hero_return_guard_text,
        "robot_summary": robot_summary,
        "robot_alerts": render_robot_alerts(summary),
        "presence_summary": presence_summary,
        "presence_summary_class": presence_class,
        "travel_metric_class": travel_metric_class,
        "window_metric_class": window_metric_class,
        "presence_rows": render_presence_rows(summary),
        "travel_guard": render_travel_guard(summary),
        "travel_time": format_number(closest_travel_time, " min"),
        "return_buffer": format_number(buffer_min, " min"),
        "return_window": format_number(return_window, " min"),
        "return_window_min": str(int(return_window)),
        "room_sections": render_room_sections(summary),
        "history_card": render_history_card(summary),
    }

    output = load_dashboard_template()
    for key, value in replacements.items():
        output = output.replace("{{{" + key + "}}}", str(value))
        output = output.replace("{{" + key + "}}", escape(value))
    return output


HTML = """<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Vacuum Automation Dashboard</title>
  <style>
    :root {
      --bg: #f4f6f5;
      --panel: #ffffff;
      --panel-soft: #f0f3f2;
      --ink: #17201b;
      --muted: #63706a;
      --line: #dde4e0;
      --accent: #227a55;
      --accent-soft: #e4f3ec;
      --accent-strong: #145b3d;
      --warning: #a76522;
      --warning-soft: #fef6e6;
      --danger: #b24c3d;
      --danger-soft: #fef2f0;
      --shadow: 0 2px 8px rgba(21, 32, 25, 0.06);
      --radius: 10px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--ink);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      line-height: 1.4;
    }
    .shell {
      max-width: 1400px;
      margin: 0 auto;
      padding: 20px;
    }

    /* Tabs */
    .tabs {
      display: flex;
      gap: 4px;
      padding: 4px;
      margin-bottom: 16px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      width: fit-content;
    }
    .tab-button {
      border: 0;
      border-radius: 7px;
      padding: 8px 16px;
      background: transparent;
      color: var(--muted);
      font-size: 14px;
      font-weight: 600;
      cursor: pointer;
      transition: all 150ms ease;
    }
    .tab-button:hover { color: var(--ink); }
    .tab-button.active {
      background: var(--accent);
      color: white;
    }
    .tab-panel { display: none; }
    .tab-panel.active { display: block; }

    /* Dashboard Layout - Two Column */
    .dashboard-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      grid-template-rows: auto auto;
      gap: 16px;
    }
    .dashboard-left {
      display: grid;
      gap: 16px;
      align-content: start;
    }
    .dashboard-right {
      display: grid;
      gap: 16px;
      align-content: start;
    }

    /* Cards */
    .card {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
    }

    /* Hero Status Card */
    .hero-card {
      padding: 20px;
      display: grid;
      gap: 16px;
    }
    .hero-main {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 16px;
    }
    .hero-status {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .status-icon {
      width: 48px;
      height: 48px;
      border-radius: 12px;
      display: grid;
      place-items: center;
      font-size: 24px;
    }
    .status-icon.cleaning { background: var(--accent-soft); }
    .status-icon.idle { background: var(--panel-soft); }
    .status-icon.error { background: var(--danger-soft); }
    .status-icon.charging { background: #e8f4fd; }
    .hero-text h1 {
      margin: 0;
      font-size: 22px;
      font-weight: 700;
    }
    .hero-text p {
      margin: 4px 0 0;
      color: var(--muted);
      font-size: 14px;
    }
    .hero-badge {
      padding: 6px 12px;
      border-radius: 20px;
      font-size: 13px;
      font-weight: 600;
      white-space: nowrap;
    }
    .hero-badge.active {
      background: var(--accent);
      color: white;
    }
    .hero-badge.idle {
      background: var(--panel-soft);
      color: var(--muted);
    }
    .hero-badge.error {
      background: var(--danger);
      color: white;
    }
    .hero-progress {
      background: var(--panel-soft);
      border-radius: var(--radius);
      padding: 12px 16px;
      display: flex;
      align-items: center;
      gap: 16px;
    }
    .progress-info {
      flex: 1;
      min-width: 0;
    }
    .progress-info strong {
      display: block;
      font-size: 15px;
      margin-bottom: 2px;
    }
    .progress-info span {
      color: var(--muted);
      font-size: 13px;
    }
    .progress-bar {
      width: 120px;
      height: 6px;
      background: var(--line);
      border-radius: 3px;
      overflow: hidden;
    }
    .progress-bar > span {
      display: block;
      height: 100%;
      background: var(--accent);
      border-radius: 3px;
      transition: width 300ms ease;
    }

    /* Robot Status */
    .robot-status {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: 12px;
      padding: 16px;
      border-top: 1px solid var(--line);
    }
    .robot-stat {
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .robot-stat-icon {
      width: 36px;
      height: 36px;
      border-radius: 8px;
      background: var(--panel-soft);
      display: grid;
      place-items: center;
      font-size: 18px;
      flex-shrink: 0;
    }
    .robot-stat-icon.warning { background: var(--warning-soft); }
    .robot-stat-icon.error { background: var(--danger-soft); }
    .robot-stat-icon.ok { background: var(--accent-soft); }
    .robot-stat-text {
      min-width: 0;
    }
    .robot-stat-text strong {
      display: block;
      font-size: 14px;
      font-weight: 600;
    }
    .robot-stat-text span {
      color: var(--muted);
      font-size: 12px;
    }

    /* Presence Card */
    .presence-card {
      padding: 16px;
    }
    .presence-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 12px;
    }
    .presence-header h2 {
      margin: 0;
      font-size: 15px;
      font-weight: 600;
      color: var(--muted);
    }
    .presence-summary {
      font-size: 14px;
      font-weight: 600;
      padding: 4px 10px;
      border-radius: 6px;
    }
    .presence-summary.clear {
      background: var(--accent-soft);
      color: var(--accent-strong);
    }
    .presence-summary.blocked {
      background: var(--danger-soft);
      color: var(--danger);
    }
    .presence-list {
      display: grid;
      gap: 8px;
    }
    .person-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 12px;
      background: var(--panel-soft);
      border-radius: 8px;
    }
    .person-info {
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .person-avatar {
      width: 32px;
      height: 32px;
      border-radius: 50%;
      background: var(--line);
      display: grid;
      place-items: center;
      font-size: 14px;
    }
    .person-name {
      font-weight: 600;
      font-size: 14px;
    }
    .person-status {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 13px;
      color: var(--muted);
    }
    .person-status .pill {
      padding: 3px 8px;
      border-radius: 4px;
      font-size: 12px;
      font-weight: 600;
    }
    .pill {
      display: inline-block;
      padding: 3px 8px;
      border-radius: 4px;
      font-size: 12px;
      font-weight: 600;
    }
    .pill.home { background: var(--danger-soft); color: var(--danger); }
    .pill.away { background: var(--accent-soft); color: var(--accent-strong); }

    /* Calculation Row - modern formula display */
    .calc-row {
      display: flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
    }
    .calc-row .value {
      background: var(--panel);
      padding: 4px 10px;
      border-radius: 6px;
      font-weight: 600;
      font-size: 13px;
      color: var(--ink);
      white-space: nowrap;
    }
    .calc-row .value.muted {
      background: transparent;
      border: 1px dashed var(--line);
      color: var(--ink-muted);
      font-weight: 500;
    }
    .calc-row .op {
      color: var(--ink-muted);
      font-size: 14px;
      font-weight: 500;
    }
    .calc-row .label {
      color: var(--ink-muted);
      font-size: 12px;
      margin-left: 2px;
    }

    /* Time Window */
    .time-window {
      margin-top: 12px;
      padding: 12px;
      background: var(--panel-soft);
      border-radius: 8px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    .time-window-info {
      display: grid;
      gap: 2px;
    }
    .time-window-label {
      font-size: 13px;
      font-weight: 600;
      color: var(--ink);
    }
    .time-window-detail {
      font-size: 12px;
      color: var(--muted);
    }
    .time-window-value {
      font-size: 18px;
      font-weight: 700;
      color: var(--accent-strong);
    }

    /* Room Queue */
    .queue-card {
      padding: 16px;
    }
    .queue-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 12px;
    }
    .queue-header h2 {
      margin: 0;
      font-size: 15px;
      font-weight: 600;
      color: var(--muted);
    }
    .room-list {
      display: grid;
      gap: 8px;
    }
    .room-row {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 12px;
      background: var(--panel-soft);
      border-radius: 8px;
    }
    .room-row.next {
      background: var(--accent-soft);
      border: 1px solid rgba(34, 122, 85, 0.2);
    }
    .room-rank {
      width: 24px;
      height: 24px;
      border-radius: 6px;
      background: var(--panel);
      display: grid;
      place-items: center;
      font-size: 12px;
      font-weight: 700;
      color: var(--muted);
      flex-shrink: 0;
    }
    .room-row.next .room-rank {
      background: var(--accent);
      color: white;
    }
    .room-info {
      flex: 1;
      min-width: 0;
    }
    .room-header {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 4px;
    }
    .room-name {
      font-weight: 600;
      font-size: 14px;
    }
    .room-tags {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
    }
    .room-tag {
      display: inline-flex;
      align-items: center;
      gap: 3px;
      padding: 2px 6px;
      border-radius: 4px;
      font-size: 11px;
      font-weight: 500;
      white-space: nowrap;
    }
    .room-tag.last {
      background: var(--panel);
      color: var(--muted);
    }
    .room-tag.due {
      background: var(--warning-soft);
      color: var(--warning);
    }
    .room-tag.overdue {
      background: var(--danger-soft);
      color: var(--danger);
    }
    .room-tag .icon {
      font-size: 10px;
    }
    .room-duration {
      text-align: right;
      flex-shrink: 0;
    }
    .room-duration strong {
      display: block;
      font-size: 14px;
    }
    .room-duration span {
      font-size: 11px;
      color: var(--muted);
    }

    /* Config Tab Styles (keep existing) */
    .config-grid-layout {
      display: grid;
      grid-template-columns: minmax(0, 0.95fr) minmax(0, 1.25fr);
      gap: 16px;
      align-items: start;
    }
    .stack {
      display: grid;
      gap: 16px;
    }
    .section-title {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin-bottom: 14px;
    }
    .section-title h2,
    .section-title h3 {
      margin: 0;
      font-size: 17px;
    }
    .section-title p {
      margin: 4px 0 0;
      color: var(--muted);
      font-size: 13px;
    }
    .controls-grid {
      display: grid;
      gap: 8px;
    }
    .control {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 12px;
      align-items: center;
      background: var(--panel-soft);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 12px;
    }
    .control .name {
      font-weight: 700;
    }
    .control .desc {
      color: var(--muted);
      font-size: 13px;
      margin-top: 4px;
    }
    .control button {
      border: 0;
      border-radius: 7px;
      padding: 9px 13px;
      min-width: 72px;
      font-size: 14px;
      font-weight: 600;
      cursor: pointer;
      background: var(--accent);
      color: white;
    }
    .control button.off {
      background: var(--line);
      color: var(--ink);
    }
    .number-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }
    .number-control {
      background: var(--panel-soft);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 12px;
    }
    .number-control label {
      display: block;
      font-size: 13px;
      color: var(--muted);
      margin-bottom: 8px;
    }
    .number-inline {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
    }
    .number-inline input {
      width: 100%;
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 7px;
      padding: 8px 12px;
      font-size: 14px;
      color: var(--ink);
    }
    .number-inline button {
      border: 0;
      border-radius: 7px;
      padding: 0 14px;
      background: var(--ink);
      color: white;
      font-weight: 600;
      cursor: pointer;
    }
    .room-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }
    .room-card {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 14px;
    }
    .room-card-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 12px;
    }
    .room-card-header strong {
      font-size: 15px;
    }
    .room-card > button {
      width: 100%;
      border: 0;
      border-radius: 7px;
      padding: 9px 12px;
      background: var(--accent);
      color: #fff;
      font-weight: 600;
      cursor: pointer;
    }
    .room-card > button.off {
      background: var(--line);
      color: var(--ink);
    }
    .room-config {
      display: grid;
      gap: 6px;
      margin: 12px 0;
      font-size: 13px;
      color: var(--muted);
    }
    .room-config-row {
      display: flex;
      justify-content: space-between;
    }
    .config-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      color: var(--muted);
      font-size: 13px;
    }
    .config-grid strong {
      color: var(--ink);
      display: block;
      font-size: 14px;
      margin-bottom: 3px;
    }
    .muted { color: var(--muted); }
    .empty {
      color: var(--muted);
      padding: 16px;
      text-align: center;
      font-size: 14px;
    }

    /* Responsive */
    @media (max-width: 900px) {
      .dashboard-grid {
        grid-template-columns: 1fr;
      }
    }
    @media (max-width: 640px) {
      .shell { padding: 16px; }
      .hero-main { flex-direction: column; }
      .robot-status { grid-template-columns: 1fr 1fr; }
      .config-grid-layout { grid-template-columns: 1fr; }
      .number-grid, .room-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <nav class="tabs" aria-label="Dashboard sections">
      <button class="tab-button active" type="button" data-tab-button="dashboard" onclick="switchTab('dashboard')">Dashboard</button>
      <button class="tab-button" type="button" data-tab-button="configuration" onclick="switchTab('configuration')">Konfiguration</button>
    </nav>

    <section class="tab-panel active" data-tab-panel="dashboard">
      <div class="dashboard-grid">
        <!-- Left Column: Status & Robot -->
        <div class="dashboard-left">
          <!-- Hero Status Card -->
          <div class="card hero-card" id="hero-card">
            <div class="hero-main">
              <div class="hero-status">
                <div class="status-icon idle" id="status-icon">🤖</div>
                <div class="hero-text">
                  <h1 id="hero-title">Lädt...</h1>
                  <p id="hero-reason">Verbinde mit Home Assistant</p>
                </div>
              </div>
              <div class="hero-badge idle" id="hero-badge">Laden</div>
            </div>
            <div class="hero-progress" id="hero-progress" style="display: none;">
              <div class="progress-info">
                <strong id="progress-room">Bad</strong>
                <span id="progress-time">noch 9 min</span>
              </div>
              <div class="progress-bar">
                <span id="progress-fill" style="width: 35%"></span>
              </div>
            </div>
          </div>

          <!-- Robot Status -->
          <div class="card">
            <div class="robot-status" id="robot-status">
              <div class="robot-stat">
                <div class="robot-stat-icon ok">🔋</div>
                <div class="robot-stat-text">
                  <strong>78%</strong>
                  <span>Batterie</span>
                </div>
              </div>
              <div class="robot-stat">
                <div class="robot-stat-icon warning">🗑️</div>
                <div class="robot-stat-text">
                  <strong>Fast voll</strong>
                  <span>Staubbehälter</span>
                </div>
              </div>
              <div class="robot-stat">
                <div class="robot-stat-icon ok">💧</div>
                <div class="robot-stat-text">
                  <strong>OK</strong>
                  <span>Wassertank</span>
                </div>
              </div>
              <div class="robot-stat">
                <div class="robot-stat-icon">🧹</div>
                <div class="robot-stat-text">
                  <strong>Eingesetzt</strong>
                  <span>Mop</span>
                </div>
              </div>
            </div>
          </div>

          <!-- Presence Card -->
          <div class="card presence-card">
            <div class="presence-header">
              <h2>Anwesenheit</h2>
              <div class="presence-summary clear" id="presence-summary">Niemand zuhause</div>
            </div>
            <div class="presence-list" id="presence-list"></div>
            <div class="time-window" id="time-window">
              <div class="time-window-info">
                <span class="time-window-label">Reinigungsfenster</span>
                <span class="time-window-detail" id="time-window-detail">42 min Reisezeit − 5 min Puffer</span>
              </div>
              <span class="time-window-value" id="time-window-value">34 min</span>
            </div>
          </div>
        </div>

        <!-- Right Column: Room Queue -->
        <div class="dashboard-right">
          <div class="card queue-card">
            <div class="queue-header">
              <h2>Nächste Räume</h2>
            </div>
            <div class="room-list" id="room-list"></div>
          </div>
        </div>
      </div>
    </section>

    <section class="tab-panel" data-tab-panel="configuration">
      <div class="config-grid-layout">
        <div class="stack">
          <section class="card" style="padding: 16px;">
            <div class="section-title">
              <div>
                <h2>Schalter</h2>
                <p>Aktiviere die Automatik und optionale Schutzfunktionen.</p>
              </div>
            </div>
            <div class="controls-grid" id="toggle-controls"></div>
          </section>

          <section class="card" style="padding: 16px;">
            <div class="section-title">
              <div>
                <h3>Entitäten</h3>
                <p>Statische Quellen aus der Add-on-Konfiguration.</p>
              </div>
            </div>
            <div class="config-grid" id="config-grid"></div>
          </section>
        </div>

        <div class="stack">
          <section class="card" style="padding: 16px;">
            <div class="section-title">
              <div>
                <h2>Planung</h2>
                <p>Zeitfenster, Reise-Puffer und Grenzen.</p>
              </div>
            </div>
            <div class="number-grid" id="global-numbers"></div>
          </section>

          <section class="card" style="padding: 16px;">
            <div class="section-title">
              <div>
                <h2>Räume</h2>
                <p>Raum aktivieren, Gewichtung und Dauer anpassen.</p>
              </div>
            </div>
            <div class="room-grid" id="room-grid"></div>
          </section>
        </div>
      </div>
    </section>
  </div>

  <script>
    const roomOrderStorageKey = "vacuum_dashboard_room_order";
    let latestSummary = null;
    let draggedRoomKey = null;

    const toggleMeta = {
      enabled: ["Automatik", "Hauptlogik für automatische Reinigungen."],
      learning: ["Lernlogik", "Nutzt gelernte Raumdauern aus erfolgreichen Läufen."],
      travel_logic: ["Reiseschutz", "Pausiert bei langen Fahrten oder großer Entfernung."],
      start_push: ["Start-Benachrichtigung", "Meldet automatisch gestartete Räume."],
      return_push: ["Rückkehr-Zusammenfassung", "Sendet eine Übersicht beim Heimkommen."],
      custom_home: ["Eigener Home-Punkt", "Nutzt manuelle Koordinaten statt zone.home."]
    };

    const numberMeta = {
      start_hour: "Start ab",
      end_hour: "Ende spätestens",
      return_buffer: "Rückkehr-Puffer (min)",
      fallback_speed: "Fallback-Geschwindigkeit (km/h)",
      default_travel_time: "Standard-Reisezeit (min)",
      home_latitude: "Home Latitude",
      home_longitude: "Home Longitude",
      travel_pause_radius: "Reise-Radius (km)",
      max_distance_km: "Maximale Entfernung (km)"
    };

    function switchTab(tabName) {
      document.querySelectorAll("[data-tab-button]").forEach(button => {
        button.classList.toggle("active", button.dataset.tabButton === tabName);
      });
      document.querySelectorAll("[data-tab-panel]").forEach(panel => {
        panel.classList.toggle("active", panel.dataset.tabPanel === tabName);
      });
    }

    function entityState(entity, fallback = "unknown") {
      if (!entity || entity.state === undefined || entity.state === null) return fallback;
      return entity.state;
    }

    function boolOn(entity) {
      return ["on", "home", "true"].includes(String(entityState(entity, "off")).toLowerCase());
    }

    function personHome(person) {
      return ["home", "on", "true"].includes(String(person?.state || "").toLowerCase());
    }

    function personStateLabel(person) {
      return personHome(person) ? "zuhause" : "abwesend";
    }

    function personDisplayName(person) {
      return String(person?.entity_id || "")
        .replace(/^person\./, "")
        .replaceAll("_", " ");
    }

    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }

    function formatLastCleaned(value) {
      if (!value) return "noch nie";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return value;
      const hours = Math.max(0, Math.round((Date.now() - date.getTime()) / 36e5));
      const relative = hours < 24 ? `vor ${hours} h` : `vor ${Math.round(hours / 24)} d`;
      return `${relative} · ${date.toLocaleDateString([], { day: "2-digit", month: "2-digit" })}`;
    }

    function relativeHours(hours) {
      const abs = Math.abs(Math.round(hours));
      if (abs < 24) return `${abs} h`;
      return `${Math.round(abs / 24)} d`;
    }

    function formatNextDue(lastCleaned, intervalH) {
      if (!lastCleaned) return "jetzt fällig · noch nie gereinigt";
      const last = new Date(lastCleaned);
      if (Number.isNaN(last.getTime())) return "unbekannt";
      const due = new Date(last.getTime() + Number(intervalH || 0) * 36e5);
      const hoursUntil = (due.getTime() - Date.now()) / 36e5;
      const relative = hoursUntil <= 0
        ? `fällig seit ${relativeHours(hoursUntil)}`
        : `fällig in ${relativeHours(hoursUntil)}`;
      return `${relative} · ${due.toLocaleDateString([], { day: "2-digit", month: "2-digit" })}`;
    }

    function roomKey(item) {
      return String(item?.room_key || item?.id || item?.room || "");
    }

    function storedRoomOrder() {
      try {
        const value = JSON.parse(localStorage.getItem(roomOrderStorageKey) || "[]");
        return Array.isArray(value) ? value.map(String) : [];
      } catch {
        return [];
      }
    }

    function saveRoomOrder(order) {
      localStorage.setItem(roomOrderStorageKey, JSON.stringify(order));
    }

    function orderedRooms(queue) {
      const order = storedRoomOrder();
      if (!order.length) return queue;
      const orderIndex = new Map(order.map((key, index) => [key, index]));
      return [...queue].sort((a, b) => {
        const aIndex = orderIndex.has(roomKey(a)) ? orderIndex.get(roomKey(a)) : Number.MAX_SAFE_INTEGER;
        const bIndex = orderIndex.has(roomKey(b)) ? orderIndex.get(roomKey(b)) : Number.MAX_SAFE_INTEGER;
        if (aIndex !== bIndex) return aIndex - bIndex;
        return queue.indexOf(a) - queue.indexOf(b);
      });
    }

    function handlePlanDragStart(event) {
      const card = event.currentTarget;
      draggedRoomKey = card.dataset.roomKey;
      card.classList.add("dragging");
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", draggedRoomKey);
    }

    function handlePlanDragEnd(event) {
      event.currentTarget.classList.remove("dragging");
      draggedRoomKey = null;
    }

    function handlePlanDragOver(event) {
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
    }

    function handlePlanDrop(event) {
      event.preventDefault();
      if (!latestSummary) return;
      const targetCard = event.currentTarget;
      const targetKey = targetCard.dataset.roomKey;
      const sourceKey = draggedRoomKey || event.dataTransfer.getData("text/plain");
      if (!sourceKey || !targetKey || sourceKey === targetKey) return;

      const queue = latestSummary.status.room_queue || [];
      const order = orderedRooms(queue).map(roomKey);
      const sourceIndex = order.indexOf(sourceKey);
      const targetIndex = order.indexOf(targetKey);
      if (sourceIndex < 0 || targetIndex < 0) return;
      order.splice(sourceIndex, 1);
      order.splice(targetIndex, 0, sourceKey);
      saveRoomOrder(order);
      renderQueue(latestSummary);
    }

    function personDistance(person) {
      const value = Number(person?.distance_km);
      return Number.isFinite(value) ? value : null;
    }

    function personTravelTime(person) {
      const value = Number(person?.travel_time_min);
      return Number.isFinite(value) ? value : null;
    }

    function nearestPerson(people) {
      const withTravel = people
        .map(person => ({
          person,
          travelTime: personTravelTime(person),
          distance: personDistance(person)
        }))
        .filter(item => item.travelTime !== null || item.distance !== null);
      if (!withTravel.length) return null;
      withTravel.sort((a, b) => {
        const aValue = a.travelTime ?? Number.MAX_SAFE_INTEGER;
        const bValue = b.travelTime ?? Number.MAX_SAFE_INTEGER;
        if (aValue !== bValue) return aValue - bValue;
        return (a.distance ?? Number.MAX_SAFE_INTEGER) - (b.distance ?? Number.MAX_SAFE_INTEGER);
      });
      return withTravel[0].person;
    }

    function personDistanceLabel(person, fallback = null) {
      const value = personDistance(person);
      if (value !== null) return `${value} km`;
      return fallback ?? "-";
    }

    function personTravelLabel(person, fallback = null) {
      const value = personTravelTime(person);
      if (value !== null) return `${value} min`;
      return fallback ?? "-";
    }

    function localApiUrl(path) {
      const cleanedPath = String(path || "").replace(/^\/+/, "");
      const basePath = window.location.pathname.replace(/\/+$/, "");
      return `${basePath}/${cleanedPath}`;
    }

    async function api(path, options = {}) {
      const response = await fetch(localApiUrl(path), {
        headers: { "Content-Type": "application/json" },
        ...options
      });
      if (!response.ok) {
        const text = await response.text();
        throw new Error(text || `HTTP ${response.status}`);
      }
      const contentType = response.headers.get("content-type") || "";
      if (contentType.includes("application/json")) return response.json();
      return response.text();
    }

    async function toggleBoolean(entityId) {
      await api(`/api/toggle?entity_id=${encodeURIComponent(entityId)}`, { method: "POST" });
      await load();
    }

    async function setNumber(entityId) {
      const input = document.querySelector(`[data-input='${entityId}']`);
      if (!input) return;
      await api("/api/set_number", {
        method: "POST",
        body: JSON.stringify({ entity_id: entityId, value: Number(input.value) })
      });
      await load();
    }

    // Demo data for robot status (will be replaced with real data later)
    const demoRobot = {
      battery: 78,
      battery_charging: false,
      bin_full: false,
      bin_almost_full: true,
      water_tank_empty: false,
      water_tank_low: false,
      mop_attached: true,
      error: null
    };

    function renderHero(summary) {
      const states = summary.states.sensors || {};
      const status = summary.status || {};
      const activeRoomState = states.active_room || {};
      const activeRoomAttrs = activeRoomState.attributes || {};
      const statusText = entityState(states.status, "Idle");
      const activeRoom = entityState(activeRoomState, "");
      const isCleaning = activeRoom && activeRoom !== "unknown" && activeRoom !== "None" && activeRoom !== "";

      // Hero icon and badge
      const iconEl = document.getElementById("status-icon");
      const badgeEl = document.getElementById("hero-badge");
      const titleEl = document.getElementById("hero-title");
      const reasonEl = document.getElementById("hero-reason");
      const progressEl = document.getElementById("hero-progress");

      if (isCleaning) {
        iconEl.className = "status-icon cleaning";
        iconEl.textContent = "🧹";
        badgeEl.className = "hero-badge active";
        badgeEl.textContent = "Reinigt";
        titleEl.textContent = `Reinigt ${escapeHtml(activeRoom)}`;
        reasonEl.textContent = status.reason || "Automatisch gestartet";

        // Show progress
        progressEl.style.display = "flex";
        const remaining = activeRoomAttrs.remaining_min ?? 0;
        const planned = activeRoomAttrs.planned_duration_min ?? 15;
        const elapsed = planned - remaining;
        const percent = Math.min(100, Math.max(0, (elapsed / planned) * 100));
        document.getElementById("progress-room").textContent = activeRoom;
        document.getElementById("progress-time").textContent = `noch ${remaining} min`;
        document.getElementById("progress-fill").style.width = `${percent}%`;
      } else {
        iconEl.className = "status-icon idle";
        iconEl.textContent = "🤖";
        badgeEl.className = "hero-badge idle";
        badgeEl.textContent = statusText;
        titleEl.textContent = statusText === "Ready" ? "Bereit" : statusText;
        reasonEl.textContent = status.reason || "Wartet auf Startbedingungen";
        progressEl.style.display = "none";
      }
    }

    function renderRobotStatus() {
      // Using demo data for now
      const robot = demoRobot;
      const stats = [];

      // Battery
      const batteryIcon = robot.battery_charging ? "⚡" : "🔋";
      const batteryClass = robot.battery < 20 ? "error" : robot.battery < 40 ? "warning" : "ok";
      const batteryText = robot.battery_charging ? `${robot.battery}% lädt` : `${robot.battery}%`;
      stats.push({ icon: batteryIcon, iconClass: batteryClass, value: batteryText, label: "Batterie" });

      // Dust bin
      const binIcon = "🗑️";
      const binClass = robot.bin_full ? "error" : robot.bin_almost_full ? "warning" : "ok";
      const binText = robot.bin_full ? "Voll" : robot.bin_almost_full ? "Fast voll" : "OK";
      stats.push({ icon: binIcon, iconClass: binClass, value: binText, label: "Staubbehälter" });

      // Water tank
      const waterIcon = "💧";
      const waterClass = robot.water_tank_empty ? "error" : robot.water_tank_low ? "warning" : "ok";
      const waterText = robot.water_tank_empty ? "Leer" : robot.water_tank_low ? "Niedrig" : "OK";
      stats.push({ icon: waterIcon, iconClass: waterClass, value: waterText, label: "Wassertank" });

      // Mop
      const mopIcon = "🧹";
      const mopClass = "";
      const mopText = robot.mop_attached ? "Eingesetzt" : "Nicht eingesetzt";
      stats.push({ icon: mopIcon, iconClass: mopClass, value: mopText, label: "Mop" });

      // Error
      if (robot.error) {
        stats.push({ icon: "⚠️", iconClass: "error", value: robot.error, label: "Fehler" });
      }

      document.getElementById("robot-status").innerHTML = stats.map(stat => `
        <div class="robot-stat">
          <div class="robot-stat-icon ${stat.iconClass}">${stat.icon}</div>
          <div class="robot-stat-text">
            <strong>${escapeHtml(stat.value)}</strong>
            <span>${escapeHtml(stat.label)}</span>
          </div>
        </div>
      `).join("");
    }

    function renderPresence(summary) {
      const states = summary.states.sensors || {};
      const status = summary.status || {};
      const globalStates = summary.states.global || {};
      const people = status.presence_summary || [];
      const homePeople = people.filter(personHome);
      const awayPeople = people.filter(p => !personHome(p));
      const returnWindow = Number(entityState(states.return_window, 0));

      // Summary badge
      const summaryEl = document.getElementById("presence-summary");
      if (homePeople.length > 0) {
        summaryEl.className = "presence-summary blocked";
        summaryEl.textContent = `${homePeople.length} zuhause`;
      } else {
        summaryEl.className = "presence-summary clear";
        summaryEl.textContent = "Niemand zuhause";
      }

      // Time window with calculation details
      const travelTime = Number(entityState(states.travel_time, 0));
      const returnBuffer = Number(entityState(globalStates.return_buffer, 5));
      const closest = nearestPerson(people);
      const closestTravel = personTravelTime(closest);
      const closestName = closest ? personDisplayName(closest) : null;

      let detailHtml = "";
      if (homePeople.length > 0) {
        detailHtml = "Blockiert — jemand ist zuhause";
      } else if (closestTravel !== null) {
        detailHtml = `
          <div class="calc-row">
            <span class="value">${closestTravel} min</span>
            <span class="op">−</span>
            <span class="value muted">${returnBuffer} min</span>
            <span class="label">Puffer</span>
          </div>`;
      } else if (travelTime > 0) {
        detailHtml = `
          <div class="calc-row">
            <span class="value">${travelTime} min</span>
            <span class="op">−</span>
            <span class="value muted">${returnBuffer} min</span>
            <span class="label">Puffer</span>
          </div>`;
      } else {
        detailHtml = "Keine Reisedaten verfügbar";
      }

      document.getElementById("time-window-value").textContent = returnWindow > 0 ? `${returnWindow} min` : "—";
      document.getElementById("time-window-detail").innerHTML = detailHtml;

      // Person list
      if (!people.length) {
        document.getElementById("presence-list").innerHTML = '<div class="empty">Keine Personen konfiguriert</div>';
        return;
      }

      document.getElementById("presence-list").innerHTML = people.map(person => {
        const isHome = personHome(person);
        const name = personDisplayName(person);
        const initials = name.split(" ").map(n => n[0]).join("").toUpperCase().slice(0, 2);
        const distance = personDistance(person);
        const travel = personTravelTime(person);

        let statusHtml = "";
        if (isHome) {
          statusHtml = '<span class="pill home">zuhause</span>';
        } else if (travel !== null || distance !== null) {
          const parts = [];
          if (distance !== null) parts.push(`${distance} km`);
          if (travel !== null) parts.push(`${travel} min`);
          statusHtml = `<span>${parts.join(" · ")}</span><span class="pill away">weg</span>`;
        } else {
          statusHtml = '<span class="pill away">weg</span>';
        }

        return `
          <div class="person-row">
            <div class="person-info">
              <div class="person-avatar">${escapeHtml(initials)}</div>
              <span class="person-name">${escapeHtml(name)}</span>
            </div>
            <div class="person-status">${statusHtml}</div>
          </div>
        `;
      }).join("");
    }

    function renderToggles(summary) {
      const entities = summary.entities.global || {};
      const states = summary.states.global || {};
      document.getElementById("toggle-controls").innerHTML = Object.entries(toggleMeta).map(([key, [title, desc]]) => {
        const entityId = entities[key];
        const on = boolOn(states[key]);
        return `
          <div class="control">
            <div class="name">${escapeHtml(title)}</div>
            <div class="desc">${escapeHtml(desc)}</div>
            <button class="${on ? "" : "off"}" onclick="toggleBoolean('${entityId}')">
              ${on ? "An" : "Aus"}
            </button>
          </div>
        `;
      }).join("");
    }

    function renderNumbers(summary) {
      const entities = summary.entities.global || {};
      const states = summary.states.global || {};
      document.getElementById("global-numbers").innerHTML = Object.entries(numberMeta).map(([key, label]) => {
        const entityId = entities[key];
        const value = entityState(states[key], "");
        return `
          <div class="number-control">
            <label>${escapeHtml(label)}</label>
            <div class="number-inline">
              <input type="number" step="any" value="${escapeHtml(value)}" data-input="${entityId}">
              <button onclick="setNumber('${entityId}')">OK</button>
            </div>
          </div>
        `;
      }).join("");
    }

    function renderQueue(summary) {
      const queue = summary.status.room_queue || [];
      const roomStats = summary.status.room_stats || [];
      if (!queue.length) {
        document.getElementById("room-list").innerHTML = '<div class="empty">Keine Räume konfiguriert</div>';
        return;
      }

      // Show only top 3 rooms
      const topRooms = orderedRooms(queue).slice(0, 3);

      document.getElementById("room-list").innerHTML = topRooms.map((item, index) => {
        const stats = roomStats.find(stat => stat.room_key === item.room_key || stat.room === item.room) || {};
        const lastCleaned = formatLastCleaned(stats.last_cleaned);

        // Calculate due status
        const intervalH = item.interval_h || 24;
        const lastCleanedDate = stats.last_cleaned ? new Date(stats.last_cleaned) : null;
        let dueTag = '';
        if (lastCleanedDate) {
          const now = new Date();
          const hoursSinceCleaned = (now - lastCleanedDate) / (1000 * 60 * 60);
          const hoursUntilDue = intervalH - hoursSinceCleaned;
          const daysUntilDue = Math.round(hoursUntilDue / 24);

          if (hoursUntilDue <= 0) {
            dueTag = '<span class="room-tag overdue"><span class="icon">⏰</span> überfällig</span>';
          } else if (daysUntilDue <= 1) {
            dueTag = '<span class="room-tag due"><span class="icon">⏰</span> heute</span>';
          } else {
            dueTag = `<span class="room-tag due"><span class="icon">⏰</span> in ${daysUntilDue}d</span>`;
          }
        }

        return `
          <div class="room-row ${index === 0 ? "next" : ""}">
            <div class="room-rank">${index + 1}</div>
            <div class="room-info">
              <div class="room-header">
                <span class="room-name">${escapeHtml(item.room)}</span>
                <div class="room-tags">
                  <span class="room-tag last"><span class="icon">✓</span> ${escapeHtml(lastCleaned)}</span>
                  ${dueTag}
                </div>
              </div>
            </div>
            <div class="room-duration">
              <strong>${escapeHtml(item.effective_duration_min)} min</strong>
              <span>${item.fits_now ? "passt" : "zu lang"}</span>
            </div>
          </div>
        `;
      }).join("");
    }

    function renderRooms(summary) {
      const rooms = summary.states.rooms || [];
      const roomStats = summary.status.room_stats || [];
      if (!rooms.length) {
        document.getElementById("room-grid").innerHTML = '<div class="empty">Keine Raum-Helper konfiguriert.</div>';
        return;
      }
      document.getElementById("room-grid").innerHTML = rooms.map(room => {
        const stats = roomStats.find(item => item.room_key === room.id) || {};
        const enabledOn = boolOn(room.enabled_state);
        return `
          <div class="room-card">
            <div class="room-card-header">
              <strong>${escapeHtml(room.name)}</strong>
              <span class="pill ${enabledOn ? "away" : "home"}">${enabledOn ? "aktiv" : "aus"}</span>
            </div>
            <button class="${enabledOn ? "" : "off"}" onclick="toggleBoolean('${room.enabled}')">
              ${enabledOn ? "Deaktivieren" : "Aktivieren"}
            </button>
            <div class="room-config">
              <div class="room-config-row"><span>Dauer</span><span>${escapeHtml(stats.learned_duration_min ?? stats.configured_duration_min ?? "-")} min</span></div>
              <div class="room-config-row"><span>Intervall</span><span>${escapeHtml(entityState(room.interval_h_state, "-"))} h</span></div>
              <div class="room-config-row"><span>Läufe</span><span>${escapeHtml(stats.completed_runs ?? 0)}</span></div>
            </div>
            <div class="number-grid" style="margin-top: 12px;">
              ${[
                ["weight", "Gewichtung", room.weight],
                ["interval_h", "Intervall (h)", room.interval_h],
                ["duration_min", "Dauer (min)", room.duration_min]
              ].map(([key, label, entityId]) => `
                <div class="number-control">
                  <label>${escapeHtml(label)}</label>
                  <div class="number-inline">
                    <input type="number" step="any" value="${escapeHtml(entityState(room[`${key}_state`], ""))}" data-input="${entityId}">
                    <button onclick="setNumber('${entityId}')">OK</button>
                  </div>
                </div>
              `).join("")}
            </div>
          </div>
        `;
      }).join("");
    }

    function renderConfig(summary) {
      const options = summary.options || {};
      const rows = [
        ["Staubsauger", options.vacuum_entity],
        ["Benachrichtigung", options.notify_service || "deaktiviert"],
        ["Bewohner", options.presence_entities],
        ["Reise-Person", options.travel_person_entity],
        ["Home Zone", options.home_zone],
        ["Historie", `${options.history_weeks ?? "-"} Wochen`]
      ];
      document.getElementById("config-grid").innerHTML = rows.map(([label, value]) => `
        <div>
          <strong>${escapeHtml(label)}</strong>
          <span>${escapeHtml(value ?? "n/a")}</span>
        </div>
      `).join("");
    }

    function render(summary) {
      latestSummary = summary;
      renderHero(summary);
      renderRobotStatus();
      renderPresence(summary);
      renderQueue(summary);
      renderToggles(summary);
      renderNumbers(summary);
      renderRooms(summary);
      renderConfig(summary);
    }

    async function load() {
      try {
        const summary = await api("/api/summary");
        render(summary);
      } catch (error) {
        document.body.innerHTML = `<div class="shell"><div class="card"><h2>Dashboard konnte nicht geladen werden</h2><p>${escapeHtml(error.message)}</p></div></div>`;
      }
    }

    load();
    setInterval(load, 15000);
  </script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/summary":
            self._json_response(build_summary())
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(render_dashboard_html().encode("utf-8"))

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/start_room":
            self._handle_start_room()
            return
        if parsed.path == "/api/stop_cleaning":
            self._handle_stop_cleaning()
            return
        if parsed.path == "/api/set_room_due":
            self._handle_set_room_due()
            return
        if parsed.path == "/api/prioritize_room":
            self._handle_prioritize_room()
            return
        if parsed.path == "/api/toggle":
            self._handle_toggle(parsed)
            return
        if parsed.path == "/api/set_number":
            self._handle_set_number()
            return
        self.send_response(404)
        self.end_headers()

    def _handle_prioritize_room(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception:
            self._json_response({"ok": False, "error": "invalid payload"}, status=400)
            return

        room_key = str(payload.get("room_key") or "").strip()
        options = load_options()
        rooms = parse_rooms(options.get("rooms"))
        valid_room_ids = {room["id"] for room in rooms}
        if room_key not in valid_room_ids:
            self._json_response({"ok": False, "error": "unknown room"}, status=400)
            return

        helper_prefix = options.get("helper_prefix", "vacuum_automation")
        entity_id = options.get(
            "one_time_room_override_entity",
            f"input_text.{helper_prefix}_one_time_room_override",
        )

        try:
            service_call("input_text", "set_value", entity_id, {"value": room_key})
            self._json_response({"ok": True, "room_key": room_key})
        except urllib.error.HTTPError as err:
            self._json_response({"ok": False, "error": str(err)}, status=502)
        except Exception as err:
            self._json_response({"ok": False, "error": str(err)}, status=500)

    def _room_from_payload(self) -> tuple[dict, str] | tuple[None, None]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception:
            self._json_response({"ok": False, "error": "invalid payload"}, status=400)
            return None, None

        room_key = str(payload.get("room_key") or "").strip()
        options = load_options()
        rooms = parse_rooms(options.get("rooms"))
        valid_room_ids = {room["id"] for room in rooms}
        if room_key not in valid_room_ids:
            self._json_response({"ok": False, "error": "unknown room"}, status=400)
            return None, None
        return payload, room_key

    def _handle_start_room(self):
        payload, room_key = self._room_from_payload()
        if payload is None:
            return

        try:
            fire_event(EVENT_START_ROOM, {"room_key": room_key})
            self._json_response({"ok": True, "room_key": room_key})
        except urllib.error.HTTPError as err:
            self._json_response({"ok": False, "error": str(err)}, status=502)
        except Exception as err:
            self._json_response({"ok": False, "error": str(err)}, status=500)

    def _handle_stop_cleaning(self):
        options = load_options()
        vacuum_entity = options.get("vacuum_entity", "")
        if not vacuum_entity:
            self._json_response({"ok": False, "error": "no vacuum configured"}, status=400)
            return

        try:
            service_call("vacuum", "return_to_base", vacuum_entity)
            self._json_response({"ok": True})
        except urllib.error.HTTPError as err:
            self._json_response({"ok": False, "error": str(err)}, status=502)
        except Exception as err:
            self._json_response({"ok": False, "error": str(err)}, status=500)

    def _handle_set_room_due(self):
        payload, room_key = self._room_from_payload()
        if payload is None:
            return

        due = bool(payload.get("due"))
        try:
            fire_event(EVENT_SET_ROOM_DUE, {"room_key": room_key, "due": due})
            self._json_response({"ok": True, "room_key": room_key, "due": due})
        except urllib.error.HTTPError as err:
            self._json_response({"ok": False, "error": str(err)}, status=502)
        except Exception as err:
            self._json_response({"ok": False, "error": str(err)}, status=500)

    def _handle_toggle(self, parsed):
        query = urllib.parse.parse_qs(parsed.query)
        entity_id = (query.get("entity_id") or [""])[0]
        if not entity_id.startswith("input_boolean."):
            self._json_response({"ok": False, "error": "unsupported entity"}, status=400)
            return

        try:
            service_call("input_boolean", "toggle", entity_id)
            self._json_response({"ok": True})
        except urllib.error.HTTPError as err:
            self._json_response({"ok": False, "error": str(err)}, status=502)
        except Exception as err:
            self._json_response({"ok": False, "error": str(err)}, status=500)

    def _handle_set_number(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._json_response({"ok": False, "error": "invalid payload"}, status=400)
            return

        entity_id = str(payload.get("entity_id") or "")
        value = payload.get("value")
        if not entity_id.startswith("input_number."):
            self._json_response({"ok": False, "error": "unsupported entity"}, status=400)
            return

        try:
            service_call("input_number", "set_value", entity_id, {"value": value})
            self._json_response({"ok": True})
        except urllib.error.HTTPError as err:
            self._json_response({"ok": False, "error": str(err)}, status=502)
        except Exception as err:
            self._json_response({"ok": False, "error": str(err)}, status=500)

    def _json_response(self, payload: dict, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode("utf-8"))

    def log_message(self, format, *args):
        return


def main():
    port = int(os.environ.get("SIDEBAR_REDIRECT_PORT", "8099"))
    server = HTTPServer(("0.0.0.0", port), DashboardHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
