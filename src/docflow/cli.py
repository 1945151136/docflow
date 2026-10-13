"""docflow CLI: convert / to-docx / split."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .core.config import get_key, load_config
from .backends.api.somark import SoMarkBackend
from .backends.api.mistral import MistralBackend
from .backends.api.firecrawl import FirecrawlBackend
from .backends.local.paddleocr import PaddleOCRBackend
from .exporters.markdown import export_markdown
from .exporters.docx import md_to_docx
from .core.splitter import split_pdf


def _build_backends(cfg, work_dir=None):
    backends = {
        "somark": lambda: SoMarkBackend(api_key=get_key(cfg, "somark"),
                                         qps=cfg["api"]["qps"],
                                         poll_interval=cfg["api"]["poll_interval"],
                                         timeout=cfg["api"]["timeout"],
                                         work_dir=work_dir),
        "paddleocr": lambda: PaddleOCRBackend(),
        "mistral": lambda: MistralBackend(api_key=get_key(cfg, "mistral")),
        "firecrawl": lambda: FirecrawlBackend(api_key=get_key(cfg, "firecrawl")),
    }
    return backends


def _page_progress(label: str = "本地 OCR"):
    """Per-page progress on stderr.

    Local OCR takes minutes per scanned page, and without this a long run is
    indistinguishable from a hang. Overwrites one line on a terminal; emits a
    line per page when redirected, so logs stay readable on their own.
    """
    tty = sys.stderr.isatty()

    def report(done: int, total: int) -> None:
        line = f"[docflow] {label} {done}/{total} 页"
        if tty:
            print("\r" + line, end="\n" if done >= total else "", file=sys.stderr, flush=True)
        elif done >= total or done % 10 == 0:
            print(line, file=sys.stderr, flush=True)

    return report


def cmd_convert(args, cfg):
    backends = _build_backends(cfg, work_dir=args.work_dir)
    name = args.backend or cfg["default_backend"]
    if name not in backends:
        print(f"unknown backend: {name}", file=sys.stderr)
        return 2
    backend = backends[name]()
    print(f"[docflow] converting {args.file} with backend={name} ...")
    result = backend.convert(args.file,
                             chunk_pages=cfg["splitter"]["chunk_pages"],
                             work_dir=args.work_dir,
                             progress=_page_progress())

    out = args.output or (str(Path(args.file).with_suffix("")) + "_docflow.md")
    export_markdown(result, out)
    print(f"[docflow] wrote markdown -> {out}  ({len(result)} chars, {len(result.images)} images)")
    return 0


def cmd_to_docx(args, cfg):
    out = args.output or (str(Path(args.md).with_suffix("")) + ".docx")
    images_dir = Path(out).with_suffix("") / cfg["images"]["dir"]
    width = float(cfg["images"]["width_inch"])
    print(f"[docflow] markdown -> docx: {args.md} -> {out}")
    md_to_docx(args.md, out, images_dir=str(images_dir), width_inch=width)
    print(f"[docflow] wrote docx -> {out}")
    return 0


def cmd_split(args, cfg):
    parts = split_pdf(args.pdf, args.output_dir or (str(Path(args.pdf).parent / ".docflow_work")),
                     chunk_pages=args.chunk_pages)
    print(f"[docflow] split into {len(parts)} part(s):")
    for p in parts:
        print("  -", p)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="docflow", description="Pluggable document converter.")
    p.add_argument("--config", default=None,
                   help="path to config.yaml (default: ./config.yaml if it exists)")
    sub = p.add_subparsers(dest="cmd", required=True)

    # `--config` is also accepted after the subcommand. SUPPRESS keeps the
    # subparser from clobbering the top-level value with its own default.
    cfg_kw = dict(default=argparse.SUPPRESS,
                  help="path to config.yaml (default: ./config.yaml if it exists)")

    pc = sub.add_parser("convert", help="Convert PDF/image -> Markdown")
    pc.add_argument("--config", **cfg_kw)
    pc.add_argument("file")
    pc.add_argument("--backend", default=None, help="somark|paddleocr|mistral|firecrawl")
    pc.add_argument("-o", "--output", default=None)
    pc.add_argument("--work-dir", default=None,
                    help="dir for split chunks + .taskid.json (resume state). "
                         "Keep it stable across re-runs or you will be re-billed.")
    pc.set_defaults(func=cmd_convert)

    pt = sub.add_parser("to-docx", help="Convert Markdown -> Word (.docx)")
    pt.add_argument("--config", **cfg_kw)
    pt.add_argument("md")
    pt.add_argument("-o", "--output", default=None)
    pt.set_defaults(func=cmd_to_docx)

    ps = sub.add_parser("split", help="Split a PDF into API-friendly chunks")
    ps.add_argument("--config", **cfg_kw)
    ps.add_argument("pdf")
    ps.add_argument("--chunk-pages", type=int, default=150)
    ps.add_argument("-o", "--output-dir", default=None)
    ps.set_defaults(func=cmd_split)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Explicit --config wins; otherwise fall back to ./config.yaml when present.
    cfg_path = getattr(args, "config", None)
    if cfg_path is None:
        default = Path("config.yaml")
        cfg_path = str(default) if default.exists() else None

    cfg = load_config(cfg_path)
    return args.func(args, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
