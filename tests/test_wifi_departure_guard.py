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
            self.assertFalse(self.app._person_is_away('person.a'))
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
