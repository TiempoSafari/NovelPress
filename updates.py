"""Public stable-release checks. Windows Installer owns upgrades."""
from pathlib import Path
import re
import webbrowser

import requests

VERSION = Path(__file__).with_name("VERSION").read_text(encoding="ascii").strip()
REPOSITORY = "TiempoSafari/NovelPress"
BASE = "https://github.com/" + REPOSITORY


def latest():
    with requests.get(f"https://api.github.com/repos/{REPOSITORY}/releases/latest",
                      headers={"User-Agent": f"NovelPress/{VERSION}"}, timeout=(5, 12)) as response:
        if response.status_code == 404:
            return {"message": "尚无公开的稳定版。"}
        response.raise_for_status()
        release = response.json()
    tag = release["tag_name"]
    if release["draft"] or release["prerelease"] or not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        return {"message": "尚无可安装的稳定版。"}
    version = tag[1:]
    if tuple(map(int, version.split("."))) <= tuple(map(int, VERSION.split("."))):
        return {"message": f"已是最新版本（{VERSION}）。"}
    name = f"NovelPress-{version}-x64.msi"
    if not any(a["name"] == name and a.get("state") == "uploaded" for a in release["assets"]):
        return {"message": "新版安装包尚未发布。"}
    return {"version": version, "message": f"拾卷 {version} 可更新。"}


def download(version):
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("无效的版本号")
    webbrowser.open(f"{BASE}/releases/download/v{version}/NovelPress-{version}-x64.msi")
