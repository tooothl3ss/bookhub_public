"""Defects found by the review of the first round of fixes. Engine only: no database, no shelf."""
import io
from pathlib import Path
import tempfile
import unittest
import zipfile

from bookhub.engine.epub_xpath import EpubIndex, validate_epub

CONTAINER = ('<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
             '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
QUOTED = 'He said &quot;go&quot; &amp; left for the R&amp;D lab &lt;at once&gt; with Smith &apos;n&apos; Wesson.'
SHOWN = 'He said "go" & left for the R&D lab <at once> with Smith \'n\' Wesson.'


def epub(*bodies):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr(zipfile.ZipInfo('mimetype'), 'application/epub+zip')
        z.writestr(zipfile.ZipInfo('META-INF/container.xml'), CONTAINER)
        manifest = ''.join(f'<item id="i{n}" href="c{n}.xhtml" media-type="application/xhtml+xml"/>' for n in range(len(bodies)))
        spine = ''.join(f'<itemref idref="i{n}"/>' for n in range(len(bodies)))
        z.writestr(zipfile.ZipInfo('OEBPS/content.opf'), '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
                   '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Review</dc:title></metadata>'
                   f'<manifest>{manifest}</manifest><spine>{spine}</spine></package>')
        for n, body in enumerate(bodies):
            z.writestr(zipfile.ZipInfo(f'OEBPS/c{n}.xhtml'),
                       '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head>' + body + '</html>')
    return buf.getvalue()


class ReviewFixes(unittest.TestCase):
    def index(self, *bodies):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / 'book.epub'
        path.write_bytes(epub(*bodies))
        return validate_epub(path)

    def test_recovered_chapter_keeps_the_characters_written_as_xml_entities(self):
        # An unclosed <p> is the first error; everything after it used to lose & < > " '.
        broken = self.index(f'<body><div><p>Heading<p>Subheading</div><p>{QUOTED}</p></body>')
        clean = self.index(f'<body><div><p>Heading</p><p>Subheading</p></div><p>{QUOTED}</p></body>')
        self.assertIn(SHOWN, broken.text)
        self.assertIn(SHOWN, clean.text)
        self.assertNotIn('&amp;', broken.text)
        # Where the recovery puts the paragraph is libxml2's business; the well-formed twin is addressable.
        point = clean.resolve(f"/body/DocFragment[1]/body/p[1]/text()[1].{SHOWN.index('lab')}")
        self.assertTrue(clean.text[point['raw_index']:].startswith('lab <at once>'))

    def test_cdata_and_comments_are_not_rewritten_by_the_recovery(self):
        index = self.index('<body><p>open<p>next</p><!-- R&amp;D --><p><![CDATA[literal &amp; text]]> and R&amp;D</p></body>')
        self.assertIn('literal &amp; text and R&D', index.text)

    def test_half_a_surrogate_pair_does_not_refuse_the_book(self):
        index = self.index('<body><p>Smile &#55357;&#56832; and &#xD83D; the rest of the paragraph.</p></body>')
        self.assertIn('Smile �� and � the rest of the paragraph.', index.text)

    def test_picture_pages_in_a_row_do_not_carry_the_kindle_to_distant_text(self):
        from bookhub.engine.kindle_map import PositionMap
        from bookhub.engine.position_bridge import PositionBridge
        opening = ['The album opens with one long unique sentence about distant harbours.']
        closing = ['Its afterword is the very last visible text of this whole picture book.']
        picture = '<body><div><img src="p.jpg"/></div></body>'
        index = self.index('<body><p>%s</p></body>' % opening[0], picture, picture, picture, '<body><p>%s</p></body>' % closing[0])
        chunks = [dict(pid=0, eid=100, offset=0, length=len(opening[0]), text=opening[0], section='c0'),
                  dict(pid=len(opening[0]), eid=101, offset=0, length=len(closing[0]), text=closing[0], section='c1')]
        bridge = PositionBridge(index, PositionMap(dict(version=1, book=dict(key='ALBUM'), chunks=chunks,
                                                        total_positions=sum(c['length'] for c in chunks))))
        # Reading order is kept for the shelf whatever the Kindle can be told.
        ordinals = [index.resolve(f'/body/DocFragment[{n}]/body/div/img')['raw_index'] for n in (2, 3, 4)]
        self.assertEqual(sorted(ordinals), ordinals)
        for fragment in (2, 3):  # the afterword is two and more spine items away
            self.assertEqual('unresolved', bridge.to_kindle(f'/body/DocFragment[{fragment}]/body/div/img')['status'])
        near = bridge.to_kindle('/body/DocFragment[4]/body/div/img')  # the page right before the afterword
        self.assertEqual(('mapped', len(opening[0])), (near['status'], near['matches'][0]['pid']))

    def test_bare_and_unquoted_attributes_do_not_cut_the_chapter(self):
        index = self.index('<body><audio controls src="a.mp3"></audio><p class=a>one</p><p align=center>centered</p>'
                           '<table border=1 width=100%><tr><td nowrap>cell</td></tr></table><p>a &lt; b and x = 1 &gt; 0</p><p>after</p></body>')
        for shown in ('one', 'centered', 'cell', 'a < b and x = 1 > 0', 'after'):
            self.assertIn(shown, index.text)
        for phantom in ('src=', 'a>one', 'center>', 'nowrap'):
            self.assertNotIn(phantom, index.text)
        self.assertTrue(index.text[index.resolve('/body/DocFragment[1]/body/p[4]/text()[1].0')['raw_index']:].startswith('after'))

    def test_rebuilt_map_under_the_same_path_gets_a_new_bridge(self):
        from unittest.mock import patch
        from bookhub.sync.store import Store
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.addCleanup(Store.built.cache_clear)
        mapping = Path(folder.name) / 'map.json'
        mapping.write_text('{"first": 1}')
        with patch('bookhub.sync.store.PositionBridge', side_effect=lambda index, path: object()), \
                patch.object(Store, 'index', side_effect=lambda epub: epub):
            first = Store.bridge('book.epub', str(mapping))
            self.assertIs(first, Store.bridge('book.epub', str(mapping)))
            mapping.write_text('{"rebuilt": "with other positions"}')
            self.assertIsNot(first, Store.bridge('book.epub', str(mapping)))

    def test_well_formed_chapter_is_not_touched(self):
        index = self.index(f'<body><p>{QUOTED}</p><p>A&#160;B &#x2014; C</p></body>')
        self.assertIn(SHOWN, index.text)
        self.assertIn('A B — C', index.text)


if __name__ == '__main__':
    unittest.main()
