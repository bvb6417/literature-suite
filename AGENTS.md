# 使用文献工作台

- 从项目根目录的 `literature_suite.py` 调用 CLI；图形界面入口为 `main.py`。
- 检索、补全元数据和下载全文分别报告，检索到 DOI 不代表获得全文。
- 下载默认保存到程序目录下的 `downloads/`；保留缓存，读取返回结果的 `source` 和 `path`。
- 不打印、不提交 `config.local.json`、账号密码、API Key、Cookie、浏览器配置或用户数据库。
- 仅使用开放全文、官方 API 及用户已有机构授权；验证码和多因素认证由用户完成。
- 自动填表仅用于有适配规则的学校；不得重复尝试错误密码。
- 运行回归检查：`python -B -m unittest discover -s tests -v`。
- 提交时遵循 `.gitignore`，不得把个人数据或构建缓存加入版本控制。
