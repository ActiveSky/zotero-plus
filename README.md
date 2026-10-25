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
```

Papers with LaTeX source work anonymously. Papers without source require
uploading the local PDF, which needs a hjfy.top login; the cookie is read from
`~/.config/arxiv2zh/cookies.txt`, then `--cookie`, then Zotero's cookie jar.

### Resolve venue, CCF and CORE

```bash
python scripts/zotero_plus.py venue --keys DMI9KNFZ
python scripts/zotero_plus.py venue --query "KV cache" --limit 20
python scripts/zotero_plus.py venue-apply --plan state/plan-*.json --yes
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
python scripts/zotero_plus.py titles
python scripts/zotero_plus.py titles --english --added 2026-10-24 --apply
```

House rule: `English PDF = <title>.pdf`, `Chinese PDF = <title>_zh_CN.pdf`,
additional copies get ` (2)`, ` (3)`. Windows-illegal characters are replaced
(`:` becomes ` -`).

`rename-*` modifies `zotero.sqlite` and the storage folder, backs up the
database to `state/`, and requires Zotero to be closed.

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
