"""OPDS catalogue: the sync password, feeds as the X3 firmware (CrossPoint 1.6.5) reads them, file names
by its rule, what a download binds, the X3 mark, the menu of the shelf. Temporary data, no network.

Feed, build_url, sanitize_filename and opds_book_filename below are ports of the firmware source,
kept apart from the server's own code: the catalogue is checked against what the reader does."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from xml.etree import ElementTree
from xml.parsers import expat
from xml.sax.saxutils import escape
import zipfile

from fastapi.testclient import TestClient

XPATH = '/body/DocFragment[1]/body/p[1]/text()[1].'
AUTH = ('reader','password')
ATOM = '{http://www.w3.org/2005/Atom}'
LAYOUTS = ('author-title','title-author','title')  # OpdsFilenameFormat 0, 1, 2

def epub(title,author='Автор',text=''):
    buf=io.BytesIO()
    creator=f'<dc:creator>{escape(author)}</dc:creator>' if author else ''
    with zipfile.ZipFile(buf,'w') as z:
        z.writestr(zipfile.ZipInfo('META-INF/container.xml'),'<container><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>')
        z.writestr(zipfile.ZipInfo('book.opf'),f'<package xmlns:dc="http://purl.org/dc/elements/1.1/"><metadata><dc:title>{escape(title)}</dc:title>{creator}</metadata><manifest><item id="ch" href="ch.xhtml"/></manifest><spine><itemref idref="ch"/></spine></package>')
        z.writestr(zipfile.ZipInfo('ch.xhtml'),'<html><body><p>A paragraph about sailing across the northern ocean and watching distant mountains in the morning. '+text+'</p></body></html>')
    return buf.getvalue()

def md5(name):
    return hashlib.md5(name.encode()).hexdigest()

def forget_shelf():
    # bookhub.shelf binds its paths and engine at import: this class has its own data directory.
    import bookhub
    for name in ('shelf','application','web_epub','delivery','description','worker'):
        sys.modules.pop('bookhub.'+name,None)
        if hasattr(bookhub,name):delattr(bookhub,name)


class Feed:
    """A feed as the reader takes it: lib/OpdsParser/OpdsParser.cpp, handler for handler. Elements are
    known by name or by a ":name" ending, limits are bytes, the last fitting link of an entry wins."""
    def __init__(self,data):
        self.entries=[];self.search='';self.next='';self.previous=''
        self.entry=None;self.collect=False;self.text=b'';self.in_entry=self.in_title=self.in_author=self.in_name=self.in_id=False
        parser=expat.ParserCreate()  # XML_ParserCreate(nullptr): no namespace processing
        parser.StartElementHandler=self.start;parser.EndElementHandler=self.end;parser.CharacterDataHandler=self.characters
        parser.Parse(data,True)

    @staticmethod
    def named(name,what):
        return name==what or ':'+what in name

    def start(self,name,attrs):
        if self.named(name,'entry'):
            self.in_entry=True;self.collect=len(self.entries)<62
            self.entry=dict(type='navigation',title=b'',author=b'',href='',id=b'');self.text=b''
            self.in_title=self.in_author=self.in_name=self.in_id=False
            return
        if self.named(name,'link') and 'href' in attrs:
            href,rel,kind=attrs['href'].encode()[:768].decode(),attrs.get('rel'),attrs.get('type')
            if rel=='search':
                if '{searchTerms}' in href:self.search=href
            elif rel=='next' and not self.in_entry:self.next=href
            elif rel=='previous' and not self.in_entry:self.previous=href
            if self.in_entry and self.collect:
                e=self.entry;plain=lambda h:'.epub' in h or '/epub/' in h
                if rel and kind and 'opds-spec.org/acquisition' in rel and kind=='application/epub+zip':
                    if e['type']!='book' or (plain(href) and not plain(e['href'])):e.update(type='book',href=href)
                elif kind and 'application/atom+xml' in kind:
                    if e['type']!='book':e.update(type='navigation',href=href)
        if not self.in_entry or not self.collect:return
        if self.named(name,'title'):self.in_title=True;self.text=b''
        elif self.named(name,'author'):self.in_author=True
        elif self.in_author and self.named(name,'name'):self.in_name=True;self.text=b''
        elif self.named(name,'id'):self.in_id=True;self.text=b''

    def end(self,name):
        if self.named(name,'entry'):
            if self.collect and self.entry['title'] and self.entry['href']:
                self.entries.append({k:v.decode() if isinstance(v,bytes) else v for k,v in self.entry.items()})
            self.in_entry=self.collect=False
        elif self.in_entry:
            if self.named(name,'title'):
                if self.in_title:self.entry['title']=self.text
                self.in_title=False
            elif self.named(name,'author'):self.in_author=False
            elif self.in_name and self.named(name,'name'):self.entry['author']=self.text;self.in_name=False
            elif self.named(name,'id'):
                if self.in_id:self.entry['id']=self.text
                self.in_id=False

    def characters(self,data):
        if not self.collect:return
        limit=160 if self.in_title else 120 if self.in_name else 128 if self.in_id else 0
        self.text+=data.encode()[:max(limit-len(self.text),0)]

    def titles(self,kind=None):
        return [e['title'] for e in self.entries if kind in (None,e['type'])]


def build_url(server,path):
    """UrlUtils::buildUrl and encodeUnsafeUrlChars (src/util/UrlUtils.cpp): the address the reader asks for."""
    def encode(url):
        raw=url.encode();out='';i=0
        while i<len(raw):
            c=raw[i]
            if c==0x25 and i+2<len(raw) and all(chr(h) in '0123456789abcdefABCDEF' for h in raw[i+1:i+3]):out+=raw[i:i+3].decode();i+=2
            elif c==0x25 or c<=0x20 or c>=0x7f or chr(c) in '"<>\\^`{|}':out+='%%%02X'%c
            else:out+=chr(c)
            i+=1
        return out
    if '://' in path:return encode(path)
    if '://' not in server:server='http://'+server
    if not path:return encode(server)
    if path[0]=='/':
        slash=server.find('/',server.find('://')+3)
        return encode((server if slash<0 else server[:slash])+path)
    base=server.split('?')[0]
    return encode(base+('' if base.endswith('/') else '/')+path)

def sanitize_filename(name,max_bytes=100):
    """StringUtils::sanitizeFilename (src/util/StringUtils.cpp), over bytes as the firmware does it."""
    text=name.encode();i=0;result=b''
    while text[i:i+1] in (b' ',b'.'):i+=1
    while i<len(text):
        size=1 if text[i]<0x80 else 2 if text[i]>>5==6 else 3 if text[i]>>4==14 else 4
        cp=text[i:i+size];i+=size
        if cp in (b'/',b'\\',b':',b'*',b'?',b'"',b'<',b'>',b'|'):
            if len(result)+1>max_bytes:break
            result+=b'_'
        elif size>1 or 32<=cp[0]<127:
            if len(result)+size>max_bytes:break
            result+=cp
    return (result.rstrip(b' .') or b'book').decode()

def opds_book_filename(author,title,layout):
    """opdsBookFilename (src/util/OpdsFilename.cpp): layout is the value of OpdsFilenameFormat."""
    base=title if not author or layout==2 else title+' - '+author if layout==1 else author+' - '+title
    return sanitize_filename(base)+'.epub'


class Reader:
    """The OPDS browser of the X3 in front of a test client (OpdsBookBrowserActivity.cpp): Basic with
    every request, only 200 is an answer, a feed without rows is an error screen."""
    SERVER='http://books.waifu/opds'

    def __init__(self,test,client,layout=0,agent='CrossPoint-ESP32-1.6.5'):
        self.test,self.client,self.layout,self.agent=test,client,layout,agent
        self.path='';self.feed=None

    def get(self,url):
        self.test.assertTrue(url.startswith('http://books.waifu/opds'),url)  # the password goes nowhere else
        r=self.client.get(url[len('http://books.waifu'):],auth=AUTH,headers={'User-Agent':self.agent})
        self.test.assertEqual(200,r.status_code,url)
        return r

    def open(self,path=''):
        self.path=path;self.feed=Feed(self.get(build_url(self.SERVER,path)).content)
        self.test.assertTrue(self.feed.entries or self.feed.next or self.feed.previous,'error screen: no entries in '+path)
        for e in self.feed.entries:
            self.test.assertTrue(e['href'].startswith('/opds'),e)
        return self.feed

    def follow(self,href):
        return self.open(build_url(build_url(self.SERVER,self.path),href))

    def enter(self,title):
        return self.follow(next(e['href'] for e in self.feed.entries if e['title']==title))

    def search(self,query):
        self.test.assertIn('{searchTerms}',self.feed.search)
        quoted=''.join(chr(c) if chr(c).isascii() and (chr(c).isalnum() or chr(c) in '-_.~') else '%%%02X'%c for c in query.encode())
        return self.open(self.feed.search.replace('{searchTerms}',quoted))

    def every(self,path=''):
        """All rows of a list, page after page by the «next» row."""
        feed=self.open(path);rows=list(feed.entries)
        while feed.next:
            feed=self.follow(feed.next);rows+=feed.entries
        return rows

    def download(self,title):
        """(the name the file gets on the card, its bytes)"""
        entry=next(e for e in self.feed.entries if e['title']==title and e['type']=='book')
        r=self.get(build_url(build_url(self.SERVER,self.path),entry['href']))
        self.test.assertEqual(len(r.content),int(r.headers['content-length']))
        return opds_book_filename(entry['author'],entry['title'],self.layout),r.content


class FilenameRuleTests(unittest.TestCase):
    """The server's copy of the firmware's rule, and the copy of this file, against the firmware's own cases."""
    def names(self,author,title,layout):
        from bookhub.opds import reader_filename
        mine=reader_filename(author,title,LAYOUTS[layout] if layout<3 else 'unknown')
        self.assertEqual(opds_book_filename(author,title,layout),mine)
        return mine

    def test_cases_of_the_firmware_test_suite(self):
        # test/opds_filename/OpdsFilenameTest.cpp
        self.assertEqual('J. Doe - My Book.epub',self.names('J. Doe','My Book',0))
        self.assertEqual('My Book - J. Doe.epub',self.names('J. Doe','My Book',1))
        self.assertEqual('My Book.epub',self.names('J. Doe','My Book',2))
        for layout in range(3):
            self.assertEqual('My Book.epub',self.names('','My Book',layout))
        self.assertEqual('A_B - C_D_E_.epub',self.names('A/B','C:D*E?',0))
        self.assertEqual('book.epub',self.names('','',0))
        self.assertEqual('book.epub',self.names('','',2))
        self.assertEqual('a'*100+'.epub',self.names('','a'*200,2))
        self.assertEqual('J. Doe - My Book.epub',self.names('J. Doe','My Book',99))

    def test_rules_of_sanitize_filename(self):
        # src/util/StringUtils.cpp:7-44
        self.assertEqual('Том 1.epub',self.names('',' .. Том 1',2))  # leading spaces and dots are skipped
        self.assertEqual('_________.epub',self.names('','/\\:*?"<>|',2))  # each of the nine becomes "_"
        self.assertEqual('ab.epub',self.names('','a\x01\t\x7fb',2))  # control characters and DEL are dropped
        self.assertEqual('«Ёж» — 日本語 №5.epub',self.names('','«Ёж» — 日本語 №5',2))  # everything from U+0080 stays
        self.assertEqual('я'*50+'.epub',self.names('','я'*60,2))  # 100 bytes, not characters
        self.assertEqual('a'*99+'.epub',self.names('','a'*99+'яb',2))  # the first character that does not fit ends the name
        self.assertEqual('a'*97+'.epub',self.names('','a'*97+' . bcd',2))  # cut first, trailing spaces and dots after
        self.assertEqual('book.epub',self.names('',' . . .',2))
        self.assertEqual('Лев Толстой - Война и мир.epub',self.names('Лев Толстой','Война и мир...',0))
        self.assertEqual('Автор - '+'я'*43+'.epub',self.names('Автор','я'*60,0))  # the author takes the room first
        self.assertEqual('я'*50+'.epub',self.names('Автор','я'*60,1))


class OpdsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.root=Path(cls.temp.name)
        cls.env=patch.dict(os.environ,BOOKHUB_DATA=cls.temp.name,BOOKHUB_DB=str(cls.root/'bookhub.sqlite3'),AI_URL='',AI_MODEL='',
                           SYNC_API_KEY='token',SYNC_USER='reader',SYNC_PASSWORD='password',X3_URL='',X3_BOOKS_DIR='/',X3_SYNC_DEVICE='')
        cls.env.start()
        forget_shelf()
        from bookhub import application,opds,shelf
        from bookhub.sync.service import get_store
        cls.application=application;cls.opds=opds;cls.shelf=shelf
        cls.web=TestClient(application.create_web_app(),follow_redirects=False)
        cls.sync=TestClient(application.create_sync_app(),follow_redirects=False)
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
        self.store.core.idle.clear();self.store.core.index.cache_clear()
        self.made=0

    # --- helpers ---
    def add(self,title,author='Автор',status=None,filename=None,**fields):
        """A card with its own EPUB; fields are columns of the card set straight in the database."""
        self.made+=1;data=epub(title,author,f'Copy {self.made}.')
        r=self.web.post('/api/epubs',params={'filename':filename or f'upload-{self.made}.epub'},content=data)
        self.assertEqual(200,r.status_code,r.text)
        book=r.json()
        if status:self.assertEqual(200,self.web.patch(f"/api/books/{book['book_id']}",json={'status':status}).status_code)
        for name,value in fields.items():
            self.sql(f'UPDATE books SET {name}=? WHERE id=?',value,book['book_id'])
        return dict(book,id=book['book_id'],href=f"/opds/b/{book['book_id']}-{book['sha256'][:12]}.epub",data=data)

    def sql(self,statement,*args):
        with self.store.connect(write=True) as db:
            return [tuple(r) for r in db.execute(statement,args).fetchall()]

    def reader(self,client=None,**settings):
        return Reader(self,client or self.sync,**settings)

    def menu(self,sections=None,page_size=20,expect=200):
        r=self.web.get('/api/opds/menu') if sections is None else self.web.put('/api/opds/menu',json=dict(page_size=page_size,sections=sections))
        self.assertEqual(expect,r.status_code,r.text)
        return r.json()

    def card(self,bid):
        r=self.web.get(f'/api/books/{bid}')
        self.assertEqual(200,r.status_code,r.text)
        return r.json()

    def push(self,doc,offset,device='CrossPoint',device_id='crosspoint-reader'):
        return self.sync.put('/syncs/progress',headers=self.ko,json=dict(document=doc,progress=XPATH+str(offset),percentage=offset/100,device=device,device_id=device_id))

    def bound(self,*names):
        """(card, EPUB version) each of these file names is bound to as a KOSync document"""
        rows=dict((r[0],r[1:]) for r in self.sql('SELECT document,library_book_id,epub_sha256 FROM documents'))
        return [rows.get(md5(name)) for name in names]

    # --- the password ---
    def test_only_basic_with_the_sync_account_opens_the_catalogue(self):
        book=self.add('Книга')
        basic=lambda user,password:{'Authorization':'Basic '+base64.b64encode(f'{user}:{password}'.encode()).decode()}
        paths=['/opds','/opds/','/opds/s/all','/opds/search?q=a','/opds/opensearch.xml',book['href'],'/opds/nothing/here']
        for client in (self.web,self.sync):
            for path in paths:
                r=client.get(path)
                self.assertEqual((401,'Basic realm="BookHub OPDS"'),(r.status_code,r.headers.get('www-authenticate')),path)
            refused=[basic('reader','wrong'),basic('other','password'),basic('reader',''),basic('','password'),basic('reader','password '),basic('READER','password'),
                     {'Authorization':'Basic !!!'},{'Authorization':'Basic'},{'Authorization':'Basic '+base64.b64encode(b'no colon').decode()},
                     basic('reader','пароль'),basic('читатель','password'),{'Authorization':'Basic пароль'.encode()},{'Authorization':b'Basic \xff\xfe'},
                     {'Authorization':'Bearer token'},self.ko,{**self.ko,'Authorization':'Bearer token'},{'Authorization':'Digest username="reader"'}]
            for headers in refused:
                self.assertEqual(401,client.get('/opds',headers=headers).status_code,headers)
                self.assertEqual(401,client.get(book['href'],headers=headers).status_code,headers)
            for path in paths[:-1]:
                self.assertEqual(200,client.get(path,headers=basic('reader','password')).status_code,path)
            self.assertEqual(200,client.get('/opds',headers={'Authorization':'basic '+base64.b64encode(b'reader:password').decode()}).status_code)
            self.assertEqual(404,client.get(paths[-1],auth=AUTH).status_code)
            # Reading only, and no page of another site is let in.
            for method in ('POST','PUT','PATCH','DELETE','OPTIONS'):
                for path in ('/opds','/opds/s/all',book['href'],'/opds/nothing/here'):
                    r=client.request(method,path,auth=AUTH,headers={'Origin':'http://evil.example','Access-Control-Request-Method':'GET'})
                    self.assertEqual(405,r.status_code,(method,path))
                self.assertEqual(401,client.request(method,'/opds').status_code,method)
            r=client.get('/opds',auth=AUTH,headers={'Origin':'http://evil.example'})
            self.assertFalse([name for name in r.headers if name.startswith('access-control')])
        # A refused request is no download: only the two that were let in are on record.
        self.assertEqual([(book['id'],book['sha256'],'testclient')]*2,self.sql('SELECT book_id,sha256,agent FROM opds_downloads'))

    def test_non_ascii_sync_account_opens_it_too(self):
        app=self.opds.create_opds_app(self.store,user='читатель',password='пароль')
        with TestClient(app,follow_redirects=False) as client:
            good='Basic '+base64.b64encode('читатель:пароль'.encode()).decode()
            self.assertEqual(200,client.get('/opds',headers={'Authorization':good}).status_code)
            self.assertEqual(401,client.get('/opds',auth=AUTH).status_code)

    def test_without_a_sync_password_there_is_no_catalogue(self):
        with patch.dict(os.environ,SYNC_PASSWORD=''):
            self.assertFalse(self.menu()['enabled'])
            with TestClient(self.opds.create_opds_app(self.store),follow_redirects=False) as client:
                for headers in ({},{'Authorization':'Basic '+base64.b64encode(b'reader:').decode()}):
                    self.assertEqual(404,client.get('/opds',headers=headers).status_code)
        self.assertTrue(self.menu()['enabled'])

    def test_no_address_answers_with_a_redirect(self):
        self.add('Книга')
        for client in (self.web,self.sync):
            plain,slash=client.get('/opds',auth=AUTH),client.get('/opds/',auth=AUTH)
            self.assertEqual((200,200),(plain.status_code,slash.status_code))
            self.assertEqual(Feed(plain.content).titles(),Feed(slash.content).titles())
            self.assertIn('application/atom+xml',plain.headers['content-type'])
            for path in ('/opds/s/all/','/opds/search/','/opds/s','/opds/b','/opds//'):
                self.assertEqual(404,client.get(path,auth=AUTH).status_code,path)
            head=client.head('/opds',auth=AUTH)
            self.assertEqual((200,b''),(head.status_code,head.content))
        # Neither port sends its neighbours' paths into the catalogue or the catalogue's into them.
        self.assertEqual(404,self.web.get('/opdsx',auth=AUTH).status_code)
        self.assertEqual(200,self.web.get('/api/opds/menu').status_code)
        self.assertEqual(404,self.sync.get('/api/opds/menu',auth=AUTH).status_code)
        self.assertEqual(200,self.sync.get('/health').status_code)

    def test_running_sync_port_keeps_its_heartbeat_and_serves_the_catalogue(self):
        beat=self.root/'sync-heartbeat.json';beat.unlink(missing_ok=True)
        with TestClient(self.application.create_sync_app(),follow_redirects=False) as running:  # a served app, as under uvicorn
            deadline=time.time()+5
            while not beat.exists() and time.time()<deadline:time.sleep(.01)
            self.assertTrue(beat.exists())
            self.assertEqual(200,running.get('/opds',auth=AUTH).status_code)
            self.assertEqual(200,running.get('/users/auth',headers=self.ko).status_code)

    # --- feeds ---
    def test_sections_and_books_as_the_reader_shows_them(self):
        reading=self.add('The Master and Margarita','Михаил Булгаков','reading',title_ru='Мастер и Маргарита')
        want=self.add('Пикник на обочине','Аркадий Стругацкий, Борис Стругацкий','want')
        self.add('Солярис','Станислав Лем')
        self.add('Прочитанная','Автор','read');self.add('Брошенная','Автор','dropped')
        self.sql("INSERT INTO books(source_id,title,authors,status) VALUES('/works/OL1W','Без файла','Автор','want')")
        for client in (self.web,self.sync):
            x3=self.reader(client);root=x3.open()
            self.assertEqual(['Читаю (1)','В планах (1)','Новые (3)','Все (3)'],root.titles('navigation'))
            self.assertEqual([],root.titles('book'))
            self.assertEqual('/opds/search?q={searchTerms}',root.search)
            self.assertEqual(['/opds/s/reading','/opds/s/want','/opds/s/new','/opds/s/all'],[e['href'] for e in root.entries])
            shown=x3.enter('Читаю (1)')
            self.assertEqual([dict(type='book',title='Мастер и Маргарита',author='Михаил Булгаков',href=reading['href'],id='urn:bookhub:book:%d'%reading['id'])],shown.entries)
            self.assertEqual('/opds/search?q={searchTerms}',shown.search)  # the reader forgets the template with every feed
            x3.open();planned=x3.enter('В планах (1)')
            self.assertEqual([('Пикник на обочине','Аркадий Стругацкий, Борис Стругацкий',want['href'])],[(e['title'],e['author'],e['href']) for e in planned.entries])
            x3.open()
            self.assertEqual(['Мастер и Маргарита','Пикник на обочине','Солярис'],x3.enter('Все (3)').titles('book'))
        # What the firmware would take for a second title, id or name is not in an entry at all.
        tree=ElementTree.fromstring(self.sync.get('/opds/s/all',auth=AUTH).content)
        for entry in tree.iter(ATOM+'entry'):
            self.assertEqual(['title','id','updated','author','name','link'],[e.tag.replace(ATOM,'') for e in entry.iter()][1:])
            link=entry.find(ATOM+'link').attrib
            self.assertEqual(('http://opds-spec.org/acquisition','application/epub+zip'),(link['rel'],link['type']))
        self.assertEqual('Все',tree.findtext(ATOM+'title'))

    def test_pages_have_next_and_previous_and_never_run_empty(self):
        titles=['Книга %02d'%n for n in range(1,13)]
        for title in titles:self.add(title,status='want')
        self.menu([dict(id='all',title='Все',filter={},sort='title')],page_size=5)
        x3=self.reader();x3.open()
        first=x3.enter('Все (12)')
        self.assertEqual((titles[:5],'/opds/s/all?page=2',''),(first.titles(),first.next,first.previous))
        second=x3.follow(first.next)
        self.assertEqual((titles[5:10],'/opds/s/all?page=3','/opds/s/all?page=1'),(second.titles(),second.next,second.previous))
        third=x3.follow(second.next)
        self.assertEqual((titles[10:],'','/opds/s/all?page=2'),(third.titles(),third.next,third.previous))
        self.assertEqual(titles[5:10],x3.follow(third.previous).titles())
        self.assertEqual(titles,[e['title'] for e in x3.every('/opds/s/all')])
        # A book left the list between two requests, or the page is nonsense: still a list.
        for page,expected in (('4',titles[10:]),('999',titles[10:]),('0',titles[:5]),('-3',titles[:5]),('two',titles[:5]),('',titles[:5])):
            self.assertEqual(expected,x3.open('/opds/s/all?page='+page).titles(),page)
        # Other readers follow «next» only by its type.
        tree=ElementTree.fromstring(self.sync.get('/opds/s/all?page=2',auth=AUTH).content)
        paging={e.attrib['rel']:e.attrib['type'] for e in tree.findall(ATOM+'link') if e.attrib['rel'] in ('next','previous')}
        self.assertEqual({'next','previous'},set(paging))
        self.assertTrue(all('application/atom+xml' in kind for kind in paging.values()))
        # The largest page still fits what the firmware keeps of a feed.
        for n in range(13,66):self.add('Книга %02d'%n,status='want')
        self.menu([dict(id='all',title='Все',filter={},sort='title')],page_size=60)
        full=x3.open('/opds/s/all')
        self.assertEqual((60,'/opds/s/all?page=2'),(len(full.entries),full.next))
        self.assertEqual(65,len(x3.every('/opds/s/all')))

    def test_every_feed_has_a_row_to_show(self):
        x3=self.reader()
        empty=x3.open()
        self.assertEqual([('navigation','В каталоге пока нет книг','/opds')],[(e['type'],e['title'],e['href']) for e in empty.entries])
        self.assertEqual(['Ничего не найдено'],x3.search('что угодно').titles('navigation'))
        self.assertEqual(['В этом разделе сейчас нет книг'],x3.open('/opds/s/reading').titles('navigation'))
        self.add('Прочитанная','Автор','read')
        self.assertEqual(['В каталоге пока нет книг'],x3.open().titles())
        self.add('Книга')
        self.menu([dict(id='reading',title='Читаю',filter=dict(status=['reading']),sort='read'),dict(id='all',title='Все',enabled=False,filter={},sort='title')])
        self.assertEqual(['Все разделы каталога пусты или выключены'],x3.open().titles('navigation'))
        self.assertEqual(['Книга'],x3.search('книга').titles('book'))  # search does not depend on the menu
        # A section that is off or gone is not a feed.
        for path in ('/opds/s/all','/opds/s/unknown','/opds/s/%D0%B2%D1%81%D0%B5'):
            self.assertEqual(404,self.sync.get(path,auth=AUTH).status_code,path)

    def test_read_and_dropped_books_are_nowhere(self):
        kept=self.add('Нужная книга','Автор','want')
        hidden=[self.add('Прочитанная книга','Автор','read'),self.add('Брошенная книга','Автор','dropped')]
        self.menu([dict(title='Все',filter={},sort=order) for order in ('read','added','title','author','updated')]+
                  [dict(title='По фильтру',filter=dict(status=['reading','want','unsorted'],started=False,on_x3=False,added_days=3650),sort='title')])
        x3=self.reader();root=x3.open()
        self.assertEqual(['Все (1)']*5+['По фильтру (1)'],root.titles())
        for entry in root.entries:
            self.assertEqual(['Нужная книга'],[e['title'] for e in x3.every(entry['href'])])
        x3.open()
        self.assertEqual(['Нужная книга'],x3.search('книга').titles('book'))
        self.assertEqual(['Ничего не найдено'],x3.search('прочитанная').titles())
        self.assertEqual([1]*6,[s['count'] for s in self.menu()['sections']])
        untouched=self.sql('SELECT * FROM documents WHERE library_book_id<>? ORDER BY document',kept['id'])
        for client in (self.web,self.sync):
            self.assertEqual(200,client.get(kept['href'],auth=AUTH).status_code)
            for book in hidden:
                self.assertEqual(404,client.get(book['href'],auth=AUTH).status_code)
                self.assertEqual(404,client.head(book['href'],auth=AUTH).status_code)
        self.assertEqual({kept['id']},{r[0] for r in self.sql('SELECT book_id FROM opds_downloads')})
        self.assertEqual(untouched,self.sql('SELECT * FROM documents WHERE library_book_id<>? ORDER BY document',kept['id']))
        # Read again: back in the catalogue under the same address.
        self.web.patch(f"/api/books/{hidden[0]['id']}",json={'status':'reading'})
        self.assertEqual(hidden[0]['data'],self.sync.get(hidden[0]['href'],auth=AUTH).content)
        x3.open()
        self.assertEqual(['Нужная книга','Прочитанная книга'],x3.search('книга').titles('book'))

    def test_title_and_author_fit_the_reader_in_bytes(self):
        long_title='Очень длинное название книги, которое никак не помещается в строку списка читалки '*3
        authors=', '.join(f'Автор Номер{n}' for n in range(1,12))
        self.add(long_title,authors)
        self.add('  Заголовок\n\tв  две строки\u00a0и с хвостом…  ','  Имя\nФамилия ')
        self.add('Q&A: <теги> "кавычки" и \'апострофы\'','O\'Brien & <Co>')
        self.add('Без автора','');self.add('Каталожная','Автор не указан')
        raw=self.sync.get('/opds/s/all',auth=AUTH).content
        sent={e.findtext(ATOM+'title'):e.findtext(ATOM+'author/'+ATOM+'name') for e in ElementTree.fromstring(raw).iter(ATOM+'entry')}
        seen={e['title']:e['author'] for e in Feed(raw).entries}
        self.assertEqual({t:a or '' for t,a in sent.items()},seen)  # nothing was cut by the reader, least of all inside a character
        cut=next(t for t in sent if t.startswith('Очень'))
        self.assertTrue(cut.endswith('...') and 150<len(cut.encode())<=160 and long_title.startswith(cut[:-3]),cut)
        self.assertTrue(sent[cut].endswith('...') and 110<len(sent[cut].encode())<=120 and authors.startswith(sent[cut][:-3]),sent[cut])
        self.assertEqual('Имя Фамилия',sent['Заголовок в две строки и с хвостом...'])  # one line, ASCII dots for the ellipsis
        self.assertEqual("O'Brien & <Co>",sent['Q&A: <теги> "кавычки" и \'апострофы\''])
        self.assertEqual((None,None),(sent['Без автора'],sent['Каталожная']))  # the shelf's «no author» is not a name
        for title,author in sent.items():
            self.assertEqual((title.strip(),(author or '').strip()),(title,author or ''))
            self.assertTrue(len(title.encode())<=160 and len((author or '').encode())<=120)
        self.assertNotIn('\u2026',raw.decode())

    # --- file names ---
    def names(self,client=None):
        """{card: [its file name in each of the three layouts]} over everything the catalogue lists"""
        rows=self.reader(client).every('/opds/s/all')
        return {int(e['id'].rsplit(':',1)[1]):[opds_book_filename(e['author'],e['title'],layout) for layout in range(3)] for e in rows}

    def assert_distinct(self,names):
        pool=[name.casefold() for per_book in names.values() for name in dict.fromkeys(per_book)]
        self.assertEqual(len(pool),len(set(pool)),sorted(pool))

    def test_books_of_one_name_get_a_mark_that_stays(self):
        first=self.add('Война и мир','Лев Толстой');second=self.add('Война и мир','Лев Толстой')
        x3=self.reader()
        self.assertEqual(['Война и мир','Война и мир [#%d]'%second['id']],x3.open('/opds/s/all').titles())
        before=self.names();self.assert_distinct(before)
        self.assertEqual(['Лев Толстой - Война и мир.epub','Война и мир - Лев Толстой.epub','Война и мир.epub'],before[first['id']])
        self.assertEqual('Лев Толстой - Война и мир [#%d].epub'%second['id'],before[second['id']][0])
        # Names that differ only in case are one file on the card; the same title by another author is one in the «title» layout.
        shout=self.add('ВОЙНА И МИР','ЛЕВ ТОЛСТОЙ');other=self.add('Война и мир','Другой Автор')
        self.add('Анна Каренина','Лев Толстой')
        after=self.names();self.assert_distinct(after)
        self.assertEqual(before,{bid:after[bid] for bid in before})  # new books rename nobody
        self.assertIn('[#%d]'%shout['id'],after[shout['id']][0])
        self.assertEqual('Война и мир [#%d].epub'%other['id'],after[other['id']][2])
        # The earlier card is finished or dropped: the later one keeps its name and its files on the reader.
        self.web.patch(f"/api/books/{first['id']}",json={'status':'read'})
        kept=self.names()
        self.assertEqual({bid:names for bid,names in after.items() if bid!=first['id']},kept)
        self.web.patch(f"/api/books/{first['id']}",json={'status':'want'})
        self.assertEqual(after,self.names())
        self.assertEqual(self.names(self.web),self.names(self.sync))

    def test_long_names_with_one_beginning_stay_apart(self):
        series='Реинкарнация безработного: История о приключениях в другом мире. Том %d'
        volumes=[self.add(series%n,'Рифудзин-на Магонотэ') for n in (1,2,3)]
        crowd=', '.join(f'Соавтор Номер{n}' for n in range(1,9))
        many=[self.add(title,crowd) for title in ('Сборник первый','Сборник второй')]
        names=self.names();self.assert_distinct(names)
        titles={int(e['id'].rsplit(':',1)[1]):(e['title'],e['author']) for e in self.reader().every('/opds/s/all')}
        self.assertEqual(series%1,titles[volumes[0]['id']][0])  # the earlier card keeps its whole title
        for n,volume in ((2,volumes[1]),(3,volumes[2])):
            title,mark=titles[volume['id']][0],'[#%d]'%volume['id']
            self.assertTrue(title.startswith('Реинкарнация') and title.endswith(f'Том {n} {mark}') and '...' in title,title)
            for name in names[volume['id']]:  # the mark is inside the 100 bytes of every layout
                self.assertTrue(mark in name and len(name.encode())<=105,name)
        # An author line that fills the whole name alone gives way too.
        title,author=titles[many[0]['id']]
        self.assertTrue(title=='Сборник первый' and author.endswith('...') and crowd.startswith(author[:-3]) and 110<len(author.encode())<=120,(title,author))
        title,author=titles[many[1]['id']]
        self.assertTrue(title=='Сборник второй [#%d]'%many[1]['id'] and len(author.encode())<=57 and author.endswith('...'),(title,author))
        for name in names[many[1]['id']]:
            self.assertIn('[#%d]'%many[1]['id'],name)

    # --- download ---
    def test_download_is_the_stored_file_with_its_length(self):
        book=self.add('Война и мир','Лев Толстой')
        other=self.store.core.ingest(epub('Война и мир','Лев Толстой','Another edition.'),'second.epub',book['id'])
        for client in (self.web,self.sync):
            r=client.get(book['href'],auth=AUTH)
            self.assertEqual((200,book['data'],'application/epub+zip',str(len(book['data']))),
                             (r.status_code,r.content,r.headers['content-type'],r.headers['content-length']))
            self.assertEqual(hashlib.sha256(r.content).hexdigest(),book['sha256'])
            head=client.head(book['href'],auth=AUTH)
            self.assertEqual((200,b'',str(len(book['data']))),(head.status_code,head.content,head.headers['content-length']))
            wrong=[f"/opds/b/{book['id']}-{other['sha256'][:12]}.epub",  # only the primary EPUB is offered
                   f"/opds/b/{book['id']}-{'0'*12}.epub",f"/opds/b/{book['id']+100}-{book['sha256'][:12]}.epub",
                   f"/opds/b/{book['id']}-{book['sha256']}.epub",f"/opds/b/{book['id']}-{book['sha256'][:12].upper()}.epub",
                   f"/opds/b/{book['id']}-{book['sha256'][:12]}",f"/opds/b/{book['id']}.epub",'/opds/b/book.epub',
                   f"/opds/b/{book['id']}-{book['sha256'][:12]}.epub/"]
            for path in wrong:
                self.assertEqual(404,client.get(path,auth=AUTH).status_code,path)
        self.assertEqual(2,len(self.sql('SELECT * FROM opds_downloads')))  # HEAD and refusals are not downloads
        # The file is gone from the disk: no book, not a broken answer.
        stored=Path(self.sql('SELECT path FROM epub_assets WHERE sha256=?',book['sha256'])[0][0]);away=stored.with_suffix('.gone')
        stored.rename(away)
        self.assertEqual(404,self.sync.get(book['href'],auth=AUTH).status_code)
        away.rename(stored)
        self.web.delete(f"/api/books/{book['id']}")
        self.assertEqual(404,self.sync.get(book['href'],auth=AUTH).status_code)
        self.assertEqual([],self.sql('SELECT * FROM opds_downloads'))

    def test_download_binds_the_name_the_reader_gives_the_file(self):
        books=[self.add('Война и мир','Лев Толстой'),self.add('Q&A: что/где?','O\'Brien'),self.add('Без автора','')]
        for layout in range(3):
            self.sql('DELETE FROM documents');self.sql('DELETE FROM reader_observations');self.sql('DELETE FROM opds_downloads')
            self.sql('UPDATE reading_positions SET xpath=NULL,ordinal=NULL,percentage=NULL')
            x3=self.reader(layout=layout);x3.open('/opds/s/all')
            for offset,book in enumerate(books,1):
                title=self.card(book['id'])['title']
                name,data=x3.download(title)
                self.assertEqual(book['data'],data)
                # The firmware's «Filename» document is MD5 of the name on the card: its first sync finds the card.
                answer=self.push(md5(name),offset*10)
                self.assertTrue(answer.json().get('accepted'),(name,answer.text))
                self.assertEqual(offset/10,self.card(book['id'])['epubs'][0]['percentage'])
                self.assertEqual(XPATH+str(offset*10),self.sync.get('/syncs/progress/'+md5(name),headers=self.ko).json()['progress'])
            self.assertEqual([],[d for d in self.sync.get('/api/library',auth=AUTH).json()['documents'] if not d['library_book_id']])
        self.assertEqual(['Лев Толстой - Война и мир.epub','Война и мир - Лев Толстой.epub','Война и мир.epub'],
                         json.loads(self.sql('SELECT filenames FROM opds_downloads WHERE book_id=?',books[0]['id'])[0][0]))
        self.assertEqual([(books[0]['id'],books[0]['sha256'])]*3,self.bound('Лев Толстой - Война и мир.epub','Война и мир - Лев Толстой.epub','Война и мир.epub'))
        self.assertEqual([(books[1]['id'],books[1]['sha256'])],self.bound("O'Brien - Q&A_ что_где_.epub"))
        self.assertEqual(1,len(self.sql('SELECT * FROM documents WHERE library_book_id=?',books[2]['id'])))  # one name without an author
        # The «Binary» document, KOReader's digest of the same bytes, was bound when the EPUB arrived.
        digest=self.store.core.koreader_digest(books[0]['data'])
        self.sql('DELETE FROM documents');self.store.core.ingest(books[0]['data'],'again.epub')
        self.assertTrue(self.push(digest,60,'KOReader','koreader').json()['accepted'])

    def test_download_adds_bindings_and_changes_none(self):
        book=self.add('Война и мир','Лев Толстой');stranger=self.add('Чужая книга','Автор')
        names=['Лев Толстой - Война и мир.epub','Война и мир - Лев Толстой.epub','Война и мир.epub']
        # One name waits unbound with a position, the others are unknown.
        waiting=self.push(md5(names[1]),40).json()
        self.assertFalse(waiting.get('accepted'))
        self.assertIsNone(self.card(book['id'])['epubs'][0]['percentage'])
        x3=self.reader(agent='KOReader');x3.open('/opds/s/all');x3.download('Война и мир')
        mine,theirs=(book['id'],book['sha256']),(stranger['id'],stranger['sha256'])
        self.assertEqual([mine,mine,mine],self.bound(*names))
        self.assertEqual(.4,self.card(book['id'])['epubs'][0]['percentage'])  # the position that waited for the file
        x3.download('Чужая книга');self.assertTrue(self.push(md5('Автор - Чужая книга.epub'),30).json()['accepted'])
        # Another version becomes the primary one: the old bindings and what they hold stay where they are.
        newer=self.store.core.ingest(epub('Война и мир','Лев Толстой','Second edition.'),'second.epub',book['id'])
        self.sql('UPDATE book_assets SET canonical=0 WHERE book_id=?',book['id'])
        self.sql('UPDATE book_assets SET canonical=1 WHERE book_id=? AND sha256=?',book['id'],newer['sha256'])
        fresh=self.reader(agent='KOReader');fresh.open('/opds/s/all')
        self.assertEqual([f"/opds/b/{book['id']}-{newer['sha256'][:12]}.epub"],[e['href'] for e in fresh.feed.entries if e['title']=='Война и мир'])
        documents=self.sql('SELECT * FROM documents ORDER BY document');positions=self.sql('SELECT * FROM reading_positions ORDER BY book_id,sha256')
        fresh.download('Война и мир')
        self.assertEqual([mine,mine,mine],self.bound(*names))
        self.assertEqual(documents,self.sql('SELECT * FROM documents ORDER BY document'))
        self.assertEqual(positions,self.sql('SELECT * FROM reading_positions ORDER BY book_id,sha256'))
        # Downloading again resets nothing either.
        x3.open('/opds/s/all');x3.download('Чужая книга');x3.download('Чужая книга')
        self.assertEqual(.3,self.card(stranger['id'])['epubs'][0]['percentage'])
        self.assertEqual([theirs],self.bound('Автор - Чужая книга.epub'))
        self.assertEqual(positions,self.sql('SELECT * FROM reading_positions ORDER BY book_id,sha256'))
        self.assertEqual([],self.sql('SELECT * FROM deliveries'))  # no reader named itself an X3

    def test_name_a_reader_keeps_for_one_card_is_never_given_to_another(self):
        first=self.add('Война и мир','Лев Толстой');second=self.add('Война и мир','Лев Толстой')
        plain='Лев Толстой - Война и мир.epub'
        titles=lambda:{e['title'] for e in self.reader().open('/opds/s/all').entries}
        self.assertEqual({'Война и мир',f"Война и мир [#{second['id']}]"},titles())
        x3=self.reader();x3.open('/opds/s/all');x3.download('Война и мир')
        self.assertEqual([(first['id'],first['sha256'])],self.bound(plain))
        # The earlier card is renamed (a Russian title, a fixed author): the plain name is still the first
        # card's file on the reader, so the second card does not inherit it.
        self.web.patch(f"/api/books/{first['id']}",json={'title_ru':'Война и мир. Том 1'})
        self.assertEqual({'Война и мир. Том 1',f"Война и мир [#{second['id']}]"},titles())
        # A name bound by hand or by an upload under exactly that file name counts the same way.
        third=self.add('Анна Каренина','Лев Толстой')
        self.store.core.bind(md5('Анна Каренина.epub'),first['id'])
        self.assertIn(f"Анна Каренина [#{third['id']}]",titles())
        x3.open('/opds/s/all');x3.download(f"Анна Каренина [#{third['id']}]")
        self.assertEqual([(first['id'],first['sha256'])],self.bound('Анна Каренина.epub'))
        self.assertEqual([(third['id'],third['sha256'])],self.bound(f"Лев Толстой - Анна Каренина [#{third['id']}].epub"))

    def test_download_counts_only_when_the_whole_file_reached_a_reader_still_there(self):
        import asyncio
        from bookhub import opds
        path=Path(self.temp.name)/'handed.epub';path.write_bytes(b'x'*300000)
        def hand(method,gone):
            taken=[]
            async def receive():
                if gone:return {'type':'http.disconnect'}
                await asyncio.sleep(3600)
            async def send(message):pass
            scope=dict(type='http',method=method,path='/',headers=[],query_string=b'',http_version='1.1',scheme='http',
                       server=('test',80),client=('reader',1),root_path='',extensions={})
            asyncio.run(opds.Handed(str(path),taken=lambda:taken.append(1))(scope,receive,send))
            return taken
        # A cancelled or lost transfer and a HEAD are not downloads: the reader has no file.
        self.assertEqual(([1],[],[]),(hand('GET',False),hand('GET',True),hand('HEAD',False)))

    def test_failed_bookkeeping_does_not_cost_the_reader_its_book(self):
        book=self.add('Война и мир','Лев Толстой')
        for failure in (RuntimeError('boom'),sqlite3.OperationalError('database is locked')):
            with patch.object(self.store.core,'downloaded',side_effect=failure),self.assertLogs(level='ERROR'):
                r=self.sync.get(book['href'],auth=AUTH)
            self.assertEqual((200,book['data']),(r.status_code,r.content))
        self.assertEqual([],self.sql('SELECT * FROM documents WHERE document=?',md5('Лев Толстой - Война и мир.epub')))
        self.assertEqual(200,self.sync.get(book['href'],auth=AUTH).status_code)
        self.assertEqual([(book['id'],book['sha256'])],self.bound('Лев Толстой - Война и мир.epub'))

    def test_download_by_crosspoint_counts_as_delivery_to_the_x3(self):
        book=self.add('Война и мир','Лев Толстой','reading');other=self.add('Анна Каренина','Лев Толстой','want')
        name='Лев Толстой - Война и мир.epub'
        section=[dict(id='not-on-x3',title='Нет на X3',filter=dict(on_x3=False),sort='title'),dict(title='На X3',filter=dict(on_x3=True),sort='title')]
        self.assertEqual([2,0],[s['count'] for s in self.menu(section)['sections']])
        # Another reader says nothing about the X3.
        koreader=self.reader(agent='KOReader/2026.03');koreader.open('/opds/s/not-on-x3');koreader.download('Война и мир')
        self.assertEqual(([],None),(self.sql('SELECT * FROM deliveries'),self.card(book['id'])['x3']))
        x3=self.reader(layout=1);x3.open('/opds/s/not-on-x3');x3.download('Война и мир')
        self.assertEqual([(book['id'],book['sha256'],'x3',name)],self.sql('SELECT book_id,sha256,device,filename FROM deliveries'))
        mark=self.card(book['id'])['x3']
        self.assertEqual(('present',False,name),(mark['state'],mark['confirmed'],mark['filename']))
        self.assertIsNone(self.card(other['id'])['x3'])
        self.assertEqual(['Анна Каренина'],x3.open('/opds/s/not-on-x3').titles())
        self.assertEqual(['Нет на X3 (1)','На X3 (1)'],x3.open().titles())
        self.assertEqual([1,1],[s['count'] for s in self.menu()['sections']])
        # A later file listing says otherwise; then it shows the file under the name of the device's own layout.
        self.sql('UPDATE deliveries SET sent_at=sent_at-60')
        self.shelf.save_x3_listing([dict(name='stranger.epub',size=10,epub=True)])
        self.assertEqual(('missing',True),tuple(self.card(book['id'])['x3'][k] for k in ('state','confirmed')))
        self.assertEqual(['Нет на X3 (2)'],x3.open().titles())
        self.shelf.save_x3_listing([dict(name='Война и мир - Лев Толстой.epub',size=len(book['data']),epub=True)])
        self.assertEqual(('present',True,'Война и мир - Лев Толстой.epub'),tuple(self.card(book['id'])['x3'][k] for k in ('state','confirmed','filename')))
        # Again after the listing: on the X3 once more, until the next listing.
        self.shelf.save_x3_listing([]);self.sql('UPDATE x3_listing SET checked_at=checked_at-30')
        self.assertEqual('missing',self.card(book['id'])['x3']['state'])
        x3.open('/opds/s/not-on-x3');x3.download('Война и мир')
        self.assertEqual(('present',False),tuple(self.card(book['id'])['x3'][k] for k in ('state','confirmed')))
        self.assertEqual(1,len(self.sql('SELECT * FROM deliveries')))

    def test_user_agent_is_a_hint_and_takes_no_delivery_from_another_book(self):
        book=self.add('Война и мир','Лев Толстой');other=self.add('Анна Каренина','Лев Толстой')
        name='Лев Толстой - Война и мир.epub'
        # The name was sent for another card, and for another version of this one: both rows stay as they are.
        for owner,sha in ((other['id'],other['sha256']),(book['id'],'f'*64)):
            self.sql('DELETE FROM deliveries');self.sql("INSERT INTO deliveries VALUES(?,?,'x3',?,1000)",owner,sha,name)
            x3=self.reader();x3.open('/opds/s/all');x3.download('Война и мир')
            self.assertEqual([(owner,sha,'x3',name,1000)],self.sql('SELECT * FROM deliveries'))
        # A row left by a deleted card belongs to nobody: the file under that name is this book now.
        self.sql('UPDATE deliveries SET book_id=NULL')
        x3.download('Война и мир')
        self.assertEqual([(book['id'],book['sha256'],name)],self.sql('SELECT book_id,sha256,filename FROM deliveries'))
        self.assertGreater(self.sql('SELECT sent_at FROM deliveries')[0][0],1000)
        # Removed from the X3 by the shelf's button: the download does not undo the record of it.
        self.sql('UPDATE books SET x3_removed_at=? WHERE id=?',int(time.time())-60,book['id'])
        x3.download('Война и мир')
        self.assertEqual(1,len(self.sql('SELECT 1 FROM books WHERE id=? AND x3_removed_at IS NOT NULL',book['id'])))
        self.assertEqual('present',self.card(book['id'])['x3']['state'])

    def test_merged_and_deleted_cards_take_their_downloads_along(self):
        source=self.add('Война и мир','Лев Толстой');target=self.add('Война и мир. Издание второе','Лев Толстой')
        x3=self.reader();x3.open('/opds/s/all');x3.download('Война и мир')
        r=self.web.post(f"/api/books/{source['id']}/merge",json={'target_id':target['id']})
        self.assertEqual(200,r.status_code,r.text)
        self.assertEqual([(target['id'],source['sha256'])],self.sql('SELECT book_id,sha256 FROM opds_downloads'))
        self.assertEqual([(target['id'],source['sha256'])],self.bound('Лев Толстой - Война и мир.epub'))
        self.web.delete(f"/api/books/{target['id']}")
        self.assertEqual([],self.sql('SELECT * FROM opds_downloads'))

    # --- search ---
    def test_search_ignores_case_and_yo_and_reads_three_fields(self):
        self.add('Ёжик в тумане','Сергей Козлов','want')
        self.add('The Master and Margarita','Михаил Булгаков','reading',title_ru='Мастер и Маргарита')
        self.add('Ежедневник','Пётр Ёлкин');self.add('Прочитанный ёжик','Автор','read')
        x3=self.reader();x3.open()
        found=lambda query:x3.search(query).titles('book')
        self.assertEqual(['Ёжик в тумане'],found('ежик'))
        self.assertEqual(['Ёжик в тумане'],found('ЁЖИК'))
        self.assertEqual(['Ежедневник','Ёжик в тумане'],found('еж'))
        self.assertEqual(['Ежедневник'],found('елкин'))
        self.assertEqual(['Мастер и Маргарита'],found('MASTER'))
        self.assertEqual(['Мастер и Маргарита'],found('маргарита булгаков'))  # every word, in any field
        self.assertEqual(['Мастер и Маргарита'],found('  Булгаков   мастер '))
        self.assertEqual(['Ничего не найдено'],x3.search('булгаков туман').titles())
        self.assertEqual('/opds/search?q={searchTerms}',x3.feed.search)  # search again from a result
        self.assertEqual(['Ёжик в тумане'],x3.search('Козлов').titles('book'))
        self.assertEqual(3,len(x3.open('/opds/search?q=').titles('book')))
        # Other readers take the template from the description document.
        r=self.sync.get('/opds/opensearch.xml',auth=AUTH)
        url=ElementTree.fromstring(r.content).find('{http://a9.com/-/spec/opensearch/1.1/}Url').attrib
        self.assertEqual(('/opds/search?q={searchTerms}',True,True),(url['template'],'application/atom+xml' in url['type'],'opensearchdescription' in r.headers['content-type']))
        described=[e.attrib for e in ElementTree.fromstring(self.sync.get('/opds',auth=AUTH).content).findall(ATOM+'link') if e.attrib['rel']=='search']
        self.assertEqual({'/opds/opensearch.xml','/opds/search?q={searchTerms}'},{e['href'] for e in described})

    def test_search_results_are_paged_by_links_the_reader_can_follow(self):
        for n in range(1,8):self.add('Том %d: проба & <пера>'%n,'Автор')
        self.menu([dict(id='all',title='Все',filter={},sort='title')],page_size=5)
        x3=self.reader();x3.open()
        query='том & <пера>'
        first=x3.search(query)
        self.assertEqual((5,''),(len(first.entries),first.previous))
        self.assertEqual('/opds/search?q=%D1%82%D0%BE%D0%BC%20%26%20%3C%D0%BF%D0%B5%D1%80%D0%B0%3E&page=2',first.next)
        second=x3.follow(first.next)
        self.assertEqual((['Том 6: проба & <пера>','Том 7: проба & <пера>'],''),(second.titles(),second.next))
        self.assertEqual(5,len(x3.follow(second.previous).entries))
        # A query that looks like an address must not send the reader, and its password, to another host.
        for odd in ('http://evil.example/x','a://b','{searchTerms}','%2F%2F','x'*5000,'ж'*500):
            feed=x3.search(odd)
            self.assertEqual(['Ничего не найдено'],feed.titles(),odd)
            for href in (feed.next,feed.previous,*[e['href'] for e in feed.entries]):
                self.assertTrue(not href or (href.startswith('/opds') and '://' not in href and len(href)<768),href)
        tree=ElementTree.fromstring(self.sync.get('/opds/search',params={'q':'ж'*500},auth=AUTH).content)
        self.assertLess(len(next(e.attrib['href'] for e in tree.findall(ATOM+'link') if e.attrib['rel']=='self')),768)

    # --- the menu ---
    def test_menu_is_the_built_in_one_until_the_owner_changes_it(self):
        self.add('Книга','Автор','reading')
        menu=self.menu()
        self.assertEqual(['enabled','path','page_size','sections','filters','default'],list(menu))
        self.assertEqual((True,'/opds',20,True),(menu['enabled'],menu['path'],menu['page_size'],menu['default']))
        self.assertEqual(dict(status=['reading','want','unsorted'],sort=['read','added','title','author','updated']),menu['filters'])
        self.assertEqual([('reading','Читаю',True,{'status':['reading']},'read',None,1,True),('want','В планах',True,{'status':['want']},'added',None,0,True),
                          ('new','Новые',True,{'added_days':30},'added',30,1,True),('all','Все',True,{},'title',None,1,True),
                          ('unsorted','Общее',False,{'status':['unsorted']},'added',None,0,True),('not-on-x3','Нет на X3',False,{'on_x3':False},'title',None,1,True)],
                         [tuple(s[k] for k in ('id','title','enabled','filter','sort','limit','count','builtin')) for s in menu['sections']])
        self.assertEqual([],self.sql("SELECT * FROM metadata WHERE key='opds_menu'"))
        # What the page sends back is what it got, changed: count and builtin are not settings.
        sections=[dict(menu['sections'][3],title='Вся полка'),dict(menu['sections'][0],enabled=False),
                  dict(title='  Русские  новинки ',enabled=True,filter=dict(language='RU-ru',added_days=7,status=['want','reading'],started=None),sort='updated',limit=10),
                  dict(id='',title='Начатые',filter=dict(started=True,status=[],language='',added_days=None,on_x3=''),limit='')]
        saved=self.menu(sections,page_size=7)
        self.assertEqual((7,False),(saved['page_size'],saved['default']))
        self.assertEqual(saved,self.menu())
        self.assertEqual([('all','Вся полка',True,True),('reading','Читаю',False,True)],[(s['id'],s['title'],s['enabled'],s['builtin']) for s in saved['sections'][:2]])
        new=saved['sections'][2]
        self.assertEqual(('Русские новинки',{'status':['reading','want'],'language':'ru','added_days':7},'updated',10,False),
                         (new['title'],new['filter'],new['sort'],new['limit'],new['builtin']))
        self.assertEqual(({'started':True},'title',None,True),tuple(saved['sections'][3][k] for k in ('filter','sort','limit','enabled')))
        ids=[s['id'] for s in saved['sections']]
        self.assertTrue(len(set(ids))==4 and all(ids),ids)
        self.assertEqual(ids,[s['id'] for s in self.menu(saved['sections'],page_size=7)['sections']])  # an id is kept
        # The sync process reads the same row: the reader sees the new menu at once.
        self.assertEqual(['Вся полка (1)'],self.reader(self.sync).open().titles())
        self.assertEqual(404,self.sync.get('/opds/s/reading',auth=AUTH).status_code)
        # Back to the built-in menu.
        r=self.web.delete('/api/opds/menu')
        self.assertEqual((200,menu),(r.status_code,r.json()))
        self.assertEqual([],self.sql("SELECT * FROM metadata WHERE key='opds_menu'"))
        self.assertEqual(menu,self.menu(menu['sections']))  # the built-in menu saved as it is, is still the built-in one

    def test_invalid_menu_is_refused_in_russian_and_changes_nothing(self):
        good=dict(title='Все',filter={},sort='title')
        before=self.menu([dict(good,id='mine')],page_size=9)
        bad=[dict(page_size=4,sections=[good]),dict(page_size=61,sections=[good]),dict(page_size='20',sections=[good]),dict(page_size=True,sections=[good]),
             dict(page_size=20),dict(page_size=20,sections='all'),dict(page_size=20,sections=[good]*31),dict(page_size=20,sections=['all']),[good],'menu',None]
        bad+=[dict(page_size=20,sections=[dict(good,**change)]) for change in (
            dict(title=''),dict(title='   '),dict(title=None),dict(title='я'*61),dict(id='Все'),dict(id='a b'),dict(id='x'*33),dict(id=5),
            dict(enabled='yes'),dict(filter='reading'),dict(filter=dict(shelf='a')),dict(filter=dict(status=['read'])),dict(filter=dict(status=['dropped','want'])),
            dict(filter=dict(status='reading')),dict(filter=dict(status=[1])),dict(filter=dict(language=5)),dict(filter=dict(language='x'*17)),
            dict(filter=dict(added_days=0)),dict(filter=dict(added_days='7')),dict(filter=dict(added_days=1.5)),dict(filter=dict(added_days=True)),
            dict(filter=dict(started='yes')),dict(filter=dict(on_x3=1)),dict(sort='size'),dict(sort=None),dict(sort='title; DROP TABLE books'),
            dict(limit=0),dict(limit=-1),dict(limit='5'),dict(limit=1001),dict(limit=True))]
        bad.append(dict(page_size=20,sections=[dict(good,id='same'),dict(good,id='same')]))
        for payload in bad:
            r=self.web.put('/api/opds/menu',json=payload)
            self.assertEqual(400,r.status_code,payload)
            self.assertRegex(r.json()['detail'],'[а-яё]')
        r=self.web.put('/api/opds/menu',content=b'{"sections":',headers={'Content-Type':'application/json'})
        self.assertEqual(400,r.status_code);self.assertRegex(r.json()['detail'],'[а-яё]')
        self.assertEqual(before,self.menu())
        # The shelf's rule for writes covers the menu: no page of another site changes it.
        evil={'Origin':'http://evil.example'}
        self.assertEqual(403,self.web.put('/api/opds/menu',json=dict(page_size=20,sections=[]),headers=evil).status_code)
        self.assertEqual(403,self.web.delete('/api/opds/menu',headers=evil).status_code)
        self.assertEqual(before,self.menu())
        self.assertEqual(200,self.web.put('/api/opds/menu',json=dict(page_size=20,sections=[good]),headers={'Origin':'http://testserver'}).status_code)
        # A stored menu that no longer passes the check is not served: the built-in one is.
        for value in ('{broken',json.dumps(dict(page_size=20,sections=[dict(good)])),json.dumps(dict(sections=[dict(good,id='a',sort='size')])),None,'[]'):
            self.sql("INSERT OR REPLACE INTO metadata VALUES('opds_menu',?)",value)
            with self.assertLogs(level='WARNING'):
                self.assertEqual(['reading','want','new','all','unsorted','not-on-x3'],[s['id'] for s in self.menu()['sections']])
        with self.assertLogs(level='WARNING'):
            self.assertEqual(['В каталоге пока нет книг'],self.reader().open().titles())

    def test_sections_filter_and_sort_by_the_closed_list(self):
        day=86400;now=int(time.time());stamp=lambda ago:time.strftime('%Y-%m-%d %H:%M:%S',time.gmtime(now-ago))
        a=self.add('Яблоко','Борис','reading',language='ru',created_at=stamp(40*day),updated_at=stamp(5*day))
        b=self.add('арбуз','Анна','want',language='en-US',created_at=stamp(10*day),updated_at=stamp(1*day))
        c=self.add('Ёлка','','unsorted',language='rus',created_at=stamp(2*day),updated_at=stamp(20*day))
        d=self.add('Banana','Вера','want',language=None,created_at=stamp(3*day),updated_at=stamp(2*day))
        for book,offset in ((a,10),(d,20)):
            self.assertTrue(self.push(md5(book['filename']),offset).json()['accepted'])
        self.sql('UPDATE reading_positions SET updated_at=? WHERE book_id=?',now-500,a['id'])
        self.sql('UPDATE reading_positions SET updated_at=? WHERE book_id=?',now-100,d['id'])
        def listed(order='title',limit=None,**filters):
            menu=self.menu([dict(id='probe',title='Проба',filter=filters,sort=order,limit=limit)])
            titles=[e['title'] for e in self.reader().every('/opds/s/probe') if e['type']=='book']
            self.assertEqual(len(titles),menu['sections'][0]['count'])
            return titles
        self.assertEqual(['Banana','арбуз','Ёлка','Яблоко'],listed('title'))  # letters, not code points: Ё sits with Е
        self.assertEqual(['арбуз','Яблоко','Banana','Ёлка'],listed('author'))  # Анна, Борис, Вера, then no author
        self.assertEqual(['Ёлка','Banana','арбуз','Яблоко'],listed('added'))
        self.assertEqual(['арбуз','Banana','Яблоко','Ёлка'],listed('updated'))
        self.assertEqual(['Banana','Яблоко','Ёлка','арбуз'],listed('read'))  # last read first, the unread by date added
        self.assertEqual(['Ёлка','Banana'],listed('added',limit=2))
        self.assertEqual(['Banana','арбуз'],listed(status=['want']))
        self.assertEqual(['Banana','арбуз','Яблоко'],listed(status=['reading','want']))
        self.assertEqual(['Ёлка','Яблоко'],listed(language='ru'))
        self.assertEqual(['арбуз'],listed(language='en'))
        self.assertEqual(['Banana','Ёлка'],listed(added_days=5))
        self.assertEqual(['Banana','арбуз','Ёлка'],listed(added_days=30))
        self.assertEqual(['Banana','Яблоко'],listed(started=True))
        self.assertEqual(['арбуз','Ёлка'],listed(started=False))
        self.assertEqual(['Banana'],listed(started=True,status=['want'],added_days=5))
        self.assertEqual([],listed(started=True,status=['unsorted']))
        # The two books the X3 synced are on it as far as their cards know.
        self.assertEqual(['Banana','Яблоко'],listed(on_x3=True))
        self.assertEqual(['арбуз','Ёлка'],listed(on_x3=False))
        self.assertEqual(['Яблоко'],listed(on_x3=True,status=['reading']))


if __name__=='__main__':unittest.main()
