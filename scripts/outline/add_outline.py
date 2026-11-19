#!/usr/bin/env python3
"""Write a PDF outline (bookmarks) for a paper that shipped without one.

Papers from arXiv, MLSys, and machine-translated copies often carry no
`/Outlines` tree, so Zotero's outline pane and Jasminum's 大纲 panel both stay
empty.  This command rebuilds a 1–3 level outline from the PDF's own text:

    * numbered headings   1 / 2.1 / 3.1.2   (level from the dot count)
    * unnumbered big type Abstract, References, Appendix A …
    * a number printed on its own line is joined to the title below it

and writes it back as real PDF bookmarks, so WPS, Acrobat, Zotero's native
outline pane, and every other reader see it.

`--jasminum` additionally fills `jasminum-outline.json` next to the PDF.  The
Jasminum plugin never re-reads a PDF once that sidecar exists — an empty
`{"outline":[]}` created on first open is cached forever — so the sidecar is
the only way to make its own pane show anything.

Preview by default; --apply writes.  A PDF is backed up before it is touched.

    zotero-plus outline --keys DMI9KNFZ                 # preview
    zotero-plus outline --keys DMI9KNFZ --apply         # write bookmarks
    zotero-plus outline --keys DMI9KNFZ --apply --jasminum
    zotero-plus outline --path paper.pdf --apply
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover - environment problem, not a bug
    print("PyMuPDF is required: pip install pymupdf", file=sys.stderr)
    raise SystemExit(2)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from workflow.common import attachment_paths, get_item, pdf_attachments  # noqa: E402

KEEP_BACKUPS = 3
JASMINUM_SCHEMA = 2

BAD_TITLE_START = ".。，,;、：:)]}"


def good_title(title: str) -> bool:
    """A heading title starts with a word, not a number or punctuation."""
    if not title:
        return False
    if title[0] in BAD_TITLE_START or title[0].isdigit():
        return False
    if title[0].isascii() and title[0].isalpha() and not title[0].isupper():
        return False        # "2.5.6 in the transformers library." is body text
    return any(ch.isalpha() for ch in title)


HEAD_NUM_ONLY = re.compile(r"^(?:\d{1,2}(?:\.\d{1,2}){0,2}|[A-G])$")
NUM_HEAD = re.compile(r"^(?P<num>\d{1,2}(?:\.\d{1,2}){0,3})[\s　]*(?P<title>\S.*)$")
APP_HEAD = re.compile(r"^(?P<num>[A-Z])(?:\.(?P<sub>\d{1,2}))?[\s　]+(?P<title>[^\s].*)$")
PLAIN_HEADS = {"摘要", "Abstract", "致谢", "Acknowledgments", "Acknowledgements",
               "参考文献", "References", "Bibliography", "Conclusion", "Conclusions",
               "结论", "引言", "Introduction", "Related Work", "相关工作"}
CAPTION = re.compile(r"^(Table|Figure|Fig\.|Algorithm|Listing|式|图|表)\s*\d+", re.I)


# --------------------------------------------------------------------------- state

def default_state_dir() -> Path:
    override = os.environ.get("ZOTERO_PLUS_STATE_DIR")
    return Path(override) if override else Path.home() / ".cache" / "zotero-plus"


def prune_backups(directory: Path, prefix: str, keep: int = KEEP_BACKUPS) -> list[Path]:
    try:
        files = sorted(directory.glob(f"{prefix}-*.pdf"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return []
    removed = []
    for old in files[:-keep] if len(files) > keep else []:
        try:
            old.unlink()
            removed.append(old)
        except OSError:
            pass
    return removed


# ----------------------------------------------------------------------- extraction

def body_font_size(doc) -> float:
    """The size that carries the most characters — the body text."""
    weight: dict[float, int] = {}
    for page in doc:
        for block in page.get_text("dict").get("blocks", []):
            for line in block.get("lines", []):
                for span in line["spans"]:
                    text = span["text"].strip()
                    if not text:
                        continue
                    size = round(span["size"], 1)
                    weight[size] = weight.get(size, 0) + len(text)
    if not weight:
        return 10.5
    return max(weight.items(), key=lambda kv: kv[1])[0]


def is_bold(span: dict) -> bool:
    name = span["font"].lower()
    if "bold" in name or "black" in name or "heavy" in name or "semibold" in name:
        return True
    return bool(span.get("flags", 0) & 16)


def column_origins(page, body: float) -> list[float]:
    """Left edge of each text column: the two most common x starts."""
    counts: dict[float, int] = {}
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            for span in line["spans"]:
                if not span["text"].strip() or abs(span["size"] - body) > 0.6:
                    continue
                x = round(span["bbox"][0] / 2) * 2
                counts[x] = counts.get(x, 0) + len(span["text"])
    if not counts:
        return [page.rect.width * 0.09]
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    left = min(x for x, _ in ranked[:3])
    origins = [float(left)]
    mid = page.rect.width * 0.45
    for x, _ in ranked:
        if x >= mid:
            origins.append(float(x))
            break
    return origins


def is_two_column(page, body: float, origins: list[float]) -> bool:
    """True when the page really has a right-hand text column.

    A title page has a centred Abstract that sits right of the midpoint, so
    the x-origins alone over-count columns; a column needs a run of lines.
    """
    if len(origins) < 2:
        return False
    cut = origins[-1] - 15
    count = 0
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            for span in line["spans"]:
                if span["text"].strip() and abs(span["size"] - body) <= 0.6 \
                        and span["bbox"][0] >= cut:
                    count += 1
    return count >= 8


def page_rows(page, body: float, origins: list[float]) -> list[dict]:
    """Spans → visual rows, one row per (column, baseline)."""
    spans = []
    # The gutter sits just left of the right column's origin; the midpoint of
    # the two origins would land inside the left column of an asymmetric layout.
    two_col = is_two_column(page, body, origins)
    cut = origins[-1] - 15 if two_col else page.rect.width
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            for span in line["spans"]:
                if not span["text"].strip():
                    continue
                x0, y0, x1, _ = span["bbox"]
                spans.append({"x0": x0, "y0": y0, "x1": x1, "size": span["size"],
                              "bold": is_bold(span), "text": span["text"],
                              "col": 1 if x0 >= cut else 0})
    spans.sort(key=lambda s: (s["col"], s["y0"], s["x0"]))

    rows: list[dict] = []
    for span in spans:
        if rows and rows[-1]["col"] == span["col"] and abs(span["y0"] - rows[-1]["y0"]) <= 5:
            rows[-1]["spans"].append(span)
            rows[-1]["y0"] = min(rows[-1]["y0"], span["y0"])
        else:
            rows.append({"col": span["col"], "y0": span["y0"], "spans": [span]})

    out = []
    for row in rows:
        parts = sorted(row["spans"], key=lambda s: s["x0"])
        text, prev_x1 = "", None
        for part in parts:
            if prev_x1 is not None and part["x0"] - prev_x1 > 2.0:
                text += " "
            text += part["text"]
            prev_x1 = part["x1"]
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        out.append({
            "col": row["col"], "y0": row["y0"],
            "x0": min(p["x0"] for p in parts),
            "x1": max(p["x1"] for p in parts),
            "size": max(p["size"] for p in parts),
            "bold": any(p["bold"] for p in parts),
            "text": text,
        })
    return out


def merge_number_rows(rows: list[dict], origins: list[float],
                      height: float, body: float) -> list[dict]:
    """LaTeX puts the section number and its title in separate text runs.

    A row that is nothing but `2` or `3.1` is joined to the next row in the
    same column when that row starts a little to its right.  Rows outside the
    text block (running page numbers) are left alone.
    """
    out: list[dict] = []
    i = 0
    while i < len(rows):
        row = rows[i]
        at_origin = any(abs(row["x0"] - o) < 36 for o in origins)
        in_block = 0.04 * height < row["y0"] < 0.96 * height
        if not (HEAD_NUM_ONLY.match(row["text"]) and at_origin and in_block
                and i + 1 < len(rows)):
            out.append(row)
            i += 1
            continue
        nxt = rows[i + 1]
        # A bare number is only a heading number when something sets it apart
        # from body text; a table row or a page number must not swallow the
        # next line.
        stands_out = (row["bold"] or nxt["bold"] or row["size"] > body * 1.03
                      or nxt["size"] > body * 1.03)
        if stands_out:
            gap = nxt["x0"] - row["x1"]
            if (nxt["col"] == row["col"] and 0 <= gap <= 40
                    and abs(nxt["y0"] - row["y0"]) <= 25):
                out.append({**nxt, "text": f"{row['text']} {nxt['text']}",
                            "y0": row["y0"], "x0": row["x0"],
                            "size": max(row["size"], nxt["size"]),
                            "bold": row["bold"] or nxt["bold"]})
                i += 2
                continue
        out.append(row)
        i += 1
    return out


def join_wrapped_headings(rows: list[dict], body: float) -> list[dict]:
    """A long heading that wrapped onto a second line stays one entry."""
    out: list[dict] = []
    for row in rows:
        if out:
            prev = out[-1]
            joins = (
                prev["col"] == row["col"]
                and prev["bold"] and prev["size"] > body * 1.05
                and row["bold"] and abs(row["size"] - prev["size"]) < 0.6
                and -2 <= row["x0"] - prev["x0"] < 40   # same line or wrapped
                and 0 < row["y0"] - prev["y0"] <= 22
                and len(prev["text"]) < 80
                and not NUM_HEAD.match(row["text"])      # the next heading
                and not APP_HEAD.match(row["text"])
                and row["text"][:1].isupper()
            )
            if joins:
                prev["text"] = f"{prev['text']} {row['text']}"
                prev["x1"] = max(prev["x1"], row["x1"])
                continue
        out.append(dict(row))
    return out


def column_edges(page, body: float, origins: list[float]) -> dict[int, float]:
    """Right edge of the text block, per column."""
    two_col = is_two_column(page, body, origins)
    cut = origins[-1] - 15 if two_col else page.rect.width
    edges: dict[int, float] = {}
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            for span in line["spans"]:
                if not span["text"].strip() or abs(span["size"] - body) > 0.6:
                    continue
                col = 1 if span["bbox"][0] >= cut else 0
                edges[col] = max(edges.get(col, 0.0), span["bbox"][2])
    return {col: (edge or page.rect.width * 0.94) for col, edge in edges.items()} or {0: page.rect.width * 0.94}


def detect(doc, max_level: int) -> list[dict]:
    """Entries as {level, title, page(1-based), x, y_top}."""
    body = body_font_size(doc)
    entries: list[dict] = []

    for page in doc:
        origins = column_origins(page, body)
        rows = join_wrapped_headings(
            merge_number_rows(page_rows(page, body, origins), origins,
                              page.rect.height, body), body)
        edges = column_edges(page, body, origins)

        for row in rows:
            text = row["text"].strip()
            if not text or len(text) > 100:
                continue
            at_origin = any(abs(row["x0"] - o) < 36 for o in origins)
            edge = edges.get(row["col"], max(edges.values()))
            short = row["x1"] < edge - 12
            bigger = row["size"] >= body * 1.06
            # A heading set in large bold type may fill the whole column;
            # only plain body-width lines need to stop short.
            heading_like = short or (row["bold"] and bigger)

            # numbered heading: 1 / 2.1 / 3.1.2
            m = NUM_HEAD.match(text)
            if m and at_origin and heading_like and not CAPTION.match(text):
                num, title = m.group("num"), m.group("title").strip()
                level = num.count(".") + 1
                if len(num) > 1 and num[0] == "0":       # 0.5 is a probability
                    continue
                if level > max_level or not good_title(title):
                    continue
                aligned = any(abs(row["x0"] - o) < 8 for o in origins)
                if level == 1 and not (bigger or row["bold"]):
                    continue
                if level > 1 and not (bigger or row["bold"] or aligned):
                    continue
                entries.append({"level": level, "title": f"{num} {title}",
                                "page": page.number + 1, "x": row["x0"],
                                "y_top": row["y0"]})
                continue

            # appendix heading: A Evaluation Details / A.1 Dataset
            m = APP_HEAD.match(text)
            if (m and m.group("num") in "ABCDEFG" and at_origin and heading_like
                    and (bigger or row["bold"]) and len(text) <= 80
                    and not CAPTION.match(text)):
                num, sub = m.group("num"), m.group("sub")
                title = m.group("title").strip().rstrip("。.")
                level = 2 if sub else 1
                if level <= max_level and good_title(title):
                    label = f"{num}.{sub}" if sub else num
                    entries.append({"level": level, "title": f"{label} {title}",
                                    "page": page.number + 1, "x": row["x0"],
                                    "y_top": row["y0"]})
                continue

            # unnumbered banner: Abstract / References / 致谢
            if text in PLAIN_HEADS and bigger:
                entries.append({"level": 1, "title": text,
                                "page": page.number + 1, "x": row["x0"],
                                "y_top": row["y0"]})

    return normalize(entries, max_level)


def normalize(entries: list[dict], max_level: int) -> list[dict]:
    """Enforce a sane tree and drop duplicates."""
    out: list[dict] = []
    seen: set[tuple] = set()
    for entry in entries:
        level = min(entry["level"], max_level)
        if out and level > out[-1]["level"] + 1:
            level = out[-1]["level"] + 1
        key = (level, entry["title"], entry["page"])
        if key in seen:
            continue
        seen.add(key)
        out.append({**entry, "level": level})
    # an outline must start at level 1
    while out and out[0]["level"] > 1:
        out[0]["level"] = 1
    return out


def with_positions(entries: list[dict], doc, max_level: int) -> list[dict]:
    """Give bookmarks that already exist the coordinates of their headings.

    A bookmark written by another tool often points at the page origin, which
    makes every jump land at the top of the page. The heading's own position is
    recovered from the text and matched by (page, title).
    """
    found = {(e["page"], e["title"]): e for e in detect(doc, max_level)}
    for entry in entries:
        match = found.get((entry["page"], entry["title"]))
        if match:
            entry["x"], entry["y_top"] = match["x"], match["y_top"]
    return entries


def read_existing(doc) -> list[dict]:
    entries = []
    for item in doc.get_toc(simple=False):
        level, title, page = item[0], item[1], item[2]
        dest = item[3] if len(item) > 3 else {}
        point = dest.get("to") if isinstance(dest, dict) else None
        entries.append({"level": level, "title": title.strip(), "page": page,
                        "x": float(point.x) if point else 72.0,
                        "y_top": float(point.y) if point else 36.0})
    return entries


# ------------------------------------------------------------------------ writing

def tree_of(entries: list[dict], doc) -> list[dict]:
    """Nest entries; y is converted to PDF space (origin bottom-left)."""
    root: list[dict] = []
    stack: list[dict] = []
    for entry in entries:
        height = doc[entry["page"] - 1].rect.height
        node = {"level": entry["level"], "title": entry["title"],
                "page": entry["page"], "x": round(entry["x"], 1),
                "y": round(height - entry["y_top"], 1),
                "children": [], "collapsed": False}
        while stack and stack[-1]["level"] >= node["level"]:
            stack.pop()
        (stack[-1]["children"] if stack else root).append(node)
        stack.append(node)
    return root


def write_pdf(doc, entries: list[dict], path: Path,
              backup_dir: Path | None) -> dict:
    result: dict = {"backup": None, "pruned": []}
    if backup_dir is not None:
        backup_dir.mkdir(parents=True, exist_ok=True)
        prefix = path.stem[:40]
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = backup_dir / f"{prefix}-{stamp}.pdf"
        shutil.copy2(path, backup)
        result["backup"] = backup
        result["pruned"] = prune_backups(backup_dir, prefix)

    doc.set_toc([[e["level"], e["title"], e["page"]] for e in entries])
    tmp = path.with_name(path.name + ".zotero-plus.tmp")
    doc.save(tmp, garbage=4, deflate=True)
    # Windows refuses to replace a file that is still open, and PyMuPDF holds
    # the handle, so the document is closed before the swap.
    doc.close()
    try:
        os.replace(tmp, path)
    except PermissionError:
        tmp.unlink(missing_ok=True)
        raise SystemExit(f"cannot overwrite {path.name}: close it in Zotero first")
    return result


def write_jasminum(doc, entries: list[dict], path: Path) -> Path:
    sidecar = path.parent / "jasminum-outline.json"
    info = {"itemID": 0, "schema": JASMINUM_SCHEMA,
            "jasminumVersion": "1.1.47", "baseFontSize": 12}
    if sidecar.exists():
        try:
            old = json.loads(sidecar.read_text(encoding="utf-8"))
            info["itemID"] = old.get("info", {}).get("itemID", 0)
            info["baseFontSize"] = old.get("info", {}).get("baseFontSize", 12)
            info["jasminumVersion"] = old.get("info", {}).get(
                "jasminumVersion", info["jasminumVersion"])
        except Exception:
            pass
    payload = {"info": info, "outline": tree_of(entries, doc)}
    sidecar.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return sidecar


# --------------------------------------------------------------------------- main

def split_keys(value: str | None) -> set[str] | None:
    if not value:
        return None
    keys = {k.strip().upper() for k in value.split(",") if k.strip()}
    return keys or None


def targets(args) -> list[dict]:
    """[{label, path, att_key, item_key}] in the order given."""
    out: list[dict] = []
    for raw in args.path or []:
        path = Path(raw).expanduser()
        out.append({"label": path.name, "path": path, "att_key": path.parent.name,
                    "item_key": ""})

    keys = split_keys(args.keys)
    attachment_keys = split_keys(args.attachment_keys)
    if not keys and not attachment_keys:
        return out

    if attachment_keys and not keys:
        # An attachment key resolves to its own file; no parent lookup needed.
        for akey in sorted(attachment_keys):
            path = attachment_paths(akey).get(akey)
            if not path or not path.lower().endswith(".pdf"):
                print(f"not found: {akey} (no PDF for that attachment key)",
                      file=sys.stderr)
                continue
            out.append({"label": f"{akey} {Path(path).name}", "path": Path(path),
                        "att_key": akey, "item_key": ""})
        return out

    for key in sorted(keys or set()):
        try:
            item = get_item(key)
        except SystemExit as exc:
            print(f"{key}: {exc}", file=sys.stderr)
            continue
        if item.get("itemType") in ("attachment", "note", "annotation"):
            continue
        for att in pdf_attachments(key):
            if attachment_keys and att["key"].upper() not in attachment_keys:
                continue
            out.append({"label": f"{key} {att['key']} {att['name']}",
                        "path": Path(att["path"]), "att_key": att["key"],
                        "item_key": key})
    return out


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--keys", help="Comma-separated Zotero item keys")
    p.add_argument("--attachment-keys", help="Comma-separated attachment keys")
    p.add_argument("--path", action="append",
                   help="A PDF file outside Zotero (repeatable)")
    p.add_argument("--apply", action="store_true", help="Write the bookmarks")
    p.add_argument("--jasminum", action="store_true",
                   help="Also write jasminum-outline.json next to the PDF")
    p.add_argument("--force", action="store_true",
                   help="Re-detect from text even when the PDF already has bookmarks")
    p.add_argument("--max-level", type=int, default=3, choices=(1, 2, 3))
    p.add_argument("--json", action="store_true", help="Print the plan as JSON")
    p.add_argument("--backup-dir", default=None)
    args = p.parse_args()

    if not (args.path or args.keys or args.attachment_keys):
        p.error("give --keys, --attachment-keys or --path")

    def say(msg: str = "") -> None:
        if not args.json:
            print(msg, flush=True)

    backup_dir = (Path(args.backup_dir) if args.backup_dir
                  else default_state_dir() / "pdf-backups")
    report = []
    failures = 0

    for target in targets(args):
        path: Path = target["path"]
        say(f"\n{target['label']}")
        if not path.exists():
            print(f"  missing: {path}", file=sys.stderr)
            failures += 1
            continue

        doc = fitz.open(path)
        existing = read_existing(doc)
        if existing and not args.force:
            entries, source = existing, "existing"
            say(f"  bookmarks: {len(existing)} already present")
            if not args.jasminum:
                say("  nothing to do (use --force to rebuild from text,"
                    " --jasminum to sync the sidecar)")
                report.append({**target, "path": str(path), "entries": entries,
                               "source": source, "wrote_pdf": False})
                doc.close()
                continue
        else:
            entries, source = detect(doc, args.max_level), "detected"
            say("  bookmarks: rebuilt" if existing else "  bookmarks: none")
            say(f"  detected: {len(entries)} entries")

        if not entries:
            say("  no headings found — this PDF may be a scan"
                " (run OCR first) or have no heading numbering")
            failures += 1
            doc.close()
            continue

        for entry in entries:
            say(f"    {'  ' * (entry['level'] - 1)}{entry['title']}  [{entry['page']}]")

        if not args.apply:
            say("  preview only — re-run with --apply")
            report.append({**target, "path": str(path), "entries": entries,
                           "source": source, "wrote_pdf": False})
            doc.close()
            continue

        try:
            wrote_pdf = False
            if source != "existing":
                result = write_pdf(doc, entries, path, backup_dir)
                if result["backup"]:
                    say(f"  backup: {result['backup']}")
                for old in result["pruned"]:
                    say(f"  pruned: {old}")
                say(f"  wrote {len(entries)} bookmarks into {path.name}")
                wrote_pdf = True
                if args.jasminum:
                    # write_pdf closed the document to swap the file
                    with fitz.open(path) as fresh:
                        sidecar = write_jasminum(fresh, entries, path)
                    say(f"  wrote {sidecar}")
                    say("  reopen the PDF tab in Zotero so Jasminum reads it")
            elif args.jasminum:
                entries = with_positions(entries, doc, args.max_level)
                sidecar = write_jasminum(doc, entries, path)
                say(f"  wrote {sidecar}")
                say("  reopen the PDF tab in Zotero so Jasminum reads it")
        finally:
            if not doc.is_closed:
                doc.close()

        report.append({**target, "path": str(path), "entries": entries,
                       "source": source, "wrote_pdf": wrote_pdf})

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if failures and not report else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
