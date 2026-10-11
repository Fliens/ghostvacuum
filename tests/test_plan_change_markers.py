from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import test_live_dashboard as live

d=live.dashboard

class PlanChangeMarkers(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        p=patch.object(d,'DASHBOARD_ORDER_PATH',Path(temp.name)/'order.json');p.start();self.addCleanup(p.stop)
        self.s=live.LiveDashboard().summary()
        self.s['status']['observation_mode']=False
        self.s['room_segments'][0]['name']='Bad'
        self.s['entities']={'global':{'one_time_room_override':'input_text.order'}}
        self.s['states']['global']={'one_time_room_override':{'state':''}}
        self.s['states']['rooms']=[{'id':key,'enabled_state':{'state':'on'}} for key in ('room_1','room_2')]
        self.s['status']['room_queue']=[{'room_key':'room_1','enabled':True,'is_due':True,'priority':1}, {'room_key':'room_2','enabled':True,'is_due':False,'priority':.5}]

    def view(self):return d.build_live_dashboard_view(self.s)

    def test_adding_kitchen_appends_and_marks_only_kitchen(self):
        def service(domain,service,entity,data):self.s['states']['global']['one_time_room_override']['state']=data['value']
        with patch.object(d,'build_summary',return_value=self.s),patch.object(d,'load_options',return_value={}),patch.object(d,'service_call',side_effect=service),patch.object(d,'fire_event') as event:
            d.save_dashboard_room_move('room_2','planned_auto')
        rooms=self.view()['rooms']
        self.assertEqual([r['name'] for r in rooms],['Bad','Küche'])
        self.assertTrue(all(r['group']=='planned' for r in rooms))
        self.assertFalse(rooms[0]['plan_override']);self.assertTrue(rooms[1]['plan_override'])
        self.assertEqual(d.load_dashboard_plan_changes(),{'room_2'})
        event.assert_not_called()

    def test_legacy_queue_anchors_do_not_get_dots(self):
        self.s['states']['global']['one_time_room_override']['state']='["room_1","room_2"]'
        rooms=self.view()['rooms']
        self.assertFalse(rooms[0]['plan_override']);self.assertTrue(rooms[1]['plan_override'])

    def test_due_room_at_original_position_has_no_dot(self):
        self.s['states']['global']['one_time_room_override']['state']='["room_1"]'
        self.assertFalse(self.view()['rooms'][0]['plan_override'])
        d.save_dashboard_plan_changes({'room_1'})
        self.assertFalse(self.view()['rooms'][0]['plan_override'])
        self.s['states']['global']['one_time_room_override']['state']=''
        self.assertFalse(self.view()['rooms'][0]['plan_override'])

    def test_service_failure_does_not_leave_false_markers(self):
        d.save_dashboard_plan_changes({'previous'})
        with patch.object(d,'build_summary',return_value=self.s),patch.object(d,'load_options',return_value={}),patch.object(d,'service_call',side_effect=RuntimeError('HA unavailable')):
            with self.assertRaises(RuntimeError):d.save_dashboard_room_move('room_2','planned_auto')
        self.assertEqual(d.load_dashboard_plan_changes(),{'previous'})

    def test_reset_clears_explicit_change_markers(self):
        d.save_dashboard_plan_changes({'room_1','room_2'})
        with patch.object(d,'build_summary',return_value=self.s),patch.object(d,'load_options',return_value={}),patch.object(d,'service_call'),patch.object(d,'fire_event'):
            result=d.reset_dashboard_plan()
        self.assertEqual(d.load_dashboard_plan_changes(),set())
        self.assertTrue(all(not r['plan_override'] for r in result['rooms']))

    def test_reordering_back_removes_dot_but_keeps_explicit_addition(self):
        def service(domain,service,entity,data):
            self.s['states']['global']['one_time_room_override']['state']=data['value']
        with patch.object(d,'build_summary',return_value=self.s),patch.object(d,'load_options',return_value={}),patch.object(d,'service_call',side_effect=service),patch.object(d,'fire_event'):
            d.save_dashboard_room_move('room_2','planned_auto')
            d.save_dashboard_room_move('room_1','planned_auto')
            changed={r['id']:r for r in self.view()['rooms']}
            self.assertTrue(changed['room_1']['plan_override'])
            d.save_dashboard_room_move('room_1','planned_auto','room_2')
        restored={r['id']:r for r in self.view()['rooms']}
        self.assertFalse(restored['room_1']['plan_override'])
        self.assertTrue(restored['room_2']['plan_override'])

    def test_naturally_due_rooms_return_to_priority_order_without_dots(self):
        self.s['status']['room_queue'][1]['is_due']=True
        d.save_dashboard_plan_changes({'room_1','room_2'})
        self.s['states']['global']['one_time_room_override']['state']='["room_2","room_1"]'
        self.assertTrue(all(r['plan_override'] for r in self.view()['rooms']))
        self.s['states']['global']['one_time_room_override']['state']='["room_1","room_2"]'
        self.assertFalse(any(r['plan_override'] for r in self.view()['rooms']))

    def test_remove_extra_plan_restores_without_suspending(self):
        def service(domain,service,entity,data):
            self.s['states']['global']['one_time_room_override']['state']=data['value']
        with patch.object(d,'build_summary',return_value=self.s),patch.object(d,'load_options',return_value={}),patch.object(d,'service_call',side_effect=service),patch.object(d,'fire_event') as event:
            d.save_dashboard_room_move('room_2','planned_auto')
            d.save_dashboard_room_move('room_2','rest')
        kitchen=next(r for r in self.view()['rooms'] if r['id']=='room_2')
        self.assertEqual(kitchen['group'],'rest')
        self.assertFalse(kitchen['suspended_today'])
        self.assertFalse(kitchen['plan_override'])
        self.assertNotIn('room_2',d.load_dashboard_plan_changes())
        event.assert_not_called()

    def test_old_suspension_of_non_due_room_has_no_red_marker(self):
        self.s['status']['room_queue'][1]['suspended_today']=True
        kitchen=next(r for r in self.view()['rooms'] if r['id']=='room_2')
        self.assertFalse(kitchen['suspended_today'])

    def test_removing_naturally_due_room_still_suspends_today(self):
        with patch.object(d,'build_summary',return_value=self.s),patch.object(d,'load_options',return_value={}),patch.object(d,'service_call') as service,patch.object(d,'fire_event') as event:
            d.save_dashboard_room_move('room_1','rest')
        event.assert_called_once_with(d.EVENT_SET_ROOM_DUE,{'room_key':'room_1','suspend_today':True})
        service.assert_not_called()

    def test_reset_hidden_when_only_untouched_queue_anchors_remain(self):
        self.s['states']['global']['one_time_room_override']['state']='["room_1"]'
        d.save_dashboard_plan_changes({'room_1'})
        self.assertFalse(self.view()['manual_order'])

    def test_reset_hidden_when_rest_order_matches_original(self):
        self.s['status']['room_queue'][0]['is_due']=False
        d.save_dashboard_rest_order(['room_1','room_2'])
        model=self.view()
        self.assertFalse(model['rest_manual_order'])
        self.assertFalse(model['manual_order'])
        d.save_dashboard_rest_order(['room_2','room_1'])
        model=self.view()
        self.assertTrue(model['rest_manual_order'])
        self.assertTrue(model['manual_order'])

    def test_reset_available_for_actual_extra_plan_and_suspension(self):
        self.s['states']['global']['one_time_room_override']['state']='["room_1","room_2"]'
        self.assertTrue(self.view()['manual_order'])
        self.s['states']['global']['one_time_room_override']['state']=''
        self.s['status']['room_queue'][0]['suspended_today']=True
        self.assertTrue(self.view()['manual_order'])
