#!/usr/bin/env python3
"""Build and query a KFX coordinate map. All offsets are Unicode code points."""
from __future__ import annotations

import argparse
from array import array
import base64
from bisect import bisect_right
import hashlib
import json
import logging
import math
from pathlib import Path
import re
import shlex
import struct
import sys
import unicodedata

VERSION = 1
VENDOR_COMMIT = "5ed7485338c331e6502ac33dba1cb026dd7cc2a2"

# Some Kindle KFX files declare JPEG XR although their images are ordinary JPEG.
# This exact metadata warning does not invalidate native position coordinates.
NON_BLOCKING_WARNINGS = frozenset({
    "Feature/content mismatch: yj_jpegxr_sd=1 has_jpeg_xr_image=False",
})


def integer(value, name, minimum=0, maximum=0xffffffff):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in {minimum}..{maximum}")
    return value


def encode_long(eid, offset):
    integer(eid, "eid", 1)
    integer(offset, "offset")
    return base64.b64encode(struct.pack("<BII", 1, eid, offset)).decode("ascii")


def decode_long(value):
    if not re.fullmatch(r"[A-Za-z0-9+/]{12}", value):
        raise ValueError("LONG must contain 12 base64 characters")
    version, eid, offset = struct.unpack("<BII", base64.b64decode(value, validate=True))
    if version != 1 or eid == 0:
        raise ValueError("Unsupported LONG version or zero eid")
    return eid, offset


class PositionMap:
    def __init__(self, payload):
        if payload.get("version") != VERSION:
            raise ValueError("Unsupported map version")
        self.payload = payload
        self.key = payload["book"]["key"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.key):
            raise ValueError("Invalid book key")
        self.total = integer(payload["total_positions"], "total_positions", 1)
        self.chunks = payload["chunks"]
        cursor = 0
        for c in self.chunks:
            for field in ("pid", "eid", "offset", "length"):
                integer(c[field], field, 1 if field in ("eid", "length") else 0)
            if c["pid"] != cursor or c["offset"] + c["length"] - 1 > 0xffffffff:
                raise ValueError("Map contains a gap, overlap, or offset overflow")
            text = c.get("text")
            if text is not None and (not isinstance(text, str) or len(text) != c["length"]):
                raise ValueError("Text length differs from native coordinate length")
            cursor += c["length"]
        if cursor != self.total:
            raise ValueError("Map total does not match its intervals")
        self.starts = [c["pid"] for c in self.chunks]

    @classmethod
    def read(cls, path):
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def at_pid(self, pid):
        integer(pid, "pid", 0, self.total - 1)
        i = bisect_right(self.starts, pid) - 1
        c = self.chunks[i]
        delta = pid - c["pid"]
        offset = c["offset"] + delta
        long = encode_long(c["eid"], offset)
        value = {
            "book_key": self.key, "pid": pid, "eid": c["eid"], "offset": offset,
            "long": long, "position": f"{long}:{pid}",
            "kfx_percent": round(100 * pid / self.total, 6),
            "section": c.get("section"),
        }
        if c.get("text") is not None:
            value["context_before"] = c["text"][max(0, delta - 70):delta]
            value["context_after"] = c["text"][delta:delta + 160]
        else:
            value["context_before"] = ""
            value["context_after"] = "[non-text position]"
        value["kindle_command"] = "/mnt/us/kindle-sync/bin/kindle-pos set-pid " + shlex.quote(self.key) + " " + str(pid)
        return value

    def at_percent(self, percent):
        if not math.isfinite(percent) or not 0 <= percent <= 100:
            raise ValueError("Percent must be finite and in 0..100")
        return self.at_pid(min(self.total - 1, math.floor(self.total * percent / 100)))

    def at_native(self, position):
        long, sep, raw_pid = position.partition(":")
        if not sep or not raw_pid.isascii() or not raw_pid.isdecimal():
            raise ValueError("Position must be LONG:PID")
        eid, offset = decode_long(long)
        result = self.at_pid(int(raw_pid))
        # A preceding entity's end offset can alias the next entity's start.
        valid = any(c["eid"] == eid and c["offset"] <= offset <= c["offset"] + c["length"]
                    and c["pid"] + offset - c["offset"] == int(raw_pid) for c in self.chunks)
        if not valid:
            raise ValueError("LONG and PID disagree with this book's map")
        result["input_position"] = position
        return result

    def text_index(self):
        if hasattr(self, "_text_index"):
            return self._text_index
        # Keep a native PID for each normalized character, including expansions
        # such as casefold('ß') -> 'ss'. Never use UTF-8 byte offsets.
        chars, pids = [], array("q")
        previous = None
        for c in self.chunks:
            text = c.get("text")
            if text is None:
                previous = None
                continue
            if chars and (previous is None or previous["eid"] != c["eid"]):
                if chars[-1] != " ":
                    chars.append(" ")
                    pids.append(-1)  # paragraph separator has no native position
            normalized, offsets = normalize(text)
            for ch, offset in zip(normalized, offsets):
                if ch == " " and chars and chars[-1] == " ":
                    continue
                chars.append(ch)
                pids.append(c["pid"] + offset)
            previous = c
        self._text_index = ("".join(chars), pids)
        return self._text_index

    def find(self, quote, anchor_offset=None):
        haystack, pids = self.text_index()
        result, seen = [], set()
        for pid, end in find_anchors(haystack, pids, quote, anchor_offset):
            if pid >= self.total:
                continue  # exclusive end of book has no native character
            if pid not in seen:
                item = self.at_pid(pid)
                item["match_end_pid_inclusive"] = end
                result.append(item)
                seen.add(pid)
        return result


TRANSLATE = str.maketrans({"«": '"', "»": '"', "“": '"', "”": '"',
                          "‘": "'", "’": "'", "‐": "-", "‑": "-", "–": "-", "—": "-"})


def normalize(text):
    chars, offsets = [], []
    for index, char in enumerate(text):
        if char in "\u00ad\u200b\ufeff":
            continue
        for ch in unicodedata.normalize("NFKC", char).casefold().translate(TRANSLATE):
            ch = " " if ch.isspace() else ch
            if ch == " " and chars and chars[-1] == " ":
                continue
            chars.append(ch)
            offsets.append(index)
    return "".join(chars), offsets


def find_anchors(haystack, origins, quote, anchor_offset=None):
    """Map a position *inside* a normalized quote back to original coordinates.

    Missing/ambiguous Unicode boundaries are not rounded to a nearby character.
    origins may contain -1 for synthetic paragraph separators.
    """
    needle, q_origins = normalize(quote)
    left = len(needle) - len(needle.lstrip())
    right = len(needle.rstrip())
    needle, q_origins = needle[left:right], q_origins[left:right]
    if not needle:
        raise ValueError("Quote is empty after normalization")
    anchor = 0
    if anchor_offset is not None:
        integer(anchor_offset, "anchor_offset", 0, len(quote))
        if anchor_offset == len(quote) and q_origins[-1] == len(quote) - 1:
            anchor = len(needle)
        else:
            try:
                anchor = q_origins.index(anchor_offset)
            except ValueError:
                raise ValueError("Anchor falls on whitespace or a character removed by normalization") from None
    start = 0
    while True:
        start = haystack.find(needle, start)
        if start < 0:
            return
        end = start + len(needle)
        at = start + anchor
        # An expanded character (e.g. ß -> ss) has no boundary between the s's.
        if not (0 < at < len(origins) and origins[at] >= 0 and origins[at] == origins[at - 1]):
            pid = origins[end - 1] + 1 if anchor == len(needle) else origins[at]
            if pid >= 0 and origins[end - 1] >= 0:
                yield pid, origins[end - 1]
        start += 1



def book_title(payload):
    """Prefer metadata; recover filename-like titles from corroborated opening text."""
    title = payload.get("book", {}).get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    title = title.strip()
    if "_" not in title and not re.search(r"[-_]\w{8,}$", title):
        return title

    # Filename slugs in these KFX files use this transliteration. Only exact
    # matching text is accepted; no reverse transliteration or invented titles.
    letters = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                       ("a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "j",
                        "k", "l", "m", "n", "o", "p", "r", "s", "t", "u", "f",
                        "h", "tc", "ch", "sh", "sch", "", "i", "", "e", "yu", "ya")))

    def slug(value):
        value = "".join(letters.get(c, c) for c in value.casefold())
        return re.sub(r"[^a-z0-9]", "", value)

    filename = re.sub(r"(?:[-_](?:[0-9a-f]{8,}|[0-9]{6}))+\Z", "", title)
    # Most names are author-title-id; keep only the title portion when present.
    author = None
    if "-" in filename and "_" in filename.split("-", 1)[0]:
        author, filename = filename.split("-", 1)
        author = slug(author)
    expected = slug(filename)
    best, best_length = None, 0
    seen = 0
    for chunk in payload.get("chunks", []):
        text = chunk.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        seen += 1
        if seen > 8:
            break
        text = " ".join(text.split())
        author_line, separator, rest = text.partition(". ")
        if separator and author and slug(author_line) == author:
            text = rest
        if not 2 <= len(text) <= 240:
            continue
        candidate = slug(text)
        if not candidate or not expected:
            continue
        matched = 0
        if candidate == expected:
            matched = len(candidate)
        elif min(len(candidate), len(expected)) >= 4 and (candidate in expected or expected in candidate):
            matched = min(len(candidate), len(expected))
        else:
            # Some source filenames were truncated. Accept only a long exact
            # prefix of a title ending at the end of the available filename.
            for length in range(min(len(candidate), len(expected)), 11, -1):
                if expected.endswith(candidate[:length]):
                    matched = length
                    break
        if matched > best_length:
            best, best_length = text, matched
    return best or title


def start_pid(payload):
    """Where the text of the book begins; a book without text begins at its first position."""
    return next((c["pid"] for c in payload.get("chunks", []) if (c.get("text") or "").strip()), 0)


def build(kfx_path):
    sys.path.insert(0, str(Path(__file__).parent / "vendor"))
    from kfxlib import JobLog, YJ_Book, set_logger
    path = Path(kfx_path).resolve()
    if path.suffix.lower() != ".kfx":
        raise ValueError("This builder expects the original .kfx file")
    logger = JobLog(logging.getLogger("kfx"))
    set_logger(logger)
    try:
        book = YJ_Book(str(path))
        book.decode_book(pure=True)
        content = book.collect_content_position_info()
        native = book.collect_position_map_info()
        fields = lambda c: (int(c.pid), int(c.eid), int(c.eid_offset), int(c.length), str(c.section_name))
        # Be strict: never export guessed coordinates from a mismatched layout.
        if [fields(c) for c in content if c.length] != [fields(c) for c in native if c.length]:
            raise ValueError("Content and native position map disagree; unsupported layout")
        blocking_warnings = [w for w in logger.warnings if w not in NON_BLOCKING_WARNINGS]
        if logger.errors or blocking_warnings:
            raise ValueError("KFX parsing reported errors/warnings; map was not exported: " +
                             "; ".join(logger.errors + blocking_warnings))
        chunks = [{"pid": int(c.pid), "eid": int(c.eid), "offset": int(c.eid_offset),
                   "length": int(c.length), "section": str(c.section_name), "text": c.text}
                  for c in content if c.length]
        sources = []
        for datafile in book.container_datafiles:
            raw = datafile.get_data()
            sources.append({"name": Path(datafile.name).name, "size": len(raw),
                            "sha256": hashlib.sha256(raw).hexdigest()})
        return PositionMap({
            "version": VERSION, "offset_unit": "unicode-codepoint", "parser_commit": VENDOR_COMMIT,
            "book": {"key": book.get_metadata_value("ASIN"),
                     "title": book.get_metadata_value("title"), "sources": sources},
            "total_positions": sum(c["length"] for c in chunks), "chunks": chunks,
        })
    finally:
        set_logger()


def kfx_metadata(kfx_path):
    """Authors, language and cover from KFX title metadata; the content is not decoded."""
    sys.path.insert(0, str(Path(__file__).parent / "vendor"))
    from kfxlib import JobLog, YJ_Book, set_logger
    set_logger(JobLog(logging.getLogger("kfx")))
    try:
        meta = YJ_Book(str(Path(kfx_path).resolve())).get_metadata()
        authors = [" ".join(a.split()) for a in (meta.authors or []) if a and a.strip()]
        cover = meta.cover_image_data or None
        return {"authors": authors, "language": meta.language, "publisher": meta.publisher,
                "cover": (cover[0], cover[1]) if cover else None}
    finally:
        set_logger()


def write_map(mapping, json_path, kindle_path):
    for path in (json_path, kindle_path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(json_path).write_text(json.dumps(mapping.payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    rows = [f"KINDLE-POS-MAP\t1\t{mapping.key}\t{mapping.total}"]
    rows += ["\t".join(str(c[f]) for f in ("pid", "eid", "offset", "length")) for c in mapping.chunks]
    Path(kindle_path).write_text("\n".join(rows) + "\n", encoding="ascii")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("build", help="Parse KFX and export JSON + compact Kindle map")
    p.add_argument("kfx")
    p.add_argument("--out", required=True, help="Host JSON map")
    p.add_argument("--kindle-map", required=True, help="Kindle TSV map")
    p = commands.add_parser("resolve", help="Print a native coordinate and a Kindle command")
    p.add_argument("map")
    choices = p.add_mutually_exclusive_group(required=True)
    choices.add_argument("--pid", type=int)
    choices.add_argument("--percent", type=float)
    choices.add_argument("--position", help="Validate and inspect LONG:PID")
    choices.add_argument("--text", help="Quote beginning exactly where reading should resume")
    p.add_argument("--near-percent", type=float, help="Sort text matches by approximate progress; does not silently choose")
    p.add_argument("--occurrence", type=int, help="Explicitly choose match N (1-based, after sorting)")
    p.add_argument("--after", action="store_true", help="Resume immediately after the matched quote")
    p.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            mapping = build(args.kfx)
            write_map(mapping, args.out, args.kindle_map)
            print(f"OK: {len(mapping.chunks)} chunks, {mapping.total} positions; key={mapping.key}")
            print(f"Host: {args.out}\nKindle: {args.kindle_map}")
            return 0
        mapping = PositionMap.read(args.map)
        if args.text is None and (args.near_percent is not None or args.occurrence is not None or args.after):
            raise ValueError("--near-percent, --occurrence and --after require --text")
        if args.text is not None:
            matches = mapping.find(args.text)
            if not matches:
                raise ValueError("Text not found. Try a shorter unchanged excerpt from the same edition")
            if args.near_percent is not None:
                mapping.at_percent(args.near_percent)  # validate input
                matches.sort(key=lambda x: abs(x["kfx_percent"] - args.near_percent))
            if args.occurrence is not None:
                integer(args.occurrence, "occurrence", 1, len(matches))
                matches = [matches[args.occurrence - 1]]
            if args.after:
                matches = [mapping.at_pid(min(mapping.total - 1, x["match_end_pid_inclusive"] + 1)) for x in matches]
        elif args.pid is not None:
            matches = [mapping.at_pid(args.pid)]
        elif args.percent is not None:
            matches = [mapping.at_percent(args.percent)]
        else:
            matches = [mapping.at_native(args.position)]
        ambiguous = len(matches) > 1
        if args.json:
            print(json.dumps({"ambiguous": ambiguous, "matches": matches}, ensure_ascii=False, indent=2))
        else:
            if ambiguous:
                print(f"Found {len(matches)} matches. Use a longer quote or --occurrence N; no match selected.")
            for i, match in enumerate(matches, 1):
                print(f"[{i}] {match['position']}  eid={match['eid']} offset={match['offset']}  KFX={match['kfx_percent']:.4f}%")
                print(match["context_before"] + "▌" + match["context_after"])
                if not ambiguous:
                    print(match["kindle_command"])
        return 2 if ambiguous else 0
    except (ValueError, OSError, KeyError, TypeError, ImportError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
