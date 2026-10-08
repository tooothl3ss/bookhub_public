"""OPDS catalogue for e-readers: the shelf's EPUBs as Atom feeds, the menu the owner arranges.

Written to what CrossPoint 1.6.5 (the X3) reads: lib/OpdsParser/OpdsParser.cpp takes elements by
name, without namespaces, and its limits are in bytes; src/util/UrlUtils.cpp resolves only links
that start at the root; a feed without entries is an error screen. Other readers get plain OPDS 1.x.
"""
import base64
import hmac
import hashlib
import itertools
import json
import logging
import os
import re
import stat
import time
import unicodedata
import uuid
from urllib.parse import quote
from xml.sax.saxutils import escape, quoteattr

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from .sync.store import Problem

ROOT = '/opds'
ATOM = 'application/atom+xml'
NAVIGATION = ATOM + ';profile=opds-catalog;kind=navigation'
ACQUISITION = ATOM + ';profile=opds-catalog;kind=acquisition'
EPUB = 'application/epub+zip'  # the reader compares the type of a book link with exactly this
SEARCH = ROOT + '/search?q={searchTerms}'

# OpdsParser.cpp:9-16 and StringUtils.h:35 of the firmware, all in bytes of UTF-8.
TITLE_BYTES, AUTHOR_BYTES, NAME_BYTES = 160, 120, 100
TITLE_ROOM = 40  # what a title that needs a mark keeps of the file name next to a long author
QUERY_BYTES = 200  # a link to the next page of a search stays within the reader's 768 bytes
LAYOUTS = ('author-title', 'title-author', 'title')  # opdsFilenameFormat on the device; the first is its default
HIDDEN = ('read', 'dropped')  # the owner's decision: never in the catalogue, whatever a section says
NO_AUTHOR = 'Автор не указан'  # what the shelf writes on a card without one
UNTITLED = 'Без названия'

PAGE_SIZE, PAGE_SIZES = 20, range(5, 61)  # the reader keeps 62 entries and scrolls long lists badly
STATUSES = ('reading', 'want', 'unsorted')
FILTERS = ('status', 'language', 'added_days', 'started', 'on_x3')
SORTS = {
    'read': lambda b: (-(b['read_at'] or 0), -(b['added_at'] or 0), -b['id']),
    'added': lambda b: (-(b['added_at'] or 0), -b['id']),
    'title': lambda b: (fold(b['title']), b['id']),
    'author': lambda b: (not b['author'], fold(b['author']), fold(b['title']), b['id']),
    'updated': lambda b: (-(b['updated_at'] or 0), -b['id']),
}
BUILTIN = dict(page_size=PAGE_SIZE, sections=[
    dict(id='reading', title='Читаю', enabled=True, filter=dict(status=['reading']), sort='read', limit=None),
    dict(id='want', title='В планах', enabled=True, filter=dict(status=['want']), sort='added', limit=None),
    dict(id='new', title='Новые', enabled=True, filter=dict(added_days=30), sort='added', limit=30),
    dict(id='all', title='Все', enabled=True, filter={}, sort='title', limit=None),
    dict(id='unsorted', title='Общее', enabled=False, filter=dict(status=['unsorted']), sort='added', limit=None),
    dict(id='not-on-x3', title='Нет на X3', enabled=False, filter=dict(on_x3=False), sort='title', limit=None),
])


# --- text as the reader takes it ---

def plain(text):
    """One line a reader can show: composed characters, single spaces, no control characters,
    ASCII dots for the ellipsis (whether the X3 font has that glyph is unknown)."""
    text = unicodedata.normalize('NFC', text or '').replace('…', '...')
    return ' '.join(''.join(ch for ch in text if ch.isspace() or
                            unicodedata.category(ch) not in ('Cc', 'Cs') and ord(ch) not in (0xFFFE, 0xFFFF)).split())


def fold(text):
    return (text or '').casefold().replace('ё', 'е')


def clip(text, limit):
    """The longest beginning of text within limit bytes: the reader cuts bytes, not characters."""
    return text.encode()[:max(limit, 0)].decode(errors='ignore')


def cut(text, limit):
    return text if len(text.encode()) <= limit else clip(text, limit - 3).rstrip(' ,') + '...'


def squeeze(text, limit):
    """text within limit bytes with its end kept: «Том 2» is what tells two volumes apart."""
    if len(text.encode()) <= limit:
        return text
    tail = text.encode()[-(limit // 3):].decode(errors='ignore').lstrip()
    return clip(text, limit - 3 - len(tail.encode())).rstrip() + '...' + tail


def tongue(code):
    code = re.split('[-_]', (code or '').strip().lower())[0]
    return {'rus': 'ru', 'eng': 'en'}.get(code, code)


def reader_filename(author, title, layout=LAYOUTS[0]):
    """The name CrossPoint 1.6.5 gives a downloaded book: opdsBookFilename (src/util/OpdsFilename.cpp)
    over StringUtils::sanitizeFilename (src/util/StringUtils.cpp). Its KOSync document is MD5 of it."""
    base = title if not author or layout == 'title' else \
        f'{title} - {author}' if layout == 'title-author' else f'{author} - {title}'
    name, size = '', 0
    for ch in base.lstrip(' .'):
        if ch in '/\\:*?"<>|':
            ch = '_'
        elif ord(ch) < 32 or ord(ch) == 127:
            continue
        size += len(ch.encode())
        if size > NAME_BYTES:
            break  # the first character that does not fit ends the name, shorter ones after it are not tried
        name += ch
    return (name.rstrip(' .') or 'book') + '.epub'


def filenames(author, title):
    """Every name the device may choose, its default first; one name when there is no author."""
    return list(dict.fromkeys(reader_filename(author, title, layout) for layout in LAYOUTS))


# --- the catalogue ---

def named(book, taken, held):
    """Title and author of a card as the feeds give them, so that no file name of the card is taken.

    The reader silently deletes a file of the same name, and names differing only in case are one
    file on its card. A name is taken by an earlier card of this pass, and also by any other card a
    reader document of that name is bound to (held): what a reader already keeps under a name stays
    that book's, whichever cards got an EPUB or a new title since. A card whose name is taken gets a
    mark made of its id, which never changes; title and author are shortened until the mark is
    inside every name the reader may build.
    """
    full = plain((book['title_ru'] or '').strip() or book['title']) or UNTITLED
    author = plain(book['authors'])
    author = '' if author == NO_AUTHOR else cut(author, AUTHOR_BYTES)
    title = cut(full, TITLE_BYTES)
    keys = lambda: {name.casefold() for name in filenames(author, title)}
    free = lambda: taken.isdisjoint(keys()) and all(
        held.get(hashlib.md5(name.encode()).hexdigest(), book['id']) == book['id'] for name in filenames(author, title))
    if not free():
        author = cut(author, NAME_BYTES - 3 - TITLE_ROOM)
        room = NAME_BYTES - (len(author.encode()) + 3 if author else 0)
        for attempt in itertools.count(1):
            mark = f"[#{book['id']}]" if attempt == 1 else f"[#{book['id']}-{attempt}]"
            title = squeeze(full, room - len(mark) - 1) + ' ' + mark
            if free():
                break
    taken |= keys()
    return title, author


def catalogue(store):
    """What readers are offered: the cards that have an EPUB, without the read and the dropped.

    Names are settled over every card with an EPUB, by card id: the earlier card keeps the plain
    name, and a book does not change its name when another one is finished or dropped.
    """
    taken, held, books = set(), store.core.bound(), []
    for book in store.core.catalogue():
        title, author = named(book, taken, held)
        if book['status'] not in HIDDEN:
            books.append(dict(book, title=title, author=author, filenames=filenames(author, title),
                              words=fold(plain(' '.join(filter(None, (book['title'], book['title_ru'], book['authors'])))))))
    return books


def x3_present(sections, books):
    """Ids of the cards whose X3 mark is «есть». The verdict belongs to the shelf, which is asked
    only when a section filters by it."""
    if not books or not any('on_x3' in s['filter'] for s in sections):
        return frozenset()
    from . import shelf
    with shelf.db_session() as db:
        cards = shelf.books_out(db, db.query(shelf.Book).filter(shelf.Book.id.in_([b['id'] for b in books])).all())
    return frozenset(card.id for card in cards if card.x3 and card.x3['state'] == 'present')


def select(section, books, on_x3=frozenset(), now=None):
    """The books of a section in its order. Filters and sorts are a closed list applied here."""
    f, now = section['filter'], time.time() if now is None else now
    chosen = [b for b in books
              if ('status' not in f or b['status'] in f['status'])
              and ('language' not in f or tongue(b['language']) == f['language'])
              and ('added_days' not in f or (b['added_at'] or 0) >= now - f['added_days'] * 86400)
              and ('started' not in f or (b['read_at'] is not None) == f['started'])
              and ('on_x3' not in f or (b['id'] in on_x3) == f['on_x3'])]
    chosen.sort(key=SORTS[section['sort']])
    return chosen[:section['limit']] if section['limit'] else chosen


# --- the menu ---

def check_menu(payload, stored=False):
    """The menu in the form it is kept in: known fields checked, anything else dropped."""
    if not isinstance(payload, dict) or not isinstance(payload.get('sections'), list):
        raise Problem('Меню каталога — объект со списком разделов sections')
    size = payload.get('page_size', PAGE_SIZE)
    if type(size) is not int or size not in PAGE_SIZES:
        raise Problem(f'Размер страницы — целое число от {PAGE_SIZES[0]} до {PAGE_SIZES[-1]}')
    if len(payload['sections']) > 30:
        raise Problem('В меню не больше 30 разделов')
    sections, ids = [], set()
    for raw in payload['sections']:
        if not isinstance(raw, dict):
            raise Problem('Раздел меню — объект')
        title = plain(raw['title']) if isinstance(raw.get('title'), str) else ''
        if not 1 <= len(title) <= 60:
            raise Problem('Название раздела — от 1 до 60 символов')
        wrong = lambda message: Problem(f'Раздел «{title}»: {message}')
        ident = raw.get('id')
        if ident in (None, '') and not stored:
            ident = uuid.uuid4().hex[:8]  # a section without an id is new
        if not isinstance(ident, str) or not re.fullmatch('[a-z0-9][a-z0-9-]{0,31}', ident):
            raise wrong('id — до 32 строчных латинских букв, цифр и дефисов')
        if ident in ids:
            raise wrong(f'id «{ident}» повторяется')
        ids.add(ident)
        enabled, given = raw.get('enabled', True), raw.get('filter') or {}
        if type(enabled) is not bool:
            raise wrong('enabled — true или false')
        if not isinstance(given, dict):
            raise wrong('filter — объект')
        if set(given) - set(FILTERS):
            raise wrong('неизвестный фильтр ' + ', '.join(sorted(set(given) - set(FILTERS))))
        # A field the page's form left empty filters nothing; a value of the wrong kind is an error.
        given, chosen = {key: value for key, value in given.items() if value not in (None, '', [])}, {}
        if 'status' in given:
            if not isinstance(given['status'], list) or not all(isinstance(s, str) and s in STATUSES for s in given['status']):
                raise wrong('статусы — список из ' + ', '.join(STATUSES))
            chosen['status'] = [s for s in STATUSES if s in given['status']]
        if 'language' in given:
            if not isinstance(given['language'], str) or len(given['language']) > 16:
                raise wrong('язык — код вроде ru или en')
            if tongue(given['language']):
                chosen['language'] = tongue(given['language'])
        if 'added_days' in given:
            if type(given['added_days']) is not int or not 1 <= given['added_days'] <= 3650:
                raise wrong('срок добавления — целое число дней от 1 до 3650')
            chosen['added_days'] = given['added_days']
        for flag in ('started', 'on_x3'):
            if flag in given:
                if type(given[flag]) is not bool:
                    raise wrong(f'{flag} — true или false')
                chosen[flag] = given[flag]
        order, limit = raw.get('sort', 'title'), raw.get('limit')
        if not isinstance(order, str) or order not in SORTS:
            raise wrong('сортировка — одна из ' + ', '.join(SORTS))
        if limit not in (None, '') and (type(limit) is not int or not 1 <= limit <= 1000):
            raise wrong('ограничение — целое число книг от 1 до 1000 или пусто')
        sections.append(dict(id=ident, title=title, enabled=enabled, filter=chosen, sort=order, limit=limit or None))
    return dict(page_size=size, sections=sections)


def load_menu(store):
    """The owner's menu; the built-in one when nothing usable is stored."""
    with store.connect() as db:
        row = db.execute("SELECT value FROM metadata WHERE key='opds_menu'").fetchone()
    try:
        if row:
            return check_menu(json.loads(row['value']), stored=True)
    except (Problem, ValueError, TypeError):
        logging.warning('Stored OPDS menu is unusable, the built-in one is served')
    return check_menu(BUILTIN, stored=True)


def save_menu(store, payload):
    menu = check_menu(payload)
    with store.connect(write=True) as db:
        db.execute("INSERT INTO metadata VALUES('opds_menu',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (json.dumps(menu, ensure_ascii=False),))
    return menu


def reset_menu(store):
    with store.connect(write=True) as db:
        db.execute("DELETE FROM metadata WHERE key='opds_menu'")


def credentials():
    """The sync account: the catalogue has no login of its own."""
    return os.environ.get('SYNC_USER', 'reader'), os.environ.get('SYNC_PASSWORD', '')


def menu_view(store):
    """GET /api/opds/menu of the shelf: the menu with what each section holds now."""
    menu, books = load_menu(store), catalogue(store)
    on_x3 = x3_present(menu['sections'], books)
    builtin = {s['id'] for s in BUILTIN['sections']}
    return dict(enabled=all(credentials()), path=ROOT, page_size=menu['page_size'],
                sections=[dict(s, count=len(select(s, books, on_x3)), builtin=s['id'] in builtin) for s in menu['sections']],
                filters=dict(status=list(STATUSES), sort=list(SORTS)), default=menu == check_menu(BUILTIN, stored=True))


# --- feeds ---

def link(rel, kind, href):
    return f'<link rel={quoteattr(rel)} type={quoteattr(kind)} href={quoteattr(href)}/>'


def entry(ident, title, links, author='', updated=None):
    # Inside an entry the reader takes title, id, author/name and link by name: nothing else may be
    # called so, and the text of title and name carries no whitespace around it.
    stamp = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(updated or time.time()))
    return (f'<entry><title>{escape(title)}</title><id>urn:bookhub:{ident}</id><updated>{stamp}</updated>'
            + (f'<author><name>{escape(author)}</name></author>' if author else '') + links + '</entry>')


def book_entry(book):
    href = f"{ROOT}/b/{book['id']}-{book['sha256'][:12]}.epub"
    return entry(f"book:{book['id']}", book['title'], link('http://opds-spec.org/acquisition', EPUB, href),
                 book['author'], book['updated_at'])


def notice(text):
    """An empty feed is an error screen on the reader: a line that says why and leads back to the start."""
    return entry('notice', text, link('start', NAVIGATION, ROOT))


def feed(path, title, entries, kind=ACQUISITION, previous=None, following=None):
    # Links start at the root: the reader appends anything else to the address of the feed it shows.
    # It finds search only by a template in the feed itself; the description document is for others.
    links = [link('self', kind, path), link('start', NAVIGATION, ROOT),
             link('search', 'application/opensearchdescription+xml', ROOT + '/opensearch.xml'), link('search', ATOM, SEARCH)]
    links += [link(rel, ACQUISITION, href) for rel, href in (('previous', previous), ('next', following)) if href]
    stamp = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    body = '\n'.join(['<?xml version="1.0" encoding="UTF-8"?>', '<feed xmlns="http://www.w3.org/2005/Atom">',
                      f'<id>urn:bookhub:opds:{escape(path)}</id><title>{escape(title)}</title><updated>{stamp}</updated>',
                      *links, *entries, '</feed>', ''])
    return Response(body, media_type=kind + ';charset=utf-8')


def listing(path, title, books, page, size, empty):
    """One page of books; a page past the end is the last one, an empty list a notice."""
    try:
        page = int(page)
    except ValueError:
        page = 1
    pages = max(1, -(-len(books) // size))
    page = min(max(page, 1), pages)
    address = lambda number: f"{path}{'&' if '?' in path else '?'}page={number}"
    return feed(address(page) if page > 1 else path, title,
                [book_entry(b) for b in books[(page - 1) * size:page * size]] or [notice(empty)],
                previous=address(page - 1) if page > 1 else None, following=address(page + 1) if page < pages else None)


class Gate:
    """Everything under /opds behind the sync account, for reading only.

    CrossPoint sends Basic with its first request and shows any answer but 200 as one error;
    the 401 challenge is for the readers that ask for a password. Bearer and KOSync keys open nothing here.
    """
    def __init__(self, app, user, password):
        self.app, self.user, self.password = app, user.encode(), password.encode()

    def refusal(self, scope):
        if not (self.user and self.password):
            return JSONResponse({'error': 'Каталог выключен: не задан пароль синка'}, 404)
        auth, valid = Headers(scope=scope).get('authorization', ''), False
        if auth[:6].lower() == 'basic ':
            try:
                # Bytes in constant time, as bookhub/sync/api.py does: foreign input is 401, not 500.
                user, password = base64.b64decode(auth[6:].strip(), validate=True).split(b':', 1)
                valid = hmac.compare_digest(user, self.user) & hmac.compare_digest(password, self.password)
            except ValueError:
                pass
        if not valid:
            return JSONResponse({'error': 'Unauthorized'}, 401, headers={'WWW-Authenticate': 'Basic realm="BookHub OPDS"'})
        if scope['method'] not in ('GET', 'HEAD'):
            return JSONResponse({'error': 'Method not allowed'}, 405, headers={'Allow': 'GET, HEAD'})

    async def __call__(self, scope, receive, send):
        refusal = self.refusal(scope) if scope['type'] == 'http' else None
        await (refusal or self.app)(scope, receive, send)


class Handed(FileResponse):
    """A file whose download is recorded once the whole of it has left for a reader that is still there.
    uvicorn drops what is written to a closed connection without a word, so the reader is asked
    before the last piece; a cancelled or lost transfer, a HEAD and a Range record nothing."""
    def __init__(self, *args, taken=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.taken = taken

    async def __call__(self, scope, receive, send):
        request, state = Request(scope, receive), dict(status=None, whole=False)

        async def watched(message):
            if message['type'] == 'http.response.start':
                state['status'] = message['status']
            elif message['type'] == 'http.response.body' and not message.get('more_body'):
                state['whole'] = scope['method'] == 'GET' and state['status'] == 200 and not await request.is_disconnected()
            await send(message)
        await super().__call__(scope, receive, watched)
        if self.taken and state['whole']:
            await run_in_threadpool(self.taken)


def create_opds_app(store, user=None, password=None):
    account = credentials()
    # The reader repeats its password to wherever a redirect points: there are none, /opds and /opds/ both answer.
    app = FastAPI(title='BookHub OPDS', docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
    app.add_middleware(Gate, user=user or account[0], password=password or account[1])
    get = lambda path: app.api_route(path, methods=['GET', 'HEAD'])  # FastAPI answers HEAD only where it is named

    @app.exception_handler(Problem)
    async def problem(request, exc):
        return JSONResponse({'error': str(exc)}, status_code=exc.status)

    @get(ROOT)
    @get(ROOT + '/')
    def root():
        menu, books = load_menu(store), catalogue(store)
        shown = [s for s in menu['sections'] if s['enabled']]
        on_x3, entries = x3_present(shown, books), []
        for s in shown:
            count = len(select(s, books, on_x3))
            if count:  # an empty section would open as an error screen
                entries.append(entry('section:' + s['id'], f"{cut(s['title'], TITLE_BYTES - 16)} ({count})",
                                     link('subsection', ACQUISITION, f"{ROOT}/s/{s['id']}")))
        empty = 'Все разделы каталога пусты или выключены' if books else 'В каталоге пока нет книг'
        return feed(ROOT, 'BookHub', entries or [notice(empty)], NAVIGATION)

    @get(ROOT + '/s/{ident}')
    def section(ident: str, page: str = '1'):
        menu = load_menu(store)
        chosen = next((s for s in menu['sections'] if s['id'] == ident and s['enabled']), None)
        if not chosen:
            raise Problem('Такого раздела в каталоге нет', 404)
        books = catalogue(store)
        return listing(f'{ROOT}/s/{ident}', chosen['title'], select(chosen, books, x3_present([chosen], books)),
                       page, menu['page_size'], 'В этом разделе сейчас нет книг')

    @get(ROOT + '/search')
    def search(q: str = '', page: str = '1'):
        q = clip(plain(q), QUERY_BYTES)
        words = fold(q).split()
        found = sorted((b for b in catalogue(store) if all(word in b['words'] for word in words)), key=SORTS['title'])
        # Quoted whole: the reader takes an address with "://" in it for another server.
        return listing(f"{ROOT}/search?q={quote(q, safe='')}", 'Поиск: ' + q, found, page, load_menu(store)['page_size'], 'Ничего не найдено')

    @get(ROOT + '/opensearch.xml')
    def opensearch():
        return Response('<?xml version="1.0" encoding="UTF-8"?>\n<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">'
                        '<ShortName>BookHub</ShortName><Description>Поиск по каталогу BookHub</Description>'
                        '<InputEncoding>UTF-8</InputEncoding><OutputEncoding>UTF-8</OutputEncoding>'
                        f'<Url type={quoteattr(ACQUISITION)} template={quoteattr(SEARCH)}/></OpenSearchDescription>\n',
                        media_type='application/opensearchdescription+xml;charset=utf-8')

    @get(ROOT + '/b/{name}')
    def download(name: str, request: Request):
        wanted = re.fullmatch(r'(\d{1,18})-([0-9a-f]{12})\.epub', name)
        book = wanted and next((b for b in catalogue(store) if b['id'] == int(wanted[1])), None)
        # Only the primary EPUB of an offered card, under the address its feed entry carries.
        try:
            found = book and book['sha256'][:12] == wanted[2] and os.stat(book['path'])
        except OSError:
            found = None
        if not found or not stat.S_ISREG(found.st_mode):
            raise Problem('Этой книги в каталоге нет', 404)

        def taken():
            agent = request.headers.get('user-agent', '')
            try:
                # The X3 and the X4 share this User-Agent and anyone can send it: a hint for the X3 mark, no more.
                store.core.downloaded(book['id'], book['sha256'], book['filenames'], agent,
                                      book['filenames'][0] if 'crosspoint' in agent.lower() else None)
            except Exception:  # bookkeeping never costs the reader its book
                logging.exception('Cannot record the download of book %s', book['id'])
        # Bytes as stored: KOReader's content digest of the file is already bound by Library.ingest.
        # A proxy that buffers the whole answer would hide a reader that gave up: it is told not to.
        return Handed(book['path'], media_type=EPUB, filename=book['filenames'][0], stat_result=found,
                      headers={'X-Accel-Buffering': 'no'}, taken=taken)

    return app
