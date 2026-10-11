from copy import deepcopy
from unittest.mock import Mock
import unittest
import test_robot_state as robot
import test_cleaning_profiles as profiles
import test_manual_cleaning as manual
import test_job_restart as restart

module = robot.module

class RoomHandoff(unittest.TestCase):
    def setUp(self):
        fixture = robot.RobotState(); fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.a = fixture.app
        self.a.args['observation_mode'] = False
        self.a.rooms['kitchen'] = {'segment_id':9}
        self.profile = dict(module.CLEANING_PROFILE_DEFAULT)
        self.a._effective_cleaning_profile = lambda room: dict(self.profile)
        self.a._room_handoff = {
            'room':'office', 'segment':1, 'map_id':23,
            'finished_at':robot.NOW.timestamp()-100,
            'created_at':robot.NOW.timestamp(), 'profile':dict(self.profile), 'automatic':True,
        }
        self.data = {**robot.snapshot(), 'phase':'returning', 'fresh':True, 'error':'no_error'}

    def test_only_next_matching_room_can_start_during_verified_return(self):
        self.assertEqual(self.a._robot_start_blockers(self.data, 'kitchen'), [])
        self.assertIn('robot_returning', self.a._robot_start_blockers(self.data, 'office'))
        self.profile['moisture'] = 'moist'
        self.assertIn('robot_returning', self.a._robot_start_blockers(self.data, 'kitchen'))

    def test_no_permission_for_foreign_stale_expired_wash_or_charge_jobs(self):
        for changes in ({'map_id':99}, {'task_changed_at':0}, {'fresh':False},
                        {'error':'fault'}, {'active_segments':[9]}, {'phase':'returning_to_wash'},
                        {'phase':'washing'}, {'phase':'draining'}, {'phase':'idle'},
                        {'started':True}, {'running':True}, {'task_status':'segment_cleaning'},
                        {'station_status':'washing'}, {'battery':29}):
            with self.subTest(changes=changes):
                self.assertTrue(self.a._robot_start_blockers({**self.data, **changes}, 'kitchen'))
        self.a._room_handoff['created_at'] -= 31
        self.assertTrue(self.a._robot_start_blockers(self.data, 'kitchen'))
        self.assertIsNone(self.a._room_handoff)
        self.assertTrue(self.a._robot_start_blockers(self.data, 'kitchen'))

    def test_observation_and_unresolved_jobs_still_block(self):
        self.a.args['observation_mode'] = True
        self.assertFalse(self.a._room_handoff_allowed(self.data, 'kitchen'))
        self.a.args['observation_mode'] = False
        self.a._vacuum_job_problem = 'command_failed'
        self.assertIn('previous_job_unresolved', self.a._robot_start_blockers(self.data, 'kitchen'))

    def test_profile_mismatch_never_sends_selector_while_moving(self):
        f=profiles.CleaningProfiles(); f.setUp()
        f.a._robot_snapshot=lambda:{'phase':'returning'}
        f.states['select.willi_cleangenius']={'state':'off','attributes':{'options':['routine_cleaning','off']}}
        self.assertFalse(f.a._prepare_cleaning_profile('kitchen'))
        f.a.call_service.assert_not_called()

    def test_only_successful_owned_completion_creates_permission(self):
        for outcome in ('success','abort','incomplete','no_profile'):
            with self.subTest(outcome=outcome):
                f=robot.JobCorrelation(); f.setUp(); f.accept(); f.terminal(outcome != 'incomplete')
                a=f.make_app(); a.active_room_cleaning_profile=dict(self.profile) if outcome!='no_profile' else None
                a.abort_requested=outcome=='abort'
                a._check_cleaning=Mock(); a._observe_active_job()
                self.assertEqual(bool(a._room_handoff),outcome=='success')
                self.assertIsNone(a.active_room_cleaning_profile)
                a.call_service.assert_not_called()

    def test_start_consumes_permission_even_if_service_fails(self):
        f=manual.ManualCleaning(); f.setUp(); a=f.a
        a._room_handoff=deepcopy(self.a._room_handoff)
        a.call_service.side_effect=RuntimeError('network')
        a._start_cleaning('office',manual=True)
        self.assertIsNone(a._room_handoff)
        self.assertEqual(a.active_room_cleaning_profile,self.profile)
        self.assertEqual(a._vacuum_job_problem,'command_failed')

    def test_captured_profile_survives_restart_without_handoff_authority(self):
        f=restart.JobRestart(); f.setUp(); self.addCleanup(f.doCleanups)
        f.state['cp']=dict(self.profile); f.write()
        a=f.restore()
        self.assertEqual(a.active_room_cleaning_profile,self.profile)
        self.assertEqual(a._restart_state_payload()['cp'],self.profile)
        self.assertFalse(getattr(a,'_room_handoff',None))
        f.state['cp']={'mode':'bad'};f.write()
        self.assertIsNone(f.restore().active_room_cleaning_profile)

    def test_real_start_path_accepts_handoff_once_and_tracks_new_room(self):
        f=manual.ManualCleaning(); f.setUp(); a=f.a
        a.rooms=self.a.rooms; a.datetime=lambda aware=False:robot.NOW
        a._room_handoff=deepcopy(self.a._room_handoff)
        a._robot_snapshot=lambda:dict(self.data)
        a._robot_start_blockers=module.VacuumAutomation._robot_start_blockers.__get__(a)
        a._vacuum_job_problem=None
        a._start_cleaning('kitchen',manual=True)
        a.call_service.assert_called_once_with('dreame_vacuum/vacuum_clean_segment',entity_id='vacuum.willi',segments=[9],repeats=1)
        self.assertEqual(a._vacuum_job.segment,9)
        self.assertTrue(a.active_room_manual)
        self.assertIsNone(a._room_handoff)
        a._start_cleaning('office',manual=True)
        self.assertEqual(a.call_service.call_count,1)

    def test_hardware_change_during_profile_readback_prevents_start(self):
        f=manual.ManualCleaning(); f.setUp(); a=f.a
        def prepare(*args,**kwargs):
            a._robot_start_blockers.return_value=['robot_returning_to_wash']
            return True
        a._prepare_cleaning_profile=prepare
        a._start_cleaning('office',manual=True)
        a.call_service.assert_not_called()

    def test_profile_saved_in_file_but_not_size_limited_ha_helper(self):
        a=self.a; a.state_helper='input_text.state';a.call_service=Mock()
        payload={'cp':dict(self.profile),'ar':'office'}
        a._restart_state_payload=lambda:deepcopy(payload)
        a._save_job_checkpoint=Mock()
        a._save_state()
        # Copy before helper serialization removes cp from its local payload.
        self.assertNotIn('cp',a.call_service.call_args.kwargs['value'])
        self.assertEqual(payload['cp'],self.profile)
