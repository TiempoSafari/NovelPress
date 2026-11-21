"""网页解析与 EPUB 3 打包。"""
import base64
import re
import time
import uuid
import zipfile
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit, urldefrag
from xml.sax.saxutils import escape

import site_plugins
import requests
from bs4 import BeautifulSoup, UnicodeDammit
from bs4.dammit import EncodingDetector

CHAPTER = re.compile(r"第\s*[零〇一二两三四五六七八九十百千万两\d０-９廿卅]+\s*[章回节節卷幕篇]|^(?:chapter|part|book)\s+[\divxlcdm]+\b|^(?:序章|楔子|序言|尾声|尾聲|番外|后记|後記|prologue|epilogue)", re.I)
NAV = re.compile(r"^(?:上一[章页頁回]|下一[章页頁回]|返回目录|回目錄|加入书签|投推荐票|章节目录|下一节|上一节|next chapter|previous chapter)$", re.I)
BODY = "#chaptercontent,#chapterContent,#chapter-content,#nr1,.chapter-content,.chapter_content,.read-content,.readContent,.novel-content,.prp-pages-output,.mw-parser-output,#content,article,main"
JUNK = "script,style,noscript,nav,header,footer,aside,form,iframe,button,[hidden],.advertisement,.ads,.navigation,.mw-editsection,.noprint,.ws-noexport,.ws-pagenum,.ws-header,.licenseContainer"


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


def lines(node):
    for item in node.select(JUNK):
        item.decompose()
    for tag in node.select("br, p, div, section, h1, h2, h3, h4, li, blockquote"):
        tag.insert_before("\n")
        tag.insert_after("\n")
    return [s for line in node.get_text().splitlines() if (s := " ".join(line.split())) and not NAV.fullmatch(s)]


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


def chapter_text(doc, fragment, name):
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
            return [p for p in lines(start) if p != name]
        block = BeautifulSoup("<article></article>", "html.parser").article
        for sibling in list(start.next_siblings):
            if getattr(sibling, "name", None) in ("h1", "h2", "h3") or (getattr(sibling, "name", None) and sibling.find(["h1","h2"])):
                if block.get_text(strip=True):
                    break
            block.append(sibling)
    else:
        block = next((el for sel in BODY.split(",") if (el := doc.select_one(sel)) and el.get_text(strip=True)), None)
        if not block:
            paragraphs = doc.select("p")
            block = max((p.parent for p in paragraphs), key=lambda el: sum(len(p.get_text(strip=True)) for p in el.find_all("p", recursive=False)), default=None)
    if not block:
        raise ValueError("未识别到正文，网页可能需要登录或运行脚本。")
    result = [p for p in lines(block) if p != name]
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
        result.extend(chapter_text(doc, fragment, name))
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


def write_book(path, title, author, chapters, cover, progress, check, rule=None):
    cache_url, cached = None, None
    uid = uuid.uuid4()
    chapter_urls = {urldefrag(ch["url"])[0] for ch in chapters}
    items, spine, links = [], [], []
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as book:
        book.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        book.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="book.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        book.writestr("cover.png", cover)
        book.writestr("cover.xhtml", xhtml(title, '<div style="text-align:center"><img src="cover.png" alt="封面" style="max-width:100%;max-height:95vh"/></div>'))
        book.writestr("book.css", "body{margin:5%;line-height:1.75}h1{font-size:1.5em}p{text-indent:2em;margin:.5em 0}")
        for i, ch in enumerate(chapters, 1):
            check()
            progress(i - 1, ch["name"])
            address, fragment = urldefrag(ch["url"])
            try:
                if address != cache_url or cached is None:
                    if i > 1:
                        time.sleep(.35)
                    doc, _ = fetch(address)
                    cache_url, cached = address, str(doc) if fragment else None
                if cached:
                    doc = BeautifulSoup(cached, "html.parser")
                paragraphs = site_plugins.paragraphs(doc, rule, lines) if rule else collect_pages(doc, address, fragment, ch.get("source_name", ch["name"]), check, chapter_urls)
            except InterruptedError:
                raise
            except Exception as error:
                raise ValueError(f'《{ch["name"]}》读取失败：{error}') from error
            name = xml(ch["name"])
            book.writestr(f"ch{i}.xhtml", xhtml(ch["name"], f"<h1>{name}</h1>" + "".join(f"<p>{xml(p)}</p>" for p in paragraphs)))
            items.append(f'<item id="c{i}" href="ch{i}.xhtml" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="c{i}"/>')
            links.append(f'<li><a href="ch{i}.xhtml">{name}</a></li>')
        check()
        book.writestr("nav.xhtml", xhtml("目录", '<nav epub:type="toc"><h1>目录</h1><ol>'+ "".join(links) + "</ol></nav>"))
        modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        language = "zh" if re.search(r"[\u3400-\u9fff]", title + chapters[0]["name"] + "".join(paragraphs[:2])) else "und"
        book.writestr("book.opf", f'''<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="uid">urn:uuid:{uid}</dc:identifier><dc:title>{xml(title)}</dc:title><dc:language>{language}</dc:language><dc:creator>{xml(author or "佚名")}</dc:creator><meta property="dcterms:modified">{modified}</meta></metadata><manifest><item id="img" href="cover.png" media-type="image/png" properties="cover-image"/><item id="front" href="cover.xhtml" media-type="application/xhtml+xml"/><item id="css" href="book.css" media-type="text/css"/><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>{"".join(items)}</manifest><spine><itemref idref="front"/>{"".join(spine)}</spine></package>''')

