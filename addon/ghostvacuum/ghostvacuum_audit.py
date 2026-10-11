"""Bounded, durable JSON event journal. Contains no credentials or coordinates."""
import fcntl
import json
import os
from pathlib import Path
from datetime import datetime, timezone

class EventJournal:
    def __init__(self, path, max_bytes=2*1024*1024, backups=10):
        self.path=Path(path);self.max_bytes=max_bytes;self.backups=backups

    def append(self, event, **fields):
        record={"timestamp":datetime.now(timezone.utc).isoformat(),"event":event,**fields}
        data=(json.dumps(record,ensure_ascii=False,separators=(",",":"))+"\n").encode()
        if len(data)>8192:raise ValueError("Audit event too large")
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.path.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            if self.path.exists() and self.path.stat().st_size+len(data)>self.max_bytes:
                for i in range(self.backups,0,-1):
                    old=self.path if i==1 else Path(str(self.path)+f'.{i-1}')
                    new=Path(str(self.path)+f'.{i}')
                    if old.exists():os.replace(old,new)
            with self.path.open('ab') as stream:
                os.chmod(self.path,0o600);stream.write(data);stream.flush();os.fsync(stream.fileno())
        return record

    def read(self, limit=2000):
        records=[]
        # Read newest files first and keep memory bounded even with long retention.
        for path in [self.path]+[Path(str(self.path)+f'.{i}') for i in range(1,self.backups+1)]:
            if not path.exists():continue
            batch=[]
            try:stream=path.open(errors='replace')
            except OSError:continue
            with stream:
                for line in stream:
                    try:
                        item=json.loads(line)
                        if isinstance(item,dict) and item.get('timestamp') and item.get('event'):batch.append(item)
                    except (ValueError,TypeError):pass
            records=batch+records
            if len(records)>=limit:break
        return records[-limit:]
