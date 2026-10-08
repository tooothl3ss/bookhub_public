"""Direct application operations, shared by HTTP adapters and background jobs."""
import hashlib
import threading
import uuid
from functools import lru_cache
from pathlib import Path
from ..config import data_root, database_path
from ..engine.kindle_map import kfx_metadata
from .store import Store, Problem

_kfx_lock = threading.Lock()

def get_store():
    return _store(data_root(), database_path())

@lru_cache(maxsize=4)
def _store(root, database):
    return Store(root, database=database)

class ReaderService:
    @property
    def store(self):
        return get_store()

    def library(self):
        return self.store.library()

    def metadata(self, key):
        b = self.store.book(key)
        if not b['kfx_path']:
            raise Problem('KFX ещё не загружен', 409)
        with _kfx_lock:
            m = kfx_metadata(b['kfx_path'])
        return dict(key=key, title=self.store.public(b)['title'], authors=m['authors'],
                    language=m['language'], publisher=m['publisher'], has_cover=m['cover'] is not None)

    def cover(self, key):
        b = self.store.book(key)
        if not b['kfx_path']:
            raise Problem('KFX ещё не загружен', 409)
        with _kfx_lock:
            m = kfx_metadata(b['kfx_path'])
        if not m['cover']:
            raise Problem('Нет обложки', 404)
        return m['cover'][1]

    def file(self, key, kind):
        if kind not in ('kfx', 'epub', 'map'):
            raise Problem('Unknown file kind', 404)
        path = self.store.book(key)[kind + '_path']
        if not path:
            raise Problem('Файл ещё не готов', 404)
        return Path(path)

    def upload_epub(self, key, data):
        store = self.store
        store.book(key)
        if hasattr(data, 'read'):
            data = data.read(256 * 1024 * 1024 + 1)
        if len(data) > 256 * 1024 * 1024:
            raise Problem('EPUB exceeds 256 MiB', 413)
        temp = store.root / 'tmp' / (uuid.uuid4().hex + '.upload')
        temp.write_bytes(data)
        return store.replace_epub(key, temp, hashlib.sha256(data).hexdigest())

    def generate_epub(self, key, title=None, author=None):
        return self.store.request_epub(key, title, author)

    def action(self, key, action):
        store = self.store
        if action == 'reset':
            return store.reset_position(key)
        if action != 'rebuild':
            raise Problem('Unknown action', 404)
        b = store.book(key)
        if not b['kfx_path']:
            raise Problem('Upload KFX first', 409)
        with store.connect(write=True) as db:
            db.execute("UPDATE kindle_books SET map_status='queued',map_error=NULL WHERE key=?", (key,))
            store.enqueue(db, 'map', key, b['kfx_path'], {'path': b['kfx_path']})
        return dict(status='queued')

    def bind(self, document, key):
        return self.store.bind(document, key)

    def forget(self, document):
        return self.store.forget_document(document)

reader_service = ReaderService()
