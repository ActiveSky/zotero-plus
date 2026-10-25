# zotero-plus

![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)

[English](README.md) | **简体中文**

一个通过命令行操作 Zotero 的工具集，基于
[`zotero-cli`](https://github.com/54yyyu/zotero-mcp)，封装为 agent skill。
它可以：把 arXiv 论文翻译成中文 PDF、识别预印本正式发表的会议/期刊并标注
CCF/CORE 等级、检查 arXiv 新版本、查找正式发表版与开放获取 PDF、统一附件
文件名和标题。

## 功能

| 分组 | 命令 | 作用 |
|---|---|---|
| 翻译 | `translate` `status` `download` `open` `login` `cookies` | 通过 hjfy.top 把 arXiv 论文翻译成中文 PDF 并附加 |
| 会议/期刊 | `venue` `venue-apply` `venue-undo` | Semantic Scholar → OpenReview → DBLP，附带 CCF/CORE 等级 |
| 工作流 | `arxiv-version` `find` `pdf` | arXiv 版本检查、正式发表版发现、开放获取 PDF |
| 附件整理 | `rename-title` `rename-zh` `rename-en` `titles` | 纯标题命名、标题与文件名对齐 |

所有操作都走 `zotero-cli`，不需要安装任何 Zotero 插件。

## 前置条件

- **Zotero** 正在运行，并开启本地 API。
- **`zotero-cli`** 在 `PATH` 中：

  ```bash
  uv tool install zotero-mcp-server
  ```

- **Python 3.10+**。多数命令只用标准库。`venue-apply`、`venue-undo` 和改名
  命令会直接读取 `zotero.sqlite`，因此需要 zotero-cli 所属的 Python 解释器；
  脚本会依次尝试 `ZOTERO_MCP_PYTHON`、`uv tool dir`、pipx。

## 安装

把本目录复制或链接到 agent 的 skills 目录：

```text
~/.agents/skills/zotero-plus      （通用 agent skills）
~/.claude/skills/zotero-plus      （Claude Code）
```

然后直接运行入口脚本：

```bash
python scripts/zotero_plus.py --help
```

`SKILL.md` 是给 agent 的指令；每个命令也支持 `--help`。

## 用法

### 翻译 arXiv 论文

```bash
python scripts/zotero_plus.py translate --key DMI9KNFZ
python scripts/zotero_plus.py status 2503.24000
python scripts/zotero_plus.py translate --arxiv-id 2503.24000 --no-attach --out-dir ./pdfs
```

有 LaTeX 源码的论文匿名即可翻译。没有源码时需要上传本地 PDF，这一步需要
hjfy.top 登录；cookie 依次从 `~/.config/arxiv2zh/cookies.txt`、`--cookie`、
Zotero 的 cookie jar 读取。

### 识别会议/期刊与 CCF/CORE

```bash
python scripts/zotero_plus.py venue --keys DMI9KNFZ
python scripts/zotero_plus.py venue --query "KV cache" --limit 20
python scripts/zotero_plus.py venue-apply --plan state/plan-*.json --yes
python scripts/zotero_plus.py venue-undo --yes
```

`venue` 只生成计划。`venue-apply` 写入前会生成快照，更新条目类型、venue
字段、出版社、DOI、Extra 和标签（`venue:…`、`year:…`、`CCF:…`、`CORE:…`、
`acceptance:…`、`cite:…`）。

### 工作流

```bash
python scripts/zotero_plus.py arxiv-version --keys C2I975UC
python scripts/zotero_plus.py arxiv-version --collection RBYJTLIG --download
python scripts/zotero_plus.py find --keys IGEDXRM8
python scripts/zotero_plus.py pdf --keys IGEDXRM8 --attach
```

- `arxiv-version` 读取本地 PDF 第一页的 `arXiv:xxxxvN`，与 arXiv 线上版本
  比较；`--download` 会下载新版并附加。
- `find` 依次查询 arXiv `data-doi`、Semantic Scholar、DBLP、PubMed、
  Crossref，全程只读。
- `pdf` 依次尝试 Unpaywall、Semantic Scholar、OpenAlex、arXiv，只能获取
  开放获取版本。

### 附件命名

```bash
python scripts/zotero_plus.py rename-title
python scripts/zotero_plus.py rename-title --apply    # 需关闭 Zotero
python scripts/zotero_plus.py titles
python scripts/zotero_plus.py titles --english --added 2026-10-24 --apply
```

命名规则：`英文 PDF = <标题>.pdf`，`中文 PDF = <标题>_zh_CN.pdf`，同一篇的
多份附件加 ` (2)`、` (3)`。Windows 不允许的字符会被替换（`:` 变成 ` -`）。

`rename-*` 会修改 `zotero.sqlite` 和 storage 目录，写入前自动备份数据库到
`state/`，并且要求关闭 Zotero。

## 数据来源

| 来源 | 用途 |
|---|---|
| hjfy.top | 中文翻译 |
| Semantic Scholar | 会议/期刊、正式 DOI、开放 PDF |
| OpenReview | 会议/期刊回退 |
| DBLP | 会议/期刊、正式 DOI |
| Crossref | 正式 DOI |
| PubMed | 正式 DOI |
| Unpaywall / OpenAlex | 开放获取 PDF |
| arXiv | 版本检查、PDF |

## 安全说明

- `find`、`venue`、`status`、`download` 均为只读。
- `venue-apply` 和 `rename-*` 写入前都会快照或备份。
- 不做条目合并，也不做超出 `venue-apply` 显式范围的类型迁移。
- cookie 不会提交到仓库，除请求头外不会离开本机。

## 上游项目

本项目是移植，不是 fork。行为和数据的来源：

| 部分 | 上游 | 许可证 |
|---|---|---|
| `scripts/translate/arxiv2zh.py` | [kongyan66/arxiv2zh](https://github.com/kongyan66/arxiv2zh) | AGPL-3.0 |
| `scripts/venue/arxiv_marker.py` | [lelelelelelelelelelelelele/arxiv-marker](https://github.com/lelelelelelelelelelelelele/arxiv-marker) | MIT |
| `data/rankings.json` | arxiv-marker (`zm-data.js`) | MIT |
| `data/ccf.json` | [GroundbreakerLhy/CCF-Rank](https://github.com/GroundbreakerLhy/CCF-Rank) | AGPL-3.0 |
| `scripts/workflow/` | [AllanChain/zotero-arxiv-workflow](https://github.com/AllanChain/zotero-arxiv-workflow) | AGPL-3.0 |

## 许可证

**AGPL-3.0-or-later。** 本项目部分代码和数据来自 AGPL-3.0 项目，因此整体以
相同许可证发布。详见 [`LICENSE`](LICENSE)。

## 贡献

欢迎提交 issue 和 PR。请阅读 [`CONTRIBUTING.md`](CONTRIBUTING.md) 和
[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)。安全问题请按
[`SECURITY.md`](SECURITY.md) 的方式报告。

## 免责声明

非官方工具，与 Zotero、arXiv、hjfy.top 及上述任何服务均无隶属关系。第三方
服务的可用性和条款由它们自己决定。
