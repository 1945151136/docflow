import pytest

from docflow.cli import main


def test_cli_help():
    with pytest.raises(SystemExit) as e:
        main(["--help"])
    assert e.value.code == 0


def test_cli_convert_help():
    with pytest.raises(SystemExit) as e:
        main(["convert", "--help"])
    assert e.value.code == 0


def test_cli_to_docx_help():
    with pytest.raises(SystemExit) as e:
        main(["to-docx", "--help"])
    assert e.value.code == 0
