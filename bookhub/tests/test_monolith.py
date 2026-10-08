import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from fastapi.testclient import TestClient

TEXT = 'A unique paragraph about sailing across the northern ocean and watching distant mountains in the morning.'
XPATH = '/body/DocFragment[1]/body/p[1]/text()[1].'

def epub(text=TEXT,title='My book'):
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w') as z:
        z.writestr(zipfile.ZipInfo('META-INF/container.xml'),'<container><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>')
        z.writestr(zipfile.ZipInfo('book.opf'),f'<package xmlns:dc="http://purl.org/dc/elements/1.1/"><metadata><dc:title>{title}</dc:title><dc:creator>Author</dc:creator></metadata><manifest><item id="ch" href="ch.xhtml"/></manifest><spine><itemref idref="ch"/></spine></package>')
        z.writestr(zipfile.ZipInfo('ch.xhtml'),'<html><body><p>'+text+'</p></body></html>')
    return buf.getvalue()


class MonolithTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.env=patch.dict(os.environ,BOOKHUB_DATA=cls.temp.name,BOOKHUB_DB=str(Path(cls.temp.name)/'bookhub.sqlite3'),AI_URL='',AI_MODEL='',SYNC_API_KEY='token',SYNC_USER='reader',SYNC_PASSWORD='password')
        cls.env.start()
        from bookhub.application import create_web_app,create_sync_app
        from bookhub import shelf
        from bookhub.sync.service import get_store
        cls.shelf=shelf
        cls.web=TestClient(create_web_app())
        cls.sync=TestClient(create_sync_app())
        cls.store=get_store()
        cls.ko={'x-auth-user':'reader','x-auth-key':hashlib.md5(b'password').hexdigest()}

    @classmethod
    def tearDownClass(cls):
        cls.web.close();cls.sync.close();cls.shelf.engine.dispose();cls.env.stop();cls.temp.cleanup()

    def setUp(self):
        self.network=patch('requests.sessions.Session.request',side_effect=AssertionError('Unexpected external request'))
        self.network.start();self.addCleanup(self.network.stop)
        with self.store.connect() as db:
            db.execute('PRAGMA foreign_keys=OFF')
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
                db.execute('DELETE FROM "'+row['name']+'"')
        self.shelf.AI_ENABLED=False

    def add(self, data=None, filename='book.epub'):
        r=self.web.post('/api/epubs',params={'filename':filename},content=data or epub())
        self.assertEqual(200,r.status_code,r.text)
        return r.json()

    def push(self,doc,offset,device='one'):
        return self.sync.put('/syncs/progress',headers=self.ko,json=dict(document=doc,progress=XPATH+str(offset),percentage=offset/100,device='KOReader',device_id=device))

    def test_epub_creates_complete_card_without_any_kindle(self):
        b=self.add()
        card=self.web.get('/api/books/'+str(b['book_id'])).json()
        self.assertEqual(('My book','Author','idle',''),(card['title'],card['authors'],card['summary_status'],card['summary']))
        self.assertEqual(b['sha256'],card['epubs'][0]['sha256'])
        self.assertEqual([],self.store.library()['books'])
        self.assertEqual(epub(),self.web.get(f"/api/books/{b['book_id']}/file/epub").content)

    def test_two_koreaders_share_epub_position_without_kfx(self):
        b=self.add();doc=hashlib.md5(b'book.epub').hexdigest()
        self.assertTrue(self.push(doc,20,'one').json()['accepted'])
        self.assertEqual(XPATH+'20',self.sync.get('/syncs/progress/'+doc,headers=self.ko).json()['progress'])
        self.assertTrue(self.push(doc,40,'two').json()['accepted'])
        self.assertFalse(self.push(doc,10,'one').json()['accepted'])
        self.assertEqual(XPATH+'40',self.web.get('/syncs/progress/'+doc,headers=self.ko).json()['progress'])
        self.assertEqual(2,len(self.web.get('/api/devices').json()['readers']))

    def test_book_delivery_badges_keep_history_beyond_activity_feed(self):
        b=self.add();bid=b['book_id']
        with self.store.connect(write=True) as db:
            for n in range(35):
                db.execute('INSERT INTO deliveries VALUES(?,?,?,?,?)',
                           (bid,b['sha256'],'x3',f'version-{n}.epub',1000+n))
        card=self.web.get(f'/api/books/{bid}').json()
        self.assertEqual(35,len(card['deliveries']))
        self.assertEqual([],card['reader'])
        self.assertEqual(35,len(self.web.get('/api/books').json()[0]['deliveries']))
        self.assertEqual(30,len(self.shelf.x3_status()['history']))

    def test_legacy_sent_files_are_evidence_but_failed_and_queued_files_are_not(self):
        b=self.add();bid=b['book_id']
        with self.shelf.db_session() as db:
            for key,status in [('OLD','sent'),('WAIT','queued'),('BAD','error')]:
                db.add(self.shelf.ReaderLink(key=key,book_id=bid))
                db.add(self.shelf.X3Upload(key=key,filename=key+'.epub',status=status))
            db.commit()
        data=self.web.get(f'/api/books/{bid}').json()['deliveries']
        self.assertEqual(['OLD.epub'],[d['filename'] for d in data])
        self.assertIsNone(data[0]['sha256'])
        with self.store.connect(write=True) as db:
            db.execute('INSERT INTO deliveries VALUES(?,?,?,?,?)',(bid,b['sha256'],'x3','OLD.epub',1000))
        data=self.web.get(f'/api/books/{bid}').json()['deliveries']
        self.assertEqual(1,len(data));self.assertEqual(b['sha256'],data[0]['sha256'])

    def test_koreader_implicit_first_indices_resolve(self):
        b=self.add();doc=hashlib.md5(b'book.epub').hexdigest()
        r=self.sync.put('/syncs/progress',headers=self.ko,json=dict(document=doc,progress='/body/DocFragment/body/p/text().20',percentage=.2,device='KOReader',device_id='a'))
        self.assertTrue(r.json()['accepted'],r.text)

    def test_unknown_document_can_bind_directly_to_epub_card(self):
        b=self.add()
        self.assertTrue(self.push('unknown',25).json()['pending'])
        r=self.web.post('/api/reader/documents/unknown/bind',json={'book_id':b['book_id']})
        self.assertEqual(200,r.status_code,r.text)
        self.assertTrue(r.json()['accepted'])
        self.assertEqual(XPATH+'25',self.sync.get('/syncs/progress/unknown',headers=self.ko).json()['progress'])

    def test_invalid_epub_does_not_create_card(self):
        r=self.web.post('/api/epubs?filename=bad.epub',content=b'PKnot a zip')
        self.assertEqual(400,r.status_code)
        self.assertEqual([],self.web.get('/api/books').json())

    def test_same_filename_does_not_rebind_existing_document(self):
        a=self.add();b=self.add(epub(TEXT+' Another ending.',title='Other'))
        self.assertNotEqual(a['book_id'],b['book_id'])
        doc=hashlib.md5(b'book.epub').hexdigest()
        self.push(doc,20)
        self.assertEqual(XPATH+'20',self.store.core.files(a['book_id'])[0]['xpath'])
        self.assertIsNone(self.store.core.files(b['book_id'])[0]['xpath'])

    def test_new_asset_preserves_existing_epub_and_position(self):
        a=self.add();self.push(hashlib.md5(b'book.epub').hexdigest(),20)
        r=self.web.put(f"/api/books/{a['book_id']}/epub?filename=new.epub",content=epub(TEXT+' More.'))
        self.assertEqual(200,r.status_code,r.text)
        files=self.store.core.files(a['book_id'])
        self.assertEqual(2,len(files));self.assertEqual(a['sha256'],files[0]['sha256'])
        self.assertEqual(XPATH+'20',files[0]['xpath'])

    def test_add_read_refresh_and_reconcile_never_enqueue_ai_or_x3(self):
        self.shelf.AI_ENABLED=True
        b=self.add()
        from bookhub.worker import reconcile
        with patch.object(self.shelf,'ai_chat') as ai,patch.object(self.shelf,'translate_missing_titles') as titles,patch.object(self.shelf,'x3_request') as x3:
            self.web.get('/api/books');self.web.get('/api/recommendations')
            reconcile(self.store)
            self.assertFalse(ai.called);self.assertFalse(titles.called);self.assertFalse(x3.called)
        with self.store.connect() as db:
            self.assertEqual(0,db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0])
            self.assertEqual(0,db.execute('SELECT COUNT(*) FROM x3_uploads').fetchone()[0])

    def test_manual_summary_is_durable_and_preserves_old_text_until_ready(self):
        self.shelf.AI_ENABLED=True;b=self.add();bid=b['book_id']
        self.web.patch(f'/api/books/{bid}',json={'summary':'My review'})
        r=self.web.post(f'/api/books/{bid}/regenerate-summary')
        self.assertEqual(200,r.status_code,r.text)
        self.assertEqual('My review',self.web.get(f'/api/books/{bid}').json()['summary'])
        from bookhub import tasks
        with patch.object(self.shelf,'ai_chat',return_value='New requested review'):
            self.assertTrue(tasks.run_next('ai',dict(summary=self.shelf.generate_summary_background),self.store))
        self.assertEqual('New requested review',self.web.get(f'/api/books/{bid}').json()['summary'])

    def test_description_translation_is_manual_durable_and_preserves_original(self):
        from bookhub import description, tasks
        from bookhub.worker import reconcile
        self.shelf.AI_ENABLED=True
        bid=self.add()['book_id'];url=f'/api/books/{bid}'
        original='From the back cover:\n\nA journey across the ocean.'
        self.web.patch(url,json={'description':original,'summary':'Existing review'})
        with patch.object(self.shelf,'ai_chat',return_value='С обложки:\n\nПутешествие через океан.') as ai:
            self.web.get(url);self.web.get('/api/books');reconcile(self.store)
            ai.assert_not_called()
            with self.store.connect() as db:
                self.assertEqual(0,db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0])
            # Simulate a process exit between committing the request and enqueueing it.
            with patch.object(tasks,'enqueue'):
                self.assertEqual(200,self.web.post(url+'/translate-description').status_code)
            self.assertEqual(409,self.web.post(url+'/translate-description').status_code)
            reconcile(self.store);reconcile(self.store)
            with self.store.connect() as db:
                self.assertEqual(1,db.execute("SELECT COUNT(*) FROM tasks WHERE kind='description'").fetchone()[0])
            self.assertEqual(original,self.web.get(url).json()['description'])
            self.assertTrue(tasks.run_next('ai',dict(description=description.translate_background),self.store))
            self.assertIn('A journey across the ocean.',ai.call_args.args[0])
            ai.assert_called_once()
        result=self.web.get(url).json()
        self.assertEqual((original,'С обложки:\n\nПутешествие через океан.','ready','Existing review'),
                         (result['description'],result['description_ru'],result['description_translation_status'],result['summary']))
        self.assertEqual({'status':'ready'},self.web.post(url+'/translate-description').json())
        self.assertFalse(tasks.run_next('ai',dict(description=description.translate_background),self.store))

    def test_description_translation_retries_empty_or_failed_results(self):
        from bookhub import description, tasks
        bid=self.add()['book_id'];url=f'/api/books/{bid}'
        self.assertEqual(400,self.web.post(url+'/translate-description').status_code)
        self.shelf.AI_ENABLED=True
        self.assertEqual(404,self.web.post('/api/books/999999/translate-description').status_code)
        self.assertEqual(400,self.web.post(url+'/translate-description').status_code)
        self.web.patch(url,json={'description':'An English description.'})
        for response in ['',self.shelf.AIError('offline'),'Перевод описания.']:
            self.assertEqual(200,self.web.post(url+'/translate-description').status_code)
            with patch.object(self.shelf,'ai_chat',side_effect=[response]):
                tasks.run_next('ai',dict(description=description.translate_background),self.store)
            result=self.web.get(url).json()
            self.assertEqual('An English description.',result['description'])
            self.assertEqual('ready' if response=='Перевод описания.' else 'error',result['description_translation_status'])
        self.assertEqual('Перевод описания.',result['description_ru'])

    def test_description_edit_invalidates_inflight_and_saved_translation(self):
        from bookhub import description, tasks
        self.shelf.AI_ENABLED=True
        bid=self.add()['book_id'];url=f'/api/books/{bid}'
        self.web.patch(url,json={'description':'Old description'})
        self.web.post(url+'/translate-description')
        def edit_during_request(*args):
            self.web.patch(url,json={'description':'New description'})
            return 'Устаревший перевод'
        with patch.object(self.shelf,'ai_chat',side_effect=edit_during_request):
            tasks.run_next('ai',dict(description=description.translate_background),self.store)
        result=self.web.get(url).json()
        self.assertEqual(('New description',None,'idle'),(result['description'],result['description_ru'],result['description_translation_status']))
        self.web.post(url+'/translate-description')
        with patch.object(self.shelf,'ai_chat',return_value='Новый перевод'):
            tasks.run_next('ai',dict(description=description.translate_background),self.store)
        self.web.patch(url,json={'title':'Corrected title','description':'New description'})
        self.assertEqual('Новый перевод',self.web.get(url).json()['description_ru'])
        self.web.patch(url,json={'description':''})
        self.assertIsNone(self.web.get(url).json()['description_ru'])

    def test_translation_cannot_overwrite_a_deleted_or_reused_book(self):
        from bookhub import description, tasks
        self.shelf.AI_ENABLED=True
        bid=self.add()['book_id'];url=f'/api/books/{bid}'
        self.web.patch(url,json={'description':'Original'})
        self.web.post(url+'/translate-description')
        def replace_during_request(*args):
            self.web.delete(url)
            with self.shelf.db_session() as db:
                db.add(self.shelf.Book(id=bid,source_id='replacement',title='Replacement',description='Original'))
                db.commit()
            return 'Старый перевод'
        with patch.object(self.shelf,'ai_chat',side_effect=replace_during_request):
            tasks.run_next('ai',dict(description=description.translate_background),self.store)
        self.assertIsNone(self.web.get(url).json()['description_ru'])

    def test_merge_preserves_translation_only_for_matching_original(self):
        from bookhub import description, tasks
        self.shelf.AI_ENABLED=True
        for target_description in ['', 'Different original']:
            with self.subTest(target_description=target_description):
                a=self.add(epub(title='Source '+target_description))['book_id']
                b=self.add(epub(title='Target '+target_description))['book_id']
                self.web.patch(f'/api/books/{a}',json={'description':'Original'})
                self.web.patch(f'/api/books/{b}',json={'description':target_description})
                self.web.post(f'/api/books/{a}/translate-description')
                with patch.object(self.shelf,'ai_chat',return_value='Перевод'):
                    tasks.run_next('ai',dict(description=description.translate_background),self.store)
                self.assertEqual(200,self.web.post(f'/api/books/{a}/merge',json={'target_id':b}).status_code)
                result=self.web.get(f'/api/books/{b}').json()
                self.assertEqual(None if target_description else 'Перевод',result['description_ru'])

    def test_reset_rejects_an_inflight_old_epub_position(self):
        b=self.add();core=self.store.core
        self.push(hashlib.md5(b'book.epub').hexdigest(),40)
        original=core.locate
        def during(asset,xpath):
            point=original(asset,xpath);core.reset(b['book_id']);return point
        with patch.object(core,'locate',side_effect=during):
            result=core.accept(b['book_id'],b['sha256'],XPATH+'50',.5,'reader')
        self.assertEqual('reset',result['reason'])
        self.assertIsNone(core.files(b['book_id'])[0]['xpath'])

    def test_common_hostname_keeps_sync_auth_separate(self):
        self.assertEqual(200,self.web.get('/api/books').status_code)
        self.assertEqual(401,self.web.get('/api/library').status_code)
        self.assertEqual(401,self.web.get('/users/auth').status_code)
        self.assertEqual(200,self.web.get('/users/auth',headers=self.ko).status_code)

    def test_generated_epub_is_saved_without_delivery(self):
        from bookhub.sync.jobs import run_next
        bid=self.add()['book_id'];key='KINDLEBOOK'
        self.store.inventory('kindle',[dict(key=key,fingerprint='a'*64)])
        source=self.store.root/'source.kfx';source.write_bytes(b'synthetic')
        with self.store.connect(write=True) as db:
            db.execute('UPDATE kindle_books SET kfx_path=? WHERE key=?',(str(source),key))
            db.execute('INSERT INTO reader_links(key,book_id) VALUES(?,?)',(key,bid))
        self.store.request_epub(key)
        with patch('bookhub.sync.jobs.build_epub',return_value=epub()),patch.object(self.shelf,'x3_request') as x3:
            self.assertTrue(run_next(self.store));self.assertTrue(run_next(self.store));x3.assert_not_called()
        self.assertEqual('ready',self.store.book(key)['epub_status'])
        self.assertTrue(self.store.core.files(bid))
        with self.store.connect() as db:self.assertEqual(0,db.execute('SELECT COUNT(*) FROM x3_uploads').fetchone()[0])

    def test_merge_and_delete_preserve_assets_and_document_ownership(self):
        a=self.add();b=self.add(epub(TEXT+' Other.',title='Other'),filename='other.epub')
        doc=hashlib.md5(b'book.epub').hexdigest();self.push(doc,20)
        r=self.web.post(f"/api/books/{a['book_id']}/merge",json={'target_id':b['book_id']})
        self.assertEqual(200,r.status_code,r.text)
        self.assertEqual(2,len(self.store.core.files(b['book_id'])))
        self.assertEqual(XPATH+'20',self.sync.get('/syncs/progress/'+doc,headers=self.ko).json()['progress'])
        self.assertEqual(200,self.web.delete(f"/api/books/{b['book_id']}").status_code)
        self.assertEqual(404,self.sync.get('/syncs/progress/'+doc,headers=self.ko).status_code)

    def test_koreader_content_digest_binds_without_a_kindle(self):
        data=epub();b=self.add(data)
        digest=self.store.core.koreader_digest(data)
        self.assertTrue(self.push(digest,30,'koreader').json()['accepted'])
        card=self.web.get(f"/api/books/{b['book_id']}").json()
        self.assertEqual('koreader',card['devices'][0]['device_id'])
        self.assertEqual(XPATH+'30',card['epubs'][0]['xpath'])

    def test_concurrent_document_pushes_keep_each_payload(self):
        self.add();doc=hashlib.md5(b'book.epub').hexdigest()
        process=self.store.process_document
        first=[True]
        def concurrent(document,snapshot=None):
            if first.pop() if first else False:
                self.store.save_document(doc,dict(progress=XPATH+'10',percentage=.1,device='KOReader',device_id='two'))
            return process(document,snapshot)
        with patch.object(self.store,'process_document',side_effect=concurrent):
            answer=self.store.save_document(doc,dict(progress=XPATH+'40',percentage=.4,device='KOReader',device_id='one'))
        self.assertTrue(answer['accepted'])
        self.assertEqual(XPATH+'40',self.store.core.progress(doc)['progress'])
        self.assertEqual(2,len(self.web.get('/api/devices').json()['readers']))

    def test_reset_increments_kindle_epoch_once_and_only_for_matching_epub(self):
        a=self.add();bid=a['book_id']
        b=self.store.core.ingest(epub(TEXT+' Another edition.'),'other.epub',bid)
        for key,asset in [('FIRST',a),('SECOND',b)]:
            self.store.inventory('kindle',[dict(key=key,fingerprint='a'*64)])
            path=self.store.core.asset(bid,asset['sha256'])['path']
            with self.store.connect(write=True) as db:
                db.execute("UPDATE kindle_books SET epub_path=?,epub_status='ready',pid=40,xpath=?,percentage=.4 WHERE key=?",(path,XPATH+'40',key))
                db.execute('INSERT INTO reader_links(key,book_id) VALUES(?,?)',(key,bid))
            self.store.core.attach_kindle(key)
        self.store.reset_position('FIRST')
        self.assertEqual((1,None),(self.store.book('FIRST')['epoch'],self.store.book('FIRST')['pid']))
        self.assertEqual((0,40),(self.store.book('SECOND')['epoch'],self.store.book('SECOND')['pid']))
        self.assertEqual(1,self.store.core.files(bid)[0]['epoch'])

    def test_generated_epub_cannot_replace_a_concurrent_user_upload(self):
        from bookhub.sync.jobs import run_next
        bid=self.add()['book_id'];key='RACING'
        self.store.inventory('kindle',[dict(key=key,fingerprint='a'*64)])
        source=self.store.root/'race.kfx';source.write_bytes(b'KFX')
        uploaded=self.store.root/'user.epub';uploaded.write_bytes(epub(TEXT+' User edition.'))
        with self.store.connect(write=True) as db:
            db.execute('UPDATE kindle_books SET kfx_path=? WHERE key=?',(str(source),key))
            db.execute('INSERT INTO reader_links(key,book_id) VALUES(?,?)',(key,bid))
        self.store.request_epub(key)
        def generate(*args):
            self.store.register_epub(key,uploaded)
            return epub(TEXT+' Generated edition.')
        with patch('bookhub.sync.jobs.build_epub',side_effect=generate):
            run_next(self.store);run_next(self.store)
        self.assertEqual(str(uploaded),self.store.book(key)['epub_path'])
        self.assertEqual('ready',self.store.book(key)['epub_status'])

    def test_offline_manual_delivery_is_not_queued(self):
        b=self.add()
        with patch.object(self.shelf,'X3_ENABLED',True),patch.object(self.shelf,'x3_check',return_value=False),patch.object(self.shelf,'x3_request') as upload:
            r=self.web.post(f"/api/books/{b['book_id']}/send-x3")
        self.assertEqual(503,r.status_code);upload.assert_not_called()
        with self.store.connect() as db:
            self.assertEqual(0,db.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0])
            self.assertEqual(0,db.execute('SELECT COUNT(*) FROM x3_uploads').fetchone()[0])

    def test_manual_delivery_versions_collision_retry_and_history(self):
        from bookhub.delivery import send_x3
        from bookhub.sync.store import Problem
        a=self.add();bid=a['book_id'];sent=[];existing={}
        def upload(request,base_url,directory,filename,source,timeout):
            sent.append((filename,Path(source).read_bytes()));existing[filename]=len(sent[-1][1])
            return 'ws'
        listing=lambda:[dict(name=name,size=size,epub=True) for name,size in existing.items()]
        with patch.object(self.shelf,'X3_ENABLED',True),patch.object(self.shelf,'x3_check',return_value=True),patch.object(self.shelf,'x3_fetch_listing',side_effect=listing),patch('bookhub.x3.upload',side_effect=upload),patch('bookhub.x3.read_progress',return_value=None):
            first=send_x3(bid)
            send_x3(bid)
            self.assertEqual(1,len(sent))
            b=self.store.core.ingest(epub(TEXT+' New edition.'),'book.epub',bid)
            newname='book-'+b['sha256'][:12]+'.epub';existing[newname]=1
            with self.assertRaises(Problem) as exc:send_x3(bid,b['sha256'])
            self.assertEqual(409,exc.exception.status)
            self.assertEqual(1,len(sent))
            del existing[newname]
            second=send_x3(bid,b['sha256'])
            self.assertNotEqual(first['filename'],second['filename'])
            self.assertEqual(2,len(self.shelf.x3_status()['history']))
        with self.store.connect() as db:
            rows=db.execute('SELECT document,epub_sha256 FROM documents WHERE document IN (?,?)',(hashlib.md5(first['filename'].encode()).hexdigest(),hashlib.md5(second['filename'].encode()).hexdigest())).fetchall()
        self.assertEqual({a['sha256'],b['sha256']},{r['epub_sha256'] for r in rows})

    def test_manual_recommendations_are_queued_only_by_post(self):
        self.shelf.AI_ENABLED=True;self.add()
        self.web.get('/api/recommendations')
        with self.store.connect() as db:self.assertEqual(0,db.execute("SELECT COUNT(*) FROM tasks WHERE lane='ai'").fetchone()[0])
        r=self.web.post('/api/recommendations/refresh')
        self.assertEqual(200,r.status_code,r.text)
        with self.store.connect() as db:
            self.assertEqual(('recommendations','queued'),tuple(db.execute("SELECT kind,status FROM tasks WHERE lane='ai'").fetchone()))

    def test_migration_snapshot_reads_wal_without_touching_source_sidecars(self):
        import sqlite3
        from bookhub.migrate import readonly
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'legacy.sqlite3'
            original=sqlite3.connect(path)
            original.execute('PRAGMA journal_mode=WAL')
            original.execute('PRAGMA wal_autocheckpoint=0')
            original.execute('CREATE TABLE items(value)')
            original.execute('INSERT INTO items VALUES(42)');original.commit()
            for close_first in (False,True):
                if close_first:original.close()
                before={p.name:p.read_bytes() for p in root.iterdir()}
                with readonly(path) as snapshot:
                    self.assertEqual(42,snapshot.execute('SELECT value FROM items').fetchone()[0])
                self.assertEqual(before,{p.name:p.read_bytes() for p in root.iterdir()})

    def test_migration_preserves_legacy_records_and_source_files(self):
        import sqlite3
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        from bookhub.sync.store import Store
        from bookhub.migrate import migrate
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'legacy';dest=root/'new'
            (source/'myshelf/static/covers').mkdir(parents=True)
            (source/'myshelf/incoming').mkdir()
            (source/'myshelf/static/covers/1.jpg').write_bytes(b'original cover')
            (source/'myshelf/incoming/old.epub').write_bytes(epub())
            engine=create_engine('sqlite:///'+str(source/'myshelf/library.db'))
            self.shelf.Base.metadata.create_all(engine)
            with Session(engine) as db:
                b=self.shelf.Book(id=1,source_id='/works/OLD',title='Manual title',authors='Manual author',status='reading',summary='Manual review',summary_status='ready',instance_id='old-instance')
                db.add(b);db.commit()
                db.add(self.shelf.ReaderLink(key='OLDKEY',book_id=1,snapshot='{"retained":true}'))
                db.add(self.shelf.ReaderLink(key='HIDDEN',book_id=None,snapshot='{}'))
                db.add(self.shelf.X3Upload(key='OLDKEY',filename='old.epub',status='queued'))
                run=self.shelf.RecommendationRun(status='ready',analysis='Old analysis');db.add(run);db.commit()
                db.add(self.shelf.Recommendation(run_id=run.id,book_id=1,kind='planned',title='Old recommendation',reason='Keep me',status='dismissed'));db.commit()
            engine.dispose()
            old=Store(source/'reader-sync/data')
            old.inventory('kindle',[dict(key='OLDKEY',fingerprint='a'*64),dict(key='HIDDEN',fingerprint='b'*64)])
            path=old.root/'books/OLDKEY/epub/hash/book.epub';path.parent.mkdir(parents=True);path.write_bytes(epub())
            with old.connect(write=True) as db:
                db.execute("UPDATE kindle_books SET epub_path=?,epub_status='ready',pid=20,xpath=?,revision=9,epoch=2,percentage=.2 WHERE key='OLDKEY'",(str(path),XPATH+'20'))
                db.execute("INSERT INTO documents(document,book_key,progress,percentage) VALUES('old-doc','OLDKEY',?,.2)",(XPATH+'20',))
                db.execute('ALTER TABLE kindle_books RENAME TO books')
            with sqlite3.connect(old.db) as db,sqlite3.connect(old.root/'sync.sqlite3') as legacy:
                db.backup(legacy)
            before=(source/'myshelf/library.db').read_bytes()
            report=migrate(source,dest)
            self.assertEqual(before,(source/'myshelf/library.db').read_bytes())
            self.assertEqual(b'original cover',(dest/'covers/1.jpg').read_bytes())
            with sqlite3.connect(dest/'bookhub.sqlite3') as db:
                self.assertEqual(('Manual title','Manual author','reading','Manual review','old-instance'),db.execute('SELECT title,authors,status,summary,instance_id FROM books').fetchone())
                self.assertEqual((20,9,2),db.execute("SELECT pid,revision,epoch FROM kindle_books WHERE key='OLDKEY'").fetchone())
                self.assertEqual(('old-doc','OLDKEY',XPATH+'20'),db.execute('SELECT document,book_key,progress FROM documents').fetchone())
                self.assertEqual(('dismissed','Keep me'),db.execute('SELECT status,reason FROM recommendations').fetchone())
                self.assertEqual('paused',db.execute('SELECT status FROM x3_uploads').fetchone()[0])
                self.assertIsNone(db.execute("SELECT book_id FROM reader_links WHERE key='HIDDEN'").fetchone()[0])
                migrated=Path(db.execute("SELECT epub_path FROM kindle_books WHERE key='OLDKEY'").fetchone()[0])
                self.assertEqual(epub(),migrated.read_bytes())
            with self.assertRaises(RuntimeError):migrate(source,dest)
