from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_observation_mode import module


class PresenceLogic(unittest.TestCase):
    def setUp(self):
        self.app = module.VacuumAutomation()
        self.app.args = {'observation_mode': True}
        self.app.presence_entities = ['person.a', 'person.b']
        self.app.selected_presence_entities_entity = None
        self.app.home_zone = 'zone.home'
        self.app.fallback_speed_entity = None
        self.app.fallback_speed_kmh = 5
        self.app.travel_pause_radius_entity = None
        self.app.travel_pause_radius_km = 10
        self.app.travel_pause_after_hours = 2
        self.app.travel_mode_enabled_entity = None
        self.app.travel_mode_enabled = True
        self.app.outside_since = {}
        self.app._presence_timer_signature = None
        self.app.log = lambda *a, **k: None
        self.now = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
        self.app.datetime = lambda aware=False: self.now
        self.states = {
            'zone.home': {'state': '0', 'attributes': {'latitude': 52, 'longitude': 13}},
            'zone.office': {'state': '0', 'attributes': {'friendly_name': 'Büro'}},
        }
        self.person('person.a', 'not_home', 52.2)
        self.person('person.b', 'not_home', 52.3)
        self.app.get_state = self.get_state
        self.clock = patch.object(module.time, 'time', side_effect=lambda: self.now.timestamp())
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def get_state(self, entity=None, attribute=None):
        if entity == 'zone':
            return {k: v for k, v in self.states.items() if k.startswith('zone.')}
        value = self.states.get(entity)
        return value if attribute == 'all' else value.get('state') if value else None

    def person(self, entity, state, latitude):
        self.states[entity] = {'state': state, 'last_reported': self.now.isoformat(),
                              'attributes': {'latitude': latitude, 'longitude': 13}}

    def test_tracking_selection_uses_only_assigned_person_and_valid_units(self):
        self.app.tracking_mode_entity = 'input_text.tracking'
        self.app.travel_person_entity = 'person.a'
        self.app.waze_entity = 'sensor.return_time'
        self.app.distance_entity = 'sensor.return_distance'
        self.states['input_text.tracking'] = {'state': 'waze'}
        self.states['sensor.return_time'] = {'state': '120', 'last_reported': self.now.isoformat(), 'attributes': {'unit_of_measurement': 's'}}
        self.assertEqual(self.app._person_presence_summary('person.a')['travel_time_min'], 2)
        self.assertIsNone(self.app._person_presence_summary('person.b')['travel_time_min'])
        self.assertEqual(self.app._get_travel_time_minutes(), 0)
        self.states['sensor.return_time']['state'] = 'unavailable'
        self.assertIsNone(self.app._person_presence_summary('person.a')['travel_time_min'])
        self.states['sensor.return_time']['state'] = '120'
        self.states['sensor.return_time']['last_reported'] = (self.now - timedelta(hours=2)).isoformat()
        self.assertIsNone(self.app._person_presence_summary('person.a')['travel_time_min'])
        self.states['input_text.tracking']['state'] = 'distance'
        self.states['sensor.return_distance'] = {'state': '1000', 'last_reported': self.now.isoformat(), 'attributes': {'unit_of_measurement': 'm'}}
        expected = self.app._person_presence_summary('person.a')['travel_time_min']
        self.assertEqual(self.app._current_tracking_mode(), 'local')
        self.assertGreater(expected, 12)
        self.states['sensor.return_distance']['attributes']['unit_of_measurement'] = '%'
        self.assertEqual(self.app._person_presence_summary('person.a')['travel_time_min'], expected)
        self.assertIsNotNone(self.app._person_presence_summary('person.b')['travel_time_min'])
        self.states['input_text.tracking']['state'] = 'local'
        self.assertIsNotNone(self.app._person_presence_summary('person.b')['travel_time_min'])

    def advance(self, hours):
        for _ in range(int(hours * 60)):
            self.now += timedelta(minutes=1)
            for key, value in self.states.items():
                if key.startswith('person.'):
                    value['last_reported'] = self.now.isoformat()
            self.app._evaluate_travel_mode()

    def test_all_individual_timers_must_expire_and_reentry_disables(self):
        self.person('person.b', 'home', 52)
        self.app._evaluate_travel_mode()
        self.advance(1)
        self.person('person.b', 'not_home', 52.3)
        self.app._evaluate_travel_mode()
        self.advance(1)
        self.app._evaluate_travel_mode()
        self.assertFalse(self.app.travel_mode_active)
        self.advance(1)
        self.app._evaluate_travel_mode()
        self.assertTrue(self.app.travel_mode_active)
        # Re-entering the radius is sufficient, even before reaching home.
        self.person('person.b', 'not_home', 52.01)
        self.app._evaluate_travel_mode()
        self.assertFalse(self.app.travel_mode_active)
        self.assertNotIn('person.b', self.app.outside_since)

    def test_earliest_return_and_home_take_precedence(self):
        self.assertGreater(self.app._get_travel_time_minutes(), 0)
        times = [p['travel_time_min'] for p in self.app._presence_summary()]
        self.assertEqual(self.app._get_travel_time_minutes(), min(times))
        self.person('person.b', 'home', 52)
        self.assertEqual(self.app._get_travel_time_minutes(), 0)
        self.assertFalse(self.app._is_everyone_away())

    def test_known_named_zone_away_arbitrary_text_unknown(self):
        self.person('person.a', 'Büro', 52.2)
        self.assertTrue(self.app._is_everyone_away())
        self.person('person.a', 'some invalid state', 52.2)
        self.assertFalse(self.app._is_everyone_away())
        self.assertIsNone(self.app._presence_summary()[0]['travel_time_min'])

    def test_unknown_and_stale_position_reset_timer_and_window(self):
        self.app._evaluate_travel_mode()
        self.person('person.b', 'unknown', 52.3)
        self.app._evaluate_travel_mode()
        self.assertNotIn('person.b', self.app.outside_since)
        self.assertEqual(self.app._get_travel_time_minutes(), 0)
        self.person('person.b', 'not_home', 52.3)
        self.states['person.b']['last_reported'] = (self.now - timedelta(hours=2)).isoformat()
        self.assertIsNone(self.app._presence_summary()[1]['distance_km'])
        self.assertEqual(self.app._get_travel_time_minutes(), 0)

    def test_new_person_does_not_inherit_timer_and_removed_person_disappears(self):
        self.app._evaluate_travel_mode()
        self.advance(2)
        self.app._evaluate_travel_mode()
        self.assertTrue(self.app.travel_mode_active)
        self.app.presence_entities.append('person.c')
        self.person('person.c', 'not_home', 52.4)
        self.app._evaluate_travel_mode()
        self.assertFalse(self.app.travel_mode_active)
        self.app.presence_entities.remove('person.c')
        self.app._evaluate_travel_mode()
        self.assertTrue(self.app.travel_mode_active)
        self.assertNotIn('person.c', self.app.outside_since)

    def test_radius_change_restarts_observation_and_no_hard_distance_shortcut(self):
        self.app.max_distance_km = 1
        self.app._evaluate_travel_mode()
        self.assertFalse(self.app.travel_mode_active)
        self.advance(2)
        self.app.travel_pause_radius_km = 15
        self.app._evaluate_travel_mode()
        self.assertFalse(self.app.travel_mode_active)

    def test_nonfinite_and_zero_speed_do_not_create_estimate(self):
        for value in (0, float('nan'), float('inf')):
            self.app.fallback_speed_kmh = value
            self.assertEqual(self.app._get_travel_time_minutes(), 0)
        self.app.fallback_speed_kmh = 5
        self.states['person.a']['attributes']['latitude'] = float('nan')
        self.assertEqual(self.app._get_travel_time_minutes(), 0)

    def test_timer_survives_short_restart_but_not_unobserved_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            self.app.storage_dir = Path(directory)
            self.app._load_presence_timers()
            self.app._evaluate_travel_mode()
            expected = dict(self.app.outside_since)
            self.app.outside_since = {}
            self.app._load_presence_timers()
            self.assertEqual(self.app.outside_since, expected)
            self.now += timedelta(hours=1)
            self.app.outside_since = {}
            self.app._load_presence_timers()
            self.assertEqual(self.app.outside_since, {})

    def test_observation_gap_and_imprecise_gps_do_not_activate_travel(self):
        self.app._evaluate_travel_mode()
        self.now += timedelta(hours=3)
        for key in ('person.a', 'person.b'):
            self.states[key]['last_reported'] = self.now.isoformat()
        self.app._evaluate_travel_mode()
        self.assertFalse(self.app.travel_mode_active)
        self.states['person.a']['attributes']['gps_accuracy'] = 5000
        self.assertEqual(self.app._get_travel_time_minutes(), 0)


class PresenceConfiguration(unittest.TestCase):
    def test_configuration_preserves_observer_and_changes_both_files(self):
        import importlib.util
        import yaml
        path = Path(__file__).resolve().parents[1] / 'scripts/configure_presence.py'
        spec = importlib.util.spec_from_file_location('presence_config', path)
        config = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(config)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'apps').mkdir()
            (root / 'options.yaml').write_text('observation_mode: true\n')
            (root / 'apps/apps.yaml').write_text('vacuum_automation:\n  observation_mode: true\n')
            result = config.configure(root, ['person.a', 'person.b'], 20, 6, 5)
            self.assertTrue(result['observation_mode'])
            self.assertEqual(result['presence_entities'], ['person.a', 'person.b'])
            app = yaml.safe_load((root / 'apps/apps.yaml').read_text())['vacuum_automation']
            self.assertEqual(app['travel_pause_after_hours'], 6)
            with self.assertRaises(ValueError):
                config.configure(root, [], 20, 6, 5)


if __name__ == '__main__':
    unittest.main()
