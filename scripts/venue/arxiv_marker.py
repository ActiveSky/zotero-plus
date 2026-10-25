#!/usr/bin/env python3
# Derived from arxiv-marker (https://github.com/lelelelelelelelelelelelele/arxiv-marker),
# Copyright (c) lele, MIT License. See also data/ccf.json (CCF-Rank, AGPL-3.0).
"""Resolve arXiv preprints in a Zotero library to their published venue.

Ported from lele's arxiv-marker Zotero plugin (v0.2.2). Same data sources and
scoring, but driven from the shell instead of a Zotero menu:

    Semantic Scholar  ->  OpenReview  ->  DBLP

Everything is deterministic: no model calls, no guessing. A venue is only
"accepted" when one of those sources actually names it.

Subcommands
-----------
resolve   read items (by key, query or collection), look up venues, emit a plan
apply     write a reviewed plan back to Zotero (item type + venue fields + tags)
undo      restore the last applied plan from its snapshot

The resolve step needs nothing but the standard library. The apply/undo steps
import zotero_mcp, so they must run under the interpreter that owns zotero-cli;
the script re-execs itself into that interpreter automatically when needed.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL_ROOT = HERE.parent.parent
DATA = json.loads((SKILL_ROOT / "data" / "rankings.json").read_text(encoding="utf-8"))
RANKINGS = DATA["RANKINGS"]
OVERRIDES = DATA.get("OVERRIDES", {})
CCF = json.loads((SKILL_ROOT / "data" / "ccf.json").read_text(encoding="utf-8"))
STATE_DIR = SKILL_ROOT / "state"

UA = "arxiv-marker-skill/1.0 (+https://github.com/lelelelelelelelelelelelele/arxiv-marker)"

# --------------------------------------------------------------------------- util

_ARXIV_NEW = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?", re.I)
_ARXIV_FULL = re.compile(r"^(\d{4}\.\d{4,5})(v\d+)?$", re.I)
_ARXIV_TAGGED = re.compile(r"arxiv[:.\s/]*?(\d{4}\.\d{4,5})", re.I)


def extract_arxiv_id(data: dict) -> str | None:
    archive = (data.get("archiveID") or "").strip()
    m = _ARXIV_TAGGED.search(archive) or _ARXIV_NEW.search(archive)
    if m and ("arxiv" in archive.lower() or _ARXIV_FULL.match(archive)):
        return m.group(1)
    m = _ARXIV_TAGGED.search(data.get("DOI") or "")
    if m:
        return m.group(1)
    for field in ("url", "extra"):
        val = data.get(field) or ""
        if "arxiv" in val.lower():
            m = _ARXIV_TAGGED.search(val) or _ARXIV_NEW.search(val)
            if m:
                return m.group(1)
    return None


def norm_title(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"[^a-z0-9 ]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def title_jaccard(a: str | None, b: str | None) -> float:
    na, nb = norm_title(a), norm_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ta, tb = set(na.split()), set(nb.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def first_author_lastname(data: dict) -> str:
    for c in data.get("creators") or []:
        if c.get("creatorType") == "author" and c.get("lastName"):
            return c["lastName"]
    return ""


def item_authors(data: dict) -> list[str]:
    out = []
    for c in data.get("creators") or []:
        if c.get("creatorType") != "author":
            continue
        if c.get("name"):
            out.append(c["name"])
        else:
            name = " ".join(x for x in (c.get("firstName"), c.get("lastName")) if x)
            if name:
                out.append(name)
    return out


def item_year(data: dict) -> int | None:
    date = (data.get("date") or "").strip()
    m = re.search(r"\b(19|20)\d{2}\b", date)
    return int(m.group(0)) if m else None


# ---------------------------------------------------------------------- rankings

_DISQUALIFIERS = {
    "workshop", "workshops", "findings", "doctoral", "companion", "tutorial",
    "tutorials", "demonstration", "demonstrations", "poster", "abstracts", "satellite",
}
_GENERIC_SUFFIX = {
    "symposium", "conference", "conferences", "proceedings", "meeting", "congress",
}
_TABLE: list[dict] | None = None
_CCF_TABLE: list[dict] | None = None


def _tokens(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", s.lower())


def _table() -> list[dict]:
    global _TABLE
    if _TABLE is None:
        _TABLE = []
        for r in RANKINGS:
            aliases = [a.strip().lower() for a in (r.get("aliases") or "").split("|") if a.strip()]
            aliases.append(r["canonical"].lower())
            _TABLE.append({
                "canonical": r["canonical"],
                "kind": (r.get("kind") or "conference").strip(),
                "core": (r.get("core_tier") or "").strip(),
                "ccf": (r.get("ccf_rank") or "").strip(),
                "aliases": aliases,
                "write_as": (r.get("write_as") or "").strip(),
            })
    return _TABLE


def _ccf_table() -> list[dict]:
    global _CCF_TABLE
    if _CCF_TABLE is None:
        _CCF_TABLE = []
        for kind, entries in (("conference", CCF.get("conferences") or []),
                              ("journal", CCF.get("journals") or [])):
            for e in entries:
                abbr = (e.get("abbr") or "").strip()
                full = (e.get("fullName") or "").strip()
                if not abbr:
                    continue
                aliases = [a.lower() for a in (abbr, full) if a]
                _CCF_TABLE.append({
                    "canonical": abbr,
                    "kind": kind,
                    "core": "",
                    "ccf": (e.get("rank") or "").strip(),
                    "aliases": aliases,
                    "write_as": full,
                })
    return _CCF_TABLE


def _run_index(hay: list[str], needle: list[str]) -> int:
    if not needle:
        return -1
    for i in range(len(hay) - len(needle) + 1):
        if hay[i:i + len(needle)] == needle:
            return i
    return -1


def _lookup_table(rows: list[dict], venue_raw: str | None) -> dict | None:
    if not venue_raw:
        return None
    v = venue_raw.lower().strip()
    v_tokens = _tokens(v)
    if not v_tokens:
        return None
    blocked = any(t in _DISQUALIFIERS for t in v_tokens)
    hit, matched = None, 0
    for row in rows:
        for alias in row["aliases"]:
            if v == alias:
                return row
            if blocked:
                continue
            at = _tokens(alias)
            if len(at) <= matched:
                continue
            idx = _run_index(v_tokens, at)
            if idx < 0:
                continue
            after = v_tokens[idx + len(at):]
            if after and not all(re.fullmatch(r"\d+", t) or t in _GENERIC_SUFFIX for t in after):
                continue
            hit, matched = row, len(at)
    return hit


def lookup_ranking(venue_raw: str | None) -> dict | None:
    """CORE-ranked venues first (they carry write_as forms), then the CCF catalogue."""
    return _lookup_table(_table(), venue_raw) or _lookup_table(_ccf_table(), venue_raw)


def is_nonvenue(v: str | None) -> bool:
    if not v:
        return True
    s = v.strip().lower()
    return "arxiv" in s or s in ("corr", "preprint", "")


# ------------------------------------------------------------------------- HTTP

def _request(method: str, url: str, data: bytes | None = None, headers: dict | None = None,
             timeout: int = 60, retries: int = 4) -> tuple[int, object, dict]:
    hdrs = {"User-Agent": UA, **(headers or {})}
    delay = 2.0
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
                try:
                    parsed = json.loads(body)
                except json.JSONDecodeError:
                    parsed = body
                return resp.status, parsed, dict(resp.headers)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry_after = e.headers.get("Retry-After")
                try:
                    wait = float(retry_after) if retry_after else delay
                except ValueError:
                    wait = delay
                time.sleep(min(wait, 30))
                delay = min(delay * 2, 30)
                continue
            return e.code, None, dict(e.headers or {})
        except Exception:
            if attempt == retries - 1:
                return 0, None, {}
            time.sleep(delay)
            delay = min(delay * 2, 30)
    return 0, None, {}


# ------------------------------------------------------------------ semantic scholar

S2_FIELDS = ("title,venue,publicationVenue,year,externalIds,publicationTypes,"
             "citationCount,influentialCitationCount")
S2_BATCH = "https://api.semanticscholar.org/graph/v1/paper/batch"


def _s2_venue_type(pv: dict, pub_types: list) -> str | None:
    if pv.get("type"):
        return pv["type"]
    if "JournalArticle" in (pub_types or []):
        return "journal"
    if "Conference" in (pub_types or []):
        return "conference"
    if pv.get("issn"):
        return "journal"
    return None


def s2_batch(arxiv_ids: list[str], api_key: str | None = None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["x-api-key"] = api_key
    for i in range(0, len(arxiv_ids), 100):
        chunk = arxiv_ids[i:i + 100]
        payload = json.dumps({"ids": [f"ARXIV:{a}" for a in chunk]}).encode()
        status, data, _ = _request("POST", f"{S2_BATCH}?fields={urllib.parse.quote(S2_FIELDS)}",
                                   data=payload, headers=headers, retries=6)
        if status != 200 or not isinstance(data, list):
            continue
        for arxiv_id, rec in zip(chunk, data):
            if not rec:
                continue
            pv = rec.get("publicationVenue") or {}
            ext = rec.get("externalIds") or {}
            name = pv.get("name") or rec.get("venue")
            alts = pv.get("alternate_names") or []
            abbrev = next((a for a in alts if re.search(r"[A-Z]", a) and a == a.upper() and 2 <= len(a) <= 8), None)
            out[arxiv_id] = {
                "source": "semantic_scholar",
                "venue_raw": None if is_nonvenue(name) else name,
                "year": rec.get("year"),
                "venue_type": _s2_venue_type(pv, rec.get("publicationTypes")),
                "citation_count": rec.get("citationCount"),
                "influential_citations": rec.get("influentialCitationCount"),
                "external_doi": ext.get("DOI"),
                "dblp_key": ext.get("DBLP"),
                "evidence_url": f"https://www.semanticscholar.org/arxiv/{arxiv_id}",
                "issn": pv.get("issn"),
                "abbrev": abbrev,
            }
    return out


# ------------------------------------------------------------------- openreview

OPENREVIEW_SEARCH = "https://api2.openreview.net/notes/search"
_OR_REJECT = re.compile(
    r"\b(?:submitted|submission|reject(?:ed|ion)?|withdrawn)\b|\bunder[\s_-]+review\b|\bdesk[\s_-]+rejected\b", re.I)
_OR_PRESENTATION = re.compile(
    r"(?:\s*[-–—,:]\s*|\s+)(?:poster|spotlight|oral|regular)(?:\s+presentation)?\s*$", re.I)


def _or_value(content: dict, key: str):
    value = content.get(key)
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value


def _or_rendered_title(value) -> str:
    if not value:
        return ""
    value = (str(value).replace("‘", "'").replace("’", "'")
             .replace("“", '"').replace("”", '"')
             .replace("‐", "-").replace("‑", "-").replace("‒", "-")
             .replace("–", "-").replace("—", "-").replace(" ", " "))
    for delim in ("\\(", "\\)", "\\[", "\\]"):
        value = value.replace(delim, "")
    return re.sub(r"\s+", " ", value.replace("$", "")).strip()


def _or_norm_title(value) -> str:
    return _or_rendered_title(value).lower()


def _or_title_quality(query: str, candidate: str) -> int:
    q, c = _or_norm_title(query), _or_norm_title(candidate)
    if not q or not c:
        return 0
    if q == c:
        return 2
    if c.startswith(q + ":") or q.startswith(c + ":"):
        return 1
    return 0


def _or_surname(name: str) -> str:
    value = str(name or "")
    if "," in value:
        value = value.split(",", 1)[0]
    value = re.sub(r"[^\w\s-]", "", value.lower())
    # Unicode letters, not just a-z: OpenReview has non-Latin names too.
    parts = re.findall(r"[^\W\d_]+", value, flags=re.UNICODE)
    while parts and parts[-1] in ("jr", "sr", "ii", "iii", "iv"):
        parts.pop()
    return parts[-1] if parts else ""


def _or_author_match(expected: list[str], candidate: list[str]) -> dict:
    exp = [s for s in map(_or_surname, expected) if s]
    got = [s for s in map(_or_surname, candidate) if s]
    if not exp or not got:
        return {"ok": True, "overlap": 0, "first": False}
    overlap = len(set(exp) & set(got))
    first = exp[0] == got[0]
    return {"ok": first or overlap >= 2, "overlap": overlap, "first": first}


def _or_year(content: dict, venue: str, venue_id: str) -> int | None:
    value = _or_value(content, "year")
    if re.fullmatch(r"\d+", str(value or "")):
        return int(value)
    m = re.search(r"\b(?:19|20)\d{2}\b", f"{venue} {venue_id}")
    return int(m.group(0)) if m else None


def _or_doi(content: dict) -> str | None:
    for key in ("doi", "DOI"):
        value = _or_value(content, key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _or_candidate(note: dict, title: str, authors: list[str], year: int | None,
                  arxiv_id: str | None) -> dict | None:
    content = note.get("content")
    if not isinstance(content, dict):
        return None
    candidate_title = _or_value(content, "title")
    if not isinstance(candidate_title, str):
        return None
    quality = _or_title_quality(title, candidate_title)
    if not quality:
        return None
    raw_authors = _or_value(content, "authors")
    cand_authors = [x for x in raw_authors if isinstance(x, str)] if isinstance(raw_authors, list) else []
    author = _or_author_match(authors, cand_authors)
    if not author["ok"] or (quality == 1 and (not authors or not cand_authors)):
        return None
    if arxiv_id:
        for key in ("arxiv", "arxiv_id", "arxivId"):
            value = _or_value(content, key)
            if isinstance(value, str):
                m = re.search(r"\d{4}\.\d{4,5}", value)
                if m and m.group(0) != arxiv_id:
                    return None
    venue_value = _or_value(content, "venue")
    venue_id = _or_value(content, "venueid") or _or_value(content, "venue_id")
    if not isinstance(venue_value, str) or not isinstance(venue_id, str):
        return None
    venue, venue_id = venue_value.strip(), venue_id.strip()
    decision = _or_value(content, "decision")
    status = _or_value(content, "status")
    if _OR_REJECT.search(f"{venue} {venue_id} {decision or ''} {status or ''}") or is_nonvenue(venue):
        return None
    if re.fullmatch(r"(?:poster|spotlight|oral|regular)(?:\s+presentation)?", venue, re.I):
        return None
    provenance = " ".join(str(x or "") for x in (note.get("domain"), note.get("invitations"), note.get("signatures")))
    if re.search(r"(?:^|[./_\s-])dblp(?:[./_\s-]|$)", provenance, re.I):
        return None
    venue_raw = _OR_PRESENTATION.sub("", venue).strip()
    if not venue_raw or is_nonvenue(venue_raw):
        return None
    row = lookup_ranking(venue_raw)
    scope = f"{venue_id} {note.get('domain') or ''}".lower()
    is_workshop = "/workshop" in scope or "workshop" in venue_raw.lower()
    is_main = "/conference" in scope and not is_workshop
    if "/conference" in scope or "/workshop" in scope:
        venue_type = "conference"
    elif row:
        venue_type = row["kind"]
    else:
        return None
    cand_year = _or_year(content, venue_raw, venue_id)
    forum = note.get("forum") or note.get("id")
    if not isinstance(forum, str) or not forum:
        return None
    score = quality * 100
    if author["first"]:
        score += 20
    score += min(author["overlap"], 5) * 2
    if year is not None and cand_year == year:
        score += 5
    if is_main:
        score += 8
    if row and row["kind"] == "conference":
        score += 4
    if is_workshop:
        score -= 4
    return {
        "score": score,
        "hit": {
            "source": "openreview", "venue_raw": venue_raw, "year": cand_year,
            "venue_type": venue_type, "citation_count": None, "influential_citations": None,
            "external_doi": _or_doi(content), "dblp_key": None,
            "evidence_url": "https://openreview.net/forum?id=" + urllib.parse.quote(forum),
            "issn": None, "abbrev": None,
        },
    }


def openreview_best(title: str, authors: list[str], year: int | None,
                    arxiv_id: str | None) -> dict | None:
    term = _or_rendered_title(title) or title
    params = urllib.parse.urlencode({
        "term": term, "content": "title", "type": "exact", "source": "forum", "limit": 25,
    })
    status, payload, _ = _request("GET", f"{OPENREVIEW_SEARCH}?{params}", retries=3)
    if status != 200 or not isinstance(payload, dict):
        return None
    notes = payload.get("notes")
    if not isinstance(notes, list):
        return None
    candidates = []
    for note in notes:
        try:
            c = _or_candidate(note, title, authors, year, arxiv_id)
        except Exception:
            continue
        if c:
            candidates.append(c)
    if not candidates:
        return None
    best_score = max(c["score"] for c in candidates)
    best = [c["hit"] for c in candidates if c["score"] == best_score]
    identities = {(h["venue_raw"].lower(), h["year"]) for h in best}
    if len(identities) > 1:
        return None
    return sorted(best, key=lambda h: h.get("evidence_url") or "")[0]


# ------------------------------------------------------------------------- dblp

def _dblp_authors(info: dict) -> list[str]:
    a = (info.get("authors") or {}).get("author")
    if a and not isinstance(a, list):
        a = [a]
    return [x.get("text", "") for x in (a or []) if isinstance(x, dict)]


def dblp_best(title: str, lastname: str, year: int | None) -> dict | None:
    def search(q: str):
        url = "https://dblp.org/search/publ/api?" + urllib.parse.urlencode(
            {"q": q, "format": "json", "h": 10})
        status, data, _ = _request("GET", url, retries=2)
        if status != 200 or not isinstance(data, dict):
            return None
        return (((data.get("result") or {}).get("hits") or {}).get("hit") or [])

    hits = search(f"{title} {lastname}".strip()) or search(title) or []
    cands = []
    for h in hits:
        info = h.get("info") or {}
        jac = title_jaccard(title, info.get("title") or "")
        authors = _dblp_authors(info)
        y = int(info["year"]) if re.fullmatch(r"\d+", str(info.get("year") or "")) else None
        author_ok = bool(lastname) and any(lastname.lower() in a.lower() for a in authors)
        if not (jac >= 0.85 or (jac >= 0.5 and author_ok and year is not None and y == year)):
            continue
        venue = info.get("venue")
        if isinstance(venue, list):
            venue = venue[0] if venue else None
        if is_nonvenue(venue):
            continue
        kind = "conference" if "Conference" in (info.get("type") or "") else "journal"
        cands.append({"venue": venue, "y": y, "kind": kind, "info": info})
    if not cands:
        return None

    def score(c):
        s = 4
        s += 3 if c["kind"] == "conference" else 0
        s += 2 if year is not None and c["y"] == year else 0
        s += 1 if lookup_ranking(c["venue"]) else 0
        return s

    best = max(cands, key=score)
    return {
        "source": "dblp", "venue_raw": best["venue"], "year": best["y"],
        "venue_type": best["kind"], "external_doi": best["info"].get("doi"),
        "evidence_url": best["info"].get("url"), "citation_count": None,
        "influential_citations": None, "issn": None, "abbrev": None, "dblp_key": None,
    }


# --------------------------------------------------------------------- pipeline

_CITE_BUCKETS = [10000, 5000, 1000, 500, 100, 50, 10]


def cite_bucket(n: int | None) -> str | None:
    if n is None:
        return None
    for b in _CITE_BUCKETS:
        if n >= b:
            return f"{b}+"
    return "<10"


def choose_venue(hits: list[dict]) -> tuple[dict | None, dict | None]:
    scored = []
    for h in hits:
        if not h or not h.get("venue_raw"):
            continue
        row = lookup_ranking(h["venue_raw"])
        score = 0
        if row:
            score += 3
            if row["kind"] == "conference":
                score += 2
            if row["core"] == "A*":
                score += 1
        scored.append((score, h, row))
    if not scored:
        return None, None
    scored.sort(key=lambda x: -x[0])
    return scored[0][1], scored[0][2]


def confidence(hits: list[dict], chosen: dict | None, row: dict | None) -> float:
    if not chosen:
        return 0.0
    if not row:
        return 0.6
    agree = 0
    for h in hits:
        if not h or not h.get("venue_raw"):
            continue
        r = lookup_ranking(h["venue_raw"])
        if r and r["canonical"] == row["canonical"]:
            agree += 1
    return 0.95 if agree >= 2 else 0.85


def build_tags(res: dict) -> list[str]:
    tags = []
    if res.get("canonical"):
        tags.append(f"venue:{res['canonical']}")
    if res.get("year"):
        tags.append(f"year:{res['year']}")
    if res.get("core_tier"):
        tags.append(f"CORE:{res['core_tier']}")
    if res.get("ccf_rank"):
        tags.append(f"CCF:{res['ccf_rank']}")
    tags.append(f"acceptance:{res.get('acceptance', 'unknown')}")
    bucket = cite_bucket(res.get("citation_count"))
    if bucket:
        tags.append(f"cite:{bucket}")
    return tags


_PUBLISHER = {
    "CVPR": "IEEE", "ICCV": "IEEE", "WACV": "IEEE", "ICRA": "IEEE", "IROS": "IEEE",
    "ECCV": "Springer", "ICML": "PMLR", "AISTATS": "PMLR", "COLT": "PMLR", "UAI": "PMLR",
    "ACL": "ACL", "EMNLP": "ACL", "NAACL": "ACL",
    "KDD": "ACM", "WWW": "ACM", "SIGIR": "ACM", "SIGGRAPH": "ACM", "ACM MM": "ACM",
    "AAAI": "AAAI Press", "IJCAI": "IJCAI", "USENIX Security": "USENIX",
}
_TITLE_STOPWORDS = {"a", "an", "the", "of", "for", "and", "in", "on", "to", "at", "via"}
_TOOL_LINE = re.compile(
    r"^\s*(?:\d+\s+citations\s*\(semantic\s*scholar\)|citations:\s*\d+\s*\(semanticscholar\)|(?:zotero|arxiv)-marker:)",
    re.I)


def is_arxiv_doi(doi: str | None) -> bool:
    return bool(doi) and "arxiv" in doi.lower()


def smart_title(s: str) -> str:
    words = [w for w in s.split() if w]
    return " ".join(
        w.lower() if i and w.lower() in _TITLE_STOPWORDS else w[:1].upper() + w[1:]
        for i, w in enumerate(words)
    )


def full_name(canonical: str | None, raw: str | None) -> str:
    row = lookup_ranking(canonical or raw or "")
    if row and row["write_as"]:
        return row["write_as"]
    if raw and len(raw.split()) >= 2:
        return raw
    if row:
        cands = [row["canonical"], *row["aliases"]]
        longest = max(cands, key=len)
        if len(longest.split()) >= 2:
            return smart_title(longest)
    return raw or canonical or ""


def today() -> str:
    return _dt.date.today().isoformat()


def build_proposal(res: dict, s2hit: dict | None, arxiv_id: str | None,
                   data: dict, day: str) -> tuple[str | None, dict]:
    if res["acceptance"] != "accepted" or not res["canonical"]:
        return None, {}
    name = full_name(res["canonical"], res.get("venue_raw"))
    doi = issn = None
    abbrev = res["canonical"]
    if s2hit:
        if s2hit.get("external_doi") and not is_arxiv_doi(s2hit["external_doi"]):
            doi = s2hit["external_doi"]
        issn = s2hit.get("issn")
        abbrev = s2hit.get("abbrev") or res["canonical"]
    fields: dict = {}
    if (res.get("kind") or "conference") == "journal":
        itype = "journalArticle"
        fields["publicationTitle"] = name
        if abbrev:
            fields["journalAbbreviation"] = abbrev
        if issn:
            fields["ISSN"] = issn
    else:
        itype = "conferencePaper"
        fields["proceedingsTitle"] = name
        fields["conferenceName"] = name
        if _PUBLISHER.get(res["canonical"]):
            fields["publisher"] = _PUBLISHER[res["canonical"]]
    if doi:
        fields["DOI"] = doi

    lines = re.split(r"\r\n|\r|\n", data.get("extra") or "")
    kept = [ln for ln in lines if ln and not _TOOL_LINE.match(ln)]
    if arxiv_id and not any("arxiv" in ln.lower() for ln in kept):
        kept.append(f"arXiv:{arxiv_id}")
    if res.get("citation_count") is not None:
        kept.append(f"Citations: {res['citation_count']} (SemanticScholar) [{day}]")
    kept.append(f"arxiv-marker: resolved {day}")
    fields["extra"] = "\n".join(kept).strip()

    venue_field = "publicationTitle" if itype == "journalArticle" else "proceedingsTitle"
    if data.get("itemType") == itype and (data.get(venue_field) or "") == (fields.get(venue_field) or ""):
        return None, {}
    return itype, fields


def resolve_one(data: dict, key: str, s2map: dict[str, dict], or_cache: dict) -> dict:
    title = data.get("title") or ""
    arxiv_id = extract_arxiv_id(data)
    s2hit = s2map.get(arxiv_id) if arxiv_id else None
    hits = [h for h in [s2hit] if h]

    need_fallback = not s2hit or not s2hit.get("venue_raw")
    if not need_fallback:
        row = lookup_ranking(s2hit["venue_raw"])
        need_fallback = not (row and row["kind"] == "conference")

    year_hint = (s2hit or {}).get("year") or item_year(data)
    or_hit = None
    if need_fallback:
        authors = item_authors(data)
        cache_key = f"arxiv:{arxiv_id}" if arxiv_id else json.dumps(
            ["metadata", norm_title(title), authors, year_hint], ensure_ascii=False)
        if cache_key not in or_cache:
            or_cache[cache_key] = openreview_best(title, authors, year_hint, arxiv_id)
        or_hit = or_cache[cache_key]
        if or_hit:
            hits.append(or_hit)

    dblp_hit = None
    if need_fallback and not or_hit:
        dblp_hit = dblp_best(title, first_author_lastname(data), year_hint)
        if dblp_hit:
            hits.append(dblp_hit)

    chosen, row = choose_venue(hits)
    res = {
        "item_key": key,
        "title": title,
        "arxiv_id": arxiv_id,
        "venue_raw": chosen["venue_raw"] if chosen else None,
        "canonical": row["canonical"] if row else (chosen["venue_raw"] if chosen else None),
        "kind": row["kind"] if row else (chosen.get("venue_type") if chosen else None),
        "year": (chosen or {}).get("year") or (s2hit or {}).get("year"),
        "core_tier": row["core"] if row else None,
        "ccf_rank": (row.get("ccf") or None) if row else None,
        "acceptance": "accepted" if chosen else "unknown",
        "confidence": round(confidence(hits, chosen, row), 2),
        "citation_count": (s2hit or {}).get("citation_count"),
        "influential_citations": (s2hit or {}).get("influential_citations"),
        "sources": [h["source"] for h in hits],
        "evidence": [f"{h['source']}: {h.get('venue_raw') or '-'} ({h.get('evidence_url') or ''})" for h in hits],
        "current_item_type": data.get("itemType") or "preprint",
        "existing_tags": [t.get("tag", "") for t in (data.get("tags") or [])],
    }
    override = OVERRIDES.get(arxiv_id or "")
    if override and override.get("canonical"):
        orow = lookup_ranking(override["canonical"])
        res.update({
            "canonical": orow["canonical"] if orow else override["canonical"],
            "venue_raw": override["canonical"],
            "kind": orow["kind"] if orow else res["kind"],
            "year": override.get("year") or res["year"],
            "core_tier": orow["core"] if orow else None,
            "ccf_rank": (orow.get("ccf") or None) if orow else None,
            "acceptance": "accepted",
            "confidence": 1.0,
            "sources": ["override", *res["sources"]],
        })
    res["suggested_tags"] = build_tags(res)
    proposal_hit = chosen if (chosen and chosen["source"] == "openreview" and chosen.get("external_doi")) else s2hit
    itype, fields = build_proposal(res, proposal_hit, arxiv_id, data, today())
    res["target_item_type"] = itype
    res["fields"] = fields
    return res


# ------------------------------------------------------------------ zotero I/O

def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", **kw)


def zotero_json(*args: str) -> object:
    proc = _run(["zotero-cli", "--json", *args])
    if proc.returncode != 0:
        raise SystemExit(f"zotero-cli failed: {proc.stderr.strip() or proc.stdout.strip()}")
    payload = json.loads(proc.stdout)
    if not payload.get("ok"):
        raise SystemExit(f"zotero-cli error: {payload}")
    return payload.get("data")


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


def fetch_items(keys: list[str] | None, query: str | None, collection: str | None,
                limit: int) -> list[dict]:
    if keys:
        selected = keys
    elif query:
        args = ["search", "--detail", "keys_only", "--limit", str(limit)]
        if collection:
            args += ["--collection", collection]
        selected = _keys_from(zotero_json(*args, query))
    elif collection:
        selected = _keys_from(zotero_json("get", "collection-items", collection,
                                          "--detail", "keys_only", "--limit", str(limit)))
    else:
        selected = _keys_from(zotero_json("get", "recent", "--limit", str(limit)))

    out = []
    for key in selected:
        data = zotero_json("get", "metadata", key)
        item = data.get("data", {}) if isinstance(data, dict) else {}
        item.setdefault("key", key)
        out.append(item)
    return out


def ensure_zotero_mcp() -> None:
    try:
        import zotero_mcp  # noqa: F401
        return
    except ImportError:
        pass
    if os.environ.get("ARXIV_MARKER_REEXEC") == "1":
        raise SystemExit("zotero_mcp is not importable; run this with zotero-cli's own Python.")
    for cand in _zotero_python_candidates():
        if cand and Path(cand).exists():
            env = dict(os.environ, ARXIV_MARKER_REEXEC="1")
            os.execve(cand, [cand, str(Path(__file__).resolve()), *sys.argv[1:]], env)
    raise SystemExit(
        "Cannot import zotero_mcp. Set ZOTERO_MCP_PYTHON to the Python that owns zotero-cli.")


def _zotero_python_candidates():
    """Interpreters that may own zotero-cli: explicit env, uv tool, pipx."""
    explicit = os.environ.get("ZOTERO_MCP_PYTHON")
    if explicit:
        yield explicit
    try:
        proc = subprocess.run(["uv", "tool", "dir"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        if proc.returncode == 0:
            versions = Path(proc.stdout.strip()) / "versions"
            for venv in versions.glob("zotero-mcp-server*"):
                for rel in ("Scripts/python.exe", "bin/python", "bin/python3"):
                    p = venv / rel
                    if p.exists():
                        yield str(p)
    except Exception:
        pass
    for base in (Path.home() / ".local" / "share" / "pipx" / "venvs" / "zotero-mcp-server",
                 Path.home() / ".local" / "pipx" / "venvs" / "zotero-mcp-server"):
        for rel in ("Scripts/python.exe", "bin/python"):
            p = base / rel
            if p.exists():
                yield str(p)


def zotero_update(item_key: str, fields: dict, add_tags: list[str] | None = None,
                  replace_tags: list[str] | None = None) -> str:
    from zotero_mcp.cli import setup_zotero_environment
    from zotero_mcp.cli_standalone import CLIContext
    from zotero_mcp.tools import write as zwrite

    setup_zotero_environment()
    result = zwrite.update_item(
        item_key=item_key,
        fields=fields,
        tags=replace_tags,
        add_tags=add_tags or None,
        ctx=CLIContext(verbose=False),
    )
    return str(result)


# ------------------------------------------------------------------- commands

def cmd_resolve(args) -> int:
    keys = [k.strip() for k in args.keys.split(",")] if args.keys else None
    items = fetch_items(keys, args.query, args.collection, args.limit)
    if not items:
        print("No items found.", file=sys.stderr)
        return 1

    ids = sorted({aid for it in items if (aid := extract_arxiv_id(it))})
    print(f"Resolving {len(items)} item(s), {len(ids)} with an arXiv id…", file=sys.stderr)
    s2map = s2_batch(ids, args.s2_api_key) if ids else {}
    or_cache: dict = {}
    results = []
    for item in items:
        key = item.get("key") or item.get("itemKey")
        if not key:
            continue
        try:
            results.append(resolve_one(item, key, s2map, or_cache))
        except Exception as exc:
            results.append({"item_key": key, "title": item.get("title"), "error": str(exc),
                            "acceptance": "unknown", "confidence": 0.0})

    plan = {"generated": _dt.datetime.now().isoformat(timespec="seconds"), "results": results}
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    plan_path = Path(args.out) if args.out else STATE_DIR / f"plan-{int(time.time())}.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    else:
        _print_table(results, args.min_confidence)
        print(f"\nPlan written to {plan_path}")
    return 0


def _print_table(results: list[dict], min_conf: float) -> None:
    print(f"{'KEY':<9} {'CONF':<5} {'SRC':<16} {'VENUE':<10} {'YEAR':<5} {'CCF':<4} {'CORE':<4} {'TYPE':<16} TITLE")
    print("-" * 124)
    for r in results:
        mark = "*" if r.get("confidence", 0) >= min_conf and r.get("target_item_type") else " "
        print(f"{r.get('item_key',''):<9} {r.get('confidence',0):<5} "
              f"{','.join(r.get('sources') or [])[:16]:<16} "
              f"{str(r.get('canonical') or '-'):<10} {str(r.get('year') or '-'):<5} "
              f"{str(r.get('ccf_rank') or '-'):<4} "
              f"{str(r.get('core_tier') or '-'):<4} "
              f"{str(r.get('target_item_type') or '-'):<16} {mark}{(r.get('title') or '')[:60]}")
    print("\n* = ready to write at or above the confidence threshold")


def cmd_apply(args) -> int:
    ensure_zotero_mcp()
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    results = plan.get("results") or []
    selected = [
        r for r in results
        if r.get("target_item_type") and r.get("fields")
        and r.get("confidence", 0) >= args.min_confidence
        and (not args.keys or r["item_key"] in args.keys.split(","))
    ]
    if not selected:
        print("Nothing to write (no accepted results at or above the threshold).")
        return 0

    if not args.yes:
        _print_table(selected, args.min_confidence)
        print(f"\n{len(selected)} item(s) would be written. Re-run with --yes to apply.")
        return 0

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    snapshot = {"time": _dt.datetime.now().isoformat(timespec="seconds"), "items": []}
    for r in selected:
        data = zotero_json("get", "metadata", r["item_key"])
        snapshot["items"].append({"key": r["item_key"], "data": data.get("data", data)})
    snap_path = STATE_DIR / f"undo-{int(time.time())}.json"
    snap_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    ok = failed = 0
    for r in selected:
        try:
            msg = zotero_update(r["item_key"], r["fields"], r.get("suggested_tags") or [])
            if "error" in msg.lower() and "skip" not in msg.lower():
                print(f"  {r['item_key']}: {msg.strip()[:200]}")
                failed += 1
            else:
                ok += 1
                print(f"  {r['item_key']}: -> {r['target_item_type']} @ {r.get('canonical')}")
        except Exception as exc:
            failed += 1
            print(f"  {r['item_key']}: failed: {exc}")
    print(f"\nWrote {ok}, failed {failed}. Undo snapshot: {snap_path}")
    return 0 if failed == 0 else 1


def cmd_undo(args) -> int:
    ensure_zotero_mcp()
    snaps = sorted(STATE_DIR.glob("undo-*.json"), key=lambda p: p.stat().st_mtime)
    if not snaps:
        print("No undo snapshot found.")
        return 1
    snap = json.loads(snaps[-1].read_text(encoding="utf-8"))
    if not args.yes:
        print(f"Would restore {len(snap['items'])} item(s) from {snap['time']}.")
        print("Re-run with --yes to apply.")
        return 0
    restore_fields = ("proceedingsTitle", "conferenceName", "publicationTitle",
                      "journalAbbreviation", "ISSN", "publisher", "DOI", "extra")
    ok = 0
    for rec in snap["items"]:
        data = rec["data"]
        # Empty strings must not be sent: Zotero maps publicationTitle onto
        # proceedingsTitle for conference papers, so a blank one wipes the venue.
        fields = {k: data[k] for k in restore_fields if data.get(k)}
        fields["item_type"] = data.get("itemType")
        original_tags = [t.get("tag", "") for t in (data.get("tags") or []) if t.get("tag")]
        try:
            zotero_update(rec["key"], fields, replace_tags=original_tags)
            ok += 1
        except Exception as exc:
            print(f"  {rec['key']}: failed: {exc}")
    print(f"Restored {ok}/{len(snap['items'])} item(s).")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("resolve", help="Look up venues and write a plan")
    r.add_argument("--keys", help="Comma-separated Zotero item keys")
    r.add_argument("--query", help="Search query instead of keys")
    r.add_argument("--collection", help="Scope a query to a collection key")
    r.add_argument("--limit", type=int, default=50)
    r.add_argument("--s2-api-key", default=os.environ.get("S2_API_KEY"))
    r.add_argument("--min-confidence", type=float, default=0.8)
    r.add_argument("--json", action="store_true", help="Print the plan as JSON")
    r.add_argument("--out", help="Where to write the plan JSON")
    r.set_defaults(func=cmd_resolve)

    a = sub.add_parser("apply", help="Write a reviewed plan to Zotero")
    a.add_argument("--plan", required=True)
    a.add_argument("--min-confidence", type=float, default=0.8)
    a.add_argument("--keys", help="Only these item keys")
    a.add_argument("--yes", action="store_true")
    a.set_defaults(func=cmd_apply)

    u = sub.add_parser("undo", help="Restore the last applied plan")
    u.add_argument("--yes", action="store_true")
    u.set_defaults(func=cmd_undo)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
