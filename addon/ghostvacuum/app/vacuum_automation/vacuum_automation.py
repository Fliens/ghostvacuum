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
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import appdaemon.plugins.hass.hassapi as hass

EVENT_START_ROOM = "vacuum_automation_start_room"
EVENT_SET_ROOM_DUE = "vacuum_automation_set_room_due"


def robot_bool(value):
    """Accept only explicit HA boolean values, never truthy unknown strings."""
    if value in (True, "true", "on", "True"):
        return True
    if value in (False, "false", "off", "False"):
        return False
    return None


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


class VacuumJobTracker:
    """Correlate one requested room with acceptance and a new completed history.

    This class only observes data. It cannot send commands or write history.
    """

    def __init__(self, segment, map_id, requested_at, baseline_history, timeout=120):
        self.segment = segment
        self.map_id = map_id
        self.requested_at = requested_at
        self.baseline_history = baseline_history
        self.timeout = timeout
        self.accepted_at = None
        self.result = "awaiting_acceptance"
        self.problem = None
        self.room_progress_percent = None

    def observe(self, snapshot, now):
        if self.result in {"completed", "aborted"} or self.problem:
            return self.problem or self.result
        phase = snapshot["phase"]
        if snapshot["map_id"] != self.map_id:
            self.problem = "map_changed"
        elif phase in {"error", "unavailable", "unknown", "stale"}:
            self.problem = phase
        elif snapshot["active_segments"] and snapshot["active_segments"] != [self.segment]:
            self.problem = "different_room_task"
        elif phase == "cleaning" and snapshot["task_status"] not in {"segment_cleaning", "completed", "unknown", "unavailable"}:
            self.problem = "different_task_type"
        if self.problem:
            return self.problem
        if self.accepted_at is None:
            if now - self.requested_at >= self.timeout:
                self.problem = "acceptance_timeout"
            elif (phase == "cleaning" and snapshot["task_status"] == "segment_cleaning"
                    and snapshot["active_segments"] == [self.segment]):
                self.accepted_at = now
                self.result = "accepted"
            return self.problem or self.result

        # A task-wide value belongs to this room only while the accepted task
        # still targets exactly that segment. Ignore pre-acceptance/reset values.
        progress = snapshot.get("progress_percent")
        updated = snapshot.get("progress_updated_at")
        if (phase == "cleaning" and snapshot["task_status"] == "segment_cleaning"
                and snapshot["active_segments"] == [self.segment]
                and type(progress) in (int, float) and math.isfinite(progress) and 0 <= progress <= 100
                and type(updated) in (int, float) and self.accepted_at < updated <= now + 1
                and now - updated <= 120):
            self.room_progress_percent = max(self.room_progress_percent or 0, progress)
        self.result = phase
        # A dock visit or a pre-existing 'completed' state alone is no proof.
        if (phase in {"docked", "drying"} and snapshot["task_status"] == "completed"
                and snapshot["task_changed_at"] is not None
                and snapshot["task_changed_at"] >= self.accepted_at
                and snapshot["task_changed_at"] <= now + 1
                and snapshot["started"] is False and snapshot["running"] is False):
            for entry in snapshot["history"]:
                timestamp = entry.get("timestamp")
                if (isinstance(timestamp, (int, float)) and math.isfinite(timestamp)
                        and timestamp > self.baseline_history
                        and timestamp <= now
                        # Device history timestamps have minute resolution.
                        and int(self.requested_at // 60) * 60 <= timestamp <= self.accepted_at + 60):
                    completed = robot_bool(entry.get("completed"))
                    if completed is not None:
                        self.result = "completed" if completed else "aborted"
                        return self.result
        return self.result


def restart_state_entity(args):
    return args.get("state_helper") or f"input_text.{args.get('helper_prefix', 'vacuum_automation')}_state"


class VacuumAutomation(hass.Hass):
    """Adaptive vacuum automation that aims to finish before residents get home."""

    def call_service(self, service, **kwargs):
        """Observation deployments must never send services to Home Assistant."""
        if self.args.get("observation_mode", False):
            self.log(f"[BEOBACHTUNG] Service blockiert: {service}")
            return None
        return super().call_service(service, **kwargs)

    def initialize(self):
        """Load configuration, restore state, and register listeners."""
        self.vacuum_entity = self.args.get("vacuum_entity", "vacuum.dreame")
        self.presence_entity = self.args.get("presence_entity", "person.user")
        configured_presence = self.args.get("presence_entities")
        self.presence_entities = (
            list(configured_presence) if configured_presence else [self.presence_entity]
        )
        self.selected_presence_entities_entity = self.args.get(
            "selected_presence_entities_entity"
        )

        self.person_entity = self.args.get("person_entity", self.presence_entity)
        self.travel_person_entity = self.args.get(
            "travel_person_entity", self.person_entity
        )
        self.tracking_mode_entity = self.args.get("tracking_mode_entity")
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
        self.state_helper = restart_state_entity(self.args)
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
        self.travel_pause_radius_km = float(self.args.get("travel_pause_radius_km", 100))
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
        self.return_buffer_entity = self.args.get("return_buffer_entity")
        self.start_margin_entity = self.args.get("start_margin_entity")
        self.cleaning_defaults_entity = self.args.get("cleaning_defaults_entity", f"input_text.{self.helper_prefix}_cleaning_defaults")
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
        self.simulation_mode = bool(self.args.get("simulation_mode", False))

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
        self._vacuum_job = None
        self._vacuum_job_problem = None
        self.history_entries: List[dict] = []
        self.learned_durations: Dict[str, float] = {}
        self._presence_listener_entities: set[str] = set()
        self._wifi_departure_guards = {}
        self._wifi_listener_pairs = set()
        self.outside_since: Dict[str, datetime] = {}
        self._presence_timer_signature = None
        self._load_presence_timers()

        self._load_return_analysis()

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
        # Check for missing helpers every 5 minutes
        self.run_every(
            self._check_helper_entities,
            self.datetime() + timedelta(minutes=2),
            300,  # 5 minutes
        )
        self.missing_helpers: List[str] = []

        self._ensure_presence_listeners()
        # Coordinate changes matter even while the person's state stays not_home.
        self.run_every(self._refresh_presence_status, self.datetime() + timedelta(seconds=10), 60)
        base = self.vacuum_entity.split(".", 1)[-1]
        for entity in [self.vacuum_entity] + [f"sensor.{base}_{suffix}" for suffix in (
            "state", "task_status", "error", "self_wash_base_status", "cleaning_history", "cleaning_progress",
        )]:
            self.listen_state(self._on_vacuum_state_change, entity, attribute="all")
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
        if self.args.get("observation_mode", False):
            self.log("Vacuum Automation initialisiert [BEOBACHTUNG - Automatik aus, Services gesperrt]")
        elif self.simulation_mode:
            self.log("Vacuum Automation initialisiert [SIMULATION MODE - keine echten Vacuum-Befehle]")
        else:
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
        if self.args.get("observation_mode", False):
            return
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
                self._vacuum_job_problem = data.get("jp")
                if self.active_room:
                    self._vacuum_job_problem = "restored_job_unconfirmed"
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
        if self.args.get("observation_mode", False):
            return
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
            "jp": self._vacuum_job_problem,
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

    def _current_return_buffer_min(self) -> int:
        return self._runtime_numeric(
            self.return_buffer_entity, self.return_buffer_min, int
        )

    def _current_start_margin_min(self) -> int:
        return max(1, self._runtime_numeric(getattr(self, "start_margin_entity", None),
                                           int(self.args.get("start_margin_min", 5)), int))

    def _room_completion_percent(self, room: str) -> int:
        entity = self._room_helper_entity(room, "input_number", "completion_percent")
        return max(1, min(100, self._runtime_numeric(entity, 100, int)))

    def _current_active_weekdays(self):
        entity = self.args.get("active_weekdays_entity")
        raw = self.get_state(entity) if entity else self.args.get("active_weekdays", "0,1,2,3,4,5,6")
        try:
            names = {name: i for i, name in enumerate(("mon", "tue", "wed", "thu", "fri", "sat", "sun"))}
            values = {names[v.strip()] if v.strip() in names else int(v) for v in str(raw).split(",") if str(v).strip()}
            return values if values <= set(range(7)) else set()
        except (TypeError, ValueError):
            return set()

    def _effective_cleaning_profile(self, room):
        def read(entity, inherit=False):
            try:
                value = json.loads(self.get_state(entity) or "{}")
                return value if valid_cleaning_profile(value, inherit) else None
            except (TypeError, ValueError):
                return None
        default = read(self.cleaning_defaults_entity)
        override = read(self._room_helper_entity(room,"input_text","cleaning_profile"), True)
        if default is None or override is None:
            return None
        return override or default

    def _prepare_cleaning_profile(self, room):
        # Only called after start authority is checked. Never change device
        # properties while observing, and never start before readback matches.
        if self.args.get("observation_mode", False):
            return False
        profile = self._effective_cleaning_profile(room)
        if profile is None:
            self.log("Reinigungseinstellungen fehlen oder sind ungültig",level="WARNING")
            return False
        base = self.vacuum_entity.split(".",1)[-1]
        values = [("cleangenius",profile["genius"] if profile["mode"] == "cleangenius" else "off")]
        if profile["mode"] == "cleangenius":
            values.append(("cleangenius_mode",profile["sequence"]))
        else:
            values.append(("cleaning_mode",profile["mode"]))
            if profile["mode"] != "mopping":
                values.append(("suction_level",profile["suction"]))
            if profile["mode"] != "sweeping":
                values.append(("mop_pad_humidity",profile["moisture"]))
            values.append(("cleaning_route",profile["route"]))
        for suffix, option in values:
            entity = f"select.{base}_{suffix}"
            state = self.get_state(entity, attribute="all") or {}
            if state.get("state") == option:
                continue
            if option not in state.get("attributes", {}).get("options", []):
                self.log(f"Reinigungsoption derzeit nicht verfügbar: {suffix}",level="WARNING")
                return False
            # Recheck start authority immediately before every device setting.
            if not self._can_start_cleaning():
                return False
            self.call_service("select/select_option",entity_id=entity,option=option)
            return False
        return True

    def _counts_as_cleaned(self, room, outcome, confirmed_room_percent=None):
        if outcome == "completed":
            return True
        # No aggregate percentage, elapsed-time estimate or current_segment can
        # prove room coverage unless correlated to an accepted single-room task.
        if confirmed_room_percent is None:
            return False
        threshold = self._room_completion_percent(room)
        return (threshold < 100 and confirmed_room_percent is not None
                and math.isfinite(confirmed_room_percent)
                and threshold <= confirmed_room_percent <= 100)

    def _return_policy(self, running=False) -> dict:
        """Home and missing data always block; proved dwell can release a short ETA."""
        people = self._presence_summary()
        buffer = self._current_return_buffer_min()
        margin = self._current_start_margin_min()
        eta = min((p["travel_time_min"] for p in people), default=None) if people and all(
            p["travel_time_min"] is not None for p in people) else None
        timed_people = [p for p in people if not p.get("stationary", False)]
        timed_eta = min((p["travel_time_min"] for p in timed_people), default=None) if all(p["travel_time_min"] is not None for p in timed_people) else None
        staying = any(p.get("stationary", False) for p in people)
        now = self.datetime()
        reason = None
        if not people or any(p["presence"] == "unknown" for p in people):
            reason = "Standort unbekannt"
        elif any(p["presence"] == "home" for p in people):
            reason = "Person zuhause"
        elif eta is None:
            reason = "Heimwegzeit unbekannt oder veraltet"
        elif not self._current_start_hour() <= now.hour < self._current_end_hour():
            reason = "Außerhalb der Reinigungszeit"
        elif now.weekday() not in self._current_active_weekdays():
            reason = "Heute keine Automatik"
        elif self.travel_mode_active:
            reason = "Reisemodus"
        elif (now.replace(hour=self._current_end_hour(), minute=0, second=0, microsecond=0) - now).total_seconds() / 60 <= buffer:
            reason = "Reinigungszeit endet"
        elif timed_eta is not None and timed_eta <= buffer:
            reason = "Puffergrenze erreicht"
        elif not running and min(timed_eta if timed_eta is not None else float("inf"), (now.replace(hour=self._current_end_hour(), minute=0, second=0, microsecond=0) - now).total_seconds() / 60) < buffer + margin:
            reason = "Startabstand noch nicht erreicht"
        return {"allowed": reason is None, "reason": reason or ("Aufenthalt erkannt · Rückkehr wird überwacht" if staying else "Reinigung fortsetzen" if running else "Startgrenze erreicht"),
                "stationary_release": staying and reason is None,
                "all_stationary": bool(people) and not timed_people, "timed_eta_min": timed_eta,
                "eta_min": eta, "buffer_min": buffer, "start_margin_min": margin,
                "start_at_min": buffer + margin, "stop_at_min": buffer}

    def _load_return_analysis(self):
        self._return_samples = []
        self._analysis_last_minute = None
        path = self.storage_dir / "return_analysis.json"
        try:
            data = json.loads(path.read_text())
            if isinstance(data, list):
                cutoff = self.datetime().timestamp() - 14 * 86400
                self._return_samples = [r for r in data if isinstance(r, dict)
                                        and isinstance(r.get("t"), (int, float)) and cutoff <= r["t"] <= self.datetime().timestamp()][-20160:]
                if self._return_samples:
                    self._analysis_last_minute = int(self._return_samples[-1]["t"] // 60)
        except (OSError, ValueError, TypeError):
            pass

    def _return_analysis(self, robot=None) -> dict:
        policy = self._return_policy(bool(self.active_room))
        blockers = self._robot_start_blockers(robot) if robot else []
        due = any(self._room_enabled(room) and (self._calculate_score(room)[0] >= .8 or self._calculate_score(room)[1] >= 1)
                  for room in self.rooms)
        decision = "Würde zur Station fahren" if self.active_room and not policy["allowed"] else (
            "Würde weiterreinigen" if self.active_room else "Würde starten" if policy["allowed"] and due and not blockers else "Würde warten")
        detail = policy["reason"] if not policy["allowed"] or self.active_room else (
            "Kein aktiver Raum fällig" if not due else ", ".join(blockers) if blockers else "Fälliger Raum kann beginnen")
        if robot and robot.get("started") is True and not self.active_room:
            decision, detail = "Extern gestartete Reinigung", "Wird von der Automatik nicht beendet"
        now = self.datetime().timestamp()
        minute = int(now // 60)
        samples = getattr(self, "_return_samples", [])
        # One real observation per minute; gaps are never filled or counted as dwell time.
        if minute != getattr(self, "_analysis_last_minute", None):
            eligible = policy["reason"] in {"Startgrenze erreicht", "Reinigung fortsetzen", "Puffergrenze erreicht", "Startabstand noch nicht erreicht", "Aufenthalt erkannt · Rückkehr wird überwacht"}
            samples = [r for r in samples if r["t"] >= now - 14 * 86400]
            samples.append({"t": now, "eta": policy["timed_eta_min"] if policy["timed_eta_min"] is not None else policy["eta_min"], "eligible": eligible,
                            "reason": detail, "buffer": policy["buffer_min"], "margin": policy["start_margin_min"],
                            "stationary": policy["all_stationary"]})
            self._return_samples = samples[-20160:]
            self._analysis_last_minute = minute
            try:
                self.storage_dir.mkdir(parents=True, exist_ok=True)
                path = self.storage_dir / "return_analysis.json"
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self._return_samples, ensure_ascii=False))
                tmp.replace(path)
            except OSError as error:
                self.log(f"Analyse konnte nicht gespeichert werden: {error}", level="WARNING")
        valid = [r for r in samples if r.get("eligible") and isinstance(r.get("eta"), (int, float))]
        below = sum(not r.get("stationary", False) and r["eta"] <= policy["buffer_min"] for r in valid)
        simulations = [{"buffer_min": b, "start_samples": sum(r.get("stationary", False) or r["eta"] >= b + policy["start_margin_min"] for r in valid),
                        "stop_samples": sum(not r.get("stationary", False) and r["eta"] <= b for r in valid)}
                       for b in sorted({5, 10, 15, 20, 30, policy["buffer_min"]})]
        reasons = {key: sum(r.get("reason") == key for r in samples) for key in sorted({r.get("reason", "Unbekannt") for r in samples})}
        runs = getattr(self, "history_entries", [])
        try:
            motion_history = json.loads((self.storage_dir / "return_motion_history.json").read_text())
        except (OSError, ValueError, TypeError):
            motion_history = None
        return {"motion_history": motion_history,
                "stationary_samples": sum(r.get("stationary", False) and r.get("eligible", False) for r in samples),
                "policy": policy, "decision": decision, "detail": detail, "samples": len(samples),
                "away_samples": len(valid), "below_buffer_samples": below, "reasons": reasons,
                "since": datetime.fromtimestamp(samples[0]["t"]).astimezone().isoformat() if samples else None,
                "simulations": simulations, "actual_runs": len(runs),
                "interrupted_runs": sum(r.get("outcome") == "aborted" for r in runs),
                "room_progress_supported": True,
                "progress_note": "Bei bestätigten Einzelraum-Aufträgen zählt der Gesamtfortschritt für diesen Raum. Ohne aktuellen Fortschrittswert zählt nur ein bestätigter Abschluss."}

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
        if self.args.get("observation_mode", False):
            return False
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
                    self._room_helper_entity(room, "input_number", "completion_percent"),
                    self._room_helper_entity(room, "input_text", "cleaning_profile"),
                ]
            )
        return entities

    def _list_runtime_helper_entities(self) -> List[str]:
        return [
            entity_id
            for entity_id in [
                self.tracking_mode_entity,
                self.start_hour_entity,
                self.end_hour_entity,
                self.selected_presence_entities_entity,
                self.return_buffer_entity,
                self.start_margin_entity,
                self.cleaning_defaults_entity,
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

    def _check_helper_entities(self, kwargs=None):
        """Check if all required helper entities exist and update missing_helpers list."""
        if self.args.get("observation_mode", False):
            return
        all_helpers = (
            [self.state_helper, self.one_time_room_override_entity, self.enabled_entity]
            + self._list_runtime_helper_entities()
            + self._list_room_helper_entities()
        )

        missing = []
        for entity_id in all_helpers:
            if not entity_id:
                continue
            state = self.get_state(entity_id, attribute="all")
            if (
                not isinstance(state, dict)
                or state.get("state") in (None, "unavailable")
            ):
                missing.append(entity_id)

        if missing and missing != self.missing_helpers:
            self.log(f"Missing helper entities detected: {missing}")
            self.missing_helpers = missing
        elif not missing and self.missing_helpers:
            self.log("All helper entities restored")
            self.missing_helpers = []

    def _presence_state(self, entity_id: str) -> str:
        raw = str(self.get_state(entity_id) or "unknown")
        return self._wifi_presence_state(entity_id, raw)

    def _wifi_presence_state(self, entity_id: str, raw: str) -> str:
        """Router loss is not departure: require a GPS fix obtained after loss/startup."""
        person = self.get_state(entity_id, attribute="all") or {}
        assigned = person.get("attributes", {}).get("device_trackers", [])
        guards = getattr(self, "_wifi_departure_guards", None)
        if guards is None:
            guards = self._wifi_departure_guards = {}
        previous = guards.get(entity_id, {})
        routers = {}
        gps = []
        for tracker in assigned:
            data = self.get_state(tracker, attribute="all") or {}
            source_type = data.get("attributes", {}).get("source_type")
            if source_type != "gps" or tracker in previous.get("routers", {}):
                routers[tracker] = (str(data.get("state", "unknown")), data.get("last_changed"))
            elif source_type == "gps":
                gps.append(data)
        if not routers:
            guards.pop(entity_id, None)
            return raw
        now = self.datetime().timestamp()
        if routers != previous.get("routers"):
            previous = {"routers": routers, "after": now, "home": previous.get("home", False)}
            guards[entity_id] = previous
        if any(self._is_home_like_state(value[0]) for value in routers.values()):
            previous["home"] = True
            return "home"
        home = self.get_state(self.home_zone, attribute="all") or {}
        attrs = home.get("attributes", {})
        confirmed = False
        for fix in gps:
            try:
                a = fix["attributes"]
                # last_reported can advance when HA merely repeats an old state.
                updated = datetime.fromisoformat(fix["last_updated"]).timestamp()
                accuracy = float(a["gps_accuracy"])
                lat, lon = float(a["latitude"]), float(a["longitude"])
                home_lat, home_lon = float(attrs["latitude"]), float(attrs["longitude"])
                radius = float(attrs.get("radius", 100))
                age_limit = min(600, float(self.args.get("position_max_age_min", 60)) * 60)
                if not all(math.isfinite(v) for v in (updated, accuracy, lat, lon, home_lat, home_lon, radius, age_limit)):
                    continue
                if not (-90 <= lat <= 90 and -180 <= lon <= 180 and -90 <= home_lat <= 90 and -180 <= home_lon <= 180):
                    continue
                if not (0 <= accuracy <= 50 and radius >= 0 and updated > previous["after"] and 0 <= now - updated <= age_limit):
                    continue
                if str(fix.get("state", "")).lower() in {"unknown", "unavailable", "home", "on", ""}:
                    continue
                if self._haversine_km(lat, lon, home_lat, home_lon) * 1000 > radius + accuracy:
                    confirmed = True
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
        if confirmed:
            previous["home"] = False
            return raw
        return "home" if previous.get("home") or self._is_home_like_state(raw) else "unknown"

    def _on_wifi_tracker_change(self, entity, attribute, old, new, kwargs):
        person = kwargs["person"]
        self._wifi_presence_state(person, str(self.get_state(person) or "unknown"))
        self._on_presence_change(person, attribute, old, new, {})

    def _current_presence_entities(self) -> List[str]:
        if not self.selected_presence_entities_entity:
            return self.presence_entities
        raw = str(self.get_state(self.selected_presence_entities_entity) or "").strip()
        if raw.lower() in {"", "unknown", "unavailable", "none"}:
            return self.presence_entities
        selected = [item.strip() for item in raw.replace(";", ",").split(",") if item.strip()]
        return selected or self.presence_entities

    def _ensure_presence_listeners(self):
        for entity_id in set(self.presence_entities) | set(self._current_presence_entities()):
            if not entity_id or entity_id in self._presence_listener_entities:
                continue
            # Prime the restart barrier before any cached away state can be used.
            self._presence_state(entity_id)
            self.listen_state(self._on_presence_change, entity_id, attribute="all")
            self._presence_listener_entities.add(entity_id)

        if not hasattr(self, "_wifi_listener_pairs"):
            self._wifi_listener_pairs = set()
        for person in self._current_presence_entities():
            data = self.get_state(person, attribute="all") or {}
            for tracker in data.get("attributes", {}).get("device_trackers", []):
                pair = (person, tracker)
                if pair not in self._wifi_listener_pairs:
                    self.listen_state(self._on_wifi_tracker_change, tracker, attribute="all", person=person)
                    self._wifi_listener_pairs.add(pair)

    def _is_home_like_state(self, state: str) -> bool:
        return state.lower() in {"home", "on"}

    def _is_away_like_state(self, state: str) -> bool:
        return state.lower() in {"not_home", "away", "off"}

    def _is_anyone_home(self) -> bool:
        return any(
            self._is_home_like_state(self._presence_state(entity_id))
            for entity_id in self._current_presence_entities()
        )

    def _is_everyone_away(self) -> bool:
        entities = self._current_presence_entities()
        if not entities:
            return False
        return all(
            self._person_is_away(entity_id)
            for entity_id in entities
        )

    def _presence_summary(self) -> List[dict]:
        return [
            self._person_presence_summary(entity_id)
            for entity_id in self._current_presence_entities()
        ]

    def _person_is_away(self, entity_id: str) -> bool:
        state = self._presence_state(entity_id).lower()
        if self._is_away_like_state(state):
            return True
        if state in {"unknown", "unavailable", "none", "", "home", "on"}:
            return False
        # Only actual HA zones count as named away states. Arbitrary text does not.
        zones = self.get_state("zone", attribute="all") or {}
        if not isinstance(zones, dict):
            return False
        return any(
            key != self.home_zone and isinstance(zone, dict) and state in {
                key.split(".", 1)[-1].replace("_", " ").lower(),
                str(zone.get("attributes", {}).get("friendly_name", "")).lower(),
            }
            for key, zone in zones.items()
        )

    def _current_tracking_mode(self) -> str:
        entity = getattr(self, "tracking_mode_entity", None)
        mode = str(self.get_state(entity) or "local") if entity else "local"
        # Legacy distance selections now use the direct location estimate.
        return mode if mode in {"local", "waze"} else "local"

    def _tracking_sensor_minutes(self, entity_id: str, mode: str, speed: float) -> Optional[float]:
        # A global sensor belongs only to the configured travel person. Never
        # apply one person's route time to another resident.
        if entity_id != getattr(self, "travel_person_entity", None):
            return None
        sensor_id = getattr(self, "waze_entity" if mode == "waze" else "distance_entity", None)
        source = self.get_state(sensor_id, attribute="all") if sensor_id else None
        if not isinstance(source, dict):
            return None
        try:
            reported = source.get("last_reported") or source.get("last_updated")
            age = self.datetime().timestamp() - datetime.fromisoformat(reported).timestamp()
            if not -60 <= age <= float(self.args.get("position_max_age_min", 60)) * 60:
                return None
            value = float(source.get("state"))
            unit = source.get("attributes", {}).get("unit_of_measurement")
            if not math.isfinite(value) or value < 0:
                return None
            if mode == "waze":
                factor = {"min":1, "s":1/60, "h":60}.get(unit)
                return math.ceil(value * factor) if factor is not None else None
            if unit not in {"m", "km"} or not math.isfinite(speed) or speed <= 0:
                return None
            km = value / 1000 if unit == "m" else value
            return math.ceil(km / speed * 60)
        except (TypeError, ValueError):
            return None

    def _person_motion(self, entity_id, presence, distance_km):
        """Dwell is proved by fresh distinct GPS reports, never by repeated polls."""
        if not hasattr(self, "_motion_observations"):
            self._motion_observations = {}
        observations = self._motion_observations
        unknown = {"stationary": False, "motion": "unknown", "stay_minutes": 0}
        if presence != "away" or distance_km is None:
            observations.pop(entity_id, None)
            return unknown
        try:
            person = self.get_state(entity_id, attribute="all") or {}
            attrs = person.get("attributes", {})
            source = self.get_state(attrs.get("source"), attribute="all") if attrs.get("source") else person
            if not isinstance(source, dict) or source.get("state") in {"unknown", "unavailable"}:
                raise ValueError("missing GPS")
            source_attrs = source.get("attributes", {})
            if source_attrs.get("source_type", attrs.get("source_type")) != "gps":
                raise ValueError("not GPS")
            accuracy = float(source_attrs.get("gps_accuracy", attrs.get("gps_accuracy", 10000)))
            lat, lon = float(attrs["latitude"]), float(attrs["longitude"])
            reported = source.get("last_reported") or source.get("last_updated")
            timestamp = datetime.fromisoformat(reported).timestamp()
            now = self.datetime().timestamp()
            if not all(math.isfinite(v) for v in (accuracy, lat, lon, timestamp, distance_km)) or not 0 <= accuracy <= 50 or not -60 <= now - timestamp <= 600:
                raise ValueError("GPS inaccurate or stale")
        except (ValueError, TypeError, KeyError):
            observations.pop(entity_id, None)
            return unknown
        previous = observations.get(entity_id)
        # A restart, a data gap or an out-of-order report requires new evidence.
        if previous is None or timestamp < previous["last"] or timestamp - previous["last"] > 600:
            previous = {"lat": lat, "lon": lon, "distance": distance_km,
                        "since": timestamp, "last": timestamp, "approaching": False, "stationary": False}
            observations[entity_id] = previous
        elif timestamp > previous["last"]:
            displacement = self._haversine_km(previous["lat"], previous["lon"], lat, lon) * 1000
            towards_home = (previous["distance"] - distance_km) * 1000 >= 75
            if displacement > 75 or towards_home:
                previous.update(lat=lat, lon=lon, distance=distance_km, since=timestamp,
                                stationary=False, approaching=previous["approaching"] or towards_home)
            elif timestamp - previous["since"] >= 600:
                previous.update(stationary=True, approaching=False)
            previous["last"] = timestamp
        return {"stationary": previous["stationary"],
                "motion": "staying" if previous["stationary"] else "approaching" if previous["approaching"] else "observing",
                "stay_minutes": int((previous["last"] - previous["since"]) / 60)}

    def _person_presence_summary(self, entity_id: str) -> dict:
        state = self._presence_state(entity_id)
        distance_km = self._person_distance_to_home(entity_id, state)
        travel_time_min = None
        speed = self._current_fallback_speed_kmh()
        if distance_km is not None and speed > 0 and math.isfinite(speed):
            travel_time_min = 0 if distance_km <= 0 else max(
                1,
                math.ceil((distance_km / speed) * 60),
            )

        mode = self._current_tracking_mode()
        if mode != "local" and not self._is_home_like_state(state):
            travel_time_min = self._tracking_sensor_minutes(entity_id, mode, speed) if self._person_is_away(entity_id) else None
        presence = "home" if self._is_home_like_state(state) else "away" if self._person_is_away(entity_id) else "unknown"
        motion = self._person_motion(entity_id, presence, distance_km)
        return {
            "entity_id": entity_id,
            "state": state,
            "presence": presence,
            **motion,
            "distance_km": round(distance_km, 1) if distance_km is not None else None,
            "travel_time_min": travel_time_min,
            "travel_time_source": "home" if self._is_home_like_state(state) else (
                ("local_estimate" if mode == "local" else mode) if travel_time_min is not None else "unknown"),
            "estimate_speed_kmh": speed,
            "outside_since": self.outside_since.get(entity_id).isoformat()
                if getattr(self, "outside_since", {}).get(entity_id) else None,
            "outside_radius": getattr(self, "_outside_radius", {}).get(entity_id),
            "travel_mode_active": getattr(self, "_person_travel_mode", {}).get(entity_id, False),
        }

    def _person_distance_to_home(self, entity_id: str, state: str) -> Optional[float]:
        if self._is_home_like_state(state):
            return 0.0
        if not self._person_is_away(entity_id):
            return None

        person_state = self.get_state(entity_id, attribute="all")
        home_state = self.get_state(self.home_zone, attribute="all")
        if not isinstance(person_state, dict) or not isinstance(home_state, dict):
            return None

        person_attrs = person_state.get("attributes", {})
        home_attrs = home_state.get("attributes", {})
        try:
            source = person_attrs.get("source")
            position = self.get_state(source, attribute="all") if source else person_state
            if not isinstance(position, dict):
                return None
            if str(position.get("state", "")).lower() in {"unknown", "unavailable"}:
                return None
            accuracy = float(person_attrs.get("gps_accuracy", 0))
            if not math.isfinite(accuracy) or not 0 <= accuracy <= float(self.args.get("max_gps_accuracy_m", 1000)):
                return None
            reported = position.get("last_reported") or position.get("last_updated")
            age = time.time() - datetime.fromisoformat(reported).timestamp()
            max_age = float(self.args.get("position_max_age_min", 60)) * 60
            if not math.isfinite(max_age) or max_age <= 0 or not -60 <= age <= max_age:
                return None
            coordinates = [float(person_attrs["latitude"]), float(person_attrs["longitude"]),
                           float(home_attrs["latitude"]), float(home_attrs["longitude"])]
            if not all(math.isfinite(v) for v in coordinates):
                return None
            if not (-90 <= coordinates[0] <= 90 and -180 <= coordinates[1] <= 180
                    and -90 <= coordinates[2] <= 90 and -180 <= coordinates[3] <= 180):
                return None
            return self._haversine_km(
                float(person_attrs["latitude"]),
                float(person_attrs["longitude"]),
                float(home_attrs["latitude"]),
                float(home_attrs["longitude"]),
            )
        except (TypeError, ValueError, KeyError):
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
        if self.args.get("observation_mode", False):
            return
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

    def _load_presence_timers(self):
        """Presence timers are separate from cleaning history and contain no GPS."""
        self._presence_timer_file = self.storage_dir / "presence_timers.json"
        try:
            data = json.loads(self._presence_timer_file.read_text())
            # An unobserved gap cannot prove continuous absence outside the radius.
            if 0 <= self.datetime().timestamp() - data["observed_at"] <= 180:
                self.outside_since = {k: datetime.fromisoformat(v)
                                      for k, v in data.get("outside_since", {}).items()}
                self._presence_timer_signature = data.get("signature")
        except (OSError, ValueError, TypeError, KeyError):
            pass

    def _save_presence_timers(self):
        if not hasattr(self, "_presence_timer_file"):
            return
        payload = {"observed_at": self.datetime().timestamp(),
                   "signature": self._presence_timer_signature,
                   "outside_since": {k: v.isoformat() for k, v in self.outside_since.items()}}
        try:
            self.storage_dir.mkdir(parents=True, exist_ok=True)
            temporary = self._presence_timer_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload))
            temporary.replace(self._presence_timer_file)
        except OSError:
            self.log("Anwesenheits-Timer konnten nicht gespeichert werden", level="WARNING")

    def _evaluate_travel_mode(self, person_state: Optional[dict] = None):
        # The optional argument remains for existing callback compatibility.
        entities = self._current_presence_entities()
        radius = self._current_travel_pause_radius_km()
        hours = self.travel_pause_after_hours
        center = self.get_state(self.home_zone, attribute="all") or {}
        attrs = center.get("attributes", {}) if isinstance(center, dict) else {}
        # A changed center/radius/duration starts a new observation period.
        import hashlib
        signature = hashlib.sha256(json.dumps(
            [attrs.get("latitude"), attrs.get("longitude"), radius, hours]
        ).encode()).hexdigest()
        if getattr(self, "_presence_timer_signature", None) != signature:
            self.outside_since = {}
            self._presence_timer_signature = signature
        self.outside_since = {k: v for k, v in self.outside_since.items() if k in entities}
        self._outside_radius = {}
        self._person_travel_mode = {}
        enabled = self._travel_mode_enabled()
        valid_settings = math.isfinite(radius) and radius > 0 and math.isfinite(hours) and hours >= 0
        now = self.datetime()
        last_evaluation = getattr(self, "_presence_last_evaluation", None)
        if last_evaluation and not 0 <= (now - last_evaluation).total_seconds() <= 180:
            self.outside_since = {}
        self._presence_last_evaluation = now
        for entity in entities:
            state = self._presence_state(entity)
            distance = self._person_distance_to_home(entity, state)
            outside = distance > radius if distance is not None and valid_settings else None
            person = self.get_state(entity, attribute="all") or {}
            accuracy_km = float(person.get("attributes", {}).get("gps_accuracy", 0)) / 1000
            if distance is not None and abs(distance - radius) <= accuracy_km and accuracy_km > 0:
                outside = None
            self._outside_radius[entity] = outside
            if enabled and outside is True and self._person_is_away(entity):
                self.outside_since.setdefault(entity, now)
                elapsed = (now - self.outside_since[entity]).total_seconds() / 3600
                self._person_travel_mode[entity] = elapsed >= hours
            else:
                self.outside_since.pop(entity, None)
                self._person_travel_mode[entity] = False
        self.travel_mode_active = bool(entities) and enabled and all(self._person_travel_mode.values())
        self.travel_mode_reason = "all_outside_radius" if self.travel_mode_active else None
        self.left_pause_zone_time = min(self.outside_since.values(), default=None)
        self._save_presence_timers()

    def _refresh_presence_status(self, kwargs=None):
        self._observe_active_job()
        self._ensure_presence_listeners()
        if not self.args.get("observation_mode", False):
            if self.active_room:
                self._monitor_active_job({})
            else:
                self._check_cleaning({})
        else:
            self._publish_dashboard_state("anwesenheit_update")

    def _get_distance_km(self) -> Optional[float]:
        people = self._presence_summary()
        if not people or any(p["distance_km"] is None for p in people):
            return None
        return min(p["distance_km"] for p in people)

    def _get_travel_time_minutes(self) -> float:
        # Unknown ETA must not create an artificial cleaning window.
        people = self._presence_summary()
        if not people or any(p["travel_time_min"] is None for p in people):
            return 0.0
        return min(p["travel_time_min"] for p in people)

    def _calculate_available_time_minutes(self, log_context: bool) -> int:
        now = self.datetime()
        end_time = now.replace(
            hour=self._current_end_hour(),
            minute=0,
            second=0,
            microsecond=0,
        )
        time_until_end = max(0.0, (end_time - now).total_seconds() / 60)
        people = self._presence_summary()
        # A stationary release is monitored permission, not a predicted absence.
        if not people or any(p["presence"] != "away" or p["travel_time_min"] is None for p in people):
            travel_minutes = 0.0
        else:
            timed = [p["travel_time_min"] for p in people if not p.get("stationary", False)]
            travel_minutes = min(timed, default=time_until_end)
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
            if available_time < self._current_return_buffer_min() + self._current_start_margin_min():
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

        for override_room in self._dashboard_room_order():
            for candidate in candidates:
                if candidate["room"] == override_room:
                    if log_selection:
                        self.log(f"Manuelle Reihenfolge: {self._room_label(override_room)}")
                    return str(override_room)

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

    def _robot_snapshot(self) -> dict:
        """Read the robot's HA cache; no services or device property writes."""
        base = self.vacuum_entity.split(".", 1)[-1]
        vacuum = self.get_state(self.vacuum_entity, attribute="all") or {}
        vacuum = vacuum if isinstance(vacuum, dict) else {}
        attrs = vacuum.get("attributes", {})

        def sensor(suffix):
            value = self.get_state(f"sensor.{base}_{suffix}", attribute="all")
            return value if isinstance(value, dict) else {}

        def timestamp(value):
            try:
                return datetime.fromisoformat(value).timestamp()
            except (TypeError, ValueError):
                return None

        progress_state = sensor("cleaning_progress")
        try:
            progress_percent = float(progress_state.get("state"))
            if not math.isfinite(progress_percent) or not 0 <= progress_percent <= 100:
                progress_percent = None
        except (TypeError, ValueError):
            progress_percent = None
        task = sensor("task_status")
        task_state = str(task.get("state", "unknown")).lower()
        device_state = str(sensor("state").get("state", "unknown")).lower()
        station = str(sensor("self_wash_base_status").get("state", "unknown")).lower()
        error = str(sensor("error").get("state", "unknown")).lower()
        state = str(vacuum.get("state", "unknown")).lower()
        updated = timestamp(vacuum.get("last_reported") or vacuum.get("last_updated"))
        max_age = float(self.args.get("robot_state_max_age_sec", 300))
        fresh = updated is not None and -60 <= time.time() - updated <= max_age
        history = [v for v in sensor("cleaning_history").get("attributes", {}).values()
                   if isinstance(v, dict) and isinstance(v.get("timestamp"), (int, float))
                   and math.isfinite(v["timestamp"])]
        history.sort(key=lambda entry: entry["timestamp"], reverse=True)
        battery = self._read_numeric_state(f"sensor.{base}_battery_level")
        if battery is None:
            battery = attrs.get("battery")
        try:
            battery = float(battery)
            if not math.isfinite(battery) or not 0 <= battery <= 100:
                battery = None
        except (TypeError, ValueError):
            battery = None
        segments = attrs.get("active_segments")
        if not isinstance(segments, list) or any(type(v) is not int for v in segments):
            segments = []
        known_rooms = []
        rooms = attrs.get("rooms", {})
        if isinstance(rooms, dict):
            for room in rooms.get(attrs.get("selected_map"), []):
                if isinstance(room, dict) and type(room.get("id")) is int:
                    known_rooms.append(room["id"])

        phase = "unknown"
        if state in {"unavailable", "unknown"}:
            phase = "unavailable"
        elif not fresh:
            phase = "stale"
        elif state == "error" or robot_bool(attrs.get("has_error")) is True or error not in {"no_error", "none", "0"}:
            phase = "unknown" if error in {"unknown", "unavailable"} else "error"
        elif any(robot_bool(attrs.get(k)) is True for k in ("washing_paused", "returning_to_wash_paused")):
            phase = "paused"
        elif device_state == "washing_paused" or station == "paused":
            phase = "paused"
        elif robot_bool(attrs.get("washing")) is True or device_state == "washing" or station == "washing":
            phase = "washing"
        elif robot_bool(attrs.get("returning_to_wash")) is True or device_state == "returning_to_wash":
            phase = "returning_to_wash"
        elif robot_bool(attrs.get("draining")) is True or device_state in {"draining", "auto_water_draining"}:
            phase = "draining"
        elif state == "paused" or robot_bool(attrs.get("paused")) is True or "paused" in task_state:
            phase = "paused"
        elif robot_bool(attrs.get("mapping")) is True or device_state in {"building", "upgrading", "station_cleaning", "auto_emptying", "water_check"}:
            phase = "busy"
        elif state == "cleaning" or device_state in {"sweeping", "mopping", "sweeping_and_mopping", "spot_cleaning"}:
            phase = "cleaning"
        elif state == "returning" or robot_bool(attrs.get("returning")) is True:
            phase = "returning"
        elif robot_bool(attrs.get("drying")) is True or station == "drying":
            phase = "drying"
        elif state == "docked" and robot_bool(attrs.get("docked")) is True:
            phase = "docked"
        elif state == "idle":
            phase = "idle"
        return {
            "phase": phase, "vacuum_state": state, "device_state": device_state,
            "task_status": task_state, "task_changed_at": timestamp(task.get("last_changed")),
            "station_status": station, "error": error, "battery": battery,
            "fresh": fresh, "map_id": attrs.get("selected_map_id"), "room_ids": known_rooms,
            "reported_at": updated,
            "data_age_sec": max(0, round(time.time() - updated)) if updated is not None else None,
            "located": robot_bool(attrs.get("located")),
            "has_saved_map": robot_bool(attrs.get("has_saved_map")),
            "started": robot_bool(attrs.get("started")), "running": robot_bool(attrs.get("running")),
            "active_segments": segments, "history": history, "dnd": attrs.get("dnd"),
            "progress_percent": progress_percent,
            "progress_updated_at": timestamp(progress_state.get("last_updated")),
        }

    def _robot_start_blockers(self, snapshot=None, room=None) -> List[str]:
        snapshot = snapshot or self._robot_snapshot()
        blockers = []
        if snapshot["phase"] != "docked":
            blockers.append(f"robot_{snapshot['phase']}")
        if snapshot["task_status"] != "completed":
            blockers.append("task_not_finished")
        if snapshot["started"] is not False or snapshot["running"] is not False:
            blockers.append("task_active_or_unknown")
        if snapshot["station_status"] != "idle":
            blockers.append("station_busy_or_unknown")
        min_battery = float(self.args.get("minimum_start_battery", 30))
        confirmation_timeout = float(self.args.get("start_confirmation_timeout_sec", 120))
        if not math.isfinite(confirmation_timeout) or confirmation_timeout <= 0:
            blockers.append("confirmation_timeout_invalid")
        if (not math.isfinite(min_battery) or not 0 < min_battery <= 100
                or snapshot["battery"] is None or snapshot["battery"] < min_battery):
            blockers.append("battery_low_or_unknown")
        if snapshot["located"] is not True or snapshot["has_saved_map"] is not True or snapshot["map_id"] is None:
            blockers.append("map_or_location_unknown")
        if not snapshot["room_ids"]:
            blockers.append("rooms_unknown")
        if room is not None and self.rooms.get(room, {}).get("segment_id") not in snapshot["room_ids"]:
            blockers.append("room_not_on_selected_map")
        if getattr(self, "_vacuum_job_problem", None):
            blockers.append("previous_job_unresolved")
        # For now treat enabled DND windows conservatively on every day. No
        # assumption about the integration's weekday bitmask can grant a start.
        schedules = snapshot["dnd"]
        if not isinstance(schedules, list):
            blockers.append("quiet_hours_unknown")
        else:
            now = self.datetime()
            minute = now.hour * 60 + now.minute
            for schedule in schedules:
                if not isinstance(schedule, dict):
                    blockers.append("quiet_hours_unknown")
                    break
                enabled = robot_bool(schedule.get("enabled"))
                if enabled is False:
                    continue
                if enabled is not True:
                    blockers.append("quiet_hours_unknown")
                    break
                try:
                    start_parts = [int(v) for v in schedule["start"].split(":")]
                    end_parts = [int(v) for v in schedule["end"].split(":")]
                    if any(len(p) != 2 or not 0 <= p[0] < 24 or not 0 <= p[1] < 60
                           for p in (start_parts, end_parts)):
                        raise ValueError
                    start, end = (p[0] * 60 + p[1] for p in (start_parts, end_parts))
                    active = (start <= minute < end) if start < end else (minute >= start or minute < end)
                    if active:
                        blockers.append("quiet_hours")
                        break
                except (KeyError, TypeError, ValueError, AttributeError):
                    blockers.append("quiet_hours_unknown")
                    break
        return blockers

    def _can_start_cleaning(self) -> bool:
        if self.args.get("observation_mode", False):
            return False
        if self.enabled_entity and self.get_state(self.enabled_entity) != "on":
            return False

        now = self.datetime()
        if not (self._current_start_hour() <= now.hour < self._current_end_hour()):
            return False
        if not self._is_everyone_away():
            return False
        if self.travel_mode_active:
            return False
        if self.active_room:
            return False

        return self._return_policy()["allowed"] and not self._robot_start_blockers()

    def _dashboard_room_order(self) -> list:
        if not getattr(self, "one_time_room_override_entity", None):
            return []
        raw = str(self.get_state(self.one_time_room_override_entity) or '').strip()
        try:
            value = json.loads(raw) if raw.startswith('[') else [raw]
        except (ValueError, TypeError):
            return []
        if not isinstance(value, list):
            return []
        return list(dict.fromkeys(room for room in value if isinstance(room, str) and room in self.rooms))

    def _one_time_room_override(self) -> Optional[str]:
        order = self._dashboard_room_order()
        return order[0] if order else None

    def _clear_one_time_room_override(self):
        if not getattr(self, "one_time_room_override_entity", None):
            return
        raw = str(self.get_state(self.one_time_room_override_entity) or '').strip()
        order = self._dashboard_room_order()
        consumed = self.active_room if self.active_room in order else (order[0] if order else None)
        remaining = [room for room in order if room != consumed]
        value = json.dumps(remaining, separators=(',', ':')) if raw.startswith('[') and remaining else ''
        try:
            self.call_service('input_text/set_value', entity_id=self.one_time_room_override_entity, value=value)
        except Exception as err:
            self.log(f"Einmaligen Raum-Vorzug konnte nicht geloescht werden: {err}", level="WARNING")

    def _mark_room_due_state(self, room: str, due: bool):
        interval_h = max(1, int(round(self._room_interval_h(room))))
        self.last_cleaned[room] = (
            self.datetime() - timedelta(hours=interval_h) if due else self.datetime()
        )
        self._save_state()
        self._publish_dashboard_state("raum_override")

    def _on_dashboard_set_room_due(self, event_name, data, kwargs):
        if self.args.get("observation_mode", False):
            return
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
        if not self._return_policy()["allowed"]:
            self.log(
                f"Manueller Start abgelehnt fuer {self._room_label(room)}: "
                f"verfuegbar={available_time}min, dauer={duration}min",
                level="INFO",
            )
            return

        self._start_cleaning(room)

    def _start_cleaning(self, room: str):
        if not self._can_start_cleaning():
            return
        snapshot = self._robot_snapshot()
        if self._robot_start_blockers(snapshot, room):
            return
        if not self.simulation_mode and not self._prepare_cleaning_profile(room):
            return
        profile = self._effective_cleaning_profile(room)
        if profile is None:
            return
        segment_id = self.rooms[room]["segment_id"]
        self.active_room = room
        self.active_room_started_at = self.datetime()
        self.active_room_planned_duration_min = self._room_effective_duration_min(room)
        self.active_room_configured_duration_min = self._room_configured_duration_min(room)
        self.active_room_travel_time_min = int(round(self._get_travel_time_minutes()))
        self.active_room_distance_km = self._get_distance_km()
        self.abort_requested = False
        self._vacuum_job = None

        if self.simulation_mode:
            self.log(
                f"[SIMULATION] Wuerde Reinigung starten fuer {self._room_label(room)} "
                f"(segment={segment_id}, geplant={self.active_room_planned_duration_min}min)"
            )
            self._schedule_simulated_completion(room)
        else:
            self._vacuum_job = VacuumJobTracker(
                segment_id, snapshot["map_id"], self.datetime().timestamp(),
                max((entry["timestamp"] for entry in snapshot["history"]), default=0),
                float(self.args.get("start_confirmation_timeout_sec", 120)),
            )
            try:
                self.call_service(
                    "dreame_vacuum/vacuum_clean_segment",
                    entity_id=self.vacuum_entity,
                    segments=[segment_id],
                    repeats=1 if profile["mode"] == "cleangenius" else profile["repeats"],
                )
            except Exception:
                self._vacuum_job_problem = "command_failed"
                self._save_state()
                self._publish_dashboard_state("start_unbestaetigt")
                return
            self.log(
                f"Reinigung angefordert fuer {self._room_label(room)} "
                f"(segment={segment_id}, geplant={self.active_room_planned_duration_min}min)"
            )

        self._clear_one_time_room_override()
        self._save_state()
        self._publish_dashboard_state("gestartet")
        if self.simulation_mode:
            self._send_start_notification(room)

    def _schedule_simulated_completion(self, room: str):
        """Schedule automatic completion of a simulated cleaning run."""
        duration_min = self.active_room_planned_duration_min or self._room_effective_duration_min(room)
        self.run_in(
            self._on_simulated_cleaning_complete,
            duration_min * 60,
            room=room,
        )
        self.log(f"[SIMULATION] Raum {self._room_label(room)} wird in {duration_min}min als fertig markiert")

    def _on_simulated_cleaning_complete(self, kwargs):
        if self.args.get("observation_mode", False):
            return
        """Handle completion of a simulated cleaning run."""
        room = kwargs.get("room")
        if not self.active_room or self.active_room != room:
            return

        if self.abort_requested:
            self._record_history_entry(room, "aborted")
            self.log(f"[SIMULATION] Reinigung fuer {self._room_label(room)} wurde abgebrochen")
        else:
            self._record_history_entry(room, "completed")
            self.last_cleaned[room] = self.datetime()
            if room not in self.cleaned_during_absence:
                self.cleaned_during_absence.append(room)
            self.log(f"[SIMULATION] Raum erfolgreich gereinigt: {self._room_label(room)}")

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
        if self.args.get("observation_mode", False):
            return
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
            "confirmed_progress_percent": self._vacuum_job.room_progress_percent if self._vacuum_job else None,
            "counted_as_cleaned": self._counts_as_cleaned(room, outcome, self._vacuum_job.room_progress_percent if self._vacuum_job else None),
        }

        self.history_entries.append(entry)
        self.history_entries = self.history_entries[-self.max_history_entries :]

        if outcome == "completed" and self._learning_enabled():
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
        if self.args.get("observation_mode", False):
            return
        if not self.active_room or self.abort_requested:
            return

        self.abort_requested = True
        if self.simulation_mode:
            self.log(f"[SIMULATION] Breche Auto-Reinigung ab: {reason}")
        else:
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
                    "completion_percent": self._room_completion_percent(room),
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
                    "fits_now": available_time >= self._current_return_buffer_min() + self._current_start_margin_min(),
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
        if self.args.get("observation_mode", False):
            return "Beobachtung"
        prefix = "[SIM] " if self.simulation_mode else ""
        if getattr(self, "_vacuum_job_problem", None):
            return f"{prefix}Pruefung noetig"
        if self.enabled_entity and self.get_state(self.enabled_entity) != "on":
            return f"{prefix}Pausiert"
        if self.travel_mode_active:
            return f"{prefix}Reisemodus"
        if self._is_anyone_home():
            return f"{prefix}Zuhause"
        if self.active_room and self.abort_requested:
            return f"{prefix}Abbruch laeuft"
        if self.active_room:
            job = getattr(self, "_vacuum_job", None)
            if job is not None:
                if job.accepted_at is None:
                    return f"{prefix}Start angefordert"
                labels = {"paused": "Pausiert", "washing": "Moppwaesche",
                          "returning_to_wash": "Rueckkehr zur Moppwaesche",
                          "returning": "Rueckkehr zur Station", "drying": "Trocknung"}
                if job.result in labels:
                    return f"{prefix}{labels[job.result]}"
            return f"{prefix}Reinigt"
        if self._is_everyone_away():
            return f"{prefix}Bereit"
        return f"{prefix}Warten"

    def _publish_dashboard_state(self, reason: str):
        self._evaluate_travel_mode()
        if self._is_everyone_away():
            if self.away_since is None:
                self.away_since = self.datetime()
        else:
            self.away_since = None
        status = self._derive_status()
        available_time = self._calculate_available_time_minutes(log_context=False)
        travel_time = round(self._get_travel_time_minutes())
        people = self._presence_summary()
        eta_known = bool(people) and all(p["travel_time_min"] is not None for p in people)
        distance_km = self._get_distance_km()
        observing = bool(self.args.get("observation_mode", False))
        next_room = (
            self._select_next_room(available_time, log_selection=False)
            if not self.active_room and not observing
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
        room_queue = [] if observing else self._build_room_queue(available_time)
        weekly_stats = self._weekly_stats()
        room_stats = [] if observing else self._room_stats()
        recent_runs = self._recent_runs()
        robot = self._robot_snapshot()
        robot_diagnostics = {key: value for key, value in robot.items() if key not in {"history", "dnd"}}
        robot_diagnostics.update({
            "start_blockers": self._robot_start_blockers(robot),
            "minimum_start_battery": float(self.args.get("minimum_start_battery", 30)),
            "job_status": self._vacuum_job_problem or (
                self._vacuum_job.result if self._vacuum_job else "no_owned_job"),
        })
        weekly_runs = weekly_stats[0]["runs"] if weekly_stats else 0
        weekly_minutes = weekly_stats[0]["minutes"] if weekly_stats else 0

        common_attributes = {
            "observation_mode": self._ha_state(observing),
            "anyone_home": self._ha_state(self._is_anyone_home()),
            "everyone_away": self._ha_state(self._is_everyone_away()),
            "friendly_name": "Vacuum Automation",
            "icon": "mdi:robot-vacuum-variant",
            "simulation_mode": self.simulation_mode,
            "reason": reason,
            "presence_entities": self.presence_entities,
            "selected_presence_entities": self._current_presence_entities(),
            "presence_summary": self._presence_summary(),
            "return_policy": self._return_policy(bool(self.active_room)),
            "travel_time_source": ("home" if travel_time == 0 else ("local_estimate" if self._current_tracking_mode() == "local" else self._current_tracking_mode())) if eta_known else "unknown",
            "travel_time_note": "Lokale Luftlinien-Schätzung, keine ÖPNV-Routenzeit",
            "travel_pause_after_hours": self.travel_pause_after_hours,
            "vacuum_entity": self.vacuum_entity,
            "travel_person_entity": self.travel_person_entity,
            "travel_pause_zone": self.travel_pause_zone,
            "travel_mode_active": self.travel_mode_active,
            "travel_mode_reason": self.travel_mode_reason,
            "travel_mode_enabled": self._travel_mode_enabled(),
            "enabled": "off" if self.args.get("observation_mode", False) else (self.get_state(self.enabled_entity) if self.enabled_entity else "on"),
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
            "travel_time_min": int(travel_time) if eta_known else None,
            "distance_km": round(distance_km, 1) if distance_km is not None else None,
            "max_distance_km": round(self._current_max_distance_km(), 1),
            "travel_home_zone": custom_home_zone,
            "travel_home_zone_distance_km": round(custom_home_zone_distance_km, 1)
            if custom_home_zone_distance_km is not None
            else None,
            "travel_pause_radius_km": round(self._current_travel_pause_radius_km(), 1),
            "return_analysis": self._return_analysis(robot),
            "start_margin_min": self._current_start_margin_min(),
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
            "missing_helpers": self.missing_helpers,
            "robot_diagnostics": robot_diagnostics,
        }
        # The pinned AppDaemon release prunes False and numeric zero recursively
        # from HTTP attributes. JSON text preserves their exact diagnostic values.
        common_attributes["diagnostics_json"] = json.dumps(common_attributes, ensure_ascii=False)

        self.set_state(
            f"sensor.{self.dashboard_prefix}_status",
            state=self._ha_state(status),
            attributes=common_attributes,
            replace=True,
        )
        self.set_state(
            f"sensor.{self.dashboard_prefix}_travel_time",
            state=self._ha_state(int(travel_time) if eta_known else None),
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

        old_state = old.get("state") if isinstance(old, dict) else old
        new_state = new.get("state") if isinstance(new, dict) else new

        if self.args.get("observation_mode", False):
            # Observe every transition, including unknown states, without entering
            # the cleaning, history or notification paths.
            if self._is_everyone_away():
                if self.away_since is None:
                    self.away_since = self.datetime()
            else:
                self.away_since = None
            self._evaluate_travel_mode()
            self._publish_dashboard_state("anwesenheit")
            return

        if old_state == new_state:
            if self.active_room:
                self._monitor_active_job({})
            else:
                self._check_cleaning({})
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
        self._ensure_presence_listeners()
        self._evaluate_travel_mode()
        self._publish_dashboard_state("einstellung")
        if self.active_room:
            self._monitor_active_job({})
        else:
            self._check_cleaning({})

    def _observe_active_job(self):
        if self.args.get("observation_mode", False) or not self.active_room or self.simulation_mode:
            return
        if self._vacuum_job_problem:
            return
        if self._vacuum_job is None:
            self._vacuum_job_problem = "restored_job_unconfirmed"
            self._save_state()
            return
        previously_accepted = self._vacuum_job.accepted_at is not None
        result = self._vacuum_job.observe(self._robot_snapshot(), self.datetime().timestamp())
        if self._vacuum_job.problem:
            self._vacuum_job_problem = self._vacuum_job.problem
            self._save_state()
            return
        if not previously_accepted and self._vacuum_job.accepted_at is not None:
            self._send_start_notification(self.active_room)
        if result not in {"completed", "aborted"}:
            return
        room = self.active_room
        outcome = "aborted" if self.abort_requested or result == "aborted" else "completed"
        self._record_history_entry(room, outcome)
        if self._counts_as_cleaned(room, outcome, self._vacuum_job.room_progress_percent):
            self.last_cleaned[room] = self.datetime()
            if room not in self.cleaned_during_absence:
                self.cleaned_during_absence.append(room)
        elif not self.abort_requested:
            self._vacuum_job_problem = "job_incomplete"
        self.active_room = None
        self.active_room_started_at = None
        self.active_room_planned_duration_min = None
        self.active_room_configured_duration_min = None
        self.active_room_travel_time_min = None
        self.active_room_distance_km = None
        self.abort_requested = False
        self._vacuum_job = None
        self._save_state()
        self._publish_dashboard_state("raum_fertig" if outcome == "completed" else "raum_abgebrochen")
        # No automatic restart after an incomplete, lost or ambiguous task.
        if outcome == "completed" and self._can_start_cleaning():
            self._check_cleaning({})

    def _on_vacuum_state_change(self, entity, attribute, old, new, kwargs):
        self._observe_active_job()
        self._publish_dashboard_state("staubsauger_update")

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
        self._observe_active_job()
        if self.args.get("observation_mode", False) or self._vacuum_job_problem:
            self._publish_dashboard_state("staubsauger_beobachtung")
            return
        if not self.active_room:
            self._publish_dashboard_state("warten")
            return

        policy = self._return_policy(running=True)
        self._publish_dashboard_state("aktiv")
        if not policy["allowed"]:
            self._abort_cleaning(policy["reason"])
