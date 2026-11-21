# 拾卷 NovelPress

在 Windows 上将支持站点的小说章节整理成带封面和目录的 EPUB 3。可选择章节、修改书名与作者，并通过 JSON 插件扩展站点。

内置 ESJ Zone、银龙诺艾尔和中文维基文库插件。ESJ 未向访客显示作品时，可在输入网址后点「登录 ESJ Zone」，通过站点自己的登录页登录，然后回到主窗口重新解析。登录状态仅用于本次运行。

章节可逐条改名，也可勾选后使用文字替换、正则替换或编号模板，一次预览并应用。网站封面可用时自动采用，也可从本地选择图片。目录和章标题均使用确认后的名称。

正文中的插图随 EPUB 一起保存，离线阅读时保留原有图文顺序。导出完成会显示已嵌入的图片数量；遇到无法下载的插图，会标明受影响的章节。

## 安装与更新

从 [GitHub Releases](https://github.com/TiempoSafari/NovelPress/releases/latest) 下载 x64 MSI，双击后选择当前用户可写的安装目录。从开始菜单启动「拾卷 NovelPress」。界面使用 Microsoft Edge WebView2 Runtime。

启动时和持续运行期间每 12 小时检查一次 GitHub 稳定版。也可以点「检查更新」。安装版发现新版后，点「更新并重启」，下载和校验完成后会自动退出、静默升级并在原目录重开。更新失败时会显示原因和日志位置。便携版可下载 MSI 转为安装版。用户安装的插件存放在 %LOCALAPPDATA%\NovelPress\plugins，优先于随安装包提供的规则。原便携版的自定义 JSON 可从「安装插件」重新导入。

## 开发和发布

Windows x64 开发环境使用 Python 3.13。安装 requirements.txt 后运行 python studio.py。添加站点可参照 [插件编写说明](plugins/插件编写说明.md)。

本地打包需要 PyInstaller 6、WiX CLI 5.0.2 及同版本 WixToolset.UI.wixext 扩展。运行 ./package.ps1，安装包位于 artifacts 目录。

发布时修改 VERSION 为三段数字版本号，提交后推送对应 vX.Y.Z 标签。GitHub Actions 会核对标签、构建 MSI 并发布稳定版 Release。客户端只提示高于本机的稳定版本。
