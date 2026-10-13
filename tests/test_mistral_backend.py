"""Mistral OCR backend tests — fully mocked, no network, no key, no billing.

Locks the sync single-shot contract: upload (/files) -> OCR (/ocr) -> merge.
"""
import os
import tempfile

import pytest
import requests
from reportlab.pdfgen import canvas

import docflow.backends.api.base_api as base_api
from docflow.backends.api.mistral import MistralBackend


# --------------------------------------------------------------------------
# mock transport
# --------------------------------------------------------------------------
class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _install_mock(monkeypatch, ocr_pages=None, status_for=None):
    """Patch requests.post and record every call. Returns the call log."""
    calls = []
    ocr_pages = ocr_pages if ocr_pages is not None else [
        {"index": 0, "markdown": "# 第一页\n\n甲", "images": [{"id": "i0", "image_url": "https://cdn/0.jpg"}]},
        {"index": 1, "markdown": "第二页内容", "images": []},
    ]
    status_for = status_for or {}

    def _post(url, timeout=None, headers=None, files=None, data=None, json=None):
        calls.append({
            "url": url,
            "headers": headers,
            "files": {k: v[0] for k, v in (files or {}).items()},
            "data": data,
            "json": json,
        })
        endpoint = "/files" if url.endswith("/files") else "/ocr"
        status = status_for.get(endpoint, 200)
        if status >= 400:
            return _Resp({"error": "boom"}, status=status)
        if endpoint == "/files":
            return _Resp({"id": f"file-{sum(1 for c in calls if c['url'].endswith('/files'))}"})
        return _Resp({"pages": ocr_pages})

    monkeypatch.setattr(base_api.requests, "post", _post)
    return calls


def _make_pdf(path, n_pages):
    c = canvas.Canvas(path)
    for i in range(n_pages):
        c.drawString(72, 720, f"Page {i + 1}")
        c.showPage()
    c.save()


def _backend(**kw):
    # high qps -> no real sleeping in the rate limiter during tests
    kw.setdefault("qps", 1000)
    return MistralBackend(api_key="test-key", **kw)


# --------------------------------------------------------------------------
# happy path
# --------------------------------------------------------------------------
def test_convert_uploads_then_ocrs_then_merges(monkeypatch):
    calls = _install_mock(monkeypatch)
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "doc.pdf")
        _make_pdf(src, 3)
        b = _backend(work_dir=os.path.join(td, "work"))

        result = b.convert(src, chunk_pages=150)

    assert [c["url"] for c in calls] == [
        "https://api.mistral.ai/v1/files",
        "https://api.mistral.ai/v1/ocr",
    ]

    upload, ocr = calls
    assert upload["headers"]["Authorization"] == "Bearer test-key"
    assert upload["data"] == {"purpose": "ocr"}
    assert "file" in upload["files"]

    assert ocr["headers"]["Authorization"] == "Bearer test-key"
    assert ocr["json"]["model"] == "mistral-ocr-latest"
    assert ocr["json"]["document"] == {"type": "file", "file_id": "file-1"}
    assert ocr["json"]["include_image_base64"] is False

    assert "# 第一页" in result.markdown
    assert "第二页内容" in result.markdown
    assert result.markdown.index("# 第一页") < result.markdown.index("第二页内容")
    assert result.images == ["https://cdn/0.jpg"]


def test_convert_chunks_large_pdf(monkeypatch):
    calls = _install_mock(monkeypatch)
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "big.pdf")
        _make_pdf(src, 12)
        b = _backend(work_dir=os.path.join(td, "work"))

        b.convert(src, chunk_pages=5)

    uploads = [c for c in calls if c["url"].endswith("/files")]
    ocrs = [c for c in calls if c["url"].endswith("/ocr")]
    assert len(uploads) == 3, "12 pages @ 5/page should upload 3 chunks"
    assert len(ocrs) == 3
    # each OCR must reference the file_id its own upload returned
    assert [c["json"]["document"]["file_id"] for c in ocrs] == ["file-1", "file-2", "file-3"]


def test_empty_pages_are_dropped_from_merge(monkeypatch):
    _install_mock(monkeypatch, ocr_pages=[
        {"index": 0, "markdown": "", "images": []},
        {"index": 1, "markdown": "只有这页", "images": []},
        {"index": 2, "markdown": "   ", "images": []},
    ])
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "doc.pdf")
        _make_pdf(src, 2)
        b = _backend(work_dir=os.path.join(td, "work"))
        result = b.convert(src)

    assert result.markdown == "只有这页"


def test_ocr_can_request_base64_images(monkeypatch):
    calls = _install_mock(monkeypatch)
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "doc.pdf")
        _make_pdf(src, 2)
        b = _backend(work_dir=os.path.join(td, "work"))
        b.convert(src, include_image_base64=True)

    ocr = [c for c in calls if c["url"].endswith("/ocr")][0]
    assert ocr["json"]["include_image_base64"] is True


def test_custom_model_is_forwarded(monkeypatch):
    calls = _install_mock(monkeypatch)
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "doc.pdf")
        _make_pdf(src, 2)
        b = _backend(work_dir=os.path.join(td, "work"))
        b.convert(src, model="mistral-ocr-2503")

    ocr = [c for c in calls if c["url"].endswith("/ocr")][0]
    assert ocr["json"]["model"] == "mistral-ocr-2503"


# --------------------------------------------------------------------------
# failure paths
# --------------------------------------------------------------------------
def test_missing_api_key_raises_before_any_request(monkeypatch):
    calls = _install_mock(monkeypatch)
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "doc.pdf")
        _make_pdf(src, 2)
        b = MistralBackend(api_key=None, qps=1000)
        with pytest.raises(ValueError, match="api_key is required"):
            b.convert(src)
    assert calls == [], "must not touch the network when the key is missing"


def test_upload_http_error_propagates(monkeypatch):
    calls = _install_mock(monkeypatch, status_for={"/files": 401})
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "doc.pdf")
        _make_pdf(src, 2)
        b = _backend(work_dir=os.path.join(td, "work"))
        with pytest.raises(requests.HTTPError):
            b.convert(src)
    assert [c["url"] for c in calls] == ["https://api.mistral.ai/v1/files"], (
        "a failed upload must stop the run, not fall through to OCR"
    )


def test_ocr_http_error_propagates(monkeypatch):
    _install_mock(monkeypatch, status_for={"/ocr": 500})
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "doc.pdf")
        _make_pdf(src, 2)
        b = _backend(work_dir=os.path.join(td, "work"))
        with pytest.raises(requests.HTTPError):
            b.convert(src)


# --------------------------------------------------------------------------
# does not disturb the async contract it inherits
# --------------------------------------------------------------------------
def test_base_api_async_hooks_remain_abstract():
    b = MistralBackend(api_key="k")
    with pytest.raises(NotImplementedError):
        b._submit_file("x.pdf")
    with pytest.raises(NotImplementedError):
        b._poll("task-1")
