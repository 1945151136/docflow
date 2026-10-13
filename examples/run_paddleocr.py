"""Run docflow fully offline with the local PaddleOCR backend.

Prerequisites
-------------
1. ``pip install -e ".[paddle,dev]"``  (downloads paddlepaddle + paddleocr, large)
2. Nothing else — no API key, no network, no quota.

What it does
------------
Converts a PDF/image to Markdown entirely on your machine via PP-StructureV3.
Sensitive documents never leave the computer. Slower than cloud, but free
and private.

Usage
-----
    python examples/run_paddleocr.py path/to/book.pdf -o book.md
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from docflow.cli import main as cli_main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pdf", help="PDF/image to convert")
    ap.add_argument("-o", "--output", default=None)
    args = ap.parse_args()

    argv = ["convert", args.pdf, "--backend", "paddleocr"]
    if args.output:
        argv += ["-o", args.output]
    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
