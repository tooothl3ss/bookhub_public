from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
from app.engine.epub_xpath import EpubIndex
from app.engine.kindle_map import PositionMap
from app.engine.position_bridge import PositionBridge


def make_epub(path, bodies):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>')
        manifest = ''.join(f'<item id="c{i}" href="chapter%20{i}.xhtml"/>' for i in range(len(bodies)))
        spine = ''.join(f'<itemref idref="c{i}"/>' for i in range(len(bodies)))
        z.writestr("OPS/book.opf", f'<package><manifest>{manifest}</manifest><spine>{spine}</spine></package>')
        for i, body in enumerate(bodies):
            z.writestr(f"OPS/chapter {i}.xhtml", '<html xmlns="http://www.w3.org/1999/xhtml"><body>' + body + '</body></html>')


class EpubTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "book.epub"
        make_epub(self.path, ['<div><p>Белый <em>свет</em> уже <b>не</b> слепит.</p><p>А😀Б &amp; В</p>'
                              '<p>до<!-- comment -->после<?pi ignored?>конец</p>'
                              '<p>repeated phrase</p><p>Straße…</p><script>secret script</script></div>',
                             '<div><p>repeated phrase</p><p>after duplicate</p></div>'])
        self.index = EpubIndex(self.path)

    def test_nested_inline_and_tail_xpath(self):
        m = self.index.find("Белый свет уже не слепит.", anchor_offset=11)
        self.assertEqual(1, len(m))
        self.assertEqual('/body/DocFragment[1]/body/div[1]/p[1]/text()[2].1', m[0]['xpath'])
        self.assertTrue(self.index.resolve(m[0]['xpath'])['context_after'].startswith('уже'))

    def test_comment_and_pi_tail_node_numbers(self):
        match = self.index.find("после")[0]
        self.assertEqual('/body/DocFragment[1]/body/div[1]/p[3]/text()[2].0', match['xpath'])
        self.assertEqual('/body/DocFragment[1]/body/div[1]/p[3]/text()[3].0', self.index.find('конец')[0]['xpath'])
        self.assertEqual([], self.index.find('comment'))

    def test_codepoints_entities_and_href_unquote(self):
        m = self.index.find('Б & В')[0]
        self.assertEqual(2, m['char_offset'])
        self.assertEqual('OPS/chapter 0.xhtml', m['spine_file'])
        self.assertTrue(self.index.resolve(m['xpath'])['context_before'].endswith('А😀'))

    def test_duplicate_search_and_fragment_filter(self):
        self.assertEqual(2, len(self.index.find('repeated phrase')))
        self.assertEqual(1, self.index.find('repeated phrase', fragment=2)[0]['spine_index'])
        with self.assertRaises(ValueError): self.index.find('repeated', fragment=0)

    def test_invisible_content_and_cross_block_query(self):
        self.assertEqual([], self.index.find('secret script'))
        self.assertEqual(1, len(self.index.find('слепит. А😀Б')))

    def test_normalization_does_not_invent_character_boundaries(self):
        m = self.index.find('STRASSE...')[0]
        self.assertEqual(0, m['char_offset'])
        self.assertEqual([], self.index.find('STRASSE...', anchor_offset=5))
        with self.assertRaises(ValueError): self.index.find(' ­ ')

    def test_forward_rejects_missing_and_out_of_range_nodes(self):
        for xpath in ('/body/DocFragment[0]/body', '/body/DocFragment[9]/body',
                      '/body/DocFragment[1]/body/div[1]/p[2]/text()[1].99',
                      '/body/DocFragment[1]/body/div[0]/text()[1].0'):
            with self.assertRaises(ValueError): self.index.resolve(xpath)

    def test_all_text_nodes_roundtrip(self):
        for run in self.index.runs:
            for offset in {0, len(run['text']) // 2, len(run['text'])-1}:
                result = self.index.at_raw(run['start'] + offset)
                self.assertEqual(run['start'] + offset, self.index.resolve(result['xpath'])['raw_index'])

    def test_text_node_end_is_a_valid_xpath_boundary(self):
        match = self.index.find('А😀Б & В', anchor_offset=len('А😀Б & В'))[0]
        self.assertEqual('/body/DocFragment[1]/body/div[1]/p[2]/text()[1].7', match['xpath'])
        self.assertEqual(7, self.index.resolve(match['xpath'])['char_offset'])

    def test_implicit_text_index_one(self):
        self.assertEqual(2, self.index.resolve('/body/DocFragment[1]/body/div[1]/p[2]/text().2')['char_offset'])

    def test_malformed_xml_is_recovered_and_logged(self):
        # A reader opens such a book, so it is accepted; a position the repaired tree lacks fails alone.
        make_epub(self.path, ['<p>broken<br>line</p><p>next', '<p>second chapter</p>'])
        with self.assertLogs(level='WARNING') as logs: index = EpubIndex(self.path)
        self.assertIn('chapter 0.xhtml is not well-formed', logs.output[0])
        self.assertEqual('line', index.resolve('/body/DocFragment[1]/body/p[1]/text()[2].0')['context_after'][:4])
        self.assertEqual(2, index.resolve('/body/DocFragment[1]/body/p[2]/text().2')['char_offset'])
        self.assertEqual(1, index.find('second chapter')[0]['spine_index'])
        with self.assertRaises(ValueError): index.resolve('/body/DocFragment[1]/body/p[3]/text().0')

    def test_named_entities_are_single_characters_in_any_encoding(self):
        body = '<p>до&nbsp;после&hellip; AT&T &bogus; <![CDATA[&nbsp;]]><!-- &nbsp; --></p>'
        for encoding in ('utf-8', 'utf-16', 'windows-1251'):
            with self.subTest(encoding), zipfile.ZipFile(self.path, 'w') as z:
                z.writestr('META-INF/container.xml', '<container><rootfiles><rootfile full-path="b.opf"/></rootfiles></container>')
                z.writestr('b.opf', '<package><manifest><item id="c" href="c.xhtml"/></manifest><spine><itemref idref="c"/></spine></package>')
                z.writestr('c.xhtml', f'<?xml version="1.0" encoding="{encoding}"?><html><body>{body}</body></html>'.encode(encoding))
            runs = EpubIndex(self.path).runs
            self.assertEqual(['до\xa0после… AT&T &bogus; &nbsp;'], [run['text'] for run in runs])

    def test_no_match_does_not_fallback_to_percentage(self):
        text = 'Completely different wording from another book, with enough context.'
        mapping = PositionMap(dict(version=1, book=dict(key='TEST'), total_positions=len(text),
                                   chunks=[dict(pid=0,eid=1,offset=0,length=len(text),text=text)]))
        self.assertEqual('unresolved', PositionBridge(self.index, mapping).to_epub(10)['status'])


class CrossPointPositionTests(unittest.TestCase):
    """EpubIndex.xpath_for_spine: what CrossPoint 1.6.5 keeps in progress.bin, as an XPath."""
    BASE = '/body/DocFragment[1]/body'
    # Offsets as the firmware counts them: "\n" 0, Глава 1-5, "\n" 6, Первый абзац. 7-19, "\n" 20, "\n" after the
    # picture 21, "Второй & " 22-30, третий 31-36, " хвост" 37-42, "\n" 43, пункт 44-48, "\n" 49.
    CHAPTER = ('\n<h1>Глава</h1>\n<p>Первый абзац.</p>\n<div><img src="i.jpg"/></div>\n<p>Второй &amp; <b>третий</b>'
               ' хвост</p>\n<ul><li>пункт</li></ul><style>p{}</style><script>s()</script>\n')

    def index(self, *bodies):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / 'book.epub'
        make_epub(path, list(bodies))
        return EpubIndex(path)

    def test_offset_counts_all_body_text_as_the_page_table_does(self):
        index = self.index(self.CHAPTER, '<div><img src="c.jpg"/></div>', '<p>Последняя глава.</p>')
        at = lambda *position: index.xpath_for_spine(*position)['xpath']
        for offset, expected in {
            1: '/h1[1]/text()[1].0',       # a heading is outside every paragraph and still counts
            7: '/p[1]/text()[1].0',        # so do the line breaks between the blocks
            14: '/p[1]/text()[1].7',
            21: '/p[2]/text()[1].0',       # a page that opens with the picture: the word after it
            29: '/p[2]/text()[1].7',       # &amp; is one character
            31: '/p[2]/b[1]/text()[1].0',
            39: '/p[2]/text()[2].2',
            44: '/ul[1]/li[1]/text()[1].0',  # style and script hold no visible text
            49: '/ul[1]/li[1]/text()[1].4', 50: '/ul[1]/li[1]/text()[1].4', 5000: '/ul[1]/li[1]/text()[1].4',  # no word follows: the last one
        }.items():
            with self.subTest(offset): self.assertEqual(self.BASE + expected, at(0, offset))
        # The page that opens a chapter is offset 0 whatever precedes its first word; no offset means the same.
        for position in ((0,), (0, 0), (0, None, 0), (0, None, .01)):
            self.assertEqual(self.BASE, at(*position))
        self.assertEqual('Глава', index.resolve(self.BASE)['context_after'][:5])
        # The fallback takes that share of the same text; an offset, when there is one, wins.
        self.assertEqual(self.BASE + '/p[2]/text()[1].3', at(0, None, .5))
        self.assertEqual(self.BASE + '/ul[1]/li[1]/text()[1].4', at(0, None, 1))
        self.assertEqual(self.BASE + '/ul[1]/li[1]/text()[1].4', at(0, None, 7))
        self.assertEqual(self.BASE + '/p[1]/text()[1].7', at(0, 14, .9))
        # A chapter without text is a position too, whatever the device counted there.
        self.assertEqual('/body/DocFragment[2]/body', at(1, 12))
        self.assertEqual('/body/DocFragment[1]/body', self.index('\n<div><img src="c.jpg"/></div>\n', '<p>Текст.</p>').xpath_for_spine(0, 2)['xpath'])
        self.assertEqual('Последняя', index.resolve(at(1))['context_after'][:9])
        self.assertEqual('/body/DocFragment[3]/body/p[1]/text()[1].4', at(2, 4))

    def test_finished_book_reading_order_and_share_of_the_book(self):
        index = self.index(self.CHAPTER, '<div><img src="c.jpg"/></div>', '<p>Последняя глава.</p>')
        points = [index.xpath_for_spine(*p) for p in ((0,), (0, 1), (0, 14), (0, 21), (0, 44), (0, 50), (1,), (2,), (2, 4), (3,))]
        self.assertEqual(dict(xpath='/body/DocFragment[3]/body/p[1]/text()[1].15', percentage=1.0), points[-1])  # spine count: finished
        resolved = [index.resolve(p['xpath']) for p in points]
        order = [(r['spine_index'], r['raw_index']) for r in resolved]
        self.assertEqual(sorted(order), order)
        self.assertEqual([0, 0, 0, 0, 0, 0, 1, 2, 2, 2], [spine for spine, _ in order])
        shares = [p['percentage'] for p in points]
        self.assertEqual((0.0, sorted(shares)), (shares[0], shares))
        self.assertEqual([r['raw_index'] / len(index.text) for r in resolved[:-1]], shares[:-1])

    def test_vectors_of_the_firmware_resolver_where_all_text_is_in_paragraphs(self):
        # test/koreader_xpath_resolver in crosspoint-reader 1.6.5. With no text outside <p>, the XPath the
        # firmware sends for an offset is the place of that offset; elsewhere it lies further down the chapter.
        nested = self.index('<div><section><p>Alpha bravo</p><p>Second <em>nested</em> tail</p></section></div>')
        at = lambda index, offset: index.xpath_for_spine(0, offset)['xpath']
        self.assertEqual(self.BASE + '/div[1]/section[1]/p[1]/text()[1].6', at(nested, 6))
        self.assertEqual(self.BASE + '/div[1]/section[1]/p[2]/text()[2].2', at(nested, 26))
        self.assertEqual(self.BASE + '/div[1]/section[1]/p[1]/text()[1].0', nested.resolve(at(nested, 0))['xpath'])
        self.assertEqual(self.BASE + '/p[1]/text()[1].3', at(self.index('<p>Aé中B</p>'), 3))
        self.assertEqual(self.BASE + '/p[1]/text()[1].2', at(self.index('<p><RP><span>hidden</span></RP>Visible text</p>'), 2))
        for boundary in ('<!--comment-->', '<?marker?>'):
            index = self.index(f'<p>before{boundary}after</p>')
            self.assertEqual([self.BASE + '/p[1]/text()[2].0', self.BASE + '/p[1]/text()[2].1'], [at(index, 6), at(index, 7)])

    def test_refuses_what_is_not_a_position(self):
        index = self.index('<p>Одна глава с текстом.</p>')
        for position in ((2,), (-1,), (True,), ('0',), (0, -1), (0, 1.5)):
            with self.subTest(position), self.assertRaises(ValueError): index.xpath_for_spine(*position)
        pictures = self.index('<div><img src="1.jpg"/></div>', '<div><img src="2.jpg"/></div>')
        for position in ((0,), (1, 0), (2,)):
            with self.assertRaisesRegex(ValueError, 'no text'): pictures.xpath_for_spine(*position)


if __name__ == '__main__':
    unittest.main()
