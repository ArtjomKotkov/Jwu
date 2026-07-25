"""Версия не должна расходиться между pyproject и кодом.

Расхождение уже случалось: пакет подняли до 1.0.0, а ``jwu.__version__`` остался
0.4.8 — и MCP-сервер отчитывался старой версией, хотя исполнял новый код.
"""

import pathlib

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

import jwu


def _pyproject_version() -> str:
    root = pathlib.Path(__file__).resolve().parent.parent
    with (root / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def test_code_version_matches_pyproject():
    assert jwu.__version__ == _pyproject_version()


def test_mcp_reports_code_version_not_metadata():
    """Сервер обязан отчитываться версией КОДА: метаданные при editable-установке врут."""
    from jwu import mcp_server as srv

    assert srv._version() == jwu.__version__
