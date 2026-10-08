"""Shelf backend regressions: idle sync pass, batched book list, card deletion and its record,
cross-site requests, X3 presence and the X3 web channel, incoming EPUB, per-process status.
Temporary data, no network, a fake X3."""
import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import posixpath
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from fastapi.testclient import TestClient
from sqlalchemy import event

TEXT = 'A unique paragraph about sailing across the northern ocean and watching distant mountains in the morning.'
XPATH = '/body/DocFragment[1]/body/p[1]/text()[1].'

def epub(text=TEXT,title='My book'):
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w') as z:
        z.writestr(zipfile.ZipInfo('META-INF/container.xml'),'<container><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>')
        z.writestr(zipfile.ZipInfo('book.opf'),f'<package xmlns:dc="http://purl.org/dc/elements/1.1/"><metadata><dc:title>{title}</dc:title><dc:creator>Author</dc:creator></metadata><manifest><item id="ch" href="ch.xhtml"/></manifest><spine><itemref idref="ch"/></spine></package>')
        z.writestr(zipfile.ZipInfo('ch.xhtml'),'<html><body><p>'+text+'</p></body></html>')
    return buf.getvalue()

def md5(name):
    return hashlib.md5(name.encode()).hexdigest()

def forget_shelf():
    # shelf binds its engine and paths when imported: a class with its own data directory needs
    # a fresh copy, and must not leave its copy to the next class.
    import bookhub
    for name in ('shelf','application','web_epub','delivery','description','worker'):
        sys.modules.pop('bookhub.'+name,None)
        if hasattr(bookhub,name):delattr(bookhub,name)

class Together:
    """Several patches as one context manager that can be entered again."""
    def __init__(self,*patches):self.patches=patches
    def __enter__(self):
        for p in self.patches:p.start()
    def __exit__(self,*exc):
        for p in reversed(self.patches):p.stop()

def stand_in(module):
    # bookhub.x3, the device protocol, is written by another part of this round against a contract.
    # The shelf imports it where it is needed and here gets the contract's functions instead,
    # whether a real module is importable or not.
    import bookhub
    return patch.dict(sys.modules,{'bookhub.x3':module}),patch.object(bookhub,'x3',module,create=True)


class ShelfFixesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.root=Path(cls.temp.name)
        cls.env=patch.dict(os.environ,BOOKHUB_DATA=cls.temp.name,BOOKHUB_DB=str(cls.root/'bookhub.sqlite3'),AI_URL='',AI_MODEL='',
                           SYNC_API_KEY='token',SYNC_USER='reader',SYNC_PASSWORD='password',X3_URL='http://x3.invalid',X3_SYNC_DEVICE='')
        cls.env.start()
        forget_shelf()
        from bookhub import application
        from bookhub import shelf
        from bookhub.sync.service import get_store
        cls.application=application;cls.shelf=shelf
        cls.web=TestClient(application.create_web_app())
        cls.sync=TestClient(application.create_sync_app())
        cls.store=get_store()
        cls.ko={'x-auth-user':'reader','x-auth-key':hashlib.md5(b'password').hexdigest()}

    @classmethod
    def tearDownClass(cls):
        cls.web.close();cls.sync.close();cls.shelf.engine.dispose();forget_shelf();cls.env.stop();cls.temp.cleanup()

    def setUp(self):
        self.network=patch('requests.sessions.Session.request',side_effect=AssertionError('Unexpected external request'))
        self.network.start();self.addCleanup(self.network.stop)
        with self.store.connect() as db:
            db.execute('PRAGMA foreign_keys=OFF')
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
                db.execute('DELETE FROM "'+row['name']+'"')
        self.shelf.READER_STATE.update(library=None,fetched_at=None,synced_at=None,error=None,snapshots_at=None)
        self.shelf.X3_STATE.update(online=None,checked_at=None,version=None,error=None)
        for name in self.shelf.HEARTBEAT_FILES.values():
            (self.root/name).unlink(missing_ok=True)

    # --- helpers ---
    def add(self,data=None,filename='book.epub'):
        r=self.web.post('/api/epubs',params={'filename':filename},content=data or epub())
        self.assertEqual(200,r.status_code,r.text)
        return r.json()

    def push(self,doc,offset,device='CrossPoint',device_id='crosspoint-reader'):
        return self.sync.put('/syncs/progress',headers=self.ko,json=dict(document=doc,progress=XPATH+str(offset),percentage=offset/100,device=device,device_id=device_id))

    def card(self,bid):
        r=self.web.get(f'/api/books/{bid}')
        self.assertEqual(200,r.status_code,r.text)
        return r.json()

    def status(self):
        self.shelf.READER_STATE['fetched_at']=None  # no 15-second cache between assertions
        return self.web.get('/api/reader/status').json()

    def sql(self,statement,*args):
        with self.store.connect(write=True) as db:
            return [tuple(r) for r in db.execute(statement,args).fetchall()]

    def device(self,files=(),online=True):
        """The X3: its file-transfer service behind shelf.x3_request (status, file list) and the
        protocol module with the functions of its contract (saved positions, delete, upload).
        progress: what the device saved, by path of the book; refuse: its answer to delete and upload;
        fails: what reading a position raises; stored: the size an uploaded file ends up with."""
        shelf=self.shelf
        state=SimpleNamespace(files={name:1000 for name in files},online=online,calls=[],progress={},asked=[],deleted=[],uploads=[],
                              refuse=None,fails=None,leaves=False,stored=lambda size:size,DeviceError=type('DeviceError',(Exception,),{}))
        def reply(data=None,code=200):return SimpleNamespace(ok=code<400,status_code=code,text='',json=lambda:data)
        def reach(request=None):
            if request not in (None,shelf.x3_request):raise AssertionError('The module is given shelf.x3_request')
            if not state.online:raise shelf.X3Error('X3 не отвечает')
        def request(method,path,**kwargs):
            state.calls.append(path);reach()
            if path=='/api/status':return reply({'version':'1.6.5'})
            if path=='/api/files':
                return reply([dict(name='System Volume Information',isDirectory=True)]+
                             [dict(name=n,size=s,isDirectory=False,isEpub=n.endswith('.epub')) for n,s in state.files.items()])
            raise AssertionError(path)
        def read_progress(request,path):
            state.asked.append(path);reach(request)
            if state.fails:raise state.fails
            return state.progress.get(path)
        def delete(request,paths):
            reach(request)
            if state.refuse:raise state.DeviceError(state.refuse)
            state.deleted.append(list(paths))
            for path in paths:del state.files[posixpath.basename(path)]
            if state.leaves:state.online=False
        def upload(request,base_url,directory,filename,source,timeout):
            reach(request)
            if state.refuse:raise state.DeviceError(state.refuse)
            state.uploads.append((base_url,directory,filename));size=state.stored(Path(source).stat().st_size)
            if size is not None:state.files[filename]=size
            return 'ws'
        module=SimpleNamespace(DeviceError=state.DeviceError,read_progress=read_progress,delete=delete,upload=upload)
        state.patch=Together(patch.object(shelf,'x3_request',side_effect=request),*stand_in(module))
        return state

    def size(self,bid):
        return self.sql('SELECT f.size FROM book_assets a JOIN epub_assets f USING(sha256) WHERE a.book_id=?',bid)[0][0]

    def check(self,device):
        with device.patch:
            r=self.web.post('/api/x3/check')
        self.assertEqual(200,r.status_code,r.text)
        return r.json()

    def send(self,device,bid):
        with device.patch:
            r=self.web.post(f'/api/books/{bid}/send-x3')
        self.assertEqual(200,r.status_code,r.text)
        return r.json()

    def kindle(self,key,bid,**fields):
        self.store.inventory('kindle',[dict(key=key,fingerprint='a'*64,path=f'/mnt/us/documents/{key}.kfx')])
        self.sql('INSERT INTO reader_links(key,book_id) VALUES(?,?)',key,bid)
        for name,value in fields.items():
            self.sql(f'UPDATE kindle_books SET {name}=? WHERE key=?',value,key)

    def statements(self,action):
        seen=[]
        def record(conn,cursor,statement,parameters,context,executemany):seen.append(' '.join(statement.split()))
        event.listen(self.shelf.engine,'before_cursor_execute',record)
        try:action()
        finally:event.remove(self.shelf.engine,'before_cursor_execute',record)
        return seen

    # --- the 30-second pass writes only what changed ---
    def test_idle_sync_pass_leaves_links_and_synced_at_alone(self):
        bid=self.add()['book_id'];self.kindle('KINDLEBOOK',bid,percentage=.2)
        self.shelf.reader_sync_once()
        first=self.status();stored=self.sql('SELECT snapshot,synced_at FROM reader_links')
        self.assertTrue(first['synced_at'])
        self.assertEqual(.2,json.loads(stored[0][0])['percentage'])
        writes=[s for s in self.statements(self.shelf.reader_sync_once) if not s.startswith('SELECT')]
        self.assertEqual([],writes)
        idle=self.status()
        self.assertEqual((first['synced_at'],first['library_revision']),(idle['synced_at'],idle['library_revision']))
        self.assertEqual(stored,self.sql('SELECT snapshot,synced_at FROM reader_links'))
        # Not shown on a card: the snapshot follows, open pages are not told to reload.
        self.sql('UPDATE kindle_books SET last_seen=last_seen+100')
        self.shelf.reader_sync_once()
        self.assertEqual(first['synced_at'],self.status()['synced_at'])
        self.assertNotEqual(stored[0][0],self.sql('SELECT snapshot FROM reader_links')[0][0])
        # Shown on a card.
        self.sql('UPDATE kindle_books SET percentage=.5')
        self.shelf.reader_sync_once()
        self.assertNotEqual(first['synced_at'],self.status()['synced_at'])
        self.assertEqual(.5,self.card(bid)['reader'][0]['percentage'])

    # --- one set of queries for the whole list ---
    def test_book_list_needs_the_same_queries_for_one_book_and_for_many(self):
        first=self.add();self.push(md5('book.epub'),20)
        self.check(self.device(['book.epub']))
        one=self.statements(lambda:self.web.get('/api/books'))
        for n in range(5):
            b=self.add(epub(TEXT+f' Volume {n}.',title=f'Volume {n}'),filename=f'volume-{n}.epub')
            self.push(md5(f'volume-{n}.epub'),10+n,device='KOReader',device_id=f'reader-{n}')
            self.sql('INSERT INTO deliveries VALUES(?,?,?,?,?)',b['book_id'],b['sha256'],'x3',f'volume-{n}.epub',1000+n)
        many=self.statements(lambda:self.web.get('/api/books'))
        self.assertEqual(len(one),len(many),many)
        # Nothing is loaded per book through separate SQLite connections any more.
        with patch.object(self.store,'connect',side_effect=AssertionError('separate connection')):
            books=self.web.get('/api/books').json()
            single=self.web.get(f"/api/books/{first['book_id']}").json()
        self.assertEqual(6,len(books))
        self.assertEqual(single,next(b for b in books if b['id']==first['book_id']))
        # Same shape as the per-book loaders it replaces, plus the X3 flag on devices.
        self.assertEqual(self.web.get(f"/api/books/{first['book_id']}/files").json(),single['epubs'])
        self.assertEqual([dict(d,x3=True) for d in self.store.core.readers(first['book_id'])],single['devices'])
        self.assertEqual({'reader','epubs','devices','deliveries','x3','deleted_before'},set(single)-set(self.shelf.Book.__table__.columns.keys()))
        flags={d['device']:d['x3'] for b in books for d in b['devices']}
        self.assertEqual({'CrossPoint':True,'KOReader':False},flags)

    def test_x3_devices_are_recognised_by_name_or_id(self):
        is_x3=self.shelf.is_x3_device
        self.assertTrue(is_x3(dict(device='CrossPoint',device_id='crosspoint-reader')))
        self.assertTrue(is_x3(dict(device=None,device_id='CROSSPOINT-reader')))
        self.assertTrue(is_x3(dict(device='Xteink X3',device_id='')))
        self.assertFalse(is_x3(dict(device='KOReader',device_id='box3')))
        self.assertFalse(is_x3(dict(device=None,device_id=None)))
        with patch.dict(os.environ,X3_SYNC_DEVICE='^pocket'):
            self.assertTrue(self.shelf._env_regex('X3_SYNC_DEVICE','x3').search('PocketReader'))
        with patch.dict(os.environ,X3_SYNC_DEVICE='('):
            self.assertTrue(self.shelf._env_regex('X3_SYNC_DEVICE','x3').search('x3'))

    # --- a deleted card leaves a record instead of its files ---
    def test_deleted_card_leaves_a_record_and_the_same_epub_makes_a_marked_card(self):
        from bookhub.worker import reconcile
        data=epub();doc=md5('bot.epub')
        r=self.web.post('/api/incoming/epub',params={'filename':'bot.epub','source':'bookbot'},content=data)
        self.assertEqual(200,r.status_code,r.text)
        self.assertIsNone(r.json()['deleted_before'])
        bid=r.json()['book_id'];sha=hashlib.sha256(data).hexdigest()
        self.assertTrue(self.push(doc,20).json()['accepted'])
        self.assertEqual(1,len(self.status()['incoming']))
        copy=Path(self.shelf.incoming_path(sha));stored=Path(self.sql('SELECT path FROM epub_assets')[0][0])
        cover=Path(self.shelf.COVERS_DIR)/f'{bid}.jpg';cover.write_bytes(b'cover')
        self.assertTrue(copy.exists() and stored.exists())
        self.assertEqual(200,self.web.delete(f'/api/books/{bid}').status_code)
        after=self.status()
        self.assertEqual(([],[]),(after['incoming'],after['unbound_documents']))
        self.assertEqual([],self.sql('SELECT * FROM documents'))
        self.assertEqual([],self.sql('SELECT * FROM incoming_epubs'))
        # The files go with the card; a record is what stays.
        self.assertEqual([],self.sql('SELECT sha256 FROM epub_assets'))
        self.assertEqual([],[p for p in (copy,stored,stored.parent,cover) if p.exists()])
        (record,*kept),=self.sql('SELECT id,title,title_ru,authors,status,x3_files FROM deleted_books')
        self.assertEqual(['My book',None,'Author','unsorted','[]'],kept)
        self.assertEqual([(sha,record)],self.sql('SELECT sha256,deleted_book_id FROM deleted_epubs'))
        with patch.object(self.shelf,'x3_request',side_effect=AssertionError('no device calls')):
            reconcile(self.store);reconcile(self.store)
        self.assertEqual([],self.web.get('/api/books').json())
        # The same file again: an ordinary new card that says it was deleted before.
        again=self.add(data,'bot.epub');note=again['deleted_before']
        self.assertEqual((record,'My book'),(note['id'],note['title']))
        self.assertTrue(note['deleted_at'].endswith('Z'))
        card=self.card(again['book_id'])
        self.assertEqual((note,note),(card['deleted_before'],self.web.get('/api/books').json()[0]['deleted_before']))
        self.assertEqual(('My book','unsorted',[sha],[None]),(card['title'],card['status'],[f['sha256'] for f in card['epubs']],[f['xpath'] for f in card['epubs']]))
        self.assertTrue(self.push(doc,30).json()['accepted'])
        card=self.card(again['book_id'])
        self.assertEqual((XPATH+'30','crosspoint-reader'),(card['epubs'][0]['xpath'],card['devices'][0]['device_id']))
        self.assertEqual([],self.status()['unbound_documents'])
        self.assertEqual(note,self.web.post('/api/incoming/epub',params={'filename':'bot.epub'},content=data).json()['deleted_before'])
        # The owner dismisses the note: the record goes, other open pages are told to reload.
        revision=self.status()['library_revision']
        for _ in range(2):
            self.assertEqual({'status':'forgotten'},self.web.delete(f"/api/deleted/{note['id']}").json())
        self.assertIsNone(self.card(again['book_id'])['deleted_before'])
        # The record and its hashes stay, only hidden: files still wait to be cleaned from the X3, and
        # without the hashes the worker would bring the card back from a kept bookbot record.
        self.assertEqual(([1],[sha]),([row[0] for row in self.sql('SELECT dismissed FROM deleted_books')],[row[0] for row in self.sql('SELECT sha256 FROM deleted_epubs')]))
        self.assertNotEqual(revision,self.status()['library_revision'])

    def test_deleting_one_of_two_cards_keeps_what_the_other_still_uses(self):
        data=epub();sha=hashlib.sha256(data).hexdigest()
        a=self.web.post('/api/incoming/epub',params={'filename':'bot.epub'},content=data).json()['book_id']
        b=self.add(epub(TEXT+' Other.',title='Other'),'other.epub')['book_id']
        self.assertEqual(200,self.web.put(f'/api/books/{b}/epub',params={'filename':'bot.epub'},content=data).status_code)
        self.sql("UPDATE books SET created_at=datetime('now','-1 minute')")
        self.web.delete(f'/api/books/{a}')
        self.assertEqual(1,len(self.status()['incoming']))
        self.assertTrue(Path(self.shelf.incoming_path(sha)).exists())
        card=self.card(b)
        self.assertEqual(2,len(card['epubs']))
        self.assertEqual(data,self.web.get(f'/api/books/{b}/file/epub',params={'sha256':sha}).content)
        # A card that already stood on the shelf was not "added again", though it holds the same file.
        self.assertIsNone(card['deleted_before'])

    def test_record_matches_a_new_card_by_catalogue_work_or_by_title_and_authors(self):
        shelf=self.shelf
        def shelve(source_id,title,authors='Author',**fields):
            with shelf.db_session() as db:
                book=shelf.Book(source_id=source_id,title=title,authors=authors,status='want',**fields)
                db.add(book);db.commit();return book.id
        note=lambda bid:(self.card(bid)['deleted_before'] or {}).get('title')
        for source_id,title,title_ru in (('/works/OL1W','Dust','Пыль'),('kindle:KEY','From the Kindle',None),('epub:abc','Night Train!',None)):
            self.web.delete(f"/api/books/{shelve(source_id,title,title_ru=title_ru)}")
        self.assertEqual('Пыль',note(shelve('/works/OL1W','Another record of it','Somebody')))  # the title its card showed
        self.assertIsNone(note(shelve('kindle:KEY','Something else','Somebody')))  # a synthetic id names no book
        self.assertEqual('Night Train!',note(shelve('epub:xyz','night  train','AUTHOR')))
        self.assertIsNone(note(shelve('epub:other','Night Train','Another Author')))
        self.assertIsNone(note(shelve('/works/OL2W','Unrelated')))
        # Adding from the catalogue says it at once; of several records the latest one counts.
        bundle=dict(title='Wool',authors='Hugh Howey',year=None,language=None,description='',cover_urls=[])
        with patch.object(shelf,'fetch_work_bundle',return_value=bundle):
            add=lambda:self.web.post('/api/books',json={'source_id':'/works/OL9W'}).json()
            first=add()
            self.assertEqual(('ok',None),(first['status'],first['deleted_before']))
            self.web.delete(f"/api/books/{first['id']}")
            second=add();exists=add()
            self.assertEqual('Wool',second['deleted_before']['title'])
            self.assertEqual(('exists',second['deleted_before']),(exists['status'],exists['deleted_before']))
            self.web.delete(f"/api/books/{second['id']}")
            self.assertGreater(add()['deleted_before']['id'],second['deleted_before']['id'])

    def test_merged_duplicate_leaves_no_record_and_keeps_its_files(self):
        a=self.add();b=self.add(epub(TEXT+' Other.',title='Other'),'other.epub')
        self.assertEqual(200,self.web.post(f"/api/books/{a['book_id']}/merge",json={'target_id':b['book_id']}).status_code)
        self.assertEqual([],self.sql('SELECT * FROM deleted_books'))
        card=self.card(b['book_id'])
        self.assertEqual((2,None),(len(card['epubs']),card['deleted_before']))
        self.assertEqual(epub(),self.web.get(f"/api/books/{b['book_id']}/file/epub",params={'sha256':a['sha256']}).content)

    def test_deleting_a_card_does_not_touch_the_kindle_side(self):
        data=epub();bid=self.add(data)['book_id']
        own=self.root/'books'/'KINDLEBOOK'/'epub'/'book.epub';own.parent.mkdir(parents=True,exist_ok=True);own.write_bytes(data)
        self.kindle('KINDLEBOOK',bid,epub_path=str(own),epub_status='ready')
        self.store.core.attach_kindle('KINDLEBOOK')
        self.assertEqual([('KINDLEBOOK',bid)],self.sql('SELECT key,book_id FROM kindle_assets'))
        self.web.delete(f'/api/books/{bid}')
        self.assertEqual([],self.sql('SELECT * FROM epub_assets'))  # the library's copy went with the card
        self.assertTrue(own.exists())
        self.assertEqual([('KINDLEBOOK',str(own),'ready')],self.sql('SELECT key,epub_path,epub_status FROM kindle_books'))
        self.assertEqual([('KINDLEBOOK',None)],self.sql('SELECT key,book_id FROM reader_links'))  # hidden: it does not return

    # --- other sites can neither read nor change the shelf ---
    def test_shelf_sends_no_cors_headers_and_refuses_cross_site_writes(self):
        bid=self.add()['book_id'];evil={'Origin':'http://evil.example'}
        cors=lambda r:[h for h in r.headers if h.startswith('access-control-')]
        r=self.web.get('/api/books',headers=evil)
        self.assertEqual((200,[]),(r.status_code,cors(r)))
        r=self.web.options(f'/api/books/{bid}',headers={**evil,'Access-Control-Request-Method':'DELETE'})
        self.assertEqual((405,[]),(r.status_code,cors(r)))
        # Writes a browser sends without asking first: a form, a request without a body.
        for method,url,body in (('POST','/api/epubs?filename=evil.epub',dict(content=epub(TEXT+' Evil.'))),('POST','/api/reader/sync',{}),
                                ('DELETE',f'/api/books/{bid}',{}),('PATCH',f'/api/books/{bid}',dict(json={'status':'read'}))):
            for origin in ('http://evil.example','http://testserver.evil.example','null'):
                self.assertEqual(403,self.web.request(method,url,headers={'Origin':origin},**body).status_code,(method,url,origin))
        self.assertEqual([(bid,'unsorted')],[(b['id'],b['status']) for b in self.web.get('/api/books').json()])
        # The shelf's own page, also behind a proxy that terminates TLS; clients that are not browsers.
        for headers in ({'Origin':'http://testserver'},{'Origin':'https://testserver'},{}):
            self.assertEqual(200,self.web.patch(f'/api/books/{bid}',json={'status':'reading'},headers=headers).status_code,headers)
        with patch.object(self.shelf,'CORS_ORIGINS',['http://tools.lan']):
            self.assertEqual(200,self.web.patch(f'/api/books/{bid}',json={'status':'read'},headers={'Origin':'http://tools.lan'}).status_code)
            self.assertEqual(403,self.web.patch(f'/api/books/{bid}',json={'status':'want'},headers=evil).status_code)
        self.assertEqual('read',self.card(bid)['status'])
        # KOSync on the same port authenticates by itself.
        self.assertTrue(self.web.put('/syncs/progress',headers={**self.ko,**evil},json=dict(document=md5('book.epub'),progress=XPATH+'20',percentage=.2,device='KOReader',device_id='k')).json()['accepted'])

    # --- is the book on the X3 ---
    def test_sent_book_is_present_until_a_newer_listing_lacks_it(self):
        bid=self.add()['book_id'];device=self.device()
        self.assertIsNone(self.card(bid)['x3'])
        revision=self.status()['library_revision']
        with device.patch:
            sent=self.web.post(f'/api/books/{bid}/send-x3')
        self.assertEqual(200,sent.status_code,sent.text)
        name=sent.json()['filename']
        x3=self.card(bid)['x3']
        self.assertEqual(('present',True,name),(x3['state'],x3['confirmed'],x3['filename']))  # the listing taken after the upload has it
        self.assertTrue(x3['sent_at'] and x3['checked_at']);self.assertIsNone(x3['synced_at'])
        self.assertNotEqual(revision,self.status()['library_revision'])
        # The next listing has the file.
        revision=self.status()['library_revision']
        status=self.check(device)
        self.assertNotIn('interval',status);self.assertTrue(status['listing_checked_at'])
        self.assertEqual(status['listing_checked_at'],self.card(bid)['x3']['checked_at'])
        self.assertEqual(('present',True),(self.card(bid)['x3']['state'],self.card(bid)['x3']['confirmed']))
        self.assertNotEqual(revision,self.status()['library_revision'])
        # The file was removed on the device; a listing of the same second is not yet "newer".
        del device.files[name];self.check(device)
        self.sql('UPDATE deliveries SET sent_at=(SELECT checked_at FROM x3_listing)')
        self.assertEqual(('present',False),(self.card(bid)['x3']['state'],self.card(bid)['x3']['confirmed']))
        self.sql('UPDATE deliveries SET sent_at=sent_at-60')
        x3=self.web.get('/api/books').json()[0]['x3']
        self.assertEqual(('missing',True,name),(x3['state'],x3['confirmed'],x3['filename']))
        # Absence is expected for finished and abandoned books: no mark at all.
        for status in ('read','dropped'):
            self.web.patch(f'/api/books/{bid}',json={'status':status})
            self.assertIsNone(self.card(bid)['x3'])
        self.web.patch(f'/api/books/{bid}',json={'status':'reading'})
        self.assertEqual('missing',self.card(bid)['x3']['state'])
        # An unreachable device changes nothing: the saved listing stays.
        before=self.sql('SELECT checked_at,files,revision FROM x3_listing')
        self.assertFalse(self.check(self.device(online=False))['online'])
        self.assertEqual(before,self.sql('SELECT checked_at,files,revision FROM x3_listing'))
        self.assertEqual('missing',self.card(bid)['x3']['state'])

    def test_finished_book_still_on_the_device_stays_marked_present(self):
        bid=self.add()['book_id'];device=self.device()
        with device.patch:
            self.web.post(f'/api/books/{bid}/send-x3')
        self.web.patch(f'/api/books/{bid}',json={'status':'read'})
        self.assertEqual(('present',True),(self.card(bid)['x3']['state'],self.card(bid)['x3']['confirmed']))
        self.sql('DELETE FROM x3_listing')
        self.assertEqual(('present',False),(self.card(bid)['x3']['state'],self.card(bid)['x3']['confirmed']))
        self.check(device)
        self.assertEqual(('present',True),(self.card(bid)['x3']['state'],self.card(bid)['x3']['confirmed']))

    def test_sideloaded_file_bound_to_a_book_is_present_without_any_send(self):
        direct=self.add()['book_id']
        through_kindle=self.add(epub(TEXT+' Other.',title='Other'),'other.epub')['book_id']
        stranger=self.add(epub(TEXT+' Third.',title='Third'),'third.epub')['book_id']
        self.assertEqual(200,self.web.post(f"/api/reader/documents/{md5('sideloaded.epub')}/bind",json={'book_id':direct}).status_code)
        self.kindle('KINDLEBOOK',through_kindle)
        self.sql('INSERT INTO documents(document,book_key) VALUES(?,?)',md5('from-kindle.epub'),'KINDLEBOOK')
        self.check(self.device(['sideloaded.epub','from-kindle.epub','unknown.epub','notes.txt']))
        for bid,name in ((direct,'sideloaded.epub'),(through_kindle,'from-kindle.epub')):
            x3=self.card(bid)['x3']
            self.assertEqual(('present',True,name,None,None),(x3['state'],x3['confirmed'],x3['filename'],x3['sent_at'],x3['synced_at']))
        self.assertIsNone(self.card(stranger)['x3'])
        self.assertEqual([{'name':'sideloaded.epub','size':1000,'epub':True},{'name':'from-kindle.epub','size':1000,'epub':True},
                          {'name':'unknown.epub','size':1000,'epub':True},{'name':'notes.txt','size':1000,'epub':False}],
                         json.loads(self.sql('SELECT files FROM x3_listing')[0][0]))

    def test_x3_sync_newer_than_the_listing_counts_as_present(self):
        bid=self.add()['book_id'];other=self.add(epub(TEXT+' Other.',title='Other'),'other.epub')['book_id']
        self.check(self.device(['something-else.epub']))
        self.sql('UPDATE x3_listing SET checked_at=checked_at-60')
        self.assertTrue(self.push(md5('book.epub'),20).json()['accepted'])
        self.assertTrue(self.push(md5('other.epub'),20,device='KOReader',device_id='koreader').json()['accepted'])
        x3=self.card(bid)['x3']
        self.assertEqual(('present',False,None,None),(x3['state'],x3['confirmed'],x3['filename'],x3['sent_at']))
        self.assertTrue(x3['synced_at'])
        self.assertIsNone(self.card(other)['x3'])  # another reader says nothing about the X3
        self.sql('UPDATE reader_observations SET received_at=received_at-120')
        self.assertEqual(('missing',True),(self.card(bid)['x3']['state'],self.card(bid)['x3']['confirmed']))
        self.sql('DELETE FROM x3_listing')
        self.assertEqual(('present',False,None),(self.card(bid)['x3']['state'],self.card(bid)['x3']['confirmed'],self.card(bid)['x3']['checked_at']))

    def test_legacy_sent_upload_is_delivery_evidence(self):
        bid=self.add()['book_id'];self.kindle('OLDKEY',bid)
        with self.shelf.db_session() as db:
            db.add(self.shelf.X3Upload(key='OLDKEY',filename='old.epub',status='sent',sent_at=self.shelf.utcnow()))
            db.commit()
        x3=self.card(bid)['x3']
        self.assertEqual(('present',False,'old.epub'),(x3['state'],x3['confirmed'],x3['filename']))
        self.assertTrue(x3['sent_at'])
        self.check(self.device(['old.epub']))
        self.assertEqual(('present',True,'old.epub'),tuple(self.card(bid)['x3'][k] for k in ('state','confirmed','filename')))

    def test_every_explicit_device_request_saves_the_listing_and_nothing_else_asks(self):
        from bookhub.worker import reconcile
        bid=self.add()['book_id'];device=self.device(['a.epub'])
        revision=lambda:self.sql('SELECT revision FROM x3_listing')
        with device.patch,patch.object(self.shelf,'x3_latest_release',return_value=None):
            self.web.get('/api/books');self.web.get(f'/api/books/{bid}');self.status();reconcile(self.store)
            self.assertEqual(([],[]),(device.calls,revision()))
            self.assertEqual({'count':1,'bytes':1000},self.web.get('/api/x3/device').json()['books'])
            self.assertEqual([(1,)],revision())
            self.assertEqual([{'name':'a.epub','size':1000,'epub':True,'key':None}],self.web.get('/api/x3/files').json()['files'])
            self.assertEqual([(2,)],revision())
            self.web.post('/api/x3/check')
            self.assertEqual([(3,)],revision())
            self.assertEqual(200,self.web.post(f'/api/books/{bid}/send-x3').status_code)
            self.assertEqual([(5,)],revision())  # before the upload and after it
        # A device that answers the status but not the file list keeps the previous listing.
        def no_files(method,path,**kwargs):
            if path=='/api/files':return SimpleNamespace(ok=False,status_code=500,text='',json=lambda:None)
            return SimpleNamespace(ok=True,status_code=200,text='',json=lambda:{'version':'1.6.5'})
        with patch.object(self.shelf,'x3_request',side_effect=no_files):
            status=self.web.post('/api/x3/check').json()
            self.assertEqual(502,self.web.get('/api/x3/files').status_code)
        self.assertTrue(status['online'] and status['error'])
        self.assertEqual([(5,)],revision())

    # --- the X3 web channel: every contact also does what was waiting for the device ---
    def test_delivery_is_recorded_only_when_the_listing_shows_the_whole_file(self):
        b=self.add();bid=b['book_id'];device=self.device();name='book-'+b['sha256'][:12]+'.epub'
        def refused():
            with device.patch:
                r=self.web.post(f'/api/books/{bid}/send-x3')
            self.assertEqual(([],None),(self.sql('SELECT * FROM deliveries'),self.card(bid)['x3']))
            return r.status_code,r.json()['detail']
        device.stored=lambda size:None  # "done", and no such file
        self.assertEqual((502,'X3 сообщил об успехе, но файла на нём нет. Отправь ещё раз.'),refused())
        device.stored=lambda size:size-1
        self.assertEqual((502,f'X3 сохранил файл не целиком ({len(epub())-1} из {len(epub())} байт). Удали его на устройстве и отправь ещё раз.'),refused())
        self.assertEqual(409,refused()[0])  # the broken file is still there under that name
        device.files.clear();device.refuse='Disk full'
        self.assertEqual((502,'X3 не принял файл: Disk full'),refused())
        device.refuse=None;device.stored=lambda size:size
        answer=self.send(device,bid)
        self.assertEqual(('sent',name,{'positions':0,'removed':[]}),(answer['status'],answer['filename'],answer['contact']))
        self.assertEqual((('http://x3.invalid','/',name),len(epub())),(device.uploads[-1],device.files[name]))
        self.assertEqual([(bid,b['sha256'],name)],self.sql('SELECT book_id,sha256,filename FROM deliveries'))
        self.assertEqual(('present',True,name),tuple(self.card(bid)['x3'][k] for k in ('state','confirmed','filename')))

    def test_files_of_deleted_cards_leave_the_x3_at_the_next_contact(self):
        a=self.add()['book_id'];b=self.add(epub(TEXT+' Other.',title='Other'),'other.epub')['book_id']
        device=self.device(['stranger.epub','side.epub','lookalike.epub'])
        device.files['side.epub']=self.size(a)  # the owner's own copy of this EPUB under another name
        mine,others=self.send(device,a)['filename'],self.send(device,b)['filename']
        for bound in ('side.epub','lookalike.epub'):  # a name alone proves nothing: lookalike has another size
            self.assertEqual(200,self.web.post(f"/api/reader/documents/{md5(bound)}/bind",json={'book_id':a}).status_code)
        self.web.delete(f'/api/books/{a}')
        waiting=lambda:[json.loads(g[0]) for g in self.sql('SELECT x3_files FROM deleted_books')]
        self.assertEqual([sorted([mine,'side.epub'])],waiting())  # what was sent for the card and what the listing ties to it
        self.assertEqual([],device.deleted)  # deleting the card does not reach for the device
        status=self.check(device)
        self.assertEqual({'positions':0,'removed':['side.epub',mine]},status['contact'])
        self.assertEqual([['/side.epub','/'+mine]],device.deleted)
        self.assertEqual(['stranger.epub','lookalike.epub',others],list(device.files))
        self.assertEqual(['stranger.epub','lookalike.epub',others],[f['name'] for f in json.loads(self.sql('SELECT files FROM x3_listing')[0][0])])
        self.assertEqual(('present',True),tuple(self.card(b)['x3'][k] for k in ('state','confirmed')))
        # One contact settles the record: the same name put on the device later is left alone.
        self.assertEqual([[]],waiting())
        device.files[mine]=1000
        self.assertEqual([],self.check(device)['contact']['removed'])
        self.assertIn(mine,device.files)

    def test_file_claimed_by_a_live_card_or_refused_by_the_device_stays(self):
        data=epub();a=self.add(data)['book_id'];device=self.device(['side.epub'])
        device.files['side.epub']=self.size(a)
        self.web.post(f"/api/reader/documents/{md5('side.epub')}/bind",json={'book_id':a})
        name=self.send(device,a)['filename']
        self.web.delete(f'/api/books/{a}')
        # The device refuses: the action goes on, the files wait for the next contact.
        device.refuse='Failed to delete some items'
        with self.assertLogs('myshelf',level='WARNING'):
            status=self.check(device)
        self.assertEqual((True,[]),(status['online'],status['contact']['removed']))
        self.assertEqual([sorted(['side.epub',name])],[json.loads(g[0]) for g in self.sql('SELECT x3_files FROM deleted_books')])
        # The book is on the shelf again and the sideloaded file is bound to its new card.
        device.refuse=None;again=self.add(data)['book_id']
        self.web.post(f"/api/reader/documents/{md5('side.epub')}/bind",json={'book_id':again})
        # The copy sent before the deletion is the re-added card's delivery again (Library.ingest), so it stays as well.
        self.assertEqual([],self.check(device)['contact']['removed'])
        self.assertEqual(sorted(['side.epub',name]),sorted(device.files))
        self.assertEqual(('present',True),(self.card(again)['x3']['state'],self.card(again)['x3']['confirmed']))

    def test_dismissed_note_still_cleans_the_x3_and_an_upload_without_confirmation_is_not_stuck(self):
        bid=self.add()['book_id'];device=self.device()
        name=self.send(device,bid)['filename']
        self.web.delete(f'/api/books/{bid}')
        record=self.sql('SELECT id FROM deleted_books')[0][0]
        # The note is closed before the X3 was seen again: the file of the deleted card is still removed.
        self.assertEqual({'status':'forgotten'},self.web.delete(f'/api/deleted/{record}').json())
        self.assertEqual([name],self.check(device)['contact']['removed'])
        self.assertEqual([],list(device.files))
        # A failed action does not swallow what the contact did: the next answer reports it.
        other=self.add(epub(TEXT+' Second.',title='Second'),'second.epub')['book_id']
        sent=self.send(device,other)['filename'];self.web.delete(f'/api/books/{other}')
        third=self.add(epub(TEXT+' Third.',title='Third'),'third.epub')['book_id']
        with device.patch:
            self.assertEqual(409,self.web.post(f'/api/books/{third}/x3-remove').status_code)
        self.assertEqual([],list(device.files))
        self.assertEqual([sent],self.check(device)['contact']['removed'])
        # The upload arrived but the listing that confirms it could not be had: the delivery counts,
        # and a file with this name and size met later is the same file, not a stranger.
        device.leaves=False
        with device.patch, patch.object(self.shelf,'x3_fetch_listing',side_effect=[[],self.shelf.X3Error('X3 не отвечает')]):
            r=self.web.post(f'/api/books/{third}/send-x3')
        self.assertEqual(200,r.status_code,r.text)
        self.assertEqual('present',self.card(third)['x3']['state'])
        self.sql('DELETE FROM deliveries');self.sql('DELETE FROM documents WHERE library_book_id=?',third)
        size=Path(self.sql('SELECT path FROM epub_assets WHERE sha256=(SELECT sha256 FROM book_assets WHERE book_id=?)',third)[0][0]).stat().st_size
        device.files={r.json()['filename']:size};uploads=len(device.uploads)
        self.assertEqual(r.json()['filename'],self.send(device,third)['filename'])
        self.assertEqual((uploads,1),(len(device.uploads),len(self.sql('SELECT 1 FROM deliveries'))))

    def test_removal_waits_for_the_position_and_a_resend_checks_the_size(self):
        bid=self.add()['book_id'];device=self.device()
        name=self.send(device,bid)['filename'];size=self.size(bid)
        # The device cannot give the saved position of this file: the file and its position stay on it.
        device.fails=device.DeviceError('Failed to open file')
        with device.patch:
            r=self.web.post(f'/api/books/{bid}/x3-remove')
        self.assertEqual((502,[],[name]),(r.status_code,device.deleted,list(device.files)))
        device.fails=None
        with device.patch:
            self.assertEqual(200,self.web.post(f'/api/books/{bid}/x3-remove').status_code)
        # The name carries the content hash: the same name with another size is a broken copy, not a delivery.
        device.files[name]=size-1
        with device.patch:
            r=self.web.post(f'/api/books/{bid}/send-x3')
        self.assertEqual((409,None),(r.status_code,self.card(bid)['x3']))
        device.files[name]=size
        self.assertEqual(name,self.send(device,bid)['filename'])
        self.assertEqual(('present',True),tuple(self.card(bid)['x3'][k] for k in ('state','confirmed')))

    def test_positions_saved_on_the_x3_reach_the_library_once(self):
        a=self.add();bid=a['book_id'];other=self.add(epub(TEXT+' Other.',title='Other'),'other.epub')
        device=self.device(['side.epub','unknown.epub','notes.txt','from-kindle.epub','lookalike.epub'])
        device.files['side.epub']=self.size(other['book_id'])
        name=self.send(device,bid)['filename']
        for document in ('side.epub',name,'lookalike.epub'):  # what was sent under a name is what lies under it, whatever is bound later
            self.web.post(f'/api/reader/documents/{md5(document)}/bind',json={'book_id':other['book_id']})
        self.kindle('KINDLEBOOK',bid);self.sql('INSERT INTO documents(document,book_key) VALUES(?,?)',md5('from-kindle.epub'),'KINDLEBOOK')
        device.progress={'/side.epub':dict(spine=2,page=5,page_count=None,visible_offset=None),
                         '/'+name:dict(spine=0,page=3,page_count=10,visible_offset=42),
                         '/lookalike.epub':dict(spine=5,page=0,page_count=3,visible_offset=7)}
        taken=[]
        def accept(book_id,sha256,spine,**how):
            taken.append((book_id,sha256,spine,how));return dict(accepted=book_id==bid,reason=None if book_id==bid else 'behind')
        with patch.object(self.store.core,'accept_reader_position',side_effect=accept,create=True):
            device.asked.clear();status=self.check(device)
            # Asked one by one: every EPUB whose library file is known, by a delivery or by a bound document.
            self.assertEqual(['/side.epub','/'+name],device.asked)
            # A file bound by name only and of another size (lookalike.epub) is not asked at all.
            self.assertEqual([(other['book_id'],other['sha256'],2,dict(visible_offset=None,document=md5('side.epub'))),
                              (bid,a['sha256'],0,dict(visible_offset=42,document=md5(name)))],taken)
            self.assertEqual({'positions':1,'removed':[]},status['contact'])  # one of the two moved its book
            # The same values at the next contact are not news: a position reset on the shelf stays reset.
            self.assertEqual(200,self.web.post(f'/api/books/{bid}/reset-position').status_code)
            self.assertEqual((0,2),(self.check(device)['contact']['positions'],len(taken)))
            device.progress['/'+name]=dict(spine=1,page=2,page_count=4,visible_offset=None)
            self.assertEqual(1,self.check(device)['contact']['positions'])
            # Without a text offset only the chapter is known: a page count says nothing reliable.
            self.assertEqual((bid,a['sha256'],1,dict(visible_offset=None,document=md5(name))),taken[-1])
            # A book the device has not opened since (its saved position is gone) is told again when it has one.
            del device.progress['/side.epub'];self.check(device)
            device.progress['/side.epub']=dict(spine=2,page=5,page_count=None,visible_offset=None);self.check(device)
            self.assertEqual((4,other['book_id']),(len(taken),taken[-1][0]))

    def test_path_on_the_card_is_absolute_however_the_folder_is_configured(self):
        for folder,path in (('/','/a.epub'),('/Books','/Books/a.epub'),('/Books/','/Books/a.epub'),('Books','/Books/a.epub')):
            with patch.object(self.shelf,'X3_BOOKS_DIR',folder):
                self.assertEqual(path,self.shelf.x3_path('a.epub'))

    def test_reading_positions_never_fails_the_action(self):
        a=self.add()['book_id'];b=self.add(epub(TEXT+' Other.',title='Other'),'other.epub')['book_id'];device=self.device()
        first,second=('/'+self.send(device,bid)['filename'] for bid in (a,b))
        device.progress={first:dict(spine=7,page=0,page_count=1,visible_offset=0),second:dict(spine=0,page=0,page_count=1,visible_offset=0)}
        def accept(book_id,sha256,spine,**how):
            if spine==7:raise ValueError('spine index out of range')
            return dict(accepted=True)
        with patch.object(self.store.core,'accept_reader_position',side_effect=accept,create=True) as accepted:
            with self.assertLogs('myshelf',level='WARNING'):
                status=self.check(device)
            self.assertEqual((True,1),(status['online'],status['contact']['positions']))
            # What the library could not place is not tried again until the device says something new.
            self.check(device);self.assertEqual(2,accepted.call_count)
            device.progress[first]['spine']=device.progress[second]['spine']=1
            # The device leaves in the middle: the first network error ends the reading, the listing is kept.
            device.fails=self.shelf.X3Error('X3 не отвечает');device.asked.clear();listed=self.sql('SELECT revision FROM x3_listing')[0][0]
            status=self.check(device)
            self.assertEqual((True,0,[first]),(status['online'],status['contact']['positions'],device.asked))
            self.assertEqual(listed+1,self.sql('SELECT revision FROM x3_listing')[0][0])
            # One unreadable file does not stop the others.
            device.fails=device.DeviceError('progress.bin of 3 bytes');device.asked.clear()
            with self.assertLogs('myshelf',level='WARNING'):
                self.assertEqual(0,self.check(device)['contact']['positions'])
            self.assertEqual([first,second],device.asked)
            device.fails=None
            self.assertEqual((2,4),(self.check(device)['contact']['positions'],accepted.call_count))

    def test_book_removed_from_the_x3_on_request_shows_no_x3_mark(self):
        bid=self.add()['book_id'];device=self.device(['stranger.epub','side.epub'])
        self.web.patch(f'/api/books/{bid}',json={'status':'reading'})
        remove=lambda book=bid:self.web.post(f'/api/books/{book}/x3-remove')
        with device.patch:
            self.assertEqual((409,404),(remove().status_code,remove(999).status_code))  # nothing of this book is there
        name=self.send(device,bid)['filename']
        self.web.post(f"/api/reader/documents/{md5('side.epub')}/bind",json={'book_id':bid})
        device.online=False
        with device.patch:
            self.assertEqual(503,remove().status_code)
        device.online=True;revision=self.status()['library_revision']
        with device.patch:
            r=remove()
        self.assertEqual(200,r.status_code,r.text)
        self.assertEqual({'status':'removed','files':sorted(['side.epub',name]),'contact':{'positions':0,'removed':[]}},r.json())
        self.assertEqual(([sorted(['/side.epub','/'+name])],['stranger.epub']),(device.deleted,list(device.files)))
        self.assertNotEqual(revision,self.status()['library_revision'])
        # No mark at all, not «нет»: the owner took the book off the device, its old delivery proves nothing.
        card=self.card(bid)
        self.assertEqual((None,1),(card['x3'],len(card['deliveries'])))
        self.sql('UPDATE deliveries SET sent_at=sent_at-120');self.sql('UPDATE books SET x3_removed_at=x3_removed_at-60')
        self.assertIsNone(self.card(bid)['x3'])  # nor does a later listing without the file
        with device.patch:
            self.assertEqual(409,remove().status_code)
        # Asking again when nothing is there renews the wish: still no mark, and the stamp is fresh.
        self.assertIsNone(self.card(bid)['x3']);self.sql('UPDATE books SET x3_removed_at=x3_removed_at-60')
        # What is newer than the removal counts again: a sync from the X3, a new delivery.
        self.assertTrue(self.push(md5('side.epub'),20).json()['accepted'])
        self.assertEqual(('present',False),tuple(self.card(bid)['x3'][k] for k in ('state','confirmed')))
        self.sql('DELETE FROM reader_observations');self.assertIsNone(self.card(bid)['x3'])
        self.assertEqual(name,self.send(device,bid)['filename'])
        self.assertEqual(('present',True,name),tuple(self.card(bid)['x3'][k] for k in ('state','confirmed','filename')))
        self.assertEqual([(None,)],self.sql('SELECT x3_removed_at FROM books'))

    def test_removal_counts_even_when_the_device_leaves_right_after_it(self):
        bid=self.add()['book_id'];device=self.device()
        self.web.patch(f'/api/books/{bid}',json={'status':'reading'})
        name=self.send(device,bid)['filename'];device.leaves=True
        with device.patch:
            r=self.web.post(f'/api/books/{bid}/x3-remove')
        self.assertEqual((200,[name]),(r.status_code,r.json()['files']))
        self.assertIn(name,self.sql('SELECT files FROM x3_listing')[0][0])  # the saved listing is the one taken before
        self.assertIsNone(self.card(bid)['x3'])

    def test_every_answer_after_a_contact_tells_what_was_done_on_the_device(self):
        data=epub();bid=self.add(data)['book_id'];device=self.device();nothing={'positions':0,'removed':[]}
        own=self.root/'contact.epub';own.write_bytes(data)
        self.kindle('KINDLEBOOK',bid,epub_path=str(own),epub_status='ready')
        with device.patch,patch.object(self.shelf,'x3_latest_release',return_value=None):
            answers=[self.web.post('/api/x3/check'),self.web.get('/api/x3/device'),self.web.get('/api/x3/files'),self.web.post(f'/api/books/{bid}/send-x3'),
                     self.web.post('/api/reader/KINDLEBOOK/x3'),self.web.post(f'/api/books/{bid}/x3-remove')]
            self.assertEqual([(200,nothing)]*6,[(r.status_code,r.json().get('contact')) for r in answers],[r.text for r in answers])
            # An unreachable device: the answers that still come say that nothing was done.
            device.online=False
            self.assertEqual([nothing,nothing],[self.web.post('/api/x3/check').json()['contact'],self.web.get('/api/x3/device').json()['contact']])

    # --- leftovers of the first round ---
    def test_epub_refused_by_its_kindle_book_is_not_offered_again_on_every_sync(self):
        from bookhub.sync.store import Problem
        inc=self.web.post('/api/incoming/epub',params={'filename':'sent.epub'},content=epub()).json()['id']
        self.store.inventory('kindle',[dict(key=key,fingerprint='a'*64,path=f'/mnt/us/documents/{stem}_{key}.kfx') for key,stem in (('KINDLEBOOK','sent'),('OTHERBOOK','other'))])
        service=self.shelf.reader_service;row=lambda:self.sql('SELECT status,error FROM incoming_epubs')
        # Not accepted for now: the next pass tries again.
        with patch.object(service,'upload_epub',side_effect=Problem('Inventory changed during upload; scan again',409)) as upload:
            self.shelf.reader_sync_once();self.shelf.reader_sync_once()
        self.assertEqual((2,[('waiting','KINDLEBOOK: Inventory changed during upload; scan again')]),(upload.call_count,row()))
        # A refusal of the file itself is final.
        with patch.object(service,'upload_epub',side_effect=Problem('EPUB не совпадает с книгой Kindle')) as upload:
            self.shelf.reader_sync_once();self.shelf.reader_sync_once()
        self.assertEqual((1,[('rejected','KINDLEBOOK: EPUB не совпадает с книгой Kindle')]),(upload.call_count,row()))
        # It still asks for attention, and the owner can attach it by hand.
        self.assertEqual([(inc,'rejected','KINDLEBOOK: EPUB не совпадает с книгой Kindle')],[(i['id'],i['status'],i['error']) for i in self.status()['incoming']])
        with patch.object(service,'upload_epub',return_value=dict(status='queued')):
            r=self.web.post(f'/api/incoming/{inc}/attach',json={'key':'OTHERBOOK'})
        self.assertEqual(('attached',['OTHERBOOK'],[]),(r.json()['status'],r.json()['attached'],self.status()['incoming']))
        # A refused EPUB goes with its card, like a waiting one.
        gone=self.web.post('/api/incoming/epub',params={'filename':'gone.epub'},content=epub(TEXT+' Gone.')).json()
        self.sql("UPDATE incoming_epubs SET status='rejected' WHERE id=?",gone['id'])
        self.web.delete(f"/api/books/{gone['book_id']}")
        self.assertEqual([('attached',)],self.sql('SELECT status FROM incoming_epubs'))

    def test_revision_follows_the_time_of_the_listing_and_x3_answers_carry_it(self):
        self.add();checked=self.check(self.device())
        self.assertTrue(checked['listing_checked_at'])
        revision=self.status()['library_revision']
        self.sql('UPDATE x3_listing SET checked_at=checked_at+1')  # the cards show this time
        self.assertNotEqual(revision,self.status()['library_revision'])
        with self.shelf.db_session() as db:
            db.add(self.shelf.X3Upload(key='OLDKEY',filename='old.epub',status='queued'));db.commit()
        dequeued=self.web.delete('/api/x3/queue/OLDKEY').json()
        self.assertEqual(([],self.status()['x3']['listing_checked_at']),(dequeued['queue'],dequeued['listing_checked_at']))
        self.assertNotEqual(checked['listing_checked_at'],dequeued['listing_checked_at'])

    # --- bookbot uploads do not block the event loop ---
    def test_incoming_epub_is_ingested_in_the_threadpool(self):
        seen=[];ingest=self.store.core.ingest
        def spy(*args,**kwargs):
            try:asyncio.get_running_loop();seen.append('event loop')
            except RuntimeError:seen.append('thread')
            return ingest(*args,**kwargs)
        with patch.object(self.store.core,'ingest',side_effect=spy):
            first=self.web.post('/api/incoming/epub',params={'filename':'bot.epub','source':'bookbot'},content=epub())
            again=self.web.post('/api/incoming/epub',params={'filename':'bot.epub','source':'bookbot'},content=epub())
        self.assertEqual((200,200),(first.status_code,again.status_code),first.text)
        self.assertEqual(['thread','thread'],seen)
        self.assertEqual(('ready',first.json()['book_id']),(first.json()['status'],again.json()['book_id']))
        self.assertTrue(again.json()['duplicate']);self.assertEqual(first.json()['id'],again.json()['id'])
        self.assertEqual([('bot.epub',len(epub()),'bookbot','waiting')],self.sql('SELECT filename,size,source,status FROM incoming_epubs'))
        self.assertEqual(400,self.web.post('/api/incoming/epub',params={'filename':'bad.epub'},content=b'PKnot a zip').status_code)
        self.assertEqual(1,len(self.web.get('/api/books').json()))

    # --- "ok" is the web process; the others report for themselves ---
    def test_status_reports_sync_and_worker_by_their_heartbeats(self):
        services=lambda:self.status()['services']
        self.assertEqual({'sync':False,'worker':False},services())
        worker=self.root/'worker-heartbeat.json'
        worker.write_text(json.dumps(dict(time=time.time())))
        self.assertEqual({'sync':False,'worker':True},services())
        worker.write_text(json.dumps(dict(time=time.time()-91)))
        self.assertEqual({'sync':False,'worker':False},services())
        worker.write_text('not json')
        self.assertEqual({'sync':False,'worker':False},services())
        beat=self.root/'sync-heartbeat.json'
        with TestClient(self.application.create_sync_app()) as running:  # a served app, as under uvicorn
            deadline=time.time()+5
            while not beat.exists() and time.time()<deadline:time.sleep(.01)
            self.assertEqual(200,running.get('/health').status_code)
            self.assertEqual({'sync':True,'worker':False},services())
            self.assertTrue(self.status()['ok'])
        self.assertEqual([],[p.name for p in self.root.glob('*.tmp')])
        beat.write_text(json.dumps(dict(time=time.time()-91)))
        self.assertEqual({'sync':False,'worker':False},services())
        self.assertTrue(self.status()['ok'])  # the web process itself is still fine


class CorsAllowListTests(unittest.TestCase):
    """CORS_ORIGINS is read when the shelf is imported: its own data directory, its own copy of the modules."""
    def test_listed_origins_get_cors_headers_and_may_write(self):
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,BOOKHUB_DATA=temp,BOOKHUB_DB=str(Path(temp)/'bookhub.sqlite3'),AI_URL='',AI_MODEL='',
                                                             SYNC_API_KEY='token',SYNC_USER='reader',SYNC_PASSWORD='password',CORS_ORIGINS='http://tools.lan, https://notes.lan/'):
            forget_shelf()
            from bookhub import application,shelf
            web=TestClient(application.create_web_app())
            try:
                listed={'Origin':'http://tools.lan'};evil={'Origin':'http://evil.example'}
                self.assertEqual('http://tools.lan',web.get('/api/books',headers=listed).headers['access-control-allow-origin'])
                self.assertNotIn('access-control-allow-origin',web.get('/api/books',headers=evil).headers)
                r=web.options('/api/books/1',headers={**listed,'Access-Control-Request-Method':'DELETE'})
                self.assertEqual((200,'http://tools.lan'),(r.status_code,r.headers['access-control-allow-origin']))
                added=web.post('/api/epubs?filename=book.epub',content=epub(),headers={'Origin':'https://notes.lan'})
                self.assertEqual(200,added.status_code,added.text)
                self.assertEqual(403,web.delete(f"/api/books/{added.json()['book_id']}",headers=evil).status_code)
                self.assertEqual(1,len(web.get('/api/books').json()))
            finally:
                web.close();shelf.engine.dispose();forget_shelf()


if __name__=='__main__':unittest.main()
