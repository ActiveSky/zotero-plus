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
import uuid
import webbrowser
from pathlib import Path

DEFAULT_SERVICE = "https://hjfy.top"
UA = "arxiv2zh-skill/1.0"
ZH_TITLES = {"中文翻译 - arxiv2zh", "Chinese Translation - arxiv2zh"}
CONFIG_DIR = Path.home() / ".config" / "arxiv2zh"
DEFAULT_CONFIG_FILE = CONFIG_DIR / "config.json"
# Kept for `--cookie-file`; the config file replaced it and `cookies` migrates it.
LEGACY_COOKIE_FILE = CONFIG_DIR / "cookies.txt"

CONFIG_DEFAULTS: dict = {
    "version": 1,
    "web": {
        "service_url": DEFAULT_SERVICE,
        "cookies": {},
    },
    "local": {
        # Empty engine/model/api_key mean "not configured yet"; the command says so.
        "engine": "",
        "base_url": "https://labhub.activesky.asia/v1",
        "api_key": "",
        "model": "",
        "source_language": "en",
        "target_language": "zh-Hans",
        "font_profile": "notocjk",
        "ocr_mode": "auto",
        "concurrency": 4,
        # The engine's own switch: service-default, enable-thinking-false,
        # thinking-disabled or reasoning-none. Each gateway spells "no thinking"
        # differently, so the right value depends on local.base_url.
        "thinking_mode": "service-default",
        # Table cells stay in the source language unless asked otherwise: the
        # engine's own default is "auto", which translates prose-heavy tables
        # and leaves numeric ones alone, but a mistranslated result table costs
        # more than an untranslated one. auto/on/off, as the engine spells them.
        "translate_tables": "off",
        # Raw hjfy-pdf options appended to every run (`"--pages=1-10"`, ...).
        # Options the script sets itself are rejected, so a stale entry cannot
        # silently steal the output path or the credentials.
        "extra_args": [],
    },
}

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


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: Path | None = None) -> dict:
    """Read config.json, filling in every default the file leaves out.

    A broken or missing file is not fatal: the defaults are usable, and the
    commands that need a setting report which one is missing.
    """
    target = Path(path) if path else DEFAULT_CONFIG_FILE
    if not target.exists():
        return _merge(CONFIG_DEFAULTS, {})
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except Exception as e:
        out(f"warning: {target} is not valid JSON ({e}); using defaults")
        return _merge(CONFIG_DEFAULTS, {})
    if not isinstance(raw, dict):
        out(f"warning: {target} must contain a JSON object; using defaults")
        return _merge(CONFIG_DEFAULTS, {})
    return _merge(CONFIG_DEFAULTS, raw)


def config_path(args) -> Path:
    explicit = getattr(args, "config", None)
    return Path(explicit) if explicit else DEFAULT_CONFIG_FILE


def write_config(config: dict, path: Path | None = None) -> Path:
    target = Path(path) if path else DEFAULT_CONFIG_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def cookie_from_config(config: dict) -> str | None:
    cookies = (config.get("web") or {}).get("cookies") or {}
    if not isinstance(cookies, dict):
        return None
    parts = [f"{name}={value}" for name, value in cookies.items() if name and value]
    return "; ".join(parts) or None


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
    """--cookie beats config.json, which beats a cookie file, which beats Zotero's jar."""
    if getattr(args, "cookie", None):
        return args.cookie
    value = cookie_from_config(getattr(args, "config_data", None) or load_config(config_path(args)))
    if value:
        return value
    cookie_file = getattr(args, "cookie_file", None)
    if cookie_file:
        value = cookie_from_file(Path(cookie_file))
        if value:
            return value
    if getattr(args, "cookie_from_zotero", False):
        return cookie_from_zotero()
    return None


def service_from(args) -> "Service":
    config = getattr(args, "config_data", None) or load_config(config_path(args))
    url = (getattr(args, "service_url", None)
           or (config.get("web") or {}).get("service_url")
           or DEFAULT_SERVICE)
    return Service(url, resolve_cookie(args))


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

    def post_json(self, path: str, payload: dict, idempotency_key: str | None = None) -> dict:
        body = json.dumps(payload).encode("utf-8")
        extra = {"Content-Type": "application/json"}
        if idempotency_key:
            extra["Idempotency-Key"] = idempotency_key
        req = urllib.request.Request(
            self.base + path,
            data=body,
            headers=self._headers(extra),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", "replace")[:300]
            die(f"{path}: HTTP {e.code} {body_text}")
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


def access_problem(files: dict) -> str | None:
    """Why the result is not the whole translation, or None when it is.

    hjfy.top hands out a fixed-size preview to callers without an unlocked
    result, and the preview is a valid PDF: downloading it silently leaves a
    five-page file attached to the item. The plugin's own client refuses such
    a result (`canDownloadArxiv`), and so must this one.
    """
    if files.get("isPreview") or files.get("accessSource") == "preview":
        limit = files.get("previewPageLimit")
        pages = files.get("translatedPageCount")
        what = f"a {limit}-page preview" if limit else "a preview"
        if pages:
            what += f" of {pages} translated pages"
        return f"hjfy.top returns {what}"
    if files.get("unlocked") is False or files.get("canReadFullPdf") is False:
        return "the full PDF is locked"
    if files.get("accessSource") == "membership" and files.get("accessExpiresAt") is None:
        return "the reading pass for this PDF has expired"
    return None


def unlock_hint(files: dict, remote_id: str) -> str:
    quote = files.get("unlockQuote") or {}
    pages = quote.get("pages")
    cost = f" ({pages} page(s) from your balance)" if pages else ""
    return (f"Unlock it to download the whole file{cost}: re-run with --unlock, "
            f"or open {DEFAULT_SERVICE}/arxiv/{remote_id}")


def unlock_result(svc: Service, kind: str, remote_id: str, files: dict) -> dict:
    """Spend the quoted pages on the full result, then re-read it."""
    if kind != "arxiv":
        die("--unlock applies to arXiv results only; uploaded PDFs are charged when submitted.")
    quote = files.get("unlockQuote") or {}
    quote_id = quote.get("id")
    if not quote_id:
        die(f"no unlock quote in the result: {files}")
    pages = quote.get("pages")
    out(f"Unlocking {remote_id}" + (f" for {pages} page(s)" if pages else "") + "…")
    # The endpoint bills the account, so it demands an idempotency key: a retry
    # with the same key cannot charge twice. It answers {"unlocked": true}
    # rather than the usual {status, data} envelope.
    raw = svc.post_json(f"/api/arxivUnlock/{urllib.parse.quote(remote_id)}",
                        {"quoteId": quote_id}, idempotency_key=uuid.uuid4().hex)
    if str(raw.get("error") or "").upper() == "LOGIN_REQUIRED":
        die("hjfy.top requires a login to unlock. Sign in there and check web.cookies "
            f"in {DEFAULT_CONFIG_FILE} (or run `zotero-plus account`).")
    if isinstance(raw.get("status"), int) and raw["status"] != 0:
        die(f"unlock failed: {raw.get('msg') or raw['status']}")
    if raw.get("unlocked") is not True:
        die(f"unexpected unlock response: {raw}")
    return fetch_files(svc, kind, remote_id)


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


# ------------------------------------------------------------------ local engine

LOCAL_KEY_ENV = "ARXIV2ZH_LOCAL_KEY"


def source_pdf(item_key: str) -> Path | None:
    """The item's English PDF, the input the local engine translates."""
    paths = attachment_paths(item_key)
    for att in child_attachments(item_key):
        path = paths.get(att["key"])
        if not path:
            continue
        p = Path(path)
        if p.suffix.lower() == ".pdf" and not p.name.lower().endswith("_zh_cn.pdf"):
            return p
    return None


def engine_path(config: dict) -> Path:
    """local.engine if set, else the newest runtime the Zotero plugin installed."""
    configured = (config.get("local") or {}).get("engine")
    if configured:
        p = Path(os.path.expandvars(os.path.expanduser(str(configured))))
        if p.is_file():
            return p
        die(f"local.engine points at a missing file: {p}")
    roots = []
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        roots.append(Path(os.environ["LOCALAPPDATA"]) / "hjfy-engine")
    roots.append(Path.home() / "hjfy-engine")
    for root in roots:
        for candidate in sorted(root.glob("runtime-*/bin/hjfy-pdf*"), reverse=True):
            if candidate.is_file():
                return candidate
    die("No local engine found. Set local.engine in the config to the hjfy-pdf "
        "executable (the Zotero plugin's runtime), or download it there first.")


def engine_env(engine: Path) -> dict:
    """PATH the engine needs for its shared libraries.

    The published runtime ships onnxruntime and pdfium in lib/ but not the MinGW
    runtime, and on Windows the UCRT forwarders live under System32\\downlevel.
    """
    root = engine.parent.parent
    env = dict(os.environ)
    prepend = [str(engine.parent), str(root / "deps"), str(root / "lib")]
    if os.name == "nt":
        prepend.append(str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "downlevel"))
    env["PATH"] = os.pathsep.join(prepend + [env.get("PATH", "")])
    return env


# Options the script always sets itself. Repeating one of these would make the
# run depend on argv order, and for --output/--api-key-env it would be wrong,
# so they are a hard error rather than a silent override.
ENGINE_RESERVED = {
    "-o", "--output", "--output-dir", "--resource-root", "--api-key-env",
    "--translation-cache-db", "--base-url", "--model", "--provider",
    "--source-language", "--target-language", "--font-profile", "--ocr-mode",
    "--concurrency", "--thinking-mode",
}


def named_engine_args(args) -> list[str]:
    """The convenience switches, turned into the engine's own options."""
    extra: list[str] = []
    if getattr(args, "pages", None):
        extra.append(f"--pages={args.pages}")
    if getattr(args, "pdf_mode", None):
        extra.append(f"--pdf-mode={args.pdf_mode}")
    if getattr(args, "bilingual", False):
        extra.append("--pdf-bilingual=true")
    if getattr(args, "bilingual_order", None):
        extra.append(f"--pdf-bilingual-order={args.bilingual_order}")
    if getattr(args, "translate_tables", None):
        extra.append(f"--translate-tables={args.translate_tables}")
    if getattr(args, "translate_bookmarks", None) is not None:
        extra.append(f"--translate-bookmarks={'true' if args.translate_bookmarks else 'false'}")
    if getattr(args, "output_format", None):
        extra.append(f"--output-format={args.output_format}")
    for path in getattr(args, "terminology", None) or []:
        extra.append(f"--terminology={path}")
    if getattr(args, "request_timeout", None):
        extra.append(f"--request-timeout={args.request_timeout}")
    if getattr(args, "on_page_error", None):
        extra.append(f"--on-page-error={args.on_page_error}")
    return extra


ENGINE_REPEATABLE = {"--terminology"}


def last_wins(items: list[str]) -> list[str]:
    """Drop earlier occurrences of a single-value option, keeping the last one.

    The engine would honour the last value anyway; removing the dead entries
    keeps the printed command (and the bug report) readable.
    """
    kept: list[str] = []
    seen: set[str] = set()
    for item in reversed(items):
        option = item.split("=", 1)[0]
        if option in ENGINE_REPEATABLE or option not in seen:
            kept.append(item)
            seen.add(option)
    return list(reversed(kept))


def engine_extras(args, local: dict) -> list[str]:
    """Engine options beyond the fixed set, lowest precedence first.

    Order is config default -> local.extra_args -> named switches ->
    --engine-arg. The engine keeps the last value for a repeated single-value
    option, so the command line always wins over the config file.
    """
    merged: list[str] = []
    tables = local.get("translate_tables")
    if tables:
        merged.append(f"--translate-tables={tables}")
    merged += [str(a) for a in (local.get("extra_args") or [])]
    merged += named_engine_args(args)
    merged += [str(a) for a in (getattr(args, "engine_arg", None) or [])]

    for item in merged:
        option = item.split("=", 1)[0].strip()
        if not item.startswith("--") or any(ch.isspace() for ch in item):
            die(f"--engine-arg takes one option per value, got {item!r}; "
                "write --engine-arg=--pages=1-10, not --engine-arg \"--pages 1-10\"")
        if option in ENGINE_RESERVED:
            die(f"{option} is set by zotero-plus itself and cannot be passed through; "
                "use the matching config key or command option instead")
    return last_wins(merged)


def cmd_translate_local(args) -> int:
    config = getattr(args, "config_data", None) or load_config(config_path(args))
    local = config.get("local") or {}
    missing = [f"local.{k}" for k in ("api_key", "model") if not local.get(k)]
    if missing:
        verb = "are" if len(missing) > 1 else "is"
        die(f"{' and '.join(missing)} {verb} empty in {config_path(args)}. "
            "Fill in the API credentials, then run again.")
    engine = engine_path(config)

    item_key = args.key
    if item_key:
        src = source_pdf(item_key)
        if not src:
            die(f"No English PDF attachment on {item_key} to translate.")
        base_name = english_pdf_basename(item_key) or src.stem
    else:
        if not args.pdf:
            die("Give --key (a Zotero item) or --pdf (a local file) with --local.")
        src = Path(args.pdf)
        if not src.is_file():
            die(f"No such file: {src}")
        base_name = src.stem

    target_dir = Path(args.out_dir) if args.out_dir else Path(tempfile.mkdtemp(prefix="arxiv2zh-local-"))
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / translated_name(base_name)

    cache = Path.home() / ".cache" / "zotero-plus" / "translation-cache.sqlite3"
    cache.parent.mkdir(parents=True, exist_ok=True)

    thinking = (getattr(args, "thinking_mode", None)
                or local.get("thinking_mode") or "service-default")

    cmd = [
        str(engine), str(src), "--output", str(target),
        "--resource-root", str(engine.parent.parent),
        "--font-profile", local.get("font_profile") or "notocjk",
        "--source-language", local.get("source_language") or "en",
        "--target-language", local.get("target_language") or "zh-Hans",
        "--provider", "chat-completions",
        "--thinking-mode", thinking,
        "--base-url", local.get("base_url") or "",
        "--model", local.get("model"),
        "--api-key-env", LOCAL_KEY_ENV,
        "--ocr-mode", local.get("ocr_mode") or "auto",
        "--concurrency", str(local.get("concurrency") or 4),
        "--translation-cache-db", str(cache),
    ]
    extras = engine_extras(args, local)
    cmd += extras
    env = engine_env(engine)
    env[LOCAL_KEY_ENV] = str(local.get("api_key"))

    if getattr(args, "dry_run", False):
        # The key travels in the environment, never in argv, so the printed
        # command is safe to paste into a bug report.
        printable = " ".join(f'"{a}"' if " " in a else a for a in cmd)
        out(f"engine: {printable}")
        out(f"env: {LOCAL_KEY_ENV}=<local.api_key>")
        return 0

    out(f"Translating {src.name} with {engine.name} ({local.get('model')}, thinking={thinking})…")
    proc = subprocess.run(cmd, env=env)
    if proc.returncode != 0:
        die(f"engine exited {proc.returncode}; the PDF was not attached")
    if not target.exists() or target.stat().st_size == 0:
        die("engine produced no output PDF")
    if target.read_bytes()[:5] != b"%PDF-":
        die("engine output is not a PDF")
    out(f"Saved {target} ({target.stat().st_size // 1024} KB)")

    # --output-format=pdf-docx also writes a sibling Office file; report it so
    # it is not left unnoticed in the temporary output directory.
    for ext in (".docx", ".pptx"):
        sibling = target.with_suffix(ext)
        if sibling.exists():
            out(f"Also wrote {sibling} ({sibling.stat().st_size // 1024} KB)")
    if getattr(args, "pages", None):
        out("note: only the selected pages are translated; the attachment still "
            "holds the whole document, with the other pages left in the source language")

    if item_key and not args.no_attach:
        out(attach_pdf(item_key, target, args.force))
        if args.open_translation:
            subprocess.run(["zotero-cli", "open", item_key], check=False)
    return 0


def validate_pdf(data: bytes, name: str) -> None:
    if not data:
        die(f"downloaded file is empty: {name}")
    if b"%PDF-" not in data[:1024]:
        die(f"downloaded content is not a PDF (no %PDF- header): {name}")
    if b"%%EOF" not in data[-65536:]:
        die(f"downloaded PDF looks truncated (no %%EOF): {name}")


def cmd_status(args) -> int:
    svc = service_from(args)
    code, data, msg = envelope(svc.get_json(f"/api/arxivStatus/{urllib.parse.quote(args.arxiv_id)}"))
    if code == 101:
        out("login required")
        return 0
    if code != 0:
        die(f"status query failed: {msg or code}")
    out(json.dumps({"arxiv_id": args.arxiv_id, **data}, ensure_ascii=False, indent=2))
    return 0


def cmd_translate(args) -> int:
    svc = service_from(args)

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
    problem = access_problem(files)
    if problem:
        if args.unlock:
            files = unlock_result(svc, kind, remote_id, files)
            problem = access_problem(files)
        if problem:
            die(f"{problem}. {unlock_hint(files, remote_id)}")
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
    svc = service_from(args)
    arxiv_id = parse_arxiv_id(args.arxiv_id) or args.arxiv_id
    files = fetch_files(svc, "arxiv", arxiv_id)
    problem = access_problem(files)
    if problem:
        if args.unlock:
            files = unlock_result(svc, "arxiv", arxiv_id, files)
            problem = access_problem(files)
        if problem:
            die(f"{problem}. {unlock_hint(files, arxiv_id)}")
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
    svc = service_from(args)
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
    config = load_config(config_path(args))
    web = config.setdefault("web", {})
    cookies = dict(web.get("cookies") or {})
    for _host, _path, _secure, _expiry, name, value in rows:
        cookies[name] = value
    web["cookies"] = cookies
    target = write_config(config, config_path(args))
    out(f"Wrote {len(cookies)} cookie(s) to {target}")
    out("Includes: " + ", ".join(cookies))
    return 0


def cmd_config(args) -> int:
    """Show the config, hiding secrets, and where it lives."""
    target = config_path(args)
    config = load_config(target)
    shown = json.loads(json.dumps(config))
    for section in ("web", "local"):
        values = shown.get(section) or {}
        for key in ("cookies", "api_key"):
            if isinstance(values.get(key), str) and values[key]:
                values[key] = "***"
        if isinstance(values.get("cookies"), dict):
            values["cookies"] = {k: "***" for k in values["cookies"]}
    out(f"config: {target}" + ("" if target.exists() else "  (not created yet)"))
    out(json.dumps(shown, ensure_ascii=False, indent=2))
    return 0


def cmd_account(args) -> int:
    """Whether the configured cookies still work, and how many pages are left.

    The website cookie has no refresh flow: when it dies the API answers
    LOGIN_REQUIRED and the only fix is signing in again and re-exporting it.
    """
    svc = service_from(args)
    if not svc.cookie:
        out("Not configured: web.cookies is empty in " + str(config_path(args)))
        return 1
    raw = svc.get_json("/api/billing/balance")
    if str(raw.get("error") or "") == "LOGIN_REQUIRED":
        out("Login expired: hjfy.top rejected the cookies. Sign in again, then:")
        out(f"  zotero-plus cookies    # import from Zotero's jar")
        out(f"  or edit web.cookies in {config_path(args)}")
        return 1
    balances = raw.get("balances") or []
    if not balances:
        out(json.dumps(raw, ensure_ascii=False, indent=2))
        return 0
    out("Signed in.")
    for row in balances:
        resource = row.get("resource", "?")
        available = row.get("available", 0)
        detail = ", ".join(f"{k} {row[k]}" for k in ("gift", "purchased", "reserved")
                           if isinstance(row.get(k), (int, float)) and row[k])
        out(f"  {resource:<20} available {available}" + (f"  ({detail})" if detail else ""))
    return 0


def cmd_login(args) -> int:
    svc = service_from(args)
    webbrowser.open(svc.base)
    out(f"Opened {svc.base}. Sign in there, then write the cookies into:")
    out(f"  {config_path(args)}  (web.cookies)")
    out("or re-export them from Zotero's jar:  zotero-plus cookies")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", help=f"Config file (default: {DEFAULT_CONFIG_FILE})")
    p.add_argument("--service-url", default=os.environ.get("ARXIV2ZH_SERVICE"),
                   help="Override web.service_url from the config")
    p.add_argument("--cookie", default=os.environ.get("ARXIV2ZH_COOKIE"),
                   help="Cookie header value for hjfy.top, overriding the config")
    p.add_argument("--cookie-file", default=None,
                   help=f"Legacy Netscape cookie file ({LEGACY_COOKIE_FILE})")
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
    t.add_argument("--unlock", action="store_true",
                   help="Spend the quoted pages to download the whole translation instead of a preview")
    t.add_argument("--local", action="store_true",
                   help="Use the local hjfy-pdf engine and the config's API settings instead of hjfy.top")
    t.add_argument("--pdf", help="With --local: translate this PDF instead of the item's")
    t.add_argument("--thinking-mode",
                   choices=["service-default", "enable-thinking-false",
                            "thinking-disabled", "reasoning-none"],
                   help="With --local: how to switch thinking off at the gateway "
                        "(overrides local.thinking_mode)")
    g = t.add_argument_group("local engine pass-through")
    g.add_argument("--engine-arg", action="append", metavar="OPT=VAL",
                   help="With --local: repeatable raw hjfy-pdf option, one per value "
                        "(--engine-arg=--pages=1-10). Options zotero-plus sets itself are rejected")
    g.add_argument("--pages", help="With --local: translate only these pages, 1,5-10")
    g.add_argument("--pdf-mode",
                   choices=["auto", "preserve", "reflow-translated", "reflow-bilingual"],
                   help="With --local: PDF layout. preserve keeps the source layout")
    g.add_argument("--bilingual", action="store_true",
                   help="With --local: interleave original and translated pages")
    g.add_argument("--bilingual-order", choices=["original-first", "translated-first"],
                   help="With --local and --bilingual: order inside each page pair")
    g.add_argument("--translate-tables", choices=["auto", "on", "off"],
                   help="With --local: table cells (overrides local.translate_tables, default off)")
    g.add_argument("--translate-bookmarks", action=argparse.BooleanOptionalAction, default=None,
                   help="With --local: translate PDF bookmark titles (engine default: on)")
    g.add_argument("--output-format", choices=["pdf", "pdf-docx", "pdf-pptx"],
                   help="With --local: also emit a sibling Word or PowerPoint file")
    g.add_argument("--terminology", action="append", metavar="CSV",
                   help="With --local: terminology CSV, repeatable (merged in order)")
    g.add_argument("--request-timeout", type=int, metavar="SECONDS",
                   help="With --local: end-to-end timeout for one HTTP request")
    g.add_argument("--on-page-error", choices=["fail", "keep-original"],
                   help="With --local: what to do when one page cannot be translated safely")
    g.add_argument("--dry-run", action="store_true",
                   help="With --local: print the engine command instead of running it")
    t.set_defaults(func=cmd_translate)

    d = sub.add_parser("download", help="Only download the finished Chinese PDF")
    d.add_argument("arxiv_id")
    d.add_argument("--out-dir", default=".")
    d.add_argument("--unlock", action="store_true",
                   help="Spend the quoted pages to download the whole translation instead of a preview")
    d.set_defaults(func=cmd_download)

    o = sub.add_parser("open", help="Open the paper on hjfy.top")
    o.add_argument("arxiv_id")
    o.set_defaults(func=cmd_open)

    lg = sub.add_parser("login", help="Open the sign-in page")
    lg.set_defaults(func=cmd_login)

    cf = sub.add_parser("config", help="Show the config file and where it lives")
    cf.set_defaults(func=cmd_config)

    ac = sub.add_parser("account", help="Check whether the cookies still work and show the balance")
    ac.set_defaults(func=cmd_account)

    ec = sub.add_parser("export-cookies", help="Import hjfy.top cookies from Zotero into the config")
    ec.set_defaults(func=cmd_export_cookies)

    args = p.parse_args()
    args.config_data = load_config(config_path(args))
    if getattr(args, "local", False):
        return cmd_translate_local(args)
    return args.func(args)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
