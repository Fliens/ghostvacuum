import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch
import test_live_dashboard as live_tests
dashboard = live_tests.dashboard


class DashboardSchedule(unittest.TestCase):
    def summary(self):
        s = live_tests.LiveDashboard().summary()
        s['status']['observation_mode'] = False
        s['states']['global'] = {'active_weekdays': {'state': 'mon,tue,wed,thu,fri,sat'},
                                'start_hour': {'state': '8'}, 'end_hour': {'state': '22'}}
        return s

    def now(self, day, hour, minute=0):
        return datetime(2026, 10, day, hour, minute, tzinfo=ZoneInfo('Europe/Berlin'))

    def test_disabled_weekday_is_visible_even_when_person_home(self):
        summary = self.summary()
        notice = dashboard.dashboard_schedule_notice(summary, self.now(11, 12))
        self.assertEqual(notice, 'Sonntag · Automatik pausiert')
        summary['status']['presence_summary'][0]['state'] = 'home'
        with patch.object(dashboard, 'dashboard_schedule_notice', return_value=notice):
            model = dashboard.build_live_dashboard_view(summary)
        self.assertEqual(model['schedule_notice'], notice)
        self.assertEqual(model['reason'], 'Person zuhause')
        self.assertEqual(model['window'], 0)

    def test_before_start_after_end_and_exact_start(self):
        summary = self.summary()
        for hour in (7, 22, 23):
            notice = dashboard.dashboard_schedule_notice(summary, self.now(12, hour))
            self.assertEqual(notice, 'Außerhalb der Reinigungszeit')
        self.assertIsNone(dashboard.dashboard_schedule_notice(summary, self.now(12, 8)))
        self.assertIsNone(dashboard.dashboard_schedule_notice(summary, self.now(12, 21, 54)))
        self.assertEqual('Reinigungszeit endet', dashboard.dashboard_schedule_notice(summary, self.now(12, 21, 55)))

    def test_blocked_schedule_never_claims_available_cleaning_time(self):
        with patch.object(dashboard, 'dashboard_schedule_notice', return_value='Keine automatischen Starts · Reinigungszeit 08:00–22:00'):
            model = dashboard.build_live_dashboard_view(self.summary())
        self.assertEqual(model['window'], 0)
        self.assertEqual(model['caption'], 'Keine automatische Reinigung')

    def test_numeric_weekdays_and_no_schedule_snapshot(self):
        s = self.summary()
        s['states']['global']['active_weekdays']['state'] = '0,1,2,3,4,5'
        self.assertIsNone(dashboard.dashboard_schedule_notice(s, self.now(12, 12)))
        self.assertIsNone(dashboard.dashboard_schedule_notice(live_tests.LiveDashboard().summary(), self.now(11, 12)))
