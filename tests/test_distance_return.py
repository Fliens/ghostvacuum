import json
from unittest.mock import patch
import unittest
import test_return_policy
from test_live_dashboard import dashboard

class DistanceReturn(unittest.TestCase):
    def setUp(self):
        test_return_policy.ReturnPolicy.setUp(self)
        path=self.a.storage_dir/'dashboard';path.mkdir()
        (path/'return_settings.json').write_text(json.dumps({'mode':'distance','buffer_m':500,'margin_m':500}))
        self.people[0].update(distance_km=1.5,travel_time_min=1)

    def test_distance_boundaries_ignore_eta(self):
        for km,start,running in [(0.4,False,False),(0.5,False,False),(0.501,False,True),(0.999,False,True),(1,True,True),(5,True,True)]:
            self.people[0]['distance_km']=km
            self.assertEqual(self.a._return_policy()['allowed'],start)
            self.assertEqual(self.a._return_policy(True)['allowed'],running)
        self.assertEqual(self.a._return_policy()['eta_min'],1)
        self.assertEqual(self.a._return_policy()['nearest_value'],5000)

    def test_dwell_never_overrides_buffer(self):
        self.people[0].update(stationary=True,distance_km=.5)
        self.assertFalse(self.a._return_policy()['allowed'])
        self.people[0]['distance_km']=.6
        self.assertTrue(self.a._return_policy()['allowed'])

    def test_missing_distance_blocks_start_but_not_running(self):
        self.people[0]['distance_km']=None
        self.assertFalse(self.a._return_policy()['allowed'])
        self.assertTrue(self.a._return_policy(True)['allowed'])

    def test_available_time_is_independent_of_eta(self):
        self.assertEqual(self.a._calculate_available_time_minutes(False),600)

    def test_journey_distance_and_waiting_region(self):
        p={**self.people[0],'motion':'observing','distance_km':.75}
        policy=self.a._return_policy()
        view={'kind':'away','stay_progress':{'minutes':3,'required':10}}
        j=dashboard.person_journey_scale(p,view,5,policy,{})
        self.assertEqual(j['station'],2)
        self.assertEqual(j['eta'],'750 m von zuhause')
        self.assertEqual(j['progress']['minutes'],3)
        self.assertEqual(j['steps'][1]['hint'],'≤ 500 m')


    def test_confirmed_journey_uses_configured_duration(self):
        p={**self.people[0], 'stationary':True, 'distance_km':.75}
        with patch.object(dashboard, 'DASHBOARD_ORDER_PATH', self.a.storage_dir/'dashboard/order.json'):
            dashboard.save_return_settings({'dwell_min':15})
            journey=dashboard.person_journey_scale(p, {'kind':'away'}, 5, self.a._return_policy(), {})
            self.assertEqual(journey['progress'], {'minutes':15, 'required':15})

    def test_settings_validation_and_default(self):
        from pathlib import Path
        with patch.object(dashboard,'DASHBOARD_ORDER_PATH',self.a.storage_dir/'dashboard/order.json'):
            self.assertEqual(dashboard.load_return_settings()['mode'],'distance')
            self.assertTrue(dashboard.load_return_settings()['require_cleaned_area'])
            dashboard.save_return_settings({'require_cleaned_area':False})
            self.assertFalse(self.a._return_settings()['require_cleaned_area'])
            with self.assertRaises(ValueError):dashboard.save_return_settings({'require_cleaned_area':0})
            self.assertEqual(dashboard.load_return_settings()['dwell_min'],10)
            dashboard.save_return_settings({'dwell_min':15})
            self.assertEqual(self.a._return_settings()['dwell_min'],15)
            dashboard.save_return_settings({'mode':'time'})
            self.assertEqual(self.a._return_settings()['mode'],'time')
            for payload in [{'mode':'other'},{'buffer_m':-1},{'margin_m':float('nan')},{'buffer_m':True},{'dwell_min':0},{'dwell_min':121},{'dwell_min':float('nan')},{'dwell_min':True}]:
                with self.assertRaises(ValueError):dashboard.save_return_settings(payload)
            self.assertEqual(self.a._return_settings()['mode'],'time')
            (self.a.storage_dir/'dashboard/return_settings.json').unlink()
            self.assertEqual(self.a._return_settings()['mode'],'time')
