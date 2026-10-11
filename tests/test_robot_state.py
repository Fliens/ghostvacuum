from copy import deepcopy
from datetime import datetime, timezone
import unittest
from unittest.mock import Mock, patch

from test_observation_mode import module


NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)


def snapshot():
    return {
        'phase': 'docked', 'task_status': 'completed', 'task_changed_at': NOW.timestamp() - 100,
        'station_status': 'idle', 'battery': 90, 'located': True, 'has_saved_map': True,
        'map_id': 23, 'room_ids': [1, 9], 'started': False, 'running': False,
        'active_segments': [], 'history': [],
        'dnd': [{'enabled': True, 'start': '22:00', 'end': '08:00', 'repeats': 127}],
    }


class RobotState(unittest.TestCase):
    def setUp(self):
        self.app = module.VacuumAutomation()
        self.app.args = {'observation_mode': True}
        self.app.vacuum_entity = 'vacuum.willi'
        self.app.rooms = {'office': {'segment_id': 1}}
        self.app.datetime = lambda aware=False: NOW
        self.app._vacuum_job_problem = None
        self.states = {
            'vacuum.willi': {'state': 'docked', 'last_reported': NOW.isoformat(), 'attributes': {
                'docked': True, 'has_error': False, 'started': False, 'running': False,
                'located': True, 'has_saved_map': True, 'selected_map_id': 23,
                'selected_map': 'Map 1', 'rooms': {'Map 1': [{'id': 1}, {'id': 9}]},
                'dnd': [], 'active_segments': None,
            }},
        }
        for suffix, state in [('state', 'charging_completed'), ('task_status', 'completed'),
                              ('error', 'no_error'), ('self_wash_base_status', 'idle'),
                              ('battery_level', '90'), ('cleaning_history', NOW.isoformat())]:
            self.states['sensor.willi_' + suffix] = {
                'state': state, 'attributes': {}, 'last_changed': NOW.isoformat()}
        self.app.get_state = lambda entity, attribute=None: (
            self.states.get(entity) if attribute == 'all' else
            self.states.get(entity, {}).get('state'))
        self.clock = patch.object(module.time, 'time', return_value=NOW.timestamp())
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def test_docked_robot_ready_does_not_mean_automation_enabled(self):
        self.assertEqual(self.app._robot_snapshot()['phase'], 'docked')
        self.assertEqual(self.app._robot_start_blockers(), [])
        self.assertFalse(self.app._can_start_cleaning())
        self.app._start_cleaning('office')  # Must return before touching room/job state.
        self.assertFalse(hasattr(self.app, 'active_room'))

    def test_station_visits_and_pause_have_precedence_over_docked(self):
        attrs = self.states['vacuum.willi']['attributes']
        for flag, phase in [('washing', 'washing'), ('returning_to_wash', 'returning_to_wash'),
                            ('drying', 'drying'), ('paused', 'paused'), ('draining', 'draining')]:
            with self.subTest(flag=flag):
                attrs[flag] = True
                self.assertEqual(self.app._robot_snapshot()['phase'], phase)
                self.assertTrue(self.app._robot_start_blockers())
                attrs[flag] = False

    def test_stale_missing_and_faulted_robot_block_start(self):
        for state in ['unknown', 'unavailable', 'error']:
            self.states['vacuum.willi']['state'] = state
            self.assertTrue(self.app._robot_start_blockers())
        self.states['vacuum.willi']['state'] = 'docked'
        self.states['vacuum.willi']['last_reported'] = '2026-10-09T12:00:00+00:00'
        self.assertEqual(self.app._robot_snapshot()['phase'], 'stale')

    def test_low_battery_bad_map_running_task_unknown_station_block(self):
        for key, value, reason in [('battery', 29, 'battery_low_or_unknown'),
                                   ('battery', None, 'battery_low_or_unknown'),
                                   ('located', None, 'map_or_location_unknown'),
                                   ('task_status', 'segment_cleaning', 'task_not_finished'),
                                   ('running', True, 'task_active_or_unknown'),
                                   ('station_status', 'unknown', 'station_busy_or_unknown')]:
            with self.subTest(key=key):
                state = snapshot()
                state[key] = value
                self.assertIn(reason, self.app._robot_start_blockers(state))
        self.app.rooms['office']['segment_id'] = 100
        self.assertIn('room_not_on_selected_map', self.app._robot_start_blockers(snapshot(), 'office'))

    def test_quiet_hours_across_midnight_and_missing_schedule(self):
        self.app.datetime = lambda aware=False: NOW.replace(hour=23)
        self.assertIn('quiet_hours', self.app._robot_start_blockers(snapshot()))
        self.app.datetime = lambda aware=False: NOW.replace(hour=7)
        self.assertIn('quiet_hours', self.app._robot_start_blockers(snapshot()))
        self.app.datetime = lambda aware=False: NOW.replace(hour=8)
        self.assertNotIn('quiet_hours', self.app._robot_start_blockers(snapshot()))
        state = snapshot()
        state['dnd'] = None
        self.assertIn('quiet_hours_unknown', self.app._robot_start_blockers(state))


class JobCorrelation(unittest.TestCase):
    def setUp(self):
        self.now = NOW.timestamp()
        self.job = module.VacuumJobTracker(1, 23, self.now, self.now - 300)
        self.data = snapshot()

    def accept(self):
        self.data.update(phase='cleaning', task_status='segment_cleaning',
                         active_segments=[1], started=True, running=True)
        self.assertEqual(self.job.observe(self.data, self.now + 10), 'accepted')

    def terminal(self, completed=True):
        self.data.update(phase='docked', task_status='completed',
                         task_changed_at=self.now + 200, active_segments=[], started=False, running=False)
        self.data['history'] = [{'timestamp': self.now, 'completed': completed}]

    def test_idle_and_old_completed_do_not_confirm_start(self):
        self.assertEqual(self.job.observe(self.data, self.now + 1), 'awaiting_acceptance')
        self.assertEqual(self.job.observe(self.data, self.now + 121), 'acceptance_timeout')
        self.accept_after_timeout_does_not_retry()

    def test_late_first_acceptance_cannot_bypass_timeout(self):
        self.data.update(phase='cleaning', task_status='segment_cleaning', active_segments=[1])
        self.assertEqual(self.job.observe(self.data, self.now + 121), 'acceptance_timeout')

    def accept_after_timeout_does_not_retry(self):
        self.data.update(phase='cleaning', task_status='segment_cleaning', active_segments=[1])
        self.assertEqual(self.job.observe(self.data, self.now + 130), 'acceptance_timeout')

    def test_intermediate_wash_docking_and_pause_never_complete(self):
        self.accept()
        for phase in ['returning_to_wash', 'washing', 'paused', 'returning', 'docked']:
            self.data['phase'] = phase
            self.assertNotEqual(self.job.observe(self.data, self.now + 100), 'completed')

    def test_complete_requires_new_history_success_and_fresh_task_transition(self):
        self.accept()
        self.terminal()
        self.data['history'] = [{'timestamp': self.now - 300, 'completed': True}]
        self.assertNotEqual(self.job.observe(self.data, self.now + 201), 'completed')
        self.terminal()
        self.data['task_changed_at'] = self.now - 100
        self.assertNotEqual(self.job.observe(self.data, self.now + 202), 'completed')
        self.terminal()
        self.assertEqual(self.job.observe(self.data, self.now + 203), 'completed')

    def test_history_reports_incomplete_not_success(self):
        self.accept()
        self.terminal(False)
        self.assertEqual(self.job.observe(self.data, self.now + 201), 'aborted')

    def test_map_change_other_room_other_task_error_and_disconnect_latch(self):
        cases = [('map_id', 42, 'map_changed'), ('active_segments', [9], 'different_room_task'),
                 ('phase', 'error', 'error'), ('phase', 'unavailable', 'unavailable'),
                 ('task_status', 'auto_cleaning', 'different_task_type')]
        for key, value, expected in cases:
            with self.subTest(key=key):
                self.job = module.VacuumJobTracker(1, 23, self.now, self.now - 300)
                self.accept()
                state = deepcopy(self.data)
                state[key] = value
                self.assertEqual(self.job.observe(state, self.now + 11), expected)
                self.assertEqual(self.job.observe(self.data, self.now + 12), expected)

    def make_app(self, observation=False):
        app = module.VacuumAutomation()
        app.args = {'observation_mode': observation}
        app.active_room = 'office'
        app.simulation_mode = False
        app._vacuum_job_problem = None
        app._vacuum_job = self.job
        app.abort_requested = False
        app.datetime = lambda aware=False: NOW.replace(minute=4)
        app.last_cleaned = {}
        app.cleaned_during_absence = []
        app._robot_snapshot = lambda: self.data
        app._record_history_entry = Mock()
        app._save_state = Mock()
        app._publish_dashboard_state = Mock()
        app._can_start_cleaning = lambda: False
        app.call_service = Mock(side_effect=AssertionError('No services during observation of a job'))
        return app

    def test_confirmed_success_updates_once_and_abort_does_not_mark_clean(self):
        for abort in [False, True]:
            self.job = module.VacuumJobTracker(1, 23, self.now, self.now - 300)
            self.accept()
            self.terminal()
            app = self.make_app()
            app.abort_requested = abort
            app._observe_active_job()
            app._observe_active_job()
            app._record_history_entry.assert_called_once_with('office', 'aborted' if abort else 'completed')
            self.assertEqual('office' in app.last_cleaned, not abort)
            app.call_service.assert_not_called()

    def test_observer_never_adopts_or_records_a_real_job(self):
        self.accept()
        self.terminal()
        app = self.make_app(observation=True)
        app._observe_active_job()
        app._record_history_entry.assert_not_called()
        app._save_state.assert_not_called()
        app.call_service.assert_not_called()
        self.assertEqual(app.last_cleaned, {})

    def test_start_notification_waits_for_acceptance_and_is_sent_once(self):
        app = self.make_app()
        app.datetime = lambda aware=False: NOW.replace(second=10)
        app._send_start_notification = Mock()
        app._observe_active_job()
        app._send_start_notification.assert_not_called()
        self.data.update(phase='cleaning', task_status='segment_cleaning', active_segments=[1])
        app._observe_active_job()
        app._observe_active_job()
        app._send_start_notification.assert_called_once_with('office')

    def test_restored_job_requires_review(self):
        app = self.make_app()
        app._vacuum_job = None
        app._observe_active_job()
        self.assertEqual(app._vacuum_job_problem, 'restored_job_unconfirmed')
        app._record_history_entry.assert_not_called()
        app.call_service.assert_not_called()

    def test_failed_request_is_not_retried_or_marked_successful(self):
        app = self.make_app()
        app.rooms = {'office': {'segment_id': 1}}
        app._can_start_cleaning = lambda: app._vacuum_job_problem is None
        app._robot_start_blockers = lambda *a: []
        app._room_effective_duration_min = lambda room: 10
        app._room_configured_duration_min = lambda room: 10
        app._get_travel_time_minutes = lambda: 30
        app._get_distance_km = lambda: 10
        app.vacuum_entity = 'vacuum.willi'
        app._clear_one_time_room_override = Mock()
        app.log = Mock()
        app._send_start_notification = Mock()
        app._prepare_cleaning_profile = lambda room: True
        app._effective_cleaning_profile = lambda room: dict(module.CLEANING_PROFILE_DEFAULT)
        app._start_cleaning('office')  # Mock service raises; no real HA connection.
        self.assertEqual(app._vacuum_job_problem, 'command_failed')
        app._start_cleaning('office')
        self.assertEqual(app.call_service.call_count, 1)
        app._record_history_entry.assert_not_called()
        app._clear_one_time_room_override.assert_not_called()
        self.assertEqual(app.last_cleaned, {})


if __name__ == '__main__':
    unittest.main()

class RestartStateHelper(unittest.TestCase):
    def test_null_or_missing_helper_never_selects_entire_namespace(self):
        for args in ({}, {'state_helper': None}, {'state_helper': ''}):
            with self.subTest(args=args):
                self.assertEqual(module.restart_state_entity(args),'input_text.vacuum_automation_state')
    def test_explicit_helper_and_custom_prefix_are_preserved(self):
        self.assertEqual(module.restart_state_entity({'helper_prefix':'other'}),'input_text.other_state')
        self.assertEqual(module.restart_state_entity({'state_helper':'input_text.custom'}),'input_text.custom')
