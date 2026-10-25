"""Find the published version of an arXiv preprint across several sources (read-only).

Derived from arxiv-workflow (https://github.com/AllanChain/zotero-arxiv-workflow),
Copyright (c) Allan Chain, AGPL-3.0-or-later.
"""

from __future__ import annotations

import json
import re
import urllib.parse

import common

ALL_SOURCES = ("doi", "s2", "dblp", "pubmed", "crossref")


def is_preprint_doi(doi: str | None) -> bool:
    return bool(doi) and "arxiv" in doi.lower()


def _published(doi: str | None, source: str) -> list[dict]:
    if not doi or doi.lower() in ("na", "n/a", "none") or is_preprint_doi(doi):
        return []
    return [{"source": source, "kind": "exact", "doi": doi, "score": 1.0,
             "title": "Published PDF", "url": f"https://doi.org/{doi}"}]


def related_doi(item: dict, arxiv_id: str | None) -> list[dict]:
    """The published DOI a preprint page links to.

    arXiv embeds it as data-doi; bioRxiv/medRxiv expose it through their
    details API; chemRxiv through the item's version-of-record record.
    """
    url = str(item.get("url") or "")
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return []

    if host.endswith("arxiv.org"):
        html = common.fetch_text(url)
        m = re.search(r'data-doi="([^"]+)"', html)
        return _published(m.group(1) if m else None, "relatedDOI")

    if host in ("www.biorxiv.org", "biorxiv.org", "www.medrxiv.org", "medrxiv.org"):
        m = re.search(r"/([\d./]+)v\d+$", url)
        if not m:
            return []
        server = "biorxiv" if "biorxiv" in host else "medrxiv"
        data = common.fetch_json(f"https://api.{server}.org/details/{server}/{m.group(1)}")
        collection = ((data or {}).get("collection") or []) if isinstance(data, dict) else []
        doi = collection[0].get("published") if collection else None
        return _published(doi, "relatedDOI")

    if host.endswith("chemrxiv.org"):
        m = re.search(r"/([0-9a-f]+)$", url, re.I)
        if not m:
            return []
        data = common.fetch_json(
            "https://chemrxiv.org/engage/chemrxiv/public-api/v1/items/" + m.group(1))
        doi = ((data or {}).get("vor") or {}).get("vorDoi") if isinstance(data, dict) else None
        return _published(doi, "relatedDOI")

    return []


def semantic_scholar(arxiv_id: str | None) -> list[dict]:
    if not arxiv_id:
        return []
    data = common.fetch_json(
        f"https://api.semanticscholar.org/graph/v1/paper/ARXIV:{common.q(arxiv_id)}"
        "?fields=externalIds,title,year,venue")
    if not isinstance(data, dict):
        return []
    doi = (data.get("externalIds") or {}).get("DOI")
    if not doi or is_preprint_doi(doi):
        return []
    return [{"source": "semantic_scholar", "kind": "exact", "doi": doi, "score": 1.0,
             "title": data.get("title"), "year": data.get("year"),
             "venue": data.get("venue"), "url": f"https://doi.org/{doi}"}]


def dblp(title: str, lastname: str, year: int | None) -> list[dict]:
    query = f"{title} {lastname}".strip()
    data = common.fetch_json(
        f"https://dblp.org/search/publ/api?q={common.q(query)}&format=json&h=100")
    hits = (((data or {}).get("result") or {}).get("hits") or {}).get("hit") or []
    out = []
    for hit in hits:
        info = hit.get("info") or {}
        cand_title = info.get("title")
        if not isinstance(cand_title, str):
            continue
        match = common.evaluate_title_pair(title, cand_title)
        if match["kind"] == "reject":
            continue
        if info.get("venue") == "CoRR":
            continue
        doi = info.get("doi")
        if doi and not is_preprint_doi(str(doi)):
            target = {"doi": doi, "url": f"https://doi.org/{doi}"}
        else:
            url = info.get("ee") or info.get("url")
            if not url or "arxiv.org" in url:
                continue
            if "openreview.net" in url:
                url = f"https://dblp.org/rec/{info.get('key')}.html?view=bibtex"
            target = {"url": url}
        authors_field = info.get("authors")
        if isinstance(authors_field, dict):
            authors = authors_field.get("author")
        elif isinstance(authors_field, list):
            authors = authors_field
        else:
            authors = None
        first = authors[0].get("text") if isinstance(authors, list) and authors else (
            authors.get("text") if isinstance(authors, dict) else None)
        if match["kind"] == "fuzzy":
            if not common.surname_matches(lastname, first) or not common.year_gate(year, info.get("year")):
                continue
        out.append({"source": "dblp", "kind": match["kind"], "score": round(match["score"], 3),
                    "title": cand_title, "year": info.get("year"), "venue": info.get("venue"),
                    **target})
    return out


def pubmed(title: str, lastname: str, year: int | None) -> list[dict]:
    search = common.fetch_json(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        f"?db=pubmed&term={common.q(title)}&retmode=json")
    ids = (((search or {}).get("esearchresult") or {}).get("idlist") or [])[:10]
    if not ids:
        return []
    summary = common.fetch_json(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
        f"?db=pubmed&id={','.join(ids)}&retmode=json")
    out = []
    for pid in ids:
        info = ((summary or {}).get("result") or {}).get(pid)
        if not isinstance(info, dict) or not isinstance(info.get("title"), str):
            continue
        match = common.evaluate_title_pair(title, info["title"])
        if match["kind"] == "reject":
            continue
        doi = next((x.get("value") for x in info.get("articleids") or []
                    if x.get("idtype") == "doi"), None)
        if not doi:
            continue
        authors = info.get("authors") or []
        first = authors[0].get("name") if authors else None
        if match["kind"] == "fuzzy":
            if not common.surname_matches(lastname, first) or not common.year_gate(year, info.get("pubdate")):
                continue
        out.append({"source": "pubmed", "kind": match["kind"], "score": round(match["score"], 3),
                    "doi": doi, "title": info["title"], "year": info.get("pubdate"),
                    "venue": info.get("fulljournalname"),
                    "url": f"https://pubmed.ncbi.nlm.nih.gov/{pid}/"})
    return out


def crossref(title: str, lastname: str, year: int | None) -> list[dict]:
    params = {
        "query.title": title,
        "rows": "20",
        "select": "DOI,title,author,issued,container-title",
        "filter": "type:journal-article,type:proceedings-article,type:book-chapter",
    }
    if lastname:
        params["query.author"] = lastname
    from urllib.parse import urlencode
    data = common.fetch_json("https://api.crossref.org/works?" + urlencode(params))
    works = ((data or {}).get("message") or {}).get("items") or []
    out = []
    for work in works:
        titles = work.get("title") or []
        cand_title = titles[0] if titles else None
        if not isinstance(cand_title, str):
            continue
        match = common.evaluate_title_pair(title, cand_title)
        if match["kind"] == "reject":
            continue
        doi = work.get("DOI")
        if not doi:
            continue
        first = (work.get("author") or [{}])[0].get("family")
        cand_year = ((work.get("issued") or {}).get("date-parts") or [[None]])[0][0]
        if match["kind"] == "fuzzy":
            if not common.surname_matches(lastname, first) or not common.year_gate(year, cand_year):
                continue
        out.append({"source": "crossref", "kind": match["kind"], "score": round(match["score"], 3),
                    "doi": doi, "title": cand_title, "year": cand_year,
                    "venue": (work.get("container-title") or [None])[0],
                    "url": f"https://doi.org/{doi}"})
    return out


def find(item: dict, sources: set[str]) -> list[dict]:
    title = item.get("title") or ""
    arxiv_id = common.extract_arxiv_id(item)
    lastname = common.first_author_lastname(item)
    year = common.item_year(item)
    out: list[dict] = []
    if "doi" in sources:
        out += related_doi(item, arxiv_id)
    if "s2" in sources:
        out += semantic_scholar(arxiv_id)
    if "dblp" in sources:
        out += dblp(title, lastname, year)
    if "pubmed" in sources:
        out += pubmed(title, lastname, year)
    if "crossref" in sources:
        out += crossref(title, lastname, year)
    return out


def run(args) -> int:
    items = common.fetch_items(args.keys, args.query, args.collection, args.limit)
    if not items:
        common.err("No items found.")
        return 1
    sources = set(args.sources.split(",")) if args.sources else set(ALL_SOURCES)

    results = []
    for item in items:
        key = item.get("key")
        found = find(item, sources)
        results.append({"key": key, "title": item.get("title"),
                        "arxiv": common.extract_arxiv_id(item), "candidates": found})

    if args.json:
        print(json.dumps({"sources": sorted(sources), "results": results},
                         ensure_ascii=False, indent=2))
        return 0

    for row in results:
        print(f"\n{row['key']}  arXiv:{row['arxiv'] or '-'}  {str(row['title'] or '')[:70]}")
        if not row["candidates"]:
            print("  no published version found")
            continue
        seen = set()
        for c in row["candidates"]:
            ident = c.get("doi") or c.get("url") or ""
            if ident in seen:
                continue
            seen.add(ident)
            print(f"  {c['source']:<16} {c['kind']:<5} score={c['score']:<5} "
                  f"{str(c.get('year') or '-'):<5} {str(c.get('venue') or '-')[:30]:<30} {ident}")
    return 0


def add_parser(sub) -> None:
    p = sub.add_parser("find", help="Find the published version of preprints (read-only)")
    p.add_argument("--keys", help="Comma-separated Zotero item keys")
    p.add_argument("--query", help="Search query instead of keys")
    p.add_argument("--collection", help="Scope to a collection key")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--sources", help="Comma list of doi,s2,dblp,pubmed,crossref")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=run)
