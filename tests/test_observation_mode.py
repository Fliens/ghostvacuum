"""Verify hardware safety without AppDaemon or a Home Assistant connection."""
import importlib.util
from pathlib import Path
import sys
import types
import unittest

class FakeHass:
    def call_service(self, service, **kwargs):
        raise AssertionError(f"Unexpected outgoing service: {service}")

for name in ('appdaemon', 'appdaemon.plugins', 'appdaemon.plugins.hass'):
    sys.modules.setdefault(name, types.ModuleType(name))
hassapi = types.ModuleType('appdaemon.plugins.hass.hassapi')
hassapi.Hass = FakeHass
sys.modules[hassapi.__name__] = hassapi
source = Path(__file__).resolve().parents[1] / 'apps/vacuum_automation/vacuum_automation.py'
spec = importlib.util.spec_from_file_location('vacuum_app', source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class ObservationSafety(unittest.TestCase):
    def setUp(self):
        self.app = module.VacuumAutomation()
        self.app.args = {'observation_mode': True}
        self.app.log = lambda *a, **k: None
        self.app.presence_entities = ['person.simon']
        self.app.selected_presence_entities_entity = None

    def test_every_service_is_blocked(self):
        for service in ('dreame_vacuum/vacuum_clean_segment', 'vacuum/return_to_base',
                        'notify/mobile_app', 'input_boolean/turn_on'):
            self.assertIsNone(self.app.call_service(service, entity_id='vacuum.mova'))

    def test_start_blocked_even_without_helpers(self):
        self.assertFalse(self.app._can_start_cleaning())
        self.assertEqual(self.app._derive_status(), 'Beobachtung')

    def test_presence_truth_table(self):
        for state, home, away in [('home', True, False), ('not_home', False, True),
                                  ('unknown', False, False), ('unavailable', False, False)]:
            with self.subTest(state=state):
                self.app.get_state = lambda entity, **kw: {} if kw else state
                self.assertEqual(self.app._is_anyone_home(), home)
                self.assertEqual(self.app._is_everyone_away(), away)

    def test_one_resident_home_blocks_all_away(self):
        self.app.presence_entities = ['person.simon', 'person.other']
        self.app.get_state = lambda entity, **kw: {} if kw else ('home' if entity == 'person.other' else 'not_home')
        self.assertTrue(self.app._is_anyone_home())
        self.assertFalse(self.app._is_everyone_away())

    def test_no_residents_cannot_mean_everyone_away(self):
        self.app.presence_entities = []
        self.assertFalse(self.app._is_everyone_away())

    def test_missing_selection_helper_uses_configured_residents(self):
        self.app.selected_presence_entities_entity = 'input_text.people'
        for helper_state in (None, '', 'unknown', 'unavailable'):
            with self.subTest(state=helper_state):
                self.app.get_state = lambda entity, **kw: {} if kw else (helper_state if entity == 'input_text.people' else 'home')
                self.assertEqual(self.app._current_presence_entities(), ['person.simon'])
                self.assertTrue(self.app._is_anyone_home())

    def test_observed_departure_unknown_and_return_never_enter_action_paths(self):
        from datetime import datetime
        from unittest.mock import Mock
        self.app.away_since = None
        self.app.datetime = lambda: datetime(2026, 10, 10, 17)
        self.app._evaluate_travel_mode = Mock()
        self.app._publish_dashboard_state = Mock()
        for name in ('_check_cleaning', '_abort_cleaning', '_send_return_notification', '_save_state'):
            setattr(self.app, name, Mock(side_effect=AssertionError(name)))
        for old, new, away in [('home', 'not_home', True), ('not_home', 'unknown', False),
                              ('unknown', 'not_home', True), ('not_home', 'home', False)]:
            with self.subTest(state=new):
                self.app.get_state = lambda entity, **kw: {} if kw else new
                self.app._on_presence_change('person.simon', 'state', old, new, {})
                self.assertEqual(self.app.away_since is not None, away)
        self.assertEqual(self.app._publish_dashboard_state.call_count, 4)


class DashboardSafety(unittest.TestCase):
    def test_idle_connection_does_not_block_observation_response(self):
        import socket
        import threading
        import urllib.request
        import urllib.error
        from unittest.mock import patch
        source = Path(__file__).resolve().parents[1] / 'addon/ghostvacuum/redirect_dashboard.py'
        spec = importlib.util.spec_from_file_location('dashboard_connections', source)
        dashboard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(dashboard)
        server = dashboard.DashboardHTTPServer(('127.0.0.1', 0), dashboard.DashboardHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        idle = socket.create_connection(server.server_address, timeout=2)
        try:
            with patch.object(dashboard, 'load_options', return_value={'observation_mode': True}):
                request = urllib.request.Request(
                    f'http://127.0.0.1:{server.server_port}/api/toggle?entity_id=input_boolean.vacuum_automation_enabled', data=b'{}')
                with self.assertRaises(urllib.error.HTTPError) as result:
                    urllib.request.urlopen(request, timeout=2)
                self.assertEqual(result.exception.code, 403)
        finally:
            idle.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_post_endpoints_are_blocked_before_dispatch(self):
        from unittest.mock import patch
        source = Path(__file__).resolve().parents[1] / 'addon/ghostvacuum/redirect_dashboard.py'
        spec = importlib.util.spec_from_file_location('dashboard', source)
        dashboard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(dashboard)
        for endpoint in ('/api/start_room', '/api/stop_cleaning', '/api/toggle?entity_id=input_boolean.vacuum_automation_enabled',
                         '/api/recreate_helpers', '/api/cleanup_helpers'):
            handler = object.__new__(dashboard.DashboardHandler)
            handler.path = endpoint
            replies = []
            handler._json_response = lambda payload, status=200: replies.append((payload,status))
            with patch.object(dashboard, 'load_options', return_value={'observation_mode': True}):
                handler.do_POST()
            self.assertEqual(replies[0][1], 403)

class MovaDataMapping(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).resolve().parents[1] / 'addon/ghostvacuum/redirect_dashboard.py'
        spec = importlib.util.spec_from_file_location('dashboard_mapping', source)
        self.dashboard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.dashboard)

    def test_appdaemon_zero_false_and_unknown_diagnostics_survive(self):
        import json
        expected = {'travel_mode_active': False, 'travel_time_min': 0,
                    'presence_summary': [{'travel_time_min': 0, 'distance_km': 0}],
                    'away_since': None}
        state = {'attributes': {'travel_mode_active': 'true',
                                'diagnostics_json': json.dumps(expected)}}
        actual = self.dashboard.decode_status_attributes(state)
        for key, value in expected.items():
            self.assertEqual(actual[key], value)

    def test_native_mova_room_ids_are_detected(self):
        attrs = {'rooms': {'Karte 1': [{'id': 1, 'name': 'Büro'}, {'id': 9, 'name': 'Küche'}]},
                 'maps': [{'id': 99, 'name': 'Karte 1'}]}
        self.assertEqual(self.dashboard.extract_vacuum_segments(attrs),
                         [{'segment_id': 1, 'name': 'Büro'}, {'segment_id': 9, 'name': 'Küche'}])

    def test_bagless_fixed_water_does_not_show_missing_consumables(self):
        summary = {'options': {'dust_collection_type': 'container', 'water_connection': True},
                   'states': {'robot': {'signals': {
                       'dust_bag_status': {'state': 'not_installed'},
                       'clean_water_tank_status': {'state': 'not_installed'},
                       'dirty_water_tank_status': {'state': 'not_installed_or_full'},
                       'error': {'state': 'no_error'}}}}}
        robot, station = self.dashboard.build_hero_metrics(summary)
        labels = {m['label'] for m in station}
        self.assertIn('Staubkasten', labels)
        self.assertIn('Festwasseranschluss', labels)
        self.assertNotIn('Staubbeutel', labels)
        self.assertNotIn('Frischwasser', labels)
        self.assertFalse(any(m['class'] == 'error' for m in station))
        self.assertFalse(any(m['label'] == 'Staubbehälter' for m in robot))

if __name__ == '__main__':
    unittest.main()
