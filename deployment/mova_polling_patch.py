"""Small, version-checked patch for installed Dreame v2.0.1 idle telemetry.
Run as root inside HA container, then reload only the dreame_vacuum config entry.
No robot actions: the existing update(True) path requests properties only.
"""
import ast
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import shutil

ROOT=Path('/config/custom_components/dreame_vacuum')
OLD='''            self.update(force_request_properties)
            if self._ready:
'''
PREVIOUS='''            # GhostVacuum: confirmed read heartbeat, never a cleaning command.
            confirm = force_request_properties or (
                time.time() - getattr(self, "_ghostvacuum_last_confirmed_poll", 0) >= 60
            )
            self.update(confirm)
            if confirm:
                self._ghostvacuum_last_confirmed_poll = time.time()
                self.status._last_updated_time["ghostvacuum_confirmed_poll"] = int(time.time())
                self._property_changed(False)
            if self._ready:
'''

NEW=PREVIOUS.replace('self.status._last_updated_time["ghostvacuum_confirmed_poll"] = int(time.time())',
    'self.status._last_updated_time = {**self.status._last_updated_time, "ghostvacuum_confirmed_poll": int(time.time())}')

def patched(source):
    if NEW in source:return source
    if PREVIOUS in source:
        result=source.replace(PREVIOUS,NEW)
        ast.parse(result)
        return result
    if source.count(OLD)!=1:raise ValueError('Unbekannte Quellversion. Patch abgebrochen.')
    result=source.replace(OLD,NEW)
    ast.parse(result)
    return result

def main():
    version=json.loads((ROOT/'manifest.json').read_text())['version']
    if version!='v2.0.1':raise ValueError('Patch gilt nur für v2.0.1')
    path=ROOT/'dreame/device.py';before=path.read_text();after=patched(before)
    if before==after:print('Patch bereits installiert');return
    backup=Path('/config/.ghostvacuum-integration-backups')/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup.mkdir(mode=0o700,parents=True)
    shutil.copy2(path,backup/'device.py')
    tmp=path.with_name('device.py.ghostvacuum-tmp');tmp.write_text(after)
    shutil.copymode(path,tmp);os.chown(tmp,path.stat().st_uid,path.stat().st_gid);os.replace(tmp,path)
    print('Bestätigte Geräteabfrage alle 60 Sekunden installiert. Backup:',backup)
if __name__=='__main__':main()
