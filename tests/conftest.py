"""Общая изоляция тестов от окружения пользователя.

Команды jwu ходят в БД по глобальному пути (``JWU_DB_PATH`` → config.toml → ~/.local/share),
а с переездом кредов в БД туда пишет и ``jwu configure``. Поэтому КАЖДЫЙ тест получает
свою временную БД и свой конфиг: иначе тестовый прогон способен затереть рабочие настройки
и секреты пользователя. Проверено на практике — так и произошло, пока фикстуры не было.
"""

from __future__ import annotations

import os

import pytest

# Метрики инструментов (core.toolstats) в тестах выключены на весь процесс: буфер сбрасывается
# в БД по таймеру и при выходе — уже без подмены JWU_DB_PATH, то есть в настоящую БД.
os.environ["JWU_TOOLSTATS"] = "0"


@pytest.fixture(autouse=True)
def isolate_jwu_env(tmp_path, monkeypatch):
    monkeypatch.setenv("JWU_DB_PATH", str(tmp_path / "autouse-state.db"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    # выбор воркспейса не должен зависеть от того, что выставлено в оболочке разработчика
    monkeypatch.delenv("JWU_WORKSPACE", raising=False)
    for var in ("JIRA_TOKEN", "SUPPORT_TOKEN", "BITBUCKET_TOKEN", "JENKINS_TOKEN", "GITHUB_TOKEN",
                "TELEGRAM_BOT_TOKEN", "JIRA_PASSWORD", "JIRA_GATE_PASSWORD", "SUPPORT_PASSWORD",
                "SUPPORT_GATE_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    for var in list(__import__("os").environ):
        if var.startswith("JWU_") and var not in ("JWU_DB_PATH", "JWU_TOOLSTATS"):
            monkeypatch.delenv(var, raising=False)
