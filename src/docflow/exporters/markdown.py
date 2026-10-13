"""Markdown exporter: merge multiple ConversionResults into one clean Markdown.

Strips the per-chunk split comments the splitter/API layer may have injected,
and normalizes heading spacing. This is the bridge between any backend and a
single deliverable Markdown file.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List

from ..core.base import ConversionResult

_SPLIT_COMMENT_RE = re.compile(r"<!--\s*=+\s*.*?\s*=+\s*-->", re.DOTALL)


def merge_results(
    results: List[ConversionResult], strip_split_comments: bool = True
) -> ConversionResult:
    """Concatenate chunk results; collect images and page maps."""
    chunks: List[str] = []
    images: List[str] = []
    page_map: dict = {}
    for r in results:
        md = r.markdown or ""
        if strip_split_comments:
            md = _SPLIT_COMMENT_RE.sub("", md)
        chunks.append(md.strip())
        images.extend(r.images or [])
        page_map.update(r.page_map or {})

    text = "\n\n".join(c for c in chunks if c)
    return ConversionResult(
        markdown=text,
        images=images,
        meta={"backend": _first_meta(results, "backend"), "chunks": len(results)},
        page_map=page_map,
    )


def _first_meta(results: List[ConversionResult], key: str):
    for r in results:
        if key in (r.meta or {}):
            return r.meta[key]
    return None


def export_markdown(result: ConversionResult, out_path: str) -> str:
    """Write a ConversionResult's markdown to `out_path`; return the path."""
    Path(out_path).write_text(result.markdown, encoding="utf-8")
    return out_path
