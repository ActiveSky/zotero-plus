#!/usr/bin/env python3
"""One entry point for the Zotero helpers in this skill.

The real work lives in four scripts; this file only routes:

    zotero-plus translate  ...   arXiv -> Chinese PDF through hjfy.top
    zotero-plus venue      ...   resolve arXiv preprints to their published venue
    zotero-plus rename-zh  ...   rename Chinese PDFs to match the English PDF
    zotero-plus rename-en  ...   rename English PDFs to match the item title
    zotero-plus titles     ...   make attachment titles match filenames/titles

Run `zotero-plus <command> --help` to see that command's own options.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# command -> (script, fixed subcommand or None)
ROUTES: dict[str, tuple[str, str | None]] = {
    # arXiv -> Chinese PDF (arxiv2zh)
    "translate": ("translate/arxiv2zh.py", "translate"),
    "status": ("translate/arxiv2zh.py", "status"),
    "download": ("translate/arxiv2zh.py", "download"),
    "open": ("translate/arxiv2zh.py", "open"),
    "login": ("translate/arxiv2zh.py", "login"),
    "cookies": ("translate/arxiv2zh.py", "export-cookies"),
    # venue / CCF / CORE (arxiv-marker)
    "venue": ("venue/arxiv_marker.py", "resolve"),
    "venue-apply": ("venue/arxiv_marker.py", "apply"),
    "venue-undo": ("venue/arxiv_marker.py", "undo"),
    # workflow: version checks, published-version discovery, open PDFs
    "arxiv-version": ("workflow/main.py", "arxiv-version"),
    "find": ("workflow/main.py", "find"),
    "pdf": ("workflow/main.py", "pdf"),
    # attachment naming and titles
    "rename-zh": ("naming/rename_attachments.py", None),
    "rename-en": ("naming/rename_english.py", None),
    "rename-title": ("naming/rename_by_title.py", None),
    "titles": ("naming/update_titles.py", None),
}

GROUPS = {
    "翻译 (arXiv -> 中文 PDF)": ["translate", "status", "download", "open", "login", "cookies"],
    "会议/期刊 (venue, CCF, CORE)": ["venue", "venue-apply", "venue-undo"],
    "工作流 (版本 / 正式发表版 / 开放 PDF)": ["arxiv-version", "find", "pdf"],
    "附件命名与标题": ["rename-title", "rename-zh", "rename-en", "titles"],
}


def usage() -> str:
    lines = [
        "zotero-plus — one entry point for the Zotero helpers",
        "",
        "usage: zotero-plus <command> [options]",
        "",
    ]
    for title, commands in GROUPS.items():
        lines.append(f"{title}:")
        for cmd in commands:
            script, sub = ROUTES[cmd]
            target = f"{script} {sub}" if sub else script
            lines.append(f"  {cmd:12} -> {target}")
        lines.append("")
    lines += [
        "Examples:",
        "  zotero-plus translate --key DMI9KNFZ",
        "  zotero-plus status 2503.24000",
        "  zotero-plus venue --keys DMI9KNFZ",
        "  zotero-plus venue-apply --plan state/plan-*.json --yes",
        "  zotero-plus titles --english --added 2026-10-24",
        "",
        "Run `zotero-plus <command> --help` for that command's options.",
    ]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(usage())
        return 0

    command = argv[0]
    route = ROUTES.get(command)
    if not route:
        print(f"unknown command: {command}\n", file=sys.stderr)
        print(usage(), file=sys.stderr)
        return 2

    script_name, fixed = route
    script = HERE / script_name
    if not script.exists():
        print(f"missing script: {script}", file=sys.stderr)
        return 2

    cmd = [sys.executable, str(script)]
    if fixed:
        cmd.append(fixed)
    cmd.extend(argv[1:])

    try:
        return subprocess.run(cmd).returncode
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main(sys.argv[1:]))
