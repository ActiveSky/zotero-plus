#!/usr/bin/env python3
"""Workflow commands: arxiv-version, find, pdf.

Routed from scripts/zotero_plus.py, but also runnable directly:

    python scripts/workflow/main.py arxiv-version --keys KEY
"""

from __future__ import annotations

import argparse
import sys

import arxiv_version
import find
import oa_pdf


def main() -> int:
    p = argparse.ArgumentParser(
        prog="zotero-plus workflow",
        description="arXiv version checks, published-version discovery, open PDFs")
    sub = p.add_subparsers(dest="command", required=True)
    arxiv_version.add_parser(sub)
    find.add_parser(sub)
    oa_pdf.add_parser(sub)
    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
