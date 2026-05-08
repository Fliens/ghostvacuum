#!/usr/bin/env python3
"""Render AppDaemon, helper, and dashboard config from Home Assistant add-on options."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import yaml


OPTIONS_PATH = Path("/data/options.json")
APPDAEMON_CONFIG_PATH = Path("/config/appdaemon/appdaemon.yaml")
APPDAEMON_APPS_DIR = Path("/config/appdaemon/apps")
APP_CONFIG_PATH = APPDAEMON_APPS_DIR / "vacuum_arrival_automation.yaml"
GENERATED_DIR = Path("/config/vacuum_arrival_automation")
HELPERS_PATH = GENERATED_DIR / "helpers.generated.yaml"
STANDARD_DASHBOARD_PATH = GENERATED_DIR / "dashboard.generated.yaml"
MUSHROOM_DASHBOARD_PATH = GENERATED_DIR / "dashboard_mushroom.generated.yaml"

DEFAULT_PRESENCE_ENTITIES = ["person.resident_1"]
DEFAULT_ROOMS = [
    {
        "id": "bad",
        "name": "Bad",
        "segment_id": 1,
        "interval_h": 24,
        "weight": 1.4,
        "duration_min": 15,
        "enabled": True,
        "icon": "mdi:shower",
    },
    {
        "id": "kueche",
        "name": "Kueche",
        "segment_id": 2,
        "interval_h": 48,
        "weight": 1.2,
        "duration_min": 12,
        "enabled": True,
        "icon": "mdi:silverware-fork-knife",
    },
    {
        "id": "wohnzimmer",
        "name": "Wohnzimmer",
        "segment_id": 3,
        "interval_h": 72,
        "weight": 1.0,
        "duration_min": 20,
        "enabled": True,
        "icon": "mdi:sofa",
    },
    {
        "id": "schlafzimmer",
        "name": "Schlafzimmer",
        "segment_id": 4,
        "interval_h": 72,
        "weight": 1.0,
        "duration_min": 18,
        "enabled": True,
        "icon": "mdi:bed-king-outline",
    },
]


class NoAliasDumper(yaml.SafeDumper):
    def ignore_aliases(self, data: Any) -> bool:
        return True


def read_options() -> Dict[str, Any]:
    if not OPTIONS_PATH.exists():
        return {}
    return json.loads(OPTIONS_PATH.read_text())


def normalize_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def slugify(value: str) -> str:
    text = value.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_") or "room"


def parse_presence_entities(value: Any) -> List[str]:
    if isinstance(value, list):
        items = [normalize_str(item) for item in value]
        return [item for item in items if item]

    text = normalize_str(value)
    if not text:
        return list(DEFAULT_PRESENCE_ENTITIES)

    parsed: Any = None
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError:
        parsed = None

    if isinstance(parsed, list):
        items = [normalize_str(item) for item in parsed]
        return [item for item in items if item]

    raw_items = [part.strip() for part in re.split(r"[\n,]+", text)]
    entities = [item for item in raw_items if item]
    return entities or list(DEFAULT_PRESENCE_ENTITIES)


def parse_rooms(value: Any) -> List[Dict[str, Any]]:
    parsed: Any = value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            parsed = None
        else:
            parsed = yaml.safe_load(text)

    if not parsed:
        parsed = DEFAULT_ROOMS

    if not isinstance(parsed, list):
        raise ValueError("rooms must be a YAML/JSON list")

    rooms: List[Dict[str, Any]] = []
    for index, item in enumerate(parsed, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"room #{index} must be a mapping")

        room_id = normalize_str(item.get("id")) or normalize_str(item.get("slug"))
        room_name = normalize_str(item.get("name"))
        room_id = slugify(room_id or room_name or f"room_{index}")
        room_name = room_name or room_id.replace("_", " ").title()
        segment_id = item.get("segment_id")
        if segment_id in [None, ""]:
            raise ValueError(f"room '{room_id}' is missing segment_id")

        room = {
            "id": room_id,
            "name": room_name,
            "segment_id": int(segment_id),
            "interval_h": float(item.get("interval_h", 48)),
            "weight": float(item.get("weight", 1.0)),
            "duration_min": int(item.get("duration_min", 15)),
            "enabled": bool(item.get("enabled", True)),
            "icon": normalize_str(item.get("icon")) or "mdi:robot-vacuum",
        }
        rooms.append(room)

    return rooms


def helper_object_id(entity_id: Optional[str], domain: str) -> Optional[str]:
    if not entity_id:
        return None
    if "." not in entity_id:
        raise ValueError(f"helper entity '{entity_id}' must include a domain")
    helper_domain, object_id = entity_id.split(".", 1)
    if helper_domain != domain:
        raise ValueError(
            f"helper entity '{entity_id}' must use domain '{domain}', not '{helper_domain}'"
        )
    return object_id


def default_options(raw_options: Dict[str, Any]) -> Dict[str, Any]:
    helper_prefix = normalize_str(raw_options.get("helper_prefix")) or "vacuum_automation"
    dashboard_prefix = (
        normalize_str(raw_options.get("dashboard_prefix")) or "vacuum_automation"
    )

    return {
        "appdaemon_http_url": normalize_str(
            ((raw_options.get("appdaemon_http") or {}).get("url"))
        )
        or "http://127.0.0.1:5050",
        "vacuum_entity": normalize_str(raw_options.get("vacuum_entity"))
        or "vacuum.robot_vacuum",
        "notify_service": normalize_str(raw_options.get("notify_service")),
        "dashboard_path": normalize_str(raw_options.get("dashboard_path")) or "",
        "presence_entities": parse_presence_entities(raw_options.get("presence_entities")),
        "selected_presence_entities_entity": normalize_str(
            raw_options.get("selected_presence_entities_entity")
        )
        or f"input_text.{helper_prefix}_selected_presence_entities",
        "person_entity": normalize_str(raw_options.get("person_entity")) or "person.resident_1",
        "travel_person_entity": normalize_str(raw_options.get("travel_person_entity"))
        or normalize_str(raw_options.get("person_entity"))
        or "person.resident_1",
        "waze_entity": normalize_str(raw_options.get("waze_entity")),
        "distance_entity": normalize_str(raw_options.get("distance_entity")),
        "home_zone": normalize_str(raw_options.get("home_zone")) or "zone.home",
        "travel_pause_zone": normalize_str(raw_options.get("travel_pause_zone")),
        "travel_pause_after_hours": float(
            raw_options.get("travel_pause_after_hours", 24)
        ),
        "state_helper": normalize_str(raw_options.get("state_helper"))
        or f"input_text.{helper_prefix}_state",
        "one_time_room_override_entity": normalize_str(
            raw_options.get("one_time_room_override_entity")
        )
        or f"input_text.{helper_prefix}_one_time_room_override",
        "enabled_entity": normalize_str(raw_options.get("enabled_entity"))
        or f"input_boolean.{helper_prefix}_enabled",
        "learning_enabled_entity": normalize_str(
            raw_options.get("learning_enabled_entity")
        )
        or f"input_boolean.{helper_prefix}_learning_enabled",
        "dashboard_prefix": dashboard_prefix,
        "helper_prefix": helper_prefix,
        "storage_dir": normalize_str(raw_options.get("storage_dir"))
        or "/config/appdaemon/storage/vacuum_automation",
        "max_history_entries": int(raw_options.get("max_history_entries", 1500)),
        "history_weeks": int(raw_options.get("history_weeks", 8)),
        "learning_window": int(raw_options.get("learning_window", 6)),
        "start_hour_entity": normalize_str(raw_options.get("start_hour_entity"))
        or f"input_number.{helper_prefix}_start_hour",
        "end_hour_entity": normalize_str(raw_options.get("end_hour_entity"))
        or f"input_number.{helper_prefix}_end_hour",
        "return_buffer_entity": normalize_str(raw_options.get("return_buffer_entity"))
        or f"input_number.{helper_prefix}_return_buffer",
        "fallback_speed_entity": normalize_str(raw_options.get("fallback_speed_entity"))
        or f"input_number.{helper_prefix}_fallback_speed",
        "default_travel_time_entity": normalize_str(
            raw_options.get("default_travel_time_entity")
        )
        or f"input_number.{helper_prefix}_default_travel_time",
        "home_override_enabled_entity": normalize_str(
            raw_options.get("home_override_enabled_entity")
        )
        or f"input_boolean.{helper_prefix}_home_override_enabled",
        "home_latitude_entity": normalize_str(raw_options.get("home_latitude_entity"))
        or f"input_number.{helper_prefix}_home_latitude",
        "home_longitude_entity": normalize_str(raw_options.get("home_longitude_entity"))
        or f"input_number.{helper_prefix}_home_longitude",
        "travel_pause_radius_entity": normalize_str(
            raw_options.get("travel_pause_radius_entity")
        )
        or f"input_number.{helper_prefix}_travel_pause_radius",
        "max_distance_km_entity": normalize_str(
            raw_options.get("max_distance_km_entity")
        )
        or f"input_number.{helper_prefix}_max_distance_km",
        "travel_mode_enabled_entity": normalize_str(
            raw_options.get("travel_mode_enabled_entity")
        )
        or f"input_boolean.{helper_prefix}_travel_mode_enabled",
        "start_notifications_enabled_entity": normalize_str(
            raw_options.get("start_notifications_enabled_entity")
        )
        or f"input_boolean.{helper_prefix}_start_notifications_enabled",
        "return_summary_enabled_entity": normalize_str(
            raw_options.get("return_summary_enabled_entity")
        )
        or f"input_boolean.{helper_prefix}_return_summary_enabled",
        "active_weekdays_entity": normalize_str(
            raw_options.get("active_weekdays_entity")
        )
        or f"input_text.{helper_prefix}_active_weekdays",
        "start_hour": int(raw_options.get("start_hour", 8)),
        "end_hour": int(raw_options.get("end_hour", 22)),
        "active_weekdays": raw_options.get(
            "active_weekdays", "mon,tue,wed,thu,fri,sat"
        ),
        "check_interval_min": int(raw_options.get("check_interval_min", 30)),
        "monitor_interval_min": int(raw_options.get("monitor_interval_min", 5)),
        "return_buffer_min": int(raw_options.get("return_buffer_min", 5)),
        "fallback_speed_kmh": float(raw_options.get("fallback_speed_kmh", 30)),
        "default_travel_time_min": float(
            raw_options.get("default_travel_time_min", 60)
        ),
        "home_override_enabled": bool(raw_options.get("home_override_enabled", False)),
        "home_latitude": float(raw_options.get("home_latitude", 52.52)),
        "home_longitude": float(raw_options.get("home_longitude", 13.405)),
        "travel_pause_radius_km": float(
            raw_options.get("travel_pause_radius_km", 100)
        ),
        "max_distance_km": float(raw_options.get("max_distance_km", 0)),
        "travel_mode_enabled": bool(raw_options.get("travel_mode_enabled", True)),
        "start_notifications_enabled": bool(
            raw_options.get("start_notifications_enabled", False)
        ),
        "return_summary_enabled": bool(
            raw_options.get("return_summary_enabled", True)
        ),
        "simulation_mode": bool(raw_options.get("simulation_mode", False)),
        "rooms": parse_rooms(raw_options.get("rooms")),
        "generate_helper_package": bool(
            raw_options.get("generate_helper_package", True)
        ),
    }


def build_app_config(options: Dict[str, Any]) -> Dict[str, Any]:
    app_config: Dict[str, Any] = {
        "module": "vacuum_automation",
        "class": "VacuumAutomation",
        "vacuum_entity": options["vacuum_entity"],
        "dashboard_path": options["dashboard_path"],
        "presence_entities": options["presence_entities"],
        "selected_presence_entities_entity": options[
            "selected_presence_entities_entity"
        ],
        "person_entity": options["person_entity"],
        "travel_person_entity": options["travel_person_entity"],
        "home_zone": options["home_zone"],
        "travel_pause_after_hours": options["travel_pause_after_hours"],
        "state_helper": options["state_helper"],
        "one_time_room_override_entity": options["one_time_room_override_entity"],
        "enabled_entity": options["enabled_entity"],
        "learning_enabled_entity": options["learning_enabled_entity"],
        "dashboard_prefix": options["dashboard_prefix"],
        "helper_prefix": options["helper_prefix"],
        "storage_dir": options["storage_dir"],
        "max_history_entries": options["max_history_entries"],
        "history_weeks": options["history_weeks"],
        "learning_window": options["learning_window"],
        "start_hour_entity": options["start_hour_entity"],
        "end_hour_entity": options["end_hour_entity"],
        "return_buffer_entity": options["return_buffer_entity"],
        "fallback_speed_entity": options["fallback_speed_entity"],
        "default_travel_time_entity": options["default_travel_time_entity"],
        "home_override_enabled_entity": options["home_override_enabled_entity"],
        "home_latitude_entity": options["home_latitude_entity"],
        "home_longitude_entity": options["home_longitude_entity"],
        "travel_pause_radius_entity": options["travel_pause_radius_entity"],
        "max_distance_km_entity": options["max_distance_km_entity"],
        "travel_mode_enabled_entity": options["travel_mode_enabled_entity"],
        "start_notifications_enabled_entity": options[
            "start_notifications_enabled_entity"
        ],
        "return_summary_enabled_entity": options[
            "return_summary_enabled_entity"
        ],
        "active_weekdays_entity": options["active_weekdays_entity"],
        "start_hour": options["start_hour"],
        "end_hour": options["end_hour"],
        "active_weekdays": options["active_weekdays"],
        "check_interval_min": options["check_interval_min"],
        "monitor_interval_min": options["monitor_interval_min"],
        "return_buffer_min": options["return_buffer_min"],
        "fallback_speed_kmh": options["fallback_speed_kmh"],
        "default_travel_time_min": options["default_travel_time_min"],
        "home_override_enabled": options["home_override_enabled"],
        "home_latitude": options["home_latitude"],
        "home_longitude": options["home_longitude"],
        "travel_pause_radius_km": options["travel_pause_radius_km"],
        "max_distance_km": options["max_distance_km"],
        "travel_mode_enabled": options["travel_mode_enabled"],
        "start_notifications_enabled": options["start_notifications_enabled"],
        "return_summary_enabled": options["return_summary_enabled"],
        "simulation_mode": options["simulation_mode"],
        "rooms": {
            room["id"]: {
                "name": room["name"],
                "segment_id": room["segment_id"],
                "interval_h": room["interval_h"],
                "weight": room["weight"],
                "duration_min": room["duration_min"],
                "enabled": room["enabled"],
            }
            for room in options["rooms"]
        },
    }

    for key in ["notify_service", "waze_entity", "distance_entity", "travel_pause_zone"]:
        if options.get(key):
            app_config[key] = options[key]

    return {"vacuum_automation": app_config}


def sensor_entity(prefix: str, suffix: str) -> str:
    return f"sensor.{prefix}_{suffix}"


def room_helper_entity(helper_prefix: str, room_id: str, domain: str, suffix: str) -> str:
    return f"{domain}.{helper_prefix}_{room_id}_{suffix}"


def room_title(room: Dict[str, Any]) -> str:
    return f"Raum {room['name']}"


def build_helpers(options: Dict[str, Any]) -> Dict[str, Any]:
    helper_prefix = options["helper_prefix"]
    state_object = helper_object_id(options["state_helper"], "input_text")
    one_time_room_override_object = helper_object_id(
        options["one_time_room_override_entity"], "input_text"
    )
    active_weekdays_object = helper_object_id(
        options["active_weekdays_entity"], "input_text"
    )
    selected_presence_object = helper_object_id(
        options["selected_presence_entities_entity"], "input_text"
    )
    enabled_object = helper_object_id(options["enabled_entity"], "input_boolean")
    learning_object = helper_object_id(
        options["learning_enabled_entity"], "input_boolean"
    )
    start_hour_object = helper_object_id(options["start_hour_entity"], "input_number")
    end_hour_object = helper_object_id(options["end_hour_entity"], "input_number")
    return_buffer_object = helper_object_id(
        options["return_buffer_entity"], "input_number"
    )
    fallback_speed_object = helper_object_id(
        options["fallback_speed_entity"], "input_number"
    )
    default_travel_time_object = helper_object_id(
        options["default_travel_time_entity"], "input_number"
    )
    home_override_object = helper_object_id(
        options["home_override_enabled_entity"], "input_boolean"
    )
    home_latitude_object = helper_object_id(
        options["home_latitude_entity"], "input_number"
    )
    home_longitude_object = helper_object_id(
        options["home_longitude_entity"], "input_number"
    )
    travel_pause_radius_object = helper_object_id(
        options["travel_pause_radius_entity"], "input_number"
    )
    max_distance_object = helper_object_id(
        options["max_distance_km_entity"], "input_number"
    )
    travel_mode_enabled_object = helper_object_id(
        options["travel_mode_enabled_entity"], "input_boolean"
    )
    start_notifications_enabled_object = helper_object_id(
        options["start_notifications_enabled_entity"], "input_boolean"
    )
    return_summary_enabled_object = helper_object_id(
        options["return_summary_enabled_entity"], "input_boolean"
    )

    input_text = {
        state_object: {
            "name": "Vacuum Automation State",
            "max": 1024,
            "initial": "{}",
        },
        one_time_room_override_object: {
            "name": "Vacuum One-Time Room Priority",
            "max": 128,
            "initial": "",
        },
        active_weekdays_object: {
            "name": "Vacuum Active Weekdays",
            "max": 64,
            "initial": options["active_weekdays"],
        },
        selected_presence_object: {
            "name": "Vacuum Selected Residents",
            "max": 255,
            "initial": ",".join(options["presence_entities"]),
        },
    }

    input_boolean: Dict[str, Dict[str, Any]] = {
        enabled_object: {
            "name": "Vacuum Automation Active",
            "icon": "mdi:robot-vacuum",
        },
        learning_object: {
            "name": "Adaptive Learning",
            "icon": "mdi:brain",
        },
        home_override_object: {
            "name": "Use Custom Home Point",
            "icon": "mdi:home-edit",
            "initial": options["home_override_enabled"],
        },
        travel_mode_enabled_object: {
            "name": "Travel Logic Enabled",
            "icon": "mdi:airplane",
            "initial": options["travel_mode_enabled"],
        },
        start_notifications_enabled_object: {
            "name": "Push On Auto Start",
            "icon": "mdi:bell-ring-outline",
            "initial": options["start_notifications_enabled"],
        },
        return_summary_enabled_object: {
            "name": "Push Return Summary",
            "icon": "mdi:message-text-outline",
            "initial": options["return_summary_enabled"],
        },
    }

    input_number: Dict[str, Dict[str, Any]] = {
        start_hour_object: {
            "name": "Allowed Start Hour",
            "min": 0,
            "max": 23,
            "step": 1,
            "mode": "box",
            "initial": options["start_hour"],
        },
        end_hour_object: {
            "name": "Allowed End Hour",
            "min": 1,
            "max": 23,
            "step": 1,
            "mode": "box",
            "initial": options["end_hour"],
        },
        return_buffer_object: {
            "name": "Return Safety Buffer",
            "min": 0,
            "max": 30,
            "step": 1,
            "unit_of_measurement": "min",
            "mode": "slider",
            "initial": options["return_buffer_min"],
        },
        fallback_speed_object: {
            "name": "Fallback Speed",
            "min": 5,
            "max": 130,
            "step": 1,
            "unit_of_measurement": "km/h",
            "mode": "slider",
            "initial": options["fallback_speed_kmh"],
        },
        default_travel_time_object: {
            "name": "Default Travel Time",
            "min": 5,
            "max": 240,
            "step": 5,
            "unit_of_measurement": "min",
            "mode": "slider",
            "initial": options["default_travel_time_min"],
        },
        home_latitude_object: {
            "name": "Custom Home Latitude",
            "min": -90,
            "max": 90,
            "step": 0.0001,
            "mode": "box",
            "initial": options["home_latitude"],
        },
        home_longitude_object: {
            "name": "Custom Home Longitude",
            "min": -180,
            "max": 180,
            "step": 0.0001,
            "mode": "box",
            "initial": options["home_longitude"],
        },
        travel_pause_radius_object: {
            "name": "Long-Trip Radius",
            "min": 1,
            "max": 500,
            "step": 1,
            "unit_of_measurement": "km",
            "mode": "box",
            "initial": options["travel_pause_radius_km"],
        },
        max_distance_object: {
            "name": "Maximum Distance Cutoff",
            "min": 0,
            "max": 5000,
            "step": 10,
            "unit_of_measurement": "km",
            "mode": "box",
            "initial": options["max_distance_km"],
        },
    }

    for room in options["rooms"]:
        room_id = room["id"]
        room_name = room["name"]
        input_boolean[f"{helper_prefix}_{room_id}_enabled"] = {
            "name": f"{room_name} Enabled",
            "icon": room["icon"],
            "initial": room["enabled"],
        }
        input_number[f"{helper_prefix}_{room_id}_weight"] = {
            "name": f"{room_name} Priority Weight",
            "min": 0.1,
            "max": 3,
            "step": 0.05,
            "mode": "box",
            "initial": room["weight"],
        }
        input_number[f"{helper_prefix}_{room_id}_interval_h"] = {
            "name": f"{room_name} Cleaning Interval",
            "min": 6,
            "max": 168,
            "step": 1,
            "unit_of_measurement": "h",
            "mode": "box",
            "initial": room["interval_h"],
        }
        input_number[f"{helper_prefix}_{room_id}_duration_min"] = {
            "name": f"{room_name} Planned Duration",
            "min": 1,
            "max": 120,
            "step": 1,
            "unit_of_measurement": "min",
            "mode": "box",
            "initial": room["duration_min"],
        }

    return {
        "input_text": input_text,
        "input_boolean": input_boolean,
        "input_number": input_number,
    }


def dashboard_sections(options: Dict[str, Any]) -> List[Dict[str, Any]]:
    dashboard_prefix = options["dashboard_prefix"]
    helper_prefix = options["helper_prefix"]
    status_sensor = sensor_entity(dashboard_prefix, "status")
    active_room_sensor = sensor_entity(dashboard_prefix, "active_room")
    next_room_sensor = sensor_entity(dashboard_prefix, "next_room")
    travel_time_sensor = sensor_entity(dashboard_prefix, "travel_time")
    return_window_sensor = sensor_entity(dashboard_prefix, "return_window")
    distance_sensor = sensor_entity(dashboard_prefix, "distance_to_home")
    weekly_runs_sensor = sensor_entity(dashboard_prefix, "weekly_runs")
    weekly_minutes_sensor = sensor_entity(dashboard_prefix, "weekly_minutes")
    history_sensor = sensor_entity(dashboard_prefix, "history")
    travel_home_zone_entity = f"zone.{dashboard_prefix}_travel_home_zone"
    max_radius_zone_entity = f"zone.{dashboard_prefix}_max_radius_zone"

    overview_entities: List[Any] = [
        {"entity": options["vacuum_entity"], "name": "Staubsauger"},
        {"entity": status_sensor, "name": "App-Status"},
        {"entity": travel_time_sensor, "name": "Rueckreisezeit"},
        {"entity": return_window_sensor, "name": "Verfuegbares Fenster"},
        {"entity": distance_sensor, "name": "Distanz"},
    ]
    if options.get("waze_entity"):
        overview_entities.append({"entity": options["waze_entity"], "name": "Waze ETA"})

    room_cards = []
    for room in options["rooms"]:
        room_cards.append(
            {
                "type": "entities",
                "title": room_title(room),
                "show_header_toggle": False,
                "entities": [
                    room_helper_entity(helper_prefix, room["id"], "input_boolean", "enabled"),
                    room_helper_entity(helper_prefix, room["id"], "input_number", "weight"),
                    room_helper_entity(helper_prefix, room["id"], "input_number", "interval_h"),
                    room_helper_entity(
                        helper_prefix, room["id"], "input_number", "duration_min"
                    ),
                ],
            }
        )

    return [
        {
            "type": "grid",
            "cards": [
                {
                    "type": "heading",
                    "heading": "Vacuum Automation",
                    "heading_style": "title",
                    "icon": "mdi:robot-vacuum-variant",
                },
                {
                    "type": "tile",
                    "entity": options["enabled_entity"],
                    "name": "Automatik",
                    "color": "green",
                },
                {
                    "type": "tile",
                    "entity": options["learning_enabled_entity"],
                    "name": "Lernlogik",
                    "color": "blue",
                },
                {"type": "tile", "entity": status_sensor, "name": "Status", "color": "teal"},
                {
                    "type": "tile",
                    "entity": active_room_sensor,
                    "name": "Aktiver Raum",
                    "color": "amber",
                },
                {
                    "type": "tile",
                    "entity": next_room_sensor,
                    "name": "Naechster Raum",
                    "color": "indigo",
                },
                {
                    "type": "tile",
                    "entity": travel_time_sensor,
                    "name": "Rueckreisezeit",
                    "color": "purple",
                },
                {
                    "type": "tile",
                    "entity": return_window_sensor,
                    "name": "Reinigungsfenster",
                    "color": "cyan",
                },
                {
                    "type": "tile",
                    "entity": weekly_runs_sensor,
                    "name": "Diese Woche",
                    "color": "pink",
                },
                {
                    "type": "tile",
                    "entity": weekly_minutes_sensor,
                    "name": "Wochenminuten",
                    "color": "red",
                },
            ],
        },
        {
            "type": "grid",
            "cards": [
                {
                    "type": "markdown",
                    "content": (
                        "{% set status = states('"
                        + status_sensor
                        + "') %}\n{% set people = state_attr('"
                        + status_sensor
                        + "', 'presence_summary') or [] %}\n{% set cleaned = state_attr('"
                        + status_sensor
                        + "', 'cleaned_during_absence') or [] %}\n{% set weekly = state_attr('"
                        + history_sensor
                        + "', 'weekly_stats') or [] %}\n## Live\n\n**Status:** {{ status }}\n\n**Bewohner:**{% for person in people %}\n- {{ person.entity_id }}: {{ person.state }}{% endfor %}\n\n**Abwesend seit:** {{ state_attr('"
                        + status_sensor
                        + "', 'away_since') or 'Niemand komplett weg' }}\n\n**Schon gereinigt:** {{ cleaned | join(', ') if cleaned else 'Noch nichts' }}\n\n**Aktuelle Woche:** {% if weekly %}{{ weekly[0].runs }} Laeufe / {{ weekly[0].minutes }} min{% else %}Noch keine Daten{% endif %}"
                    ),
                },
                {
                    "type": "entities",
                    "title": "Live Uebersicht",
                    "show_header_toggle": False,
                    "entities": overview_entities,
                },
                {
                    "type": "map",
                    "title": "Home Radius And Distance",
                    "default_zoom": 6,
                    "hours_to_show": 24,
                    "entities": [
                        options["travel_person_entity"],
                        options["home_zone"],
                        travel_home_zone_entity,
                        max_radius_zone_entity,
                    ],
                },
                {
                    "type": "history-graph",
                    "title": "ETA und Fenster",
                    "hours_to_show": 24,
                    "entities": [
                        {"entity": travel_time_sensor, "name": "Reisezeit"},
                        {"entity": return_window_sensor, "name": "Fenster"},
                    ],
                },
            ],
        },
        {
            "type": "grid",
            "cards": [
                {
                    "type": "markdown",
                    "content": (
                        "{% set queue = state_attr('"
                        + status_sensor
                        + "', 'room_queue') or [] %}\n## Raumplanung\n\n| Raum | Aktiv | Prio | Intervall | Konfig | Gelernt | Effektiv | Passt |\n|---|---|---:|---:|---:|---:|---:|---|\n{% for item in queue %}\n| {{ item.room }} | {{ 'Ja' if item.enabled else 'Nein' }} | {{ item.priority }} | {{ item.interval_h }} h | {{ item.configured_duration_min }} | {{ item.learned_duration_min if item.learned_duration_min is not none else '-' }} | {{ item.effective_duration_min }} | {{ 'Ja' if item.fits_now else 'Nein' }} |\n{% endfor %}"
                    ),
                },
                {
                    "type": "markdown",
                    "content": (
                        "{% set weekly = state_attr('"
                        + history_sensor
                        + "', 'weekly_stats') or [] %}\n## Wochenstatistik\n\n| Woche | Laeufe | Minuten | Raeume |\n|---|---:|---:|---|\n{% for item in weekly %}\n| {{ item.week }} | {{ item.runs }} | {{ item.minutes }} | {{ item.rooms | join(', ') }} |\n{% endfor %}"
                    ),
                },
                {
                    "type": "markdown",
                    "content": (
                        "{% set recent = state_attr('"
                        + history_sensor
                        + "', 'recent_runs') or [] %}\n## Letzte Laeufe\n\n| Ende | Raum | Ergebnis | Minuten |\n|---|---|---|---:|\n{% for item in recent %}\n| {{ item.finished_at or '-' }} | {{ item.room }} | {{ item.outcome }} | {{ item.actual_duration_min or 0 }} |\n{% endfor %}"
                    ),
                },
            ],
        },
        {
            "type": "grid",
            "cards": [
                {
                    "type": "heading",
                    "heading": "Automations-Regler",
                    "heading_style": "subtitle",
                },
                {
                    "type": "markdown",
                    "content": (
                        "## Reise- und Push-Logik\n\n"
                        "**Travel Logic Enabled:** aktiviert die Langzeit-Abwesenheitslogik.\n\n"
                        "**Use Custom Home Point:** nimmt statt `zone.home` die unten gesetzten Koordinaten.\n\n"
                        "**Long-Trip Radius:** wenn du diesen Radius laenger verlaesst, pausiert die Automatik.\n\n"
                        "**Maximum Distance Cutoff:** harter Sicherheitswert, der sofort pausiert.\n\n"
                        "**Push On Auto Start:** Nachricht fuer jeden automatisch gestarteten Raum.\n\n"
                        "**Push Return Summary:** Zusammenfassung bei Rueckkehr."
                    ),
                },
                {
                    "type": "entities",
                    "title": "Globale Einstellungen",
                    "show_header_toggle": False,
                    "entities": [
                        options["enabled_entity"],
                        options["learning_enabled_entity"],
                        options["start_hour_entity"],
                        options["end_hour_entity"],
                        options["return_buffer_entity"],
                        options["fallback_speed_entity"],
                        options["default_travel_time_entity"],
                        options["travel_mode_enabled_entity"],
                        options["start_notifications_enabled_entity"],
                        options["return_summary_enabled_entity"],
                        options["home_override_enabled_entity"],
                        options["home_latitude_entity"],
                        options["home_longitude_entity"],
                        options["travel_pause_radius_entity"],
                        options["max_distance_km_entity"],
                    ],
                },
                {
                    "type": "conditional",
                    "conditions": [{"entity": active_room_sensor, "state_not": "Keine"}],
                    "card": {
                        "type": "markdown",
                        "content": (
                            "## Laufende Reinigung\n\n**Raum:** {{ states('"
                            + active_room_sensor
                            + "') }}\n\n**Restzeit:** {{ state_attr('"
                            + active_room_sensor
                            + "', 'remaining_min') }} min\n\n**Geplant:** {{ state_attr('"
                            + active_room_sensor
                            + "', 'planned_duration_min') }} min\n\n**Rueckkehrfenster:** {{ states('"
                            + return_window_sensor
                            + "') }} min"
                        ),
                    },
                },
            ],
        },
        {"type": "grid", "cards": room_cards},
        {
            "type": "grid",
            "cards": [
                {
                    "type": "heading",
                    "heading": "Debug Sensoren",
                    "heading_style": "subtitle",
                },
                {
                    "type": "entities",
                    "title": "App Sensoren",
                    "show_header_toggle": False,
                    "entities": [
                        status_sensor,
                        active_room_sensor,
                        next_room_sensor,
                        travel_time_sensor,
                        return_window_sensor,
                        distance_sensor,
                        weekly_runs_sensor,
                        weekly_minutes_sensor,
                        history_sensor,
                    ],
                },
            ],
        },
    ]


def build_standard_dashboard(options: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "title": "Vacuum Automation",
        "views": [
            {
                "title": "Vacuum",
                "path": options["dashboard_path"].strip("/").split("/")[-1],
                "icon": "mdi:robot-vacuum-variant",
                "type": "sections",
                "max_columns": 4,
                "sections": dashboard_sections(options),
            }
        ],
    }


def build_mushroom_dashboard(options: Dict[str, Any]) -> Dict[str, Any]:
    dashboard_prefix = options["dashboard_prefix"]
    helper_prefix = options["helper_prefix"]
    status_sensor = sensor_entity(dashboard_prefix, "status")
    active_room_sensor = sensor_entity(dashboard_prefix, "active_room")
    next_room_sensor = sensor_entity(dashboard_prefix, "next_room")
    travel_time_sensor = sensor_entity(dashboard_prefix, "travel_time")
    return_window_sensor = sensor_entity(dashboard_prefix, "return_window")
    distance_sensor = sensor_entity(dashboard_prefix, "distance_to_home")
    weekly_runs_sensor = sensor_entity(dashboard_prefix, "weekly_runs")
    weekly_minutes_sensor = sensor_entity(dashboard_prefix, "weekly_minutes")
    history_sensor = sensor_entity(dashboard_prefix, "history")
    travel_home_zone_entity = f"zone.{dashboard_prefix}_travel_home_zone"
    max_radius_zone_entity = f"zone.{dashboard_prefix}_max_radius_zone"

    room_cards = []
    for room in options["rooms"]:
        room_cards.append(
            {
                "type": "entities",
                "title": room["name"],
                "entities": [
                    room_helper_entity(helper_prefix, room["id"], "input_boolean", "enabled"),
                    room_helper_entity(helper_prefix, room["id"], "input_number", "weight"),
                    room_helper_entity(helper_prefix, room["id"], "input_number", "interval_h"),
                    room_helper_entity(
                        helper_prefix, room["id"], "input_number", "duration_min"
                    ),
                ],
            }
        )

    return {
        "title": "Vacuum Automation Mushroom",
        "views": [
            {
                "title": "Vacuum",
                "path": f"{options['dashboard_path'].strip('/').split('/')[-1]}-mushroom",
                "icon": "mdi:robot-vacuum-variant",
                "badges": [],
                "cards": [
                    {
                        "type": "custom:mushroom-title-card",
                        "title": "Vacuum Automation",
                        "subtitle": "Ankunftsbasierte Reinigung mit Reise-Logik, Pushes und Wochenstatistik",
                    },
                    {
                        "type": "horizontal-stack",
                        "cards": [
                            {
                                "type": "custom:mushroom-entity-card",
                                "entity": options["enabled_entity"],
                                "name": "Automatik",
                                "icon_color": "green",
                                "tap_action": {"action": "toggle"},
                            },
                            {
                                "type": "custom:mushroom-entity-card",
                                "entity": options["learning_enabled_entity"],
                                "name": "Lernlogik",
                                "icon_color": "blue",
                                "tap_action": {"action": "toggle"},
                            },
                            {
                                "type": "custom:mushroom-entity-card",
                                "entity": status_sensor,
                                "name": "Status",
                                "icon_color": "teal",
                            },
                        ],
                    },
                    {
                        "type": "custom:mushroom-chips-card",
                        "chips": [
                            {"type": "entity", "entity": active_room_sensor, "icon_color": "amber"},
                            {"type": "entity", "entity": next_room_sensor, "icon_color": "indigo"},
                            {"type": "entity", "entity": travel_time_sensor, "icon_color": "purple"},
                            {
                                "type": "entity",
                                "entity": return_window_sensor,
                                "icon_color": "cyan",
                            },
                            {"type": "entity", "entity": weekly_runs_sensor, "icon_color": "pink"},
                        ],
                    },
                    {
                        "type": "grid",
                        "columns": 2,
                        "square": False,
                        "cards": [
                            {
                                "type": "custom:mushroom-template-card",
                                "primary": "Live-Zustand",
                                "secondary": (
                                    "Status: {{ states('"
                                    + status_sensor
                                    + "') }} • Raum: {{ states('"
                                    + active_room_sensor
                                    + "') }} • Naechster Raum: {{ states('"
                                    + next_room_sensor
                                    + "') }}"
                                ),
                                "icon": "mdi:robot-vacuum",
                                "icon_color": "teal",
                                "multiline_secondary": True,
                            },
                            {
                                "type": "custom:mushroom-template-card",
                                "primary": "Bewohner",
                                "secondary": (
                                    "{% for person in state_attr('"
                                    + status_sensor
                                    + "', 'presence_summary') or [] %}{{ person.entity_id }}: {{ person.state }}{% if not loop.last %} • {% endif %}{% endfor %}"
                                ),
                                "icon": "mdi:account-group",
                                "icon_color": "blue",
                                "multiline_secondary": True,
                            },
                            {
                                "type": "custom:mushroom-template-card",
                                "primary": "Rueckweg",
                                "secondary": (
                                    "ETA {{ states('"
                                    + travel_time_sensor
                                    + "') }} min • Distanz {{ states('"
                                    + distance_sensor
                                    + "') }} km"
                                ),
                                "icon": "mdi:car-clock",
                                "icon_color": "purple",
                            },
                            {
                                "type": "custom:mushroom-template-card",
                                "primary": "Wochenleistung",
                                "secondary": (
                                    "{{ states('"
                                    + weekly_runs_sensor
                                    + "') }} Laeufe • {{ states('"
                                    + weekly_minutes_sensor
                                    + "') }} min"
                                ),
                                "icon": "mdi:chart-box",
                                "icon_color": "pink",
                            },
                        ],
                    },
                    {
                        "type": "custom:apexcharts-card",
                        "header": {"show": True, "title": "Wochenuebersicht"},
                        "graph_span": "7d",
                        "series": [
                            {"entity": weekly_runs_sensor, "name": "Laeufe"},
                            {"entity": weekly_minutes_sensor, "name": "Minuten"},
                        ],
                    },
                    {
                        "type": "markdown",
                        "content": (
                            "{% set queue = state_attr('"
                            + status_sensor
                            + "', 'room_queue') or [] %}\n## Raumranking\n\n| Raum | Prio | Konfig | Gelernt | Effektiv | Passt |\n|---|---:|---:|---:|---:|---|\n{% for item in queue %}\n| {{ item.room }} | {{ item.priority }} | {{ item.configured_duration_min }} | {{ item.learned_duration_min if item.learned_duration_min is not none else '-' }} | {{ item.effective_duration_min }} | {{ 'Ja' if item.fits_now else 'Nein' }} |\n{% endfor %}"
                        ),
                    },
                    {
                        "type": "markdown",
                        "content": (
                            "{% set weekly = state_attr('"
                            + history_sensor
                            + "', 'weekly_stats') or [] %}\n{% set recent = state_attr('"
                            + history_sensor
                            + "', 'recent_runs') or [] %}\n## Verlauf\n\n**Wochen**\n{% for item in weekly %}\n- {{ item.week }}: {{ item.runs }} Laeufe / {{ item.minutes }} min / {{ item.rooms | join(', ') }}\n{% endfor %}\n\n**Letzte Laeufe**\n{% for item in recent %}\n- {{ item.finished_at or '-' }} · {{ item.room }} · {{ item.outcome }} · {{ item.actual_duration_min or 0 }} min\n{% endfor %}"
                        ),
                    },
                    {
                        "type": "entities",
                        "title": "Globale Einstellungen",
                        "entities": [
                            options["enabled_entity"],
                            options["learning_enabled_entity"],
                            options["start_hour_entity"],
                            options["end_hour_entity"],
                            options["return_buffer_entity"],
                            options["fallback_speed_entity"],
                            options["default_travel_time_entity"],
                            options["travel_mode_enabled_entity"],
                            options["start_notifications_enabled_entity"],
                            options["return_summary_enabled_entity"],
                            options["home_override_enabled_entity"],
                            options["home_latitude_entity"],
                            options["home_longitude_entity"],
                            options["travel_pause_radius_entity"],
                            options["max_distance_km_entity"],
                        ],
                    },
                    {
                        "type": "markdown",
                        "content": (
                            "## Reise- und Push-Logik\n\n"
                            "**Travel Logic Enabled:** aktiviert die Langzeit-Abwesenheitslogik.\n\n"
                            "**Use Custom Home Point:** nimmt statt `zone.home` die unten gesetzten Koordinaten.\n\n"
                            "**Long-Trip Radius:** wenn du diesen Radius laenger verlaesst, pausiert die Automatik.\n\n"
                            "**Maximum Distance Cutoff:** harter Sicherheitswert, der sofort pausiert.\n\n"
                            "**Push On Auto Start:** Nachricht fuer jeden automatisch gestarteten Raum.\n\n"
                            "**Push Return Summary:** Zusammenfassung bei Rueckkehr."
                        ),
                    },
                    {
                        "type": "map",
                        "title": "Home Radius And Distance",
                        "default_zoom": 6,
                        "hours_to_show": 24,
                        "entities": [
                            options["travel_person_entity"],
                            options["home_zone"],
                            travel_home_zone_entity,
                            max_radius_zone_entity,
                        ],
                    },
                    {
                        "type": "grid",
                        "columns": 2,
                        "square": False,
                        "cards": room_cards,
                    },
                ],
            }
        ],
    }


def write_yaml(path: Path, content: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.dump(
            content,
            Dumper=NoAliasDumper,
            sort_keys=False,
            allow_unicode=False,
            width=1000,
        )
    )


def read_yaml_mapping(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}

    try:
        loaded = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        return {}

    if isinstance(loaded, dict):
        return loaded
    return {}


def build_managed_appdaemon_yaml(options: Dict[str, Any]) -> Dict[str, Any]:
    token = normalize_str(os.environ.get("SUPERVISOR_TOKEN"))
    if not token:
        raise RuntimeError(
            "SUPERVISOR_TOKEN is missing. The add-on needs Home Assistant API access."
        )

    return {
        "appdaemon": {
            "time_zone": "Europe/Berlin",
            "latitude": 52.52,
            "longitude": 13.405,
            "elevation": 34,
            "plugins": {
                "HASS": {
                    "type": "hass",
                    "ha_url": "http://supervisor/core",
                    "token": token,
                }
            },
        },
        "http": {"url": options["appdaemon_http_url"].replace("127.0.0.1", "0.0.0.0")},
        "admin": None,
        "api": None,
    }


def write_appdaemon_yaml(options: Dict[str, Any]) -> None:
    managed = build_managed_appdaemon_yaml(options)
    existing = read_yaml_mapping(APPDAEMON_CONFIG_PATH)

    merged = dict(existing)
    appdaemon_config = merged.get("appdaemon")
    if not isinstance(appdaemon_config, dict):
        appdaemon_config = {}
    plugins = appdaemon_config.get("plugins")
    if not isinstance(plugins, dict):
        plugins = {}
    hass_plugin = plugins.get("HASS")
    if not isinstance(hass_plugin, dict):
        hass_plugin = {}

    managed_appdaemon = managed["appdaemon"]
    managed_hass_plugin = managed_appdaemon["plugins"]["HASS"]

    for key in ("time_zone", "latitude", "longitude", "elevation"):
        appdaemon_config.setdefault(key, managed_appdaemon[key])
    hass_plugin.update(managed_hass_plugin)
    plugins["HASS"] = hass_plugin
    appdaemon_config["plugins"] = plugins
    merged["appdaemon"] = appdaemon_config

    http_config = merged.get("http")
    if not isinstance(http_config, dict):
        http_config = {}
    http_config["url"] = managed["http"]["url"]
    merged["http"] = http_config

    merged.setdefault("admin", managed["admin"])
    merged.setdefault("api", managed["api"])

    text = yaml.dump(
        merged,
        Dumper=NoAliasDumper,
        sort_keys=False,
        allow_unicode=False,
        width=1000,
    )
    APPDAEMON_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    APPDAEMON_CONFIG_PATH.write_text(text)


def main() -> None:
    raw_options = read_options()
    options = default_options(raw_options)

    APPDAEMON_APPS_DIR.mkdir(parents=True, exist_ok=True)
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)

    write_appdaemon_yaml(options)
    write_yaml(APP_CONFIG_PATH, build_app_config(options))

    if options["generate_helper_package"]:
        write_yaml(HELPERS_PATH, build_helpers(options))
    summary = {
        "app_config": str(APP_CONFIG_PATH),
        "helpers": str(HELPERS_PATH) if options["generate_helper_package"] else None,
    }
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
