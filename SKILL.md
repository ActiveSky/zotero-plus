---
name: zotero-plus
description: Zotero library toolbox with one entry point — translate arXiv papers into Chinese PDFs through hjfy.top, resolve arXiv preprints to the conference or journal they were published at (with CCF/CORE tiers), and fix attachment filenames and titles. Use when the user asks to 翻译 arXiv 论文为中文、下载中文 PDF、识别 arXiv 发在哪个会议或期刊、补 venue、标 CCF/CORE 等级、统一附件命名或标题, or mentions zotero-plus, arxiv2zh, arxiv-marker, hjfy.top.
---

# zotero-plus

One command surface for three jobs that all drive `zotero-cli`:

| Group | Command | What it does |
|---|---|---|
| 翻译 | `translate` | arXiv → Chinese PDF, attached to the item |
| | `status` | remote translation state |
| | `download` | download the Chinese PDF only |
| | `open` | open the paper on hjfy.top |
| | `login` / `cookies` | sign-in page / re-export the cookie file |
| 会议/期刊 | `venue` | resolve venue, confidence, CCF/CORE tier (plan only) |
| | `venue-apply` | write a reviewed plan back to Zotero |
| | `venue-undo` | restore the last applied plan |
| 工作流 | `arxiv-version` | is the local arXiv PDF behind the live version? |
| | `find` | discover the published version across five sources (read-only) |
| | `pdf` | find an open-access PDF and optionally attach it |
| 附件整理 | `rename-title` | **recommended**: every PDF filename = item title (`<title>.pdf`, `<title>_zh_CN.pdf`) |
| | `rename-zh` | Chinese PDF filename = English PDF name + `_zh_CN` |
| | `rename-en` | English PDF filename = item title (one day's imports) |
| | `titles` | align attachment titles (Chinese: filename; English: paper title) |

`zotero-plus` itself is a thin router (`scripts/zotero_plus.py`); each command
maps to one of the real scripts. `zotero-plus <command> --help` shows that
command's own options.

## Quick start

```powershell
# Chinese translation
zotero-plus translate --key DMI9KNFZ
zotero-plus translate --arxiv-id 2503.24000 --no-attach --out-dir .\pdfs
zotero-plus status 2503.24000

# Venue / CCF / CORE
zotero-plus venue --keys DMI9KNFZ
zotero-plus venue --query "KV cache" --limit 20
zotero-plus venue-apply --plan state\plan-*.json --yes
zotero-plus venue-undo --yes

# Workflow
zotero-plus arxiv-version --keys C2I975UC
zotero-plus arxiv-version --collection RBYJTLIG --download
zotero-plus find --keys IGEDXRM8
zotero-plus pdf --keys IGEDXRM8 --attach

# Filenames and titles (title-only naming)
zotero-plus rename-title                # preview
zotero-plus rename-title --apply        # requires Zotero closed
zotero-plus titles
zotero-plus titles --english --added 2026-10-24 --apply
```

## Prerequisites

- Zotero running, local API on; writes authorized
  (`zotero-mcp authorize-local` once).
- `zotero-cli` on PATH.
- Python 3.10+ (standard library only for most commands; `venue-apply` and
  `venue-undo` import `zotero_mcp` and re-exec into its interpreter by
  themselves).
- Network for hjfy.top, Semantic Scholar, OpenReview, DBLP, Crossref.

`rename-zh` and `rename-en` edit Zotero's SQLite database and the storage
folder, so Zotero must be closed for `--apply`. `titles`, `translate`,
`venue`, `venue-apply` and `venue-undo` work with Zotero open.

## 翻译 (arXiv → 中文 PDF)

Flow: `arxivInfo` → poll `arxivStatus`, or upload the item's PDF and poll
`fileStatus` → `arxivFiles`/`fileFiles` → download → attach.

- Naming: **`<English PDF basename>_zh_CN.pdf`**, falling back to the arXiv id.
- An existing translation attachment is reused unless `--force`.
- Papers with LaTeX source work anonymously. Uploading a PDF (no source) needs
  a hjfy.top login; the cookie is read from
  `~/.config/arxiv2zh/cookies.txt`, then `--cookie`, then Zotero's jar.
- Refresh the cookie after signing in again:

```powershell
zotero-plus cookies
```

Full options: `zotero-plus translate --help`.

## 会议/期刊 (venue, CCF, CORE)

Flow: arXiv id → Semantic Scholar batch → OpenReview fallback → DBLP fallback
→ rank against two catalogues → optional write-back.

- `data/rankings.json` — 36 CORE-ranked venues with official `write_as` names
  and overrides.
- `data/ccf.json` — 385 conferences + 295 journals from the CCF Rank plugin
  (data 2026-04).
- Confidence: 0.95 when two sources agree, 0.85 one ranked source, 0.6 an
  unranked venue. Default write threshold 0.8.
- Writes item type, `proceedingsTitle`/`conferenceName` or
  `publicationTitle`, publisher, DOI, Extra lines, and tags
  `venue:… year:… CCF:… CORE:… acceptance:accepted cite:…`.
- `venue-apply` snapshots every item first; `venue-undo` restores it.

Full options: `zotero-plus venue --help`, `zotero-plus venue-apply --help`.

## 工作流 (版本 / 正式发表版 / 开放 PDF)

These come from arxiv-workflow's read-only and additive paths. Nothing here
merges items or rewrites metadata.

### `arxiv-version` — 本地 PDF 是否过期

Reads the English PDF full text for `arXiv:xxxxvN`, fetches `arxiv.org/abs/...`
for the live version, and compares. `--download` fetches the newer PDF and
attaches it as `<title> (vN).pdf` (the old PDF stays, with its annotations).

```powershell
zotero-plus arxiv-version --keys C2I975UC
zotero-plus arxiv-version --collection RBYJTLIG --download
```

Reading full text is slow for long papers; keep batches modest (`--limit`).

### `find` — 正式发表版发现（只读）

Queries, in order: arXiv's `data-doi`, Semantic Scholar, DBLP, PubMed,
Crossref. Titles are matched exactly or by Dice similarity ≥ 0.8 plus
first-author/year gates. Prints DOIs/URLs; it never writes to Zotero.

```powershell
zotero-plus find --keys IGEDXRM8
zotero-plus find --query "KV cache" --sources doi,s2,crossref --limit 10
```

No result is common for venues without DOIs (e.g. MLSys proceedings).

### `pdf` — 开放获取 PDF

Tries Unpaywall, Semantic Scholar, OpenAlex, then arXiv. Downloads the first
URL that passes PDF validation and, with `--attach`, adds it as
`<title>.pdf` (numbered if one exists).

```powershell
zotero-plus pdf --keys IGEDXRM8
zotero-plus pdf --keys IGEDXRM8 --attach
zotero-plus pdf --keys KEY --out-dir .\pdfs --email you@example.com
```

Only open-access copies. Publisher paywalls are out of scope — arxiv-workflow
used Zotero's built-in `addAvailableFile`, which has no CLI equivalent.

## 附件整理

The house rule is **title-only filenames** — the item metadata already carries
authors and year, so repeating them in the filename is noise:

```
English PDF : <title>.pdf        (2nd, 3rd copy: <title> (2).pdf)
Chinese PDF : <title>_zh_CN.pdf
```

```powershell
zotero-plus rename-title                # preview every item that has a Chinese PDF
# quit Zotero
zotero-plus rename-title --apply        # files + DB paths + titles, one pass
```

It also sets displayed titles: English attachments get the paper title, Chinese
ones the filename without `.pdf`. A database backup goes to `state/`.

The older, narrower helpers remain available:

```powershell
zotero-plus rename-zh                                  # Chinese name = English name + _zh_CN
zotero-plus rename-en --added 2026-10-24 --apply       # one day's English PDFs
zotero-plus titles --apply                             # Chinese titles = filenames
zotero-plus titles --english --added 2026-10-24 --apply
```

Windows forbids `\ / : * ? " < > |` in filenames, so `:` becomes ` -` and the
rest are dropped. The displayed title keeps the original characters.

`rename-*` back up `zotero.sqlite` into `state/` before writing.

## Upstream sources

| What | Where | Ported version | License |
|---|---|---|---|
| arxiv2zh | https://github.com/kongyan66/arxiv2zh | 0.2.0 | AGPL-3.0 |
| arxiv-marker | https://github.com/lelelelelelelelelelelelele/arxiv-marker | 0.2.2 | MIT |
| CCF Rank (venue catalogue) | https://github.com/GroundbreakerLhy/CCF-Rank | data 2026-04 (1.9.0) | AGPL-3.0 |
| arxiv-workflow (workflow commands) | https://github.com/AllanChain/zotero-arxiv-workflow | 0.4.1 | AGPL-3.0 |

Because three of the four are AGPL-3.0, **this skill as a whole is
AGPL-3.0-or-later** (see `LICENSE`). The arxiv-marker-derived parts remain MIT
and keep their copyright notice in the source file.

Check for updates:

```powershell
gh api repos/kongyan66/arxiv2zh/releases/latest --jq '.tag_name, .published_at'
gh api repos/lelelelelelelelelelelelele/arxiv-marker/releases/latest --jq '.tag_name, .published_at'
gh api repos/GroundbreakerLhy/CCF-Rank/releases/latest --jq '.tag_name, .published_at'
```

Mapping when diffing:

| Upstream file | Skill file |
|---|---|
| arxiv2zh `arxiv2zh.js` — `HjfyClient` / task flow / `ResultImporter` | `scripts/arxiv2zh.py` |
| arxiv-marker `resolver.js` | `scripts/arxiv_marker.py` |
| arxiv-marker `zm-data.js` | `data/rankings.json` |
| CCF-Rank `ccf-rank.js` (`var ccf_conferences_default`) | `data/ccf.json` |
| arxiv-workflow `arxiv-workflow.js` — `PaperFinder` | `scripts/workflow/find.py`, `oa_pdf.py` |
| arxiv-workflow `arxiv-workflow.js` — `arXivPDF`, `UpdatePDF` | `scripts/workflow/arxiv_version.py` |

## Files

```
SKILL.md                      agent-facing instructions
README.md                     project overview for GitHub
LICENSE                       AGPL-3.0
.gitignore                    excludes state/, caches, databases
scripts/
  zotero_plus.py              router (the only entry point)
  translate/arxiv2zh.py       translate / status / download / open / login / cookies
  venue/arxiv_marker.py       venue / venue-apply / venue-undo
  naming/rename_by_title.py   rename-title (title-only names for all PDFs)
  naming/rename_attachments.py rename-zh
  naming/rename_english.py    rename-en
  naming/update_titles.py     titles
  workflow/main.py            arxiv-version / find / pdf
  workflow/common.py          zotero-cli, HTTP, arXiv id, title matching
  workflow/arxiv_version.py   version comparison and download
  workflow/find.py            five-source published-version discovery
  workflow/oa_pdf.py          open-access PDF lookup
data/rankings.json            CORE venue table (36)
data/ccf.json                 CCF catalogue (680)
state/.gitkeep                runtime plans, undo snapshots, database backups
```
