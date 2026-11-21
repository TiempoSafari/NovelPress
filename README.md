# 拾卷 NovelPress

在 Windows 上将支持站点的小说章节整理成带封面和目录的 EPUB 3。可选择章节、修改书名与作者，并通过 JSON 插件扩展站点。

## 安装与更新

从 [GitHub Releases](https://github.com/TiempoSafari/NovelPress/releases/latest) 下载 x64 MSI，双击后选择当前用户可写的安装目录。从开始菜单启动「拾卷 NovelPress」。界面使用 Microsoft Edge WebView2 Runtime。

启动时和持续运行期间每 12 小时检查一次 GitHub 稳定版。也可以点「检查更新」。发现新版后，在提示中点「下载安装包」，待浏览器下载完毕，关闭拾卷并运行 MSI。升级会沿用安装位置。用户安装的插件存放在 %LOCALAPPDATA%\NovelPress\plugins，优先于随安装包提供的规则。原便携版的自定义 JSON 可从「安装插件」重新导入。

## 开发和发布

Windows x64 开发环境使用 Python 3.13。安装 requirements.txt 后运行 python studio.py。添加站点可参照 [插件编写说明](plugins/插件编写说明.md)。

本地打包需要 PyInstaller 6、WiX CLI 5.0.2 及同版本 WixToolset.UI.wixext 扩展。运行 ./package.ps1，安装包位于 artifacts 目录。

发布时修改 VERSION 为三段数字版本号，提交后推送对应 vX.Y.Z 标签。GitHub Actions 会核对标签、构建 MSI 并发布稳定版 Release。客户端只提示高于本机的稳定版本。
