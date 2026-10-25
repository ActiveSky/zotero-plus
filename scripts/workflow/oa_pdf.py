"""Find an openly available PDF for an item and optionally attach it.

Derived from arxiv-workflow (https://github.com/AllanChain/zotero-arxiv-workflow),
Copyright (c) Allan Chain, AGPL-3.0-or-later.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import common

DEFAULT_EMAIL = "zotero-plus@users.noreply.github.com"


def unpaywall(doi: str, email: str) -> list[tuple[str, str]]:
    data = common.fetch_json(f"https://api.unpaywall.org/v2/{common.q(doi)}?email={common.q(email)}")
    if not isinstance(data, dict):
        return []
    out = []
    best = data.get("best_oa_location") or {}
    if best.get("url_for_pdf"):
        out.append(("unpaywall", best["url_for_pdf"]))
    for loc in data.get("oa_locations") or []:
        url = loc.get("url_for_pdf")
        if url and all(url != u for _, u in out):
            out.append(("unpaywall", url))
    return out


def semantic_scholar(doi: str | None, arxiv_id: str | None) -> list[tuple[str, str]]:
    ident = f"DOI:{doi}" if doi else (f"ARXIV:{arxiv_id}" if arxiv_id else None)
    if not ident:
        return []
    data = common.fetch_json(
        f"https://api.semanticscholar.org/graph/v1/paper/{common.q(ident)}"
        "?fields=openAccessPdf,externalIds")
    if not isinstance(data, dict):
        return []
    out = []
    oa = data.get("openAccessPdf") or {}
    if oa.get("url"):
        out.append(("semantic_scholar", oa["url"]))
    ext = data.get("externalIds") or {}
    if ext.get("ArXiv"):
        url = f"https://arxiv.org/pdf/{ext['ArXiv']}"
        if all(url != u for _, u in out):
            out.append(("semantic_scholar/arXiv", url))
    return out


def openalex(doi: str | None, arxiv_id: str | None) -> list[tuple[str, str]]:
    ident = f"doi:{doi}" if doi else None
    if not ident:
        return []
    data = common.fetch_json(f"https://api.openalex.org/works/{common.q(ident)}")
    if not isinstance(data, dict):
        return []
    out = []
    for loc in [data.get("best_oa_location") or {}] + (data.get("locations") or []):
        url = loc.get("pdf_url")
        if url and all(url != u for _, u in out):
            out.append(("openalex", url))
    return out


def arxiv_direct(arxiv_id: str | None) -> list[tuple[str, str]]:
    return [("arXiv", f"https://arxiv.org/pdf/{arxiv_id}")] if arxiv_id else []


def candidates(item: dict, email: str) -> list[tuple[str, str]]:
    doi = str(item.get("DOI") or "").strip() or None
    if doi and "arxiv" in doi.lower():
        doi = None
    arxiv_id = common.extract_arxiv_id(item)
    out: list[tuple[str, str]] = []
    for source, urls in (
        ("unpaywall", unpaywall(doi, email) if doi else []),
        ("semantic_scholar", semantic_scholar(doi, arxiv_id)),
        ("openalex", openalex(doi, arxiv_id)),
        ("arXiv", arxiv_direct(arxiv_id)),
    ):
        for _, url in urls:
            if all(url != u for _, u in out):
                out.append((source, url))
    return out


def unique_name(item: dict, taken: set[str]) -> str:
    base = common.sanitize_filename(item.get("title") or item.get("key") or "document")
    name = f"{base}.pdf"
    n = 2
    while name.lower() in taken:
        name = f"{base} ({n}).pdf"
        n += 1
    return name


def download_first(urls: list[tuple[str, str]], dest_dir: Path, name: str) -> tuple[str, bytes] | None:
    for source, url in urls:
        status, body = common.fetch(url, timeout=120)
        if common.validate_pdf(body):
            dest_dir.mkdir(parents=True, exist_ok=True)
            (dest_dir / name).write_bytes(body)
            return source, body
    return None


def run(args) -> int:
    items = common.fetch_items(args.keys, args.query, args.collection, args.limit)
    if not items:
        common.err("No items found.")
        return 1

    results = []
    for item in items:
        key = item.get("key")
        found = candidates(item, args.email)
        row = {"key": key, "title": item.get("title"), "candidates": found}
        if not found:
            row["status"] = "no open PDF"
            results.append(row)
            continue

        taken = {a["name"].lower() for a in common.pdf_attachments(key)}
        name = unique_name(item, taken)
        target = Path(args.out_dir) if args.out_dir else Path(tempfile.mkdtemp(prefix="zotero-pdf-"))
        got = download_first(found, target, name)
        if not got:
            row["status"] = "found but download failed"
            results.append(row)
            continue
        source, body = got
        row.update({"status": "downloaded", "source": source, "file": str(target / name),
                    "size": len(body)})

        if args.attach:
            proc = subprocess.run(["zotero-cli", "attach", key, "--file", str(target / name),
                                   "--filename", name],
                                  capture_output=True, text=True, encoding="utf-8", errors="replace")
            row["attached"] = proc.returncode == 0
            if proc.returncode != 0:
                row["status"] = "downloaded, attach failed"
        results.append(row)

    if args.json:
        print(json.dumps({"email": args.email, "results": results}, ensure_ascii=False, indent=2))
        return 0

    print(f"{'KEY':<9} {'SOURCE':<24} {'STATUS':<28} TITLE")
    print("-" * 120)
    for r in results:
        source = r.get("source") or "-"
        print(f"{r['key']:<9} {source:<24} {r['status']:<28} {(r.get('title') or '')[:55]}")
        if r.get("file"):
            print(f"          {r['file']}")
        if r.get("candidates") and not r.get("file"):
            for s, u in r["candidates"][:2]:
                print(f"          {s}: {u}")
    return 0


def add_parser(sub) -> None:
    p = sub.add_parser("pdf", help="Find an open-access PDF and optionally attach it")
    p.add_argument("--keys", help="Comma-separated Zotero item keys")
    p.add_argument("--query", help="Search query instead of keys")
    p.add_argument("--collection", help="Scope to a collection key")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--email", default=DEFAULT_EMAIL, help="Contact email for Unpaywall")
    p.add_argument("--attach", action="store_true", help="Attach the PDF to the item")
    p.add_argument("--out-dir", help="Where to save the PDF (default: a temp dir)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=run)
