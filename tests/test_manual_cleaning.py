from datetime import datetime
import unittest
from unittest.mock import Mock
from test_observation_mode import module
import test_cleaning_profiles as profiles
import test_live_dashboard as live

class ManualCleaning(unittest.TestCase):
    def setUp(self):
        self.a=module.VacuumAutomation()
        a=self.a
        a.args={'observation_mode':False}
        a.rooms={'office':{'segment_id':1}}
        a.active_room=None
        a._robot_start_blockers=Mock(return_value=[])
        a._can_start_cleaning=Mock(return_value=False)
        a._robot_snapshot=lambda:{'map_id':1,'history':[]}
        a._prepare_cleaning_profile=Mock(return_value=True)
        a._effective_cleaning_profile=lambda room:dict(module.CLEANING_PROFILE_DEFAULT)
        a._room_effective_duration_min=lambda room:10
        a._room_configured_duration_min=lambda room:10
        a._get_travel_time_minutes=lambda:0
        a._get_distance_km=lambda:0
        a.datetime=lambda aware=False:datetime(2026,10,11,23)
        a.simulation_mode=False
        a.vacuum_entity='vacuum.willi'
        a.call_service=Mock();a.run_in=Mock()
        a._save_state=Mock();a._save_job_checkpoint=Mock(return_value=True);a._publish_dashboard_state=Mock()
        a._clear_one_time_room_override=Mock();a.log=Mock()

    def test_explicit_start_bypasses_automatic_policy_and_tracks_manual_origin(self):
        self.a._on_dashboard_start_room('',{'room_key':'office'},{})
        self.a._can_start_cleaning.assert_not_called()
        self.assertEqual(self.a.active_room,'office')
        self.assertTrue(self.a.active_room_manual)
        self.a.call_service.assert_called_once()
        self.a._prepare_cleaning_profile.assert_called_once_with('office',manual=True)

    def test_observation_device_blocker_and_existing_task_prevent_commands(self):
        for condition in ('observation','blocker','active','unknown_room'):
            with self.subTest(condition=condition):
                self.setUp()
                if condition=='observation':self.a.args['observation_mode']=True
                if condition=='blocker':self.a._robot_start_blockers.return_value=['battery_low_or_unknown']
                if condition=='active':self.a.active_room='office'
                self.a._on_dashboard_start_room('',{'room_key':'other' if condition=='unknown_room' else 'office'},{})
                self.a.call_service.assert_not_called()

    def test_manual_job_not_aborted_by_policy_master_or_homecoming(self):
        a=self.a;a.active_room='office';a.active_room_manual=True
        a._observe_active_job=Mock();a._vacuum_job_problem=None
        a._abort_cleaning=Mock();a._return_policy=Mock(side_effect=AssertionError('No automation policy for manual task'))
        a._monitor_active_job({})
        a._on_enabled_change('', '', 'on','off',{})
        a.away_since=datetime(2026,10,11,12)
        a._is_anyone_home=lambda:True;a._is_everyone_away=lambda:False
        a._send_return_notification=Mock()
        a._on_presence_change('', '', 'not_home','home',{})
        a._abort_cleaning.assert_not_called()

    def test_manual_profile_preparation_rechecks_device_safety(self):
        fixture=profiles.CleaningProfiles();fixture.setUp();a=fixture.a
        a._can_start_cleaning=Mock(side_effect=AssertionError('Automatic gate used'))
        a._can_start_manual_cleaning=Mock(return_value=False)
        # A pending profile change must still require fresh manual authority.
        a.get_state=lambda e,**kw: fixture.states.get(e) if e in fixture.states else {'state':'off','attributes':{'options':['routine_cleaning','off']}}
        a._prepare_cleaning_profile('office',manual=True)
        a.call_service.assert_not_called()
        a._can_start_manual_cleaning.assert_called_once_with('office')

    def test_dashboard_labels_owned_manual_job(self):
        summary=live.LiveDashboard().summary()
        summary['states']['vacuum']['state']='cleaning'
        summary['status'].update(observation_mode=False,active_room='Küche',active_room_manual=True)
        self.assertEqual(live.dashboard.build_live_dashboard_view(summary)['robot']['origin_label'],'Manuell gestartet')

    def test_profile_retry_is_bounded_rechecks_safety_and_ignores_old_request(self):
        a=self.a;a._prepare_cleaning_profile.return_value=False
        a._on_dashboard_start_room('',{'room_key':'office'},{})
        a.call_service.assert_not_called()
        a.run_in.assert_called_once()
        queued=a.run_in.call_args.kwargs
        a._robot_start_blockers.return_value=['robot_stale']
        a._retry_manual_start(queued)
        a.call_service.assert_not_called()
        self.assertEqual(a.run_in.call_count,1)
        a._robot_start_blockers.return_value=[]
        a._manual_start_generation+=1
        a._prepare_cleaning_profile.return_value=True
        a._retry_manual_start(queued)
        a.call_service.assert_not_called()
        a._prepare_cleaning_profile.return_value=False
        a._start_cleaning('office',manual=True,profile_attempt=8)
        self.assertEqual(a.run_in.call_count,1)

    def test_manual_origin_survives_restart(self):
        import json
        a=self.a;a.state_helper='input_text.state'
        a.get_state=lambda entity:json.dumps({'lc':{},'ar':'office','am':True})
        a._load_state()
        self.assertTrue(a.active_room_manual)
        self.assertEqual(a._vacuum_job_problem,'restored_job_unconfirmed')

    def test_manual_completion_clears_origin_without_chaining_auto_job(self):
        import test_robot_state as robot
        fixture=robot.JobCorrelation();fixture.setUp();fixture.accept();fixture.terminal()
        a=fixture.make_app();a.active_room_manual=True
        a._can_start_cleaning=Mock(return_value=True);a._check_cleaning=Mock()
        a._observe_active_job()
        self.assertIsNone(a.active_room)
        self.assertFalse(a.active_room_manual)
        a._record_history_entry.assert_called_once()
        a._check_cleaning.assert_not_called()

    def test_manual_queue_bypasses_policy_but_device_blocker_prevents_start(self):
        a=self.a
        a.one_time_room_override_entity='input_text.order'
        a.get_state=lambda entity:'["!office"]'
        a._room_enabled=lambda room:True
        a._check_cleaning({})
        self.assertTrue(a.active_room_manual)
        self.assertEqual(a.active_room,'office')
        self.setUp();a=self.a
        a.one_time_room_override_entity='input_text.order'
        a.get_state=lambda entity:'["!office"]'
        a._room_enabled=lambda room:True
        a._robot_start_blockers=Mock(return_value=['battery_low_or_unknown'])
        a._check_cleaning({})
        a.call_service.assert_not_called()

    def test_removed_manual_label_cancels_pending_profile_retry(self):
        a=self.a
        a.one_time_room_override_entity='input_text.order'
        a.get_state=lambda entity:'["!office"]'
        a._room_enabled=lambda room:True
        a._prepare_cleaning_profile=Mock(return_value=False)
        a._check_cleaning({})
        queued=a.run_in.call_args.kwargs
        a.get_state=lambda entity:'["office"]'
        a._prepare_cleaning_profile=Mock(return_value=True)
        a._retry_manual_start(queued)
        a.call_service.assert_not_called()

    def test_disabled_room_never_starts_from_manual_queue(self):
        a=self.a;a.one_time_room_override_entity='input_text.order'
        a.get_state=lambda entity:'["!office"]'
        a._room_enabled=lambda room:False
        a._check_cleaning({})
        a.call_service.assert_not_called()

    def test_confirmed_completion_checks_next_manual_room(self):
        import test_robot_state as robot
        fixture=robot.JobCorrelation();fixture.setUp();fixture.accept();fixture.terminal()
        a=fixture.make_app();a.active_room_manual=True
        a._queued_manual_room=Mock(return_value='next_room')
        a._check_cleaning=Mock()
        a._observe_active_job()
        self.assertIsNone(a.active_room)
        a._check_cleaning.assert_called_once_with({})

    def test_unresolved_job_blocks_manual_queue(self):
        a=self.a;a.one_time_room_override_entity='input_text.order'
        a.get_state=lambda entity:'["!office"]';a._room_enabled=lambda room:True
        a._robot_start_blockers=Mock(return_value=['previous_job_unresolved'])
        a._check_cleaning({})
        a.call_service.assert_not_called()
