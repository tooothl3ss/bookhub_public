import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from fastapi.testclient import TestClient
from app.main import create_app
from app.store import Store
from app.worker import run_next
from app.engine.kindle_map import PositionMap, write_map

KEY='TESTBOOK'
TOKEN='test-key'
TEXT='Белый свет уже не так слепит. Здесь начинается глава с уникальным текстом. Продолжение истории впереди.'
OTHER='Entirely different language and wording without a single matching phrase.'
XPATH='/body/DocFragment[1]/body/p[1]/text()[1].3'

def epub_bytes(text=TEXT):
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w') as z:
        z.writestr('META-INF/container.xml','<container><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>')
        z.writestr('book.opf','<package><manifest><item id="ch" href="ch.xhtml"/></manifest><spine><itemref idref="ch"/></spine></package>')
        z.writestr('ch.xhtml','<html><body><p>'+text+'</p></body></html>')
    return buf.getvalue()

def archive(name='book.kfx',content=b'fake'):
    buf=io.BytesIO()
    with tarfile.open(fileobj=buf,mode='w') as t:
        info=tarfile.TarInfo(name);info.size=len(content);t.addfile(info,io.BytesIO(content))
    return buf.getvalue()

class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.app=create_app(self.temp.name,TOKEN,'reader','password')
        self.store=self.app.state.store
        self.client=TestClient(self.app);self.addCleanup(self.client.close)
        self.auth={'Authorization':'Bearer '+TOKEN}
        self.ko={'x-auth-user':'reader','x-auth-key':hashlib.md5(b'password').hexdigest()}
        self.fp=hashlib.sha256(b'book').hexdigest()
        self.api('POST','/api/kindle/inventory',json={'device_id':'kindle','books':[{'key':KEY,'title':'Тестовая книга','fingerprint':self.fp}]})

    def api(self,method,path,**kwargs):
        headers={**self.auth,**kwargs.pop('headers',{})}
        return self.client.request(method,path,headers=headers,**kwargs)

    def map_ready(self,m=None):
        # Map fixture isolates protocol/state tests from binary parser tests.
        m=m or PositionMap(dict(version=1,book=dict(key=KEY),total_positions=len(TEXT),chunks=[dict(pid=0,eid=1,offset=0,length=len(TEXT),text=TEXT)]))
        folder=self.store.root/'maps';host=folder/'test.json';compact=folder/'test.posmap'
        write_map(m,host,compact)
        with self.store.connect(write=True) as db:
            db.execute("UPDATE kindle_books SET map_path=?,compact_path=?,map_status='ready',asset_version=1 WHERE key=?",(str(host),str(compact),KEY))

    def ready(self):
        self.map_ready()
        r=self.api('PUT',f'/api/books/{KEY}/epub',content=epub_bytes());self.assertEqual(200,r.status_code)
        self.assertTrue(run_next(self.store));self.assertEqual('ready',self.store.book(KEY)['epub_status'])

    def test_auth_and_browser_csrf(self):
        self.assertEqual(401,self.client.get('/api/library').status_code)
        self.assertEqual(200,self.client.get('/health').status_code)
        self.assertEqual(200,self.client.get('/api/library',auth=('reader','password')).status_code)
        # The old Reader Sync pages are gone; the API they used stays.
        for path in ('/','/static/app.js'):self.assertEqual(404,self.client.get(path,auth=('reader','password')).status_code)
        self.assertEqual(403,self.client.post('/api/kindle/inventory',auth=('reader','password'),json={}).status_code)
        self.assertEqual(401,self.client.get('/users/auth',headers={'x-auth-user':'reader','x-auth-key':'bad'}).status_code)
        self.assertEqual(200,self.client.get('/users/auth',headers=self.ko).status_code)

    def test_invalid_json_and_archive_paths(self):
        self.assertEqual(400,self.api('POST','/api/kindle/inventory',json=[]).status_code)
        for data in (b'not tar',archive('../escape.kfx'),archive('/escape.kfx'),archive('book.yjf')):
            r=self.api('PUT',f'/api/books/{KEY}/bundle',content=data,headers={'X-Source-Fingerprint':self.fp})
            self.assertEqual(400,r.status_code,r.text)
        self.assertFalse(list((self.store.root/'tmp').iterdir()))

    def test_ingest_idempotency_and_persistent_queue(self):
        for _ in range(2):
            r=self.api('PUT',f'/api/books/{KEY}/bundle',content=archive(),headers={'X-Source-Fingerprint':self.fp})
            self.assertEqual(200,r.status_code,r.text)
        reopened=Store(self.temp.name)
        self.assertEqual(1,len(reopened.library()['jobs']))
        self.assertFalse(reopened.manifest(KEY)['needs_upload'])
        self.assertEqual('queued',reopened.book(KEY)['map_status'])
        self.assertEqual(409,self.api('GET',f'/api/books/{KEY}/map').status_code)

    def test_unknown_document_then_bind_and_translate(self):
        r=self.client.put('/syncs/progress',headers=self.ko,json=dict(document='doc',progress=XPATH,percentage=.03,device='X3'))
        self.assertTrue(r.json()['pending'])
        self.assertEqual(404,self.client.get('/syncs/progress/doc',headers=self.ko).status_code)
        self.ready()
        r=self.api('POST','/api/documents/doc/bind',json={'key':KEY})
        self.assertTrue(r.json()['accepted'],r.text);self.assertEqual(3,r.json()['pid'])
        result=self.client.get('/syncs/progress/doc',headers=self.ko).json()
        self.assertEqual(XPATH,result['progress']);self.assertEqual(.03,result['percentage'])
        self.assertEqual(result,self.client.get('/syncs/progress/doc',headers=self.ko).json())

    def test_forward_only_retries_and_asset_version(self):
        self.ready()
        route=f'/api/books/{KEY}/position'
        first=self.api('PUT',route,json={'pid':40,'asset_version':1}).json()
        self.assertTrue(first['accepted'])
        for pid,reason in ((40,'unchanged'),(3,'behind')):
            r=self.api('PUT',route,json={'pid':pid,'asset_version':1}).json()
            self.assertFalse(r['accepted']);self.assertEqual(reason,r['reason']);self.assertEqual(first['revision'],r['revision']);self.assertEqual(first['updated_at'],r['updated_at'])
        self.assertEqual(409,self.api('PUT',route,json={'pid':60,'asset_version':0}).status_code)
        self.assertEqual(400,self.api('PUT',route,json={'pid':-1,'asset_version':1}).status_code)
        self.assertEqual(400,self.api('PUT',route,json={'pid':True,'asset_version':1}).status_code)
        self.api('POST','/api/documents/doc/bind',json={'key':KEY})
        r=self.client.put('/syncs/progress',headers=self.ko,json=dict(document='doc',progress=XPATH,percentage=.03))
        self.assertEqual('behind',r.json()['reason']);self.assertEqual(40,self.store.book(KEY)['pid'])
        self.assertEqual(409,self.api('POST',f'/api/books/{KEY}/applied',json={'pid':3,'revision':1}).status_code)
        self.assertEqual(200,self.api('POST',f'/api/books/{KEY}/applied',json={'pid':40,'revision':first['revision'],'device_id':'kindle'}).status_code)

    def test_reset_accepts_earlier_position_and_rejects_stale_kindle(self):
        self.ready()
        route=f'/api/books/{KEY}/position'
        self.api('POST','/api/documents/doc/bind',json={'key':KEY})
        self.client.put('/syncs/progress',headers=self.ko,json=dict(document='doc',progress=XPATH,percentage=.03))
        self.assertTrue(self.api('PUT',route,json={'pid':40,'asset_version':1}).json()['accepted'])
        self.api('POST',f'/api/books/{KEY}/applied',json={'pid':40,'revision':self.store.book(KEY)['revision'],'device_id':'kindle'})
        self.assertEqual(403,self.client.post(f'/api/books/{KEY}/reset',auth=('reader','password')).status_code)
        r=self.client.post(f'/api/books/{KEY}/reset',auth=('reader','password'),headers={'X-Reader-Sync':'1'}).json()
        self.assertIsNone(r['pid']);self.assertEqual(1,r['epoch']);self.assertEqual('reset',r['source'])
        self.assertEqual(404,self.client.get('/syncs/progress/doc',headers=self.ko).status_code)
        self.assertEqual([],self.store.library()['applied'])
        # Old X3 progress must not come back when artifacts are refreshed.
        self.store.refresh(KEY);self.assertIsNone(self.store.book(KEY)['pid'])
        # Kindle writes from before the reset (explicit or missing epoch) are rejected.
        for payload in ({'pid':40,'asset_version':1},{'pid':40,'asset_version':1,'epoch':0}):
            r=self.api('PUT',route,json=payload).json()
            self.assertFalse(r['accepted']);self.assertEqual('reset',r['reason'])
        self.assertIsNone(self.store.book(KEY)['pid'])
        self.assertEqual(400,self.api('PUT',route,json={'pid':3,'asset_version':1,'epoch':'1'}).status_code)
        r=self.api('PUT',route,json={'pid':3,'asset_version':1,'epoch':1}).json()
        self.assertTrue(r['accepted']);self.assertEqual(3,self.store.book(KEY)['pid'])
        # Forward-only resumes within the new epoch.
        self.assertEqual('behind',self.api('PUT',route,json={'pid':1,'asset_version':1,'epoch':1}).json()['reason'])
        self.assertEqual(1,Store(self.temp.name).book(KEY)['epoch'])

    def test_data_directory_can_be_moved(self):
        import shutil
        self.ready()
        self.store.update_position(KEY,40,'kindle',1)
        with tempfile.TemporaryDirectory() as target:
            dest=Path(target)/'moved'
            shutil.copytree(self.store.root,dest)
            moved=Store(dest)
            self.assertTrue(moved.book(KEY)['map_path'].startswith(str(dest)))
            self.assertTrue(Path(moved.book(KEY)['epub_path']).is_file())
            self.assertEqual(40,moved.book(KEY)['pid'])
            self.assertEqual('mapped',moved.translate_pid(moved.book(KEY),40)['bridge_status'])

    def test_concurrent_positions_keep_the_furthest(self):
        from concurrent.futures import ThreadPoolExecutor
        self.ready()
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(lambda pid:self.store.update_position(KEY,pid,'kindle',1),[20,80,3,60,40]))
        self.assertEqual(80,self.store.book(KEY)['pid'])

    def test_unmatched_epub_never_falls_back_to_percentage(self):
        # Only an EPUB attached before the map existed can still be another text: nothing could compare them then.
        r=self.api('PUT',f'/api/books/{KEY}/epub',content=epub_bytes(OTHER));self.assertEqual(200,r.status_code,r.text)
        self.assertTrue(run_next(self.store));self.map_ready()
        r=self.api('PUT',f'/api/books/{KEY}/position',json={'pid':3,'asset_version':1})
        self.assertEqual('unresolved',r.json()['bridge_status'])
        self.api('POST','/api/documents/doc/bind',json={'key':KEY})
        self.assertEqual(404,self.client.get('/syncs/progress/doc',headers=self.ko).status_code)

    def book(self,same,count=64):
        """A Kindle map of `count` paragraphs and an EPUB that shares the first `same` of them."""
        ours=lambda i:f'Paragraph {i} tells of {i*7} ships that left harbour {i*13} days before the storm.'
        theirs=lambda i:f'Absatz {i} erzählt von {i*5} Wagen, die im Jahr {1000+i} nach Norden zogen.'
        chunks,pid=[],0
        for i in range(count):
            chunks.append(dict(pid=pid,eid=10+i,offset=0,length=len(ours(i)),text=ours(i)));pid+=len(ours(i))
        self.map_ready(PositionMap(dict(version=1,book=dict(key=KEY),total_positions=pid,chunks=chunks)))
        return epub_bytes('</p><p>'.join((ours if i<same else theirs)(i) for i in range(count)))

    def test_epub_of_another_text_is_refused_and_the_previous_one_stays(self):
        route=f'/api/books/{KEY}/epub'
        r=self.api('PUT',route,content=self.book(64));self.assertEqual(200,r.status_code,r.text)
        self.assertTrue(run_next(self.store));before=self.store.book(KEY)
        self.assertTrue(self.api('PUT',f'/api/books/{KEY}/position',json={'pid':90,'asset_version':1}).json()['accepted'])
        r=self.api('PUT',route,content=epub_bytes(OTHER))
        self.assertEqual((400,'EPUB не совпадает с книгой Kindle: совпало 0 из 32 проверенных мест'),(r.status_code,r.json()['error']))
        # A quarter of the book is not the book either; the answer says how much of it was found.
        quarter=self.book(16);r=self.api('PUT',route,content=quarter)
        self.assertEqual(400,r.status_code,r.text);self.assertRegex(r.json()['error'],r'совпало [5-9] из 32 проверенных мест')
        after=self.store.book(KEY)
        self.assertEqual((before['epub_path'],'ready',None),(after['epub_path'],after['epub_status'],after['epub_error']))
        self.assertEqual(('mapped',90),(after['bridge_status'],after['pid']))
        self.assertEqual([Path(before['epub_path']).parent.name],[p.name for p in (self.store.root/'books'/KEY/'epub').iterdir()])
        self.assertEqual([],list((self.store.root/'tmp').iterdir()))
        self.assertEqual(['done'],[j['status'] for j in self.store.library()['jobs']])
        # Another edition with most of the text in common still translates most positions: it is accepted.
        r=self.api('PUT',route,content=self.book(48));self.assertEqual(200,r.status_code,r.text)
        self.assertTrue(run_next(self.store));self.assertNotEqual(before['epub_path'],self.store.book(KEY)['epub_path'])

    def test_check_parses_the_epub_once_and_leaves_its_bridge_to_the_job(self):
        from app.engine.epub_xpath import EpubIndex,validate_epub
        from app.engine.position_bridge import PositionBridge
        self.map_ready();self.store.update_position(KEY,5,'kindle',1)
        with patch('app.store.validate_epub',wraps=validate_epub) as parsed,patch('app.store.PositionBridge',wraps=PositionBridge) as bridged:
            self.assertEqual(200,self.api('PUT',f'/api/books/{KEY}/epub',content=epub_bytes()).status_code)
            self.assertEqual((1,1),(parsed.call_count,bridged.call_count))
            self.assertIsInstance(bridged.call_args.args[0],EpubIndex)
            # The job that follows in this process, its refresh and every later translation work with the same two.
            self.assertTrue(run_next(self.store))
            self.assertEqual(('ready','mapped',XPATH[:-1]+'5'),tuple(self.store.book(KEY)[f] for f in ('epub_status','bridge_status','xpath')))
            self.assertEqual('mapped',self.api('PUT',f'/api/books/{KEY}/position',json={'pid':40,'asset_version':1}).json()['bridge_status'])
            self.assertEqual((1,1),(parsed.call_count,bridged.call_count))

    def test_epub_built_from_the_kfx_and_too_little_text_are_not_compared(self):
        source=self.store.root/'book.kfx';source.write_bytes(b'KFX')
        with self.store.connect(write=True) as db:
            db.execute('UPDATE kindle_books SET kfx_path=? WHERE key=?',(str(source),KEY))
        self.map_ready()
        self.store.request_epub(KEY)
        # Whatever the converter wrote is this book: the check is for files the owner picks.
        with patch('app.worker.build_epub',return_value=epub_bytes(OTHER)):
            while run_next(self.store):pass
        self.assertEqual(('ready',None),(self.store.book(KEY)['epub_status'],self.store.book(KEY)['epub_error']))
        # A map with nothing long enough to quote cannot tell one EPUB from another.
        self.map_ready(PositionMap(dict(version=1,book=dict(key=KEY),total_positions=4,chunks=[dict(pid=0,eid=1,offset=0,length=4,text='Fin.')])))
        self.assertEqual(200,self.api('PUT',f'/api/books/{KEY}/epub',content=epub_bytes(TEXT)).status_code)

    def test_start_pid_is_where_the_text_begins(self):
        route=f'/api/books/{KEY}/position'
        self.assertIsNone(self.api('GET',route).json()['start_pid'])  # no map yet
        chunks=[dict(pid=0,eid=1,offset=0,length=1,text=None),dict(pid=1,eid=2,offset=0,length=2,text=' \n'),
                dict(pid=3,eid=3,offset=0,length=len(TEXT),text=TEXT)]  # a cover and an empty line before the text
        self.map_ready(PositionMap(dict(version=1,book=dict(key=KEY),total_positions=3+len(TEXT),chunks=chunks)))
        self.assertEqual(3,self.api('GET',route).json()['start_pid'])
        self.assertEqual(200,self.api('PUT',f'/api/books/{KEY}/epub',content=epub_bytes()).status_code);self.assertTrue(run_next(self.store))
        moved=self.api('PUT',route,json={'pid':43,'asset_version':1}).json()
        self.assertTrue(moved['accepted']);self.assertNotIn('start_pid',moved)
        self.assertEqual((43,3),tuple(self.api('GET',route).json()[f] for f in ('pid','start_pid')))
        for answer in (self.api('POST',f'/api/books/{KEY}/reset').json(),self.api('GET',route).json()):
            self.assertEqual((None,1,3),tuple(answer[f] for f in ('pid','epoch','start_pid')))
        with self.store.connect(write=True) as db:
            db.execute("UPDATE kindle_books SET map_status='queued' WHERE key=?",(KEY,))
        self.assertIsNone(self.api('GET',route).json()['start_pid'])  # the map is being rebuilt

    def test_pending_document_processed_when_epub_arrives(self):
        self.api('POST','/api/documents/doc/bind',json={'key':KEY})
        self.client.put('/syncs/progress',headers=self.ko,json=dict(document='doc',progress=XPATH,percentage=.03))
        self.ready()
        self.assertEqual(3,self.store.book(KEY)['pid'])

    def test_inventory_marks_books_deleted_on_kindle(self):
        mine={'key':KEY,'title':'Тестовая книга','fingerprint':self.fp};other={'key':'OTHERBOOK','title':'Другая','fingerprint':self.fp}
        inventory=lambda books:self.assertEqual(200,self.api('POST','/api/kindle/inventory',json={'device_id':'kindle','books':books}).status_code)
        inventory([mine,other]);self.assertEqual(1,self.store.book('OTHERBOOK')['present'])
        inventory([mine]);self.assertEqual(0,self.store.book('OTHERBOOK')['present']);self.assertEqual(1,self.store.book(KEY)['present'])
        # An empty scan must not mark the whole library as deleted.
        inventory([]);self.assertEqual(1,self.store.book(KEY)['present'])
        inventory([mine,other]);self.assertEqual(1,self.store.book('OTHERBOOK')['present'])

    def test_metadata_requires_kfx(self):
        self.assertEqual(409,self.api('GET',f'/api/books/{KEY}/meta').status_code)
        self.assertEqual(409,self.api('GET',f'/api/books/{KEY}/cover').status_code)


@unittest.skipUnless(os.environ.get('SYNC_FIXTURES'),'Set SYNC_FIXTURES to local test books directory')
class RealBookTests(unittest.TestCase):
    def test_kfx_ingest_map_epub_kosync(self):
        fixtures=Path(os.environ['SYNC_FIXTURES']);kfx=next(fixtures.glob('endi*.kfx'));epub=next(fixtures.glob('endi*.epub'))
        key='XNZDUFSVLCHEUKLM374WPKP7762ZJ6J4';fp=hashlib.sha256(kfx.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory() as d,TestClient(create_app(d,TOKEN,'reader','password')) as client:
            auth={'Authorization':'Bearer '+TOKEN};ko={'x-auth-user':'reader','x-auth-key':hashlib.md5(b'password').hexdigest()}
            r=client.post('/api/kindle/inventory',headers=auth,json={'device_id':'kindle','books':[{'key':key,'fingerprint':fp,'title':'Проект Аве Мария'}]});self.assertEqual(200,r.status_code)
            r=client.put(f'/api/books/{key}/bundle',headers={**auth,'X-Source-Fingerprint':fp},content=archive(kfx.name,kfx.read_bytes()));self.assertEqual(200,r.status_code,r.text)
            store=client.app.state.store;run_next(store);self.assertEqual('ready',store.book(key)['map_status'])
            meta=client.get(f'/api/books/{key}/meta',headers=auth).json();self.assertEqual(['Энди Уир'],meta['authors'],meta)
            if meta['has_cover']:self.assertTrue(client.get(f'/api/books/{key}/cover',headers=auth).headers['content-type'].startswith('image/'))
            self.assertIn('KINDLE-POS-MAP',client.get(f'/api/books/{key}/map',headers=auth).text)
            client.put(f'/api/books/{key}/epub',headers=auth,content=epub.read_bytes());run_next(store)
            client.post('/api/documents/x3/bind',headers=auth,json={'key':key})
            xpath='/body/DocFragment[6]/body/span[1]/p[23]/text()[1].3'
            r=client.put('/syncs/progress',headers=ko,json=dict(document='x3',progress=xpath,percentage=.002))
            self.assertEqual(1922,r.json()['pid'],r.text)
            r=client.put(f'/api/books/{key}/position',headers=auth,json={'pid':228605,'asset_version':store.book(key)['asset_version']})
            self.assertEqual('mapped',r.json()['bridge_status'],r.text)
            remote=client.get('/syncs/progress/x3',headers=ko).json()
            self.assertEqual(228605,store.bridge(store.book(key)['epub_path'],store.book(key)['map_path']).to_kindle(remote['progress'])['matches'][0]['pid'])

class KfxEpubTests(unittest.TestCase):
    OLD='endi_uir-proekt_ave_mariya-66216d3327f0a'

    def book(self,creator='Энди Уир'):
        buf=io.BytesIO()
        with zipfile.ZipFile(buf,'w') as z:
            z.writestr('mimetype','application/epub+zip',compress_type=zipfile.ZIP_STORED)
            z.writestr('META-INF/container.xml','<container><rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>',compress_type=zipfile.ZIP_DEFLATED)
            meta=f'<dc:title>{self.OLD}</dc:title>'+(f'<dc:creator>{creator}</dc:creator>' if creator is not None else '')
            z.writestr('OEBPS/content.opf',f'<package><metadata>{meta}<dc:language>ru</dc:language></metadata></package>',compress_type=zipfile.ZIP_DEFLATED)
            z.writestr('OEBPS/toc.ncx',f'<ncx><docTitle><text>{self.OLD}</text></docTitle></ncx>',compress_type=zipfile.ZIP_DEFLATED)
            z.writestr('OEBPS/nav.xhtml',f'<html><head><title>{self.OLD}</title></head></html>',compress_type=zipfile.ZIP_DEFLATED)
            z.writestr('OEBPS/c1.xhtml','<html><head><title>Глава 1</title></head><body><p>Текст</p></body></html>',compress_type=zipfile.ZIP_DEFLATED)
        return buf.getvalue()

    def test_title_replaced_everywhere_author_only_instead_of_filename(self):
        from app.engine.kfx_epub import set_metadata
        out=zipfile.ZipFile(io.BytesIO(set_metadata(self.book(),'Проект «Аве Мария» & Co','Andy Weir')))
        first=out.infolist()[0]
        self.assertEqual((first.filename,first.compress_type),('mimetype',zipfile.ZIP_STORED))
        opf=out.read('OEBPS/content.opf').decode()
        self.assertIn('<dc:title>Проект «Аве Мария» &amp; Co</dc:title>',opf)
        self.assertIn('<dc:creator>Энди Уир</dc:creator>',opf)  # у Kindle автор верный — не трогаем
        self.assertIn('<text>Проект «Аве Мария» &amp; Co</text>',out.read('OEBPS/toc.ncx').decode())
        self.assertIn('<title>Проект «Аве Мария» &amp; Co</title>',out.read('OEBPS/nav.xhtml').decode())
        self.assertIn('<title>Глава 1</title>',out.read('OEBPS/c1.xhtml').decode())
        self.assertEqual(out.read('mimetype'),b'application/epub+zip')

    def test_author_filled_when_missing_or_filename(self):
        from app.engine.kfx_epub import set_metadata
        for creator in ('endi_uir','',None):
            opf=zipfile.ZipFile(io.BytesIO(set_metadata(self.book(creator),None,'Энди Уир'))).read('OEBPS/content.opf').decode()
            self.assertEqual(opf.count('<dc:creator>Энди Уир</dc:creator>'),1,creator)
            self.assertIn(self.OLD,opf)  # без названия — название не трогаем

    def test_generate_epub_job_queue_and_errors(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        app=create_app(temp.name,TOKEN,'reader','password');store=app.state.store
        client=TestClient(app);self.addCleanup(client.close)
        auth={'Authorization':'Bearer '+TOKEN};fp=hashlib.sha256(b'book').hexdigest()
        client.post('/api/kindle/inventory',headers=auth,json={'device_id':'kindle','books':[{'key':KEY,'title':'t','fingerprint':fp}]})
        body={'title':'Проект «Аве Мария»','author':'Энди Уир'}
        self.assertEqual(409,client.post(f'/api/books/{KEY}/generate-epub',headers=auth,json=body).status_code)  # KFX ещё нет
        client.put(f'/api/books/{KEY}/bundle',headers={**auth,'X-Source-Fingerprint':fp},content=archive())
        r=client.post(f'/api/books/{KEY}/generate-epub',headers=auth,json=body)
        self.assertEqual((200,'generating'),(r.status_code,r.json()['status']))
        self.assertEqual('generating',store.book(KEY)['epub_status'])
        seen=[]
        def fake_build(path,title,author):
            seen.append((Path(path).suffix,title,author));return epub_bytes()
        with patch('app.worker.build_epub',side_effect=fake_build):
            while run_next(store):pass
        b=store.book(KEY)
        self.assertEqual(seen,[('.kfx','Проект «Аве Мария»','Энди Уир')])
        self.assertEqual(('ready',True),(b['epub_status'],bool(b['epub_path'])))
        self.assertEqual(409,client.post(f'/api/books/{KEY}/generate-epub',headers=auth,json=body).status_code)  # EPUB уже есть
        with store.connect(write=True) as db:
            db.execute("UPDATE kindle_books SET epub_path=NULL,epub_status='missing' WHERE key=?",(KEY,))
        client.post(f'/api/books/{KEY}/generate-epub',headers=auth,json=body)
        with patch('app.worker.build_epub',side_effect=ValueError('Книга защищена DRM — EPUB из неё не собрать')):
            while run_next(store):pass
        b=store.book(KEY)
        self.assertEqual('error',b['epub_status']);self.assertIn('DRM',b['epub_error']);self.assertIsNone(b['epub_path'])

    def test_forget_unbound_document_only(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        app=create_app(temp.name,TOKEN,'reader','password');client=TestClient(app);self.addCleanup(client.close)
        auth={'Authorization':'Bearer '+TOKEN};ko={'x-auth-user':'reader','x-auth-key':hashlib.md5(b'password').hexdigest()}
        client.post('/api/kindle/inventory',headers=auth,json={'device_id':'kindle','books':[{'key':KEY,'title':'t','fingerprint':hashlib.sha256(b'b').hexdigest()}]})
        client.put('/syncs/progress',headers=ko,json=dict(document='test-doc',progress=XPATH,percentage=.01,device='X3'))
        self.assertEqual(200,client.delete('/api/documents/test-doc',headers=auth).status_code)
        self.assertEqual(404,client.delete('/api/documents/test-doc',headers=auth).status_code)
        client.post('/api/documents/bound/bind',headers=auth,json={'key':KEY})
        self.assertEqual(409,client.delete('/api/documents/bound',headers=auth).status_code)

def manga_epub(pages):
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w') as z:
        z.writestr('META-INF/container.xml','<container><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>')
        items=''.join(f'<item id="p{i}" href="p{i}.xhtml"/>' for i in range(pages))
        refs=''.join(f'<itemref idref="p{i}"/>' for i in range(pages))
        z.writestr('book.opf',f'<package><manifest>{items}</manifest><spine>{refs}</spine></package>')
        for i in range(pages):
            z.writestr(f'p{i}.xhtml',f'<html><body><div><img src="i{i}.jpg" alt=""/></div></body></html>')
    return buf.getvalue()

def manga_map(pages,per=2,key=KEY):
    chunks=[dict(pid=k*per+j,eid=100+k*per+j,offset=0,length=1,section=f'c{k}') for k in range(pages) for j in range(per)]
    return PositionMap(dict(version=1,book=dict(key=key),total_positions=pages*per,chunks=chunks))

class PageModeTests(unittest.TestCase):
    """Манга: страницы-картинки без текста — позиции сопоставляются по номеру страницы."""

    def bridge(self,epub_pages,map_pages):
        from app.engine.position_bridge import PositionBridge
        from app.engine.epub_xpath import EpubIndex
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        path=Path(temp.name)/'m.epub';path.write_bytes(manga_epub(epub_pages))
        return PositionBridge(EpubIndex(path),manga_map(map_pages))

    def test_pages_map_both_ways(self):
        b=self.bridge(76,76)
        r=b.to_epub(23)  # PID 23 — раздел 11, т.е. 12-я страница
        self.assertEqual((r['status'],r['matches'][0]['xpath']),('mapped','/body/DocFragment[12]/body'))
        k=b.to_kindle('/body/DocFragment[7]/body')  # так X3 шлёт позицию на странице без текста
        self.assertEqual((k['status'],k['matches'][0]['pid']),('mapped',12))
        self.assertEqual(b.to_kindle('/body/DocFragment[7]/body/div/img')['matches'][0]['pid'],12)
        self.assertEqual(b.to_kindle('/body/DocFragment[77]/body')['status'],'unresolved')
        for pid in (0,1,150,151):
            back=b.to_kindle(b.to_epub(pid)['matches'][0]['xpath'])['matches'][0]['pid']
            self.assertEqual(back,pid-pid%2)  # в пределах страницы — её первая позиция

    def test_no_page_mode_when_counts_differ_or_text_exists(self):
        b=self.bridge(75,76)
        self.assertIsNone(b.pages);self.assertEqual(b.to_epub(23)['status'],'unresolved')
        from app.engine.position_bridge import PositionBridge
        from app.engine.epub_xpath import EpubIndex
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        path=Path(temp.name)/'t.epub';path.write_bytes(epub_bytes())
        text_map=PositionMap(dict(version=1,book=dict(key=KEY),total_positions=len(TEXT),chunks=[dict(pid=0,eid=1,offset=0,length=len(TEXT),text=TEXT,section='c0')]))
        self.assertIsNone(PositionBridge(EpubIndex(path),text_map).pages)

    def test_x3_and_kindle_positions_through_server(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        app=create_app(temp.name,TOKEN,'reader','password');store=app.state.store
        client=TestClient(app);self.addCleanup(client.close)
        auth={'Authorization':'Bearer '+TOKEN};ko={'x-auth-user':'reader','x-auth-key':hashlib.md5(b'password').hexdigest()}
        client.post('/api/kindle/inventory',headers=auth,json={'device_id':'kindle','books':[{'key':KEY,'title':'манга','fingerprint':hashlib.sha256(b'm').hexdigest()}]})
        folder=store.root/'maps';host=folder/'m.json';compact=folder/'m.posmap'
        write_map(manga_map(76),host,compact)
        with store.connect(write=True) as db:
            db.execute("UPDATE kindle_books SET map_path=?,compact_path=?,map_status='ready',asset_version=1 WHERE key=?",(str(host),str(compact),KEY))
        # Без текста сверять нечего, кроме страниц: их должно быть столько же, и текста в EPUB быть не должно.
        for data,reason in ((manga_epub(75),'страниц в EPUB 75, в книге Kindle 76'),(epub_bytes(),'в книге Kindle нет текста, а в EPUB есть')):
            r=client.put(f'/api/books/{KEY}/epub',headers=auth,content=data)
            self.assertEqual((400,'EPUB не совпадает с книгой Kindle: '+reason),(r.status_code,r.json()['error']))
        self.assertIsNone(store.book(KEY)['epub_path'])
        client.put(f'/api/books/{KEY}/epub',headers=auth,content=manga_epub(76))
        self.assertTrue(run_next(store));self.assertEqual('ready',store.book(KEY)['epub_status'])
        self.assertEqual(0,client.get(f'/api/books/{KEY}/position',headers=auth).json()['start_pid'])  # книга без текста начинается с первой позиции
        client.post('/api/documents/doc/bind',headers=auth,json={'key':KEY})
        r=client.put('/syncs/progress',headers=ko,json=dict(document='doc',progress='/body/DocFragment[7]/body',percentage=.0826,device='X3'))
        self.assertEqual(200,r.status_code,r.text)
        self.assertEqual((12,'x3'),(store.book(KEY)['pid'],store.book(KEY)['source']))
        r=client.put(f'/api/books/{KEY}/position',headers=auth,json={'pid':23,'asset_version':1})
        self.assertTrue(r.json()['accepted'])
        remote=client.get('/syncs/progress/doc',headers=ko).json()
        self.assertEqual(remote['progress'],'/body/DocFragment[12]/body')
        self.assertAlmostEqual(remote['percentage'],11/76,places=4)
        r=client.put('/syncs/progress',headers=ko,json=dict(document='doc',progress='/body/DocFragment[5]/body',percentage=.05,device='X3'))
        self.assertEqual(23,store.book(KEY)['pid'])  # только вперёд: X3 позади Kindle — позиция не откатилась

if __name__=='__main__':unittest.main()
