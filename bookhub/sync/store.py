"""Single-user SQLite state. One current position per book, no position history."""
from contextlib import contextmanager
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time

from ..engine.epub_xpath import validate_epub
from ..engine.kindle_map import PositionMap, book_title, start_pid
from ..engine.position_bridge import PositionBridge

KEY = re.compile(r'^[A-Za-z0-9_-]{1,100}$')

class Problem(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status

class Store:
    def __init__(self, directory=None, database=None):
        self.root = Path(directory or os.environ.get('SYNC_DATA', 'data')).resolve()
        for name in ('books', 'maps', 'tmp'):
            (self.root/name).mkdir(parents=True, exist_ok=True)
        self.db = Path(database) if database else self.root/'bookhub.sqlite3'
        with self.connect() as db:
            db.executescript("""
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS kindle_books (
 key TEXT PRIMARY KEY, title TEXT NOT NULL, kindle_path TEXT, size INTEGER,
 local_fingerprint TEXT, uploaded_fingerprint TEXT, kfx_path TEXT, epub_path TEXT,
 map_path TEXT, compact_path TEXT, map_status TEXT DEFAULT 'missing', map_error TEXT,
 epub_status TEXT DEFAULT 'missing', epub_error TEXT,
 pid INTEGER, xpath TEXT, percentage REAL, revision INTEGER DEFAULT 0,
 source TEXT, updated_at INTEGER, bridge_status TEXT DEFAULT 'missing',
 context_before TEXT, context_after TEXT, asset_version INTEGER DEFAULT 0,
 present INTEGER DEFAULT 1, last_seen INTEGER
);
CREATE TABLE IF NOT EXISTS devices (
 id TEXT PRIMARY KEY, last_seen INTEGER, books_count INTEGER
);
CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, book_key TEXT, token TEXT,
 payload TEXT, status TEXT DEFAULT 'queued', error TEXT, created_at INTEGER,
 started_at INTEGER, finished_at INTEGER, UNIQUE(kind,book_key,token)
);
CREATE TABLE IF NOT EXISTS documents (
 document TEXT PRIMARY KEY, book_key TEXT REFERENCES kindle_books(key), progress TEXT,
 percentage REAL, device TEXT, device_id TEXT, received_at INTEGER, error TEXT
);
CREATE TABLE IF NOT EXISTS device_books (
 device_id TEXT, book_key TEXT, observed_pid INTEGER, applied_revision INTEGER,
 status TEXT, updated_at INTEGER, PRIMARY KEY(device_id,book_key)
);
""")
        with self.connect(write=True) as db:
            # epoch counts position resets; Kindle writes from an older epoch are stale.
            if 'epoch' not in {r['name'] for r in db.execute('PRAGMA table_info(kindle_books)')}:
                db.execute('ALTER TABLE kindle_books ADD COLUMN epoch INTEGER DEFAULT 0')
        self.relocate()
        with self.connect(write=True) as db:
            # Before the intake check a refused file stayed as epub_path and blocked the book for good.
            # Such files are checked once more; a job that fails now clears the path.
            for b in db.execute("SELECT key,epub_path FROM kindle_books WHERE epub_status='error' AND epub_path IS NOT NULL").fetchall():
                db.execute("UPDATE kindle_books SET epub_status='queued',epub_error=NULL WHERE key=?",(b['key'],))
                self.enqueue(db,'epub',b['key'],b['epub_path'],dict(path=b['epub_path']))
        self.core = None
        with self.connect() as db:
            has_library = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='books'").fetchone()
        if has_library:
            from ..library import Library
            self.core = Library(self)
            self.core.initialize()

    def relocate(self):
        # A copied data directory must remain usable under a new host/container path.
        with self.connect(write=True) as db:
            row=db.execute("SELECT value FROM metadata WHERE key='data_root'").fetchone()
            old=Path(row['value']) if row else None
            if old is None:
                b=db.execute('SELECT kfx_path,map_path FROM kindle_books WHERE kfx_path IS NOT NULL OR map_path IS NOT NULL LIMIT 1').fetchone()
                if b:
                    old=Path(b['kfx_path']).parents[4] if b['kfx_path'] else Path(b['map_path']).parents[3]
            if old and old!=self.root:
                def moved(path):
                    if not path:return path
                    return str(self.root/Path(path).relative_to(old))
                for b in db.execute('SELECT key,kfx_path,epub_path,map_path,compact_path FROM kindle_books').fetchall():
                    paths=[moved(b[f]) for f in ('kfx_path','epub_path','map_path','compact_path')]
                    db.execute('UPDATE kindle_books SET kfx_path=?,epub_path=?,map_path=?,compact_path=? WHERE key=?',(*paths,b['key']))
                for job in db.execute('SELECT id,token,payload FROM jobs').fetchall():
                    payload=json.loads(job['payload']);payload['path']=moved(payload['path'])
                    if payload.get('previous'):payload['previous']=moved(payload['previous'])
                    db.execute('UPDATE jobs SET token=?,payload=? WHERE id=?',(moved(job['token']),json.dumps(payload),job['id']))
                tables = {r['name'] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'epub_assets' in tables:
                    for asset in db.execute('SELECT sha256,path FROM epub_assets').fetchall():
                        db.execute('UPDATE epub_assets SET path=? WHERE sha256=?',(moved(asset['path']),asset['sha256']))
                if 'kindle_assets' in tables:
                    for asset in db.execute('SELECT key,epub_path FROM kindle_assets').fetchall():
                        db.execute('UPDATE kindle_assets SET epub_path=? WHERE key=?',(moved(asset['epub_path']),asset['key']))
            db.execute("INSERT INTO metadata VALUES('data_root',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(self.root),))

    @contextmanager
    def connect(self, write=False):
        db = sqlite3.connect(self.db, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            if write:
                db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def book(self, key):
        if not isinstance(key,str) or not KEY.fullmatch(key):
            raise Problem('Invalid book key')
        with self.connect() as db:
            row = db.execute('SELECT * FROM kindle_books WHERE key=?', (key,)).fetchone()
        if not row:
            raise Problem('Book not registered by Kindle', 404)
        return dict(row)

    @staticmethod
    @lru_cache(maxsize=128)
    def read_map(path, modified, size):
        # One parse per version of the file yields everything the answers take from a map.
        payload = json.loads(Path(path).read_text(encoding='utf-8'))
        return book_title(payload), start_pid(payload)

    def map_facts(self, b):
        """(title, first text PID) from the book's ready map; (None, None) without one."""
        if b['map_status'] == 'ready' and b['map_path']:
            try:
                stat = Path(b['map_path']).stat()
                return self.read_map(b['map_path'], stat.st_mtime_ns, stat.st_size)
            except (OSError, ValueError, TypeError, AttributeError):
                pass  # A missing or unreadable map must not hide the book.
        return None, None

    def public(self, book):
        b = dict(book)
        b['filename'] = Path(b['kindle_path'] or b['kfx_path'] or '').name
        b['title'] = self.map_facts(b)[0] or b['title']
        for field in ('kfx_path', 'epub_path', 'map_path', 'compact_path'):
            b['has_'+field.removesuffix('_path')] = bool(b.pop(field))
        return b

    def library(self):
        with self.connect() as db:
            books = sorted((self.public(r) for r in db.execute('SELECT * FROM kindle_books')),
                           key=lambda b: (b['title'].casefold(), b['key']))
            documents = [dict(r) for r in db.execute('SELECT * FROM documents ORDER BY received_at DESC')]
            jobs = [dict(r) for r in db.execute('SELECT id,kind,book_key,status,error,created_at,started_at,finished_at FROM jobs ORDER BY id DESC LIMIT 100')]
            devices = [dict(r) for r in db.execute('SELECT * FROM devices')]
            applied = [dict(r) for r in db.execute('SELECT * FROM device_books')]
        return dict(books=books, documents=documents, jobs=jobs, devices=devices, applied=applied)

    def inventory(self, device, books):
        if not isinstance(device,str) or not KEY.fullmatch(device) or not isinstance(books, list) or len(books)>10000:
            raise Problem('Invalid device/inventory')
        now = int(time.time())
        with self.connect(write=True) as db:
            db.execute('INSERT INTO devices VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen,books_count=excluded.books_count', (device,now,len(books)))
            for b in books:
                if not isinstance(b,dict) or not KEY.fullmatch(str(b.get('key',''))):
                    raise Problem('Invalid inventory book')
                fp = b.get('fingerprint')
                if not isinstance(fp,str) or not re.fullmatch('[a-f0-9]{64}',fp):
                    raise Problem('Inventory requires SHA256 fingerprint')
                db.execute("""INSERT INTO kindle_books(key,title,kindle_path,size,local_fingerprint,last_seen)
VALUES(?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET title=excluded.title,
kindle_path=excluded.kindle_path,size=excluded.size,local_fingerprint=excluded.local_fingerprint,last_seen=excluded.last_seen,present=1""",
                    (b['key'],str(b.get('title') or b['key'])[:1000],str(b.get('path',''))[:4096],b.get('size',0),fp,now))
            # The client always sends the device's full book list (one Kindle): books missing from it
            # were deleted on the Kindle. Server data stays; an empty list is a failed scan, not a wipe.
            if books:
                keys=[b['key'] for b in books]
                db.execute(f"UPDATE kindle_books SET present=0 WHERE present=1 AND key NOT IN ({','.join('?'*len(keys))})",keys)
        return [dict(key=b['key'], **self.manifest(b['key'])) for b in books]

    def manifest(self, key):
        b=self.book(key)
        return dict(needs_upload=not b['kfx_path'] or b['local_fingerprint']!=b['uploaded_fingerprint'],
                    fingerprint=b['uploaded_fingerprint'], map_status=b['map_status'],
                    map_url=f'/api/books/{key}/map' if b['map_status']=='ready' else None,
                    asset_version=b['asset_version'], error=b['map_error'])

    def enqueue(self, db, kind, key, token, payload):
        db.execute('''INSERT INTO jobs(kind,book_key,token,payload,created_at) VALUES(?,?,?,?,?)
ON CONFLICT(kind,book_key,token) DO UPDATE SET status='queued',error=NULL,finished_at=NULL,payload=excluded.payload''',
            (kind,key,token,json.dumps(payload),int(time.time())))

    def request_epub(self, key, title=None, author=None):
        """Собрать EPUB из загруженного KFX (задание воркера); существующий EPUB не подменяем."""
        b=self.book(key)
        if not b['kfx_path']:raise Problem('KFX с Kindle ещё не загружен',409)
        if b['epub_path']:raise Problem('У книги уже есть EPUB',409)
        clean=lambda v:str(v).strip()[:300] if isinstance(v,str) and v.strip() else None
        with self.connect(write=True) as db:
            db.execute("UPDATE kindle_books SET epub_status='generating',epub_error=NULL WHERE key=? AND epub_path IS NULL",(key,))
            self.enqueue(db,'generate_epub',key,b['kfx_path'],dict(path=b['kfx_path'],title=clean(title),author=clean(author)))
        return dict(status='generating')

    def forget_document(self, document):
        """Убрать документ X3, который так и не связали с книгой (например, тестовый файл)."""
        with self.connect(write=True) as db:
            d=db.execute('SELECT book_key FROM documents WHERE document=?',(document,)).fetchone()
            if not d:raise Problem('Document not found',404)
            if d['book_key']:raise Problem('Документ связан с книгой',409)
            db.execute('DELETE FROM documents WHERE document=?',(document,))
        return dict(deleted=document)

    def register_kfx(self, key, path, fingerprint):
        self.book(key)
        with self.connect(write=True) as db:
            old=dict(db.execute('SELECT * FROM kindle_books WHERE key=?',(key,)).fetchone())
            if old['local_fingerprint']!=fingerprint:
                raise Problem('Inventory changed during upload; scan again',409)
            if old['kfx_path']==str(path) and old['uploaded_fingerprint']==fingerprint:
                return
            db.execute("""UPDATE kindle_books SET kfx_path=?,uploaded_fingerprint=?,map_status='queued',map_error=NULL,
map_path=NULL,compact_path=NULL,pid=NULL,xpath=NULL,percentage=NULL,context_before=NULL,context_after=NULL,
bridge_status='missing',asset_version=asset_version+1,revision=revision+1 WHERE key=?""", (str(path),fingerprint,key))
            self.enqueue(db,'map',key,str(path),dict(path=str(path)))

    def register_epub(self, key, path, only_if_missing=False, kfx_path=None):
        b=self.book(key)
        # The same check as for a library upload, before anything points at the file:
        # a refused EPUB must not take the place of a working one.
        try:
            self.index(str(path))
        except Exception as exc:
            raise Problem('EPUB не читается: '+str(exc)[:300]) from exc
        # An EPUB built from the book's own KFX (kfx_path) is its text by construction.
        if kfx_path is None:self.check_match(b,str(path))
        with self.connect(write=True) as db:
            current=db.execute('SELECT epub_path,epub_status,kfx_path FROM kindle_books WHERE key=?',(key,)).fetchone()
            if only_if_missing and (current['epub_path'] or current['kfx_path']!=kfx_path):
                return False
            # The job returns to the last EPUB that was ready if this file fails after all.
            previous=current['epub_path'] if current['epub_status']=='ready' else None
            if current['epub_path'] and not previous:
                job=db.execute("SELECT payload FROM jobs WHERE kind='epub' AND book_key=? AND token=?",(key,current['epub_path'])).fetchone()
                previous=json.loads(job['payload']).get('previous') if job else None
            db.execute("UPDATE kindle_books SET epub_path=?,epub_status='queued',epub_error=NULL,xpath=NULL,bridge_status='pending' WHERE key=?",(str(path),key))
            self.enqueue(db,'epub',key,str(path),dict(path=str(path),previous=previous))
        return True

    def replace_epub(self, key, upload, digest):
        """Take an uploaded file as this Kindle book's EPUB. A refused file leaves nothing behind."""
        upload=Path(upload)
        try:
            self.book(key)
            dest=self.root/'books'/key/'epub'/digest/'book.epub'
            fresh=not dest.exists()
            if fresh:
                dest.parent.mkdir(parents=True,exist_ok=True)
                upload.replace(dest)
        finally:
            upload.unlink(missing_ok=True)
        try:
            self.register_epub(key,dest)
        except Problem:
            if fresh:
                dest.unlink(missing_ok=True)
                try:dest.parent.rmdir()
                except OSError:pass
            raise
        return dict(status='queued',sha256=digest)

    def check_match(self, b, path):
        """An uploaded EPUB must be the text of this Kindle book, or no position would ever translate.
        Without a ready map there is nothing to compare it with, and it is accepted as before."""
        if b['map_status']!='ready':return
        bridge=self.bridge(path,b['map_path'])
        if bridge.pages:return
        kindle,epub=bridge.textless
        if kindle and epub:
            # Page mode needs one page of the EPUB for every section of the Kindle book.
            reason=f"страниц в EPUB {len(bridge.epub.spine)}, в книге Kindle {len({c.get('section') for c in bridge.kfx.chunks})}"
        elif kindle:
            reason='в книге Kindle нет текста, а в EPUB есть'
        else:
            mapped,tried=bridge.agreement()
            # The same text maps nearly every sample, another one next to none. No sample: too little text to tell.
            if mapped*2>tried or not tried:return
            reason=f'совпало {mapped} из {tried} проверенных мест'
        raise Problem('EPUB не совпадает с книгой Kindle: '+reason)

    @staticmethod
    @lru_cache(maxsize=2)
    def index(epub):
        # The intake check and the index in one pass: the bridge and the job that follow take it instead of parsing again.
        return validate_epub(epub)

    @staticmethod
    def bridge(epub, mapping):
        # A rebuilt map keeps its path; the file's time and size tell the versions apart.
        try:
            status=os.stat(mapping);stamp=(status.st_mtime_ns,status.st_size)
        except (OSError,TypeError):
            stamp=None
        return Store.built(epub,mapping,stamp)

    @staticmethod
    @lru_cache(maxsize=6)
    def built(epub, mapping, stamp):
        return PositionBridge(Store.index(epub),mapping)

    def translate_pid(self,b,pid):
        if b['map_status']!='ready':
            raise Problem('Map is not ready',409)
        mapping=PositionMap.read(b['map_path'])
        point=mapping.at_pid(pid)
        result=dict(pid=pid,xpath=None,percentage=pid/mapping.total,bridge_status='missing',
                    context_before=point['context_before'],context_after=point['context_after'])
        if b['epub_status']=='ready':
            translated=self.bridge(b['epub_path'],b['map_path']).to_epub(pid)
            result['bridge_status']=translated['status']
            if translated['status']=='mapped':
                target=translated['matches'][0]
                result['xpath']=target['xpath']
                idx=self.bridge(b['epub_path'],b['map_path']).epub
                # Постраничный режим (манга): процент — по номеру страницы, текста для него нет.
                result['percentage']=(target['page']/max(1,target['pages']) if 'page' in target
                                      else target['raw_index']/max(1,len(idx.text)))
        return result

    def update_position(self,key,pid,source,asset_version=None,force=False,xpath=None,percentage=None,epoch=None,library_position=None):
        b=self.book(key)
        if type(pid) is not int:
            raise Problem('PID must be an integer')
        if asset_version is not None and asset_version!=b['asset_version']:
            raise Problem('Book artifact changed; refresh map',409)
        point=self.translate_pid(b,pid)
        if xpath is not None:
            point.update(xpath=xpath,percentage=percentage,bridge_status='mapped')
        with self.connect(write=True) as db:
            current=dict(db.execute('SELECT * FROM kindle_books WHERE key=?',(key,)).fetchone())
            if current['map_path']!=b['map_path'] or current['epub_path']!=b['epub_path']:
                raise Problem('Artifacts changed during translation; retry',409)
            if library_position is not None:
                position = db.execute('SELECT revision,epoch FROM reading_positions WHERE book_id=? AND sha256=?',
                                      (library_position['book_id'],library_position['sha256'])).fetchone()
                if not position or tuple(position) != (library_position['revision'],library_position['epoch']):
                    return dict(accepted=False,reason='stale',**self.position(current))
            if epoch is not None and epoch!=current['epoch']:
                return dict(accepted=False,reason='reset',**self.position(current))
            if current['pid'] is not None and (pid==current['pid'] or (pid<current['pid'] and not force)):
                return dict(accepted=False,reason='unchanged' if pid==current['pid'] else 'behind',**self.position(current))
            timestamp=max(int(time.time()), (current['updated_at'] or 0)+1)
            db.execute("""UPDATE kindle_books SET pid=:pid,xpath=:xpath,percentage=:percentage,bridge_status=:bridge_status,
context_before=:context_before,context_after=:context_after,source=:source,updated_at=:ts,revision=revision+1 WHERE key=:key""",dict(point,source=source,ts=timestamp,key=key))
            current=dict(db.execute('SELECT * FROM kindle_books WHERE key=?',(key,)).fetchone())
        if self.core and source != 'epub':
            self.core.attach_kindle(key)
        return dict(accepted=True,**self.position(current))

    @staticmethod
    def clear_position(db, key):
        current = db.execute('SELECT updated_at FROM kindle_books WHERE key=?',(key,)).fetchone()
        timestamp = max(int(time.time()), (current['updated_at'] or 0)+1)
        db.execute("""UPDATE kindle_books SET pid=NULL,xpath=NULL,percentage=NULL,bridge_status='missing',
context_before=NULL,context_after=NULL,source='reset',updated_at=?,revision=revision+1,epoch=epoch+1 WHERE key=?""",(timestamp,key))
        db.execute('UPDATE documents SET progress=NULL,percentage=NULL,error=NULL WHERE book_key=?',(key,))
        db.execute('DELETE FROM device_books WHERE book_key=?',(key,))

    def reset_position(self, key, propagate=True):
        self.book(key)
        attached = self.core.attach_kindle(key) if propagate and self.core else None
        if attached:
            self.core.reset(attached['book_id'], attached['sha256'])
        else:
            with self.connect(write=True) as db:
                self.clear_position(db, key)
        return self.current(key)

    def position(self,b):
        return {k:b[k] for k in ('key','pid','xpath','percentage','revision','updated_at','source','asset_version','bridge_status','epoch')}

    def current(self, key):
        """The position as a client reads it. After a reset there is none to go to: start_pid is
        where the text of the book begins, None until the map is ready."""
        b=self.book(key)
        return dict(self.position(b),start_pid=self.map_facts(b)[1])

    def refresh(self,key):
        b=self.book(key)
        if b['pid'] is not None and b['map_status']=='ready':
            point=self.translate_pid(b,b['pid'])
            with self.connect(write=True) as db:
                db.execute("""UPDATE kindle_books SET xpath=:xpath,percentage=:percentage,bridge_status=:bridge_status,
context_before=:context_before,context_after=:context_after WHERE key=:key AND revision=:rev AND epub_path IS :epub""",
                    dict(point,key=key,rev=b['revision'],epub=b['epub_path']))
        with self.connect() as db:
            docs=[dict(r) for r in db.execute('SELECT * FROM documents WHERE book_key=? AND progress IS NOT NULL',(key,))]
        if self.core:
            self.core.attach_kindle(key)
        for d in docs:
            self.process_document(d['document'])

    def save_document(self,document,payload):
        if not isinstance(document,str) or not re.fullmatch('[A-Za-z0-9_.-]{1,128}',document):
            raise Problem('Invalid document ID')
        xpath=payload.get('progress'); percentage=payload.get('percentage')
        if not isinstance(xpath,str) or not xpath.startswith('/body/') or len(xpath)>4000:
            raise Problem('Invalid progress XPath')
        if not isinstance(percentage,(int,float)) or isinstance(percentage,bool) or not 0<=percentage<=1:
            raise Problem('percentage must be in 0..1')
        with self.connect(write=True) as db:
            db.execute('''INSERT INTO documents(document,progress,percentage,device,device_id,received_at)
VALUES(?,?,?,?,?,?) ON CONFLICT(document) DO UPDATE SET progress=excluded.progress,percentage=excluded.percentage,
device=excluded.device,device_id=excluded.device_id,received_at=excluded.received_at,error=NULL''',
                (document,xpath,percentage,str(payload.get('device','X3'))[:200],str(payload.get('device_id',''))[:200],int(time.time())))
            if self.core:
                snapshot = dict(db.execute("""SELECT d.*,p.epoch AS position_epoch FROM documents d
LEFT JOIN reading_positions p ON p.book_id=d.library_book_id AND p.sha256=d.epub_sha256 WHERE document=?""",(document,)).fetchone())
            else:
                snapshot = dict(db.execute('SELECT * FROM documents WHERE document=?',(document,)).fetchone())
        return self.process_document(document, snapshot)

    def process_document(self,document,snapshot=None):
        if self.core:
            answer=self.core.process_document(document,snapshot)
            if answer is not None:return answer
        with self.connect() as db:
            d=snapshot or dict(db.execute('SELECT * FROM documents WHERE document=?',(document,)).fetchone())
        try:
            if not d['book_key']:
                raise Problem('Выберите книгу для этого document ID в UI',409)
            b=self.book(d['book_key'])
            if b['map_status']!='ready' or b['epub_status']!='ready':
                raise Problem('Ожидание карты и EPUB',409)
            result=self.bridge(b['epub_path'],b['map_path']).to_kindle(d['progress'])
            if result['status']!='mapped':
                raise Problem('XPath translation: '+result['status'],409)
            answer=self.update_position(b['key'],result['matches'][0]['pid'],'x3',asset_version=b['asset_version'],epoch=b['epoch'],xpath=d['progress'],percentage=d['percentage'])
            error=None
        except (Problem,ValueError) as exc:
            error=str(exc);answer=dict(pending=True,error=error)
        with self.connect(write=True) as db:
            db.execute('UPDATE documents SET error=? WHERE document=? AND received_at=? AND progress=?',
                       (error,document,d['received_at'],d['progress']))
        return answer

    def bind(self,document,key):
        self.book(key)
        attached = self.core.attach_kindle(key) if self.core else None
        if attached:
            return self.core.bind(document,attached["book_id"],attached["sha256"])
        if not re.fullmatch('[A-Za-z0-9_.-]{1,128}',document):
            raise Problem('Invalid document ID')
        with self.connect(write=True) as db:
            db.execute('INSERT INTO documents(document,book_key) VALUES(?,?) ON CONFLICT(document) DO UPDATE SET book_key=excluded.book_key',(document,key))
        with self.connect() as db:
            d=db.execute('SELECT progress FROM documents WHERE document=?',(document,)).fetchone()
        return self.process_document(document) if d['progress'] else dict(pending=True)

    def observe(self,device,key,pid,revision,status):
        self.book(key)
        with self.connect(write=True) as db:
            db.execute('''INSERT INTO device_books VALUES(?,?,?,?,?,?) ON CONFLICT(device_id,book_key)
DO UPDATE SET observed_pid=excluded.observed_pid,applied_revision=excluded.applied_revision,status=excluded.status,updated_at=excluded.updated_at''',
                (device,key,pid,revision,status,int(time.time())))
