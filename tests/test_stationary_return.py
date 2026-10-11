from datetime import datetime, timedelta, timezone
import unittest
from test_observation_mode import module
from test_return_policy import ReturnPolicy

class StationaryMotion(unittest.TestCase):
    def setUp(self):
        self.app=module.VacuumAutomation()
        self.now=datetime(2026,10,10,12,tzinfo=timezone.utc)
        self.app.datetime=lambda:self.now
        self.position={'state':'not_home','last_reported':self.now.isoformat(),'attributes':{'source_type':'gps','latitude':52.005,'longitude':13,'gps_accuracy':10}}
        self.app.get_state=lambda *args,**kw:self.position

    def report(self,minutes,lat=52.005,distance=.5,accuracy=10,presence='away'):
        self.now=datetime(2026,10,10,12,tzinfo=timezone.utc)+timedelta(minutes=minutes)
        self.position.update(last_reported=self.now.isoformat())
        self.position['attributes'].update(latitude=lat,gps_accuracy=accuracy)
        return self.app._person_motion('person.a',presence,distance)

    def test_dwell_requires_fresh_distinct_reports_and_tolerates_jitter(self):
        self.assertFalse(self.report(0)['stationary'])
        self.now+=timedelta(minutes=10)
        self.assertFalse(self.app._person_motion('person.a','away',.5)['stationary'])
        self.assertTrue(self.report(10,lat=52.0051,distance=.51)['stationary'])

    def test_approach_stops_release_and_requires_new_dwell(self):
        self.report(0);self.assertTrue(self.report(10)['stationary'])
        approaching=self.report(11,lat=52.004,distance=.4)
        self.assertFalse(approaching['stationary']);self.assertEqual(approaching['motion'],'approaching')
        self.assertFalse(self.report(12,lat=52.0041,distance=.41)['stationary'])
        self.assertFalse(self.report(20,lat=52.004,distance=.4)['stationary'])
        self.assertTrue(self.report(21,lat=52.004,distance=.4)['stationary'])

    def test_stale_poor_home_and_restart_never_keep_release(self):
        for failure in ['stale','poor','home','restart']:
            self.app._motion_observations={};self.report(0);self.report(10)
            if failure=='stale':
                self.now+=timedelta(minutes=11);result=self.app._person_motion('person.a','away',.5)
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
        away = self.report(11, lat=52.007, distance=.7)
        self.assertFalse(away['stationary'])
        self.assertTrue(away['monitored_absence'])
        self.assertEqual(away['motion'], 'observing')
        approach = self.report(12, lat=52.006, distance=.6)
        self.assertEqual(approach['motion'], 'approaching')
        self.assertEqual(self.report(13, lat=52.0061, distance=.61)['motion'], 'approaching')
        self.assertEqual(self.report(14, lat=52.008, distance=.8)['motion'], 'observing')

    def test_moving_without_previous_dwell_does_not_gain_permission(self):
        self.report(0)
        self.assertFalse(self.report(1, lat=52.007, distance=.7)['monitored_absence'])


class StationaryPolicy(unittest.TestCase):
    setUp = ReturnPolicy.setUp
    def test_cafe_release_and_all_other_safety_gates(self):
        self.people=[{'presence':'away','travel_time_min':1,'stationary':True}]
        self.assertTrue(self.a._return_policy()['allowed'])
        self.assertTrue(self.a._return_policy(True)['allowed'])
        self.people[0]['stationary']=False
        self.assertFalse(self.a._return_policy(True)['allowed'])
        self.people[0].update(stationary=True,presence='home')
        self.assertEqual(self.a._return_policy(True)['reason'],'Person zuhause')
        self.people[0].update(presence='away',travel_time_min=None)
        self.assertFalse(self.a._return_policy()['allowed'])
        self.people[0]['travel_time_min']=1
        self.a.travel_mode_active=True
        self.assertFalse(self.a._return_policy()['allowed'])
        self.a.travel_mode_active=False
        self.a.datetime=lambda:datetime(2026,10,10,21,56,tzinfo=timezone.utc)
        self.assertFalse(self.a._return_policy(True)['allowed'])

    def test_other_resident_near_home_always_blocks(self):
        self.people=[{'presence':'away','travel_time_min':1,'stationary':True}, {'presence':'away','travel_time_min':4}]
        self.assertFalse(self.a._return_policy(True)['allowed'])
        self.people[1]['travel_time_min']=7
        self.assertFalse(self.a._return_policy()['allowed'])
        self.assertTrue(self.a._return_policy(True)['allowed'])
        self.people[1]['travel_time_min']=10
        self.assertTrue(self.a._return_policy()['allowed'])

    def test_available_window_releases_only_proven_dwell(self):
        self.people=[{'presence':'away','travel_time_min':1,'stationary':True}]
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
        self.people=[{'presence':'away','travel_time_min':1,'stationary':True}]
        self.a._monitor_active_job({});self.a._abort_cleaning.assert_not_called()
        self.people[0]['stationary']=False
        self.a._monitor_active_job({});self.a._abort_cleaning.assert_called_once_with('Puffergrenze erreicht')
        self.a._abort_cleaning.reset_mock();self.a.active_room=None
        self.a._monitor_active_job({});self.a._abort_cleaning.assert_not_called()

    def test_ongoing_dwell_job_only_stops_on_approach_inside_buffer(self):
        self.people=[{'presence':'away','travel_time_min':1,'stationary':False,
                      'monitored_absence':True,'motion':'observing'}]
        self.assertTrue(self.a._return_policy(True)['allowed'])
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
