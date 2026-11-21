"""Artwork discovery and EPUB-local image resources."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import tempfile
import threading
import uuid
from urllib.parse import unquote_to_bytes, urldefrag, urljoin, urlsplit
from xml.sax.saxutils import quoteattr
from zipfile import ZIP_STORED

import requests
from PIL import Image

MAX_BYTES = 32 * 1024 * 1024
IMAGE_PATH = re.compile(r"\.(?:png|jpe?g|gif|webp|avif)(?:$)", re.I)


@dataclass(frozen=True)
class Illustration:
    sources: tuple
    referer: str
    alt: str


def srcset_urls(value):
    entries = []
    # A data URL contains its own comma; the descriptor follows the URL.
    for match in re.finditer(r"(data:[^,\s]+,[^\s,]+|[^,\s]+)(?:\s+(\d+(?:\.\d+)?)[wx])?\s*(?:,|$)", value):
        entries.append((float(match[2] or 1), match[1]))
    return [url for _, url in sorted(entries, reverse=True)]


def illustration(tag, base, referer):
    urls = []
    for attr in ("data-original", "data-src", "data-lazy-src", "data-original-src"):
        if tag.get(attr):
            urls.append(tag[attr])
    picture = tag.find_parent("picture")
    for source in [*(picture.find_all("source") if picture else []), tag]:
        for key in ("data-srcset", "srcset"):
            if source.get(key):
                urls.extend(srcset_urls(source[key]))
    if tag.get("src"):
        urls.append(tag["src"])
    addresses = tuple(dict.fromkeys(urldefrag(urljoin(base, u.strip()))[0] for u in urls if u.strip()))
    return Illustration(addresses, referer, tag.get("alt") or "章节插图")


def read_image(url, referer, cookies, check, session=None):
    scheme = urlsplit(url).scheme
    if scheme == "data":
        header, _, encoded = url.partition(",")
        if not header.lower().startswith("data:image/") or len(encoded) > MAX_BYTES * 2:
            raise ValueError("无效的内嵌图片")
        data = base64.b64decode(encoded) if ";base64" in header.lower() else unquote_to_bytes(encoded)
    elif scheme in ("http", "https"):
        # Send account cookies only for HTTPS requests to ESJ itself.
        jar = cookies if scheme == "https" and urlsplit(url).hostname in ("esjzone.cc", "www.esjzone.cc") else None
        client = session or requests
        with client.get(url, headers={"Referer": referer}, cookies=jar,
                        stream=True, timeout=(5, 18)) as reply:
            reply.raise_for_status()
            data = bytearray()
            for chunk in reply.iter_content(65536):
                check()
                data.extend(chunk)
                if len(data) > MAX_BYTES:
                    raise ValueError("图片超过 32 MB")
    else:
        raise ValueError("不支持的图片地址")
    if len(data) > MAX_BYTES:
        raise ValueError("图片超过 32 MB")
    with Image.open(BytesIO(data)) as picture:
        if picture.width * picture.height > 40_000_000:
            raise ValueError("图片尺寸过大")
        if picture.format in ("PNG", "JPEG", "GIF"):
            fmt = picture.format.lower()
            picture.verify()
            return bytes(data), fmt
        # Convert newer raster formats for readers without WebP/AVIF support.
        converted = BytesIO()
        picture.convert("RGBA").save(converted, format="PNG")
        return converted.getvalue(), "png"


class ImageArchive:
    def __init__(self, book, cookies, check, workers=4):
        self.book = book
        self.cookies = cookies
        self.check = check
        self.paths = {}
        self.manifest = {}
        self.missing = []
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="novelpress-image")
        self._temporary = tempfile.TemporaryDirectory(prefix="novelpress-images-")
        self._futures = {}
        self._local = threading.local()
        self._sessions = []
        self._sessions_lock = threading.Lock()
        self._progress_lock = threading.Lock()
        self._progress_done = 0
        self._progress_callback = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self._executor.shutdown(wait=True, cancel_futures=True)
        for session in self._sessions:
            session.close()
        self._temporary.cleanup()

    def _session(self):
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            self._local.session = session
            with self._sessions_lock:
                self._sessions.append(session)
        return session

    def _key(self, image):
        return image.sources, image.referer

    def prefetch(self, image):
        """Start downloading an image in the background, deduplicating repeated references."""
        key = self._key(image)
        if key not in self._futures:
            self._futures[key] = self._executor.submit(self._download, image)

    def _download(self, image):
        thread_cookies = self.cookies.copy() if self.cookies is not None else None
        for url in image.sources:
            self.check()
            try:
                data, fmt = read_image(url, image.referer, thread_cookies, self.check, self._session())
            except InterruptedError:
                raise
            except (requests.RequestException, ValueError, OSError, Image.DecompressionBombError, SyntaxError):
                continue
            self.check()
            digest = hashlib.sha256(data).hexdigest()
            path = Path(self._temporary.name) / (uuid.uuid4().hex + "." + fmt)
            path.write_bytes(data)
            return str(path), fmt, digest
        return None

    def set_progress_callback(self, callback):
        """Report completed image downloads once the complete denominator is known."""
        self._progress_callback = callback
        total = len(self._futures)
        if not total:
            return
        for future in self._futures.values():
            future.add_done_callback(lambda _future, count=total: self._image_finished(count))

    def _image_finished(self, total):
        with self._progress_lock:
            self._progress_done += 1
            done = self._progress_done
            if self._progress_callback:
                self._progress_callback(done, total)

    def render(self, image, chapter):
        self.check()
        key = self._key(image)
        path = self.paths.get(key)
        if path is None:
            future = self._futures.get(key)
            if future is None:
                self.prefetch(image)
                future = self._futures[key]
            try:
                result = future.result()
            except InterruptedError:
                raise
            except Exception:
                result = None
            if result:
                staged, fmt, digest = result
                path = f"images/{digest}.{fmt}"
                if path not in self.manifest:
                    with open(staged, "rb") as data:
                        self.book.writestr(path, data.read(), compress_type=ZIP_STORED)
                    self.manifest[path] = f'<item id="i{digest}" href="{path}" media-type="image/{fmt}"/>'
                self.paths[key] = path
                Path(staged).unlink(missing_ok=True)
            else:
                self.paths[key] = ""
        if path:
            alt = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]", "", image.alt)
            return f'<img src="{path}" alt={quoteattr(alt)}/>'
        self.missing.append(chapter)
        return '<span>〔插图未能下载〕</span>'
