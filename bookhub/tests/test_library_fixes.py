"""Library and reconcile regressions: idle passes, deleted cards, waiting documents, merges, orphans, pages,
discarded files, positions saved by the reader itself."""
import hashlib
import io
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

from fastapi.testclient import TestClient

TEXT = 'A unique paragraph about sailing across the northern ocean and watching distant mountains in the morning.'
XPATH = '/body/DocFragment[1]/body/p[1]/text()[1].'

def epub(text=TEXT,title='My book',bodies=None):
    bodies=bodies or ['<p>'+text+'</p>']
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w') as z:
        z.writestr(zipfile.ZipInfo('META-INF/container.xml'),'<container><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>')
        items=''.join(f'<item id="c{n}" href="c{n}.xhtml"/>' for n in range(len(bodies)))
        spine=''.join(f'<itemref idref="c{n}"/>' for n in range(len(bodies)))
        z.writestr(zipfile.ZipInfo('book.opf'),f'<package xmlns:dc="http://purl.org/dc/elements/1.1/"><metadata><dc:title>{title}</dc:title><dc:creator>Author</dc:creator></metadata><manifest>{items}</manifest><spine>{spine}</spine></package>')
        for n,body in enumerate(bodies):
            z.writestr(zipfile.ZipInfo(f'c{n}.xhtml'),'<html><body>'+body+'</body></html>')
    return buf.getvalue()

def xpath_for_spine(index,spine_index,visible_offset=None,fraction=None):
    """EpubIndex.xpath_for_spine as its contract describes it, for a book of one <p> per chapter."""
    chapters=len(index.spine);finished=spine_index==chapters
    if not 0<=spine_index<=chapters:raise ValueError('Spine index outside EPUB')
    run=index.runs[spine_index-finished]
    offset=len(run['text']) if finished else int((fraction or 0)*len(run['text'])) if visible_offset is None else visible_offset
    return dict(xpath=f"{run['xpath']}.{offset}",percentage=1.0 if finished else (run['start']+offset)/len(index.text))

def forget_shelf():
    # bookhub.shelf binds its paths and engine at import: every fixture of this process imports its own copy.
    import bookhub
    for name in ('shelf','application','worker','description','bootstrap'):
        sys.modules.pop('bookhub.'+name,None)
        vars(bookhub).pop(name,None)


class LibraryFixesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.env=patch.dict(os.environ,BOOKHUB_DATA=cls.temp.name,BOOKHUB_DB=str(Path(cls.temp.name)/'bookhub.sqlite3'),AI_URL='',AI_MODEL='',SYNC_API_KEY='token',SYNC_USER='reader',SYNC_PASSWORD='password')
        cls.env.start()
        forget_shelf()
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
        forget_shelf()

    def setUp(self):
        self.network=patch('requests.sessions.Session.request',side_effect=AssertionError('Unexpected external request'))
        self.network.start();self.addCleanup(self.network.stop)
        with self.store.connect() as db:
            db.execute('PRAGMA foreign_keys=OFF')
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
                db.execute('DELETE FROM "'+row['name']+'"')
        self.shelf.AI_ENABLED=False
        # Files of earlier tests stay on disk under the same names: nothing cached may outlive a test.
        self.store.core.idle.clear();self.store.core.index.cache_clear();self.store.built.cache_clear()

    def add(self, data=None, filename='book.epub'):
        r=self.web.post('/api/epubs',params={'filename':filename},content=data or epub())
        self.assertEqual(200,r.status_code,r.text)
        return r.json()

    def push(self,doc,progress,device='one'):
        if isinstance(progress,int):progress=XPATH+str(progress)
        return self.sync.put('/syncs/progress',headers=self.ko,json=dict(document=doc,progress=progress,percentage=.5,device='KOReader',device_id=device))

    def position(self,card):
        return next(f for f in self.store.core.files(card['book_id']) if f['sha256']==card['sha256'])

    def remove_card(self,book_id):
        """What deleting a card leaves to the library: its files, held by no row of that card."""
        with self.store.connect(write=True) as db:
            db.execute('DELETE FROM books WHERE id=?',(book_id,))

    def kindle(self,key,map_text=None):
        """A card with its EPUB and a Kindle book holding the same file with a ready map."""
        from bookhub.engine.kindle_map import PositionMap,write_map
        text=key+' edition. '+TEXT;data=epub(text,title='Volume '+key)
        card=self.store.core.ingest(data,key.lower()+'.epub')
        path=self.store.root/'books'/key/'epub'/card['sha256']/'book.epub'
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
        chunk=map_text or text;folder=self.store.root/'maps'/key/'kfx'
        write_map(PositionMap(dict(version=1,book=dict(key=key),total_positions=len(chunk),chunks=[dict(pid=0,eid=1,offset=0,length=len(chunk),text=chunk)])),folder/'map.json',folder/(key+'.posmap'))
        with self.store.connect(write=True) as db:
            db.execute("""INSERT INTO kindle_books(key,title,epub_path,epub_status,map_path,compact_path,map_status,asset_version)
VALUES(?,?,?,'ready',?,?,'ready',1)""",(key,'Volume '+key,str(path),str(folder/'map.json'),str(folder/(key+'.posmap'))))
            db.execute('INSERT INTO reader_links(key,book_id) VALUES(?,?)',(key,card['book_id']))
        return dict(card,key=key,at=lambda word:text.index(word))

    def broken_kindle(self,book_id):
        with self.store.connect(write=True) as db:
            db.execute("INSERT INTO kindle_books(key,title,epub_path,epub_status) VALUES('BROKEN','A broken book',?,'ready')",(str(self.store.root/'gone.epub'),))
            db.execute("INSERT INTO reader_links(key,book_id) VALUES('BROKEN',?)",(book_id,))

    def test_idle_reconcile_only_reads_and_real_changes_still_propagate(self):
        from bookhub.worker import reconcile
        from bookhub.engine.epub_xpath import EpubIndex
        from bookhub.engine.position_bridge import PositionBridge
        core=self.store.core
        books=[self.kindle(f'SYNC{n}') for n in range(8)]
        for b in books:
            self.assertTrue(self.store.update_position(b['key'],b['at']('sailing'),'kindle',1)['accepted'])
            self.assertEqual(XPATH+str(b['at']('sailing')),self.position(b)['xpath'])
        # The library is ahead of this Kindle and its map cannot translate the position back.
        ahead=self.kindle('AHEAD',map_text='Entirely different words that never occur anywhere in the volume itself.')
        with self.store.connect(write=True) as db:
            db.execute("UPDATE kindle_books SET pid=5,xpath=?,revision=revision+1 WHERE key='AHEAD'",(XPATH+str(ahead['at']('edition')),))
        self.assertTrue(core.accept(ahead['book_id'],ahead['sha256'],XPATH+str(ahead['at']('northern')),.5,'reader',propagate=False)['accepted'])
        # Sorted first: its missing file must not keep the pass from the books after it.
        self.broken_kindle(books[0]['book_id'])

        built,translated,writes=[],[],[]
        build,translate,connect=EpubIndex.__init__,PositionBridge.to_kindle,self.store.connect
        def index(this,path):built.append(str(path));build(this,path)
        def to_kindle(this,*args,**kwargs):translated.append(this.kfx.key);return translate(this,*args,**kwargs)
        def connection(write=False):
            if write:writes.append(1)
            return connect(write)
        def run():
            del built[:],translated[:],writes[:]
            with patch.object(EpubIndex,'__init__',index),patch.object(PositionBridge,'to_kindle',to_kindle),patch.object(self.store,'connect',connection),patch.object(self.shelf,'reader_sync_once'):
                reconcile(self.store)
        def kindle_assets():
            with self.store.connect() as db:return {r['key'] for r in db.execute('SELECT key FROM kindle_assets')}

        with self.assertLogs(level='ERROR') as logs:run()
        self.assertEqual(1,len(logs.records));self.assertIn('BROKEN',logs.output[0])
        self.assertEqual({'AHEAD',*(b['key'] for b in books)},kindle_assets())
        self.assertTrue(built);self.assertEqual(['AHEAD'],translated)

        watch=sqlite3.connect(self.store.db);self.addCleanup(watch.close)
        version=lambda:watch.execute('PRAGMA data_version').fetchone()[0]
        for _ in range(2):
            before=version()
            with self.assertNoLogs(level='ERROR'):run()
            self.assertEqual(([],[],[]),(built,translated,writes))
            self.assertEqual(before,version())

        # A library position that never reached the Kindle, a Kindle position that never reached the library,
        # and a new library position for the map that cannot translate.
        first,second=books[0],books[1]
        self.assertTrue(core.accept(first['book_id'],first['sha256'],XPATH+str(first['at']('ocean')),.6,'reader',propagate=False)['accepted'])
        with patch.object(core,'attach_kindle'):
            self.assertTrue(self.store.update_position(second['key'],second['at']('mountains'),'kindle',1)['accepted'])
        self.assertTrue(core.accept(ahead['book_id'],ahead['sha256'],XPATH+str(ahead['at']('watching')),.7,'reader',propagate=False)['accepted'])
        before=version()
        with self.assertNoLogs(level='ERROR'):run()
        self.assertNotEqual(before,version())
        kindle=self.store.book(first['key'])
        self.assertEqual((first['at']('ocean'),XPATH+str(first['at']('ocean')),'epub'),(kindle['pid'],kindle['xpath'],kindle['source']))
        self.assertEqual(XPATH+str(second['at']('mountains')),self.position(second)['xpath'])
        self.assertIn('AHEAD',translated)
        with self.assertNoLogs(level='ERROR'):run()
        self.assertEqual(([],[],[]),(built,translated,writes))

        # A failure is not final: the broken book gets another attempt after RETRY.
        with patch.object(time,'monotonic',return_value=time.monotonic()+core.RETRY+1),self.assertLogs(level='ERROR') as logs:run()
        self.assertIn('BROKEN',logs.output[0])

    def test_broken_kindle_book_does_not_stop_the_rest_of_the_pass(self):
        from bookhub.worker import reconcile
        good=self.kindle('GOOD')
        self.broken_kindle(good['book_id'])
        with self.store.connect(write=True) as db:
            db.execute("UPDATE kindle_books SET pid=?,xpath=?,revision=revision+1 WHERE key='GOOD'",(good['at']('sailing'),XPATH+str(good['at']('sailing'))))
            db.execute("UPDATE books SET summary_status='pending',summary_job_id='job' WHERE id=?",(good['book_id'],))
        with self.assertLogs(level='ERROR') as logs:reconcile(self.store)
        self.assertEqual(1,len(logs.records));self.assertIn('BROKEN',logs.output[0])
        self.assertEqual(XPATH+str(good['at']('sailing')),self.position(good)['xpath'])
        with self.store.connect() as db:
            self.assertEqual(('summary','job'),tuple(db.execute('SELECT kind,token FROM tasks').fetchone()))
        with self.assertNoLogs(level='ERROR'):reconcile(self.store)

    def test_repeated_position_is_answered_without_parsing_the_epub(self):
        b=self.add();doc=hashlib.md5(b'book.epub').hexdigest()
        self.assertTrue(self.push(doc,20).json()['accepted'])
        with patch.object(self.store.core,'locate',side_effect=AssertionError('The EPUB was parsed')):
            answer=self.push(doc,20,'two').json()
        self.assertEqual((False,'unchanged',1),(answer['accepted'],answer['reason'],answer['revision']))
        self.assertEqual(2,len(self.store.core.readers(b['book_id'])))

    def test_deleted_card_of_a_bookbot_file_does_not_return(self):
        from bookhub.worker import reconcile
        r=self.web.post('/api/incoming/epub',params={'filename':'sent.epub'},content=epub(title='Sent to Kindle'))
        self.assertEqual(200,r.status_code,r.text)
        self.assertEqual(200,self.web.delete(f"/api/books/{r.json()['book_id']}").status_code)
        reconcile(self.store);reconcile(self.store)
        self.assertEqual([],self.web.get('/api/books').json())

    def test_legacy_incoming_file_is_imported_once_and_a_failing_one_is_not_retried_every_pass(self):
        from bookhub.worker import reconcile
        core=self.store.core;legacy=epub(title='Legacy');sha=hashlib.sha256(legacy).hexdigest()
        Path(self.shelf.incoming_path('b'*64)).write_bytes(b'PKnot a zip')
        Path(self.shelf.incoming_path(sha)).write_bytes(legacy)
        with self.shelf.db_session() as db:
            db.add(self.shelf.IncomingEpub(filename='unreadable.epub',sha256='b'*64))
            db.add(self.shelf.IncomingEpub(filename='legacy.epub',sha256=sha))
            db.commit()
        with patch.object(core,'ingest',wraps=core.ingest) as ingest,self.assertLogs(level='ERROR') as logs:reconcile(self.store)
        self.assertEqual(2,ingest.call_count);self.assertEqual(1,len(logs.records))
        books=self.web.get('/api/books').json()
        self.assertEqual(['Legacy'],[b['title'] for b in books])
        with patch.object(core,'ingest',wraps=core.ingest) as ingest,self.assertNoLogs(level='ERROR'):reconcile(self.store)
        ingest.assert_not_called()
        self.assertEqual(200,self.web.delete(f"/api/books/{books[0]['id']}").status_code)
        with patch.object(core,'ingest',wraps=core.ingest) as ingest:reconcile(self.store);reconcile(self.store)
        ingest.assert_not_called()
        self.assertEqual([],self.web.get('/api/books').json())

    def test_upload_adopts_waiting_reader_documents_and_applies_their_position(self):
        data=epub(TEXT+' It waited.',title='Waited')
        by_name,by_content=hashlib.md5(b'waited.epub').hexdigest(),self.store.core.koreader_digest(data)
        self.assertTrue(self.push(by_name,20,'x3').json()['pending'])
        self.assertTrue(self.push(by_content,40,'koreader').json()['pending'])
        b=self.add(data,'waited.epub')
        card=self.web.get(f"/api/books/{b['book_id']}").json()
        self.assertEqual(XPATH+'40',card['epubs'][0]['xpath'])
        self.assertEqual({'x3','koreader'},{d['device_id'] for d in card['devices']})
        self.assertEqual(XPATH+'40',self.sync.get('/syncs/progress/'+by_name,headers=self.ko).json()['progress'])
        self.assertTrue(self.push(by_name,60,'x3').json()['accepted'])
        with self.store.connect() as db:
            self.assertEqual([None,None],[r['error'] for r in db.execute('SELECT error FROM documents')])

    def test_upload_leaves_documents_bound_elsewhere_untouched(self):
        other=self.add(epub(TEXT+' Another book.',title='Other'),'other.epub')
        data=epub(TEXT+' Same name.',title='Same')
        legacy,claimed=hashlib.md5(b'same.epub').hexdigest(),self.store.core.koreader_digest(data)
        with self.store.connect(write=True) as db:
            db.execute("INSERT INTO kindle_books(key,title) VALUES('OWNER','Kindle owner')")
            db.execute("INSERT INTO documents(document,book_key,progress,percentage) VALUES(?,'OWNER',?,.2)",(legacy,XPATH+'20'))
        self.assertTrue(self.push('manual',30).json()['pending'])
        self.assertTrue(self.store.core.bind(claimed,other['book_id'])['pending'])
        b=self.add(data,'same.epub')
        self.assertIsNone(self.position(b)['xpath'])
        with self.store.connect() as db:
            rows={r['document']:(r['book_key'],r['library_book_id']) for r in db.execute('SELECT * FROM documents')}
        self.assertEqual(('OWNER',None),rows[legacy])
        self.assertEqual((None,other['book_id']),rows[claimed])
        self.assertEqual((None,None),rows['manual'])

    def test_merge_keeps_the_latest_position_of_an_epub_held_by_both_cards(self):
        core=self.store.core
        # A row is the time it was last written and what was written: a position, or a reset of one.
        # Without a time it was never written. A reset never beats a position, however late it was.
        saved,reset=True,False
        cases=[((200,saved),(100,saved),'source'),((100,saved),(200,saved),'target'),((100,saved),(100,saved),'target'),
               ((None,reset),(100,saved),'target'),((100,saved),(None,reset),'source'),
               ((300,reset),(100,saved),'target'),((100,saved),(300,reset),'source'),
               ((300,reset),(100,reset),'source'),((None,reset),(None,reset),'target')]
        for n,(source_row,target_row,winner) in enumerate(cases):
            with self.subTest(source=source_row,target=target_row):
                data=epub(TEXT+f' Shared {n}.',title=f'Source {n}')
                source=self.add(data,f'shared{n}.epub')
                target=self.add(epub(TEXT+f' Only here {n}.',title=f'Target {n}'),f'target{n}.epub')
                sha=core.ingest(data,f'shared{n}.epub',target['book_id'])['sha256']
                for card,offset,(at,holds),revision,epoch in ((source,20,source_row,5,3),(target,40,target_row,2,1)):
                    if at:self.assertTrue(core.accept(card['book_id'],sha,XPATH+str(offset),offset/100,'reader')['accepted'])
                    if at and not holds:core.reset(card['book_id'],sha)
                    with self.store.connect(write=True) as db:
                        db.execute('UPDATE reading_positions SET updated_at=?,revision=?,epoch=? WHERE book_id=? AND sha256=?',(at,revision,epoch,card['book_id'],sha))
                r=self.web.post(f"/api/books/{source['book_id']}/merge",json={'target_id':target['book_id']})
                self.assertEqual(200,r.status_code,r.text)
                offset,(at,holds)=dict(source=(20,source_row),target=(40,target_row))[winner]
                merged=self.position(dict(book_id=target['book_id'],sha256=sha))
                self.assertEqual((XPATH+str(offset),offset/100,at,3,6) if holds else (None,None,at,3,6),
                                 tuple(merged[f] for f in ('xpath','percentage','updated_at','epoch','revision')))
                progress=self.sync.get('/syncs/progress/'+hashlib.md5(f'shared{n}.epub'.encode()).hexdigest(),headers=self.ko)
                if holds:
                    self.assertEqual(XPATH+str(offset),progress.json()['progress'])
                    # The surviving row is a consistent position: the same place is known, an earlier one is behind.
                    self.assertEqual('unchanged',core.accept(target['book_id'],sha,XPATH+str(offset),0,'reader',epoch=3)['reason'])
                    self.assertEqual('behind',core.accept(target['book_id'],sha,XPATH+'10',0,'reader',epoch=3)['reason'])
                else:
                    # Neither card had a position: the merged one starts its reading cycle anywhere.
                    self.assertEqual(404,progress.status_code)
                    self.assertTrue(core.accept(target['book_id'],sha,XPATH+'10',.1,'reader',epoch=3)['accepted'])

    def test_failed_ingest_leaves_no_file_under_epubs(self):
        from bookhub.sync.store import Problem
        core=self.store.core;root=self.store.root
        unreadable=b'PKnot a zip archive, whatever its name says'
        homeless=epub(TEXT+' Nobody owns this file.',title='Homeless')
        self.assertEqual(400,self.web.post('/api/epubs?filename=unreadable.epub',content=unreadable).status_code)
        with self.assertRaises(Problem) as missing:core.ingest(homeless,'homeless.epub',987654)
        self.assertEqual(404,missing.exception.status)
        for data in (unreadable,homeless):
            self.assertFalse((root/'epubs'/hashlib.sha256(data).hexdigest()).exists())
        self.assertEqual([],list((root/'tmp').iterdir()))
        with self.store.connect() as db:
            self.assertEqual(0,db.execute('SELECT COUNT(*) FROM epub_assets').fetchone()[0])
        b=self.add(homeless,'homeless.epub')
        self.assertEqual(homeless,(root/'epubs'/b['sha256']/'book.epub').read_bytes())
        self.assertEqual(homeless,self.web.get(f"/api/books/{b['book_id']}/file/epub").content)
        self.assertEqual(b['book_id'],self.add(homeless,'homeless.epub')['book_id'])
        self.assertEqual([],list((root/'tmp').iterdir()))

    def test_textless_book_takes_any_node_of_a_page_as_that_page(self):
        from bookhub.sync.store import Problem
        core=self.store.core
        b=self.add(epub(title='Manga',bodies=[f'<div><img src="page{n}.jpg"/></div>' for n in range(3)]),'manga.epub')
        answer=core.accept(b['book_id'],b['sha256'],'/body/DocFragment[2]/body/div/img',.5,'reader')
        self.assertEqual((True,10**12),(answer['accepted'],answer['ordinal']))
        self.assertEqual('unchanged',core.accept(b['book_id'],b['sha256'],'/body/DocFragment[2]/body',.5,'reader')['reason'])
        self.assertEqual('unchanged',core.accept(b['book_id'],b['sha256'],'/body/DocFragment[2].0',.5,'reader')['reason'])
        self.assertEqual('behind',core.accept(b['book_id'],b['sha256'],'/body/DocFragment[1]/body/div/img',.1,'reader')['reason'])
        with self.assertRaises(Problem):core.accept(b['book_id'],b['sha256'],'/body/DocFragment[4]/body/div/img',.9,'reader')
        answer=self.push(hashlib.md5(b'manga.epub').hexdigest(),'/body/DocFragment[3]/body/div[1]/img[1]').json()
        self.assertTrue(answer['accepted'],answer)
        self.assertEqual('/body/DocFragment[3]/body/div[1]/img[1]',self.position(b)['xpath'])

    def test_discard_removes_only_the_files_nothing_holds(self):
        core=self.store.core
        data={name:epub(TEXT+f' A {name} file.',title=name) for name in ('own','shared','kindle','kept')}
        cards={name:self.add(data[name],name+'.epub') for name in data}
        sha={name:card['sha256'] for name,card in cards.items()};keeper=cards['kept']['book_id']
        def stored():
            with self.store.connect() as db:rows={r['sha256'] for r in db.execute('SELECT sha256 FROM epub_assets')}
            files={name for name in sha if (self.store.root/'epubs'/sha[name]/'book.epub').exists()}
            self.assertEqual({sha[name] for name in files},rows)
            return files
        core.ingest(data['shared'],'shared.epub',keeper)
        with self.store.connect(write=True) as db:
            db.execute("INSERT INTO kindle_assets VALUES('KEY',?,'/books/KEY/book.epub',?)",(keeper,sha['kindle']))
        self.assertEqual([],core.discard(sha.values()))
        for name in ('own','shared','kindle'):self.remove_card(cards[name]['book_id'])
        # Another card and a Kindle book still hold theirs; a hash the library never had is nobody's.
        self.assertEqual([sha['own']],core.discard([*sha.values(),'f'*64]))
        self.assertEqual({'shared','kindle','kept'},stored())
        self.assertFalse((self.store.root/'epubs'/sha['own']).exists())
        self.assertEqual(data['shared'],self.web.get(f'/api/books/{keeper}/file/epub',params={'sha256':sha['shared']}).content)
        with self.store.connect(write=True) as db:db.execute('DELETE FROM kindle_assets')
        self.assertEqual([sha['kindle']],core.discard(sha.values()))
        self.assertEqual([],core.discard(sha.values()))
        self.assertEqual({'shared','kept'},stored())

    def test_same_bytes_uploaded_after_or_during_a_discard_are_stored(self):
        from bookhub.sync import jobs
        core=self.store.core;data=epub(TEXT+' Deleted and uploaded again.',title='Again')
        first=self.add(data,'again.epub');path=self.store.root/'epubs'/first['sha256']/'book.epub'
        self.remove_card(first['book_id'])
        self.assertEqual([first['sha256']],core.discard([first['sha256']]))
        self.assertFalse(path.exists())
        second=self.add(data,'again.epub')
        self.assertEqual(data,self.web.get(f"/api/books/{second['book_id']}/file/epub").content)
        # The card is deleted while its file is being uploaded again: the discard lands after the
        # upload looked at the stored copy and before its transaction.
        self.remove_card(second['book_id'])
        check=jobs.validate_epub
        def validate(upload):
            self.assertEqual([first['sha256']],core.discard([first['sha256']]))
            return check(upload)
        with patch.object(jobs,'validate_epub',validate):third=self.add(data,'again.epub')
        self.assertEqual(data,path.read_bytes())
        self.assertEqual([first['sha256']],[f['sha256'] for f in core.files(third['book_id'])])
        self.assertEqual(data,self.web.get(f"/api/books/{third['book_id']}/file/epub").content)
        # A discard that comes second finds the file held by its new card.
        self.assertEqual([],core.discard([first['sha256']]))
        self.assertEqual([],list((self.store.root/'tmp').iterdir()))

    def test_incoming_file_of_a_deleted_card_is_not_imported_again(self):
        from bookhub.worker import reconcile
        core=self.store.core;data=epub(TEXT+' Sent by the bot.',title='From the bot');sha=hashlib.sha256(data).hexdigest()
        Path(self.shelf.incoming_path(sha)).write_bytes(data)
        with self.shelf.db_session() as db:
            db.add(self.shelf.IncomingEpub(filename='bot.epub',sha256=sha));db.commit()
        books=lambda:[(b['id'],b['title']) for b in self.web.get('/api/books').json()]
        # deleted_epubs is the shelf's table: a store that never ran the shelf has none and no deleted cards.
        with self.store.connect(write=True) as db:
            table=db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='deleted_epubs'").fetchone()
            db.execute('DROP TABLE IF EXISTS deleted_epubs')
        try:reconcile(self.store)
        finally:
            with self.store.connect(write=True) as db:
                db.execute(table['sql'] if table else 'CREATE TABLE deleted_epubs(sha256 TEXT PRIMARY KEY, deleted_book_id INTEGER)')
        (book_id,title),=books()
        self.assertEqual('From the bot',title)
        # The card is deleted the way the shelf does it: the file leaves the library, its hash is remembered.
        self.remove_card(book_id)
        self.assertEqual([sha],core.discard([sha]))
        with self.store.connect() as db:
            db.execute('PRAGMA foreign_keys=OFF')
            db.execute('INSERT INTO deleted_epubs(sha256,deleted_book_id) VALUES(?,?)',(sha,book_id))
        with patch.object(core,'ingest',wraps=core.ingest) as ingest:reconcile(self.store);reconcile(self.store)
        ingest.assert_not_called()
        self.assertEqual([],books())
        # Nothing else held it back: without that record the file is new to the library.
        with self.store.connect(write=True) as db:db.execute('DELETE FROM deleted_epubs')
        reconcile(self.store)
        self.assertEqual(['From the bot'],[title for _,title in books()])

    def test_position_saved_by_the_reader_itself_moves_the_book_forward_and_is_observed(self):
        # EpubIndex.xpath_for_spine belongs to the engine: the library is tested against a stand-in.
        from bookhub.engine.epub_xpath import EpubIndex
        core=self.store.core
        chapters=[f'Chapter {n}. '+TEXT for n in range(3)]
        b=self.add(epub(title='Read on the X3',bodies=['<p>'+c+'</p>' for c in chapters]),'x3.epub')
        at=lambda chapter,offset:f'/body/DocFragment[{chapter+1}]/body/p[1]/text()[1].{offset}'
        def observed(device_id='x3-web',document='doc'):
            return next((r['device'],r['progress'],r['percentage'],r['accepted']) for r in core.readers(b['book_id'])
                        if (r['device_id'],r['document'])==(device_id,document))
        def saved(spine,**kwargs):return core.accept_reader_position(b['book_id'],b['sha256'],spine,document='doc',**kwargs)
        with patch.object(EpubIndex,'xpath_for_spine',xpath_for_spine,create=True):
            answer=saved(1,visible_offset=30)
            self.assertEqual((True,at(1,30),'X3',1),tuple(answer[f] for f in ('accepted','xpath','source','revision')))
            self.assertTrue(1/3<answer['percentage']<2/3,answer)
            self.assertEqual(('X3',at(1,30),answer['percentage'],1),observed())
            self.assertEqual((at(1,30),answer['percentage']),tuple(self.position(b)[f] for f in ('xpath','percentage')))
            # It is the position of the card: the other readers of the book get it.
            progress=self.sync.get('/syncs/progress/'+hashlib.md5(b'x3.epub').hexdigest(),headers=self.ko).json()
            self.assertEqual((at(1,30),'X3'),(progress['progress'],progress['device']))
            # Forward only, as for any reader: an earlier place is observed and moves nothing.
            answer=saved(0,fraction=.5)
            self.assertEqual((False,'behind',at(1,30),1),tuple(answer[f] for f in ('accepted','reason','xpath','revision')))
            self.assertEqual(('X3',at(0,len(chapters[0])//2),0),observed()[:2]+observed()[3:])
            self.assertEqual('unchanged',saved(1,visible_offset=30)['reason'])
            # Neither an offset nor a fraction is the start of the chapter.
            answer=saved(2)
            self.assertEqual((True,at(2,0)),(answer['accepted'],answer['xpath']))
            # One past the last chapter: the book is finished. Without a document the file names the observation.
            answer=core.accept_reader_position(b['book_id'],b['sha256'],3,source='Xteink',device_id='x3-usb',device='Xteink X3')
            self.assertEqual((True,at(2,len(chapters[2])),1.0,'Xteink'),tuple(answer[f] for f in ('accepted','xpath','percentage','source')))
            self.assertEqual(('Xteink X3',at(2,len(chapters[2])),1.0,1),observed('x3-usb',b['sha256']))
            # A chapter the book does not have: nothing is accepted, nothing is recorded.
            with self.assertRaises(ValueError):saved(4)
            self.assertEqual(('X3',at(2,0),1),observed()[:2]+observed()[3:])
            self.assertEqual(at(2,len(chapters[2])),self.position(b)['xpath'])
            # A Kindle holding the same file is moved as by any other reader.
            kindle=self.kindle('ONKINDLE');core.attach_kindle('ONKINDLE')
            self.assertTrue(core.accept_reader_position(kindle['book_id'],kindle['sha256'],0,visible_offset=kindle['at']('ocean'))['accepted'])
            self.assertEqual((kindle['at']('ocean'),'epub'),tuple(self.store.book('ONKINDLE')[f] for f in ('pid','source')))

    def test_position_saved_by_the_reader_in_a_book_without_text_is_its_page(self):
        from bookhub.engine.epub_xpath import EpubIndex
        core=self.store.core
        b=self.add(epub(title='Manga',bodies=[f'<div><img src="page{n}.jpg"/></div>' for n in range(4)]),'manga.epub')
        def saved(spine,**kwargs):return core.accept_reader_position(b['book_id'],b['sha256'],spine,document='doc',**kwargs)
        with patch.object(EpubIndex,'xpath_for_spine',side_effect=AssertionError('There is no text to count by'),create=True):
            answer=saved(2,visible_offset=0)
            self.assertEqual((True,'/body/DocFragment[3]/body',2*10**12,.5),tuple(answer[f] for f in ('accepted','xpath','ordinal','percentage')))
            self.assertEqual('behind',saved(1,fraction=.9)['reason'])
            answer=saved(4)
            self.assertEqual((True,'/body/DocFragment[4]/body',1.0),tuple(answer[f] for f in ('accepted','xpath','percentage')))
            with self.assertRaises(ValueError):saved(5)
        self.assertEqual([('x3-web','doc','/body/DocFragment[4]/body',1)],
                         [tuple(r[f] for f in ('device_id','document','progress','accepted')) for r in core.readers(b['book_id'])])


if __name__=='__main__':
    unittest.main()
