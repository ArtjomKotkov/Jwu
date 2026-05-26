"""Конфиг и секреты.

Конфиг читается из ``~/.config/jwu/config.toml`` (с уважением к XDG_CONFIG_HOME).
Файла может не быть — тогда используются разумные дефолты под jira.example.com / git.example.com.
Токены берутся из macOS keychain (``security``) с фолбэком на переменные окружения.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import tomli_w

from . import secrets

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # Python 3.10 — внешний бэкпорт tomli
    import tomli as tomllib  # type: ignore[no-redef]


DEFAULT_VIEWS: dict[str, str] = {
    "mine": (
        "assignee = currentUser() AND resolution = Unresolved "
        "ORDER BY updated DESC"
    ),
    # «Ждут моего ревью» здесь — это PR в Bitbucket (см. `prs --view review`),
    # а не задачи Jira: на инстансе нет поля reviewer. Для Jira-задач вью review не задаём.
    # На Jira Server нет чистого JQL под @mentions — берём задачи, где я фигурирую,
    # и комменты потом сканируются локально в service-слое.
    "mentions": (
        "(comment ~ currentUser() OR watcher = currentUser()) "
        "AND updated >= -14d ORDER BY updated DESC"
    ),
}


@dataclass
class JiraConfig:
    base_url: str = "https://jira.example.com"
    project: str = "PROJ"
    username: str = ""  # для локального матчинга упоминаний; пусто => берём из /myself
    views: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_VIEWS))
    token_account: str = "jira"
    token_service: str = "jira-pat"
    token_env: str = "JIRA_TOKEN"
    # nginx Basic-гейт перед Jira (если есть): keychain service, account=логин, -w=пароль
    proxy_basic_service: str = "jira-proxy-basic"
    # сессионный логин в Jira (когда гейт занимает заголовок Authorization):
    # keychain service, account=логин Jira, -w=пароль Jira
    login_service: str = "jira-login"
    proxy_basic_user: str = ""  # логин nginx-гейта (account для секрета proxy_basic)


@dataclass
class BitbucketConfig:
    base_url: str = "https://git.example.com"
    project: str = "PROJ"
    repo: str = "repo"
    token_account: str = "bitbucket"
    token_service: str = "bitbucket-pat"
    token_env: str = "BITBUCKET_TOKEN"


@dataclass
class StorageConfig:
    db_path: str = ""  # пусто => дефолт data_dir()/state.db; переопределяется env JWU_DB_PATH


@dataclass
class Config:
    jira: JiraConfig = field(default_factory=JiraConfig)
    bitbucket: BitbucketConfig = field(default_factory=BitbucketConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)


class ConfigError(RuntimeError):
    """Проблема с конфигом или отсутствующим токеном."""


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "jwu" / "config.toml"


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    d = Path(base) / "jwu"
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path(cfg: "Config | None" = None) -> Path:
    """Путь до БД: env JWU_DB_PATH → [storage].db_path → дефолт data_dir()/state.db."""
    env = os.environ.get("JWU_DB_PATH")
    if env:
        return Path(env).expanduser()
    cfg = cfg or load_config()
    if cfg.storage.db_path:
        return Path(cfg.storage.db_path).expanduser()
    return data_dir() / "state.db"


def load_config(path: Path | None = None) -> Config:
    """Прочитать конфиг; при отсутствии файла вернуть дефолты."""
    path = path or config_path()
    cfg = Config()
    if not path.exists():
        return cfg
    if tomllib is None:  # pragma: no cover
        raise ConfigError("tomllib недоступен — нужен Python 3.11+ для чтения config.toml")
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    return _apply_raw(cfg, raw)


def _apply_raw(cfg: Config, raw: dict) -> Config:
    """Наложить разобранный TOML (dict) на Config. Общая логика load_config и import_bundle."""
    j = raw.get("jira", {}) or {}
    cfg.jira.base_url = j.get("base_url", cfg.jira.base_url).rstrip("/")
    cfg.jira.project = j.get("project", cfg.jira.project)
    cfg.jira.username = j.get("username", cfg.jira.username)
    cfg.jira.token_account = j.get("token_account", cfg.jira.token_account)
    cfg.jira.token_service = j.get("token_service", cfg.jira.token_service)
    cfg.jira.token_env = j.get("token_env", cfg.jira.token_env)
    cfg.jira.proxy_basic_service = j.get("proxy_basic_service", cfg.jira.proxy_basic_service)
    cfg.jira.proxy_basic_user = j.get("proxy_basic_user", cfg.jira.proxy_basic_user)
    cfg.jira.login_service = j.get("login_service", cfg.jira.login_service)
    views = j.get("views") or {}
    if views:
        cfg.jira.views = {**DEFAULT_VIEWS, **views}

    b = raw.get("bitbucket", {}) or {}
    cfg.bitbucket.base_url = b.get("base_url", cfg.bitbucket.base_url).rstrip("/")
    cfg.bitbucket.project = b.get("project", cfg.bitbucket.project)
    cfg.bitbucket.repo = b.get("repo", cfg.bitbucket.repo)
    cfg.bitbucket.token_account = b.get("token_account", cfg.bitbucket.token_account)
    cfg.bitbucket.token_service = b.get("token_service", cfg.bitbucket.token_service)
    cfg.bitbucket.token_env = b.get("token_env", cfg.bitbucket.token_env)

    s = raw.get("storage", {}) or {}
    cfg.storage.db_path = s.get("db_path", cfg.storage.db_path)
    return cfg


def save_config(cfg: Config, path: Path | None = None) -> Path:
    """Записать несекретные поля в config.toml, сохранив прочие ключи и views.

    Секреты сюда НЕ пишутся (они в keyring). Каталог создаётся при необходимости.
    """
    path = path or config_path()
    raw: dict = {}
    if path.exists() and tomllib is not None:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)

    jira = raw.setdefault("jira", {})
    jira["base_url"] = cfg.jira.base_url
    jira["username"] = cfg.jira.username
    jira["project"] = cfg.jira.project
    if cfg.jira.proxy_basic_user:
        jira["proxy_basic_user"] = cfg.jira.proxy_basic_user

    bb = raw.setdefault("bitbucket", {})
    bb["base_url"] = cfg.bitbucket.base_url
    bb["project"] = cfg.bitbucket.project
    bb["repo"] = cfg.bitbucket.repo

    if cfg.storage.db_path:
        raw.setdefault("storage", {})["db_path"] = cfg.storage.db_path

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        tomli_w.dump(raw, fh)
    return path


def _require_secret(service: str, account: str, env_var: str) -> str:
    val = secrets.get_secret(service, account, env_var=env_var)
    if not val:
        raise ConfigError(
            f"Секрет не найден ({service}). Запусти `jwu configure` "
            f"или задай переменную окружения {env_var}."
        )
    return val


def jira_token(cfg: Config) -> str:
    return _require_secret(cfg.jira.token_service, cfg.jira.token_account, cfg.jira.token_env)


def bitbucket_token(cfg: Config) -> str:
    return _require_secret(
        cfg.bitbucket.token_service, cfg.bitbucket.token_account, cfg.bitbucket.token_env
    )


def jira_login(cfg: Config) -> tuple[str, str] | None:
    """Сессионный логин Jira (username из конфига, пароль из keyring) или None."""
    if not cfg.jira.username:
        return None
    pw = secrets.get_secret(cfg.jira.login_service, cfg.jira.username)
    return (cfg.jira.username, pw) if pw else None


def jira_proxy_basic(cfg: Config) -> tuple[str, str] | None:
    """Креды nginx-гейта (proxy_basic_user из конфига, пароль из keyring) или None."""
    if not cfg.jira.proxy_basic_user:
        return None
    pw = secrets.get_secret(cfg.jira.proxy_basic_service, cfg.jira.proxy_basic_user)
    return (cfg.jira.proxy_basic_user, pw) if pw else None


def secret_slots(cfg: Config) -> list[tuple[str, str]]:
    """Все (service, account) секретов, известных по конфигу (с пустым account отброшены).

    Единый источник правды о том, какие секреты у jwu есть: PAT Jira, сессионный
    пароль Jira, пароль nginx-гейта, PAT Bitbucket.
    """
    pairs = [
        (cfg.jira.token_service, cfg.jira.token_account),
        (cfg.jira.login_service, cfg.jira.username),
        (cfg.jira.proxy_basic_service, cfg.jira.proxy_basic_user),
        (cfg.bitbucket.token_service, cfg.bitbucket.token_account),
    ]
    return [(s, a) for (s, a) in pairs if s and a]


def export_bundle(cfg: Config, path: Path) -> int:
    """Записать переносимый бандл: несекретные поля + СЕКРЕТЫ из keyring (плайнтекст).

    Возвращает число выгруженных секретов. Файл содержит пароли в открытом виде —
    предназначен для переноса между машинами, хранить безопасно.
    """
    raw: dict = {
        "jira": {
            "base_url": cfg.jira.base_url,
            "username": cfg.jira.username,
            "project": cfg.jira.project,
        },
        "bitbucket": {
            "base_url": cfg.bitbucket.base_url,
            "project": cfg.bitbucket.project,
            "repo": cfg.bitbucket.repo,
        },
        "storage": {"db_path": cfg.storage.db_path},
    }
    if cfg.jira.proxy_basic_user:
        raw["jira"]["proxy_basic_user"] = cfg.jira.proxy_basic_user

    sec_list: list[dict] = []
    for service, account in secret_slots(cfg):
        val = secrets.get_secret(service, account)
        if val:
            sec_list.append({"service": service, "account": account, "value": val})
    raw["secrets"] = sec_list

    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        tomli_w.dump(raw, fh)
    return len(sec_list)


def import_bundle(path: Path) -> tuple[Config, int]:
    """Прочитать бандл: применить конфиг (в config.toml) и записать секреты в keyring.

    Возвращает (cfg, число записанных секретов). KeyringError при записи секрета
    пробрасывается наверх — обработает CLI.
    """
    path = Path(path).expanduser()
    if not path.exists():
        raise ConfigError(f"Файл бандла не найден: {path}")
    if tomllib is None:  # pragma: no cover
        raise ConfigError("tomllib недоступен — нужен Python 3.11+ для чтения бандла")
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    cfg = _apply_raw(Config(), raw)
    save_config(cfg)

    written = 0
    for entry in raw.get("secrets", []) or []:
        service = entry.get("service")
        account = entry.get("account")
        value = entry.get("value")
        if service and account and value:
            secrets.set_secret(service, account, value)
            written += 1
    return cfg, written
