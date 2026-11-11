"""Транспорт Telegram: Bot API, резервный бот, отметка «лежит», последнее сообщение в чате.

Встроенный фасад jwu (аддон ``telegram``): ядро шлёт и читает чат только через отправщик,
который собирает :func:`sender_from_config`, — само о Telegram ничего не знает.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Optional

import httpx

from jwu.core.notify import KEYBOARD, OFFSET_META, NotifyError, all_commands, split_message

if TYPE_CHECKING:
    from jwu.core.config import Config

TELEGRAM_API = "https://api.telegram.org"


def _last_path():
    from jwu.core.config import data_dir

    return data_dir() / "telegram-last.json"


def last_message_id(chat_id: str) -> int:
    """id последнего сообщения бота в чате (по отметке jwu): склейка дописывает только в него."""
    import json

    try:
        return int(json.loads(_last_path().read_text(encoding="utf-8")).get(str(chat_id), 0))
    except (OSError, ValueError, TypeError):
        return 0


def _remember_last(chat_id: str, message_id: int) -> None:
    import json

    if not message_id:
        return
    path = _last_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data[str(chat_id)] = int(message_id)
    try:
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


_SHARED: dict[str, httpx.Client] = {}
_SHARED_LOCK = __import__("threading").Lock()


class TelegramDown(NotifyError):
    """Бот не отвечает: нет связи, таймаут, 5xx. Повод слать через резервного бота."""


DOWN_FOR = 300.0                  # бот не ответил — столько секунд сразу идём в резерв
_DOWN: dict[str, float] = {}      # токен → до какого времени (monotonic) считаем лежащим


def mark_down(token: str) -> None:
    _DOWN[token] = time.monotonic() + DOWN_FOR


def is_down(token: str) -> bool:
    return _DOWN.get(token, 0.0) > time.monotonic()


def _unavailable(exc: Exception) -> TelegramDown:
    return TelegramDown(f"Telegram недоступен: {exc}")


def _failed(resp: httpx.Response, text: str = "") -> NotifyError:
    # Токен в URL — в ошибку его не тащим.
    msg = f"Telegram ответил {resp.status_code}: {text or resp.text[:200]}"
    return TelegramDown(msg) if resp.status_code >= 500 else NotifyError(msg)


def _shared_client(token: str, timeout: float) -> httpx.Client:
    """Одно постоянное соединение на бота на весь процесс. Новое TLS-соединение до Telegram
    стоит ~1 с и временами виснет на 10+ с — каждое нажатие кнопки ждало бы его заново."""
    with _SHARED_LOCK:
        client = _SHARED.get(token)
        if client is None or client.is_closed:
            client = httpx.Client(
                timeout=httpx.Timeout(timeout, connect=3.0),
                transport=httpx.HTTPTransport(retries=2),       # завис коннект — быстро ещё раз
                limits=httpx.Limits(max_keepalive_connections=8, keepalive_expiry=300),
            )
            _SHARED[token] = client
        return client


class TelegramNotifier:
    """Отправка в чат через Bot API. ``client`` подменяется в тестах."""

    def __init__(self, token: str, chat_id: str, *, client: Optional[httpx.Client] = None,
                 timeout: float = 10.0, api: str = TELEGRAM_API, role: str = "main") -> None:
        if not token or not chat_id:
            raise NotifyError("Telegram не настроен: нужны токен бота и chat_id")
        self.token = token
        self.chat_id = chat_id
        self.role = role                  # main | backup — у каждого бота своё смещение опроса
        self.offset_meta = OFFSET_META if role == "main" else f"{OFFSET_META}:{role}"
        self._owns_client = False    # общий клиент процесса не закрываем вместе с отправщиком
        self._client = client or _shared_client(token, timeout)
        self._api = api.rstrip("/")

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def last_message_id(self) -> int:
        """id последнего сообщения бота в этом чате — склейка уведомлений дописывает только в него."""
        return last_message_id(self.chat_id)

    def send(self, text: str, *, reply_to: Optional[int] = None,
             keyboard: bool = False, markup: Optional[dict] = None, silent: bool = False) -> list[int]:
        """Отправить текст (при необходимости — несколькими сообщениями). Вернуть их id.

        ``reply_to`` — ответить на конкретное сообщение (подтверждение заметки, ответ на
        команду), чтобы в чате было видно, к чему это. ``keyboard`` — приложить постоянную
        клавиатуру с командами (к последнему куску). ``markup`` — своя клавиатура (например,
        inline-кнопки меню), тоже к последнему куску; главнее ``keyboard``.
        """
        ids: list[int] = []
        chunks = split_message(text)
        for i, chunk in enumerate(chunks):
            payload: dict = {
                "chat_id": self.chat_id, "text": chunk, "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if silent:
                payload["disable_notification"] = True
            if reply_to is not None:
                payload["reply_to_message_id"] = int(reply_to)
                payload["allow_sending_without_reply"] = True
            if i == len(chunks) - 1:
                if markup:
                    payload["reply_markup"] = markup
                elif keyboard:
                    payload["reply_markup"] = KEYBOARD
            try:
                resp = self._client.post(f"{self._api}/bot{self.token}/sendMessage", json=payload)
            except httpx.HTTPError as exc:
                raise _unavailable(exc) from exc
            if resp.status_code >= 400:
                raise _failed(resp)
            body = resp.json() if resp.content else {}
            ids.append(int((body.get("result") or {}).get("message_id", 0) or 0))
        if ids:
            _remember_last(self.chat_id, ids[-1])
        return ids

    def set_commands(self) -> bool:
        """Меню команд бота (подсказки на «/»). Ошибка не критична."""
        try:
            resp = self._client.post(f"{self._api}/bot{self.token}/setMyCommands",
                                     json={"commands": all_commands()})
        except httpx.HTTPError:
            return False
        return resp.status_code < 400

    def get_updates(self, offset: Optional[int] = None, *, limit: int = 100,
                    timeout: int = 0) -> list[dict]:
        """Входящие сообщения боту. ``timeout`` > 0 — long polling: Telegram держит запрос
        до появления сообщения (или до истечения секунд), так что реакция мгновенная,
        а пустых опросов нет."""
        params: dict = {"limit": limit, "timeout": max(0, int(timeout)),
                        "allowed_updates": '["message","callback_query"]'}
        if offset is not None:
            params["offset"] = offset
        try:
            resp = self._client.get(f"{self._api}/bot{self.token}/getUpdates", params=params,
                                    timeout=httpx.Timeout(float(max(10, timeout + 10))))
        except httpx.HTTPError as exc:
            raise _unavailable(exc) from exc
        if resp.status_code >= 400:
            raise _failed(resp)
        return list((resp.json() or {}).get("result") or [])

    def _post(self, method: str, payload: dict) -> dict:
        try:
            resp = self._client.post(f"{self._api}/bot{self.token}/{method}", json=payload)
        except httpx.HTTPError as exc:
            raise _unavailable(exc) from exc
        if resp.status_code >= 400:
            raise _failed(resp)
        return resp.json() if resp.content else {}

    def edit(self, message_id: int, text: str, *, markup: Optional[dict] = None) -> None:
        """Заменить текст и кнопки своего сообщения (меню, склейка уведомлений)."""
        payload: dict = {"chat_id": self.chat_id, "message_id": int(message_id), "text": text,
                         "parse_mode": "HTML", "disable_web_page_preview": True,
                         "reply_markup": markup or {"inline_keyboard": []}}
        try:
            self._post("editMessageText", payload)
        except NotifyError as exc:
            if "message is not modified" in str(exc):
                return   # нажали то, что уже на экране — не ошибка
            raise

    def delete(self, message_id: int) -> bool:
        """Удалить сообщение в чате (своё меню или нажатие кнопки меню). Не вышло — не беда."""
        try:
            self._post("deleteMessage", {"chat_id": self.chat_id, "message_id": int(message_id)})
            return True
        except NotifyError:
            return False

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        """Снять «часики» с нажатой кнопки (без этого Telegram крутит их ~15 с)."""
        payload: dict = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text[:200]
        self._post("answerCallbackQuery", payload)

    def get_me(self) -> dict:
        """Проверка токена: кто мы (`/getMe`)."""
        try:
            resp = self._client.get(f"{self._api}/bot{self.token}/getMe")
        except httpx.HTTPError as exc:
            raise _unavailable(exc) from exc
        if resp.status_code >= 400:
            raise _failed(resp, "токен бота неверный?")
        return (resp.json() or {}).get("result") or {}


class FallbackNotifier:
    """Основной бот + резервный: основной не ответил — то же сообщение уходит через резервный.

    Лежащий бот (TelegramDown) помечается на ``DOWN_FOR`` секунд: следующие отправки сразу идут
    в резерв, не ожидая таймаута. Порядок ``first``/``second`` — кто главный для этого отправщика:
    ответ на нажатие в резервном боте собирается как ``swapped()`` и уходит через него же.
    """

    def __init__(self, first: TelegramNotifier, second: TelegramNotifier) -> None:
        self.first, self.second = first, second
        self.token = first.token
        self.chat_id = first.chat_id
        self.role = first.role
        self.offset_meta = first.offset_meta

    def swapped(self) -> "FallbackNotifier":
        return FallbackNotifier(self.second, self.first)

    def bots(self) -> list[TelegramNotifier]:
        if is_down(self.first.token) and not is_down(self.second.token):
            return [self.second, self.first]
        return [self.first, self.second]

    def _try(self, method: str, *args, any_error: bool = False, **kwargs):
        last: Optional[Exception] = None
        for bot in self.bots():
            try:
                return getattr(bot, method)(*args, **kwargs)
            except TelegramDown as exc:
                mark_down(bot.token)
                last = exc
            except NotifyError as exc:
                if not any_error:
                    raise
                last = exc
        raise last  # type: ignore[misc]

    def send(self, text: str, **kwargs) -> list[int]:
        return self._try("send", text, **kwargs)

    def edit(self, message_id: int, text: str, **kwargs) -> None:
        # сообщение мог отправить любой из двух — чужое бот править не может (400), пробуем оба
        self._try("edit", message_id, text, any_error=True, **kwargs)

    def delete(self, message_id: int) -> bool:
        return any(bot.delete(message_id) for bot in self.bots())

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.first.answer_callback(callback_id, text)     # нажатие пришло в этот бот — ему и отвечать

    def get_updates(self, offset: Optional[int] = None, **kwargs) -> list[dict]:
        try:
            return self.first.get_updates(offset, **kwargs)
        except TelegramDown:
            mark_down(self.first.token)
            raise

    def set_commands(self) -> bool:
        return all([self.first.set_commands(), self.second.set_commands()])

    def get_me(self) -> dict:
        return self.first.get_me()

    def last_message_id(self) -> int:
        return last_message_id(self.chat_id)

    def check_main(self) -> str:
        """Кнопка резервного бота «проверить основной»: отвечает ли он. Ожил — снять отметку «лежит»."""
        import html

        main = self.second
        try:
            me = main.get_me()
        except NotifyError as exc:
            mark_down(main.token)
            return f"❌ Основной бот не отвечает: {html.escape(str(exc))}\nПишу через резервный, проверю ещё раз сам."
        _DOWN.pop(main.token, None)
        return (f"✅ Основной бот @{html.escape(me.get('username', '?'))} отвечает — "
                "уведомления снова идут через него. Кнопки и команды — тоже в нём.")

    def close(self) -> None:
        self.first.close()
        self.second.close()


def sender_from_config(cfg: "Config"):
    """Собрать отправщик из конфига воркспейса; None — уведомления не настроены.

    Задан резервный бот (секрет ``telegram.backup_token``) — :class:`FallbackNotifier`.
    """
    from jwu.core.config import telegram_backup_token, telegram_token

    chat_id = (cfg.telegram.chat_id or "").strip()
    if not chat_id:
        return None
    token = telegram_token(cfg)
    if not token:
        return None
    main = TelegramNotifier(token, chat_id)
    backup = telegram_backup_token(cfg)
    if backup and backup != token:
        return FallbackNotifier(main, TelegramNotifier(backup, chat_id, role="backup"))
    return main
