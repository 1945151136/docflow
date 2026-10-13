"""Pure helpers for updating a `.env` file without disturbing the rest of it.

`render_env` is the pure core (text in -> text out); `save_env_value` is the
thin I/O wrapper. Keeping the merge logic pure is what makes the GUI's
"save API key" button testable without touching a real `.env`.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict


def _key_pattern(key: str) -> re.Pattern:
    # matches `KEY=...` at line start, ignoring surrounding spaces and an
    # optional `export ` prefix
    return re.compile(rf"^(\s*(?:export\s+)?){re.escape(key)}\s*=", re.IGNORECASE)


def render_env(original: str, updates: Dict[str, str]) -> str:
    """Return `original` with `updates` applied.

    - An existing `KEY=...` line is replaced in place (comments and the order
      of every other line are preserved).
    - A missing key is appended at the end.
    - A key may only appear once in the result.

    Pure: no filesystem access.
    """
    remaining = dict(updates)
    out_lines = []

    for line in original.splitlines():
        matched = None
        prefix = ""
        for key in remaining:
            m = _key_pattern(key).match(line)
            if m:
                matched = key
                prefix = m.group(1)  # keep any `export ` / indentation
                break
        if matched is None:
            out_lines.append(line)
        else:
            out_lines.append(f"{prefix}{matched}={remaining.pop(matched)}")

    if remaining:
        # keep a blank separator if the file was non-empty and has no trailing
        # blank line, so appended keys stay readable
        if out_lines and out_lines[-1].strip():
            out_lines.append("")
        for key, value in remaining.items():
            out_lines.append(f"{key}={value}")

    text = "\n".join(out_lines)
    if not text.endswith("\n"):
        text += "\n"
    return text


def save_env_value(path: str, key: str, value: str) -> str:
    """Apply `render_env` to the file at `path` (created if absent).

    Returns the path written to.
    """
    p = Path(path)
    original = p.read_text(encoding="utf-8") if p.exists() else ""
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_env(original, {key: value}), encoding="utf-8")
    return str(p)


def save_env_values(path: str, updates: Dict[str, str]) -> str:
    """Same as `save_env_value` but for several keys in one write."""
    p = Path(path)
    original = p.read_text(encoding="utf-8") if p.exists() else ""
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_env(original, dict(updates)), encoding="utf-8")
    return str(p)
