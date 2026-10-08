"""Durable jobs with explicit lanes; AI is only enqueued by manual requests."""
import json
import time
import uuid
from .sync.service import get_store


def initialize(store=None):
    store = store or get_store()
    with store.connect() as db:
        db.executescript('''
CREATE TABLE IF NOT EXISTS tasks (
 id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, lane TEXT NOT NULL,
 token TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
 attempts INTEGER NOT NULL DEFAULT 0, error TEXT, created_at INTEGER NOT NULL,
 started_at INTEGER, finished_at INTEGER, UNIQUE(kind,token)
);
''')


def enqueue(kind, payload, *, token=None, lane='files', store=None):
    store = store or get_store()
    initialize(store)
    with store.connect(write=True) as db:
        db.execute('''INSERT OR IGNORE INTO tasks(kind,lane,token,payload,created_at) VALUES(?,?,?,?,?)''',
                   (kind,lane,token or uuid.uuid4().hex,json.dumps(payload),int(time.time())))


def run_next(lane, handlers, store=None):
    store = store or get_store()
    with store.connect(write=True) as db:
        row = db.execute("SELECT * FROM tasks WHERE lane=? AND status='queued' ORDER BY id LIMIT 1", (lane,)).fetchone()
        if not row:
            return False
        job = dict(row)
        db.execute("UPDATE tasks SET status='running',attempts=attempts+1,started_at=? WHERE id=?", (int(time.time()),job['id']))
    error = None
    try:
        handlers[job['kind']](**json.loads(job['payload']))
    except Exception as exc:
        import logging
        logging.exception('BookHub task %s failed', job['id'])
        error = str(exc)[:2000]
    with store.connect(write=True) as db:
        db.execute('UPDATE tasks SET status=?,error=?,finished_at=? WHERE id=?',
                   ('error' if error else 'done',error,int(time.time()),job['id']))
    return True
