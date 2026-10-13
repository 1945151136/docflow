"""Run docflow with the SoMark cloud backend (full pipeline).

Prerequisites
-------------
1. ``pip install -e ".[cloud,dev]"``
2. A ``.env`` file with ``SOMARK_API_KEY=sk-...``
   (copy from ``.env.example``). Key is read from env, never hardcoded.

What it does
------------
- Reads the PDF's real page count and compares it against today's remaining
  free pages from ``/usage`` BEFORE submitting anything. If the free quota
  cannot cover the whole document, it stops (no paid charges triggered).
- If the ``/usage`` check itself fails, it stops rather than running blind;
  pass ``--force`` to skip the check (paid charges may then apply).
- Splits the PDF and converts with ``docflow convert --backend somark``,
  which uses task_id resume so re-runs never double-bill.

Usage
-----
    python examples/run_somark.py path/to/book.pdf -o book.md
    python examples/run_somark.py path/to/book.pdf -o book.md --force
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# make `docflow` importable when run from repo root
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import requests
from dotenv import load_dotenv

from docflow.core.config import get_key, load_config
from docflow.core.splitter import page_count
from docflow.cli import main as cli_main

SOMARK_BASE = "https://somark.cn/api/v1"


def _free_pages_left(api_key: str) -> int:
    """Free pages still available TODAY (SoMark resets the free quota daily)."""
    r = requests.post(f"{SOMARK_BASE}/usage", data={"api_key": api_key}, timeout=30)
    r.raise_for_status()
    data = r.json().get("data", {}) or {}
    # verified against the live API: data["remaining_free_pages_today"]
    for field in ("remaining_free_pages_today", "remaining_free_pages", "free_pages_left"):
        if field in data:
            return int(data[field])
    return 0


def main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pdf", help="PDF to convert")
    ap.add_argument("-o", "--output", default=None)
    ap.add_argument("--force", action="store_true",
                    help="skip the /usage quota pre-check (paid charges may apply)")
    args = ap.parse_args()

    api_key = get_key(load_config(), "somark")
    if not api_key:
        print("ERROR: SOMARK_API_KEY not set in .env", file=sys.stderr)
        return 2

    # How many pages are we actually about to ask for?
    try:
        total = page_count(args.pdf)
    except Exception as e:
        print(f"ERROR: cannot read page count of {args.pdf}: {e}", file=sys.stderr)
        return 2

    if args.force:
        print(f"[run_somark] --force: skipping quota check (need {total} pages). "
              f"Paid charges may apply.", file=sys.stderr)
    else:
        # cheap pre-check: don't burn paid quota
        try:
            left = _free_pages_left(api_key)
        except Exception as e:
            print(f"STOP: /usage check failed ({e}). Refusing to run blind to avoid "
                  f"charges; re-run with --force to skip this check.", file=sys.stderr)
            return 3

        print(f"[run_somark] 免费额度剩余 {left} 页 / 需要 {total} 页")
        if left < total:
            print(f"STOP: 今日免费仅剩 {left} 页，不足以转完 {total} 页（差 {total - left} 页），"
                  f"继续会扣付费余额。已中止；确需继续请加 --force。", file=sys.stderr)
            return 3

    # delegate to the CLI convert path (somark backend, resume-safe)
    argv = ["convert", args.pdf, "--backend", "somark"]
    if args.output:
        argv += ["-o", args.output]
    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
