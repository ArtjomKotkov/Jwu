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
class SdeskConfig:
    """Второй Jira-инстанс (SDESK) — тот же REST API, но отдельный хост и СВОИ секреты.

    Креды могут совпадать с основной Jira, но лежат под отдельными keyring-сервисами
    (``sdesk-*``), чтобы их можно было ротировать/сносить независимо. Инстанс считается
    подключённым, только когда заданы ``base_url`` и ``project`` (см. ``sdesk_enabled``);
    иначе jwu ведёт себя как раньше, будто SDESK нет. Задачи резолвятся в этот инстанс
    по префиксу ключа, совпадающему с ``project`` (напр. ``SDESK-39336``).
    """

    base_url: str = ""
    project: str = ""  # префикс ключей этого инстанса (SDESK); пусто => инстанс выключен
    username: str = ""  # логин для login/proxy-секретов; пусто => берётся из session_login
    token_account: str = "sdesk"
    token_service: str = "sdesk-pat"
    token_env: str = "SDESK_TOKEN"
    proxy_basic_service: str = "sdesk-proxy-basic"
    login_service: str = "sdesk-login"
    proxy_basic_user: str = ""


@dataclass
class BitbucketConfig:
    base_url: str = "https://git.example.com"
    project: str = "PROJ"
    repo: str = "repo"
    token_account: str = "bitbucket"
    token_service: str = "bitbucket-pat"
    token_env: str = "BITBUCKET_TOKEN"


@dataclass
class JenkinsConfig:
    """CI Jenkins: глубокие детали сборок (тест-репорт, консоль) поверх статусов из Bitbucket.

    Авторизация — HTTP basic ``username:apiToken`` (API-токен из профиля Jenkins).
    Сборки jwu видит и без Jenkins (через build-status API Bitbucket); токен нужен только
    чтобы вытащить причину падения (упавшие кейсы, лог). Без него `jwu build` деградирует
    до списка статусов.
    """

    base_url: str = "https://jenkins.example.com"
    username: str = ""  # логин Jenkins = account для basic-auth и для секрета токена
    token_service: str = "jenkins-pat"
    token_env: str = "JENKINS_TOKEN"


@dataclass
class StorageConfig:
    db_path: str = ""  # пусто => дефолт data_dir()/state.db; переопределяется env JWU_DB_PATH


@dataclass
class Config:
    jira: JiraConfig = field(default_factory=JiraConfig)
    sdesk: SdeskConfig = field(default_factory=SdeskConfig)
    bitbucket: BitbucketConfig = field(default_factory=BitbucketConfig)
    jenkins: JenkinsConfig = field(default_factory=JenkinsConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    # Откуда берутся секреты. По умолчанию — системный keyring (как исторически);
    # для конфига воркспейса подставляется secrets.DbSecrets (см. workspaces.config_for_workspace).
    secrets: "secrets.SecretSource" = field(default_factory=secrets.KeyringSecrets)


# Слот секрета -> где он лежал в keyring: (service, account). Пара вычисляется из конфига,
# потому что account — это логин/имя аккаунта из соответствующей секции.
def slot_keyring_ref(cfg: Config, slot: str) -> tuple[str, str] | None:
    section, _, kind = slot.partition(".")
    if section in ("jira", "sdesk"):
        sec = cfg.jira if section == "jira" else cfg.sdesk
        pairs = {
            "token": (sec.token_service, sec.token_account),
            "password": (sec.login_service, sec.username),
            "gate_password": (sec.proxy_basic_service, sec.proxy_basic_user),
        }
    elif section == "bitbucket":
        pairs = {"token": (cfg.bitbucket.token_service, cfg.bitbucket.token_account)}
    elif section == "jenkins":
        pairs = {"token": (cfg.jenkins.token_service, cfg.jenkins.username)}
    else:
        return None
    ref = pairs.get(kind)
    return ref if ref and all(ref) else None


def get_slot(cfg: Config, slot: str) -> str | None:
    """Значение секрета по слоту из настроенного источника (env → источник → keyring)."""
    return cfg.secrets.get(
        slot, env_var=secrets.SLOT_ENV.get(slot), keyring_ref=slot_keyring_ref(cfg, slot)
    )


def set_slot(cfg: Config, slot: str, value: str) -> None:
    """Записать секрет по слоту в настроенный источник."""
    cfg.secrets.set(slot, value, keyring_ref=slot_keyring_ref(cfg, slot))


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
    """Наложить разобранный TOML (dict) на Config. Общая логика load_config и read_bundle."""
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

    sd = raw.get("sdesk", {}) or {}
    cfg.sdesk.base_url = sd.get("base_url", cfg.sdesk.base_url).rstrip("/")
    cfg.sdesk.project = sd.get("project", cfg.sdesk.project)
    cfg.sdesk.username = sd.get("username", cfg.sdesk.username)
    cfg.sdesk.token_account = sd.get("token_account", cfg.sdesk.token_account)
    cfg.sdesk.token_service = sd.get("token_service", cfg.sdesk.token_service)
    cfg.sdesk.token_env = sd.get("token_env", cfg.sdesk.token_env)
    cfg.sdesk.proxy_basic_service = sd.get("proxy_basic_service", cfg.sdesk.proxy_basic_service)
    cfg.sdesk.proxy_basic_user = sd.get("proxy_basic_user", cfg.sdesk.proxy_basic_user)
    cfg.sdesk.login_service = sd.get("login_service", cfg.sdesk.login_service)

    b = raw.get("bitbucket", {}) or {}
    cfg.bitbucket.base_url = b.get("base_url", cfg.bitbucket.base_url).rstrip("/")
    cfg.bitbucket.project = b.get("project", cfg.bitbucket.project)
    cfg.bitbucket.repo = b.get("repo", cfg.bitbucket.repo)
    cfg.bitbucket.token_account = b.get("token_account", cfg.bitbucket.token_account)
    cfg.bitbucket.token_service = b.get("token_service", cfg.bitbucket.token_service)
    cfg.bitbucket.token_env = b.get("token_env", cfg.bitbucket.token_env)

    k = raw.get("jenkins", {}) or {}
    cfg.jenkins.base_url = k.get("base_url", cfg.jenkins.base_url).rstrip("/")
    cfg.jenkins.username = k.get("username", cfg.jenkins.username)
    cfg.jenkins.token_service = k.get("token_service", cfg.jenkins.token_service)
    cfg.jenkins.token_env = k.get("token_env", cfg.jenkins.token_env)

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

    # SDESK пишем, только если инстанс подключён — иначе не засоряем конфиг пустой секцией.
    if sdesk_enabled(cfg):
        sd = raw.setdefault("sdesk", {})
        sd["base_url"] = cfg.sdesk.base_url
        sd["project"] = cfg.sdesk.project
        sd["username"] = cfg.sdesk.username
        if cfg.sdesk.proxy_basic_user:
            sd["proxy_basic_user"] = cfg.sdesk.proxy_basic_user

    bb = raw.setdefault("bitbucket", {})
    bb["base_url"] = cfg.bitbucket.base_url
    bb["project"] = cfg.bitbucket.project
    bb["repo"] = cfg.bitbucket.repo

    if cfg.jenkins.username or cfg.jenkins.base_url != JenkinsConfig().base_url:
        jk = raw.setdefault("jenkins", {})
        jk["base_url"] = cfg.jenkins.base_url
        jk["username"] = cfg.jenkins.username

    if cfg.storage.db_path:
        raw.setdefault("storage", {})["db_path"] = cfg.storage.db_path

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        tomli_w.dump(raw, fh)
    return path


def _require_slot(cfg: Config, slot: str) -> str:
    val = get_slot(cfg, slot)
    if not val:
        env_var = secrets.SLOT_ENV.get(slot, "")
        hint = f" или задай переменную окружения {env_var}" if env_var else ""
        raise ConfigError(
            f"Секрет не найден ({slot}). Запусти `jwu configure`{hint}."
        )
    return val


def sdesk_enabled(cfg: Config) -> bool:
    """Подключён ли второй Jira-инстанс (SDESK): заданы и хост, и project-префикс."""
    return bool(cfg.sdesk.base_url and cfg.sdesk.project)


# --- секреты Jira-подобного инстанса (Jira / SDESK) ------------------------- #
# Обе секции (JiraConfig / SdeskConfig) устроены одинаково, поэтому логика доступа
# общая и параметризуется префиксом слота ("jira" | "sdesk").


def _section_login(cfg: Config, prefix: str) -> tuple[str, str] | None:
    """Сессионный логин (username из секции, пароль из хранилища) или None."""
    section = cfg.jira if prefix == "jira" else cfg.sdesk
    if not section.username:
        return None
    pw = get_slot(cfg, f"{prefix}.password")
    return (section.username, pw) if pw else None


def _section_proxy_basic(cfg: Config, prefix: str) -> tuple[str, str] | None:
    """Креды nginx-гейта (proxy_basic_user из секции, пароль из хранилища) или None."""
    section = cfg.jira if prefix == "jira" else cfg.sdesk
    if not section.proxy_basic_user:
        return None
    pw = get_slot(cfg, f"{prefix}.gate_password")
    return (section.proxy_basic_user, pw) if pw else None


def jira_token(cfg: Config) -> str:
    return _require_slot(cfg, "jira.token")


def sdesk_token(cfg: Config) -> str:
    return _require_slot(cfg, "sdesk.token")


def bitbucket_token(cfg: Config) -> str:
    return _require_slot(cfg, "bitbucket.token")


def jenkins_auth(cfg: Config) -> tuple[str, str] | None:
    """Basic-auth (username, apiToken) для Jenkins или None, если токен/логин не заданы.

    Возвращает None мягко (не бросает): Jenkins опционален — без него jwu всё равно
    показывает статусы сборок через Bitbucket, просто без детализации причины падения.
    """
    if not cfg.jenkins.username:
        return None
    token = get_slot(cfg, "jenkins.token")
    return (cfg.jenkins.username, token) if token else None


def jira_login(cfg: Config) -> tuple[str, str] | None:
    """Сессионный логин Jira (username из конфига, пароль из хранилища) или None."""
    return _section_login(cfg, "jira")


def jira_proxy_basic(cfg: Config) -> tuple[str, str] | None:
    """Креды nginx-гейта Jira (proxy_basic_user из конфига, пароль из хранилища) или None."""
    return _section_proxy_basic(cfg, "jira")


def sdesk_login(cfg: Config) -> tuple[str, str] | None:
    """Сессионный логин SDESK (username из конфига, пароль из хранилища) или None."""
    return _section_login(cfg, "sdesk")


def sdesk_proxy_basic(cfg: Config) -> tuple[str, str] | None:
    """Креды nginx-гейта SDESK (proxy_basic_user из конфига, пароль из хранилища) или None."""
    return _section_proxy_basic(cfg, "sdesk")


def export_bundle(cfg: Config, path: Path) -> int:
    """Записать переносимый бандл: несекретные поля + СЕКРЕТЫ (плайнтекст).

    Секреты берутся из источника, настроенного в ``cfg`` (БД воркспейса либо keyring),
    и пишутся слотами — так бандл не зависит от того, где они лежали на этой машине.
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
    if sdesk_enabled(cfg):
        raw["sdesk"] = {
            "base_url": cfg.sdesk.base_url,
            "project": cfg.sdesk.project,
            "username": cfg.sdesk.username,
        }
        if cfg.sdesk.proxy_basic_user:
            raw["sdesk"]["proxy_basic_user"] = cfg.sdesk.proxy_basic_user
    if cfg.jenkins.username:
        raw["jenkins"] = {"base_url": cfg.jenkins.base_url, "username": cfg.jenkins.username}

    sec_list: list[dict] = []
    for slot in secrets.SECRET_SLOTS:
        val = get_slot(cfg, slot)
        if val:
            sec_list.append({"slot": slot, "value": val})
    raw["secrets"] = sec_list

    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        tomli_w.dump(raw, fh)
    return len(sec_list)


def read_bundle(path: Path) -> tuple[Config, dict[str, str]]:
    """Прочитать бандл: вернуть (конфиг, секреты по слотам). Ничего никуда не пишет.

    Понимает и старый формат, где секреты были записаны парой (service, account):
    такие записи разворачиваются в слоты по тому же конфигу.
    """
    path = Path(path).expanduser()
    if not path.exists():
        raise ConfigError(f"Файл бандла не найден: {path}")
    if tomllib is None:  # pragma: no cover
        raise ConfigError("tomllib недоступен — нужен Python 3.11+ для чтения бандла")
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    cfg = _apply_raw(Config(), raw)
    by_ref = {slot_keyring_ref(cfg, slot): slot for slot in secrets.SECRET_SLOTS}
    values: dict[str, str] = {}
    for entry in raw.get("secrets", []) or []:
        value = entry.get("value")
        if not value:
            continue
        slot = entry.get("slot")
        if not slot:  # старый формат: (service, account)
            slot = by_ref.get((entry.get("service"), entry.get("account")))
        if slot:
            values[slot] = value
    return cfg, values
