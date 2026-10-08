import base64
import hashlib
import hmac
import json
import os
from pathlib import PurePosixPath
import re
import shutil
import tarfile
import tempfile
import threading
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from ..engine.kindle_map import kfx_metadata
from .store import Store, Problem


def create_app(data=None, api_key=None, user=None, password=None, store=None):
    store=store or Store(data)
    api_key=api_key or os.environ.get('SYNC_API_KEY','')
    user=user or os.environ.get('SYNC_USER','reader')
    password=password or os.environ.get('SYNC_PASSWORD','')
    if not api_key or not password:
        raise RuntimeError('Set SYNC_API_KEY and SYNC_PASSWORD (see scripts/configure.py)')
    app=FastAPI(title='Reader Sync',docs_url=None,redoc_url=None,openapi_url=None)
    app.state.store=store

    def same(given,expected):
        # compare_digest takes only ASCII str; bytes turn any foreign input into 401 instead of 500.
        return hmac.compare_digest(given,expected.encode())

    @app.exception_handler(Problem)
    async def problem_handler(request,exc):return JSONResponse({'error':str(exc)},status_code=exc.status)

    @app.exception_handler(ValueError)
    async def value_handler(request,exc):return JSONResponse({'error':str(exc)},status_code=400)

    @app.middleware('http')
    async def authenticate(request,call_next):
        path=request.url.path
        if path=='/health':return await call_next(request)
        # Starlette decodes header bytes as latin-1; encoding them back gives what the client sent.
        header=lambda name:request.headers.get(name,'').encode('latin-1')
        auth=request.headers.get('authorization','')
        bearer=auth.startswith('Bearer ') and same(header('authorization')[7:],api_key)
        basic=False
        if auth.startswith('Basic '):
            try:
                u,p=base64.b64decode(auth[6:],validate=True).split(b':',1)
                basic=same(u,user) and same(p,password)
            except ValueError:pass
        kosync=path.startswith('/syncs/') or path.startswith('/users/')
        if kosync:
            expected=hashlib.md5(password.encode()).hexdigest()
            valid=same(header('x-auth-user'),user) and same(header('x-auth-key'),expected)
            # Account is preconfigured; create validates the submitted credentials.
            if path=='/users/create' and request.method=='POST':
                valid=True
        else:valid=bearer or basic
        if not valid:
            return JSONResponse({'error':'Unauthorized'},401,headers={'WWW-Authenticate':'Basic realm="Reader Sync"'} if not kosync else {})
        # Browser writes carry this header; raw Kindle requests use a bearer token.
        if not kosync and request.method not in ('GET','HEAD','OPTIONS') and basic and not bearer:
            if request.headers.get('x-reader-sync')!='1':return JSONResponse({'error':'Missing UI request header'},403)
        response=await call_next(request)
        response.headers['Cache-Control']='no-store'
        return response

    async def body(request,limit=1024*1024):
        chunks=[];size=0
        async for part in request.stream():
            size+=len(part)
            if size>limit:raise Problem('Request too large',413)
            chunks.append(part)
        try:payload=json.loads(b''.join(chunks))
        except (ValueError,UnicodeError):raise Problem('Invalid JSON')
        if not isinstance(payload,dict):raise Problem('JSON object required')
        return payload

    async def upload(request):
        path=store.root/'tmp'/(uuid.uuid4().hex+'.upload')
        size=0;digest=hashlib.sha256()
        try:
            with path.open('wb') as f:
                async for chunk in request.stream():
                    size+=len(chunk)
                    if size>256*1024*1024:raise Problem('Upload exceeds 256 MiB',413)
                    digest.update(chunk);f.write(chunk)
            if not size:raise Problem('Empty upload')
            return path,digest.hexdigest()
        except BaseException:
            path.unlink(missing_ok=True);raise

    @app.get('/health')
    def health():return {'status':'ok','service':'reader-sync'}

    @app.get('/api/library')
    def library():return store.library()

    @app.post('/api/kindle/inventory')
    async def inventory(request:Request):
        p=await body(request)
        return {'books':await run_in_threadpool(store.inventory,p.get('device_id',''),p.get('books'))}

    @app.get('/api/books/{key}/manifest')
    def manifest(key:str):return store.manifest(key)

    @app.put('/api/books/{key}/bundle')
    async def bundle(key:str,request:Request):
        b=store.book(key)
        fp=request.headers.get('x-source-fingerprint','')
        if not re.fullmatch('[a-f0-9]{64}',fp) or fp!=b['local_fingerprint']:
            raise Problem('Inventory fingerprint required',409)
        path,digest=await upload(request)
        folder=store.root/'books'/key/'kfx'/digest
        staging=store.root/'tmp'/uuid.uuid4().hex
        try:
            staging.mkdir()
            count=0;size=0
            with tarfile.open(path,'r:*') as tar:
                for item in tar:
                    name=PurePosixPath(item.name)
                    if name.is_absolute() or '..' in name.parts or '\\' in item.name:
                        raise Problem('Unsafe archive path')
                    if item.isdir():continue
                    if not item.isfile():raise Problem('Only regular files allowed')
                    count+=1;size+=item.size
                    if count>10000 or size>512*1024*1024:raise Problem('Archive expanded limit exceeded',413)
                    if name.suffix.lower() not in ('.kfx','.azw','.azw8','.azw9','.res','.yj','.md'):
                        raise Problem('Bundle must contain only KFX content containers')
                    dest=staging.joinpath(*name.parts)
                    dest.parent.mkdir(parents=True,exist_ok=True)
                    if dest.exists():raise Problem('Duplicate archive entry')
                    with tar.extractfile(item) as src,dest.open('wb') as dst:shutil.copyfileobj(src,dst)
            mains=list(staging.glob('*.kfx'))
            if len(mains)!=1:raise Problem('Bundle requires exactly one root .kfx file')
            mainname=mains[0].name
            folder.parent.mkdir(parents=True,exist_ok=True)
            if folder.exists():shutil.rmtree(staging)
            else:staging.rename(folder)
            await run_in_threadpool(store.register_kfx,key,folder/mainname,fp)
            return {'status':'queued','sha256':digest}
        except tarfile.TarError as exc:
            raise Problem('Invalid tar archive') from exc
        finally:
            path.unlink(missing_ok=True)
            if staging.exists():shutil.rmtree(staging)

    @app.put('/api/books/{key}/epub')
    async def epub(key:str,request:Request):
        store.book(key);path,digest=await upload(request)
        return await run_in_threadpool(store.replace_epub,key,path,digest)

    @app.get('/api/books/{key}/map')
    def compact(key:str):
        b=store.book(key)
        if b['map_status']!='ready':raise Problem('Map not ready',409)
        return FileResponse(b['compact_path'],filename=key+'.posmap')

    @app.get('/api/books/{key}/file/{kind}')
    def book_file(key:str,kind:str):
        if kind not in ('epub','kfx','map'):raise Problem('Unknown file kind',404)
        b=store.book(key);path=b[kind+'_path']
        if not path:raise Problem('File not available',404)
        return FileResponse(path,filename=key+{'epub':'.epub','kfx':'.kfx','map':'.json'}[kind])

    # kfxlib keeps its logger in a module global: one metadata read at a time.
    kfx_lock=threading.Lock()
    def read_kfx(key):
        b=store.book(key)
        if not b['kfx_path']:raise Problem('Upload KFX first',409)
        with kfx_lock:return b,kfx_metadata(b['kfx_path'])

    @app.get('/api/books/{key}/meta')
    async def meta(key:str):
        b,m=await run_in_threadpool(read_kfx,key)
        return dict(key=key,title=store.public(b)['title'],authors=m['authors'],language=m['language'],
                    publisher=m['publisher'],has_cover=m['cover'] is not None)

    @app.get('/api/books/{key}/cover')
    async def cover(key:str):
        _,m=await run_in_threadpool(read_kfx,key)
        if not m['cover']:raise Problem('No cover in KFX',404)
        return Response(m['cover'][1],media_type='image/'+m['cover'][0])

    @app.get('/api/books/{key}/position')
    def position(key:str):return store.current(key)

    @app.put('/api/books/{key}/position')
    async def put_position(key:str,request:Request):
        p=await body(request)
        if type(p.get('asset_version')) is not int:raise Problem('asset_version required')
        # Clients without epoch predate resets and count as epoch 0.
        epoch=p.get('epoch',0)
        if type(epoch) is not int:raise Problem('epoch must be an integer')
        return await run_in_threadpool(store.update_position,key,p.get('pid'),'kindle',p['asset_version'],epoch=epoch)

    @app.post('/api/books/{key}/reset')
    def reset(key:str):return store.reset_position(key)

    @app.post('/api/books/{key}/applied')
    async def applied(key:str,request:Request):
        p=await body(request);b=store.book(key)
        if p.get('pid')!=b['pid'] or p.get('revision')!=b['revision']:
            raise Problem('Current position differs from acknowledgement',409)
        await run_in_threadpool(store.observe,str(p.get('device_id','kindle')),key,p['pid'],p['revision'],'applied')
        return {'status':'ok'}

    @app.post('/api/books/{key}/rebuild')
    def rebuild(key:str):
        b=store.book(key)
        if not b['kfx_path']:raise Problem('Upload KFX first',409)
        with store.connect(write=True) as db:
            db.execute("UPDATE kindle_books SET map_status='queued',map_error=NULL WHERE key=?",(key,))
            store.enqueue(db,'map',key,b['kfx_path'],{'path':b['kfx_path']})
        return {'status':'queued'}

    @app.post('/api/books/{key}/generate-epub')
    async def generate_epub(key:str,request:Request):
        p=await body(request)
        return await run_in_threadpool(store.request_epub,key,p.get('title'),p.get('author'))

    @app.delete('/api/documents/{document}')
    def forget_document(document:str):return store.forget_document(document)

    @app.post('/api/documents/{document}/bind')
    async def bind(document:str,request:Request):
        p=await body(request)
        if p.get('book_id') is not None and store.core:
            return await run_in_threadpool(store.core.bind,document,p['book_id'],p.get('sha256'))
        return await run_in_threadpool(store.bind,document,p.get('key',''))

    @app.get('/users/auth')
    def auth():return {'authorized':'OK'}

    @app.post('/users/create',status_code=201)
    async def create_user(request:Request):
        p=await body(request,16384)
        if p.get('username')!=user or not same(str(p.get('password','')).encode(errors='surrogatepass'),hashlib.md5(password.encode()).hexdigest()):
            raise Problem('Use the account configured by the server owner',401)
        return {'username':user}

    @app.put('/syncs/progress')
    async def sync_put(request:Request):
        p=await body(request,32768)
        result=await run_in_threadpool(store.save_document,p.get('document'),p)
        # Unmapped documents are durably kept for UI binding; no fake remote GET.
        return {'status':'OK',**result}

    @app.get('/syncs/progress/{document}')
    def sync_get(document:str):
        if store.core:
            result=store.core.progress(document)
            if result is not None:return result
        with store.connect() as db:
            d=db.execute('SELECT * FROM documents WHERE document=?',(document,)).fetchone()
        if not d or not d['book_key']:raise Problem('Document is not linked to a book',404)
        b=store.book(d['book_key'])
        if b['xpath'] is None or b['bridge_status']!='mapped':raise Problem('No translated remote position yet',404)
        return dict(progress=b['xpath'],percentage=b['percentage'],timestamp=b['updated_at'],
                    device='Reader Sync',device_id='reader-sync-backend')
    return app
