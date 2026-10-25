#!/usr/bin/env python3
# Derived from arxiv2zh (https://github.com/kongyan66/arxiv2zh),
# Copyright (c) kongyan66 and contributors, AGPL-3.0-or-later.
"""Translate arXiv papers to Chinese through hjfy.top and attach the PDF to Zotero.

Ported from kongyan66's arxiv2zh Zotero plugin (v0.2.0). The service itself does
the work; this script reproduces the plugin's flow:

    arxivInfo -> (source available? poll arxivStatus : upload PDF, poll fileStatus)
              -> arxivFiles/fileFiles -> download zh-CN PDF -> attach in Zotero

Anonymous use works for papers that already have a translation (or whose LaTeX
source the service can fetch). Papers without LaTeX source need a PDF upload,
which the service gates behind a hjfy.top login; pass a cookie in that case.

Subcommands
-----------
status     show the remote translation state for an arXiv id
translate  wait for a translation, download it, attach it to a Zotero item
download   only download the Chinese PDF
open       open the paper on hjfy.top
login      open the sign-in page and explain how to reuse the cookie
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
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

DEFAULT_SERVICE = "https://hjfy.top"
UA = "arxiv2zh-skill/1.0"
ZH_TITLES = {"中文翻译 - arxiv2zh", "Chinese Translation - arxiv2zh"}
DEFAULT_COOKIE_FILE = Path.home() / ".config" / "arxiv2zh" / "cookies.txt"

_ARXIV_NEW = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?", re.I)
_ARXIV_TAGGED = re.compile(r"arxiv[:.\s/]*?(\d{4}\.\d{4,5})", re.I)


def out(msg: str) -> None:
    print(msg, flush=True)


def die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr, flush=True)
    raise SystemExit(code)


# ----------------------------------------------------------------------- arXiv id

def parse_arxiv_id(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    m = _ARXIV_TAGGED.search(value) or _ARXIV_NEW.search(value)
    if not m:
        return None
    base = m.group(1)
    # Old-style ids (hep-th/9901001) are preserved by the service as hep-th_9901001.
    return base


def arxiv_id_of_item(data: dict) -> str | None:
    for field in ("archiveID", "DOI", "url", "extra"):
        val = str(data.get(field) or "")
        if field == "archiveID" or "arxiv" in val.lower():
            aid = parse_arxiv_id(val)
            if aid:
                return aid
    return None


# ------------------------------------------------------------------------ zotero

def zotero_json(*args: str) -> object:
    proc = subprocess.run(["zotero-cli", "--json", *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        die(f"zotero-cli failed: {proc.stderr.strip() or proc.stdout.strip()}")
    payload = json.loads(proc.stdout)
    if not payload.get("ok"):
        die(f"zotero-cli error: {payload}")
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
    """attachment key -> local path, parsed from `zotero-cli path --json`."""
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


def find_translation_attachment(key: str) -> dict | None:
    """Existing Chinese attachment, by title or by the `_zh_CN.pdf` filename.

    The title test covers the plugin's original label; the filename test covers
    libraries (including this one) whose titles were later aligned with the
    filename.
    """
    paths = attachment_paths(key)
    for att in child_attachments(key):
        title = str(att.get("title") or "")
        path = paths.get(att["key"], "")
        name = Path(path).name if path else ""
        if title in ZH_TITLES or name.lower().endswith("_zh_cn.pdf"):
            return att
    return None


def attach_pdf(item_key: str, pdf: Path, force: bool) -> str:
    existing = find_translation_attachment(item_key)
    if existing and not force:
        return f"already attached ({existing['key']}), use --force to replace"

    # Attach the new file first; only delete the old one once it landed, so a
    # failed attach cannot leave the item with no Chinese PDF at all.
    proc = subprocess.run(["zotero-cli", "attach", item_key, "--file", str(pdf),
                           "--filename", pdf.name],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        die(f"attach failed: {proc.stderr.strip() or proc.stdout.strip()}")

    if existing and force:
        proc = subprocess.run(["zotero-cli", "delete", "item", existing["key"]],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace")
        if proc.returncode != 0:
            return (f"attached {pdf.name}, but could not remove the old attachment "
                    f"{existing['key']}: {proc.stderr.strip() or proc.stdout.strip()}")
    return "attached " + pdf.name


# ----------------------------------------------------------------------- cookies

def profile_dir() -> Path | None:
    """Zotero profile directory, on Windows, macOS or Linux."""
    candidates = []
    if os.name == "nt":
        candidates.append(Path(os.environ.get("APPDATA", "")) / "Zotero" / "Zotero" / "Profiles")
    elif sys.platform == "darwin":
        candidates.append(Path.home() / "Library" / "Application Support" / "Zotero" / "Profiles")
    else:
        candidates += [
            Path.home() / ".zotero" / "zotero",
            Path.home() / "snap" / "zotero-snap" / "common" / ".zotero" / "zotero",
        ]
    for base in candidates:
        if not base.exists():
            continue
        for prefs in base.glob("*/prefs.js"):
            return prefs.parent
    return None


def cookie_from_file(path: Path | None) -> str | None:
    """Read a Netscape cookie file and return the hjfy.top Cookie header."""
    if not path or not path.exists():
        return None
    parts = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return None
    for line in lines:
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 7:
            continue
        domain, name, value = fields[0], fields[5], fields[6]
        if "hjfy.top" in domain:
            parts.append(f"{name}={value}")
    return "; ".join(parts) or None


def resolve_cookie(args) -> str | None:
    """--cookie beats the cookie file, which beats a live read of Zotero's jar."""
    if getattr(args, "cookie", None):
        return args.cookie
    cookie_file = getattr(args, "cookie_file", None)
    if cookie_file:
        value = cookie_from_file(Path(cookie_file))
        if value:
            return value
    if getattr(args, "cookie_from_zotero", False):
        return cookie_from_zotero()
    return None


def cookie_from_zotero() -> str | None:
    prof = profile_dir()
    if not prof:
        return None
    src = prof / "cookies.sqlite"
    if not src.exists():
        return None
    tmp = Path(tempfile.mkdtemp(prefix="arxiv2zh-cookies-"))
    try:
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(src) + suffix)
            if p.exists():
                shutil.copy2(p, tmp / p.name)
        con = sqlite3.connect(tmp / "cookies.sqlite")
        try:
            rows = con.execute(
                "SELECT name, value FROM moz_cookies "
                "WHERE host LIKE '%hjfy.top%' OR host LIKE '%hjfy%'"
            ).fetchall()
        finally:
            con.close()
        if not rows:
            return None
        return "; ".join(f"{n}={v}" for n, v in rows)
    except Exception:
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- HTTP

class Service:
    def __init__(self, base: str, cookie: str | None):
        self.base = base.rstrip("/")
        self.cookie = cookie

    def _headers(self, extra: dict | None = None) -> dict:
        h = {"User-Agent": UA, "Accept": "application/json, */*"}
        if self.cookie:
            h["Cookie"] = self.cookie
        return {**h, **(extra or {})}

    def get_json(self, path: str) -> dict:
        req = urllib.request.Request(self.base + path, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            die(f"{path}: HTTP {e.code} {body}")
        except Exception as e:
            die(f"{path}: {e}")

    def get_bytes(self, url: str) -> bytes:
        req = urllib.request.Request(url, headers=self._headers())
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read()

    def post_pdf(self, path: str, filename: str, data: bytes) -> dict:
        boundary = "----arxiv2zh" + str(int(time.time() * 1000))
        safe = filename.replace('"', "_").replace("\r", "_").replace("\n", "_")
        head = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{safe}"\r\n'
            "Content-Type: application/pdf\r\n\r\n"
        ).encode()
        mid = (
            f"\r\n--{boundary}\r\n"
            'Content-Disposition: form-data; name="fileName"\r\n\r\n'
            f"{filename}\r\n--{boundary}--\r\n"
        ).encode()
        body = head + data + mid
        headers = self._headers({
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        })
        req = urllib.request.Request(self.base + path, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            die(f"upload: HTTP {e.code} {e.read().decode('utf-8', 'replace')[:300]}")
        except Exception as e:
            die(f"upload: {e}")


def envelope(raw: dict) -> tuple[int, dict, str]:
    if not isinstance(raw, dict) or not isinstance(raw.get("status"), int):
        die(f"unexpected service response: {raw}")
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    msg = raw.get("msg") or raw.get("message") or ""
    return raw["status"], data, str(msg)


# ------------------------------------------------------------------------ workflow

def fetch_info(svc: Service, arxiv_id: str) -> bool:
    """Does the service have LaTeX source?

    Mirrors the plugin's getInfoWithFallback: if the info endpoint fails, assume
    the source path is usable and let the arxiv status call decide. Without
    this, a transient arxivInfo error aborts a translation that would work.
    """
    try:
        code, data, msg = envelope(svc.get_json(f"/api/arxivInfo/{urllib.parse.quote(arxiv_id)}"))
    except SystemExit:
        return True
    if code != 0:
        return True
    return bool(data.get("hasSrc", True))


def wait_arxiv(svc: Service, arxiv_id: str, interval: int, timeout: int) -> dict:
    start = time.time()
    last = ""
    while True:
        code, data, msg = envelope(svc.get_json(f"/api/arxivStatus/{urllib.parse.quote(arxiv_id)}"))
        if code == 101:
            die("hjfy.top requires a login for this paper. Run `login`, sign in, "
                "then pass --cookie or use --cookie-from-zotero.")
        if code != 0:
            die(f"status query failed: {msg or code}")
        state = str(data.get("status") or "")
        info = str(data.get("info") or "")
        if info and info != last:
            out(f"  {state}: {info}")
            last = info
        if state == "finished":
            return data
        if state in ("failed", "error", "fault"):
            die(f"remote translation failed: {info or state}")
        if time.time() - start > timeout:
            die(f"timed out after {timeout}s (last state: {state})")
        time.sleep(max(5, interval))


def wait_file(svc: Service, file_key: str, interval: int, timeout: int) -> dict:
    start = time.time()
    last = ""
    while True:
        code, data, msg = envelope(svc.get_json(f"/api/fileStatus/{urllib.parse.quote(file_key)}"))
        if code == 101:
            die("hjfy.top requires a login for uploaded PDFs. Run `login`, sign in, "
                "then pass --cookie or use --cookie-from-zotero.")
        if code != 0:
            die(f"status query failed: {msg or code}")
        state = str(data.get("status") or "")
        info = str(data.get("info") or "")
        if info and info != last:
            out(f"  {state}: {info}")
            last = info
        if state == "finished":
            return data
        if state in ("failed", "error", "fault"):
            die(f"remote translation failed: {info or state}")
        if time.time() - start > timeout:
            die(f"timed out after {timeout}s (last state: {state})")
        time.sleep(max(5, interval))


def fetch_files(svc: Service, kind: str, remote_id: str) -> dict:
    code, data, msg = envelope(svc.get_json(f"/api/{kind}Files/{urllib.parse.quote(remote_id)}"))
    if code == 101:
        die("hjfy.top requires a login to read the result.")
    if code != 0:
        die(f"result lookup failed: {msg or code}")
    return data


def upload_item_pdf(svc: Service, item_key: str) -> tuple[str, str, str]:
    """Upload the item's first PDF. Returns (kind, remote_id, file_name)."""
    atts = child_attachments(item_key)
    paths = attachment_paths(item_key)
    for att in atts:
        path = paths.get(att["key"])
        if not path or not Path(path).exists():
            continue
        if not path.lower().endswith(".pdf") and not path.lower().endswith(".epub"):
            continue
        pdf = Path(path)
        size = pdf.stat().st_size
        if size > 50 * 1024 * 1024:
            die(f"{pdf.name} is {size // 1024 // 1024} MB, over the 50 MB upload limit")
        out(f"Uploading {pdf.name} ({size // 1024} KB)…")
        raw = svc.post_pdf("/api/uploadFiles", pdf.name, pdf.read_bytes())
        code, data, msg = envelope(raw)
        if code == 101:
            die("hjfy.top requires a login to upload PDFs. Run `login`, sign in, "
                "then pass --cookie or use --cookie-from-zotero.")
        if code == 302 and raw.get("arxivId"):
            aid = str(raw["arxivId"]).replace("_", "/")
            return "arxiv", aid, pdf.name
        if code != 0:
            die(f"upload failed: {msg or code}")
        key = str(data.get("fileKey") or "")
        if not key:
            die(f"upload response missing fileKey: {raw}")
        return "file", key, pdf.name
    die("No local PDF attachment found. Attach the original PDF to the item first.")


def safe_name(base: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base)
    return cleaned.rstrip(". ") or "document"


def translated_name(base: str) -> str:
    return safe_name(base) + "_zh_CN.pdf"


def english_pdf_basename(item_key: str) -> str | None:
    """The item's English PDF filename without .pdf — the base for the Chinese one."""
    paths = attachment_paths(item_key)
    for att in child_attachments(item_key):
        path = paths.get(att["key"])
        if not path:
            continue
        name = Path(path).name
        if name.lower().endswith(".pdf") and not name.lower().endswith("_zh_cn.pdf"):
            return Path(name).stem
    return None


def validate_pdf(data: bytes, name: str) -> None:
    if not data:
        die(f"downloaded file is empty: {name}")
    if b"%PDF-" not in data[:1024]:
        die(f"downloaded content is not a PDF (no %PDF- header): {name}")
    if b"%%EOF" not in data[-65536:]:
        die(f"downloaded PDF looks truncated (no %%EOF): {name}")


def cmd_status(args) -> int:
    svc = Service(args.service_url, resolve_cookie(args))
    code, data, msg = envelope(svc.get_json(f"/api/arxivStatus/{urllib.parse.quote(args.arxiv_id)}"))
    if code == 101:
        out("login required")
        return 0
    if code != 0:
        die(f"status query failed: {msg or code}")
    out(json.dumps({"arxiv_id": args.arxiv_id, **data}, ensure_ascii=False, indent=2))
    return 0


def cmd_translate(args) -> int:
    svc = Service(args.service_url, resolve_cookie(args))

    item_key = args.key
    item = get_item(item_key) if item_key else None
    arxiv_id = args.arxiv_id or (arxiv_id_of_item(item) if item else None)
    if not arxiv_id and not item_key:
        die("Give --arxiv-id or --key")
    if arxiv_id:
        arxiv_id = parse_arxiv_id(arxiv_id)

    kind = "arxiv"
    remote_id = arxiv_id
    source_name = None

    if arxiv_id:
        out(f"arXiv {arxiv_id}: checking source availability…")
        has_source = fetch_info(svc, arxiv_id)
        if not has_source:
            if not item_key:
                die("This arXiv entry has no LaTeX source; give --key so its PDF can be uploaded.")
            out("No LaTeX source; uploading the item's PDF instead.")
            kind, remote_id, source_name = upload_item_pdf(svc, item_key)
    elif item_key:
        kind, remote_id, source_name = upload_item_pdf(svc, item_key)

    out(f"Waiting for translation ({kind} {remote_id})…")
    if kind == "arxiv":
        wait_arxiv(svc, remote_id, args.poll_interval, args.timeout)
    else:
        wait_file(svc, remote_id, args.poll_interval, args.timeout)

    files = fetch_files(svc, kind, remote_id)
    url = files.get("zhCN")
    if not url:
        die(f"result has no zhCN PDF URL: {files}")

    # Name the Chinese PDF after the item's English PDF, not the arXiv id.
    base_name = english_pdf_basename(item_key) if item_key else None
    if not base_name and source_name:
        base_name = Path(source_name).stem
    name = translated_name(base_name or arxiv_id or remote_id)
    if args.out_dir:
        target_dir = Path(args.out_dir)
    elif item_key:
        target_dir = Path(tempfile.mkdtemp(prefix="arxiv2zh-"))
    else:
        target_dir = Path.cwd()
    target_dir.mkdir(parents=True, exist_ok=True)
    pdf = target_dir / name

    out(f"Downloading {name}…")
    data = svc.get_bytes(url)
    validate_pdf(data, name)
    pdf.write_bytes(data)
    out(f"Saved {pdf} ({len(data) // 1024} KB)")

    if item_key and not args.no_attach:
        out(attach_pdf(item_key, pdf, args.force))
        if args.open_translation:
            subprocess.run(["zotero-cli", "open", item_key], check=False)
    elif args.open_translation:
        webbrowser.open(files.get("origin") or url)
    return 0


def cmd_download(args) -> int:
    svc = Service(args.service_url, resolve_cookie(args))
    files = fetch_files(svc, "arxiv", parse_arxiv_id(args.arxiv_id) or args.arxiv_id)
    url = files.get("zhCN")
    if not url:
        die(f"result has no zhCN PDF URL: {files}")
    name = translated_name(args.arxiv_id)
    target = Path(args.out_dir or ".") / name
    target.parent.mkdir(parents=True, exist_ok=True)
    data = svc.get_bytes(url)
    validate_pdf(data, name)
    target.write_bytes(data)
    out(f"Saved {target} ({len(data) // 1024} KB)")
    return 0


def cmd_open(args) -> int:
    svc = Service(args.service_url, None)
    url = f"{svc.base}/arxiv/{urllib.parse.quote(args.arxiv_id)}"
    webbrowser.open(url)
    out(url)
    return 0


def zotero_cookie_rows() -> list[tuple]:
    prof = profile_dir()
    if not prof:
        return []
    src = prof / "cookies.sqlite"
    if not src.exists():
        return []
    tmp = Path(tempfile.mkdtemp(prefix="arxiv2zh-cookies-"))
    try:
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(src) + suffix)
            if p.exists():
                shutil.copy2(p, tmp / p.name)
        con = sqlite3.connect(tmp / "cookies.sqlite")
        try:
            return con.execute(
                "SELECT host, path, isSecure, expiry, name, value FROM moz_cookies "
                "WHERE host LIKE '%hjfy.top%' ORDER BY host, name"
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def cmd_export_cookies(args) -> int:
    rows = zotero_cookie_rows()
    if not rows:
        die("No hjfy.top cookie in Zotero's jar. Sign in at https://hjfy.top first "
            "(the plugin's sign-in window, or `arxiv2zh.py login`).")
    dest = Path(args.cookie_file)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write("# Netscape HTTP Cookie File\n")
        fh.write("# Generated from Zotero's cookie jar for hjfy.top\n\n")
        for host, path, secure, expiry, name, value in rows:
            domain_flag = "TRUE" if host.startswith(".") else "FALSE"
            secure_flag = "TRUE" if secure else "FALSE"
            fh.write(f"{host}\t{domain_flag}\t{path}\t{secure_flag}\t{expiry}\t{name}\t{value}\n")
    out(f"Wrote {len(rows)} cookie(s) to {dest}")
    out("Includes: " + ", ".join(n for _, _, _, _, n, _ in rows))
    return 0


def cmd_login(args) -> int:
    svc = Service(args.service_url, None)
    webbrowser.open(svc.base)
    out(f"Opened {svc.base}. Sign in there, then either:")
    out("  - let the script read Zotero's cookies:  --cookie-from-zotero  (default)")
    out("  - or copy the Cookie header from devtools: --cookie \"k=v; k2=v2\"")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--service-url", default=os.environ.get("ARXIV2ZH_SERVICE", DEFAULT_SERVICE))
    p.add_argument("--cookie", default=os.environ.get("ARXIV2ZH_COOKIE"),
                   help="Cookie header value for hjfy.top")
    p.add_argument("--cookie-file", default=str(DEFAULT_COOKIE_FILE),
                   help="Netscape cookie file (default: ~/.config/arxiv2zh/cookies.txt)")
    p.add_argument("--no-cookie-from-zotero", dest="cookie_from_zotero",
                   action="store_false", help="Do not read the cookie from Zotero's profile")
    p.set_defaults(cookie_from_zotero=True)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("status", help="Show remote translation state")
    s.add_argument("arxiv_id")
    s.set_defaults(func=cmd_status)

    t = sub.add_parser("translate", help="Wait for, download and attach the Chinese PDF")
    t.add_argument("--key", help="Zotero item key")
    t.add_argument("--arxiv-id", help="arXiv id (creates nothing in Zotero)")
    t.add_argument("--force", action="store_true", help="Replace an existing translation attachment")
    t.add_argument("--no-attach", action="store_true", help="Download only")
    t.add_argument("--out-dir", help="Where to save the PDF")
    t.add_argument("--open", dest="open_translation", action="store_true",
                   help="Open the PDF in Zotero after attaching")
    t.add_argument("--poll-interval", type=int, default=10)
    t.add_argument("--timeout", type=int, default=1800)
    t.set_defaults(func=cmd_translate)

    d = sub.add_parser("download", help="Only download the finished Chinese PDF")
    d.add_argument("arxiv_id")
    d.add_argument("--out-dir", default=".")
    d.set_defaults(func=cmd_download)

    o = sub.add_parser("open", help="Open the paper on hjfy.top")
    o.add_argument("arxiv_id")
    o.set_defaults(func=cmd_open)

    lg = sub.add_parser("login", help="Open the sign-in page")
    lg.set_defaults(func=cmd_login)

    ec = sub.add_parser("export-cookies", help="Export hjfy.top cookies from Zotero to the cookie file")
    ec.set_defaults(func=cmd_export_cookies)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
