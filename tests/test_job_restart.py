import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
import test_robot_state as robot
module=robot.module

class JobRestart(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.storage=Path(self.temp.name)
        self.fixture=robot.JobCorrelation();self.fixture.setUp();self.fixture.accept()
        self.fixture.job.room_progress_percent=41
        self.state={'lc':{},'ar':'office','am':True,'as':self.fixture.now,'ab':False}
        self.write()

    def write(self):
        (self.storage/'active_job.json').write_text(json.dumps({'version':1,'state':self.state,'job':self.fixture.job.checkpoint()}))

    def restore(self):
        a=self.fixture.make_app()
        a.rooms={'office':{'segment_id':1}};a.storage_dir=self.storage
        a.state_helper='input_text.state';a.get_state=lambda entity:''
        a.log=Mock();a._load_state()
        a._send_start_notification=Mock()
        a._save_job_checkpoint=Mock(return_value=True)
        return a

    def test_resume_confirmed_room_preserves_origin_progress_and_sends_no_commands(self):
        a=self.restore();a._observe_active_job()
        self.assertIsNone(a._vacuum_job_problem)
        self.assertTrue(a.active_room_manual)
        self.assertEqual(a._vacuum_job.room_progress_percent,41)
        a.call_service.assert_not_called();a._send_start_notification.assert_not_called()

    def test_changed_map_room_or_task_cannot_resume(self):
        for changes in ({'map_id':24},{'active_segments':[2]}, {'task_changed_at':self.fixture.now+99},{'task_status':'auto_cleaning'}):
            with self.subTest(changes=changes):
                a=self.restore();a._robot_snapshot=lambda:{**self.fixture.data,**changes}
                a._observe_active_job()
                self.assertEqual(a._vacuum_job_problem,'restored_job_mismatch')
                a.call_service.assert_not_called();a._record_history_entry.assert_not_called()

    def test_stale_then_fresh_snapshot_resumes_without_command(self):
        a=self.restore();original=a._robot_snapshot
        a._robot_snapshot=lambda:{**self.fixture.data,'phase':'stale'}
        a._observe_active_job();self.assertTrue(a._restored_job_pending)
        a._robot_snapshot=original;a._observe_active_job()
        self.assertIsNone(a._vacuum_job_problem);a.call_service.assert_not_called()

    def test_completion_during_restart_is_recorded_once_from_new_history(self):
        self.fixture.terminal();a=self.restore()
        a._observe_active_job();a._observe_active_job()
        a._record_history_entry.assert_called_once_with('office','completed', finished_at=robot.datetime.fromtimestamp(self.fixture.data['task_changed_at'], tz=robot.NOW.tzinfo))
        self.assertIsNone(a.active_room);a.call_service.assert_not_called()

    def test_unaccepted_saved_request_is_never_reissued(self):
        self.fixture.job.accepted_at=None;self.fixture.job.task_started_at=None;self.write()
        a=self.restore();a._observe_active_job()
        self.assertEqual(a._vacuum_job_problem,'restored_job_unconfirmed')
        a.call_service.assert_not_called()

    def test_mova_room_cleaning_alias_is_accepted_only_for_exact_room(self):
        j=module.VacuumJobTracker(1,23,self.fixture.now,self.fixture.now-300)
        data={**self.fixture.data,'task_status':'room_cleaning'}
        self.assertEqual(j.observe(data,self.fixture.now+10),'accepted')
        j2=module.VacuumJobTracker(1,23,self.fixture.now,self.fixture.now-300)
        self.assertEqual(j2.observe({**data,'active_segments':[1,2]},self.fixture.now+10),'different_room_task')

    def test_atomic_checkpoint_roundtrip_and_corrupt_file_blocks(self):
        a=self.restore();a._save_job_checkpoint=module.VacuumAutomation._save_job_checkpoint.__get__(a)
        a._restart_state_payload=lambda:self.state
        self.assertTrue(a._save_job_checkpoint())
        self.assertFalse((self.storage/'active_job.tmp').exists())
        saved=json.loads((self.storage/'active_job.json').read_text())
        self.assertEqual(saved['job']['room_progress_percent'],41)
        (self.storage/'active_job.json').write_text('{broken')
        a=self.restore();self.assertEqual(a._vacuum_job_problem,'restart_checkpoint_invalid')
        a.call_service.assert_not_called()

    def test_invalid_saved_timing_rejected(self):
        for changes in ({'accepted_at':float('nan')},{'room_progress_percent':150},{'baseline_history':self.fixture.now+5}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                module.VacuumJobTracker.restore({**self.fixture.job.checkpoint(),**changes})

    def test_restored_task_never_aborted_before_revalidation(self):
        a=self.restore();a._abort_cleaning('Person zuhause')
        a.call_service.assert_not_called()
        self.assertFalse(a.abort_requested)

    def test_checkpoint_failure_blocks_device_start(self):
        import test_manual_cleaning as manual
        fixture=manual.ManualCleaning();fixture.setUp();a=fixture.a
        a._save_job_checkpoint=Mock(return_value=False)
        a._on_dashboard_start_room('',{'room_key':'office'},{})
        a.call_service.assert_not_called()
        self.assertEqual(a._vacuum_job_problem,'job_checkpoint_failed')

    def test_history_replay_after_crash_does_not_duplicate_or_learn_twice(self):
        a=self.restore()
        a.active_room_started_at=robot.NOW
        a.active_room_planned_duration_min=10;a.active_room_configured_duration_min=10
        a.active_room_travel_time_min=0;a.active_room_distance_km=0
        a._room_label=lambda room:room;a._counts_as_cleaned=lambda *args:True
        a.history_entries=[];a.max_history_entries=100
        a._learning_enabled=lambda:True;a._update_learning=Mock();a._save_history_storage=Mock()
        a._record_history_entry=module.VacuumAutomation._record_history_entry.__get__(a)
        a._record_history_entry('office','completed');a._record_history_entry('office','completed')
        self.assertEqual(len(a.history_entries),1)
        a._update_learning.assert_called_once_with('office')

    def test_future_acceptance_cannot_resume(self):
        self.fixture.job.accepted_at=self.fixture.now+1000;self.write()
        a=self.restore();a._observe_active_job()
        self.assertEqual(a._vacuum_job_problem,'restored_job_unconfirmed')
        a.call_service.assert_not_called()
