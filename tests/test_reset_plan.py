from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_live_dashboard import dashboard as d

class ResetPlan(unittest.TestCase):
    def test_readonly_storage_fails_before_changing_ha(self):
        with patch.object(d,'build_summary',return_value={}),patch.object(d,'load_options',return_value={}),patch.object(d,'build_live_dashboard_view',return_value={}),patch.object(d,'save_dashboard_rest_order',side_effect=OSError('Read-only file system')),patch.object(d,'service_call') as service,patch.object(d,'fire_event') as event:
            with self.assertRaises(OSError):d.reset_dashboard_plan()
            service.assert_not_called();event.assert_not_called()

    def test_response_clears_stale_overrides_without_changing_active_room(self):
        model={'manual_order':True,'rest_manual_order':True,'rooms':[
            {'id':'active','group':'active','manual_queued':True,'plan_override':True},
            {'id':'queued','group':'planned','automatic_due':False,'manual_queued':True,'plan_override':True,'default_rank':0,'enabled':True},
            {'id':'due','group':'rest','automatic_due':True,'suspended_today':True,'default_rank':1,'enabled':True},
        ]}
        previous=deepcopy(model)
        result=d.reset_dashboard_plan_model(model)
        self.assertEqual(result['rooms'][0],model['rooms'][0])
        self.assertEqual([(r['id'],r['group']) for r in result['rooms']],[('active','active'),('due','planned'),('queued','rest')])
        self.assertFalse(result['manual_order']);self.assertFalse(result['rest_manual_order'])
        for r in result['rooms'][1:]:
            for key in ('manual_queued','plan_override','suspended_today','order_changed'):self.assertFalse(r[key])
        self.assertEqual(model,previous)

    def test_acknowledged_reset_needs_no_second_summary_read(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(d,'DASHBOARD_ORDER_PATH',Path(temp)/'order.json'),patch.object(d,'build_summary',return_value={'entities':{'global':{'one_time_room_override':'input_text.order'}}}) as summary,patch.object(d,'load_options',return_value={}),patch.object(d,'build_live_dashboard_view',return_value={'rooms':[],'manual_order':True}),patch.object(d,'service_call'),patch.object(d,'fire_event'):
            self.assertFalse(d.reset_dashboard_plan()['manual_order'])
            summary.assert_called_once()
            self.assertEqual(d.load_dashboard_rest_order(),[])

    def test_failed_ha_write_restores_previous_local_order(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(d,'DASHBOARD_ORDER_PATH',Path(temp)/'order.json'),patch.object(d,'build_summary',return_value={'entities':{'global':{'one_time_room_override':'input_text.order'}}}),patch.object(d,'load_options',return_value={}),patch.object(d,'build_live_dashboard_view',return_value={}),patch.object(d,'service_call',side_effect=RuntimeError('HA unavailable')),patch.object(d,'fire_event') as event:
            d.save_dashboard_rest_order(['b','a'])
            with self.assertRaises(RuntimeError):d.reset_dashboard_plan()
            self.assertEqual(d.load_dashboard_rest_order(),['b','a']);event.assert_not_called()

    def test_deploy_mount_is_limited_to_dashboard_data(self):
        import yaml
        root=Path(__file__).resolve().parents[1]
        config=yaml.safe_load((root/'deployment/runtipi/docker-compose.yml').read_text())
        volumes=config['services']['dashboard']['volumes']
        self.assertIn('/home/simon/ghostvacuum-observer:/conf:ro',volumes)
        self.assertIn('/home/simon/ghostvacuum-observer/storage/dashboard:/conf/storage/dashboard',volumes)

    def test_reset_errors_are_named_for_reset_not_room_move(self):
        from unittest.mock import Mock
        handler=object.__new__(d.Handler) if hasattr(d,'Handler') else None
        # Handler name is resolved below to avoid starting an HTTP server.
        if handler is None:
            cls=next(value for value in vars(d).values() if isinstance(value,type) and 'do_POST' in value.__dict__)
            handler=object.__new__(cls)
        handler.path='/api/reset_room_order';handler._json_response=Mock()
        with patch.object(d,'load_options',return_value={}),patch.object(d,'reset_dashboard_plan',side_effect=OSError('read-only')),self.assertLogs(level='ERROR'):
            handler.do_POST()
        self.assertEqual(handler._json_response.call_args.args[0]['error'],'Manuelle Änderungen konnten nicht zurückgesetzt werden.')
