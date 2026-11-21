"""网页解析与 EPUB 3 打包。"""
import base64
import json
from checkpoint import retry_network
import re
import time
import uuid
import zipfile
from datetime import datetime, timezone
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urldefrag
from xml.sax.saxutils import escape

import site_plugins
import requests
from illustrations import Illustration, ImageArchive, IMAGE_PATH, illustration
from bs4 import BeautifulSoup, UnicodeDammit, NavigableString, Comment, Tag
from bs4.dammit import EncodingDetector

CHAPTER = re.compile(r"第\s*[零〇一二两三四五六七八九十百千万两\d０-９廿卅]+\s*[章回节節卷幕篇]|^(?:chapter|part|book)\s+[\divxlcdm]+\b|^(?:序章|楔子|序言|尾声|尾聲|番外|后记|後記|prologue|epilogue)", re.I)
NAV = re.compile(r"^(?:上一[章页頁回]|下一[章页頁回]|返回目录|回目錄|加入书签|投推荐票|章节目录|下一节|上一节|next chapter|previous chapter)$", re.I)
BODY = "#chaptercontent,#chapterContent,#chapter-content,#nr1,.chapter-content,.chapter_content,.read-content,.readContent,.novel-content,.prp-pages-output,.mw-parser-output,#content,article,main"
JUNK = "script,style,noscript,nav,header,footer,aside,form,iframe,button,[hidden],.advertisement,.ads,.navigation,.mw-editsection,.noprint,.ws-noexport,.ws-pagenum,.ws-header,.licenseContainer"


@dataclass(frozen=True)
class InlineText:
    """Text whose superscript/subscript appearance must survive XHTML export."""
    tags: tuple
    text: str

    def render(self):
        value = xml(self.text)
        for tag in reversed(self.tags):
            value = f"<{tag}>{value}</{tag}>"
        return value


# A session explicitly opened in the ESJ login window, kept only for this run.
esj_cookies = requests.cookies.RequestsCookieJar()


def fetch(url):
    if urlsplit(url).scheme not in ("https", "http") or not urlsplit(url).hostname:
        raise ValueError("请输入以 https:// 或 http:// 开头的网页地址。")
    with requests.get(url, cookies=esj_cookies if site_plugins.hostname(url) == "esjzone.cc" else None, headers={"User-Agent": "NovelPress/1.0 (personal EPUB reader)"}, timeout=(8, 16), stream=True) as response:
        response.raise_for_status()
        kind = response.headers.get("Content-Type", "").lower()
        if kind and not any(k in kind for k in ("html", "text/plain", "octet-stream")):
            raise ValueError("这个链接没有返回 HTML 网页。")
        data = bytearray()
        for chunk in response.iter_content(65536):
            data.extend(chunk)
            if len(data) > 16 * 1024 * 1024:
                raise ValueError("单页超过 16 MB，请使用章节目录网址。")
        raw = bytes(data)
        charset = re.search(r"""charset\s*=\s*["']?([\w.-]+)""", kind)
        charset = charset[1] if charset else None
        declared = EncodingDetector.find_declared_encoding(raw, is_html=True)
        for encoding in (declared, charset, "utf-8-sig", "gb18030"):
            if encoding:
                try:
                    content = raw.decode("gb18030" if encoding.lower() in ("gb2312", "gbk") else encoding)
                    break
                except (UnicodeError, LookupError):
                    pass
        else:
            content = UnicodeDammit(raw, is_html=True).unicode_markup
        return BeautifulSoup(content, "html.parser"), response.url


def meta(doc, *names):
    for name in names:
        tag = doc.find("meta", attrs={"property": name}) or doc.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return ""


def lines(node, source, base=None):
    """Preserve paragraph boundaries and the order of mixed text/image runs."""
    for item in node.select(JUNK):
        item.decompose()
    for tag in node.select("br, p, div, section, h1, h2, h3, h4, li, blockquote, figure, figcaption"):
        tag.insert_before("\n")
        tag.insert_after("\n")
    paragraphs, runs, text = [], [], []

    def flush_text():
        if text:
            runs.append("".join(text))
            text.clear()

    def finish_line():
        flush_text()
        if any(isinstance(run, Illustration) for run in runs):
            paragraphs.append(tuple(runs))
        elif runs:
            line = " ".join("".join(run.text if isinstance(run, InlineText) else run for run in runs).split())
            if line and not NAV.fullmatch(line):
                paragraphs.append(tuple(runs) if any(isinstance(run, InlineText) for run in runs) else line)
        runs.clear()

    # Some editors link directly to the image rather than inserting an img.
    for link in node.select("a[href]"):
        if not link.find("img") and IMAGE_PATH.search(urlsplit(link["href"]).path):
            img = BeautifulSoup("", "html.parser").new_tag("img", src=link["href"], alt=link.get_text(strip=True))
            link.replace_with(img)
    for child in node.descendants:
        if isinstance(child, Tag) and child.name == "img":
            flush_text()
            runs.append(illustration(child, base or source, source))
        elif isinstance(child, NavigableString) and not isinstance(child, Comment):
            tags = []
            parent = child.parent
            while parent is not None and parent is not node:
                if isinstance(parent, Tag) and parent.name in ("sup", "sub"):
                    tags.append(parent.name)
                parent = parent.parent
            tags.reverse()
            for i, segment in enumerate(str(child).split("\n")):
                if i:
                    finish_line()
                if tags and segment:
                    flush_text()
                    runs.append(InlineText(tuple(tags), segment))
                else:
                    text.append(segment)
    finish_line()
    return paragraphs


def page_base(doc, source):
    base = doc.select_one("base[href]")
    return urljoin(source, base["href"]) if base else source


def website_cover(doc, base, rule):
    node = doc.select_one(rule["cover"]) if rule.get("cover") else None
    src = (node.get("data-src") or node.get("src") or node.get("href")) if node else None
    src = src or meta(doc, "og:image", "twitter:image")
    if not src:
        return ""
    address = urljoin(base, src)
    if urlsplit(address).scheme not in ("http", "https"):
        return ""
    try:
        with requests.get(address, headers={"Referer": base}, stream=True, timeout=(5, 12)) as reply:
            reply.raise_for_status()
            data = bytearray()
            for block in reply.iter_content(65536):
                data.extend(block)
                if len(data) > 12 * 1024 * 1024:
                    return ""
            if data.startswith(b"\x89PNG"): mime = "png"
            elif data.startswith(b"\xff\xd8"): mime = "jpeg"
            elif data[:4] == b"RIFF" and data[8:12] == b"WEBP": mime = "webp"
            elif data[:3] == b"GIF": mime = "gif"
            else: return ""
            return f"data:image/{mime};base64," + base64.b64encode(data).decode("ascii")
    except requests.RequestException:
        return ""


def analyze(url, check=lambda: None, report=lambda pages, count: None):
    url = url.strip()
    rule = site_plugins.load(url)
    doc, base = fetch(url)
    if site_plugins.hostname(url) != site_plugins.hostname(base):
        rule = site_plugins.load(base)
    if rule.get("parser") != "wikimedia":
        if site_plugins.hostname(base) == "esjzone.cc" and "/detail/" in base and not doc.select_one("#chapterList"):
            raise ValueError("ESJ 未返回作品目录。可登录 ESJ 后重新解析，或确认该作品仍可访问。")
        result = site_plugins.catalog(doc, base, rule, fetch, check, report)
        result["cover"] = website_cover(doc, base, rule)
        return result
    heading = doc.find("h1") or doc.title
    title = meta(doc, "og:novel:book_name", "og:title") or (heading.get_text(strip=True) if heading else "网页选集")
    author = meta(doc, "og:novel:author", "author")
    anchors = []
    seen = set()
    summary = doc.select(".ws-summary li a[href]")
    if summary and heading:
        title = heading.get_text(strip=True)
    byline = doc.select_one('a[title^="Author:"]')
    if byline and not author:
        author = byline.get_text(strip=True)
    for a in summary or doc.select("a[href]"):
        name = a.get_text(" ", strip=True)
        dest = urljoin(base, a["href"])
        catalog = a.find_parent(id=re.compile(r"^(list|chapterlist|chapters|toc)$", re.I)) or a.find_parent(class_=re.compile(r"^(chapter[-_]?list|book[-_]?list|toc)$", re.I))
        if not name or NAV.fullmatch(name) or not (summary or CHAPTER.search(name) or catalog):
            continue
        if urlsplit(dest).netloc != urlsplit(base).netloc or urlsplit(dest).scheme not in ("http", "https") or dest in seen or dest == base:
            continue
        seen.add(dest)
        anchors.append({"name": name, "url": dest})
    if not anchors:
        anchors = [{"name": title, "url": base}]
    return {"title": title, "author": author, "chapters": anchors, "base": base, "adapter": rule["name"], "cover": website_cover(doc, base, rule)}


def chapter_text(doc, fragment, name, source):
    base = page_base(doc, source)
    if fragment:
        from urllib.parse import unquote
        marker = doc.find(id=unquote(fragment)) or doc.find("a", attrs={"name": unquote(fragment)})
        if not marker:
            raise ValueError("未找到页内章节定位。")
        start = marker.find_parent(re.compile("^h[1-6]$")) or marker
        if start.name == "a" and start.parent.name in ("p", "div") and not start.get_text(strip=True):
            start = start.parent
        if start.parent and "mw-heading" in start.parent.get("class", []):
            start = start.parent
        if start.name in ("div", "section", "article") and start.find("p"):
            return [p for p in lines(start, source, base) if p != name]
        block = BeautifulSoup("<article></article>", "html.parser").article
        for sibling in list(start.next_siblings):
            if getattr(sibling, "name", None) in ("h1", "h2", "h3") or (getattr(sibling, "name", None) and sibling.find(["h1","h2"])):
                if block.get_text(strip=True) or block.find("img"):
                    break
            block.append(sibling)
    else:
        block = next((el for sel in BODY.split(",") if (el := doc.select_one(sel)) and (el.get_text(strip=True) or el.find("img"))), None)
        if not block:
            paragraphs = doc.select("p")
            block = max((p.parent for p in paragraphs), key=lambda el: sum(len(p.get_text(strip=True)) for p in el.find_all("p", recursive=False)), default=None)
    if not block:
        raise ValueError("未识别到正文，网页可能需要登录或运行脚本。")
    result = [p for p in lines(block, source, base) if p != name]
    if not result:
        raise ValueError("章节正文为空。")
    return result


def collect_pages(doc, source, fragment, name, check, chapter_urls):
    """Follow links explicitly labelled as the next page of the same chapter."""
    seen = {source}
    result = []
    while True:
        check()
        link = None if fragment else next((a for a in doc.select("a[href]")
            if a.get_text(strip=True) in ("下一页", "下一頁", "本章下一页", "本章下一頁")), None)
        following = urldefrag(urljoin(source, link["href"]))[0] if link else ""
        result.extend(chapter_text(doc, fragment, name, source))
        if not following or following == source or following in chapter_urls:
            return result
        if following in seen or len(seen) >= 80:
            raise ValueError("章节分页循环或页数过多，请核对来源网页。")
        if urlsplit(following).netloc != urlsplit(source).netloc:
            raise ValueError("章节分页跳转至其他站点，请核对来源网页。")
        seen.add(following)
        time.sleep(.35)
        doc, source = fetch(following)


def xml(text):
    return escape(re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]", "", text))


def xhtml(title, body):
    return f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>{xml(title)}</title><link rel="stylesheet" href="book.css" type="text/css"/></head><body>{body}</body></html>'


def encode_chapter(paragraphs):
    def encode(run):
        if isinstance(run, Illustration):
            return {"sources": run.sources, "referer": run.referer, "alt": run.alt}
        if isinstance(run, InlineText):
            return {"tags": run.tags, "text": run.text}
        return run
    return json.dumps([p if isinstance(p, str) else [encode(r) for r in p]
                       for p in paragraphs], ensure_ascii=False)


def decode_chapter(raw):
    def decode(run):
        if isinstance(run, str):
            return run
        if "tags" in run:
            return InlineText(tuple(run["tags"]), run["text"])
        return Illustration(tuple(run["sources"]), run["referer"], run["alt"])
    return [p if isinstance(p, str) else tuple(decode(r) for r in p) for p in json.loads(raw)]


def write_book(path, title, author, chapters, cover, progress, check, rule=None, checkpoint=None):
    cache_url, cached, cached_source = None, None, None
    uid = uuid.uuid4()
    chapter_urls = {urldefrag(ch["url"])[0] for ch in chapters}
    items, spine, links = [], [], []
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as book, ImageArchive(book, esj_cookies, check, checkpoint=checkpoint) as images:
        book.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        book.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="book.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        book.writestr("cover.png", cover)
        book.writestr("cover.xhtml", xhtml(title, '<div style="text-align:center"><img src="cover.png" alt="封面" style="max-width:100%;max-height:95vh"/></div>'))
        book.writestr("book.css", "body{margin:5%;line-height:1.75}h1{font-size:1.5em}p{text-indent:2em;margin:.5em 0}p.art{text-indent:0}img{max-width:100%;height:auto}")
        prepared = []
        failures = []
        for i, ch in enumerate(chapters, 1):
            check()
            progress(i - 1, f"正在读取《{ch['name']}》· 插图后台并行下载中")
            address, fragment = urldefrag(ch["url"])
            def read():
                nonlocal cache_url, cached, cached_source
                if address == cache_url and cached is not None:
                    doc, source = BeautifulSoup(cached, "html.parser"), cached_source
                else:
                    doc, source = fetch(address)
                    cache_url, cached = address, str(doc) if fragment else None
                    cached_source = source
                if rule:
                    return site_plugins.paragraphs(doc, rule, lambda block: lines(block, source, page_base(doc, source)), source)
                return collect_pages(doc, source, fragment, ch.get("source_name", ch["name"]), check, chapter_urls)

            raw = checkpoint.get("chapter", ch["url"]) if checkpoint else None
            if raw is not None:
                paragraphs = decode_chapter(raw)
            else:
                if i > 1:
                    time.sleep(.35)
                try:
                    paragraphs = retry_network(read, check, lambda n: progress(
                        len(prepared), f"《{ch['name']}》网络异常，第 {n}/2 次重试"))
                except InterruptedError:
                    raise
                except Exception as error:
                    if not checkpoint:
                        raise ValueError(f'《{ch["name"]}》读取失败：{error}') from error
                    failures.append(f'《{ch["name"]}》：{error}')
                    continue
                if checkpoint:
                    checkpoint.put("chapter", ch["url"], encode_chapter(paragraphs))
            for paragraph in paragraphs:
                if not isinstance(paragraph, str):
                    for run in paragraph:
                        if isinstance(run, Illustration):
                            images.prefetch(run)
            prepared.append((i, ch, paragraphs))

        if failures:
            raise ValueError(f"{len(failures)} 章未完成，已保留 {len(prepared)} 章。再次导出可继续。\n" + "\n".join(failures[:3]))
        written = len(prepared)

        def image_progress(done, total):
            progress(written, f"插图处理完成（{done}/{total}）")

        images.set_progress_callback(image_progress)
        for i, ch, paragraphs in prepared:
            check()
            name = xml(ch["name"])
            body = [f"<h1>{name}</h1>"]
            for paragraph in paragraphs:
                if isinstance(paragraph, str):
                    body.append(f"<p>{xml(paragraph)}</p>")
                else:
                    content_parts = []
                    for run in paragraph:
                        if isinstance(run, Illustration):
                            content_parts.append(images.render(run, ch["name"]))
                        elif isinstance(run, InlineText):
                            content_parts.append(run.render())
                        else:
                            content_parts.append(xml(run))
                    content = "".join(content_parts)
                    css_class = ' class="art"' if any(isinstance(run, Illustration) for run in paragraph) else ""
                    body.append(f"<p{css_class}>{content}</p>")
            book.writestr(f"ch{i}.xhtml", xhtml(ch["name"], "".join(body)))
            items.append(f'<item id="c{i}" href="ch{i}.xhtml" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="c{i}"/>')
            links.append(f'<li><a href="ch{i}.xhtml">{name}</a></li>')
            written = i
            progress(written, f"正在整理章节（{written}/{len(chapters)}）")
        progress(len(chapters), "正在生成 EPUB 目录")
        check()
        book.writestr("nav.xhtml", xhtml("目录", '<nav epub:type="toc"><h1>目录</h1><ol>'+ "".join(links) + "</ol></nav>"))
        items.extend(images.manifest.values())
        modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        language = "zh" if re.search(r"[\u3400-\u9fff]", title + chapters[0]["name"] + "".join(p for p in paragraphs[:2] if isinstance(p, str))) else "und"
        book.writestr("book.opf", f'''<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="uid">urn:uuid:{uid}</dc:identifier><dc:title>{xml(title)}</dc:title><dc:language>{language}</dc:language><dc:creator>{xml(author or "佚名")}</dc:creator><meta property="dcterms:modified">{modified}</meta></metadata><manifest><item id="img" href="cover.png" media-type="image/png" properties="cover-image"/><item id="front" href="cover.xhtml" media-type="application/xhtml+xml"/><item id="css" href="book.css" media-type="text/css"/><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>{"".join(items)}</manifest><spine><itemref idref="front"/>{"".join(spine)}</spine></package>''')
    return {"images": len(images.manifest), "missing": images.missing}
