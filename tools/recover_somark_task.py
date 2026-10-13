"""Recover the markdown of an already-submitted SoMark task.

Why this exists
---------------
A SoMark `parse/async` job that has already been billed stays queryable by
`task_id` for a while. If a run was interrupted *after* the submit (window
closed, machine slept, process killed), the credits are spent but no output
was written. Polling `parse/async_check` is a **read-only** status query — it
costs nothing and never re-submits.

This is the recovery path for exactly that situation. It is intentionally
separate from `docflow convert`: it never uploads, never splits, never
submits. It can only ever read.

Usage:
    PYTHONPATH=src python tools/recover_somark_task.py <task_id> -o out.md
    PYTHONPATH=src python tools/recover_somark_task.py --from-work-dir <dir> -o out.md
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from docflow.core.base import ConversionResult  # noqa: E402
from docflow.core.config import get_key, load_config  # noqa: E402
from docflow.exporters.markdown import export_markdown, merge_results  # noqa: E402

CHECK_URL = "https://somark.cn/api/v1/parse/async_check"


def task_ids_from_work_dir(work_dir: str) -> list[str]:
    """Read the task_ids base_api.py persisted for a work dir."""
    f = Path(work_dir) / ".taskid.json"
    if not f.exists():
        raise SystemExit(f"no .taskid.json in {work_dir}")
    return list(json.loads(f.read_text(encoding="utf-8")).values())


def fetch(task_id: str, api_key: str) -> dict:
    r = requests.post(CHECK_URL, data={"api_key": api_key, "task_id": task_id}, timeout=60)
    r.raise_for_status()
    payload = r.json()
    if payload.get("code") != 0:
        raise SystemExit(f"{task_id}: {payload.get('message')}")
    return payload.get("data") or {}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("task_id", nargs="?", help="a single task_id to fetch")
    ap.add_argument("--from-work-dir", help="read every task_id from <dir>/.taskid.json")
    ap.add_argument("-o", "--out", required=True, help="output markdown path")
    args = ap.parse_args(argv)

    if bool(args.task_id) == bool(args.from_work_dir):
        raise SystemExit("give exactly one of: <task_id> | --from-work-dir <dir>")

    ids = [args.task_id] if args.task_id else task_ids_from_work_dir(args.from_work_dir)
    api_key = get_key(load_config(), "somark")
    if not api_key:
        raise SystemExit("SOMARK_API_KEY is not set in .env")

    chunks, meta = [], {}
    for task_id in ids:
        data = fetch(task_id, api_key)
        status = data.get("status")
        markdown = ((data.get("result") or {}).get("outputs") or {}).get("markdown") or ""
        print(f"[recover] {task_id}  status={status}  {len(markdown)} chars  "
              f"({data.get('file_name')})")
        if status != "SUCCESS" or not markdown:
            print(f"[recover] 跳过 {task_id}（未完成或无 markdown）")
            continue
        chunks.append(markdown)
        meta[task_id] = {"status": status, "file_name": data.get("file_name"),
                         "record_id": data.get("record_id")}

    if not chunks:
        raise SystemExit("没有可取回的 markdown")

    # Go through merge_results (not a bare join) so the recovered file is
    # byte-identical to what a normal `docflow convert` run would have written.
    result = merge_results([ConversionResult(markdown=c, meta=meta) for c in chunks])
    path = export_markdown(result, args.out)
    print(f"[recover] 已写出 {path}（{len(result.markdown)} 字符）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
