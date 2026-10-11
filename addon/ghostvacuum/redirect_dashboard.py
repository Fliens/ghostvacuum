#!/usr/bin/env python3
"""Ingress dashboard for the vacuum automation add-on."""

from __future__ import annotations

import json
import os
import html
import re
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
LOCAL_DASHBOARD_PATH = Path(__file__).parent / "dashboard" / "index.html"
LOCAL_STYLES_PATH = Path(__file__).parent / "dashboard" / "styles.css"
LOCAL_SCRIPTS_PATH = Path(__file__).parent / "dashboard" / "scripts.js"
LOCAL_SETTINGS_PATH = Path(__file__).parent / "dashboard" / "settings.html"
LOCAL_STATIC_PATH = Path(__file__).parent / "dashboard" / "static"
MOCK_STATES: dict[str, dict] | None = None
ADDON_NAME = "GhostVacuum"
DEFAULT_PRESENCE_ENTITIES = ["person.resident_1"]
WEEKDAY_OPTIONS = [
    ("mon", "Mo"),
    ("tue", "Di"),
    ("wed", "Mi"),
    ("thu", "Do"),
    ("fri", "Fr"),
    ("sat", "Sa"),
    ("sun", "So"),
]
DEFAULT_ACTIVE_WEEKDAYS = "mon,tue,wed,thu,fri,sat"


def load_dashboard_template() -> str:
  # Assemble dashboard from partials when available (styles, body, scripts)
  body = LOCAL_DASHBOARD_PATH.read_text(encoding="utf-8") if LOCAL_DASHBOARD_PATH.exists() else ""
  styles = LOCAL_STYLES_PATH.read_text(encoding="utf-8") if LOCAL_STYLES_PATH.exists() else ""
  scripts = LOCAL_SCRIPTS_PATH.read_text(encoding="utf-8") if LOCAL_SCRIPTS_PATH.exists() else ""
  parts = []
  if styles and "</head>" in body:
    body = body.replace("</head>", f'<style>\n{styles}\n</style>\n</head>', 1)

  # If body contains a closing </body>, inject scripts right before it so the
  # assembled page stays valid. Otherwise append body and scripts in sequence.
  if body:
    if scripts and "</body>" in body:
      body = body.replace("</body>", f'<script>\n{scripts}\n</script></body>', 1)
    parts.append(body)
  else:
    if scripts:
      parts.append(f'<script>\n{scripts}\n</script>')

  if parts:
    return "".join(parts)
  return HTML


def load_settings_template() -> str:
    if LOCAL_SETTINGS_PATH.exists():
        return LOCAL_SETTINGS_PATH.read_text(encoding="utf-8")
    return SETTINGS_HTML


def local_dev_enabled() -> bool:
    return os.environ.get(LOCAL_DEV_ENV, "").lower() in {"1", "true", "yes", "on"}


def load_options() -> dict:
    override_path = os.environ.get(LOCAL_OPTIONS_ENV)
    if override_path:
        loaded = yaml.safe_load(Path(override_path).read_text()) or {}
        return loaded.get("options", loaded)
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


def parse_presence_entities(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    if not text:
        return list(DEFAULT_PRESENCE_ENTITIES)
    try:
        parsed = yaml.safe_load(text)
    except Exception:
        parsed = None
    if isinstance(parsed, list):
        return [str(item).strip() for item in parsed if str(item).strip()]
    items = [part.strip() for part in re.split(r"[\n,]+", text)]
    return [item for item in items if item] or list(DEFAULT_PRESENCE_ENTITIES)


def slugify(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_") or "segment"


def parse_json_mapping(value: Any) -> dict:
    text = str(value or "").strip()
    if not text or text.lower() in {"unknown", "unavailable"}:
        return {}
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(key): str(val) for key, val in parsed.items() if str(val).strip()}


def segment_id_value(value: Any) -> Optional[int]:
    if value in [None, ""]:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def extract_vacuum_segments(value: Any, depth: int = 0, room_context: bool = False) -> List[dict]:
    if depth > 5:
        return []
    segments: List[dict] = []
    if isinstance(value, dict):
        direct_id = None
        id_keys = ["segment_id", "segmentId", "segment", "room_id", "roomId"]
        if room_context and value.get("name"):
            id_keys.append("id")
        for key in id_keys:
            direct_id = segment_id_value(value.get(key))
            if direct_id is not None:
                break
        if direct_id is not None:
            name = (
                value.get("name")
                or value.get("room_name")
                or value.get("roomName")
                or value.get("friendly_name")
                or value.get("label")
                or f"Segment {direct_id}"
            )
            segments.append({"segment_id": direct_id, "name": str(name)})

        for key, item in value.items():
            if isinstance(item, dict) and str(key).isdigit():
                nested_name = (
                    item.get("name")
                    or item.get("room_name")
                    or item.get("roomName")
                    or item.get("friendly_name")
                    or f"Segment {key}"
                )
                segments.append({"segment_id": int(key), "name": str(nested_name)})
            segments.extend(extract_vacuum_segments(item, depth + 1, room_context or key in {"rooms", "segments"}))
    elif isinstance(value, list):
        for item in value:
            segments.extend(extract_vacuum_segments(item, depth + 1, room_context))

    unique: dict[int, dict] = {}
    for item in segments:
        segment_id = segment_id_value(item.get("segment_id"))
        if segment_id is None:
            continue
        unique[segment_id] = {"segment_id": segment_id, "name": str(item.get("name") or f"Segment {segment_id}")}
    return [unique[key] for key in sorted(unique)]


def room_name_overrides_from_summary(summary: dict) -> dict:
    global_states = summary.get("states", {}).get("global", {})
    return parse_json_mapping(state_value(global_states.get("room_names"), ""))


def selected_presence_from_summary(summary: dict) -> List[str]:
    global_states = summary.get("states", {}).get("global", {})
    raw = state_value(global_states.get("selected_presence_entities"), "")
    parsed = parse_presence_entities(raw)
    return parsed if raw else parse_presence_entities(summary.get("options", {}).get("presence_entities"))


CLEANING_PROFILE_DEFAULT = {"mode":"cleangenius","genius":"routine_cleaning","sequence":"vacuum_and_mop","suction":"strong","moisture":"wet","route":"deep","repeats":1}
CLEANING_PROFILE_CHOICES = {
    "mode": [("cleangenius","CleanGenius"),("sweeping","Nur saugen"),("mopping","Nur wischen"),("sweeping_and_mopping","Saugen & Wischen gemeinsam"),("mopping_after_sweeping","Erst saugen, dann wischen")],
    "genius": [("routine_cleaning","Routine"),("deep_cleaning","Gründlich")],
    "sequence": [("vacuum_and_mop","Saugen & Wischen gemeinsam"),("mop_after_vacuum","Erst saugen, dann wischen")],
    "suction": [("quiet","Leise"),("standard","Standard"),("strong","Stark"),("turbo","Turbo")],
    "moisture": [("slightly_dry","Wenig"),("moist","Mittel"),("wet","Viel")],
    "route": [("quick","Schnell"),("standard","Standard"),("intensive","Intensiv"),("deep","Gründlich")],
    "repeats": [(1,"1×"),(2,"2×"),(3,"3×")],
}

def valid_cleaning_profile(value, inherit=False):
    return (isinstance(value, dict) and ((inherit and not value) or (
        set(value) == set(CLEANING_PROFILE_CHOICES) and all(
            type(value[k]) is type(CLEANING_PROFILE_DEFAULT[k]) and value[k] in [v for v, label in choices]
            for k, choices in CLEANING_PROFILE_CHOICES.items()))))


def editable_setting_entities(options: dict) -> set[str]:
    """Only configuration helpers may be written while observing."""
    entities = helper_entities(options, parse_rooms(options.get("rooms")))
    allowed = {value for key, value in entities["global"].items()
               if key not in {"enabled", "one_time_room_override"}}
    for room in entities["rooms"]:
        allowed.update(room[key] for key in ("enabled", "weight", "interval_h", "duration_min", "completion_percent", "cleaning_profile"))
    return allowed


def observation_setting_write(path: str, method: str, payload: dict | None, options: dict) -> bool:
    if method != "POST" or not isinstance(payload, dict):
        return False
    entity = payload.get("entity_id")
    if not isinstance(entity, str) or entity not in editable_setting_entities(options):
        return False
    domain = entity.split(".", 1)[0]
    service = {"input_boolean": "toggle", "input_number": "set_value", "input_text": "set_value"}.get(domain)
    return service is not None and path == f"/services/{domain}/{service}"


def api_request(path: str, method: str = "GET", payload: dict | None = None):
    options = load_options()
    if options.get("observation_mode", False) and method != "GET" and not observation_setting_write(path, method, payload, options):
        raise PermissionError("Beobachtungsmodus: Gerätebefehle und Automatik-Aktivierung gesperrt")
    token_file = os.environ.get("VACUUM_DASHBOARD_TOKEN_FILE")
    token = (
        yaml.safe_load(Path(token_file).read_text())["ha_token"]
        if token_file else os.environ.get("SUPERVISOR_TOKEN", "")
    )
    api_base = os.environ.get("VACUUM_DASHBOARD_API", SUPERVISOR_CORE_API).rstrip("/")
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(
        f"{api_base}{path}",
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

    return next((state for state in all_states() if state.get("entity_id") == entity_id), None)


_all_states_cache: dict = {"states": None, "timestamp": 0}
_ALL_STATES_CACHE_TTL = 2.0  # seconds


def all_states():
    if local_dev_enabled():
        return list(mock_states().values())

    import time
    now = time.time()

    # Return cached states if still fresh
    if _all_states_cache["states"] is not None and (now - _all_states_cache["timestamp"]) < _ALL_STATES_CACHE_TTL:
        return _all_states_cache["states"]

    try:
        states = api_request("/states")
        result = states if isinstance(states, list) else []
        _all_states_cache["states"] = result
        _all_states_cache["timestamp"] = now
        return result
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
    result = api_request(
        f"/services/{domain}/{service}",
        method="POST",
        payload=payload,
    )
    # A settings refresh must not reuse the snapshot from before this write.
    _all_states_cache["states"] = None
    _all_states_cache["timestamp"] = 0
    return result


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
                "completion_percent": f"input_number.{helper_prefix}_{room_id}_completion_percent",
                "cleaning_profile": f"input_text.{helper_prefix}_{room_id}_cleaning_profile",
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
            "cleaning_defaults": options.get("cleaning_defaults_entity", f"input_text.{helper_prefix}_cleaning_defaults"),
            "tracking_mode": options.get("tracking_mode_entity", f"input_text.{helper_prefix}_tracking_mode"),
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
            "room_names": options.get(
                "room_names_entity",
                f"input_text.{helper_prefix}_room_names",
            ),
            "selected_presence_entities": options.get(
                "selected_presence_entities_entity",
                f"input_text.{helper_prefix}_selected_presence_entities",
            ),
            "active_weekdays": options.get(
                "active_weekdays_entity",
                f"input_text.{helper_prefix}_active_weekdays",
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
            "start_margin": options.get("start_margin_entity", f"input_number.{helper_prefix}_start_margin"),
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
            value = options.get("travel_pause_radius_km", 100)
        elif key == "max_distance_km":
            value = options.get("max_distance_km", 0)
        elif key == "active_weekdays":
            value = options.get("active_weekdays", DEFAULT_ACTIVE_WEEKDAYS)
        elif key == "room_names":
            value = options.get("room_names", "{}")
        elif key == "selected_presence_entities":
            value = ",".join(parse_presence_entities(options.get("presence_entities")))
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
        "travel_pause_radius_km": options.get("travel_pause_radius_km", 100),
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
            "segments": [
                {"segment_id": 1, "name": "Segment 1"},
                {"segment_id": 2, "name": "Segment 2"},
                {"segment_id": 3, "name": "Segment 3"},
                {"segment_id": 4, "name": "Segment 4"},
            ],
        },
    )
    states["person.resident_1"] = mock_state(
        "person.resident_1",
        "not_home",
        {"friendly_name": "Resident 1", "latitude": 52.6, "longitude": 13.5},
    )
    states["person.resident_2"] = mock_state(
        "person.resident_2",
        "not_home",
        {"friendly_name": "Resident 2", "latitude": 52.7, "longitude": 13.6},
    )
    states["person.gast"] = mock_state(
        "person.gast",
        "home",
        {"friendly_name": "Gast", "latitude": 52.52, "longitude": 13.405},
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
        for key in ["enabled", "weight", "interval_h", "duration_min", "completion_percent", "cleaning_profile"]:
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


PERSON_DEVICE_LOCK = threading.Lock()


def wifi_presence_candidates(states):
    personal = re.compile(r"iphone|phone|android|pixel|galaxy|ipad|tablet|watch|macbook|laptop|notebook", re.I)
    result = []
    for state in states:
        entity = str(state.get('entity_id', ''))
        attrs = state_attrs(state)
        name = str(attrs.get('friendly_name') or entity.split('.')[-1].replace('_', ' '))
        if entity.startswith('device_tracker.') and attrs.get('source_type') == 'router' and personal.search(name + ' ' + entity):
            result.append({'entity_id': entity, 'name': name, 'state': state.get('state')})
    return sorted(result, key=lambda item: (item['name'].casefold(), item['entity_id']))


def person_wifi_trackers(person, states):
    by_id = {item.get('entity_id'): item for item in states}
    return [entity for entity in person.get('device_trackers', [])
            if state_attrs(by_id.get(entity, {})).get('source_type') == 'router']


def updated_person_trackers(person, selected, states, other_people):
    candidates = {item['entity_id'] for item in wifi_presence_candidates(states)}
    if selected and selected not in candidates:
        raise ValueError('Bitte ein geeignetes WLAN-Gerät auswählen.')
    if selected and any(selected in item.get('device_trackers', []) for item in other_people):
        raise ValueError('Dieses Gerät ist bereits einer anderen Person zugeordnet.')
    old_wifi = set(person_wifi_trackers(person, states))
    trackers = [entity for entity in person.get('device_trackers', []) if entity not in old_wifi]
    if selected:
        trackers.append(selected)
    return list(dict.fromkeys(trackers))


def save_person_wifi(person_entity, selected):
    # HA combines router presence with the existing GPS trackers. No robot commands.
    import asyncio
    import aiohttp
    if not isinstance(person_entity, str) or not person_entity.startswith('person.') or not isinstance(selected, str):
        raise ValueError('Ungültige Gerätezuordnung.')
    state = state_for(person_entity)
    person_id = state_attrs(state).get('id')
    if not person_id:
        raise ValueError('Person nicht verfügbar.')
    states = all_states()
    async def update():
        token_file = os.environ.get('VACUUM_DASHBOARD_TOKEN_FILE')
        token = yaml.safe_load(Path(token_file).read_text())['ha_token'] if token_file else os.environ.get('SUPERVISOR_TOKEN', '')
        base = os.environ.get('VACUUM_DASHBOARD_API', SUPERVISOR_CORE_API).rstrip('/')
        url = base.replace('https://', 'wss://', 1).replace('http://', 'ws://', 1) + '/websocket'
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.ws_connect(url) as ws:
                await ws.receive_json(timeout=10)
                await ws.send_json({'type':'auth', 'access_token':token})
                if (await ws.receive_json(timeout=10)).get('type') != 'auth_ok':
                    raise RuntimeError('Home-Assistant-Anmeldung fehlgeschlagen.')
                await ws.send_json({'id':1, 'type':'person/list'})
                response = await ws.receive_json(timeout=10)
                if not response.get('success'):
                    raise RuntimeError('Personen konnten nicht geladen werden.')
                people = response['result'].get('storage', [])
                person = next((item for item in people if item['id'] == person_id), None)
                if person is None:
                    raise ValueError('Diese Person wird in der Home-Assistant-Konfiguration verwaltet.')
                all_people = people + response['result'].get('config', [])
                trackers = updated_person_trackers(person, selected, states, [item for item in all_people if item['id'] != person_id])
                await ws.send_json({'id':2, 'type':'person/update', 'person_id':person_id, 'device_trackers':trackers})
                response = await ws.receive_json(timeout=10)
                if not response.get('success'):
                    raise RuntimeError('Gerätezuordnung konnte nicht gespeichert werden.')
    with PERSON_DEVICE_LOCK:
        asyncio.run(update())


def render_person_wifi_picker(person, summary):
    clients = summary.get('wifi_presence_candidates', [])
    assigned = set(person.get('device_trackers', []))
    selected = next((item['entity_id'] for item in clients if item['entity_id'] in assigned), '')
    others = {entity for other in summary.get('available_presence_entities', []) if other.get('entity_id') != person.get('entity_id') for entity in other.get('device_trackers', [])}
    options = ['<option value="">Kein zusätzliches Gerät</option>']
    for client in clients:
        entity = client['entity_id']
        suffix = entity.split('.')[-1] if sum(item['name'] == client['name'] for item in clients) > 1 else ''
        status = 'Im WLAN' if client['state'] == 'home' else 'Nicht im WLAN'
        unavailable = entity in others or client['state'] in {'unavailable', 'unknown'}
        label = client['name'] + (f' ({suffix})' if suffix else '') + ' · ' + ('Anderer Person zugeordnet' if entity in others else status)
        options.append(f'<option value="{escape(entity)}" {"selected" if entity == selected else ""} {"disabled" if unavailable else ""}>{escape(label)}</option>')
    return f'<label class="person-wifi"><span>WLAN-Gerät <small>optional · Zuhause-Erkennung</small></span><select data-person-wifi="{escape(person.get("entity_id"))}" aria-label="{escape(person.get("name"))}: WLAN-Gerät">{"".join(options)}</select></label>'


def available_presence_entities(configured: List[str], options: dict = None) -> List[dict]:
    if options is None:
        options = {}

    waze_entity = options.get("waze_entity", "")
    distance_entity = options.get("distance_entity", "")

    seen = set()
    people: List[dict] = []
    observed = all_states()
    by_id = {row.get("entity_id"): row for row in observed}
    for state in observed:
        entity_id = str(state.get("entity_id") or "")

        # Only include person entities, or sensor entities if explicitly configured
        # device_tracker entities are too granular and technical for presence selection
        is_person = entity_id.startswith("person.")
        is_tracked_sensor = entity_id == waze_entity or entity_id == distance_entity

        if not (is_person or is_tracked_sensor):
            continue

        attrs = state.get("attributes", {}) if isinstance(state.get("attributes"), dict) else {}
        seen.add(entity_id)

        # For person entities, check if they have a device tracker linked
        # The "source" attribute contains the device_tracker entity if linked
        has_device = False
        device_source = attrs.get("source")
        if is_person and device_source and str(device_source).startswith("device_tracker."):
            has_device = True

        # Determine source label
        source = "Default"
        if entity_id == waze_entity:
            source = "Waze"
        elif entity_id == distance_entity:
            source = "Entfernung"
        elif not entity_id.startswith("person."):
            source = "Sensor"

        people.append(
            {
                "entity_id": entity_id,
                "name": str(attrs.get("friendly_name") or entity_id.split(".", 1)[-1].replace("_", " ").title()),
                "state": str(state.get("state") or "unknown"),
                "source": source,
                "has_device": has_device,
                "presence_source": state_attrs(by_id.get(device_source, {})).get("source_type"),
                "device_trackers": attrs.get("device_trackers", []),
            }
        )
    for entity_id in configured:
        if entity_id not in seen:
            source = "Default"
            if entity_id == waze_entity:
                source = "Waze"
            elif entity_id == distance_entity:
                source = "Entfernung"

            people.append(
                {
                    "entity_id": entity_id,
                    "name": entity_id.split(".", 1)[-1].replace("_", " ").title(),
                    "state": "unknown",
                    "source": source,
                    "has_device": False,
                }
            )
    # Sort: persons with devices first, then by name
    return sorted(people, key=lambda item: (not item.get("has_device", False), item["name"].lower()))


def room_segment_catalog(vacuum_state: Any, configured_rooms: List[dict], room_names: dict) -> List[dict]:
    configured_by_segment: dict[int, dict] = {}
    for room in configured_rooms:
        segment_id = segment_id_value(room.get("segment_id"))
        if segment_id is not None:
            configured_by_segment[segment_id] = room

    discovered = extract_vacuum_segments(vacuum_state.get("attributes", {}) if isinstance(vacuum_state, dict) else {})
    discovered_by_segment = {int(item["segment_id"]): item for item in discovered}
    all_segment_ids = sorted(set(configured_by_segment) | set(discovered_by_segment))

    result = []
    for segment_id in all_segment_ids:
        configured = configured_by_segment.get(segment_id) or {}
        discovered_item = discovered_by_segment.get(segment_id) or {}
        result.append(
            {
                "segment_id": segment_id,
                "room_key": configured.get("id") or f"segment_{segment_id}",
                "name": room_names.get(str(segment_id))
                or configured.get("name")
                or discovered_item.get("name")
                or f"Segment {segment_id}",
                "detected_name": discovered_item.get("name") or f"Segment {segment_id}",
                "configured": bool(configured),
                "detected": bool(discovered_item),
            }
        )
    return result


def build_summary() -> dict:
    options = load_options()
    rooms = parse_rooms(options.get("rooms"))
    entity_map = helper_entities(options, rooms)
    states = collect_states(entity_map)
    status_state = states["sensors"].get("status") or {}
    history_state = states["sensors"].get("history") or {}
    vacuum_state = state_for(options.get("vacuum_entity", ""))
    robot_states = collect_robot_states(options, vacuum_state)
    configured_presence = parse_presence_entities(options.get("presence_entities"))
    room_names = parse_json_mapping(state_value(states["global"].get("room_names"), ""))
    status_attrs = decode_status_attributes(status_state)
    history_attrs = history_state.get("attributes", {}) if isinstance(history_state, dict) else {}
    return {
        "options": {
            "dust_collection_type": options.get("dust_collection_type", "bag"),
            "water_connection": options.get("water_connection", False),
            "fallback_speed_kmh": options.get("fallback_speed_kmh", 30),
            "vacuum_entity": options.get("vacuum_entity"),
            "notify_service": options.get("notify_service"),
            "presence_entities": options.get("presence_entities"),
            "room_names_entity": options.get("room_names_entity"),
            "selected_presence_entities_entity": options.get("selected_presence_entities_entity"),
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
        "available_presence_entities": available_presence_entities(configured_presence, options),
        "wifi_presence_candidates": wifi_presence_candidates(all_states()),
        "tracking_sources": {key: state_for(options.get(key + "_entity", "")) if options.get(key + "_entity") else None for key in ("waze", "distance")},
        "room_segments": room_segment_catalog(vacuum_state, rooms, room_names),
        "status": {
            "enabled": "off" if options.get("observation_mode", False) else status_attrs.get("enabled", "on"),
            "observation_mode": status_attrs.get("observation_mode", options.get("observation_mode", False)),
            "anyone_home": status_attrs.get("anyone_home"),
            "everyone_away": status_attrs.get("everyone_away"),
            "reason": status_attrs.get("reason"),
            "presence_summary": status_attrs.get("presence_summary", []),
            "travel_time_min": status_attrs.get("travel_time_min"),
            "travel_time_source": status_attrs.get("travel_time_source"),
            "travel_time_note": status_attrs.get("travel_time_note"),
            "robot_diagnostics": status_attrs.get("robot_diagnostics", {}),
            "travel_pause_after_hours": status_attrs.get("travel_pause_after_hours", options.get("travel_pause_after_hours")),
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
            "return_analysis": status_attrs.get("return_analysis", {}),
            "return_policy": status_attrs.get("return_policy", {}),
            "missing_helpers": status_attrs.get("missing_helpers", []),
        },
        "history": {
            "weekly_stats": history_attrs.get("weekly_stats", []),
            "recent_runs": history_attrs.get("recent_runs", []),
        },
    }


def decode_status_attributes(state: Any) -> dict:
    attrs = dict(state.get("attributes", {})) if isinstance(state, dict) else {}
    try:
        diagnostics = json.loads(attrs.get("diagnostics_json", "{}"))
        if isinstance(diagnostics, dict):
            attrs.update(diagnostics)
    except (TypeError, ValueError):
        pass
    return attrs


def render_robot_diagnostics(summary: dict) -> str:
    diagnostics = summary.get("status", {}).get("robot_diagnostics", {})
    if not diagnostics:
        return '<section class="card"><h2>Staubsaugerprüfung</h2><p>Noch keine geprüften Gerätedaten.</p></section>'
    phases = {"docked": "Angedockt", "idle": "Untätig außerhalb der Station",
              "cleaning": "Reinigung läuft", "returning": "Rückkehr zur Station",
              "returning_to_wash": "Rückkehr zur Moppwäsche", "washing": "Moppwäsche",
              "drying": "Trocknung", "draining": "Entwässerung", "paused": "Pausiert",
              "busy": "Anderer Geräte- oder Stationsvorgang", "error": "Fehler",
              "unavailable": "Gerätedaten nicht verfügbar", "stale": "Letzte Zustandsmeldung zu alt für eine Startfreigabe", "unknown": "Zustand unklar"}
    reasons = {"task_not_finished": "Auftrag noch aktiv oder unklar",
               "task_active_or_unknown": "Laufender Auftrag nicht sicher ausgeschlossen",
               "station_busy_or_unknown": "Station beschäftigt oder Zustand unklar",
               "battery_low_or_unknown": "Akku zu niedrig oder unbekannt",
               "map_or_location_unknown": "Karte oder Standortbestimmung nicht bestätigt",
               "rooms_unknown": "Keine Räume auf der ausgewählten Karte erkannt",
               "room_not_on_selected_map": "Gewünschter Raum fehlt auf der ausgewählten Karte",
               "previous_job_unresolved": "Vorheriger Auftrag muss geprüft werden",
               "confirmation_timeout_invalid": "Frist für die Startbestätigung ist ungültig",
               "quiet_hours_unknown": "Ruhezeiten nicht eindeutig lesbar", "quiet_hours": "Ruhezeit aktiv"}
    blockers = diagnostics.get("start_blockers", [])
    rows = ''.join('<li>' + escape(phases.get(str(reason)[6:], "Gerätezustand prüfen")
                                  if str(reason).startswith("robot_") else reasons.get(reason, "Prüfung erforderlich")) + '</li>'
                   for reason in blockers)
    detail = '<ul>' + rows + '</ul>' if rows else '<p>Technische Startvoraussetzungen erfüllt.</p>'
    if diagnostics.get("job_status") != "no_owned_job":
        detail += '<p>Auftragszuordnung muss vor einer weiteren Freigabe geprüft werden.</p>'
    else:
        detail += '<p>Kein von GhostVacuum gestarteter Auftrag.</p>'
    return '<section class="card"><h2>Staubsaugerprüfung</h2><p>' + escape(phases.get(diagnostics.get("phase"), "Zustand unklar")) + '</p>' + detail + '<p>Automatik AUS · Gerätebefehle gesperrt.</p></section>'


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
        rows.append(
            f"""
            <div class="hero-level-item">
              <span class="hero-level-icon">{escape(metric.get("icon", ""))}</span>
              <span class="hero-level-label">{escape(metric.get("label", ""))}</span>
              <span class="hero-level-value {escape(metric.get("class", ""))}">{escape(metric.get("value", ""))}</span>
            </div>
            """
        )
    return "".join(rows)


def build_hero_metrics(summary: dict) -> tuple[List[dict], List[dict]]:
    hardware = summary.get("options", {})
    bagless = hardware.get("dust_collection_type") == "container"
    fixed_water = bool(hardware.get("water_connection", False))
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
    if bin_full or bin_almost_full or "bin_full" in attrs or "dustbin_full" in attrs:
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
    if bagless:
        station_metrics.append(hero_metric("🗑️", "Staubkasten", "Füllstand nicht verfügbar"))
    elif dust_bag_state or dust_bag_error:
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
    if fixed_water:
        station_metrics.append(hero_metric("💧", "Festwasseranschluss", "Konfiguriert"))
    elif clean_water_state:
        station_metrics.append(
            hero_metric(
                "💧",
                "Frischwasser",
                localize_status_value(clean_water_state),
                status_class(clean_water_state, {"low_water"}, {"not_installed"}),
            )
        )

    dirty_water_state = value_from_signals(signals, "dirty_water_tank_status")
    if dirty_water_state and not fixed_water:
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
    if not bagless and dust_bag_drying_state and dust_bag_drying_state not in {"idle", "unknown"}:
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
    after_hours = number_value(status.get("travel_pause_after_hours"), 0) or 0
    away_since = parse_datetime(status.get("away_since"))
    remaining_label = None
    if away_since and after_hours > 0:
        now = datetime.now(away_since.tzinfo) if away_since.tzinfo else datetime.now()
        elapsed_hours = max(0, (now - away_since).total_seconds() / 3600)
        remaining_label = format_duration_hours(max(0, after_hours - elapsed_hours))

    rows = []
    for person in people:
        outside_since = parse_datetime(person.get("outside_since"))
        remaining_label = None
        if outside_since is not None:
            now = datetime.now(outside_since.tzinfo) if outside_since.tzinfo else datetime.now()
            remaining_label = format_duration_hours(max(0, after_hours - (now - outside_since).total_seconds() / 3600))
        name = person_name(person)
        initials = "".join(part[:1] for part in name.split())[:2].upper() or "P"
        home = person_is_home(person)
        details = []
        distance = person_distance(person)
        travel_time = person_travel_time(person)
        if distance is not None:
            details.append(format_number(distance, " km"))
        if travel_time is not None:
            suffix = " min (lokale Schätzung)" if person.get("travel_time_source") == "local_estimate" else " min"
            details.append(format_number(travel_time, suffix))
        elif not home:
            details.append("Heimwegzeit unbekannt")
        outside_radius = not home and radius > 0 and distance is not None and distance > radius
        if outside_radius:
            if person.get("travel_mode_active"):
                details = ["Reisemodus aktiv"]
            elif remaining_label:
                details = [f"Reisemodus aktiv in {remaining_label}"]
            else:
                details = []
        detail_text = " · ".join(details)
        if person.get("presence") == "unknown":
            badge = "unbekannt"
            badge_class = "warning"
            detail_text = "Keine verlässliche Anwesenheitsinformation"
        elif home:
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
            <span class="room-tag last">läuft gerade</span>
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


def parse_dashboard_room_order(raw):
    try:
        value = json.loads(raw) if str(raw).startswith('[') else [raw]
    except (ValueError, TypeError):
        return []
    return list(dict.fromkeys(room for room in value if isinstance(room, str) and room and room not in {'unknown','unavailable','none'})) if isinstance(value,list) else []


ROOM_ORDER_LOCK = threading.Lock()

def save_dashboard_room_move(room_key, target, before=None):
    with ROOM_ORDER_LOCK:
        return _save_dashboard_room_move(room_key, target, before)

def _save_dashboard_room_move(room_key, target, before=None):
    if not isinstance(room_key, str) or not isinstance(target, str) or (before is not None and not isinstance(before, str)):
        raise ValueError("Ungültiger Raum.")
    summary = build_summary()
    model = build_live_dashboard_view(summary)
    if model['commands_locked'] or not model['automation_enabled']:
        raise ValueError('Automatik muss aktiv und Gerätebefehle müssen freigegeben sein.')
    room = next((r for r in model['rooms'] if r['id'] == room_key), None)
    if not room or room['enabled'] is not True or target not in {'active','planned'}:
        raise ValueError('Dieser Raum kann hier nicht abgelegt werden.')
    active = next((r for r in model['rooms'] if r['group'] == 'active'), None)
    if target == 'active' and model['robot']['cleaning'] and active is None:
        raise ValueError('Der aktuell gereinigte Raum ist unbekannt.')
    if target == 'active' and active and active['id'] == room_key:
        return
    order = [r['id'] for r in model['rooms'] if r['group'] == 'planned' and r['id'] != room_key]
    if target == 'active':
        order.insert(0, room_key)
        if active:
            order.insert(1, active['id'])
    else:
        index = order.index(before) if before in order else len(order)
        order.insert(index, room_key)
    value = json.dumps(order, separators=(',', ':'))
    if len(value) > 128:
        raise ValueError('Zu viele Räume für die manuelle Reihenfolge.')
    for key in [room_key] + ([active['id']] if target == 'active' and active else []):
        if next(r for r in model['rooms'] if r['id'] == key)['group'] != 'planned':
            fire_event(EVENT_SET_ROOM_DUE, {'room_key':key, 'due':True})
    entity = summary['entities']['global']['one_time_room_override']
    service_call('input_text','set_value',entity,{'value':value})
    stop = active and (target == 'active' or active['id'] == room_key)
    if stop:
        service_call('vacuum','return_to_base',load_options().get('vacuum_entity'))
    elif target == 'active':
        fire_event(EVENT_START_ROOM, {'room_key':room_key})


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
    order = parse_dashboard_room_order(override)
    if order:
        ordered.sort(key=lambda item: order.index(item.get('room_key')) if item.get('room_key') in order else len(order))
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


STATE_MACHINE_STEPS = [
    ("paused", "Pausiert"),
    ("home", "Zuhause"),
    ("away", "Bereit"),
    ("travel", "Reisemodus"),
    ("planning", "Planung"),
    ("selected", "Raum gewählt"),
    ("cleaning", "Reinigt"),
    ("aborting", "Abbruch"),
    ("done", "Abgeschlossen"),
    ("waiting", "Warten"),
]


def _state_machine_context(summary: dict) -> dict:
    sensors = summary.get("states", {}).get("sensors", {})
    global_states = summary.get("states", {}).get("global", {})
    status = summary.get("status", {})
    status_text = state_value(sensors.get("status"), "Warten")
    status_key = status_text.lower()
    active_room = active_room_label(summary)
    next_rooms = ordered_room_queue(summary)
    next_room = next_rooms[0] if next_rooms else None
    return_window = number_value(state_value(sensors.get("return_window"), ""), 0) or 0
    travel_time = number_value(state_value(sensors.get("travel_time"), ""), 0) or 0
    distance = number_value(state_value(sensors.get("distance_to_home"), ""))
    enabled = state_value(global_states.get("enabled"), status.get("enabled", "on")).lower()
    people = status.get("presence_summary", []) or []
    home_people = [person for person in people if person_is_home(person)]
    active_attrs = state_attrs(sensors.get("active_room"))
    remaining = number_value(active_attrs.get("remaining_min"), 0) or 0
    planned = number_value(active_attrs.get("planned_duration_min"), 0) or 0
    progress = 0
    if active_room and planned > 0:
        progress = max(0, min(100, ((planned - remaining) / planned) * 100))

    if enabled == "off" or status_key == "pausiert":
        active_step = "paused"
    elif "abbruch" in status_key:
        active_step = "aborting"
    elif status.get("travel_mode_active") or "reisemodus" in status_key:
        active_step = "travel"
    elif active_room or "reinigt" in status_key:
        active_step = "cleaning"
    elif home_people or "zuhause" in status_key:
        active_step = "home"
    elif status.get("reason") == "raum_fertig":
        active_step = "done"
    elif next_room:
        active_step = "selected"
    elif "bereit" in status_key:
        active_step = "planning"
    elif people and not home_people:
        active_step = "away"
    else:
        active_step = "waiting"

    if active_step == "paused":
        headline = "Automatik pausiert"
        detail = "Aktiviere die Automatik, damit wieder geplant wird."
    elif active_step == "home":
        headline = "Reinigung blockiert"
        detail = f"{len(home_people)} Bewohner zuhause."
    elif active_step == "travel":
        headline = "Reisemodus aktiv"
        reason = status.get("travel_mode_reason") or "Langzeit- oder Distanzregel"
        detail = str(reason).replace("_", " ")
    elif active_step == "cleaning":
        headline = f"Reinigt {active_room}"
        detail = f"{format_number(remaining, ' min')} Restzeit von {format_number(planned, ' min')} geplant."
    elif active_step == "aborting":
        headline = "Abbruch läuft"
        detail = "Der Roboter kehrt zur Basis zurück."
    elif active_step == "selected" and next_room:
        headline = f"Nächster Raum: {next_room.get('room')}"
        detail = f"{format_number(next_room.get('effective_duration_min'), ' min')} Dauer, Priorität {format_number(next_room.get('priority'))}."
    elif active_step == "planning":
        headline = "Plant nächsten Lauf"
        detail = "Rückkehrfenster und Raumprioritäten werden bewertet."
    elif active_step == "away":
        headline = "Alle weg"
        detail = "Die Automatik ist bereit, sobald ein Raum passt."
    elif active_step == "done":
        headline = "Letzter Raum abgeschlossen"
        detail = "Historie und Lernwerte wurden aktualisiert."
    else:
        headline = "Wartet auf passende Bedingungen"
        detail = "Kein startbarer Raum oder Daten fehlen."

    return {
        "active_step": active_step,
        "headline": headline,
        "detail": detail,
        "progress": progress,
        "metrics": {
            "status": status_text,
            "eta": format_number(travel_time, " min"),
            "window": format_number(return_window, " min"),
            "distance": format_number(distance, " km"),
            "active_room": active_room or "Keiner",
            "next_room": str(next_room.get("room")) if next_room else "Keiner",
            "reason": status.get("reason") or "-",
        },
    }


def render_state_machine(summary: dict) -> str:
    """Render automation conditions as compact horizontal pills for the hero card."""
    if summary.get("status", {}).get("observation_mode") in {True, "on", "true"}:
        return ""
    sensors = summary.get("states", {}).get("sensors", {})
    global_states = summary.get("states", {}).get("global", {})
    status = summary.get("status", {})
    
    # Derive conditions
    status_key = state_value(sensors.get("status"), "Warten").lower()
    active_room = state_value(sensors.get("active_room"), "")
    active_attrs = state_attrs(sensors.get("active_room"))
    active_room_label_value = "" if str(active_room).lower() in {"", "-", "keine", "keiner", "none"} else active_room
    vacuum_state = state_value(summary.get("states", {}).get("vacuum"), "").lower()
    enabled = state_value(global_states.get("enabled"), status.get("enabled", "on")).lower()
    people = status.get("presence_summary", []) or []
    home_people = [p for p in people if isinstance(p, dict) and p.get("state", "").lower() in {"home", "zuhause"}]
    next_rooms = ordered_room_queue(summary)
    next_room = next_rooms[0].get("room") if next_rooms else ""
    next_duration = number_value(next_rooms[0].get("effective_duration_min"), 0) if next_rooms else 0
    planned = number_value(active_attrs.get("planned_duration_min"), 0) or 0
    remaining = number_value(active_attrs.get("remaining_min"), 0) or 0
    progress = max(0, min(100, ((planned - remaining) / planned) * 100)) if planned > 0 else 0
    relevant_duration = remaining if active_room_label_value else (next_duration or 0)
    return_window = number_value(state_value(sensors.get("return_window"), ""), 0) or 0
    return_buffer = number_value(
        state_value(global_states.get("return_buffer"), ""),
        status.get("return_buffer_min", 5),
    ) or 0
    start_hour = number_value(
        state_value(global_states.get("start_hour"), ""),
        status.get("time_window", {}).get("start_hour", 8),
    ) or 0
    end_hour = number_value(
        state_value(global_states.get("end_hour"), ""),
        status.get("time_window", {}).get("end_hour", 22),
    ) or 0
    now = datetime.now()
    current_hour = now.hour + now.minute / 60
    in_time_window = (
        start_hour <= current_hour < end_hour
        if start_hour <= end_hour
        else current_hour >= start_hour or current_hour < end_hour
    )
    weekday_keys = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    weekday_matches = weekday_keys[now.weekday()] in active_weekday_keys(summary)
    robot = summary.get("states", {}).get("robot", {}) or {}
    robot_signals = robot.get("signals", {}) or {}
    vacuum_attrs = state_attrs(summary.get("states", {}).get("vacuum"))
    battery = first_number(
        vacuum_attrs.get("battery_level"),
        vacuum_attrs.get("battery"),
        state_value(robot_signals.get("battery_level"), ""),
    )
    error_state = value_from_signals(robot_signals, "error")
    error_attr = signal_attr(robot_signals.get("error"), "value")
    error_value = str(error_attr or error_state or vacuum_attrs.get("error") or "").lower()
    error_ok = error_value in {"", "no_error", "none", "unknown", "unavailable"}
    robot_ready = vacuum_state not in {"", "unknown", "unavailable", "error"} and error_ok and (battery is None or battery >= 20)
    window_sufficient = (
        return_window >= relevant_duration + return_buffer
        if relevant_duration > 0
        else False
    )
    
    automation_active = enabled != "off"
    nobody_home = len(home_people) == 0
    travel_inactive = not status.get("travel_mode_active") and "reisemodus" not in status_key
    manual_cleaning_active = vacuum_state in {"cleaning", "returning", "paused"} and not active_room_label_value
    manual_inactive = not manual_cleaning_active
    aborting = "abbruch" in status_key or "abbrechen" in status_key
    prerequisites_ok = (
        automation_active
        and robot_ready
        and weekday_matches
        and in_time_window
        and window_sufficient
        and nobody_home
        and travel_inactive
        and manual_inactive
    )
    checks = [
        ("robot_ready", "Roboter bereit", "ok" if robot_ready else "failed"),
        ("window_sufficient", "Reinigungsfenster reicht", "ok" if window_sufficient else "failed"),
        ("nobody_home", "Niemand zuhause", "ok" if nobody_home else "failed"),
        ("travel_inactive", "Reisemodus aus", "ok" if travel_inactive else "failed"),
        ("manual_inactive", "Kein manueller Lauf", "ok" if manual_inactive else "failed"),
    ]
    if active_room_label_value:
        state_text = f"Reinigt {active_room_label_value} · {progress:.0f}%"
        state_class = "cleaning"
    elif aborting:
        state_text = "Abbruch"
        state_class = "blocked"
    elif prerequisites_ok:
        state_text = "Bereit"
        state_class = "ready"
    else:
        state_text = "Blockiert"
        state_class = "blocked"

    jobs_html = "".join(
        f'<div class="job {escape(check_state)}" data-check-key="{escape(check_key)}">'
        f'<span class="job-icon"></span>'
        f'<span class="job-name">{escape(check_label)}</span>'
        f'</div>'
        for check_key, check_label, check_state in checks
    )
    stage_class = "stage ok" if all(state == "ok" for _, _, state in checks) else "stage"
    next_room_html = (
        f'<strong class="hero-condition-state next">Nächstes: {escape(str(next_room))}</strong>'
        if next_room else ""
    )
    
    return (
        '<div class="hero-conditions" data-state-machine>'
        '<div class="hero-conditions-head">'
        '<span>Vorbedingungen</span>'
        '<div class="hero-condition-states">'
        f'<strong class="hero-condition-state current {escape(state_class)}">{escape(state_text)}</strong>'
        f'{next_room_html}'
        '</div>'
        '</div>'
        f'<div class="{stage_class}"><div class="jobs">{jobs_html}</div></div>'
        '</div>'
    )


def helper_state_is_missing(state_obj: Any) -> bool:
    if not state_obj or not isinstance(state_obj, dict):
        return True
    return state_obj.get("state") in (None, "unavailable")


def check_missing_helpers(summary: dict) -> List[str]:
    """Check which helper entities are missing by verifying their state."""
    global_entities = summary.get("entities", {}).get("global", {})
    global_states = summary.get("states", {}).get("global", {})

    missing = []
    for key, entity_id in global_entities.items():
        if not entity_id:
            continue
        state_obj = global_states.get(key)
        if helper_state_is_missing(state_obj):
            missing.append(entity_id)

    return missing


def render_missing_helpers_banner(summary: dict) -> str:
    if summary.get("status", {}).get("observation_mode") in {True, "on", "true"}:
        return ""
    """Render a warning banner if helper entities are missing."""
    # First check status sensor, then do our own check
    status_missing = summary.get("status", {}).get("missing_helpers", [])
    missing = status_missing
    if status_missing:
        missing = [
            entity_id
            for entity_id in status_missing
            if helper_state_is_missing(state_for(entity_id))
        ]
    else:
        missing = check_missing_helpers(summary)

    if not missing:
        return ""

    count = len(missing)
    helper_list = ", ".join(missing[:5])
    if count > 5:
        helper_list += f" ... und {count - 5} weitere"

    return (
        '<div class="missing-helpers-banner">'
        '<div class="missing-helpers-content">'
        '<span class="missing-helpers-icon">⚠️</span>'
        '<div class="missing-helpers-text">'
        f'<strong>{count} Helper-Entities fehlen</strong>'
        f'<span class="missing-helpers-list">{html.escape(helper_list)}</span>'
        '</div>'
        '<button class="missing-helpers-repair-btn" data-action="repair-helpers">'
        'Reparieren'
        '</button>'
        '</div>'
        '</div>'
    )


def dashboard_flag(value):
    return value is True or str(value).lower() in {'on', 'true', '1', 'yes'}


def room_due_badge(next_due, now=None):
    due = parse_datetime(next_due)
    if due is None:
        return {"label": "Fällig", "level": "neutral"}
    now = now or datetime.now(due.tzinfo)
    overdue_days = max(0, (now - due).total_seconds() / 86400)
    level = "danger" if overdue_days >= 2 else "warning" if overdue_days > 1 else "neutral"
    days = int(overdue_days)
    label = f"{days} {'Tag' if days == 1 else 'Tage'} überfällig" if overdue_days > 1 else "Heute fällig"
    return {"label": label, "level": level}


def dashboard_schedule_notice(summary, now=None):
    """Show schedule blockers independently of presence and master-switch state."""
    settings = summary.get('states', {}).get('global', {})
    window = summary.get('status', {}).get('time_window', {})
    if not any(key in settings for key in ('active_weekdays', 'start_hour', 'end_hour')) and not window:
        return None
    now = now or datetime.now(ZoneInfo('Europe/Berlin'))
    keys = [key for key, _ in WEEKDAY_OPTIONS]
    days = active_weekday_keys(summary)
    days = {keys[int(day)] if day.isdigit() and 0 <= int(day) < 7 else day for day in days}
    if keys[now.weekday()] not in days:
        return ['Montag', 'Dienstag', 'Mittwoch', 'Donnerstag', 'Freitag', 'Samstag', 'Sonntag'][now.weekday()] + ' · Automatik pausiert'
    start = number_value(state_value(settings.get('start_hour'), ''), window.get('start_hour', 8))
    end = number_value(state_value(settings.get('end_hour'), ''), window.get('end_hour', 22))
    # Same-day schedule, matching the automation's current start/end rules.
    if start is None or end is None:
        return None
    if not start <= now.hour < end:
        return 'Außerhalb der Reinigungszeit'
    if (now.replace(hour=int(end), minute=0, second=0, microsecond=0) - now).total_seconds() / 60 <= first_number(state_value(settings.get('return_buffer'), ''), 5):
        return 'Reinigungszeit endet'
    return None


def person_journey_scale(person, view, buffer, policy, status, now=None):
    """Six display stations, derived from existing presence signals only."""
    import math
    eta = person_travel_time(person)
    eta = eta if eta is not None and math.isfinite(eta) and eta >= 0 else None
    margin = number_value(policy.get('start_margin_min'), 5)
    margin = margin if margin is not None and math.isfinite(margin) and margin >= 0 else 5
    start = buffer + margin if buffer is not None else None
    radius = number_value(status.get('travel_pause_radius_km'))
    hours = number_value(status.get('travel_pause_after_hours'))
    duration = hours if hours is not None and math.isfinite(hours) and hours >= 0 else None
    steps = [
        {'label':'Zuhause', 'hint':'WLAN' if view.get('presence_source') == 'wifi' else ''},
        {'label':'Im Puffer', 'hint':f'≤ {buffer:g} min' if buffer is not None else ''},
        {'label':'Aufenthalt nötig', 'hint':f'{buffer:g}–{start:g} min' if start is not None else ''},
        {'label':'Genug Abstand', 'hint':f'≥ {start:g} min' if start is not None else ''},
        {'label':'Außerhalb Reiseradius', 'hint':f'> {radius:g} km' if radius is not None and math.isfinite(radius) else ''},
        {'label':'Reisemodus', 'hint':f'nach {duration:g} h' if duration is not None else ''},
    ]
    result = {'steps':steps, 'station':None, 'tone':'unknown', 'eta':None,
              'note':'', 'progress':None, 'approaching':False}
    if view['kind'] == 'home':
        result.update(station=0, tone='ready')
        return result
    if view['kind'] == 'unknown' or eta is None or person.get('motion') == 'unknown' or buffer is None:
        result['note'] = 'Abwesenheit wird bestätigt' if view.get('detail') == 'Abwesenheit wird bestätigt' else 'Standortdaten fehlen oder veraltet'
        return result
    result.update(eta=f'ca. {round(eta)} min bis zuhause', approaching=person.get('motion') == 'approaching')
    if dashboard_flag(person.get('travel_mode_active')):
        result.update(station=5, tone='travel', note='Reisemodus aktiv')
    elif dashboard_flag(person.get('outside_radius')):
        result.update(station=4, tone='ready')
        since = parse_datetime(person.get('outside_since'))
        if since and duration is not None:
            clock = now or datetime.now(since.tzinfo)
            if clock.tzinfo is None and since.tzinfo is not None:
                clock = clock.replace(tzinfo=since.tzinfo)
            elapsed = max(0, (clock - since).total_seconds() / 60)
            remaining = max(0, math.ceil(duration * 60 - elapsed))
            if duration > 0:
                result['travel_progress'] = {'minutes':min(elapsed, duration * 60), 'required':duration * 60}
            result['note'] = f'Reisemodus in {remaining // 60} h {remaining % 60:02d} min' if remaining else 'Reisemodus wird geprüft'
        else:
            result['note'] = 'Reise-Timer wird erfasst'
    elif person.get('near_home_unconfirmed') or eta <= buffer:
        result.update(station=1, tone='waiting')
    elif start is not None and eta < start:
        result.update(station=2, tone='ready' if person.get('stationary') else 'waiting')
        if person.get('stationary'):
            result.update(note='Aufenthalt bestätigt', progress={'minutes':10,'required':10})
        elif view.get('stay_progress'):
            result['progress'] = view['stay_progress']
    else:
        result.update(station=3, tone='ready')
    return result


def build_live_dashboard_view(summary):
    """Read-only, coordinate-free projection. Missing data is never sample data."""
    import math
    status = summary.get('status', {})
    sensors = summary.get('states', {}).get('sensors', {})
    vacuum = summary.get('states', {}).get('vacuum') or {}
    attrs = state_attrs(vacuum)
    diagnostics = status.get('robot_diagnostics') or {}
    observing = dashboard_flag(status.get('observation_mode'))
    people = []
    native_people = {p.get('entity_id'): p for p in summary.get('available_presence_entities', [])}
    person_sources = {key: p.get('presence_source') for key, p in native_people.items()}
    pending_confirmation = False
    near_home_unconfirmed = False
    confirmed_home = False
    home = False
    unknown = not bool(status.get('presence_summary'))
    after_hours = number_value(status.get('travel_pause_after_hours'))
    for person in status.get('presence_summary') or []:
        is_home = person_is_home(person)
        is_unknown = person.get('presence') == 'unknown' or str(person.get('state', '')).lower() in {'', 'unknown', 'unavailable'}
        native = native_people.get(person.get('entity_id'), {})
        native_state = str(native.get('state', '')).lower()
        near_home = dashboard_flag(person.get('near_home_unconfirmed'))
        near_home_unconfirmed = near_home_unconfirmed or near_home
        pending = (is_home or is_unknown) and native.get('presence_source') == 'gps' and native_state not in {'', 'unknown', 'unavailable', 'home', 'on'}
        pending_confirmation = pending_confirmation or pending
        confirmed_home = confirmed_home or (is_home and not pending)
        home = home or is_home
        unknown = unknown or is_unknown
        travelling = dashboard_flag(person.get('travel_mode_active')) and not is_home and not is_unknown
        eta = person_travel_time(person)
        if eta is not None and (not math.isfinite(eta) or eta < 0):
            eta = None
        text = 'Zuhause' if is_home else 'Reisemodus' if travelling else 'Heimweg unbekannt' if is_unknown or eta is None else f'ca. {round(eta)} min bis zuhause'
        if pending:
            text = 'Abwesenheit wird bestätigt'
        if near_home:
            text = f'Im Puffer · {round(eta)} min bis zuhause' if eta is not None else 'Im Puffer'
        countdown = None
        since = parse_datetime(person.get('outside_since'))
        if not is_home and not is_unknown and not travelling and dashboard_flag(person.get('outside_radius')) and since and after_hours is not None:
            now = datetime.now(since.tzinfo) if since.tzinfo else datetime.now()
            remaining = max(0, math.ceil(after_hours * 60 - (now - since).total_seconds() / 60))
            countdown = f'Reisemodus in {remaining // 60} h {remaining % 60:02d} min' if remaining else 'Reisemodus wird geprüft'
        if not is_home and not is_unknown and not travelling and countdown is None:
            if person.get('stationary'):
                countdown = 'Aufenthalt zählt'
            elif person.get('motion') == 'approaching':
                countdown = 'Annäherung erkannt'
            elif person.get('motion') == 'observing':
                observed_minutes = number_value(person.get('stay_minutes'), 0) or 0
                remaining = max(0, math.ceil(10 - observed_minutes))
                countdown = f'Aufenthalt · noch {remaining} min' if remaining else 'Wartet auf Standortmeldung'
            elif person.get('motion') == 'unknown':
                countdown = 'Wartet auf Standortmeldung'
        people.append({'name': person_name(person), 'presence_source': {'router':'wifi','gps':'mobile'}.get(person.get('presence_source') or person_sources.get(person.get('entity_id'))), 'kind': 'away' if near_home else 'unknown' if pending else 'home' if is_home else 'unknown' if is_unknown else 'travel' if travelling else 'away', 'detail': text, 'countdown': countdown})
    travel = dashboard_flag(status.get('travel_mode_active'))
    buffer = number_value(state_value(summary.get("states", {}).get("global", {}).get("return_buffer"), ""), status.get("return_buffer_min"))
    buffer = max(0, round(buffer)) if buffer is not None and math.isfinite(buffer) else None
    window = number_value(state_value(sensors.get('return_window'), ''))
    window = max(0, window) if window is not None and math.isfinite(window) else None
    raw_window = window
    if window is not None and buffer is not None:
        window = max(0, window - buffer)
    schedule_notice = dashboard_schedule_notice(summary)
    policy = status.get('return_policy', {})
    nearest_eta = first_number(policy.get('timed_eta_min'), policy.get('eta_min'))
    within_buffer = nearest_eta is not None and buffer is not None and nearest_eta <= buffer and not dashboard_flag(policy.get('allowed'))
    caption, reason = 'Reinigung möglich für', 'Heimweg geschätzt · Puffer berücksichtigt'
    if pending_confirmation and not confirmed_home:
        window, caption, reason = 0, 'Keine Freigabe', 'Abwesenheit wird bestätigt'
    elif home or dashboard_flag(status.get('anyone_home')):
        window, caption, reason = 0, 'Keine Reinigung', 'Person zuhause'
    elif near_home_unconfirmed:
        window, caption, reason = 0, 'Kein automatischer Start', 'Innerhalb des Puffers'
    elif travel:
        window, caption, reason = 0, 'Reinigung pausiert', 'Reisemodus aktiv'
    elif unknown or not dashboard_flag(status.get('everyone_away')) or window is None:
        window, caption, reason = None, 'Keine Freigabe', 'Anwesenheit oder Heimweg unbekannt'
    elif schedule_notice:
        window, caption, reason = 0, 'Keine automatische Reinigung', schedule_notice
    elif observing:
        reason = 'Zeitfenster geschätzt · Beobachtung, kein Start'
    elif dashboard_flag(status.get('return_policy', {}).get('stationary_release')):
        caption, reason = 'Maximale Reinigungszeit', 'Aufenthalt erkannt · Stopp bei Annäherung im Puffer'
    elif within_buffer:
        window, caption, reason = 0, 'Kein automatischer Start', 'Innerhalb des Puffers'
    # Each person gets one status, selected by the strongest applicable condition.
    for person_view, person in zip(people, status.get('presence_summary', [])):
        person_view['stay_progress'] = None
        if person_view['kind'] in {'home', 'unknown', 'travel'} or dashboard_flag(person.get('near_home_unconfirmed')):
            person_view['countdown'] = None
            continue
        eta = person_travel_time(person)
        valid_eta = eta is not None and math.isfinite(eta) and eta >= 0
        continued = bool(status.get('active_room')) and person.get('monitored_absence') and person.get('motion') == 'observing'
        if valid_eta and buffer is not None and eta <= buffer and not person.get('stationary') and not continued:
            person_view['detail'] = f'Im Puffer · {round(eta)} min bis zuhause'
            person_view['countdown'] = None
        elif person.get('motion') == 'unknown':
            person_view['detail'], person_view['countdown'] = 'Wartet auf Standortmeldung', None
        elif valid_eta and buffer is not None and eta < buffer + (number_value(policy.get('start_margin_min'), 5) or 5) and not person.get('stationary') and not status.get('active_room'):
            if person.get('motion') == 'observing':
                observed = number_value(person.get('stay_minutes'), 0) or 0
                observed = max(0, min(10, observed)) if math.isfinite(observed) else 0
                person_view['stay_progress'] = {'minutes': math.floor(observed), 'required': 10}
            person_view['countdown'] = None
        else:
            # Return ETA is sufficient when no personal condition blocks a start.
            person_view['countdown'] = None
    for person_view, person in zip(people, status.get('presence_summary', [])):
        person_view['journey'] = person_journey_scale(person, person_view, buffer, policy, status)
    calculation = f'Puffer {buffer} min' if buffer is not None else 'Puffer unbekannt'
    if window is not None and window > 0 and raw_window is not None and buffer is not None:
        calculation = f'Zeitfenster {round(raw_window)} min − Puffer {buffer} min = {round(window)} min'
        if dashboard_flag(status.get('return_policy', {}).get('stationary_release')):
            calculation = f'Höchstens bis zum Zeitfensterende · Puffer {buffer} min'
    if within_buffer and not home and not unknown and not pending_confirmation and not travel and not schedule_notice and not observing:
        calculation = f'{round(nearest_eta)} min bis zuhause · Puffer {buffer} min'
    # The dashboard shows permission, not an estimated amount of cleaning time.
    permission_ready = False
    permission_title = 'Kein automatischer Start'
    permission_reason = reason
    automation_enabled = bool_on(summary.get('states', {}).get('global', {}).get('enabled'))
    if observing:
        permission_title, permission_reason = 'Beobachtungsmodus', 'Gerätebefehle gesperrt'
    elif not automation_enabled:
        permission_title, permission_reason = 'Automatik deaktiviert', 'Keine automatischen Starts'
    elif not (pending_confirmation or home or near_home_unconfirmed or unknown or travel or schedule_notice):
        permission_ready = dashboard_flag(policy.get('allowed'))
        if permission_ready:
            permission_title = 'Reinigung freigegeben'
            permission_reason = 'Aufenthalt bestätigt' if dashboard_flag(policy.get('stationary_release')) else 'Ausreichend Abstand'
        else:
            permission_reason = policy.get('reason') or 'Freigabe wird geprüft'
            if permission_reason == 'Startabstand noch nicht erreicht':
                permission_title = 'Automatik wartet'
                permission_reason = 'Freigabe nach 10 Minuten Aufenthalt' if any(p.get('stay_progress') is not None for p in people) else 'Noch zu nah zuhause'
            if permission_reason == 'Puffergrenze erreicht':
                permission_reason = 'Innerhalb des Puffers'
    cleaning_permission = {'ready': permission_ready, 'title': permission_title, 'reason': permission_reason}
    raw_state = state_value(vacuum, 'unknown')
    cleaning = raw_state in {'cleaning', 'paused'}
    catalog = summary.get('room_segments') or []
    current_segment = attrs.get('current_segment') if cleaning else None
    active_name = active_room_label(summary) if cleaning else ''
    active_attrs = state_attrs(sensors.get('active_room')) if cleaning else {}
    remaining = first_number(active_attrs.get('remaining_min'), attrs.get('remaining_time')) if cleaning else None
    progress = number_value(attrs.get('cleaning_progress')) if cleaning else None
    if progress is not None and (not math.isfinite(progress) or not 0 <= progress <= 100):
        progress = None
    queue = ordered_room_queue(summary)
    rooms = []
    seen = set()
    settings_by_id = {str(r.get('id')): r for r in summary.get('states', {}).get('rooms', [])}
    for room in catalog:
        key = room.get('room_key') or str(room.get('segment_id'))
        if key in seen:
            continue
        seen.add(key)
        name = room.get('name') or key
        settings = settings_by_id.get(str(key), {})
        enabled_state = settings.get('enabled_state')
        enabled = bool_on(enabled_state) if enabled_state is not None else None
        item = next((q for q in queue if q.get('room_key') == key or str(q.get('segment_id')) == str(room.get('segment_id'))), {})
        stats = room_stats_for(summary, {'room_key': key, 'room': name})
        is_active = cleaning and ((current_segment is not None and str(room.get('segment_id')) == str(current_segment)) or name == active_name)
        # Never infer a plan in observation mode, even if old helpers contain a queue.
        due = not observing and enabled is not False and bool(item.get('enabled')) and (number_value(item.get('forecast_score'), 0) or 0) >= 1
        last = stats.get('last_cleaned')
        interval = first_number(state_value(settings.get('interval_h_state'), ''), stats.get('interval_h'), item.get('interval_h'))
        last_dt = parse_datetime(last)
        next_due = None
        if last_dt and interval is not None and math.isfinite(interval) and interval >= 0:
            next_due = (last_dt + timedelta(hours=interval)).isoformat()
        rooms.append({'id': key, 'name': name, 'enabled': enabled, 'group': 'active' if is_active else 'planned' if due else 'rest', 'last': last, 'next_due': next_due, 'interval_h': interval, 'due_badge': room_due_badge(next_due), 'duration': first_number(stats.get('learned_duration_min'), item.get('learned_duration_min')), 'plan_rank': queue.index(item) if item else len(queue), 'priority': number_value(item.get('priority'), 0) or 0})
    # A device can report multiple selected segments; the current segment alone is active.
    selected_active = next((r for r in rooms if r['group'] == 'active'), None)
    for room in rooms:
        if room['group'] == 'active' and room is not selected_active:
            room['group'] = 'rest'
    rooms.sort(key=lambda r: (0 if r['group'] == 'active' else 1 if r['group'] == 'planned' else 2, r['plan_rank'] if r['group'] == 'planned' else 0))
    robot_metrics, station_metrics = build_hero_metrics(summary)
    battery = next((m['value'] for m in robot_metrics if m['label'] == 'Akku'), 'Nicht verfügbar')
    subtitle = {'docked': 'An der Station', 'cleaning': 'Reinigung läuft', 'paused': 'Reinigung pausiert', 'returning': 'Rückkehr zur Station'}.get(raw_state, localize_vacuum_state(raw_state))
    if diagnostics.get('phase') == 'stale':
        age = number_value(diagnostics.get('data_age_sec'))
        subtitle = 'Letzte Zustandsmeldung' + (f' vor {round(age / 60)} min' if age is not None else ' veraltet')
    elif diagnostics.get('phase') == 'error':
        subtitle = 'Gerätefehler · ' + str(diagnostics.get('error') or 'Prüfung erforderlich')
    title = f'Reinigt {selected_active["name"]}.' if raw_state == 'cleaning' and selected_active else localize_vacuum_state(raw_state)
    mode = attrs.get('cleaning_mode') if cleaning else None
    mode = {'Sweeping': 'Saugen', 'Mopping': 'Wischen', 'Sweeping and mopping': 'Saugen & Wischen', 'Mopping after sweeping': 'Wischen nach Saugen'}.get(mode, '')
    if mode:
        subtitle = mode + ' · ' + subtitle
    external_job = cleaning and (observing or (diagnostics.get('job_status') == 'no_owned_job' and not active_room_label(summary)))
    robot_name = str(attrs.get('friendly_name') or 'Staubsauger')
    name_parts = robot_name.split()
    if len(name_parts) == 2 and name_parts[0] == name_parts[1]:
        robot_name = name_parts[0]
    return {'cleaning_permission': cleaning_permission, 'manual_order': bool(parse_dashboard_room_order(state_value(summary.get('states', {}).get('global', {}).get('one_time_room_override'), ''))), 'observation': observing, 'commands_locked': observing, 'automation_enabled': bool_on(summary.get('states', {}).get('global', {}).get('enabled')), 'robot': {'name': robot_name, 'title': title, 'subtitle': subtitle, 'origin_label': 'Manuell / extern gestartet' if external_job else None, 'cleaning': cleaning, 'progress': progress, 'remaining': remaining, 'active_name': selected_active['name'] if selected_active else None, 'battery': battery, 'metrics': robot_metrics + station_metrics}, 'people': people, 'presence_summary': 'Bestätigung ausstehend' if pending_confirmation and not confirmed_home else f'{sum(p["kind"] == "home" for p in people)} zuhause' if home else 'Unterwegs' if near_home_unconfirmed else 'Daten fehlen' if unknown else 'Reisemodus' if travel else 'Alle unterwegs', 'schedule_notice': schedule_notice, 'window': window, 'buffer_min': buffer, 'calculation': calculation, 'caption': caption, 'reason': reason, 'rooms': rooms, 'updated_at': datetime.now().astimezone().isoformat(), 'recent_runs': status.get('recent_runs') or []}


def render_live_observation_dashboard(summary):
    folder = Path(__file__).parent / 'dashboard'
    template = (folder / 'observation.html').read_text(encoding='utf-8')
    payload = json.dumps(build_live_dashboard_view(summary), ensure_ascii=False, allow_nan=False).replace('<', '\\u003c')
    return template.replace('/*LIVE_STYLES*/', (folder / 'observation.css').read_text(encoding='utf-8')).replace('/*LIVE_SCRIPT*/', (folder / 'observation.js').read_text(encoding='utf-8')).replace('LIVE_INITIAL_JSON', payload)


def render_dashboard_html() -> str:
    summary = build_summary()
    return render_live_observation_dashboard(summary)
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
    global_states = summary.get("states", {}).get("global", {})
    automation_enabled = state_value(global_states.get("enabled"), status.get("enabled", "on")).lower() != "off"
    automation_text = "Automation aktiv" if automation_enabled else "Automation inaktiv"
    automation_class = "on" if automation_enabled else "off"

    # Hero card: Robot status header
    hero_status_title = addon_name()
    if status.get("observation_mode") in {True, "on", "true"}:
        hero_status_title = f'{vacuum_attrs.get("friendly_name", "Staubsauger")} · {robot_summary}'
    hero_status_class = "cleaning" if is_cleaning else "idle"

    robot_metrics, station_metrics = build_hero_metrics(summary)

    # Hero card: Settings toggles
    def is_on(entity_state):
        return state_value(entity_state, "off").lower() in {"on", "true", "1"}

    start_push_on = is_on(global_states.get("start_push"))
    return_push_on = is_on(global_states.get("return_push"))
    travel_on = is_on(global_states.get("travel_logic"))
    start_hour = number_value(state_value(global_states.get("start_hour"), ""), summary.get("status", {}).get("time_window", {}).get("start_hour", 8))
    end_hour = number_value(state_value(global_states.get("end_hour"), ""), summary.get("status", {}).get("time_window", {}).get("end_hour", 22))
    schedule_text = f"{int(start_hour or 0):02d}:00-{int(end_hour or 0):02d}:00"
    weekdays_text = format_active_weekdays(summary)
    now = datetime.now()
    current_hour = now.hour + now.minute / 60
    schedule_matches = (
        start_hour <= current_hour < end_hour
        if start_hour <= end_hour
        else current_hour >= start_hour or current_hour < end_hour
    )
    weekday_keys = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    weekday_matches = weekday_keys[now.weekday()] in active_weekday_keys(summary)

    hero_start_push_class = "on" if start_push_on else ""
    hero_start_push_text = "Aktiv" if start_push_on else "Inaktiv"
    hero_return_push_class = "on" if return_push_on else ""
    hero_return_push_text = "Aktiv" if return_push_on else "Inaktiv"
    hero_travel_class = "on" if travel_on else ""
    hero_travel_text = "Aktiv" if travel_on else "Inaktiv"

    replacements = {
        "hero_status_class": hero_status_class,
        "hero_status_title": hero_status_title,
        "hero_automation_text": automation_text,
        "hero_automation_class": automation_class,
        "hero_robot_items": render_hero_metric_rows(robot_metrics, "Keine Robotersensoren verfügbar"),
        "hero_station_items": render_hero_metric_rows(station_metrics, "Keine Stationssensoren verfügbar"),
        "hero_start_push_class": hero_start_push_class,
        "hero_start_push_text": hero_start_push_text,
        "hero_return_push_class": hero_return_push_class,
        "hero_return_push_text": hero_return_push_text,
        "hero_travel_class": hero_travel_class,
        "hero_travel_text": hero_travel_text,
        "hero_schedule_text": schedule_text,
        "hero_schedule_class": "on" if schedule_matches else "",
        "hero_weekdays_text": weekdays_text,
        "hero_weekdays_class": "on" if weekday_matches else "",
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
        "state_machine": render_state_machine(summary),
        "room_sections": render_room_sections(summary),
        "history_card": render_history_card(summary),
        "missing_helpers_banner": render_missing_helpers_banner(summary),
    }

    output = load_dashboard_template()
    for key, value in replacements.items():
        output = output.replace("{{{" + key + "}}}", str(value))
        output = output.replace("{{" + key + "}}", escape(value))
    return output


def render_setting_toggle(key: str, title: str, description: str, entity_id: str, state: Any) -> str:
    active = bool_on(state)
    checked = "checked" if active else ""
    status = "Aktiv" if active else "Inaktiv"
    highlight_class = " primary" if key == "enabled" else ""
    return f"""
      <article class="setting-row{highlight_class}">
        <div class="setting-copy">
          <strong>{escape(title)}</strong>
          <span>{escape(description)}</span>
        </div>
        <label class="switch" aria-label="{escape(title)}">
          <input type="checkbox" data-toggle-entity="{escape(entity_id)}" data-setting-key="{escape(key)}" {checked}>
          <span></span>
          <em>{escape(status)}</em>
        </label>
      </article>
    """


def render_setting_number(key: str, title: str, description: str, entity_id: str, state: Any, step: str = "1") -> str:
    value = state_value(state, "")
    input_type = "time" if key in {"start_hour", "end_hour"} else "number"
    display_value = value
    if key in {"return_buffer", "start_margin"} and number_value(value) is not None:
        display_value = str(int(round(number_value(value))))
    if input_type == "time":
        hour = int(number_value(value, 0) or 0)
        hour = max(0, min(23, hour))
        display_value = f"{hour:02d}:00"
    return f"""
      <label class="number-row">
        <span>
          <strong>{escape(title)}</strong>
          <small>{escape(description)}</small>
        </span>
        <input type="{input_type}" step="{escape(step)}" value="{escape(display_value)}" data-number-entity="{escape(entity_id)}" data-setting-key="{escape(key)}">
      </label>
    """


def render_settings_toggles(summary: dict) -> str:
  """Render the basic toggle controls shown in Settings (no state machine).

  This returns a set of switch rows for global helper entities (enabled, push,
  travel logic, ...). The detailed live state machine belongs only on the
  dashboard and must not be injected here.
  """
  global_entities = summary.get("entities", {}).get("global", {})
  global_states = summary.get("states", {}).get("global", {})

  # Only include core/general toggles here. Tab-specific toggles live in their
  # respective renderers (`render_settings_push`, `render_settings_travel_toggle`).
  items = [
    ("enabled", "Automatik aktivieren", "Hauptschalter für automatische Starts."),
  ]

  rows = [
    render_setting_toggle(key, title, description, global_entities.get(key, ""), global_states.get(key))
    for key, title, description in items
    if global_entities.get(key)
  ]
  return "".join(rows) or '<p class="empty">Keine Schalter konfiguriert.</p>'
  


def active_weekday_keys(summary: dict) -> set[str]:
    global_states = summary.get("states", {}).get("global", {})
    raw = state_value(global_states.get("active_weekdays"), DEFAULT_ACTIVE_WEEKDAYS)
    if not raw or raw in {"unknown", "unavailable"}:
        raw = DEFAULT_ACTIVE_WEEKDAYS
    return {part.strip().lower() for part in str(raw).replace(";", ",").split(",") if part.strip()}


def format_active_weekdays(summary: dict) -> str:
    active = active_weekday_keys(summary)
    labels = {key: label for key, label in WEEKDAY_OPTIONS}
    if active == {key for key, _label in WEEKDAY_OPTIONS}:
        return "Täglich"
    if active == {"mon", "tue", "wed", "thu", "fri"}:
        return "Mo-Fr"
    if active == {"mon", "tue", "wed", "thu", "fri", "sat"}:
        return "Mo-Sa"
    return ", ".join(labels[key] for key, _label in WEEKDAY_OPTIONS if key in active) or "Keine Tage"


def render_settings_weekdays(summary: dict) -> str:
    global_entities = summary.get("entities", {}).get("global", {})
    entity_id = global_entities.get("active_weekdays", "")
    if not entity_id:
        return ""
    active = active_weekday_keys(summary)
    buttons = "".join(
        f"""
          <button class="weekday-button {'active' if key in active else ''}" type="button" data-weekday="{escape(key)}" aria-pressed="{'true' if key in active else 'false'}">
            {escape(label)}
          </button>
        """
        for key, label in WEEKDAY_OPTIONS
    )
    value = ",".join(key for key, _label in WEEKDAY_OPTIONS if key in active)
    return f"""
      <section class="weekday-row" data-weekdays data-weekdays-entity="{escape(entity_id)}">
        <div>
          <strong>Automatisch reinigen an</strong>
          <span>Wochentage, an denen automatische Starts erlaubt sind.</span>
        </div>
        <div class="weekday-buttons" data-weekday-buttons data-value="{escape(value)}">
          {buttons}
        </div>
      </section>
    """


def render_settings_push(summary: dict) -> str:
    global_entities = summary.get("entities", {}).get("global", {})
    global_states = summary.get("states", {}).get("global", {})
    items = [
        ("start_push", "Start-Push", "Benachrichtigung, wenn der Staubsauger automatisch loslegt."),
        ("return_push", "Rückkehr-Zusammenfassung", "Benachrichtigt dich bei der Rückkehr, welche Räume während der Abwesenheit gereinigt wurden."),
    ]
    rows = [
        render_setting_toggle(key, title, description, global_entities.get(key, ""), global_states.get(key))
        for key, title, description in items
        if global_entities.get(key)
    ]
    return "".join(rows) or '<p class="empty">Keine Push-Helper gefunden.</p>'


def render_settings_schedule(summary: dict) -> str:
    """Render schedule settings (start/end hour)."""
    global_entities = summary.get("entities", {}).get("global", {})
    global_states = summary.get("states", {}).get("global", {})
    items = [
        ("start_hour", "Startzeit", "Stunde", global_entities.get("start_hour", ""), global_states.get("start_hour")),
        ("end_hour", "Endzeit", "Stunde", global_entities.get("end_hour", ""), global_states.get("end_hour")),
    ]
    rows = [
        render_setting_number(key, title, description, entity_id, state)
        for key, title, description, entity_id, state in items
        if entity_id
    ]
    return "".join(rows) or ""


def render_settings_travel_toggle(summary: dict) -> str:
    """Render travel mode toggle."""
    global_entities = summary.get("entities", {}).get("global", {})
    global_states = summary.get("states", {}).get("global", {})
    items = [
        ("travel_logic", "Reiselogik", "Automatisch den Staubsauger pausieren, wenn du reist."),
    ]
    rows = [
        render_setting_toggle(key, title, description, global_entities.get(key, ""), global_states.get(key))
        for key, title, description in items
        if global_entities.get(key)
    ]
    return "".join(rows) or '<p class="empty">Keine Reiselogik-Helper gefunden.</p>'


def render_settings_numbers(summary: dict) -> str:
    global_entities = summary.get("entities", {}).get("global", {})
    global_states = summary.get("states", {}).get("global", {})
    items = [
        ("return_buffer", "Puffer", "Bei dieser Heimwegzeit fährt der Sauger zur Station.", "1"),
        ("start_margin", "Zusätzlicher Startabstand", "Erneuter Start erst ab Puffer + Startabstand. Verhindert ständiges Starten und Stoppen.", "1"),
    ]
    rows = [
        render_setting_number(key, title, description, global_entities.get(key, ""), global_states.get(key), step)
        for key, title, description, step in items
        if global_entities.get(key)
    ]
    return "".join(rows) or '<p class="empty">Keine Zahlen-Helper gefunden.</p>'


def render_settings_home_map(summary: dict) -> str:
    global_entities = summary.get("entities", {}).get("global", {})
    global_states = summary.get("states", {}).get("global", {})
    lat_entity = global_entities.get("home_latitude", "")
    lng_entity = global_entities.get("home_longitude", "")
    radius_entity = global_entities.get("travel_pause_radius", "")
    lat = number_value(state_value(global_states.get("home_latitude"), ""), 52.52) or 52.52
    lng = number_value(state_value(global_states.get("home_longitude"), ""), 13.405) or 13.405
    radius = number_value(state_value(global_states.get("travel_pause_radius"), ""), 100) or 100
    return f"""
      <div class="home-map-editor" data-home-map
        data-lat-entity="{escape(lat_entity)}" data-lng-entity="{escape(lng_entity)}" data-radius-entity="{escape(radius_entity)}">
        <div class="map-view-tabs" role="tablist" aria-label="Kartenansicht">
          <button type="button" role="tab" aria-selected="true" aria-controls="map-position-panel" data-map-tab="position">Wohnungsposition</button>
          <button type="button" role="tab" aria-selected="false" aria-controls="map-radius-panel" data-map-tab="radius">Reiseradius</button>
        </div>
        <div class="home-map-pair">
          <section class="map-card map-panel-active" data-map-panel="position" id="map-position-panel">
            <h3>Wohnungsposition</h3>
            <div class="map-canvas" data-map-canvas aria-label="Genaue Wohnungsposition"></div>
            <span class="map-coordinate-label" data-map-coordinates>{escape(format_number(lat))} · {escape(format_number(lng))}</span>
          </section>
          <section class="map-card" data-map-panel="radius" id="map-radius-panel">
            <h3>Reiseradius</h3>
            <div class="map-canvas" data-radius-canvas aria-label="Vollständiger Reiseradius"></div>
            <label class="radius-field"><span>Radius</span><input type="number" min="1" max="500" step="1" value="{escape(f'{radius:g}')}" data-map-radius-input aria-label="Reiseradius in Kilometern"><span>km</span></label>
          </section>
        </div>
        <input type="hidden" value="{escape(lat)}" data-map-lat>
        <input type="hidden" value="{escape(lng)}" data-map-lng>
        <div class="map-actions"><button type="button" data-save-map>Speichern</button></div>
      </div>
    """


def parse_cleaning_profile(raw):
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if valid_cleaning_profile(value, inherit=True) else {}


def cleaning_defaults(summary):
    raw = state_value(summary.get("states", {}).get("global", {}).get("cleaning_defaults"), "")
    value = parse_cleaning_profile(raw)
    return value if valid_cleaning_profile(value) else dict(CLEANING_PROFILE_DEFAULT)


def render_cleaning_profile_editor(entity, profile, defaults, title, global_editor=False):
    own = valid_cleaning_profile(profile)
    effective = profile if own else defaults
    labels = {"mode":"Reinigungsart","genius":"CleanGenius","sequence":"Reihenfolge","suction":"Saugkraft","moisture":"Feuchtigkeit","route":"Route","repeats":"Durchgänge"}
    fields = "".join(f'<label data-profile-field="{key}">{escape(labels[key])}<select data-profile-key="{key}" aria-label="{escape(title)}: {escape(labels[key])}">' + ''.join(
        f'<option value="{escape(value)}" {"selected" if effective[key] == value else ""}>{escape(label)}</option>' for value,label in choices) + '</select></label>' for key,choices in CLEANING_PROFILE_CHOICES.items())
    mode_label = dict(CLEANING_PROFILE_CHOICES['mode'])[effective['mode']]
    attrs = f'data-profile-editor data-entity="{escape(entity)}" data-profile-defaults="{escape(json.dumps(defaults))}"'
    source = '' if global_editor else f'<label class="profile-source">Einstellungen<select data-profile-source aria-label="{escape(title)}: Reinigungseinstellungen"><option value="inherit" {"selected" if not own else ""}>Standard verwenden</option><option value="own" {"selected" if own else ""}>Eigene Einstellungen</option></select></label>'
    body = f'{source}<div class="profile-fields">{fields}</div>'
    if global_editor:
        return f'<div {attrs} data-profile-global>{body}</div>'
    return f'<details class="room-cleaning" {attrs}><summary>Reinigung <span data-profile-summary>{"Eigene Einstellungen" if own else "Standard"} · {escape(mode_label)}</span></summary>{body}</details>'


def render_settings_cleaning_defaults(summary):
    entity = summary.get("entities", {}).get("global", {}).get("cleaning_defaults", "")
    defaults = cleaning_defaults(summary)
    return render_cleaning_profile_editor(entity,defaults,defaults,"Standard",True)


def render_settings_rooms(summary: dict) -> str:
    rooms = summary.get("states", {}).get("rooms", []) or []
    rooms_by_id = {str(room.get("id")): room for room in rooms}
    segments = summary.get("room_segments", []) or []
    if not segments:
        return '<p class="empty">Keine Räume konfiguriert.</p>'

    room_names_entity = summary.get("entities", {}).get("global", {}).get("room_names", "")
    rendered = {True: [], False: []}
    segments = sorted(segments, key=lambda segment: -(
        number_value(state_value(rooms_by_id.get(str(segment.get("room_key")), {}).get("weight_state"), ""), 1) or 1
    ))
    for segment in segments:
        room = rooms_by_id.get(str(segment.get("room_key"))) or {}
        room_key = str(segment.get("room_key") or room.get("id") or f"segment_{segment.get('segment_id')}")
        enabled = bool_on(room.get("enabled_state")) if room else True
        enabled_text = "Aktiv" if enabled else "Inaktiv"
        enabled_class = "on" if enabled else ""
        room_class = "" if enabled else " inactive"
        interval_hours = number_value(state_value(room.get("interval_h_state"), ""), 0) or 0
        interval_days = interval_hours / 24 if interval_hours else 0
        meta = f"Segment {segment.get('segment_id')}"
        enabled_button = ""
        if room.get("enabled"):
            enabled_button = f"""
                    <button class="status-button {enabled_class}" type="button" data-toggle-entity="{escape(room.get("enabled"))}">
                      {escape(enabled_text)}
                    </button>
            """
        else:
            enabled_button = '<span class="status-button on">Neu</span>'

        interval_row = ""
        if room.get("interval_h"):
            interval_row = f"""
                <label class="number-row room-interval-row">
                  <span>
                    <strong>Intervall</strong>
                    <small>Reinigung alle</small>
                  </span>
                  <input type="number" step="0.5" min="0.5" value="{escape(f"{interval_days:g}")}" data-number-entity="{escape(room.get("interval_h", ""))}" data-setting-key="interval_days">
                  <span class="room-interval-unit">Tage</span>
                </label>
            """
        index = len(rendered[enabled]) + 1
        weight = number_value(state_value(room.get("weight_state"), ""), 1) or 1
        sort_controls = f'''<span class="room-drag-cue" draggable="true" title="Priorität durch Ziehen ändern" aria-hidden="true"></span><span class="room-order-buttons"><button type="button" data-room-move="up" aria-label="{escape(segment.get('name'))}: höhere Priorität">↑</button><button type="button" data-room-move="down" aria-label="{escape(segment.get('name'))}: niedrigere Priorität">↓</button></span>''' if enabled and room.get("weight") else ""
        rendered[enabled].append(
            f"""
                <article class="room-settings-card{room_class}" data-room-card data-room-key="{escape(room_key)}" data-weight-entity="{escape(room.get('weight', ''))}" data-weight="{weight}">
                  <div class="room-settings-head">
                    <div class="room-info">
                      <div class="room-title-line">
                        {sort_controls}
                        <strong><span>{index}.</span> <b class="room-name-editable" data-room-name data-segment-id="{escape(segment.get("segment_id"))}" data-room-names-entity="{escape(room_names_entity)}">{escape(segment.get("name"))}</b><button type="button" class="room-name-edit-btn" aria-label="Lokalen Raumnamen bearbeiten" title="Ändert nur den Namen in GhostVacuum, nicht in der MOVA-App">✎</button></strong>
                      </div>
                      <span>{escape(meta or "Noch keine Laufdaten")}</span>
                    </div>
                    <div class="room-actions">
                      {enabled_button}
                    </div>
                  </div>
                  <div class="room-settings-grid">
                    {interval_row}
                    <div class="room-completion" data-completion-row data-entity="{escape(room.get('completion_percent', ''))}">
                      <label>Als gereinigt werten
                        <select data-completion-mode aria-label="{escape(segment.get('name'))}: Als gereinigt werten">
                          <option value="complete" {'selected' if (number_value(state_value(room.get('completion_percent_state'), ''), 100) or 100) >= 100 else ''}>Nur vollständig abgeschlossen</option>
                          <option value="percent" {'selected' if (number_value(state_value(room.get('completion_percent_state'), ''), 100) or 100) < 100 else ''}>Ab Fortschritt</option>
                        </select>
                      </label>
                      <label data-completion-threshold {'hidden' if (number_value(state_value(room.get('completion_percent_state'), ''), 100) or 100) >= 100 else ''}>Mindestfortschritt
                        <span><input type="number" min="1" max="99" step="1" value="{int(number_value(state_value(room.get('completion_percent_state'), ''), 100) if 0 < (number_value(state_value(room.get('completion_percent_state'), ''), 100) or 100) < 100 else 80)}" data-completion-value aria-label="{escape(segment.get('name'))}: Mindestfortschritt"> %</span>
                      </label>
                    </div>
                  </div>
                  {render_cleaning_profile_editor(room.get('cleaning_profile', ''), parse_cleaning_profile(state_value(room.get('cleaning_profile_state'), '')), cleaning_defaults(summary), segment.get('name', room_key))}
                </article>
            """
        )
    return "".join(
        f'<section class="room-group"><div class="section-header"><h3>{label} <span data-room-count>{len(rendered[enabled])}</span></h3>'
        f'<span class="section-hint">{hint}</span></div><div class="room-group-list" data-room-group="{group}">'
        + "".join(rendered[enabled]) + f'</div><p class="empty room-group-empty" {"hidden" if rendered[enabled] else ""}>Keine {"aktiven" if enabled else "inaktiven"} Räume.</p></section>'
        for enabled, group, label, hint in [(True, "active", "Aktive Räume", "Oben = höhere Priorität"), (False, "inactive", "Inaktive Räume", "Nicht in der Automatik berücksichtigt")]
    )


def tracking_choices(summary: dict) -> list[dict]:
    options = summary.get("options", {})
    configured = state_value(summary.get("states", {}).get("global", {}).get("tracking_mode"), "local")
    active = configured if configured in {"local", "waze"} else "local"
    people = selected_presence_from_summary(summary)
    tracked = {item.get("entity_id") for item in summary.get("available_presence_entities", []) if item.get("has_device")}
    local_available = bool(people) and all(person in tracked for person in people)
    result = [{"id": "local", "name": "Entfernungsschätzung", "available": local_available, "active": active == "local",
               "detail": "Zeit bis zuhause aus Standort und Schätzgeschwindigkeit berechnen" if local_available else "Den berücksichtigten Personen in Home Assistant ein Standortgerät zuordnen."}]
    for mode, name, description, units in [
        ("waze", "Waze", "Fahrzeit nach Hause mit aktueller Verkehrslage berechnen", {"min", "s", "h"}),
    ]:
        source = summary.get("tracking_sources", {}).get(mode)
        value = number_value(state_value(source, ""))
        import math
        numeric = value is not None and math.isfinite(value) and value >= 0
        unit = state_attrs(source).get("unit_of_measurement")
        person_matches = len(people) == 1 and people[0] == (options.get("travel_person_entity") or options.get("person_entity"))
        available = numeric and unit in units and person_matches
        detail = description
        result.append({"id":mode,"name":name,"available":available,"active":active == mode,"detail":detail})
    return result


def render_settings_presence(summary: dict) -> str:
    options = summary.get("options", {})
    entity_id = summary.get("entities", {}).get("global", {}).get("selected_presence_entities", "")
    selected = set(selected_presence_from_summary(summary))
    people = summary.get("available_presence_entities", []) or []

    if not people:
        return '<p class="empty">Keine Personen gefunden.</p>'

    # Separate people with and without devices
    with_device = [p for p in people if p.get("has_device")]
    without_device = [p for p in people if not p.get("has_device") and p.get("entity_id", "").startswith("person.")]

    rows = []
    for person in with_device:
        person_id = str(person.get("entity_id") or "")
        active = person_id in selected
        state = person.get("state", "unknown")

        # State display
        state_lower = state.lower()
        if state_lower in ("home", "zuhause"):
            state_display = "Zuhause"
            state_class = "state-home"
        elif state_lower in ("not_home", "away", "nicht_zuhause"):
            state_display = "Unterwegs"
            state_class = "state-away"
        else:
            state_display = state.title()
            state_class = "state-unknown"

        rows.append(
            f"""
            <article class="person-setting {'active' if active else ''}">
              <div class="person-setting-info"><strong>{escape(person.get("name"))}</strong><span class="state-badge {state_class}">{escape(state_display)}</span></div>
              <label class="switch" aria-label="{escape(person.get("name"))} berücksichtigen">
                <input type="checkbox" role="switch" data-presence-person="{escape(person_id)}" {'checked' if active else ''}>
                <span aria-hidden="true"></span><em>Berücksichtigen</em>
              </label>
              {render_person_wifi_picker(person, summary)}
            </article>
            """
        )

    # Show persons without devices (greyed out, not selectable)
    for person in without_device:
        person_id = str(person.get("entity_id") or "")
        rows.append(
            f"""
            <div class="presence-choice disabled" aria-disabled="true">
              <span>
                <strong>{escape(person.get("name"))}</strong>
                <small>{escape(person_id)}</small>
                <span class="state-badge state-no-device">Kein Gerät</span>
              </span>
              <em>Nicht nutzbar</em>
              {render_person_wifi_picker(person, summary)}
            </div>
            """
        )

    tracking = tracking_choices(summary)
    tracking_entity = summary.get("entities", {}).get("global", {}).get("tracking_mode", "")
    travel_person = escape(options.get("travel_person_entity") or options.get("person_entity") or "person.deine_person")
    guides = {
        "local": (
            ["Home Assistant Companion App auf dem Handy einrichten und Standortzugriff erlauben.",
             "In Home Assistant unter Einstellungen → Personen das Handy der Person zur Standortverfolgung zuordnen.",
             "Die Home-Zone prüfen. Die Person muss einen aktuellen Standort mit Breiten- und Längengrad liefern.",
             "Diese Seite neu laden, die Person berücksichtigen und Entfernungsschätzung auswählen. Die Zeit bis zuhause wird direkt aus dem Standort und der Schätzgeschwindigkeit berechnet. Ein zusätzlicher Sensor ist nicht nötig."],
            "https://www.home-assistant.io/integrations/person/"),
        "waze": (
            ["In Home Assistant Einstellungen → Geräte & Dienste → Integration hinzufügen öffnen und Waze Travel Time hinzufügen.",
             f"Als Ursprung <code>{travel_person}</code>, als Ziel die Home-Zone (zum Beispiel <code>zone.home</code>) und für Deutschland die Region EU wählen.",
             "Die Entität des Fahrzeitsensors kopieren und prüfen, ob sie eine gültige Fahrzeit liefert.",
             f"In der GhostVacuum-Konfiguration den Sensor als <code>waze_entity</code> und <code>{travel_person}</code> als <code>travel_person_entity</code> hinterlegen. Diese Zuordnung erfolgt derzeit in der Konfiguration, nicht auf dieser Seite.",
             "Nach Übernahme der Konfiguration diese Seite neu laden und Waze auswählen. Aktuell wird eine berücksichtigte Person mit ihrem Sensor unterstützt."],
            "https://www.home-assistant.io/integrations/waze_travel_time/"),
    }
    rendered_tracking = []
    for item in tracking:
        steps, documentation = guides[item["id"]]
        guide = '<details class="tracking-setup"><summary>Einrichtung</summary><ol>' + ''.join(f'<li>{step}</li>' for step in steps) + f'</ol><a href="{documentation}" target="_blank" rel="noopener">Home-Assistant-Anleitung ↗</a></details>'
        rendered_tracking.append(f'<article class="tracking-option {"selected" if item["active"] else ""} {"unavailable" if not item["available"] else ""}" data-tracking-option="{item["id"]}" data-available="{str(item["available"]).lower()}"><label class="tracking-option-choice"><div><strong>{escape(item["name"])}</strong><span class="tracking-choice-state"><span class="tracking-availability {"available" if item["available"] else ""}">{"Aktiv" if item["active"] else "Verfügbar" if item["available"] else "Nicht verfügbar"}</span><input type="radio" name="tracking-method" value="{item["id"]}" data-tracking-radio aria-label="{escape(item["name"])}" {"checked" if item["active"] else ""} {"disabled" if not item["available"] else ""}></span></div><p>{escape(item["detail"])}</p></label>{guide}</article>')
    tracking_cards = ''.join(rendered_tracking)

    tracking_ui = f'<div class="tracking-options" data-tracking-picker data-entity="{escape(tracking_entity)}"><strong class="tracking-select-label">Rückkehrzeit berechnen mit</strong><div class="tracking-option-list" role="radiogroup" aria-label="Tracking-Methode">{tracking_cards}</div></div>'

    ha_base = os.environ.get("VACUUM_DASHBOARD_API", "").removesuffix("/api").rstrip("/")
    manage_link = f'<a class="manage-people" href="{escape(ha_base)}/config/person" target="_blank" rel="noopener">＋ Person hinzufügen ↗</a>' if ha_base.startswith(("http://", "https://")) else ""
    return f"""
      <div class="presence-picker" data-presence-picker data-presence-entity="{escape(entity_id)}">
        {''.join(rows)}
      </div>
      {manage_link}
      {tracking_ui}
    """


def render_settings_analysis(summary: dict) -> str:
    a = summary.get("status", {}).get("return_analysis") or {}
    policy = a.get("policy") or {}
    total = int(a.get("samples") or 0)
    away = int(a.get("away_samples") or 0)
    below = int(a.get("below_buffer_samples") or 0)
    hint = f"Bei {below} von {away} auswertbaren Abwesenheitsmessungen lag die Heimwegzeit innerhalb deines Puffers." if away else "Noch keine auswertbaren Abwesenheitsmessungen."
    rows = ''.join(f"<tr><td>{escape(r['buffer_min'])} min</td><td>{escape(r['start_samples'])}</td><td>{escape(r['stop_samples'])}</td></tr>" for r in a.get('simulations', []))
    if away >= 30 and below / away >= .5:
        hint += " Dein Puffer blockiert häufig. Vergleiche kleinere Werte unten; die sichere Rückfahrtdauer ist noch nicht vermessen."
    reasons = ''.join(f"<div class='system-row'><span>{escape(key)}</span><strong>{escape(value)}</strong></div>" for key, value in (a.get('reasons') or {}).items())
    historical = a.get("motion_history") or {}
    history_metrics = ''.join(f'<div><strong>{int(historical.get(key) or 0)}</strong><span>{label}</span></div>' for key, label in [
        ('gps_reports', 'GPS-Meldungen'), ('usable_reports', 'Auswertbare Meldungen'),
        ('dwell_episodes', 'Erkannte Aufenthalte'), ('extra_release_reports', 'Zusätzliche Startgelegenheiten'),
        ('approach_stops', 'Annäherungen an Stoppgrenze'), ('gaps', 'Datenlücken über 10 min')])
    from zoneinfo import ZoneInfo
    def history_date(value):
        parsed = parse_datetime(value)
        return parsed.astimezone(ZoneInfo('Europe/Berlin')).strftime('%d.%m.%Y %H:%M') if parsed else 'keine Daten'
    coverage = f"Aufgezeichnet: {history_date(historical.get('first_report'))} bis {history_date(historical.get('last_report'))}." if historical else 'Historische Standortmeldungen noch nicht ausgewertet.'
    historical_section = f'<section class="section"><div class="section-header"><h2>Aufenthalt & Annäherung · Historie</h2></div><p>{escape(coverage)}</p><div class="analysis-metrics">{history_metrics}</div><p class="analysis-note">{int(historical.get("home_reports") or 0)} Meldungen zuhause · {int(historical.get("unknown_presence_reports") or 0)} mit historisch unvollständiger Anwesenheit · {int(historical.get("unusable_gps_reports") or 0)} mit unbrauchbaren GPS-Daten. Null erkannte Aufenthalte bedeutet bei Datenlücken nicht, dass keine stattgefunden haben.</p><p class="analysis-note">{escape(historical.get("note", "Fehlende Standortverläufe werden nicht aus Rückkehrzeiten rekonstruiert."))}</p><p class="analysis-note">Vergleich: {escape(historical.get("speed_kmh", "—"))} km/h · Puffer {escape(historical.get("buffer_min", "—"))} min · Startabstand {escape(historical.get("margin_min", "—"))} min. Seit Einführung: {int(a.get("stationary_samples") or 0)} Messpunkte mit Aufenthaltsfreigabe. Keine Speicherung genauer Positionen in dieser Auswertung.</p></section>'
    return f"""<section class="section analysis-live">
      <div class="section-header"><h2>Jetzt · Beobachtung</h2></div>
      <strong class="analysis-decision">{escape(a.get('decision', 'Daten werden geladen'))}</strong>
      <p>{escape(a.get('detail', 'Die Auswertung beginnt mit der nächsten Standortmeldung.'))}</p>
      <div class="analysis-metrics"><div><strong>{escape(policy.get('start_at_min', '—'))} min</strong><span>Start ab</span></div><div><strong>{escape(policy.get('stop_at_min', '—'))} min</strong><span>Reinigung stoppen bei</span></div><div><strong>{escape(policy.get('eta_min') if policy.get('eta_min') is not None else '—')} min</strong><span>Zeit bis zuhause</span></div></div>
      <p class="analysis-note">Aufenthalt: mindestens 10 min innerhalb von 75 m mit frischen GPS-Meldungen. Laufende Reinigung stoppt bei Annäherung innerhalb des Puffers. Bewegung weg von zuhause stoppt sie nicht. Bei Datenlücken gilt wieder die Zeitgrenze.</p>
    </section>
    {historical_section}
    <section class="section"><div class="section-header"><h2>Puffer vergleichen</h2></div>
      <p>{escape(hint)}</p>
      <table class="analysis-table"><thead><tr><th>Puffer</th><th>Start möglich</th><th>Stopgrenze erreicht</th></tr></thead><tbody>{rows}</tbody></table>
      <p class="analysis-note">{total} Messpunkte · letzte 14 Tage · seit {escape(str(a.get('since'))[:16].replace('T', ' ') if a.get('since') else 'Beginn der Aufzeichnung')}. Verglichen werden dieselben Messpunkte mit dem aktuellen Startabstand. Messlücken werden nicht hochgerechnet. Das sind mögliche Zeitfenster, keine zusätzlichen abgeschlossenen Reinigungen.</p>
    </section>
    <section class="section"><div class="section-header"><h2>Beobachtete Gründe</h2></div><div class="system-list">{reasons or '<p>Noch keine Messungen.</p>'}</div></section>
    <section class="section"><div class="section-header"><h2>Reinigungsergebnisse</h2></div><p>{int(a.get('actual_runs') or 0)} bestätigte Automatik-Läufe · {int(a.get('interrupted_runs') or 0)} unterbrochen</p><p class="analysis-note">Extern gestartete Läufe werden nicht als Automatik-Läufe gezählt. Prozentwerte werden nicht aus der Laufzeit geschätzt.</p><p>{escape(a.get('progress_note', 'Raumfortschritt noch nicht bestätigt.'))}</p></section>"""


def render_settings_system(summary: dict) -> str:
    options = summary.get("options", {})
    entities = summary.get("entities", {})
    global_entities = entities.get("global", {})
    one_time_entity = global_entities.get("one_time_room_override", "")
    rows = [
        ("Staubsauger", options.get("vacuum_entity")),
        ("Benachrichtigung", options.get("notify_service") or "deaktiviert"),
        ("Bewohner", options.get("presence_entities")),
        ("Reise-Person", options.get("travel_person_entity")),
        ("Home-Zone", options.get("home_zone")),
        ("Reise-Pause-Zone", options.get("travel_pause_zone") or "nicht gesetzt"),
        ("Historie", f"{options.get('history_weeks', '-')} Wochen"),
        ("Lernfenster", f"{options.get('learning_window', '-')} Läufe"),
    ]
    entity_rows = "".join(
        f"<div class=\"system-row\"><span>{escape(label)}</span><strong>{escape('n/a' if value in (None, '') else value)}</strong></div>"
        for label, value in rows
    )
    override_action = ""
    if one_time_entity:
        override_value = state_value(summary.get("states", {}).get("global", {}).get("one_time_room_override"), "")
        override_action = f"""
          <div class="system-action">
            <div>
              <strong>Einmaliger Raum-Override</strong>
              <span>{escape(override_value or "Kein Override gesetzt")}</span>
            </div>
            <button type="button" data-clear-text="{escape(one_time_entity)}">Zurücksetzen</button>
          </div>
        """
    return entity_rows + override_action


def render_settings_html() -> str:
    summary = build_summary()
    replacements = {
        "missing_helpers_banner": render_missing_helpers_banner(summary),
        "settings_toggles": render_settings_toggles(summary),
        "settings_travel_toggle": render_settings_travel_toggle(summary),
        "settings_schedule": render_settings_schedule(summary),
        "settings_weekdays": render_settings_weekdays(summary),
        "settings_push": render_settings_push(summary),
        "settings_numbers": render_settings_numbers(summary),
        "settings_home_map": render_settings_home_map(summary),
        "settings_presence": render_settings_presence(summary),
        "settings_rooms": render_settings_rooms(summary),
        "settings_cleaning_defaults": render_settings_cleaning_defaults(summary),
        "settings_system": render_settings_system(summary),
        "settings_analysis": render_settings_analysis(summary),
    }
    output = load_settings_template()
    for key, value in replacements.items():
        output = output.replace("{{{" + key + "}}}", str(value))
        output = output.replace("{{" + key + "}}", escape(value))
    return output


SETTINGS_HTML = """<!doctype html>
<html lang="de">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Einstellungen</title></head>
<body><main><a href="/">Zurueck</a><h1>Einstellungen</h1>{{{settings_toggles}}}{{{settings_numbers}}}{{{settings_rooms}}}{{{settings_system}}}</main></body>
</html>
"""


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
    # Browsers may pre-open idle connections. Bound their lifetime independently.
    timeout = 10

    def _observation_html(self, content):
        if 'data-live-observation="true"' in content:
            return content
        if not load_options().get("observation_mode", False):
            return content
        banner = '<aside style="position:sticky;top:0;z-index:10000;background:#164b36;color:white;padding:16px;text-align:center;font:600 16px system-ui">Beobachtungsbetrieb · Automatik AUS · Gerätebefehle gesperrt</aside>'
        content = re.sub(r"(<body[^>]*>)", lambda m: m.group(1) + banner, content, count=1)
        catalog = build_summary().get("room_segments", [])
        rooms = "".join(f'<li>{escape(room["name"])} · Segment {room["segment_id"]}</li>' for room in catalog if room.get("detected"))
        room_panel = '<section class="card"><h2>Erkannte Räume</h2><ul>' + (rooms or '<li>Noch keine Raumdaten verfügbar</li>') + '</ul><p>Es werden keine Reinigungen geplant oder gestartet.</p></section>'
        summary = build_summary()
        status = summary.get("status", {})
        presence_panel = '<section class="card"><h2>Anwesenheit und Heimweg</h2>' + render_presence_rows(summary)
        speed = summary.get("states", {}).get("global", {}).get("fallback_speed")
        speed_value = state_value(speed, summary.get("options", {}).get("fallback_speed_kmh", 30))
        presence_panel += '<p>Heimweg: lokale Luftlinien-Schätzung mit ' + escape(speed_value) + ' km/h, keine ÖPNV-Route.</p>'
        presence_panel += '<p>Reisemodus: alle Personen jeweils ' + escape(status.get("travel_pause_after_hours")) + ' Stunden außerhalb von ' + escape(status.get("travel_pause_radius_km")) + ' km. Aktiv: ' + ('ja' if status.get("travel_mode_active") else 'nein') + '.</p></section>'
        room_panel = presence_panel + render_robot_diagnostics(summary) + room_panel
        content = re.sub(r'<section class="card queue-card".*?</section>', lambda m: room_panel, content, count=1, flags=re.S)
        content = content.replace('</head>', '<style>.hero-settings-list,.presence-overview,.travel-guard,.dashboard-right{display:none!important}.dashboard-grid{grid-template-columns:1fr!important}</style></head>')
        if urllib.parse.urlparse(self.path).path == "/settings":
            script = '<script>function lockObservation(){document.querySelectorAll("[data-setting-key=enabled],[data-action=repair-helpers],[data-clear-text]").forEach(e=>{e.disabled=true;e.title="Im Beobachtungsbetrieb gesperrt";if(e.type==="checkbox")e.checked=false;});}lockObservation();new MutationObserver(lockObservation).observe(document.body,{childList:true,subtree:true});</script>'
        else:
            script = '<script>function lockObservation(){document.querySelectorAll("button:not([data-tab]),input,select,textarea").forEach(e=>{e.disabled=true;e.title="Im Beobachtungsbetrieb gesperrt";});}lockObservation();new MutationObserver(lockObservation).observe(document.body,{childList:true,subtree:true});</script>'
        return content.replace("</body>", script + "</body>")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/analysis":
            self._json_response({"html": render_settings_analysis(build_summary())})
            return
        if parsed.path == "/api/dashboard":
            self._json_response(build_live_dashboard_view(build_summary()))
            return
        if parsed.path == "/api/summary":
            self._json_response(build_summary())
            return
        if parsed.path.startswith("/static/"):
            self._handle_static(parsed.path)
            return
        if parsed.path == "/settings":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(self._observation_html(render_settings_html()).encode("utf-8"))
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(self._observation_html(render_dashboard_html()).encode("utf-8"))

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if load_options().get("observation_mode", False) and parsed.path not in {"/api/toggle", "/api/set_number", "/api/set_text", "/api/person_wifi"}:
            self._json_response(
                {"ok": False, "error": "Beobachtungsmodus: Gerätebefehle sind gesperrt."},
                status=403,
            )
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in {'/api/move_room', '/api/reset_room_order'}:
            try:
                if parsed.path == '/api/reset_room_order':
                    summary = build_summary()
                    service_call('input_text','set_value',summary['entities']['global']['one_time_room_override'],{'value':''})
                else:
                    payload = json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))))
                    save_dashboard_room_move(payload.get('room_key'), payload.get('target'), payload.get('before'))
                self._json_response({'ok':True})
            except (ValueError, TypeError) as error:
                self._json_response({'ok':False,'error':str(error)},status=400)
            except Exception:
                self._json_response({'ok':False,'error':'Raum konnte nicht verschoben werden.'},status=502)
            return
        if parsed.path == "/api/start_room":
            self._handle_start_room()
            return
        if parsed.path == "/api/pause_cleaning":
            self._handle_pause_cleaning()
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
        if parsed.path == "/api/person_wifi":
            try:
                payload = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
                save_person_wifi(payload.get('person'), payload.get('device', ''))
                self._json_response({'ok': True})
            except (ValueError, TypeError) as error:
                self._json_response({'ok': False, 'error': str(error)}, status=400)
            except Exception:
                self._json_response({'ok': False, 'error': 'Gerätezuordnung konnte nicht gespeichert werden.'}, status=502)
            return
        if parsed.path == "/api/set_number":
            self._handle_set_number()
            return
        if parsed.path == "/api/set_text":
            self._handle_set_text()
            return
        if parsed.path == "/api/recreate_helpers":
            self._handle_recreate_helpers()
            return
        if parsed.path == "/api/cleanup_helpers":
            self._handle_cleanup_helpers()
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

    def _handle_pause_cleaning(self):
        options = load_options()
        vacuum_entity = options.get("vacuum_entity", "")
        if not vacuum_entity:
            self._json_response({"ok": False, "error": "no vacuum configured"}, status=400)
            return
        try:
            service_call("vacuum", "pause", vacuum_entity)
            self._json_response({"ok": True})
        except Exception as err:
            self._json_response({"ok": False, "error": str(err)}, status=502)

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

        if load_options().get("observation_mode", False) and entity_id not in editable_setting_entities(load_options()):
            self._json_response({"ok": False, "error": "Beobachtungsmodus: Dieses Feld bleibt gesperrt."}, status=403)
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
        config = load_options()
        entity_map = helper_entities(config, parse_rooms(config.get("rooms")))
        limits = {entity_map["global"]["return_buffer"]: (0, 30), entity_map["global"]["start_margin"]: (1, 60)}
        limits.update({room["completion_percent"]: (1, 100) for room in entity_map["rooms"]})
        if entity_id in limits:
            numeric = number_value(value)
            import math
            low, high = limits[entity_id]
            if numeric is None or not math.isfinite(numeric) or not numeric.is_integer() or not low <= numeric <= high:
                self._json_response({"ok": False, "error": f"Ganze Zahl zwischen {low} und {high} erforderlich."}, status=400)
                return
            value = int(numeric)
        buffer_entity = entity_map["global"]["return_buffer"]
        if entity_id == buffer_entity:
            numeric = number_value(value)
            import math
            if numeric is None or not math.isfinite(numeric) or not numeric.is_integer():
                self._json_response({"ok": False, "error": "Puffer muss eine ganze Minutenzahl sein."}, status=400)
                return
            value = int(numeric)
        if not entity_id.startswith("input_number."):
            self._json_response({"ok": False, "error": "unsupported entity"}, status=400)
            return

        if load_options().get("observation_mode", False) and entity_id not in editable_setting_entities(load_options()):
            self._json_response({"ok": False, "error": "Beobachtungsmodus: Dieses Feld bleibt gesperrt."}, status=403)
            return

        try:
            service_call("input_number", "set_value", entity_id, {"value": value})
            self._json_response({"ok": True})
        except urllib.error.HTTPError as err:
            self._json_response({"ok": False, "error": str(err)}, status=502)
        except Exception as err:
            self._json_response({"ok": False, "error": str(err)}, status=500)

    def _handle_set_text(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._json_response({"ok": False, "error": "invalid payload"}, status=400)
            return

        entity_id = str(payload.get("entity_id") or "")
        value = str(payload.get("value") or "")
        profile_entities = helper_entities(load_options(), parse_rooms(load_options().get("rooms")))
        is_default = entity_id == profile_entities["global"]["cleaning_defaults"]
        is_room = entity_id in {r["cleaning_profile"] for r in profile_entities["rooms"]}
        if is_default or is_room:
            try:
                profile = json.loads(value)
            except (ValueError, TypeError):
                profile = None
            if not valid_cleaning_profile(profile, inherit=is_room):
                self._json_response({"ok":False,"error":"Ungültige Reinigungseinstellungen."},status=400)
                return
            value = json.dumps(profile, separators=(",", ":"))
        if entity_id == helper_entities(load_options(), [])["global"]["tracking_mode"]:
            choices = tracking_choices(build_summary())
            if not any(item["id"] == value and item["available"] for item in choices):
                self._json_response({"ok": False, "error": "Tracking-Methode nicht verfügbar."}, status=400)
                return
        if not entity_id.startswith("input_text."):
            self._json_response({"ok": False, "error": "unsupported entity"}, status=400)
            return

        if load_options().get("observation_mode", False) and entity_id not in editable_setting_entities(load_options()):
            self._json_response({"ok": False, "error": "Beobachtungsmodus: Dieses Feld bleibt gesperrt."}, status=403)
            return

        try:
            service_call("input_text", "set_value", entity_id, {"value": value})
            self._json_response({"ok": True})
        except urllib.error.HTTPError as err:
            self._json_response({"ok": False, "error": str(err)}, status=502)
        except Exception as err:
            self._json_response({"ok": False, "error": str(err)}, status=500)

    def _handle_recreate_helpers(self):
        """Trigger recreation of missing helper entities."""
        import subprocess
        import sys
        from pathlib import Path

        options = load_options()
        helper_prefix = options.get("helper_prefix", "vacuum_automation")

        # Find helper_setup.py - check multiple locations
        script_paths = [
            Path("/opt/vacuum_automation/helper_setup.py"),  # Container location
            Path(__file__).parent / "helper_setup.py",  # Same directory as this file
        ]
        script_path = None
        for path in script_paths:
            if path.exists():
                script_path = str(path)
                break

        if not script_path:
            self._json_response({
                "ok": False,
                "error": "helper_setup.py not found",
                "searched": [str(p) for p in script_paths],
            }, status=500)
            return

        # Check for SUPERVISOR_TOKEN
        supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "")
        if not supervisor_token:
            self._json_response({
                "ok": False,
                "error": "SUPERVISOR_TOKEN not set - helper creation only works inside the add-on",
                "hint": "Run this inside Home Assistant, not locally",
            }, status=400)
            return

        try:
            # Run the helper_setup script with --check flag
            result = subprocess.run(
                [sys.executable, script_path, "--check"],
                capture_output=True,
                text=True,
                timeout=60,
                env={**os.environ, "SUPERVISOR_TOKEN": supervisor_token},
            )
            self._json_response({
                "ok": result.returncode == 0,
                "helper_prefix": helper_prefix,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode,
            })
        except subprocess.TimeoutExpired:
            self._json_response({"ok": False, "error": "timeout"}, status=504)
        except FileNotFoundError as err:
            self._json_response({"ok": False, "error": f"Command not found: {err}"}, status=500)
        except Exception as err:
            self._json_response({"ok": False, "error": str(err)}, status=500)

    def _handle_cleanup_helpers(self):
        """Delete all helper entities created by this add-on."""
        import subprocess
        import sys
        from pathlib import Path

        # Find helper_setup.py - check multiple locations
        script_paths = [
            Path("/opt/vacuum_automation/helper_setup.py"),  # Container location
            Path(__file__).parent / "helper_setup.py",  # Same directory as this file
        ]
        script_path = None
        for path in script_paths:
            if path.exists():
                script_path = str(path)
                break

        if not script_path:
            self._json_response({
                "ok": False,
                "error": "helper_setup.py not found",
            }, status=500)
            return

        supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "")
        if not supervisor_token:
            self._json_response({
                "ok": False,
                "error": "SUPERVISOR_TOKEN not set - cleanup only works inside the add-on",
            }, status=400)
            return

        try:
            # Run the helper_setup script with --cleanup flag
            result = subprocess.run(
                [sys.executable, script_path, "--cleanup"],
                capture_output=True,
                text=True,
                timeout=120,
                env={**os.environ, "SUPERVISOR_TOKEN": supervisor_token},
            )
            self._json_response({
                "ok": result.returncode == 0,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode,
            })
        except subprocess.TimeoutExpired:
            self._json_response({"ok": False, "error": "timeout"}, status=504)
        except FileNotFoundError as err:
            self._json_response({"ok": False, "error": f"Command not found: {err}"}, status=500)
        except Exception as err:
            self._json_response({"ok": False, "error": str(err)}, status=500)

    def _handle_static(self, path: str):
        name = Path(urllib.parse.unquote(path)).name
        if name not in {"leaflet.js", "leaflet.css"}:
            self.send_response(404)
            self.end_headers()
            return

        file_path = LOCAL_STATIC_PATH / name
        if not file_path.exists():
            self.send_response(404)
            self.end_headers()
            return

        content_type = "text/css; charset=utf-8" if name.endswith(".css") else "application/javascript; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(file_path.read_bytes())

    def _json_response(self, payload: dict, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode("utf-8"))

    def log_message(self, format, *args):
        return


class DashboardHTTPServer(ThreadingHTTPServer):
    """Keep idle browser connections from blocking status and safety responses."""


def main():
    port = int(os.environ.get("SIDEBAR_REDIRECT_PORT", "8099"))
    server = DashboardHTTPServer(("0.0.0.0", port), DashboardHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
