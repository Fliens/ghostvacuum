import unittest
from test_observation_settings import d


class RoomSettingsOrder(unittest.TestCase):
    def test_groups_and_ranks_follow_persisted_weights(self):
        summary = {
            'room_segments': [{'room_key': key, 'segment_id': i, 'name': name}
                              for i, (key, name) in enumerate([('off', 'Büro'), ('low', 'Bad'), ('high', 'Küche')])],
            'states': {'rooms': [
                {'id': key, 'enabled': f'input_boolean.{key}', 'enabled_state': {'state': enabled},
                 'weight': f'input_number.{key}', 'weight_state': {'state': weight}}
                for key, enabled, weight in [('off', 'off', 3), ('low', 'on', 1), ('high', 'on', 2)]]}}
        html = d.render_settings_rooms(summary)
        active, inactive = html.split('data-room-group="inactive"')
        self.assertLess(active.index('data-room-key="high"'), active.index('data-room-key="low"'))
        self.assertNotIn('data-room-key="off"', active)
        self.assertIn('data-room-key="off"', inactive)
        self.assertIn('data-weight-entity="input_number.high"', active)
        self.assertIn('draggable="true"', active)
        self.assertNotIn('draggable="true"', inactive)

    def test_empty_active_group_keeps_inactive_rooms_visible(self):
        html = d.render_settings_rooms({'room_segments':[{'room_key':'off', 'name':'Büro'}],
                'states':{'rooms':[{'id':'off','enabled_state':{'state':'off'}}]}})
        self.assertIn('Keine aktiven Räume.', html)
        self.assertIn('data-room-key="off"', html)
