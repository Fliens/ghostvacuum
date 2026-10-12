from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from test_observation_mode import module
from test_live_dashboard import dashboard


class PersonNotifications(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.a=module.VacuumAutomation();a=self.a
        a.storage_dir=Path(self.temp.name);a.args={'observation_mode':False}
        a.simulation_mode=False;a.dashboard_path='/ghostvacuum/'
        a.away_since=datetime(2026,10,12,9,tzinfo=timezone.utc)
        a.cleaned_during_absence=['bath'];a.history_entries=[]
        a._room_label=lambda room:'Bad';a._return_summary_enabled=lambda:True
        a._start_notifications_enabled=lambda:True;a._is_everyone_away=lambda:True
        a._current_presence_entities=lambda:['person.a','person.b']
        self.states={'person.a':'home','person.b':'not_home'}
        a._presence_state=lambda person:self.states[person]
        a.call_service=Mock();a._audit_event=Mock();a.log=Mock()
        self.prefs={'return_delivery':'each','people':{p:{'service':'notify/mobile_app_'+p[-1], 'start':True,'return':True} for p in self.states}}

    def test_short_title_and_actual_duration_despite_reset_absence_clock(self):
        self.a.away_since=datetime(2026,10,12,21,55,tzinfo=timezone.utc)
        self.a.history_entries=[{'room':'bath', 'outcome':'completed', 'counted_as_cleaned':True,
                                 'finished_at':'2026-10-12T19:01:00+00:00', 'actual_duration_min':18}]
        self.a._send_person_return_reports(self.prefs)
        self.assertEqual(self.a.call_service.call_args.kwargs['title'], 'GhostVacuum')
        self.assertEqual(self.a.call_service.call_args.kwargs['message'], 'Während deiner Abwesenheit gereinigt: Bad (18 min)')

    def test_missing_duration_is_omitted_instead_of_zero(self):
        self.assertEqual(self.a._return_report_message(), 'Während deiner Abwesenheit gereinigt: Bad')
        self.a.history_entries=[{'room':'bath','outcome':'completed','actual_duration_min':0}]
        self.assertNotIn('0 min', self.a._return_report_message())

    def test_unknown_presence_does_not_reset_absence_when_publishing(self):
        self.a._evaluate_travel_mode=Mock()
        self.a._is_everyone_away=lambda:False
        self.a._is_anyone_home=lambda:False
        self.a._derive_status=Mock(side_effect=RuntimeError('stop after presence update'))
        original=self.a.away_since
        with self.assertRaisesRegex(RuntimeError, 'stop after presence update'):
            self.a._publish_dashboard_state('test')
        self.assertEqual(self.a.away_since,original)
        self.a._is_anyone_home=lambda:True
        with self.assertRaisesRegex(RuntimeError, 'stop after presence update'):
            self.a._publish_dashboard_state('test')
        self.assertEqual(self.a.away_since,original)

    def test_each_arrival_once_and_report_survives_restart(self):
        a=self.a;a._send_person_return_reports(self.prefs)
        a.call_service.assert_called_once();self.assertEqual(a.call_service.call_args.args[0],'notify/mobile_app_a')
        a.away_since=None;a.cleaned_during_absence=[]
        a._send_person_return_reports(self.prefs);self.assertEqual(a.call_service.call_count,1)
        # No memory cache: a fresh instance reads the persisted pending report.
        restored=module.VacuumAutomation();restored.__dict__.update(a.__dict__)
        self.states['person.b']='home';restored._send_person_return_reports(self.prefs)
        self.assertEqual(a.call_service.call_count,2)
        self.assertEqual(a.call_service.call_args.args[0],'notify/mobile_app_b')
        restored._send_person_return_reports(self.prefs);self.assertEqual(a.call_service.call_count,2)

    def test_first_mode_suppresses_second_arrival(self):
        self.prefs['return_delivery']='first';self.a._send_person_return_reports(self.prefs)
        self.a.away_since=None;self.a.cleaned_during_absence=[]
        self.states['person.b']='home';self.a._send_person_return_reports(self.prefs)
        self.assertEqual(self.a.call_service.call_count,1)

    def test_unknown_and_opt_out_never_get_push(self):
        self.states['person.a']='unknown';self.a._send_person_return_reports(self.prefs)
        self.a.call_service.assert_not_called()
        self.states['person.a']='home';self.prefs['people']['person.a']['enabled']=False
        self.a._send_person_return_reports(self.prefs);self.a.call_service.assert_not_called()

    def test_observation_simulation_and_master_switch_block_push(self):
        for attr in ['observation','simulation']:
            with self.subTest(attr=attr):
                self.a.args['observation_mode']=attr=='observation'
                self.a.simulation_mode=attr=='simulation'
                self.a._return_summary_enabled=lambda:attr!='master'
                self.a._send_person_return_reports(self.prefs)
                self.a.call_service.assert_not_called()

    def test_person_and_global_switches_suppress_notifications(self):
        self.prefs['people']['person.a']['enabled']=False
        self.a._send_person_return_reports(self.prefs)
        self.a.call_service.assert_not_called()
        self.prefs['people']['person.a']['enabled']=True
        self.a._return_summary_enabled=lambda:False
        self.a._send_person_return_reports(self.prefs)
        self.a.call_service.assert_not_called()
        self.a._notification_preferences=lambda:self.prefs
        self.a._start_notifications_enabled=lambda:False
        self.a._send_start_notification('bath')
        self.a.call_service.assert_not_called()

    def test_failure_is_logged_and_not_marked_sent(self):
        self.a.call_service.side_effect=RuntimeError('offline')
        self.a._send_person_return_reports(self.prefs)
        import json
        report=json.loads((self.a.storage_dir/'return_notification.json').read_text())
        self.assertEqual(report['sent'],[])
        self.assertEqual(self.a._audit_event.call_args.args[0],'notification_failed')

    def test_start_push_obeys_per_person_choice_and_master(self):
        self.a._notification_preferences=lambda:self.prefs
        self.prefs['people']['person.b']['enabled']=False
        self.prefs['people']['person.a'].update(enabled=True,start=False,**{'return':False})
        self.a._send_start_notification('bath')
        self.a.call_service.assert_called_once()
        self.a._is_everyone_away=lambda:False
        self.a._send_start_notification('bath');self.assertEqual(self.a.call_service.call_count,1)


class NotificationSettings(unittest.TestCase):
    def test_auto_mapping_needs_matching_companion_app_not_wifi(self):
        services=['notify/mobile_app_schoimphone','notify/mobile_app_other']
        person={'device_trackers':['device_tracker.schoimphone','device_tracker.iphone']}
        self.assertEqual(dashboard.inferred_person_push(person,services),'notify/mobile_app_schoimphone')
        self.assertEqual(dashboard.inferred_person_push({'device_trackers':['device_tracker.iphone']},services),'')
        self.assertEqual(dashboard.inferred_person_push({'device_trackers':['device_tracker.schoimphone','device_tracker.other']},services),'')

    def test_invalid_targets_rejected_and_preferences_preserved(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(dashboard,'DASHBOARD_ORDER_PATH',Path(temp)/'order.json'), patch.object(dashboard,'all_states',return_value=[{'entity_id':'person.a'}]), patch.object(dashboard,'mobile_push_services',return_value=['notify/mobile_app_phone']):
            payload={'person':'person.a','service':'notify/mobile_app_phone','start':True,'return':False}
            dashboard.save_notification_preferences(payload)
            dashboard.save_notification_preferences({'return_delivery':'each'})
            self.assertEqual(dashboard.load_notification_preferences()['people']['person.a'],{**{k:payload[k] for k in ('service','start','return')},'enabled':True})
            for value in ['notify/notify','light/turn_on','notify/mobile_app_missing']:
                with self.assertRaises(ValueError):dashboard.save_notification_preferences({**payload,'service':value})

    def test_person_switch_preserves_recipient_choices(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(dashboard,'DASHBOARD_ORDER_PATH',Path(temp)/'order.json'), patch.object(dashboard,'all_states',return_value=[{'entity_id':'person.a','attributes':{'device_trackers':['device_tracker.phone']}}]), patch.object(dashboard,'mobile_push_services',return_value=['notify/mobile_app_phone']):
            dashboard.save_notification_preferences({'person':'person.a','enabled':True})
            dashboard.save_notification_preferences({'person':'person.a','start':False})
            dashboard.save_notification_preferences({'person':'person.a','enabled':False})
            dashboard.save_notification_preferences({'person':'person.a','enabled':True})
            config=dashboard.load_notification_preferences()['people']['person.a']
            self.assertTrue(config['enabled']);self.assertFalse(config['start']);self.assertTrue(config['return'])
            self.assertEqual(config['service'],'notify/mobile_app_phone')
