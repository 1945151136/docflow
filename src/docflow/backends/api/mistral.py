"""Mistral OCR backend (https://api.mistral.ai/v1) — synchronous single-shot.

Unlike SoMark, Mistral OCR is **not** an async submit/poll service: one request
returns the whole document. So this backend deliberately does NOT use
`APIBackend`'s submit/poll/task_id machinery — it overrides `convert()` and runs
the sync path itself, reusing only the shared pieces that still apply:

  * `APIBackend._post()`     -> rate limiting + per-request timeout
  * `split_pdf()`            -> chunk oversized PDFs to a per-request-safe size
  * `merge_results()`        -> strip chunk markers and join into one document

Flow per chunk:
  POST /files   (multipart upload, purpose="ocr")  -> {"id": file_id}
  POST /ocr     {"model": ..., "document": {"type": "file", "file_id": ...}}
                -> {"pages": [{"index", "markdown", "images": [...]}]}

`base_api.py` is intentionally untouched.

Note on billing: because the OCR step is synchronous there is no `task_id` to
resume from, so a re-run re-uploads and re-bills every chunk. Keep runs
contiguous; see the README caveat before re-running an expensive document.
"""
from __future__ import annotations

import os
from typing import List, Optional, Tuple

from ...core.base import ConversionResult
from ...core.splitter import split_pdf
from ...exporters.markdown import merge_results
from .base_api import APIBackend


class MistralBackend(APIBackend):
    name = "mistral"
    BASE_URL = "https://api.mistral.ai/v1"
    UPLOAD_ENDPOINT = "/files"
    OCR_ENDPOINT = "/ocr"
    OCR_MODEL = "mistral-ocr-latest"

    def __init__(self, api_key: Optional[str] = None, **cfg):
        super().__init__(api_key=api_key, **cfg)

    # ---- shared header helper ----
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    # ---- sync single-shot pipeline ----
    def convert(self, src: str, **opts) -> ConversionResult:
        if not self.api_key:
            raise ValueError("mistral: api_key is required (set MISTRAL_API_KEY in .env)")

        chunk_pages = int(opts.get("chunk_pages", 150))
        wd = self._resolve_work_dir(src)
        self._active_work_dir = wd
        parts: List[str] = split_pdf(src, str(wd), chunk_pages=chunk_pages)

        results = [self._ocr_part(part, **opts) for part in parts]
        return merge_results(results)

    def _ocr_part(self, part: str, **opts) -> ConversionResult:
        file_id = self._upload(part)
        markdown, images = self._ocr(file_id, **opts)
        return ConversionResult(
            markdown=markdown,
            images=images,
            meta={"backend": self.name, "part": part, "file_id": file_id},
        )

    # ---- step 1: upload ----
    def _upload(self, file_path: str) -> str:
        with open(file_path, "rb") as f:
            r = self._post(
                self.UPLOAD_ENDPOINT,
                headers=self._headers(),
                data={"purpose": "ocr"},
                files={"file": (os.path.basename(file_path), f)},
            )
        r.raise_for_status()
        return r.json()["id"]

    # ---- step 2: OCR ----
    def _ocr(self, file_id: str, **opts) -> Tuple[str, List[str]]:
        payload = {
            "model": opts.get("model", self.OCR_MODEL),
            "document": {"type": "file", "file_id": file_id},
            "include_image_base64": bool(opts.get("include_image_base64", False)),
        }
        r = self._post(self.OCR_ENDPOINT, headers=self._headers(), json=payload)
        r.raise_for_status()
        j = r.json()

        pages = j.get("pages") or []
        blocks: List[str] = []
        images: List[str] = []
        for page in pages:
            md = (page.get("markdown") or "").strip()
            if md:
                blocks.append(md)
            for im in page.get("images") or []:
                url = im.get("image_url")
                if url:
                    images.append(url)

        return "\n\n".join(blocks), images
