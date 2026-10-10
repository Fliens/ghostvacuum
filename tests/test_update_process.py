import hashlib
import importlib.util
import json
import io
from contextlib import redirect_stdout
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('updater',Path(__file__).resolve().parents[1]/'deployment/update/remote.py')
u=importlib.util.module_from_spec(spec);spec.loader.exec_module(u)

class FakeServices:
    def __init__(self,fail_health=0,busy=False):
        self.fail_health=fail_health;self.busy=busy;self.events=[]
    def preflight(self):
        self.events.append('check')
        if self.busy:raise RuntimeError('busy')
    def stop_observer(self):self.events.append('stop_observer')
    def stop_dashboard(self):self.events.append('stop_dashboard')
    def start(self):self.events.append('start')
    def health(self):
        self.events.append('health')
        if self.fail_health:
            self.fail_health-=1;raise RuntimeError('injected unhealthy update')

class UpdateProcess(unittest.TestCase):
    def setUp(self):
        silence=redirect_stdout(io.StringIO())
        silence.__enter__()
        self.addCleanup(silence.__exit__,None,None,None)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'live';self.root.mkdir()
        self.payload=Path(self.temp.name)/'payload';self.payload.mkdir()
        self.backups=Path(self.temp.name)/'backups'
        self.files={'apps/vacuum_automation.py':'version="new"\n','web/redirect_dashboard.py':'version="new"\n','web/dashboard/new.css':'body{}'}
        for rel,data in self.files.items():
            p=self.payload/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(data)
            if not rel.endswith('new.css'):
                old=self.root/rel;old.parent.mkdir(parents=True,exist_ok=True);old.write_text('version="old"\n')
        (self.root/'options.yaml').write_text('do_not_change: true\n')
        (self.root/'history.json').write_text('["existing history"]')
        self.manifest={'release':'20261010T200000Z-0123abcd','files':{rel:hashlib.sha256(data.encode()).hexdigest() for rel,data in self.files.items()}}
        self.save_manifest()
    def save_manifest(self):(self.payload/'manifest.json').write_text(json.dumps(self.manifest))
    def test_success_backups_and_preserves_data(self):
        services=FakeServices();b=u.update(self.payload,self.root,self.backups,services)
        self.assertEqual((self.root/'apps/vacuum_automation.py').read_text(),'version="new"\n')
        self.assertEqual((self.root/'options.yaml').read_text(),'do_not_change: true\n')
        with tarfile.open(b/'data.tar.gz') as t:
            self.assertEqual(t.extractfile('ghostvacuum/history.json').read(),b'["existing history"]')
        self.assertEqual((b/'data.tar.gz').stat().st_mode&0o777,0o600)
        self.assertEqual(json.loads((b/'result.json').read_text())['status'],'installed')
        self.assertEqual(services.events[:4],['check','stop_observer','check','stop_dashboard'])
    def test_unhealthy_update_rolls_back_and_rechecks(self):
        services=FakeServices(fail_health=1)
        with self.assertRaises(RuntimeError):u.update(self.payload,self.root,self.backups,services)
        self.assertEqual((self.root/'apps/vacuum_automation.py').read_text(),'version="old"\n')
        self.assertFalse((self.root/'web/dashboard/new.css').exists())
        self.assertEqual((self.root/'history.json').read_text(),'["existing history"]')
        self.assertEqual(services.events.count('health'),2)
        self.assertEqual(json.loads((self.backups/self.manifest['release']/'result.json').read_text())['status'],'rolled_back')
    def test_failed_recovery_stops_observer(self):
        services=FakeServices(fail_health=2)
        with self.assertRaisesRegex(RuntimeError,'Rollback nicht bestätigt'):u.update(self.payload,self.root,self.backups,services)
        self.assertEqual(services.events[-1],'stop_observer')
    def test_busy_aborts_without_changes_or_restart(self):
        services=FakeServices(busy=True)
        with self.assertRaises(RuntimeError):u.update(self.payload,self.root,self.backups,services)
        self.assertEqual(services.events,['check']);self.assertFalse(self.backups.exists())
    def test_bad_hash_aborts_before_services(self):
        (self.payload/'apps/vacuum_automation.py').write_text('tampered')
        services=FakeServices()
        with self.assertRaises(ValueError):u.update(self.payload,self.root,self.backups,services)
        self.assertEqual(services.events,[])
    def test_bad_path_aborts_before_services(self):
        self.manifest['files']['../outside']='bad';self.save_manifest()
        with self.assertRaises(ValueError):u.update(self.payload,self.root,self.backups,FakeServices())
    def test_partial_copy_failure_restores_all_files(self):
        original=u.replace;calls=0
        def fail_once(source,dest):
            nonlocal calls
            calls+=1
            if calls==2:raise OSError('injected disk write failure')
            original(source,dest)
        u.replace=fail_once
        try:
            with self.assertRaises(OSError):u.update(self.payload,self.root,self.backups,FakeServices())
        finally:u.replace=original
        self.assertEqual((self.root/'apps/vacuum_automation.py').read_text(),'version="old"\n')
    def test_syntax_error_aborts_before_services(self):
        rel='apps/vacuum_automation.py';(self.payload/rel).write_text('def :')
        self.manifest['files'][rel]=hashlib.sha256(b'def :').hexdigest();self.save_manifest()
        services=FakeServices()
        with self.assertRaises(SyntaxError):u.update(self.payload,self.root,self.backups,services)
        self.assertEqual(services.events,[])

class PreflightSafety(unittest.TestCase):
    def test_master_on_refuses_update(self):
        with patch.object(u,'run',return_value=json.dumps({'master':'on','vacuum':'docked'})):
            with self.assertRaisesRegex(RuntimeError,'Automatik zuerst'):u.Services().preflight()
    def test_cleaning_or_unknown_robot_refuses_update(self):
        for state in ('cleaning','paused','returning','unknown','unavailable'):
            with self.subTest(state=state),patch.object(u,'run',return_value=json.dumps({'master':'off','vacuum':state})):
                with self.assertRaisesRegex(RuntimeError,'angedockt'):u.Services().preflight()
    def test_off_and_docked_passes(self):
        with patch.object(u,'run',return_value=json.dumps({'master':'off','vacuum':'docked'})):u.Services().preflight()
