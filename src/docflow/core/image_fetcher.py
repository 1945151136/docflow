"""Download remote images referenced by Markdown into a local dir.

Fixes the original "image depends on a remote OSS URL" foot-gun: the Word
export becomes self-contained because every image is fetched & embedded.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import List, Tuple

import requests

_IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def _download(url: str, dest: Path, retries: int = 3, timeout: int = 30) -> bool:
    for attempt in range(retries):
        try:
            r = requests.get(url, timeout=timeout)
            r.raise_for_status()
            dest.write_bytes(r.content)
            return True
        except Exception:
            time.sleep(1.0)
    return False


def fetch_images(
    markdown: str,
    images_dir: str,
    base_name: str = "doc",
    width_inch: float = 6.0,
) -> Tuple[str, List[str]]:
    """Rewrite remote image URLs in `markdown` to local paths under `images_dir`.

    Returns (new_markdown, [downloaded_local_paths]).
    Local paths (file:// or relative) are left untouched.
    """
    images_dir = Path(images_dir)
    images_dir.mkdir(parents=True, exist_ok=True)
    downloaded: List[str] = []
    counter = 0

    def _repl(m: re.Match) -> str:
        nonlocal counter
        alt, url = m.group(1), m.group(2).strip()
        if url.startswith("http://") or url.startswith("https://"):
            counter += 1
            ext = Path(url.split("?")[0]).suffix or ".jpg"
            dest = images_dir / f"{base_name}_{counter:03d}{ext}"
            ok = _download(url, dest)
            if ok:
                downloaded.append(str(dest))
                return f"![{alt}]({dest.as_posix()})"
            # keep original if download failed (graceful)
            return m.group(0)
        return m.group(0)

    new_md = _IMG_RE.sub(_repl, markdown)
    return new_md, downloaded


def fetch_one(url: str, images_dir: str, name: str) -> str | None:
    """Download a single image; return local path or None on failure."""
    images_dir = Path(images_dir)
    images_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(url.split("?")[0]).suffix or ".jpg"
    dest = images_dir / f"{name}{ext}"
    return str(dest) if _download(url, dest) else None
