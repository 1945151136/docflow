"""PaddleOCR backend.

The engine is an optional multi-gigabyte dependency, so nothing here loads
it: `_load_engine` is the single seam and the tests swap it out. What they
pin down is the glue that was actually wrong —

  * the shape PP-StructureV3 returns. A page is a dict-like
    `LayoutParsingResultV2`, NOT a list of blocks. An earlier version of the
    backend did `for block in page:` and then `block.get(...)`, which iterates
    the dict's *string keys* and dies with AttributeError on every document.
  * the field names. `label` / `content`, not `type` / `text`.
  * the engine's own Markdown rendering, which we should use instead of
    re-deriving layout -> Markdown ourselves.
  * one page per `predict()` call, so an interrupted run resumes instead of
    redoing every page it already finished.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate
from reportlab.lib.styles import getSampleStyleSheet

from docflow.backends.local import paddleocr as mod
from docflow.backends.local.paddleocr import PaddleOCRBackend


# --------------------------------------------------------------------------
# fixtures: real PDFs (the backend now splits them, so they must be real)
# --------------------------------------------------------------------------
def make_pdf(path: Path, pages: int = 1) -> Path:
    """A real N-page PDF. PageBreak matters: reportlab happily flows several
    one-line paragraphs onto a single page, so without it a "3-page" fixture
    is silently a 1-page file and every multi-page test tests nothing."""
    doc = SimpleDocTemplate(str(path), pagesize=A4)
    style = getSampleStyleSheet()["BodyText"]
    flow = []
    for i in range(pages):
        if i:
            flow.append(PageBreak())
        flow.append(Paragraph(f"page {i + 1}", style))
    doc.build(flow)
    return path


class _FakeBlock:
    def __init__(self, label, content):
        self.label = label
        self.content = content


class _FakePage(dict):
    """Mimics LayoutParsingResultV2: a dict that *also* has `.markdown`.

    Being a dict matters — it is what made the original `for block in page:`
    bug fail instead of quietly doing nothing.
    """

    def __init__(self, blocks=(), markdown_texts=None, markdown_images=None):
        super().__init__(parsing_res_list=list(blocks))
        self._md = None
        if markdown_texts is not None or markdown_images is not None:
            self._md = {
                "markdown_texts": markdown_texts or "",
                "markdown_images": markdown_images or {},
            }

    @property
    def markdown(self):
        return self._md


class _FakeEngine:
    """Stands in for PPStructureV3.

    `pages` holds one entry per `predict()` call, in call order, because the
    backend feeds the engine a single page at a time.
    """

    instances = []
    pages = []
    raises = None
    fail_on_call = None  # 1-based predict() call that blows up, for resuming

    def __init__(self, **kw):
        self.init_kwargs = kw
        self.predict_calls = []
        _FakeEngine.instances.append(self)

    def predict(self, input=None, **kw):
        self.predict_calls.append((input, kw))
        if _FakeEngine.fail_on_call == len(self.predict_calls):
            raise RuntimeError("interrupted")
        if _FakeEngine.raises is not None:
            raise _FakeEngine.raises
        i = len(self.predict_calls) - 1
        return [_FakeEngine.pages[i]] if i < len(_FakeEngine.pages) else []


def _reset_engine():
    _FakeEngine.instances = []
    _FakeEngine.pages = []
    _FakeEngine.raises = None
    _FakeEngine.fail_on_call = None


@pytest.fixture(autouse=True)
def _fake_engine(monkeypatch):
    _reset_engine()
    monkeypatch.setattr(mod, "_load_engine", lambda: _FakeEngine)
    yield
    _reset_engine()


@pytest.fixture
def pdf(tmp_path):
    return make_pdf(tmp_path / "book.pdf", pages=1)


def _convert(tmp_path, pdf, **opts):
    # "figures", not "imgs": the source refs start with "imgs/", so a
    # same-named output dir would make substring assertions pass by accident.
    opts.setdefault("images_dir", str(tmp_path / "figures"))
    opts.setdefault("work_dir", str(tmp_path / "work"))
    return PaddleOCRBackend().convert(str(pdf), **opts)


# --------------------------------------------------------------------------
# availability — must not claim True when the engine is unusable
# --------------------------------------------------------------------------
def test_available_is_true_when_the_engine_loads(monkeypatch):
    monkeypatch.setattr(mod, "_load_engine", lambda: _FakeEngine)
    assert PaddleOCRBackend().available() is True


def test_available_is_false_when_the_engine_is_missing(monkeypatch):
    def boom():
        raise ImportError("cannot import name 'PPStructureV3' from 'paddleocr'")

    monkeypatch.setattr(mod, "_load_engine", boom)
    assert PaddleOCRBackend().available() is False, \
        "an older paddleocr imports fine but has no PPStructureV3 — not usable"


def test_available_is_false_for_any_import_failure(monkeypatch):
    def boom():
        raise ModuleNotFoundError("No module named 'paddleocr'")

    monkeypatch.setattr(mod, "_load_engine", boom)
    assert PaddleOCRBackend().available() is False


# --------------------------------------------------------------------------
# error messages — must say what is actually wrong
# --------------------------------------------------------------------------
def test_convert_explains_a_wrong_version_instead_of_crying_not_installed(tmp_path, monkeypatch):
    def boom():
        raise ImportError("cannot import name 'PPStructureV3' from 'paddleocr'")

    monkeypatch.setattr(mod, "_load_engine", boom)
    monkeypatch.setitem(sys.modules, "paddleocr",
                        type("m", (), {"__version__": "2.10.0"}))

    with pytest.raises(RuntimeError) as ei:
        PaddleOCRBackend().convert(str(make_pdf(tmp_path / "x.pdf")))

    msg = str(ei.value)
    assert "2.10.0" in msg, "the installed version must appear in the message"
    assert "paddleocr>=3" in msg
    assert "not installed" not in msg, "it *is* installed — saying otherwise is a lie"


def test_convert_says_not_installed_when_paddleocr_is_absent(tmp_path, monkeypatch):
    def boom():
        raise ModuleNotFoundError("No module named 'paddleocr'")

    monkeypatch.setattr(mod, "_load_engine", boom)

    real_import = __import__

    def fake_import(name, *a, **k):
        if name == "paddleocr":
            raise ModuleNotFoundError("No module named 'paddleocr'")
        return real_import(name, *a, **k)

    monkeypatch.setattr("builtins.__import__", fake_import)

    with pytest.raises(RuntimeError) as ei:
        PaddleOCRBackend().convert(str(make_pdf(tmp_path / "x.pdf")))

    assert "not installed" in str(ei.value)
    assert "docflow[paddle]" in str(ei.value)


def test_convert_chains_the_original_error(tmp_path, monkeypatch):
    def boom():
        raise ImportError("original cause")

    monkeypatch.setattr(mod, "_load_engine", boom)

    with pytest.raises(RuntimeError) as ei:
        PaddleOCRBackend().convert(str(make_pdf(tmp_path / "x.pdf")))

    assert isinstance(ei.value.__cause__, ImportError)
    assert "original cause" in str(ei.value.__cause__)


# --------------------------------------------------------------------------
# the page shape that broke the old implementation
# --------------------------------------------------------------------------
def test_a_page_is_dict_like_and_does_not_crash_the_reader(tmp_path, pdf):
    """The exact old bug: iterating a dict page yields keys, not blocks."""
    _FakeEngine.pages = [_FakePage(blocks=[_FakeBlock("text", "正文")])]

    assert "正文" in _convert(tmp_path, pdf).markdown


def test_the_engines_own_markdown_wins_over_re_rendering(tmp_path, pdf):
    _FakeEngine.pages = [_FakePage(
        blocks=[_FakeBlock("text", "BARE BLOCK")],
        markdown_texts="## 引擎渲染的标题\n\n引擎渲染的正文",
    )]

    md = _convert(tmp_path, pdf).markdown

    assert "引擎渲染的标题" in md
    assert "BARE BLOCK" not in md, \
        "the engine already rendered this page; re-deriving it loses fidelity"


def test_heading_labels_become_headings_in_the_fallback(tmp_path, pdf):
    _FakeEngine.pages = [_FakePage(blocks=[
        _FakeBlock("doc_title", "第一章 营养学基础"),
        _FakeBlock("text", "1.蛋白质是人体必需营养素。"),
    ])]

    md = _convert(tmp_path, pdf).markdown

    assert "# 第一章 营养学基础" in md
    assert "1.蛋白质是人体必需营养素。" in md


def test_blocks_are_read_by_label_and_content_not_guessed_names(tmp_path, pdf):
    """Guard against a relapse to the invented `type`/`text` field names."""
    page = _FakePage(blocks=[
        {"type": "text", "text": "GHOST"},
        {"label": "text", "content": "REAL"},
    ])
    page._md = None  # force the fallback renderer
    _FakeEngine.pages = [page]

    md = _convert(tmp_path, pdf).markdown

    assert "REAL" in md
    assert "GHOST" not in md


def test_pages_are_joined_in_order(tmp_path):
    pdf = make_pdf(tmp_path / "three.pdf", pages=3)
    _FakeEngine.pages = [_FakePage(markdown_texts=t) for t in ("第一页", "第二页", "第三页")]

    md = _convert(tmp_path, pdf).markdown

    assert md.index("第一页") < md.index("第二页") < md.index("第三页")


def test_pages_without_content_are_skipped(tmp_path, pdf):
    _FakeEngine.pages = [_FakePage()]

    assert _convert(tmp_path, pdf).markdown == ""


def test_engine_is_constructed_once_and_given_the_source(tmp_path, pdf):
    _FakeEngine.pages = [_FakePage(markdown_texts="x")]

    _convert(tmp_path, pdf)

    assert len(_FakeEngine.instances) == 1
    assert len(_FakeEngine.instances[0].predict_calls) == 1


# --------------------------------------------------------------------------
# one page per call, progress, and resume
# --------------------------------------------------------------------------
def test_a_page_is_fed_to_the_engine_one_at_a_time(tmp_path):
    pdf = make_pdf(tmp_path / "three.pdf", pages=3)
    _FakeEngine.pages = [_FakePage(markdown_texts=str(i)) for i in range(3)]

    _convert(tmp_path, pdf)

    assert len(_FakeEngine.instances[0].predict_calls) == 3


def test_a_single_page_pdf_is_handed_over_unsplit(tmp_path, pdf):
    """No point writing a copy of a one-page file."""
    _FakeEngine.pages = [_FakePage(markdown_texts="x")]

    _convert(tmp_path, pdf)

    src_seen = _FakeEngine.instances[0].predict_calls[0][0]
    assert src_seen == str(pdf)


def test_progress_counts_every_page_and_ends_at_the_total(tmp_path):
    pdf = make_pdf(tmp_path / "three.pdf", pages=3)
    _FakeEngine.pages = [_FakePage(markdown_texts=str(i)) for i in range(3)]
    seen = []

    _convert(tmp_path, pdf, progress=lambda done, total: seen.append((done, total)))

    assert seen == [(1, 3), (2, 3), (3, 3)]


def test_a_restarted_run_does_not_re_ocr_finished_pages(tmp_path):
    pdf = make_pdf(tmp_path / "three.pdf", pages=3)
    _FakeEngine.pages = [_FakePage(markdown_texts=t) for t in ("第一页", "第二页", "第三页")]
    _FakeEngine.fail_on_call = 2  # die part-way through page 2

    with pytest.raises(RuntimeError):
        _convert(tmp_path, pdf)

    _FakeEngine.instances = []
    _FakeEngine.pages = [_FakePage(markdown_texts="第二页"), _FakePage(markdown_texts="第三页")]
    _FakeEngine.fail_on_call = None
    second = _convert(tmp_path, pdf)

    engine = _FakeEngine.instances[0]
    assert len(engine.predict_calls) == 2, \
        "page 1 is on disk; only pages 2 and 3 should reach the engine"
    assert "第一页" in second.markdown, "the cached page is still part of the output"
    assert "第二页" in second.markdown and "第三页" in second.markdown


def test_a_blank_page_is_cached_and_not_redone(tmp_path):
    """Blank pages are real; re-OCRing them on every resume would be waste."""
    pdf = make_pdf(tmp_path / "two.pdf", pages=2)
    _FakeEngine.pages = [_FakePage(markdown_texts="有字"), _FakePage()]

    first = _convert(tmp_path, pdf)
    cache = tmp_path / "work" / "two.pages"
    assert (cache / "page_0002.md").read_text(encoding="utf-8") == ""

    _FakeEngine.instances = []
    _FakeEngine.pages = [_FakePage(markdown_texts="不该被调用")]
    second = _convert(tmp_path, pdf)

    assert _FakeEngine.instances == [], \
        "a fully cached run should not even build the engine (it loads ~1.5GB of models)"
    assert second.markdown == first.markdown == "有字"


def test_the_engine_is_built_on_the_first_cache_miss_not_up_front(tmp_path):
    pdf = make_pdf(tmp_path / "two.pdf", pages=2)
    _FakeEngine.pages = [_FakePage(markdown_texts="第一页")]
    _FakeEngine.fail_on_call = 2
    with pytest.raises(RuntimeError):
        _convert(tmp_path, pdf)

    _FakeEngine.instances = []
    _FakeEngine.pages = [_FakePage(markdown_texts="第二页")]
    _convert(tmp_path, pdf)

    assert len(_FakeEngine.instances) == 1
    assert len(_FakeEngine.instances[0].predict_calls) == 1


def test_resume_rebuilds_the_image_list_from_the_cache(tmp_path):
    pdf = make_pdf(tmp_path / "two.pdf", pages=2)
    img = _png(tmp_path)
    _FakeEngine.pages = [_FakePage(
        markdown_texts="![](imgs/fig.jpg)",
        markdown_images={"imgs/fig.jpg": img},
    )]
    first = _convert(tmp_path, pdf)
    assert len(first.images) == 1

    # Second run: page 1 comes from cache, so nothing is written this time.
    _FakeEngine.instances = []
    _FakeEngine.pages = [_FakePage(markdown_texts="")]
    second = _convert(tmp_path, pdf)

    assert second.images == first.images, \
        "a resumed run must still report the figures the cached page references"


def test_work_dir_is_honoured(tmp_path, pdf):
    _FakeEngine.pages = [_FakePage(markdown_texts="x")]

    _convert(tmp_path, pdf, work_dir=str(tmp_path / "somewhere"))

    assert (tmp_path / "somewhere").exists()
    assert list((tmp_path / "somewhere").glob("*.pages"))


# --------------------------------------------------------------------------
# oneDNN — the flag without which predict() crashes on paddlepaddle 3.3.1
# --------------------------------------------------------------------------
def test_onEDNN_is_disabled_by_default(tmp_path, pdf, monkeypatch):
    monkeypatch.delenv(mod._MKLDNN_ENV, raising=False)
    _FakeEngine.pages = [_FakePage(markdown_texts="x")]

    _convert(tmp_path, pdf)

    assert _FakeEngine.instances[0].init_kwargs == {"enable_mkldnn": False}


def test_caller_can_turn_oneDNN_back_on(tmp_path, pdf, monkeypatch):
    monkeypatch.delenv(mod._MKLDNN_ENV, raising=False)
    _FakeEngine.pages = [_FakePage(markdown_texts="x")]

    _convert(tmp_path, pdf, mkldnn=True)

    assert _FakeEngine.instances[0].init_kwargs == {"enable_mkldnn": True}


def test_env_var_can_turn_oneDNN_back_on(tmp_path, pdf, monkeypatch):
    monkeypatch.setenv(mod._MKLDNN_ENV, "1")
    _FakeEngine.pages = [_FakePage(markdown_texts="x")]

    _convert(tmp_path, pdf)

    assert _FakeEngine.instances[0].init_kwargs == {"enable_mkldnn": True}


def test_the_explicit_option_beats_the_env_var(tmp_path, pdf, monkeypatch):
    monkeypatch.setenv(mod._MKLDNN_ENV, "1")
    _FakeEngine.pages = [_FakePage(markdown_texts="x")]

    _convert(tmp_path, pdf, mkldnn=False)

    assert _FakeEngine.instances[0].init_kwargs == {"enable_mkldnn": False}


# --------------------------------------------------------------------------
# images: PIL objects in, real files + rewritten refs out
# --------------------------------------------------------------------------
def _png(tmp_path, name="src.png"):
    p = tmp_path / name
    Image.new("RGB", (4, 4), "white").save(p)
    return Image.open(p)


def test_figures_are_written_to_disk_and_reported(tmp_path, pdf):
    _FakeEngine.pages = [_FakePage(
        markdown_texts="看图：\n\n![](imgs/img_in_table_box_1_2_3_4.jpg)",
        markdown_images={"imgs/img_in_table_box_1_2_3_4.jpg": _png(tmp_path)},
    )]

    result = _convert(tmp_path, pdf)

    assert len(result.images) == 1
    written = Path(result.images[0])
    assert written.exists(), "the reference must point at a file that exists"
    assert written.read_bytes() == (tmp_path / "figures" / written.name).read_bytes()


def test_image_references_are_rewritten_to_the_saved_path(tmp_path, pdf):
    _FakeEngine.pages = [_FakePage(
        markdown_texts="![](imgs/fig.jpg)",
        markdown_images={"imgs/fig.jpg": _png(tmp_path)},
    )]

    result = _convert(tmp_path, pdf)

    assert "](imgs/fig.jpg)" not in result.markdown, \
        "the original ref resolves to nothing on disk — Word would drop the figure"
    saved = Path(result.images[0]).resolve().as_posix()
    assert f"![]({saved})" in result.markdown


def test_images_the_markdown_never_references_are_not_written(tmp_path, pdf):
    """PP-StructureV3 parks table crops it does not link; don't hoard them."""
    _FakeEngine.pages = [_FakePage(
        markdown_texts="正文，没有插图",
        markdown_images={"imgs/img_in_table_box_416_166_689_318.jpg": _png(tmp_path)},
    )]

    result = _convert(tmp_path, pdf)

    assert result.images == []
    assert not (tmp_path / "figures").exists()


def test_a_document_without_figures_creates_no_directory(tmp_path, pdf):
    _FakeEngine.pages = [_FakePage(markdown_texts="纯文字")]
    images_dir = tmp_path / "figures"

    _convert(tmp_path, pdf)

    assert not images_dir.exists()


def test_an_unwritable_figure_does_not_sink_the_page(tmp_path, pdf):
    class Unsaveable:
        def save(self, dest):
            raise OSError("disk full")

    _FakeEngine.pages = [_FakePage(
        markdown_texts="正文\n\n![](imgs/fig.jpg)",
        markdown_images={"imgs/fig.jpg": Unsaveable()},
    )]

    result = _convert(tmp_path, pdf)

    assert "正文" in result.markdown
    assert result.images == []


# --------------------------------------------------------------------------
# HTML tables -> GitHub Markdown
# --------------------------------------------------------------------------
def test_html_tables_become_github_tables(tmp_path, pdf):
    _FakeEngine.pages = [_FakePage(markdown_texts=(
        '<div style="text-align: center;"><html><body>'
        "<table border=\"1\"><tr><td>Nutrient</td><td>Per 100 g</td></tr>"
        "<tr><td>Energy</td><td>165</td></tr></table>"
        "</body></html></div>"
    ))]

    md = _convert(tmp_path, pdf).markdown

    assert "| Nutrient | Per 100 g |" in md
    assert "| --- | --- |" in md
    assert "| Energy | 165 |" in md
    assert "<table" not in md, "raw HTML reaches the Word exporter as literal text"
    assert "<div" not in md


def test_table_block_content_is_converted_in_the_fallback(tmp_path, pdf):
    page = _FakePage(blocks=[_FakeBlock(
        "table",
        "<html><body><table><tr><td>a</td><td>b</td></tr></table></body></html>",
    )])
    page._md = None
    _FakeEngine.pages = [page]

    md = _convert(tmp_path, pdf).markdown

    assert "| a | b |" in md
    assert "<html>" not in md
