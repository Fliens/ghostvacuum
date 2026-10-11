import importlib.util
import json
from pathlib import Path
import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch
import test_live_dashboard as live
import test_job_restart as restart

Journal=live.dashboard.EventJournal

class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'events.jsonl'

    def test_concurrent_writers_and_reopen(self):
        def write(i):Journal(self.path).append('state',number=i)
        with ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(write,range(100)))
        self.assertEqual({e['number'] for e in Journal(self.path).read()},set(range(100)))
        self.assertEqual(self.path.stat().st_mode & 0o777,0o600)

    def test_rotation_keeps_newest_and_ignores_corrupt_lines(self):
        j=Journal(self.path,max_bytes=250,backups=2)
        for i in range(30):j.append('state',number=i)
        with self.path.open('a') as stream:stream.write('broken json\n')
        events=Journal(self.path,max_bytes=250,backups=2).read()
        self.assertEqual(events[-1]['number'],29)
        self.assertEqual([e['number'] for e in events],sorted(e['number'] for e in events))
        self.assertFalse(Path(str(self.path)+'.3').exists())

    def test_dashboard_timeline_links_request_and_escapes_text(self):
        d=live.dashboard
        with patch.object(d,'DASHBOARD_ORDER_PATH',self.path.parent/'dashboard/order.json'):
            d.audit_dashboard_event('planned_manual','bath',new_request=True)
            identifier=json.loads((self.path.parent/'dashboard/request_ids.json').read_text())['bath']
            Journal(self.path).append('completed',room='bath',room_name='<Bad>',request_id=identifier)
            html=d.render_job_events()
            self.assertEqual(html.count('<details>'),1)
            self.assertIn('&lt;Bad&gt;',html)
            self.assertLess(html.index('Manuelle Vormerkung angefordert'),html.index('<b>Abgeschlossen'))

class LifecycleAuditTests(unittest.TestCase):
    setUp=restart.JobRestart.setUp
    write=restart.JobRestart.write
    restore=restart.JobRestart.restore

    def test_pending_request_is_stable_and_next_job_has_own_id(self):
        a=self.restore();a._dashboard_queue=lambda:[]
        first=a._audit_request_id('office')
        self.assertEqual(first,a._audit_request_id('office'))
        a._audit_pending_ids.clear()
        self.assertNotEqual(first,a._audit_request_id('office'))

    def test_restart_preserves_request_id_and_deduplicates_progress(self):
        self.fixture.job.request_id='test-request-123';self.write()
        a=self.restore();a._observe_active_job();a._observe_active_job()
        events=Journal(self.storage/'events.jsonl').read()
        self.assertEqual([e['event'] for e in events].count('device_state'),1)
        self.assertTrue(all(e['request_id']=='test-request-123' for e in events))
        self.assertEqual(events[0]['room_progress'],40)
        a.call_service.assert_not_called()

    def test_logging_failure_does_not_interrupt_tracking(self):
        a=self.restore()
        with patch.object(restart.module.EventJournal,'append',side_effect=OSError('disk full')):
            a._observe_active_job()
        self.assertIsNone(a._vacuum_job_problem)
        a.log.assert_called();a.call_service.assert_not_called()
