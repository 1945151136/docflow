"""PaddleOCR (PP-StructureV3) local backend — the default offline path.

Apache-2.0, strong on Chinese, CPU-runnable. The heavy deps (paddlepaddle,
paddleocr) are imported lazily so the rest of docflow imports without them
(installed via `pip install 'docflow[paddle]'`).

What the engine actually gives us (verified against paddleocr 3.7.0 /
paddlepaddle 3.3.1, not guessed from docs):

    pages = PPStructureV3().predict(input=pdf)   # list, one item per page
    page                                  # LayoutParsingResultV2, a dict
    page.markdown["markdown_texts"]       # the page rendered as Markdown
    page.markdown["markdown_images"]      # {relative_path: PIL.Image}
    page["parsing_res_list"]              # list[LayoutBlock], each with
                                          #   .label / .content

So we do not hand-roll a layout->Markdown renderer: PP-StructureV3 already
produced one, and it handles tables, formulas and reading order far better
than anything we would write. Our job is the glue the engine leaves out —
writing the in-memory images to disk, and turning the HTML tables it emits
into GitHub Markdown so the rest of the pipeline sees what it expects.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .base_local import LocalBackend
from ...core.base import ConversionResult


def _load_engine():
    """Import and return the PP-StructureV3 class (indirection for testing)."""
    from paddleocr import PPStructureV3

    return PPStructureV3


def _install_hint(err: Exception) -> str:
    """Explain *why* the engine could not be loaded, and how to fix it.

    A bare `import paddleocr` is not proof the backend works: paddleocr 2.x
    imports fine but has no `PPStructureV3`. Reporting that as "not installed"
    sent users to reinstall a package they already had.
    """
    try:
        import paddleocr

        version = getattr(paddleocr, "__version__", "unknown")
    except Exception:
        return ("PaddleOCR is not installed.\n"
                "Run: pip install 'docflow[paddle]'")

    return (
        f"PaddleOCR {version} is installed, but it has no PPStructureV3 — "
        f"that class only exists in paddleocr >= 3.\n"
        f"Run: pip install -U 'paddleocr>=3'\n"
        f"(import error: {err})"
    )


# PP-StructureV3 cannot run on paddlepaddle 3.3.1 with oneDNN left on: the
# layout-detection model — the first thing predict() does — dies inside
# paddle's new PIR executor with
#   NotImplementedError: ConvertPirAttribute2RuntimeAttribute not support
#   [pir::ArrayAttribute<pir::DoubleAttribute>]   (onednn_instruction.cc:118)
# That took out every conversion, on a plain CPU box, with no workaround but
# this flag. It costs CPU throughput, so `mkldnn=True` / DOCFLOW_PADDLE_MKLDNN=1
# restores the old behaviour on machines where oneDNN is healthy.
_MKLDNN_DEFAULT = False
_MKLDNN_ENV = "DOCFLOW_PADDLE_MKLDNN"

# Per-page Markdown cache + the split single-page PDFs live here. Matches the
# CLI's --work-dir default and is gitignored.
_DEFAULT_WORK_DIR = ".docflow_work"

# Labels PP-StructureV3 uses for headings, by the depth they deserve.
_HEADING_LABELS = {"doc_title": "#", "paragraph_title": "##"}
_OTHER_TITLE_LABELS = {"figure_title", "table_title", "chart_title", "title"}

_TABLE_RE = re.compile(r"<table\b.*?</table>", re.DOTALL | re.IGNORECASE)
_WRAPPER_TAG_RE = re.compile(r"</?(?:html|body|div)\b[^>]*>", re.IGNORECASE)


class PaddleOCRBackend(LocalBackend):
    name = "paddleocr"

    def available(self) -> bool:
        """True only if the engine this backend actually calls can be imported."""
        try:
            _load_engine()
            return True
        except Exception:
            return False

    def convert(self, src: str, **opts) -> ConversionResult:
        try:
            engine_cls = _load_engine()
        except Exception as e:
            raise RuntimeError(_install_hint(e)) from e

        work_dir = Path(opts.get("work_dir") or _DEFAULT_WORK_DIR)
        # One page at a time: predict() renders a whole file in a single call,
        # so a run interrupted at page 300 would redo all 300 on retry. Local
        # OCR is slow enough (minutes per scanned page) that this matters.
        page_srcs = _page_sources(src, work_dir)
        cache_dir = work_dir / (Path(src).stem + ".pages")

        images_dir = Path(opts.get("images_dir") or _default_images_dir(src))
        progress = opts.get("progress")

        # Constructed on the first cache miss, not up front: building the
        # pipeline loads ~1.5GB of models, and a run that is fully cached
        # should not pay a minute of model loading to read its own files.
        engine: Any = None

        chunks: List[str] = []
        images: List[str] = []
        total = len(page_srcs)
        for idx, page_src in enumerate(page_srcs, start=1):
            cached = cache_dir / f"page_{idx:04d}.md"
            if cached.exists():
                md = cached.read_text(encoding="utf-8")
                images.extend(_local_images_in(md))
            else:
                if engine is None:
                    engine = engine_cls(enable_mkldnn=_mkldnn_enabled(opts))
                md = self._render(engine, page_src, images_dir, images)
                cache_dir.mkdir(parents=True, exist_ok=True)
                cached.write_text(md, encoding="utf-8")
            if md:
                chunks.append(md)
            if progress:
                progress(idx, total)

        return ConversionResult(
            markdown="\n\n".join(chunks),
            images=images,
            meta={"backend": "paddleocr", "src": str(src), "pages": total},
        )

    def _render(self, engine: Any, page_src: Path, images_dir: Path,
                images: List[str]) -> str:
        """Convert a single-page file to Markdown."""
        parts: List[str] = []
        for page in engine.predict(input=str(page_src)) or []:
            md = _page_markdown(page)
            if not md:
                continue
            md = self._write_images(md, page, images_dir, images)
            parts.append(self._html_tables_to_md(md))
        return "\n\n".join(parts)

    # ---- images: the engine hands us PIL objects, the pipeline wants paths ----
    @staticmethod
    def _write_images(md: str, page: Any, images_dir: Path, images: List[str]) -> str:
        """Save the page's in-memory images and point the Markdown at them.

        The Markdown references them as `imgs/...`, which resolves to nothing
        on disk, so the Word export would silently drop every figure.
        Absolute paths are used because the caller decides where the `.md`
        lands, and only absolute references resolve from anywhere.
        """
        by_rel = _markdown_images(page)
        wanted = {rel: img for rel, img in by_rel.items() if f"({rel})" in md}
        if not wanted:
            # Nothing this page's Markdown points at. The engine also parks
            # crops it never references (table cutouts), and writing those
            # would leave orphan files plus a `result.images` that overcounts.
            return md
        images_dir.mkdir(parents=True, exist_ok=True)
        for rel, img in wanted.items():
            dest = images_dir / Path(rel).name
            try:
                img.save(dest)
            except Exception:
                continue  # a figure we cannot write must not kill the page
            # One canonical spelling, used for both the Markdown ref and the
            # `images` entry: a resumed run rebuilds `images` by parsing the
            # cached Markdown, so any difference here would make two runs of
            # the same document disagree.
            posix = dest.resolve().as_posix()
            images.append(posix)
            md = md.replace(f"({rel})", f"({posix})")
        return md

    @classmethod
    def _html_tables_to_md(cls, md: str) -> str:
        """Replace the engine's HTML tables with GitHub tables.

        The engine wraps them in `<div style="text-align:center"><html><body>`;
        that markup would reach the Word exporter as literal text, since it
        only understands Markdown.
        """
        md = _TABLE_RE.sub(lambda m: "\n" + cls._html_table_to_md(m.group(0)) + "\n", md)
        return _WRAPPER_TAG_RE.sub("", md)

    @staticmethod
    def _html_table_to_md(html: str) -> str:
        """Best-effort HTML <table> -> GitHub Markdown table."""
        try:
            from html.parser import HTMLParser

            class _T(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.rows: List[List[str]] = []
                    self.cur: List[str] = []
                    self.cell = ""
                    self.in_cell = False

                def handle_starttag(self, tag, attrs):
                    if tag in ("td", "th"):
                        self.in_cell = True
                        self.cell = ""

                def handle_endtag(self, tag):
                    if tag in ("td", "th"):
                        self.in_cell = False
                        self.cur.append(self.cell.strip())
                    elif tag == "tr":
                        if self.cur:
                            self.rows.append(self.cur)
                        self.cur = []

                def handle_data(self, data):
                    if self.in_cell:
                        self.cell += data

            p = _T()
            p.feed(html)
            if not p.rows:
                return html
            head, *body = p.rows
            out = ["| " + " | ".join(head) + " |",
                   "| " + " | ".join(["---"] * len(head)) + " |"]
            for r in body:
                out.append("| " + " | ".join(r) + " |")
            return "\n".join(out)
        except Exception:
            return html


# --------------------------------------------------------------------------
# page / block access — tolerant of both the object API and plain dicts
# --------------------------------------------------------------------------
def _field(obj: Any, name: str, default: Any = None) -> Any:
    """Read `name` off an object or a dict (LayoutBlock vs a plain dict)."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _markdown_images(page: Any) -> Dict[str, Any]:
    md = getattr(page, "markdown", None)
    if isinstance(md, dict):
        found = md.get("markdown_images")
        if isinstance(found, dict):
            return found
    return {}


def _page_markdown(page: Any) -> str:
    """The page's Markdown — the engine's own rendering, or a fallback."""
    md = getattr(page, "markdown", None)
    if isinstance(md, dict):
        text = md.get("markdown_texts")
        if text:
            return text
    blocks = _field(page, "parsing_res_list", []) or []
    return _blocks_to_md(blocks)


def _blocks_to_md(blocks: Sequence[Any]) -> str:
    """Fallback renderer, used only when the engine gives no Markdown.

    Reads the fields PP-StructureV3 actually sets on LayoutBlock: `label` and
    `content`. (An earlier version of this file guessed at `type`/`text` and
    never worked against a real engine.)
    """
    out: List[str] = []
    for block in blocks:
        label = str(_field(block, "label", "") or "").lower()
        content = str(_field(block, "content", "") or "").strip()
        if not content:
            continue
        if label in _HEADING_LABELS:
            out.append(f"{_HEADING_LABELS[label]} {content}")
        elif label in _OTHER_TITLE_LABELS:
            out.append(f"### {content}")
        elif label == "table" and "<table" in content.lower():
            out.append(PaddleOCRBackend._html_table_to_md(content))
        else:
            out.append(content)
    return "\n\n".join(out)


def _mkldnn_enabled(opts: Dict[str, Any]) -> bool:
    if "mkldnn" in opts:
        return bool(opts["mkldnn"])
    flag = os.environ.get(_MKLDNN_ENV)
    if flag is None:
        return _MKLDNN_DEFAULT
    return flag.strip().lower() not in ("", "0", "false", "no", "off")


def _default_images_dir(src: str) -> str:
    """Where to put extracted figures: beside the source document."""
    return str(Path(src).with_suffix("")) + "_images"


def _local_images_in(md: str) -> List[str]:
    """Image refs in `md` that point at files on disk.

    On a resumed run the pages come back from cache, so the `images` list has
    to be rebuilt from the Markdown rather than from what we just wrote.
    """
    found = []
    for ref in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", md):
        if not ref.startswith(("http://", "https://")) and Path(ref).exists():
            found.append(ref)
    return found


def _page_sources(src: str, work_dir: Path) -> List[Path]:
    """One single-page PDF per page (the source itself if it has only one).

    Splitting is what buys the per-page cache in convert(): it lets a restarted
    run hand the engine only the pages it has not done yet.
    """
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(src)
    if len(reader.pages) <= 1:
        return [Path(src)]

    out_dir = work_dir / (Path(src).stem + ".pages_src")
    out_dir.mkdir(parents=True, exist_ok=True)
    sources: List[Path] = []
    for i, page in enumerate(reader.pages, start=1):
        one = out_dir / f"p{i:04d}.pdf"
        if not one.exists():
            writer = PdfWriter()
            writer.add_page(page)
            with open(one, "wb") as fh:
                writer.write(fh)
        sources.append(one)
    return sources
