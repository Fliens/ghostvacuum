"""No real HA/device calls: simulate WLAN loss, AP failure and new GPS fixes."""
from datetime import timedelta
import unittest
import test_presence_logic as presence_tests


class WifiDepartureGuard(unittest.TestCase):
    person = presence_tests.PresenceLogic.person
    get_state = presence_tests.PresenceLogic.get_state
    def setUp(self):
        presence_tests.PresenceLogic.setUp(self)
        self.person('person.a', 'home', 52)
        self.states['person.a']['attributes'].update(device_trackers=['device_tracker.wifi', 'device_tracker.gps'])
        self.states['device_tracker.wifi'] = {'state': 'home', 'last_changed': self.now.isoformat(), 'attributes': {'source_type': 'router'}}
        self.fix(52.01, -1)
        self.app._current_return_buffer_min = lambda: 5
        self.app._current_start_margin_min = lambda: 5
        self.app._current_start_hour = lambda: 8
        self.app._current_end_hour = lambda: 22
        self.app._current_active_weekdays = lambda: list(range(7))
        self.app.travel_mode_active = False

    def policy(self, running=False):
        return self.app._return_policy(running=running)

    def fix(self, lat, seconds=1, accuracy=10):
        self.states['device_tracker.gps'] = {'state': 'not_home', 'last_updated': (self.now + timedelta(seconds=seconds)).isoformat(), 'attributes': {'source_type': 'gps', 'gps_accuracy': accuracy, 'latitude': lat, 'longitude': 13}}

    def lose_wifi(self, state='not_home'):
        self.now += timedelta(seconds=5)
        self.states['device_tracker.wifi'].update(state=state, last_changed=self.now.isoformat())
        self.states['person.a']['state'] = 'not_home'

    def test_old_away_gps_never_releases_after_wifi_loss(self):
        self.assertEqual(self.app._presence_state('person.a'), 'home')
        self.lose_wifi()
        self.assertEqual(self.app._presence_state('person.a'), 'home')
        self.states['device_tracker.gps']['last_reported'] = self.now.isoformat()
        self.assertFalse(self.app._person_is_away('person.a'))
        self.fix(52.01)
        self.now += timedelta(seconds=2)
        self.assertTrue(self.app._person_is_away('person.a'))

    def test_ap_unavailable_needs_fresh_precise_outside_fix(self):
        self.app._presence_state('person.a')
        self.lose_wifi('unavailable')
        self.app._presence_state('person.a')
        for lat, accuracy in [(52, 10), (52.0009, 30), (52.01, 100)]:
            self.fix(lat, accuracy=accuracy)
            self.now += timedelta(seconds=2)
            # A fresh near-home fix may say unterwegs, but can never release cleaning.
            self.assertFalse(self.policy()['allowed'])
        self.fix(52.01)
        self.now += timedelta(seconds=2)
        self.assertTrue(self.app._person_is_away('person.a'))

    def test_restart_away_and_reconnection_create_new_barriers(self):
        self.lose_wifi()
        self.assertEqual(self.app._presence_state('person.a'), 'unknown')
        self.fix(52.01); self.now += timedelta(seconds=2)
        self.assertTrue(self.app._person_is_away('person.a'))
        self.app._wifi_departure_guards = {}
        self.assertFalse(self.app._person_is_away('person.a'))
        self.states['device_tracker.wifi']['state'] = 'home'
        self.assertEqual(self.app._presence_state('person.a'), 'home')
        self.lose_wifi()
        self.assertFalse(self.app._person_is_away('person.a'))

    def test_stale_fix_and_missing_source_fail_closed(self):
        self.lose_wifi(); self.app._presence_state('person.a')
        self.fix(52.01); self.now += timedelta(minutes=11)
        self.assertFalse(self.app._person_is_away('person.a'))
        del self.states['device_tracker.wifi']['attributes']['source_type']
        self.assertFalse(self.app._person_is_away('person.a'))

    def test_wifi_home_beats_even_fresh_outside_fix(self):
        self.fix(52.01); self.now += timedelta(seconds=2)
        self.states['person.a']['state'] = 'not_home'
        self.assertEqual(self.app._presence_state('person.a'), 'home')

    def test_unavailable_tracker_without_attributes_is_not_ignored_at_startup(self):
        self.lose_wifi('unavailable')
        self.states['device_tracker.wifi']['attributes'] = {}
        self.assertFalse(self.app._person_is_away('person.a'))

    def test_release_expires_and_second_outage_requires_another_fix(self):
        self.app._presence_state('person.a'); self.lose_wifi()
        self.app._presence_state('person.a'); self.fix(52.01)
        self.now += timedelta(seconds=2)
        self.assertTrue(self.app._person_is_away('person.a'))
        self.lose_wifi('unavailable')
        self.assertFalse(self.app._person_is_away('person.a'))
        self.fix(52.01); self.now += timedelta(seconds=2)
        self.assertTrue(self.app._person_is_away('person.a'))
        self.now += timedelta(minutes=11)
        self.assertFalse(self.app._person_is_away('person.a'))

    def test_appdaemon_local_naive_clock_does_not_shift_gps_freshness(self):
        from zoneinfo import ZoneInfo
        # AppDaemon defaults to naive Berlin time, container system clock is UTC.
        self.app.datetime = lambda aware=False: self.now.astimezone(ZoneInfo('Europe/Berlin')) if aware else self.now.astimezone(ZoneInfo('Europe/Berlin')).replace(tzinfo=None)
        self.assertEqual(self.app._presence_state('person.a'), 'home')
        self.lose_wifi(); self.app._presence_state('person.a')
        self.fix(52.01); self.now += timedelta(seconds=2)
        self.assertTrue(self.app._person_is_away('person.a'))

    def test_gps_home_does_not_confirm_return_without_wifi(self):
        self.app._presence_state('person.a')
        self.lose_wifi(); self.app._presence_state('person.a')
        self.fix(52.01); self.now += timedelta(seconds=2)
        self.assertTrue(self.app._person_is_away('person.a'))
        self.states['person.a']['state'] = 'home'
        self.fix(52.0004)
        self.states['device_tracker.gps']['state'] = 'home'
        self.now += timedelta(seconds=2)
        self.assertEqual(self.app._presence_state('person.a'), 'not_home')
        self.assertTrue(self.app._person_is_away('person.a'))
        summary = self.app._person_presence_summary('person.a')
        self.assertTrue(summary['near_home_unconfirmed'])
        self.assertEqual(summary['travel_time_min'],1)
        self.assertEqual(self.policy()['reason'],'Puffergrenze erreicht')
        self.assertFalse(self.policy(True)['allowed'])
        self.states['device_tracker.wifi']['state'] = 'home'
        summary = self.app._person_presence_summary('person.a')
        self.assertEqual(summary['presence'], 'home')
        self.assertEqual(summary['presence_source'], 'router')
        self.assertFalse(summary['near_home_unconfirmed'])

    def test_gps_home_without_wifi_assignment_still_confirms_return(self):
        self.states['person.a']['attributes']['device_trackers'] = ['device_tracker.gps']
        self.assertEqual(self.app._presence_state('person.a'), 'home')
        self.assertFalse(self.app._person_presence_summary('person.a')['near_home_unconfirmed'])

    def test_stationary_or_continuing_cannot_release_fresh_home_area(self):
        self.app._presence_state('person.a'); self.lose_wifi()
        self.app._presence_state('person.a'); self.fix(52)
        self.now += timedelta(seconds=2)
        person = self.app._person_presence_summary('person.a')
        person.update(stationary=True,monitored_absence=True,motion='observing')
        self.app._presence_summary = lambda: [person]
        for running in (False,True):
            self.assertFalse(self.policy(running)['allowed'])
            self.assertEqual(self.policy(running)['reason'],'Puffergrenze erreicht')

    def test_near_home_gps_expires_without_confirming_arrival_or_releasing(self):
        self.lose_wifi(); self.app._presence_state('person.a'); self.fix(52)
        self.now += timedelta(seconds=2)
        self.assertTrue(self.app._person_presence_summary('person.a')['near_home_unconfirmed'])
        self.now += timedelta(minutes=11)
        person = self.app._person_presence_summary('person.a')
        self.assertEqual(person['presence'],'unknown')
        self.assertFalse(person['near_home_unconfirmed'])
        self.assertFalse(self.policy()['allowed'])

    def test_native_gps_home_cannot_override_fresh_outside_fix_without_wifi(self):
        self.lose_wifi(); self.app._presence_state('person.a')
        self.fix(52.01); self.now += timedelta(seconds=2)
        self.states['person.a']['state'] = 'home'
        self.assertEqual(self.app._presence_state('person.a'),'not_home')
