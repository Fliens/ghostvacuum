from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from unittest.mock import Mock
import unittest
from test_observation_mode import module
import test_live_dashboard as live
dashboard = live.dashboard

class CalendarDue(unittest.TestCase):
    def setUp(self):
        a=self.a=module.VacuumAutomation()
        a.rooms={'room_1':{'segment_id':1}}
        self.now=datetime(2026,10,12,23,59,59)
        a.datetime=lambda aware=False:self.now
        a.last_cleaned={'room_1':datetime(2026,10,11,12,41,35)}
        self.interval=48
        a._room_interval_h=lambda room:self.interval
        a._room_enabled=lambda room:True
        a._room_suspended_today=lambda room:False
        a._dashboard_room_order=lambda:[]
        a._room_effective_duration_min=lambda room:41
        a._room_configured_duration_min=lambda room:41
        a._current_return_buffer_min=lambda:5
        a._current_start_margin_min=lambda:5
        a._room_weight=lambda room:1
        a.learned_durations={}
        a._get_travel_time_minutes=Mock(side_effect=AssertionError('ETA must not affect due date'))

    def model(self):
        s=live.LiveDashboard().summary()
        s['status']['observation_mode']=False
        s['status']['room_queue']=self.a._build_room_queue(60)
        s['status']['room_stats']=[{'room_key':'room_1','last_cleaned':self.a.last_cleaned.get('room_1').isoformat() if self.a.last_cleaned.get('room_1') else None}]
        s['states']['rooms']=[{'id':'room_1','enabled_state':{'state':'on'},'interval_h_state':{'state':str(self.interval)}}]
        return dashboard.build_live_dashboard_view(s)['rooms'][0]

    def test_midnight_changes_selection_and_dashboard_together(self):
        self.assertFalse(self.a._room_due_info('room_1')['is_due'])
        self.assertIsNone(self.a._select_next_room(60))
        self.assertEqual(self.model()['group'],'rest')
        self.assertEqual(self.model()['next_due'],'2026-10-13')
        self.now=datetime(2026,10,13)
        self.assertEqual(self.a._select_next_room(60),'room_1')
        self.assertEqual(self.model()['group'],'planned')
        self.assertTrue(self.model()['automatic_due'])
        self.assertEqual(self.a._calculate_score('room_1'),(1,1,1))

    def test_completion_time_does_not_affect_due_day(self):
        for hour in (0,12,23):
            self.a.last_cleaned['room_1']=datetime(2026,10,11,hour,59)
            self.assertEqual(self.a._room_due_info('room_1')['next_due_date'],'2026-10-13')
            self.assertIsNone(self.a._select_next_room(60))

    def test_no_eighty_percent_threshold_or_forecast(self):
        self.interval=240
        self.now=datetime(2026,10,20,23,59)  # 90% of ten days, still not due.
        self.assertEqual(self.a._calculate_score('room_1'),(.9,.9,.9))
        self.assertIsNone(self.a._select_next_room(60))
        self.assertEqual(self.model()['group'],'rest')
        self.now=datetime(2026,10,21)
        self.assertEqual(self.a._select_next_room(60),'room_1')

    def test_same_day_completion_prevents_automatic_replanning(self):
        self.interval=24
        self.a.last_cleaned['room_1']=self.now
        self.assertIsNone(self.a._select_next_room(60))
        self.assertEqual(self.model()['group'],'rest')
        self.a._dashboard_room_order=lambda:['room_1']
        self.assertEqual(self.a._select_next_room(60),'room_1')  # Explicit overrides still work.
        self.a._room_suspended_today=lambda room:True
        self.assertIsNone(self.a._select_next_room(60))

    def test_unknown_history_is_due_invalid_interval_is_blocked(self):
        self.a.last_cleaned={}
        self.assertEqual(self.a._select_next_room(60),'room_1')
        self.assertIsNone(self.a._room_due_info('room_1')['next_due_date'])
        for value in (0,-24,float('nan'),float('inf')):
            self.interval=value
            self.assertIsNone(self.a._select_next_room(60))

    def test_local_calendar_across_dst_and_utc_dates(self):
        self.interval=24
        berlin=ZoneInfo('Europe/Berlin')
        for day,nextday in ((datetime(2026,3,29),datetime(2026,3,30)),(datetime(2026,10,25),datetime(2026,10,26))):
            self.a.last_cleaned['room_1']=day.replace(hour=23,tzinfo=berlin)
            self.now=nextday.replace(tzinfo=berlin)
            self.assertTrue(self.a._room_due_info('room_1')['is_due'])
        self.a.last_cleaned['room_1']=datetime(2026,10,10,23,30,tzinfo=timezone.utc) # 11 Oct in Berlin
        self.now=datetime(2026,10,11,21,59,tzinfo=timezone.utc)
        self.assertFalse(self.a._room_due_info('room_1')['is_due'])
        self.now=datetime(2026,10,11,22,tzinfo=timezone.utc)
        self.assertTrue(self.a._room_due_info('room_1')['is_due'])

    def test_legacy_fractional_intervals_round_up_to_whole_days(self):
        for hours,days in ((12,1),(24,1),(36,2),(48,2)):
            self.interval=hours
            self.assertEqual(self.a._room_due_info('room_1')['interval_days'],days)

    def test_badges_count_local_days_instead_of_elapsed_hours(self):
        now=datetime(2026,10,13,0,1,tzinfo=ZoneInfo('Europe/Berlin'))
        self.assertEqual(dashboard.room_due_badge('2026-10-12',now),{'label':'1 Tag überfällig','level':'warning'})
        self.assertEqual(dashboard.room_due_badge('2026-10-11',now)['level'],'danger')
        self.assertEqual(dashboard.room_due_badge('2026-10-14',now)['label'],'Noch nicht fällig')

    def test_display_never_uses_old_forecast_to_invent_a_plan(self):
        s=live.LiveDashboard().summary();s['status']['observation_mode']=False
        s['states']['rooms']=[{'id':'room_1','enabled_state':{'state':'on'}}]
        s['status']['room_queue']=[{'room_key':'room_1','enabled':True,'forecast_score':2,'is_due':False,'next_due_date':'2026-10-13'}]
        self.assertEqual(dashboard.build_live_dashboard_view(s)['rooms'][0]['group'],'rest')
