"""Health check: running worker heartbeat and SQLite integrity, no book contents printed."""
import json
import time
from .config import data_root
from .sync.service import get_store


def main():
    with get_store().connect() as db:
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise RuntimeError('Database integrity check failed')
    heartbeat = json.loads((data_root()/'worker-heartbeat.json').read_text())
    if time.time()-heartbeat['time'] > 90:
        raise RuntimeError('Worker heartbeat is stale')


if __name__=='__main__':main()
