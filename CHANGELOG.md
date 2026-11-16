# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `rename-title` takes `--keys` and `--attachment-keys` (comma-separated,
  case-insensitive) to scope a run to specific items or attachments, so one
  file can be renamed without touching the rest of the library.
- A scoped run also covers items that have no Chinese translation. A key that
  matches nothing is reported on stderr and exits non-zero.
- One config file, `~/.config/arxiv2zh/config.json`, replaces
  `~/.config/arxiv2zh/cookies.txt`. `web` holds the service URL and the
  cookies; `local` holds the hjfy-pdf engine, its API base URL, key and model.
  Defaults fill in anything the file omits, and a malformed file warns instead
  of failing. `cookies` now imports into the config, `--config` points
  elsewhere, and the old cookie file is still read as a fallback.
- `translate --local` translates with the hjfy-pdf engine and the config's
  `local.base_url` / `local.model`, with no page balance and no preview. The
  engine path is discovered under `%LOCALAPPDATA%\hjfy-engine` when
  `local.engine` is empty, and its `PATH` is built from `bin`, `deps`, `lib`
  and `System32\downlevel`.
- `zotero-plus config` prints the config with cookies and the API key masked.
- `translate --unlock` and `download --unlock` spend the quoted pages from the
  hjfy.top balance to fetch the whole translation. It is explicit, never implied
  by `--force`, and needs the cookie.

### Fixed

- `translate` and `download` no longer attach a 5-page preview as if it were the
  translation. hjfy.top reports `isPreview` / `accessSource: "preview"` /
  `canReadFullPdf: false` for a paper that is not unlocked, and the preview is a
  valid PDF, so nothing in the download path noticed. Both commands now stop
  with the translated page count and the unlock price.
- Runtime state no longer lands inside the skill directory. Database backups,
  `venue` plans and `venue-undo` snapshots now default to
  `~/.cache/zotero-plus/`, overridable with `ZOTERO_PLUS_STATE_DIR`. An
  installed skill lives in a directory that `npx skills` replaces on update,
  so state written there was lost on the next update.
- Only the newest 3 database backups are kept; older ones are pruned after
  each run.

## [0.1.0] - 2026-10-25

### Added

- One entry point (`scripts/zotero_plus.py`) with commands for translation,
  venue resolution, workflow checks, and attachment naming.
- arXiv → Chinese PDF translation through hjfy.top, with cookie handling,
  attachment deduplication and `--force` replacement.
- Venue resolution from Semantic Scholar, OpenReview and DBLP, writing item
  type, venue fields, publisher, DOI, Extra and tags.
- CCF catalogue (385 conferences, 295 journals) and CORE venue table, with
  `venue-apply` / `venue-undo`.
- `arxiv-version` check of the local PDF against the live arXiv version.
- `find` across arXiv `data-doi`, Semantic Scholar, DBLP, PubMed and Crossref.
- `pdf` open-access lookup through Unpaywall, Semantic Scholar, OpenAlex and
  arXiv.
- Title-based filename and attachment-title maintenance.
- Windows, macOS and Linux support; no hard-coded home or package-manager paths.

### Notes

- Derived from arxiv2zh, arxiv-marker, CCF-Rank and arxiv-workflow. The project
  is AGPL-3.0-or-later because three of those four are AGPL-3.0.
