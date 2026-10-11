import tempfile
import unittest
from pathlib import Path
from datetime import datetime
from unittest.mock import Mock, patch
from test_observation_mode import module
from test_live_dashboard import dashboard

class PlanningHistory(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        storage=patch.object(dashboard,"DASHBOARD_ORDER_PATH",Path(temp.name)/"order.json")
        storage.start();self.addCleanup(storage.stop)

    def test_legacy_plan_events_preserve_completion(self):
        app=module.VacuumAutomation();app.rooms={'a':{}};app.last_cleaned={'a':datetime(2026,10,11,12,41)}
        original=app.last_cleaned.copy();app.datetime=lambda:datetime(2026,10,11,14)
        app.one_time_room_override_entity='input_text.order';app.get_state=lambda _:''
        app._save_state=Mock();app._publish_dashboard_state=Mock();app.call_service=Mock()
        app._mark_room_due_state('a',True)
        self.assertEqual(app.last_cleaned,original)
        self.assertEqual(app.call_service.call_args.kwargs['value'],'["a"]')
        app._mark_room_due_state('a',False)
        self.assertEqual(app.last_cleaned,original)
        self.assertTrue(app._room_suspended_today('a'))

    def test_history_repairs_backdated_completion(self):
        app=module.VacuumAutomation();app.rooms={'a':{}};app.last_cleaned={'a':datetime(2026,10,9)}
        app.history_entries=[{'room':'a','finished_at':'2026-10-11T12:41:35','counted_as_cleaned':True}]
        app._save_state=Mock();app._restore_confirmed_cleaning_times()
        self.assertEqual(app.last_cleaned['a'],datetime(2026,10,11,12,41,35))
        app._save_state.assert_called_once()

    def test_queued_room_can_be_selected_before_interval_without_rewriting_history(self):
        app=module.VacuumAutomation();app.rooms={'a':{}};app._room_enabled=lambda _:True
        app._room_suspended_today=lambda _:False;app._dashboard_room_order=lambda:['a']
        app._room_effective_duration_min=lambda _:30;app._current_return_buffer_min=lambda:5
        app._current_start_margin_min=lambda:5;app._room_due_info=lambda _:{"is_due":False,"score":0.01}
        app._room_weight=lambda _:1
        self.assertEqual(app._select_next_room(30),'a')

    def test_reset_changes_only_plan_and_rest_order(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(dashboard,'DASHBOARD_ORDER_PATH',Path(directory)/'order.json'), patch.object(dashboard,'build_summary',return_value={'entities':{'global':{'one_time_room_override':'input_text.order'}}}), patch.object(dashboard,'load_options',return_value={'observation_mode':False}), patch.object(dashboard,'service_call') as service, patch.object(dashboard,'fire_event') as event, patch.object(dashboard,'build_live_dashboard_view',return_value={'manual_order':False}) as view:
            dashboard.save_dashboard_rest_order(['a','b'])
            self.assertEqual(dashboard.reset_dashboard_plan(), {'manual_order':False})
            self.assertEqual(dashboard.load_dashboard_rest_order(),[])
            service.assert_called_once_with('input_text','set_value','input_text.order',{'value':''})
            event.assert_called_once_with(dashboard.EVENT_SET_ROOM_DUE,{'reset_plan':True})
        app=module.VacuumAutomation();app.args={'observation_mode':False}
        app.active_room='a';app.active_room_manual=True;app.rooms_suspended_today={'b':'2026-10-11'}
        app._save_state=Mock();app._publish_dashboard_state=Mock();app.call_service=Mock()
        app._on_dashboard_set_room_due(None,{'reset_plan':True},None)
        self.assertEqual(app.active_room,'a');self.assertTrue(app.active_room_manual)
        self.assertEqual(app.rooms_suspended_today,{})
        app.call_service.assert_not_called()

    def test_queueing_rest_room_never_fakes_due_date(self):
        model={'commands_locked':False,'robot':{'cleaning':False},'rooms':[{'id':'a','enabled':True,'group':'rest'}]}
        with patch.object(dashboard,'build_summary',return_value={'entities':{'global':{'one_time_room_override':'input_text.order'}}}),patch.object(dashboard,'build_live_dashboard_view',return_value=model),patch.object(dashboard,'service_call') as service,patch.object(dashboard,'fire_event') as event:
            dashboard.save_dashboard_room_move('a','planned_auto')
            event.assert_not_called()
            service.assert_called_once_with('input_text','set_value','input_text.order',{'value':'["a"]'})

    def test_rest_reorder_only_saves_display_order(self):
        model={'commands_locked':False,'robot':{'cleaning':True},'rooms':[{'id':'a','enabled':True,'group':'rest'},{'id':'b','enabled':True,'group':'rest'},{'id':'c','enabled':True,'group':'active'}]}
        with tempfile.TemporaryDirectory() as directory,patch.object(dashboard,'DASHBOARD_ORDER_PATH',Path(directory)/'order.json'),patch.object(dashboard,'build_summary',return_value={}),patch.object(dashboard,'build_live_dashboard_view',return_value=model),patch.object(dashboard,'service_call') as service,patch.object(dashboard,'fire_event') as event:
            dashboard.save_dashboard_room_move('b','rest','a')
            self.assertEqual(dashboard.load_dashboard_rest_order(),['b','a'])
            service.assert_not_called();event.assert_not_called()
