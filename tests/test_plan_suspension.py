import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock
from test_observation_mode import module
import test_live_dashboard as live

class PlanSuspension(unittest.TestCase):
    def setUp(self):
        self.a=module.VacuumAutomation();a=self.a
        a.args={'observation_mode':False};a.rooms={'a':{},'b':{}}
        self.now=datetime(2026,10,11,23,59)
        a.datetime=lambda:self.now;a.active_room=None
        a.rooms_suspended_today={};a.last_cleaned={'a':datetime(2026,10,9)}
        a.one_time_room_override_entity='input_text.order';a.get_state=lambda _: '["!a","b"]'
        a._save_state=Mock();a._publish_dashboard_state=Mock();a.call_service=Mock()
        a._room_enabled=lambda _:True

    def test_remove_preserves_history_clears_manual_entry_and_expires_at_midnight(self):
        a=self.a;last=dict(a.last_cleaned)
        a._on_dashboard_set_room_due(None,{'room_key':'a','suspend_today':True},None)
        self.assertTrue(a._room_suspended_today('a'));self.assertIsNone(a._queued_manual_room())
        self.assertEqual(a.last_cleaned,last)
        a.call_service.assert_called_once_with('input_text/set_value',entity_id='input_text.order',value='["b"]')
        self.now+=timedelta(minutes=1)
        self.assertFalse(a._room_suspended_today('a'));self.assertEqual(a._queued_manual_room(),'a')

    def test_replanning_clears_suspension_without_changing_history(self):
        a=self.a;a.rooms_suspended_today={'a':'2026-10-11'}
        a._on_dashboard_set_room_due(None,{'room_key':'a','suspend_today':False},None)
        self.assertFalse(a._room_suspended_today('a'));a.call_service.assert_not_called()

    def test_observation_and_active_job_do_not_allow_removal(self):
        a=self.a
        for observing,active in [(True,None),(False,'a')]:
            a.args['observation_mode']=observing;a.active_room=active
            a._on_dashboard_set_room_due(None,{'room_key':'a','suspend_today':True},None)
        self.assertEqual(a.rooms_suspended_today,{});a.call_service.assert_not_called()

    def test_automatic_selection_excludes_suspended_room(self):
        a=self.a;a.rooms_suspended_today={'a':'2026-10-11'}
        a._room_effective_duration_min=lambda _:10;a._current_return_buffer_min=lambda:5
        a._current_start_margin_min=lambda:5;a._room_due_info=lambda _:{"is_due":True,"score":2}
        a._room_weight=lambda _:1
        self.assertEqual(a._select_next_room(30),'b')

    def test_dashboard_hides_due_room_even_with_old_override(self):
        s=live.LiveDashboard().summary();s['status']['observation_mode']=False
        s['states']['rooms']=[{'id':'room_1','enabled_state':{'state':'on'}}]
        s['states']['global']={'one_time_room_override':{'state':'["!room_1"]'}}
        s['status']['room_queue']=[{'room_key':'room_1','enabled':True,'forecast_score':2,'suspended_today':True}]
        room=next(r for r in live.dashboard.build_live_dashboard_view(s)['rooms'] if r['id']=='room_1')
        self.assertEqual(room['group'],'rest');self.assertTrue(room['suspended_today'])
