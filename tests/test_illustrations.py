import base64
from io import BytesIO
from unittest import TestCase, mock
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from bs4 import BeautifulSoup
from PIL import Image

from book import write_book


class IllustrationExportTest(TestCase):
    def test_mixed_chapter_and_image_only_chapter(self):
        raw = BytesIO()
        Image.new("RGB", (3, 4)).save(raw, "WEBP")
        inline = "data:image/webp;base64," + base64.b64encode(raw.getvalue()).decode()
        urls = ["https://example.org/first", "https://example.org/second"]
        # The second chapter has artwork and no text. Test both extraction paths.
        pages = [
            f'<article><p>之前<img alt="图 &amp; 文" data-src="{inline}" src="/placeholder">之后</p>'
            f'<p><picture><source srcset="{inline}"><img src="{inline}"></picture></p>'
            '<p><img src="data:image/png;base64,YmFk"></p></article>',
            f'<article><p><img src="{inline}"></p></article>'
        ]
        for rule in (None, {"body": "article"}):
            with self.subTest(rule=rule):
                def fetch(url):
                    return BeautifulSoup(pages[urls.index(url)], "html.parser"), url

                out = BytesIO()
                with mock.patch("book.fetch", side_effect=fetch):
                    summary = write_book(out, "书", "", [dict(name=str(i), url=u) for i, u in enumerate(urls)],
                                         raw.getvalue(), lambda *_: None, lambda: None, rule)
                self.assertEqual(summary, {"images": 1, "missing": ["0"]})
                with ZipFile(out) as archive:
                    chapter = ET.fromstring(archive.read("ch1.xhtml"))
                    p = chapter.find(".//{*}p")
                    img = p.find("{*}img")
                    self.assertEqual((p.text, img.tail, img.get("alt")), ("之前", "之后", "图 & 文"))
                    refs = chapter.findall(".//{*}img") + ET.fromstring(archive.read("ch2.xhtml")).findall(".//{*}img")
                    self.assertEqual([i.get("src") for i in refs], [img.get("src")] * 3)
                    self.assertIn("未能下载", "".join(chapter.itertext()))
                    manifest = ET.fromstring(archive.read("book.opf"))
                    item = manifest.find(f'.//{{*}}item[@href="{img.get("src")}"]')
                    self.assertEqual(item.get("media-type"), "image/png")
                    with Image.open(BytesIO(archive.read(img.get("src")))) as decoded:
                        decoded.load()
                        self.assertEqual(decoded.size, (3, 4))
