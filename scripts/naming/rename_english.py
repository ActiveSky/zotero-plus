#!/usr/bin/env python3
"""Rename English PDF attachments to match their item's title.

For items added on a given day (default: today), each PDF attachment is renamed
to `<title>.pdf`, so the displayed title and the filename agree. A Chinese
attachment under the same item, if present, follows as `<title>_zh_CN.pdf`.

Only characters Windows forbids in a filename are changed; everything else in
the title is kept. Zotero must be closed for --apply.
"""

from __future__ import annotations

import argparse
import datetime as dt
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


def sanitize(title: str) -> str:
    """Turn a paper title into a Windows-safe file base name."""
    name = title.replace("/", "-").replace("\\", "-")
    name = name.replace(":", " -")
    name = re.sub(r'[<>"|?*]', "", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > 150:
        name = name[:150].rstrip(" .")
    return name or "document"


def open_readonly(db: Path) -> tuple[sqlite3.Connection, Path | None]:
    """Open read-only, or a snapshot when Zotero holds the database."""
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.execute("SELECT 1 FROM itemAttachments LIMIT 1").fetchone()
        return con, None
    except sqlite3.OperationalError:
        tmp = Path(tempfile.mkdtemp(prefix="zotero-rename-"))
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(db) + suffix)
            if src.exists():
                shutil.copy2(src, tmp / src.name)
        return sqlite3.connect(tmp / db.name), tmp


def build_plan(con: sqlite3.Connection, added: str) -> list[dict]:
    rows = con.execute(
        "SELECT pi.key, ai.key, ia.itemID, ia.path, pv.value "
        "FROM items pi "
        "JOIN itemAttachments ia ON ia.parentItemID = pi.itemID "
        "JOIN items ai ON ai.itemID = ia.itemID "
        "JOIN itemData pid ON pid.itemID = pi.itemID "
        "JOIN fields pf ON pf.fieldID = pid.fieldID AND pf.fieldName = 'title' "
        "JOIN itemDataValues pv ON pv.valueID = pid.valueID "
        "WHERE date(pi.dateAdded) = ? AND ia.contentType = 'application/pdf' "
        "ORDER BY pi.key, ai.key",
        (added,),
    ).fetchall()

    plan = []
    for pkey, akey, item_id, path, title in rows:
        old = path[len("storage:"):] if path.startswith("storage:") else path
        base = sanitize(title)
        if old.lower().endswith("_zh_cn.pdf"):
            new = base + "_zh_CN.pdf"
        else:
            new = base + ".pdf"
        plan.append({"parent": pkey, "key": akey, "item_id": item_id,
                     "old": old, "new": new,
                     "status": "already correct" if old == new else "rename"})
    return plan


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data-dir", default=str(DEFAULT_DATA))
    p.add_argument("--added", default=None, help="Items added on this date (default: today)")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--backup-dir", default=None)
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

    added = args.added or dt.date.today().isoformat()
    con, snapshot = open_readonly(db)
    try:
        plan = build_plan(con, added)
    finally:
        con.close()
        if snapshot:
            shutil.rmtree(snapshot, ignore_errors=True)

    todo = [r for r in plan if r["status"] == "rename"]
    print(f"{len(plan)} PDF(s) for items added {added}: {len(todo)} to rename\n")
    for r in plan:
        print(f"{r['key']}  [{r['status']}]")
        print(f"   {r['old']}")
        print(f"-> {r['new']}")

    if not args.apply:
        print("\nPreview only. Quit Zotero, then re-run with --apply.")
        return 0
    if not todo:
        return 0

    backup_dir = Path(args.backup_dir) if args.backup_dir else Path(__file__).resolve().parent.parent.parent / "state"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"zotero-before-english-rename-{stamp}.sqlite"
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
                con.execute("UPDATE itemAttachments SET path = ? WHERE itemID = ?",
                            (f"storage:{r['new']}", r["item_id"]))
                con.execute("UPDATE items SET synced = 0 WHERE itemID = ?", (r["item_id"],))
                ok += 1
                print(f"  {r['key']}: {r['old']}")
                print(f"      -> {r['new']}")
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
