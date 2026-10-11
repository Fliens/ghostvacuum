#!/usr/bin/env python3
"""Build and deploy the current checkout, preserving server configuration and data."""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import uuid

REPO = Path(__file__).resolve().parent.parent

def build(dest):
    files = {'apps/ghostvacuum_audit.py': REPO/'apps/vacuum_automation/ghostvacuum_audit.py',
             'web/ghostvacuum_audit.py': REPO/'addon/ghostvacuum/ghostvacuum_audit.py',
             'apps/vacuum_automation.py': REPO/'apps/vacuum_automation/vacuum_automation.py',
             'web/redirect_dashboard.py': REPO/'addon/ghostvacuum/redirect_dashboard.py'}
    for p in (REPO/'addon/ghostvacuum/dashboard').rglob('*'):
        if p.is_file() and '__pycache__' not in p.parts:
            files['web/dashboard/'+p.relative_to(REPO/'addon/ghostvacuum/dashboard').as_posix()] = p
    manifest = {'release':datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8], 'files':{}}
    for rel, source in files.items():
        target=dest/rel;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
        manifest['files'][rel]=hashlib.sha256(target.read_bytes()).hexdigest()
    (dest/'manifest.json').write_text(json.dumps(manifest,indent=2))
    return manifest

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host',default='simon@172.27.30.30')
    p.add_argument('--key',default='/Users/simon/Documents/Codex/2026-10-08/so/outputs/kontron-ssh-key')
    p.add_argument('--known-hosts',default='/Users/simon/Documents/Codex/2026-10-08/so/outputs/kontron-known-hosts')
    p.add_argument('--check',action='store_true',help='Paket und lokale Tests prüfen, nichts übertragen')
    a=p.parse_args()
    subprocess.run(['python3','-m','unittest','discover','-s','tests','-q'],cwd=REPO,check=True)
    with tempfile.TemporaryDirectory(prefix='ghostvacuum-update-') as tmp:
        dest=Path(tmp)/'payload';dest.mkdir();m=build(dest)
        shutil.copy2(REPO/'deployment/update/remote.py',Path(tmp)/'remote.py')
        if a.check:
            print('Update-Paket geprüft: '+m['release']);return
        opts=['-i',a.key,'-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','UserKnownHostsFile='+a.known_hosts]
        remote='/home/simon/.ghostvacuum-updates/'+m['release']
        subprocess.run(['ssh',*opts,a.host,'umask 077; mkdir -p '+shlex.quote(remote)],check=True)
        subprocess.run(['scp',*opts,'-r',str(dest),str(Path(tmp)/'remote.py'),a.host+':'+remote+'/'],check=True)
        subprocess.run(['ssh',*opts,a.host,'sudo -n python3 '+shlex.quote(remote+'/remote.py')+' '+shlex.quote(remote+'/payload')],check=True)

if __name__=='__main__':main()
