"""Declarative, hostname-scoped site adapters; files are reloaded on each import."""
import json
import re
import os
import tempfile
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit, urljoin, urldefrag

import soupsieve

DEFAULTS = Path(__file__).resolve().parent / "plugins"
DIRECTORY = Path(sys.executable).parent / "plugins" if getattr(sys, "frozen", False) else DEFAULTS


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
        if rule.get("parser", "css") != "css" or rule.get("order", "document") not in ("reverse", "document"):
            raise ValueError("未知的解析方式或章节顺序")
        for key in ("catalog", "title", "links", "chapter_title", "body"):
            if not isinstance(rule.get(key), str) or not rule[key].strip():
                raise ValueError(f"缺少字段 {key}")
        for key in ("catalog", "title", "links", "older", "chapter_title", "body", "remove", "author"):
            if rule.get(key):
                soupsieve.compile(rule[key])
        if rule.get("first_page_suffix"):
            re.compile(rule["first_page_suffix"])
        return rule
    except (ValueError, TypeError, re.error, soupsieve.SelectorSyntaxError) as exc:
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


def catalog(doc, url, rule, fetch, check, report):
    home = url
    if doc.select_one(rule["catalog"]) and rule.get("first_page_suffix"):
        parts = urlsplit(url)
        root = re.sub(rule["first_page_suffix"], "/", parts.path)
        if parts.path != root:
            doc, home = fetch(parts._replace(path=root, fragment="").geturl())
    author = label(doc, rule.get("author"))
    chapters = []
    if doc.select_one(rule["catalog"]):
        title = label(doc, rule["title"])
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


def paragraphs(doc, rule, lines):
    block = doc.select_one(rule["body"])
    if not block:
        raise ValueError("插件未找到章节正文。")
    if rule.get("remove"):
        for item in block.select(rule["remove"]):
            item.decompose()
    result = lines(block)
    if not result:
        raise ValueError("章节正文为空。")
    return result
