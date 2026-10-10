import unittest
from unittest.mock import patch
from test_live_dashboard import dashboard
from test_observation_mode import module

class RoomDrag(unittest.TestCase):
    def setUp(self):
        self.model={'commands_locked':False,'automation_enabled':True,'robot':{'cleaning':False},'rooms':[
            {'id':'a','group':'planned','enabled':True}, {'id':'b','group':'planned','enabled':True},
            {'id':'c','group':'rest','enabled':False}]}
        self.summary={'entities':{'global':{'one_time_room_override':'input_text.order'}}}
        for name, value in [('build_summary',self.summary),('build_live_dashboard_view',self.model),('load_options',{'vacuum_entity':'vacuum.robot'})]:
            p=patch.object(dashboard,name,return_value=value);p.start();self.addCleanup(p.stop)
        self.services=patch.object(dashboard,'service_call').start()
        self.events=patch.object(dashboard,'fire_event').start()
        self.addCleanup(patch.stopall)

    def test_reorder_does_not_command_robot(self):
        dashboard.save_dashboard_room_move('b','planned','a')
        self.services.assert_called_once_with('input_text','set_value','input_text.order',{'value':'["b","a"]'})
        self.events.assert_not_called()

    def test_locked_disabled_and_bad_payload_rejected_without_writes(self):
        for room,target in [('c','planned'),('a','rest'),([], 'active')]:
            with self.assertRaises(ValueError): dashboard.save_dashboard_room_move(room,target)
        self.model['commands_locked']=True
        with self.assertRaises(ValueError): dashboard.save_dashboard_room_move('a','active')
        self.services.assert_not_called(); self.events.assert_not_called()

    def test_replacement_retains_old_room_and_only_stops(self):
        self.model['rooms'][0]['group']='active';self.model['robot']['cleaning']=True
        dashboard.save_dashboard_room_move('b','active')
        self.assertEqual(self.services.call_args_list[0].args[3]['value'],'["b","a"]')
        self.assertEqual(self.services.call_args_list[-1].args,('vacuum','return_to_base','vacuum.robot'))
        self.assertTrue(all(c.args[0] != dashboard.EVENT_START_ROOM for c in self.events.call_args_list))

    def test_idle_start_uses_guarded_app_event(self):
        dashboard.save_dashboard_room_move('b','active')
        self.events.assert_called_with(dashboard.EVENT_START_ROOM,{'room_key':'b'})
        self.assertTrue(all(c.args[0]=='input_text' for c in self.services.call_args_list))

    def test_queue_consumes_only_started_room(self):
        app=module.VacuumAutomation();app.rooms={'a':{},'b':{},'c':{}}
        app.one_time_room_override_entity='input_text.order';app.active_room='b'
        app.get_state=lambda _: '["a","b","c"]';app.call_service=unittest.mock.Mock()
        app._clear_one_time_room_override()
        app.call_service.assert_called_once_with('input_text/set_value',entity_id='input_text.order',value='["a","c"]')
