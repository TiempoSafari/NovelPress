"""Public stable-release checks. Windows Installer owns upgrades."""
from pathlib import Path
import re
import hashlib
import os
import subprocess
import tempfile

import requests

VERSION = Path(__file__).with_name("VERSION").read_text(encoding="ascii").strip()
REPOSITORY = "TiempoSafari/NovelPress"
BASE = "https://github.com/" + REPOSITORY


def latest(include_asset=False):
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
    asset = next((a for a in release["assets"] if a["name"] == name and a.get("state") == "uploaded"), None)
    if asset is None:
        return {"message": "新版安装包尚未发布。"}
    result = {"version": version, "message": f"拾卷 {version} 可更新。"}
    if include_asset:
        result["asset"] = asset
    return result


def download(version, report=lambda received, total, phase: None):
    info = latest(include_asset=True)
    if info.get("version") != version or not info.get("asset"):
        raise ValueError("发布信息已变化，请重新检查更新。")
    asset = info["asset"]
    digest = asset.get("digest") or ""
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("安装包缺少 GitHub SHA-256 校验值。")
    folder = Path(os.environ["LOCALAPPDATA"]) / "NovelPress" / "updates"
    folder.mkdir(parents=True, exist_ok=True)
    address = f"{BASE}/releases/download/v{version}/{asset['name']}"
    total = asset["size"]
    report(0, total, "正在下载安装包")
    hasher = hashlib.sha256()
    with tempfile.NamedTemporaryFile(dir=folder, delete=False, suffix=".part") as f:
        temp = Path(f.name)
    try:
        with requests.get(address, stream=True, timeout=(8, 30)) as r, temp.open("wb") as out:
            r.raise_for_status()
            for block in r.iter_content(32 * 1024):
                out.write(block)
                hasher.update(block)
                if out.tell() > asset["size"]:
                    raise ValueError("安装包大小异常。")
                report(out.tell(), total, "正在下载安装包")
        report(temp.stat().st_size, total, "正在核对 SHA-256")
        if temp.stat().st_size != asset["size"] or hasher.hexdigest() != digest[7:]:
            raise ValueError("安装包校验失败，请重试。")
        target = folder / asset["name"]
        os.replace(temp, target)
        subprocess.Popen([str(Path(os.environ["SystemRoot"]) / "System32" / "msiexec.exe"), "/i", str(target)])
    finally:
        temp.unlink(missing_ok=True)
