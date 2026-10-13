"""PDF splitter: break a large PDF into API-friendly chunks.

Fixes the original hard-coded split logic into a parameterized utility
(chunk_pages / max_bytes) so any backend with page/file limits can reuse it.
"""
from __future__ import annotations

import os
from pathlib import Path

from pypdf import PdfReader, PdfWriter


def split_pdf(
    src: str,
    out_dir: str,
    chunk_pages: int = 150,
    max_bytes: int | None = 200 * 1024 * 1024,
    prefix: str = "part",
) -> list[str]:
    """Split `src` into <=chunk_pages page chunks, each under max_bytes.

    Returns a list of chunk PDF paths (ordered). A small PDF (<chunk_pages
    and <max_bytes) yields a single chunk.
    """
    reader = PdfReader(src)
    total = len(reader.pages)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    parts: list[str] = []
    start = 0
    idx = 1
    while start < total:
        end = min(start + chunk_pages, total)
        # shrink the chunk if it would exceed the byte cap
        while True:
            w = PdfWriter()
            for p in range(start, end):
                w.add_page(reader.pages[p])
            tmp = out_dir / f"{prefix}{idx:02d}_p{start + 1}-{end}.pdf"
            with open(tmp, "wb") as f:
                w.write(f)
            size = tmp.stat().st_size
            if max_bytes is not None and size > max_bytes and (end - start) > 1:
                end = (start + end) // 2  # halve and retry
                tmp.unlink()
                continue
            break
        parts.append(str(tmp))
        start = end
        idx += 1
    return parts


def page_count(src: str) -> int:
    return len(PdfReader(src).pages)
