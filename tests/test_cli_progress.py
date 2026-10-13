"""CLI page-progress reporting.

Local OCR spends minutes per scanned page with nothing to show for it, so a
long run reads as a hang. These pin the two shapes the printer takes: a
self-overwriting line on a terminal, one line per page when redirected.
"""
from __future__ import annotations

import argparse
import io
import sys

import docflow.cli as cli
from docflow.backends.local.paddleocr import PaddleOCRBackend
from docflow.core.base import ConversionResult


class _FakeStderr(io.StringIO):
    def __init__(self, tty: bool):
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_a_terminal_gets_one_self_overwriting_line(monkeypatch):
    err = _FakeStderr(tty=True)
    monkeypatch.setattr(sys, "stderr", err)
    report = cli._page_progress()

    report(1, 3)
    assert err.getvalue() == "\r[docflow] 本地 OCR 1/3 页"

    err.seek(0)
    err.truncate(0)
    report(3, 3)
    assert err.getvalue() == "\r[docflow] 本地 OCR 3/3 页\n", \
        "the last page must end the line, or the next output lands on top of it"


def test_a_redirected_log_gets_one_line_per_page(monkeypatch):
    err = _FakeStderr(tty=False)
    monkeypatch.setattr(sys, "stderr", err)

    report = cli._page_progress()
    for done in range(1, 23):
        report(done, 22)

    assert err.getvalue().splitlines() == [
        "[docflow] 本地 OCR 10/22 页",
        "[docflow] 本地 OCR 20/22 页",
        "[docflow] 本地 OCR 22/22 页",
    ]


def test_a_redirected_log_always_reports_a_single_page_run(monkeypatch):
    err = _FakeStderr(tty=False)
    monkeypatch.setattr(sys, "stderr", err)

    cli._page_progress()(1, 1)

    assert err.getvalue().splitlines() == ["[docflow] 本地 OCR 1/1 页"], \
        "1 page is not a multiple of 10 — the end-of-run line must still fire"


def test_convert_hands_the_backend_the_work_dir_and_a_progress_callback(monkeypatch, tmp_path):
    seen = {}

    class _Backend:
        def convert(self, src, **opts):
            seen.update(opts)
            return ConversionResult(markdown="x")

    monkeypatch.setattr(cli, "_build_backends",
                        lambda cfg, work_dir=None: {"fake": lambda: _Backend()})
    args = argparse.Namespace(file="a.pdf", backend=None,
                              output=str(tmp_path / "o.md"), work_dir=str(tmp_path / "w"))

    cli.cmd_convert(args, {"default_backend": "fake", "splitter": {"chunk_pages": 150}})

    assert seen["work_dir"] == str(tmp_path / "w"), \
        "without a stable work dir the page cache cannot be found again"
    assert callable(seen["progress"])
    assert seen["chunk_pages"] == 150


def test_the_backend_defaults_its_work_dir_to_the_documented_place():
    from docflow.backends.local import paddleocr as mod

    assert mod._DEFAULT_WORK_DIR == ".docflow_work", \
        "must match the CLI's --work-dir default and stay gitignored"


def test_the_local_backend_accepts_progress_through_its_public_surface():
    """Cheap guard: the option names the CLI sends are the ones it reads."""
    import inspect

    sig = inspect.signature(PaddleOCRBackend.convert)
    assert any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()), \
        "convert must keep taking **opts, or the CLI's progress= call would break it"
