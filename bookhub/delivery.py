"""Explicit delivery of one immutable EPUB. There is no automatic X3 queue."""
import hashlib
from pathlib import Path
import time
from .sync.store import Problem


def send_x3(book_id, sha256=None):
    from . import shelf
    store = shelf.reader_service.store
    asset = store.core.asset(book_id, sha256)
    if not shelf.X3_ENABLED:
        raise Problem('X3 не настроен', 400)
    # A new content version always gets a new filename and hence a new KOSync ID.
    stem = shelf.x3_safe_name(Path(asset['filename']).stem)[:140]
    filename = f"{stem}-{asset['sha256'][:12]}.epub"
    with store.connect() as db:
        known = db.execute("SELECT sha256 FROM deliveries WHERE device='x3' AND filename=?", (filename,)).fetchone()
    if not shelf.x3_check():
        raise Problem('X3 не в сети. Открой передачу файлов и нажми «Отправить» ещё раз.', 503)
    files, contact = shelf.x3_contact()
    existing = {f['name']: f['size'] for f in files}
    size = Path(asset['path']).stat().st_size
    # The name carries 12 hex digits of the content hash: the same name with the same size is this file,
    # left there by an upload whose confirmation never arrived.
    if filename in existing and existing[filename] != size:
        raise Problem(f'Файл с таким именем уже есть на X3, но другого размера ({existing[filename]} вместо {size} байт). Удали его на устройстве и отправь ещё раз.', 409)
    if filename not in existing:
        from . import x3
        try:
            x3.upload(shelf.x3_request, shelf.X3_URL, shelf.X3_BOOKS_DIR, filename, asset['path'], shelf.X3_UPLOAD_TIMEOUT_SEC)
        except x3.DeviceError as exc:
            raise Problem('X3 не принял файл: '+str(exc)[:200], 502) from exc
        # The device's "done" is not proof: a fresh listing must show the whole file. A listing that
        # cannot be had is not a refusal: the delivery counts, and the next listing settles the mark.
        try:
            listed = {f['name']: f['size'] for f in shelf.x3_fetch_listing()}
        except shelf.X3Error:
            listed = None
        if listed is not None and filename not in listed:
            raise Problem('X3 сообщил об успехе, но файла на нём нет. Отправь ещё раз.', 502)
        if listed is not None and listed[filename] != size:
            raise Problem(f'X3 сохранил файл не целиком ({listed[filename]} из {size} байт). Удали его на устройстве и отправь ещё раз.', 502)
    if not known or known['sha256'] != asset['sha256'] or filename not in existing:
        with store.connect(write=True) as db:
            db.execute('INSERT OR REPLACE INTO deliveries VALUES(?,?,?,?,?)',
                       (book_id,asset['sha256'],'x3',filename,int(time.time())))
    store.core.bind(hashlib.md5(filename.encode()).hexdigest(),book_id,asset['sha256'])
    with store.connect(write=True) as db:
        db.execute("UPDATE x3_uploads SET status='sent',filename=?,error=NULL,sent_at=CURRENT_TIMESTAMP WHERE key IN (SELECT key FROM reader_links WHERE book_id=?)",(filename,book_id))
        # The book is on the device again: an earlier removal no longer hides its evidence.
        db.execute('UPDATE books SET x3_removed_at=NULL WHERE id=?',(book_id,))
    return dict(status='sent',filename=filename,sha256=asset['sha256'],online=True,contact=shelf.x3_report())
