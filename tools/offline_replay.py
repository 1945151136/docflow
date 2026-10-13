# -*- coding: utf-8 -*-
"""Offline replay: feed the ALREADY-PAID legacy chunk .md files through
docflow's own merge + export path, with zero API calls.

Purpose: verify docflow's merge/exporter is equivalent to the legacy merge
BEFORE deciding whether to spend quota re-running the cloud conversion.
Prints summary stats only (never dumps the 1.7MB of text).

Usage:
    offline_replay.py <chunks-dir> <reference.md> [-o out.md]

The paths are arguments (they used to be hardcoded to one machine's Desktop,
so nobody else could run this).
"""
import argparse
import os
import re
import sys

from docflow.core.base import ConversionResult
from docflow.exporters.markdown import merge_results, export_markdown

HEAD_RE = re.compile(r"^#{1,6}\s+\S", re.M)
IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)")
MARKER_RE = re.compile(r"<!--\s*=+\s*.*?\s*=+\s*-->", re.S)


def stats(label, text):
    return {
        "label": label,
        "chars": len(text),
        "headings": len(HEAD_RE.findall(text)),
        "images": len(IMG_RE.findall(text)),
        "markers": len(MARKER_RE.findall(text)),
        "lines": text.count("\n") + 1,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("chunks_dir", help="directory of legacy per-chunk .md files")
    ap.add_argument("reference", help="the legacy merged .md to compare against")
    ap.add_argument("-o", "--output", default="out/offline_replay.md",
                    help="where to write docflow's merge (default: %(default)s)")
    args = ap.parse_args(argv)

    parts = sorted(
        os.path.join(args.chunks_dir, f)
        for f in os.listdir(args.chunks_dir) if f.endswith(".md")
    )
    print("input parts:", len(parts))
    for p in parts:
        print(f"   {os.path.basename(p):28s} {os.path.getsize(p):>9,} B")

    results = []
    for p in parts:
        with open(p, encoding="utf-8") as f:
            md = f.read()
        results.append(ConversionResult(markdown=md, meta={"part": p}))

    merged = merge_results(results)
    # export_markdown() writes exactly where it is told and does not create
    # parents, and the default output lives in out/ (gitignored, so generated
    # documents never end up in the repo root).
    parent = os.path.dirname(args.output)
    if parent:
        os.makedirs(parent, exist_ok=True)
    export_markdown(merged, args.output)

    ref = open(args.reference, encoding="utf-8").read()
    got = open(args.output, encoding="utf-8").read()

    rows = [stats(f"legacy  {os.path.basename(args.reference)}", ref),
            stats(f"docflow {os.path.basename(args.output)}", got)]
    print()
    for r in rows:
        print("  {label:34s} chars={chars:>9,} headings={headings:>5} images={images:>4} markers={markers:>2}".format(**r))

    print()
    if got == ref:
        print("RESULT: byte-identical")
    else:
        # Characterise the delta precisely instead of dumping it.
        print(f"RESULT: differs | len delta = {len(got) - len(ref):+,}")
        ref_nomark = MARKER_RE.sub("", ref).strip()
        print(f"  reference after stripping its chunk markers: {len(ref_nomark):,} chars")
        print(f"  docflow output                             : {len(got):,} chars")
        print(f"  equal modulo markers? {got == ref_nomark}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
