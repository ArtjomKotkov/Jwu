"""Доступ к секретам (пароли/токены): env-переменная → keyring.

Изолирует хранилище от остального кода. Намеренно не импортирует другие модули
проекта (чтобы не было циклов с config). На чтении при недоступном keyring-бэкенде
возвращает None; на записи пробрасывает keyring.errors.KeyringError — обработает CLI.
"""

from __future__ import annotations

import os

import keyring
from keyring.errors import KeyringError


def get_secret(service: str, account: str, *, env_var: str | None = None) -> str | None:
    """Секрет по (service, account). Сначала env_var (если задана и непуста), затем keyring."""
    if env_var:
        val = os.environ.get(env_var)
        if val:
            return val.strip()
    try:
        secret = keyring.get_password(service, account)
    except KeyringError:
        return None
    return secret or None


def set_secret(service: str, account: str, value: str) -> None:
    """Записать секрет в keyring. Может бросить KeyringError, если бэкенд недоступен."""
    keyring.set_password(service, account, value)


def delete_secret(service: str, account: str) -> None:
    """Удалить секрет; молча игнорировать, если его не было/бэкенд недоступен."""
    try:
        keyring.delete_password(service, account)
    except KeyringError:
        pass
