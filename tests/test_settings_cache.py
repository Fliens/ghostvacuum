import unittest
from unittest.mock import patch
from test_live_dashboard import dashboard

class SettingsCache(unittest.TestCase):
    def test_successful_write_invalidates_old_room_snapshot(self):
        with patch.object(dashboard,'local_dev_enabled',return_value=False), patch.object(dashboard,'api_request',return_value=[]) as api, patch.dict(dashboard._all_states_cache,{'states':[{'state':'on'}],'timestamp':123}):
            dashboard.service_call('input_boolean','toggle','input_boolean.room')
            self.assertIsNone(dashboard._all_states_cache['states'])
            self.assertEqual(dashboard._all_states_cache['timestamp'],0)
            api.assert_called_once_with('/services/input_boolean/toggle',method='POST',payload={'entity_id':'input_boolean.room'})
