"""Live dashboard maps observed data conservatively and never fabricates a plan."""
import importlib.util
from pathlib import Path
import unittest
from datetime import datetime, timedelta, timezone

path = Path(__file__).resolve().parents[1] / 'addon/ghostvacuum/redirect_dashboard.py'
spec = importlib.util.spec_from_file_location('live_dashboard', path)
dashboard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dashboard)


class LiveDashboard(unittest.TestCase):
    def test_cleangenius_uses_confirmed_live_flag(self):
        summary = self.summary()
        vacuum = summary['states']['vacuum']
        vacuum['state'] = 'cleaning'
        vacuum['attributes'].update(cleaning_mode='Sweeping and mopping', cleangenius_cleaning=True)
        self.assertTrue(dashboard.build_live_dashboard_view(summary)['robot']['subtitle'].startswith('CleanGenius ·'))
        vacuum['attributes']['cleangenius_cleaning'] = False
        self.assertTrue(dashboard.build_live_dashboard_view(summary)['robot']['subtitle'].startswith('Saugen & Wischen ·'))
        vacuum['state'] = 'docked'
        vacuum['attributes']['cleangenius_cleaning'] = True
        self.assertNotIn('CleanGenius', dashboard.build_live_dashboard_view(summary)['robot']['subtitle'])

    def test_live_summary_preserves_manual_origin_from_sensor(self):
        from unittest.mock import patch
        states = {'global': {}, 'sensors': {'status': {'attributes': {
            'active_room': 'Küche', 'active_room_manual': True}}}}
        with patch.object(dashboard, 'load_options', return_value={}), \
             patch.object(dashboard, 'collect_states', return_value=states), \
             patch.object(dashboard, 'state_for', return_value=None), \
             patch.object(dashboard, 'all_states', return_value=[]):
            summary = dashboard.build_summary()
        self.assertEqual(summary['status']['active_room'], 'Küche')
        self.assertIs(summary['status']['active_room_manual'], True)
        summary['states']['vacuum'] = {'state': 'cleaning', 'attributes': {}}
        self.assertEqual(dashboard.build_live_dashboard_view(summary)['robot']['origin_label'], 'Manuell gestartet')

    def test_manually_queued_room_visible_even_when_not_due(self):
        summary=self.summary()
        summary['status']['observation_mode']=False
        # Use the catalog supplied by this fixture, independent of due scoring.
        initial=dashboard.build_live_dashboard_view(summary)
        room_id=initial['rooms'][0]['id']
        summary['states']['global']={'one_time_room_override':{'state':'["!'+room_id+'"]'}}
        summary['states']['rooms']=[{'id':room_id,'enabled_state':{'state':'on'}}]
        view=dashboard.build_live_dashboard_view(summary)
        room=next(r for r in view['rooms'] if r['id']==room_id)
        self.assertTrue(room['manual_queued'])
        self.assertEqual(room['group'],'planned')

    def test_remaining_time_never_uses_internal_minimum_plan_value(self):
        summary=self.summary()
        summary['states']['vacuum']['state']='cleaning'
        summary['states']['sensors']['active_room']={'state':'Küche','attributes':{'remaining_min':1}}
        self.assertIsNone(dashboard.build_live_dashboard_view(summary)['robot']['remaining'])
        attrs=summary['states']['vacuum']['attributes']
        attrs['remaining_time']=7
        self.assertEqual(dashboard.build_live_dashboard_view(summary)['robot']['remaining'],7)
        for value in (0,-1,float('nan'),float('inf'),'unknown'):
            attrs['remaining_time']=value
            self.assertIsNone(dashboard.build_live_dashboard_view(summary)['robot']['remaining'])

    def test_special_progress_is_named_and_transition_value_is_hidden(self):
        summary = self.summary()
        summary['states']['vacuum'].update(state='cleaning')
        summary['states']['vacuum']['attributes'].update(cleangenius_cleaning=True, cleaning_progress=95)
        signals = {'task_type': {'state': 'stain_cleaning', 'last_changed': '2026-10-11T10:00:00+00:00'},
                   'cleaning_progress': {'state': '80', 'last_changed': '2026-10-11T10:00:20+00:00'}}
        summary['states']['robot'] = {'signals': signals}
        robot = dashboard.build_live_dashboard_view(summary)['robot']
        self.assertEqual(robot['progress_scope'], 'special')
        self.assertEqual(robot['cleaning_mode'], 'Fleckenreinigung')
        self.assertEqual(robot['progress'], 80)
        signals['task_type'].update(state='smart', last_changed='2026-10-11T10:00:19+00:00')
        self.assertIsNone(dashboard.build_live_dashboard_view(summary)['robot']['progress'])
        signals['cleaning_progress'].update(state='42', last_changed='2026-10-11T10:00:40+00:00')
        robot = dashboard.build_live_dashboard_view(summary)['robot']
        self.assertEqual(robot['progress'], 42)
        self.assertEqual(robot['cleaning_mode'], 'CleanGenius')
        signals['cleaning_progress']['state'] = 'unavailable'
        self.assertIsNone(dashboard.build_live_dashboard_view(summary)['robot']['progress'])

    def test_station_cycle_never_looks_like_room_cleaning(self):
        for state in ('returning', 'washing', 'drying'):
            summary = self.summary()
            summary['states']['vacuum'].update(state='cleaning')
            summary['states']['vacuum']['attributes'].update(started=False, running=False)
            summary['states']['robot'] = {'signals': {'state': {'state': state}, 'task_status': {'state': 'completed'}}}
            robot = dashboard.build_live_dashboard_view(summary)['robot']
            self.assertFalse(robot['cleaning'])
            self.assertNotIn('Reinigt', robot['title'])
            self.assertEqual(robot['subtitle'], 'Reinigung abgeschlossen')

    def summary(self):
        return {
            'status': {'observation_mode': 'on', 'anyone_home': 'off', 'everyone_away': 'on',
                       'travel_mode_active': False, 'travel_pause_after_hours': 24,
                       'presence_summary': [{'entity_id': 'person.simon', 'state': 'not_home',
                                             'travel_time_min': 48}], 'room_queue': [], 'room_stats': []},
            'states': {'vacuum': {'state': 'docked', 'attributes': {'battery': 90, 'current_segment': 1}},
                       'sensors': {'return_window': {'state': '38'}}},
            'room_segments': [{'room_key': 'room_1', 'segment_id': 1, 'name': 'Büro'},
                              {'room_key': 'room_2', 'segment_id': 2, 'name': 'Küche'}],
        }

    def test_near_home_with_wifi_uses_buffer_instead_of_wifi_waiting(self):
        summary = self.summary()
        summary['status'].update(anyone_home=False, everyone_away=True,observation_mode=False,return_policy={'allowed':False,'reason':'Puffergrenze erreicht','eta_min':1})
        summary['states']['global']={'enabled':{'state':'on'},'return_buffer':{'state':'5'}}
        summary['status']['presence_summary'][0].update(state='not_home', presence='away', near_home_unconfirmed=True, travel_time_min=1)
        view = dashboard.build_live_dashboard_view(summary)
        self.assertEqual(view['people'][0]['detail'], 'Im Puffer · 1 min bis zuhause')
        self.assertEqual(view['people'][0]['kind'], 'away')
        self.assertEqual(view['presence_summary'], 'Unterwegs')
        self.assertIsNone(view['people'][0]['stay_progress'])
        self.assertIsNone(view['people'][0]['countdown'])
        self.assertEqual(view['reason'], 'Innerhalb des Puffers')
        self.assertEqual(view['cleaning_permission']['reason'],'Innerhalb des Puffers')
        self.assertFalse(view['cleaning_permission']['ready'])
        self.assertEqual(view['window'], 0)

    def test_permission_follows_actual_start_policy_instead_of_positive_time_window(self):
        summary = self.summary()
        summary['status'].update(observation_mode=False, return_policy={'allowed':False, 'reason':'Startabstand noch nicht erreicht'})
        summary['states']['global'] = {'enabled':{'state':'on'}, 'return_buffer':{'state':'5'}}
        summary['states']['sensors']['return_window']['state'] = '7'
        view = dashboard.build_live_dashboard_view(summary)
        self.assertGreater(view['window'], 0)
        self.assertFalse(view['cleaning_permission']['ready'])
        self.assertEqual(view['cleaning_permission']['title'],'Automatik wartet')
        self.assertEqual(view['cleaning_permission']['reason'],'Noch zu nah zuhause')
        summary['status']['return_policy'].update(allowed=True,stationary_release=True)
        permission = dashboard.build_live_dashboard_view(summary)['cleaning_permission']
        self.assertTrue(permission['ready'])
        self.assertEqual(permission['reason'],'Aufenthalt bestätigt')
        summary['states']['global']['enabled']['state'] = 'off'
        permission = dashboard.build_live_dashboard_view(summary)['cleaning_permission']
        self.assertFalse(permission['ready'])
        self.assertEqual(permission['title'],'Automatik deaktiviert')

    def test_dwell_progress_only_when_outside_buffer_and_waiting_for_dwell(self):
        summary = self.summary()
        summary['status'].update(observation_mode=False,return_policy={'allowed':False,'reason':'Startabstand noch nicht erreicht','start_margin_min':5})
        summary['states']['global'] = {'enabled':{'state':'on'},'return_buffer':{'state':'5'}}
        person = summary['status']['presence_summary'][0]
        person.update(travel_time_min=7,motion='observing',stay_minutes=3)
        view = dashboard.build_live_dashboard_view(summary)
        self.assertEqual(view['cleaning_permission']['title'],'Automatik wartet')
        self.assertEqual(view['cleaning_permission']['reason'],'Freigabe nach 10 Minuten Aufenthalt')
        self.assertEqual(view['people'][0]['stay_progress'],{'minutes':3,'required':10})
        person['travel_time_min'] = 5
        self.assertIsNone(dashboard.build_live_dashboard_view(summary)['people'][0]['stay_progress'])
        person['travel_time_min'] = 25
        self.assertIsNone(dashboard.build_live_dashboard_view(summary)['people'][0]['stay_progress'])
        person.update(travel_time_min=7,motion='unknown')
        self.assertIsNone(dashboard.build_live_dashboard_view(summary)['people'][0]['stay_progress'])

    def test_journey_stations_and_missing_location(self):
        base = {'presence':'away','state':'not_home','travel_time_min':7,'motion':'observing'}
        view = {'kind':'away','stay_progress':{'minutes':3,'required':10}}
        policy = {'start_margin_min':5}
        status = {'travel_pause_radius_km':100,'travel_pause_after_hours':24}
        for changes, station, tone in [
            ({'travel_time_min':3},1,'waiting'),
            ({},2,'waiting'),
            ({'stationary':True},2,'ready'),
            ({'travel_time_min':25},3,'ready'),
            ({'outside_radius':True},4,'ready'),
            ({'travel_mode_active':True,'outside_radius':True},5,'travel'),
            ({'motion':'unknown'},None,'unknown'),
            ({'travel_time_min':None},None,'unknown')]:
            with self.subTest(changes=changes):
                journey = dashboard.person_journey_scale({**base,**changes},view,5,policy,status)
                self.assertEqual(journey['station'],station)
                self.assertEqual(journey['tone'],tone)
                self.assertEqual(len(journey['steps']),6)
                if station!=2:
                    self.assertIsNone(journey['progress'])
                if station is None:
                    self.assertIsNone(journey['eta'])
                    self.assertFalse(journey['approaching'])
        home = dashboard.person_journey_scale({'motion':'unknown'},{'kind':'home','presence_source':'wifi'},5,policy,status)
        self.assertEqual(home['station'],0)
        self.assertEqual(home['steps'][0]['hint'],'WLAN')
        returning = dashboard.person_journey_scale({**base,'motion':'approaching'},{'kind':'away'},5,policy,status)
        self.assertTrue(returning['approaching'])
        self.assertIsNone(returning['progress'])

    def test_radius_station_has_individual_travel_countdown(self):
        now = datetime(2026,10,11,12,tzinfo=timezone.utc)
        person = {'travel_time_min':180,'outside_radius':True,'outside_since':(now-timedelta(hours=2)).isoformat()}
        journey = dashboard.person_journey_scale(person,{'kind':'away'},5,{'start_margin_min':5},{'travel_pause_after_hours':24},now=now)
        self.assertEqual(journey['station'],4)
        self.assertEqual(journey['note'],'Reisemodus in 22 h 00 min')
        self.assertEqual(journey['travel_progress'], {'minutes':120, 'required':1440})

    def test_each_person_shows_only_the_strongest_status(self):
        summary = self.summary()
        summary['status']['observation_mode'] = False
        summary['states']['global'] = {'return_buffer':{'state':'5'}}
        summary['status']['return_policy'] = {'allowed':False,'eta_min':1,'timed_eta_min':1,'start_margin_min':5}
        p = summary['status']['presence_summary'][0]
        p.update(motion='observing',stay_minutes=3,travel_time_min=1)
        view = dashboard.build_live_dashboard_view(summary)['people'][0]
        self.assertEqual(view['detail'],'Im Puffer · 1 min bis zuhause')
        self.assertIsNone(view['stay_progress'])
        self.assertIsNone(view['countdown'])
        p['travel_time_min'] = 7
        view = dashboard.build_live_dashboard_view(summary)['people'][0]
        self.assertEqual(view['detail'],'ca. 7 min bis zuhause')
        self.assertEqual(view['stay_progress'],{'minutes':3,'required':10})
        self.assertIsNone(view['countdown'])
        p['travel_time_min'] = 20
        self.assertEqual(dashboard.build_live_dashboard_view(summary)['people'][0]['detail'],'ca. 20 min bis zuhause')
        p.update(state='home')
        view = dashboard.build_live_dashboard_view(summary)['people'][0]
        self.assertEqual(view['detail'],'Zuhause')
        self.assertIsNone(view['stay_progress'])
        self.assertIsNone(view['countdown'])

    def test_short_homeway_explains_buffer_and_unconfirmed_cafe_stay(self):
        summary = self.summary()
        summary['status'].update(observation_mode=False, return_policy={'allowed':False, 'eta_min':1, 'timed_eta_min':1})
        summary['states']['global'] = {'return_buffer':{'state':'5'}}
        model = dashboard.build_live_dashboard_view(summary)
        self.assertEqual(model['caption'], 'Kein automatischer Start')
        self.assertEqual('Innerhalb des Puffers', model['reason'])
        self.assertEqual(model['calculation'], '1 min bis zuhause · Puffer 5 min')
        self.assertEqual(model['window'], 0)
        summary['status']['presence_summary'][0].update(motion='observing',stay_minutes=3)
        self.assertIsNone(dashboard.build_live_dashboard_view(summary)['people'][0]['countdown'])
        summary['status']['return_policy'].update(allowed=True, stationary_release=True, timed_eta_min=None)
        summary['status']['presence_summary'][0].update(stationary=True,motion='staying')
        self.assertEqual(dashboard.build_live_dashboard_view(summary)['caption'], 'Maximale Reinigungszeit')
        summary['status']['return_policy'].update(allowed=False,stationary_release=False,timed_eta_min=1)
        summary['status']['presence_summary'][0].update(stationary=False,motion='approaching')
        self.assertEqual(dashboard.build_live_dashboard_view(summary)['reason'], 'Innerhalb des Puffers')

    def test_pending_departure_does_not_claim_person_is_home(self):
        summary = self.summary()
        summary['available_presence_entities'] = [{'entity_id':'person.simon', 'presence_source':'gps', 'state':'not_home'}]
        for effective in ('home', 'unknown'):
            summary['status']['presence_summary'][0].update(state=effective, presence=effective)
            model = dashboard.build_live_dashboard_view(summary)
            self.assertEqual(model['people'][0]['detail'], 'Abwesenheit wird bestätigt')
            self.assertEqual(model['people'][0]['kind'], 'unknown')
            self.assertEqual(model['presence_summary'], 'Bestätigung ausstehend')
            self.assertEqual(model['reason'], 'Abwesenheit wird bestätigt')
            self.assertEqual(model['window'], 0)

    def test_presence_source_icon_uses_actual_tracker_type_not_home_state(self):
        summary = self.summary()
        for source, icon in [('router', 'wifi'), ('gps', 'mobile'), (None, None)]:
            summary['available_presence_entities'] = [{'entity_id':'person.simon', 'presence_source':source}]
            self.assertEqual(dashboard.build_live_dashboard_view(summary)['people'][0]['presence_source'], icon)

    def test_buffer_is_subtracted_once_and_disabled_rooms_are_explicit(self):
        summary = self.summary()
        summary['states']['global'] = {'return_buffer': {'state': '5.0'}}
        summary['states']['rooms'] = [{'id': 'room_1', 'enabled_state': {'state': 'off'}},
                                      {'id': 'room_2', 'enabled_state': {'state': 'on'}}]
        model = dashboard.build_live_dashboard_view(summary)
        self.assertEqual(model['window'], 33)
        self.assertEqual(model['buffer_min'], 5)
        self.assertEqual(model['calculation'], 'Zeitfenster 38 min − Puffer 5 min = 33 min')
        self.assertEqual([r['id'] for r in model['rooms']], ['room_2', 'room_1'])
        self.assertTrue(model['rooms'][0]['enabled'])
        self.assertFalse(model['rooms'][1]['enabled'])
        summary['status']['presence_summary'][0]['state'] = 'home'
        self.assertEqual(dashboard.build_live_dashboard_view(summary)['window'], 0)

    def test_analysis_hypothetical_decision_does_not_override_live_reason(self):
        summary = self.summary()
        summary['status']['observation_mode'] = False
        summary['states']['global'] = {'enabled': {'state': 'on'}}
        summary['status']['presence_summary'][0]['state'] = 'home'
        summary['status']['return_analysis'] = {'decision': 'Würde warten', 'detail': 'Person zuhause'}
        model = dashboard.build_live_dashboard_view(summary)
        self.assertTrue(model['automation_enabled'])
        self.assertEqual(model['reason'], 'Person zuhause')
        self.assertEqual(model['caption'], 'Keine Reinigung')
        self.assertEqual(model['window'], 0)

    def test_due_badge_thresholds_and_unknown_history(self):
        now = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
        for hours, level in [(0, 'neutral'), (24, 'warning'), (24.01, 'warning'), (47.99, 'danger'), (48, 'danger'), (72, 'danger')]:
            badge = dashboard.room_due_badge((now - timedelta(hours=hours)).isoformat(), now)
            self.assertEqual(badge['level'], level)
        self.assertEqual(dashboard.room_due_badge(None, now), {'label':'Fällig', 'level':'neutral'})
        self.assertEqual(dashboard.room_due_badge((now-timedelta(days=2)).isoformat(), now)['label'], '2 Tage überfällig')

    def test_live_interval_uses_current_helper_over_status_snapshot(self):
        summary = self.summary()
        summary['states']['rooms'] = [{'id':'room_1', 'interval_h_state':{'state':'48'}}]
        model = dashboard.build_live_dashboard_view(summary)
        self.assertEqual(model['rooms'][0]['interval_h'], 48)
        self.assertEqual(model['rooms'][0]['due_badge']['label'], 'Fällig')

    def test_duration_display_uses_learned_values_and_never_configured_placeholder(self):
        summary = self.summary()
        summary['status']['room_stats'] = [{'room_key':'room_1', 'configured_duration_min':1, 'effective_duration_min':1}]
        model = dashboard.build_live_dashboard_view(summary)
        self.assertIsNone(model['rooms'][0]['duration'])
        summary['status']['room_stats'][0]['learned_duration_min'] = 12
        self.assertEqual(dashboard.build_live_dashboard_view(summary)['rooms'][0]['duration'], 12)
        html = dashboard.render_settings_rooms(summary)
        self.assertNotIn('konfiguriert', html)
        self.assertNotIn('gelernt', html)

    def test_buffer_settings_render_as_integer(self):
        html = dashboard.render_setting_number('return_buffer', 'Puffer', '', 'input_number.buffer', {'state': '5.0'})
        self.assertIn('value="5"', html)
        self.assertIn('step="1"', html)

    def test_home_blocks_window_and_idle_segment_is_not_active(self):
        summary = self.summary()
        summary['status']['presence_summary'][0]['state'] = 'home'
        model = dashboard.build_live_dashboard_view(summary)
        self.assertEqual(model['window'], 0)
        self.assertIsNone(model['robot']['origin_label'])
        self.assertEqual(model['caption'], 'Keine Reinigung')
        self.assertEqual([r['group'] for r in model['rooms']], ['rest', 'rest'])
        self.assertTrue(model['commands_locked'])

    def test_observation_does_not_display_stale_queue_or_invent_history(self):
        summary = self.summary()
        summary['status']['room_queue'] = [{'room_key': 'room_1', 'enabled': True, 'forecast_score': 9}]
        model = dashboard.build_live_dashboard_view(summary)
        self.assertTrue(all(r['group'] == 'rest' for r in model['rooms']))
        self.assertTrue(all(r['last'] is None and r['next_due'] is None for r in model['rooms']))
        self.assertEqual(model['window'], 38)

    def test_unknown_presence_never_offers_time(self):
        summary = self.summary()
        summary['status']['presence_summary'][0]['state'] = 'unavailable'
        self.assertIsNone(dashboard.build_live_dashboard_view(summary)['window'])

    def test_string_false_does_not_activate_travel_mode(self):
        summary = self.summary()
        summary['status']['travel_mode_active'] = 'false'
        summary['status']['presence_summary'][0]['travel_mode_active'] = 'false'
        model = dashboard.build_live_dashboard_view(summary)
        self.assertEqual(model['window'], 38)
        self.assertEqual(model['people'][0]['kind'], 'away')

    def test_individual_travel_timer_and_active_travel(self):
        summary = self.summary()
        person = summary['status']['presence_summary'][0]
        person.update(outside_since=(datetime.now(timezone.utc) - timedelta(hours=22)).isoformat(), outside_radius=True)
        model = dashboard.build_live_dashboard_view(summary)
        self.assertIsNone(model['people'][0]['countdown'])
        person['travel_mode_active'] = True
        summary['status']['travel_mode_active'] = True
        model = dashboard.build_live_dashboard_view(summary)
        self.assertEqual(model['window'], 0)
        self.assertEqual(model['people'][0]['detail'], 'Reisemodus')

    def test_cleaning_uses_current_segment_and_keeps_each_room_once(self):
        summary = self.summary()
        summary['states']['vacuum']['state'] = 'cleaning'
        model = dashboard.build_live_dashboard_view(summary)
        self.assertEqual(model['robot']['active_name'], 'Büro')
        self.assertEqual(model['robot']['origin_label'], 'Manuell / extern gestartet')
        summary['states']['vacuum']['state'] = 'paused'
        self.assertEqual(dashboard.build_live_dashboard_view(summary)['robot']['origin_label'], 'Manuell / extern gestartet')
        self.assertEqual(sum(r['group'] == 'active' for r in model['rooms']), 1)
        self.assertEqual(len({r['id'] for r in model['rooms']}), 2)
        self.assertIsNone(model['robot']['remaining'])

    def test_html_escapes_script_in_sensor_names_and_contains_no_demo(self):
        summary = self.summary()
        summary['room_segments'][0]['name'] = '</script><script>alert(1)</script>'
        output = dashboard.render_live_observation_dashboard(summary)
        self.assertIn('data-live-observation="true"', output)
        self.assertNotIn('</script><script>alert(1)', output)
        self.assertNotIn('Beispieldaten', output)
        self.assertNotIn('data-scenario', output)
        self.assertIn('draggable="false"', output)

    def test_live_mode_unlocks_commands_without_turning_on_automation(self):
        summary = self.summary()
        summary['status']['observation_mode'] = 'off'
        summary['states']['global'] = {'enabled': {'state': 'off'}}
        model = dashboard.build_live_dashboard_view(summary)
        self.assertFalse(model['commands_locked'])
        self.assertFalse(model['automation_enabled'])
        summary['states']['global']['enabled']['state'] = 'on'
        self.assertTrue(dashboard.build_live_dashboard_view(summary)['automation_enabled'])

    def test_modern_dashboard_remains_in_live_mode(self):
        from unittest.mock import patch
        summary = self.summary()
        summary['status']['observation_mode'] = 'off'
        with patch.object(dashboard,'build_summary',return_value=summary):
            html = dashboard.render_dashboard_html()
        self.assertIn('data-live-observation="true"',html)
        self.assertIn('pause-cleaning',html)
        self.assertIn('"commands_locked": false',html)
