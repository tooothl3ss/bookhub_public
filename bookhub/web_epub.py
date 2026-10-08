"""Library-first web endpoints, independent of reader identifiers."""
from fastapi import Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from . import opds
from .sync.service import get_store
from .sync.store import Problem


async def upload_bytes(request):
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > 256 * 1024 * 1024:
            raise Problem('EPUB больше 256 МиБ', 413)
        chunks.append(chunk)
    return b''.join(chunks)


def register(app):
    if getattr(app.state,'epub_routes',False):
        return
    app.state.epub_routes = True

    @app.exception_handler(Problem)
    async def problem(request, exc):
        return JSONResponse({'detail':str(exc)},status_code=exc.status)

    @app.get('/health')
    def health():
        with get_store().connect() as db:
            db.execute('SELECT 1').fetchone()
        return dict(status='ok',service='bookhub')

    @app.post('/api/epubs')
    async def add_epub(request: Request, filename: str):
        data = await upload_bytes(request)
        def add():
            result = get_store().core.ingest(data,filename)
            # The shelf that owns this app tells whether such a book was deleted before.
            return dict(result,deleted_before=app.state.deleted_before(result['book_id']))
        return await run_in_threadpool(add)

    @app.put('/api/books/{book_id}/epub')
    async def attach_epub(book_id:int, request:Request, filename:str):
        data = await upload_bytes(request)
        def attach():
            result = get_store().core.ingest(data,filename,book_id)
            return dict(result,deleted_before=app.state.deleted_before(result['book_id']))
        return await run_in_threadpool(attach)

    @app.get('/api/books/{book_id}/files')
    def files(book_id:int):
        return get_store().core.files(book_id)

    @app.get('/api/books/{book_id}/file/epub')
    def download(book_id:int,sha256:str=None):
        asset = get_store().core.asset(book_id,sha256)
        return FileResponse(asset['path'],filename=asset['filename'],media_type='application/epub+zip')

    @app.post('/api/books/{book_id}/send-x3')
    def send(book_id:int,sha256:str=None):
        from .delivery import send_x3
        from .shelf import X3Error
        try:
            return send_x3(book_id,sha256)
        except X3Error as exc:
            raise Problem(str(exc),503) from exc

    @app.post('/api/books/{book_id}/reset-position')
    def reset(book_id:int,sha256:str=None):
        return get_store().core.reset(book_id,sha256)

    # Readers take the catalogue from /opds with the sync password; its menu is arranged here,
    # on the shelf, under the shelf's own rule for writes.
    @app.get('/api/opds/menu')
    def opds_menu():
        return opds.menu_view(get_store())

    @app.put('/api/opds/menu')
    async def save_opds_menu(request: Request):
        try:
            payload = await request.json()
        except ValueError as exc:
            raise Problem('Меню каталога — JSON') from exc
        def save():
            opds.save_menu(get_store(), payload)
            return opds.menu_view(get_store())
        return await run_in_threadpool(save)

    @app.delete('/api/opds/menu')
    def reset_opds_menu():
        opds.reset_menu(get_store())
        return opds.menu_view(get_store())

    @app.post('/api/ai/translate-titles')
    def translate_titles():
        from . import shelf, tasks
        if not shelf.AI_ENABLED:
            raise Problem('ИИ не настроен',400)
        tasks.enqueue('titles',{},lane='ai')
        return dict(status='queued')

    @app.get('/api/devices')
    def devices():
        with get_store().connect() as db:
            observed = [dict(r) for r in db.execute('SELECT * FROM reader_observations ORDER BY received_at DESC')]
            kindle = [dict(r) for r in db.execute('SELECT * FROM devices ORDER BY last_seen DESC')]
        return dict(kindle=kindle,readers=observed)

    @app.get('/api/tasks')
    def task_list():
        with get_store().connect() as db:
            return [dict(r) for r in db.execute('SELECT id,kind,lane,status,attempts,error,created_at,started_at,finished_at FROM tasks ORDER BY id DESC LIMIT 100')]
