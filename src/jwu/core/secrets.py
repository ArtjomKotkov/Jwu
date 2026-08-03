"""Доступ к секретам (пароли/токены).

Секрет адресуется «слотом» — ``jira.token``, ``bitbucket.token`` и т.п. (см. ``SECRET_SLOTS``).
Откуда он берётся, решает источник (``SecretSource``):

- ``KeyringSecrets`` — системный keyring по паре (service, account). Так было исторически;
  остаётся дефолтом для ``Config`` без воркспейса и фолбэком после переезда.
- ``DbSecrets`` — секреты воркспейса в SQLite. Порядок чтения: переменная окружения →
  БД → keyring (чтобы не потерять доступ, пока пользователь не смигрировал).

Переменная окружения перекрывает всё и в том, и в другом источнике.

Модуль намеренно не импортирует другие модули проекта (чтобы не было циклов с config).
На чтении при недоступном keyring-бэкенде возвращает None; на записи пробрасывает
keyring.errors.KeyringError — обработает CLI.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Optional, Protocol, Tuple

import keyring
from keyring.errors import KeyringError

if TYPE_CHECKING:  # pragma: no cover — только для типов, чтобы не тянуть store в рантайме
    from .store import Store

# Все секреты, которые вообще бывают у jwu. Единый источник правды для миграции,
# бандла и `jwu workspace show`.
SECRET_SLOTS: tuple[str, ...] = (
    "jira.token", "jira.password", "jira.gate_password",
    "sdesk.token", "sdesk.password", "sdesk.gate_password",
    "bitbucket.token", "github.token", "jenkins.token",
)

# Слоты, которые можно перебить переменной окружения (как и раньше).
SLOT_ENV: dict[str, str] = {
    "jira.token": "JIRA_TOKEN",
    "sdesk.token": "SDESK_TOKEN",
    "bitbucket.token": "BITBUCKET_TOKEN",
    # GITHUB_TOKEN — общепринятое имя (его же ставит gh CLI и Actions), поэтому
    # он и подхватывается автоматически: чаще всего токен уже есть в окружении.
    "github.token": "GITHUB_TOKEN",
    "jenkins.token": "JENKINS_TOKEN",
}

KeyringRef = Optional[Tuple[str, str]]  # (service, account) в системном keyring


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


def _env_value(env_var: str | None) -> str | None:
    if not env_var:
        return None
    val = os.environ.get(env_var)
    return val.strip() if val else None


class SecretSource(Protocol):
    """Откуда брать секрет по слоту. ``keyring_ref`` знает только вызывающий (config)."""

    def get(self, slot: str, *, env_var: str | None = None,
            keyring_ref: KeyringRef = None) -> str | None: ...

    def set(self, slot: str, value: str, *, keyring_ref: KeyringRef = None) -> None: ...


class KeyringSecrets:
    """Историческое хранилище: системный keyring по (service, account)."""

    def get(self, slot: str, *, env_var: str | None = None,
            keyring_ref: KeyringRef = None) -> str | None:
        env = _env_value(env_var)
        if env:
            return env
        if not keyring_ref or not all(keyring_ref):
            return None
        return get_secret(*keyring_ref)

    def set(self, slot: str, value: str, *, keyring_ref: KeyringRef = None) -> None:
        if not keyring_ref or not all(keyring_ref):
            raise ValueError(f"Для слота {slot} неизвестна пара (service, account) в keyring")
        set_secret(*keyring_ref, value)


class DbSecrets:
    """Секреты воркспейса в БД. Читает env → БД → keyring (фолбэк на время переезда).

    Keyring остаётся доступным на чтение, чтобы обновление jwu не отбирало доступ у тех,
    кто ещё не мигрировал; записи туда больше не идут — иначе секрет жил бы в двух местах
    и было бы неясно, какой актуален.
    """

    def __init__(self, store: "Store", workspace_id: int, *, keyring_fallback: bool = True) -> None:
        self.store = store
        self.workspace_id = workspace_id
        self.keyring_fallback = keyring_fallback

    def get(self, slot: str, *, env_var: str | None = None,
            keyring_ref: KeyringRef = None) -> str | None:
        env = _env_value(env_var)
        if env:
            return env
        value = self.store.get_workspace_secret(self.workspace_id, slot)
        if value:
            return value
        if self.keyring_fallback and keyring_ref and all(keyring_ref):
            return get_secret(*keyring_ref)
        return None

    def set(self, slot: str, value: str, *, keyring_ref: KeyringRef = None) -> None:
        self.store.set_workspace_secret(self.workspace_id, slot, value)
