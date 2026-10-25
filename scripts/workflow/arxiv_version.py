"""Check whether an item's local arXiv PDF is behind the version on arXiv.

Derived from arxiv-workflow (https://github.com/AllanChain/zotero-arxiv-workflow),
Copyright (c) Allan Chain, AGPL-3.0-or-later.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import common

LOCAL_VERSION = re.compile(r"arXiv:\s*(\d{4}\.\d{4,5})v(\d+)", re.I)
ONLINE_VERSION = re.compile(r"<strong>\[v(\d+)\]</strong>")


def local_version(item_key: str) -> tuple[int | None, str | None]:
    """arXiv version on the first page of the item's English PDF.

    Only page 1 is parsed, matching arxiv-workflow's
    `PDFWorker.getFullText(attachmentID, 1)`: arXiv stamps the version in the
    left margin of the first page, so the rest of the PDF adds nothing.
    """
    for att in common.pdf_attachments(item_key):
        if att["name"].lower().endswith("_zh_cn.pdf"):
            continue
        proc = subprocess.run(
            ["zotero-cli", "read", att["key"], "--start-page", "1", "--end-page", "1"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            continue
        versions = [int(m.group(2)) for m in LOCAL_VERSION.finditer(proc.stdout)]
        if versions:
            return max(versions), att["key"]
    return None, None


def online_version(arxiv_id: str) -> int | None:
    html = common.fetch_text(f"https://arxiv.org/abs/{common.q(arxiv_id)}")
    if not html:
        return None
    versions = [int(v) for v in ONLINE_VERSION.findall(html)]
    return max(versions) if versions else None


def download_version(item: dict, arxiv_id: str, version: int) -> str | None:
    base = common.sanitize_filename(item.get("title") or arxiv_id)
    name = f"{base} (v{version}).pdf"
    existing = {a["name"].lower() for a in common.pdf_attachments(item["key"])}
    if name.lower() in existing:
        return f"{name} (already attached)"
    url = f"https://arxiv.org/pdf/{common.q(arxiv_id)}v{version}"
    status, body = common.fetch(url, timeout=120)
    if not common.validate_pdf(body):
        return None
    tmp = Path(tempfile.mkdtemp(prefix="zotero-version-")) / name
    tmp.write_bytes(body)
    proc = subprocess.run(["zotero-cli", "attach", item["key"], "--file", str(tmp),
                           "--filename", name],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        return None
    return name


def run(args) -> int:
    items = common.fetch_items(args.keys, args.query, args.collection, args.limit)
    if not items:
        common.err("No items found.")
        return 1

    results = []
    for item in items:
        key = item.get("key")
        arxiv_id = common.extract_arxiv_id(item)
        if not arxiv_id:
            results.append({"key": key, "title": item.get("title"), "arxiv": None,
                            "local": None, "online": None, "status": "no arXiv id"})
            continue
        lv, att_key = local_version(key)
        ov = online_version(arxiv_id)
        if lv is None or ov is None:
            status = "unknown"
        elif ov > lv:
            status = f"update v{lv} -> v{ov}"
        else:
            status = "up-to-date"
        row = {"key": key, "title": item.get("title"), "arxiv": arxiv_id,
               "local": lv, "online": ov, "status": status, "attachment": att_key}
        if status.startswith("update") and args.download:
            row["downloaded"] = download_version(item, arxiv_id, ov)
        results.append(row)

    if args.json:
        print(json.dumps({"generated": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
                          "results": results}, ensure_ascii=False, indent=2))
        return 0

    print(f"{'KEY':<9} {'ARXIV':<12} {'LOCAL':<6} {'ONLINE':<7} {'STATUS':<20} TITLE")
    print("-" * 120)
    for r in results:
        print(f"{r['key']:<9} {str(r['arxiv'] or '-'):<12} {str(r['local'] if r['local'] is not None else '-'):<6} "
              f"{str(r['online'] if r['online'] is not None else '-'):<7} "
              f"{r['status']:<20} {(r.get('title') or '')[:55]}")
        if r.get("downloaded"):
            print(f"          downloaded: {r['downloaded']}")
    return 0


def add_parser(sub) -> None:
    p = sub.add_parser("arxiv-version", help="Check local arXiv PDFs against the live version")
    p.add_argument("--keys", help="Comma-separated Zotero item keys")
    p.add_argument("--query", help="Search query instead of keys")
    p.add_argument("--collection", help="Scope to a collection key")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--download", action="store_true", help="Download and attach newer versions")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=run)
