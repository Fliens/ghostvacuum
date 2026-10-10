"""Settings may be saved without opening a path to robot commands."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('settings_dashboard', Path(__file__).resolve().parents[1] / 'addon/ghostvacuum/redirect_dashboard.py')
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)

class ObservationSettings(unittest.TestCase):
    options = {'observation_mode': True, 'helper_prefix': 'test', 'rooms': []}

    def test_only_known_configuration_services_allowed(self):
        for domain, service, suffix in [('input_number', 'set_value', 'return_buffer'), ('input_number', 'set_value', 'start_margin'), ('input_text', 'set_value', 'active_weekdays'), ('input_boolean', 'toggle', 'travel_mode_enabled')]:
            self.assertTrue(d.observation_setting_write(f'/services/{domain}/{service}', 'POST', {'entity_id': f'{domain}.test_{suffix}'}, self.options))

    def test_master_override_foreign_helpers_and_robot_commands_blocked(self):
        cases = [('/services/input_boolean/toggle', 'input_boolean.test_enabled'),
                 ('/services/input_boolean/turn_on', 'input_boolean.test_travel_mode_enabled'),
                 ('/services/input_text/set_value', 'input_text.test_one_time_room_override'),
                 ('/services/input_number/set_value', ['input_number.test_return_buffer', 'input_number.foreign']),
                 ('/services/input_number/set_value', 'input_number.foreign'),
                 ('/services/vacuum/start', 'vacuum.willi'), ('/events/vacuum_automation_start_room', '')]
        for path, entity in cases:
            with self.subTest(path=path, entity=entity):
                self.assertFalse(d.observation_setting_write(path, 'POST', {'entity_id': entity}, self.options))
                with patch.object(d, 'load_options', return_value=self.options), patch.object(d.urllib.request, 'urlopen') as send:
                    with self.assertRaises(PermissionError):
                        d.api_request(path, 'POST', {'entity_id': entity})
                    send.assert_not_called()

    def test_tracking_availability_and_visible_choices(self):
        summary = {'options': {'presence_entities': ['person.a'], 'travel_person_entity': 'person.a', 'waze_entity': 'sensor.route'},
                   'available_presence_entities': [{'entity_id': 'person.a', 'has_device': True}],
                   'tracking_sources': {'waze': {'state': '12', 'attributes': {'unit_of_measurement': 'min'}}}}
        choices = {item['id']: item for item in d.tracking_choices(summary)}
        self.assertTrue(choices['local']['available'])
        self.assertTrue(choices['waze']['available'])
        self.assertEqual(set(choices), {'local', 'waze'})
        self.assertEqual(choices['local']['name'], 'Entfernungsschätzung')
        summary['options']['presence_entities'].append('person.b')
        self.assertFalse(d.tracking_choices(summary)[1]['available'])
        summary['tracking_sources']['waze']['state'] = 'unavailable'
        self.assertFalse(d.tracking_choices(summary)[1]['available'])

    def test_settings_handlers_reject_locked_helpers_before_dispatch(self):
        import io
        import json
        for path, entity in [('/api/set_text', 'input_text.test_one_time_room_override'),
                             ('/api/set_number', 'input_number.foreign'),
                             ('/api/toggle?entity_id=input_boolean.test_enabled', '')]:
            handler = object.__new__(d.DashboardHandler)
            handler.path = path
            body = json.dumps({'entity_id': entity, 'value': 1}).encode()
            handler.headers = {'Content-Length': str(len(body))}
            handler.rfile = io.BytesIO(body)
            replies = []
            handler._json_response = lambda payload, status=200: replies.append(status)
            with patch.object(d, 'load_options', return_value=self.options), patch.object(d, 'service_call') as send:
                handler.do_POST()
                self.assertEqual(replies, [403])
                send.assert_not_called()

if __name__ == '__main__':
    unittest.main()
