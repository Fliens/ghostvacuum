"""
Arrival-aware vacuum automation for Home Assistant via AppDaemon.

Features:
- arrival-aware one-room-at-a-time planning
- multi-person occupancy handling
- configurable pause zone for long trips
- persistent cleaning history database
- weekly statistics sensors
- learned room duration estimates
- room settings editable via Home Assistant helpers
- dashboard-friendly status sensors
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import appdaemon.plugins.hass.hassapi as hass

EVENT_START_ROOM = "vacuum_automation_start_room"
EVENT_SET_ROOM_DUE = "vacuum_automation_set_room_due"


class VacuumAutomation(hass.Hass):
    """Adaptive vacuum automation that aims to finish before residents get home."""

    def initialize(self):
        """Load configuration, restore state, and register listeners."""
        self.vacuum_entity = self.args.get("vacuum_entity", "vacuum.dreame")
        self.presence_entity = self.args.get("presence_entity", "person.user")
        configured_presence = self.args.get("presence_entities")
        self.presence_entities = (
            list(configured_presence) if configured_presence else [self.presence_entity]
        )

        self.person_entity = self.args.get("person_entity", self.presence_entity)
        self.travel_person_entity = self.args.get(
            "travel_person_entity", self.person_entity
        )
        self.waze_entity = self.args.get("waze_entity")
        self.distance_entity = self.args.get("distance_entity")
        self.home_zone = self.args.get("home_zone", "zone.home")
        self.travel_pause_zone = self.args.get("travel_pause_zone")
        self.home_override_enabled_entity = self.args.get("home_override_enabled_entity")
        self.home_latitude_entity = self.args.get("home_latitude_entity")
        self.home_longitude_entity = self.args.get("home_longitude_entity")
        self.travel_pause_radius_entity = self.args.get("travel_pause_radius_entity")
        self.max_distance_km_entity = self.args.get("max_distance_km_entity")
        self.travel_mode_enabled_entity = self.args.get("travel_mode_enabled_entity")
        self.start_notifications_enabled_entity = self.args.get(
            "start_notifications_enabled_entity"
        )
        self.return_summary_enabled_entity = self.args.get(
            "return_summary_enabled_entity"
        )
        self.notify_service = self.args.get("notify_service", "notify/mobile_app")
        self.dashboard_path = self.args.get("dashboard_path", "")
        self.state_helper = self.args.get(
            "state_helper", "input_text.vacuum_automation_state"
        )
        self.one_time_room_override_entity = self.args.get(
            "one_time_room_override_entity",
            f"input_text.{self.args.get('helper_prefix', 'vacuum_automation')}_one_time_room_override",
        )
        self.enabled_entity = self.args.get("enabled_entity")
        self.learning_enabled_entity = self.args.get(
            "learning_enabled_entity", "input_boolean.vacuum_automation_learning_enabled"
        )
        self.dashboard_prefix = self.args.get(
            "dashboard_prefix", "vacuum_automation"
        )
        self.helper_prefix = self.args.get("helper_prefix", "vacuum_automation")

        self.start_hour = int(self.args.get("start_hour", 8))
        self.end_hour = int(self.args.get("end_hour", 22))
        self.check_interval_min = int(self.args.get("check_interval_min", 30))
        self.monitor_interval_min = int(self.args.get("monitor_interval_min", 5))
        self.return_buffer_min = int(self.args.get("return_buffer_min", 5))
        self.forecast_buffer_min = int(self.args.get("forecast_buffer_min", 0))
        self.default_travel_time_min = float(
            self.args.get("default_travel_time_min", 60)
        )
        self.fallback_speed_kmh = float(self.args.get("fallback_speed_kmh", 30))
        self.travel_pause_after_hours = float(
            self.args.get("travel_pause_after_hours", 24)
        )
        self.home_override_enabled = bool(self.args.get("home_override_enabled", False))
        self.home_latitude = self.args.get("home_latitude")
        self.home_longitude = self.args.get("home_longitude")
        self.travel_pause_radius_km = float(self.args.get("travel_pause_radius_km", 25))
        self.max_distance_km = float(self.args.get("max_distance_km", 0))
        self.travel_mode_enabled = bool(self.args.get("travel_mode_enabled", True))
        self.start_notifications_enabled = bool(
            self.args.get("start_notifications_enabled", False)
        )
        self.return_summary_enabled = bool(
            self.args.get("return_summary_enabled", True)
        )
        self.start_hour_entity = self.args.get("start_hour_entity")
        self.end_hour_entity = self.args.get("end_hour_entity")
        self.active_weekdays = self._normalize_weekdays(
            self.args.get("active_weekdays", ["mon", "tue", "wed", "thu", "fri", "sat"])
        )
        self.active_weekdays_entity = self.args.get("active_weekdays_entity")
        self.return_buffer_entity = self.args.get("return_buffer_entity")
        self.fallback_speed_entity = self.args.get("fallback_speed_entity")
        self.default_travel_time_entity = self.args.get("default_travel_time_entity")

        self.storage_dir = Path(
            self.args.get(
                "storage_dir", "/config/appdaemon/apps/vacuum_automation/storage"
            )
        )
        self.history_file = self.storage_dir / self.args.get(
            "history_file_name", "history.json"
        )
        self.max_history_entries = int(self.args.get("max_history_entries", 1500))
        self.history_weeks = int(self.args.get("history_weeks", 8))
        self.learning_window = int(self.args.get("learning_window", 6))

        self.rooms = self.args.get(
            "rooms",
            {
                "bad": {
                    "segment_id": 1,
                    "interval_h": 24,
                    "weight": 1.40,
                    "duration_min": 15,
                    "name": "Bad",
                    "enabled": True,
                },
                "kueche": {
                    "segment_id": 2,
                    "interval_h": 48,
                    "weight": 1.20,
                    "duration_min": 12,
                    "name": "Kueche",
                    "enabled": True,
                },
                "wohnzimmer": {
                    "segment_id": 3,
                    "interval_h": 72,
                    "weight": 1.00,
                    "duration_min": 20,
                    "name": "Wohnzimmer",
                    "enabled": True,
                },
                "schlafzimmer": {
                    "segment_id": 4,
                    "interval_h": 72,
                    "weight": 1.00,
                    "duration_min": 18,
                    "name": "Schlafzimmer",
                    "enabled": True,
                },
            },
        )

        self.last_cleaned: Dict[str, datetime] = {}
        self.travel_mode_active = False
        self.travel_mode_reason: Optional[str] = None
        self.left_pause_zone_time: Optional[datetime] = None
        self.away_since: Optional[datetime] = None
        self.cleaned_during_absence: List[str] = []
        self.active_room: Optional[str] = None
        self.active_room_started_at: Optional[datetime] = None
        self.active_room_planned_duration_min: Optional[int] = None
        self.active_room_configured_duration_min: Optional[int] = None
        self.active_room_travel_time_min: Optional[int] = None
        self.active_room_distance_km: Optional[float] = None
        self.abort_requested = False
        self.history_entries: List[dict] = []
        self.learned_durations: Dict[str, float] = {}

        self._load_history_storage()
        self._load_state()

        self.run_every(
            self._check_cleaning,
            self.datetime() + timedelta(minutes=1),
            self.check_interval_min * 60,
        )
        self.run_every(
            self._monitor_active_job,
            self.datetime() + timedelta(minutes=1),
            self.monitor_interval_min * 60,
        )

        for entity_id in self.presence_entities:
            self.listen_state(self._on_presence_change, entity_id)
        self.listen_state(
            self._on_person_state_change,
            self.travel_person_entity,
            attribute="all",
        )
        self.listen_state(self._on_vacuum_state_change, self.vacuum_entity)
        self.listen_event(self._on_dashboard_start_room, EVENT_START_ROOM)
        self.listen_event(self._on_dashboard_set_room_due, EVENT_SET_ROOM_DUE)

        for entity_id in self._list_runtime_helper_entities():
            if entity_id:
                self.listen_state(self._on_runtime_setting_change, entity_id)

        for entity_id in self._list_room_helper_entities():
            self.listen_state(self._on_runtime_setting_change, entity_id)

        if self.waze_entity:
            self.listen_state(self._on_travel_signal_change, self.waze_entity)
        if self.distance_entity:
            self.listen_state(self._on_travel_signal_change, self.distance_entity)
        if self.enabled_entity:
            self.listen_state(self._on_enabled_change, self.enabled_entity)
        if self.learning_enabled_entity:
            self.listen_state(self._on_runtime_setting_change, self.learning_enabled_entity)

        self._evaluate_travel_mode()
        self._publish_dashboard_state("initialisiert")
        self.log("Vacuum Automation initialisiert")

    def _load_history_storage(self):
        """Load persistent history and learned durations from disk."""
        try:
            if not self.history_file.exists():
                return
            data = json.loads(self.history_file.read_text())
            self.history_entries = list(data.get("history_entries", []))
            self.learned_durations = {
                room: float(value)
                for room, value in data.get("learned_durations", {}).items()
            }
        except Exception as err:
            self.log(f"Fehler beim Laden der History-Datei: {err}", level="WARNING")
            self.history_entries = []
            self.learned_durations = {}

    def _save_history_storage(self):
        """Persist history database and learned durations to disk."""
        payload = {
            "history_entries": self.history_entries[-self.max_history_entries :],
            "learned_durations": self.learned_durations,
        }

        try:
            self.storage_dir.mkdir(parents=True, exist_ok=True)
            self.history_file.write_text(json.dumps(payload, separators=(",", ":")))
        except Exception as err:
            self.log(f"Fehler beim Speichern der History-Datei: {err}", level="WARNING")

    def _load_state(self):
        """Load compact restart state from a helper."""
        raw_state = self.get_state(self.state_helper)
        if not raw_state or raw_state in ["unknown", "unavailable", ""]:
            return

        try:
            data = json.loads(raw_state)

            if "lc" in data:
                self.last_cleaned = {
                    room: datetime.fromtimestamp(ts)
                    for room, ts in data.get("lc", {}).items()
                }
                self.travel_mode_active = bool(data.get("tm", False))
                self.travel_mode_reason = data.get("tr")
                self.left_pause_zone_time = self._ts_to_datetime(data.get("lp"))
                self.away_since = self._ts_to_datetime(data.get("aw"))
                self.cleaned_during_absence = list(data.get("ca", []))
                self.active_room = data.get("ar")
                self.active_room_started_at = self._ts_to_datetime(data.get("as"))
                self.active_room_planned_duration_min = data.get("ap")
                self.active_room_configured_duration_min = data.get("ac")
                self.active_room_travel_time_min = data.get("at")
                self.active_room_distance_km = data.get("ad")
                self.abort_requested = bool(data.get("ab", False))
                return

            self.last_cleaned = {
                room: datetime.fromisoformat(value)
                for room, value in data.get("last_cleaned", {}).items()
            }
            self.travel_mode_active = bool(data.get("travel_mode_active", False))
            self.travel_mode_reason = data.get("travel_mode_reason")
            if data.get("left_berlin_time"):
                self.left_pause_zone_time = datetime.fromisoformat(
                    data["left_berlin_time"]
                )
            if data.get("away_since"):
                self.away_since = datetime.fromisoformat(data["away_since"])
            self.cleaned_during_absence = list(data.get("cleaned_during_absence", []))
        except Exception as err:
            self.log(f"Fehler beim Laden des Zustands: {err}", level="WARNING")

    def _save_state(self):
        """Persist only the state required across restarts."""
        payload = {
            "lc": {room: int(ts.timestamp()) for room, ts in self.last_cleaned.items()},
            "tm": self.travel_mode_active,
            "tr": self.travel_mode_reason,
            "lp": self._datetime_to_ts(self.left_pause_zone_time),
            "aw": self._datetime_to_ts(self.away_since),
            "ca": self.cleaned_during_absence,
            "ar": self.active_room,
            "as": self._datetime_to_ts(self.active_room_started_at),
            "ap": self.active_room_planned_duration_min,
            "ac": self.active_room_configured_duration_min,
            "at": self.active_room_travel_time_min,
            "ad": self.active_room_distance_km,
            "ab": self.abort_requested,
        }

        try:
            self.call_service(
                "input_text/set_value",
                entity_id=self.state_helper,
                value=json.dumps(payload, separators=(",", ":")),
            )
        except Exception as err:
            self.log(f"Fehler beim Speichern des Zustands: {err}", level="WARNING")

    def _datetime_to_ts(self, value: Optional[datetime]) -> Optional[int]:
        if value is None:
            return None
        return int(value.timestamp())

    def _ts_to_datetime(self, value: Optional[int]) -> Optional[datetime]:
        if value in [None, ""]:
            return None
        return datetime.fromtimestamp(int(value))

    def _normalize_service(self, service: str) -> str:
        """AppDaemon expects domain/service, not domain.service."""
        if "/" in service:
            return service
        if "." in service:
            domain, name = service.split(".", 1)
            return f"{domain}/{name}"
        return service

    def _runtime_numeric(
        self, helper_entity: Optional[str], fallback: float, cast=int
    ):
        value = self._read_numeric_state(helper_entity)
        if value is None:
            return cast(fallback)
        return cast(value)

    def _runtime_bool(self, helper_entity: Optional[str], fallback: bool) -> bool:
        if not helper_entity:
            return fallback
        state = self.get_state(helper_entity)
        if state in [None, "", "unknown", "unavailable"]:
            return fallback
        return str(state).lower() in {"on", "home", "true"}

    def _current_start_hour(self) -> int:
        return self._runtime_numeric(self.start_hour_entity, self.start_hour, int)

    def _current_end_hour(self) -> int:
        return self._runtime_numeric(self.end_hour_entity, self.end_hour, int)

    def _normalize_weekdays(self, value) -> set[str]:
        valid = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
        if isinstance(value, str):
            parts = value.replace(";", ",").split(",")
        elif isinstance(value, (list, tuple, set)):
            parts = value
        else:
            parts = []
        days = {str(part).strip().lower() for part in parts if str(part).strip()}
        return {day for day in days if day in valid}

    def _current_active_weekdays(self) -> set[str]:
        if self.active_weekdays_entity:
            state = self.get_state(self.active_weekdays_entity)
            if state not in [None, "", "unknown", "unavailable"]:
                return self._normalize_weekdays(state)
        return self.active_weekdays

    def _is_active_weekday(self) -> bool:
        weekday = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][self.datetime().weekday()]
        return weekday in self._current_active_weekdays()

    def _current_return_buffer_min(self) -> int:
        return self._runtime_numeric(
            self.return_buffer_entity, self.return_buffer_min, int
        )

    def _current_fallback_speed_kmh(self) -> float:
        return self._runtime_numeric(
            self.fallback_speed_entity, self.fallback_speed_kmh, float
        )

    def _current_default_travel_time_min(self) -> float:
        return self._runtime_numeric(
            self.default_travel_time_entity, self.default_travel_time_min, float
        )

    def _home_override_enabled(self) -> bool:
        return self._runtime_bool(
            self.home_override_enabled_entity, self.home_override_enabled
        )

    def _current_travel_pause_radius_km(self) -> float:
        return self._runtime_numeric(
            self.travel_pause_radius_entity, self.travel_pause_radius_km, float
        )

    def _current_max_distance_km(self) -> float:
        return self._runtime_numeric(
            self.max_distance_km_entity, self.max_distance_km, float
        )

    def _travel_mode_enabled(self) -> bool:
        return self._runtime_bool(
            self.travel_mode_enabled_entity, self.travel_mode_enabled
        )

    def _start_notifications_enabled(self) -> bool:
        return self._runtime_bool(
            self.start_notifications_enabled_entity,
            self.start_notifications_enabled,
        )

    def _return_summary_enabled(self) -> bool:
        return self._runtime_bool(
            self.return_summary_enabled_entity,
            self.return_summary_enabled,
        )

    def _learning_enabled(self) -> bool:
        if not self.learning_enabled_entity:
            return True
        state = str(self.get_state(self.learning_enabled_entity) or "on").lower()
        return state in {"on", "home", "true"}

    def _room_helper_entity(self, room: str, domain: str, suffix: str) -> str:
        return f"{domain}.{self.helper_prefix}_{room}_{suffix}"

    def _list_room_helper_entities(self) -> List[str]:
        entities: List[str] = []
        for room in self.rooms:
            entities.extend(
                [
                    self._room_helper_entity(room, "input_boolean", "enabled"),
                    self._room_helper_entity(room, "input_number", "weight"),
                    self._room_helper_entity(room, "input_number", "interval_h"),
                    self._room_helper_entity(room, "input_number", "duration_min"),
                ]
            )
        return entities

    def _list_runtime_helper_entities(self) -> List[str]:
        return [
            entity_id
            for entity_id in [
                self.start_hour_entity,
                self.end_hour_entity,
                self.active_weekdays_entity,
                self.return_buffer_entity,
                self.fallback_speed_entity,
                self.default_travel_time_entity,
                self.home_override_enabled_entity,
                self.home_latitude_entity,
                self.home_longitude_entity,
                self.travel_pause_radius_entity,
                self.max_distance_km_entity,
                self.travel_mode_enabled_entity,
                self.start_notifications_enabled_entity,
                self.return_summary_enabled_entity,
            ]
            if entity_id
        ]

    def _presence_state(self, entity_id: str) -> str:
        return str(self.get_state(entity_id) or "unknown")

    def _is_home_like_state(self, state: str) -> bool:
        return state.lower() in {"home", "on"}

    def _is_away_like_state(self, state: str) -> bool:
        return state.lower() in {"not_home", "away", "off"}

    def _is_anyone_home(self) -> bool:
        return any(
            self._is_home_like_state(self._presence_state(entity_id))
            for entity_id in self.presence_entities
        )

    def _is_everyone_away(self) -> bool:
        return all(
            self._is_away_like_state(self._presence_state(entity_id))
            for entity_id in self.presence_entities
        )

    def _presence_summary(self) -> List[dict]:
        return [
            self._person_presence_summary(entity_id)
            for entity_id in self.presence_entities
        ]

    def _person_presence_summary(self, entity_id: str) -> dict:
        state = self._presence_state(entity_id)
        distance_km = self._person_distance_to_home(entity_id, state)
        travel_time_min = None
        if distance_km is not None:
            travel_time_min = 0 if distance_km <= 0 else max(
                1,
                round((distance_km / self._current_fallback_speed_kmh()) * 60),
            )

        return {
            "entity_id": entity_id,
            "state": state,
            "distance_km": round(distance_km, 1) if distance_km is not None else None,
            "travel_time_min": travel_time_min,
        }

    def _person_distance_to_home(self, entity_id: str, state: str) -> Optional[float]:
        if self._is_home_like_state(state):
            return 0.0

        person_state = self.get_state(entity_id, attribute="all")
        home_state = self.get_state(self.home_zone, attribute="all")
        if not isinstance(person_state, dict) or not isinstance(home_state, dict):
            return None

        person_attrs = person_state.get("attributes", {})
        home_attrs = home_state.get("attributes", {})
        try:
            return self._haversine_km(
                float(person_attrs["latitude"]),
                float(person_attrs["longitude"]),
                float(home_attrs["latitude"]),
                float(home_attrs["longitude"]),
            )
        except Exception:
            return None

    def _read_numeric_state(self, entity_id: Optional[str]) -> Optional[float]:
        if not entity_id:
            return None
        value = self.get_state(entity_id)
        if value in [None, "", "unknown", "unavailable"]:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _room_enabled(self, room: str) -> bool:
        helper_entity = self._room_helper_entity(room, "input_boolean", "enabled")
        state = self.get_state(helper_entity)
        if state not in [None, "unknown", "unavailable"]:
            return str(state).lower() in {"on", "home", "true"}
        return bool(self.rooms[room].get("enabled", True))

    def _room_weight(self, room: str) -> float:
        helper_entity = self._room_helper_entity(room, "input_number", "weight")
        value = self._read_numeric_state(helper_entity)
        if value is not None:
            return float(value)
        return float(self.rooms[room]["weight"])

    def _room_interval_h(self, room: str) -> float:
        helper_entity = self._room_helper_entity(room, "input_number", "interval_h")
        value = self._read_numeric_state(helper_entity)
        if value is not None:
            return float(value)
        return float(self.rooms[room]["interval_h"])

    def _room_configured_duration_min(self, room: str) -> int:
        helper_entity = self._room_helper_entity(room, "input_number", "duration_min")
        value = self._read_numeric_state(helper_entity)
        if value is not None:
            return max(1, int(value))
        return int(self.rooms[room]["duration_min"])

    def _room_effective_duration_min(self, room: str) -> int:
        if self._learning_enabled() and room in self.learned_durations:
            return max(1, int(round(self.learned_durations[room])))
        return self._room_configured_duration_min(room)

    def _room_label(self, room: Optional[str]) -> str:
        if not room:
            return "Keine"
        config = self.rooms.get(room, {})
        return str(config.get("name") or room.replace("_", " ").title())

    def _haversine_km(
        self,
        lat1: float,
        lon1: float,
        lat2: float,
        lon2: float,
    ) -> float:
        radius_km = 6371.0
        d_lat = math.radians(lat2 - lat1)
        d_lon = math.radians(lon2 - lon1)
        a = (
            math.sin(d_lat / 2) ** 2
            + math.cos(math.radians(lat1))
            * math.cos(math.radians(lat2))
            * math.sin(d_lon / 2) ** 2
        )
        return radius_km * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    def _is_in_zone(self, person_state: dict, zone_entity: str) -> bool:
        state_name = str(person_state.get("state", "")).lower()
        zone_name = zone_entity.split(".", 1)[-1].replace("_", " ").lower()
        if state_name == zone_name:
            return True

        attrs = person_state.get("attributes", {})
        zone_state = self.get_state(zone_entity, attribute="all")
        if not isinstance(zone_state, dict):
            return False

        zone_attrs = zone_state.get("attributes", {})

        try:
            distance_km = self._haversine_km(
                float(attrs["latitude"]),
                float(attrs["longitude"]),
                float(zone_attrs["latitude"]),
                float(zone_attrs["longitude"]),
            )
            radius_km = float(zone_attrs.get("radius", 0)) / 1000
            return distance_km <= radius_km
        except Exception:
            return False

    def _is_in_coordinate_zone(
        self,
        person_state: dict,
        latitude: float,
        longitude: float,
        radius_km: float,
    ) -> bool:
        attrs = person_state.get("attributes", {})
        try:
            distance_km = self._haversine_km(
                float(attrs["latitude"]),
                float(attrs["longitude"]),
                float(latitude),
                float(longitude),
            )
            return distance_km <= float(radius_km)
        except Exception:
            return False

    def _custom_home_zone_config(self) -> Optional[dict]:
        if not self._home_override_enabled():
            return None

        latitude = self._read_numeric_state(self.home_latitude_entity)
        longitude = self._read_numeric_state(self.home_longitude_entity)
        radius_km = self._read_numeric_state(self.travel_pause_radius_entity)

        if latitude is None and self.home_latitude not in [None, ""]:
            latitude = float(self.home_latitude)
        if longitude is None and self.home_longitude not in [None, ""]:
            longitude = float(self.home_longitude)
        if radius_km is None:
            radius_km = float(self.travel_pause_radius_km)

        if latitude is None or longitude is None or radius_km is None or radius_km <= 0:
            return None

        return {
            "latitude": float(latitude),
            "longitude": float(longitude),
            "radius_km": float(radius_km),
        }

    def _person_distance_to_coordinate_zone(self, person_state: dict, zone: dict) -> Optional[float]:
        attrs = person_state.get("attributes", {})
        try:
            return self._haversine_km(
                float(attrs["latitude"]),
                float(attrs["longitude"]),
                float(zone["latitude"]),
                float(zone["longitude"]),
            )
        except Exception:
            return None

    def _create_virtual_zone(
        self,
        entity_id: str,
        friendly_name: str,
        latitude: float,
        longitude: float,
        radius_km: float,
        icon: str,
    ):
        self.set_state(
            entity_id,
            state="zoning",
            attributes={
                "friendly_name": friendly_name,
                "icon": icon,
                "latitude": latitude,
                "longitude": longitude,
                "radius": int(radius_km * 1000),
                "passive": True,
            },
        )

    @staticmethod
    def _ha_state(value):
        if value is None:
            return "unknown"
        if isinstance(value, bool):
            return "on" if value else "off"
        if isinstance(value, float):
            if value.is_integer():
                return str(int(value))
            return f"{value:.1f}"
        return str(value)

    def _publish_virtual_zones(self):
        travel_home_zone = self._custom_home_zone_config()
        if travel_home_zone:
            self._create_virtual_zone(
                f"zone.{self.dashboard_prefix}_travel_home_zone",
                "Travel Home Zone",
                travel_home_zone["latitude"],
                travel_home_zone["longitude"],
                travel_home_zone["radius_km"],
                "mdi:home-map-marker",
            )

        max_distance_km = self._current_max_distance_km()
        home_state = self.get_state(self.home_zone, attribute="all")
        home_latitude = None
        home_longitude = None
        if travel_home_zone:
            home_latitude = travel_home_zone["latitude"]
            home_longitude = travel_home_zone["longitude"]
        elif (
            isinstance(home_state, dict)
            and isinstance(home_state.get("attributes"), dict)
        ):
            attrs = home_state["attributes"]
            try:
                home_latitude = float(attrs["latitude"])
                home_longitude = float(attrs["longitude"])
            except Exception:
                home_latitude = None
                home_longitude = None
        if (
            max_distance_km > 0
            and home_latitude is not None
            and home_longitude is not None
        ):
            self._create_virtual_zone(
                f"zone.{self.dashboard_prefix}_max_radius_zone",
                "Maximum Radius",
                home_latitude,
                home_longitude,
                max_distance_km,
                "mdi:map-marker-radius",
            )

    def _pause_zone_reason(self, person_state: dict) -> Optional[str]:
        custom_home_zone = self._custom_home_zone_config()
        if custom_home_zone:
            in_pause_zone = self._is_in_coordinate_zone(
                person_state,
                custom_home_zone["latitude"],
                custom_home_zone["longitude"],
                custom_home_zone["radius_km"],
            )
        elif self.travel_pause_zone:
            in_pause_zone = self._is_in_zone(person_state, self.travel_pause_zone)
        else:
            self.left_pause_zone_time = None
            return None

        if not in_pause_zone and self.left_pause_zone_time is None:
            self.left_pause_zone_time = self.datetime()
            self.log("Pausen-Zone verlassen - Reisemodus-Timer gestartet")
            self._save_state()
        elif in_pause_zone and self.left_pause_zone_time is not None:
            self.left_pause_zone_time = None
            self._save_state()

        if self.left_pause_zone_time:
            hours_away = (
                self.datetime() - self.left_pause_zone_time
            ).total_seconds() / 3600
            if hours_away >= self.travel_pause_after_hours:
                return "pause_radius"

        return None

    def _evaluate_travel_mode(self, person_state: Optional[dict] = None):
        if person_state is None:
            person_state = self.get_state(self.travel_person_entity, attribute="all")
        if not isinstance(person_state, dict):
            return

        if not self._travel_mode_enabled():
            state_changed = self.travel_mode_active
            reason_changed = self.travel_mode_reason is not None
            self.travel_mode_active = False
            self.travel_mode_reason = None
            self.left_pause_zone_time = None
            if state_changed or reason_changed:
                self.log("Reisemodus-Logik deaktiviert")
                self._save_state()
            return

        reason = None
        max_distance_km = self._current_max_distance_km()
        distance_km = self._get_distance_km()
        if (
            distance_km is not None
            and max_distance_km > 0
            and distance_km >= max_distance_km
        ):
            reason = "max_distance"
        else:
            reason = self._pause_zone_reason(person_state)

        state_changed = self.travel_mode_active != bool(reason)
        reason_changed = self.travel_mode_reason != reason
        self.travel_mode_active = bool(reason)
        self.travel_mode_reason = reason

        if state_changed or reason_changed:
            if reason == "max_distance":
                self.log("Reisemodus aktiviert - Maximalradius von zuhause ueberschritten")
            elif reason == "pause_radius":
                self.log("Langzeit-Abwesenheit erkannt - Reisemodus aktiviert")
            elif not self.travel_mode_active:
                self.log("Reisemodus deaktiviert")
            self._save_state()

    def _get_distance_km(self) -> Optional[float]:
        distance = self._read_numeric_state(self.distance_entity)
        if distance is not None:
            attrs = self.get_state(self.distance_entity, attribute="all") or {}
            unit = str((attrs.get("attributes") or {}).get("unit_of_measurement", "")).lower()
            if unit in ["m", "meter", "meters"]:
                return distance / 1000
            return distance

        person_state = self.get_state(self.travel_person_entity, attribute="all")
        home_state = self.get_state(self.home_zone, attribute="all")
        if not isinstance(person_state, dict) or not isinstance(home_state, dict):
            return None

        person_attrs = person_state.get("attributes", {})
        home_attrs = home_state.get("attributes", {})

        try:
            return self._haversine_km(
                float(person_attrs["latitude"]),
                float(person_attrs["longitude"]),
                float(home_attrs["latitude"]),
                float(home_attrs["longitude"]),
            )
        except Exception:
            return None

    def _get_travel_time_minutes(self) -> float:
        value = self._read_numeric_state(self.waze_entity)
        if value is not None and value > 0:
            return value

        distance_km = self._get_distance_km()
        if distance_km is not None and distance_km >= 0:
            return max(1.0, (distance_km / self._current_fallback_speed_kmh()) * 60)

        return self._current_default_travel_time_min()

    def _calculate_available_time_minutes(self, log_context: bool) -> int:
        now = self.datetime()
        end_time = now.replace(
            hour=self._current_end_hour(),
            minute=0,
            second=0,
            microsecond=0,
        )
        time_until_end = max(0.0, (end_time - now).total_seconds() / 60)
        travel_minutes = self._get_travel_time_minutes()
        available = max(0.0, min(time_until_end, travel_minutes))

        if log_context:
            distance = self._get_distance_km()
            distance_text = f"{distance:.1f}km" if distance is not None else "unbekannt"
            self.log(
                "Rueckkehrfenster: "
                f"{available:.0f}min (Reisezeit {travel_minutes:.0f}min, "
                f"Distanz {distance_text}, Tagesfenster {time_until_end:.0f}min)"
            )

        return int(available)

    def _calculate_score(self, room: str) -> Tuple[float, float, float]:
        interval_h = self._room_interval_h(room)
        last_clean = self.last_cleaned.get(room)
        if last_clean:
            hours_since = (self.datetime() - last_clean).total_seconds() / 3600
        else:
            hours_since = interval_h * 2

        score = hours_since / interval_h
        forecast_buffer_h = (self._get_travel_time_minutes() + self.forecast_buffer_min) / 60
        forecast_buffer_h = min(max(forecast_buffer_h * 0.25, 0.5), 2.0)
        score_forecast = (hours_since + forecast_buffer_h) / interval_h
        score_forecast_capped = min(score_forecast, 2.0)
        return score, score_forecast, score_forecast_capped

    def _select_next_room(
        self, available_time: int, log_selection: bool = False
    ) -> Optional[str]:
        candidates = []

        for room in self.rooms:
            if not self._room_enabled(room):
                continue

            duration = self._room_effective_duration_min(room)
            if duration + self._current_return_buffer_min() > available_time:
                continue

            score, score_forecast, score_capped = self._calculate_score(room)
            if score < 0.8 and score_forecast < 1.0:
                continue

            overdue = score_forecast >= 1.0
            priority = score_capped + (self._room_weight(room) - 1.0) * 0.2
            candidates.append(
                {
                    "room": room,
                    "duration": duration,
                    "overdue": overdue,
                    "priority": priority,
                    "score": score,
                    "score_forecast": score_forecast,
                }
            )

        override_room = self._one_time_room_override()
        if override_room:
            for candidate in candidates:
                if candidate["room"] == override_room:
                    if log_selection:
                        self.log(
                            "Einmaliger Raum-Vorzug: "
                            f"{self._room_label(override_room)} "
                            f"(dauer={candidate['duration']}min)"
                        )
                    return str(override_room)

            if log_selection:
                self.log(
                    "Einmaliger Raum-Vorzug nicht nutzbar: "
                    f"{override_room} passt nicht ins aktuelle Fenster",
                    level="INFO",
                )
            self._clear_one_time_room_override()

        candidates.sort(
            key=lambda item: (
                not item["overdue"],
                -item["priority"],
                item["duration"],
            )
        )

        if not candidates:
            return None

        winner = candidates[0]
        if log_selection:
            self.log(
                f"Ausgewaehlter Raum: {self._room_label(winner['room'])} "
                f"(score={winner['score']:.2f}, "
                f"forecast={winner['score_forecast']:.2f}, "
                f"dauer={winner['duration']}min)"
            )
        return str(winner["room"])

    def _can_start_cleaning(self) -> bool:
        if self.enabled_entity and self.get_state(self.enabled_entity) != "on":
            return False

        now = self.datetime()
        if not self._is_active_weekday():
            return False
        if not (self._current_start_hour() <= now.hour < self._current_end_hour()):
            return False
        if not self._is_everyone_away():
            return False
        if self.travel_mode_active:
            return False
        if self.active_room:
            return False

        vacuum_state = self.get_state(self.vacuum_entity)
        return vacuum_state in ["idle", "docked"]

    def _one_time_room_override(self) -> Optional[str]:
        if not self.one_time_room_override_entity:
            return None

        value = str(self.get_state(self.one_time_room_override_entity) or "").strip()
        if not value or value.lower() in {"unknown", "unavailable", "none"}:
            return None
        return value if value in self.rooms else None

    def _clear_one_time_room_override(self):
        if not self.one_time_room_override_entity:
            return

        try:
            self.call_service(
                "input_text/set_value",
                entity_id=self.one_time_room_override_entity,
                value="",
            )
        except Exception as err:
            self.log(
                f"Einmaligen Raum-Vorzug konnte nicht geloescht werden: {err}",
                level="WARNING",
            )

    def _mark_room_due_state(self, room: str, due: bool):
        interval_h = max(1, int(round(self._room_interval_h(room))))
        self.last_cleaned[room] = (
            self.datetime() - timedelta(hours=interval_h) if due else self.datetime()
        )
        self._save_state()
        self._publish_dashboard_state("raum_override")

    def _on_dashboard_set_room_due(self, event_name, data, kwargs):
        room = str((data or {}).get("room_key") or "").strip()
        due = bool((data or {}).get("due"))
        if room not in self.rooms:
            return

        self._mark_room_due_state(room, due)

    def _on_dashboard_start_room(self, event_name, data, kwargs):
        room = str((data or {}).get("room_key") or "").strip()
        if room not in self.rooms or not self._can_start_cleaning():
            return

        available_time = self._calculate_available_time_minutes(log_context=False)
        duration = self._room_effective_duration_min(room)
        if duration + self._current_return_buffer_min() > available_time:
            self.log(
                f"Manueller Start abgelehnt fuer {self._room_label(room)}: "
                f"verfuegbar={available_time}min, dauer={duration}min",
                level="INFO",
            )
            return

        self._start_cleaning(room)

    def _start_cleaning(self, room: str):
        segment_id = self.rooms[room]["segment_id"]
        self.active_room = room
        self.active_room_started_at = self.datetime()
        self.active_room_planned_duration_min = self._room_effective_duration_min(room)
        self.active_room_configured_duration_min = self._room_configured_duration_min(room)
        self.active_room_travel_time_min = int(round(self._get_travel_time_minutes()))
        self.active_room_distance_km = self._get_distance_km()
        self.abort_requested = False
        self._clear_one_time_room_override()

        self.call_service(
            "dreame_vacuum/vacuum_clean_segment",
            entity_id=self.vacuum_entity,
            segments=[segment_id],
        )

        self._save_state()
        self._publish_dashboard_state("gestartet")
        self.log(
            f"Starte Reinigung fuer {self._room_label(room)} "
            f"(segment={segment_id}, geplant={self.active_room_planned_duration_min}min)"
        )
        self._send_start_notification(room)

    def _estimate_remaining_room_minutes(self, room: Optional[str]) -> int:
        if not room:
            return 0

        duration = (
            self.active_room_planned_duration_min
            if room == self.active_room and self.active_room_planned_duration_min
            else self._room_effective_duration_min(room)
        )

        if not self.active_room_started_at or room != self.active_room:
            return duration

        elapsed = max(
            0,
            int((self.datetime() - self.active_room_started_at).total_seconds() / 60),
        )
        return max(1, duration - elapsed)

    def _record_history_entry(self, room: str, outcome: str):
        actual_duration = 0
        if self.active_room_started_at:
            actual_duration = max(
                1,
                int((self.datetime() - self.active_room_started_at).total_seconds() / 60),
            )

        entry = {
            "room": room,
            "room_name": self._room_label(room),
            "started_at": self.active_room_started_at.isoformat()
            if self.active_room_started_at
            else None,
            "finished_at": self.datetime().isoformat(),
            "outcome": outcome,
            "actual_duration_min": actual_duration,
            "planned_duration_min": self.active_room_planned_duration_min,
            "configured_duration_min": self.active_room_configured_duration_min,
            "travel_time_min_at_start": self.active_room_travel_time_min,
            "distance_km_at_start": self.active_room_distance_km,
        }

        self.history_entries.append(entry)
        self.history_entries = self.history_entries[-self.max_history_entries :]

        if outcome == "completed":
            self._update_learning(room)

        self._save_history_storage()

    def _update_learning(self, room: str):
        successful = [
            entry["actual_duration_min"]
            for entry in reversed(self.history_entries)
            if entry.get("room") == room and entry.get("outcome") == "completed"
        ][: self.learning_window]

        if successful:
            self.learned_durations[room] = sum(successful) / len(successful)

    def _abort_cleaning(self, reason: str):
        if not self.active_room or self.abort_requested:
            return

        self.abort_requested = True
        self.log(f"Breche Auto-Reinigung ab: {reason}")
        self.call_service("vacuum/return_to_base", entity_id=self.vacuum_entity)
        self._save_state()
        self._publish_dashboard_state("abbruch")

    def _send_return_notification(self):
        if (
            not self.cleaned_during_absence
            or not self.notify_service
            or not self._return_summary_enabled()
        ):
            return

        cleaned = []
        total_minutes = 0
        for room in self.cleaned_during_absence:
            label = self._room_label(room)
            if label not in cleaned:
                cleaned.append(label)

        for entry in self.history_entries:
            if (
                self.away_since
                and entry.get("outcome") == "completed"
                and entry.get("finished_at")
                and datetime.fromisoformat(entry["finished_at"]) >= self.away_since
            ):
                total_minutes += int(entry.get("actual_duration_min", 0))

        self.call_service(
            self._normalize_service(self.notify_service),
            title="Saugroboter-Bericht",
            message=(
                "Waehrend der Abwesenheit gereinigt: "
                + ", ".join(cleaned)
                + f" ({total_minutes} min)"
            ),
            data=(
                {"clickAction": self.dashboard_path, "url": self.dashboard_path}
                if self.dashboard_path
                else {}
            ),
        )

    def _send_start_notification(self, room: str):
        if (
            not self.notify_service
            or not self._start_notifications_enabled()
            or not self._is_everyone_away()
        ):
            return

        available_time = self._calculate_available_time_minutes(log_context=False)
        self.call_service(
            self._normalize_service(self.notify_service),
            title="Saugroboter startet",
            message=(
                f"Starte {self._room_label(room)} "
                f"({self.active_room_planned_duration_min or self._room_effective_duration_min(room)} min, "
                f"Fenster {available_time} min)"
            ),
            data=(
                {"clickAction": self.dashboard_path, "url": self.dashboard_path}
                if self.dashboard_path
                else {}
            ),
        )

    def _recent_runs(self, limit: int = 10) -> List[dict]:
        recent = []
        for entry in self.history_entries[-limit:]:
            recent.append(
                {
                    "room": entry.get("room_name") or self._room_label(entry.get("room")),
                    "outcome": entry.get("outcome"),
                    "actual_duration_min": entry.get("actual_duration_min"),
                    "finished_at": entry.get("finished_at"),
                }
            )
        return list(reversed(recent))

    def _weekly_stats(self) -> List[dict]:
        buckets: Dict[str, dict] = defaultdict(
            lambda: {"runs": 0, "minutes": 0, "rooms": set()}
        )

        for entry in self.history_entries:
            if entry.get("outcome") != "completed":
                continue
            finished_at = entry.get("finished_at")
            if not finished_at:
                continue
            try:
                dt = datetime.fromisoformat(finished_at)
            except ValueError:
                continue
            iso_year, iso_week, _ = dt.isocalendar()
            key = f"{iso_year}-W{iso_week:02d}"
            buckets[key]["runs"] += 1
            buckets[key]["minutes"] += int(entry.get("actual_duration_min", 0))
            buckets[key]["rooms"].add(entry.get("room_name") or self._room_label(entry.get("room")))

        stats = []
        for key in sorted(buckets.keys(), reverse=True)[: self.history_weeks]:
            bucket = buckets[key]
            stats.append(
                {
                    "week": key,
                    "runs": bucket["runs"],
                    "minutes": bucket["minutes"],
                    "rooms": sorted(bucket["rooms"]),
                }
            )
        return stats

    def _room_stats(self) -> List[dict]:
        stats = []
        for room in self.rooms:
            completed = [
                entry
                for entry in self.history_entries
                if entry.get("room") == room and entry.get("outcome") == "completed"
            ]
            avg_duration = None
            if completed:
                avg_duration = round(
                    sum(int(entry.get("actual_duration_min", 0)) for entry in completed)
                    / len(completed),
                    1,
                )

            stats.append(
                {
                    "room": self._room_label(room),
                    "room_key": room,
                    "enabled": self._room_enabled(room),
                    "segment_id": self.rooms[room]["segment_id"],
                    "weight": round(self._room_weight(room), 2),
                    "interval_h": round(self._room_interval_h(room), 1),
                    "configured_duration_min": self._room_configured_duration_min(room),
                    "learned_duration_min": round(self.learned_durations.get(room), 1)
                    if room in self.learned_durations
                    else None,
                    "effective_duration_min": self._room_effective_duration_min(room),
                    "completed_runs": len(completed),
                    "average_actual_duration_min": avg_duration,
                    "last_cleaned": self.last_cleaned.get(room).isoformat()
                    if self.last_cleaned.get(room)
                    else None,
                }
            )
        return stats

    def _build_room_queue(self, available_time: int) -> List[dict]:
        queue: List[dict] = []
        for room in self.rooms:
            score, score_forecast, score_capped = self._calculate_score(room)
            priority = score_capped + (self._room_weight(room) - 1.0) * 0.2
            queue.append(
                {
                    "room": self._room_label(room),
                    "room_key": room,
                    "segment_id": self.rooms[room]["segment_id"],
                    "enabled": self._room_enabled(room),
                    "weight": round(self._room_weight(room), 2),
                    "interval_h": round(self._room_interval_h(room), 1),
                    "configured_duration_min": self._room_configured_duration_min(room),
                    "learned_duration_min": round(self.learned_durations.get(room), 1)
                    if room in self.learned_durations
                    else None,
                    "effective_duration_min": self._room_effective_duration_min(room),
                    "fits_now": self._room_effective_duration_min(room)
                    + self._current_return_buffer_min()
                    <= available_time,
                    "score": round(score, 2),
                    "forecast_score": round(score_forecast, 2),
                    "priority": round(priority, 2),
                }
            )

        queue.sort(
            key=lambda item: (
                not (item["forecast_score"] >= 1.0),
                -item["priority"],
                item["effective_duration_min"],
            )
        )
        return queue

    def _derive_status(self) -> str:
        if self.enabled_entity and self.get_state(self.enabled_entity) != "on":
            return "Pausiert"
        if self.travel_mode_active:
            return "Reisemodus"
        if self._is_anyone_home():
            return "Zuhause"
        if self.active_room and self.abort_requested:
            return "Abbruch laeuft"
        if self.active_room:
            return "Reinigt"
        if self._is_everyone_away():
            return "Bereit"
        return "Warten"

    def _publish_dashboard_state(self, reason: str):
        status = self._derive_status()
        available_time = self._calculate_available_time_minutes(log_context=False)
        travel_time = round(self._get_travel_time_minutes())
        distance_km = self._get_distance_km()
        next_room = (
            self._select_next_room(available_time, log_selection=False)
            if not self.active_room
            else None
        )
        self._publish_virtual_zones()
        custom_home_zone = self._custom_home_zone_config()
        person_state = self.get_state(self.travel_person_entity, attribute="all")
        custom_home_zone_distance_km = (
            self._person_distance_to_coordinate_zone(person_state, custom_home_zone)
            if custom_home_zone and isinstance(person_state, dict)
            else None
        )
        room_queue = self._build_room_queue(available_time)
        weekly_stats = self._weekly_stats()
        room_stats = self._room_stats()
        recent_runs = self._recent_runs()
        weekly_runs = weekly_stats[0]["runs"] if weekly_stats else 0
        weekly_minutes = weekly_stats[0]["minutes"] if weekly_stats else 0

        common_attributes = {
            "friendly_name": "Vacuum Automation",
            "icon": "mdi:robot-vacuum-variant",
            "reason": reason,
            "presence_entities": self.presence_entities,
            "presence_summary": self._presence_summary(),
            "vacuum_entity": self.vacuum_entity,
            "travel_person_entity": self.travel_person_entity,
            "travel_pause_zone": self.travel_pause_zone,
            "travel_mode_active": self.travel_mode_active,
            "travel_mode_reason": self.travel_mode_reason,
            "travel_mode_enabled": self._travel_mode_enabled(),
            "enabled": self.get_state(self.enabled_entity) if self.enabled_entity else "on",
            "learning_enabled": self._learning_enabled(),
            "start_notifications_enabled": self._start_notifications_enabled(),
            "return_summary_enabled": self._return_summary_enabled(),
            "away_since": self.away_since.isoformat() if self.away_since else None,
            "active_room": self._room_label(self.active_room) if self.active_room else None,
            "next_room": self._room_label(next_room) if next_room else None,
            "cleaned_during_absence": [
                self._room_label(room) for room in self.cleaned_during_absence
            ],
            "available_time_min": available_time,
            "travel_time_min": int(travel_time),
            "distance_km": round(distance_km, 1) if distance_km is not None else None,
            "max_distance_km": round(self._current_max_distance_km(), 1),
            "travel_home_zone": custom_home_zone,
            "travel_home_zone_distance_km": round(custom_home_zone_distance_km, 1)
            if custom_home_zone_distance_km is not None
            else None,
            "travel_pause_radius_km": round(self._current_travel_pause_radius_km(), 1),
            "return_buffer_min": self._current_return_buffer_min(),
            "time_window": {
                "start_hour": self._current_start_hour(),
                "end_hour": self._current_end_hour(),
            },
            "room_queue": room_queue,
            "room_stats": room_stats,
            "recent_runs": recent_runs,
            "weekly_stats": weekly_stats,
            "history_entries": len(self.history_entries),
        }

        self.set_state(
            f"sensor.{self.dashboard_prefix}_status",
            state=self._ha_state(status),
            attributes=common_attributes,
        )
        self.set_state(
            f"sensor.{self.dashboard_prefix}_travel_time",
            state=self._ha_state(int(travel_time)),
            attributes={
                "friendly_name": "Rueckreisezeit",
                "unit_of_measurement": "min",
                "icon": "mdi:car-clock",
            },
        )
        self.set_state(
            f"sensor.{self.dashboard_prefix}_return_window",
            state=self._ha_state(int(available_time)),
            attributes={
                "friendly_name": "Verfuegbares Reinigungsfenster",
                "unit_of_measurement": "min",
                "icon": "mdi:timeline-clock",
            },
        )
        self.set_state(
            f"sensor.{self.dashboard_prefix}_distance_to_home",
            state=self._ha_state(round(distance_km, 1) if distance_km is not None else None),
            attributes={
                "friendly_name": "Distanz nach Hause",
                "unit_of_measurement": "km",
                "icon": "mdi:map-marker-distance",
            },
        )
        self.set_state(
            f"sensor.{self.dashboard_prefix}_active_room",
            state=self._ha_state(
                self._room_label(self.active_room) if self.active_room else "Keine"
            ),
            attributes={
                "friendly_name": "Aktiver Raum",
                "icon": "mdi:floor-plan",
                "remaining_min": self._estimate_remaining_room_minutes(self.active_room),
                "planned_duration_min": self.active_room_planned_duration_min,
                "configured_duration_min": self.active_room_configured_duration_min,
                "travel_time_min_at_start": self.active_room_travel_time_min,
            },
        )
        self.set_state(
            f"sensor.{self.dashboard_prefix}_next_room",
            state=self._ha_state(self._room_label(next_room) if next_room else "Keiner"),
            attributes={
                "friendly_name": "Naechster Raum",
                "icon": "mdi:sofa-outline",
                "queue": room_queue,
            },
        )
        self.set_state(
            f"sensor.{self.dashboard_prefix}_weekly_runs",
            state=self._ha_state(weekly_runs),
            attributes={
                "friendly_name": "Reinigungen diese Woche",
                "icon": "mdi:calendar-check",
                "weekly_stats": weekly_stats,
            },
        )
        self.set_state(
            f"sensor.{self.dashboard_prefix}_weekly_minutes",
            state=self._ha_state(weekly_minutes),
            attributes={
                "friendly_name": "Reinigungsminuten diese Woche",
                "icon": "mdi:timer-sand",
                "unit_of_measurement": "min",
                "weekly_stats": weekly_stats,
            },
        )
        self.set_state(
            f"sensor.{self.dashboard_prefix}_history",
            state=self._ha_state(len(self.history_entries)),
            attributes={
                "friendly_name": "History Eintraege",
                "icon": "mdi:chart-timeline-variant",
                "recent_runs": recent_runs,
                "weekly_stats": weekly_stats,
                "room_stats": room_stats,
            },
        )

    def _on_presence_change(self, entity, attribute, old, new, kwargs):
        if old == new:
            return

        if self.away_since is None and self._is_everyone_away():
            self.away_since = self.datetime()
            self.cleaned_during_absence = []
            self.log("Alle Bewohner sind weg - starte adaptive Reinigungslogik")
            self._save_state()
            self._publish_dashboard_state("abwesend")
            self._check_cleaning({})
            return

        if self.away_since is not None and self._is_anyone_home():
            self.log("Jemand ist zuhause - beende Auto-Reinigung falls noetig")
            if self.active_room:
                self._abort_cleaning("Mindestens eine Person ist wieder zuhause")
            self._send_return_notification()
            self.away_since = None
            self.cleaned_during_absence = []
            self._save_state()
            self._publish_dashboard_state("zuhause")

    def _on_person_state_change(self, entity, attribute, old, new, kwargs):
        person_state = (
            new
            if isinstance(new, dict)
            else self.get_state(self.travel_person_entity, attribute="all")
        )
        if not isinstance(person_state, dict):
            return

        self._evaluate_travel_mode(person_state)
        self._publish_dashboard_state("zone")

    def _on_travel_signal_change(self, entity, attribute, old, new, kwargs):
        self._publish_dashboard_state("reise_update")
        if not self._is_everyone_away():
            return

        if self.active_room:
            self._monitor_active_job({})
        else:
            self._check_cleaning({})

    def _on_enabled_change(self, entity, attribute, old, new, kwargs):
        self._publish_dashboard_state("toggle")
        if new == "off" and self.active_room:
            self._abort_cleaning("Automatik wurde deaktiviert")

    def _on_runtime_setting_change(self, entity, attribute, old, new, kwargs):
        self._evaluate_travel_mode()
        self._publish_dashboard_state("einstellung")
        if self.active_room:
            self._monitor_active_job({})
        else:
            self._check_cleaning({})

    def _on_vacuum_state_change(self, entity, attribute, old, new, kwargs):
        if not self.active_room:
            return

        if new in ["idle", "docked"] and old != new:
            room = self.active_room

            if self.abort_requested:
                self._record_history_entry(room, "aborted")
                self.log(f"Auto-Reinigung fuer {self._room_label(room)} wurde abgebrochen")
            else:
                self._record_history_entry(room, "completed")
                self.last_cleaned[room] = self.datetime()
                if room not in self.cleaned_during_absence:
                    self.cleaned_during_absence.append(room)
                self.log(f"Raum erfolgreich gereinigt: {self._room_label(room)}")

            self.active_room = None
            self.active_room_started_at = None
            self.active_room_planned_duration_min = None
            self.active_room_configured_duration_min = None
            self.active_room_travel_time_min = None
            self.active_room_distance_km = None
            self.abort_requested = False
            self._save_state()
            self._publish_dashboard_state("raum_fertig")

            if self._is_everyone_away():
                self._check_cleaning({})

    def _check_cleaning(self, kwargs):
        self._publish_dashboard_state("planung")
        if not self._can_start_cleaning():
            return

        available_time = self._calculate_available_time_minutes(log_context=True)
        if available_time <= 0:
            self.log("Keine verfuegbare Reinigungszeit")
            return

        room = self._select_next_room(available_time, log_selection=True)
        if not room:
            self.log("Kein passender Raum fuer das aktuelle Rueckkehrfenster")
            return

        self._start_cleaning(room)

    def _monitor_active_job(self, kwargs):
        if not self.active_room:
            self._publish_dashboard_state("warten")
            return

        available_time = self._calculate_available_time_minutes(log_context=False)
        remaining_time = self._estimate_remaining_room_minutes(self.active_room)
        self._publish_dashboard_state("aktiv")

        if available_time < remaining_time + self._current_return_buffer_min():
            reason = (
                f"Rueckkehrfenster zu klein: verfuegbar={available_time}min, "
                f"rest={remaining_time}min"
            )
            self._abort_cleaning(reason)
