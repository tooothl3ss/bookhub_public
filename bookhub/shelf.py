"""MyShelf — интерактивная книжная полка.

Идея: складывать сюда книги, отмечать статус (в планах / читаю / прочитано /
брошено) и держать при каждой книге короткий AI-обзор, по которому можно
решить «стоит читать или нет». Источник данных о книгах — OpenLibrary,
обзоры и рекомендации — настроенный сервис с OpenAI-совместимым API.

Книги с читалок приходят из reader-sync: всё, что лежит на Kindle, само
появляется на полке в «Общем», а на странице книги видно файлы, карты,
EPUB для X3 и текущую позицию.
"""

import os
import re
import json
import time
import shutil
import sqlite3
import difflib
import posixpath
import unicodedata
import hashlib
import tempfile
import logging
import threading
import uuid
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any, Type
from urllib.parse import quote, urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import ROOT, data_root, database_path
from .sync.service import reader_service
from .sync.store import Problem
from .x3 import DeviceError
from fastapi import FastAPI, HTTPException, Query, BackgroundTasks, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from starlette.datastructures import Headers
from pydantic import (
    AliasChoices, BaseModel, ConfigDict, Field, field_validator, field_serializer,
)
from sqlalchemy import (
    Column, Integer, String, Text, DateTime, create_engine, func, or_,
    ForeignKey, event, bindparam, text as sql_text,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker, declarative_base

# ----------------------------
# CONFIG
# ----------------------------
BASE_DIR = str(data_root())

# Грузим .env рядом с этим файлом (если он есть).


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("myshelf")

DB_PATH = str(database_path())
STATIC_DIR = str(ROOT / "myshelf" / "static")
COVERS_DIR = os.path.join(BASE_DIR, "covers")
# EPUB, которые bookbot отправил на Kindle: ждут, пока книга появится в reader-sync.
INCOMING_DIR = os.path.join(BASE_DIR, "incoming")
BACKUP_DIR = os.path.join(BASE_DIR, ".backup")


OPENLIB_BASE = "https://openlibrary.org"
COVERS_BASE = "https://covers.openlibrary.org"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        log.warning("%s is not a valid integer, falling back to %s", name, default)
        return default


def _env_regex(name: str, default: str) -> "re.Pattern[str]":
    raw = os.getenv(name, "").strip() or default
    try:
        return re.compile(raw, re.IGNORECASE)
    except re.error:
        log.warning("%s is not a valid regular expression, falling back to %s", name, default)
        return re.compile(default, re.IGNORECASE)


# Секреты и настройки только из окружения — никаких ключей в коде.
# AI uses /v1/chat/completions on the configured compatible service.
# Empty AI_URL or AI_MODEL disables AI features.
AI_URL = os.getenv("AI_URL", "").strip().rstrip("/")
AI_MODEL = os.getenv("AI_MODEL", "").strip()
# Обзор книги — то, ради чего всё затевалось, поэтому модель для него
# настраивается отдельно (по умолчанию та же, что и для рекомендаций).
AI_SUMMARY_MODEL = os.getenv("AI_SUMMARY_MODEL", "").strip() or AI_MODEL
# Подсказка оригинального названия для поиска в каталогах. Пусто — та же, что AI_MODEL.
AI_HINT_MODEL = os.getenv("AI_HINT_MODEL", "").strip() or AI_MODEL
# Bearer token, if required by the configured AI service.
AI_API_KEY = os.getenv("AI_API_KEY", "").strip()
# Allow time for models that queue requests or generate slowly.
AI_TIMEOUT_SEC = _env_int("AI_TIMEOUT_SEC", 300)

# X3 (CrossPoint): сервис передачи файлов по Wi-Fi, например http://192.168.1.20. Пусто — отправка выключена.
X3_URL = os.getenv("X3_URL", "").strip().rstrip("/")
X3_BOOKS_DIR = os.getenv("X3_BOOKS_DIR", "/").strip() or "/"
X3_UPLOAD_TIMEOUT_SEC = _env_int("X3_UPLOAD_TIMEOUT_SEC", 300)
X3_ENABLED = bool(X3_URL)
# Как X3 называет себя в KOSync: настоящий шлёт device="CrossPoint", device_id="crosspoint-reader".
# Регулярное выражение ищем в обоих полях; по нему синк позиции считается синком именно X3.
X3_SYNC_DEVICE = _env_regex("X3_SYNC_DEVICE", r"crosspoint|\bx3\b")
# Русские издания OpenLibrary почти не знает: для поиска переводим название
# и автора на английский (translate.googleapis.com, без ключа). 0 — выключить.
TRANSLATE_TITLES = os.getenv("TRANSLATE_TITLES", "1").strip().lower() not in ("0", "false", "no", "")

HOST = os.getenv("HOST", "127.0.0.1")
PORT = _env_int("PORT", 8000)
# Полка без авторизации, поэтому чужим сайтам она закрыта: заголовков CORS нет, запись с чужой
# страницы отклоняется. Здесь — адреса (через запятую), которым разрешено и то и другое.
CORS_ORIGINS = [o.strip().rstrip("/") for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
SHELF_HOSTS = {h.strip().lower() for h in os.getenv("SHELF_HOSTS", "").split(",") if h.strip()}

RECOMMENDATION_REFRESH_DAYS = _env_int("RECOMMENDATION_REFRESH_DAYS", 3)

# Язык, издания на котором предпочитаем при выборе обложки/года.
PREFER_LANG = (os.getenv("PREFER_LANG", "ru") or "ru").strip().lower()

# OpenLibrary просит присылать осмысленный User-Agent с контактом.
CONTACT = os.getenv("CONTACT_EMAIL", "").strip()
USER_AGENT = f"MyShelf/1.1 (+https://github.com/; {CONTACT})" if CONTACT else "MyShelf/1.1"

OL_TIMEOUT_SEC = _env_int("OL_TIMEOUT_SEC", 15)
SEARCH_CACHE_TTL_SEC = _env_int("SEARCH_CACHE_TTL_SEC", 300)
MAX_COVER_BYTES = _env_int("MAX_COVER_MB", 5) * 1024 * 1024
# Заглушка «нет обложки» у OpenLibrary весит 43 байта — настоящая обложка больше.
MIN_COVER_BYTES = 1024

# unsorted — «Общее»: книга пришла с читалки, статус ещё не разобран.
VALID_STATUSES = {"unsorted", "want", "reading", "read", "dropped"}
# Почему книга брошена. Текст уходит модели как есть: «бросил» — не всегда «не понравилось».
DROP_REASONS = {
    "disliked": "не понравилась: сюжет, стиль или идеи",
    "slow": "затянуто: не устроил темп",
    "lost_thread": "выпал из чтения: был перерыв, начинать заново не хочется, сама книга нравилась",
    "mood": "не под настроение, не то время",
    "translation": "плохой перевод или издание, к самой книге претензий нет",
}
DROP_NOTE_MAX = 300
RECOMMENDATION_KINDS = {"planned", "external"}
RECOMMENDATION_STATUSES = {"active", "dismissed"}
RUN_STATUSES = {"pending", "ready", "error"}
SUMMARY_STATUSES = {"idle", "pending", "ready", "error"}


def utcnow() -> datetime:
    """Наивный UTC — согласован с тем, как SQLite пишет CURRENT_TIMESTAMP."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso_utc(value: Any) -> Optional[str]:
    """Отдаём время браузеру с явной таймзоной.

    Без суффикса Z строка '2026-02-18T02:45:31' трактуется JS как локальное
    время, и все даты в UI уезжают на оффсет пользователя.
    """
    if value is None:
        return None
    if isinstance(value, str):
        raw = value.strip().replace(" ", "T")
        if raw.endswith("Z") or re.search(r"[+-]\d{2}:\d{2}$", raw):
            return raw
        return raw + "Z"
    if isinstance(value, datetime):
        dt = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    return str(value)


# ----------------------------
# HTTP (OpenLibrary)
# ----------------------------
def _build_http_session() -> requests.Session:
    """Одна сессия на процесс: keep-alive + ретраи с бэкоффом.

    Раньше на добавление книги уходило до 7 отдельных TLS-хендшейков.
    """
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=2,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=20)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return session


HTTP = _build_http_session()


class TTLCache:
    """Крошечный потокобезопасный TTL-кеш для ответов OpenLibrary."""

    def __init__(self, ttl: int, maxsize: int = 256):
        self.ttl = ttl
        self.maxsize = maxsize
        self._data: Dict[Any, Any] = {}
        self._lock = threading.Lock()

    def get(self, key: Any) -> Any:
        if self.ttl <= 0:
            return None
        with self._lock:
            hit = self._data.get(key)
            if not hit:
                return None
            expires_at, value = hit
            if expires_at < time.monotonic():
                self._data.pop(key, None)
                return None
            return value

    def set(self, key: Any, value: Any) -> None:
        if self.ttl <= 0:
            return
        with self._lock:
            if len(self._data) >= self.maxsize:
                now = time.monotonic()
                stale = [k for k, (exp, _) in self._data.items() if exp < now]
                for k in stale:
                    self._data.pop(k, None)
                if len(self._data) >= self.maxsize:
                    self._data.clear()
            self._data[key] = (time.monotonic() + self.ttl, value)


SEARCH_CACHE = TTLCache(SEARCH_CACHE_TTL_SEC)


# ----------------------------
# AI через OpenAI-совместимый API (опционально)
# ----------------------------
class AIError(RuntimeError):
    """Ошибка обращения к модели — в фоне её нельзя кидать как HTTPException."""


AI_ENABLED = bool(AI_URL and AI_MODEL)
if AI_ENABLED:
    log.info("AI via %s (reco=%s, summary=%s, hints=%s).", AI_URL, AI_MODEL, AI_SUMMARY_MODEL, AI_HINT_MODEL)
else:
    log.warning("AI_URL/AI_MODEL are not set — AI summaries/recommendations disabled.")


def ai_chat(prompt: str, model: str, schema: Optional[Type[BaseModel]] = None) -> str:
    """Один запрос к /v1/chat/completions без стриминга.

    schema уходит в response_format: Ollama по нему держит JSON, claude-gateway его игнорирует —
    поэтому промпты сами требуют строгий JSON, а ответ разбирает parse_ai_json.
    """
    if not AI_ENABLED:
        raise AIError("AI не настроен (нет AI_URL / AI_MODEL).")
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    if schema is not None:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema()},
        }
    headers = {"Authorization": f"Bearer {AI_API_KEY}"} if AI_API_KEY else {}
    try:
        r = requests.post(f"{AI_URL}/v1/chat/completions", json=payload, headers=headers, timeout=AI_TIMEOUT_SEC)
        if r.status_code >= 400:
            try:
                detail = (r.json().get("error") or {}).get("message") or r.text
            except ValueError:
                detail = r.text
            raise AIError(f"Модель ответила HTTP {r.status_code}: {str(detail)[:300]}")
        choices = r.json().get("choices") or []
        return (((choices[0] if choices else {}).get("message") or {}).get("content") or "").strip()
    except (requests.RequestException, ValueError) as exc:
        raise AIError(f"Запрос к модели не прошёл: {exc}") from exc


# ----------------------------
# SQLITE MIGRATION
# ----------------------------
def _backup_db(db_path: str, tag: str) -> None:
    """Снимок базы перед любой правкой схемы."""
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        stamp = utcnow().strftime("%Y%m%d-%H%M%S")
        dest = os.path.join(BACKUP_DIR, f"library.db.{tag}-{stamp}")
        src = sqlite3.connect(db_path)
        try:
            dst = sqlite3.connect(dest)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        log.info("DB backup written to %s", dest)
    except Exception:
        log.exception("Could not back up DB before migration (continuing anyway).")


def ensure_sqlite_schema(db_path: str) -> None:
    """Догоняем схему существующей базы до текущих моделей.

    create_all() не трогает уже созданные таблицы, поэтому расхождения между
    моделью и реальной базой надо чинить руками.
    """
    if not os.path.exists(db_path):
        return

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='books'")
        if not cur.fetchone():
            return

        cur.execute("PRAGMA table_info(books)")
        cols = {row[1] for row in cur.fetchall()}
        if not cols:
            return

        pending: List[str] = []
        if "google_id" in cols and "source_id" not in cols:
            # Легаси-имя от версии на Google Books; сейчас там ключ OpenLibrary.
            pending.append("ALTER TABLE books RENAME COLUMN google_id TO source_id")
        if "source_id" not in cols and "google_id" not in cols:
            pending.append("ALTER TABLE books ADD COLUMN source_id TEXT")
        if "year" not in cols:
            pending.append("ALTER TABLE books ADD COLUMN year INTEGER")
        if "language" not in cols:
            pending.append("ALTER TABLE books ADD COLUMN language TEXT")
        if "description" not in cols:
            pending.append("ALTER TABLE books ADD COLUMN description TEXT")
        for name in ("description_ru", "description_translation_job_id"):
            if name not in cols:
                pending.append(f"ALTER TABLE books ADD COLUMN {name} TEXT")
        if "description_translation_status" not in cols:
            pending.append("ALTER TABLE books ADD COLUMN description_translation_status TEXT DEFAULT 'idle'")
        if "created_at" not in cols:
            pending.append("ALTER TABLE books ADD COLUMN created_at DATETIME")
        if "updated_at" not in cols:
            pending.append("ALTER TABLE books ADD COLUMN updated_at DATETIME")
        added_summary_status = "summary_status" not in cols
        if added_summary_status:
            pending.append("ALTER TABLE books ADD COLUMN summary_status TEXT DEFAULT 'idle'")

        if "instance_id" not in cols:
            pending.append("ALTER TABLE books ADD COLUMN instance_id TEXT")
            pending.append("UPDATE books SET instance_id = lower(hex(randomblob(16)))")
        if "summary_job_id" not in cols:
            pending.append("ALTER TABLE books ADD COLUMN summary_job_id TEXT")
        if "drop_reason" not in cols:
            pending.append("ALTER TABLE books ADD COLUMN drop_reason TEXT")
        if "drop_note" not in cols:
            pending.append("ALTER TABLE books ADD COLUMN drop_note TEXT")
        if "title_ru" not in cols:
            pending.append("ALTER TABLE books ADD COLUMN title_ru TEXT")
        if "title_ru_source" not in cols:
            pending.append("ALTER TABLE books ADD COLUMN title_ru_source TEXT")
        if "x3_removed_at" not in cols:
            pending.append("ALTER TABLE books ADD COLUMN x3_removed_at INTEGER")
        cur.execute("PRAGMA table_info(recommendations)")
        rec_cols = {row[1] for row in cur.fetchall()}
        if rec_cols and "title_ru" not in rec_cols:
            pending.append("ALTER TABLE recommendations ADD COLUMN title_ru TEXT")
        cur.execute("PRAGMA table_info(x3_listing)")
        listing_cols = {row[1] for row in cur.fetchall()}
        if listing_cols and "positions" not in listing_cols:
            pending.append("ALTER TABLE x3_listing ADD COLUMN positions TEXT")

        if pending:
            _backup_db(db_path, "premigrate")
            for stmt in pending:
                log.info("migration: %s", stmt)
                cur.execute(stmt)
            conn.commit()

        # Кеш-бастер ?t=... раньше писался прямо в колонку с путём.
        cur.execute("UPDATE books SET cover_path = substr(cover_path, 1, instr(cover_path, '?') - 1) "
                    "WHERE cover_path LIKE '%?%'")
        if cur.rowcount:
            log.info("migration: cleaned %s cover paths from query strings", cur.rowcount)

        # ADD COLUMN ... DEFAULT 'idle' проставляет значение всем строкам сразу,
        # поэтому у книг с готовым обзором статус надо выставить отдельно.
        if added_summary_status:
            cur.execute("UPDATE books SET summary_status = 'ready' "
                        "WHERE summary IS NOT NULL AND trim(summary) <> ''")
            log.info("migration: backfilled summary_status for %s books", cur.rowcount)
        cur.execute("UPDATE books SET summary_status = 'idle' WHERE summary_status IS NULL")
        # Плейсхолдер из старой версии больше не является признаком генерации.
        cur.execute("UPDATE books SET summary = '', summary_status = 'idle' "
                    "WHERE summary = 'Генерирую обзор...'")
        conn.commit()

        # Без уникальности две параллельные вставки создадут дубль книги.
        try:
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_books_source_id ON books(source_id)")
            conn.commit()
        except sqlite3.IntegrityError:
            log.warning("Duplicate source_id values in books — unique index not created.")
    finally:
        conn.close()



# ----------------------------
# DB (SQLAlchemy)
# ----------------------------
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})


def _py_lower(value):
    """LIKE в SQLite регистронезависим только для ASCII — кириллицу чиним сами."""
    return value.lower() if isinstance(value, str) else value


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_conn, _record):
    cur = dbapi_conn.cursor()
    # WAL снижает блокировки при фоновых задачах + threadpool, foreign_keys включает каскады.
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()
    try:
        dbapi_conn.create_function("py_lower", 1, _py_lower, deterministic=True)
    except TypeError:
        dbapi_conn.create_function("py_lower", 1, _py_lower)


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


@contextmanager
def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class Book(Base):
    __tablename__ = "books"

    id = Column(Integer, primary_key=True, index=True)
    # Ключ работы в OpenLibrary, вида /works/OL8514692W.
    source_id = Column(String, nullable=False, unique=True, index=True)
    title = Column(String, default="")
    authors = Column(String, default="")
    status = Column(String, default="want")

    year = Column(Integer, nullable=True)
    language = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    description_ru = Column(Text, nullable=True)
    description_translation_status = Column(String, default="idle")
    description_translation_job_id = Column(String, nullable=True)

    cover_path = Column(String, nullable=True)
    summary = Column(Text, nullable=True)
    summary_status = Column(String, default="idle")
    # Independent of the reusable SQLite integer ID; retained for this row's lifetime.
    instance_id = Column(String, default=lambda: uuid.uuid4().hex)
    summary_job_id = Column(String, nullable=True)
    # Для status=dropped: ключ из DROP_REASONS и свободный комментарий.
    drop_reason = Column(String, nullable=True)
    drop_note = Column(Text, nullable=True)
    # Название русского издания для показа; title остаётся как есть — по нему ищем в каталогах.
    title_ru = Column(String, nullable=True)
    # ai / kindle (из файла на читалке) / manual; none — модель ответила, что русского издания нет.
    title_ru_source = Column(String, nullable=True)
    # Когда файлы книги убрали с X3 кнопкой на полке (unix-время): отправки, синки и списки
    # файлов не новее этого о наличии на X3 уже ничего не говорят. Новая отправка сбрасывает.
    x3_removed_at = Column(Integer, nullable=True)

    created_at = Column(DateTime, server_default=func.now(), nullable=True)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=True)


class RecommendationRun(Base):
    __tablename__ = "recommendation_runs"

    id = Column(Integer, primary_key=True, index=True)
    status = Column(String, default="pending", index=True)
    analysis = Column(Text, default="")
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=True)


class Recommendation(Base):
    __tablename__ = "recommendations"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey("recommendation_runs.id", ondelete="CASCADE"), index=True)
    kind = Column(String, index=True)
    status = Column(String, default="active", index=True)

    # ON DELETE SET NULL: рекомендация хранит копию названия/автора, поэтому
    # переживает удаление книги и не блокирует его.
    book_id = Column(Integer, ForeignKey("books.id", ondelete="SET NULL"), nullable=True, index=True)
    source_id = Column(String, nullable=True, index=True)
    title = Column(String, default="")
    title_ru = Column(String, nullable=True)
    authors = Column(String, default="")
    year = Column(Integer, nullable=True)
    language = Column(String, nullable=True)
    cover = Column(String, default="")
    reason = Column(Text, default="")
    search_query = Column(String, default="")

    created_at = Column(DateTime, server_default=func.now(), nullable=True)


class ReaderLink(Base):
    """Файл из reader-sync (ключ Kindle, cdeKey) ↔ книга на полке.

    У одной книги может быть несколько файлов (два издания на Kindle).
    book_id = NULL — книгу с полки удалили: заново её не импортируем.
    """

    __tablename__ = "reader_links"

    key = Column(String, primary_key=True)
    book_id = Column(Integer, ForeignKey("books.id", ondelete="SET NULL"), nullable=True, index=True)
    # Последний снимок из reader-sync: полка показывает его, даже если сервис лежит.
    snapshot = Column(Text, default="{}")
    # Чем нашли работу в каталоге: wikidata / translate / existing / none.
    match = Column(String, default="none")
    created_at = Column(DateTime, server_default=func.now(), nullable=True)
    synced_at = Column(DateTime, nullable=True)


class IncomingEpub(Base):
    """EPUB, отправленный на Kindle через bookbot.

    Kindle сохраняет книгу под именем отправленного файла + «_<cdeKey>.kfx» — по нему EPUB
    сам находит свою книгу в reader-sync и уходит туда для синхронизации с X3.
    """

    __tablename__ = "incoming_epubs"

    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String, nullable=False)
    sha256 = Column(String, nullable=False, unique=True, index=True)
    size = Column(Integer, default=0)
    source = Column(String, default="bookbot")
    # waiting — книги ещё нет на Kindle (или reader-sync пока не принял); attached — ушёл в reader-sync;
    # rejected — reader-sync отказал окончательно: сам больше не пробуем, связать можно руками.
    status = Column(String, default="waiting", index=True)
    keys = Column(String, default="")  # cdeKey через запятую, куда прицеплен
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=True)
    attached_at = Column(DateTime, nullable=True)


class X3Upload(Base):
    """Сохранённая история и приостановленные заявки старой версии приложения."""

    __tablename__ = "x3_uploads"

    key = Column(String, primary_key=True)  # cdeKey книги Kindle: EPUB лежит в reader-sync
    filename = Column(String, nullable=False)
    source = Column(String, default="bookbot")  # bookbot / manual
    # queued — ждёт X3 или проверки EPUB; sent — лежит на X3 и связан; error — отправлять нечего.
    status = Column(String, default="queued", index=True)
    error = Column(Text, nullable=True)
    attempts = Column(Integer, default=0)
    created_at = Column(DateTime, server_default=func.now(), nullable=True)
    sent_at = Column(DateTime, nullable=True)


class X3Listing(Base):
    """Последний удачно полученный список файлов папки книг на X3 — одна строка.

    X3 редко в сети: по этому списку полка помнит, какие книги лежали на нём при последней связи.
    """

    __tablename__ = "x3_listing"

    id = Column(Integer, primary_key=True)  # всегда 1
    checked_at = Column(Integer, nullable=False)  # unix-время, как в deliveries и reader_observations
    directory = Column(String, default="/")
    files = Column(Text, default="[]")  # JSON: [{"name": ..., "size": ..., "epub": bool}]
    revision = Column(Integer, default=0)  # растёт с каждой записью — по нему страницы перечитывают карточки
    # JSON: {путь файла на X3: его progress.bin при последней связи} — см. x3_take_positions.
    positions = Column(Text, nullable=True)


class DeletedBook(Base):
    """Запись об удалённой карточке: остаётся вместо её файлов.

    По ней карточка такой же книги, добавленная позже, получает пометку «вы её удаляли», а файлы
    удалённой книги убираются с X3 при первой связи с ним.
    """

    __tablename__ = "deleted_books"

    id = Column(Integer, primary_key=True)  # свой номер: id книги SQLite выдаёт повторно
    title = Column(String, default="")
    title_ru = Column(String, nullable=True)
    authors = Column(String, default="")
    source_id = Column(String, nullable=True)
    status = Column(String, nullable=True)
    deleted_at = Column(Integer, nullable=False)  # unix-время
    x3_files = Column(Text, default="[]")  # JSON: имена файлов книги, которые ещё надо убрать с X3
    # Пометку «вы её удаляли» закрыли. Сама запись остаётся: её x3_files ещё ждут связи с X3,
    # а номер не достанется другой книге.
    dismissed = Column(Integer, default=0, nullable=False)


class DeletedEpub(Base):
    """SHA-256 каждого EPUB удалённой карточки: тот же файл узнаётся при повторном добавлении,
    а обработчик не импортирует его заново из «входящих»."""

    __tablename__ = "deleted_epubs"

    sha256 = Column(String, primary_key=True)
    deleted_book_id = Column(Integer, index=True)


# ----------------------------
# FastAPI
# ----------------------------
app = FastAPI(title="MyShelf", version="1.1")
if CORS_ORIGINS:
    app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"])


class SameOriginWrites:
    """Запись с чужого сайта — 403.

    Без заголовков CORS браузер не отдаёт чужой странице ответы полки, но «простой» запрос
    (форма, POST без тела) отправляет без спроса — а полка его исполнит. У такого запроса есть
    Origin: пускаем свой адрес и CORS_ORIGINS. Сравниваем хост, а не схему: за прокси с TLS полка
    видит http. Запросы без Origin (bookbot, curl, читалки) не затрагиваются.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] not in ("GET", "HEAD", "OPTIONS"):
            headers = Headers(scope=scope)
            origin = headers.get("origin")
            host = headers.get("host") or ""
            # Чужое имя, перепривязанное на адрес полки (DNS rebinding), даёт равные Origin и Host:
            # если свои имена перечислены в SHELF_HOSTS, запись принимается только под ними.
            foreign = bool(SHELF_HOSTS) and host.lower() not in SHELF_HOSTS
            if origin and origin not in CORS_ORIGINS and (urlsplit(origin).netloc != host or foreign):
                log.warning("Refused %s %s from %s (Host: %s)", scope["method"], scope["path"], origin, headers.get("host"))
                response = JSONResponse({"detail": "Запрос с другого сайта отклонён."}, status_code=403)
                return await response(scope, receive, send)
        await self.app(scope, receive, send)


app.add_middleware(SameOriginWrites)


@app.get("/")
def root():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static/covers", StaticFiles(directory=COVERS_DIR, check_dir=False), name="covers")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ----------------------------
# Schemas
# ----------------------------
class SearchItem(BaseModel):
    source: str = "openlibrary"
    source_id: str
    title: str
    authors: str
    year: Optional[int] = None
    language: Optional[str] = None
    cover: str = ""
    # Найдено по подсказке модели: во что она превратила запрос («Пыль» → Dust / Hugh Howey).
    hint: Optional[str] = None
    # Название русского издания — от модели, только для AI-находок.
    title_ru: Optional[str] = None


class BookAddSchema(BaseModel):
    # google_id — легаси-имя поля, принимаем оба ради совместимости со старым фронтом.
    source_id: str = Field(validation_alias=AliasChoices("source_id", "google_id"))
    status: str = Field(default="want")
    # Название из выдачи поиска: пригодится, если в каталоге работа записана иероглифами.
    title: Optional[str] = None
    # Русское название из AI-поиска — сразу на полку, без отдельного запроса к модели.
    title_ru: Optional[str] = None

    @field_validator("source_id", mode="before")
    @classmethod
    def _strip(cls, v):
        return (v or "").strip()


class BookPatchSchema(BaseModel):
    """Частичное обновление: шлём только то, что реально меняем."""

    model_config = ConfigDict(extra="ignore")

    title: Optional[str] = None
    authors: Optional[str] = None
    status: Optional[str] = None
    summary: Optional[str] = None
    description: Optional[str] = None
    year: Optional[int] = None
    language: Optional[str] = None
    drop_reason: Optional[str] = None
    drop_note: Optional[str] = Field(default=None, max_length=DROP_NOTE_MAX)
    # Пусто — показывать оригинал; модель после ручной правки название не трогает.
    title_ru: Optional[str] = Field(default=None, max_length=300)

    @field_validator("drop_reason", "drop_note", "title_ru", mode="before")
    @classmethod
    def _blank_drop_to_none(cls, v):
        if isinstance(v, str):
            return v.strip() or None
        return v

    @field_validator("year", mode="before")
    @classmethod
    def _blank_year_to_none(cls, v):
        # <input type="number"> при пустом поле присылает "" — это не ошибка ввода.
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("language", mode="before")
    @classmethod
    def _blank_lang_to_none(cls, v):
        if isinstance(v, str) and not v.strip():
            return None
        return v


class BookOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_id: Optional[str] = None
    title: str = ""
    authors: str = ""
    status: str = "want"
    year: Optional[int] = None
    language: Optional[str] = None
    description: Optional[str] = None
    description_ru: Optional[str] = None
    description_translation_status: Optional[str] = "idle"
    cover_path: Optional[str] = None
    summary: Optional[str] = None
    summary_status: Optional[str] = "idle"
    drop_reason: Optional[str] = None
    drop_note: Optional[str] = None
    title_ru: Optional[str] = None
    title_ru_source: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    # Файлы этой книги на читалках (краткие снимки из reader-sync).
    reader: List[Dict[str, Any]] = Field(default_factory=list)
    epubs: List[Dict[str, Any]] = Field(default_factory=list)
    devices: List[Dict[str, Any]] = Field(default_factory=list)
    deliveries: List[Dict[str, Any]] = Field(default_factory=list)
    # Есть ли книга на X3 (x3_by_book); None — судить не по чему или отсутствие ожидаемо.
    x3: Optional[Dict[str, Any]] = None
    # Такую книгу уже удаляли с полки: {"id", "deleted_at", "title"} записи об удалении.
    deleted_before: Optional[Dict[str, Any]] = None

    @field_serializer("created_at", "updated_at")
    def _ser_dt(self, value: Optional[datetime], _info):
        return iso_utc(value)


class MergeSchema(BaseModel):
    target_id: int


class ReaderLinkSchema(BaseModel):
    book_id: int


class DocumentBindSchema(BaseModel):
    key: Optional[str] = None
    book_id: Optional[int] = None
    sha256: Optional[str] = None


class RecommendationDismissSchema(BaseModel):
    status: str = Field(default="dismissed")


# Схемы ответов модели — уходят в response_format; разбор — parse_ai_json.
class PlannedPick(BaseModel):
    book_id: int
    reason: str = ""


class RecoPlan(BaseModel):
    analysis: str = ""
    planned_picks: List[PlannedPick] = Field(default_factory=list)
    search_queries: List[str] = Field(default_factory=list)


class ExternalPick(BaseModel):
    source_id: str
    reason: str = ""
    title_ru: Optional[str] = None


class RecoChoice(BaseModel):
    external_picks: List[ExternalPick] = Field(default_factory=list)


# ----------------------------
# Helpers
# ----------------------------
LANG2_TO_3 = {"ru": "rus", "en": "eng"}
LANG3_TO_2 = {"rus": "ru", "eng": "en"}


def lang_to_ol(lang2: str) -> str:
    lang2 = (lang2 or "").strip().lower()
    return LANG2_TO_3.get(lang2, lang2)


def lang_from_ol(code: Optional[str]) -> Optional[str]:
    if not code:
        return None
    code = code.strip().lower()
    return LANG3_TO_2.get(code, code)


def escape_like(value: str) -> str:
    """% и _ из пользовательского ввода — литералы, а не подстановки."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def safe_year_from_str(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    m = re.search(r"(\d{4})", s)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


# Без default=false OpenLibrary на отсутствующую обложку отвечает 200 и отдаёт
# 43-байтную заглушку без Content-Type — отличить её от картинки нельзя.
def cover_url_from_cover_id(cover_id: int, size: str = "M") -> str:
    return f"{COVERS_BASE}/b/id/{cover_id}-{size}.jpg?default=false"


def cover_url_from_isbn(isbn: str, size: str = "M") -> str:
    return f"{COVERS_BASE}/b/isbn/{isbn}-{size}.jpg?default=false"


def normalize_authors(author_names: Optional[List[str]]) -> str:
    """OpenLibrary regularly returns the same author twice — склеиваем."""
    if not author_names:
        return "Автор не указан"
    seen = set()
    unique = []
    for name in author_names:
        name = (name or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(name)
    return ", ".join(unique) or "Автор не указан"


def ol_get(path: str, params: Optional[dict] = None) -> dict:
    url = f"{OPENLIB_BASE}{path}"
    r = HTTP.get(url, params=params or {}, timeout=OL_TIMEOUT_SEC)
    r.raise_for_status()
    return r.json()


def ol_search(q: str, limit: int = 15) -> List[Dict[str, Any]]:
    cache_key = ("search", q, limit)
    cached = SEARCH_CACHE.get(cache_key)
    if cached is not None:
        return cached
    fields = "key,title,author_name,first_publish_year,language,cover_i,isbn"
    data = ol_get("/search.json", params={"q": q, "limit": limit, "fields": fields})
    docs = data.get("docs") or []
    SEARCH_CACHE.set(cache_key, docs)
    return docs


def pick_best_cover(doc: Dict[str, Any]) -> str:
    if doc.get("cover_i"):
        return cover_url_from_cover_id(doc["cover_i"], size="M")
    isbns = doc.get("isbn") or []
    if isbns:
        return cover_url_from_isbn(isbns[0], size="M")
    return ""


def work_description(work: Dict[str, Any]) -> str:
    desc = work.get("description")
    if isinstance(desc, str):
        return desc
    if isinstance(desc, dict):
        return desc.get("value") or ""
    return ""


def edition_languages(ed: Dict[str, Any]) -> List[str]:
    out = []
    for l in ed.get("languages") or []:
        key = (l or {}).get("key") or ""
        if key:
            out.append(key.rsplit("/", 1)[-1].lower())
    return out


def _edition_score(ed: Dict[str, Any], prefer_lang: str = PREFER_LANG) -> int:
    """Издание на предпочитаемом языке лучше английского, с обложкой — лучше без."""
    langs = edition_languages(ed)
    prefer3 = lang_to_ol(prefer_lang)
    s = 0
    if prefer3 and prefer3 in langs:
        s += 200
    if "eng" in langs:
        s += 100
    if ed.get("covers"):
        s += 50
    elif ed.get("isbn_13") or ed.get("isbn_10"):
        s += 25
    if ed.get("publish_date"):
        s += 1
    return s


def pick_best_edition(
    editions: List[Dict[str, Any]], prefer_lang: str = PREFER_LANG
) -> Optional[Dict[str, Any]]:
    """Выбираем издание: сначала на предпочитаемом языке, потом английское."""
    if not editions:
        return None
    return max(editions, key=lambda ed: _edition_score(ed, prefer_lang))


def download_cover(book_id: int, url: str) -> Optional[str]:
    """Качаем обложку во временный файл и подменяем атомарно."""
    if not url:
        return None
    final_path = os.path.join(COVERS_DIR, f"{book_id}.jpg")
    tmp_path = f"{final_path}.tmp"
    try:
        with HTTP.get(url, timeout=OL_TIMEOUT_SEC, stream=True) as r:
            if r.status_code != 200:
                log.info("Cover candidate %s returned HTTP %s", url, r.status_code)
                return None
            ctype = r.headers.get("Content-Type", "")
            if not ctype.startswith("image/"):
                log.info("Cover candidate %s is not an image (Content-Type=%r)", url, ctype)
                return None

            written = 0
            with open(tmp_path, "wb") as f:
                for chunk in r.iter_content(64 * 1024):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > MAX_COVER_BYTES:
                        raise ValueError(f"cover exceeds {MAX_COVER_BYTES} bytes")
                    f.write(chunk)

        if written < MIN_COVER_BYTES:
            raise ValueError(f"cover is suspiciously small ({written} bytes)")

        os.replace(tmp_path, final_path)
        return f"/static/covers/{book_id}.jpg"
    except Exception:
        log.warning("Failed to download cover for book %s from %s", book_id, url, exc_info=True)
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return None


def download_first_cover(book_id: int, urls: List[str]) -> Optional[str]:
    """Перебираем кандидатов, пока не получим настоящую картинку."""
    seen = set()
    for url in urls:
        if not url or url in seen:
            continue
        seen.add(url)
        path = download_cover(book_id, url)
        if path:
            return path
    return None


def edition_cover_urls(ed: Dict[str, Any], size: str = "L") -> List[str]:
    urls = [cover_url_from_cover_id(cid, size) for cid in (ed.get("covers") or []) if cid and cid > 0]
    for isbn_field in ("isbn_13", "isbn_10"):
        for isbn in (ed.get(isbn_field) or [])[:2]:
            if isbn:
                urls.append(cover_url_from_isbn(isbn, size))
    return urls


def cover_candidates_for_work(work_key: str, max_editions: int = 5) -> List[str]:
    """Список URL-кандидатов на обложку, от самого подходящего издания к запасным."""
    candidates: List[str] = []

    try:
        eds = ol_get(f"{work_key}/editions.json", params={"limit": 200})
        entries = eds.get("entries") or []
        # Первым идёт лучшее издание, дальше — остальные как запасной вариант.
        ranked = sorted(entries, key=_edition_score, reverse=True)[:max_editions]
        for ed in ranked:
            candidates.extend(edition_cover_urls(ed))
    except Exception:
        log.warning("editions lookup failed for %s", work_key, exc_info=True)

    try:
        work = ol_get(f"{work_key}.json")
        for cid in (work.get("covers") or []):
            if cid and cid > 0:
                candidates.append(cover_url_from_cover_id(cid, "L"))
    except Exception:
        log.warning("work lookup failed for %s", work_key, exc_info=True)

    return candidates


def fetch_work_bundle(work_key: str) -> Dict[str, Any]:
    """Собирает всё об издании из OpenLibrary. Без обращений к БД.

    Держать сессию SQLite открытой на время 5-7 сетевых запросов — плохая идея,
    поэтому вся сеть живёт здесь и только потом пишем результат.
    """
    work = ol_get(f"{work_key}.json")

    author_keys = []
    for a in (work.get("authors") or [])[:4]:
        key = ((a.get("author") or {}).get("key")) if isinstance(a, dict) else None
        if key:
            author_keys.append(key)

    def author_name(key: str) -> Optional[str]:
        try:
            return (ol_get(f"{key}.json") or {}).get("name")
        except Exception:
            log.warning("Author lookup failed for %s", key, exc_info=True)
            return None

    author_names: List[str] = []
    if author_keys:
        # Последовательно это до 4 секунд на ровном месте.
        with ThreadPoolExecutor(max_workers=min(4, len(author_keys))) as pool:
            author_names = [n for n in pool.map(author_name, author_keys) if n]

    bundle: Dict[str, Any] = {
        "title": work.get("title") or "Без названия",
        "authors": normalize_authors(author_names),
        "description": work_description(work),
        "year": None,
        "language": None,
        "cover_urls": [],
    }

    try:
        entries = (ol_get(f"{work_key}/editions.json", params={"limit": 200}) or {}).get("entries") or []
        ranked = sorted(entries, key=_edition_score, reverse=True)
        ed = ranked[0] if ranked else None
        if ed:
            bundle["year"] = safe_year_from_str(ed.get("publish_date"))

            langs = edition_languages(ed)
            if langs:
                bundle["language"] = lang_from_ol(langs[0])

            if not bundle["description"]:
                ed_desc = ed.get("description")
                if isinstance(ed_desc, str):
                    bundle["description"] = ed_desc
                elif isinstance(ed_desc, dict):
                    bundle["description"] = ed_desc.get("value") or ""

        for candidate in ranked[:5]:
            bundle["cover_urls"].extend(edition_cover_urls(candidate))
    except Exception:
        log.warning("Editions lookup failed for %s", work_key, exc_info=True)

    for cid in (work.get("covers") or []):
        if cid and cid > 0:
            bundle["cover_urls"].append(cover_url_from_cover_id(cid, "L"))

    return bundle


# ----------------------------
# AI: обзор книги
# ----------------------------
SUMMARY_PROMPT = (
    "Напиши ОЧЕНЬ краткий обзор (2-3 предложения) книги.\n"
    "Название: {title}\n"
    "Автор(ы): {authors}\n"
    "Описание (если есть): {description}\n\n"
    "Требования:\n"
    "- На русском\n"
    "- Иронично, по делу\n"
    "- Если знаешь, пиши в начале русское название произведения\n"
    "- Без спойлеров, но понятно о чем, без абстракций\n"
    "- Не больше 3 предложений\n"
    "- Формат ответа:\n"
    "'Название книги'\n\n"
    "Автор\n\n Твой интересный обзор/описание"
)


def _set_summary_state(book_id: int, job_id: str, status: str, summary: Optional[str] = None) -> None:
    # One atomic conditional write: edits, replacement jobs and reused IDs invalidate it.
    values = {"summary_status": status, "summary_job_id": None, "updated_at": utcnow()}
    if summary is not None:
        values["summary"] = summary
    with db_session() as db:
        db.query(Book).filter(
            Book.id == book_id, Book.summary_job_id == job_id,
            Book.summary_status == "pending",
        ).update(values, synchronize_session=False)
        db.commit()


def generate_summary_background(book_id: int, title: str, authors: str, description: str, job_id: str) -> None:
    if not AI_ENABLED:
        return

    with db_session() as db:
        if not db.query(Book.id).filter(
            Book.id == book_id, Book.summary_job_id == job_id,
            Book.summary_status == "pending",
        ).first():
            return

    prompt = SUMMARY_PROMPT.format(
        title=title, authors=authors, description=(description or "")[:900]
    )

    try:
        summary_text = ai_chat(prompt, AI_SUMMARY_MODEL)
    except AIError:
        log.exception("AI summary generation failed for book %s", book_id)
        _set_summary_state(book_id, job_id, "error")
        return

    if not summary_text:
        log.warning("AI returned an empty summary for book %s", book_id)
        _set_summary_state(book_id, job_id, "error")
        return

    _set_summary_state(book_id, job_id, "ready", summary_text)


# ----------------------------
# AI: рекомендации
# ----------------------------
def parse_ai_json(text: str) -> Dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start:end + 1]
    return json.loads(raw)


def ai_structured(prompt: str, schema: Type[BaseModel], model: Optional[str] = None) -> BaseModel:
    """Просим модель вернуть JSON по схеме; parse_ai_json снимает markdown-обёртку, если она есть."""
    text = ai_chat(prompt, model or AI_MODEL, schema)
    try:
        return schema.model_validate(parse_ai_json(text))
    except Exception as exc:
        log.error("AI returned an unusable payload: %r", text[:500])
        raise AIError("Модель вернула некорректный ответ.") from exc


def book_to_recommendation_context(book: Book) -> Dict[str, Any]:
    context = {
        "id": book.id,
        "title": book.title,
        "title_ru": book.title_ru,
        "authors": book.authors,
        "status": book.status,
        "year": book.year,
        "language": book.language,
        "description": (book.description or "")[:700],
        "summary": (book.summary or "")[:500],
    }
    if book.status == "dropped":
        # Причина хранится и после смены статуса, но модели нужна только у брошенных.
        context["drop_reason"] = DROP_REASONS.get(book.drop_reason or "", "причина не указана")
        if (book.drop_note or "").strip():
            context["drop_note"] = book.drop_note.strip()
    return context


def search_item_from_doc(doc: Dict[str, Any], query: str) -> Optional[Dict[str, Any]]:
    work_key = doc.get("key")
    if not work_key:
        return None
    langs = doc.get("language") or []
    lang2 = lang_from_ol(langs[0]) if isinstance(langs, list) and langs else None
    return {
        "source": "openlibrary",
        "source_id": work_key,
        "title": doc.get("title") or "Без названия",
        "authors": normalize_authors(doc.get("author_name") or []),
        "year": doc.get("first_publish_year"),
        "language": lang2,
        "cover": pick_best_cover(doc),
        "search_query": query,
    }


def collect_openlibrary_candidates(search_queries: List[str], existing_keys: set) -> List[Dict[str, Any]]:
    seen = set(existing_keys)
    candidates: List[Dict[str, Any]] = []
    for q in search_queries[:8]:
        q = (q or "").strip()
        if len(q) < 2:
            continue
        try:
            docs = ol_search(q, limit=10)
        except Exception:
            log.warning("Candidate search failed for %r", q, exc_info=True)
            continue
        for doc in docs:
            item = search_item_from_doc(doc, q)
            if not item or item["source_id"] in seen:
                continue
            seen.add(item["source_id"])
            candidates.append(item)
            if len(candidates) >= 40:
                return candidates
    return candidates


def latest_ready_run(db) -> Optional[RecommendationRun]:
    return (
        db.query(RecommendationRun)
        .filter(RecommendationRun.status == "ready")
        .order_by(RecommendationRun.created_at.desc().nullslast(), RecommendationRun.id.desc())
        .first()
    )


def latest_run_of_any_status(db) -> Optional[RecommendationRun]:
    return (
        db.query(RecommendationRun)
        .order_by(RecommendationRun.created_at.desc().nullslast(), RecommendationRun.id.desc())
        .first()
    )


def pending_run(db) -> Optional[RecommendationRun]:
    return (
        db.query(RecommendationRun)
        .filter(RecommendationRun.status == "pending")
        .order_by(RecommendationRun.id.desc())
        .first()
    )


def recommendation_run_is_fresh(run: Optional[RecommendationRun]) -> bool:
    if not run or not run.created_at:
        return False
    created_at = run.created_at
    if isinstance(created_at, str):
        try:
            created_at = datetime.fromisoformat(created_at)
        except ValueError:
            return False
    return created_at >= utcnow() - timedelta(days=RECOMMENDATION_REFRESH_DAYS)


def serialize_recommendation(rec: Recommendation, books_by_id: Dict[int, Book], books_by_source: Dict[str, Book]) -> Dict[str, Any]:
    book = (books_by_id.get(rec.book_id) if rec.kind == "planned"
            else books_by_source.get(rec.source_id))
    available = (bool(book and book.status == "want") if rec.kind == "planned"
                 else bool(rec.source_id and not book))
    return {
        "id": rec.id,
        "kind": rec.kind,
        "status": rec.status,
        "book_id": book.id if book else None,
        "book_status": book.status if book else None,
        "available": available,
        "source_id": rec.source_id,
        "title": book.title if book else rec.title,
        "title_ru": book.title_ru if book else rec.title_ru,
        "authors": book.authors if book else rec.authors,
        "year": book.year if book else rec.year,
        "language": book.language if book else rec.language,
        "cover": (book.cover_path if book else rec.cover) or "",
        "cover_version": iso_utc(book.updated_at) if book else None,
        "reason": rec.reason,
        "search_query": rec.search_query,
    }


def serialize_recommendation_run(db, run: Optional[RecommendationRun], archive: bool = False) -> Dict[str, Any]:
    is_pending = bool(pending_run(db))
    last_run = latest_run_of_any_status(db)
    last_error = last_run.error if (last_run and last_run.status == "error") else None

    base = {
        "pending": is_pending,
        "last_error": last_error,
        "refresh_days": RECOMMENDATION_REFRESH_DAYS,
    }

    if not run:
        return {**base, "run": None, "stale": True, "planned": [], "external": [], "analysis": ""}

    recs = (
        db.query(Recommendation)
        .filter(Recommendation.run_id == run.id, Recommendation.status == "active")
        .order_by(Recommendation.id.asc())
        .all()
    )
    book_ids = [r.book_id for r in recs if r.book_id]
    source_ids = [r.source_id for r in recs if r.source_id]
    books = db.query(Book).filter(or_(Book.id.in_(book_ids), Book.source_id.in_(source_ids))).all()
    books_by_id = {b.id: b for b in books}
    books_by_source = {b.source_id: b for b in books}
    items = [serialize_recommendation(r, books_by_id, books_by_source) for r in recs]
    if not archive:
        items = [item for item in items if item["available"]]

    return {
        **base,
        "run": {
            "id": run.id,
            "created_at": iso_utc(run.created_at),
            "status": run.status,
            "error": run.error,
        },
        "stale": not recommendation_run_is_fresh(run),
        "planned": [i for i in items if i["kind"] == "planned"],
        "external": [i for i in items if i["kind"] == "external"],
        "analysis": run.analysis or "",
    }


_recommendations_lock = threading.Lock()

DROP_REASON_GUIDE = (
    "У status=dropped есть drop_reason — почему книга брошена, иногда drop_note — слова читателя.\n"
    "Отрицательный сигнал о вкусе — только «не понравилась» и «затянуто» (второе — про темп, не про тему). "
    "«Выпал из чтения» — книга нравилась: считай её положительным сигналом, как прочитанную. "
    "«Не под настроение» и «плохой перевод» ничего не говорят против содержания. "
    "«Причина не указана» — слабый сигнал, не делай по нему выводов.\n"
)

PLAN_PROMPT = (
    "Ты рекомендательная система для личной библиотеки.\n"
    "Проанализируй прочитанное, читаемое, планы и брошенные книги.\n"
    "status=unsorted — книга лежит на читалке, но ещё не разобрана (не прочитана и не в планах).\n"
    "Нужно вернуть строго JSON без markdown.\n"
    "Выбери до 3 книг из status=want, которые стоит читать следующими, и составь 5-8 поисковых "
    "запросов для OpenLibrary, чтобы найти новые книги-кандидаты.\n"
    + DROP_REASON_GUIDE +
    "Не советуй книги, похожие на брошенные из-за «не понравилась», и медленные — если бросал из-за темпа.\n"
    "reason должен быть развернутым: 4-6 предложений на русском. Объясни, почему это стоит читать "
    "именно сейчас, какие элементы вкуса читателя совпадают, чем книга может зацепить, и какой у нее "
    "ожидаемый вайб/темп. Без спойлеров, без общих фраз вроде 'интересная книга'.\n"
    "Формат:\n"
    "{{\n"
    '  "analysis": "2-4 предложения на русском о вкусах читателя",\n'
    '  "planned_picks": [{{"book_id": 123, "reason": "4-6 предложений конкретной причины"}}],\n'
    '  "search_queries": ["author title", "genre comparable book"]\n'
    "}}\n\n"
    "Библиотека JSON:\n{library}"
)

CHOOSE_PROMPT = (
    "Выбери 3-5 новых книжных кандидатов из списка OpenLibrary для этой личной библиотеки.\n"
    "Используй только source_id из candidates. Не выдумывай книги и ID.\n"
    "reason должен быть развернутым: 4-6 предложений на русском. Привяжи рекомендацию к уже "
    "прочитанному/планам, объясни ожидаемый тон, конфликт или идею, и честно обозначь, почему это не "
    "случайный похожий тайтл. Без спойлеров.\n"
    + DROP_REASON_GUIDE +
    "title_ru — под каким названием книга выходила на русском (не дословный перевод); не издавалась — null.\n"
    "Верни строго JSON без markdown.\n"
    "Формат:\n"
    "{{\n"
    '  "external_picks": [{{"source_id": "/works/...", "reason": "4-6 предложений конкретной причины", '
    '"title_ru": "название русского издания или null"}}]\n'
    "}}\n\n"
    "Анализ вкусов:\n{analysis}\n\n"
    "Библиотека JSON:\n{library}\n\n"
    "Candidates JSON:\n{candidates}"
)


def run_recommendations_job(run_id: int) -> None:
    """Фоновая генерация подборки. Ошибки складываем в сам прогон."""
    if not _recommendations_lock.acquire(blocking=False):
        # Параллельный запуск: помечаем прогон и выходим, не жжём токены дважды.
        with db_session() as db:
            run = db.get(RecommendationRun, run_id)
            if run and run.status == "pending":
                run.status = "error"
                run.error = "Другая генерация уже идёт."
                db.commit()
        return

    try:
        _run_recommendations_locked(run_id)
    except AIError as exc:
        log.warning("Recommendation run %s failed: %s", run_id, exc)
        _fail_run(run_id, str(exc))
    except Exception:
        log.exception("Recommendation run %s crashed", run_id)
        _fail_run(run_id, "Внутренняя ошибка при генерации подборки.")
    finally:
        _recommendations_lock.release()


def _fail_run(run_id: int, message: str) -> None:
    try:
        with db_session() as db:
            run = db.get(RecommendationRun, run_id)
            if run:
                run.status = "error"
                run.error = message
                db.commit()
    except Exception:
        log.exception("Could not mark run %s as failed", run_id)


def _run_recommendations_locked(run_id: int) -> None:
    with db_session() as db:
        run = db.get(RecommendationRun, run_id)
        if not run or run.status != "pending":
            return

        books = db.query(Book).order_by(Book.updated_at.desc().nullslast(), Book.id.desc()).all()
        if not books:
            run.status = "error"
            run.error = "Библиотека пуста — рекомендовать нечего."
            db.commit()
            return

        library_context = [book_to_recommendation_context(b) for b in books]
        existing_keys = {b.source_id for b in books if b.source_id}
        book_instances = {b.id: b.instance_id for b in books}

    # Тяжёлая часть — вне открытой сессии БД.
    plan = ai_structured(
        PLAN_PROMPT.format(
            library=json.dumps(library_context, ensure_ascii=False),
        ),
        RecoPlan,
    )
    analysis = (plan.analysis or "").strip()

    candidates = collect_openlibrary_candidates(
        plan.search_queries, existing_keys
    )

    external_picks: List[ExternalPick] = []
    if candidates:
        choice = ai_structured(
            CHOOSE_PROMPT.format(
                analysis=analysis,
                library=json.dumps(library_context, ensure_ascii=False),
                candidates=json.dumps(candidates, ensure_ascii=False),
            ),
            RecoChoice,
        )
        external_picks = choice.external_picks

    candidates_by_id = {c["source_id"]: c for c in candidates}

    with db_session() as db:
        # Read current books and save picks in one write transaction: no delete can
        # slip between validation and the FK inserts. Network work is already done.
        db.execute(sql_text("BEGIN IMMEDIATE"))
        run = db.get(RecommendationRun, run_id)
        if not run or run.status != "pending":
            return
        current_books = db.query(Book).all()
        books_by_id = {b.id: b for b in current_books}
        current_keys = {b.source_id for b in current_books}
        selected_ids, selected_keys = set(), set()

        added_planned = 0
        for pick in plan.planned_picks:
            if added_planned >= 3:
                break
            book = books_by_id.get(pick.book_id)
            if (not book or book.status != "want" or book.id in selected_ids
                    or book.instance_id != book_instances.get(book.id)):
                continue
            selected_ids.add(book.id)
            db.add(Recommendation(
                run_id=run.id,
                kind="planned",
                status="active",
                book_id=book.id,
                title=book.title,
                authors=book.authors,
                year=book.year,
                language=book.language,
                cover=book.cover_path or "",
                reason=(pick.reason or "").strip(),
            ))
            added_planned += 1

        added_external = 0
        for pick in external_picks:
            if added_external >= 5:
                break
            candidate = candidates_by_id.get((pick.source_id or "").strip())
            if (not candidate or candidate["source_id"] in current_keys
                    or candidate["source_id"] in selected_keys):
                continue
            selected_keys.add(candidate["source_id"])
            db.add(Recommendation(
                run_id=run.id,
                kind="external",
                status="active",
                source_id=candidate["source_id"],
                title=candidate["title"],
                title_ru=clean_title_ru(pick.title_ru),
                authors=candidate["authors"],
                year=candidate["year"],
                language=candidate["language"],
                cover=candidate["cover"],
                reason=(pick.reason or "").strip(),
                search_query=candidate["search_query"],
            ))
            added_external += 1

        run.analysis = analysis
        run.status = "ready"
        run.error = None
        db.commit()
        log.info(
            "Recommendation run %s ready: %s planned, %s external",
            run_id, added_planned, added_external,
        )


# ----------------------------
# reader-sync: книги с читалок
# ----------------------------
READER_ENABLED = True
READER_KEY = re.compile(r"^[A-Za-z0-9_-]{1,100}$")

# Что храним в снимке книги из reader-sync (его /api/library отдаёт больше).
READER_BOOK_FIELDS = (
    "key", "title", "filename", "kindle_path", "size", "present", "last_seen",
    "pid", "percentage", "revision", "source", "updated_at", "bridge_status",
    "context_before", "context_after", "asset_version",
    "map_status", "map_error", "epub_status", "epub_error",
    "has_kfx", "has_epub", "has_map", "has_compact",
)
# Что уходит в карточку полки — остальное грузит страница книги.
READER_CARD_FIELDS = (
    "key", "title", "filename", "percentage", "updated_at", "source", "present",
    "map_status", "epub_status", "has_kfx", "has_epub",
)

READER_STATE: Dict[str, Any] = {
    "library": None,      # последний ответ /api/library
    "fetched_at": None,   # когда он получен
    "synced_at": None,    # последний полный синк (снимки + импорт)
    "error": None,
    "importing": {"done": 0, "total": 0},
    "snapshots_at": None,  # когда в последний раз менялись данные карточек
}
_reader_sync_lock = threading.Lock()


ReaderSyncError = Problem


def reader_http_error(exc: ReaderSyncError) -> HTTPException:
    # 4xx reader-sync — осмысленный ответ (нет файла, не та книга), остальное — «сервис лежит».
    return HTTPException(status_code=exc.status if 400 <= exc.status < 500 else 502, detail=str(exc))


def check_reader_key(key: str) -> None:
    if not READER_KEY.fullmatch(key or ""):
        raise HTTPException(status_code=400, detail="Некорректный ключ книги")



def reader_library() -> Dict[str, Any]:
    """Свежая библиотека reader-sync; заодно обновляет кеш для статуса и страницы книги."""
    try:
        library = reader_service.library()
    except ValueError as exc:
        READER_STATE["error"] = "reader-sync вернул не JSON"
        raise ReaderSyncError(READER_STATE["error"]) from exc
    except ReaderSyncError as exc:
        READER_STATE["error"] = str(exc)
        raise
    READER_STATE.update(library=library, error=None, fetched_at=utcnow())
    try:
        save_card_snapshots(library)
    except Exception:
        log.warning("Could not refresh reader snapshots", exc_info=True)
    return library


def save_card_snapshots(library: Dict[str, Any]) -> bool:
    """Свежий ответ reader-sync → снимки книг, у которых поменялось то, что видно на карточке
    (прогресс, «на Kindle», EPUB, X3). Иначе карточка отставала бы от страницы книги до
    полного синка в обработчике. Остальное обновит полный синк."""
    fresh = library_snapshots(library)
    changed = False
    with db_session() as db:
        for link in db.query(ReaderLink).filter(ReaderLink.key.in_(list(fresh))):
            if reader_card(load_snapshot(link)) != reader_card(fresh[link.key]):
                link.snapshot = json.dumps(fresh[link.key], ensure_ascii=False)
                link.synced_at = utcnow()
                changed = True
        if changed:
            db.commit()
    if changed:
        READER_STATE["snapshots_at"] = utcnow()
    return changed


def library_snapshots(library: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Снимки книг reader-sync по ключу, вместе с привязанными X3-документами."""
    docs: Dict[str, List[Dict[str, Any]]] = {}
    for d in library.get("documents") or []:
        if d.get("book_key"):
            docs.setdefault(d["book_key"], []).append({
                k: d.get(k) for k in ("document", "progress", "percentage", "device", "device_id", "received_at", "error")
            })
    applied: Dict[str, List[Dict[str, Any]]] = {}
    for a in library.get("applied") or []:
        applied.setdefault(a.get("book_key"), []).append({
            k: a.get(k) for k in ("device_id", "observed_pid", "applied_revision", "status", "updated_at")
        })
    out = {}
    for b in library.get("books") or []:
        snap = {k: b.get(k) for k in READER_BOOK_FIELDS}
        snap["documents"] = docs.get(b["key"], [])
        snap["applied"] = applied.get(b["key"], [])
        out[b["key"]] = snap
    return out


def load_snapshot(link: ReaderLink) -> Dict[str, Any]:
    try:
        snap = json.loads(link.snapshot or "{}")
    except ValueError:
        snap = {}
    snap.setdefault("key", link.key)
    return snap


def reader_card(snap: Dict[str, Any]) -> Dict[str, Any]:
    card = {k: snap.get(k) for k in READER_CARD_FIELDS}
    card["x3"] = len(snap.get("documents") or [])
    return card


def update_link_snapshot(link: ReaderLink, snap: Dict[str, Any]) -> None:
    """Свежий снимок → строка reader_links, только если он на самом деле другой.

    synced_at двигается, лишь когда поменялось видимое на карточке: /api/reader/status отдаёт
    его максимум, и по нему каждая открытая страница перечитывает всю полку.
    """
    old = load_snapshot(link)
    encoded = json.dumps(snap, ensure_ascii=False)
    if old == json.loads(encoded):
        return
    link.snapshot = encoded
    if reader_card(old) != reader_card(snap):
        link.synced_at = utcnow()


def deliveries_by_book(db, book_ids: List[int]) -> Dict[int, List[Dict[str, Any]]]:
    """Full per-book delivery evidence, independent of the 30-item activity feed."""
    wanted = set(book_ids)
    out: Dict[int, List[Dict[str, Any]]] = {}
    if not wanted:
        return out
    for row in db.execute(sql_text("SELECT * FROM deliveries WHERE device='x3'")).mappings():
        if row['book_id'] in wanted:
            out.setdefault(row['book_id'], []).append(dict(
                device='x3', filename=row['filename'], sha256=row['sha256'],
                sent_at=datetime.fromtimestamp(row['sent_at'], timezone.utc).isoformat(),
            ))
    legacy = (db.query(X3Upload, ReaderLink.book_id).join(ReaderLink, ReaderLink.key == X3Upload.key)
              .filter(ReaderLink.book_id.in_(book_ids), X3Upload.status == 'sent'))
    for upload, book_id in legacy:
        items = out.setdefault(book_id, [])
        if not any(item['filename'] == upload.filename for item in items):
            items.append(dict(device='x3', filename=upload.filename, sha256=None, sent_at=iso_utc(upload.sent_at)))
    return out


# Те же поля и порядок, что у Library.files / Library.readers, но одним запросом на весь список книг.
BOOK_EPUBS_SQL = sql_text("""SELECT a.book_id,a.sha256,a.filename,a.canonical,a.origin,a.created_at,f.size,
p.xpath,p.percentage,p.revision,p.epoch,p.source,p.updated_at
FROM book_assets a JOIN epub_assets f ON f.sha256=a.sha256
LEFT JOIN reading_positions p ON p.book_id=a.book_id AND p.sha256=a.sha256
WHERE a.book_id IN :ids ORDER BY a.book_id,a.canonical DESC,a.created_at,a.rowid""").bindparams(bindparam("ids", expanding=True))
BOOK_DEVICES_SQL = sql_text(
    "SELECT * FROM reader_observations WHERE book_id IN :ids ORDER BY received_at DESC"
).bindparams(bindparam("ids", expanding=True))
X3_LISTED_DOCUMENTS_SQL = sql_text(
    "SELECT d.document,d.library_book_id,d.epub_sha256,l.book_id AS kindle_book_id FROM documents d "
    "LEFT JOIN reader_links l ON l.key=d.book_key WHERE d.document IN :documents"
).bindparams(bindparam("documents", expanding=True))
X3_SENT_FILES_SQL = sql_text(
    "SELECT filename,book_id,sha256 FROM deliveries WHERE device='x3' AND book_id IS NOT NULL AND filename IN :names "
    "UNION SELECT u.filename,l.book_id,NULL FROM x3_uploads u JOIN reader_links l ON l.key=u.key "
    "WHERE u.status='sent' AND l.book_id IS NOT NULL AND u.filename IN :names"
).bindparams(bindparam("names", expanding=True))
# У прочитанных и брошенных книг отсутствие на X3 ожидаемо — им «нет на X3» не показываем.
X3_ABSENCE_EXPECTED = {"read", "dropped"}


def is_x3_device(row: Any) -> bool:
    """Наблюдение KOSync пришло с X3: X3_SYNC_DEVICE совпало с device или device_id."""
    return any(X3_SYNC_DEVICE.search(row[field] or "") for field in ("device", "device_id"))


def x3_listing(db) -> tuple:
    """Сохранённый список файлов X3: (unix-время, файлы). Списка ещё не было — (None, [])."""
    row = db.get(X3Listing, 1)
    if not row or row.directory != X3_BOOKS_DIR:  # список другой папки о нынешней ничего не говорит
        return None, []
    try:
        files = json.loads(row.files or "[]")
    except ValueError:
        files = []
    return row.checked_at, files


def unix_iso(value: Optional[float]) -> Optional[str]:
    return None if value is None else iso_utc(datetime.fromtimestamp(value, timezone.utc))


def iso_unix(value: Optional[str]) -> Optional[float]:
    try:
        return datetime.fromisoformat(value).timestamp() if value else None
    except ValueError:
        return None


def x3_file_owners(db, names) -> Dict[str, Dict[int, Optional[str]]]:
    """Каким карточкам принадлежат файлы X3 с такими именами: {имя: {id книги: sha256 её EPUB}}.

    Файл относится к книге, если его отправляли для неё или MD5 его имени (так X3 называет
    документ в KOSync) связан с ней либо с её файлом Kindle. sha256 — какой именно EPUB библиотеки
    лежит под этим именем: по отправке, иначе по привязке документа; None — неизвестно. Первыми
    идут книги, для которых файл отправляли. Удалённых карточек здесь нет.
    """
    documents = {x3_document_id(name): name for name in names}
    owners: Dict[str, Dict[int, Optional[str]]] = {}
    if not documents:
        return owners
    for row in db.execute(X3_SENT_FILES_SQL, {"names": list(documents.values())}).mappings():
        books = owners.setdefault(row["filename"], {})
        books[row["book_id"]] = row["sha256"] or books.get(row["book_id"])
    for row in db.execute(X3_LISTED_DOCUMENTS_SQL, {"documents": list(documents)}).mappings():
        books = owners.setdefault(documents[row["document"]], {})
        if row["library_book_id"] is not None:
            books[row["library_book_id"]] = books.get(row["library_book_id"]) or row["epub_sha256"]
        if row["kindle_book_id"] is not None:
            books.setdefault(row["kindle_book_id"], None)
    return {name: books for name, books in owners.items() if books}


def x3_same_file(db, files: List[Dict[str, Any]], owners: Dict[str, Dict[int, Optional[str]]]) -> Dict[str, tuple]:
    """Файлы списка X3, про которые известно, какой это EPUB библиотеки: {имя: (id книги, sha256)}.

    Привязку документа по MD5 имени библиотека создаёт сама для каждого загруженного файла, поэтому
    одно только имя ничего не доказывает: чужой файл с таким же именем отдал бы карточке своё место
    чтения, а потом был бы стёрт вместе с ней. Свой — тот, чей размер равен размеру этого EPUB.
    """
    hashes = {sha256 for books in owners.values() for sha256 in books.values() if sha256}
    sizes = {row[0]: row[1] for row in db.execute(
        sql_text("SELECT sha256,size FROM epub_assets WHERE sha256 IN :hashes").bindparams(bindparam("hashes", expanding=True)),
        {"hashes": list(hashes)})} if hashes else {}
    same = {}
    for f in files:
        known = next(((book_id, sha256) for book_id, sha256 in owners.get(f["name"], {}).items()
                      if sha256 and sizes.get(sha256) == f.get("size")), None)
        if known:
            same[f["name"]] = known
    return same


def x3_by_book(db, books: List[Book], devices: Dict[int, List[Dict[str, Any]]],
               deliveries: Dict[int, List[Dict[str, Any]]]) -> Dict[int, Optional[Dict[str, Any]]]:
    """Есть ли книга на X3 — по отправкам, синкам X3 и последнему сохранённому списку его файлов.

    X3 редко в сети. После удачной отправки или синка позиции книга считается лежащей на нём
    (confirmed=False), пока список файлов не скажет своё: файл книги в списке — «есть», список
    новее последней отправки и синка и файла в нём нет — «нет». Чей файл — решает x3_file_owners.
    Книгу, убранную с X3 кнопкой на полке, прежние свидетельства не возвращают: считается только
    то, что новее Book.x3_removed_at. Сам X3 отсюда не опрашивается.
    """
    checked_at, files = x3_listing(db)
    listed: Dict[int, set] = {}
    for name, owners in x3_file_owners(db, {f.get("name") for f in files} - {None}).items():
        for book_id in owners:
            listed.setdefault(book_id, set()).add(name)
    out: Dict[int, Optional[Dict[str, Any]]] = {}
    for book in books:
        removed_at = book.x3_removed_at
        newer = lambda at: removed_at is None or (at or 0) > removed_at
        sent = sorted((d for d in deliveries.get(book.id, []) if newer(iso_unix(d["sent_at"]))),
                      key=lambda d: iso_unix(d["sent_at"]) or 0)
        observed = [d["received_at"] for d in devices.get(book.id, []) if d["x3"] and newer(d["received_at"])]
        found = sorted(listed.get(book.id, set())) if newer(checked_at) else []
        sent_at = max(filter(None, (iso_unix(d["sent_at"]) for d in sent)), default=None)
        synced_at = max(observed, default=None)
        if found:
            state, confirmed = "present", True
        elif not sent and not observed:
            out[book.id] = None
            continue
        # Время везде в целых секундах: отправка сразу после проверки списка попадает в ту же
        # секунду, поэтому при равенстве верим отправке или синку.
        elif checked_at is None or checked_at <= max(sent_at or 0, synced_at or 0):
            state, confirmed = "present", False
        elif book.status in X3_ABSENCE_EXPECTED:
            out[book.id] = None
            continue
        else:
            state, confirmed = "missing", True
        latest = sent[-1]["filename"] if sent else None
        out[book.id] = {
            "state": state, "confirmed": confirmed, "checked_at": unix_iso(checked_at),
            "sent_at": unix_iso(sent_at), "synced_at": unix_iso(synced_at),
            "filename": latest if latest in found or not found else found[0],
        }
    return out


def same_book_keys(source_id: Optional[str], title: Optional[str], authors: Optional[str]) -> List[tuple]:
    """По чему книгу узнают без файла: работа каталога (не синтетический epub:/kindle:) и название
    с авторами без учёта регистра и пунктуации."""
    keys = [("work", source_id)] if (source_id or "").startswith("/works/") else []
    name, people = tuple(name_tokens(title)), tuple(name_tokens(authors))
    if name and people:  # без автора название — не примета: «book», «Unknown», имя файла
        keys.append(("name", name, people))
    return keys


def deleted_before_by_book(db, books: List[Book], epubs: Dict[int, List[Dict[str, Any]]]) -> Dict[int, Dict[str, Any]]:
    """Пометка «вы её удаляли» — карточкам, добавленным после удаления такой же книги.

    Та же книга — тот же EPUB (SHA-256) или совпадение по same_book_keys. Карточки, которая уже
    стояла на полке в момент удаления (оставшийся дубль), запись не касается. Подходит несколько
    записей — берём последнюю.
    """
    records = {r.id: r for r in db.query(DeletedBook).filter(DeletedBook.dismissed == 0)}
    if not records:
        return {}
    by_key: Dict[Any, List[DeletedBook]] = {}
    for sha256, record_id in db.query(DeletedEpub.sha256, DeletedEpub.deleted_book_id):
        if record_id in records:
            by_key.setdefault(sha256, []).append(records[record_id])
    for record in records.values():
        for key in same_book_keys(record.source_id, record.title, record.authors):
            by_key.setdefault(key, []).append(record)
    out = {}
    for book in books:
        created = book.created_at.replace(tzinfo=timezone.utc).timestamp() if book.created_at else 0
        keys = [e["sha256"] for e in epubs.get(book.id, [])] + same_book_keys(book.source_id, book.title, book.authors)
        found = [(r, key) for key in keys for r in by_key.get(key, []) if r.deleted_at <= created]
        if found:
            record, key = max(found, key=lambda pair: (pair[0].deleted_at, pair[0].id))
            # match: по чему узнали — тот же файл, та же работа каталога или только название с автором.
            out[book.id] = {"id": record.id, "deleted_at": unix_iso(record.deleted_at),
                            "title": record.title_ru or record.title,
                            "match": "file" if isinstance(key, str) else key[0]}
    return out


def deleted_before(book_id: int) -> Optional[Dict[str, Any]]:
    """Та же пометка для ответа на добавление книги."""
    with db_session() as db:
        book = db.get(Book, book_id)
        return books_out(db, [book])[0].deleted_before if book else None


# Маршрут POST /api/epubs живёт в web_epub.py и висит на этом же приложении: пометку он берёт отсюда.
app.state.deleted_before = deleted_before


def books_out(db, books: List[Book]) -> List[BookOut]:
    """Карточки книг с файлами, устройствами, отправками, состоянием X3 и пометкой об удалении.

    Всё грузится постоянным числом запросов на весь список в сессии полки — раньше каждая книга
    открывала по три соединения SQLite.
    """
    ids = [b.id for b in books]
    if not ids:
        return []
    links = db.query(ReaderLink).filter(ReaderLink.book_id.in_(ids)).order_by(ReaderLink.key).all()
    cards: Dict[int, List[Dict[str, Any]]] = {}
    for link in links:
        cards.setdefault(link.book_id, []).append(reader_card(load_snapshot(link)))
    epubs: Dict[int, List[Dict[str, Any]]] = {}
    for row in db.execute(BOOK_EPUBS_SQL, {"ids": ids}).mappings():
        epubs.setdefault(row["book_id"], []).append(dict(row))
    devices: Dict[int, List[Dict[str, Any]]] = {}
    for row in db.execute(BOOK_DEVICES_SQL, {"ids": ids}).mappings():
        devices.setdefault(row["book_id"], []).append({**row, "x3": is_x3_device(row)})
    deliveries = deliveries_by_book(db, ids)
    x3 = x3_by_book(db, books, devices, deliveries)
    deleted = deleted_before_by_book(db, books, epubs)
    out = []
    for book in books:
        item = BookOut.model_validate(book)
        item.reader = cards.get(book.id, [])
        item.epubs = epubs.get(book.id, [])
        item.devices = devices.get(book.id, [])
        item.deliveries = deliveries.get(book.id, [])
        item.x3 = x3.get(book.id)
        item.deleted_before = deleted.get(book.id)
        out.append(item)
    return out


def save_cover_bytes(book_id: int, data: bytes) -> Optional[str]:
    """Обложку из KFX пишем так же, как скачанную: временный файл и атомарная подмена."""
    if not data or len(data) < MIN_COVER_BYTES or len(data) > MAX_COVER_BYTES:
        return None
    final_path = os.path.join(COVERS_DIR, f"{book_id}.jpg")
    tmp_path = f"{final_path}.tmp"
    try:
        with open(tmp_path, "wb") as f:
            f.write(data)
        os.replace(tmp_path, final_path)
        return f"/static/covers/{book_id}.jpg"
    except OSError:
        log.warning("Could not save cover for book %s", book_id, exc_info=True)
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return None


def reader_cover(book_id: int, keys: List[str]) -> Optional[str]:
    """Обложка того издания, что лежит на читалке."""
    for key in keys:
        try:
            data = reader_service.cover(key)
        except ReaderSyncError as exc:
            log.info("No KFX cover for %s: %s", key, exc)
            continue
        path = save_cover_bytes(book_id, data)
        if path:
            return path
    return None


# --- Каталог: русское издание → работа в OpenLibrary ---
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
TRANSLATE_API = "https://translate.googleapis.com/translate_a/single"
CYRILLIC = re.compile(r"[а-яё]", re.IGNORECASE)


def name_tokens(value: Optional[str]) -> List[str]:
    value = (value or "").casefold().replace("ё", "е")
    return re.sub(r"[^\w]+", " ", value).split()


def author_surname(authors: str) -> str:
    tokens = name_tokens((authors or "").split(",")[0])
    return tokens[-1] if tokens else ""


def title_variants(title: str) -> List[str]:
    """Полное название, потом его осмысленные куски: «Серия: Книга», «Книга. Подзаголовок», «X, или Y»."""
    variants = [title]
    if ":" in title:
        variants.append(title.split(":", 1)[1])
    if ". " in title:
        variants.append(title.split(". ", 1)[0])
    if ", или " in title.lower():
        variants.append(re.split(r",\s+или\s+", title, maxsplit=1, flags=re.IGNORECASE)[1])
    out = []
    for v in variants:
        v = v.strip(" .:«»\"")
        if len(v) >= 3 and v.lower() not in {o.lower() for o in out}:
            out.append(v)
    return out


def _similarity(a: str, b: str) -> float:
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return 0.0
    sa, sb = " ".join(sorted(ta)), " ".join(sorted(tb))
    return max(
        difflib.SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio(),
        difflib.SequenceMatcher(None, sa, sb).ratio(),
    )


def main_title(title: str) -> str:
    """Название без подзаголовка: всё до «:», «(» или тире."""
    return re.split(r"\s*[:(]\s*|\s+[-–—]\s+", title, maxsplit=1)[0]


def title_similarity(a: str, b: str) -> float:
    """Похожесть названий без учёта порядка слов («Glass Jack» == «Jack Glass»).

    Подзаголовок не мешает: «Never Split the Difference» == «Never Split the Difference: Negotiating…».
    """
    return max(_similarity(x, y) for x in {a, main_title(a)} for y in {b, main_title(b)})


def title_overlap(wanted: str, found: str) -> float:
    """Строже title_similarity: доля общих слов. «Death's End (The Three-Body Problem Series)»
    для «The Three-Body Problem» — это 0, а не «почти то же самое»."""
    want = set(name_tokens(wanted))
    best = 0.0
    for candidate in {found, main_title(found)}:
        got = set(name_tokens(candidate))
        if want and got:
            best = max(best, len(want & got) / max(len(want), len(got)))
    return best


_TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                     ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "i", "k", "l", "m", "n", "o", "p", "r", "s",
                      "t", "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


def surname_key(surname: str) -> str:
    """Грубый фонетический ключ фамилии, чтобы «Карре» совпало с «Carré», а «Скальци» — со «Scalzi»."""
    s = "".join(_TRANSLIT.get(ch, ch) for ch in (surname or "").casefold())
    s = re.sub(r"[^a-z]", "", unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode())
    for old, new in (("ph", "f"), ("ck", "k"), ("ts", "s"), ("tz", "s"), ("z", "s"), ("c", "k"), ("q", "k"),
                     ("x", "ks"), ("w", "v"), ("y", "i"), ("j", "i"), ("h", "")):
        s = s.replace(old, new)
    # Гласные в транслите гуляют сильнее всего (Хайнлайн / Heinlein) — сравниваем по согласным.
    return re.sub(r"(.)\1+", r"\1", re.sub(r"[aeiou]", "", s))


def find_same_book(db, titles: List[Optional[str]], authors: List[Optional[str]],
                   exclude_id: Optional[int] = None) -> Optional["Book"]:
    """Та же книга под другой записью каталога: OpenLibrary нередко держит одну работу в двух записях,
    а Wikidata знает одну. Название — теми же словами без пунктуации и регистра (у книги на полке
    годится и русское), фамилия автора — в том числе между кириллицей и латиницей."""
    wanted = {tuple(name_tokens(t)) for t in titles if t} - {()}
    surnames = {surname_key(author_surname(a)) for a in authors if a} - {""}
    if not wanted or not surnames:
        return None
    for book in db.query(Book).order_by(Book.id):
        if book.id == exclude_id:
            continue
        names = {tuple(name_tokens(n)) for n in (book.title, book.title_ru) if n}
        if wanted & names and surname_key(author_surname(book.authors)) in surnames:
            return book
    return None


def looks_like_filename(title: str) -> bool:
    """«yan_lankaster_fleming-moonraker-1512928728» — метаданные KFX без настоящего названия."""
    return bool(title) and " " not in title.strip() and "_" in title


def resolve_ol_work(work_key: str, seen: Optional[List[str]] = None) -> Optional[str]:
    """Работа в OpenLibrary существует? Заодно идём по редиректам после слияний.

    В seen складываются все ключи цепочки: на полке книга может лежать под старым ключом.
    """
    for _ in range(3):
        if seen is not None:
            seen.append(work_key)
        try:
            work = ol_get(f"{work_key}.json")
        except requests.RequestException:
            return None
        if (work.get("type") or {}).get("key") == "/type/redirect" and work.get("location"):
            work_key = work["location"]
            continue
        return work_key if work.get("title") else None
    return None


def wikidata_entities(ids: List[str], props: str) -> Dict[str, Any]:
    if not ids:
        return {}
    data = catalog_get(WIKIDATA_API, {
        "action": "wbgetentities", "ids": "|".join(ids[:50]), "props": props,
        "languages": "ru|en", "format": "json",
    })
    return data.get("entities") or {}


def _build_catalog_session() -> requests.Session:
    """Wikidata и переводчик. 429 обрабатывает catalog_get сам: ждёт Retry-After, но с потолком."""
    session = requests.Session()
    retry = Retry(
        total=2, backoff_factor=1, status_forcelist=(500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]), respect_retry_after_header=False, raise_on_status=False,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": f"{USER_AGENT} personal-library-catalog-lookup", "Accept": "application/json"})
    return session


CATALOG_HTTP = _build_catalog_session()
CATALOG_MIN_INTERVAL_SEC = 1.0
_catalog_lock = threading.Lock()
_catalog_last_call = [0.0]


def catalog_get(url: str, params: Dict[str, Any]) -> Any:
    """Не чаще запроса в секунду: Wikimedia режет всплески 429-м с Retry-After ~10 с."""
    for attempt in range(3):
        with _catalog_lock:
            wait = _catalog_last_call[0] + CATALOG_MIN_INTERVAL_SEC - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _catalog_last_call[0] = time.monotonic()
        r = CATALOG_HTTP.get(url, params=params, timeout=OL_TIMEOUT_SEC)
        if r.status_code == 429 and attempt < 2:
            try:
                delay = int(r.headers.get("Retry-After", "5"))
            except ValueError:
                delay = 5
            time.sleep(min(max(delay, 1), 30))
            continue
        r.raise_for_status()
        return r.json()


def claim_values(claims: Dict[str, Any], prop: str) -> List[Any]:
    return [((c.get("mainsnak") or {}).get("datavalue") or {}).get("value") for c in claims.get(prop) or []]


def wikidata_match(title: str, surname: str, seen: Optional[List[str]] = None) -> Optional[str]:
    """Wikidata знает русские названия и хранит Open Library ID (P648) — это самый точный путь.

    Принимаем сущность, только если у неё есть автор (P50) с той же фамилией.
    """
    lang = "ru" if CYRILLIC.search(title) else "en"
    found = catalog_get(WIKIDATA_API, {
        "action": "wbsearchentities", "search": title, "language": lang, "uselang": lang,
        "type": "item", "limit": 7, "format": "json",
    }).get("search") or []
    ids = [x["id"] for x in found if x.get("id")]
    entities = wikidata_entities(ids, "claims")

    candidates = []
    author_ids = set()
    for qid in ids:  # порядок поиска Wikidata — это релевантность
        claims = (entities.get(qid) or {}).get("claims") or {}
        works = [v for v in claim_values(claims, "P648") if isinstance(v, str) and v.endswith("W")]
        authors = [v.get("id") for v in claim_values(claims, "P50") if isinstance(v, dict) and v.get("id")]
        if works and authors:
            candidates.append((works, authors))
            author_ids.update(authors)
    if not candidates:
        return None

    people = wikidata_entities(sorted(author_ids), "labels|aliases")

    def person_tokens(qid: str) -> set:
        p = people.get(qid) or {}
        names = [v.get("value") for v in (p.get("labels") or {}).values()]
        for aliases in (p.get("aliases") or {}).values():
            names.extend(a.get("value") for a in aliases)
        return {t for n in names for t in name_tokens(n)}

    for works, authors in candidates:
        if any(surname in person_tokens(a) for a in authors):
            for w in works:
                key = resolve_ol_work(f"/works/{w}", seen)
                if key:
                    return key
    return None


def translate_en(text: str) -> str:
    data = catalog_get(TRANSLATE_API, {"client": "gtx", "sl": "auto", "tl": "en", "dt": "t", "q": text})
    return "".join(part[0] for part in (data[0] or []) if part and part[0]).strip()


def translated_ol_match(title: str, authors: str, seen: Optional[List[str]] = None) -> Optional[str]:
    """Перевод на английский + поиск OpenLibrary."""
    return english_ol_match(translate_en(title), translate_en((authors or "").split(",")[0]), seen)


def english_ol_match(title_en: str, author_en: str, seen: Optional[List[str]] = None) -> Optional[str]:
    """Поиск OpenLibrary по английским названию и автору. Автор обязан совпасть, название — быть похожим."""
    surname = author_surname(author_en)
    if not title_en or not surname:
        return None
    fields = "key,title,author_name"
    docs = ol_get("/search.json", params={"title": title_en, "author": author_en, "limit": 10, "fields": fields}).get("docs") or []
    if not docs:
        # Перевод часто неточен («House of the Suns») — перебираем все работы автора.
        docs = ol_get("/search.json", params={"author": author_en, "limit": 100, "fields": fields}).get("docs") or []
    best, best_score = None, 0.0
    for d in docs:
        if not any(surname in name_tokens(n) for n in d.get("author_name") or []):
            continue
        score = title_similarity(title_en, d.get("title") or "")
        if score > best_score:
            best, best_score = d, score
    if best and best_score >= 0.75:
        return resolve_ol_work(best["key"], seen)
    return None


class OriginalTitle(BaseModel):
    title: str = ""
    author: str = ""
    confident: bool = False


ORIGINAL_TITLE_PROMPT = (
    "Это книга из личной библиотеки — русское издание (или файл с Kindle).\n"
    "Название: {title}\n"
    "Автор: {authors}\n\n"
    "Назови оригинальное название произведения и имя автора так, как они записаны в англоязычных "
    "каталогах (OpenLibrary, Wikidata). Для переводной книги — оригинальное название "
    "(«Пыль» Хью Хауи → «Dust», «Укрытие. Книга 1. Иллюзия» → «Wool»). Для сборника или омнибуса — "
    "название сборника в оригинале. Для книги, написанной по-русски, — русское название как есть.\n"
    "Не выдумывай: если не уверен, поставь confident=false.\n"
    "Ответ — строго JSON без markdown и пояснений: "
    '{{"title": "...", "author": "...", "confident": true}}'
)


def ai_original_title(title: str, authors: str) -> Optional[OriginalTitle]:
    """Подсказка модели: оригинальное название и автор — для поиска там, где перевод не помог."""
    if not AI_ENABLED:
        return None
    try:
        hint = ai_structured(ORIGINAL_TITLE_PROMPT.format(title=title, authors=authors), OriginalTitle, AI_HINT_MODEL)
    except AIError:
        log.warning("AI title hint failed for %r", title, exc_info=True)
        return None
    if not hint.confident or not hint.title.strip() or not hint.author.strip():
        log.info("AI title hint for %r is not confident: %s", title, hint.model_dump())
        return None
    log.info("AI title hint: %r / %r -> %r / %r", title, authors, hint.title, hint.author)
    return hint


def ai_hint_match(title: str, authors: str, seen: List[str]) -> Optional[str]:
    hint = ai_original_title(title, authors)
    if not hint:
        return None
    # Wikidata по оригинальному названию: фамилию сверяем и по-английски, и как на Kindle.
    for surname in dict.fromkeys(filter(None, (author_surname(hint.author), author_surname(authors)))):
        key = wikidata_match(hint.title, surname, seen)
        if key:
            return key
    return english_ol_match(hint.title, hint.author, seen)


def catalog_match(title: str, authors: str, use_ai: bool = True) -> Optional[Dict[str, str]]:
    """Русское издание → работа в OpenLibrary: Wikidata, потом перевод, потом подсказка модели.

    Без совпадения автора ничего не принимаем: лучше пустая карточка, чем чужая книга.
    Подсказка модели — последней: она тратит лимит подписки и отвечает дольше всех.
    """
    surname = author_surname(authors)
    if not title or not surname:
        return None
    seen: List[str] = []
    try:
        for variant in title_variants(title):
            key = wikidata_match(variant, surname, seen)
            if key:
                return {"work_key": key, "via": "wikidata", "aliases": seen}
    except (requests.RequestException, ValueError, KeyError, TypeError):
        log.warning("Wikidata lookup failed for %r", title, exc_info=True)
    if TRANSLATE_TITLES:
        try:
            key = translated_ol_match(title, authors, seen)
            if key:
                return {"work_key": key, "via": "translate", "aliases": seen}
        except (requests.RequestException, ValueError, KeyError, TypeError, IndexError):
            log.warning("Translated OpenLibrary lookup failed for %r", title, exc_info=True)
    if use_ai and AI_ENABLED:
        try:
            key = ai_hint_match(title, authors, seen)
            if key:
                return {"work_key": key, "via": "ai", "aliases": seen}
        except (requests.RequestException, ValueError, KeyError, TypeError, IndexError):
            log.warning("AI-hinted catalog lookup failed for %r", title, exc_info=True)
    return None


def tidy_title(title: str, catalog_title: Optional[str] = None) -> str:
    """Названия из метаданных KFX бывают КАПСОМ — приводим к обычному виду."""
    title = " ".join((title or "").split())
    if title.isupper():
        if catalog_title and not CYRILLIC.search(title):
            return catalog_title
        return title[:1] + title[1:].lower()
    return title


def import_reader_book(key: str, snap: Dict[str, Any]) -> None:
    """Новая книга с читалки → полка, статус «Общее». Вся сеть — до записи в БД."""
    meta: Dict[str, Any] = {}
    try:
        meta = reader_service.metadata(key)
    except (ReaderSyncError, ValueError) as exc:
        log.warning("reader-sync meta for %s failed: %s", key, exc)

    raw_title = meta.get("title") or snap.get("title") or key
    authors = ", ".join(meta.get("authors") or []) or "Автор не указан"
    match = None  # Catalogue enrichment is requested independently; it cannot block file ingestion.
    via = match["via"] if match else "none"

    bundle: Dict[str, Any] = {}
    if match:
        try:
            bundle = fetch_work_bundle(match["work_key"])
        except requests.RequestException:
            log.warning("OpenLibrary work fetch failed for %s", match["work_key"], exc_info=True)
    title = tidy_title(raw_title, bundle.get("title"))
    if looks_like_filename(title) and bundle.get("title"):
        title, authors = bundle["title"], bundle.get("authors") or authors

    with db_session() as db:
        # Уже на полке: та же работа в каталоге или то же название и автор (второй файл того же издания).
        existing = None
        # Original EPUB may already have created the card before Kindle downloaded KFX.
        for inc in db.query(IncomingEpub).all():
            if epub_matches_kindle(inc.filename, kindle_stem(snap.get("filename") or "", key)):
                row = db.execute(sql_text("SELECT book_id FROM book_assets WHERE sha256=:sha LIMIT 1"), {"sha": inc.sha256}).first()
                if row:
                    existing = db.get(Book, row[0])
                    break
        if match and existing is None:
            keys = {match["work_key"], *match.get("aliases", [])}
            existing = db.query(Book).filter(Book.source_id.in_(keys)).first()
        if not existing:
            existing = db.query(Book).filter(
                func.py_lower(Book.title) == title.lower(),
                func.py_lower(Book.authors) == authors.lower(),
            ).first()
        if not existing:
            existing = find_same_book(db, [title, raw_title, bundle.get("title")], [authors, bundle.get("authors")])

        link = ReaderLink(key=key, snapshot=json.dumps(snap, ensure_ascii=False), synced_at=utcnow())
        if existing:
            link.book_id = existing.id
            link.match = "existing"
            # Русское издание на Kindle у книги с английским названием: название берём прямо из файла.
            kindle_ru = None if looks_like_filename(title) else clean_title_ru(title)
            if (kindle_ru and not CYRILLIC.search(existing.title or "")
                    and existing.title_ru_source in (None, "ai", "none")):
                existing.title_ru, existing.title_ru_source = kindle_ru, "kindle"
            db.add(link)
            db.commit()
            log.info("Reader book %s linked to existing book %s (%s)", key, existing.id, existing.title)
            return

        book = Book(
            source_id=match["work_key"] if match else f"kindle:{key}",
            title=title,
            authors=authors,
            status="unsorted",
            year=bundle.get("year"),
            language=meta.get("language") or bundle.get("language"),
            description=bundle.get("description") or "",
            summary="",
            summary_status="idle",
        )
        db.add(book)
        db.flush()
        link.book_id = book.id
        link.match = via
        db.add(link)
        db.commit()
        book_id, instance_id = book.id, book.instance_id

    from . import tasks
    tasks.enqueue("catalogue",dict(book_id=book_id,instance_id=instance_id),token=instance_id,lane="catalogue")

    # Обложка своего издания лучше каталожной; каталог — запасной вариант.
    cover = reader_cover(book_id, [key]) if meta.get("has_cover") else None
    if not cover and bundle.get("cover_urls"):
        cover = download_first_cover(book_id, bundle["cover_urls"])
    if cover:
        with db_session() as db:
            book = db.get(Book, book_id)
            if book:
                book.cover_path = cover
                db.commit()
    log.info("Imported reader book %s as #%s «%s» (catalog: %s)", key, book_id, title, via)


def reader_sync_once() -> Dict[str, Any]:
    """Обновить снимки книг с читалок и завести на полке новые."""
    if not _reader_sync_lock.acquire(blocking=False):
        return {"status": "busy"}
    try:
        try:
            library = reader_library()
        except ReaderSyncError as exc:
            log.warning("reader-sync sync failed: %s", exc)
            return {"status": "error", "error": str(exc)}

        READER_STATE["synced_at"] = utcnow()
        snapshots = library_snapshots(library)
        new_keys = []
        with db_session() as db:
            links = {l.key: l for l in db.query(ReaderLink).all()}
            for key, snap in snapshots.items():
                link = links.get(key)
                if link is None:
                    new_keys.append(key)
                    continue
                # Проход идёт каждые 30 с: строку трогаем, только когда снимок изменился.
                update_link_snapshot(link, snap)
            db.commit()

        READER_STATE["importing"] = {"done": 0, "total": len(new_keys)}
        for key in new_keys:
            if not snapshots[key].get('has_kfx'):
                continue
            try:
                import_reader_book(key, snapshots[key])
            except Exception:
                # Ссылка не создана — книга попробует импортироваться на следующем проходе.
                log.exception("Import of reader book %s failed", key)
            READER_STATE["importing"]["done"] += 1

        match_incoming_epubs()
        queue_missing_summaries()
        queue_title_translation()
        return {"status": "ok", "books": len(snapshots), "new": len(new_keys)}
    finally:
        _reader_sync_lock.release()


# --- EPUB из bookbot → книга в reader-sync ---
INCOMING_MAX_BYTES = 100 * 1024 * 1024
INCOMING_OPEN = ("waiting", "rejected")  # ещё не связаны с Kindle: видны в «Требуют внимания»
_incoming_lock = threading.Lock()
_incoming_save_lock = threading.Lock()


def incoming_path(digest: str) -> str:
    return os.path.join(INCOMING_DIR, f"{digest}.epub")


def kindle_stem(filename: str, key: str) -> str:
    """«piter_uotts-lozhnaya_slepota-1488914040_2UWLWYII….kfx» → имя, под которым файл ушёл на Kindle."""
    suffix = f"_{key}.kfx"
    return filename[: -len(suffix)] if filename.endswith(suffix) else os.path.splitext(filename)[0]


def _stem_key(value: str) -> str:
    return re.sub(r"[^0-9a-zа-яё]", "", (value or "").casefold())


def epub_matches_kindle(epub_filename: str, stem: str) -> bool:
    """Kindle длинные имена обрезает («kris_voss_nikakih_kompromissov_b») — тогда сравниваем по началу."""
    sent, kindle = _stem_key(os.path.splitext(epub_filename)[0]), _stem_key(stem)
    if not sent or not kindle:
        return False
    return sent == kindle or (len(kindle) >= 16 and sent.startswith(kindle))


def attach_incoming(inc_id: int, keys: List[str], replace: bool = False) -> Dict[str, Any]:
    """Отдать EPUB из «входящих» в reader-sync для этих книг Kindle. Проверку текста делает reader-sync."""
    with db_session() as db:
        inc = db.get(IncomingEpub, inc_id)
        if not inc:
            raise HTTPException(status_code=404, detail="EPUB не найден")
        digest, filename = inc.sha256, inc.filename
    library = READER_STATE["library"] or {}
    has_epub = {b["key"]: bool(b.get("has_epub")) for b in library.get("books") or []}
    done, skipped, errors, refused = [], [], [], 0
    for key in keys:
        if has_epub.get(key) and not replace:
            skipped.append(key)  # у книги уже есть EPUB — не подменяем тот, что может лежать на X3
            continue
        try:
            with open(incoming_path(digest), "rb") as f:
                reader_service.upload_epub(key, f)
            done.append(key)
        except (ReaderSyncError, OSError) as exc:
            errors.append(f"{key}: {exc}")
            refused += getattr(exc, "status", None) == 400
    with db_session() as db:
        inc = db.get(IncomingEpub, inc_id)
        if done or (skipped and not errors):
            inc.status = "attached"
            inc.keys = ",".join(done or skipped)
            inc.attached_at = utcnow()
            inc.error = "; ".join(errors) or ("У книги на Kindle уже был EPUB — оставлен прежний" if not done else None)
        else:
            inc.error = "; ".join(errors)[:1000]
            # 400 — отказ по существу (EPUB не читается или не от этой книги Kindle): повтор на каждом
            # синке ничего не изменит, дальше только связать руками. Остальное повторим на следующем синке.
            if errors and refused == len(errors):
                inc.status = "rejected"
        db.commit()
        status = inc.status
    if done:
        log.info("Incoming EPUB «%s» attached to %s", filename, ", ".join(done))
    return {"status": status, "attached": done, "skipped": skipped, "errors": errors}


def match_incoming_epubs() -> int:
    """Ждущие EPUB из bookbot → книги Kindle с тем же именем файла."""
    if not READER_STATE["library"]:
        return 0
    if not _incoming_lock.acquire(blocking=False):
        return 0
    try:
        with db_session() as db:
            waiting = [(i.id, i.filename) for i in db.query(IncomingEpub).filter(IncomingEpub.status == "waiting")]
        books = READER_STATE["library"].get("books") or []
        matched = 0
        for inc_id, filename in waiting:
            keys = [b["key"] for b in books
                    if epub_matches_kindle(filename, kindle_stem(b.get("filename") or "", b["key"]))]
            if keys:
                attach_incoming(inc_id, keys)
                matched += 1
        return matched
    finally:
        _incoming_lock.release()


# --- EPUB → X3 (CrossPoint, передача файлов по Wi-Fi) ---
# Сервис передачи файлов на X3 работает, только пока на нём открыт экран передачи файлов,
# отправка возможна только по явному нажатию кнопки.
X3_STATE: Dict[str, Any] = {"online": None, "checked_at": None, "version": None, "error": None, "sending": None}
# Последний ответ /api/status и когда X3 был в сети — переживают рестарт полки (X3 обычно вне сети).
X3_DEVICE_FILE = os.path.join(BASE_DIR, "x3_device.json")
X3_RELEASES_API = "https://api.github.com/repos/crosspoint-reader/crosspoint-reader/releases/latest"
X3_RELEASE_CACHE = TTLCache(12 * 3600)


def x3_device_saved() -> Dict[str, Any]:
    try:
        with open(X3_DEVICE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


class X3Error(DeviceError):
    """Сеть или сам X3 недоступны. Наследник DeviceError: модуль x3 такие ошибки пропускает как есть,
    и «X3 пропал» (прекращаем связь) отличается от «не вышло с одним файлом» (идём дальше)."""


def x3_document_id(filename: str) -> str:
    """KOSync-ID документа у X3 — MD5 имени файла (метод filename)."""
    return hashlib.md5(filename.encode("utf-8")).hexdigest()


def x3_safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "-", name).strip() or "book.epub"


def x3_path(name: str) -> str:
    """Путь файла на карте X3: по нему прошивка находит и кеш книги с её местом чтения."""
    return posixpath.join("/", X3_BOOKS_DIR, name)


def x3_request(method: str, path: str, **kwargs) -> requests.Response:
    if not X3_ENABLED:
        raise X3Error("X3 не настроен (X3_URL).")
    timeout = kwargs.pop("timeout", 5)
    try:
        return requests.request(method, f"{X3_URL}{path}", timeout=timeout, **kwargs)
    except requests.RequestException as exc:
        raise X3Error(f"X3 не отвечает — открой на нём передачу файлов ({exc.__class__.__name__})") from exc


def x3_check() -> bool:
    try:
        r = x3_request("GET", "/api/status", timeout=3)
        data = r.json() if r.ok else {}
        X3_STATE.update(online=r.ok, version=data.get("version"), error=None if r.ok else f"HTTP {r.status_code}")
        if r.ok:
            try:
                with open(X3_DEVICE_FILE + ".new", "w", encoding="utf-8") as f:
                    json.dump({"status": data, "last_online": iso_utc(utcnow())}, f, ensure_ascii=False)
                os.replace(X3_DEVICE_FILE + ".new", X3_DEVICE_FILE)
            except OSError:
                log.warning("Could not save X3 device status", exc_info=True)
    except (X3Error, ValueError) as exc:
        X3_STATE.update(online=False, error=str(exc))
    X3_STATE["checked_at"] = utcnow()
    return bool(X3_STATE["online"])


def x3_latest_release() -> Optional[Dict[str, Any]]:
    """Последний стабильный релиз CrossPoint на GitHub — раз в 12 часов, без ключа."""
    cached = X3_RELEASE_CACHE.get("latest")
    if cached is not None:
        return cached or None
    try:
        r = requests.get(X3_RELEASES_API, timeout=10, headers={"Accept": "application/vnd.github+json"})
        data = r.json() if r.ok else {}
        release = {"tag": data.get("tag_name"), "published_at": data.get("published_at"), "url": data.get("html_url")} \
            if data.get("tag_name") else {}
    except (requests.RequestException, ValueError):
        release = {}
    X3_RELEASE_CACHE.set("latest", release)
    return release or None


def save_x3_listing(files: List[Dict[str, Any]]) -> None:
    try:
        with db_session() as db:
            db.execute(sql_text(
                "INSERT INTO x3_listing(id,checked_at,directory,files,revision) VALUES(1,:at,:dir,:files,1) "
                "ON CONFLICT(id) DO UPDATE SET checked_at=excluded.checked_at,directory=excluded.directory,"
                "files=excluded.files,revision=x3_listing.revision+1"
            ), {"at": int(time.time()), "dir": X3_BOOKS_DIR, "files": json.dumps(files, ensure_ascii=False)})
            db.commit()
    except Exception:
        # Список уже получен: отправка или проверка не должны падать из-за записи о нём.
        log.warning("Could not save X3 file listing", exc_info=True)


def x3_fetch_listing() -> List[Dict[str, Any]]:
    """Файлы папки книг на X3: [{"name", "size", "epub"}].

    Единственное место, где полка читает /api/files устройства: каждый удачный ответ сохраняется,
    и по нему карточки показывают, лежит ли книга на X3 (x3_by_book). Фонового опроса нет —
    список обновляют только явные действия: проверка, отправка, раздел «Устройства». Они зовут
    x3_contact; напрямую сюда ходят лишь за подтверждением того, что полка сама только что изменила.
    """
    r = x3_request("GET", "/api/files", params={"path": X3_BOOKS_DIR}, timeout=15)
    if not r.ok:
        raise X3Error(f"X3 не отдал список файлов: HTTP {r.status_code}")
    try:
        entries = r.json()
    except ValueError as exc:
        raise X3Error("X3 не отдал список файлов: ответ не JSON") from exc
    if not isinstance(entries, list):
        raise X3Error("X3 не отдал список файлов: неожиданный ответ")
    files = [{"name": f["name"], "size": f.get("size"), "epub": bool(f.get("isEpub"))}
             for f in entries
             if isinstance(f, dict) and isinstance(f.get("name"), str) and f["name"] and not f.get("isDirectory")]
    save_x3_listing(files)
    return files


def x3_no_contact() -> Dict[str, Any]:
    return {"positions": 0, "removed": []}


def x3_report() -> Dict[str, Any]:
    """Что сделано на X3 с прошлого ответа, дошедшего до страницы (поле contact). Память процесса."""
    return X3_STATE.pop("unreported", None) or x3_no_contact()


def x3_contact() -> tuple:
    """Связь с X3 по явному действию: (свежий список файлов, что сделано заодно).

    X3 в сети редко, поэтому каждый полученный список — повод сделать то, что ждало устройства:
    убрать с него файлы удалённых карточек и забрать места чтения. Второе значение —
    {"positions": сколько мест чтения принято, "removed": [имена убранных файлов]} — уходит в ответ
    действия. Само действие попутная работа не роняет: что не вышло, повторится при следующей связи.
    """
    files, contact = x3_fetch_listing(), x3_no_contact()
    try:
        contact["removed"] = x3_remove_deleted(files)
        if contact["removed"]:
            # Список после удаления; не отдаст его X3 — действие продолжит с прежним без убранного.
            files = [f for f in files if f["name"] not in contact["removed"]]
            files = x3_fetch_listing()
    except Exception:
        log.warning("Files of deleted books were not cleaned from X3", exc_info=True)
    try:
        contact["positions"] = x3_take_positions(files)
    except Exception:
        log.warning("Reading positions were not taken from X3", exc_info=True)
    # Ответ действия может оказаться ошибкой и отчёт не донести: он ждёт первого ответа, который дойдёт.
    waiting = X3_STATE.setdefault("unreported", x3_no_contact())
    waiting["positions"] += contact["positions"]
    waiting["removed"] += [name for name in contact["removed"] if name not in waiting["removed"]]
    return files, contact


def x3_remove_deleted(files: List[Dict[str, Any]]) -> List[str]:
    """Убрать с X3 файлы удалённых карточек; возвращает убранные имена.

    Стираются только имена из записей об удалении (DeletedBook.x3_files), на которые не претендует
    живая карточка, — ничего другого полка с устройства сама не удаляет. Запись ждёт одной удачной
    связи: после неё имена забываются, и такой же файл, положенный на X3 позже, никто не тронет.
    """
    with db_session() as db:
        records = db.query(DeletedBook).filter(DeletedBook.x3_files != "[]").all()
        if not records:
            return []
        waited = [record.id for record in records]
        doomed = {name for record in records for name in json.loads(record.x3_files or "[]")}
        names = [f["name"] for f in files if f["name"] in doomed]
        owners = x3_file_owners(db, names)
    removed = [name for name in names if name not in owners]
    if removed:
        from . import x3
        x3.delete(x3_request, [x3_path(name) for name in removed])
    with db_session() as db:
        db.query(DeletedBook).filter(DeletedBook.id.in_(waited)).update({"x3_files": "[]"}, synchronize_session=False)
        if removed:
            # Иначе та же книга, добавленная снова, получила бы эту отправку назад и метку «X3: нет».
            db.execute(sql_text("DELETE FROM deliveries WHERE device='x3' AND book_id IS NULL AND filename IN :names")
                       .bindparams(bindparam("names", expanding=True)), {"names": removed})
        db.commit()
    return removed


def x3_take_positions(files: List[Dict[str, Any]], strict: bool = False) -> int:
    """Места чтения, которые X3 хранит сам, — в библиотеку; возвращает, сколько из них принято.

    Прошивка пишет место в progress.bin кеша книги и отдаёт его по Wi-Fi, даже когда KOSync на X3
    не настроен. Спрашиваем про каждый EPUB списка, чей файл в библиотеке известен
    (x3_file_owners), по одному: веб-сервер X3 держит одно соединение; на первой сетевой ошибке
    останавливаемся. Библиотеке передаём только то, что изменилось с прошлой связи
    (X3Listing.positions): иначе прежнее место возвращалось бы после «Сбросить позицию».
    """
    with db_session() as db:
        owners = x3_file_owners(db, [f["name"] for f in files if f["epub"]])
        same = x3_same_file(db, files, owners)
        row = db.get(X3Listing, 1)
        try:
            seen = json.loads((row.positions if row else None) or "{}")
        except ValueError:
            seen = {}
    assets = {x3_path(name): (name, *known) for name, known in same.items()}
    if not assets:
        return 0
    from . import x3
    core, taken = reader_service.store.core, 0
    fresh = {path: seen[path] for path in assets if path in seen}
    try:
        for path, (name, book_id, sha256) in assets.items():
            try:
                position = x3.read_progress(x3_request, path)
            except X3Error as exc:
                if strict:
                    raise
                log.info("X3 went away while its reading positions were read: %s", exc)
                break
            except x3.DeviceError as exc:
                if strict:
                    raise
                log.warning("Cannot read the X3 position of %s: %s", name, exc)
                continue
            if position is None:  # книгу на X3 ещё не открывали
                fresh.pop(path, None)
                continue
            if position == seen.get(path):
                continue
            try:
                # Без смещения по тексту место известно только с точностью до главы: число страниц в
                # файле бывает не полным (после «Delete Book Cache»), и доля увела бы книгу вперёд.
                answer = core.accept_reader_position(
                    book_id, sha256, position["spine"], visible_offset=position.get("visible_offset"),
                    document=x3_document_id(name))
                taken += bool(answer.get("accepted"))
            except (Problem, ValueError, RuntimeError) as exc:
                log.warning("X3 position of %s was not accepted: %s", name, exc)
            fresh[path] = position
    finally:
        if fresh != seen:
            with db_session() as db:
                db.query(X3Listing).filter(X3Listing.id == 1).update(
                    {"positions": json.dumps(fresh, ensure_ascii=False)}, synchronize_session=False)
                db.commit()
    return taken


def refresh_reader_snapshots(keys: List[str]) -> None:
    """Снимки этих книг — сейчас, а не на следующем круге синка: метка «X3» на карточке
    берётся из снимка, и без этого появлялась бы только после прохода обработчика."""
    try:
        snaps = library_snapshots(reader_library())
    except ReaderSyncError:
        return
    with db_session() as db:
        for link in db.query(ReaderLink).filter(ReaderLink.key.in_(keys)):
            if link.key in snaps:
                update_link_snapshot(link, snaps[link.key])
        db.commit()


def x3_status() -> Dict[str, Any]:
    def row(i: X3Upload) -> Dict[str, Any]:
        return {"key": i.key, "filename": i.filename, "source": i.source, "status": i.status, "error": i.error,
                "attempts": i.attempts or 0, "created_at": iso_utc(i.created_at), "sent_at": iso_utc(i.sent_at)}
    with db_session() as db:
        queue = [row(i) for i in db.query(X3Upload).filter(X3Upload.status != "sent").order_by(X3Upload.created_at)]
        history = [row(i) for i in db.query(X3Upload).filter(X3Upload.status == "sent")
                   .order_by(X3Upload.sent_at.desc().nullslast()).limit(30)]
        rows = db.execute(sql_text("SELECT * FROM deliveries WHERE device='x3' ORDER BY sent_at DESC LIMIT 30")).mappings()
        new_history = [dict(key=f"book:{r['book_id']}",book_id=r['book_id'],filename=r['filename'],sha256=r['sha256'],
                            source='manual',status='sent',sent_at=datetime.fromtimestamp(r['sent_at'],timezone.utc).isoformat()) for r in rows]
        new_names = {r['filename'] for r in new_history}
        history = sorted(new_history+[r for r in history if r['filename'] not in new_names],key=lambda r:r['sent_at'] or '',reverse=True)[:30]
        listing_checked_at = unix_iso(x3_listing(db)[0])
    # checked_at — когда этот процесс последний раз проверял доступность X3; listing_checked_at —
    # когда получен сохранённый список его файлов, по которому карточки судят о наличии книг.
    return {"enabled": X3_ENABLED, "online": X3_STATE["online"], "checked_at": iso_utc(X3_STATE["checked_at"]),
            "version": X3_STATE["version"], "error": X3_STATE["error"], "sending": X3_STATE["sending"],
            "listing_checked_at": listing_checked_at, "queue": queue, "history": history}


# ----------------------------
# Русские названия
# ----------------------------
_QUOTE_PAIRS = (("**", "**"), ("«", "»"), ('"', '"'), ("'", "'"), ("„", "“"), ("“", "”"))


def clean_title_ru(value: Optional[str]) -> Optional[str]:
    """Название от модели или из файла: без внешних кавычек и markdown. Без кириллицы — не русское."""
    text = re.sub(r"\s+", " ", value or "").strip()
    changed = True
    while changed:
        changed = False
        for left, right in _QUOTE_PAIRS:
            if len(text) > len(left) + len(right) and text.startswith(left) and text.endswith(right):
                text, changed = text[len(left):-len(right)].strip(), True
    return text if CYRILLIC.search(text) else None


def needs_title_ru(book: Book) -> bool:
    return book.title_ru_source is None and not CYRILLIC.search(book.title or "")


def reset_auto_title_ru(book: Book) -> bool:
    """Название поменялось — русское от модели могло устареть. Ручное и с Kindle не трогаем."""
    if book.title_ru_source in ("ai", "none"):
        book.title_ru, book.title_ru_source = None, None
        return True
    return False


def kindle_file_hints(snapshots: List[Optional[str]]) -> List[str]:
    """Имена файлов на Kindle без ключа и расширения: у переводов это транслит русского издания."""
    hints = []
    for raw in snapshots:
        try:
            snap = json.loads(raw or "{}")
        except ValueError:
            continue
        name = re.sub(r"_[A-Z0-9]{32}\.kfx$", "", snap.get("filename") or snap.get("title") or "", flags=re.I)
        if name:
            hints.append(name)
    return hints


class TitleRu(BaseModel):
    id: int
    title_ru: Optional[str] = None


class TitlesRu(BaseModel):
    books: List[TitleRu] = Field(default_factory=list)


TITLE_RU_PROMPT = (
    "Для каждой книги из списка назови, под каким названием она выходила на русском языке.\n"
    "- Нужно название русского издания, а не дословный перевод: The Stars My Destination — «Тигр! Тигр!».\n"
    "- kindle_files — имена файлов этой книги на читалке. Если имя — транслит русского названия "
    "(li_chajld-etazh_smerti → «Этаж смерти»), бери название оттуда: это издание, которое у читателя.\n"
    "- Цикл и номер тома пиши, только если они есть в названии русского издания.\n"
    "- Не издавалась на русском или не уверен — null. Не выдумывай и не переводи сам.\n"
    "Ответ — строго JSON без markdown: "
    '{{"books": [{{"id": 1, "title_ru": "Этаж смерти"}}, {{"id": 2, "title_ru": null}}]}}\n\n'
    "Книги JSON:\n{books}"
)
TITLE_RU_BATCH = 25


def queue_title_translation() -> None:
    # AI work is requested explicitly through the API.
    return None


def translate_missing_titles() -> None:
    with db_session() as db:
        books = [b for b in db.query(Book).filter(Book.title_ru_source.is_(None)).order_by(Book.id)
                 if needs_title_ru(b)]
        if not books:
            return
        files: Dict[int, List[Optional[str]]] = {}
        for link in db.query(ReaderLink).filter(ReaderLink.book_id.in_([b.id for b in books])):
            files.setdefault(link.book_id, []).append(link.snapshot)
        items = [{"id": b.id, "title": b.title, "authors": b.authors, "year": b.year,
                  "kindle_files": kindle_file_hints(files.get(b.id, []))} for b in books]

    for start in range(0, len(items), TITLE_RU_BATCH):
        chunk = items[start:start + TITLE_RU_BATCH]
        try:
            answer = ai_structured(TITLE_RU_PROMPT.format(books=json.dumps(chunk, ensure_ascii=False)),
                                   TitlesRu, AI_HINT_MODEL)
        except AIError as exc:
            log.warning("Russian titles failed: %s", exc)
            return
        found = {t.id: clean_title_ru(t.title_ru) for t in answer.books}
        with db_session() as db:
            for item in chunk:
                book = db.get(Book, item["id"])
                # Пока ждали модель, название могли поправить или задать руками — тогда ответ не наш.
                if not book or book.title_ru_source is not None or book.title != item["title"]:
                    continue
                if found.get(item["id"]):
                    book.title_ru, book.title_ru_source = found[item["id"]], "ai"
                else:
                    # Русского издания нет или модель промолчала: не спрашиваем снова на каждом синке.
                    book.title_ru_source = "none"
            db.commit()
        log.info("Russian titles: %s of %s found", sum(1 for i in chunk if found.get(i["id"])), len(chunk))


def queue_missing_summaries() -> int:
    # Kept for compatibility with old maintenance code; never generates automatically.
    return 0


# ----------------------------
# Endpoints
# ----------------------------
@app.get("/api/search", response_model=List[SearchItem])
def search(
    q: str = Query(..., min_length=2),
    limit: int = Query(15, ge=1, le=100),
):
    try:
        docs = ol_search(q.strip(), limit=limit)
    except requests.RequestException:
        log.warning("OpenLibrary search failed for %r", q, exc_info=True)
        raise HTTPException(status_code=502, detail="OpenLibrary не отвечает, попробуй ещё раз.")

    out: List[SearchItem] = []
    for d in docs:
        work_key = d.get("key")
        if not work_key:
            continue

        langs = d.get("language") or []
        lang2 = lang_from_ol(langs[0]) if isinstance(langs, list) and langs else None

        out.append(SearchItem(
            source="openlibrary",
            source_id=work_key,
            title=d.get("title") or "Без названия",
            authors=normalize_authors(d.get("author_name") or []),
            year=d.get("first_publish_year"),
            language=lang2,
            cover=pick_best_cover(d),
        ))
    return out


class SearchHint(BaseModel):
    title: str = ""
    author: str = ""
    title_ru: Optional[str] = None


class SearchHints(BaseModel):
    books: List[SearchHint] = Field(default_factory=list)


SEARCH_HINT_PROMPT = (
    "Пользователь ищет книгу в каталоге OpenLibrary и ввёл запрос: «{q}».\n"
    "Запрос может быть на любом языке: русское название переводной книги, оригинальное название, имя автора, "
    "цикл, неточное или частичное название, пересказ сюжета.\n"
    "Перечисли до 8 книг, которые он скорее всего имеет в виду, самые вероятные первыми. Для каждой — "
    "оригинальное название и автора так, как они записаны в англоязычных каталогах "
    "(«Задача трёх тел» → «The Three-Body Problem», Cixin Liu); для книг, написанных по-русски, — "
    "русское название и автора. title_ru — под каким названием книга выходила на русском "
    "(не дословный перевод), не издавалась — null.\n"
    "Если запрос однозначно указывает на одну книгу — верни только её, не добавляй похожие. "
    "Если запрос — имя автора или цикл, перечисли самые известные книги. Не выдумывай несуществующих книг.\n"
    "Ответ — строго JSON без markdown и пояснений: "
    '{{"books": [{{"title": "...", "author": "...", "title_ru": "..."}}]}}'
)
AI_SEARCH_CACHE = TTLCache(3600)
LATIN_OR_CYRILLIC = re.compile(r"[A-Za-zА-Яа-яЁё]")


@app.get("/api/search/ai", response_model=List[SearchItem])
def search_ai(q: str = Query(..., min_length=2)):
    """Поиск «по смыслу»: модель называет, какие книги имеются в виду, в оригинале — ищем их в OpenLibrary.

    Нужен для русских запросов: переводов OpenLibrary почти не знает.
    """
    if not AI_ENABLED:
        raise HTTPException(status_code=400, detail="AI не настроен (нет AI_URL / AI_MODEL).")
    q = q.strip()
    cached = AI_SEARCH_CACHE.get(q.lower())
    if cached is not None:
        return cached
    try:
        hints = ai_structured(SEARCH_HINT_PROMPT.format(q=q), SearchHints, AI_HINT_MODEL).books[:8]
    except AIError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    fields = "key,title,author_name,first_publish_year,language,cover_i,isbn"

    def lookup(hint: SearchHint) -> Optional[Dict[str, Any]]:
        params = {"title": hint.title, "limit": 10, "fields": fields}
        if hint.author.strip():
            params["author"] = hint.author  # OpenLibrary сам фильтрует по автору, с учётом других написаний
        try:
            docs = ol_get("/search.json", params=params).get("docs") or []
        except requests.RequestException:
            log.warning("OpenLibrary lookup for AI hint %r failed", hint.title, exc_info=True)
            docs = []
        # Первым OpenLibrary часто отдаёт сборники («Set of 4 Books…») — берём самое похожее название.
        scored = sorted(((title_overlap(hint.title, d.get("title") or ""), d) for d in docs),
                        key=lambda x: x[0], reverse=True)
        doc = scored[0][1] if scored and scored[0][0] >= 0.6 else None
        if doc is None:
            # Похожего нет — работа может быть записана в оригинале («三体»). Wikidata знает точную ссылку.
            try:
                key = wikidata_match(hint.title, author_surname(hint.author)) if hint.author.strip() else None
                if key:
                    found = ol_get("/search.json", params={"q": f"key:{key}", "limit": 1, "fields": fields}).get("docs") or []
                    doc = found[0] if found else None
            except (requests.RequestException, ValueError, KeyError, TypeError):
                log.warning("Wikidata lookup for AI hint %r failed", hint.title, exc_info=True)
        if doc is None and scored and scored[0][0] >= 0.5:
            doc = scored[0][1]
        item = search_item_from_doc(doc, q) if doc else None
        if item:
            item["hint"] = f"{hint.title} — {hint.author}".strip(" —")
            if not LATIN_OR_CYRILLIC.search(item["title"]):
                item["title"] = hint.title
            title_ru = clean_title_ru(hint.title_ru)
            if title_ru and title_ru.lower() != item["title"].lower():
                item["title_ru"] = title_ru
        return item

    hints = [h for h in hints if h.title.strip()]
    out, seen = [], set()
    if hints:
        with ThreadPoolExecutor(max_workers=min(4, len(hints))) as pool:
            for item in pool.map(lookup, hints):
                if item and item["source_id"] not in seen:
                    seen.add(item["source_id"])
                    out.append(item)
    AI_SEARCH_CACHE.set(q.lower(), out)
    return out


@app.get("/api/books", response_model=List[BookOut])
def list_books(status: Optional[str] = None, q: Optional[str] = None):
    with db_session() as db:
        query = db.query(Book)

        if status:
            status = status.strip().lower()
            if status != "all":
                if status not in VALID_STATUSES:
                    raise HTTPException(status_code=400, detail=f"Invalid status: {status}")
                query = query.filter(Book.status == status)

        if q and q.strip():
            pattern = f"%{escape_like(q.strip().lower())}%"
            query = query.filter(or_(
                func.py_lower(Book.title).like(pattern, escape="\\"),
                func.py_lower(Book.authors).like(pattern, escape="\\"),
                func.py_lower(Book.title_ru).like(pattern, escape="\\"),
            ))

        books = query.order_by(Book.updated_at.desc().nullslast(), Book.id.desc()).all()
        return books_out(db, books)


@app.get("/api/stats")
def stats():
    """Сводка по полке — сколько чего и как давно обновлялось."""
    with db_session() as db:
        by_status = dict(
            db.query(Book.status, func.count(Book.id)).group_by(Book.status).all()
        )
        total = sum(by_status.values())
        pending_summaries = (
            db.query(func.count(Book.id)).filter(Book.summary_status == "pending").scalar() or 0
        )
        return {
            "total": total,
            "by_status": {s: by_status.get(s, 0) for s in sorted(VALID_STATUSES)},
            "pending_summaries": pending_summaries,
        }


@app.get("/api/books/{book_id}", response_model=BookOut)
def get_book(book_id: int):
    with db_session() as db:
        book = db.get(Book, book_id)
        if not book:
            raise HTTPException(status_code=404, detail="Book not found")
        return books_out(db, [book])[0]


@app.post("/api/books")
def add_book(data: BookAddSchema, background_tasks: BackgroundTasks):
    status = (data.status or "want").strip().lower()
    if status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status: {status}")

    work_key = data.source_id
    if not work_key.startswith("/works/"):
        raise HTTPException(status_code=400, detail="Expected Open Library work key like /works/OL...W")

    # Existing books keep their status; adding is not a status-change action.
    with db_session() as db:
        existing = db.query(Book).filter(Book.source_id == work_key).first()
        if existing:
            return {"status": "exists", "id": existing.id, "book_status": existing.status,
                    "deleted_before": deleted_before(existing.id)}

    # Сеть — вне сессии БД: это 3-6 запросов и несколько секунд.
    try:
        bundle = fetch_work_bundle(work_key)
    except requests.RequestException:
        log.warning("OpenLibrary work fetch failed for %s", work_key, exc_info=True)
        raise HTTPException(status_code=502, detail="OpenLibrary не отвечает, попробуй ещё раз.")

    job_id = None
    with db_session() as db:
        title = bundle["title"]
        if data.title and not re.search(r"[A-Za-zА-Яа-яЁё]", title):
            title = data.title.strip()
        title_ru = None if CYRILLIC.search(title) else clean_title_ru(data.title_ru)
        new_book = Book(
            source_id=work_key,
            title=title,
            title_ru=title_ru,
            title_ru_source="ai" if title_ru else None,
            authors=bundle["authors"],
            status=status,
            year=bundle["year"],
            language=bundle["language"],
            description=bundle["description"],
            summary="",
            summary_status="idle",
            summary_job_id=job_id,
        )
        db.add(new_book)
        try:
            db.commit()
        except IntegrityError:
            # Кто-то добавил ту же книгу, пока мы ходили в сеть.
            db.rollback()
            existing = db.query(Book).filter(Book.source_id == work_key).first()
            if existing:
                return {"status": "exists", "id": existing.id, "book_status": existing.status,
                        "deleted_before": deleted_before(existing.id)}
            raise
        db.refresh(new_book)
        book_id = new_book.id

    local_path = download_first_cover(book_id, bundle["cover_urls"])
    if local_path:
        with db_session() as db:
            book = db.get(Book, book_id)
            if book:
                book.cover_path = local_path
                db.commit()

    queue_title_translation()
    return {"status": "ok", "id": book_id, "deleted_before": deleted_before(book_id)}


def _apply_book_patch(book_id: int, data: BookPatchSchema) -> Dict[str, Any]:
    changes = data.model_dump(exclude_unset=True)
    if "status" in changes:
        status = (changes["status"] or "").strip().lower()
        if status not in VALID_STATUSES:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")
        changes["status"] = status
    if changes.get("drop_reason") and changes["drop_reason"] not in DROP_REASONS:
        raise HTTPException(status_code=400, detail=f"Invalid drop_reason: {changes['drop_reason']}")
    if "title_ru" in changes:
        changes["title_ru_source"] = "manual"

    with db_session() as db:
        book = db.get(Book, book_id)
        if not book:
            raise HTTPException(status_code=404, detail="Book not found")

        requeue_title = False
        if "description" in changes and changes["description"] != book.description:
            book.description_ru = None
            book.description_translation_status = "idle"
            book.description_translation_job_id = None
        if "title" in changes and changes["title"] != book.title and "title_ru" not in changes:
            requeue_title = reset_auto_title_ru(book)
        for field, value in changes.items():
            setattr(book, field, value)

        if "summary" in changes:
            # Ручная правка обзора отменяет ожидание генерации.
            book.summary_status = "ready" if (changes["summary"] or "").strip() else "idle"
            book.summary_job_id = None

        book.updated_at = utcnow()
        db.commit()
    if requeue_title:
        queue_title_translation()
    return {"status": "updated", "id": book_id}


@app.patch("/api/books/{book_id}")
def patch_book(book_id: int, data: BookPatchSchema):
    return _apply_book_patch(book_id, data)


@app.put("/api/books/{book_id}")
def update_book(book_id: int, data: BookPatchSchema):
    """PUT оставлен для совместимости со старым фронтом; семантика та же."""
    return _apply_book_patch(book_id, data)


@app.post("/api/books/{book_id}/refresh-cover")
def refresh_cover(book_id: int):
    """Перекачивает обложку: из OpenLibrary, а для книг с читалки — из их KFX."""
    with db_session() as db:
        book = db.get(Book, book_id)
        if not book:
            raise HTTPException(status_code=404, detail="Book not found")
        work_key = book.source_id or ""
        reader_keys = [l.key for l in db.query(ReaderLink).filter(ReaderLink.book_id == book_id).all()]

    local_path = None
    if work_key.startswith("/works/"):
        candidates = cover_candidates_for_work(work_key)
        local_path = download_first_cover(book_id, candidates) if candidates else None
    if not local_path and reader_keys:
        local_path = reader_cover(book_id, reader_keys)
    if not local_path:
        raise HTTPException(status_code=404, detail="Обложка не нашлась ни в OpenLibrary, ни в файле с читалки")

    with db_session() as db:
        book = db.get(Book, book_id)
        if not book:
            raise HTTPException(status_code=404, detail="Book not found")
        # Кеш браузера сбрасываем через ?v=updated_at на фронте, а не в самом пути.
        book.cover_path = local_path
        book.updated_at = utcnow()
        db.commit()
        return {"status": "ok", "cover_path": local_path, "cover_version": iso_utc(book.updated_at)}


@app.post("/api/books/{book_id}/regenerate-summary")
def regenerate_summary(book_id: int, background_tasks: BackgroundTasks):
    if not AI_ENABLED:
        raise HTTPException(status_code=400, detail="AI не настроен (нет AI_URL / AI_MODEL).")

    with db_session() as db:
        db.execute(sql_text("BEGIN IMMEDIATE"))
        book = db.get(Book, book_id)
        if not book:
            raise HTTPException(status_code=404, detail="Book not found")
        if book.summary_status == "pending":
            raise HTTPException(status_code=409, detail="Обзор уже генерируется.")

        # Старый текст не стираем: пусть висит, пока не приедет новый.
        job_id = uuid.uuid4().hex
        book.summary_job_id = job_id
        book.summary_status = "pending"
        book.updated_at = utcnow()
        db.commit()
        title, authors, description = book.title, book.authors, book.description or ""

    from .tasks import enqueue
    enqueue("summary", {"book_id": book_id, "title": title, "authors": authors, "description": description, "job_id": job_id}, token=job_id, lane="ai")
    return {"status": "queued"}


@app.post("/api/books/{book_id}/translate-description")
def translate_description(book_id: int):
    from .description import request_translation
    return request_translation(book_id)


@app.get("/api/recommendations")
def get_recommendations(run_id: Optional[int] = None):
    with db_session() as db:
        if run_id:
            run = db.get(RecommendationRun, run_id)
            if not run:
                raise HTTPException(status_code=404, detail="Recommendation run not found")
            return serialize_recommendation_run(db, run, archive=True)

        return serialize_recommendation_run(db, latest_ready_run(db))


@app.get("/api/recommendations/history")
def get_recommendation_history(limit: int = Query(20, ge=1, le=100)):
    with db_session() as db:
        runs = (
            db.query(RecommendationRun)
            .filter(RecommendationRun.status == "ready")
            .order_by(RecommendationRun.created_at.desc().nullslast(), RecommendationRun.id.desc())
            .limit(limit)
            .all()
        )
        if not runs:
            return []

        # Один group-by вместо отдельного COUNT на каждый прогон.
        counts = (
            db.query(Recommendation.run_id, Recommendation.kind, func.count(Recommendation.id))
            .filter(
                Recommendation.run_id.in_([r.id for r in runs]),
                Recommendation.status == "active",
            )
            .group_by(Recommendation.run_id, Recommendation.kind)
            .all()
        )
        count_map: Dict[int, Dict[str, int]] = {}
        for rid, kind, count in counts:
            count_map.setdefault(rid, {})[kind] = count

        return [
            {
                "id": run.id,
                "created_at": iso_utc(run.created_at),
                "status": run.status,
                "analysis": run.analysis or "",
                "planned_count": count_map.get(run.id, {}).get("planned", 0),
                "external_count": count_map.get(run.id, {}).get("external", 0),
                "stale": not recommendation_run_is_fresh(run),
            }
            for run in runs
        ]


@app.post("/api/recommendations/refresh")
def refresh_recommendations(background_tasks: BackgroundTasks):
    """Ставит генерацию в фон и сразу отвечает.

    Раньше это был синхронный запрос на 2 вызова модели + до 8 поисков
    OpenLibrary — за любым прокси он гарантированно ловил таймаут.
    """
    if not AI_ENABLED:
        raise HTTPException(status_code=400, detail="AI не настроен (нет AI_URL / AI_MODEL).")

    with db_session() as db:
        if pending_run(db):
            raise HTTPException(status_code=409, detail="Подборка уже генерируется, подожди.")

        if not db.query(func.count(Book.id)).scalar():
            raise HTTPException(status_code=400, detail="Библиотека пуста — рекомендовать нечего.")

        run = RecommendationRun(status="pending", analysis="")
        db.add(run)
        db.commit()
        db.refresh(run)
        run_id = run.id

    from .tasks import enqueue
    enqueue("recommendations", {"run_id": run_id}, token=str(run_id), lane="ai")

    with db_session() as db:
        return serialize_recommendation_run(db, latest_ready_run(db))


@app.put("/api/recommendations/{recommendation_id}")
def update_recommendation(recommendation_id: int, data: RecommendationDismissSchema):
    status = (data.status or "dismissed").strip().lower()
    if status not in RECOMMENDATION_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status: {status}")

    with db_session() as db:
        rec = db.get(Recommendation, recommendation_id)
        if not rec:
            raise HTTPException(status_code=404, detail="Recommendation not found")
        rec.status = status
        db.commit()
        return {"status": "updated"}


@app.post("/api/recommendations/{recommendation_id}/{action}")
def recommendation_book_action(recommendation_id: int, action: str):
    actions = {"start": ({"want"}, "reading"),
               "plan": ({"read", "dropped"}, "want"),
               "resume": ({"read", "dropped"}, "reading")}
    if action not in actions:
        raise HTTPException(status_code=404, detail="Unknown recommendation action")
    allowed, target = actions[action]
    with db_session() as db:
        db.execute(sql_text("BEGIN IMMEDIATE"))
        rec = db.get(Recommendation, recommendation_id)
        if not rec or rec.status != "active":
            raise HTTPException(status_code=409, detail="Рекомендация больше не доступна. Обнови подборку.")
        book = (db.get(Book, rec.book_id) if rec.kind == "planned" and rec.book_id
                else db.query(Book).filter(Book.source_id == rec.source_id).first()
                if rec.kind == "external" and rec.source_id else None)
        if not book or book.status not in allowed:
            raise HTTPException(status_code=409, detail="Статус книги изменился или книга удалена. Обнови подборку.")
        book.status = target
        book.updated_at = utcnow()
        db.commit()
        return {"id": book.id, "book_status": book.status}


def x3_names_of_book(db, book_id: int) -> List[str]:
    """Под какими именами файлы карточки известны на X3: всё, что для неё отправляли, и файлы
    последнего списка X3, которые к ней относятся."""
    names = {d["filename"] for d in deliveries_by_book(db, [book_id]).get(book_id, [])}
    listed = [f for f in x3_listing(db)[1] if f.get("name")]
    owners = x3_file_owners(db, {f["name"] for f in listed})
    names.update(name for name, known in x3_same_file(db, listed, owners).items() if known[0] == book_id)
    # Привязку через файл Kindle создаёт владелец или синк самой X3, а не загрузка: ей верим по имени.
    names.update(name for name, books in owners.items() if book_id in books and books[book_id] is None)
    return sorted(names)


def remove_book(book_id: int, tombstone: bool) -> None:
    """Убрать карточку и всё, что на неё ссылается. tombstone — оставить запись об удалении
    и стереть файлы; дублю, влитому в другую карточку, ни то ни другое не нужно."""
    with db_session() as db:
        db.execute(sql_text("BEGIN IMMEDIATE"))
        book = db.get(Book, book_id)
        if not book:
            return

        hashes: List[str] = []
        if tombstone:
            hashes = [row[0] for row in db.execute(sql_text("SELECT sha256 FROM book_assets WHERE book_id=:id"), {"id": book_id})]
            # Тот же EPUB остаётся у другой карточки: его копия на X3 теперь её, а не «файл удалённой».
            db.execute(sql_text(
                "UPDATE deliveries SET book_id=(SELECT MIN(a.book_id) FROM book_assets a WHERE a.sha256=deliveries.sha256 AND a.book_id<>:id) "
                "WHERE book_id=:id AND device='x3' AND sha256 IN (SELECT sha256 FROM book_assets WHERE book_id<>:id)"), {"id": book_id})
            record = DeletedBook(title=book.title or "", title_ru=book.title_ru, authors=book.authors or "",
                                 source_id=book.source_id, status=book.status, deleted_at=int(time.time()),
                                 x3_files=json.dumps(x3_names_of_book(db, book_id), ensure_ascii=False))
            db.add(record)
            db.flush()
            for sha256 in hashes:
                db.merge(DeletedEpub(sha256=sha256, deleted_book_id=record.id))
            # Место чтения, которое X3 хранит для этих файлов, ушло вместе с карточкой: «уже виденным»
            # оно больше не считается, иначе карточка, добавленная снова, его не получит.
            listing = db.get(X3Listing, 1)
            if listing and listing.positions:
                try:
                    seen = json.loads(listing.positions)
                except ValueError:
                    seen = {}
                kept = {path: value for path, value in seen.items() if path not in {x3_path(name) for name in json.loads(record.x3_files)}}
                if kept != seen:
                    listing.positions = json.dumps(kept, ensure_ascii=False)

        # EPUB из bookbot, ждавшие своей книги Kindle: без карточки ждать нечего, а в «Требуют
        # внимания» они висели бы дальше. EPUB, который есть и у другой книги, не трогаем.
        own = [row[0] for row in db.execute(sql_text(
            "SELECT sha256 FROM book_assets WHERE book_id=:id AND sha256 NOT IN "
            "(SELECT sha256 FROM book_assets WHERE book_id<>:id)"), {"id": book_id})]
        waiting = db.query(IncomingEpub).filter(IncomingEpub.status.in_(INCOMING_OPEN), IncomingEpub.sha256.in_(own)).all()
        incoming = [inc.sha256 for inc in waiting]
        for inc in waiting:
            db.delete(inc)

        # FK включён, а старые базы созданы без ON DELETE SET NULL — отвязываем сами,
        # иначе удаление книги, попавшей в рекомендации, падает с IntegrityError.
        db.query(Recommendation).filter(Recommendation.book_id == book_id).update(
            {"book_id": None}, synchronize_session=False
        )
        # Файлы с читалки остаются «ничьими» — так они не вернутся на полку при следующем синке.
        db.query(ReaderLink).filter(ReaderLink.book_id == book_id).update(
            {"book_id": None}, synchronize_session=False
        )

        db.delete(book)
        # Документы читалок удаляем, а не отвязываем: отвязанные показывались бы неизвестными
        # документами и не дали бы тому же файлу привязаться после повторной загрузки.
        # Привязанный через ключ Kindle остаётся при своей книге Kindle: карточка скрыта, но
        # читалка не должна вернуться «неизвестным документом» при следующем синке.
        db.execute(sql_text('UPDATE documents SET library_book_id=NULL,epub_sha256=NULL WHERE library_book_id=:id AND book_key IS NOT NULL'), {'id':book_id})
        db.execute(sql_text('DELETE FROM documents WHERE library_book_id=:id'), {'id':book_id})
        db.execute(sql_text('DELETE FROM kindle_assets WHERE book_id=:id'), {'id':book_id})
        db.execute(sql_text('UPDATE reader_observations SET book_id=NULL WHERE book_id=:id'), {'id':book_id})
        db.execute(sql_text('UPDATE deliveries SET book_id=NULL WHERE book_id=:id'), {'id':book_id})
        db.commit()

    for path in [os.path.join(COVERS_DIR, f"{book_id}.jpg"), *map(incoming_path, incoming)]:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            log.warning("Could not remove %s of deleted book %s", path, book_id, exc_info=True)
    if hashes:
        try:
            # Библиотека сама оставит то, чем пользуется другая карточка или книга Kindle.
            reader_service.store.core.discard(hashes)
        except (OSError, sqlite3.Error):
            log.warning("Could not remove EPUB files of deleted book %s", book_id, exc_info=True)


@app.delete("/api/books/{book_id}")
def delete_book(book_id: int):
    """Удаляет карточку так, чтобы от неё ничего не осталось на виду и ничто не вернуло её на полку.

    Вместе с ней уходят её EPUB (если ими не пользуется другая карточка или книга Kindle) и
    обложка; файлы на X3 убираются при первой связи с ним (x3_remove_deleted). Остаётся запись
    об удалении: такая же книга, добавленная позже, — обычная новая карточка с пометкой
    «вы её удаляли». Сторона Kindle (KFX, карты, его копия EPUB) не трогается: книга на Kindle
    лежит, а её связь с полкой остаётся скрытой.
    """
    remove_book(book_id, tombstone=True)
    return {"status": "deleted"}


@app.delete("/api/deleted/{deleted_id}")
def forget_deleted(deleted_id: int):
    """«Вы её удаляли» больше не показывать. Закрываются и более ранние записи той же книги, иначе
    под крестиком оказалась бы предыдущая дата. Записи и хеши остаются: по ним файлы ещё убираются
    с X3, а обработчик не возвращает карточку из сохранённой записи bookbot."""
    with db_session() as db:
        record = db.get(DeletedBook, deleted_id)
        if record:
            keys = set(same_book_keys(record.source_id, record.title, record.authors))
            hashes = {row[0] for row in db.query(DeletedEpub.sha256).filter(DeletedEpub.deleted_book_id == deleted_id)}
            for other in db.query(DeletedBook).filter(DeletedBook.dismissed == 0, DeletedBook.deleted_at <= record.deleted_at):
                same = other.id == record.id or keys & set(same_book_keys(other.source_id, other.title, other.authors))
                if same or hashes & {row[0] for row in db.query(DeletedEpub.sha256).filter(DeletedEpub.deleted_book_id == other.id)}:
                    other.dismissed = 1
            db.commit()
    return {"status": "forgotten"}


@app.post("/api/books/{book_id}/merge")
def merge_book(book_id: int, data: MergeSchema):
    """Книга — дубль target: всё полезное переезжает туда, сама она удаляется.

    Карточка target главная: её название, статус и данные остаются. Из дубля берём только то,
    чего у target нет: статус вместо «Общего», ссылку на каталог вместо kindle:…, описание,
    год, язык, обзор, обложку. Файлы с читалок и рекомендации переезжают целиком.
    """
    if book_id == data.target_id:
        raise HTTPException(status_code=400, detail="Нельзя объединить книгу саму с собой")
    taken: List[str] = []
    with db_session() as db:
        source, target = db.get(Book, book_id), db.get(Book, data.target_id)
        if not source or not target:
            raise HTTPException(status_code=404, detail="Book not found")

        if target.status == "unsorted" and source.status != "unsorted":
            target.status = source.status
            target.drop_reason, target.drop_note = source.drop_reason, source.drop_note
            taken.append("status")
        source_key = source.source_id or ""
        if source_key.startswith("/works/") and not (target.source_id or "").startswith("/works/"):
            # source_id уникален: сначала освобождаем его у дубля, иначе UPDATE target упрётся в индекс.
            source.source_id = f"merged:{source.id}"
            db.flush()
            target.source_id = source_key
            taken.append("catalog")
        if looks_like_filename(target.title) and source.title:
            target.title, target.authors = source.title, source.authors
            taken.append("title")
        if not target.title_ru and source.title_ru and target.title_ru_source != "manual":
            target.title_ru, target.title_ru_source = source.title_ru, source.title_ru_source
            taken.append("title_ru")
        for field in ("description", "year", "language"):
            if not getattr(target, field) and getattr(source, field):
                setattr(target, field, getattr(source, field))
                taken.append(field)
        if (target.description == source.description and source.description_ru
                and not target.description_ru and target.description_translation_status != "pending"):
            target.description_ru = source.description_ru
            target.description_translation_status = "ready"
            taken.append("description_ru")
        if not (target.summary or "").strip() and (source.summary or "").strip() and target.summary_status != "pending":
            target.summary, target.summary_status = source.summary, "ready"
            taken.append("summary")

        db.query(ReaderLink).filter(ReaderLink.book_id == book_id).update(
            {"book_id": target.id}, synchronize_session=False
        )
        db.query(Recommendation).filter(Recommendation.book_id == book_id).update(
            {"book_id": target.id}, synchronize_session=False
        )
        target_needs_cover = not target.cover_path
        # Дубль убирали с X3 кнопкой: его старые отправки не должны дать цели «X3: нет». Собственную
        # метку цели (её файл лежит на X3) это не трогает.
        if source.x3_removed_at and books_out(db, [target])[0].x3 is None:
            target.x3_removed_at = max(source.x3_removed_at, target.x3_removed_at or 0)
        from .library import merge_assets
        merge_assets(db, book_id, target.id)
        target.updated_at = utcnow()
        db.commit()

    # Обложка дубля полезнее пустой карточки.
    src_cover = os.path.join(COVERS_DIR, f"{book_id}.jpg")
    if target_needs_cover and os.path.exists(src_cover):
        os.replace(src_cover, os.path.join(COVERS_DIR, f"{data.target_id}.jpg"))
        with db_session() as db:
            target = db.get(Book, data.target_id)
            target.cover_path = f"/static/covers/{data.target_id}.jpg"
            db.commit()
        taken.append("cover")

    remove_book(book_id, tombstone=False)
    log.info("Merged book %s into %s (took: %s)", book_id, data.target_id, ", ".join(taken) or "—")
    return {"status": "merged", "id": data.target_id, "taken": taken}


@app.post("/api/books/{book_id}/catalog-match")
def catalog_rematch(book_id: int, use_ai: bool = False):
    """Ещё раз ищем работу в каталогах по текущему названию и автору (например, после правки)."""
    with db_session() as db:
        book = db.get(Book, book_id)
        if not book:
            raise HTTPException(status_code=404, detail="Book not found")
        title, authors = book.title, book.authors

    match = catalog_match(title, authors, use_ai=use_ai)
    if not match:
        raise HTTPException(status_code=404, detail="В Wikidata и OpenLibrary не нашлась. Поправь название/автора или найди через «Поиск».")

    with db_session() as db:
        keys = {match["work_key"], *match.get("aliases", [])}
        other = db.query(Book).filter(Book.source_id.in_(keys), Book.id != book_id).first()
        if other:
            return {"status": "duplicate", "id": other.id, "title": other.title}

    try:
        bundle = fetch_work_bundle(match["work_key"])
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="OpenLibrary не отвечает, попробуй ещё раз.")

    with db_session() as db:
        other = find_same_book(db, [bundle.get("title")], [bundle.get("authors")], exclude_id=book_id)
        if other:
            return {"status": "duplicate", "id": other.id, "title": other.title}

    with db_session() as db:
        book = db.get(Book, book_id)
        book.source_id = match["work_key"]
        requeue_title = False
        if looks_like_filename(book.title) and bundle.get("title"):
            book.title, book.authors = bundle["title"], bundle.get("authors") or book.authors
            requeue_title = reset_auto_title_ru(book)
        book.description = book.description or bundle["description"]
        book.year = book.year or bundle["year"]
        book.updated_at = utcnow()
        needs_cover = not book.cover_path
        db.commit()
    if requeue_title:
        queue_title_translation()
    if needs_cover:
        cover = download_first_cover(book_id, bundle["cover_urls"])
        if cover:
            with db_session() as db:
                db.get(Book, book_id).cover_path = cover
                db.commit()
    return {"status": "ok", "source_id": match["work_key"], "via": match["via"]}


READER_STATUS_MAX_AGE_SEC = 15
# Синк (:8089) и обработчик — отдельные процессы: каждый раз в 10 с пишет в каталог данных
# heartbeat-файл. Веб сам читает только общую базу, поэтому об их здоровье судит по этим файлам.
HEARTBEAT_FILES = {"sync": "sync-heartbeat.json", "worker": "worker-heartbeat.json"}
HEARTBEAT_MAX_AGE_SEC = 90  # как в bookhub.check


def heartbeat_path(service: str) -> str:
    return os.path.join(BASE_DIR, HEARTBEAT_FILES[service])


def service_alive(service: str) -> bool:
    try:
        with open(heartbeat_path(service), encoding="utf-8") as f:
            return time.time() - float(json.load(f)["time"]) <= HEARTBEAT_MAX_AGE_SEC
    except (OSError, ValueError, KeyError, TypeError):
        return False


@app.get("/api/reader/status")
def reader_status():
    # Запросы с X3 должны появляться на полке сразу, а не через интервал полного синка.
    fetched = READER_STATE["fetched_at"]
    if not fetched or (utcnow() - fetched).total_seconds() > READER_STATUS_MAX_AGE_SEC:
        try:
            reader_library()
        except ReaderSyncError:
            pass  # ошибка уже лежит в READER_STATE["error"]
    library = READER_STATE["library"] or {}
    # Удалённые с Kindle файлы reader-sync хранит с present=0 — в счётчики не идут.
    books = [b for b in library.get("books") or [] if b.get("present", 1)]
    with db_session() as db:
        hidden = db.query(func.count(ReaderLink.key)).filter(ReaderLink.book_id.is_(None)).scalar() or 0
        incoming = [
            {"id": i.id, "filename": i.filename, "size": i.size, "source": i.source, "status": i.status,
             "error": i.error, "created_at": iso_utc(i.created_at)}
            for i in db.query(IncomingEpub).filter(IncomingEpub.status.in_(INCOMING_OPEN)).order_by(IncomingEpub.id)
        ]
        # A durable revision also notices EPUB-only progress changed by the sync process,
        # and whatever moves the marks on the cards: a new file listing (its time is shown too),
        # a delivery, a removal from the X3, a dismissed "deleted before" note.
        revision = tuple(db.execute(sql_text("""SELECT
(SELECT COUNT(*) FROM books),(SELECT MAX(updated_at) FROM books),
(SELECT COUNT(*) FROM book_assets),(SELECT COALESCE(SUM(revision),0) FROM reading_positions),
(SELECT COUNT(*) FROM reader_observations),(SELECT MAX(received_at) FROM reader_observations),
(SELECT revision FROM x3_listing WHERE id=1),(SELECT checked_at FROM x3_listing WHERE id=1),
(SELECT COUNT(*) FROM deliveries),(SELECT MAX(sent_at) FROM deliveries),
(SELECT COUNT(*) FROM x3_uploads WHERE status='sent'),(SELECT MAX(sent_at) FROM x3_uploads WHERE status='sent'),
(SELECT COALESCE(SUM(x3_removed_at),0) FROM books),(SELECT COUNT(*) FROM deleted_books),
(SELECT COALESCE(SUM(dismissed),0) FROM deleted_books),
(SELECT COUNT(*) FROM documents WHERE library_book_id IS NOT NULL OR book_key IS NOT NULL),
(SELECT COALESCE(SUM(library_book_id*rowid),0) FROM documents),
(SELECT COUNT(book_id) FROM reader_links),(SELECT COALESCE(SUM(book_id*rowid),0) FROM reader_links)""")).first())
        epub_count = db.execute(sql_text('SELECT COUNT(*) FROM epub_assets')).scalar()
        last_sync = db.query(func.max(ReaderLink.synced_at)).scalar()
    return {
        "enabled": READER_ENABLED,
        "ai": AI_ENABLED,
        # ok — веб прочитал общую базу; живы ли остальные процессы — в services.
        "ok": READER_STATE["library"] is not None and READER_STATE["error"] is None,
        "services": {name: service_alive(name) for name in HEARTBEAT_FILES},
        "error": READER_STATE["error"],
        # Когда в последний раз менялись данные карточек: снимки пишутся только при изменении.
        "synced_at": iso_utc(last_sync or READER_STATE["synced_at"]),
        "library_revision": json.dumps(revision),
        "snapshots_at": iso_utc(READER_STATE["snapshots_at"]),
        "importing": dict(READER_STATE["importing"]),
        "books": len(books),
        "maps_ready": sum(b.get("map_status") == "ready" for b in books),
        "epubs": epub_count,
        "devices": library.get("devices") or [],
        "unbound_documents": [d for d in library.get("documents") or [] if not d.get("book_key") and not d.get("library_book_id")],
        "hidden": hidden,
        "incoming": incoming,
        "x3": x3_status(),
    }


@app.post("/api/reader/sync")
def reader_sync_now(background_tasks: BackgroundTasks):
    background_tasks.add_task(reader_sync_once)
    return {"status": "queued"}


@app.get("/api/books/{book_id}/reader")
def book_reader(book_id: int):
    """Всё из reader-sync по книге: файлы, карты, EPUB, позиция, X3. Живьём, с откатом на снимок."""
    with db_session() as db:
        if not db.get(Book, book_id):
            raise HTTPException(status_code=404, detail="Book not found")
        snaps = {l.key: load_snapshot(l) for l in db.query(ReaderLink).filter(ReaderLink.book_id == book_id).order_by(ReaderLink.key)}

    live, error, library = False, None, READER_STATE["library"] or {}
    if snaps:
        try:
            library = reader_library()
            fresh = library_snapshots(library)
            snaps = {k: fresh.get(k, v) for k, v in snaps.items()}
            live = True
        except ReaderSyncError as exc:
            error = str(exc)

    jobs = [j for j in library.get("jobs") or [] if j.get("book_key") in snaps][:10]
    with db_session() as db:
        x3 = {i.key: {"status": i.status, "filename": i.filename, "error": i.error, "sent_at": iso_utc(i.sent_at)}
              for i in db.query(X3Upload).filter(X3Upload.key.in_(list(snaps)))}
    snaps = {k: {**v, "x3": x3.get(k)} for k, v in snaps.items()}
    return {
        "live": live,
        "error": error,
        "items": list(snaps.values()),
        "jobs": jobs,
        "unbound_documents": [d for d in library.get("documents") or [] if not d.get("book_key") and not d.get("library_book_id")],
    }


def save_incoming_epub(body: bytes, name: str, source: str) -> Dict[str, Any]:
    """Блокирующая часть приёма EPUB из bookbot: карточка в библиотеке и копия во «входящих»."""
    imported = reader_service.store.core.ingest(body, name, origin=source or "upload")
    digest = imported["sha256"]
    # Запросы идут в разных потоках: один и тот же файл, присланный дважды подряд,
    # не должен писать одну копию и одну строку наперегонки.
    with _incoming_save_lock:
        with db_session() as db:
            existing = db.query(IncomingEpub).filter(IncomingEpub.sha256 == digest).first()
            if existing:
                return {"status": existing.status, "id": existing.id, "duplicate": True, "book_id": imported["book_id"],
                        "deleted_before": deleted_before(imported["book_id"])}

        path = incoming_path(digest)
        with open(f"{path}.tmp", "wb") as f:
            f.write(body)
        os.replace(f"{path}.tmp", path)
        with db_session() as db:
            inc = IncomingEpub(filename=name, sha256=digest, size=len(body), source=(source or "bookbot")[:40])
            db.add(inc)
            db.commit()
            inc_id = inc.id
    log.info("Incoming EPUB «%s» from %s (%s bytes)", name, source, len(body))
    return {"status": "ready", "id": inc_id, "book_id": imported["book_id"],
            "deleted_before": deleted_before(imported["book_id"])}


@app.post("/api/incoming/epub")
async def incoming_epub(request: Request, background_tasks: BackgroundTasks,
                        filename: str = Query(..., min_length=1), source: str = Query("bookbot")):
    """bookbot присылает EPUB, который только что ушёл на Kindle (тело — сам файл)."""
    name = os.path.basename(filename.strip())
    if not name.lower().endswith(".epub"):
        raise HTTPException(status_code=400, detail="Нужен .epub")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > INCOMING_MAX_BYTES:
            raise HTTPException(status_code=413, detail="EPUB больше 100 МБ")
        chunks.append(chunk)
    body = b"".join(chunks)
    if not body.startswith(b"PK"):
        raise HTTPException(status_code=400, detail="Это не EPUB (не zip)")
    # Хеш, разбор zip и SQLite — в пуле потоков: на event loop они останавливали весь веб-процесс
    # вместе с KOSync на :8088.
    result = await run_in_threadpool(save_incoming_epub, body, name, source)
    if not result.get("duplicate"):
        # Книга могла уже лежать на Kindle (повторная отправка) — пробуем прицепить сразу.
        background_tasks.add_task(match_incoming_epubs)
    return result


@app.post("/api/incoming/{inc_id}/attach")
def incoming_attach(inc_id: int, data: DocumentBindSchema):
    """Прицепить EPUB к книге Kindle руками (если имя файла не совпало). Прежний EPUB заменяется."""
    check_reader_key(data.key)
    return attach_incoming(inc_id, [data.key], replace=True)


@app.delete("/api/incoming/{inc_id}")
def incoming_delete(inc_id: int):
    with db_session() as db:
        inc = db.get(IncomingEpub, inc_id)
        if not inc:
            return {"status": "deleted"}
        digest = inc.sha256
        db.delete(inc)
        db.commit()
    try:
        os.remove(incoming_path(digest))
    except OSError:
        pass
    return {"status": "deleted"}


@app.put("/api/reader/{key}/link")
def reader_relink(key: str, data: ReaderLinkSchema):
    """Файл с читалки относится к другой книге полки."""
    check_reader_key(key)
    with db_session() as db:
        link = db.get(ReaderLink, key)
        if not link:
            raise HTTPException(status_code=404, detail="Файл с читалки не найден")
        if not db.get(Book, data.book_id):
            raise HTTPException(status_code=404, detail="Book not found")
        link.book_id = data.book_id
        db.commit()
    return {"status": "updated"}


@app.put("/api/reader/{key}/epub")
async def reader_upload_epub(key: str, request: Request):
    """EPUB книги Kindle вместо прежнего: по нему её место чтения переводится в EPUB и обратно.
    Нечитаемый или не подходящий к книге файл отклоняется сразу (400), прежний EPUB остаётся."""
    check_reader_key(key)
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Пустой файл")
    try:
        r = await run_in_threadpool(reader_service.upload_epub, key, body)
    except ReaderSyncError as exc:
        raise reader_http_error(exc)
    return r


# Раньше общего /api/reader/{key}/{action}: FastAPI берёт первый подходящий маршрут.
@app.post("/api/reader/{key}/generate-epub")
def reader_generate_epub(key: str):
    """Собрать EPUB из KFX с Kindle (в reader-sync): название — с полки, а не имя файла из метаданных KFX.
    На X3 такой EPUB сам не уходит — только кнопкой «Отправить на X3»."""
    check_reader_key(key)
    with db_session() as db:
        link = db.get(ReaderLink, key)
        book = db.get(Book, link.book_id) if link and link.book_id else None
        title = ((book.title_ru or "").strip() or book.title) if book else None
        author = book.authors if book else None
    if title and looks_like_filename(title):
        title = None  # своего названия нет — пусть останется то, что в KFX
    try:
        result = reader_service.generate_epub(key, title, author)
    except ReaderSyncError as exc:
        raise reader_http_error(exc)
    refresh_reader_snapshots([key])
    return result


@app.post("/api/reader/{key}/x3")
def reader_send_x3(key: str):
    check_reader_key(key)
    from .delivery import send_x3
    attached = reader_service.store.core.attach_kindle(key)
    if not attached:
        raise HTTPException(status_code=409, detail="EPUB ещё не готов или книга не связана с полкой.")
    try:
        return send_x3(attached["book_id"], attached["sha256"])
    except (Problem, X3Error) as exc:
        raise HTTPException(status_code=getattr(exc, "status", 503), detail=str(exc))


@app.post("/api/reader/{key}/{action}")
def reader_action(key: str, action: str):
    """Пересборка карты и сброс позиции — как в интерфейсе reader-sync."""
    check_reader_key(key)
    if action not in ("rebuild", "reset"):
        raise HTTPException(status_code=404, detail="Unknown action")
    try:
        return reader_service.action(key, action)
    except ReaderSyncError as exc:
        raise reader_http_error(exc)


@app.get("/api/reader/{key}/file/{kind}")
def reader_file(key: str, kind: str):
    check_reader_key(key)
    try:
        return FileResponse(reader_service.file(key, kind))
    except ReaderSyncError as exc:
        raise reader_http_error(exc)


@app.post("/api/x3/check")
def x3_check_now():
    """Кнопка «Проверить X3»: доступность и, если X3 в сети, свежий список его файлов для меток
    на карточках. Файлы не отправляются."""
    if not X3_ENABLED:
        raise HTTPException(status_code=400, detail="X3 не настроен (X3_URL).")
    contact = x3_no_contact()
    if x3_check():
        try:
            contact = x3_contact()[1]
        except X3Error as exc:
            X3_STATE["error"] = str(exc)  # в сети, но список не отдал: метки остаются по прежнему списку
    return {**x3_status(), "contact": x3_report()}


@app.get("/api/x3/device")
def x3_device():
    """Карточка устройства: живой /api/status, если X3 в сети, иначе последний сохранённый."""
    if not X3_ENABLED:
        raise HTTPException(status_code=400, detail="X3 не настроен (X3_URL).")
    online = x3_check()
    saved = x3_device_saved()
    books, contact = None, x3_no_contact()
    if online:
        try:
            files, contact = x3_contact()
            books = {"count": sum(1 for f in files if f["epub"]), "bytes": sum(f["size"] or 0 for f in files)}
        except X3Error:
            pass
    try:
        library = reader_library()
    except ReaderSyncError:
        library = READER_STATE["library"] or {}
    synced = [d.get("received_at") for d in library.get("documents") or [] if d.get("received_at")]
    return {"online": online, "checked_at": iso_utc(X3_STATE["checked_at"]), "error": X3_STATE["error"],
            "status": saved.get("status") or {}, "last_online": saved.get("last_online"),
            "last_sync": max(synced) if synced else None, "books": books, "latest": x3_latest_release(),
            "contact": x3_report()}


@app.get("/api/x3/files")
def x3_file_list():
    """Что лежит на X3 и с какой книгой Kindle связан каждый файл (по MD5 имени — как KOSync у X3)."""
    if not X3_ENABLED:
        raise HTTPException(status_code=400, detail="X3 не настроен (X3_URL).")
    if not x3_check():
        raise HTTPException(status_code=503, detail="X3 не в сети — открой на нём передачу файлов.")
    try:
        files, contact = x3_contact()
    except X3Error:
        raise HTTPException(status_code=502, detail="X3 не отдал список файлов.")
    try:
        library = reader_library()
    except ReaderSyncError:
        library = READER_STATE["library"] or {}
    bound = {d.get("document"): (f"book:{d['library_book_id']}" if d.get("library_book_id") else d.get("book_key")) for d in library.get("documents") or []}
    with db_session() as db:
        uploaded = {i.filename: i.key for i in db.query(X3Upload).filter(X3Upload.status == "sent")}
    return {"dir": X3_BOOKS_DIR, "contact": x3_report(), "files": [
        {**f, "key": bound.get(x3_document_id(f["name"])) or uploaded.get(f["name"])}
        for f in sorted(files, key=lambda f: f["name"].lower())]}


@app.post("/api/books/{book_id}/x3-remove")
def x3_remove_book(book_id: int):
    """Убрать файлы книги с X3. Карточка, её EPUB и место чтения остаются; метка X3 с карточки
    пропадает совсем — отсутствие на X3 теперь ожидаемо."""
    if not X3_ENABLED:
        raise HTTPException(status_code=400, detail="X3 не настроен (X3_URL).")
    with db_session() as db:
        if not db.get(Book, book_id):
            raise HTTPException(status_code=404, detail="Book not found")
    if not x3_check():
        raise HTTPException(status_code=503, detail="X3 не в сети — открой на нём передачу файлов.")
    from . import x3
    try:
        files, contact = x3_contact()
        with db_session() as db:
            owners = x3_file_owners(db, [f["name"] for f in files])
        names = sorted(name for name, books in owners.items() if book_id in books)
        if not names:
            # Убрать просили, а убирать нечего: карточке всё равно нужна «без метки», а не «X3: нет».
            with db_session() as db:
                db.query(Book).filter(Book.id == book_id).update({"x3_removed_at": int(time.time())}, synchronize_session=False)
                db.commit()
            raise HTTPException(status_code=409, detail="Файлов этой книги на X3 нет.")
        # Вместе с файлом X3 стирает и своё место чтения: забираем его наверняка, иначе не удаляем.
        x3_take_positions([f for f in files if f["name"] in names], strict=True)
        x3.delete(x3_request, [x3_path(name) for name in names])
    except X3Error as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except x3.DeviceError as exc:
        raise HTTPException(status_code=502, detail=f"X3 не удалил файл: {str(exc)[:200]}")
    with db_session() as db:
        db.query(Book).filter(Book.id == book_id).update(
            {"x3_removed_at": int(time.time())}, synchronize_session=False)
        db.commit()
    try:
        x3_fetch_listing()
    except X3Error:
        pass  # прежний список не новее удаления и для этой книги уже не считается
    return {"status": "removed", "files": names, "contact": x3_report()}


@app.delete("/api/x3/queue/{key}")
def x3_dequeue(key: str):
    check_reader_key(key)
    with db_session() as db:
        item = db.get(X3Upload, key)
        if item and item.status != "sent":
            db.delete(item)
            db.commit()
    return x3_status()


@app.delete("/api/reader/documents/{document}")
def reader_forget_document(document: str):
    """Убрать запрос X3, который не связали с книгой (например, синк тестового файла)."""
    try:
        result = reader_service.forget(document)
        reader_library()
        return result
    except ReaderSyncError as exc:
        raise reader_http_error(exc)


@app.post("/api/reader/documents/{document}/bind")
def reader_bind_document(document: str, data: DocumentBindSchema):
    """Документ X3 (KOSync document ID) ↔ книга Kindle."""
    try:
        if data.book_id is not None:
            result = reader_service.store.core.bind(document, data.book_id, data.sha256)
        else:
            check_reader_key(data.key)
            result = reader_service.bind(document, data.key)
        reader_library()
        return result
    except ReaderSyncError as exc:
        raise reader_http_error(exc)



# Читалки ходят на адрес полки (books.waifu), myshelf пересылает их запросы в reader-sync как есть.
# Авторизацию проверяет сам reader-sync: KOSync (x-auth-user / x-auth-key) у X3, Bearer у Kindle.

def initialize_storage():
    os.makedirs(BASE_DIR, exist_ok=True)
    os.makedirs(COVERS_DIR, exist_ok=True)
    os.makedirs(INCOMING_DIR, exist_ok=True)
    ensure_sqlite_schema(DB_PATH)
    Base.metadata.create_all(bind=engine)
