import importlib.util
from pathlib import Path
from datetime import datetime, timedelta, timezone
import unittest
from test_observation_mode import module
spec=importlib.util.spec_from_file_location('motion_backfill',Path(__file__).resolve().parents[1]/'deployment/backfill_presence_motion.py')
backfill=importlib.util.module_from_spec(spec);spec.loader.exec_module(backfill)

class MotionHistory(unittest.TestCase):
    def run_history(self,minutes,latitudes):
        start=datetime(2026,10,1,12,tzinfo=timezone.utc)
        rows=[{'entity_id':'device_tracker.phone','state':'not_home','last_updated':(start+timedelta(minutes=m)).isoformat(),
               'attributes':{'source_type':'gps','latitude':lat,'longitude':13,'gps_accuracy':10}}
              for m,lat in zip(minutes,latitudes)]
        return backfill.replay_motion(module.VacuumAutomation(),[rows],[{'id':'person.a','gps':'device_tracker.phone','routers':[]}],
                                     (52,13),5,5,30,start,start+timedelta(days=1))
    def test_replays_dwell_release_and_approach_without_fake_minutes(self):
        result=self.run_history([0,5,10,11],[52.0045,52.0045,52.0045,52.0035])
        self.assertEqual(result['gps_reports'],4)
        self.assertEqual(result['dwell_episodes'],1)
        self.assertEqual(result['extra_release_reports'],1)
        self.assertEqual(result['approach_stops'],1)
        self.assertNotIn('latitude',str(result))
    def test_missing_reports_do_not_turn_into_dwell(self):
        result=self.run_history([0,11],[52.0045,52.0045])
        self.assertEqual(result['gaps'],1)
        self.assertEqual(result['dwell_episodes'],0)
        self.assertEqual(result['extra_release_reports'],0)

    def test_dashboard_labels_dwell_as_monitored_maximum(self):
        from test_live_dashboard import dashboard, LiveDashboard
        summary=LiveDashboard().summary()
        summary['status'].update(observation_mode=False,return_policy={'stationary_release':True})
        summary['status']['presence_summary'][0].update(stationary=True,motion='staying')
        model=dashboard.build_live_dashboard_view(summary)
        self.assertEqual(model['caption'],'Maximale Reinigungszeit')
        self.assertIn('Stopp bei Annäherung',model['reason'])
        self.assertEqual(model['people'][0]['countdown'],'Aufenthalt erkannt')
        summary['status']['presence_summary'][0]['state']='home'
        self.assertEqual(dashboard.build_live_dashboard_view(summary)['window'],0)
