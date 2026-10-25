"""Shared helpers for the workflow commands (arxiv-version / pdf / find).

Derived from arxiv-workflow (https://github.com/AllanChain/zotero-arxiv-workflow),
Copyright (c) Allan Chain, AGPL-3.0-or-later.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

UA = "zotero-plus-workflow/1.0"


# ------------------------------------------------------------------------ zotero

def zotero_json(*args: str) -> object:
    proc = subprocess.run(["zotero-cli", "--json", *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise SystemExit(f"zotero-cli failed: {proc.stderr.strip() or proc.stdout.strip()}")
    payload = json.loads(proc.stdout)
    if not payload.get("ok"):
        raise SystemExit(f"zotero-cli error: {payload}")
    return payload.get("data")


def get_item(key: str) -> dict:
    data = zotero_json("get", "metadata", key)
    item = data.get("data", {}) if isinstance(data, dict) else {}
    item.setdefault("key", key)
    return item


def child_attachments(key: str) -> list[dict]:
    payload = zotero_json("get", "children", key)
    items = payload.get("items") if isinstance(payload, dict) else []
    return [i for i in items or [] if i.get("itemType") == "attachment"]


def attachment_paths(key: str) -> dict[str, str]:
    payload = zotero_json("path", key)
    text = payload.get("text", "") if isinstance(payload, dict) else ""
    paths: dict[str, str] = {}
    current = None
    for line in text.splitlines():
        m = re.match(r"## `([A-Z0-9]+)`", line)
        if m:
            current = m.group(1)
            continue
        m = re.search(r"- Local path: `(.+)`$", line)
        if m and current:
            paths[current] = m.group(1)
    return paths


def pdf_attachments(item_key: str) -> list[dict]:
    """[{key, path, name}] for every attachment whose local file ends in .pdf."""
    paths = attachment_paths(item_key)
    out = []
    for att in child_attachments(item_key):
        path = paths.get(att["key"])
        if not path or not path.lower().endswith(".pdf"):
            continue
        out.append({"key": att["key"], "path": path, "name": Path(path).name,
                    "title": att.get("title") or ""})
    return out


def fetch_items(keys: str | None, query: str | None, collection: str | None,
                limit: int) -> list[dict]:
    if keys:
        selected = [k.strip() for k in keys.split(",") if k.strip()]
    elif query:
        args = ["search", "--detail", "keys_only", "--limit", str(limit)]
        if collection:
            args += ["--collection", collection]
        payload = zotero_json(*args, query)
        selected = _keys_from(payload)
    elif collection:
        selected = _keys_from(zotero_json("get", "collection-items", collection,
                                          "--detail", "keys_only", "--limit", str(limit)))
    else:
        selected = _keys_from(zotero_json("get", "recent", "--limit", str(limit)))

    out = []
    for key in selected:
        item = get_item(key)
        if item.get("itemType") in ("attachment", "note", "annotation"):
            continue
        out.append(item)
    return out


def _keys_from(payload: object) -> list[str]:
    items = payload.get("items") if isinstance(payload, dict) else payload
    out = []
    for entry in items or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("itemType") in ("attachment", "note", "annotation"):
            continue
        key = entry.get("key") or entry.get("itemKey")
        if key:
            out.append(key)
    return out


# -------------------------------------------------------------------------- HTTP

def fetch(url: str, *, data: bytes | None = None, headers: dict | None = None,
          timeout: int = 45, retries: int = 3) -> tuple[int, bytes]:
    """GET/POST with backoff. 429 waits Retry-After when given, else 2s, 4s, 8s.

    Semantic Scholar's public endpoint rate-limits aggressively; retrying
    immediately (as an earlier version did) just produced more 429s.
    """
    hdrs = {"User-Agent": UA, **(headers or {})}
    last: tuple[int, bytes] = (0, b"")
    delay = 2.0
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            body = e.read()
            if e.code == 429:
                last = (429, body)
                if attempt == retries:
                    break
                retry_after = e.headers.get("Retry-After") if e.headers else None
                try:
                    wait = float(retry_after) if retry_after else delay
                except ValueError:
                    wait = delay
                time.sleep(min(max(wait, 1.0), 30))
                delay = min(delay * 2, 30)
                continue
            if e.code in (500, 502, 503, 504) and attempt < retries:
                last = (e.code, body)
                time.sleep(min(delay, 10))
                delay = min(delay * 2, 30)
                continue
            return e.code, body
        except Exception as e:
            last = (0, str(e).encode())
            if attempt < retries:
                time.sleep(min(delay, 10))
                delay = min(delay * 2, 30)
    return last


def fetch_text(url: str, **kw) -> str:
    status, body = fetch(url, **kw)
    if status and not (200 <= status < 300):
        return ""
    return body.decode("utf-8", "replace")


def fetch_json(url: str, **kw) -> object | None:
    status, body = fetch(url, **kw)
    if status and not (200 <= status < 300):
        return None
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return None


def validate_pdf(data: bytes) -> bool:
    return bool(data) and b"%PDF-" in data[:1024] and b"%%EOF" in data[-65536:]


# ------------------------------------------------------------------------- arXiv

_ARXIV_NEW = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?", re.I)
_ARXIV_FULL = re.compile(r"^(\d{4}\.\d{4,5})(v\d+)?$", re.I)
_ARXIV_TAGGED = re.compile(r"arxiv[:.\s/]*?(\d{4}\.\d{4,5})", re.I)


def extract_arxiv_id(data: dict) -> str | None:
    archive = str(data.get("archiveID") or "")
    if archive:
        m = _ARXIV_TAGGED.search(archive) or _ARXIV_NEW.search(archive)
        if m and ("arxiv" in archive.lower() or _ARXIV_FULL.fullmatch(archive.strip())):
            return m.group(1)
    m = _ARXIV_TAGGED.search(str(data.get("DOI") or ""))
    if m:
        return m.group(1)
    for field in ("url", "extra"):
        val = str(data.get(field) or "")
        if "arxiv" in val.lower():
            m = _ARXIV_TAGGED.search(val) or _ARXIV_NEW.search(val)
            if m:
                return m.group(1)
    return None


def item_year(data: dict) -> int | None:
    m = re.search(r"\b(19|20)\d{2}\b", str(data.get("date") or ""))
    return int(m.group(0)) if m else None


def item_authors(data: dict) -> list[str]:
    out = []
    for c in data.get("creators") or []:
        if c.get("creatorType") != "author":
            continue
        name = " ".join(x for x in (c.get("firstName"), c.get("lastName")) if x)
        if name:
            out.append(name)
    return out


def first_author_lastname(data: dict) -> str:
    for c in data.get("creators") or []:
        if c.get("creatorType") == "author" and c.get("lastName"):
            return c["lastName"]
    return ""


# ----------------------------------------------------------------- title match

_HTML_ENTITIES = {"&amp;": "&", "&quot;": '"', "&apos;": "'",
                  "&#39;": "'", "&lt;": "<", "&gt;": ">"}


def normalize_title(title: str | None) -> str:
    if not title:
        return ""
    title = title.strip().rstrip(".")
    for entity, char in _HTML_ENTITIES.items():
        title = title.replace(entity, char)
    title = re.sub(r"[^\w\s]", " ", title, flags=re.UNICODE)
    return re.sub(r"\s+", " ", title).strip().lower()


def dice_similarity(a: str, b: str) -> float:
    ta = set(normalize_title(a).split())
    tb = set(normalize_title(b).split())
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    return 2 * len(ta & tb) / (len(ta) + len(tb))


FUZZY_THRESHOLD = 0.8


def evaluate_title_pair(base: str, candidate: str) -> dict:
    nb, nc = normalize_title(base), normalize_title(candidate)
    if not nb or not nc:
        return {"kind": "reject", "score": 0.0}
    if nb == nc:
        return {"kind": "exact", "score": 1.0}
    score = dice_similarity(base, candidate)
    return {"kind": "fuzzy" if score >= FUZZY_THRESHOLD else "reject", "score": score}


def surname_matches(preprint_lastname: str, candidate_first_author: str | None) -> bool:
    """Token match, not substring: 'Li' must not match 'Lin'."""
    if not preprint_lastname or not candidate_first_author:
        return False
    first = str(candidate_first_author).split(",")[0]
    tokens = re.findall(r"[^\W\d_]+", first.lower(), flags=re.UNICODE)
    return preprint_lastname.strip().lower() in tokens


def year_gate(preprint_year, candidate_year) -> bool:
    def year_of(v):
        m = re.search(r"\d{4}", str(v or ""))
        return int(m.group(0)) if m else None
    py, cy = year_of(preprint_year), year_of(candidate_year)
    if cy is None:
        return False
    if py is None:
        return True
    return cy >= py


# ----------------------------------------------------------------------- misc

def sanitize_filename(title: str, limit: int = 140) -> str:
    name = title.replace("/", "-").replace("\\", "-").replace(":", " -")
    name = re.sub(r'[<>"|?*]', "", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > limit:
        name = name[:limit].rstrip(" .")
    return name or "document"


def q(value: str) -> str:
    return urllib.parse.quote(str(value), safe="")


def out(msg: str) -> None:
    print(msg, flush=True)


def err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
