# 2026.11.09 补丁

- 下载页提示学校登录失效时，若已在本机保存该校账号密码，直接启动自动登录并复用，不再先弹出学校信息窗口；验证码仍需在学校页面完成。
- 本补丁仅修改源码，未重新构建 Windows 独立包。

## 使用建议

- Elsevier 近期风控较严，建议申请 Elsevier API Key（[Elsevier Developer Portal](https://dev.elsevier.com/)）和 OpenAlex API Key，优先走官方接口。
- 不建议依赖浏览器自动化或无头浏览器批量下载；回退到浏览器路径后速度慢、成功率低。
- 单日下载量建议控制在 1000 篇以内。

# 2026.11.08

## 本次修复

- 统一 GUI 与 CLI 的机构 Cookie 路径，解决界面登录成功后下载内核仍读取旧会话的问题。
- 识别 JSON 中已有的登录账号密码；登录弹窗两项留空可复用。
- 统一凭据检查和实际自动填表来源，避免旧 Windows 凭据覆盖显式 JSON 配置。
- 相对 Cookie 路径按程序目录解析；保留旧会话路径兼容。
- 增加 15 项离线回归检查，包括有效会话保存和无效登录保留原 Cookie。

- 保留远端已有的学校网关加密参数自动探测与西北大学参数修复，并增加 4 项兼容回归检查。

## 发布包

- 源码包：用于 GitHub 仓库，包含源码、公共学校接入参数、空配置模板、测试和许可证。
- Windows x64 独立包：`LiteratureSuite.exe`，无需安装 Python；配置和数据在 EXE 同目录生成。
- 不包含个人配置、账号密码、API Key、Cookie、浏览器资料、论文、译文、文献数据库、日志或旧备份。
- 未沿用开发者的界面截图；文档图片来自无个人配置的启动检查。

## 使用

源码版：安装 Python 3.10+ 和 `requirements.txt`，双击 `main.py`。
独立版：解压后双击 `LiteratureSuite.exe`。如需机构浏览器登录或 XML 重建，目标电脑还需 Chrome 或 Edge。
初次启动没有用户密钥或会话，在“全局配置”中按需填写。

## 构建

本次通过“py简洁打包”工具的 CLI 构建，使用独立单文件模式。将下面的工具路径替换为自己的安装位置：

```powershell
python "<打包工具目录>/main.py" build . --no-config --entry main.py --requirements packaging/build-requirements.txt --python 3.11.5 --pyinstaller-version 6.21.0 --name LiteratureSuite --mode onefile --windowed --preset compatible --cwd unchanged --extra-arg=--copy-metadata=requests --extra-arg=--copy-metadata=pypdf --extra-arg=--copy-metadata=websocket-client --extra-arg=--copy-metadata=pycryptodome --extra-arg=--copy-metadata=pywin32
```

工具会自动发现 `src/` 并收集其学校库和主题资源。请始终从干净源码构建，不要添加 `config.local.json` 或运行数据。

## 本地验证

```powershell
python -B -m unittest discover -s tests -v
python -B main.py --help
python -B literature_suite.py check
```

验证应使用发布包的独立测试副本；运行生成的配置、数据库和日志不应再放回发布包。

构建时实际解析的版本见 `packaging/build-info.json` 和 `packaging/build-requirements.txt`。
