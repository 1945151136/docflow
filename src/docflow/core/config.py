"""Configuration loader: .env (API keys) + config.yaml (behavior).

Keys are NEVER hardcoded in code — they come from the environment only.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Dict

try:
    from dotenv import load_dotenv
except Exception:  # python-dotenv optional at import time
    def load_dotenv(*_a, **_k):  # type: ignore
        return False

import yaml

DEFAULTS: Dict[str, Any] = {
    "default_backend": "paddleocr",
    "splitter": {"chunk_pages": 150, "max_bytes": 200 * 1024 * 1024},
    "api": {"qps": 4, "poll_interval": 4, "timeout": 60},
    "images": {"dir": "_images", "width_inch": 6.0},
    "markdown": {"strip_split_comments": True},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | None = None) -> Dict[str, Any]:
    """Load YAML config merged over DEFAULTS, then inject env-derived keys."""
    load_dotenv()
    # deepcopy: DEFAULTS is module-level state. Binding it directly (the old
    # `cfg = DEFAULTS`) meant the `keys`/`default_backend` writes below leaked
    # back into DEFAULTS and into every later load_config() call.
    cfg: Dict[str, Any] = copy.deepcopy(DEFAULTS)
    if path and Path(path).exists():
        with open(path, "r", encoding="utf-8") as f:
            cfg = _deep_merge(DEFAULTS, yaml.safe_load(f) or {})

    cfg["default_backend"] = os.getenv("DOCFLOW_DEFAULT_BACKEND", cfg["default_backend"])
    cfg["keys"] = {
        "somark": os.getenv("SOMARK_API_KEY"),
        "mistral": os.getenv("MISTRAL_API_KEY"),
        "firecrawl": os.getenv("FIRECRAWL_API_KEY"),
    }
    return cfg


def get_key(cfg: Dict[str, Any], backend: str) -> str | None:
    return (cfg.get("keys") or {}).get(backend)
