"""docflow GUI — a thin Tkinter front-end over the existing public API.

Design notes
------------
* This module owns **no** conversion logic. It drives the same public entry
  points the CLI does (`backend.convert`, `export_markdown`, `md_to_docx`), so
  the CLI and GUI cannot drift apart.
* Everything that touches the network or does heavy work runs on a worker
  thread. The UI is only ever mutated on the Tk main thread, by draining a
  queue from an `after()` callback. Workers never call into Tk — not even to
  read a `StringVar`. Anything a worker needs is read out of the widgets
  *before* the thread starts and passed in as a plain argument.
* Confirmation dialogs are the one thing a worker cannot do itself, so they go
  through a small request/answer handshake over the same queue: the worker
  posts `("confirm", ...)` and waits on an `Event`; the main thread shows the
  dialog inside `_pump` and sets the answer. See `_ask_main`.
* Interactive controls are disabled for the duration of a run so the user
  cannot start two conversions (which, for a paid backend, would double-bill).
"""
from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, List, Optional, Tuple

from dotenv import find_dotenv, load_dotenv

from .backends.api.firecrawl import FirecrawlBackend
from .backends.api.mistral import MistralBackend
from .backends.api.somark import SoMarkBackend
from .backends.local.paddleocr import PaddleOCRBackend
from .core.config import get_key, load_config
from .core.envfile import save_env_value
from .core.splitter import page_count
from .exporters.docx import md_to_docx
from .exporters.markdown import export_markdown

BACKENDS: Tuple[str, ...] = ("somark", "paddleocr", "mistral", "firecrawl")

# Which env var holds the key for each cloud backend (local ones need none).
ENV_KEY_NAMES = {
    "somark": "SOMARK_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "firecrawl": "FIRECRAWL_API_KEY",
}

# Backends that cost money. Only `somark` exposes a balance we can read, so
# only it can be pre-checked; the rest get a warning instead.
BILLED_BACKENDS = frozenset({"somark", "mistral", "firecrawl"})

# Field names seen in SoMark's /usage payload, most specific first.
_USAGE_FIELDS = ("remaining_free_pages_today", "remaining_free_pages", "free_pages_left")

LOG_TAG_COLORS = {"err": "#ff6b6b", "ok": "#7ee787", "info": "#8ab4f8"}


def free_pages_left(api_key: str) -> int:
    """Today's remaining SoMark free pages.

    Read-only: `/usage` does not consume quota and cannot trigger a charge.
    Mirrors the guard used by ``examples/run_somark.py``.
    """
    payload = SoMarkBackend(api_key=api_key).usage() or {}
    data = payload.get("data") or {}
    for field in _USAGE_FIELDS:
        if field in data:
            return int(data[field])
    return 0


def make_backend(name: str, cfg: dict):
    """Build a backend from config using only public constructors."""
    if name == "somark":
        return SoMarkBackend(
            api_key=get_key(cfg, "somark"),
            qps=cfg["api"]["qps"],
            poll_interval=cfg["api"]["poll_interval"],
            timeout=cfg["api"]["timeout"],
        )
    if name == "mistral":
        return MistralBackend(
            api_key=get_key(cfg, "mistral"),
            qps=cfg["api"]["qps"],
            timeout=cfg["api"]["timeout"],
        )
    if name == "firecrawl":
        return FirecrawlBackend(api_key=get_key(cfg, "firecrawl"))
    return PaddleOCRBackend()


def _default_env_path() -> Path:
    """Where the "save API key" button writes.

    Prefer the `.env` that `load_config()` would actually load — python-dotenv
    searches upwards from the cwd — so the GUI always edits the same file the
    rest of docflow reads. Falling back to `cwd/.env` covers a fresh checkout
    that has none yet.

    Without this, launching `docflow-gui` from any directory other than the
    project root would silently write a second, unused `.env` there.
    """
    found = find_dotenv(usecwd=True)
    return Path(found) if found else Path.cwd() / ".env"


def _enable_hidpi() -> None:
    """Opt into per-monitor DPI awareness (Windows) so text isn't blurry."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass  # cosmetic only — never fatal


def _pick_theme(style: ttk.Style) -> str:
    """Use the native-looking theme when the platform has one."""
    available = set(style.theme_names())
    for name in ("vista", "winnative", "aqua", "clam", "default"):
        if name in available:
            style.theme_use(name)
            return name
    return style.theme_use()


class DocflowGUI:
    def __init__(self, root: tk.Tk, env_path: Optional[str] = None) -> None:
        self.root = root
        self.env_path = Path(env_path) if env_path else _default_env_path()
        self.log_queue: "queue.Queue[Tuple[str, Any, Any]]" = queue.Queue()
        self._controls: List[Tuple[tk.Widget, str]] = []
        self._busy = False
        self._out_touched = False

        # confirmation handshake (worker -> main thread -> worker)
        self._gate_event = threading.Event()
        self._gate_answer = False

        self.style = ttk.Style()
        _pick_theme(self.style)

        root.title("docflow — 文档转换")
        root.minsize(880, 620)
        self._center(920, 680)
        self._build()
        self._pump()

    # ------------------------------------------------------------------
    # layout
    # ------------------------------------------------------------------
    def _center(self, width: int, height: int) -> None:
        self.root.update_idletasks()
        x = max(0, (self.root.winfo_screenwidth() - width) // 2)
        y = max(0, (self.root.winfo_screenheight() - height) // 3)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _register(self, widget, normal_state: str = "normal"):
        """Track a widget so it can be disabled while a run is in flight."""
        self._controls.append((widget, normal_state))
        return widget

    def _build(self) -> None:
        root = self.root
        outer = ttk.Frame(root, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)

        self._build_credentials(outer, row=0)
        self._build_files(outer, row=1)
        self._build_actions(outer, row=2)
        self._build_log(outer, row=3)

        ttk.Separator(outer, orient="horizontal").grid(row=4, column=0, sticky="we", pady=(10, 6))
        self._build_statusbar(outer, row=5)
        self._refresh_docx_state()  # nothing to export until a markdown exists

    def _build_credentials(self, parent, row: int) -> None:
        box = ttk.LabelFrame(parent, text=" 后端与密钥 ", padding=10)
        box.grid(row=row, column=0, sticky="we", pady=(0, 8))
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text="后端").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        default = load_config()["default_backend"]
        if default not in BACKENDS:
            default = BACKENDS[0]
        self.backend_var = tk.StringVar(value=default)
        self.backend_box = self._register(
            ttk.Combobox(box, textvariable=self.backend_var, values=list(BACKENDS),
                         state="readonly", width=16),
            normal_state="readonly",
        )
        self.backend_box.grid(row=0, column=1, sticky="w", pady=4)

        self.usage_btn = self._register(
            ttk.Button(box, text="查询免费额度", command=self._on_usage))
        self.usage_btn.grid(row=0, column=2, sticky="e", padx=(8, 0), pady=4)
        self.quota_var = tk.StringVar(value="今日免费额度：— 页")
        ttk.Label(box, textvariable=self.quota_var).grid(
            row=0, column=3, sticky="w", padx=(10, 0), pady=4)

        ttk.Label(box, text="API Key").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        self.key_var = tk.StringVar()
        self.key_entry = self._register(
            ttk.Entry(box, textvariable=self.key_var, show="•"))
        self.key_entry.grid(row=1, column=1, columnspan=2, sticky="we", pady=4)
        self.save_key_btn = self._register(
            ttk.Button(box, text="保存到 .env", command=self._on_save_key))
        self.save_key_btn.grid(row=1, column=3, sticky="e", padx=(10, 0), pady=4)

        ttk.Label(box, text=f".env：{self.env_path}", foreground="#7a7a7a").grid(
            row=2, column=1, columnspan=3, sticky="w", pady=(2, 0))

    def _build_files(self, parent, row: int) -> None:
        box = ttk.LabelFrame(parent, text=" 文件 ", padding=10)
        box.grid(row=row, column=0, sticky="we", pady=(0, 8))
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text="PDF").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        self.pdf_var = tk.StringVar()
        self.pdf_entry = self._register(ttk.Entry(box, textvariable=self.pdf_var))
        self.pdf_entry.grid(row=0, column=1, sticky="we", pady=4)
        self.pdf_btn = self._register(ttk.Button(box, text="浏览…", command=self._browse_pdf))
        self.pdf_btn.grid(row=0, column=2, sticky="e", padx=(8, 0), pady=4)

        ttk.Label(box, text="输出 MD").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        self.out_var = tk.StringVar()
        self.out_entry = self._register(ttk.Entry(box, textvariable=self.out_var))
        self.out_entry.grid(row=1, column=1, sticky="we", pady=4)
        self.out_btn = self._register(ttk.Button(box, text="浏览…", command=self._browse_out))
        self.out_btn.grid(row=1, column=2, sticky="e", padx=(8, 0), pady=4)

        ttk.Label(box, text="分片页数").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
        chunkrow = ttk.Frame(box)
        chunkrow.grid(row=2, column=1, columnspan=2, sticky="w", pady=4)
        self.chunk_var = tk.StringVar(value=str(load_config()["splitter"]["chunk_pages"]))
        self.chunk_spin = self._register(
            ttk.Spinbox(chunkrow, from_=1, to=300, textvariable=self.chunk_var, width=8))
        self.chunk_spin.pack(side="left")
        ttk.Label(chunkrow, text="  页（云端建议 ≤300 页且 ≤200MB，超出会自动切片）",
                  foreground="#7a7a7a").pack(side="left")

        # Remember whether the user typed the output path themselves; only
        # auto-derive it while they haven't.
        self.out_entry.bind("<KeyRelease>", lambda _e: self._mark_out_touched())
        self.pdf_entry.bind("<KeyRelease>", lambda _e: self._sync_out_default())

    def _build_actions(self, parent, row: int) -> None:
        box = ttk.Frame(parent)
        box.grid(row=row, column=0, sticky="we", pady=(0, 8))

        self.convert_btn = self._register(
            ttk.Button(box, text="开始转换", command=self._on_convert))
        self.convert_btn.pack(side="left")
        self.docx_btn = self._register(
            ttk.Button(box, text="转 Word", command=self._on_to_docx))
        self.docx_btn.pack(side="left", padx=(8, 0))
        self.clear_btn = self._register(ttk.Button(box, text="清空日志", command=self._clear_log))
        self.clear_btn.pack(side="left", padx=(8, 0))

        self.root.bind("<Control-Return>", lambda _e: self._on_convert())
        self.root.bind("<F5>", lambda _e: self._on_convert())

    def _build_log(self, parent, row: int) -> None:
        box = ttk.LabelFrame(parent, text=" 日志 ", padding=10)
        box.grid(row=row, column=0, sticky="nsew")
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)

        self.log_text = tk.Text(box, height=14, wrap="word", state="disabled",
                                background="#111418", foreground="#d6d6d6",
                                insertbackground="#d6d6d6", relief="flat",
                                padx=8, pady=6)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(box, orient="vertical", command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)
        for tag, color in LOG_TAG_COLORS.items():
            self.log_text.tag_configure(tag, foreground=color)

    def _build_statusbar(self, parent, row: int) -> None:
        bar = ttk.Frame(parent)
        bar.grid(row=row, column=0, sticky="we")
        bar.columnconfigure(0, weight=1)

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(bar, textvariable=self.status_var).grid(row=0, column=0, sticky="w")
        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=220)
        self.progress.grid(row=0, column=1, sticky="e")

    # ------------------------------------------------------------------
    # log + busy plumbing (main thread only)
    # ------------------------------------------------------------------
    def _log(self, text: str, tag: Optional[str] = None) -> None:
        self.log_text.configure(state="normal")
        if tag:
            self.log_text.insert("end", text.rstrip() + "\n", tag)
        else:
            self.log_text.insert("end", text.rstrip() + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _put(self, kind: str, payload: Any = None, tag: Any = None) -> None:
        """Thread-safe: the only way a worker talks to the UI."""
        self.log_queue.put((kind, payload, tag))

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for widget, normal in self._controls:
            widget.configure(state="disabled" if busy else normal)
        if busy:
            self.progress.start(15)
            self.root.configure(cursor="watch")
        else:
            self.progress.stop()
            self.root.configure(cursor="")
        self._refresh_docx_state()

    def _refresh_docx_state(self) -> None:
        """"转 Word" is pointless until there is a markdown file to convert."""
        if self._busy:
            return
        target = self.out_var.get().strip()
        ready = bool(target) and Path(target).is_file()
        self.docx_btn.configure(state="normal" if ready else "disabled")

    def _pump(self) -> None:
        """Drain the worker queue on the main thread, then reschedule."""
        if not self.log_text.winfo_exists():
            return  # window torn down; stop the loop instead of erroring
        dialog = None
        while True:
            try:
                kind, payload, tag = self.log_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self._log(payload, tag)
            elif kind == "busy":
                self._set_busy(bool(payload))
            elif kind == "status":
                self._set_status(payload)
            elif kind == "quota":
                self.quota_var.set(f"今日免费额度：{payload} 页")
            elif kind == "done":
                title, message = payload
                failed = "失败" in title
                self._log(f"[docflow] {title}：{message}", "err" if failed else "ok")
                self._set_status(title)
                dialog = payload
            elif kind == "confirm":
                title, message = payload
                self._gate_answer = bool(messagebox.askyesno(title, message))
                self._gate_event.set()

        if dialog is not None:
            messagebox.showinfo(dialog[0], dialog[1])
        self.root.after(100, self._pump)

    def _ask_main(self, title: str, message: str) -> bool:
        """Worker-side: ask the user something, via the main thread."""
        self._gate_event.clear()
        self._put("confirm", (title, message))
        if not self._gate_event.wait(timeout=600):
            self._put("log", "[docflow] 等待确认超时，已取消", "err")
            return False
        return self._gate_answer

    def _start(self, target, status: str = "处理中…") -> None:
        self._set_busy(True)
        self._set_status(status)
        threading.Thread(target=target, daemon=True).start()

    # ------------------------------------------------------------------
    # file pickers
    # ------------------------------------------------------------------
    def _browse_pdf(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 PDF", filetypes=[("PDF", "*.pdf"), ("所有文件", "*.*")])
        if path:
            self.pdf_var.set(path)
            self._sync_out_default(force=True)

    def _browse_out(self) -> None:
        path = filedialog.asksaveasfilename(
            title="输出 Markdown", defaultextension=".md", filetypes=[("Markdown", "*.md")])
        if path:
            self._mark_out_touched()
            self.out_var.set(path)

    def _mark_out_touched(self) -> None:
        self._out_touched = True

    def _sync_out_default(self, force: bool = False) -> None:
        """Keep the output path beside the PDF until the user takes over."""
        if self._out_touched and not force:
            return
        pdf = self.pdf_var.get().strip()
        if pdf:
            self._out_touched = False
            self.out_var.set(str(Path(pdf).with_suffix("")) + "_docflow.md")

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------
    def _on_save_key(self) -> None:
        """Write the key to .env and hot-reload it. Local I/O — no thread."""
        backend = self.backend_var.get()
        env_name = ENV_KEY_NAMES.get(backend)
        if env_name is None:
            messagebox.showinfo("docflow", f"{backend} 是本地后端，不需要 API key。")
            return

        key = self.key_var.get().strip()
        if not key:
            messagebox.showwarning("docflow", "请先填写 API key。")
            return

        save_env_value(str(self.env_path), env_name, key)
        load_dotenv(dotenv_path=str(self.env_path), override=True)
        self.key_var.set("")
        self._log(f"[docflow] 已保存 {env_name} → {self.env_path}", "ok")
        messagebox.showinfo("docflow", f"已保存 {env_name}\n到 {self.env_path}")

    def _somark_key(self) -> Optional[str]:
        backend = self.backend_var.get()
        typed = self.key_var.get().strip()
        if backend == "somark" and typed:
            return typed
        return get_key(load_config(), "somark")

    def _on_usage(self) -> None:
        api_key = self._somark_key()
        if not api_key:
            messagebox.showwarning("docflow", "请先填写并保存 SOMARK_API_KEY。")
            return
        self._start(lambda: self._usage_worker(api_key), status="查询额度…")

    def _on_convert(self) -> None:
        if self._busy:
            return
        pdf = self.pdf_var.get().strip()
        if not pdf:
            messagebox.showwarning("docflow", "请先选择 PDF 文件。")
            return
        if not Path(pdf).exists():
            messagebox.showerror("docflow", f"文件不存在：\n{pdf}")
            return

        out = self.out_var.get().strip() or str(Path(pdf).with_suffix("")) + "_docflow.md"
        self.out_var.set(out)

        try:
            chunk = int(self.chunk_var.get())
        except ValueError:
            messagebox.showerror("docflow", "分片页数必须是整数。")
            return
        if chunk < 1:
            messagebox.showerror("docflow", "分片页数必须 ≥ 1。")
            return

        # Read every Tk variable HERE, on the main thread: touching a Tk
        # variable from the worker raises "main thread is not in main loop".
        backend_name = self.backend_var.get()
        api_key = self._somark_key()
        self._start(lambda: self._preflight_worker(backend_name, api_key, pdf, out, chunk),
                    status="转换中…")

    def _on_to_docx(self) -> None:
        md = self.out_var.get().strip() or self.pdf_var.get().strip()
        if md.lower().endswith(".pdf"):
            md = str(Path(md).with_suffix("")) + "_docflow.md"
        if not md or not Path(md).exists():
            messagebox.showwarning("docflow", "找不到要转换的 Markdown 文件，请先执行转换。")
            return

        out = filedialog.asksaveasfilename(
            title="输出 Word", defaultextension=".docx",
            initialfile=Path(md).with_suffix(".docx").name,
            filetypes=[("Word", "*.docx")])
        if not out:
            return
        self._start(lambda: self._docx_worker(md, out), status="生成 Word…")

    # ------------------------------------------------------------------
    # quota gate (worker side; dialogs are proxied to the main thread)
    # ------------------------------------------------------------------
    def _quota_gate(self, backend_name: str, api_key: Optional[str], pdf: str) -> bool:
        """Return True to proceed with the conversion.

        Deliberately never raises and never refuses on its own failure: an
        unreadable page count or a flaky `/usage` must not become a new way
        for the app to block work. Only an explicit "no" from the user stops a
        run.
        """
        if backend_name not in BILLED_BACKENDS:
            return True
        if backend_name != "somark":
            self._put("log", f"[docflow] 提示：{backend_name} 按量计费，且无法预查余额", "info")
            return True

        try:
            needed = page_count(pdf)
        except Exception as e:
            self._put("log", f"[docflow] 读不出页数，跳过额度检查：{e}", "info")
            return True

        try:
            left = free_pages_left(api_key) or 0
        except Exception as e:
            return self._ask_main(
                "额度检查失败",
                f"无法查询 SoMark 免费额度：\n{e}\n\n"
                f"本次约需 {needed} 页，直接转换可能产生费用。\n是否继续？",
            )

        self._put("log", f"[docflow] 免费额度剩余 {left} 页 / 本次需要 {needed} 页")
        if left >= needed:
            return True
        return self._ask_main(
            "免费额度不足",
            f"今日免费额度剩余 {left} 页，本次约需 {needed} 页。\n\n"
            f"继续将消耗付费额度（余额见 SoMark 后台）。是否继续？",
        )

    # ------------------------------------------------------------------
    # workers — never touch Tk directly
    # ------------------------------------------------------------------
    def _preflight_worker(self, backend_name: str, api_key: Optional[str],
                          pdf: str, out_md: str, chunk_pages: int) -> None:
        if not self._quota_gate(backend_name, api_key, pdf):
            self._put("log", "[docflow] 已取消转换")
            self._put("status", "已取消")
            self._put("busy", False)
            return
        self._convert_worker(backend_name, pdf, out_md, chunk_pages)

    def _usage_worker(self, api_key: str) -> None:
        try:
            self._put("log", "[docflow] 查询 /usage …")
            left = free_pages_left(api_key)
            self._put("log", f"[docflow] 免费额度剩余 {left} 页（今日）", "ok")
            self._put("quota", left)
            self._put("done", ("额度查询", f"今日免费额度剩余 {left} 页"))
        except Exception as e:
            self._put("log", f"[docflow] 额度查询失败：{e}", "err")
            self._put("done", ("额度查询失败", str(e)))
        finally:
            self._put("busy", False)

    def _convert_worker(self, backend_name: str, pdf: str, out_md: str, chunk_pages: int) -> None:
        try:
            self._put("log", f"[docflow] 后端={backend_name} 分片={chunk_pages} 开始转换 {pdf}")
            backend = make_backend(backend_name, load_config())
            # Local OCR is minutes per page; without page progress the UI looks
            # frozen for the whole run.
            result = backend.convert(
                pdf,
                chunk_pages=chunk_pages,
                progress=lambda done, total: self._put("status", f"转换中… {done}/{total} 页"),
            )
            export_markdown(result, out_md)
            self._put("log", f"[docflow] 完成 → {out_md}（{len(result)} 字符, {len(result.images)} 图）", "ok")
            self._put("done", ("转换完成", f"已写出\n{out_md}"))
        except Exception as e:
            self._put("log", traceback.format_exc(), "err")
            self._put("done", ("转换失败", f"{type(e).__name__}: {e}"))
        finally:
            self._put("busy", False)

    def _docx_worker(self, md_path: str, out_docx: str) -> None:
        try:
            cfg = load_config()
            images_dir = Path(out_docx).with_suffix("") / cfg["images"]["dir"]
            self._put("log", f"[docflow] 转 Word：{md_path} → {out_docx}")
            md_to_docx(md_path, out_docx,
                       images_dir=str(images_dir),
                       width_inch=float(cfg["images"]["width_inch"]))
            self._put("log", f"[docflow] 完成 → {out_docx}", "ok")
            self._put("done", ("转 Word 完成", f"已写出\n{out_docx}"))
        except Exception as e:
            self._put("log", traceback.format_exc(), "err")
            self._put("done", ("转 Word 失败", f"{type(e).__name__}: {e}"))
        finally:
            self._put("busy", False)


def main(argv=None) -> int:
    _enable_hidpi()
    root = tk.Tk()
    DocflowGUI(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
