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
| PDF 书签 | `outline` | 从正文重建三级目录；`--jasminum` 顺带补齐茉莉花的 `jasminum-outline.json` |

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

### 使用 skills CLI

```bash
npx skills add ActiveSky/zotero-plus
```

全局安装到指定 agent（而不是当前项目）：

```bash
npx skills add ActiveSky/zotero-plus -g -a claude-code
```

以后升级用 `npx skills update zotero-plus`。

### 手动安装

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
python scripts/zotero_plus.py translate --key DMI9KNFZ --local
python scripts/zotero_plus.py config
python scripts/zotero_plus.py account
```

`account` 检查 cookie 是否还有效，并打印页面余额。网页 cookie 没有续期机制：
hjfy.top 不会在 API 请求里重发 cookie，过期后接口直接返回 `LOGIN_REQUIRED`，
只能重新登录再导入。

一个配置文件 `~/.config/arxiv2zh/config.json` 驱动两种引擎：

| | `web` | `local` |
|---|---|---|
| 引擎 | hjfy.top 云端 | 本机 `hjfy-pdf` |
| 认证 | `web.cookies` | `local.api_key` + `local.model` |
| 计费 | 页面余额；未解锁只给预览 | 自己的 API 用量 |

未解锁时 hjfy.top 只给**前 5 页预览**，而预览本身是个合法 PDF，不校验就会
被当成译文挂到条目上。`translate` 和 `download` 现在会直接停下，报出译文总
页数和解锁价格，不再静默保存。加 `--unlock` 才会从账户余额里扣掉报价页数，
然后下载完整文件。

`--local` 用 `local.base_url` / `local.model` 跑本机引擎，没有页面余额也没有
预览。`local.engine` 留空时会自动使用最新的
`%LOCALAPPDATA%\hjfy-engine\runtime-*`。该运行时需要 MinGW 的
`libstdc++-6`、`libgcc_s_seh-1`、`libwinpthread-1` 三个 DLL，放在
`<runtime>\deps\` 下即可；引擎的 `PATH` 由 `bin`、`deps`、`lib` 和
`System32\downlevel` 组成。

`local.thinking_mode` 决定用哪种方式告诉网关关闭推理，各家字段名不一样：
`reasoning-none` 发 `reasoning_effort: "none"`（OpenAI 系），
`thinking-disabled` 发 `thinking: {"type": "disabled"}`（Anthropic / 方舟 /
GLM），`enable-thinking-false` 发 `enable_thinking: false`（Qwen / vLLM），
`service-default` 不加任何字段。推理模型开着思考时每段要慢好几倍。

引擎有 126 个参数，常用的一部分做成了开关，其余走 `--engine-arg`：

```bash
python scripts/zotero_plus.py translate --key KEY --local --dry-run       # 只打印命令
python scripts/zotero_plus.py translate --key KEY --local --pages 1-10
python scripts/zotero_plus.py translate --key KEY --local --bilingual
python scripts/zotero_plus.py translate --key KEY --local --output-format=pdf-docx
python scripts/zotero_plus.py translate --key KEY --local --engine-arg=--min-font-scale=0.8
```

优先级 `local.extra_args` → 具名开关 → `--engine-arg`，后者覆盖前者。脚本自己
设置的参数（`--output`、`--api-key-env`、`--model`、`--base-url` 等）会被拒绝，
而不是静默忽略。

**表格默认不翻译**：`local.translate_tables` 默认 `off`，结果表的单元格保持
原文——机翻数字表的风险大于收益。需要时改成 `auto`（只翻以文字为主的表）或
`on`，也可以单次用 `--translate-tables=auto`。

### 识别会议/期刊与 CCF/CORE

```bash
python scripts/zotero_plus.py venue --keys DMI9KNFZ
python scripts/zotero_plus.py venue --query "KV cache" --limit 20
python scripts/zotero_plus.py venue-apply --plan ~/.cache/zotero-plus/plan-*.json --yes
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
python scripts/zotero_plus.py rename-title --keys DC8UEKWS
python scripts/zotero_plus.py rename-title --attachment-keys G7NBDFSE
python scripts/zotero_plus.py titles
python scripts/zotero_plus.py titles --english --added 2026-10-24 --apply
```

命名规则：`英文 PDF = <标题>.pdf`，`中文 PDF = <标题>_zh_CN.pdf`，同一篇的
多份附件加 ` (2)`、` (3)`。Windows 不允许的字符会被替换（`:` 变成 ` -`）。

`--keys` 和 `--attachment-keys`（逗号分隔、大小写不敏感）把一次运行限定到
指定条目或附件，也覆盖没有中文译文的条目。key 一个都没匹配上时以非零码退出。

`rename-*` 会修改 `zotero.sqlite` 和 storage 目录，写入前自动备份数据库到
`~/.cache/zotero-plus/backups/`（只保留最新 3 份），并且要求关闭 Zotero。
`ZOTERO_PLUS_STATE_DIR` 可以改这个位置。skill 目录内不写任何运行时数据，因为
`npx skills` 更新时会整个替换掉该目录。

### PDF 书签

```bash
python scripts/zotero_plus.py outline --keys DMI9KNFZ            # 预览
python scripts/zotero_plus.py outline --keys DMI9KNFZ --apply    # 写进 PDF
python scripts/zotero_plus.py outline --keys DMI9KNFZ --apply --jasminum
python scripts/zotero_plus.py outline --attachment-keys BI78XU9F --apply
python scripts/zotero_plus.py outline --path paper.pdf --apply
```

从正文里识别 `1` / `2.1` / `3.1.2` 编号标题、`A 评估细节` 这类附录、以及
`Abstract`、`参考文献` 这种无编号大字标题，按点号层数定级；编号和标题被 LaTeX
拆成两行时会合并，跨行标题也会接回去。单栏、双栏都处理，默认最多三级，
`--max-level 2` 可以只要到二级。已有书签的 PDF 除非加 `--force`，否则不动。

`--apply` 先把 PDF 备份到 `~/.cache/zotero-plus/pdf-backups/`（每个文件保留最新
3 份）再原地替换；Zotero 阅读器要是正开着这篇，Windows 会占用文件句柄，需要先
关掉该标签页。扫描版没有文字层，得先 OCR。

`--jasminum` 会顺带写 PDF 同目录的 `jasminum-outline.json`（茉莉花插件的
`{level, title, page, x, y, children, collapsed}` 结构）。这个开关有实际意义：
茉莉花第一次打开附件时若 PDF 没有书签，会把空数组写进这个文件，之后**永远**只读
缓存、不再解析 PDF——所以它的“大纲”面板即使 PDF 已有书签也可能一直空着。
`--jasminum` 按 PDF 坐标系（左下原点）写 `y`，因为插件用 `goToDestination`
跳转，而不是它自己保存时用的视口坐标。

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
