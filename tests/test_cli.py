import pytest

from harness import __version__
from harness.cli import main


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"corpus-harness {__version__}"


def test_help_uses_standalone_product_language(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "provenance-rich Markdown" in help_text
    assert "corpus-harness" in help_text
