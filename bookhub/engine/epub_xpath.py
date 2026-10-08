#!/usr/bin/env python3

import argparse
from array import array
from bisect import bisect_left, bisect_right
import hashlib
from html.entities import html5
import json
import logging
from pathlib import Path
import posixpath
import re
import sys
import zipfile
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from lxml import etree

from .kindle_map import find_anchors, integer, normalize


XPATH_RE = re.compile(
    r"^/body/DocFragment\[(\d+)\](?:/body"
    r"(?P<elements>(?:/[A-Za-z_][\w:.-]*\[\d+\])*)"
    r"(?:/text\(\)(?:\[(\d+)\])?\.(\d+))?)?$"
)

SEGMENT_RE = re.compile(r"/([A-Za-z_][\w:.-]*)\[(\d+)\]")


def local_name(tag):
    if not isinstance(tag, str):
        return None
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    if ":" in tag:
        return tag.rsplit(":", 1)[1]
    return tag


XML_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}

# Comments, CDATA and PIs pass through untouched; group 1 is the body of a complete reference.
REFERENCE_RE = re.compile(
    r"<!--.*?-->|<!\[CDATA\[.*?\]\]>|<\?.*?\?>"
    r"|&(?:(#\d+|#[xX][0-9A-Fa-f]+|[A-Za-z_][\w.-]*);)?",
    re.S | re.A,
)

VOID_RE = re.compile(
    rb"<(/?)(area|base|br|col|embed|hr|img|input|link|meta|param|source|track|wbr)(?=[\s/>])"
    rb"((?:[^<>\"']|\"[^\"]*\"|'[^']*')*?)/?>",
    re.I,
)


# An opening tag whose attributes are not all name="value": bare (controls, nowrap) or unquoted (class=a).
START_TAG_RE = re.compile(
    rb"<([A-Za-z_][\w:.-]*)((?:\s+[^\s<>\"'=/]+(?:\s*=\s*(?:\"[^\"<]*\"|'[^'<]*'|[^\s<>\"'`=]+))?)+)\s*(/?)>")
ATTRIBUTE_RE = re.compile(rb"([^\s<>\"'=/]+)(?:\s*=\s*(\"[^\"<]*\"|'[^'<]*'|[^\s<>\"'`=]+))?")


def quoted_attributes(match):
    def attribute(found):
        name, value = found.group(1), found.group(2)
        if value is None:
            value = b'"' + name + b'"'
        elif value[:1] not in b"\"'":
            value = b'"' + value + b'"'
        return b" " + name + b"=" + value
    return b"<" + match.group(1) + b"".join(attribute(a) for a in ATTRIBUTE_RE.finditer(match.group(2))) + match.group(3) + b">"


def plain_reference(match):
    text, name = match.group(0), match.group(1)
    if text[0] != "&" or (name and name in XML_ENTITIES):
        return text
    if name and name[0] == "#":
        # Half of a surrogate pair is not a character: the strict parser refuses it and the
        # recovering one builds text that cannot be read back.
        code = int(name[2:], 16) if name[1] in "xX" else int(name[1:])
        return "&#65533;" if 0xD800 <= code <= 0xDFFF else text
    value = html5.get(name + ";") if name else None
    if value is None:
        return "&amp;" + text[1:]
    return "".join(f"&#{ord(c)};" for c in value)


def numeric_reference(match):
    text, name = match.group(0), match.group(1)
    if text[0] != "&" or name not in XML_ENTITIES:
        return text
    return f"&#{ord(html5[name + ';'])};"


def plain_references(data, replace=plain_reference):
    """Leave the parser no general entity to expand.

    An HTML name (&nbsp;, &mdash;) becomes the character a reader shows; any other name and a
    bare '&' stay the literal text they are on screen. No DTD is loaded or needed, and declared
    entities (XXE, entity bombs) are never referenced.
    """
    if b"&" not in data:
        return data
    if data[:2] in (b"\xff\xfe", b"\xfe\xff") and data[2:4] != b"\x00\x00":
        codec = "utf-16"
    elif b"\x00" in data[:4]:
        return data  # UTF-32 or UTF-16 without a BOM: left to the parser's own rules
    else:
        codec = "latin-1"  # keeps every byte of an ASCII-compatible encoding
    try:
        text = data.decode(codec)
    except UnicodeError:
        return data
    return REFERENCE_RE.sub(replace, text).encode(codec)


def xml_parser(recover=False):
    return etree.XMLParser(
        recover=recover,
        resolve_entities=False,
        no_network=True,
        remove_comments=False,
        remove_pis=False,
        strip_cdata=True,
    )


def parse_xml(data, name="XML"):
    """Strict first. Markup a reader would still open is recovered and logged, not refused."""
    data = plain_references(data)
    try:
        return etree.fromstring(data, xml_parser())
    except etree.XMLSyntaxError as exc:
        logging.warning("%s is not well-formed, recovering: %s", name, exc)
    # After its first error libxml2 stops expanding &amp; &lt; &gt; &quot; &apos; in text and the
    # characters vanish; numeric references survive.
    data = plain_references(data, numeric_reference)
    # A bare or unquoted attribute makes libxml2 drop the element and close <body> early.
    data = START_TAG_RE.sub(quoted_attributes, data)
    # An HTML void tag left open would swallow the rest of the chapter as its children.
    data = VOID_RE.sub(lambda m: b"" if m.group(1) else b"<" + m.group(2) + m.group(3) + b"/>", data)
    root = etree.fromstring(data, xml_parser(recover=True))
    if root is None:
        raise ValueError(f"{name} is not XML")
    return root


def first_by_local(root, name):
    for el in root.iter():
        if local_name(el.tag) == name:
            return el
    return None


def children_by_name(parent, name):
    return [
        c
        for c in parent
        if isinstance(c.tag, str) and local_name(c.tag) == name
    ]


@dataclass
class SpineItem:
    index: int
    idref: str
    href: str
    zip_path: str
    media_type: str = ""


def load_spine(zf):
    try:
        container = parse_xml(zf.read("META-INF/container.xml"), "META-INF/container.xml")
    except KeyError:
        raise ValueError("META-INF/container.xml not found") from None

    rootfile = None
    for el in container.iter():
        if local_name(el.tag) == "rootfile":
            rootfile = el
            break

    if rootfile is None:
        raise RuntimeError("META-INF/container.xml: rootfile not found")

    opf_path = rootfile.get("full-path")
    if not opf_path:
        raise RuntimeError("rootfile has no full-path")

    try:
        opf = parse_xml(zf.read(opf_path), opf_path)
    except KeyError:
        raise ValueError(f"OPF {opf_path} not found") from None
    opf_dir = posixpath.dirname(opf_path)

    manifest = {}
    for el in opf.iter():
        if local_name(el.tag) == "item":
            item_id = el.get("id")
            href = el.get("href")
            if item_id and href:
                manifest[item_id] = href, el.get("media-type") or ""

    spine_ids = []
    for el in opf.iter():
        if local_name(el.tag) == "itemref":
            idref = el.get("idref")
            if idref:
                spine_ids.append(idref)

    result = []

    for idref in spine_ids:
        if idref not in manifest:
            # Readers skip such an itemref; it gets no DocFragment number.
            logging.warning("EPUB spine idref %r absent from manifest, skipped", idref)
            continue
        href, media_type = manifest[idref]

        # fragment/query не относятся к имени файла внутри ZIP
        href_file = unquote(urlsplit(href).path)

        zip_path = posixpath.normpath(
            posixpath.join(opf_dir, href_file)
        )

        result.append(
            SpineItem(
                index=len(result),
                idref=idref,
                href=href,
                zip_path=zip_path,
                media_type=media_type,
            )
        )

    return result


def spine_body(zf, item):
    """<body> of a spine item, or None for a page without text: SVG, image, missing file."""
    if item.media_type.startswith("image/"):
        return None
    try:
        data = zf.read(item.zip_path)
    except KeyError:
        logging.warning("EPUB spine item %s is missing from the archive", item.zip_path)
        return None
    try:
        root = parse_xml(data, item.zip_path)
    except (ValueError, etree.XMLSyntaxError) as exc:
        logging.warning("EPUB spine item %s has no readable markup: %s", item.zip_path, exc)
        return None
    return first_by_local(root, "body")


FONT_OBFUSCATION = {"http://www.idpf.org/2008/embedding", "http://ns.adobe.com/pdf/enc#RC"}


def protected(zf):
    """DRM is refused, never worked around. Obfuscated fonts are not DRM: readers open such books."""
    if any(info.flag_bits & 1 for info in zf.infolist()):
        return True
    try:
        data = zf.read("META-INF/encryption.xml")
    except KeyError:
        return False
    return any(
        local_name(el.tag) == "EncryptionMethod" and el.get("Algorithm") not in FONT_OBFUSCATION
        for el in parse_xml(data, "META-INF/encryption.xml").iter()
    )


def validate_epub(path):
    """The single intake check: every EPUB passes it before any state points at the file."""
    with zipfile.ZipFile(path) as zf:
        items = zf.infolist()
        if len(items) > 10000 or sum(i.file_size for i in items) > 512 * 1024 * 1024:
            raise ValueError("EPUB exceeds expanded size/file count limit")
        if protected(zf):
            raise ValueError("EPUB is encrypted (DRM)")
    return EpubIndex(path)


def resolve_element(body, element_path):
    current = body

    for tag, idx_text in SEGMENT_RE.findall(element_path):
        idx = int(idx_text)

        matches = children_by_name(current, tag)

        if idx < 1 or idx > len(matches):
            raise RuntimeError(
                f"cannot resolve {tag}[{idx}] under "
                f"<{local_name(current.tag)}>; "
                f"found {len(matches)} matching children"
            )

        current = matches[idx - 1]

    return current


def direct_text_nodes(element):
    """
    Equivalent to XPath ./text() for the cases CrossPoint emits.

    lxml smart strings preserve whether the node is element.text
    or a child's tail.
    """
    return element.xpath("./text()")


def nearest_paragraph(element):
    cur = element
    while cur is not None:
        if local_name(cur.tag) in ("p", "li"):
            return cur
        cur = cur.getparent()
    return element


def text_node_identity(node):
    """
    Return (owner, kind), where:
      kind=text -> owner.text
      kind=tail -> owner.tail
    """
    if getattr(node, "is_text", False):
        return node.getparent(), "text"

    if getattr(node, "is_tail", False):
        return node.getparent(), "tail"

    return None, None


def collect_text_chunks(element):
    """
    Flatten element text in document order while remembering which XML
    text/tail field each chunk came from.
    """
    chunks = []

    def walk(el):
        if el.text:
            chunks.append((el, "text", el.text))

        for child in el:
            # Comments/PIs have non-string tags. Their own payload isn't
            # visible book text, but their tail may be.
            if isinstance(child.tag, str):
                walk(child)

            if child.tail:
                chunks.append((child, "tail", child.tail))

    walk(element)
    return chunks


def normalize_display(s):
    # Для человеческого вывода, не для расчёта offset.
    return re.sub(r"\s+", " ", s).strip()


BLOCKS = {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "section",
          "article", "blockquote", "tr", "br", "hr"}
NON_TEXT = {"script", "style", "metadata"}
# CrossPoint lays out and counts no text inside these (VisibleTextUtils::isNonVisibleElement).
UNCOUNTED = {"head", "style", "script", "title", "rp"}


class EpubIndex:
    """Original XML text-node coordinates; never serialize or rewrite the EPUB.

    Synthetic block separators aid quote search but cannot be used as anchors.
    This is a DOM index, not a CSS layout engine.
    """
    def __init__(self, path):
        self.path = Path(path)
        self.sha256 = hashlib.sha256(self.path.read_bytes()).hexdigest()
        # elements: where each element starts in self.text, including those without any text.
        self.runs, self.by_xpath, self.elements = [], {}, {}
        pieces, length = [], 0

        def separator():
            nonlocal length
            pieces.append("\n")
            length += 1

        def walk(el, xpath, item):
            nonlocal length
            name = local_name(el.tag)
            self.elements[xpath] = length
            if name in NON_TEXT:
                return
            if name in BLOCKS:
                separator()
            identities = {text_node_identity(n): i for i, n in enumerate(direct_text_nodes(el), 1)}

            def emit(owner, kind, text):
                nonlocal length
                if not text:
                    return
                index = identities[(owner, kind)]
                run = dict(start=length, text=text, xpath=f"{xpath}/text()[{index}]",
                           spine_index=item.index, spine_file=item.zip_path)
                self.runs.append(run)
                self.by_xpath[run["xpath"]] = run
                pieces.append(text)
                length += len(text)

            emit(el, "text", el.text)
            counts = {}
            for child in el:
                tag = local_name(child.tag)
                if tag:
                    counts[tag] = counts.get(tag, 0) + 1
                    walk(child, f"{xpath}/{tag}[{counts[tag]}]", item)
                emit(child, "tail", child.tail)
            if name in BLOCKS:
                separator()

        with zipfile.ZipFile(path) as zf:
            self.spine = load_spine(zf)
            for item in self.spine:
                xpath = f"/body/DocFragment[{item.index + 1}]/body"
                body = spine_body(zf, item)
                if body is None:
                    self.elements[xpath] = length  # still owns its DocFragment number
                else:
                    walk(body, xpath, item)
                separator()
        self.text = "".join(pieces)
        self.starts = [r["start"] for r in self.runs]
        self.normalized, offsets = normalize(self.text)
        self.origins = array("q", offsets)

    def at_raw(self, position, before=120, after=300):
        index = bisect_right(self.starts, position) - 1
        if index < 0 or position > self.runs[index]["start"] + len(self.runs[index]["text"]):
            raise ValueError("Position has no XML text node (block separator/end of book)")
        run = self.runs[index]
        offset = position - run["start"]
        return self._result(run, offset, before, after)

    def _result(self, run, offset, before=120, after=300):
        position = run["start"] + offset
        return dict(xpath=f"{run['xpath']}.{offset}", spine_index=run["spine_index"],
                    spine_file=run["spine_file"], char_offset=offset, raw_index=position,
                    context_before=self.text[max(0, position-before):position],
                    context_after=self.text[position:position+after],
                    text_node=run["text"], epub_sha256=self.sha256)

    def visible(self, position):
        """Raw index of the first non-whitespace character at or after position, else of the last
        one before it; None in a book without text. Separators, indentation and empty elements
        cannot anchor a quote."""
        at = bisect_left(self.origins, position)
        i = at
        while i < len(self.normalized) and self.normalized[i] == " ":
            i += 1
        if i == len(self.normalized):
            i = at - 1
            while i >= 0 and self.normalized[i] == " ":
                i -= 1
        return self.origins[i] if i >= 0 else None

    def resolve(self, xpath, before=120, after=300):
        # KOReader appends an offset to an element step as well (img.0, p[3].0); it has no meaning there.
        head, slash, last = xpath.rpartition("/")
        if not last.startswith("text()"):
            xpath = head + slash + re.sub(r"\.\d+$", "", last)
        xpath = re.sub(r'/(?!body(?:/|$))([A-Za-z_][\w:.-]*)(?=/|$)', r'/\1[1]', xpath)
        match = XPATH_RE.fullmatch(xpath)
        if not match:
            raise ValueError(f"Unsupported CrossPoint XPath: {xpath}")
        fragment = int(match.group(1))
        if not 1 <= fragment <= len(self.spine):
            raise ValueError("DocFragment outside EPUB spine")
        base = f"/body/DocFragment[{fragment}]/body" + (match.group("elements") or "")
        if match.group(4) is None:
            start = self.elements.get(base)
            if start is None:
                raise ValueError("XPath element not found")
            position = self.visible(start)
            if position is None:
                raise ValueError("EPUB has no text")
            # The text (xpath, text_index) is the first visible character at or after the element. The
            # ordinal (spine_index, raw_index) stays at the element itself, in reading order with text positions.
            result = self.at_raw(position, before, after)
            result.update(spine_index=fragment - 1, spine_file=self.spine[fragment - 1].zip_path,
                          raw_index=start, text_index=position)
            return result
        base += f"/text()[{int(match.group(3) or 1)}]"
        run = self.by_xpath.get(base)
        if run is None:
            raise ValueError("XPath text node not found")
        offset = int(match.group(4))
        if offset > len(run["text"]):
            raise ValueError("Offset outside text node")
        return self._result(run, offset, before, after)

    def xpath_for_spine(self, spine_index, visible_offset=None, fraction=None):
        """A CrossPoint position, as its progress.bin holds it, as an XPath and a share of the book.

        visible_offset is the firmware's "visible text offset" of the open page: the code points of
        all character data in <body> before the page's first word, text inside UNCOUNTED excluded
        (ChapterHtmlSlimParser::characterData fills the section's page table with it). The firmware
        turns an XPath it receives back into the same count (ProgressMapper.cpp, ParagraphStreamer),
        so the XPath returned here is the page the reader has open. The XPath the firmware sends
        over KOSync is not: ChapterXPathResolver.cpp counts only text inside <p> and <li>, and
        lands later in the chapter by all the text outside them.
        Without an offset, fraction of that text; with neither, the start of the chapter.
        spine_index == len(spine) is a finished book: its last visible position.
        Known differences from the firmware's own count, all on rare markup, each putting the result a
        little ahead of the open page: text inside <svg><metadata>, elements the firmware knows by a
        prefixed name (svg:title), HTML5 entity names outside its table, entities declared in a DTD.
        """
        integer(spine_index, "spine_index", 0, len(self.spine))
        if not any(ch.isalnum() for ch in self.text):
            raise ValueError("EPUB has no text")
        if spine_index == len(self.spine):
            return dict(xpath=self.at_raw(self.visible(len(self.text)))["xpath"], percentage=1.0)
        key = lambda run: run["spine_index"]
        chapter = self.runs[bisect_left(self.runs, spine_index, key=key):bisect_right(self.runs, spine_index, key=key)]
        counted = [run for run in chapter
                   if UNCOUNTED.isdisjoint(name.lower() for name, _ in SEGMENT_RE.findall(run["xpath"]))]
        if visible_offset is None:
            visible_offset = int(min(1, max(0, fraction or 0)) * sum(len(run["text"]) for run in counted))
        left = integer(visible_offset, "visible_offset")
        xpath = f"/body/DocFragment[{spine_index + 1}]/body"
        # Offset 0 is the page that opens the chapter, whatever whitespace precedes its first word.
        if left and counted:
            for run in counted:
                if left < len(run["text"]):
                    break
                left -= len(run["text"])
            else:
                left = len(run["text"])  # past the text: the last page holds a picture only, or the file differs
            raw, start, end = run["start"] + left, chapter[0]["start"], chapter[-1]["start"] + len(chapter[-1]["text"])
            # A page that opens with a picture starts in the whitespace around it. Every reader can
            # resolve a word: the one after the picture, else the last one of the chapter.
            ahead = self.visible(raw)
            raw = ahead if raw <= ahead < end else start + len(self.text[start:raw].rstrip()) - 1
            if raw >= start:
                xpath = self.at_raw(raw)["xpath"]
        return dict(xpath=xpath, percentage=self.resolve(xpath)["raw_index"] / len(self.text))

    def find(self, quote, anchor_offset=None, fragment=None):
        if fragment is not None and not 1 <= fragment <= len(self.spine):
            raise ValueError("DocFragment outside EPUB spine")
        matches, seen = [], set()
        for position, end in find_anchors(self.normalized, self.origins, quote, anchor_offset):
            try:
                item = self.at_raw(position)
            except ValueError:
                continue
            if item["xpath"] in seen or (fragment is not None and item["spine_index"] != fragment - 1):
                continue
            item["match_end_raw_inclusive"] = end
            matches.append(item)
            seen.add(item["xpath"])
        return matches


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("epub")
    ap.add_argument("xpath", nargs="?")
    ap.add_argument("--text", help="Find a CrossPoint XPath by quote")
    ap.add_argument("--anchor-offset", type=int, help="Code-point offset INSIDE the input quote")
    ap.add_argument("--fragment", type=int, help="Restrict reverse search to DocFragment N")
    ap.add_argument("--occurrence", type=int, help="Choose match N, 1-based")
    ap.add_argument("--json", action="store_true")
    ap.add_argument(
        "--before",
        type=int,
        default=120,
        help="characters before anchor",
    )
    ap.add_argument(
        "--after",
        type=int,
        default=300,
        help="characters after anchor",
    )
    args = ap.parse_args()

    if (args.xpath is None) == (args.text is None):
        ap.error("Provide either xpath or --text")
    if args.before < 0 or args.after < 0:
        ap.error("Context sizes must be nonnegative")
    if args.xpath and (args.anchor_offset is not None or args.fragment is not None or args.occurrence is not None):
        ap.error("Search options require --text")
    if args.text is not None or args.json:
        try:
            index = EpubIndex(args.epub)
            matches = index.find(args.text, args.anchor_offset, args.fragment) if args.text is not None else [index.resolve(args.xpath, args.before, args.after)]
            if not matches:
                raise ValueError("Text not found")
            if args.occurrence is not None:
                if not 1 <= args.occurrence <= len(matches):
                    raise ValueError("Occurrence outside match list")
                matches = [matches[args.occurrence - 1]]
            for result in matches:
                context = index.resolve(result["xpath"], args.before, args.after)
                result["context_before"] = context["context_before"]
                result["context_after"] = context["context_after"]
            if args.json:
                print(json.dumps(dict(ambiguous=len(matches)>1, matches=matches), ensure_ascii=False, indent=2))
            else:
                for i, result in enumerate(matches, 1):
                    print(f"[{i}] {result['xpath']}")
                    print(normalize_display(result["context_before"] + "▌" + result["context_after"]))
                if len(matches) > 1:
                    print("Multiple matches: use a longer quote, --fragment or --occurrence")
            return 2 if len(matches) > 1 else 0
        except (ValueError, OSError, RuntimeError, zipfile.BadZipFile, etree.XMLSyntaxError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    m = XPATH_RE.match(args.xpath)
    if not m:
        raise SystemExit(f"unsupported CrossPoint XPath: {args.xpath}")

    doc_fragment = int(m.group(1))
    element_path = m.group("elements") or ""
    text_index = int(m.group(3) or (1 if m.group(4) is not None else 0))
    char_offset = int(m.group(4) or 0)

    # CrossPoint:
    # DocFragment[1] = spineIndex 0
    spine_index = doc_fragment - 1

    with zipfile.ZipFile(args.epub) as zf:
        spine = load_spine(zf)

        if spine_index < 0 or spine_index >= len(spine):
            raise SystemExit(
                f"DocFragment[{doc_fragment}] is outside EPUB spine "
                f"(spine count={len(spine)})"
            )

        item = spine[spine_index]
        raw = zf.read(item.zip_path)
        root = parse_xml(raw)

    body = first_by_local(root, "body")
    if body is None:
        raise SystemExit(f"{item.zip_path}: <body> not found")

    target = resolve_element(body, element_path)

    print(f"EPUB:          {args.epub}")
    print(f"XPath:         {args.xpath}")
    print(f"spineIndex:    {spine_index}")
    print(f"spine file:    {item.zip_path}")
    print(f"target tag:    <{local_name(target.tag)}>")

    if text_index == 0:
        text = "".join(target.itertext())
        print()
        print(normalize_display(text))
        return

    nodes = direct_text_nodes(target)

    print(f"text nodes:    {len(nodes)}")
    print(f"text()[N]:     {text_index}")
    print(f"char offset:   {char_offset}")

    if text_index < 1 or text_index > len(nodes):
        raise SystemExit(
            f"text()[{text_index}] not found; "
            f"target has {len(nodes)} direct text nodes"
        )

    selected = nodes[text_index - 1]
    selected_text = str(selected)

    if char_offset < 0 or char_offset > len(selected_text):
        raise SystemExit(
            f"offset {char_offset} outside text node "
            f"(length={len(selected_text)})"
        )

    print()
    print("DIRECT TEXT NODE:")
    print(
        selected_text[:char_offset]
        + "▌"
        + selected_text[char_offset:]
    )

    # Найдём ближайший <p>/<li> и поставим marker уже в полном его тексте.
    paragraph = nearest_paragraph(target)
    chunks = collect_text_chunks(paragraph)

    selected_owner, selected_kind = text_node_identity(selected)

    flat = ""
    anchor = None

    for owner, kind, text in chunks:
        if (
            anchor is None
            and owner is selected_owner
            and kind == selected_kind
        ):
            anchor = len(flat) + char_offset

        flat += text

    if anchor is not None:
        lo = max(0, anchor - args.before)
        hi = min(len(flat), anchor + args.after)

        context = (
            flat[lo:anchor]
            + "▌"
            + flat[anchor:hi]
        )

        print()
        print("CONTEXT:")
        print(normalize_display(context))

        print()
        print("FROM ANCHOR:")
        print(normalize_display(flat[anchor:anchor + args.after]))
    else:
        print()
        print("WARNING: could not map direct text node into paragraph context")


if __name__ == "__main__":
    raise SystemExit(main())
