"""Base class for cloud document-intelligence APIs.

Provides the shared machinery every async OCR/parse API needs:
  * file upload + async submit
  * status polling (with QPS rate-limit + retry)
  * **task_id persistence -> resumable runs** (re-running never re-submits,
    so you never pay twice for the same chunk)

Subclasses only implement `_submit_file()` and `_poll()`.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

import requests

from ...core.base import BaseConverter, ConversionResult
from ...core.splitter import split_pdf
from ...exporters.markdown import merge_results


class APIBackend(BaseConverter):
    BASE_URL: str = ""
    # subclasses set endpoints
    SUBMIT_ENDPOINT: str = "/parse/async"
    CHECK_ENDPOINT: str = "/parse/async_check"

    def __init__(
        self,
        api_key: Optional[str] = None,
        qps: float = 4,
        poll_interval: float = 4,
        timeout: int = 60,
        work_dir: Optional[str] = None,
        **cfg,
    ):
        self.api_key = api_key
        self.qps = qps
        self.poll_interval = poll_interval
        self.timeout = timeout
        self.cfg = cfg
        self._last_call = 0.0
        self.work_dir = Path(work_dir) if work_dir else None

    # ---- rate-limited POST ----
    def _post(self, endpoint: str, **kw) -> requests.Response:
        wait = 1.0 / max(self.qps, 0.1) - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()
        url = self.BASE_URL.rstrip("/") + endpoint
        return requests.post(url, timeout=self.timeout, **kw)

    # ---- task_id persistence (resume) ----
    def _resolve_work_dir(self, src: str) -> Path:
        """Single source of truth for the work dir.

        Both the split chunks and `.taskid.json` MUST live in the same place:
        `_load_taskid` is keyed on the chunk's absolute path, so if the two
        disagree the resume lookup silently misses and the chunk is
        re-submitted -> billed twice.
        """
        wd = Path(self.work_dir) if self.work_dir else Path(src).parent / ".docflow_work"
        wd.mkdir(parents=True, exist_ok=True)
        return wd

    def _taskid_file(self) -> Path:
        wd = getattr(self, "_active_work_dir", None) or self.work_dir or Path.cwd() / ".docflow_work"
        wd = Path(wd)
        wd.mkdir(parents=True, exist_ok=True)
        return wd / ".taskid.json"

    def _load_taskid(self, part: str) -> Optional[str]:
        f = self._taskid_file()
        if not f.exists():
            return None
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return None
        return data.get(os.path.abspath(part))

    def _save_taskid(self, part: str, task_id: str) -> None:
        f = self._taskid_file()
        data: Dict[str, str] = {}
        if f.exists():
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data[os.path.abspath(part)] = task_id
        f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- subclass hooks ----
    def _submit_file(self, file_path: str, **opts) -> str:
        raise NotImplementedError

    def _poll(self, task_id: str, **opts) -> str:
        raise NotImplementedError

    # ---- generic orchestration ----
    def convert(self, src: str, **opts) -> ConversionResult:
        if not self.api_key:
            raise ValueError(f"{self.name}: api_key is required (set it via .env / config)")

        chunk_pages = int(opts.get("chunk_pages", 150))
        wd = self._resolve_work_dir(src)
        self._active_work_dir = wd
        parts: List[str] = split_pdf(src, str(wd), chunk_pages=chunk_pages)

        results: List[ConversionResult] = []
        for part in parts:
            tid = self._load_taskid(part)
            if not tid:
                tid = self._submit_file(part, **opts)
                self._save_taskid(part, tid)
            md = self._poll(tid, **opts)
            results.append(ConversionResult(markdown=md, meta={"part": part, "task_id": tid}))
        return merge_results(results)
