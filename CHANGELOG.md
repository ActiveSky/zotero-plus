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

### Fixed

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
