#!/usr/bin/env python3
"""Set each Chinese attachment's title to its filename, without the .pdf suffix.

The plugin titled every Chinese PDF "Chinese Translation - arxiv2zh", which
says nothing about the paper. This aligns the title with the file name, e.g.

    文件名: Gao et al. - 2025 - Rethinking ... Serving_zh_CN.pdf
    标题:   Gao et al. - 2025 - Rethinking ... Serving_zh_CN

Titles are written through `zotero-cli edit`, so Zotero may stay open.
Run without --apply for a preview.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_DATA = Path(os.environ.get("ZOTERO_DATA_DIR", Path.home() / "Zotero"))


def open_readonly(db: Path) -> tuple[sqlite3.Connection, Path | None]:
    """Open read-only, or a snapshot when Zotero holds the database.

    Returns the connection and the snapshot directory (caller removes it).
    """
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.execute("SELECT 1 FROM itemAttachments LIMIT 1").fetchone()
        return con, None
    except sqlite3.OperationalError:
        tmp = Path(tempfile.mkdtemp(prefix="zotero-titles-"))
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(db) + suffix)
            if src.exists():
                shutil.copy2(src, tmp / src.name)
        return sqlite3.connect(tmp / db.name), tmp


def english_targets(con: sqlite3.Connection, added: str) -> list[dict]:
    """PDF attachments added on `added`, titled with their parent's title."""
    rows = con.execute(
        "SELECT ai.key, av.value AS att_title, pv.value AS parent_title "
        "FROM items pi "
        "JOIN itemAttachments ia ON ia.parentItemID = pi.itemID "
        "JOIN items ai ON ai.itemID = ia.itemID "
        "JOIN itemData aid ON aid.itemID = ai.itemID "
        "JOIN fields af ON af.fieldID = aid.fieldID AND af.fieldName = 'title' "
        "JOIN itemDataValues av ON av.valueID = aid.valueID "
        "JOIN itemData pid ON pid.itemID = pi.itemID "
        "JOIN fields pf ON pf.fieldID = pid.fieldID AND pf.fieldName = 'title' "
        "JOIN itemDataValues pv ON pv.valueID = pid.valueID "
        "WHERE date(pi.dateAdded) = ? AND ia.contentType = 'application/pdf' "
        "ORDER BY pi.key, ai.key",
        (added,),
    ).fetchall()
    out = []
    for key, att_title, parent_title in rows:
        out.append({"key": key, "current": att_title or "", "wanted": parent_title,
                    "status": "ok" if att_title == parent_title else "update"})
    return out


def targets(con: sqlite3.Connection) -> list[dict]:
    # INNER joins on fieldName='title': a LEFT JOIN here yields one row per
    # field (url, date, ...) and reads those values as the current title.
    rows = con.execute(
        "SELECT ai.key, ia.path, pv.value "
        "FROM itemAttachments ia "
        "JOIN items ai ON ai.itemID = ia.itemID "
        "JOIN itemData id ON id.itemID = ai.itemID "
        "JOIN fields f ON f.fieldID = id.fieldID AND f.fieldName = 'title' "
        "JOIN itemDataValues pv ON pv.valueID = id.valueID "
        "WHERE lower(ia.path) LIKE 'storage:%_zh_cn.pdf'"
    ).fetchall()
    out = []
    for key, path, title in rows:
        name = path[len("storage:"):]
        want = name[:-4] if name.lower().endswith(".pdf") else name
        out.append({"key": key, "current": title or "", "wanted": want,
                    "status": "ok" if title == want else "update"})
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data-dir", default=str(DEFAULT_DATA))
    p.add_argument("--apply", action="store_true")
    p.add_argument("--english", action="store_true",
                   help="Target English PDF attachments instead of Chinese ones")
    p.add_argument("--added", default=None,
                   help="With --english: items added on this date (YYYY-MM-DD, default today)")
    args = p.parse_args()

    db = Path(args.data_dir) / "zotero.sqlite"
    if not db.exists():
        print(f"database not found: {db}", file=sys.stderr)
        return 1

    con, snapshot = open_readonly(db)
    try:
        if args.english:
            added = args.added or __import__("datetime").date.today().isoformat()
            rows = english_targets(con, added)
            label = f"English PDF(s) added on {added}"
        else:
            rows = targets(con)
            label = "Chinese attachment(s)"
    finally:
        con.close()
        if snapshot:
            shutil.rmtree(snapshot, ignore_errors=True)

    todo = [r for r in rows if r["status"] == "update"]
    print(f"{len(rows)} {label}: {len(todo)} title(s) to update\n")
    for r in rows:
        print(f"{r['key']}  [{r['status']}]")
        if r["status"] == "update":
            print(f"   now: {r['current'][:100]}")
            print(f"   new: {r['wanted'][:100]}")

    if not args.apply:
        print("\nPreview only. Re-run with --apply to write the titles.")
        return 0
    if not todo:
        return 0

    ok = failed = 0
    for r in todo:
        proc = subprocess.run(["zotero-cli", "edit", r["key"], "--title", r["wanted"]],
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.returncode == 0:
            ok += 1
            print(f"  {r['key']}: title updated")
        else:
            failed += 1
            print(f"  {r['key']}: failed: {(proc.stderr or proc.stdout).strip()[:200]}")
    print(f"\nUpdated {ok}, failed {failed}.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
