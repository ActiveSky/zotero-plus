#!/usr/bin/env python3
"""Rename PDF attachments using only the parent item's title.

For every item that has a Chinese attachment:

    English PDFs  -> <title>.pdf, <title> (2).pdf, ...
    Chinese PDFs  -> <title>_zh_CN.pdf

Displayed titles follow: English attachments get the paper title, Chinese ones
get the filename without .pdf. Author/year prefixes are dropped because the
item metadata already carries them.

Zotero must be closed for --apply. Run without --apply for a preview.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_DATA = Path(os.environ.get("ZOTERO_DATA_DIR", Path.home() / "Zotero"))


def zotero_running() -> bool:
    try:
        if os.name == "nt":
            proc = subprocess.run(["tasklist", "/FI", "IMAGENAME eq zotero.exe", "/NH"],
                                  capture_output=True, text=True, errors="replace")
            return "zotero.exe" in proc.stdout.lower()
        proc = subprocess.run(["pgrep", "-x", "zotero"], capture_output=True)
        return proc.returncode == 0
    except Exception:
        return True


def sanitize(title: str, limit: int = 140) -> str:
    name = title.replace("/", "-").replace("\\", "-").replace(":", " -")
    name = re.sub(r'[<>"|?*]', "", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > limit:
        name = name[:limit].rstrip(" .")
    return name or "document"


def open_db(db: Path) -> sqlite3.Connection:
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.execute("SELECT 1 FROM itemAttachments LIMIT 1").fetchone()
        return con
    except sqlite3.OperationalError:
        tmp = Path(tempfile.mkdtemp(prefix="zotero-rename-"))
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(db) + suffix)
            if src.exists():
                shutil.copy2(src, tmp / src.name)
        return sqlite3.connect(tmp / db.name)


def build_plan(con: sqlite3.Connection) -> list[dict]:
    rows = con.execute(
        "SELECT pi.key, pi.itemID, pv.value, ai.key, ai.itemID, ia.path "
        "FROM items pi "
        "JOIN itemAttachments ia ON ia.parentItemID = pi.itemID "
        "JOIN items ai ON ai.itemID = ia.itemID "
        "JOIN itemData pid ON pid.itemID = pi.itemID "
        "JOIN fields pf ON pf.fieldID = pid.fieldID AND pf.fieldName = 'title' "
        "JOIN itemDataValues pv ON pv.valueID = pid.valueID "
        "WHERE ia.contentType = 'application/pdf' "
        "  AND pi.itemID IN ("
        "    SELECT ia2.parentItemID FROM itemAttachments ia2 "
        "    WHERE lower(ia2.path) LIKE '%_zh_cn.pdf'"
        "  ) "
        "ORDER BY pi.key, ai.itemID"
    ).fetchall()

    by_parent: dict[str, list] = {}
    for pkey, pid, title, akey, aid, path in rows:
        by_parent.setdefault(pkey, []).append(
            {"pkey": pkey, "parent_id": pid, "title": title, "key": akey,
             "item_id": aid, "old": path[len("storage:"):] if path.startswith("storage:") else path}
        )

    plan = []
    for pkey, atts in by_parent.items():
        title = atts[0]["title"]
        base = sanitize(title)
        zh = [a for a in atts if a["old"].lower().endswith("_zh_cn.pdf")]
        en = [a for a in atts if not a["old"].lower().endswith("_zh_cn.pdf")]
        for i, a in enumerate(en, start=1):
            new = f"{base}.pdf" if i == 1 else f"{base} ({i}).pdf"
            plan.append({**a, "new": new, "kind": "en",
                         "wanted_title": title if i == 1 else f"{title} ({i})"})
        for a in zh:
            new = f"{base}_zh_CN.pdf"
            plan.append({**a, "new": new, "kind": "zh", "wanted_title": new[:-4]})
    for row in plan:
        row["status"] = "already correct" if row["old"] == row["new"] else "rename"
    return plan


def set_title(con: sqlite3.Connection, item_id: int, value: str) -> None:
    fid = con.execute("SELECT fieldID FROM fields WHERE fieldName='title'").fetchone()[0]
    row = con.execute("SELECT valueID FROM itemDataValues WHERE value=?", (value,)).fetchone()
    if row:
        vid = row[0]
    else:
        vid = con.execute("INSERT INTO itemDataValues (value) VALUES (?)", (value,)).lastrowid
    updated = con.execute(
        "UPDATE itemData SET valueID=? WHERE itemID=? AND fieldID=?", (vid, item_id, fid)
    ).rowcount
    if updated == 0:
        con.execute("INSERT INTO itemData (itemID, fieldID, valueID) VALUES (?,?,?)",
                    (item_id, fid, vid))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data-dir", default=str(DEFAULT_DATA))
    p.add_argument("--apply", action="store_true")
    p.add_argument("--backup-dir", default=None)
    p.add_argument("--json", action="store_true", help="Print the plan as JSON")
    args = p.parse_args()

    data = Path(args.data_dir)
    db = data / "zotero.sqlite"
    storage = data / "storage"
    if not db.exists():
        print(f"database not found: {db}", file=sys.stderr)
        return 1
    if args.apply and zotero_running():
        print("Zotero is running. Quit it first, then re-run with --apply.", file=sys.stderr)
        return 1

    con = open_db(db)
    try:
        plan = build_plan(con)
    finally:
        con.close()

    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0

    todo = [r for r in plan if r["status"] == "rename"]
    print(f"{len(plan)} PDF(s): {len(todo)} to rename, {len(plan) - len(todo)} unchanged\n")
    for r in plan:
        mark = "  " if r["status"] == "rename" else "= "
        print(f"{mark}{r['key']} [{r['kind']}] {r['old']}")
        if r["status"] == "rename":
            print(f"      -> {r['new']}")

    if not args.apply:
        print("\nPreview only. Quit Zotero, then re-run with --apply.")
        return 0
    if not todo:
        return 0

    backup_dir = Path(args.backup_dir) if args.backup_dir else Path(__file__).resolve().parent.parent.parent / "state"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"zotero-before-title-rename-{stamp}.sqlite"
    shutil.copy2(db, backup)
    print(f"\nBackup: {backup}")

    ok = failed = 0
    con = sqlite3.connect(db)
    try:
        for r in todo:
            att_dir = storage / r["key"]
            old_file = att_dir / r["old"]
            new_file = att_dir / r["new"]
            if not old_file.exists():
                print(f"  {r['key']}: missing on disk: {r['old']}")
                failed += 1
                continue
            if new_file.exists() and new_file != old_file:
                print(f"  {r['key']}: target exists: {r['new']}")
                failed += 1
                continue
            try:
                os.replace(old_file, new_file)
                con.execute("UPDATE itemAttachments SET path=? WHERE itemID=?",
                            (f"storage:{r['new']}", r["item_id"]))
                set_title(con, r["item_id"], r["wanted_title"])
                con.execute("UPDATE items SET synced=0 WHERE itemID=?", (r["item_id"],))
                ok += 1
                print(f"  {r['key']}: {r['old']} -> {r['new']}")
            except Exception as exc:
                print(f"  {r['key']}: failed: {exc}")
                failed += 1
        con.commit()
    finally:
        con.close()

    print(f"\nRenamed {ok}, failed {failed}. Start Zotero to pick up the names.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
