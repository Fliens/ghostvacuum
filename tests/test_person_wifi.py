import unittest
from test_observation_settings import d


def tracker(entity, name, source='router', state='home'):
    return {'entity_id':entity, 'state':state, 'attributes':{'friendly_name':name, 'source_type':source}}


class PersonWifi(unittest.TestCase):
    def setUp(self):
        self.states = [tracker('device_tracker.iphone', 'iPhone'),
                       tracker('device_tracker.iphone_2', 'iPhone'),
                       tracker('device_tracker.watch', 'Watch', state='not_home'),
                       tracker('device_tracker.gps', 'iPhone GPS', 'gps'),
                       tracker('device_tracker.vacuum', 'MOVA Vacuum'),
                       tracker('device_tracker.ap', 'U7 Lite'),
                       tracker('light.phone', 'iPhone Lamp'),
                       tracker('sensor.phone', 'iPhone Battery')]
        self.person = {'id':'simon', 'device_trackers':['device_tracker.gps', 'device_tracker.watch']}

    def test_only_personal_router_trackers_are_offered(self):
        self.assertEqual({item['entity_id'] for item in d.wifi_presence_candidates(self.states)},
                         {'device_tracker.iphone','device_tracker.iphone_2','device_tracker.watch'})

    def test_replacement_and_removal_keep_gps(self):
        self.assertEqual(d.updated_person_trackers(self.person,'device_tracker.iphone',self.states,[]),
                         ['device_tracker.gps','device_tracker.iphone'])
        self.assertEqual(d.updated_person_trackers(self.person,'',self.states,[]), ['device_tracker.gps'])

    def test_foreign_entities_and_other_persons_devices_rejected(self):
        for entity in ['light.phone','device_tracker.vacuum','device_tracker.gps','device_tracker.missing']:
            with self.assertRaises(ValueError):d.updated_person_trackers(self.person,entity,self.states,[])
        with self.assertRaises(ValueError):
            d.updated_person_trackers(self.person,'device_tracker.iphone',self.states,[{'device_trackers':['device_tracker.iphone']}])

    def test_picker_shows_assignment_and_disambiguates_duplicate_names(self):
        person = {'entity_id':'person.simon','name':'Simon',**self.person}
        html=d.render_person_wifi_picker(person,{'wifi_presence_candidates':d.wifi_presence_candidates(self.states),
                                                'available_presence_entities':[person]})
        self.assertIn('iPhone (iphone)',html)
        self.assertIn('iPhone (iphone_2)',html)
        self.assertIn('Kein zusätzliches Gerät',html)
        self.assertIn('value="device_tracker.watch" selected',html)
        self.assertNotIn('device_tracker.gps',html)

    def test_presence_page_contains_optional_picker(self):
        person={'entity_id':'person.simon','name':'Simon','state':'home','has_device':True,**self.person}
        html=d.render_settings_presence({'available_presence_entities':[person],
                'wifi_presence_candidates':d.wifi_presence_candidates(self.states),
                'options':{'presence_entities':['person.simon']}})
        self.assertIn('data-person-wifi="person.simon"',html)

    def test_native_ha_update_retains_gps_and_writes_only_trackers(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        sent = []
        replies = iter([{'type':'auth_required'}, {'type':'auth_ok'},
                        {'success':True, 'result':{'storage':[self.person], 'config':[]}},
                        {'success':True, 'result':self.person}])
        class Connection:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def receive_json(self, **kwargs): return next(replies)
            async def send_json(self, data): sent.append(data)
            def ws_connect(self, url): return self
        fake = SimpleNamespace(ClientSession=lambda **kwargs:Connection(), ClientTimeout=lambda **kwargs:None)
        with patch.dict('sys.modules', {'aiohttp':fake}), patch.dict(d.os.environ, {'SUPERVISOR_TOKEN':'test-token'}, clear=True), \
             patch.object(d,'state_for',return_value={'attributes':{'id':'simon'}}), patch.object(d,'all_states',return_value=self.states):
            d.save_person_wifi('person.simon','device_tracker.iphone')
        self.assertEqual(sent[-1], {'id':2,'type':'person/update','person_id':'simon',
                                  'device_trackers':['device_tracker.gps','device_tracker.iphone']})
