"""GUI plumbing tests.

These exercise the parts of `docflow.gui` that carry risk — worker/queue
wiring, the button-lockout, `.env` saving, backend dispatch — without ever
opening a real window (the root is withdrawn) or touching the network
(backends and message boxes are faked).

Skipped automatically where Tkinter has no display.
"""
from __future__ import annotations

import threading
import time

import pytest

tk = pytest.importorskip("tkinter")

from docflow import gui as gui_mod  # noqa: E402
from docflow.core.base import ConversionResult  # noqa: E402


# --------------------------------------------------------------------------
# fixtures / fakes
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def root():
    """One Tk interpreter for the whole module.

    Creating and destroying a root per test makes Tcl re-run its `init.tcl`
    lookup dozens of times, which intermittently fails ("Can't find a usable
    init.tcl"). One long-lived root avoids that entirely.
    """
    try:
        r = tk.Tk()
    except tk.TclError as e:  # headless CI
        pytest.skip(f"no Tk display: {e}")
    r.withdraw()  # never actually show a window mid-test-suite
    try:
        yield r
    finally:
        r.destroy()


class _FakeBox:
    """Stand-in for tkinter.messagebox: records calls instead of blocking.

    `yes` is what every dialog returns, so a test can drive both answers to
    the quota-gate confirmation.
    """

    def __init__(self, yes=True):
        self.calls = []
        self.yes = yes

    def __getattr__(self, name):
        def recorder(*args, **kwargs):
            self.calls.append((name, args))
            return self.yes
        return recorder

    def named(self, name):
        return [a for n, a in self.calls if n == name]


class _FakeBackend:
    def __init__(self, result=None, error=None, record=None):
        self._result = result
        self._error = error
        self._record = record if record is not None else []

    def convert(self, src, **opts):
        self._record.append((src, opts))
        if self._error:
            raise self._error
        return self._result


@pytest.fixture
def ui(root, tmp_path, monkeypatch):
    """A GUI instance with dialogs faked and a throwaway .env path."""
    for child in root.winfo_children():  # fresh widget tree per test
        child.destroy()

    box = _FakeBox()
    monkeypatch.setattr(gui_mod, "messagebox", box)
    instance = gui_mod.DocflowGUI(root, env_path=str(tmp_path / ".env"))
    instance.box = box
    instance.tmp = tmp_path
    return instance


def _drain(instance):
    """Apply everything the workers have queued, the way the app's loop does."""
    instance._pump()


def _pump_until(instance, predicate, timeout=10.0):
    """Spin the UI loop until `predicate()` holds — i.e. until a worker reports."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        instance._pump()
        if predicate():
            return True
        time.sleep(0.02)
    return False


# --------------------------------------------------------------------------
# quota helper — read-only, must never bill
# --------------------------------------------------------------------------
def test_free_pages_left_reads_known_field(monkeypatch):
    class FakeSoMark:
        def __init__(self, api_key=None, **kw):
            self.api_key = api_key

        def usage(self):
            return {"code": 0, "data": {"remaining_free_pages_today": 500}}

    monkeypatch.setattr(gui_mod, "SoMarkBackend", FakeSoMark)
    assert gui_mod.free_pages_left("k") == 500


def test_free_pages_left_falls_back_to_alternate_field_names(monkeypatch):
    class FakeSoMark:
        def __init__(self, api_key=None, **kw):
            pass

        def usage(self):
            return {"data": {"free_pages_left": 26}}

    monkeypatch.setattr(gui_mod, "SoMarkBackend", FakeSoMark)
    assert gui_mod.free_pages_left("k") == 26


def test_free_pages_left_is_zero_when_payload_is_unexpected(monkeypatch):
    class FakeSoMark:
        def __init__(self, api_key=None, **kw):
            pass

        def usage(self):
            return {"code": 1, "message": "bad key"}

    monkeypatch.setattr(gui_mod, "SoMarkBackend", FakeSoMark)
    assert gui_mod.free_pages_left("k") == 0, "unexpected payload must not crash the UI"


# --------------------------------------------------------------------------
# backend dispatch — only public constructors
# --------------------------------------------------------------------------
def test_make_backend_dispatches_to_public_constructors():
    cfg = {
        "api": {"qps": 7, "poll_interval": 3, "timeout": 11},
        "keys": {"somark": "sk-s", "mistral": "sk-m", "firecrawl": "sk-f"},
    }

    somark = gui_mod.make_backend("somark", cfg)
    assert somark.api_key == "sk-s"
    assert somark.qps == 7 and somark.poll_interval == 3 and somark.timeout == 11

    mistral = gui_mod.make_backend("mistral", cfg)
    assert mistral.api_key == "sk-m" and mistral.qps == 7

    firecrawl = gui_mod.make_backend("firecrawl", cfg)
    assert firecrawl.api_key == "sk-f"

    assert gui_mod.make_backend("paddleocr", cfg).name == "paddleocr"


# --------------------------------------------------------------------------
# .env path resolution — the GUI must edit the file docflow actually reads
# --------------------------------------------------------------------------
def test_default_env_path_uses_the_dotenv_that_load_config_would_find(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("SOMARK_API_KEY=x\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert gui_mod._default_env_path() == tmp_path / ".env"


def test_default_env_path_prefers_an_existing_env_in_a_parent_dir(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("SOMARK_API_KEY=x\n", encoding="utf-8")
    child = tmp_path / "run" / "from" / "here"
    child.mkdir(parents=True)
    monkeypatch.chdir(child)

    assert gui_mod._default_env_path() == tmp_path / ".env", \
        "launching from a subdirectory must still find the project .env"


def test_default_env_path_falls_back_to_cwd_when_there_is_none(tmp_path, monkeypatch):
    empty = tmp_path / "no-env-anywhere"
    empty.mkdir()
    monkeypatch.chdir(empty)

    assert gui_mod._default_env_path() == empty / ".env"


def test_explicit_env_path_wins(root, tmp_path):
    explicit = tmp_path / "custom.env"

    instance = gui_mod.DocflowGUI(root, env_path=str(explicit))

    assert instance.env_path == explicit


# --------------------------------------------------------------------------
# .env saving
# --------------------------------------------------------------------------
def test_save_key_writes_env_and_clears_the_entry(ui, monkeypatch):
    seen = {}
    monkeypatch.setattr(gui_mod, "load_dotenv",
                        lambda *a, **k: seen.update(k) or True)

    ui.backend_var.set("somark")
    ui.key_var.set("sk-typed-by-user")

    ui._on_save_key()

    text = (ui.tmp / ".env").read_text(encoding="utf-8")
    assert "SOMARK_API_KEY=sk-typed-by-user" in text
    assert ui.key_var.get() == "", "the key must not linger in the entry widget"
    assert seen.get("override") is True, "dotenv must be re-loaded with override=True"
    assert seen.get("dotenv_path") == str(ui.tmp / ".env")
    assert ui.box.named("showinfo")


def test_save_key_replaces_an_existing_line(ui, monkeypatch):
    monkeypatch.setattr(gui_mod, "load_dotenv", lambda *a, **k: True)
    env = ui.tmp / ".env"
    env.write_text("SOMARK_API_KEY=old\nKEEP=1\n", encoding="utf-8")

    ui.backend_var.set("somark")
    ui.key_var.set("sk-new")
    ui._on_save_key()

    text = env.read_text(encoding="utf-8")
    assert "SOMARK_API_KEY=sk-new" in text
    assert "old" not in text
    assert "KEEP=1" in text


def test_save_key_refuses_an_empty_key(ui):
    ui.backend_var.set("somark")
    ui.key_var.set("   ")

    ui._on_save_key()

    assert not (ui.tmp / ".env").exists()
    assert ui.box.named("showwarning")


def test_save_key_is_a_noop_for_local_backends(ui):
    ui.backend_var.set("paddleocr")
    ui.key_var.set("sk-irrelevant")

    ui._on_save_key()

    assert not (ui.tmp / ".env").exists(), "a local backend must not write a key"
    assert ui.box.named("showinfo")


# --------------------------------------------------------------------------
# worker thread + queue + after()
# --------------------------------------------------------------------------
def test_busy_locks_and_restores_every_control(ui):
    assert str(ui.convert_btn["state"]) == "normal"

    ui._set_busy(True)
    assert str(ui.convert_btn["state"]) == "disabled"
    assert str(ui.key_entry["state"]) == "disabled"
    assert str(ui.backend_box["state"]) == "disabled"

    ui._set_busy(False)
    assert str(ui.convert_btn["state"]) == "normal"
    assert str(ui.backend_box["state"]) == "readonly", "combobox must return to readonly"


def test_pump_moves_queued_log_lines_into_the_widget(ui):
    ui._put("log", "hello from a worker")
    ui._put("busy", True)

    _drain(ui)

    assert "hello from a worker" in ui.log_text.get("1.0", "end")
    assert str(ui.convert_btn["state"]) == "disabled"


def test_pump_shows_a_dialog_for_done(ui):
    ui._put("done", ("转换完成", "已写出 x.md"))

    _drain(ui)

    assert ui.box.named("showinfo")
    assert "转换完成" in ui.log_text.get("1.0", "end")


def test_pump_reschedules_itself_via_after(ui):
    # The polling loop only works if every pass re-arms itself.
    assert ui.root.tk.call("after", "info"), "no pending after() callback"
    ui._pump()
    assert ui.root.tk.call("after", "info")


def test_workers_only_talk_to_the_ui_through_the_queue(ui, monkeypatch):
    """A worker must not touch Tk — it runs off the main thread."""
    touched = []
    monkeypatch.setattr(ui, "_log", lambda *a, **k: touched.append("log"))
    monkeypatch.setattr(ui, "_set_busy", lambda *a, **k: touched.append("busy"))
    monkeypatch.setattr(gui_mod.messagebox, "showinfo",
                        lambda *a, **k: touched.append("dialog"))
    # Stub the backend: the point here is the worker's plumbing, not any real
    # engine. Letting it build a real PaddleOCRBackend made this test take 45s
    # once paddleocr happened to be installed, and pass instantly when it
    # wasn't — a test whose meaning depends on the machine.
    monkeypatch.setattr(gui_mod, "make_backend",
                        lambda name, cfg: _FakeBackend(error=RuntimeError("boom")))

    ui._convert_worker("paddleocr", "missing.pdf", "out.md", 150)

    assert touched == [], "the worker called into Tk directly"
    kinds = []
    while not ui.log_queue.empty():
        kinds.append(ui.log_queue.get_nowait()[0])
    assert "busy" in kinds and "done" in kinds


def test_convert_runs_on_a_worker_thread_then_releases_the_ui(ui, monkeypatch):
    pdf = ui.tmp / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    out = ui.tmp / "book.md"

    record = []
    backend = _FakeBackend(result=ConversionResult(markdown="# 标题\n正文\n"), record=record)
    monkeypatch.setattr(gui_mod, "make_backend", lambda name, cfg: backend)

    ui.pdf_var.set(str(pdf))
    ui.out_var.set(str(out))
    ui.chunk_var.set("150")

    ui._on_convert()

    # Nothing re-enables the UI until the main thread pumps the queue.
    assert str(ui.convert_btn["state"]) == "disabled"

    # Pump like the app's loop does until the worker reports back.
    assert _pump_until(ui, lambda: bool(ui.box.named("showinfo"))), "worker never reported"
    _drain(ui)

    assert out.exists(), "the worker never wrote the markdown"
    assert out.read_text(encoding="utf-8") == "# 标题\n正文\n"
    assert record and record[0][1]["chunk_pages"] == 150
    assert str(ui.convert_btn["state"]) == "normal", "buttons must come back after the run"
    assert ui.box.named("showinfo")
    assert "转换完成" in ui.log_text.get("1.0", "end")


def test_convert_failure_logs_traceback_and_unlocks(ui, monkeypatch):
    pdf = ui.tmp / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    backend = _FakeBackend(error=RuntimeError("quota exhausted"))
    monkeypatch.setattr(gui_mod, "make_backend", lambda name, cfg: backend)

    ui.pdf_var.set(str(pdf))
    ui.out_var.set(str(ui.tmp / "book.md"))
    ui._on_convert()
    assert _pump_until(ui, lambda: bool(ui.box.named("showinfo"))), "worker never reported"
    _drain(ui)

    text = ui.log_text.get("1.0", "end")
    assert "RuntimeError" in text and "quota exhausted" in text
    assert "Traceback" in text, "the failure must be diagnosable from the log"
    assert str(ui.convert_btn["state"]) == "normal"
    assert ui.box.named("showinfo")


def test_convert_refuses_a_missing_file_without_starting_a_worker(ui):
    started = []
    ui._start = lambda target, **kw: started.append(target)

    ui.pdf_var.set(str(ui.tmp / "nope.pdf"))
    ui._on_convert()

    assert started == []
    assert ui.box.named("showerror")


def test_convert_rejects_a_non_numeric_chunk_size(ui):
    started = []
    ui._start = lambda target, **kw: started.append(target)
    pdf = ui.tmp / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    ui.pdf_var.set(str(pdf))
    ui.chunk_var.set("many")
    ui._on_convert()

    assert started == []
    assert ui.box.named("showerror")


def test_convert_defaults_the_output_path_next_to_the_pdf(ui, monkeypatch):
    pdf = ui.tmp / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    captured = {}
    monkeypatch.setattr(ui, "_start", lambda target, **kw: captured.update(target=target))

    ui.pdf_var.set(str(pdf))
    ui.out_var.set("")
    ui.chunk_var.set("150")
    ui._on_convert()

    assert ui.out_var.get() == str(ui.tmp / "book_docflow.md")


def test_usage_uses_the_somark_key_and_never_converts(ui, monkeypatch):
    calls = []
    monkeypatch.setattr(gui_mod, "free_pages_left", lambda k: calls.append(k) or 500)
    monkeypatch.setattr(gui_mod, "load_config",
                        lambda *a, **k: {"keys": {"somark": "sk-env"}})
    monkeypatch.setattr(gui_mod, "make_backend",
                        lambda *a, **k: pytest.fail("查额度不得构造转换后端"))
    # run the worker inline so the assertion below is deterministic
    ui._start = lambda target, **kw: target()
    ui.backend_var.set("somark")
    ui.key_var.set("")

    ui._on_usage()
    _drain(ui)

    assert calls == ["sk-env"]
    assert "500" in ui.log_text.get("1.0", "end")
    assert ui.box.named("showinfo")


def test_usage_warns_when_no_key_is_available(ui, monkeypatch):
    monkeypatch.setattr(gui_mod, "load_config", lambda *a, **k: {"keys": {"somark": None}})
    monkeypatch.setattr(gui_mod, "free_pages_left",
                        lambda k: pytest.fail("must not query without a key"))
    ui.backend_var.set("paddleocr")
    ui.key_var.set("")

    ui._on_usage()

    assert ui.box.named("showwarning")


def test_docx_button_warns_when_there_is_no_markdown(ui):
    started = []
    ui._start = lambda target, **kw: started.append(target)

    ui.out_var.set(str(ui.tmp / "missing.md"))
    ui.pdf_var.set("")
    ui._on_to_docx()

    assert started == []
    assert ui.box.named("showwarning")


# --------------------------------------------------------------------------
# quota gate — the guard that stops an accidental paid submit
# --------------------------------------------------------------------------
def test_gate_does_not_ask_for_a_local_backend(ui, monkeypatch):
    monkeypatch.setattr(gui_mod, "free_pages_left",
                        lambda k: pytest.fail("本地后端不该查额度"))

    assert ui._quota_gate("paddleocr", None, "whatever.pdf") is True
    assert ui.box.calls == [], "no dialog should appear for a free backend"


def test_gate_proceeds_silently_when_free_quota_covers_the_file(ui, monkeypatch):
    monkeypatch.setattr(gui_mod, "page_count", lambda p: 137)
    monkeypatch.setattr(gui_mod, "free_pages_left", lambda k: 500)

    assert ui._quota_gate("somark", "sk", "book.pdf") is True
    assert ui.box.named("askyesno") == [], "ample quota must not nag the user"
    _drain(ui)
    assert "500" in ui.log_text.get("1.0", "end") and "137" in ui.log_text.get("1.0", "end")


def test_gate_asks_and_aborts_when_quota_is_short(ui, monkeypatch):
    monkeypatch.setattr(gui_mod, "page_count", lambda p: 474)
    monkeypatch.setattr(gui_mod, "free_pages_left", lambda k: 363)
    answered = []
    monkeypatch.setattr(ui, "_ask_main",
                        lambda title, msg: (answered.append((title, msg)), False)[1])

    assert ui._quota_gate("somark", "sk", "book.pdf") is False
    assert answered and "免费额度不足" in answered[0][0]
    assert "363" in answered[0][1] and "474" in answered[0][1]


def test_gate_proceeds_when_the_user_confirms(ui, monkeypatch):
    monkeypatch.setattr(gui_mod, "page_count", lambda p: 474)
    monkeypatch.setattr(gui_mod, "free_pages_left", lambda k: 363)
    monkeypatch.setattr(ui, "_ask_main", lambda title, msg: True)

    assert ui._quota_gate("somark", "sk", "book.pdf") is True


def test_gate_survives_an_unreadable_page_count(ui, monkeypatch):
    monkeypatch.setattr(gui_mod, "page_count",
                        lambda p: (_ for _ in ()).throw(RuntimeError("corrupt pdf")))
    monkeypatch.setattr(gui_mod, "free_pages_left",
                        lambda k: pytest.fail("页数都读不出来，不该去查额度"))

    assert ui._quota_gate("somark", "sk", "book.pdf") is True, \
        "a broken page count must never block a conversion"


def test_gate_asks_instead_of_guessing_when_usage_fails(ui, monkeypatch):
    monkeypatch.setattr(gui_mod, "page_count", lambda p: 10)

    def boom(_k):
        raise RuntimeError("network down")

    monkeypatch.setattr(gui_mod, "free_pages_left", boom)
    asked = []
    monkeypatch.setattr(ui, "_ask_main",
                        lambda title, msg: (asked.append((title, msg)), False)[1])

    assert ui._quota_gate("somark", "sk", "book.pdf") is False
    assert asked and "额度检查失败" in asked[0][0]


def test_gate_warns_but_proceeds_for_other_billed_backends(ui, monkeypatch):
    monkeypatch.setattr(gui_mod, "free_pages_left",
                        lambda k: pytest.fail("只有 somark 有可查的余额"))

    assert ui._quota_gate("mistral", "sk", "book.pdf") is True


def test_aborting_the_gate_never_starts_a_conversion(ui, monkeypatch):
    converted = []
    monkeypatch.setattr(ui, "_convert_worker", lambda *a: converted.append(a))
    monkeypatch.setattr(ui, "_quota_gate", lambda *a: False)

    ui._preflight_worker("somark", "sk", "book.pdf", "out.md", 150)
    _drain(ui)

    assert converted == []
    assert str(ui.convert_btn["state"]) == "normal", "the UI must be released"
    assert "已取消" in ui.log_text.get("1.0", "end")


def test_gate_dialog_round_trips_through_the_main_thread(ui, monkeypatch):
    """The worker asks; only the main thread may touch the dialog."""
    monkeypatch.setattr(gui_mod, "page_count", lambda p: 474)
    monkeypatch.setattr(gui_mod, "free_pages_left", lambda k: 363)
    ui.box.yes = False

    answer = {}
    thread = threading.Thread(
        target=lambda: answer.update(result=ui._quota_gate("somark", "sk", "book.pdf")),
        daemon=True)
    thread.start()

    deadline = time.time() + 5
    while time.time() < deadline and thread.is_alive():
        ui._pump()
        time.sleep(0.02)

    assert answer.get("result") is False
    assert ui.box.named("askyesno"), "the dialog must be shown by the main thread"


# --------------------------------------------------------------------------
# UI details that were worth fixing
# --------------------------------------------------------------------------
def test_log_lines_carry_a_colour_tag(ui):
    ui._put("log", "all good", "ok")
    ui._put("log", "went wrong", "err")
    _drain(ui)

    text = ui.log_text
    assert text.tag_ranges("ok"), "success lines should be tagged"
    assert text.tag_ranges("err"), "error lines should be tagged"


def test_clear_log_empties_the_pane(ui):
    ui._log("something")
    assert "something" in ui.log_text.get("1.0", "end")

    ui._clear_log()

    assert ui.log_text.get("1.0", "end").strip() == ""


def test_docx_button_is_disabled_until_a_markdown_exists(ui):
    assert str(ui.docx_btn["state"]) == "disabled", "nothing to export yet"

    md = ui.tmp / "book.md"
    md.write_text("# hi\n", encoding="utf-8")
    ui.out_var.set(str(md))
    ui._refresh_docx_state()

    assert str(ui.docx_btn["state"]) == "normal"


def test_output_path_follows_the_pdf_until_the_user_edits_it(ui):
    ui.pdf_var.set(str(ui.tmp / "a.pdf"))
    ui._sync_out_default()
    assert ui.out_var.get() == str(ui.tmp / "a_docflow.md")

    ui.pdf_var.set(str(ui.tmp / "b.pdf"))
    ui._sync_out_default()
    assert ui.out_var.get() == str(ui.tmp / "b_docflow.md"), "should track the new pdf"

    ui._mark_out_touched()          # the user typed their own path
    ui.pdf_var.set(str(ui.tmp / "c.pdf"))
    ui._sync_out_default()
    assert ui.out_var.get() == str(ui.tmp / "b_docflow.md"), \
        "a hand-edited output path must not be overwritten"


def test_status_bar_tracks_the_run(ui):
    ui._put("status", "转换中…")
    _drain(ui)
    assert ui.status_var.get() == "转换中…"

    ui._put("done", ("转换完成", "已写出 x.md"))
    _drain(ui)
    assert ui.status_var.get() == "转换完成"


def test_usage_worker_publishes_the_quota_to_the_header(ui, monkeypatch):
    monkeypatch.setattr(gui_mod, "free_pages_left", lambda k: 363)

    ui._usage_worker("sk")
    _drain(ui)

    assert ui.quota_var.get() == "今日免费额度：363 页"
