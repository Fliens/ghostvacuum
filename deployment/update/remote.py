"""Transactional code update. Run via update.py on the configured Runtipi host."""
from datetime import datetime
from zoneinfo import ZoneInfo
import argparse
import ast
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import signal
import subprocess
import tarfile
import time
import urllib.request

ROOT = Path('/home/simon/ghostvacuum-observer')
BACKUPS = Path('/home/simon/ghostvacuum-backups')
CONTAINERS = ('ghostvacuum__user-observer-1', 'ghostvacuum__user-dashboard-1')
# This probe does not import the candidate application and never calls robot services.
PROBE = r'''
import json,os,urllib.request,yaml
from pathlib import Path
o=yaml.safe_load(Path('/conf/options.yaml').read_text());o=o.get('options',o)
a=yaml.safe_load(Path('/conf/apps/apps.yaml').read_text())['vacuum_automation']
t=yaml.safe_load(Path('/conf/secrets.yaml').read_text())['ha_token']
base=os.environ['VACUUM_DASHBOARD_API']
def state(entity):
 req=urllib.request.Request(base+'/states/'+entity,headers={'Authorization':'Bearer '+t})
 with urllib.request.urlopen(req,timeout=10) as r:return json.load(r)['state']
master=a.get('enabled_entity','input_boolean.vacuum_automation_enabled')
print(json.dumps({'master':state(master),'vacuum':state(a.get('vacuum_entity',o.get('vacuum_entity')))}))
'''

def run(*args):
    r = subprocess.run(args, capture_output=True, text=True, timeout=90)
    if r.returncode:
        # Do not relay arbitrary container output or configuration secrets.
        raise RuntimeError('Befehl fehlgeschlagen: ' + ' '.join(args[:3]))
    return r.stdout

class Services:
    def preflight(self):
        state = json.loads(run('docker', 'exec', CONTAINERS[1], 'python', '-c', PROBE))
        if state['master'] != 'off':
            raise RuntimeError('Automatik zuerst ausschalten. Update nicht freigegeben.')
        if state['vacuum'] != 'docked':
            raise RuntimeError('Sauger muss angedockt sein. Update nicht freigegeben.')
    def stop_observer(self):
        run('docker', 'stop', '-t', '30', CONTAINERS[0])
    def stop_dashboard(self):
        run('docker', 'stop', '-t', '15', CONTAINERS[1])
    def start(self):
        # Dashboard first so rollback safety checks remain independent of AppDaemon.
        run('docker', 'start', CONTAINERS[1])
        self.preflight()
        run('docker', 'start', CONTAINERS[0])
    def health(self):
        deadline = time.monotonic() + 60
        last = None
        while time.monotonic() < deadline:
            try:
                self.preflight()
                for c in CONTAINERS:
                    assert run('docker','inspect','--format','{{.State.Running}}',c).strip() == 'true'
                started = run('docker','inspect','--format','{{.State.StartedAt}}',CONTAINERS[0]).strip()
                logs = run('docker','logs','--since',started,CONTAINERS[0])
                logfile=ROOT/'storage/logs/appdaemon.log'
                if logfile.exists():
                    since=datetime.fromisoformat(started[:26]+'+00:00').astimezone(ZoneInfo('Europe/Berlin')).strftime('%Y-%m-%d %H:%M:%S')
                    with logfile.open('rb') as stream:
                        stream.seek(max(0,logfile.stat().st_size-2*1024*1024))
                        lines=stream.read().decode(errors='replace').splitlines()
                    logs+='\n'+'\n'.join(line for line in lines if line[:19]>=since)
                assert 'Vacuum Automation initialisiert' in logs, 'App initialisiert nicht'
                with urllib.request.urlopen('http://172.27.30.30:8099/api/dashboard',timeout=10) as r:
                    d = json.load(r)
                assert d['automation_enabled'] is False
                assert isinstance(d['rooms'],list)
                return
            except Exception as exc:
                last = exc
                time.sleep(2)
        raise RuntimeError('Gesundheitsprüfung fehlgeschlagen') from last


def manifest_files(payload):
    m = json.loads((payload/'manifest.json').read_text())
    if not re.fullmatch(r'[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}', m['release']):
        raise ValueError('Ungültige Versionskennung')
    allowed = {'apps/vacuum_automation.py','web/redirect_dashboard.py','apps/ghostvacuum_audit.py','web/ghostvacuum_audit.py'}
    for rel, digest in m['files'].items():
        p = Path(rel)
        if p.is_absolute() or '..' in p.parts or not (rel in allowed or rel.startswith('web/dashboard/')):
            raise ValueError('Ungültiger Dateipfad')
        source = payload/rel
        if source.is_symlink() or not source.is_file():
            raise ValueError('Ungültige Quelldatei')
        if hashlib.sha256(source.read_bytes()).hexdigest() != digest:
            raise ValueError('Prüfsumme stimmt nicht: '+rel)
        if p.suffix == '.py':
            ast.parse(source.read_text(),filename=rel)
    if not allowed.issubset(m['files']):
        raise ValueError('Unvollständiges Update')
    return m


def replace(source, dest):
    missing = []
    parent = dest.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for parent in reversed(missing):
        parent.mkdir(mode=0o755)
        parent.chmod(0o755)
    tmp = dest.with_name(dest.name+'.updating')
    shutil.copy2(source,tmp)
    tmp.chmod(0o644)
    os.replace(tmp,dest)


def update(payload, root=ROOT, backups=BACKUPS, services=None):
    services = services or Services()
    m = manifest_files(payload)
    services.preflight()  # No changes when busy, unavailable or automation is ON.
    backups.mkdir(mode=0o700,parents=True,exist_ok=True)
    os.chmod(backups,0o700)
    release = backups/m['release']
    release.mkdir(mode=0o700)  # Collision must never overwrite a backup.
    (release/'manifest.json').write_text(json.dumps(m,indent=2))
    stopped = False
    promoted = False
    originals = {}
    try:
        services.stop_observer()
        stopped = True
        services.preflight()  # Re-check after the scheduler can no longer start work.
        services.stop_dashboard()
        # Consistent snapshot while both GhostVacuum services are stopped.
        with tarfile.open(release/'data.tar.gz','w:gz') as tar:
            tar.add(root,arcname='ghostvacuum',recursive=True)
        os.chmod(release/'data.tar.gz',0o600)
        # Read the complete archive before modifying live code.
        with tarfile.open(release/'data.tar.gz','r:gz') as tar:
            for member in tar:
                if member.isfile():
                    with tar.extractfile(member) as stream:
                        while stream.read(1024*1024):
                            pass
        print('Backup erstellt und lesbar: '+str(release),flush=True)
        for rel in m['files']:
            target = root/rel
            originals[rel] = target.exists()
            if target.exists():
                old = release/'code'/rel
                old.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(target,old)
        (release/'originals.json').write_text(json.dumps(originals))
        promoted = True
        for rel in m['files']:
            replace(payload/rel,root/rel)
        services.start()
        services.health()
        (release/'result.json').write_text(json.dumps({'status':'installed','release':m['release']}))
        print('Update geprüft und installiert. Automatik bleibt AUS. Backup: '+str(release))
        return release
    except BaseException:
        if promoted:
            try:
                services.stop_observer()
                services.stop_dashboard()
                for rel, existed in originals.items():
                    if existed:
                        replace(release/'code'/rel,root/rel)
                    else:
                        (root/rel).unlink(missing_ok=True)
                services.start()
                services.health()
                (release/'result.json').write_text(json.dumps({'status':'rolled_back'}))
                print('Update fehlgeschlagen. Vorherige Version wiederhergestellt und geprüft. Automatik bleibt AUS.')
            except BaseException as recovery:
                # Fail closed if the old version cannot be confirmed healthy.
                try:
                    services.stop_observer()
                finally:
                    (release/'result.json').write_text(json.dumps({'status':'recovery_failed'}))
                raise RuntimeError('Rollback nicht bestätigt. Automatik-Dienst gestoppt. Backup: '+str(release)) from recovery
        elif stopped:
            services.start()  # No files were changed.
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('payload',type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    def interrupted(signum, frame):
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(sig,signal.SIG_IGN)
        raise KeyboardInterrupt('Update unterbrochen')
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig,interrupted)
    BACKUPS.mkdir(mode=0o700,exist_ok=True)
    with (BACKUPS/'update.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        update(args.payload.resolve())

if __name__ == '__main__':
    main()
