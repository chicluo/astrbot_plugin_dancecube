# 贡献指南

感谢愿意帮忙。提 PR / Issue 之前，先看这几条。

## 许可证

本项目使用 [PolyForm Noncommercial 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0)，**禁止商业使用，不提供商业授权**。

提交贡献即视为你同意：你的贡献以同样的许可证授权给本项目（以及后续使用者）。请不要提交你无权授权的代码。

## 不要引入的依赖

本项目要保持在「宽松许可 + 非商用」的组合下可以干净分发，所以：

- ❌ 不要引入 GPL / AGPL / LGPL 等带传染性的库（早期用过的 `mutagen` 就是因为 GPL-2.0 被换成了 MIT 的 `tinytag`）；
- ✅ MIT / BSD / Apache-2.0 / ISC 这类宽松许可没问题；
- 新增依赖时，请在 PR 描述里写明它的许可证。

## 代码要求

- 插件层（`main.py`）只做框架翻译；业务逻辑放 `core/service.py`，`core/` 里不允许 `import astrbot`；
- 网络请求统一走 `core/net.py`（TLS 校验 + 域名白名单 + 大小上限），不要自己裸 `urlopen`；
- 写表格单元格前用 `core/safe.safe_cell()`，回显异常用 `core/safe.safe_error()`；
- 提交前跑一遍：

```bash
python -m py_compile main.py core/*.py
python -m unittest discover -s tests -t .
```

## 提 Issue

- 报错请附上 AstrBot 版本、平台（qq_official / aiocqhttp）、复现步骤和日志里的 `Traceback`；
- 涉及 token / openid 的内容请打码，别把凭证贴到公开 issue 里。
