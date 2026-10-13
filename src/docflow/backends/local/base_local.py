"""Base class for local OCR / parsing engines."""
from __future__ import annotations

from ...core.base import BaseConverter


class LocalBackend(BaseConverter):
    """Local engines run offline; no API key required."""

    def convert(self, src: str, **opts):
        raise NotImplementedError
