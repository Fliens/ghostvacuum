import json
import unittest
from unittest.mock import Mock
from test_observation_mode import module
from test_observation_settings import d

class CleaningProfiles(unittest.TestCase):
    def setUp(self):
        self.a = module.VacuumAutomation()
        self.a.args = {'observation_mode':False}
        self.a.vacuum_entity = 'vacuum.willi'
        self.a._robot_snapshot = lambda: {'phase':'docked'}
        self.a.cleaning_defaults_entity = 'input_text.defaults'
        self.a._room_helper_entity = lambda *args: 'input_text.room'
        self.profile = dict(module.CLEANING_PROFILE_DEFAULT)
        self.states = {'input_text.defaults':json.dumps(self.profile),'input_text.room':'{}'}
        self.a.get_state = lambda entity, **kw: self.states.get(entity)
        self.a.call_service = Mock()
        self.a.log = Mock()
        self.a._can_start_cleaning = lambda:True

    def test_validation_is_strict(self):
        for validator in [module.valid_cleaning_profile,d.valid_cleaning_profile]:
            self.assertTrue(validator(self.profile))
            self.assertTrue(validator({},True))
            self.assertFalse(validator({}))
            for bad in [dict(self.profile,repeats=True),dict(self.profile,mode='unknown'),dict(self.profile,extra=1),dict(self.profile,repeats=4)]:
                self.assertFalse(validator(bad))

    def test_inheritance_override_and_invalid_data(self):
        self.assertEqual(self.a._effective_cleaning_profile('office'),self.profile)
        override = dict(self.profile,mode='mopping',moisture='moist')
        self.states['input_text.room'] = json.dumps(override)
        self.assertEqual(self.a._effective_cleaning_profile('office'),override)
        self.states['input_text.room'] = 'invalid'
        self.assertIsNone(self.a._effective_cleaning_profile('office'))

    def test_observation_never_changes_robot_settings(self):
        self.a.args['observation_mode'] = True
        self.assertFalse(self.a._prepare_cleaning_profile('office'))
        self.a.call_service.assert_not_called()

    def test_waits_for_readback_and_ignores_manual_fields_for_genius(self):
        self.states['select.willi_cleangenius'] = {'state':'off','attributes':{'options':['off','routine_cleaning']}}
        self.assertFalse(self.a._prepare_cleaning_profile('office'))
        self.a.call_service.assert_called_once_with('select/select_option',entity_id='select.willi_cleangenius',option='routine_cleaning')
        self.states['select.willi_cleangenius']['state'] = 'routine_cleaning'
        self.states['select.willi_cleangenius_mode'] = {'state':'vacuum_and_mop'}
        self.a.call_service.reset_mock()
        self.assertTrue(self.a._prepare_cleaning_profile('office'))
        self.a.call_service.assert_not_called()

    def test_manual_mode_switch_and_authority_recheck(self):
        self.states['input_text.room'] = json.dumps(dict(self.profile,mode='mopping'))
        self.states['select.willi_cleangenius'] = {'state':'routine_cleaning','attributes':{'options':['off','routine_cleaning']}}
        self.a._can_start_cleaning = lambda:False
        self.assertFalse(self.a._prepare_cleaning_profile('office'))
        self.a.call_service.assert_not_called()
        self.a._can_start_cleaning = lambda:True
        self.assertFalse(self.a._prepare_cleaning_profile('office'))
        self.a.call_service.assert_called_once_with('select/select_option',entity_id='select.willi_cleangenius',option='off')

    def test_render_inherited_and_override_controls(self):
        inherited = d.render_cleaning_profile_editor('input_text.room',{},self.profile,'Büro')
        self.assertIn('Standard verwenden',inherited)
        self.assertIn('Standard · CleanGenius',inherited)
        self.assertIn('Büro: Feuchtigkeit',inherited)
        self.assertTrue(d.observation_setting_write('/services/input_text/set_value','POST',{'entity_id':'input_text.test_cleaning_defaults'},{'observation_mode':True,'helper_prefix':'test','rooms':[]}))

    def test_saved_profiles_keep_numeric_types_for_rendering(self):
        override = dict(self.profile,mode='mopping',repeats=2)
        self.assertEqual(d.parse_cleaning_profile(json.dumps(override)),override)
        rendered = d.render_cleaning_profile_editor('input_text.room',d.parse_cleaning_profile(json.dumps(override)),self.profile,'Büro')
        self.assertIn('Eigene Einstellungen · Nur wischen',rendered)
        summary = {'states':{'global':{'cleaning_defaults':{'state':json.dumps(override)}}}}
        self.assertEqual(d.cleaning_defaults(summary),override)
