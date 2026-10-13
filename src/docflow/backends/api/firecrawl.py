"""Firecrawl backend — TODO skeleton (interface aligned, not yet implemented).

Endpoint shape (per Firecrawl docs):
  POST https://api.firecrawl.dev/v1/scrape  (or /v1/batch/scrape for async)
  body: { "url": "...", "formats": ["markdown"], "pdf": true, ... }
  For local files you typically host the PDF or use the file upload flow.
  Response: { "success": true, "data": { "markdown": "..." , "images": [...] } }

Once implemented, override _submit_file / _poll (or a single-shot convert if
Firecrawl's synchronous scrape fits your needs). Reuse APIBackend's resume
machinery if you switch to /v1/batch/scrape (async job id).
"""
from __future__ import annotations

from typing import Optional

from .base_api import APIBackend


class FirecrawlBackend(APIBackend):
    name = "firecrawl"
    BASE_URL = "https://api.firecrawl.dev/v1"

    def __init__(self, api_key: Optional[str] = None, **cfg):
        super().__init__(api_key=api_key, **cfg)

    def _submit_file(self, file_path: str, **opts) -> str:
        raise NotImplementedError("Firecrawl adapter not implemented yet (TODO)")

    def _poll(self, task_id: str, **opts) -> str:
        raise NotImplementedError("Firecrawl adapter not implemented yet (TODO)")
