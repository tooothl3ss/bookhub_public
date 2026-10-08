#!/usr/bin/env python3
"""Translate an original EPUB XPath <-> KFX PID using anchored text.

Books without any text (manga, comics: a page image per spine item) are mapped page by page
instead — only when neither side has text and the page counts match exactly.
Returns mapped / ambiguous / unresolved. No device state is written.
"""
import argparse
from bisect import bisect_right
import json
from pathlib import Path
import re
import sys

from .epub_xpath import EpubIndex
from .kindle_map import PositionMap, normalize

ALNUM = re.compile(r"[^\W_]")


class PositionBridge:
    def __init__(self, epub, kfx_map):
        self.epub = epub if isinstance(epub, EpubIndex) else EpubIndex(epub)
        self.kfx = kfx_map if isinstance(kfx_map, PositionMap) else PositionMap.read(kfx_map)
        pieces, size, previous = [], 0, None
        self.kfx_raw_starts = {}
        for chunk in self.kfx.chunks:
            if previous is None or previous != chunk["eid"]:
                pieces.append("\n")
                size += 1
            if chunk.get("text") is not None:
                self.kfx_raw_starts[chunk["pid"]] = size
                pieces.append(chunk["text"])
                size += len(chunk["text"])
                previous = chunk["eid"]
            else:
                previous = None
        self.kfx_text = "".join(pieces)
        self.pages = self._pages()

    def _pages(self):
        """Page mode for textless books: KFX section k <-> EPUB spine item k. Structural identity,
        not a percentage guess — so both sides must be textless and have the same page count."""
        self.textless = (not any(c.get("text") for c in self.kfx.chunks),
                         not any(ch.isalnum() for ch in self.epub.text))  # Kindle, EPUB
        if not all(self.textless):
            return None
        sections = list(dict.fromkeys(c.get("section") for c in self.kfx.chunks))
        if not sections or None in sections or len(sections) != len(self.epub.spine):
            return None
        first = {}
        for c in self.kfx.chunks:
            first.setdefault(c["section"], c["pid"])
        return dict(index={name: i for i, name in enumerate(sections)}, first_pid=[first[name] for name in sections])

    def _page_epub(self, pid):
        chunk = self.kfx.chunks[bisect_right(self.kfx.starts, pid)-1]
        page = self.pages["index"][chunk["section"]]
        xpath = f"/body/DocFragment[{page+1}]/body"
        return dict(status="mapped", method="page", roundtrip_verified=True, source=self.kfx.at_pid(pid),
                    matches=[dict(xpath=xpath, page=page, pages=len(self.pages["first_pid"]),
                                  spine_index=page, spine_file=self.epub.spine[page].href)])

    def _page_kindle(self, xpath):
        match = re.match(r"^/body/DocFragment\[(\d+)\]", xpath or "")
        page = int(match.group(1)) - 1 if match else -1
        if not 0 <= page < len(self.pages["first_pid"]):
            return dict(status="unresolved", matches=[], method="page", reason="DocFragment outside the page list")
        pid = self.pages["first_pid"][page]
        return dict(status="mapped", method="page", roundtrip_verified=True, source=dict(xpath=xpath, page=page),
                    matches=[dict(self.kfx.at_pid(pid), page=page)])

    def _match(self, text, offset, find):
        evidence = None
        for before, after in ((80, 160), (40, 80), (12, 40)):
            lo, hi = max(0, offset-before), min(len(text), offset+after)
            quote, anchor = text[lo:hi], offset-lo
            normalized, _ = normalize(quote)
            if sum(c.isalnum() for c in normalized) < 16:
                continue
            try:
                matches = find(quote, anchor_offset=anchor)
            except ValueError:
                continue
            evidence = dict(quote=quote, anchor_offset=anchor, method="normalized_text")
            if matches:
                return dict(status="mapped" if len(matches) == 1 else "ambiguous",
                            matches=matches, anchor=evidence)
        return dict(status="unresolved", matches=[], anchor=evidence,
                    reason="No unique representable text anchor; do not apply a percentage fallback")

    def to_epub(self, pid, verify=True):
        if self.pages:
            return self._provenance(self._page_epub(pid))
        source = self.kfx.at_pid(pid)
        chunk = self.kfx.chunks[bisect_right(self.kfx.starts, pid)-1]
        if chunk.get("text") is None:
            result = dict(status="unresolved", matches=[], reason="PID refers to non-text content")
        else:
            offset = self.kfx_raw_starts[chunk["pid"]] + pid - chunk["pid"]
            result = self._match(self.kfx_text, offset, self.epub.find)
        result["source"] = source
        if verify and result["status"] == "mapped":
            reverse = self.to_kindle(result["matches"][0]["xpath"], verify=False)
            if reverse["status"] != "mapped" or reverse["matches"][0]["pid"] != pid:
                result.update(status="unresolved", reason="Reverse mapping did not return the same PID")
            else:
                result["roundtrip_verified"] = True
        return self._provenance(result)

    def to_kindle(self, xpath, verify=True):
        if self.pages:
            return self._provenance(self._page_kindle(xpath))
        source = self.epub.resolve(xpath)
        # An element is quoted from its first visible character. A text offset is tried as it is;
        # on indentation or at the end of a text node it may be unable to anchor a quote, and the
        # same reading position is then the first visible character after it.
        exact = source.get("text_index", source["raw_index"])
        if "text_index" in source and abs(self.epub.at_raw(exact)["spine_index"] - source["spine_index"]) > 1:
            # The nearest text is neither in this spine item nor in a neighbouring one: any number of
            # picture pages lie between. The EPUB position stands; the Kindle is not moved.
            return self._provenance(dict(status="unresolved", reason="No text next to this page",
                                         matches=[], source=source))
        for anchor in dict.fromkeys((exact, self.epub.visible(exact))):
            if anchor is None:
                continue
            result = self._match(self.epub.text, anchor, self.kfx.find)
            if verify and result["status"] == "mapped":
                reverse = self.to_epub(result["matches"][0]["pid"], verify=False)
                if reverse["status"] != "mapped" or reverse["matches"][0]["raw_index"] != anchor:
                    result.update(status="unresolved", reason="Reverse mapping did not return the same EPUB character")
                else:
                    result["roundtrip_verified"] = True
            if result["status"] != "unresolved":
                break
        result["source"] = source
        return self._provenance(result)

    def agreement(self, count=32):
        """How much of the Kindle text this EPUB holds: (mapped, tried).

        Positions spread evenly over the whole Kindle text are translated the way a reading
        position is. The same text maps nearly all of them; another book or another translation,
        next to none. A position with too little text around it to quote is not tried.
        """
        starts, pids = list(self.kfx_raw_starts.values()), list(self.kfx_raw_starts)
        sample = {}
        for i in range(count):
            # A letter or digit: whitespace and paragraph breaks cannot anchor a quote.
            at = ALNUM.search(self.kfx_text, len(self.kfx_text) * (2*i + 1) // (2*count))
            if at:
                chunk = bisect_right(starts, at.start()) - 1
                sample[pids[chunk] + at.start() - starts[chunk]] = None
        tried = [result for result in map(self.to_epub, sample) if result.get("anchor")]
        return sum(result["status"] == "mapped" for result in tried), len(tried)

    def _provenance(self, result):
        result["epub_sha256"] = self.epub.sha256
        result["kfx_book_key"] = self.kfx.key
        result["kfx_sources"] = self.kfx.payload["book"].get("sources", [])
        # An unresolved candidate is evidence, never an executable sync action.
        if result["status"] != "mapped":
            for match in result["matches"]:
                match.pop("kindle_command", None)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("epub")
    parser.add_argument("map")
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--pid", type=int, help="KFX -> EPUB")
    choice.add_argument("--xpath", help="EPUB -> KFX")
    args = parser.parse_args(argv)
    try:
        bridge = PositionBridge(args.epub, args.map)
        result = bridge.to_epub(args.pid) if args.pid is not None else bridge.to_kindle(args.xpath)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return {"mapped": 0, "ambiguous": 2, "unresolved": 3}[result["status"]]
    except (ValueError, OSError, RuntimeError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
