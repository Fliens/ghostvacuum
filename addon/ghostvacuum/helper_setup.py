#!/usr/bin/env python3
"""Automatically create Home Assistant Helper entities via WebSocket API.

This script checks if the required Helper entities exist and creates them
via the Home Assistant WebSocket API if they don't. This eliminates the need
for users to manually import helpers.yaml into their configuration.

Helper creation in Home Assistant requires the WebSocket API, not REST.
The add-on connects via ws://supervisor/core/websocket using SUPERVISOR_TOKEN.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

try:
    import websockets
    from websockets.client import connect as ws_connect
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False


OPTIONS_PATH = Path("/data/options.json")
SUPERVISOR_CORE_API = "http://supervisor/core/api"
SUPERVISOR_WS_API = "ws://supervisor/core/websocket"
DEFAULT_HELPER_PREFIX = "vacuum_automation"

# Helper definitions for the core automation
# Note: Home Assistant input_text max length is limited to 255 characters
# IMPORTANT: The 'name' field determines the entity_id that HA generates.
# HA slugifies the name to create the entity_id, so names must match what we expect.
# Format: "Vacuum Automation <suffix>" -> input_*.vacuum_automation_<suffix>
CORE_INPUT_TEXT_HELPERS = [
    {
        "id": "state",
        "name": "vacuum automation state",  # -> input_text.vacuum_automation_state
        "max": 255,  # HA limit is 255, use file storage for larger state
        "initial": "{}",
    },
    {
        "id": "one_time_room_override",
        "name": "vacuum automation one time room override",  # -> input_text.vacuum_automation_one_time_room_override
        "max": 128,
        "initial": "",
    },
    {
        "id": "selected_presence_entities",
        "name": "vacuum automation selected presence entities",  # -> input_text.vacuum_automation_selected_presence_entities
        "max": 255,  # HA limit is 255
        "initial": "",
    },
    {
        "id": "active_weekdays",
        "name": "vacuum automation active weekdays",  # -> input_text.vacuum_automation_active_weekdays
        "max": 64,
        "initial": "mon,tue,wed,thu,fri,sat",
    },
    {
        "id": "room_names",
        "name": "vacuum automation room names",  # -> input_text.vacuum_automation_room_names
        "max": 255,
        "initial": "{}",
    },
]

CORE_INPUT_BOOLEAN_HELPERS = [
    {
        "id": "enabled",
        "name": "vacuum automation enabled",  # -> input_boolean.vacuum_automation_enabled
        "icon": "mdi:robot-vacuum",
        "initial": True,
    },
    {
        "id": "learning_enabled",
        "name": "vacuum automation learning enabled",  # -> input_boolean.vacuum_automation_learning_enabled
        "icon": "mdi:brain",
        "initial": True,
    },
    {
        "id": "home_override_enabled",
        "name": "vacuum automation home override enabled",  # -> input_boolean.vacuum_automation_home_override_enabled
        "icon": "mdi:home-map-marker",
        "initial": False,
    },
    {
        "id": "travel_mode_enabled",
        "name": "vacuum automation travel mode enabled",  # -> input_boolean.vacuum_automation_travel_mode_enabled
        "icon": "mdi:car",
        "initial": True,
    },
    {
        "id": "start_notifications_enabled",
        "name": "vacuum automation start notifications enabled",  # -> input_boolean.vacuum_automation_start_notifications_enabled
        "icon": "mdi:bell",
        "initial": False,
    },
    {
        "id": "return_summary_enabled",
        "name": "vacuum automation return summary enabled",  # -> input_boolean.vacuum_automation_return_summary_enabled
        "icon": "mdi:clipboard-check",
        "initial": True,
    },
]

CORE_INPUT_NUMBER_HELPERS = [
    {
        "id": "start_hour",
        "name": "vacuum automation start hour",  # -> input_number.vacuum_automation_start_hour
        "min": 0,
        "max": 23,
        "step": 1,
        "mode": "box",
        "initial": 8,
    },
    {
        "id": "end_hour",
        "name": "vacuum automation end hour",  # -> input_number.vacuum_automation_end_hour
        "min": 1,
        "max": 23,
        "step": 1,
        "mode": "box",
        "initial": 22,
    },
    {
        "id": "return_buffer",
        "name": "vacuum automation return buffer",  # -> input_number.vacuum_automation_return_buffer
        "min": 0,
        "max": 30,
        "step": 1,
        "unit_of_measurement": "min",
        "mode": "slider",
        "initial": 5,
    },
    {
        "id": "fallback_speed",
        "name": "vacuum automation fallback speed",  # -> input_number.vacuum_automation_fallback_speed
        "min": 5,
        "max": 130,
        "step": 1,
        "unit_of_measurement": "km/h",
        "mode": "slider",
        "initial": 30,
    },
    {
        "id": "default_travel_time",
        "name": "vacuum automation default travel time",  # -> input_number.vacuum_automation_default_travel_time
        "min": 5,
        "max": 240,
        "step": 5,
        "unit_of_measurement": "min",
        "mode": "slider",
        "initial": 60,
    },
    {
        "id": "home_latitude",
        "name": "vacuum automation home latitude",  # -> input_number.vacuum_automation_home_latitude
        "min": -90,
        "max": 90,
        "step": 0.0001,
        "mode": "box",
        "initial": 52.52,
    },
    {
        "id": "home_longitude",
        "name": "vacuum automation home longitude",  # -> input_number.vacuum_automation_home_longitude
        "min": -180,
        "max": 180,
        "step": 0.0001,
        "mode": "box",
        "initial": 13.405,
    },
    {
        "id": "travel_pause_radius",
        "name": "vacuum automation travel pause radius",  # -> input_number.vacuum_automation_travel_pause_radius
        "min": 1,
        "max": 500,
        "step": 1,
        "unit_of_measurement": "km",
        "mode": "slider",
        "initial": 100,
    },
    {
        "id": "max_distance_km",
        "name": "vacuum automation max distance km",  # -> input_number.vacuum_automation_max_distance_km
        "min": 0,
        "max": 1000,
        "step": 1,
        "unit_of_measurement": "km",
        "mode": "box",
        "initial": 0,
    },
]


def helper_display_prefix(prefix: str) -> str:
    """Return the human-readable prefix Home Assistant slugifies into entity IDs."""
    return str(prefix or DEFAULT_HELPER_PREFIX).replace("_", " ").strip()


def helper_display_name(prefix: str, suffix: str) -> str:
    """Build a helper name that slugifies to <prefix>_<suffix>."""
    suffix_name = str(suffix or "").replace("_", " ").strip()
    return f"{helper_display_prefix(prefix)} {suffix_name}".strip()


def read_options() -> Dict[str, Any]:
    """Read add-on options from the options.json file."""
    if not OPTIONS_PATH.exists():
        return {}
    try:
        return json.loads(OPTIONS_PATH.read_text())
    except Exception:
        return {}


def slugify(value: str) -> str:
    """Convert a string to a valid entity ID slug."""
    text = value.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_") or "room"


def parse_rooms(raw_rooms: Any) -> List[dict]:
    """Parse room configuration from options."""
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
        if not room_id:
            room_id = slugify(item.get("name", ""))
        if not room_id:
            continue

        rooms.append({
            "id": room_id,
            "name": str(item.get("name") or room_id.replace("_", " ").title()),
            "icon": str(item.get("icon") or "mdi:floor-plan"),
            "interval_h": int(item.get("interval_h", 48)),
            "weight": float(item.get("weight", 1.0)),
            "duration_min": int(item.get("duration_min", 15)),
            "enabled": bool(item.get("enabled", True)),
        })
    return rooms


def api_request(
    path: str,
    method: str = "GET",
    payload: dict | None = None,
    timeout: int = 30,
) -> dict | None:
    """Make an API request to the Home Assistant Supervisor API."""
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        print(f"[helper_setup] Warning: No SUPERVISOR_TOKEN found")
        return None

    url = f"{SUPERVISOR_CORE_API}{path}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method=method,
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            if body:
                return json.loads(body)
            return {}
    except urllib.error.HTTPError as e:
        error_body = ""
        try:
            error_body = e.read().decode("utf-8")
        except Exception:
            pass
        print(f"[helper_setup] API error {e.code} for {path}: {error_body}")
        return None
    except Exception as e:
        print(f"[helper_setup] API request failed for {path}: {e}")
        return None


def entity_exists(entity_id: str) -> bool:
    """Check if an entity already exists in Home Assistant."""
    result = api_request(f"/states/{entity_id}")
    return result is not None


class WebSocketHelper:
    """Helper class for WebSocket communication with Home Assistant."""

    def __init__(self):
        self.ws = None
        self.msg_id = 0
        self.token = os.environ.get("SUPERVISOR_TOKEN", "")

    async def connect(self) -> bool:
        """Connect to Home Assistant WebSocket API."""
        if not HAS_WEBSOCKETS:
            print("[helper_setup] websockets library not installed")
            return False

        if not self.token:
            print("[helper_setup] No SUPERVISOR_TOKEN found")
            return False

        try:
            self.ws = await ws_connect(SUPERVISOR_WS_API)

            # Wait for auth_required message
            msg = await self.ws.recv()
            data = json.loads(msg)
            if data.get("type") != "auth_required":
                print(f"[helper_setup] Unexpected message: {data}")
                return False

            # Send auth
            await self.ws.send(json.dumps({
                "type": "auth",
                "access_token": self.token,
            }))

            # Wait for auth_ok
            msg = await self.ws.recv()
            data = json.loads(msg)
            if data.get("type") != "auth_ok":
                print(f"[helper_setup] Auth failed: {data}")
                return False

            print("[helper_setup] WebSocket connected successfully")
            return True

        except Exception as e:
            print(f"[helper_setup] WebSocket connection failed: {e}")
            return False

    async def close(self):
        """Close the WebSocket connection."""
        if self.ws:
            await self.ws.close()
            self.ws = None

    async def send_command(self, msg_type: str, **kwargs) -> dict | None:
        """Send a command and wait for the response."""
        if not self.ws:
            return None

        self.msg_id += 1
        message = {
            "id": self.msg_id,
            "type": msg_type,
            **kwargs,
        }

        try:
            await self.ws.send(json.dumps(message))
            response = await self.ws.recv()
            return json.loads(response)
        except Exception as e:
            print(f"[helper_setup] WebSocket command failed: {e}")
            return None

    async def create_input_boolean(
        self,
        name: str,
        icon: str = "mdi:toggle-switch",
    ) -> dict | None:
        """Create an input_boolean helper.

        Note: The input_boolean/create API does not support setting an initial state.
        The entity will default to 'off' and can be set after creation if needed.
        """
        return await self.send_command(
            "input_boolean/create",
            name=name,
            icon=icon,
        )

    async def create_input_number(
        self,
        name: str,
        min_val: float,
        max_val: float,
        step: float = 1,
        mode: str = "box",
        unit_of_measurement: str = "",
        initial: float = 0,
    ) -> dict | None:
        """Create an input_number helper."""
        payload = {
            "name": name,
            "min": min_val,
            "max": max_val,
            "step": step,
            "mode": mode,
        }
        if unit_of_measurement:
            payload["unit_of_measurement"] = unit_of_measurement
        if initial is not None:
            payload["initial"] = initial

        return await self.send_command("input_number/create", **payload)

    async def create_input_text(
        self,
        name: str,
        max_len: int = 255,
        min_len: int = 0,
        initial: str = "",
        mode: str = "text",
    ) -> dict | None:
        """Create an input_text helper."""
        payload = {
            "name": name,
            "max": max_len,
            "min": min_len,
            "mode": mode,
        }
        if initial:
            payload["initial"] = initial

        return await self.send_command("input_text/create", **payload)

    async def list_helpers(self, helper_type: str) -> list | None:
        """List all helpers of a given type."""
        result = await self.send_command(f"{helper_type}/list")
        if result and result.get("success"):
            return result.get("result", [])
        return None

    async def delete_helper(self, helper_type: str, helper_id: str) -> bool:
        """Delete a helper by its ID."""
        result = await self.send_command(
            f"{helper_type}/delete",
            **{f"{helper_type}_id": helper_id},
        )
        return result is not None and result.get("success", False)


def get_all_helper_entity_ids(prefix: str, rooms: List[dict]) -> List[str]:
    """Get a list of all helper entity IDs that should exist."""
    entity_ids = []

    # Core input_text helpers
    for helper in CORE_INPUT_TEXT_HELPERS:
        entity_ids.append(f"input_text.{prefix}_{helper['id']}")

    # Core input_boolean helpers
    for helper in CORE_INPUT_BOOLEAN_HELPERS:
        entity_ids.append(f"input_boolean.{prefix}_{helper['id']}")

    # Core input_number helpers
    for helper in CORE_INPUT_NUMBER_HELPERS:
        entity_ids.append(f"input_number.{prefix}_{helper['id']}")

    # Room-specific helpers
    for room in rooms:
        room_id = room["id"]
        entity_ids.append(f"input_boolean.{prefix}_{room_id}_enabled")
        entity_ids.append(f"input_number.{prefix}_{room_id}_weight")
        entity_ids.append(f"input_number.{prefix}_{room_id}_interval_h")
        entity_ids.append(f"input_number.{prefix}_{room_id}_duration_min")

    return entity_ids


def get_missing_helpers(prefix: str, rooms: List[dict]) -> List[str]:
    """Check which helper entities are missing."""
    expected = get_all_helper_entity_ids(prefix, rooms)
    missing = []

    for entity_id in expected:
        if not entity_exists(entity_id):
            missing.append(entity_id)

    return missing


async def setup_helpers_async(prefix: str, rooms: List[dict]) -> int:
    """Create all helper entities using WebSocket API."""
    ws = WebSocketHelper()

    if not await ws.connect():
        print("[helper_setup] Failed to connect via WebSocket")
        return 0

    created = 0

    try:
        # Create input_text helpers
        print("[helper_setup] Setting up input_text helpers...")
        for helper in CORE_INPUT_TEXT_HELPERS:
            entity_id = f"input_text.{prefix}_{helper['id']}"
            if entity_exists(entity_id):
                print(f"[helper_setup] {entity_id} already exists, skipping")
                continue

            result = await ws.create_input_text(
                name=helper_display_name(prefix, helper["id"]),
                max_len=helper.get("max", 255),
                initial=helper.get("initial", ""),
            )

            if result and result.get("success"):
                print(f"[helper_setup] Created {entity_id}")
                created += 1
            else:
                error = result.get("error", {}) if result else {}
                print(f"[helper_setup] Failed to create {entity_id}: {error}")

        # Create input_boolean helpers
        print("[helper_setup] Setting up input_boolean helpers...")
        for helper in CORE_INPUT_BOOLEAN_HELPERS:
            entity_id = f"input_boolean.{prefix}_{helper['id']}"
            if entity_exists(entity_id):
                print(f"[helper_setup] {entity_id} already exists, skipping")
                continue

            result = await ws.create_input_boolean(
                name=helper_display_name(prefix, helper["id"]),
                icon=helper.get("icon", "mdi:toggle-switch"),
            )

            if result and result.get("success"):
                print(f"[helper_setup] Created {entity_id}")
                created += 1
            else:
                error = result.get("error", {}) if result else {}
                print(f"[helper_setup] Failed to create {entity_id}: {error}")

        # Create input_number helpers
        print("[helper_setup] Setting up input_number helpers...")
        for helper in CORE_INPUT_NUMBER_HELPERS:
            entity_id = f"input_number.{prefix}_{helper['id']}"
            if entity_exists(entity_id):
                print(f"[helper_setup] {entity_id} already exists, skipping")
                continue

            result = await ws.create_input_number(
                name=helper_display_name(prefix, helper["id"]),
                min_val=helper.get("min", 0),
                max_val=helper.get("max", 100),
                step=helper.get("step", 1),
                mode=helper.get("mode", "box"),
                unit_of_measurement=helper.get("unit_of_measurement", ""),
                initial=helper.get("initial", 0),
            )

            if result and result.get("success"):
                print(f"[helper_setup] Created {entity_id}")
                created += 1
            else:
                error = result.get("error", {}) if result else {}
                print(f"[helper_setup] Failed to create {entity_id}: {error}")

        # Create room helpers
        print(f"[helper_setup] Setting up helpers for {len(rooms)} rooms...")
        for room in rooms:
            room_id = room["id"]
            room_name = room["name"]
            room_icon = room.get("icon", "mdi:floor-plan")

            # Room enabled toggle
            # Name format: "vacuum automation {room_id} enabled" -> input_boolean.vacuum_automation_{room_id}_enabled
            entity_id = f"input_boolean.{prefix}_{room_id}_enabled"
            if not entity_exists(entity_id):
                result = await ws.create_input_boolean(
                    name=f"{prefix} {room_id} enabled",
                    icon=room_icon,
                )
                if result and result.get("success"):
                    print(f"[helper_setup] Created {entity_id}")
                    created += 1
                else:
                    error = result.get("error", {}) if result else {}
                    print(f"[helper_setup] Failed to create {entity_id}: {error}")

            # Room weight
            # Name format: "vacuum automation {room_id} weight" -> input_number.vacuum_automation_{room_id}_weight
            entity_id = f"input_number.{prefix}_{room_id}_weight"
            if not entity_exists(entity_id):
                result = await ws.create_input_number(
                    name=f"{prefix} {room_id} weight",
                    min_val=0.1,
                    max_val=3,
                    step=0.05,
                    mode="box",
                    initial=room.get("weight", 1.0),
                )
                if result and result.get("success"):
                    print(f"[helper_setup] Created {entity_id}")
                    created += 1
                else:
                    error = result.get("error", {}) if result else {}
                    print(f"[helper_setup] Failed to create {entity_id}: {error}")

            # Room interval
            # Name format: "vacuum automation {room_id} interval h" -> input_number.vacuum_automation_{room_id}_interval_h
            entity_id = f"input_number.{prefix}_{room_id}_interval_h"
            if not entity_exists(entity_id):
                result = await ws.create_input_number(
                    name=f"{prefix} {room_id} interval h",
                    min_val=6,
                    max_val=168,
                    step=1,
                    unit_of_measurement="h",
                    mode="box",
                    initial=room.get("interval_h", 48),
                )
                if result and result.get("success"):
                    print(f"[helper_setup] Created {entity_id}")
                    created += 1
                else:
                    error = result.get("error", {}) if result else {}
                    print(f"[helper_setup] Failed to create {entity_id}: {error}")

            # Room duration
            # Name format: "vacuum automation {room_id} duration min" -> input_number.vacuum_automation_{room_id}_duration_min
            entity_id = f"input_number.{prefix}_{room_id}_duration_min"
            if not entity_exists(entity_id):
                result = await ws.create_input_number(
                    name=f"{prefix} {room_id} duration min",
                    min_val=1,
                    max_val=120,
                    step=1,
                    unit_of_measurement="min",
                    mode="box",
                    initial=room.get("duration_min", 15),
                )
                if result and result.get("success"):
                    print(f"[helper_setup] Created {entity_id}")
                    created += 1
                else:
                    error = result.get("error", {}) if result else {}
                    print(f"[helper_setup] Failed to create {entity_id}: {error}")

    finally:
        await ws.close()

    return created


async def cleanup_helpers_async(prefix: str, rooms: List[dict]) -> int:
    """Delete all helper entities created by this add-on."""
    ws = WebSocketHelper()

    if not await ws.connect():
        print("[helper_setup] Failed to connect via WebSocket")
        return 0

    deleted = 0
    entity_ids = get_all_helper_entity_ids(prefix, rooms)

    try:
        # Get list of all helpers to find their IDs
        for helper_type in ["input_boolean", "input_number", "input_text"]:
            helpers = await ws.list_helpers(helper_type)
            if not helpers:
                continue

            for helper in helpers:
                # The helper has an 'id' and a 'name'
                # We need to match by entity_id pattern
                helper_id = helper.get("id", "")
                helper_name = helper.get("name", "")

                # Check if this helper matches one of ours
                for entity_id in entity_ids:
                    if entity_id.startswith(f"{helper_type}."):
                        # Match by checking if the name matches
                        if f"{prefix}_" in helper_id or helper_name.startswith("Vacuum"):
                            if await ws.delete_helper(helper_type, helper_id):
                                print(f"[helper_setup] Deleted {helper_type}.{helper_id}")
                                deleted += 1
                            break

    finally:
        await ws.close()

    return deleted


async def ensure_helpers_exist_async(prefix: str, rooms: List[dict]) -> List[str]:
    """Check for missing helpers and create them."""
    missing = get_missing_helpers(prefix, rooms)

    if not missing:
        print("[helper_setup] All helpers exist")
        return []

    print(f"[helper_setup] Found {len(missing)} missing helpers")

    ws = WebSocketHelper()
    if not await ws.connect():
        print("[helper_setup] Failed to connect via WebSocket")
        return []

    recreated = []

    try:
        for entity_id in missing:
            parts = entity_id.split(".")
            if len(parts) != 2:
                continue

            domain, object_id = parts
            if not object_id.startswith(f"{prefix}_"):
                continue

            suffix = object_id[len(prefix) + 1:]
            result = None

            if domain == "input_text":
                for helper in CORE_INPUT_TEXT_HELPERS:
                    if helper["id"] == suffix:
                        result = await ws.create_input_text(
                            name=helper_display_name(prefix, helper["id"]),
                            max_len=helper.get("max", 255),
                            initial=helper.get("initial", ""),
                        )
                        break

            elif domain == "input_boolean":
                # Check core helpers
                for helper in CORE_INPUT_BOOLEAN_HELPERS:
                    if helper["id"] == suffix:
                        result = await ws.create_input_boolean(
                            name=helper_display_name(prefix, helper["id"]),
                            icon=helper.get("icon", "mdi:toggle-switch"),
                        )
                        break
                else:
                    # Check room helpers
                    for room in rooms:
                        if suffix == f"{room['id']}_enabled":
                            result = await ws.create_input_boolean(
                                name=f"{prefix} {room['id']} enabled",
                                icon=room.get("icon", "mdi:floor-plan"),
                            )
                            break

            elif domain == "input_number":
                # Check core helpers
                for helper in CORE_INPUT_NUMBER_HELPERS:
                    if helper["id"] == suffix:
                        result = await ws.create_input_number(
                            name=helper_display_name(prefix, helper["id"]),
                            min_val=helper.get("min", 0),
                            max_val=helper.get("max", 100),
                            step=helper.get("step", 1),
                            mode=helper.get("mode", "box"),
                            unit_of_measurement=helper.get("unit_of_measurement", ""),
                            initial=helper.get("initial", 0),
                        )
                        break
                else:
                    # Check room helpers
                    for room in rooms:
                        room_id = room["id"]
                        if suffix == f"{room_id}_weight":
                            result = await ws.create_input_number(
                                name=f"{prefix} {room_id} weight",
                                min_val=0.1,
                                max_val=3,
                                step=0.05,
                                mode="box",
                                initial=room.get("weight", 1.0),
                            )
                            break
                        elif suffix == f"{room_id}_interval_h":
                            result = await ws.create_input_number(
                                name=f"{prefix} {room_id} interval h",
                                min_val=6,
                                max_val=168,
                                step=1,
                                unit_of_measurement="h",
                                mode="box",
                                initial=room.get("interval_h", 48),
                            )
                            break
                        elif suffix == f"{room_id}_duration_min":
                            result = await ws.create_input_number(
                                name=f"{prefix} {room_id} duration min",
                                min_val=1,
                                max_val=120,
                                step=1,
                                unit_of_measurement="min",
                                mode="box",
                                initial=room.get("duration_min", 15),
                            )
                            break

            if result and result.get("success"):
                print(f"[helper_setup] Recreated {entity_id}")
                recreated.append(entity_id)
            elif result:
                error = result.get("error", {})
                print(f"[helper_setup] Failed to recreate {entity_id}: {error}")

    finally:
        await ws.close()

    return recreated


def wait_for_home_assistant(max_retries: int = 30, delay: float = 2.0) -> bool:
    """Wait for Home Assistant to be ready before creating helpers."""
    print("[helper_setup] Waiting for Home Assistant to be ready...")

    for i in range(max_retries):
        result = api_request("/")
        if result is not None:
            print("[helper_setup] Home Assistant is ready")
            return True

        print(f"[helper_setup] Waiting... ({i + 1}/{max_retries})")
        time.sleep(delay)

    print("[helper_setup] Timeout waiting for Home Assistant")
    return False


def main() -> int:
    """Main entry point for helper setup."""
    import argparse

    parser = argparse.ArgumentParser(description="Manage Vacuum Automation Helper entities")
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Delete all helper entities created by this add-on",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check for missing helpers and recreate them",
    )
    args = parser.parse_args()

    print("[helper_setup] Starting helper management...")

    if not HAS_WEBSOCKETS:
        print("[helper_setup] ERROR: websockets library not installed")
        print("[helper_setup] Install it with: pip install websockets")
        return 1

    # Wait for HA to be ready
    if not wait_for_home_assistant():
        print("[helper_setup] Warning: Could not connect to Home Assistant")
        print("[helper_setup] Helpers may need to be created manually")
        return 1

    # Load options
    options = read_options()
    prefix = options.get("helper_prefix", DEFAULT_HELPER_PREFIX)
    rooms = parse_rooms(options.get("rooms", []))

    print(f"[helper_setup] Using prefix: {prefix}")
    print(f"[helper_setup] Found {len(rooms)} rooms")

    if args.cleanup:
        # Cleanup mode - delete all helpers
        deleted = asyncio.run(cleanup_helpers_async(prefix, rooms))
        print(f"[helper_setup] Cleanup complete, deleted {deleted} helpers")
        return 0

    if args.check:
        # Check mode - only recreate missing helpers
        missing_before = get_missing_helpers(prefix, rooms)
        recreated = asyncio.run(ensure_helpers_exist_async(prefix, rooms))
        if recreated:
            print(f"[helper_setup] Recreated {len(recreated)} missing helpers")
        else:
            print("[helper_setup] No helpers were recreated")
        missing_after = get_missing_helpers(prefix, rooms)
        if missing_after:
            print(
                "[helper_setup] Missing helpers remain after repair: "
                + ", ".join(missing_after)
            )
            return 1
        if missing_before:
            print("[helper_setup] All missing helpers were repaired")
        else:
            print("[helper_setup] All helpers exist")
        return 0

    # Default: Setup mode - create all helpers
    created = asyncio.run(setup_helpers_async(prefix, rooms))
    print(f"[helper_setup] Created {created} helpers")

    if created > 0:
        # Give Home Assistant time to register the new entities
        # 5 seconds is usually enough for HA to process and make entities available
        print("[helper_setup] Waiting for entities to be registered...")
        time.sleep(5)

    print("[helper_setup] Helper setup complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
