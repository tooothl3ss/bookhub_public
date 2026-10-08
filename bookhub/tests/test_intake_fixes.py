"""EPUB intake, element-level positions and the sync API. Data lives in a temporary directory."""
import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
CONTAINER = ('<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
             '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
XHTML11 = ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" '
           '"http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n')
SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 800"><image width="600" height="800" href="cover.jpg"/></svg>'
ONE = ['The first chapter opens with a long unique sentence about distant harbours.',
       'A second paragraph keeps  going with entirely different words and a double space.']
THREE = ['The third section begins after the picture with yet another distinct line.',
         'Its closing paragraph is the very last visible text of this whole book.']


def page(body, head=''):
    return head + '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head>' + body + '</html>'


def kfx_page(paragraphs):
    # What kfxlib writes: every element on its own line, so body.text is "\n".
    return page('<body class="b">\n<div class="d">\n' + ''.join(f'<p class="p">{p}</p>\n' for p in paragraphs) + '</div>\n</body>')


def make_epub(items, title='Intake', extra=None, container=CONTAINER, opf=True):
    """items: (href, media type, content or None for a file the ZIP lacks) in spine order; extra: other ZIP members."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr(zipfile.ZipInfo('mimetype'), 'application/epub+zip')
        if container:
            z.writestr(zipfile.ZipInfo('META-INF/container.xml'), container)
        manifest = ''.join(f'<item id="i{n}" href="{href}" media-type="{kind}"/>' for n, (href, kind, _) in enumerate(items))
        spine = ''.join(f'<itemref idref="i{n}"/>' for n in range(len(items)))
        if opf:
            z.writestr(zipfile.ZipInfo('OEBPS/content.opf'), '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
                       f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>{title}</dc:title><dc:creator>Author</dc:creator>'
                       f'</metadata><manifest>{manifest}</manifest><spine>{spine}</spine></package>')
        for href, _, data in items:
            if data is not None:
                z.writestr(zipfile.ZipInfo('OEBPS/' + href), data)
        for name, data in (extra or {}).items():
            z.writestr(zipfile.ZipInfo(name), data)
    return buf.getvalue()


def text_epub(text, title='Intake'):
    return make_epub([('c.xhtml', 'application/xhtml+xml', page(f'<body><p>{text}</p></body>'))], title)


def kfx_epub(title='Generated'):
    """Text, a page with only a picture, text, and a last page with only a picture."""
    picture = page('<body class="b">\n<div class="d">\n<img src="i.jpg" alt=""/>\n</div>\n</body>')
    return make_epub([('c1.xhtml', 'application/xhtml+xml', kfx_page(ONE)), ('c2.xhtml', 'application/xhtml+xml', picture),
                      ('c3.xhtml', 'application/xhtml+xml', kfx_page(THREE)), ('c4.xhtml', 'application/xhtml+xml', picture)], title)


def kfx_chunks(sections):
    chunks, pid = [], 0
    for number, paragraphs in enumerate(sections):
        for text in paragraphs:
            chunks.append(dict(pid=pid, eid=100 + len(chunks), offset=0, length=len(text), text=text, section=f'c{number}'))
            pid += len(text)
    return chunks


class IntakeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, BOOKHUB_DATA=cls.temp.name, BOOKHUB_DB=str(Path(cls.temp.name) / 'bookhub.sqlite3'),
                             AI_URL='', AI_MODEL='', SYNC_API_KEY='token', SYNC_USER='reader', SYNC_PASSWORD='password')
        cls.env.start()
        # shelf binds its database when imported: a private copy keeps bookhub.shelf free for the other test modules.
        spec = importlib.util.spec_from_file_location('bookhub.shelf_intake_tests', ROOT / 'bookhub/shelf.py')
        cls.shelf = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.shelf
        spec.loader.exec_module(cls.shelf)
        cls.shelf.initialize_storage()
        from bookhub.sync.api import create_app
        from bookhub.sync.service import get_store
        from bookhub.web_epub import register
        cls.store = get_store()
        register(cls.shelf.app)
        cls.web = TestClient(cls.shelf.app)
        cls.sync = TestClient(create_app(store=cls.store))
        cls.bearer = {'Authorization': 'Bearer token'}
        cls.ko = {'x-auth-user': 'reader', 'x-auth-key': hashlib.md5(b'password').hexdigest()}

    @classmethod
    def tearDownClass(cls):
        cls.web.close();cls.sync.close()
        cls.shelf.engine.dispose()
        del sys.modules['bookhub.shelf_intake_tests']
        cls.env.stop();cls.temp.cleanup()

    def setUp(self):
        self.network = patch('requests.sessions.Session.request', side_effect=AssertionError('Unexpected external request'))
        self.network.start();self.addCleanup(self.network.stop)
        with self.store.connect() as db:
            db.execute('PRAGMA foreign_keys=OFF')
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
                db.execute('DELETE FROM "' + row['name'] + '"')

    def add(self, data, filename='book.epub'):
        r = self.web.post('/api/epubs', params={'filename': filename}, content=data)
        self.assertEqual(200, r.status_code, r.text)
        return r.json()

    def index(self, book):
        from bookhub.engine.epub_xpath import EpubIndex
        return EpubIndex(self.store.core.asset(book['book_id'])['path'])

    def push(self, xpath, filename='book.epub', percentage=.1):
        r = self.sync.put('/syncs/progress', headers=self.ko, json=dict(
            document=hashlib.md5(filename.encode()).hexdigest(), progress=xpath, percentage=percentage, device='CrossPoint', device_id='x3'))
        self.assertEqual(200, r.status_code, r.text)
        return r.json()

    def kindle(self, key, data, sections, book_id=None):
        """A Kindle book with a ready map and this EPUB, linked to a shelf card when book_id is given."""
        from bookhub.engine.kindle_map import PositionMap, write_map
        from bookhub.sync.jobs import run_next
        chunks = kfx_chunks(sections)
        mapping = PositionMap(dict(version=1, book=dict(key=key), total_positions=sum(c['length'] for c in chunks), chunks=chunks))
        host = self.store.root / 'maps' / (key + '.json');compact = self.store.root / 'maps' / (key + '.posmap')
        write_map(mapping, host, compact)
        self.store.inventory('kindle', [dict(key=key, fingerprint='a' * 64)])
        with self.store.connect(write=True) as db:
            db.execute("UPDATE kindle_books SET map_path=?,compact_path=?,map_status='ready',asset_version=1 WHERE key=?", (str(host), str(compact), key))
            if book_id:
                db.execute('INSERT INTO reader_links(key,book_id) VALUES(?,?)', (key, book_id))
        r = self.sync.put(f'/api/books/{key}/epub', headers=self.bearer, content=data)
        self.assertEqual(200, r.status_code, r.text)
        self.assertTrue(run_next(self.store));self.assertEqual('ready', self.store.book(key)['epub_status'])
        return chunks

    # E1: one intake

    def test_xhtml11_entities_are_the_characters_a_reader_shows(self):
        body = '<body><p>Он&nbsp;сказал&nbsp;&mdash; &laquo;пора&raquo;, и&nbsp;долгая дорога через северный океан началась.</p></body>'
        shown = 'Он\xa0сказал\xa0— «пора», и\xa0долгая дорога через северный океан началась.'
        for name, head in (('epub2', XHTML11), ('epub3', '<!DOCTYPE html>\n')):
            with self.subTest(name):
                b = self.add(make_epub([('c.xhtml', 'application/xhtml+xml', page(body, head))], name), name + '.epub')
                offset = shown.index('долгая')
                point = self.index(b).resolve(f'/body/DocFragment[1]/body/p[1]/text()[1].{offset}')
                self.assertEqual(shown, point['text_node'])
                self.assertTrue(point['context_after'].startswith('долгая дорога'))
                answer = self.push(f'/body/DocFragment[1]/body/p[1]/text().{offset}', name + '.epub')
                self.assertTrue(answer['accepted'], answer)

    def test_spine_item_without_text_keeps_its_docfragment_number(self):
        chapter = page('<body><p>The only chapter of this short book starts right after its cover.</p></body>')
        for name, kind, cover in (('cover.svg', 'image/svg+xml', SVG), ('cover.jpg', 'image/jpeg', b'\xff\xd8\xff\xe0\x00\x10JFIF\x00'),
                                  ('cover.xhtml', 'application/xhtml+xml', page(SVG)), ('undeclared.svg', '', SVG),
                                  ('undeclared.jpg', '', b'\xff\xd8\xff\xe0\x00\x10JFIF\x00'), ('lost.xhtml', 'application/xhtml+xml', None)):
            with self.subTest(name):
                b = self.add(make_epub([(name, kind, cover), ('c.xhtml', 'application/xhtml+xml', chapter)], name), name + '.epub')
                index = self.index(b)
                self.assertEqual(2, len(index.spine))
                text = index.resolve('/body/DocFragment[2]/body/p[1]/text()[1].4')
                self.assertEqual((1, 'only chapter'), (text['spine_index'], text['context_after'][:12]))
                self.assertTrue(text['spine_file'].endswith('c.xhtml'))
                with self.assertRaises(ValueError):index.resolve('/body/DocFragment[1]/body/p[1]/text()[1].4')
                # The cover page is a position too: the reader is at the start of the book.
                cover_page = index.resolve('/body/DocFragment[1]/body')
                self.assertEqual((0, 'The only'), (cover_page['spine_index'], cover_page['context_after'][:8]))
                self.assertTrue(self.push('/body/DocFragment[1]/body', name + '.epub')['accepted'])
                self.assertTrue(self.push('/body/DocFragment[2]/body/p[1]/text()[1].4', name + '.epub')['accepted'])
                self.assertEqual('behind', self.push('/body/DocFragment[1]/body', name + '.epub')['reason'])

    def test_not_well_formed_markup_is_accepted_and_only_a_lost_position_is_pending(self):
        broken = '<html><head><meta charset="utf-8"><title>t</title></head><body><p>AT&T sells wires & poles<br>on the second line' \
                 '</p><p>This paragraph is never closed and the reader shows it anyway.<p class=x>Unquoted'
        with self.assertLogs(level='WARNING') as logs:
            b = self.add(make_epub([('c.xhtml', 'application/xhtml+xml', broken)], 'broken'), 'broken.epub')
        self.assertTrue(any('not well-formed' in line for line in logs.output), logs.output)
        index = self.index(b)
        self.assertEqual('AT&T sells wires & poles', index.resolve('/body/DocFragment[1]/body/p[1]/text()[1].0')['text_node'])
        self.assertEqual('on the second line', index.resolve('/body/DocFragment[1]/body/p[1]/text()[2].0')['text_node'])
        self.assertTrue(self.push('/body/DocFragment[1]/body/p[2]/text().5', 'broken.epub')['accepted'])
        answer = self.push('/body/DocFragment[1]/body/p[9]/text().0', 'broken.epub')
        self.assertTrue(answer['pending']);self.assertIn('not found', answer['error'])
        self.assertEqual('/body/DocFragment[1]/body/p[2]/text().5', self.store.core.files(b['book_id'])[0]['xpath'])

    def test_declared_entities_are_never_expanded(self):
        secret = Path(self.temp.name) / 'secret.txt';secret.write_text('TOP-SECRET-CONTENT')
        bomb = ''.join(f'<!ENTITY e{n} "{("&e%d;" % (n - 1)) * 10}">' for n in range(1, 10))
        head = f'<!DOCTYPE html [<!ENTITY e0 "boom"><!ENTITY xxe SYSTEM "file://{secret}">{bomb}]>'
        body = '<body><p title="&xxe;&e9;">Before the references &xxe; and &e9; the text stays what the file literally says.</p></body>'
        b = self.add(make_epub([('c.xhtml', 'application/xhtml+xml', page(body, head))], 'entities'), 'entities.epub')
        text = self.index(b).text
        self.assertIn('references &xxe; and &e9; the text', text)
        self.assertNotIn('TOP-SECRET', text);self.assertNotIn('boom', text);self.assertLess(len(text), 200)

    def test_unreadable_and_protected_files_are_still_refused(self):
        chapter = [('c.xhtml', 'application/xhtml+xml', page('<body><p>Readable text that would otherwise be accepted.</p></body>'))]
        drm = ('<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" xmlns:enc="http://www.w3.org/2001/04/xmlenc#">'
               '<enc:EncryptedData><enc:EncryptionMethod Algorithm="{}"/><enc:CipherData><enc:CipherReference URI="OEBPS/{}"/>'
               '</enc:CipherData></enc:EncryptedData></encryption>')
        refused = {
            'not a zip': b'PK\x03\x04 this only looks like an archive',
            'no container': make_epub(chapter, container=None),
            'no opf': make_epub(chapter, opf=False),
            'too many files': make_epub(chapter, extra={f'OEBPS/pad{n}': b'' for n in range(10000)}),
            'drm': make_epub(chapter, extra={'META-INF/encryption.xml': drm.format('http://www.w3.org/2001/04/xmlenc#aes128-cbc', 'c.xhtml')}),
        }
        for name, data in refused.items():
            with self.subTest(name):
                r = self.web.post('/api/epubs', params={'filename': 'bad.epub'}, content=data)
                self.assertEqual(400, r.status_code, r.text);self.assertIn('EPUB не читается', r.json()['detail'])
        self.assertEqual([], self.web.get('/api/books').json())
        # Obfuscated fonts are not DRM: every reader opens such a book.
        fonts = make_epub(chapter, 'fonts', extra={'META-INF/encryption.xml': drm.format('http://www.idpf.org/2008/embedding', 'font.otf')})
        self.assertTrue(self.add(fonts, 'fonts.epub')['book_id'])

    # E1: replacing the EPUB of a Kindle book

    def test_refused_replacement_keeps_the_previous_epub_working(self):
        text = 'A unique paragraph about sailing across the northern ocean and watching distant mountains.'
        good = text_epub(text, 'kept');b = self.add(good, 'kept.epub')
        self.kindle('KEPT', good, [[text]], b['book_id'])
        before = self.store.book('KEPT')
        bad = b'PK\x03\x04 not an EPUB at all'
        r = self.web.put('/api/reader/KEPT/epub', content=bad)
        self.assertEqual(400, r.status_code, r.text);self.assertIn('EPUB не читается', r.json()['detail'])
        r = self.sync.put('/api/books/KEPT/epub', headers=self.bearer, content=bad)
        self.assertEqual(400, r.status_code, r.text);self.assertIn('EPUB не читается', r.json()['error'])
        from bookhub.sync.service import reader_service
        from bookhub.sync.store import Problem
        with self.assertRaises(Problem) as refused:reader_service.upload_epub('KEPT', io.BytesIO(bad))
        self.assertEqual(400, refused.exception.status)
        after = self.store.book('KEPT')
        self.assertEqual((before['epub_path'], 'ready', None), (after['epub_path'], after['epub_status'], after['epub_error']))
        self.assertEqual([], list((self.store.root / 'tmp').iterdir()))
        self.assertFalse((self.store.root / 'books/KEPT/epub' / hashlib.sha256(bad).hexdigest()).exists())
        with self.store.connect() as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM jobs WHERE status!='done'").fetchone()[0])
        # The exchange is alive in both directions.
        self.assertEqual(b['sha256'], self.store.core.attach_kindle('KEPT')['sha256'])
        r = self.sync.put('/api/books/KEPT/position', headers=self.bearer, json={'pid': 9, 'asset_version': 1})
        self.assertEqual((True, 'mapped'), (r.json()['accepted'], r.json()['bridge_status']), r.text)
        self.assertTrue(self.push('/body/DocFragment[1]/body/p[1]/text()[1].40', 'kept.epub')['accepted'])
        self.assertEqual(40, self.store.book('KEPT')['pid'])

    def test_job_failing_later_returns_to_the_last_ready_epub(self):
        from bookhub.sync.jobs import run_next
        old = 'The old edition has this unique paragraph about a lighthouse keeper and his dog.'
        new = old + ' The new edition adds a sentence about a harbour pilot after it.'
        self.kindle('BACK', text_epub(old, 'old'), [[old]])
        first = self.store.book('BACK')['epub_path']
        self.store.update_position('BACK', 5, 'kindle', 1)
        for name in ('second', 'third'):  # two uploads in a row: the first of them never became ready
            self.assertEqual(200, self.sync.put('/api/books/BACK/epub', headers=self.bearer, content=text_epub(new, name)).status_code)
        self.assertNotEqual(first, self.store.book('BACK')['epub_path'])
        check = self.store.index

        def gone(path):  # the new files are lost between the upload and the job
            if path != first:raise OSError('disk went away')
            return check(path)
        with patch.object(type(self.store), 'index', side_effect=gone), self.assertLogs(level='ERROR'):
            while run_next(self.store):pass
        b = self.store.book('BACK')
        self.assertEqual((first, 'ready', 'Новый EPUB не принят, оставлен прежний: disk went away'), (b['epub_path'], b['epub_status'], b['epub_error']))
        self.assertEqual(('mapped', '/body/DocFragment[1]/body/p[1]/text()[1].5'), (b['bridge_status'], b['xpath']))
        # The same when the file is fine and the job fails on what follows the check.
        self.assertEqual(200, self.sync.put('/api/books/BACK/epub', headers=self.bearer, content=text_epub(new, 'fourth')).status_code)
        with patch.object(self.store, 'refresh', side_effect=[RuntimeError('map is gone'), None]), self.assertLogs(level='ERROR'):
            self.assertTrue(run_next(self.store))
        b = self.store.book('BACK')
        self.assertEqual((first, 'ready'), (b['epub_path'], b['epub_status']));self.assertIn('map is gone', b['epub_error'])
        self.assertEqual(409, self.sync.post('/api/books/BACK/generate-epub', headers=self.bearer, json={}).status_code)  # it has an EPUB

    def test_job_failing_without_a_previous_epub_frees_the_book(self):
        from bookhub.sync.jobs import run_next
        from bookhub.sync.store import Store
        self.store.inventory('kindle', [dict(key='FREE', fingerprint='a' * 64)])
        source = self.store.root / 'books/FREE/kfx/hash/book.kfx';source.parent.mkdir(parents=True);source.write_bytes(b'KFX')
        legacy = self.store.root / 'books/FREE/epub/hash/book.epub';legacy.parent.mkdir(parents=True);legacy.write_bytes(b'refused by an earlier version')
        with self.store.connect(write=True) as db:
            # What the earlier code left behind: the path on a refused file, nothing can replace it.
            db.execute("UPDATE kindle_books SET kfx_path=?,epub_path=?,epub_status='error',epub_error='old' WHERE key='FREE'", (str(source), str(legacy)))
        Store(self.store.root, database=self.store.db)
        self.assertEqual('queued', self.store.book('FREE')['epub_status'])
        with self.assertLogs(level='ERROR'):self.assertTrue(run_next(self.store))
        b = self.store.book('FREE')
        self.assertEqual((None, 'error'), (b['epub_path'], b['epub_status']));self.assertIn('not a zip', b['epub_error'])
        self.assertEqual('generating', self.store.request_epub('FREE')['status'])
        # A generated file passes the same check before it becomes the book's EPUB.
        with patch('bookhub.sync.jobs.build_epub', return_value=b'kfxlib produced garbage'), self.assertLogs(level='ERROR'):
            self.assertTrue(run_next(self.store))
        b = self.store.book('FREE')
        self.assertEqual((None, 'error'), (b['epub_path'], b['epub_status']));self.assertIn('EPUB не читается', b['epub_error'])

    def test_replacement_with_another_text_is_refused_by_the_map(self):
        data = kfx_epub('same');b = self.add(data, 'same.epub')
        self.kindle('MATCH', data, [ONE, THREE], b['book_id'])  # what kfxlib writes passes the check as an upload too
        before = self.store.book('MATCH')
        other = text_epub('A wholly different book about gardening in a cold climate, without one sentence in common.', 'other')
        r = self.web.put('/api/reader/MATCH/epub', content=other)
        self.assertEqual((400, 'EPUB не совпадает с книгой Kindle: совпало 0 из 32 проверенных мест'), (r.status_code, r.json()['detail']))
        r = self.sync.put('/api/books/MATCH/epub', headers=self.bearer, content=other)
        self.assertEqual(400, r.status_code, r.text);self.assertIn('EPUB не совпадает с книгой Kindle', r.json()['error'])
        after = self.store.book('MATCH')
        self.assertEqual((before['epub_path'], 'ready', None), (after['epub_path'], after['epub_status'], after['epub_error']))
        self.assertFalse((self.store.root / 'books/MATCH/epub' / hashlib.sha256(other).hexdigest()).exists())
        self.assertTrue(self.push('/body/DocFragment[3]/body/div/p[2].0', 'same.epub')['accepted'])
        self.assertEqual(len(''.join(ONE + THREE[:1])), self.store.book('MATCH')['pid'])
        # The same text in other markup — the EPUB the Kindle book was made from — is the same book.
        source = make_epub([('all.xhtml', 'application/xhtml+xml', page('<body>' + ''.join(f'<p>{p}</p>' for p in ONE + THREE) + '</body>'))], 'source')
        self.assertEqual(200, self.web.put('/api/reader/MATCH/epub', content=source).status_code)

    # E5

    def test_generation_overtaken_by_a_new_kfx_can_be_retried(self):
        from bookhub.sync.jobs import run_next
        self.store.inventory('kindle', [dict(key='AGAIN', fingerprint='a' * 64)])
        old = self.store.root / 'old.kfx';old.write_bytes(b'KFX1')
        new = self.store.root / 'new.kfx';new.write_bytes(b'KFX2')
        with self.store.connect(write=True) as db:
            db.execute("UPDATE kindle_books SET kfx_path=? WHERE key='AGAIN'", (str(old),))
        self.store.request_epub('AGAIN')
        text = 'Generated text with enough distinct words to be a paragraph of its own.'

        def overtaken(*args):
            with self.store.connect(write=True) as db:
                db.execute("UPDATE kindle_books SET kfx_path=? WHERE key='AGAIN'", (str(new),))
            return text_epub(text, 'stale')
        with patch('bookhub.sync.jobs.build_epub', side_effect=overtaken):
            self.assertTrue(run_next(self.store))
        b = self.store.book('AGAIN')
        self.assertEqual((None, 'missing'), (b['epub_path'], b['epub_status']))  # reconcile requests it again from this state
        self.store.request_epub('AGAIN')
        with patch('bookhub.sync.jobs.build_epub', return_value=text_epub(text, 'fresh')) as build:
            while run_next(self.store):pass
        self.assertEqual(str(new), build.call_args.args[0])
        self.assertEqual('ready', self.store.book('AGAIN')['epub_status'])

    # E2: element-level positions

    def test_element_positions_resolve_to_visible_text_in_reading_order(self):
        index = self.index(self.add(kfx_epub('elements'), 'elements.epub'))
        first = '/body/DocFragment[{}]/body/div[1]/p[1]/text()[1].0'
        for xpath, expected in {
            '/body/DocFragment[1]/body': first.format(1),                       # body.text is "\n"
            '/body/DocFragment[1]/body.0': first.format(1),
            '/body/DocFragment/body/div/p.0': first.format(1),
            '/body/DocFragment[1]/body/div/p[2].0': '/body/DocFragment[1]/body/div[1]/p[2]/text()[1].0',
            '/body/DocFragment[2]/body': first.format(3),                       # a page with a picture only
            '/body/DocFragment[2].0': first.format(3),
            '/body/DocFragment[2]/body/div/img.0': first.format(3),
            '/body/DocFragment[2]/body/div[1]/img[1]': first.format(3),
            '/body/DocFragment[3]/body/div': first.format(3),
            '/body/DocFragment[4]/body': f'/body/DocFragment[3]/body/div[1]/p[2]/text()[1].{len(THREE[1]) - 1}',  # nothing after it
        }.items():
            with self.subTest(xpath):self.assertEqual(expected, index.resolve(xpath)['xpath'])
        with self.assertRaises(ValueError):index.resolve('/body/DocFragment[2]/body/div/img[2]')
        with self.assertRaises(ValueError):index.resolve('/body/DocFragment[5]/body')
        reading_order = ['/body/DocFragment[1]/body', '/body/DocFragment[1]/body/text()[1].0', '/body/DocFragment[1]/body/div',
                         '/body/DocFragment[1]/body/div/p[1].0', '/body/DocFragment[1]/body/div/p[1]/text().0',
                         '/body/DocFragment[1]/body/div/p[1]/text().30', '/body/DocFragment[1]/body/div/text()[2].0',
                         '/body/DocFragment[1]/body/div/p[2]', '/body/DocFragment[1]/body/div/p[2]/text().70',
                         '/body/DocFragment[1]/body/text()[2].1', '/body/DocFragment[2]/body', '/body/DocFragment[2]/body/div/text()[1].0',
                         '/body/DocFragment[2]/body/div/img.0', '/body/DocFragment[2]/body/div/text()[2].1', '/body/DocFragment[3]/body',
                         '/body/DocFragment[3]/body/div/p[1]/text().0', '/body/DocFragment[3]/body/div/p[2]/text().9', '/body/DocFragment[4]/body',
                         '/body/DocFragment[4]/body/div/img']
        ordinals = [(point['spine_index'], point['raw_index']) for point in map(index.resolve, reading_order)]
        self.assertEqual(sorted(ordinals), ordinals)
        self.assertEqual([0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 3, 3], [spine for spine, _ in ordinals])

    def test_element_and_whitespace_positions_translate_to_kindle(self):
        from bookhub.engine.kindle_map import PositionMap
        from bookhub.engine.position_bridge import PositionBridge
        chunks = kfx_chunks([ONE, THREE])
        mapping = PositionMap(dict(version=1, book=dict(key='BRIDGE'), total_positions=sum(c['length'] for c in chunks), chunks=chunks))
        bridge = PositionBridge(self.index(self.add(kfx_epub('bridge'), 'bridge.epub')), mapping)
        self.assertIsNone(bridge.pages)
        gap = ONE[1].index('  ') + 1  # the second of two spaces
        for xpath, pid in {
            '/body/DocFragment[1]/body': 0,
            '/body/DocFragment[1]/body/text()[1].0': 0,                                   # the "\n" before the first element
            '/body/DocFragment[1]/body/div/p[2].0': len(ONE[0]),
            f'/body/DocFragment[1]/body/div/p[2]/text().{gap}': len(ONE[0]) + gap + 1,
            f'/body/DocFragment[1]/body/div/p[2]/text().{len(ONE[1])}': chunks[2]['pid'],     # end of the chapter's last text node
            '/body/DocFragment[2]/body': chunks[2]['pid'],
            '/body/DocFragment[2]/body/div/img.0': chunks[2]['pid'],
            '/body/DocFragment[3]/body/div/p[1]/text().4': chunks[2]['pid'] + 4,
            '/body/DocFragment[4]/body': mapping.total - 1,
        }.items():
            with self.subTest(xpath):
                result = bridge.to_kindle(xpath)
                self.assertEqual(('mapped', True, pid), (result['status'], result.get('roundtrip_verified'), result['matches'][0]['pid']), result)
        # A Kindle position on a space still comes back as the same position.
        space = len(ONE[0]) + ONE[1].index(' ')
        self.assertEqual(('mapped', True), (bridge.to_epub(space)['status'], bridge.to_epub(space).get('roundtrip_verified')))

    def test_crosspoint_page_offsets_reach_the_kindle(self):
        data = kfx_epub('offsets');b = self.add(data, 'offsets.epub')
        chunks = self.kindle('OFFSETS', data, [ONE, THREE], b['book_id'])
        index = self.index(b);kindle = self.store.book('OFFSETS')
        bridge = self.store.bridge(kindle['epub_path'], kindle['map_path'])
        first = 2  # the "\n" after <body> and the one after <div> precede the first paragraph
        second = first + len(ONE[0]) + 1
        last = sum(c['length'] for c in chunks) - 1
        for position, pid in {
            (0,): 0, (0, 0): 0, (0, first + 4): 4,
            (0, second): len(ONE[0]), (0, second + 12): len(ONE[0]) + 12,
            (1,): chunks[2]['pid'], (1, 7): chunks[2]['pid'],  # a page with a picture only
            (2, first + 4): chunks[2]['pid'] + 4,
            (2, None, .999): last, (3,): last, (4,): last,  # the book's last character, its last picture, finished
        }.items():
            with self.subTest(position):
                point = index.xpath_for_spine(*position)
                result = bridge.to_kindle(point['xpath'])
                self.assertEqual(('mapped', True, pid), (result['status'], result.get('roundtrip_verified'), result['matches'][0]['pid']), result)
        # Taken into the library like the position of any reader: forward only, and on to the Kindle.
        accept = lambda *position:self.store.core.accept(b['book_id'], b['sha256'], *index.xpath_for_spine(*position).values(), 'X3')
        self.assertTrue(accept(0, second + 12)['accepted'])
        self.assertEqual((len(ONE[0]) + 12, 'epub'), (self.store.book('OFFSETS')['pid'], self.store.book('OFFSETS')['source']))
        self.assertEqual('behind', accept(0, first + 4)['reason'])
        self.assertEqual('unchanged', self.push('/body/DocFragment[1]/body/div/p[2]/text().12', 'offsets.epub')['reason'])
        answer = accept(4)
        self.assertEqual((True, 1.0), (answer['accepted'], answer['percentage']));self.assertEqual(last, self.store.book('OFFSETS')['pid'])

    def test_x3_page_without_text_reaches_the_kindle(self):
        data = kfx_epub('server');b = self.add(data, 'server.epub')
        chunks = self.kindle('PAGES', data, [ONE, THREE], b['book_id'])
        answer = self.push('/body/DocFragment[2]/body', 'server.epub', .5)
        self.assertTrue(answer['accepted'], answer)
        book = self.store.book('PAGES')
        self.assertEqual((chunks[2]['pid'], 'epub', 'mapped'), (book['pid'], book['source'], book['bridge_status']))
        self.assertEqual('behind', self.push('/body/DocFragment[1]/body/div/p[2].0', 'server.epub', .2)['reason'])
        self.assertTrue(self.push('/body/DocFragment[3]/body/div/p[2].0', 'server.epub', .8)['accepted'])
        self.assertEqual(chunks[3]['pid'], self.store.book('PAGES')['pid'])


class SyncAuthTests(unittest.TestCase):
    def client(self, key, user, password):
        from bookhub.sync.api import create_app
        temp = tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        client = TestClient(create_app(temp.name, key, user, password));self.addCleanup(client.close)
        return client

    def test_non_ascii_credentials_are_unauthorized_not_a_server_error(self):
        client = self.client('token', 'reader', 'password')
        basic = lambda pair:{'Authorization': 'Basic ' + base64.b64encode(pair.encode()).decode()}
        for headers in ({'Authorization': 'Bearer ключ'.encode()}, {'Authorization': 'Bearer t\xf6ken'.encode('latin-1')},
                        basic('reader:пароль'), basic('читатель:password'), {'Authorization': b'Basic \xff\xfe'}):
            self.assertEqual(401, client.get('/api/library', headers=headers).status_code, headers)
        for headers in ({'x-auth-user': 'читатель'.encode(), 'x-auth-key': hashlib.md5(b'password').hexdigest()},
                        {'x-auth-user': 'reader', 'x-auth-key': 'ключ'.encode()}):
            self.assertEqual(401, client.get('/users/auth', headers=headers).status_code, headers)
        for password in ('пароль', '\ud800'):
            body = json.dumps({'username': 'reader', 'password': password}).encode()
            self.assertEqual(401, client.post('/users/create', content=body).status_code)
        self.assertEqual(200, client.get('/api/library', headers={'Authorization': 'Bearer token'}).status_code)
        self.assertEqual(200, client.get('/api/library', headers=basic('reader:password')).status_code)

    def test_non_ascii_configured_credentials_work(self):
        client = self.client('ключ', 'читатель', 'пароль')
        digest = hashlib.md5('пароль'.encode()).hexdigest()
        self.assertEqual(200, client.get('/api/library', headers={'Authorization': 'Bearer ключ'.encode()}).status_code)
        self.assertEqual(200, client.get('/api/library', auth=('читатель', 'пароль')).status_code)
        self.assertEqual(200, client.get('/users/auth', headers={'x-auth-user': 'читатель'.encode(), 'x-auth-key': digest}).status_code)
        self.assertEqual(201, client.post('/users/create', json={'username': 'читатель', 'password': digest}).status_code)
        self.assertEqual(401, client.get('/api/library', headers={'Authorization': 'Bearer ключи'.encode()}).status_code)


if __name__ == '__main__':
    unittest.main()
