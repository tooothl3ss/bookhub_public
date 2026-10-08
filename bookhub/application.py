"""Two HTTP entrypoints, one domain model and one database; no internal HTTP."""
import re
import fcntl
import json
import logging
from pathlib import Path
import threading
import time
from .config import data_root
from . import shelf
from .sync.api import create_app as protocol_app
from .opds import create_opds_app
from .sync.service import get_store
from . import tasks


def initialize():
    root = data_root()
    root.mkdir(parents=True,exist_ok=True)
    with (root / "schema.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        shelf.initialize_storage()
        store = get_store()
        tasks.initialize(store)
    return store


def heartbeat(path, stop, interval=10):
    """Liveness for /api/reader/status: the same atomic file the worker writes for itself."""
    part = path.with_name(path.name + '.tmp')
    failing = False
    while True:
        try:
            part.write_text(json.dumps(dict(time=time.time())))
            part.replace(path)
            failing = False
        except OSError as exc:
            if not failing:  # One line per outage, not one every ten seconds.
                logging.warning('Cannot write heartbeat %s: %s', path, exc)
            failing = True
        if stop.wait(interval):
            return


def create_sync_app():
    store = initialize()
    app = protocol_app(store=store)
    path, stop = Path(shelf.heartbeat_path('sync')), threading.Event()
    def start():
        stop.clear()
        threading.Thread(target=heartbeat,args=(path,stop),name='sync-heartbeat',daemon=True).start()
    # The thread lives with the running server: an app that is only constructed serves nobody.
    app.router.on_startup.append(start)
    app.router.on_shutdown.append(stop.set)
    return Dispatcher(None, app, create_opds_app(store))


class Dispatcher:
    """Preserve the shared server URL and legacy Kindle routes in-process; /opds is the
    readers' catalogue on either port. The sync port has no shelf: its web is None."""
    def __init__(self, web, sync, opds):
        self.web, self.sync, self.opds = web, sync, opds

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        if path == '/opds' or path.startswith('/opds/'):
            return await self.opds(scope, receive, send)
        # Startup and shutdown carry no path: they reach the application that owns the port.
        sync = self.web is None or path.startswith(('/users/', '/syncs/', '/api/kindle/')) or path == '/api/library'
        m = re.fullmatch(r'/api/books/([^/]+)/(manifest|map|position|applied|bundle)', path)
        if m and not m[1].isdigit():
            sync = True
        await (self.sync if sync else self.web)(scope, receive, send)


def create_web_app():
    store = initialize()
    from .web_epub import register
    register(shelf.app)
    return Dispatcher(shelf.app, protocol_app(store=store), create_opds_app(store))
