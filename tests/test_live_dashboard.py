"""Live dashboard maps observed data conservatively and never fabricates a plan."""
import importlib.util
from pathlib import Path
import unittest
from datetime import datetime, timedelta, timezone

path = Path(__file__).resolve().parents[1] / 'addon/ghostvacuum/redirect_dashboard.py'
spec = importlib.util.spec_from_file_location('live_dashboard', path)
dashboard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dashboard)


class LiveDashboard(unittest.TestCase):
    def summary(self):
        return {
            'status': {'observation_mode': 'on', 'anyone_home': 'off', 'everyone_away': 'on',
                       'travel_mode_active': False, 'travel_pause_after_hours': 24,
                       'presence_summary': [{'entity_id': 'person.simon', 'state': 'not_home',
                                             'travel_time_min': 48}], 'room_queue': [], 'room_stats': []},
            'states': {'vacuum': {'state': 'docked', 'attributes': {'battery': 90, 'current_segment': 1}},
                       'sensors': {'return_window': {'state': '38'}}},
            'room_segments': [{'room_key': 'room_1', 'segment_id': 1, 'name': 'Büro'},
                              {'room_key': 'room_2', 'segment_id': 2, 'name': 'Küche'}],
        }

    def test_buffer_is_subtracted_once_and_disabled_rooms_are_explicit(self):
        summary = self.summary()
        summary['states']['global'] = {'return_buffer': {'state': '5.0'}}
        summary['states']['rooms'] = [{'id': 'room_1', 'enabled_state': {'state': 'off'}},
                                      {'id': 'room_2', 'enabled_state': {'state': 'on'}}]
        model = dashboard.build_live_dashboard_view(summary)
        self.assertEqual(model['window'], 33)
        self.assertEqual(model['buffer_min'], 5)
        self.assertEqual(model['calculation'], 'Zeitfenster 38 min − Puffer 5 min = 33 min')
        self.assertFalse(model['rooms'][0]['enabled'])
        self.assertTrue(model['rooms'][1]['enabled'])
        summary['status']['presence_summary'][0]['state'] = 'home'
        self.assertEqual(dashboard.build_live_dashboard_view(summary)['window'], 0)

    def test_analysis_hypothetical_decision_does_not_override_live_reason(self):
        summary = self.summary()
        summary['status']['observation_mode'] = False
        summary['states']['global'] = {'enabled': {'state': 'on'}}
        summary['status']['presence_summary'][0]['state'] = 'home'
        summary['status']['return_analysis'] = {'decision': 'Würde warten', 'detail': 'Person zuhause'}
        model = dashboard.build_live_dashboard_view(summary)
        self.assertTrue(model['automation_enabled'])
        self.assertEqual(model['reason'], 'Person zuhause')
        self.assertEqual(model['caption'], 'Keine Reinigung')
        self.assertEqual(model['window'], 0)

    def test_due_badge_thresholds_and_unknown_history(self):
        now = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
        for hours, level in [(0, 'neutral'), (24, 'neutral'), (24.01, 'warning'), (47.99, 'warning'), (48, 'danger'), (72, 'danger')]:
            badge = dashboard.room_due_badge((now - timedelta(hours=hours)).isoformat(), now)
            self.assertEqual(badge['level'], level)
        self.assertEqual(dashboard.room_due_badge(None, now), {'label':'Fällig', 'level':'neutral'})
        self.assertEqual(dashboard.room_due_badge((now-timedelta(days=2)).isoformat(), now)['label'], '2 Tage überfällig')

    def test_live_interval_uses_current_helper_over_status_snapshot(self):
        summary = self.summary()
        summary['states']['rooms'] = [{'id':'room_1', 'interval_h_state':{'state':'48'}}]
        model = dashboard.build_live_dashboard_view(summary)
        self.assertEqual(model['rooms'][0]['interval_h'], 48)
        self.assertEqual(model['rooms'][0]['due_badge']['label'], 'Fällig')

    def test_duration_display_uses_learned_values_and_never_configured_placeholder(self):
        summary = self.summary()
        summary['status']['room_stats'] = [{'room_key':'room_1', 'configured_duration_min':1, 'effective_duration_min':1}]
        model = dashboard.build_live_dashboard_view(summary)
        self.assertIsNone(model['rooms'][0]['duration'])
        summary['status']['room_stats'][0]['learned_duration_min'] = 12
        self.assertEqual(dashboard.build_live_dashboard_view(summary)['rooms'][0]['duration'], 12)
        html = dashboard.render_settings_rooms(summary)
        self.assertNotIn('konfiguriert', html)
        self.assertNotIn('gelernt', html)

    def test_buffer_settings_render_as_integer(self):
        html = dashboard.render_setting_number('return_buffer', 'Puffer', '', 'input_number.buffer', {'state': '5.0'})
        self.assertIn('value="5"', html)
        self.assertIn('step="1"', html)

    def test_home_blocks_window_and_idle_segment_is_not_active(self):
        summary = self.summary()
        summary['status']['presence_summary'][0]['state'] = 'home'
        model = dashboard.build_live_dashboard_view(summary)
        self.assertEqual(model['window'], 0)
        self.assertIsNone(model['robot']['origin_label'])
        self.assertEqual(model['caption'], 'Keine Reinigung')
        self.assertEqual([r['group'] for r in model['rooms']], ['rest', 'rest'])
        self.assertTrue(model['commands_locked'])

    def test_observation_does_not_display_stale_queue_or_invent_history(self):
        summary = self.summary()
        summary['status']['room_queue'] = [{'room_key': 'room_1', 'enabled': True, 'forecast_score': 9}]
        model = dashboard.build_live_dashboard_view(summary)
        self.assertTrue(all(r['group'] == 'rest' for r in model['rooms']))
        self.assertTrue(all(r['last'] is None and r['next_due'] is None for r in model['rooms']))
        self.assertEqual(model['window'], 38)

    def test_unknown_presence_never_offers_time(self):
        summary = self.summary()
        summary['status']['presence_summary'][0]['state'] = 'unavailable'
        self.assertIsNone(dashboard.build_live_dashboard_view(summary)['window'])

    def test_string_false_does_not_activate_travel_mode(self):
        summary = self.summary()
        summary['status']['travel_mode_active'] = 'false'
        summary['status']['presence_summary'][0]['travel_mode_active'] = 'false'
        model = dashboard.build_live_dashboard_view(summary)
        self.assertEqual(model['window'], 38)
        self.assertEqual(model['people'][0]['kind'], 'away')

    def test_individual_travel_timer_and_active_travel(self):
        summary = self.summary()
        person = summary['status']['presence_summary'][0]
        person.update(outside_since=(datetime.now(timezone.utc) - timedelta(hours=22)).isoformat(), outside_radius=True)
        model = dashboard.build_live_dashboard_view(summary)
        self.assertIn('Reisemodus in 2 h', model['people'][0]['countdown'])
        person['travel_mode_active'] = True
        summary['status']['travel_mode_active'] = True
        model = dashboard.build_live_dashboard_view(summary)
        self.assertEqual(model['window'], 0)
        self.assertEqual(model['people'][0]['detail'], 'Reisemodus')

    def test_cleaning_uses_current_segment_and_keeps_each_room_once(self):
        summary = self.summary()
        summary['states']['vacuum']['state'] = 'cleaning'
        model = dashboard.build_live_dashboard_view(summary)
        self.assertEqual(model['robot']['active_name'], 'Büro')
        self.assertEqual(model['robot']['origin_label'], 'Manuell / extern gestartet')
        summary['states']['vacuum']['state'] = 'paused'
        self.assertEqual(dashboard.build_live_dashboard_view(summary)['robot']['origin_label'], 'Manuell / extern gestartet')
        self.assertEqual(sum(r['group'] == 'active' for r in model['rooms']), 1)
        self.assertEqual(len({r['id'] for r in model['rooms']}), 2)
        self.assertIsNone(model['robot']['remaining'])

    def test_html_escapes_script_in_sensor_names_and_contains_no_demo(self):
        summary = self.summary()
        summary['room_segments'][0]['name'] = '</script><script>alert(1)</script>'
        output = dashboard.render_live_observation_dashboard(summary)
        self.assertIn('data-live-observation="true"', output)
        self.assertNotIn('</script><script>alert(1)', output)
        self.assertNotIn('Beispieldaten', output)
        self.assertNotIn('data-scenario', output)
        self.assertIn('draggable="false"', output)

    def test_live_mode_unlocks_commands_without_turning_on_automation(self):
        summary = self.summary()
        summary['status']['observation_mode'] = 'off'
        summary['states']['global'] = {'enabled': {'state': 'off'}}
        model = dashboard.build_live_dashboard_view(summary)
        self.assertFalse(model['commands_locked'])
        self.assertFalse(model['automation_enabled'])
        summary['states']['global']['enabled']['state'] = 'on'
        self.assertTrue(dashboard.build_live_dashboard_view(summary)['automation_enabled'])

    def test_modern_dashboard_remains_in_live_mode(self):
        from unittest.mock import patch
        summary = self.summary()
        summary['status']['observation_mode'] = 'off'
        with patch.object(dashboard,'build_summary',return_value=summary):
            html = dashboard.render_dashboard_html()
        self.assertIn('data-live-observation="true"',html)
        self.assertIn('pause-cleaning',html)
        self.assertIn('"commands_locked": false',html)
