# zotero-plus

![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)

**English** | [简体中文](README.zh-CN.md)

A Zotero toolbox that runs from the shell through
[`zotero-cli`](https://github.com/54yyyu/zotero-mcp), packaged as an agent
skill. It translates arXiv papers into Chinese PDFs, resolves preprints to
their published venue with CCF/CORE tiers, checks for newer arXiv versions,
finds published versions and open-access PDFs, and keeps attachment filenames
and titles consistent.

## Features

| Group | Commands | What it does |
|---|---|---|
| Translate | `translate` `status` `download` `open` `login` `cookies` | arXiv → Chinese PDF through hjfy.top, attached to the item |
| Venue | `venue` `venue-apply` `venue-undo` | Semantic Scholar → OpenReview → DBLP, plus CCF/CORE tiers |
| Workflow | `arxiv-version` `find` `pdf` | arXiv version checks, published-version discovery, open-access PDFs |
| Naming | `rename-title` `rename-zh` `rename-en` `titles` | Title-based filenames and consistent attachment titles |
| Bookmarks | `outline` | Rebuild a 3-level PDF outline from the text; `--jasminum` also fills the Jasminum sidecar |

Everything goes through `zotero-cli`; no Zotero plugin is required.

## Requirements

- **Zotero** running, with its local API enabled.
- **`zotero-cli`** on `PATH`:

  ```bash
  uv tool install zotero-mcp-server
  ```

- **Python 3.10+**. Most commands use only the standard library. `venue-apply`,
  `venue-undo`, and the renaming commands read `zotero.sqlite` directly and
  therefore need the Python that owns `zotero-cli`; the scripts find it via
  `ZOTERO_MCP_PYTHON`, `uv tool dir`, then pipx.

## Install

### With the skills CLI

```bash
npx skills add ActiveSky/zotero-plus
```

Install globally for one agent instead of the current project:

```bash
npx skills add ActiveSky/zotero-plus -g -a claude-code
```

Upgrade later with `npx skills update zotero-plus`.

### Manually

Clone or copy this directory into your agent's skills folder:

```text
~/.agents/skills/zotero-plus      (generic agent skills)
~/.claude/skills/zotero-plus      (Claude Code)
```

Then run the router directly:

```bash
python scripts/zotero_plus.py --help
```

`SKILL.md` contains the agent-facing instructions; each command also supports
`--help`.

## Usage

### Translate arXiv papers

```bash
python scripts/zotero_plus.py translate --key DMI9KNFZ
python scripts/zotero_plus.py status 2503.24000
python scripts/zotero_plus.py translate --arxiv-id 2503.24000 --no-attach --out-dir ./pdfs
python scripts/zotero_plus.py translate --key DMI9KNFZ --local
python scripts/zotero_plus.py config
python scripts/zotero_plus.py account
```

`account` checks that the cookies still work and prints the page balance. The
website cookie has no refresh flow: hjfy.top reissues nothing on API calls, so
when it expires the API answers `LOGIN_REQUIRED` and only a fresh sign-in fixes
it.

One config file, `~/.config/arxiv2zh/config.json`, drives both engines:

| | `web` | `local` |
|---|---|---|
| Engine | hjfy.top cloud | `hjfy-pdf` on this machine |
| Auth | `web.cookies` | `local.api_key` + `local.model` |
| Cost | page balance; locked papers are previews | your own API tokens |

A locked result is a **preview**: hjfy.top serves the first five pages until the
paper is unlocked, and the preview is a valid PDF that would otherwise be
attached as if it were the translation. `translate` and `download` stop with the
translated page count and the price rather than saving it. `--unlock` spends the
quoted pages from the account balance and then downloads the whole file.

`--local` translates with the hjfy-pdf engine and `local.base_url` /
`local.model`, so there is no page balance and no preview. Leave `local.engine`
empty to use the newest `%LOCALAPPDATA%\hjfy-engine\runtime-*`. That runtime
needs the MinGW DLLs (`libstdc++-6`, `libgcc_s_seh-1`, `libwinpthread-1`) in
`<runtime>\deps\`; the engine's `PATH` is built from `bin`, `deps`, `lib` and
`System32\downlevel`.

`local.thinking_mode` picks how to switch reasoning off, because each vendor
spells it differently: `reasoning-none` adds `reasoning_effort: "none"`
(OpenAI), `thinking-disabled` adds `thinking: {"type": "disabled"}`
(Anthropic/ARK/GLM), `enable-thinking-false` adds `enable_thinking: false`
(Qwen/vLLM), and `service-default` adds nothing. Reasoning models spend several
times longer per segment with thinking on.

The engine has ~126 options. The common ones are switches, the rest go through
a repeatable `--engine-arg`:

```bash
python scripts/zotero_plus.py translate --key KEY --local --dry-run      # print the command
python scripts/zotero_plus.py translate --key KEY --local --pages 1-10
python scripts/zotero_plus.py translate --key KEY --local --bilingual
python scripts/zotero_plus.py translate --key KEY --local --output-format=pdf-docx
python scripts/zotero_plus.py translate --key KEY --local --engine-arg=--min-font-scale=0.8
```

Precedence is `local.extra_args` → named switch → `--engine-arg`. Options the
script sets itself (`--output`, `--api-key-env`, `--model`, `--base-url`, …) are
rejected rather than silently ignored.

**Tables stay untranslated by default**: `local.translate_tables` ships as
`off`, because a mistranslated results table costs more than an untranslated
one. Set it to `auto` (prose-heavy tables only) or `on`, or pass
`--translate-tables=auto` for one run.

### Resolve venue, CCF and CORE

```bash
python scripts/zotero_plus.py venue --keys DMI9KNFZ
python scripts/zotero_plus.py venue --query "KV cache" --limit 20
python scripts/zotero_plus.py venue-apply --plan ~/.cache/zotero-plus/plan-*.json --yes
python scripts/zotero_plus.py venue-undo --yes
```

`venue` only produces a plan. `venue-apply` takes a snapshot before writing and
writes item type, venue fields, publisher, DOI, Extra lines and tags
(`venue:…`, `year:…`, `CCF:…`, `CORE:…`, `acceptance:…`, `cite:…`).

### Workflow

```bash
python scripts/zotero_plus.py arxiv-version --keys C2I975UC
python scripts/zotero_plus.py arxiv-version --collection RBYJTLIG --download
python scripts/zotero_plus.py find --keys IGEDXRM8
python scripts/zotero_plus.py pdf --keys IGEDXRM8 --attach
```

- `arxiv-version` reads page 1 of the local PDF (`arXiv:xxxxvN`) and compares
  it with the live arXiv page. `--download` attaches the newer PDF.
- `find` queries arXiv `data-doi`, Semantic Scholar, DBLP, PubMed and Crossref;
  it is read-only.
- `pdf` tries Unpaywall, Semantic Scholar, OpenAlex and arXiv; only
  open-access copies can be fetched.

### Attachment naming

```bash
python scripts/zotero_plus.py rename-title
python scripts/zotero_plus.py rename-title --apply    # Zotero must be closed
python scripts/zotero_plus.py rename-title --keys DC8UEKWS
python scripts/zotero_plus.py rename-title --attachment-keys G7NBDFSE
python scripts/zotero_plus.py titles
python scripts/zotero_plus.py titles --english --added 2026-10-24 --apply
```

House rule: `English PDF = <title>.pdf`, `Chinese PDF = <title>_zh_CN.pdf`,
additional copies get ` (2)`, ` (3)`. Windows-illegal characters are replaced
(`:` becomes ` -`).

`--keys` and `--attachment-keys` (comma-separated, case-insensitive) scope a
run to specific items or attachments, and also cover items with no Chinese
translation. A key that matches nothing exits non-zero.

`rename-*` modifies `zotero.sqlite` and the storage folder, backs up the
database to `~/.cache/zotero-plus/backups/` (newest 3 kept), and requires
Zotero to be closed. `ZOTERO_PLUS_STATE_DIR` overrides the location. Nothing
is written inside the skill directory, which `npx skills` replaces on update.

### PDF bookmarks

```bash
python scripts/zotero_plus.py outline --keys DMI9KNFZ            # preview
python scripts/zotero_plus.py outline --keys DMI9KNFZ --apply    # write the PDF
python scripts/zotero_plus.py outline --keys DMI9KNFZ --apply --jasminum
python scripts/zotero_plus.py outline --attachment-keys BI78XU9F --apply
python scripts/zotero_plus.py outline --path paper.pdf --apply
```

Headings are read from the text: `1` / `2.1` / `3.1.2` numbering at a column's
left edge (level = dot count + 1), `A Evaluation Details` appendices, and
unnumbered banners such as `Abstract` or `References` set larger than the body.
A number printed on its own line is joined to its title, and a wrapped heading
is joined back. Single- and two-column layouts both work; `--max-level 2` stops
at sections. A PDF that already has bookmarks is left alone unless `--force`.

`--apply` copies the PDF to `~/.cache/zotero-plus/pdf-backups/` (newest 3 per
file kept) before replacing it; close that tab in Zotero first, or Windows keeps
the file handle. Scanned PDFs need OCR before they have any text to read.

`--jasminum` also writes `jasminum-outline.json` beside the PDF in the schema
the Jasminum plugin reads. That switch exists because the plugin caches the
sidecar the first time it opens an attachment and **never re-reads the PDF once
the file exists** — an empty `{"outline":[]}` is remembered forever, so its
outline pane can stay blank even for a PDF that has bookmarks. `y` is written in
PDF coordinates (origin bottom-left) because the plugin navigates with
`goToDestination`, unlike the viewport coordinates it saves itself.

## Data sources

| Source | Used for |
|---|---|
| hjfy.top | Chinese translation |
| Semantic Scholar | Venue, published DOI, open PDF |
| OpenReview | Venue fallback |
| DBLP | Venue, published DOI |
| Crossref | Published DOI |
| PubMed | Published DOI |
| Unpaywall / OpenAlex | Open-access PDFs |
| arXiv | Version checks, PDFs |

## Safety

- `find`, `venue`, `status` and `download` are read-only.
- `venue-apply` and `rename-*` snapshot/back up before writing.
- No item merging or type migration beyond what `venue-apply` explicitly does.
- Cookies are never committed and never leave the machine except as the request
  header.

## Upstream projects

This is a port, not a fork. Behaviour and data come from:

| Part | Upstream | License |
|---|---|---|
| `scripts/translate/arxiv2zh.py` | [kongyan66/arxiv2zh](https://github.com/kongyan66/arxiv2zh) | AGPL-3.0 |
| `scripts/venue/arxiv_marker.py` | [lelelelelelelelelelelelele/arxiv-marker](https://github.com/lelelelelelelelelelelelele/arxiv-marker) | MIT |
| `data/rankings.json` | arxiv-marker (`zm-data.js`) | MIT |
| `data/ccf.json` | [GroundbreakerLhy/CCF-Rank](https://github.com/GroundbreakerLhy/CCF-Rank) | AGPL-3.0 |
| `scripts/workflow/` | [AllanChain/zotero-arxiv-workflow](https://github.com/AllanChain/zotero-arxiv-workflow) | AGPL-3.0 |

## License

**AGPL-3.0-or-later.** Parts of this project are derived from AGPL-3.0
projects, so the whole is distributed under the same license. See
[`LICENSE`](LICENSE).

## Contributing

Issues and pull requests are welcome. See [`CONTRIBUTING.md`](CONTRIBUTING.md)
and [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md). For security reports, follow
[`SECURITY.md`](SECURITY.md).

## Disclaimer

Unofficial tool. Not affiliated with Zotero, arXiv, hjfy.top, or any of the
services above. Their availability and terms are their own.
