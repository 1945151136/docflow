"""SoMark cloud document-intelligence backend (https://somark.cn/api/v1).

Built from the validated ad-hoc script used in the textbook conversion:
  * POST /usage            -> quota check
  * POST /parse/async      -> upload PDF + output_formats, get data.task_id
  * POST /parse/async_check-> poll status, on SUCCESS read
                             data.result.outputs.markdown
API key is passed as a form field (NOT an Authorization header) for SoMark.
"""
from __future__ import annotations

import time
from typing import Optional

from .base_api import APIBackend


class SoMarkBackend(APIBackend):
    name = "somark"
    BASE_URL = "https://somark.cn/api/v1"
    SUBMIT_ENDPOINT = "/parse/async"
    CHECK_ENDPOINT = "/parse/async_check"

    def __init__(self, api_key: Optional[str] = None, **cfg):
        super().__init__(api_key=api_key, **cfg)

    # --- quota ---
    def usage(self) -> dict:
        r = self._post("/usage", data={"api_key": self.api_key})
        r.raise_for_status()
        return r.json()

    # --- submit ---
    def _submit_file(self, file_path: str, **opts) -> str:
        output_formats = opts.get("output_formats", ["markdown"])
        with open(file_path, "rb") as f:
            r = self._post(
                self.SUBMIT_ENDPOINT,
                data={"api_key": self.api_key, "output_formats": output_formats},
                files={"file": (file_path.split("/")[-1].split("\\")[-1], f)},
            )
        r.raise_for_status()
        j = r.json()
        return j["data"]["task_id"]

    # --- poll ---
    def _poll(self, task_id: str, **opts) -> str:
        while True:
            r = self._post(
                self.CHECK_ENDPOINT,
                data={"task_id": task_id, "api_key": self.api_key},
            )
            r.raise_for_status()
            j = r.json()
            status = j["data"]["status"]
            if status == "SUCCESS":
                return j["data"]["result"]["outputs"]["markdown"]
            if status == "FAILED":
                raise RuntimeError(f"SoMark task {task_id} failed: {j}")
            time.sleep(self.poll_interval)
