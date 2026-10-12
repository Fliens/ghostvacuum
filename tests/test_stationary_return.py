from datetime import datetime, timedelta, timezone
import unittest
from test_observation_mode import module
from test_return_policy import ReturnPolicy

class StationaryMotion(unittest.TestCase):
    def setUp(self):
        self.app=module.VacuumAutomation()
        self.now=datetime(2026,10,10,12,tzinfo=timezone.utc)
        self.app.datetime=lambda aware=False:self.now
        self.position={'state':'not_home','last_reported':self.now.isoformat(),'attributes':{'source_type':'gps','latitude':52.005,'longitude':13,'gps_accuracy':10}}
        self.app.get_state=lambda *args,**kw:self.position
        self.app._current_return_buffer_min=lambda:5

    def report(self,minutes,lat=52.005,distance=.5,accuracy=10,presence='away',eta=7):
        self.now=datetime(2026,10,10,12,tzinfo=timezone.utc)+timedelta(minutes=minutes)
        self.position.update(last_reported=self.now.isoformat())
        self.position['attributes'].update(latitude=lat,gps_accuracy=accuracy)
        return self.app._person_motion('person.a',presence,distance,eta)

    def test_configured_dwell_duration_and_changes_use_observed_time(self):
        config = {"mode": "time", "buffer_m": 500, "margin_m": 500, "dwell_min": 3}
        self.app._return_settings = lambda: config
        self.report(0)
        self.assertFalse(self.report(2)['stationary'])
        self.assertTrue(self.report(3)['stationary'])
        config['dwell_min'] = 15
        self.assertFalse(self.report(4)['stationary'])
        self.report(10)
        self.assertTrue(self.report(15)['stationary'])
        self.assertFalse(self.report(16, eta=5)['stationary'])
        self.assertEqual(self.report(17)['stay_minutes'], 0)

    def test_gap_freezes_progress_and_same_spot_resumes_without_counting_gap(self):
        self.report(0)
        self.assertEqual(self.report(3)['stay_minutes'],3)
        paused=self.report(15,presence='unknown')
        self.assertEqual(paused['stay_minutes'],3)
        self.assertTrue(paused['stay_paused'])
        self.assertFalse(paused['stationary'])
        self.assertEqual(self.report(20)['stay_minutes'],3)
        self.assertTrue(self.report(27)['stationary'])

    def test_recovery_requires_distinct_report_and_movement_resets_dwell(self):
        self.report(0);self.report(10)
        self.app._person_motion('person.a','unknown',None,None)
        self.assertFalse(self.app._person_motion('person.a','away',.5,7)['stationary'])
        self.assertTrue(self.report(20)['stationary'])
        self.report(21,presence='unknown')
        result=self.report(22,lat=51.997,distance=.4)
        self.assertEqual(result['stay_minutes'],0)
        self.assertEqual(result['motion'],'approaching')

    def test_buffer_does_not_collect_dwell_and_reentry_starts_fresh(self):
        self.report(0)
        self.assertTrue(self.report(10)['stationary'])
        for minute in (11, 21, 31):
            result=self.report(minute,eta=5)
            self.assertFalse(result['stationary'])
            self.assertFalse(result['monitored_absence'])
            self.assertEqual(result['stay_minutes'],0)
            self.assertNotIn('person.a',self.app._motion_observations)
        self.assertEqual(self.report(32,eta=7)['stay_minutes'],0)
        self.assertFalse(self.report(35,eta=7)['stationary'])
        self.assertTrue(self.report(42,eta=7)['stationary'])

    def test_dwell_requires_fresh_distinct_reports_and_tolerates_jitter(self):
        self.assertFalse(self.report(0)['stationary'])
        self.now+=timedelta(minutes=10)
        self.assertFalse(self.app._person_motion('person.a','away',.5,7)['stationary'])
        self.assertTrue(self.report(10,lat=52.0051,distance=.51)['stationary'])

    def test_approach_stops_release_and_requires_new_dwell(self):
        self.report(0);self.assertTrue(self.report(10)['stationary'])
        approaching=self.report(11,lat=51.997,distance=.4)
        self.assertFalse(approaching['stationary']);self.assertEqual(approaching['motion'],'approaching')
        self.assertFalse(self.report(12,lat=51.9971,distance=.41)['stationary'])
        self.assertFalse(self.report(20,lat=51.997,distance=.4)['stationary'])
        self.assertTrue(self.report(21,lat=51.997,distance=.4)['stationary'])

    def test_stale_poor_home_and_restart_never_keep_release(self):
        for failure in ['stale','poor','home','restart']:
            self.app._motion_observations={};self.report(0);self.report(10)
            if failure=='stale':
                self.now+=timedelta(minutes=11);result=self.app._person_motion('person.a','away',.5,7)
            elif failure=='poor':result=self.report(11,accuracy=100)
            elif failure=='home':result=self.report(11,presence='home')
            else:
                self.app._motion_observations={};result=self.report(11)
            self.assertFalse(result['stationary'])

    def test_gaps_and_movement_without_approach_do_not_fake_dwell(self):
        self.report(0)
        self.assertFalse(self.report(11)['stationary'])
        self.assertFalse(self.report(12,lat=52.007,distance=.7)['stationary'])

    def test_movement_away_keeps_monitoring_but_approach_latches_through_jitter(self):
        self.report(0); self.assertTrue(self.report(10)['stationary'])
        away = self.report(11, lat=52.015, distance=.7)
        self.assertFalse(away['stationary'])
        self.assertTrue(away['monitored_absence'])
        self.assertEqual(away['motion'], 'observing')
        approach = self.report(12, lat=52.005, distance=.6)
        self.assertEqual(approach['motion'], 'approaching')
        self.assertEqual(self.report(13, lat=52.0051, distance=.61)['motion'], 'approaching')
        self.assertEqual(self.report(14, lat=52.018, distance=.8)['motion'], 'observing')

    def test_moving_without_previous_dwell_does_not_gain_permission(self):
        self.report(0)
        self.assertFalse(self.report(1, lat=52.007, distance=.7)['monitored_absence'])


    def test_market_500_by_500_m_keeps_dwell_across_all_corners(self):
        corners = [(52.005, 13), (52.00949, 13), (52.00949, 13.00730),
                   (52.005, 13.00730), (52.007, 13.003)]
        for minute, (lat, lon) in zip([0, 3, 6, 9, 10], corners):
            self.position['attributes']['longitude'] = lon
            result = self.report(minute, lat=lat, distance=2)
        self.assertTrue(result['stationary'])
        self.assertEqual(result['stay_minutes'], 10)
        bounds = self.app._motion_observations['person.a']['bounds']
        self.assertGreater(bounds[2] - bounds[0], .004)
        # Wandering after confirmation remains valid inside the same footprint.
        self.assertTrue(self.report(12, lat=52.005, distance=1.9)['stationary'])

    def test_small_steps_cannot_move_area_unboundedly(self):
        self.report(0)
        for minute in range(1, 13):
            result = self.report(minute, lat=52.005 + minute * .0009, distance=2)
        self.assertFalse(result['stationary'])
        self.assertLess(result['stay_minutes'], 10)

    def test_bounded_area_resumes_after_gap_without_counting_missing_time(self):
        self.report(0, distance=2)
        self.report(3, lat=52.007, distance=2)
        self.report(15, presence='unknown')
        self.assertEqual(self.report(20, lat=52.009, distance=2)['stay_minutes'], 3)
        self.assertTrue(self.report(27, lat=52.008, distance=2)['stationary'])
        self.report(28, eta=5)
        self.assertNotIn('person.a', self.app._motion_observations)

    def test_appdaemon_local_clock_accepts_fresh_reports(self):
        from zoneinfo import ZoneInfo
        self.app.datetime = lambda aware=False: self.now.astimezone(ZoneInfo('Europe/Berlin')) if aware else self.now.astimezone(ZoneInfo('Europe/Berlin')).replace(tzinfo=None)
        self.report(0)
        self.assertTrue(self.report(10)['stationary'])


class StationaryPolicy(unittest.TestCase):
    setUp = ReturnPolicy.setUp
    def test_missing_location_blocks_starts_but_not_running_job(self):
        for person in ({'presence':'unknown','travel_time_min':None},
                       {'presence':'away','travel_time_min':None}):
            self.people=[person]
            self.assertFalse(self.a._return_policy()['allowed'])
            self.assertTrue(self.a._return_policy(True)['allowed'])
            self.assertTrue(self.a._return_policy(True)['location_missing'])
            for other,reason in (({'presence':'home','travel_time_min':0},'Person zuhause'),
                                 ({'presence':'away','travel_time_min':4},'Puffergrenze erreicht')):
                self.people=[person,other]
                self.assertEqual(self.a._return_policy(True)['reason'],reason)
        self.people=[{'presence':'unknown','travel_time_min':None}]
        self.a.datetime=lambda aware=False:datetime(2026,10,10,23,tzinfo=timezone.utc)
        self.assertFalse(self.a._return_policy(True)['allowed'])

    def test_monitor_continues_and_records_each_gap_once(self):
        from unittest.mock import Mock
        self.a.args['observation_mode']=False
        self.a._observe_active_job=Mock();self.a._publish_dashboard_state=Mock()
        self.a._abort_cleaning=Mock();self.a._audit_event=Mock()
        self.a._vacuum_job_problem=None;self.a.active_room='office'
        self.people=[{'presence':'unknown','travel_time_min':None}]
        self.a._monitor_active_job({});self.a._monitor_active_job({})
        self.a._abort_cleaning.assert_not_called()
        self.assertEqual(self.a._audit_event.call_count,1)
        self.people=[{'presence':'away','travel_time_min':15}]
        self.a._monitor_active_job({})
        self.people=[{'presence':'unknown','travel_time_min':None}]
        self.a._monitor_active_job({})
        self.assertEqual([c.args[0] for c in self.a._audit_event.call_args_list],
                         ['location_gap','location_restored','location_gap'])
        self.a._abort_cleaning.assert_not_called()

    def test_cafe_release_and_all_other_safety_gates(self):
        self.people=[{'presence':'away','travel_time_min':7,'stationary':True}]
        self.assertTrue(self.a._return_policy()['allowed'])
        self.assertTrue(self.a._return_policy(True)['allowed'])
        self.people[0].update(stationary=False,travel_time_min=5)
        self.assertFalse(self.a._return_policy(True)['allowed'])
        self.people[0].update(stationary=True,presence='home')
        self.assertEqual(self.a._return_policy(True)['reason'],'Person zuhause')
        self.people[0].update(presence='away',travel_time_min=None)
        self.assertFalse(self.a._return_policy()['allowed'])
        self.people[0]['travel_time_min']=1
        self.a.travel_mode_active=True
        self.assertFalse(self.a._return_policy()['allowed'])
        self.a.travel_mode_active=False
        self.a.datetime=lambda aware=False:datetime(2026,10,10,21,56,tzinfo=timezone.utc)
        self.assertFalse(self.a._return_policy(True)['allowed'])

    def test_other_resident_near_home_always_blocks(self):
        self.people=[{'presence':'away','travel_time_min':7,'stationary':True}, {'presence':'away','travel_time_min':4}]
        self.assertFalse(self.a._return_policy(True)['allowed'])
        self.people[1]['travel_time_min']=7
        self.assertFalse(self.a._return_policy()['allowed'])
        self.assertTrue(self.a._return_policy(True)['allowed'])
        self.people[1]['travel_time_min']=10
        self.assertTrue(self.a._return_policy()['allowed'])

    def test_available_window_releases_only_proven_dwell(self):
        self.people=[{'presence':'away','travel_time_min':7,'stationary':True}]
        self.assertEqual(self.a._calculate_available_time_minutes(False),600)
        self.people.append({'presence':'away','travel_time_min':15})
        self.assertEqual(self.a._calculate_available_time_minutes(False),15)
        self.people[1]['travel_time_min']=None
        self.assertEqual(self.a._calculate_available_time_minutes(False),0)

    def test_owned_job_stops_on_approach_but_external_job_is_untouched(self):
        from unittest.mock import Mock
        self.a.args['observation_mode']=False
        self.a._observe_active_job=Mock();self.a._publish_dashboard_state=Mock();self.a._abort_cleaning=Mock()
        self.a._vacuum_job_problem=None;self.a.active_room='office'
        self.people=[{'presence':'away','travel_time_min':7,'stationary':True}]
        self.a._monitor_active_job({});self.a._abort_cleaning.assert_not_called()
        self.people[0].update(stationary=False,travel_time_min=5)
        self.a._monitor_active_job({});self.a._abort_cleaning.assert_called_once_with('Puffergrenze erreicht')
        self.a._abort_cleaning.reset_mock();self.a.active_room=None
        self.a._monitor_active_job({});self.a._abort_cleaning.assert_not_called()

    def test_ongoing_dwell_job_always_blocks_inside_buffer(self):
        self.people=[{'presence':'away','travel_time_min':1,'stationary':False,
                      'monitored_absence':True,'motion':'observing'}]
        self.assertFalse(self.a._return_policy(True)['allowed'])
        self.assertFalse(self.a._return_policy(False)['allowed'])
        self.people[0]['motion']='approaching'
        self.assertFalse(self.a._return_policy(True)['allowed'])
        self.people[0]['travel_time_min']=6
        self.assertTrue(self.a._return_policy(True)['allowed'])
        self.people[0].update(travel_time_min=1,motion='unknown',monitored_absence=False)
        self.assertFalse(self.a._return_policy(True)['allowed'])

    def test_other_person_still_stops_ongoing_monitored_job(self):
        self.people=[{'presence':'away','travel_time_min':1,'stationary':False,
                      'monitored_absence':True,'motion':'observing'},
                     {'presence':'away','travel_time_min':4}]
        self.assertFalse(self.a._return_policy(True)['allowed'])
        self.people[1]['presence']='home'
        self.assertEqual(self.a._return_policy(True)['reason'],'Person zuhause')

    def test_confirmed_dwell_cannot_release_buffer_or_available_window(self):
        for eta in (0, 1, 4.99, 5):
            self.people=[{'presence':'away','travel_time_min':eta,'stationary':True,
                          'monitored_absence':True,'motion':'staying'}]
            for running in (False, True):
                policy=self.a._return_policy(running)
                self.assertFalse(policy['allowed'])
                self.assertFalse(policy['stationary_release'])
                self.assertEqual(policy['reason'],'Puffergrenze erreicht')
            self.assertEqual(self.a._calculate_available_time_minutes(False),0)
        self.people[0]['travel_time_min']=5.01
        self.assertTrue(self.a._return_policy()['allowed'])

    def test_analysis_never_counts_stationary_sample_inside_buffer_as_start(self):
        self.people=[{'presence':'away','travel_time_min':3,'stationary':True}]
        report=self.a._return_analysis()
        self.assertEqual(report['below_buffer_samples'],1)
        self.assertEqual(next(s for s in report['simulations'] if s['buffer_min']==5)['start_samples'],0)
