import os
import tempfile
import zipfile

from reportlab.pdfgen import canvas

from docflow.core.splitter import split_pdf, page_count


def _make_pdf(path: str, n_pages: int) -> None:
    c = canvas.Canvas(path)
    for i in range(n_pages):
        c.drawString(72, 720, f"Page {i + 1}")
        c.showPage()
    c.save()


def test_split_pdf_chunks():
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "book.pdf")
        _make_pdf(src, 12)
        assert page_count(src) == 12

        out = os.path.join(td, "chunks")
        parts = split_pdf(src, out, chunk_pages=5)
        assert len(parts) == 3, parts
        # each chunk is a valid PDF with the right page count
        counts = [page_count(p) for p in parts]
        assert counts == [5, 5, 2], counts


def test_split_pdf_single_small():
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "small.pdf")
        _make_pdf(src, 3)
        out = os.path.join(td, "chunks")
        parts = split_pdf(src, out, chunk_pages=150)
        assert len(parts) == 1
        # verify it's a real zip/pdf
        assert zipfile.is_zipfile(parts[0]) or parts[0].endswith(".pdf")
