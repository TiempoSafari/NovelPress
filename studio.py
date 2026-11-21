import base64
import os
import re
import tempfile
import threading
import json
from pathlib import Path

import book
import updates
import site_plugins
import webview
from book import analyze, write_book


class Studio:
    def __init__(self):
        self._update_progress = {"bytes": 0, "total": 0, "phase": "正在查询版本"}
        self._login = None
        self._book = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._progress = {"done": 0, "chapter": ""}
        self._settings = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "NovelPress" / "settings.json"

    def default_export_folder(self):
        try:
            data = json.loads(self._settings.read_text("utf-8"))
            folder = Path(data.get("export_folder", ""))
            return str(folder) if folder.is_dir() else ""
        except (OSError, ValueError):
            return ""

    def choose_export_folder(self):
        selection = webview.windows[0].create_file_dialog(webview.FileDialog.FOLDER)
        if not selection:
            return {"cancelled": True}
        folder = Path(selection if isinstance(selection, str) else selection[0]).resolve()
        if not folder.is_dir():
            return {"error": "请选择一个现有文件夹。"}
        self._settings.parent.mkdir(parents=True, exist_ok=True)
        self._settings.write_text(json.dumps({"export_folder": str(folder)}, ensure_ascii=False), encoding="utf-8")
        return {"path": str(folder)}

    def inspect(self, url):
        try:
            with self._lock:
                self._book = None
                self._stop.clear()
                self._progress = {"done": 0, "chapter": ""}
                def report(pages, count):
                    self._progress = {"done": pages, "chapter": f"已读取 {pages} 页，共 {count} 篇"}
                if self._login and site_plugins.hostname(url) == "esjzone.cc":
                    self._sync_login()
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
            info = updates.latest()
            info["restart"] = updates.installed_application() is not None
            return info
        except Exception:
            return {"message": "暂时无法连接 GitHub，请稍后重试。"}

    def download_update(self, version):
        if not self._lock.acquire(blocking=False):
            return "请先完成当前制书操作。"
        try:
            def report(received, total, phase):
                self._update_progress = {"bytes": received, "total": total, "phase": phase}
            report(0, 0, "正在查询版本")
            package, digest = updates.download(version, report)
            exe = updates.installed_application()
            if exe is None:
                os.startfile(package)
                return "已打开安装向导。"
            updates.apply(package, digest, exe)
            for window in reversed(webview.windows.copy()):
                window.destroy()
            return "正在重启拾卷…"
        except Exception as e:
            return f"未能启动更新：{e}"
        finally:
            self._lock.release()

    def update_progress(self):
        return self._update_progress

    def window_action(self, action):
        from ctypes import windll
        window = webview.windows[0]
        hwnd = window.native.Handle.ToInt64()
        if action == "close":
            window.destroy()
        elif action == "minimize":
            window.minimize()
        elif action == "maximize":
            if windll.user32.IsZoomed(hwnd):
                window.restore()
            else:
                from System.Windows.Forms import Screen
                window.native.MaximizedBounds = Screen.FromHandle(window.native.Handle).WorkingArea
                window.maximize()
        return bool(windll.user32.IsZoomed(hwnd))

    def resize_edge(self, edge):
        from ctypes import windll
        edges = {"w": 1, "e": 2, "n": 3, "nw": 4, "ne": 5, "s": 6, "sw": 7, "se": 8}
        hwnd = webview.windows[0].native.Handle.ToInt64()
        if edge in edges and not windll.user32.IsZoomed(hwnd):
            # WM_SYSCOMMAND / SC_SIZE hands the drag to Windows' sizing loop.
            windll.user32.PostMessageW(hwnd, 0x0112, 0xF000 + edges[edge], 0)

    def esj_login(self):
        if self._login is None:
            window = webview.create_window("登录 ESJ Zone · 登录后回到拾卷重新解析",
                                           "https://www.esjzone.cc/my/login", width=900, height=700)
            self._login = window
            def clear_window():
                self._login = None
            window.events.closed += clear_window
            # Capture only this site's cookies on completed navigation, including after login.
            window.events.loaded += self._sync_login
        else:
            self._login.show()

    def _sync_login(self):
        window = self._login
        if not window or site_plugins.hostname(window.get_current_url() or "") != "esjzone.cc":
            return
        jar = book.requests.cookies.RequestsCookieJar()
        for cookie in window.get_cookies() or []:
            for key, morsel in cookie.items():
                domain = morsel["domain"].lstrip(".").lower()
                if domain in ("www.esjzone.cc", "esjzone.cc"):
                    jar.set(key, morsel.value, domain=domain, path=morsel["path"] or "/", secure=True)
        book.esj_cookies = jar

    def progress(self):
        return self._progress

    def cancel(self):
        self._stop.set()

    def export(self, title, author, indices, picture, names=None):
        if not self._lock.acquire(blocking=False):
            return {"error": "请等待当前操作完成。"}
        tmp = None
        try:
            self._stop.clear()
            self._progress = {"done": 0, "chapter": ""}
            selected = [self._book["chapters"][i] for i in sorted(set(indices))]
            if any(ch.get("locked") for ch in selected):
                raise ValueError("所选目录包含站点标记为 VIP 的章节。拾卷不会绕过会员权限，请取消这些章节的选择。")
            if names is not None:
                selected = [dict(ch, source_name=ch["name"], name=names[str(i)].strip())
                            for i, ch in zip(sorted(set(indices)), selected)]
                if any(not ch["name"] for ch in selected):
                    raise ValueError("章节名不能为空。")
            if not selected or not title.strip():
                raise ValueError("请填写书名并选择章节。")
            filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title)[:100].rstrip(" .") or "小说"
            folder = self.default_export_folder()
            if folder:
                dest = Path(folder) / (filename + ".epub")
                if dest.exists():
                    from datetime import datetime
                    dest = Path(folder) / (filename + " " + datetime.now().strftime("%Y%m%d %H%M%S") + ".epub")
            else:
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

            collected = 0
            last_title = ""

            def chapter_ready(count, name):
                nonlocal collected, last_title
                collected, last_title = count, name
                report(count, f"已收录《{name}》")

            def report(done, chapter):
                self._progress = {"kind": "export", "done": done, "chapter": chapter,
                                  "collected": collected, "last_title": last_title}

            artwork = write_book(tmp, title, author, selected, base64.b64decode(picture), report, check, self._book.get("_rule"), chapter_ready)
            check()
            os.replace(tmp, dest)
            return {"path": str(dest), **artwork}
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
    updates.cleanup_helpers()
    page = Path(__file__).with_name("studio.html").read_text(encoding="utf-8-sig")
    icon = Path(__file__).with_name("book.ico")
    page = page.replace("__APP_ICON__", base64.b64encode(icon.read_bytes()).decode("ascii"))
    webview.create_window("拾卷 · 网页小说制书工具", html=page, js_api=Studio(), width=1100, height=790, min_size=(760, 600), frameless=True, easy_drag=False, shadow=True, background_color="#f7f8f4", text_select=True)
    webview.start(gui="edgechromium", private_mode=True, icon=str(icon))
