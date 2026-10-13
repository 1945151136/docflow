"""`--config` wiring: config.yaml must actually reach the backends.

Until now `main()` called `load_config()` with no path, so config.yaml was
never read and every value silently fell back to DEFAULTS.
"""
from docflow.backends.api.somark import SoMarkBackend
from docflow.cli import main as cli_main
from docflow.core.base import ConversionResult


def _capture_convert(monkeypatch):
    """Patch the somark backend's convert() so no network is touched."""
    seen = {}

    def fake_convert(self, src, **opts):
        seen["src"] = src
        seen.update(opts)
        return ConversionResult(markdown="# ok\n")

    monkeypatch.setattr(SoMarkBackend, "convert", fake_convert)
    return seen


# --------------------------------------------------------------------------
# explicit --config wins
# --------------------------------------------------------------------------
def test_config_flag_overrides_chunk_pages(tmp_path, monkeypatch):
    seen = _capture_convert(monkeypatch)
    cfg = tmp_path / "custom.yaml"
    cfg.write_text("splitter:\n  chunk_pages: 42\n", encoding="utf-8")
    out = tmp_path / "out.md"

    rc = cli_main(["--config", str(cfg), "convert", "book.pdf",
                   "--backend", "somark", "-o", str(out)])

    assert rc == 0
    assert seen["chunk_pages"] == 42


def test_config_flag_also_accepted_after_subcommand(tmp_path, monkeypatch):
    seen = _capture_convert(monkeypatch)
    cfg = tmp_path / "custom.yaml"
    cfg.write_text("splitter:\n  chunk_pages: 99\n", encoding="utf-8")
    out = tmp_path / "out.md"

    rc = cli_main(["convert", "book.pdf", "--config", str(cfg),
                   "--backend", "somark", "-o", str(out)])

    assert rc == 0
    assert seen["chunk_pages"] == 99


def test_explicit_config_wins_over_cwd_config(tmp_path, monkeypatch):
    seen = _capture_convert(monkeypatch)
    (tmp_path / "config.yaml").write_text("splitter:\n  chunk_pages: 7\n", encoding="utf-8")
    explicit = tmp_path / "explicit.yaml"
    explicit.write_text("splitter:\n  chunk_pages: 123\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "out.md"

    rc = cli_main(["--config", str(explicit), "convert", "book.pdf",
                   "--backend", "somark", "-o", str(out)])

    assert rc == 0
    assert seen["chunk_pages"] == 123, "--config must take precedence over ./config.yaml"


# --------------------------------------------------------------------------
# default: ./config.yaml if it exists
# --------------------------------------------------------------------------
def test_cwd_config_yaml_is_picked_up_by_default(tmp_path, monkeypatch):
    seen = _capture_convert(monkeypatch)
    (tmp_path / "config.yaml").write_text("splitter:\n  chunk_pages: 7\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "out.md"

    rc = cli_main(["convert", "book.pdf", "--backend", "somark", "-o", str(out)])

    assert rc == 0
    assert seen["chunk_pages"] == 7, "config.yaml in CWD was not loaded"


def test_no_config_yaml_falls_back_to_defaults(tmp_path, monkeypatch):
    seen = _capture_convert(monkeypatch)
    monkeypatch.chdir(tmp_path)  # deliberately no config.yaml here
    out = tmp_path / "out.md"

    rc = cli_main(["convert", "book.pdf", "--backend", "somark", "-o", str(out)])

    assert rc == 0
    assert seen["chunk_pages"] == 150


# --------------------------------------------------------------------------
# the whole file is honoured, not just splitter.chunk_pages
# --------------------------------------------------------------------------
def test_other_config_sections_reach_the_backend(tmp_path, monkeypatch):
    captured = {}

    class _Stub:
        def __init__(self, **kw):
            captured.update(kw)

        def convert(self, src, **opts):
            return ConversionResult(markdown="# ok\n")

    monkeypatch.setattr("docflow.cli.SoMarkBackend", _Stub)

    cfg = tmp_path / "custom.yaml"
    cfg.write_text(
        "splitter:\n  chunk_pages: 33\n"
        "api:\n  qps: 9\n  poll_interval: 2\n  timeout: 11\n",
        encoding="utf-8",
    )
    out = tmp_path / "out.md"

    rc = cli_main(["--config", str(cfg), "convert", "book.pdf",
                   "--backend", "somark", "-o", str(out)])

    assert rc == 0
    assert captured["qps"] == 9
    assert captured["poll_interval"] == 2
    assert captured["timeout"] == 11
