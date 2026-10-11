from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from test_observation_mode import module

class ReturnPolicy(unittest.TestCase):
    def setUp(self):
        self.a = module.VacuumAutomation()
        a = self.a
        a.args = {'observation_mode': True}
        a.datetime = lambda aware=False: datetime(2026,10,10,12,tzinfo=timezone.utc)
        a._current_return_buffer_min = lambda: 5
        a._current_start_margin_min = lambda: 5
        a._current_start_hour = lambda: 8
        a._current_end_hour = lambda: 22
        a.travel_mode_active = False
        a.active_room = None
        a.history_entries = []
        a.rooms = {'office':{'segment_id':1}}
        a._room_enabled = lambda r: True
        a._room_effective_duration_min = lambda r: 40
        a._room_weight = lambda r: 1
        a._room_due_info = lambda r: {"is_due":True,"score":1}
        a._one_time_room_override = lambda: None
        a._robot_start_blockers = lambda s=None: []
        a._presence_summary = lambda: self.people
        self.people = [{'presence':'away','travel_time_min':15}]
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        a.storage_dir = Path(self.temp.name)
        a.log = Mock()
        a._load_return_analysis()

    def test_start_and_stop_boundaries_have_hysteresis(self):
        for eta,start,continuing in [(4,False,False),(5,False,False),(6,False,True),(9,False,True),(10,True,True),(20,True,True)]:
            self.people[0]['travel_time_min'] = eta
            self.assertEqual(self.a._return_policy()['allowed'],start)
            self.assertEqual(self.a._return_policy(True)['allowed'],continuing)

    def test_nearest_person_home_missing_and_schedule_override_distance(self):
        self.people.append({'presence':'away','travel_time_min':3})
        self.assertFalse(self.a._return_policy(True)['allowed'])
        self.people[-1] = {'presence':'home','travel_time_min':0}
        self.assertEqual(self.a._return_policy()['reason'],'Person zuhause')
        self.people[-1] = {'presence':'away','travel_time_min':None}
        self.assertFalse(self.a._return_policy()['allowed'])
        self.people.pop()
        self.a.datetime = lambda aware=False: datetime(2026,10,10,21,56,tzinfo=timezone.utc)
        self.assertFalse(self.a._return_policy(True)['allowed'])
        self.a.args['active_weekdays_entity'] = 'input_text.days'
        self.a.get_state = lambda e: '0,1'
        self.assertFalse(self.a._return_policy()['allowed'])

    def test_named_weekdays_match_settings_helpers(self):
        self.a.args['active_weekdays_entity'] = 'input_text.days'
        self.a.get_state = lambda e: 'mon,tue,wed,thu,fri,sat'
        self.assertTrue(self.a._return_policy()['allowed'])
        self.a.get_state = lambda e: 'mon,tue,wed,thu,fri'
        self.assertFalse(self.a._return_policy()['allowed'])

    def test_long_room_can_start_in_short_window(self):
        self.assertEqual(self.a._select_next_room(10),'office')
        self.assertIsNone(self.a._select_next_room(9))

    def test_20_min_buffer_blocks_15_min_eta_and_analysis_compares(self):
        self.a._current_return_buffer_min = lambda: 20
        report = self.a._return_analysis()
        self.assertEqual(report['below_buffer_samples'],1)
        counts = {v['buffer_min']:v['start_samples'] for v in report['simulations']}
        self.assertEqual(counts[20],0)
        self.assertEqual(counts[10],1)
        self.assertEqual(self.a._return_analysis()['samples'],1)
        self.a.datetime = lambda aware=False: datetime(2026,10,10,14,tzinfo=timezone.utc)
        self.assertEqual(self.a._return_analysis()['samples'],2)
        self.a._load_return_analysis()
        self.assertEqual(self.a._return_analysis()['samples'],2)
        self.a.log.assert_not_called()

    def test_observer_decision_does_not_create_jobs_or_history(self):
        before=list(self.a.history_entries)
        self.assertEqual(self.a._return_analysis({'started':False})['decision'],'Würde starten')
        self.assertEqual(self.a.history_entries,before)
        self.assertIsNone(self.a.active_room)
        self.assertFalse(self.a._can_start_cleaning())
        self.assertEqual(self.a._return_analysis({'started':True})['decision'],'Extern gestartete Reinigung')

    def test_partial_rule_never_uses_unknown_or_time_estimates(self):
        self.a._room_completion_percent = lambda r: 80
        self.assertFalse(self.a._counts_as_cleaned('office','aborted'))
        self.assertFalse(self.a._counts_as_cleaned('office','aborted',79))
        self.assertTrue(self.a._counts_as_cleaned('office','aborted',80))
        self.assertFalse(self.a._counts_as_cleaned('office','aborted',float('nan')))
        self.a._room_completion_percent = lambda r: 100
        self.assertFalse(self.a._counts_as_cleaned('office','aborted',100))
        self.assertTrue(self.a._counts_as_cleaned('office','completed'))

if __name__ == '__main__': unittest.main()

class SingleRoomProgress(unittest.TestCase):
    def test_only_fresh_progress_of_accepted_single_room_counts(self):
        from test_robot_state import snapshot
        job = module.VacuumJobTracker(1, 23, 1000, 900)
        data = snapshot()
        data.update(phase='cleaning', task_status='segment_cleaning', active_segments=[1],
                    progress_percent=95, progress_updated_at=999)
        job.observe(data,1001)
        job.observe(data,1002)
        self.assertIsNone(job.room_progress_percent)
        data.update(progress_percent=80,progress_updated_at=1003)
        job.observe(data,1004)
        self.assertEqual(job.room_progress_percent,80)
        data.update(phase='returning',progress_percent=100,progress_updated_at=1005)
        job.observe(data,1006)
        self.assertEqual(job.room_progress_percent,80)
        data.update(phase='cleaning',active_segments=[1,9],progress_updated_at=1007)
        job.observe(data,1008)
        self.assertEqual(job.problem,'different_room_task')
        self.assertEqual(job.room_progress_percent,80)

    def test_partial_credit_requires_threshold_and_confirmed_terminal_task(self):
        from test_robot_state import JobCorrelation
        suite = JobCorrelation(); suite.setUp(); suite.accept()
        suite.data.update(progress_percent=82,progress_updated_at=suite.now+11)
        suite.job.observe(suite.data,suite.now+12)
        suite.terminal()
        suite.data['history'][0]['completed'] = False
        app=suite.make_app()
        app._room_completion_percent=lambda room:80
        app._observe_active_job()
        self.assertIn('office',app.last_cleaned)
        self.assertEqual(app._vacuum_job_problem,None)
