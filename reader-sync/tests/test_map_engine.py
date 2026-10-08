import copy
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from app.engine.kindle_map import PositionMap, build, decode_long, encode_long, normalize, write_map
from lua_runner import quote

KEY = "XNZDUFSVLCHEUKLM374WPKP7762ZJ6J4"


def lua(code, env=None):
    prefix = "local m=assert(loadfile(" + quote(str(ROOT.parent / "kindle-client/bin/kindle-pos")) + "))('kindle_pos_module');\n"
    return subprocess.run([sys.executable, str(ROOT / "tests/lua_runner.py"), "-e", prefix + code],
                          env=env, capture_output=True, text=True)


def sample():
    texts = ["А😀 Б\u00a0В", "Straße…\u00ad end", "А😀 Б\u00a0В"]
    chunks, pid = [], 0
    for eid, text in enumerate(texts, 100):
        chunks.append(dict(pid=pid, eid=eid, offset=0, length=len(text), text=text))
        pid += len(text)
    return PositionMap(dict(version=1, book=dict(key="TEST"), total_positions=pid, chunks=chunks))


class BuildDiagnosticsTests(unittest.TestCase):
    warning = "Feature/content mismatch: yj_jpegxr_sd=1 has_jpeg_xr_image=False"

    def build_with_diagnostics(self, warnings=(), errors=(), mismatch=False, invalid_text=False):
        sys.path.insert(0, str(ROOT.parent / "bookhub/engine/vendor"))
        from kfxlib.message_logging import log

        chunk = SimpleNamespace(pid=0, eid=1, eid_offset=0, length=3,
                                section_name="section", text="abc")
        native = copy.copy(chunk)
        if mismatch:
            native.eid_offset = 1
        if invalid_text:
            chunk.text = "ab"
        book = Mock()
        book.collect_content_position_info.return_value = [chunk]
        book.collect_position_map_info.return_value = [native]
        book.container_datafiles = []
        book.get_metadata_value.side_effect = lambda name: {"ASIN": "TEST", "title": "Test"}[name]

        def decode(*, pure):
            self.assertTrue(pure)
            for message in warnings:
                log.warning(message)
            for message in errors:
                log.error(message)

        book.decode_book.side_effect = decode
        with patch("kfxlib.YJ_Book", return_value=book):
            return build("test.kfx")

    def test_jpegxr_metadata_warning_allows_map_and_remains_logged(self):
        with self.assertLogs("kfx", level="WARNING") as logs:
            mapping = self.build_with_diagnostics(warnings=[self.warning])
        self.assertIn(self.warning, "\n".join(logs.output))
        self.assertEqual(3, mapping.total)
        self.assertEqual(2, mapping.at_pid(2)["offset"])

    def test_other_warnings_still_block_map(self):
        for warning in (
            "Resource image has bad image",
            self.warning.replace("sd=1", "sd=2"),
            "Feature/content mismatch: yj_jpegxr_sd=None has_jpeg_xr_image=True",
        ):
            with self.subTest(warning=warning), self.assertLogs("kfx", level="WARNING"):
                with self.assertRaises(ValueError) as error:
                    self.build_with_diagnostics(warnings=[self.warning, warning])
                self.assertIn(warning, str(error.exception))

    def test_errors_still_block_even_if_text_matches_allowed_warning(self):
        for error in ("Missing content container", self.warning):
            with self.subTest(error=error), self.assertLogs("kfx", level="WARNING"):
                with self.assertRaises(ValueError) as raised:
                    self.build_with_diagnostics(warnings=[self.warning], errors=[error])
                self.assertIn(error, str(raised.exception))

    def test_allowed_warning_does_not_bypass_coordinate_comparison(self):
        with self.assertLogs("kfx", level="WARNING"):
            with self.assertRaisesRegex(ValueError, "Content and native position map disagree"):
                self.build_with_diagnostics(warnings=[self.warning], mismatch=True)

    def test_allowed_warning_does_not_bypass_map_validation(self):
        with self.assertLogs("kfx", level="WARNING"):
            with self.assertRaisesRegex(ValueError, "Text length differs"):
                self.build_with_diagnostics(warnings=[self.warning], invalid_text=True)


class TranslationTests(unittest.TestCase):
    def test_known_native_encoding(self):
        for eid, offset, encoded in [(2066, 0, "ARIIAAAAAAAA"), (1627, 83, "AVsGAABTAAAA"), (1435, 0, "AZsFAAAAAAAA")]:
            self.assertEqual(encoded, encode_long(eid, offset))
            self.assertEqual((eid, offset), decode_long(encoded))

    def test_generated_command_requires_kindle_map(self):
        self.assertEqual("/mnt/us/kindle-sync/bin/kindle-pos set-pid TEST 3",
                         sample().at_pid(3)["kindle_command"])

    def test_uint32_edges(self):
        self.assertEqual((0xffffffff, 0xffffffff), decode_long(encode_long(0xffffffff, 0xffffffff)))
        for eid, offset in [(0, 0), (-1, 0), (1, -1), (1, 2**32), (1.5, 0)]:
            with self.assertRaises(ValueError): encode_long(eid, offset)

    def test_half_open_boundaries_and_nonzero_base_offset(self):
        payload = copy.deepcopy(sample().payload)
        payload["chunks"][1]["offset"] = 9
        m = PositionMap(payload)
        for i, c in enumerate(m.chunks):
            self.assertEqual(c["eid"], m.at_pid(c["pid"])["eid"])
            self.assertEqual(c["offset"], m.at_pid(c["pid"])["offset"])
            self.assertEqual(c["offset"] + c["length"] - 1, m.at_pid(c["pid"] + c["length"] - 1)["offset"])

    def test_unicode_normalization_retains_native_offsets(self):
        m = sample()
        # Emoji occupies one code point, two UTF-16 units and four UTF-8 bytes.
        self.assertEqual(3, m.find("б в")[0]["pid"])
        match = m.find("STRASSE... end")[0]
        self.assertEqual(m.chunks[1]["pid"], match["pid"])
        self.assertEqual(m.chunks[1]["pid"] + len(m.chunks[1]["text"]) - 1, match["match_end_pid_inclusive"])

    def test_duplicate_matches_are_retained(self):
        self.assertEqual(2, len(sample().find("А😀")))

    def test_cross_paragraph_search(self):
        m = sample()
        self.assertEqual(5, m.find("в strasse")[0]["pid"])

    def test_empty_and_missing_query(self):
        self.assertEqual([], sample().find("missing"))
        with self.assertRaises(ValueError): sample().find(" \u00ad ")

    def test_percent_endpoints_and_invalid_inputs(self):
        m = sample()
        self.assertEqual(0, m.at_percent(0)["pid"])
        self.assertEqual(m.total - 1, m.at_percent(100)["pid"])
        for x in [-1, 101, float("nan"), float("inf")]:
            with self.assertRaises(ValueError): m.at_percent(x)
        for x in [-1, m.total, 0.5, True]:
            with self.assertRaises(ValueError): m.at_pid(x)

    def test_native_mismatch_rejected(self):
        m = sample()
        with self.assertRaises(ValueError): m.at_native(encode_long(100, 3) + ":4")
        with self.assertRaises(ValueError): m.at_native("AAAAAAAAAAAA:0")

    def test_invalid_map_rejected(self):
        for field, value in [("pid", 1), ("length", 0), ("offset", 2**32), ("text", "short")]:
            payload = copy.deepcopy(sample().payload)
            payload["chunks"][0][field] = value
            with self.assertRaises(ValueError): PositionMap(payload)

    def test_lua_encoding_matches_python(self):
        rng = random.Random(718)
        cases = [(1, 0), (0xffffffff, 0xffffffff)] + [(rng.randint(1, 0xffffffff), rng.randint(0, 0xffffffff)) for _ in range(100)]
        code = "\n".join(f"assert(m.encode_long({eid},{off})=={quote(encode_long(eid, off))})" for eid, off in cases)
        result = lua(code)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_lua_map_validation(self):
        code = """
local data='KINDLE-POS-MAP\t1\tTEST\t4\n0\t10\t0\t2\n2\t11\t7\t2\n'
local map=assert(m.parse_position_map(data,'TEST'))
assert(not m.parse_position_map(data,'OTHER'))
assert(not m.parse_position_map(data:gsub('2\\t11','3\\t11'),'TEST'))
assert(not m.parse_position_map(data:sub(1,-4),'TEST'))
assert(m.decode_long(assert(m.coordinate_for_pid(map,2))).offset==7)
assert(m.decode_long(assert(m.coordinate_for_pid(map,1))).eid==10)
assert(not m.coordinate_for_pid(map,4))
assert(not m.coordinate_for_pid(map,-1))
assert(not m.coordinate_for_pid(map,0.5))
assert(m.pid_for_percent(map,100)==3)
assert(m.pid_for_percent(map,0)==0)
assert(not m.pid_for_percent(map,0/0))
assert(not m.pid_for_percent(map,math.huge))
"""
        # Python multiline literals above contain real newlines in the Lua
        # quoted fixture; use a Lua long string for that one assignment.
        start = code.index("local data='")
        end = code.index("'\nlocal map")
        data = code[start + len("local data='"):end]
        code = "local data=" + quote(data) + code[end + 1:]
        result = lua(code)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_cli_ambiguous_has_no_selected_command(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "map.json"
            path.write_text(json.dumps(sample().payload), encoding="utf-8")
            cmd = [sys.executable, str(ROOT / "app/engine/kindle_map.py"), "resolve", str(path), "--text", "А😀"]
            result = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(2, result.returncode)
            self.assertNotIn("/mnt/us/", result.stdout)
            result = subprocess.run(cmd + ["--occurrence", "2", "--after", "--json"], capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(sample().chunks[2]["pid"] + 2, json.loads(result.stdout)["matches"][0]["pid"])


if __name__ == '__main__':
    unittest.main()
