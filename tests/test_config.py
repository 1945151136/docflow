"""`load_config` must never hand out (or leak into) shared module state."""
from docflow.core.config import DEFAULTS, load_config


def test_two_loads_do_not_share_state():
    a = load_config()
    a["splitter"]["chunk_pages"] = 999
    a["keys"]["somark"] = "leaked-key"

    b = load_config()

    assert b["splitter"]["chunk_pages"] == 150, "second load saw the first load's mutation"
    assert b["keys"]["somark"] != "leaked-key"


def test_nested_sections_are_independent():
    a = load_config()
    a["api"]["qps"] = 12345

    b = load_config()

    assert b["api"]["qps"] == 4


def test_defaults_constant_is_not_polluted():
    before = DEFAULTS["splitter"]["chunk_pages"]

    load_config()
    load_config()

    assert "keys" not in DEFAULTS, "load_config leaked its `keys` section into DEFAULTS"
    assert DEFAULTS["splitter"]["chunk_pages"] == before


def test_keys_section_is_populated():
    cfg = load_config()
    assert set(cfg["keys"]) == {"somark", "mistral", "firecrawl"}
