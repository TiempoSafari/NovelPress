"""Declarative, hostname-scoped site adapters; files are reloaded on each import."""
import json
import re
import os
import tempfile
import sys
import time
import base64
from pathlib import Path
from urllib.parse import urlsplit, urljoin, urldefrag

import soupsieve
import requests

DEFAULTS = Path(__file__).resolve().parent / "plugins"
DIRECTORY = Path(sys.executable).parent / "plugins" if getattr(sys, "frozen", False) else DEFAULTS
BROWSER_AGENT = "Mozilla/5.0"


BUNDLED = DIRECTORY.parent / "bundled-plugins"
if getattr(sys, "frozen", False) and BUNDLED.is_dir():
    DIRECTORY = Path(os.environ["LOCALAPPDATA"]) / "NovelPress" / "plugins"


def plugin_folder():
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    return DIRECTORY


def hostname(url):
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


class UnsupportedSite(ValueError):
    def __init__(self, host):
        self.host = host
        super().__init__(f"不支持此网站（{host}）。请安装对应的站点插件。")


def validate(content, filename):
    try:
        rule = json.loads(content.decode("utf-8-sig"))
        if not isinstance(rule, dict) or rule.get("version") != 1:
            raise ValueError("不支持的插件版本")
        if not isinstance(rule.get("name"), str) or not rule["name"].strip():
            raise ValueError("缺少插件名称")
        if rule.get("parser") == "wikimedia":
            return rule
        parser = rule.get("parser", "css")
        if parser not in ("css", "ciweimao") or rule.get("order", "document") not in ("reverse", "document"):
            raise ValueError("未知的解析方式或章节顺序")
        api_catalog = bool(rule.get("chapter_api"))
        required = ("title", "chapter_title", "body")
        if not api_catalog:
            required += ("catalog", "links")
        for key in required:
            if not isinstance(rule.get(key), str) or not rule[key].strip():
                raise ValueError(f"缺少字段 {key}")
        for key in ("catalog", "catalog_link", "title", "title_remove", "links", "older", "chapter_title", "body", "remove", "author", "cover"):
            if rule.get(key):
                soupsieve.compile(rule[key])
        if rule.get("first_page_suffix"):
            re.compile(rule["first_page_suffix"])
        if rule.get("ignore_links"):
            re.compile(rule["ignore_links"])
        if api_catalog:
            if not isinstance(rule.get("chapter_api"), str):
                raise ValueError("chapter_api 必须是站内接口路径")
            if rule.get("chapter_lock_field") and not isinstance(rule["chapter_lock_field"], str):
                raise ValueError("chapter_lock_field 必须是字符串")
            if not all(isinstance(rule.get(key), str) and rule[key].strip()
                       for key in ("book_id_pattern", "chapter_url")):
                raise ValueError("章节 API 目录缺少 book_id_pattern 或 chapter_url")
            re.compile(rule["book_id_pattern"])
            if not rule["chapter_api"].startswith("/") or not rule["chapter_url"].startswith("/"):
                raise ValueError("章节 API 和章节链接必须使用站内绝对路径")
            rule["chapter_url"].format(book_id="1", chapter_id="1")
        return rule
    except (ValueError, TypeError, KeyError, re.error, soupsieve.SelectorSyntaxError) as exc:
        raise ValueError(f"站点插件 {filename} 格式错误：{exc}") from exc


def load(url):
    host = hostname(url)
    if urlsplit(url).scheme not in ("http", "https") or not host:
        raise ValueError("请输入完整的 HTTP 或 HTTPS 网页地址。")
    path = next((p for folder in (DIRECTORY, BUNDLED) for p in folder.glob("*.json") if p.stem.lower() == host), None)
    if path is None:
        raise UnsupportedSite(host)
    return validate(path.read_bytes(), path.name)


def install(source):
    path = Path(source)
    host = path.stem.lower().removeprefix("www.")
    if path.suffix.lower() != ".json" or not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z0-9-]+", host):
        raise ValueError("插件文件名须为站点域名加 .json，例如 books.example.org.json。")
    content = path.read_bytes()
    rule = validate(content, path.name)
    folder = plugin_folder()
    with tempfile.NamedTemporaryFile(dir=folder, delete=False) as f:
        temp = Path(f.name)
    try:
        temp.write_bytes(content)
        os.replace(temp, folder / (host + ".json"))
    finally:
        temp.unlink(missing_ok=True)
    return {"host": host, "name": rule["name"]}


def label(doc, selector):
    tag = doc.select_one(selector) if selector else None
    return tag.get_text(" ", strip=True) if tag else ""


def selected_label(doc, rule, selector):
    tag = doc.select_one(selector) if selector else None
    if not tag:
        return ""
    remove = rule.get("title_remove") if selector == rule.get("title") else None
    if remove:
        from bs4 import BeautifulSoup
        tag = BeautifulSoup(str(tag), "html.parser").find()
        for item in tag.select(remove):
            item.decompose()
    return tag.get_text(" ", strip=True)


def api_catalog(doc, url, rule, check, report):
    match = re.search(rule["book_id_pattern"], urlsplit(url).path)
    if not match:
        return None
    book_id = match.group(1)
    title = selected_label(doc, rule, rule["title"])
    author = label(doc, rule.get("author"))
    api_url = urljoin(url, rule["chapter_api"])
    if hostname(api_url) != hostname(url):
        raise ValueError("章节 API 必须位于作品所在网站。")
    check()
    try:
        response = requests.get(api_url, params={"book_id": book_id},
                                headers={"User-Agent": BROWSER_AGENT, "Referer": url,
                                         "Accept": "application/json, text/plain, */*"},
                                timeout=(8, 16))
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise ValueError(f"获取章节目录失败：{exc}") from exc
    data = payload.get("data", {}) if isinstance(payload, dict) else {}
    entries = data.get("chapters", []) if isinstance(data, dict) else []
    if not isinstance(entries, list) or not entries:
        raise ValueError("网站没有返回章节目录，请确认作品仍可访问。")
    ordered = []
    seen = set()
    for position, item in enumerate(entries):
        if not isinstance(item, dict):
            continue
        chapter_id, name = str(item.get("id", "")).strip(), str(item.get("title", "")).strip()
        if not chapter_id or not name or chapter_id in seen:
            continue
        seen.add(chapter_id)
        try:
            order = int(item.get("index", position))
        except (TypeError, ValueError):
            order = position
        chapter_path = rule["chapter_url"].format(book_id=book_id, chapter_id=chapter_id)
        chapter_url = urljoin(url, chapter_path)
        if hostname(chapter_url) != hostname(url):
            raise ValueError("章节链接必须位于作品所在网站。")
        chapter = {"name": name, "url": chapter_url}
        locked_field = rule.get("chapter_lock_field")
        if locked_field and str(item.get(locked_field, "")).strip().lower() in ("1", "true", "yes", "vip"):
            chapter["locked"] = True
        ordered.append((order, position, chapter))
    chapters = [item[2] for item in sorted(ordered, key=lambda item: (item[0], item[1]))]
    if not title or not chapters:
        raise ValueError("插件未识别到书名或章节，请检查目录页。")
    report(1, len(chapters))
    return {"title": title, "author": author, "base": url, "chapters": chapters,
            "adapter": rule["name"], "_rule": rule}


def catalog(doc, url, rule, fetch, check, report):
    if rule.get("chapter_api"):
        result = api_catalog(doc, url, rule, check, report)
        if result:
            return result

    source_doc = doc
    home = url
    catalog_link = doc.select_one(rule["catalog_link"]) if rule.get("catalog_link") else None
    if catalog_link and catalog_link.get("href"):
        dest = urldefrag(urljoin(url, catalog_link["href"]))[0]
        if hostname(dest) != hostname(url):
            raise ValueError("目录链接跳转至其他网站。")
        if dest != url:
            doc, home = fetch(dest)
    elif rule.get("catalog") and doc.select_one(rule["catalog"]) and rule.get("first_page_suffix"):
        parts = urlsplit(url)
        root = re.sub(rule["first_page_suffix"], "/", parts.path)
        if parts.path != root:
            doc, home = fetch(parts._replace(path=root, fragment="").geturl())
    author = label(doc, rule.get("author")) or label(source_doc, rule.get("author"))
    chapters = []
    if rule.get("catalog") and doc.select_one(rule["catalog"]):
        title = selected_label(doc, rule, rule["title"]) or selected_label(source_doc, rule, rule["title"])
        seen_pages, seen_links = set(), set()
        current = home
        while True:
            check()
            if current in seen_pages or hostname(current) != hostname(home):
                raise ValueError("目录分页循环或跳转至其他网站。")
            seen_pages.add(current)
            nodes = doc.select(rule["links"])
            if not nodes:
                raise ValueError(f"第 {len(seen_pages)} 页未匹配到章节，请检查站点插件。")
            for node in nodes:
                dest = urldefrag(urljoin(current, node.get("href", "")))[0]
                name = node.get_text(" ", strip=True)
                if rule.get("ignore_links") and re.search(rule["ignore_links"], name):
                    continue
                if name and hostname(dest) == hostname(home) and dest not in seen_links and node.get("href"):
                    seen_links.add(dest)
                    chapters.append({"url": dest, "name": name})
            report(len(seen_pages), len(chapters))
            older = doc.select_one(rule["older"]) if rule.get("older") else None
            if not older:
                break
            dest = urldefrag(urljoin(current, older["href"]))[0]
            if dest in seen_pages or hostname(dest) != hostname(home):
                raise ValueError("目录分页地址无效。")
            time.sleep(.35)
            check()
            doc, current = fetch(dest)
        if rule.get("order") == "reverse":
            chapters.reverse()
    else:
        title = label(doc, rule["chapter_title"])
        if not title or not doc.select_one(rule["body"]):
            raise ValueError("该插件支持小说分类目录页和文章正文页，请检查网址。")
        chapters.append({"name": title, "url": home})
    if not title or not chapters:
        raise ValueError("插件未识别到书名或章节，请检查目录页。")
    return {"title": title, "author": author, "base": home, "chapters": chapters,
            "adapter": rule["name"], "_rule": rule}


def ciweimao_content(doc, source):
    script = next((item.get_text() for item in doc.select("script")
                   if "chapter_is_paid" in item.get_text()), "")
    chapter = re.search(r"chapter_id\s*:\s*(\d+)", script)
    paid = re.search(r"chapter_is_paid\s*:\s*(\d+)", script)
    if not chapter or not paid:
        raise ValueError("刺猬猫没有提供章节读取信息，无法收录正文。")
    if paid.group(1) != "0":
        raise ValueError("该刺猬猫章节标记为付费章节，拾卷不会绕过站点权限。")

    from bs4 import BeautifulSoup
    from Crypto.Cipher import AES

    parts = urlsplit(source)
    root = f"{parts.scheme}://{parts.netloc}/"
    session = requests.Session()
    session.headers.update({"User-Agent": BROWSER_AGENT, "Referer": source,
                            "X-Requested-With": "XMLHttpRequest"})
    try:
        access_response = session.get(urljoin(root, "chapter/ajax_get_session_code"),
                                      params={"chapter_id": chapter.group(1)}, timeout=(8, 16))
        access_response.raise_for_status()
        access_data = access_response.json()
        access_key = access_data.get("chapter_access_key") if isinstance(access_data, dict) and access_data.get("code") == 100000 else None
        if not access_key:
            raise ValueError(access_data.get("tip") or "站点未授权读取该章节。")
        content_response = session.get(urljoin(root, "chapter/get_book_chapter_detail_info"),
                                       params={"chapter_id": chapter.group(1),
                                               "chapter_access_key": access_key}, timeout=(8, 16))
        content_response.raise_for_status()
        data = content_response.json()
    except (requests.RequestException, ValueError) as exc:
        raise ValueError(f"获取刺猬猫章节正文失败：{exc}") from exc
    if not isinstance(data, dict) or data.get("code") != 100000 or not data.get("chapter_content"):
        tip = data.get("tip") if isinstance(data, dict) else None
        raise ValueError(tip or "刺猬猫没有授权读取该章节正文。")
    keys = data.get("encryt_keys") or []
    if len(keys) < 2:
        raise ValueError("刺猬猫返回的正文格式无法识别。")
    indices = (ord(access_key[-1]) % len(keys), ord(access_key[0]) % len(keys))
    try:
        raw = base64.b64decode(data["chapter_content"], validate=True)
        plain = b""
        for index, key_index in enumerate(indices):
            if index:
                raw = base64.b64decode(plain, validate=True)
            if len(raw) <= 16 or (len(raw) - 16) % AES.block_size:
                raise ValueError("加密正文长度异常。")
            decrypted = AES.new(base64.b64decode(keys[key_index]), AES.MODE_CBC, raw[:16]).decrypt(raw[16:])
            padding = decrypted[-1]
            if padding < 1 or padding > AES.block_size or decrypted[-padding:] != bytes([padding]) * padding:
                raise ValueError("正文校验失败。")
            plain = decrypted[:-padding]
        markup = plain.decode("utf-8")
    except (ValueError, TypeError, IndexError, UnicodeError) as exc:
        raise ValueError(f"刺猬猫正文解密失败：{exc}") from exc
    content = BeautifulSoup(markup, "html.parser")
    return content.select_one("body") or content


def paragraphs(doc, rule, lines, source=None):
    block = ciweimao_content(doc, source) if rule.get("parser") == "ciweimao" else doc.select_one(rule["body"])
    if not block:
        raise ValueError("插件未找到章节正文。")
    if rule.get("remove"):
        for item in block.select(rule["remove"]):
            item.decompose()
    result = lines(block)
    if not result:
        raise ValueError("章节正文为空。")
    return result
