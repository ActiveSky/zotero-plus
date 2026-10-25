#!/usr/bin/env python3
"""Rename existing *_zh_CN.pdf attachments so they match their English PDF.

The arxiv2zh plugin named Chinese PDFs after the arXiv id (`2503.24000_zh_CN.pdf`).
This renames them to `<English PDF basename>_zh_CN.pdf`, in both the storage
folder and Zotero's database, without recreating the attachment — so highlights
and annotations on the Chinese PDF stay attached to the same item.

Zotero must be closed: it holds the database and watches the storage folder.
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
import time
from pathlib import Path

DEFAULT_DATA = Path(os.environ.get("ZOTERO_DATA_DIR", Path.home() / "Zotero"))
SUFFIX = "_zh_CN.pdf"


def zotero_running() -> bool:
    try:
        if os.name == "nt":
            proc = subprocess.run(["tasklist", "/FI", "IMAGENAME eq zotero.exe", "/NH"],
                                  capture_output=True, text=True, errors="replace")
            return "zotero.exe" in proc.stdout.lower()
        proc = subprocess.run(["pgrep", "-x", "zotero"], capture_output=True)
        return proc.returncode == 0
    except Exception:
        return True  # cannot tell: treat as running and refuse to write


def build_plan(con: sqlite3.Connection) -> list[dict]:
    zh = con.execute(
        "SELECT ia.itemID, ia.parentItemID, ia.path, ai.key, pi.key "
        "FROM itemAttachments ia JOIN items ai ON ai.itemID = ia.itemID "
        "LEFT JOIN items pi ON pi.itemID = ia.parentItemID "
        "WHERE ia.path LIKE 'storage:%' AND lower(ia.path) LIKE '%_zh_cn.pdf'"
    ).fetchall()

    pdfs: dict[int, list[str]] = {}
    for parent_id, path in con.execute(
        "SELECT ia.parentItemID, ia.path FROM itemAttachments ia "
        "WHERE ia.contentType = 'application/pdf' AND ia.path LIKE 'storage:%'"
    ):
        pdfs.setdefault(parent_id, []).append(path)

    plan = []
    for item_id, parent_id, path, att_key, parent_key in zh:
        old = path[len("storage:"):]
        english = [p[len("storage:"):] for p in pdfs.get(parent_id, [])
                   if not p.lower().endswith(SUFFIX.lower())]
        if not english:
            plan.append({"item_id": item_id, "att_key": att_key, "parent_key": parent_key,
                         "old": old, "new": None, "status": "no English PDF"})
            continue
        base = english[0]
        if base.lower().endswith(".pdf"):
            base = base[:-4]
        new = base + SUFFIX
        status = "already correct" if new == old else "rename"
        plan.append({"item_id": item_id, "att_key": att_key, "parent_key": parent_key,
                     "old": old, "new": new, "status": status})
    return plan


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data-dir", default=str(DEFAULT_DATA), help="Zotero data directory")
    p.add_argument("--apply", action="store_true", help="Actually rename (default is a preview)")
    p.add_argument("--backup-dir", default=None,
                   help="Where to put the database backup (default: the skill's state dir)")
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

    # A read connection succeeds when Zotero is closed; otherwise read a snapshot
    # (db + WAL) so the preview works while Zotero is still running.
    snapshot_dir = None
    try:
        con = sqlite3.connect(db)
        plan = build_plan(con)
        con.close()
    except sqlite3.OperationalError:
        snapshot_dir = Path(tempfile.mkdtemp(prefix="arxiv2zh-db-"))
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(db) + suffix)
            if src.exists():
                shutil.copy2(src, snapshot_dir / src.name)
        con = sqlite3.connect(snapshot_dir / db.name)
        try:
            plan = build_plan(con)
        finally:
            con.close()
            shutil.rmtree(snapshot_dir, ignore_errors=True)

    todo = [row for row in plan if row["status"] == "rename"]
    skip = [row for row in plan if row["status"] != "rename"]

    print(f"{len(plan)} Chinese attachment(s): {len(todo)} to rename, {len(skip)} skipped\n")
    for row in plan:
        mark = "  " if row["status"] == "rename" else "! "
        new = row["new"] or "-"
        print(f"{mark}{row['att_key']}  {row['old']}")
        print(f"   -> {new}   [{row['status']}]")

    if not args.apply:
        print("\nPreview only. Quit Zotero, then re-run with --apply.")
        return 0
    if not todo:
        print("\nNothing to rename.")
        return 0

    backup_dir = Path(args.backup_dir) if args.backup_dir else Path(__file__).resolve().parent.parent.parent / "state"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"zotero-before-rename-{stamp}.sqlite"
    shutil.copy2(db, backup)
    print(f"\nBackup: {backup}")

    ok = failed = 0
    con = sqlite3.connect(db)
    try:
        for row in todo:
            att_dir = storage / row["att_key"]
            old_file = att_dir / row["old"]
            new_file = att_dir / row["new"]
            if not old_file.exists():
                print(f"  {row['att_key']}: missing on disk: {old_file.name}")
                failed += 1
                continue
            if new_file.exists() and new_file != old_file:
                print(f"  {row['att_key']}: target already exists: {new_file.name}")
                failed += 1
                continue
            try:
                os.replace(old_file, new_file)
                con.execute("UPDATE itemAttachments SET path = ? WHERE itemID = ?",
                            (f"storage:{row['new']}", row["item_id"]))
                con.execute("UPDATE items SET synced = 0 WHERE itemID = ?", (row["item_id"],))
                ok += 1
                print(f"  {row['att_key']}: {row['old']} -> {row['new']}")
            except Exception as exc:
                print(f"  {row['att_key']}: failed: {exc}")
                failed += 1
        con.commit()
    finally:
        con.close()

    print(f"\nRenamed {ok}, failed {failed}. Start Zotero to pick up the new names.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
