import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock,patch

spec=importlib.util.spec_from_file_location('mova_patch',Path(__file__).resolve().parents[1]/'deployment/mova_polling_patch.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
SOURCE='''class Device:
    def _update_task(self, force_request_properties=False):
        try:
            self.update(force_request_properties)
            if self._ready:
                self.available = True
            self._update_fail_count = 0
            self._last_update_failed = None
        except Exception:
            self._update_fail_count += 1
        self.schedule_update()
'''
class MovaHeartbeat(TestCase):
    def device(self):
        clock=SimpleNamespace(time=lambda:1000)
        namespace={'time':clock};exec(compile(m.patched(SOURCE),'patched','exec'),namespace)
        d=namespace['Device']();d.status=SimpleNamespace(_last_updated_time={});d.update=Mock();d._ready=True
        d._property_changed=Mock();d.schedule_update=Mock();d._update_fail_count=0
        return d,clock
    def test_confirmed_read_emits_timestamp_and_ha_update(self):
        d,_=self.device();previous=d.status._last_updated_time;d._update_task();d.update.assert_called_once_with(True)
        self.assertEqual(previous,{})  # HA must see a new attribute value, not a mutated old one.
        self.assertEqual(d.status._last_updated_time['ghostvacuum_confirmed_poll'],1000)
        d._property_changed.assert_called_once_with(False)
    def test_no_heartbeat_from_failed_device_read(self):
        d,_=self.device();d.update.side_effect=RuntimeError('offline');d._update_task()
        self.assertEqual(d.status._last_updated_time,{})
        d._property_changed.assert_not_called();self.assertEqual(d._update_fail_count,1)
    def test_forced_read_only_each_minute_unless_requested(self):
        d,clock=self.device();d._update_task();d.update.reset_mock();d._property_changed.reset_mock()
        clock.time=lambda:1059;d._update_task();d.update.assert_called_once_with(False);d._property_changed.assert_not_called()
        clock.time=lambda:1060;d._update_task();self.assertTrue(d.update.call_args.args[0])
        d.update.reset_mock();clock.time=lambda:1061;d._update_task(True);d.update.assert_called_once_with(True)
    def test_idempotent_and_unrecognized_source_refused(self):
        once=m.patched(SOURCE);self.assertEqual(once,m.patched(once))
        with self.assertRaises(ValueError):m.patched('unrelated source')
