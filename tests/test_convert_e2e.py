"""End-to-end convert() tests for the cloud (APIBackend) path.

Fully mocked — no network, no real API key, no billing. These lock the three
things that silently cost money or corrupt output:

  a) resume        : a second run must reuse task_id and NOT re-submit
                     (re-submitting a chunk = paying for it twice)
  b) rate limit    : `_post` must space calls by 1/qps
  c) merge         : `merge_results` must strip per-chunk split comments
"""
import os
import tempfile
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

import docflow.backends.api.base_api as base_api
from docflow.backends.api.base_api import APIBackend
from docflow.core.base import ConversionResult
from docflow.exporters.markdown import merge_results, export_markdown


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
class _FakeAPI(APIBackend):
    """Records submissions so we can assert on billing-relevant behaviour."""

    name = "fake"
    BASE_URL = "https://example.invalid"

    def __init__(self, **kw):
        super().__init__(api_key="dummy-key-not-used", **kw)
        self.submits = []
        self.polls = []

    def _submit_file(self, file_path, **opts):
        self.submits.append(file_path)
        return f"task-{len(self.submits)}"

    def _poll(self, task_id, **opts):
        self.polls.append(task_id)
        return f"# {task_id}\n\nbody of {task_id}"


def _make_pdf(path, n_pages):
    c = canvas.Canvas(path)
    for i in range(n_pages):
        c.drawString(72, 720, f"Page {i + 1}")
        c.showPage()
    c.save()


# --------------------------------------------------------------------------
# a) resume / never double-bill
# --------------------------------------------------------------------------
def test_rerun_reuses_task_id_and_never_resubmits():
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "book.pdf")
        _make_pdf(src, 12)
        work = os.path.join(td, "work")

        first = _FakeAPI(work_dir=work)
        r1 = first.convert(src, chunk_pages=5)
        assert len(first.submits) == 3, "12 pages @ 5/page should split into 3 chunks"
        assert len(first.polls) == 3

        # A fresh backend object against the same work dir must reuse every
        # task_id from disk. Zero new submissions == zero new charges.
        second = _FakeAPI(work_dir=work)
        r2 = second.convert(src, chunk_pages=5)
        assert second.submits == [], (
            "resume failed: chunk(s) were re-submitted and would be billed "
            f"twice: {second.submits}"
        )
        assert len(second.polls) == 3, "resumed chunks must still be polled"
        assert r2.markdown == r1.markdown


def test_taskid_file_sits_beside_chunks_when_workdir_omitted():
    """If the resume file and the chunks diverge, the lookup misses -> re-bill."""
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "book.pdf")
        _make_pdf(src, 3)

        b = _FakeAPI()  # no explicit work_dir
        b.convert(src)

        chunk_dir = Path(b.submits[0]).parent
        assert b._taskid_file().parent == chunk_dir
        assert b._taskid_file().exists()


# --------------------------------------------------------------------------
# b) QPS rate limiting
# --------------------------------------------------------------------------
class _FakeClock:
    """Deterministic clock: sleeps advance virtual time instead of blocking."""

    def __init__(self, start=1000.0):
        self.now = start
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, secs):
        self.sleeps.append(secs)
        self.now += secs


def test_post_enforces_qps_interval(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(base_api, "time", clock)

    stamps = []

    class _Resp:
        status_code = 200

        def json(self):
            return {}

    def fake_post(url, timeout=None, **kw):
        stamps.append(clock.now)
        return _Resp()

    monkeypatch.setattr(base_api.requests, "post", fake_post)

    b = APIBackend(api_key="k", qps=4)  # -> 0.25s between calls
    b._post("/a")
    b._post("/b")
    b._post("/c")

    gaps = [round(stamps[i + 1] - stamps[i], 6) for i in range(len(stamps) - 1)]
    assert gaps == [0.25, 0.25], f"rate limiter did not space calls: {gaps}"
    assert len(clock.sleeps) == 2


def test_post_respects_configured_qps(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(base_api, "time", clock)

    stamps = []

    class _Resp:
        def json(self):
            return {}

    monkeypatch.setattr(
        base_api.requests, "post", lambda url, timeout=None, **kw: (stamps.append(clock.now), _Resp())[1]
    )

    b = APIBackend(api_key="k", qps=2)  # -> 0.5s
    b._post("/a")
    b._post("/b")
    assert round(stamps[1] - stamps[0], 6) == 0.5


# --------------------------------------------------------------------------
# c) merge_results cleans per-chunk split comments
# --------------------------------------------------------------------------
def test_merge_results_strips_split_comments():
    r1 = ConversionResult(markdown="<!-- ===== part01_p1-150.pdf ===== -->\n\n# 第一章\n\n甲")
    r2 = ConversionResult(
        markdown="<!-- ===== part02_p151-300.pdf ===== -->\n\n# 第二章\n\n乙",
        images=["img_a.png"],
    )

    merged = merge_results([r1, r2])

    assert "<!--" not in merged.markdown, "split comments leaked into the deliverable"
    assert "part01_p1-150.pdf" not in merged.markdown
    assert "# 第一章" in merged.markdown and "# 第二章" in merged.markdown
    assert merged.markdown.index("# 第一章") < merged.markdown.index("# 第二章")
    assert merged.images == ["img_a.png"]
    assert merged.meta["chunks"] == 2


def test_merge_results_can_keep_split_comments():
    r = ConversionResult(markdown="<!-- ===== part01 ===== -->\n\nbody")
    merged = merge_results([r], strip_split_comments=False)
    assert "<!--" in merged.markdown


def test_merge_results_drops_empty_chunks():
    merged = merge_results([ConversionResult(markdown="a"), ConversionResult(markdown="")])
    assert merged.markdown == "a"


# --------------------------------------------------------------------------
# export round-trip (this was the NameError that lost a paid run)
# --------------------------------------------------------------------------
def test_export_markdown_writes_utf8():
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "sub", "out.md")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        export_markdown(ConversionResult(markdown="# 标题\n\n正文"), out)
        assert Path(out).read_text(encoding="utf-8") == "# 标题\n\n正文"
