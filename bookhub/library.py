"""EPUB assets and positions. No Kindle is required to create or read a book."""
from functools import lru_cache
import hashlib
import json
import logging
from pathlib import Path
import re
import shutil
import tempfile
import time
import uuid
import zipfile

from .engine.epub_xpath import EpubIndex, parse_xml, local_name
from .sync.store import Problem


def merge_assets(session, source, target):
    """Transfer file ownership in the same transaction as merging shelf cards."""
    from sqlalchemy import text
    params={'source':source,'target':target}
    had_canonical=session.execute(text('SELECT 1 FROM book_assets WHERE book_id=:target AND canonical=1'),params).first()
    session.execute(text('''INSERT OR IGNORE INTO book_assets(book_id,sha256,filename,canonical,origin,created_at)
SELECT :target,sha256,filename,0,origin,created_at FROM book_assets WHERE book_id=:source'''),params)
    if not had_canonical:
        session.execute(text('''UPDATE book_assets SET canonical=1 WHERE book_id=:target AND sha256=(
SELECT sha256 FROM book_assets WHERE book_id=:source ORDER BY canonical DESC,created_at LIMIT 1)'''),params)
    # The same EPUB on both cards: the latest saved position survives. A row without one (reset or
    # never read) loses to a row that holds one, however late the reset was; a tie keeps the target.
    session.execute(text('''UPDATE reading_positions AS t SET xpath=s.xpath,ordinal=s.ordinal,percentage=s.percentage,
source=s.source,updated_at=s.updated_at FROM reading_positions AS s WHERE t.book_id=:target AND s.book_id=:source
AND s.sha256=t.sha256 AND (s.xpath IS NOT NULL,COALESCE(s.updated_at,-1))>(t.xpath IS NOT NULL,COALESCE(t.updated_at,-1))'''),params)
    # Its reset generation and a revision above both make readers and the Kindle bridge notice.
    session.execute(text('''UPDATE reading_positions AS t SET epoch=MAX(t.epoch,s.epoch),revision=MAX(t.revision,s.revision)+1
FROM reading_positions AS s WHERE t.book_id=:target AND s.book_id=:source AND s.sha256=t.sha256'''),params)
    session.execute(text('''INSERT OR IGNORE INTO reading_positions
SELECT :target,sha256,xpath,ordinal,percentage,revision,epoch,source,updated_at FROM reading_positions WHERE book_id=:source'''),params)
    session.execute(text('UPDATE documents SET library_book_id=:target WHERE library_book_id=:source'),params)
    session.execute(text('UPDATE reader_observations SET book_id=:target WHERE book_id=:source'),params)
    session.execute(text('UPDATE kindle_assets SET book_id=:target WHERE book_id=:source'),params)
    session.execute(text('UPDATE deliveries SET book_id=:target WHERE book_id=:source'),params)
    session.execute(text('UPDATE opds_downloads SET book_id=:target WHERE book_id=:source'),params)


# What a device last reported for a document, whether it moved the book or not.
OBSERVATION = '''INSERT INTO reader_observations VALUES(?,?,?,?,?,?,?,?)
ON CONFLICT(device_id,document) DO UPDATE SET device=excluded.device,book_id=excluded.book_id,
progress=excluded.progress,percentage=excluded.percentage,accepted=excluded.accepted,received_at=excluded.received_at'''


class Library:
    RETRY = 3600  # seconds before an attempt that raised is repeated: the cause may be transient

    def __init__(self, store):
        self.store = store
        # Attempts that changed nothing, by everything that decides their outcome. The 30 s
        # reconcile pass repeats none of them until that state changes; process memory only.
        self.idle = {}

    def settled(self, slot, state):
        seen = self.idle.get(slot)
        return seen is not None and seen[0] == state and time.monotonic() < seen[1]

    def settle(self, slot, state, lifetime=float('inf')):
        self.idle[slot] = (state, time.monotonic() + lifetime)

    @staticmethod
    def stamp(path):
        # A rebuilt map keeps its path and its row; its file does not keep its time.
        try:
            status = Path(path).stat()
        except (OSError, TypeError):
            return None
        return status.st_mtime_ns, status.st_size

    def initialize(self):
        with self.store.connect(write=True) as db:
            db.executescript('''
CREATE TABLE IF NOT EXISTS epub_assets (
 sha256 TEXT PRIMARY KEY, path TEXT NOT NULL, size INTEGER NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS book_assets (
 book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
 sha256 TEXT NOT NULL REFERENCES epub_assets(sha256), filename TEXT NOT NULL,
 canonical INTEGER NOT NULL DEFAULT 0, origin TEXT NOT NULL, created_at INTEGER NOT NULL,
 PRIMARY KEY(book_id,sha256)
);
CREATE UNIQUE INDEX IF NOT EXISTS book_canonical_epub ON book_assets(book_id) WHERE canonical=1;
CREATE TABLE IF NOT EXISTS reading_positions (
 book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE, sha256 TEXT NOT NULL,
 xpath TEXT, ordinal INTEGER, percentage REAL, revision INTEGER NOT NULL DEFAULT 0,
 epoch INTEGER NOT NULL DEFAULT 0, source TEXT, updated_at INTEGER,
 PRIMARY KEY(book_id,sha256),
 FOREIGN KEY(book_id,sha256) REFERENCES book_assets(book_id,sha256) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS reader_observations (
 device_id TEXT NOT NULL, document TEXT NOT NULL, device TEXT, book_id INTEGER,
 progress TEXT, percentage REAL, accepted INTEGER NOT NULL, received_at INTEGER NOT NULL,
 PRIMARY KEY(device_id,document)
);
CREATE TABLE IF NOT EXISTS deliveries (
 book_id INTEGER, sha256 TEXT, device TEXT, filename TEXT, sent_at INTEGER,
 PRIMARY KEY(device,filename)
);
CREATE TABLE IF NOT EXISTS kindle_assets (
 key TEXT PRIMARY KEY, book_id INTEGER NOT NULL, epub_path TEXT NOT NULL, sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS opds_downloads (
 book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE, sha256 TEXT NOT NULL,
 filenames TEXT NOT NULL, agent TEXT, downloaded_at INTEGER NOT NULL
);
''')
            db.execute('BEGIN IMMEDIATE')
            columns = {r['name'] for r in db.execute('PRAGMA table_info(documents)')}
            for name, definition in (('library_book_id', 'INTEGER'), ('epub_sha256', 'TEXT')):
                if name not in columns:
                    db.execute(f'ALTER TABLE documents ADD COLUMN {name} {definition}')

    @staticmethod
    def koreader_digest(data):
        # KOReader frontend/util.lua partialMD5: 1 KiB at exponentially spaced offsets.
        # https://github.com/koreader/koreader/blob/master/frontend/util.lua
        digest = hashlib.md5()
        for offset in [0] + [1024 << (2*i) for i in range(11)]:
            if offset >= len(data):
                break
            digest.update(data[offset:offset+1024])
        return digest.hexdigest()

    def readers(self, book_id):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM reader_observations WHERE book_id=? ORDER BY received_at DESC',(book_id,))]

    @staticmethod
    @lru_cache(maxsize=6)
    def index(path):
        return EpubIndex(path)

    def book(self, book_id):
        with self.store.connect() as db:
            b = db.execute('SELECT * FROM books WHERE id=?', (book_id,)).fetchone()
        if not b:
            raise Problem('Книга не найдена', 404)
        return dict(b)

    def files(self, book_id):
        self.book(book_id)
        with self.store.connect() as db:
            return [dict(r) for r in db.execute('''SELECT a.*,f.size,p.xpath,p.percentage,p.revision,p.epoch,p.source,p.updated_at
FROM book_assets a JOIN epub_assets f USING(sha256)
LEFT JOIN reading_positions p ON p.book_id=a.book_id AND p.sha256=a.sha256
WHERE a.book_id=? ORDER BY a.canonical DESC,a.created_at''', (book_id,))]

    def asset(self, book_id, sha256=None):
        with self.store.connect() as db:
            row = db.execute('''SELECT a.*,f.path FROM book_assets a JOIN epub_assets f USING(sha256)
WHERE book_id=? AND (? IS NULL OR a.sha256=?) ORDER BY canonical DESC,created_at LIMIT 1''',
                             (book_id, sha256, sha256)).fetchone()
        if not row:
            raise Problem('У книги ещё нет EPUB', 409)
        return dict(row)

    def catalogue(self):
        """Every card with its primary EPUB, by card id: all a reader can be offered, in one query."""
        with self.store.connect() as db:
            return [dict(r) for r in db.execute('''SELECT b.id,b.title,b.title_ru,b.authors,b.status,b.language,
CAST(strftime('%s',b.created_at) AS INTEGER) AS added_at,CAST(strftime('%s',b.updated_at) AS INTEGER) AS updated_at,
a.sha256,f.path,(SELECT MAX(p.updated_at) FROM reading_positions p WHERE p.book_id=b.id AND p.xpath IS NOT NULL) AS read_at
FROM books b JOIN book_assets a ON a.book_id=b.id AND a.canonical=1 JOIN epub_assets f ON f.sha256=a.sha256 ORDER BY b.id''')]

    def bound(self):
        """Reader documents that belong to a card: {document: card id}."""
        with self.store.connect() as db:
            return {r['document']: r['library_book_id'] for r in
                    db.execute('SELECT document,library_book_id FROM documents WHERE library_book_id IS NOT NULL')}

    @staticmethod
    def claim(db, documents, book_id, sha256):
        """Bind reader documents to an EPUB without taking any from anybody; returns those whose
        position waited for the file."""
        if not documents:
            return []
        # A reader that synced before its file was known here left an unbound row: it is adopted.
        waiting = [r['document'] for r in db.execute(f'''SELECT document FROM documents WHERE document IN ({','.join('?'*len(documents))})
AND library_book_id IS NULL AND book_key IS NULL AND progress IS NOT NULL''', documents)]
        # A row bound to anything else stays: a collision requires explicit binding.
        for doc in documents:
            db.execute('''INSERT INTO documents(document,library_book_id,epub_sha256) VALUES(?,?,?)
ON CONFLICT(document) DO UPDATE SET library_book_id=excluded.library_book_id,epub_sha256=excluded.epub_sha256,error=NULL
WHERE documents.library_book_id IS NULL AND documents.book_key IS NULL''', (doc,book_id,sha256))
        return waiting

    def downloaded(self, book_id, sha256, filenames, agent='', x3_filename=None):
        """A reader took this EPUB from the catalogue and will keep it under one of filenames.

        Only adds: the download itself, bindings for the names nobody holds and, for an X3, the
        delivery. No binding moves to another version and no stored position is touched."""
        now = int(time.time())
        with self.store.connect(write=True) as db:
            if not db.execute('SELECT 1 FROM book_assets WHERE book_id=? AND sha256=?', (book_id,sha256)).fetchone():
                return  # the card or this version went away meanwhile
            db.execute('INSERT INTO opds_downloads VALUES(?,?,?,?,?)',
                       (book_id,sha256,json.dumps(filenames,ensure_ascii=False),agent[:200],now))
            waiting = self.claim(db, [hashlib.md5(name.encode()).hexdigest() for name in filenames], book_id, sha256)
            if x3_filename:
                # The reader's own word is a hint: a name that another card or version was sent under stays theirs.
                db.execute('''INSERT INTO deliveries VALUES(?,?,'x3',?,?)
ON CONFLICT(device,filename) DO UPDATE SET book_id=excluded.book_id,sha256=excluded.sha256,sent_at=excluded.sent_at
WHERE deliveries.book_id IS NULL OR (deliveries.book_id=excluded.book_id AND deliveries.sha256=excluded.sha256)''',
                           (book_id,sha256,x3_filename,now))
        for doc in waiting:
            self.process_document(doc)  # the position that waited for this file appears on the card

    def ingest(self, data, filename, book_id=None, origin='upload'):
        if not data or len(data) > 256 * 1024 * 1024:
            raise Problem('Пустой EPUB или файл больше 256 МиБ', 413)
        filename = Path(filename.replace('\\', '/')).name
        if not filename.lower().endswith('.epub'):
            raise Problem('Нужен файл EPUB')
        digest = hashlib.sha256(data).hexdigest()
        path = self.store.root / 'epubs' / digest / 'book.epub'
        # The upload waits outside epubs/: a rejected one must leave no orphan there, and a stored
        # copy of the same bytes may be discarded at any moment before the transaction below.
        with tempfile.NamedTemporaryFile(dir=self.store.root / 'tmp', delete=False) as f:
            f.write(data)
            temp = Path(f.name)
        from .sync.jobs import validate_epub
        try:
            try:
                validate_epub(temp)
                with zipfile.ZipFile(temp) as z:
                    container = parse_xml(z.read('META-INF/container.xml'))
                    opf_path = next(e.get('full-path') for e in container.iter() if local_name(e.tag) == 'rootfile')
                    opf = parse_xml(z.read(opf_path))
                    def values(tag):
                        return [''.join(e.itertext()).strip() for e in opf.iter() if local_name(e.tag) == tag]
                    title = next(iter(values('title')), '') or Path(filename).stem
                    authors = ', '.join(values('creator'))
                    language = next(iter(values('language')), None)
            except Exception as exc:
                raise Problem('EPUB не читается: ' + str(exc)[:300]) from exc
            now = int(time.time())
            aliases = (hashlib.md5(filename.encode()).hexdigest(), self.koreader_digest(data))
            with self.store.connect(write=True) as db:
                if book_id is None:
                    existing = db.execute('SELECT book_id FROM book_assets WHERE sha256=? ORDER BY book_id LIMIT 1', (digest,)).fetchone()
                    if existing:
                        book_id = existing['book_id']
                    else:
                        cursor = db.execute('''INSERT INTO books(source_id,title,authors,status,language,summary,summary_status,
instance_id,created_at,updated_at) VALUES(?,?,?,'unsorted',?,'','idle',?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)''',
                            ('epub:' + uuid.uuid4().hex, title, authors, language, uuid.uuid4().hex))
                        book_id = cursor.lastrowid
                elif not db.execute('SELECT 1 FROM books WHERE id=?', (book_id,)).fetchone():
                    raise Problem('Книга не найдена', 404)
                db.execute('INSERT OR IGNORE INTO epub_assets VALUES(?,?,?,?)', (digest,str(path),len(data),now))
                canonical = not db.execute('SELECT 1 FROM book_assets WHERE book_id=? AND canonical=1', (book_id,)).fetchone()
                db.execute('INSERT OR IGNORE INTO book_assets VALUES(?,?,?,?,?,?)',
                           (book_id,digest,filename,int(canonical),origin,now))
                db.execute('INSERT OR IGNORE INTO reading_positions(book_id,sha256) VALUES(?,?)', (book_id,digest))
                # Copies of this file sent to the X3 for a card that no longer exists carry their own
                # names there: they are this card's deliveries and documents again.
                aliases += tuple(hashlib.md5(r['filename'].encode()).hexdigest() for r in db.execute(
                    "SELECT filename FROM deliveries WHERE device='x3' AND sha256=? AND book_id IS NULL", (digest,)))
                db.execute("UPDATE deliveries SET book_id=? WHERE device='x3' AND sha256=? AND book_id IS NULL", (book_id,digest))
                waiting = self.claim(db, aliases, book_id, digest)
                # Last step before commit: the file enters epubs/ only together with its epub_assets row.
                if not path.exists():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)
        for doc in waiting:
            self.process_document(doc)  # the position that waited for this file appears on the card
        return dict(status='ready',book_id=book_id,sha256=digest,filename=filename)

    def discard(self, sha256s):
        """Remove the EPUBs that no card and no Kindle book holds any more; returns their hashes."""
        removed = []
        # In a write transaction, where ingest() places its file: a discard and an upload of the
        # same bytes never interleave, and whichever comes second sees what the first one did.
        with self.store.connect(write=True) as db:
            for sha in sha256s:
                held = db.execute('''SELECT 1 FROM book_assets WHERE sha256=?1
UNION ALL SELECT 1 FROM kindle_assets WHERE sha256=?1''', (sha,)).fetchone()
                if not held and db.execute('DELETE FROM epub_assets WHERE sha256=?', (sha,)).rowcount:
                    removed.append(sha)
            # Files go last: no statement can fail after them and bring back a row without its file.
            for sha in removed:
                try:
                    shutil.rmtree(self.store.root / 'epubs' / sha)
                except FileNotFoundError:
                    pass
                except OSError:  # a leftover file is harmless: the same bytes can be uploaded again
                    logging.exception('Cannot remove EPUB %s', sha)
        return removed

    def attach_kindle(self, key):
        b = self.store.book(key)
        if b['epub_status'] != 'ready' or not b['epub_path']:
            return None
        with self.store.connect() as db:
            link = db.execute('SELECT book_id FROM reader_links WHERE key=?', (key,)).fetchone()
            if not link or link['book_id'] is None:
                return None  # An intentionally hidden card must not be resurrected.
            known = db.execute('SELECT * FROM kindle_assets WHERE key=? AND book_id=? AND epub_path=?', (key,link['book_id'],b['epub_path'])).fetchone()
            position = known and db.execute('SELECT * FROM reading_positions WHERE book_id=? AND sha256=?', (link['book_id'],known['sha256'])).fetchone()
            # An attached book is only read: nothing is written unless the link, a legacy document or the epoch would change.
            attached = bool(position) and position['epoch'] >= b['epoch'] and not db.execute(
                'SELECT 1 FROM documents WHERE book_key=? AND library_book_id IS NULL', (key,)).fetchone()
        if known:
            result = dict(book_id=link['book_id'],sha256=known['sha256'])
        else:
            filename = re.sub(r'_' + re.escape(key) + r'$', '', Path(b['kindle_path'] or key).stem) + '.epub'
            result = self.ingest(Path(b['epub_path']).read_bytes(), filename, link['book_id'], 'kindle')
        if not attached:
            with self.store.connect(write=True) as db:
                actual = db.execute('SELECT revision,epoch,epub_path FROM kindle_books WHERE key=?',(key,)).fetchone()
                if not actual or tuple(actual) != (b['revision'],b['epoch'],b['epub_path']):
                    return result
                db.execute('INSERT OR REPLACE INTO kindle_assets VALUES(?,?,?,?)', (key,link['book_id'],b['epub_path'],result['sha256']))
                db.execute('UPDATE documents SET library_book_id=?,epub_sha256=? WHERE book_key=? AND library_book_id IS NULL',
                           (link['book_id'],result['sha256'],key))
                # Preserve the reset generation across migration / KFX-only period.
                db.execute('UPDATE reading_positions SET epoch=MAX(epoch,?) WHERE book_id=? AND sha256=?',
                           (b['epoch'],link['book_id'],result['sha256']))
        if b['xpath']:
            # The same Kindle position against the same library position was already answered.
            native = (b['xpath'],b['revision'],b['epoch'],b['epub_path'])
            if not (attached and self.settled(('accept',key),(native,dict(position)))):
                answer = self.accept(link['book_id'],result['sha256'],b['xpath'],b['percentage'] or 0,
                                     b['source'] or 'kindle', propagate=False, native=b)
                del answer['accepted']
                if answer.pop('reason',None) in ('behind','unchanged'):
                    self.settle(('accept',key),(native,answer))
        return result

    def locate(self, asset, xpath):
        index = self.index(asset['path'])
        # A book without any text is positioned by page: any node of DocFragment[N] is page N.
        page_match = re.match(r'/body/DocFragment\[(\d+)\](?:[/.]|$)', xpath)
        if page_match and not any(ch.isalnum() for ch in index.text):
            page = int(page_match.group(1)) - 1
            if not 0 <= page < len(index.spine):
                raise Problem('Страница за пределами EPUB')
            return page * 10**12
        point = index.resolve(xpath)
        return point['spine_index'] * 10**12 + point['raw_index']

    def accept(self, book_id, sha256, xpath, percentage, source, *, epoch=None, propagate=True, native=None):
        asset = self.asset(book_id, sha256)
        with self.store.connect() as db:
            before = dict(db.execute('SELECT * FROM reading_positions WHERE book_id=? AND sha256=?', (book_id,sha256)).fetchone())
        expected_epoch = before['epoch'] if epoch is None else epoch
        if before['xpath'] == xpath and before['epoch'] == expected_epoch:
            return dict(accepted=False,reason='unchanged',**before)  # the stored position again: no EPUB parsing, no write
        ordinal = self.locate(asset, xpath)
        with self.store.connect(write=True) as db:
            current = dict(db.execute('SELECT * FROM reading_positions WHERE book_id=? AND sha256=?', (book_id,sha256)).fetchone())
            if native:
                actual = db.execute('SELECT revision,epoch,epub_path FROM kindle_books WHERE key=?',(native['key'],)).fetchone()
                if not actual or tuple(actual) != (native['revision'],native['epoch'],native['epub_path']):
                    return dict(accepted=False,reason='stale',**current)
            reason = ('reset' if current['epoch'] != expected_epoch else
                      'behind' if current['ordinal'] is not None and ordinal < current['ordinal'] else
                      'unchanged' if current['ordinal'] == ordinal else None)
            if reason:
                return dict(accepted=False,reason=reason,**current)
            timestamp = max(int(time.time()), (current['updated_at'] or 0) + 1)
            db.execute('''UPDATE reading_positions SET xpath=?,ordinal=?,percentage=?,source=?,updated_at=?,revision=revision+1
WHERE book_id=? AND sha256=?''', (xpath,ordinal,percentage,source,timestamp,book_id,sha256))
            result = dict(db.execute('SELECT * FROM reading_positions WHERE book_id=? AND sha256=?', (book_id,sha256)).fetchone())
        if propagate:
            self.propagate_to_kindle(book_id, sha256, result)
        return dict(accepted=True,**result)

    def propagate_to_kindle(self, book_id, sha256, position):
        with self.store.connect() as db:
            linked = [dict(r) for r in db.execute("""SELECT k.* FROM kindle_books k
JOIN kindle_assets a ON a.key=k.key AND a.epub_path=k.epub_path
WHERE a.book_id=? AND a.sha256=? AND k.map_status='ready' AND k.epub_status='ready'""", (book_id,sha256))]
        for b in linked:
            if b['xpath'] == position['xpath']:
                continue
            # An attempt that moved nothing is repeated only when one of its inputs changes.
            slot = ('propagate',b['key'],book_id,sha256)
            state = (*(b[f] for f in ('revision','epoch','epub_path','map_path','asset_version','pid','xpath')),
                     self.stamp(b['map_path']),position['revision'],position['epoch'],position['xpath'])
            if self.settled(slot,state):
                continue
            try:
                translated = self.store.bridge(b['epub_path'],b['map_path']).to_kindle(position['xpath'])
                if translated['status'] == 'mapped':
                    answer = self.store.update_position(b['key'],translated['matches'][0]['pid'],'epub',
                                                        b['asset_version'],epoch=b['epoch'],library_position=position,
                                                        xpath=position['xpath'],percentage=position['percentage'])
                    if answer['accepted'] or answer.get('reason') not in ('unchanged','behind'):
                        continue  # written, or lost a race that the next pass sees as a new state
                self.settle(slot,state)
            except Exception:  # one unreadable book must not stop the other positions
                logging.exception('Cannot translate EPUB position for Kindle %s',b['key'])
                self.settle(slot,state,self.RETRY)

    def reconcile_positions(self):
        # A restart between committing an EPUB position and updating Kindle loses no progress.
        with self.store.connect() as db:
            positions = [dict(r) for r in db.execute('''SELECT DISTINCT p.* FROM kindle_assets a
JOIN kindle_books k ON k.key=a.key AND k.epub_path=a.epub_path
JOIN reading_positions p ON p.book_id=a.book_id AND p.sha256=a.sha256
WHERE k.map_status='ready' AND k.epub_status='ready' AND p.xpath IS NOT NULL AND k.xpath IS NOT p.xpath''')]
        for position in positions:
            self.propagate_to_kindle(position['book_id'],position['sha256'],position)

    def bind(self, document, book_id, sha256=None):
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', document):
            raise Problem('Invalid document ID')
        asset = self.asset(book_id, sha256)
        with self.store.connect(write=True) as db:
            db.execute('''INSERT INTO documents(document,library_book_id,epub_sha256) VALUES(?,?,?)
ON CONFLICT(document) DO UPDATE SET library_book_id=excluded.library_book_id,
epub_sha256=excluded.epub_sha256,book_key=NULL,error=NULL''', (document,book_id,asset['sha256']))
        return self.process_document(document)

    def process_document(self, document, snapshot=None):
        with self.store.connect() as db:
            d = snapshot or dict(db.execute('''SELECT d.*,p.epoch AS position_epoch FROM documents d
LEFT JOIN reading_positions p ON p.book_id=d.library_book_id AND p.sha256=d.epub_sha256 WHERE document=?''', (document,)).fetchone())
        if not d['library_book_id'] or not d['epub_sha256']:
            return None  # Legacy Kindle-only document; legacy adapter can still serve it.
        if not d['progress']:
            return dict(pending=True)
        try:
            answer = self.accept(d['library_book_id'],d['epub_sha256'],d['progress'],d['percentage'],d['device'] or 'kosync',epoch=d['position_epoch'])
            error = None
        except (Problem, ValueError, RuntimeError) as exc:
            error = str(exc)
            answer = dict(pending=True,error=error)
        with self.store.connect(write=True) as db:
            db.execute('UPDATE documents SET error=? WHERE document=? AND progress=? AND received_at=?',
                       (error,document,d['progress'],d['received_at']))
            db.execute(OBSERVATION, (d['device_id'] or d['device'] or 'unknown',document,d['device'],d['library_book_id'],
                                     d['progress'],d['percentage'],int(bool(answer.get('accepted'))),d['received_at'] or int(time.time())))
        return answer

    def accept_reader_position(self, book_id, sha256, spine_index, *, visible_offset=None, fraction=None,
                               source='X3', device_id='x3-web', device='X3', document=None):
        """A position read from the reader's own files: a spine item and a place inside it, no XPath."""
        asset = self.asset(book_id, sha256)
        index = self.index(asset['path'])
        if any(ch.isalnum() for ch in index.text):
            point = index.xpath_for_spine(spine_index, visible_offset=visible_offset, fraction=fraction)
        else:
            # A book without any text is positioned by page, as in locate(): the spine item is the page.
            pages = len(index.spine)
            if not pages or not 0 <= spine_index <= pages:
                raise ValueError('Spine index outside EPUB')
            point = dict(xpath=f'/body/DocFragment[{min(spine_index, pages - 1) + 1}]/body', percentage=spine_index / pages)
        answer = self.accept(book_id, asset['sha256'], point['xpath'], point['percentage'], source)
        with self.store.connect(write=True) as db:
            db.execute(OBSERVATION, (device_id,document or asset['sha256'],device,book_id,
                                     point['xpath'],point['percentage'],int(answer['accepted']),int(time.time())))
        return answer

    def progress(self, document):
        with self.store.connect() as db:
            p = db.execute('''SELECT p.* FROM documents d JOIN reading_positions p
ON p.book_id=d.library_book_id AND p.sha256=d.epub_sha256 WHERE d.document=?''', (document,)).fetchone()
        if not p:
            return None
        if p['xpath'] is None:
            raise Problem('Позиция ещё не передана', 404)
        return dict(progress=p['xpath'],percentage=p['percentage'],timestamp=p['updated_at'],
                    device=p['source'],device_id='bookhub')

    def reset(self, book_id, sha256=None):
        asset = self.asset(book_id, sha256)
        with self.store.connect(write=True) as db:
            db.execute('''UPDATE reading_positions SET xpath=NULL,ordinal=NULL,percentage=NULL,
epoch=epoch+1,revision=revision+1,source='reset',updated_at=? WHERE book_id=? AND sha256=?''',
                       (int(time.time()),book_id,asset['sha256']))
            db.execute('UPDATE documents SET progress=NULL,percentage=NULL,error=NULL WHERE library_book_id=? AND epub_sha256=?', (book_id,asset['sha256']))
            keys = [r['key'] for r in db.execute('SELECT a.key FROM kindle_assets a JOIN kindle_books k ON k.key=a.key AND k.epub_path=a.epub_path WHERE a.book_id=? AND a.sha256=?', (book_id,asset['sha256']))]
            for key in keys:
                self.store.clear_position(db,key)
        return dict(status='reset')
