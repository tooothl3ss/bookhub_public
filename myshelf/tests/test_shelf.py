"""Shelf regressions. All data lives in a temporary directory; network is forbidden.
Run: python3 -B -m unittest discover -s myshelf/tests -v
"""
import importlib.util
import os
from pathlib import Path
import shutil
import json
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from fastapi import BackgroundTasks, HTTPException


class ShelfTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='bookhub-tests-')
        root = Path(cls.temp.name)
        shutil.copy(Path(__file__).resolve().parents[2] / 'bookhub/shelf.py', root / 'main.py')
        (root / 'static').mkdir()
        cls.env = patch.dict(os.environ, {'DB_PATH': str(root / 'test.db'), 'BOOKHUB_DB': str(root / 'test.db'), 'BOOKHUB_DATA': str(root),
                                         'AI_URL': '', 'AI_MODEL': ''})
        cls.env.start()
        cls.network = patch('requests.sessions.Session.request', side_effect=AssertionError('Unexpected network'))
        cls.network.start()
        spec = importlib.util.spec_from_file_location('bookhub.shelf_regression_app', root / 'main.py')
        cls.m = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.m
        spec.loader.exec_module(cls.m)
        cls.m.initialize_storage()
        cls.m.reader_service.store.core.initialize()

    @classmethod
    def tearDownClass(cls):
        cls.m.engine.dispose()
        cls.network.stop()
        cls.env.stop()
        cls.temp.cleanup()

    def setUp(self):
        self.m.AI_ENABLED = True
        with self.m.reader_service.store.connect() as db:
            db.execute('DELETE FROM tasks') if db.execute("SELECT 1 FROM sqlite_master WHERE name='tasks'").fetchone() else None
            db.execute('DELETE FROM documents')
            db.execute('DELETE FROM kindle_books')
        with self.m.db_session() as db:
            for table in reversed(self.m.Base.metadata.sorted_tables):
                db.execute(table.delete())
            db.commit()

    def book(self, key='/works/OL1W', status='want', summary=''):
        with self.m.db_session() as db:
            book = self.m.Book(source_id=key, title=key, authors='Author', status=status,
                               summary=summary, summary_status='ready' if summary else 'idle')
            db.add(book); db.commit(); db.refresh(book)
            return book.id

    def row(self, bid):
        with self.m.db_session() as db:
            b = db.get(self.m.Book, bid)
            return {c.name: getattr(b, c.name) for c in self.m.Book.__table__.columns}

    def task(self, bid):
        tasks = BackgroundTasks()
        self.m.regenerate_summary(bid, tasks)
        b = self.row(bid)
        return lambda: self.m.generate_summary_background(bid,b['title'],b['authors'],b['description'] or '',b['summary_job_id'])

    def run_record(self, status='pending'):
        with self.m.db_session() as db:
            run = self.m.RecommendationRun(status=status)
            db.add(run); db.commit(); db.refresh(run)
            return run.id

    def rec(self, rid, bid=None, source_id=None):
        with self.m.db_session() as db:
            rec = self.m.Recommendation(run_id=rid, book_id=bid, source_id=source_id,
                                       title='Candidate', kind='planned' if bid else 'external', status='active')
            db.add(rec); db.commit(); db.refresh(rec)
            return rec.id

    def test_duplicate_add_preserves_all_statuses(self):
        for status in self.m.VALID_STATUSES:
            bid = self.book('/works/'+status, status)
            result = self.m.add_book(self.m.BookAddSchema(source_id='/works/'+status, status='want'), BackgroundTasks())
            self.assertEqual(result, {'status': 'exists', 'id': bid, 'book_status': status, 'deleted_before': None})
            self.assertEqual(self.row(bid)['status'], status)

    def test_duplicate_created_during_catalog_lookup_preserves_status(self):
        bundle = dict(title='Book', authors='Author', year=None, language=None, description='', cover_urls=[])
        def lookup(key):
            self.book(key, 'read')
            return bundle
        with patch.object(self.m, 'fetch_work_bundle', side_effect=lookup):
            result = self.m.add_book(self.m.BookAddSchema(source_id='/works/OL1W'), BackgroundTasks())
        self.assertEqual(result['book_status'], 'read')

    def test_manual_review_survives_late_ai_response_and_error(self):
        for failure in (False, True):
            bid = self.book('/works/manual'+str(failure))
            task = self.task(bid)
            def ai(*args):
                self.m.patch_book(bid, self.m.BookPatchSchema(summary='My review'))
                if failure:
                    raise self.m.AIError('Service unavailable')
                return 'Old generated review'
            with patch.object(self.m, 'ai_chat', side_effect=ai):
                task()
            self.assertEqual(self.row(bid)['summary'], 'My review')
            self.assertEqual(self.row(bid)['summary_status'], 'ready')

    def test_title_only_patch_keeps_finished_review(self):
        bid = self.book()
        task = self.task(bid)
        with patch.object(self.m, 'ai_chat', return_value='Finished review'):
            task()
        self.m.patch_book(bid, self.m.BookPatchSchema(title='Corrected title'))
        self.assertEqual(self.row(bid)['summary'], 'Finished review')
        self.assertEqual(self.row(bid)['summary_status'], 'ready')

    def test_deleted_id_reuse_does_not_receive_old_review(self):
        bid = self.book()
        task = self.task(bid)
        def ai(*args):
            self.m.delete_book(bid)
            self.assertEqual(self.book('/works/OL2W', summary='Book B review'), bid)
            return 'Book A review'
        with patch.object(self.m, 'ai_chat', side_effect=ai):
            task()
        self.assertEqual(self.row(bid)['summary'], 'Book B review')

    def test_queued_job_cannot_start_on_reused_id(self):
        bid = self.book()
        task = self.task(bid)
        self.m.delete_book(bid)
        self.assertEqual(self.book('/works/OL2W'), bid)
        with patch.object(self.m, 'ai_chat') as ai:
            task()
            ai.assert_not_called()

    def test_old_job_cannot_overwrite_replacement_job(self):
        bid = self.book()
        old_task = self.task(bid)
        def old_ai(*args):
            self.m.patch_book(bid, self.m.BookPatchSchema(summary='Manual'))
            new_task = self.task(bid)
            with patch.object(self.m, 'ai_chat', return_value='New review'):
                new_task()
            return 'Old review'
        with patch.object(self.m, 'ai_chat', side_effect=old_ai):
            old_task()
        self.assertEqual(self.row(bid)['summary'], 'New review')

    def test_new_book_and_periodic_queue_never_generate_summary(self):
        bundle = dict(title='Book', authors='Author', year=None, language=None, description='', cover_urls=[])
        tasks = BackgroundTasks()
        with patch.object(self.m, 'fetch_work_bundle', return_value=bundle), patch.object(self.m, 'download_first_cover', return_value=None):
            bid = self.m.add_book(self.m.BookAddSchema(source_id='/works/OL1W'), tasks)['id']
        self.assertEqual(tasks.tasks, [])
        self.assertEqual(self.row(bid)['summary_status'], 'idle')
        with patch.object(self.m, 'ai_chat', side_effect=AssertionError('AI only on request')):
            self.assertEqual(self.m.queue_missing_summaries(), 0)
        self.assertEqual(self.row(bid)['summary_status'], 'idle')

    def test_hidden_books_can_be_recommended_in_next_run(self):
        bid = self.book(); old = self.run_record('ready')
        for rec in (self.rec(old, bid), self.rec(old, source_id='/works/OL2W')):
            self.m.update_recommendation(rec, self.m.RecommendationDismissSchema())
        rid = self.run_record()
        candidate = dict(source_id='/works/OL2W', title='External', authors='Author', year=None, language=None, cover='', search_query='query')
        def candidates(queries, excluded):
            self.assertNotIn('/works/OL2W', excluded)
            return [candidate]
        responses = [self.m.RecoPlan(planned_picks=[self.m.PlannedPick(book_id=bid)], search_queries=['query']),
                     self.m.RecoChoice(external_picks=[self.m.ExternalPick(source_id='/works/OL2W')])]
        with patch.object(self.m, 'ai_structured', side_effect=responses), patch.object(self.m, 'collect_openlibrary_candidates', side_effect=candidates):
            self.m.run_recommendations_job(rid)
        result = self.m.get_recommendations()
        self.assertEqual(len(result['planned']), 1)
        self.assertEqual(len(result['external']), 1)
        self.assertEqual(self.m.get_recommendations(old)['planned'], [])

    def test_current_filters_changed_books_but_archive_keeps_them(self):
        bid=self.book();rid=self.run_record('ready');rec=self.rec(rid,bid)
        self.m.patch_book(bid,self.m.BookPatchSchema(status='read'))
        self.assertEqual(self.m.get_recommendations()['planned'], [])
        archived=self.m.get_recommendations(rid)['planned'][0]
        self.assertFalse(archived['available']);self.assertEqual(archived['book_status'],'read')
        with self.assertRaises(HTTPException) as exc:
            self.m.recommendation_book_action(rec,'start')
        self.assertEqual(exc.exception.status_code,409)
        self.assertEqual(self.row(bid)['status'],'read')
        self.m.delete_book(bid)
        archived=self.m.get_recommendations(rid)['planned'][0]
        self.assertFalse(archived['available']);self.assertIsNone(archived['book_id'])
        with self.assertRaises(HTTPException):self.m.recommendation_book_action(rec,'resume')

    def test_accept_recommendation_remains_in_archive_and_can_be_read_again(self):
        bid=self.book();rid=self.run_record('ready');rec=self.rec(rid,bid)
        self.m.recommendation_book_action(rec,'start')
        self.assertEqual(self.m.get_recommendations()['planned'], [])
        self.assertEqual(len(self.m.get_recommendations(rid)['planned']),1)
        for status in ('read','dropped'):
            self.m.patch_book(bid,self.m.BookPatchSchema(status=status))
            self.m.recommendation_book_action(rec,'plan')
            self.assertEqual(self.row(bid)['status'],'want')
            self.m.patch_book(bid,self.m.BookPatchSchema(status=status))
            self.m.recommendation_book_action(rec,'resume')
            self.assertEqual(self.row(bid)['status'],'reading')

    def test_external_book_added_to_library_disappears_from_current(self):
        rid=self.run_record('ready');rec=self.rec(rid,source_id='/works/OL1W')
        bid=self.book(status='read')
        self.assertEqual(self.m.get_recommendations()['external'],[])
        archived=self.m.get_recommendations(rid)['external'][0]
        self.assertFalse(archived['available']);self.assertEqual(archived['book_id'],bid)
        self.m.recommendation_book_action(rec,'plan')
        self.assertEqual(self.row(bid)['status'],'want')

    def test_deleted_changed_or_replaced_candidate_does_not_break_generation(self):
        for action in ('delete','status','replace'):
            with self.subTest(action=action):
                self.setUp()
                bid=self.book();survivor=self.book('/works/survivor');rid=self.run_record()
                def plan(*args, **kwargs):
                    if action=='status': self.m.patch_book(bid,self.m.BookPatchSchema(status='read'))
                    else:
                        self.m.delete_book(bid)
                        if action=='replace':
                            with self.m.db_session() as db:
                                db.add(self.m.Book(id=bid,source_id='/works/replacement',title='Other',status='want'))
                                db.commit()
                    return self.m.RecoPlan(planned_picks=[self.m.PlannedPick(book_id=bid),self.m.PlannedPick(book_id=survivor)])
                with patch.object(self.m,'ai_structured',side_effect=plan),patch.object(self.m,'collect_openlibrary_candidates',return_value=[]):
                    self.m.run_recommendations_job(rid)
                result=self.m.get_recommendations()
                self.assertEqual(result['run']['status'],'ready')
                self.assertEqual([r['book_id'] for r in result['planned']],[survivor])

    def test_existing_schema_migration_preserves_books_and_assigns_identity(self):
        path=Path(self.temp.name)/'legacy.db'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE books (id INTEGER PRIMARY KEY, source_id TEXT, title TEXT, authors TEXT, status TEXT, cover_path TEXT, summary TEXT)')
            db.execute("INSERT INTO books VALUES(1,'/works/old','Old title','Author','read',NULL,'Manual review')")
        self.m.ensure_sqlite_schema(str(path))
        with sqlite3.connect(path) as db:
            first=db.execute('SELECT title,status,summary,instance_id,summary_job_id,description_ru,description_translation_status,description_translation_job_id FROM books').fetchone()
        self.m.ensure_sqlite_schema(str(path))
        with sqlite3.connect(path) as db:
            second=db.execute('SELECT title,status,summary,instance_id,summary_job_id,description_ru,description_translation_status,description_translation_job_id FROM books').fetchone()
        self.assertEqual(first,second)
        self.assertEqual(first[:3],('Old title','read','Manual review'))
        self.assertEqual(len(first[3]),32);self.assertIsNone(first[4])
        self.assertEqual((None,'idle',None),first[5:])


    def context(self, bid):
        with self.m.db_session() as db:
            return self.m.book_to_recommendation_context(db.get(self.m.Book, bid))

    def test_drop_reason_is_saved_validated_and_sent_to_recommendations(self):
        bid=self.book(status='read')
        self.m.patch_book(bid,self.m.BookPatchSchema(status='dropped',drop_reason='lost_thread',drop_note='  вернусь потом '))
        row=self.row(bid)
        self.assertEqual((row['status'],row['drop_reason'],row['drop_note']),('dropped','lost_thread','вернусь потом'))
        ctx=self.context(bid)
        self.assertEqual(ctx['drop_reason'],self.m.DROP_REASONS['lost_thread']);self.assertEqual(ctx['drop_note'],'вернусь потом')
        with self.assertRaises(HTTPException) as exc:
            self.m.patch_book(bid,self.m.BookPatchSchema(drop_reason='boring'))
        self.assertEqual(exc.exception.status_code,400);self.assertEqual(self.row(bid)['drop_reason'],'lost_thread')
        with self.assertRaises(Exception):
            self.m.BookPatchSchema(drop_note='x'*(self.m.DROP_NOTE_MAX+1))
        self.m.patch_book(bid,self.m.BookPatchSchema(drop_reason='',drop_note=' '))
        self.assertEqual(self.context(bid)['drop_reason'],'причина не указана');self.assertNotIn('drop_note',self.context(bid))
        self.m.patch_book(bid,self.m.BookPatchSchema(drop_reason='disliked'))
        self.m.patch_book(bid,self.m.BookPatchSchema(status='reading'))
        self.assertNotIn('drop_reason',self.context(bid));self.assertEqual(self.row(bid)['drop_reason'],'disliked')
        self.assertIn('«Выпал из чтения» — книга нравилась',self.m.PLAN_PROMPT)
        self.assertIn('«Выпал из чтения» — книга нравилась',self.m.CHOOSE_PROMPT)

    def test_merge_moves_drop_reason_with_status(self):
        source=self.book('/works/OL1W',status='dropped');target=self.book('/works/OL2W',status='unsorted')
        self.m.patch_book(source,self.m.BookPatchSchema(drop_reason='mood',drop_note='летом'))
        self.m.merge_book(source,self.m.MergeSchema(target_id=target))
        self.assertEqual({k:self.row(target)[k] for k in ('status','drop_reason','drop_note')},
                         {'status':'dropped','drop_reason':'mood','drop_note':'летом'})

    def test_migration_adds_drop_reason_columns(self):
        path=Path(self.temp.name)/'legacy-drop.db'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE books (id INTEGER PRIMARY KEY, source_id TEXT, title TEXT, authors TEXT, status TEXT, cover_path TEXT, summary TEXT)')
            db.execute("INSERT INTO books VALUES(1,'/works/old','Old','Author','dropped',NULL,NULL)")
        self.m.ensure_sqlite_schema(str(path))
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute('SELECT status,drop_reason,drop_note FROM books').fetchone(),('dropped',None,None))
            self.assertEqual(db.execute('SELECT title_ru,title_ru_source FROM books').fetchone(),(None,None))

    def test_migration_adds_x3_columns_to_existing_tables(self):
        path=Path(self.temp.name)/'legacy-x3.db'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE books (id INTEGER PRIMARY KEY, source_id TEXT, title TEXT, authors TEXT, status TEXT, cover_path TEXT, summary TEXT)')
            db.execute('CREATE TABLE x3_listing (id INTEGER PRIMARY KEY, checked_at INTEGER NOT NULL, directory TEXT, files TEXT, revision INTEGER)')
            db.execute("INSERT INTO books VALUES(1,'/works/old','Old','Author','read',NULL,NULL)")
            db.execute("INSERT INTO x3_listing VALUES(1,100,'/','[]',7)")
        self.m.ensure_sqlite_schema(str(path));self.m.ensure_sqlite_schema(str(path))
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute('SELECT x3_removed_at FROM books').fetchone(),(None,))
            self.assertEqual(db.execute('SELECT checked_at,revision,positions FROM x3_listing').fetchone(),(100,7,None))


    def link(self, bid, key, filename):
        with self.m.db_session() as db:
            db.add(self.m.ReaderLink(key=key, book_id=bid, snapshot=json.dumps({'filename': filename})))
            db.commit()

    def set_title(self, bid, title):
        with self.m.db_session() as db:
            db.get(self.m.Book, bid).title = title; db.commit()

    def test_clean_title_ru_and_kindle_hints(self):
        self.assertEqual(self.m.clean_title_ru('**«Казино «Руаяль»»**'), 'Казино «Руаяль»')
        self.assertEqual(self.m.clean_title_ru("  'Конец   Вечности' "), 'Конец Вечности')
        self.assertIsNone(self.m.clean_title_ru('Red Rising'));self.assertIsNone(self.m.clean_title_ru(None))
        snap=json.dumps({'filename':'dzhon_skaltci-vojna_starika-63c_ZIJ57NRHRKYJ6XKOGIGPSVITTEONG6NJ.kfx'})
        self.assertEqual(self.m.kindle_file_hints([snap,None,'bad json']),['dzhon_skaltci-vojna_starika-63c'])

    def test_title_translation_fills_skips_and_respects_edits(self):
        plain=self.book('/works/A');self.set_title(plain,'The End of Eternity')
        kindle=self.book('/works/B');self.set_title(kindle,"Old Man's War")
        self.link(kindle,'K'*32,'dzhon_skaltci-vojna_starika-63c_'+'K'*32+'.kfx')
        russian=self.book('/works/C');self.set_title(russian,'Пыль')
        unknown=self.book('/works/D');self.set_title(unknown,'A Pervert\'s World')
        edited=self.book('/works/E');self.set_title(edited,'Wool')
        prompts=[]
        def answer(prompt, schema, model=None):
            prompts.append(prompt)
            self.set_title(edited,'Wool Omnibus')  # правка, пока модель думает
            return schema(books=[{'id':plain,'title_ru':'«Конец Вечности»'},{'id':kindle,'title_ru':'Война старика'},
                                 {'id':unknown,'title_ru':None},{'id':edited,'title_ru':'Укрытие'}])
        with patch.object(self.m,'ai_structured',side_effect=answer):
            self.m.translate_missing_titles()
        self.assertIn('vojna_starika',prompts[0]);self.assertNotIn('Пыль',prompts[0])
        got=lambda b:(self.row(b)['title_ru'],self.row(b)['title_ru_source'])
        self.assertEqual(got(plain),('Конец Вечности','ai'));self.assertEqual(got(kindle),('Война старика','ai'))
        self.assertEqual(got(russian),(None,None));self.assertEqual(got(unknown),(None,'none'))
        self.assertEqual(got(edited),(None,None))
        with patch.object(self.m,'ai_structured',return_value=self.m.TitlesRu()) as ai:
            self.m.translate_missing_titles()
        self.assertIn('Wool Omnibus',ai.call_args[0][0]);self.assertNotIn('Pervert',ai.call_args[0][0])
        self.assertEqual(got(edited),(None,'none'))
        with patch.object(self.m,'ai_structured',side_effect=AssertionError('nothing left to ask')):
            self.m.translate_missing_titles()

    def test_failed_title_translation_leaves_the_book_for_the_next_request(self):
        bid=self.book('/works/A');self.set_title(bid,'Dust')
        with patch.object(self.m,'ai_structured',side_effect=self.m.AIError('down')):
            self.m.translate_missing_titles()
        self.assertEqual(self.row(bid)['title_ru_source'],None)
        with patch.object(self.m,'ai_structured',side_effect=AssertionError('AI only on request')):
            self.m.queue_title_translation()  # синк и правки сами модель не зовут
        with patch.object(self.m,'ai_structured',return_value=self.m.TitlesRu(books=[{'id':bid,'title_ru':'Пыль'}])):
            self.m.translate_missing_titles()
        self.assertEqual((self.row(bid)['title_ru'],self.row(bid)['title_ru_source']),('Пыль','ai'))

    def test_manual_title_ru_and_title_change(self):
        bid=self.book('/works/A');self.set_title(bid,'Dust')
        with self.m.db_session() as db:
            b=db.get(self.m.Book,bid);b.title_ru,b.title_ru_source='Пыль','ai';db.commit()
        with patch.object(self.m,'queue_title_translation') as queue:
            self.m.patch_book(bid,self.m.BookPatchSchema(title='Dust (Silo 3)'))
        queue.assert_called_once()
        self.assertEqual((self.row(bid)['title_ru'],self.row(bid)['title_ru_source']),(None,None))
        self.m.patch_book(bid,self.m.BookPatchSchema(title_ru='  Пыль. Укрытие 3 '))
        self.assertEqual((self.row(bid)['title_ru'],self.row(bid)['title_ru_source']),('Пыль. Укрытие 3','manual'))
        with patch.object(self.m,'queue_title_translation') as queue:
            self.m.patch_book(bid,self.m.BookPatchSchema(title='Dust'))
        queue.assert_not_called();self.assertEqual(self.row(bid)['title_ru'],'Пыль. Укрытие 3')
        self.m.patch_book(bid,self.m.BookPatchSchema(title_ru=''))
        self.assertEqual((self.row(bid)['title_ru'],self.row(bid)['title_ru_source']),(None,'manual'))

    def test_add_book_takes_title_ru_from_ai_search(self):
        bundle=dict(title='The Martian',authors='Andy Weir',year=2011,language='en',description='',cover_urls=[])
        with patch.object(self.m,'fetch_work_bundle',return_value=bundle),patch.object(self.m,'queue_title_translation'):
            res=self.m.add_book(self.m.BookAddSchema(source_id='/works/OL1W',title='The Martian',title_ru='«Марсианин»'),BackgroundTasks())
            ru=self.m.add_book(self.m.BookAddSchema(source_id='/works/OL2W',title_ru='Марсианин'),BackgroundTasks())
        self.assertEqual((self.row(res['id'])['title_ru'],self.row(res['id'])['title_ru_source']),('Марсианин','ai'))
        self.assertEqual(self.row(ru['id'])['title_ru_source'],'ai')
        bundle['title']='Пыль'
        with patch.object(self.m,'fetch_work_bundle',return_value=bundle),patch.object(self.m,'queue_title_translation'):
            res=self.m.add_book(self.m.BookAddSchema(source_id='/works/OL3W',title_ru='Пыль'),BackgroundTasks())
        self.assertEqual((self.row(res['id'])['title_ru'],self.row(res['id'])['title_ru_source']),(None,None))

    def test_ai_search_returns_russian_titles(self):
        self.m.AI_SEARCH_CACHE._data.clear()
        hints=self.m.SearchHints(books=[{'title':'The Martian','author':'Andy Weir','title_ru':'«Марсианин»'}])
        doc={'key':'/works/OL1W','title':'The Martian','author_name':['Andy Weir'],'first_publish_year':2011}
        with patch.object(self.m,'ai_structured',return_value=hints) as ai,patch.object(self.m,'ol_get',return_value={'docs':[doc]}):
            items=self.m.search_ai('марсианин вейр')
        self.assertIn('title_ru',ai.call_args[0][0])
        self.assertEqual((items[0]['title'],items[0]['title_ru']),('The Martian','Марсианин'))

    def test_kindle_file_gives_russian_title_to_existing_english_book(self):
        bid=self.book('/works/OL5734647W');self.set_title(bid,"Old Man's War")
        with self.m.db_session() as db:
            b=db.get(self.m.Book,bid);b.title_ru="Война старика";b.authors="Джон Скальци";db.commit()
        meta=type('R',(),{'json':lambda self:{'title':'Война старика','authors':['Джон Скальци']}})()
        bundle=dict(title="Old Man's War",authors='John Scalzi',year=2005,language='en',description='',cover_urls=[])
        with patch.object(self.m.reader_service,'metadata',return_value=meta.json()), \
             patch.object(self.m,'catalog_match',return_value={'work_key':'/works/OL5734647W','via':'wikidata','aliases':[]}), \
             patch.object(self.m,'fetch_work_bundle',return_value=bundle):
            self.m.import_reader_book('Z'*32,{'filename':'dzhon_skaltci-vojna_starika.kfx'})
        self.assertEqual((self.row(bid)['title_ru'],self.row(bid)['title_ru_source']),('Война старика','kindle'))
        self.assertEqual(self.row(bid)['title'],"Old Man's War")

    def test_merge_and_recommendations_carry_title_ru(self):
        source=self.book('/works/OL1W',status='read');target=self.book('/works/OL2W',status='unsorted')
        with self.m.db_session() as db:
            b=db.get(self.m.Book,source);b.title_ru,b.title_ru_source='Этаж смерти','kindle';db.commit()
        res=self.m.merge_book(source,self.m.MergeSchema(target_id=target))
        self.assertIn('title_ru',res['taken']);self.assertEqual(self.row(target)['title_ru_source'],'kindle')
        with self.m.db_session() as db:
            db.get(self.m.Book,target).status='want';db.commit()
        rid=self.run_record('ready');self.rec(rid,target)
        planned=self.m.get_recommendations()['planned'][0]
        self.assertEqual(planned['title_ru'],'Этаж смерти')
        self.assertEqual(self.m.book_to_recommendation_context(self.m.Book(title='Killing Floor',title_ru='Этаж смерти',status='read'))['title_ru'],'Этаж смерти')


    def import_kindle(self, meta, match, bundle, key='Q'*32):
        response=type('R',(),{'json':lambda self:meta})()
        with patch.object(self.m.reader_service,'metadata',return_value=meta), \
             patch.object(self.m,'catalog_match',return_value=match), \
             patch.object(self.m,'fetch_work_bundle',return_value=bundle):
            self.m.import_reader_book(key,{'filename':'kindle.kfx'})
        with self.m.db_session() as db:
            return db.query(self.m.ReaderLink).filter_by(key=key).one().book_id, db.query(self.m.Book).count()

    def spy(self):
        bid=self.book('/works/OL17893647W','want')
        with self.m.db_session() as db:
            b=db.get(self.m.Book,bid);b.title,b.authors,b.title_ru='Tinker, Tailor, Soldier, Spy','John le Carré','Шпион, выйди вон!'
            db.commit()
        return bid

    def test_surname_key_crosses_scripts(self):
        k=self.m.surname_key
        self.assertEqual(k('Карре'),k('Carré'));self.assertEqual(k('Хайнлайн'),k('Heinlein'));self.assertEqual(k('Скальци'),k('Scalzi'))
        self.assertNotEqual(k('Карре'),k('Scalzi'));self.assertEqual(k('Хауи'),'')

    def test_import_links_same_book_from_another_catalog_record(self):
        bid=self.spy()
        linked,count=self.import_kindle({'title':'Шпион, выйди вон!','authors':['Джон Ле Карре']},
            {'work_key':'/works/OL15723131W','via':'wikidata','aliases':[]},
            dict(title='Tinker Tailor Soldier Spy',authors='John le Carré',year=1974,language='en',description='',cover_urls=[]))
        self.assertEqual((linked,count),(bid,1))

    def test_import_links_by_russian_title_without_catalog(self):
        bid=self.spy()
        linked,count=self.import_kindle({'title':'Шпион, выйди вон!','authors':['Джон Ле Карре']},None,{})
        self.assertEqual((linked,count),(bid,1))

    def test_import_does_not_merge_other_author_or_other_title(self):
        spy=self.spy()
        linked,count=self.import_kindle({'title':'Шпион, выйди вон!','authors':['Иван Петров']},None,{})
        self.assertNotEqual(linked,spy);self.assertEqual(count,2)
        linked,count=self.import_kindle({'title':'Шпион, пришедший с холода','authors':['Джон Ле Карре']},None,{},key='R'*32)
        self.assertNotEqual(linked,spy);self.assertEqual(count,3)

    def test_catalog_rematch_reports_same_book_under_other_record(self):
        spy=self.spy();kindle=self.book('kindle:X','unsorted')
        with self.m.db_session() as db:
            b=db.get(self.m.Book,kindle);b.title,b.authors='Шпион, выйди вон!','Джон Ле Карре';db.commit()
        bundle=dict(title='Tinker Tailor Soldier Spy',authors='John le Carré',year=1974,language='en',description='',cover_urls=[])
        with patch.object(self.m,'catalog_match',return_value={'work_key':'/works/OL15723131W','via':'wikidata','aliases':[]}), \
             patch.object(self.m,'fetch_work_bundle',return_value=bundle):
            res=self.m.catalog_rematch(kindle)
        self.assertEqual((res['status'],res['id']),('duplicate',spy))
        self.assertEqual(self.row(kindle)['source_id'],'kindle:X')


    def x3_env(self, books, files=(), online=True, upload=(200, 'File uploaded successfully'), documents=()):
        class R:
            def __init__(self, code=200, data=None, text='', content=b''):
                self.status_code, self.ok, self._data, self.text, self.content = code, code < 400, data, text, content
            def json(self): return self._data
        calls = {'upload': [], 'bind': []}
        self.m.READER_STATE['library'] = {'books': books}
        def x3(method, path, **kw):
            if path == '/api/status':
                if not online: raise self.m.X3Error('X3 не отвечает')
                return R(data={'version': '1.6.5'})
            if path == '/api/files': return R(data=[{'name': n, 'isDirectory': False, 'isEpub': n.endswith('.epub'), 'size': 1000} for n in files])
            if path == '/upload':
                name, data, _ = kw['files']['file']
                calls['upload'].append((kw['params']['path'], name, data))
                return R(upload[0], text=upload[1])
            raise AssertionError(path)
        def reader(method, path, **kw):
            if path.endswith('/file/epub'): return R(content=b'EPUB')
            if path.endswith('/bind'): calls['bind'].append((path.split('/')[3], kw['json']['key'])); return R(data={})
            if path == '/api/library':  # как reader-sync: связанные документы видны сразу
                return R(data={'books': books, 'documents': [*documents, *({'document': d, 'book_key': k} for d, k in calls['bind'])]})
            raise AssertionError(path)
        return calls, patch.multiple(self.m, X3_ENABLED=True, X3_URL='http://x3', x3_request=MagicMock(side_effect=x3),
                                     reader_service=type('ReaderFake',(),dict(library=staticmethod(lambda:reader('GET','/api/library').json()), file=staticmethod(lambda k,t:MagicMock(read_bytes=lambda:reader('GET',f'/api/books/{k}/file/{t}').content)), bind=staticmethod(lambda d,k:reader('POST',f'/api/documents/{d}/bind',json={'key':k}).json()), upload_epub=staticmethod(lambda k,f:None)))())

    def x3_row(self, key):
        with self.m.db_session() as db:
            i = db.get(self.m.X3Upload, key)
            return i and (i.status, i.filename, i.error, i.attempts)

    def epub_book(self, key, stem, status='ready'):
        return {'key': key, 'filename': f'{stem}_{key}.kfx', 'has_epub': status != 'missing', 'epub_status': status}

    def legacy_queue(self, key, filename):
        with self.m.db_session() as db:
            db.add(self.m.X3Upload(key=key, filename=filename, source='manual', status='queued'));db.commit()

    def test_x3_document_id_is_md5_of_the_file_name(self):
        self.assertEqual(self.m.x3_document_id('piter_uotts-rifteri-1490777777.epub'), 'f4c80c9c6cec9592e3f7819b6bcf86bb')

    def test_attach_incoming_does_not_queue_epub_for_x3(self):
        os.makedirs(self.m.INCOMING_DIR, exist_ok=True)
        with open(self.m.incoming_path('b'*64), 'wb') as f: f.write(b'EPUB')
        with self.m.db_session() as db:
            inc = self.m.IncomingEpub(filename='mik_gerron-mertvie_lvi-65dc48298e2cc.epub', sha256='b'*64, status='waiting')
            db.add(inc);db.commit();inc_id = inc.id
        calls, env = self.x3_env([])
        with env:
            self.assertEqual(self.m.attach_incoming(inc_id, ['K'])['attached'], ['K'])
            self.m.x3_request.assert_not_called()
        self.assertIsNone(self.x3_row('K'))

    def test_legacy_queue_is_never_sent_by_a_sync_pass(self):
        calls, env = self.x3_env([self.epub_book('B', 'b')])
        with env:
            self.legacy_queue('B', 'b.epub')
            self.assertEqual(self.m.reader_sync_once()['status'], 'ok')
            self.m.x3_request.assert_not_called()
            self.assertEqual(self.x3_row('B')[0], 'queued')

    def test_checking_x3_does_not_flush_legacy_queue(self):
        calls, env = self.x3_env([self.epub_book('B', 'b')])
        with env:
            self.legacy_queue('B', 'b.epub')
            self.m.x3_check_now()
            self.assertEqual(calls['upload'], [])
            self.assertEqual(self.x3_row('B')[0], 'queued')
            self.assertEqual(self.m.x3_dequeue('B')['queue'], [])

    def test_x3_routes_are_not_shadowed(self):
        # FastAPI берёт первый подходящий маршрут: /api/reader/{key}/{action} не должен перехватить /x3.
        from starlette.routing import Match
        def endpoint(method, path):
            scope = {'type': 'http', 'path': path, 'method': method, 'root_path': ''}
            return next(r.endpoint for r in self.m.app.router.routes if r.matches(scope)[0] == Match.FULL)
        self.assertIs(endpoint('POST', '/api/reader/'+'A'*32+'/x3'), self.m.reader_send_x3)
        self.assertIs(endpoint('POST', '/api/reader/'+'A'*32+'/reset'), self.m.reader_action)
        self.assertIs(endpoint('POST', '/api/x3/check'), self.m.x3_check_now)
        self.assertIs(endpoint('DELETE', '/api/x3/queue/'+'A'*32), self.m.x3_dequeue)
        self.assertIs(endpoint('POST', '/api/reader/'+'A'*32+'/generate-epub'), self.m.reader_generate_epub)
        self.assertIs(endpoint('DELETE', '/api/reader/documents/55f405a5'), self.m.reader_forget_document)


    def test_x3_file_list_marks_linked_files(self):
        with self.m.db_session() as db:
            db.add(self.m.X3Upload(key='B', filename='old.epub', status='sent'));db.commit()
        calls, env = self.x3_env([], files=['a.epub', 'old.epub', 'other.epub'],
                                 documents=[{'document': self.m.x3_document_id('a.epub'), 'book_key': 'A'}])
        with env:
            res = self.m.x3_file_list()
        self.assertEqual([(f['name'], f['key']) for f in res['files']], [('a.epub', 'A'), ('old.epub', 'B'), ('other.epub', None)])
        calls, env = self.x3_env([], online=False)
        with env, self.assertRaises(HTTPException) as exc:
            self.m.x3_file_list()
        self.assertEqual(exc.exception.status_code, 503)

    def test_x3_device_live_then_saved(self):
        if os.path.exists(self.m.X3_DEVICE_FILE): os.remove(self.m.X3_DEVICE_FILE)
        calls, env = self.x3_env([], files=['a.epub', 'b.epub'], documents=[{'document': 'd1', 'book_key': 'A', 'received_at': 100},
                                                                            {'document': 'd2', 'book_key': 'B', 'received_at': 250}])
        latest = {'tag': '1.6.5', 'published_at': '2026-09-27T09:22:02Z', 'url': 'u'}
        with env, patch.object(self.m, 'x3_latest_release', return_value=latest):
            live = self.m.x3_device()
        self.assertTrue(live['online']);self.assertEqual(live['status']['version'], '1.6.5')
        self.assertEqual(live['last_sync'], 250);self.assertEqual(live['latest'], latest)
        self.assertEqual(live['books'], {'count': 2, 'bytes': 2000})
        calls, env = self.x3_env([], online=False)
        with env, patch.object(self.m, 'x3_latest_release', return_value=None):
            saved = self.m.x3_device()
        self.assertFalse(saved['online']);self.assertEqual(saved['status']['version'], '1.6.5')
        self.assertTrue(saved['last_online'])  # когда был в сети — из сохранённого файла
        self.assertIsNone(saved['books'])

    def test_x3_latest_release_is_cached(self):
        self.m.X3_RELEASE_CACHE._data.clear()
        response = MagicMock(ok=True);response.json.return_value = {'tag_name': '1.6.5', 'published_at': 'p', 'html_url': 'u'}
        with patch.object(self.m.requests, 'get', return_value=response) as get:
            self.assertEqual(self.m.x3_latest_release()['tag'], '1.6.5');self.m.x3_latest_release()
        get.assert_called_once()
        self.m.X3_RELEASE_CACHE._data.clear()
        with patch.object(self.m.requests, 'get', side_effect=self.m.requests.RequestException('offline')) as get:
            self.assertIsNone(self.m.x3_latest_release());self.assertIsNone(self.m.x3_latest_release())
        get.assert_called_once()
        self.m.X3_RELEASE_CACHE._data.clear()


    def test_generate_epub_sends_shelf_title_not_filename(self):
        bid = self.book('/works/A', 'want')
        with self.m.db_session() as db:
            b = db.get(self.m.Book, bid); b.title, b.title_ru, b.authors = 'Tinker, Tailor, Soldier, Spy', 'Шпион, выйди вон!', 'John le Carré'
            db.add(self.m.ReaderLink(key='A'*32, book_id=bid, snapshot='{}')); db.commit()
        with patch.object(self.m.reader_service,'generate_epub',return_value={'status':'generating'}) as generate, patch.object(self.m,'refresh_reader_snapshots'):
            self.assertEqual(self.m.reader_generate_epub('A'*32), {'status':'generating'})
            generate.assert_called_once_with('A'*32,'Шпион, выйди вон!','John le Carré')


    def test_card_snapshots_follow_fresh_reader_data(self):
        bid = self.book('/works/A', 'unsorted')
        old = {'key': 'K', 'percentage': 0.144, 'present': 1, 'has_epub': True, 'epub_status': 'ready', 'last_seen': 1}
        with self.m.db_session() as db:
            db.add(self.m.ReaderLink(key='K', book_id=bid, snapshot=json.dumps(old)));db.commit()
        self.m.READER_STATE['snapshots_at'] = None
        library = lambda **kw: {'books': [{**old, **kw}], 'documents': [], 'applied': []}
        self.assertFalse(self.m.save_card_snapshots(library(last_seen=99)))  # на карточке не видно — не пишем
        self.assertIsNone(self.m.READER_STATE['snapshots_at'])
        self.assertTrue(self.m.save_card_snapshots(library(percentage=0.197, last_seen=99)))
        self.assertIsNotNone(self.m.READER_STATE['snapshots_at'])
        with self.m.db_session() as db:
            self.assertEqual(self.m.load_snapshot(db.get(self.m.ReaderLink, 'K'))['percentage'], 0.197)
        self.assertFalse(self.m.save_card_snapshots(library(percentage=0.197, last_seen=100)))


if __name__=='__main__':unittest.main()
