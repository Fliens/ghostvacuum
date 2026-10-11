"""MOVA TASK_TYPE subcycles must never satisfy a room's percentage rule."""
import unittest
from test_observation_mode import module
from test_robot_state import snapshot


class SpecialProgress(unittest.TestCase):
    def setUp(self):
        self.job = module.VacuumJobTracker(9, 23, 1000, 900)
        self.data = snapshot()
        self.data.update(phase='cleaning', task_status='room_cleaning', active_segments=[9],
                         task_changed_at=1001, task_type='smart', task_type_changed_at=1001)
        self.job.observe(self.data, 1002)

    def progress(self, value, now, kind='smart', changed=1001):
        self.data.update(progress_percent=value, progress_updated_at=now,
                         task_type=kind, task_type_changed_at=changed)
        self.job.observe(self.data, now)

    def test_subcycle_100_never_counts_as_room_100(self):
        self.progress(41, 1020)
        self.progress(100, 1060, 'intensive_stain_cleaning', 1040)
        self.assertEqual(self.job.room_progress_percent, 41)
        self.assertEqual(self.job.special_progress_percent, 100)
        app = module.VacuumAutomation()
        app._room_completion_percent = lambda room: 80
        self.assertFalse(app._counts_as_cleaned('kitchen', 'aborted', self.job.room_progress_percent))

    def test_actual_live_transition_rejects_lingering_95_then_accepts_38(self):
        self.progress(41, 1020)
        self.progress(8, 1060, 'large_particle_cleaning', 1040)
        self.progress(95, 1071, 'boosted_edge_cleaning', 1070)
        self.assertEqual(self.job.room_progress_percent, 41)
        self.progress(38, 1090, 'smart', 1075)
        self.assertEqual(self.job.room_progress_percent, 38)
        self.assertIsNone(self.job.special_progress_percent)

    def test_restart_during_stain_keeps_separate_values(self):
        self.progress(41, 1020)
        self.progress(85, 1060, 'stain_cleaning', 1040)
        self.job = module.VacuumJobTracker.restore(self.job.checkpoint())
        self.assertEqual(self.job.room_progress_percent, 41)
        self.assertEqual(self.job.special_progress_percent, 85)
        self.progress(100, 1061, 'stain_cleaning', 1040)
        self.assertEqual(self.job.room_progress_percent, 41)
        self.progress(42, 1100, 'smart', 1080)
        self.assertEqual(self.job.room_progress_percent, 42)

    def test_unknown_or_missing_phase_does_not_credit_progress(self):
        self.progress(100, 1020, 'unknown')
        self.assertIsNone(self.job.room_progress_percent)
        self.progress(100, 1030, changed=None)
        self.assertIsNone(self.job.room_progress_percent)

    def test_legacy_checkpoint_discards_potentially_contaminated_maximum(self):
        old = self.job.checkpoint()
        old.pop('progress_schema')
        old['room_progress_percent'] = 100
        self.assertIsNone(module.VacuumJobTracker.restore(old).room_progress_percent)

    def test_history_start_after_initial_mop_wash_can_confirm_completion(self):
        self.data.update(phase='docked', task_status='completed', task_changed_at=1400,
                         active_segments=[], started=False, running=False,
                         history=[{'timestamp': 1180, 'completed': True}])
        self.assertEqual(self.job.observe(self.data, 1410), 'completed')

    def test_new_same_room_task_cannot_replace_accepted_task(self):
        self.data['task_changed_at'] = 1040
        self.assertEqual(self.job.observe(self.data, 1041), 'different_task_instance')

    def test_confirmed_history_finishes_already_during_return_or_wash(self):
        for phase in ('returning', 'washing', 'draining', 'drying'):
            self.setUp()
            self.data.update(phase=phase, task_status='completed', task_changed_at=1400,
                             active_segments=[], started=False, running=False,
                             history=[{'timestamp': 1180, 'completed': True}])
            self.assertEqual(self.job.observe(self.data, 1410), 'completed')
            self.assertEqual(self.job.finished_at, 1400)

    def test_returning_without_confirmed_history_is_not_completion(self):
        self.data.update(phase='returning', task_status='completed', task_changed_at=1400,
                         active_segments=[], started=False, running=False)
        self.assertNotEqual(self.job.observe(self.data, 1410), 'completed')

    def test_epoch_conversion_uses_appdaemon_timezone_not_container_timezone(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        app = module.VacuumAutomation()
        wall = datetime(2026, 10, 11, 12, 11, 2)
        app.datetime = lambda aware=False: wall.replace(tzinfo=ZoneInfo('Europe/Berlin')) if aware else wall
        self.assertEqual(app._datetime_to_ts(wall), 1791713462)
        self.assertEqual(app._ts_to_datetime(1791713462), wall)
