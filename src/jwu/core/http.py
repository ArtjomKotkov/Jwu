"""HTTP-клиент с повторами: один недоступный сервис не должен ронять синк целиком.

На ноутбуке сеть мигает: SSL handshake timeout к Bitbucket, 503 от Jira на редеплое,
обрыв соединения в вагоне. Раньше первый же такой сбой превращался в ошибку синка
вкладки (а из демона — в пропущенный проход). Здесь два уровня защиты:

- транспорт httpx повторяет ПОДКЛЮЧЕНИЕ (``retries`` — только connect-ошибки);
- ``RetryingClient.send`` повторяет сам запрос при таймауте, обрыве сети и 502/503/504,
  с растущей паузой — но только для GET/HEAD: повтор POST мог бы задвоить комментарий
  или задачу в PR, а сетевая ошибка не говорит, дошёл ли первый запрос.

Тайм-ауты и число попыток настраиваются переменными окружения ``JWU_HTTP_TIMEOUT``
(секунды, по умолчанию 30) и ``JWU_HTTP_RETRIES`` (по умолчанию 2 повтора). Нули —
выключить повторы.
"""

from __future__ import annotations

import os
import time
from typing import Callable, Optional

import httpx

DEFAULT_TIMEOUT = 30.0
DEFAULT_RETRIES = 2
# Первая пауза и множитель: 0.5с, 1.5с, 4.5с…
BACKOFF_BASE = 0.5
BACKOFF_FACTOR = 3.0
RETRY_STATUSES = frozenset({502, 503, 504})
IDEMPOTENT = frozenset({"GET", "HEAD", "OPTIONS"})


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def default_timeout() -> float:
    return _env_float("JWU_HTTP_TIMEOUT", DEFAULT_TIMEOUT)


def default_retries() -> int:
    return max(0, int(_env_float("JWU_HTTP_RETRIES", DEFAULT_RETRIES)))


class RetryingClient(httpx.Client):
    """httpx.Client, который повторяет идемпотентные запросы при сетевых сбоях и 5xx."""

    def __init__(self, *args, retries: Optional[int] = None,
                 sleep: Callable[[float], None] = time.sleep, **kwargs) -> None:
        self._retries = default_retries() if retries is None else max(0, int(retries))
        self._sleep = sleep
        kwargs.setdefault("transport", httpx.HTTPTransport(retries=self._retries))
        super().__init__(*args, **kwargs)

    def send(self, request: httpx.Request, **kwargs) -> httpx.Response:  # type: ignore[override]
        attempts = self._retries + 1 if request.method.upper() in IDEMPOTENT else 1
        delay = BACKOFF_BASE
        last_exc: Optional[Exception] = None
        for attempt in range(attempts):
            try:
                resp = super().send(request, **kwargs)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                last_exc = exc
                resp = None
            if resp is not None and resp.status_code not in RETRY_STATUSES:
                return resp
            if attempt == attempts - 1:
                if resp is not None:
                    return resp  # последний 5xx отдаём как есть — вызывающий сформулирует ошибку
                raise last_exc  # type: ignore[misc]
            if resp is not None:
                resp.close()
            self._sleep(delay)
            delay *= BACKOFF_FACTOR
        raise AssertionError("unreachable")  # pragma: no cover


def new_client(**kwargs) -> httpx.Client:
    """Клиент для API-обёрток jwu: общий тайм-аут из окружения и повторы (см. модуль)."""
    kwargs.setdefault("timeout", default_timeout())
    return RetryingClient(**kwargs)
