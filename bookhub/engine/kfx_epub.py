"""KFX с Kindle → EPUB через kfxlib, с названием и автором, которые знает полка.

Kindle в метаданных личных документов часто хранит вместо названия имя файла
(«endi_uir-proekt_ave_mariya-66216d3327f0a»), и kfxlib переносит его в EPUB как есть.
Текст и разметку kfxlib собирает сам; позиции потом сопоставляются по тексту, как для
любого EPUB. Книги с DRM не собираются — и обходить защиту здесь никто не будет.
"""
import html
import io
import logging
import re
import sys
import zipfile
from pathlib import Path

VENDOR = Path(__file__).parent / "vendor"


def looks_like_filename(value):
    value = (value or "").strip()
    return not value or (" " not in value and "_" in value)


def convert(kfx_path):
    if str(VENDOR) not in sys.path:
        sys.path.insert(0, str(VENDOR))
    from kfxlib import JobLog, YJ_Book, set_logger
    logger = JobLog(logging.getLogger("kfx"))
    set_logger(logger)
    try:
        data = YJ_Book(str(kfx_path)).convert_to_epub()
    except Exception as exc:
        if "drm" in str(exc).lower():
            raise ValueError("Книга защищена DRM — EPUB из неё не собрать") from exc
        raise
    if logger.errors:
        raise ValueError("KFX не собрался в EPUB: " + "; ".join(logger.errors)[:500])
    return data


def _sub_first(pattern, xml, value, only_if=None):
    """Заменить текст в первой группе-обёртке; only_if — заменять, только если там было это."""
    def repl(m):
        if only_if is not None and html.unescape(m.group(2)).strip() != only_if:
            return m.group(0)
        return m.group(1) + html.escape(value, quote=False) + m.group(3)
    return re.sub(pattern, repl, xml, count=1, flags=re.S)


def set_metadata(epub, title=None, author=None):
    """Название — в OPF и там, где kfxlib повторил старое (NCX, nav); автор — только вместо пустого
    или имени файла: у Kindle он обычно верный и по-русски, а у полки бывает из англоязычного каталога."""
    if not title and not author:
        return epub
    src = zipfile.ZipFile(io.BytesIO(epub))
    opf_path = re.search(r'full-path="([^"]+)"', src.read("META-INF/container.xml").decode("utf-8")).group(1)
    opf = src.read(opf_path).decode("utf-8")
    old = re.search(r"<dc:title[^>]*>(.*?)</dc:title>", opf, re.S)
    old_title = html.unescape(old.group(1)).strip() if old else None
    if title:
        opf = _sub_first(r"(<dc:title[^>]*>)(.*?)(</dc:title>)", opf, title)
    creator = re.search(r"<dc:creator[^>]*>(.*?)</dc:creator>", opf, re.S)
    if author and (not creator or looks_like_filename(html.unescape(creator.group(1)))):
        if creator:
            opf = _sub_first(r"(<dc:creator[^>]*>)(.*?)(</dc:creator>)", opf, author)
        else:
            opf = opf.replace("</metadata>", f"<dc:creator>{html.escape(author, quote=False)}</dc:creator></metadata>", 1)

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == opf_path:
                data = opf.encode("utf-8")
            elif title and old_title and info.filename.endswith(".ncx"):
                data = _sub_first(r"(<docTitle>\s*<text>)(.*?)(</text>)", data.decode("utf-8"), title,
                                  only_if=old_title).encode("utf-8")
            elif title and old_title and info.filename.endswith(".xhtml"):
                text = data.decode("utf-8")
                if old_title in text:
                    data = _sub_first(r"(<title>)(.*?)(</title>)", text, title, only_if=old_title).encode("utf-8")
            # Тот же порядок и сжатие: mimetype остаётся первым и несжатым, как требует EPUB.
            dst.writestr(info, data)
    return out.getvalue()


def build_epub(kfx_path, title=None, author=None):
    return set_metadata(convert(kfx_path), title, author)
