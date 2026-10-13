"""Core abstractions: BaseConverter + ConversionResult."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class ConversionResult:
    """Normalized output of any converter backend."""

    markdown: str = ""
    images: List[str] = field(default_factory=list)  # local image file paths
    meta: Dict[str, Any] = field(default_factory=dict)
    page_map: Dict[int, str] = field(default_factory=dict)  # page_no -> section title

    def __len__(self) -> int:
        return len(self.markdown)


class BaseConverter:
    """Every backend (cloud API or local engine) implements this."""

    name: str = "base"

    def convert(self, src: str, **opts) -> ConversionResult:
        raise NotImplementedError

    @classmethod
    def available(cls) -> bool:
        """Return True if the backend's heavy deps are importable."""
        return True
