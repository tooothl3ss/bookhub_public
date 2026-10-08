"""Independent lanes: local files, library reconciliation, and manually requested AI."""
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import logging
from pathlib import Path
import signal
import threading
import time

from .application import initialize
from . import description, shelf, tasks
from .sync.jobs import run_next as run_file_job


def enrich_imported(book_id, instance_id):
    with shelf.db_session() as db:
        book = db.get(shelf.Book, book_id)
        if not book or book.instance_id != instance_id or not book.source_id.startswith('kindle:'):
            return
    try:
        result = shelf.catalog_rematch(book_id,use_ai=False)
        if result.get('status') == 'duplicate':
            shelf.merge_book(book_id,shelf.MergeSchema(target_id=result['id']))
    except shelf.HTTPException as exc:
        if exc.status_code != 404:
            raise


def attempt(store, slot, item, action):
    """A broken item is logged and left alone until it changes or RETRY passes; the pass goes on."""
    if store.core.settled(slot, item):
        return
    try:
        action(store, item)
    except Exception:
        logging.exception('Cannot reconcile %s %s', *slot)
        store.core.settle(slot, item, store.core.RETRY)


def import_incoming(store, item):
    keys = [key for key in (item['keys'] or '').split(',') if key]
    with store.connect() as db:
        links = [db.execute('SELECT book_id FROM reader_links WHERE key=?',(key,)).fetchone() for key in keys]
    target = next((link['book_id'] for link in links if link and link['book_id']),None)
    if links and not target and all(link is not None for link in links):
        return  # Deleted/hidden cards must not come back during reconciliation.
    store.core.ingest(Path(shelf.incoming_path(item['sha256'])).read_bytes(),item['filename'],target,origin=item['source'] or 'bookbot')


def reconcile_kindle(store, b):
    # Existing KFX-only books get an EPUB, never an X3 delivery job.
    if b['has_kfx'] and not b['has_epub'] and b['epub_status'] not in ('generating', 'error'):
        with store.connect() as db:
            book = db.execute('SELECT b.title,b.authors FROM books b JOIN reader_links l ON l.book_id=b.id WHERE l.key=?', (b['key'],)).fetchone()
        store.request_epub(b['key'], book['title'] if book else b['title'], book['authors'] if book else None)
    if b['epub_status'] == 'ready':
        store.core.attach_kindle(b['key'])


def reconcile(store):
    shelf.reader_sync_once()
    # Legacy bookbot files become library assets even before a Kindle downloads them.
    # A file of a deleted card leaves epub_assets with the card and stays in deleted_epubs:
    # it never brings the card back. A store that never ran the shelf has no such table.
    with store.connect() as db:
        deleted = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='deleted_epubs'").fetchone()
        incoming = [dict(r) for r in db.execute("""SELECT i.* FROM incoming_epubs i
WHERE NOT EXISTS(SELECT 1 FROM epub_assets a WHERE a.sha256=i.sha256)""" + ("""
AND NOT EXISTS(SELECT 1 FROM deleted_epubs d WHERE d.sha256=i.sha256)""" if deleted else ''))]
    for item in incoming:
        attempt(store, ('incoming EPUB', item['id']), item, import_incoming)
    for b in store.library()['books']:
        attempt(store, ('Kindle book', b['key']), b, reconcile_kindle)
    try:
        store.core.reconcile_positions()
    except Exception:
        logging.exception('Cannot reconcile positions')
    # Recover the small enqueue gap after a committed, explicit manual request.
    with store.connect() as db:
        summaries = [dict(r) for r in db.execute("SELECT * FROM books WHERE summary_status='pending' AND summary_job_id IS NOT NULL")]
        translations = [dict(r) for r in db.execute("SELECT * FROM books WHERE description_translation_status='pending' AND description_translation_job_id IS NOT NULL")]
        runs = [r['id'] for r in db.execute("SELECT id FROM recommendation_runs WHERE status='pending'")]
    for b in summaries:
        tasks.enqueue('summary', dict(book_id=b['id'],title=b['title'],authors=b['authors'],description=b['description'] or '',job_id=b['summary_job_id']), token=b['summary_job_id'],lane='ai',store=store)
    for b in translations:
        tasks.enqueue('description', dict(book_id=b['id'],description=b['description'],job_id=b['description_translation_job_id']), token=b['description_translation_job_id'],lane='ai',store=store)
    for rid in runs:
        tasks.enqueue('recommendations', dict(run_id=rid),token=str(rid),lane='ai',store=store)


def main():
    store = initialize()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    handlers = dict(description=description.translate_background,summary=shelf.generate_summary_background,recommendations=shelf.run_recommendations_job,catalogue=enrich_imported,titles=shelf.translate_missing_titles)
    with (store.root/'worker.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with store.connect(write=True) as db:
            db.execute("UPDATE tasks SET status='queued' WHERE status='running'")
            db.execute("UPDATE jobs SET status='queued' WHERE status='running'")
        def heartbeat():
            part=store.root/"worker-heartbeat.json.tmp"
            part.write_text(json.dumps(dict(time=time.time())))
            part.replace(store.root/"worker-heartbeat.json")

        def loop(action, delay):
            while not stop.is_set():
                try:
                    worked = action()
                except Exception:
                    logging.exception('Background lane failed')
                    worked = False
                stop.wait(.05 if worked else delay)
        with ThreadPoolExecutor(max_workers=5,thread_name_prefix='bookhub') as pool:
            futures = [pool.submit(loop,heartbeat,10),
                       pool.submit(loop,lambda:run_file_job(store),2),
                       pool.submit(loop,lambda:tasks.run_next('ai',handlers,store),2),
                       pool.submit(loop,lambda:tasks.run_next('catalogue',handlers,store),2),
                       pool.submit(loop,lambda:reconcile(store),30)]
            for f in futures:
                f.result()


if __name__ == '__main__':
    main()
