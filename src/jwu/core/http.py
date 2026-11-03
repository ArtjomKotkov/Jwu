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

import contextvars
import os
import time
from contextlib import contextmanager
from typing import Callable, Iterator, Optional

import httpx

DEFAULT_TIMEOUT = 30.0
# Установка соединения — отдельно и коротко: недоступный хост не должен держать минуты
# (30 с × повторы). Ответ сервера по-прежнему ждём DEFAULT_TIMEOUT.
DEFAULT_CONNECT_TIMEOUT = 5.0
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


def default_connect_timeout() -> float:
    return _env_float("JWU_HTTP_CONNECT_TIMEOUT", DEFAULT_CONNECT_TIMEOUT)


def make_timeout(total: Optional[float] = None) -> httpx.Timeout:
    """Таймаут jwu: соединение — коротко (JWU_HTTP_CONNECT_TIMEOUT, 5 с), остальное — total."""
    total = default_timeout() if total is None else float(total)
    return httpx.Timeout(total, connect=min(total, default_connect_timeout()))


def default_retries() -> int:
    return max(0, int(_env_float("JWU_HTTP_RETRIES", DEFAULT_RETRIES)))


class DeadlineExceeded(RuntimeError):
    """Общий бюджет времени на операцию кончился (см. :func:`deadline`)."""


_DEADLINE: contextvars.ContextVar = contextvars.ContextVar("jwu_http_deadline", default=None)


@contextmanager
def deadline(seconds: float, what: str) -> Iterator[None]:
    """Общий бюджет на все запросы внутри блока: таймаут на запрос, повторы и число запросов
    больше не складываются в минуты. Кончился — :class:`DeadlineExceeded` с понятным текстом."""
    token = _DEADLINE.set((time.monotonic() + float(seconds), float(seconds), what))
    try:
        yield
    finally:
        _DEADLINE.reset(token)


def _budget(request: httpx.Request) -> None:
    dl = _DEADLINE.get()
    if dl is None:
        return
    until, total, what = dl
    left = until - time.monotonic()
    if left <= 0.5:
        raise DeadlineExceeded(f"{what}: сервер не ответил за {total:.0f} с — попробуй позже или проверь сеть/VPN")
    current = request.extensions.get("timeout") or {}
    request.extensions["timeout"] = {k: min(v, left) if v is not None else left
                                     for k, v in (current or httpx.Timeout(left).as_dict()).items()}


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
            _budget(request)
            try:
                resp = super().send(request, **kwargs)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                last_exc = exc
                resp = None
            if resp is not None and resp.status_code not in RETRY_STATUSES:
                return resp
            if attempt == attempts - 1 or _DEADLINE.get() is not None and _DEADLINE.get()[0] - time.monotonic() < delay:
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
    timeout = kwargs.get("timeout")
    if timeout is None or isinstance(timeout, (int, float)):
        kwargs["timeout"] = make_timeout(timeout)
    return RetryingClient(**kwargs)
