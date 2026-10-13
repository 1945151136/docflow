"""`.env` rewriting: replace in place, append if missing, never disturb the rest."""
from docflow.core.envfile import render_env, save_env_value, save_env_values


# --------------------------------------------------------------------------
# render_env  (pure: text in -> text out)
# --------------------------------------------------------------------------
def test_render_replaces_existing_key_in_place():
    original = "# my keys\nSOMARK_API_KEY=old\nOTHER=1\n"

    out = render_env(original, {"SOMARK_API_KEY": "new"})

    assert "SOMARK_API_KEY=new" in out
    assert "old" not in out
    assert "# my keys" in out, "comments must survive"
    assert "OTHER=1" in out, "unrelated keys must survive"
    assert out.count("SOMARK_API_KEY") == 1, "key must not be duplicated"


def test_render_appends_missing_key():
    out = render_env("A=1\n", {"NEW_KEY": "v"})

    assert "NEW_KEY=v" in out
    assert out.count("NEW_KEY") == 1


def test_render_empty_file():
    assert render_env("", {"K": "v"}) == "K=v\n"


def test_render_preserves_order_and_blank_lines():
    original = "X=1\n\n# note\nY=2\n"

    out = render_env(original, {"Y": "9"})

    assert out.splitlines() == ["X=1", "", "# note", "Y=9"]


def test_render_is_pure():
    original = "A=1\n"
    render_env(original, {"A": "2"})
    assert original == "A=1\n", "render_env mutated its input"


def test_render_does_not_match_key_as_substring():
    # Updating SOMARK_API_KEY must not clobber LOOKALIKE_SOMARK_API_KEY.
    out = render_env("MYSOMARK_API_KEY=keep\n", {"SOMARK_API_KEY": "new"})

    assert "MYSOMARK_API_KEY=keep" in out
    assert "\nSOMARK_API_KEY=new" in out


def test_render_handles_export_prefix():
    out = render_env("export SOMARK_API_KEY=old\n", {"SOMARK_API_KEY": "new"})
    assert out == "export SOMARK_API_KEY=new\n", "the `export ` prefix must be preserved"


def test_render_handles_spaces_around_equals():
    out = render_env("SOMARK_API_KEY  =  old\n", {"SOMARK_API_KEY": "new"})
    assert out.count("SOMARK_API_KEY") == 1
    assert "SOMARK_API_KEY=new" in out


def test_render_multiple_updates_at_once():
    out = render_env("A=1\nB=2\n", {"A": "9", "C": "3"})
    assert "A=9" in out
    assert "B=2" in out
    assert "C=3" in out


# --------------------------------------------------------------------------
# save_env_value  (thin I/O wrapper)
# --------------------------------------------------------------------------
def test_save_env_value_replaces_and_keeps_others(tmp_path):
    p = tmp_path / ".env"
    p.write_text("SOMARK_API_KEY=old\nKEEP_ME=1\n", encoding="utf-8")

    save_env_value(str(p), "SOMARK_API_KEY", "sk-new")

    text = p.read_text(encoding="utf-8")
    assert "SOMARK_API_KEY=sk-new" in text
    assert "KEEP_ME=1" in text
    assert "old" not in text


def test_save_env_value_creates_missing_file(tmp_path):
    p = tmp_path / "nested" / ".env"

    save_env_value(str(p), "K", "v")

    assert p.read_text(encoding="utf-8") == "K=v\n"


def test_save_env_values_writes_several_keys(tmp_path):
    p = tmp_path / ".env"
    save_env_values(str(p), {"A": "1", "B": "2"})
    text = p.read_text(encoding="utf-8")
    assert "A=1" in text and "B=2" in text


def test_saved_env_is_reloadable(tmp_path):
    """Round-trip: what save_env_value writes must parse back via dotenv."""
    p = tmp_path / ".env"
    save_env_value(str(p), "DOCFLOW_TEST_KEY", "hello-world")

    from dotenv import dotenv_values

    assert dotenv_values(str(p))["DOCFLOW_TEST_KEY"] == "hello-world"
