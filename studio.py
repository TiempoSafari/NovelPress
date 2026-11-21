import base64
import os
import re
import tempfile
import threading
from pathlib import Path

import updates
import site_plugins
import webview
from book import analyze, write_book


class Studio:
    def __init__(self):
        self._book = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._progress = {"done": 0, "chapter": ""}

    def inspect(self, url):
        try:
            with self._lock:
                self._book = None
                self._stop.clear()
                self._progress = {"done": 0, "chapter": ""}
                def report(pages, count):
                    self._progress = {"done": pages, "chapter": f"已读取 {pages} 页，共 {count} 篇"}
                self._book = analyze(url, self._check, report)
                return {k: v for k, v in self._book.items() if k != "_rule"}
        except site_plugins.UnsupportedSite as e:
            return {"error": str(e), "unsupported": e.host}
        except InterruptedError:
            return {"error": "已取消解析。"}
        except Exception as e:
            return {"error": str(e)}

    def _check(self):
        if self._stop.is_set():
            raise InterruptedError()

    def open_plugins(self):
        os.startfile(site_plugins.plugin_folder())

    def install_plugin(self):
        try:
            selection = webview.windows[0].create_file_dialog(webview.FileDialog.OPEN, allow_multiple=False, file_types=("拾卷站点插件 (*.json)",))
            if not selection:
                return {"cancelled": True}
            return site_plugins.install(selection if isinstance(selection, str) else selection[0])
        except Exception as e:
            return {"error": str(e)}

    def check_updates(self):
        try:
            return updates.latest()
        except Exception:
            return {"message": "暂时无法连接 GitHub，请稍后重试。"}

    def download_update(self, version):
        updates.download(version)

    def progress(self):
        return self._progress

    def cancel(self):
        self._stop.set()

    def export(self, title, author, indices, picture):
        if not self._lock.acquire(blocking=False):
            return {"error": "请等待当前操作完成。"}
        tmp = None
        try:
            self._stop.clear()
            self._progress = {"done": 0, "chapter": ""}
            selected = [self._book["chapters"][i] for i in sorted(set(indices))]
            if not selected or not title.strip():
                raise ValueError("请填写书名并选择章节。")
            filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title)[:100].rstrip(" .") or "小说"
            target = webview.windows[0].create_file_dialog(webview.FileDialog.SAVE, save_filename=filename + ".epub", file_types=("电子书 (*.epub)",))
            if not target:
                return {"cancelled": True}
            dest = Path(target if isinstance(target, str) else target[0])
            if dest.suffix.lower() != ".epub":
                dest = Path(str(dest) + ".epub")
                if dest.exists():
                    raise ValueError("同名 EPUB 已存在，请重新选择文件名。")
            with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False, suffix=".part") as file:
                tmp = Path(file.name)

            def check():
                if self._stop.is_set():
                    raise InterruptedError()

            def report(done, chapter):
                self._progress = {"done": done, "chapter": chapter}

            write_book(tmp, title, author, selected, base64.b64decode(picture), report, check, self._book.get("_rule"))
            check()
            os.replace(tmp, dest)
            return {"path": str(dest)}
        except InterruptedError:
            return {"cancelled": True}
        except Exception as e:
            return {"error": str(e)}
        finally:
            if tmp:
                tmp.unlink(missing_ok=True)
            self._lock.release()


if __name__ == "__main__":
    from ctypes import windll
    windll.shell32.SetCurrentProcessExplicitAppUserModelID("NovelPress.Studio")
    page = Path(__file__).with_name("studio.html").read_text(encoding="utf-8-sig")
    webview.create_window("拾卷 · 网页小说制书工具", html=page, js_api=Studio(), width=1100, height=790, min_size=(760, 600), background_color="#f7f8f4", text_select=True)
    webview.start(gui="edgechromium", private_mode=True)
