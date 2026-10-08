"""Persistent map/index jobs. Run one worker per data directory."""
import argparse
import fcntl
import hashlib
import json
import logging
from pathlib import Path
import time

from .store import Store
from ..engine.kindle_map import build, write_map
from ..engine.epub_xpath import validate_epub  # отсюда его берёт library.py
from ..engine.kfx_epub import build_epub


def run_next(store):
    with store.connect(write=True) as db:
        row=db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
        if not row:
            return False
        job=dict(row)
        db.execute("UPDATE jobs SET status='running',started_at=? WHERE id=?",(int(time.time()),job['id']))
    try:
        payload=json.loads(job['payload']); key=job['book_key']; source=payload['path']
        b=store.book(key)
        if job['kind']=='map':
            mapping=build(source)
            if mapping.key!=key:
                raise ValueError('KFX metadata key differs from registered Kindle key')
            folder=store.root/'maps'/key/Path(source).parent.name
            folder.mkdir(parents=True,exist_ok=True)
            host=folder/'map.json'; compact=folder/(key+'.posmap')
            write_map(mapping,host,compact)
            with store.connect(write=True) as db:
                db.execute("UPDATE kindle_books SET map_path=?,compact_path=?,map_status='ready',map_error=NULL WHERE key=? AND kfx_path=?",(str(host),str(compact),key,source))
        elif job['kind']=='generate_epub':
            # EPUB мог появиться, пока задание ждало очереди, — тогда не подменяем; из устаревшего KFX не собираем.
            registered=False
            if not b['epub_path'] and b['kfx_path']==source:
                data=build_epub(source,payload.get('title'),payload.get('author'))
                folder=store.root/'books'/key/'epub'/hashlib.sha256(data).hexdigest()
                folder.mkdir(parents=True,exist_ok=True)
                part=folder/'book.epub.part';part.write_bytes(data)
                dest=folder/'book.epub';part.replace(dest)
                registered=store.register_epub(key,dest,only_if_missing=True,kfx_path=source)
            if not registered:
                # KFX сменился до или во время сборки: книга без EPUB не должна остаться в «generating»,
                # иначе reconcile её больше не тронет. Если новая сборка уже в очереди, статус её и оставляем.
                with store.connect(write=True) as db:
                    db.execute("""UPDATE kindle_books SET epub_status='missing' WHERE key=? AND epub_path IS NULL AND epub_status='generating'
AND NOT EXISTS(SELECT 1 FROM jobs WHERE kind='generate_epub' AND book_key=? AND id!=? AND status IN ('queued','running'))""",(key,key,job['id']))
        elif job['kind']=='epub':
            # Тот же индекс, что при приёме файла в этом процессе, иначе — разбор заново; из него же строится мост для refresh.
            store.index(source)
            with store.connect(write=True) as db:
                db.execute("UPDATE kindle_books SET epub_status='ready',epub_error=NULL WHERE key=? AND epub_path=?",(key,source))
        else:
            raise ValueError('Unknown job kind')
        store.refresh(key)
        with store.connect(write=True) as db:
            db.execute("UPDATE jobs SET status='done',finished_at=? WHERE id=?",(int(time.time()),job['id']))
    except Exception as exc:
        logging.exception('Job %s failed',job['id'])
        message=str(exc)[:2000]
        restored=False
        with store.connect(write=True) as db:
            db.execute("UPDATE jobs SET status='error',error=?,finished_at=? WHERE id=?",(message,int(time.time()),job['id']))
            if job['kind']=='generate_epub':
                db.execute("UPDATE kindle_books SET epub_status='error',epub_error=? WHERE key=? AND epub_path IS NULL",(message,job['book_key']))
            elif job['kind']=='map':
                db.execute("UPDATE kindle_books SET map_status='error',map_error=? WHERE key=? AND kfx_path=?",(message,job['book_key'],json.loads(job['payload'])['path']))
            elif job['kind']=='epub':
                # Файл уже прошёл проверку при приёме, но задание всё равно упало: вернуть прежний EPUB.
                # Останься путь на этом файле — книга не свяжется с полкой и не соберёт EPUB из KFX заново.
                payload=json.loads(job['payload']);previous=payload.get('previous')
                note=('Новый EPUB не принят, оставлен прежний: ' if previous else 'EPUB не принят: ')+message
                changed=db.execute("""UPDATE kindle_books SET epub_path=?,epub_status=?,epub_error=?,xpath=NULL,bridge_status='pending'
WHERE key=? AND epub_path=?""",(previous,'ready' if previous else 'error',note,job['book_key'],payload['path'])).rowcount
                restored=bool(previous and changed)
        if restored:
            try:
                store.refresh(job['book_key'])
            except Exception:
                logging.exception('Cannot refresh %s after restoring its EPUB',job['book_key'])
    return True


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--once',action='store_true');args=ap.parse_args()
    store=Store()
    with (store.root/'worker.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with store.connect(write=True) as db:
            db.execute("UPDATE jobs SET status='queued' WHERE status='running'")
        while True:
            worked=run_next(store)
            if args.once:return
            if not worked:time.sleep(2)

if __name__=='__main__':main()
