"""Воркспейсы: определение активного контура работы и операции над ним.

Воркспейс — именованный набор папок + флаги подключённых интеграций + свои локальные
данные (работы, фичи, заметки, анализы, снапшоты). Активный воркспейс определяется
БЕЗ участия пользователя, когда это возможно: по текущей рабочей папке.

Порядок резолва (первое сработавшее выигрывает):

1. явное указание — флаг ``-W/--workspace`` (slug или id);
2. переменная окружения ``JWU_WORKSPACE``;
3. текущая папка внутри зарегистрированной папки воркспейса (побеждает самое
   длинное совпадение — так вложенные воркспейсы работают корректно);
4. последний выбранный вручную (``meta['active_workspace']``);
5. единственный воркспейс в БД;
6. иначе — ``WorkspaceNotSelected`` (CLI печатает подсказку, TUI показывает экран выбора).

Cwd-матч намеренно НЕ переписывает ``active_workspace``: иначе ``cd`` в другую папку молча
менял бы дефолт для команд, запускаемых вне зарегистрированных папок.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import secrets as secrets_mod
from .config import Config, _apply_raw, load_config
from .models import Workspace
from .store import DEFAULT_WORKSPACE_SLUG as DEFAULT_SLUG, Store

ACTIVE_META_KEY = "active_workspace"
ENV_VAR = "JWU_WORKSPACE"

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class WorkspaceError(RuntimeError):
    """Проблема с воркспейсом (не найден, некорректный slug и т.п.)."""


class WorkspaceNotSelected(WorkspaceError):
    """Активный воркспейс определить не удалось — нужен выбор пользователя."""


@dataclass
class Resolution:
    """Какой воркспейс выбран и почему (источник важен для подсказок и скиллов)."""

    workspace: Workspace
    source: str  # explicit | env | cwd | active | only
    matched_path: str = ""

    @property
    def source_human(self) -> str:
        return {
            "explicit": "указан явно (-W)",
            "env": f"переменная окружения {ENV_VAR}",
            "cwd": f"текущая папка ({self.matched_path})",
            "active": "последний выбранный",
            "only": "единственный воркспейс",
        }.get(self.source, self.source)


def normalize_path(path: str | Path) -> str:
    """Абсолютный путь без симлинков и хвостового разделителя — так папки сравнимы."""
    return str(Path(path).expanduser().resolve())


def normalize_slug(slug: str) -> str:
    """Проверить slug воркспейса (a-z, 0-9, точка, дефис, подчёркивание)."""
    slug = (slug or "").strip().lower()
    if not _SLUG_RE.match(slug):
        raise WorkspaceError(
            f"Некорректный slug воркспейса: «{slug}». Разрешены латиница в нижнем регистре, "
            "цифры, дефис, точка и подчёркивание; начинаться должен с буквы или цифры."
        )
    return slug


def find_workspace(store: Store, ref: str) -> Workspace | None:
    """Воркспейс по slug либо по числовому id."""
    ref = (ref or "").strip()
    if not ref:
        return None
    ws = store.get_workspace_by_slug(ref.lower())
    if ws:
        return ws
    if ref.isdigit():
        return store.get_workspace(int(ref))
    return None


def workspace_for_path(store: Store, path: str | Path) -> tuple[Workspace, str] | None:
    """Воркспейс, которому принадлежит папка: самое длинное совпадение по пути."""
    try:
        cur = Path(normalize_path(path))
    except OSError:  # папки может уже не быть
        return None
    best: tuple[int, str, int] | None = None  # (глубина, путь, workspace_id)
    for row in store.all_workspace_paths():
        root = Path(row.path)
        if cur == root or _is_relative_to(cur, root):
            depth = len(root.parts)
            if best is None or depth > best[0]:
                best = (depth, row.path, row.workspace_id)
    if best is None:
        return None
    ws = store.get_workspace(best[2])
    return (ws, best[1]) if ws else None


def _is_relative_to(path: Path, root: Path) -> bool:
    # Path.is_relative_to появился в 3.9; держим совместимую реализацию явно
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def resolve(
    store: Store, *, explicit: str | None = None, cwd: str | Path | None = None
) -> Resolution:
    """Определить активный воркспейс (см. порядок в докстринге модуля)."""
    if explicit:
        ws = find_workspace(store, explicit)
        if ws is None:
            known = ", ".join(w.slug for w in store.list_workspaces()) or "— (ни одного)"
            raise WorkspaceError(
                f"Воркспейс «{explicit}» не найден. Известные: {known}. "
                "Создать: jwu workspace create <slug>"
            )
        return Resolution(ws, "explicit")

    env = os.environ.get(ENV_VAR, "").strip()
    if env:
        ws = find_workspace(store, env)
        if ws is None:
            raise WorkspaceError(
                f"{ENV_VAR}={env}: такого воркспейса нет. Список: jwu workspace list"
            )
        return Resolution(ws, "env")

    match = workspace_for_path(store, cwd or Path.cwd())
    if match is not None:
        ws, path = match
        return Resolution(ws, "cwd", matched_path=path)

    active = store.get_meta(ACTIVE_META_KEY)
    if active:
        ws = find_workspace(store, active)
        if ws is not None:
            return Resolution(ws, "active")

    all_ws = store.list_workspaces()
    if len(all_ws) == 1:
        return Resolution(all_ws[0], "only")

    raise WorkspaceNotSelected(
        "Не понял, в каком воркспейсе работать: текущая папка ни к одному не привязана. "
        "Выбрать: jwu workspace use <slug>   ·   привязать папку: jwu workspace add-path . "
        "  ·   список: jwu workspace list"
    )


def resolve_workspace(
    store: Store, *, explicit: str | None = None, cwd: str | Path | None = None
) -> Workspace:
    """Как ``resolve``, но сразу возвращает воркспейс (когда источник не важен)."""
    return resolve(store, explicit=explicit, cwd=cwd).workspace


def set_active(store: Store, workspace: Workspace) -> None:
    """Запомнить воркспейс как активный (используется вне зарегистрированных папок)."""
    store.set_meta(ACTIVE_META_KEY, workspace.slug)


def add_path(
    store: Store, workspace: Workspace, path: str | Path, label: str = ""
) -> tuple[str, str | None]:
    """Привязать папку к воркспейсу.

    Возвращает ``(нормализованный путь, предупреждение | None)``. Несуществующая папка —
    не ошибка (может быть на внешнем диске), но о ней сообщаем. Папка, уже привязанная
    к другому воркспейсу, — ошибка: иначе резолв по cwd стал бы неоднозначным.
    """
    norm = normalize_path(path)
    for row in store.all_workspace_paths():
        if row.path == norm:
            if row.workspace_id == workspace.id:
                return norm, "папка уже привязана к этому воркспейсу"
            other = store.get_workspace(row.workspace_id)
            raise WorkspaceError(
                f"Папка {norm} уже принадлежит воркспейсу «{other.slug if other else '?'}». "
                "Сначала отвяжи её: jwu workspace remove-path"
            )
    store.add_workspace_path(workspace.id, norm, label)
    warn = None if Path(norm).exists() else "папки сейчас нет на диске"
    return norm, warn


# --------------------------------------------------------------------------- #
# Конфиг воркспейса (настройки в БД, секреты — там же)
# --------------------------------------------------------------------------- #

# Несекретные поля конфига, которые хранятся в workspace_settings. Плоский список
# «путь в Config» — чтобы добавление поля не требовало ALTER TABLE.
_SETTING_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("jira.base_url", "jira", "base_url"),
    ("jira.username", "jira", "username"),
    ("jira.project", "jira", "project"),
    ("jira.proxy_basic_user", "jira", "proxy_basic_user"),
    ("sdesk.base_url", "sdesk", "base_url"),
    ("sdesk.project", "sdesk", "project"),
    ("sdesk.username", "sdesk", "username"),
    ("sdesk.proxy_basic_user", "sdesk", "proxy_basic_user"),
    ("bitbucket.base_url", "bitbucket", "base_url"),
    ("bitbucket.project", "bitbucket", "project"),
    ("bitbucket.repo", "bitbucket", "repo"),
    ("jenkins.base_url", "jenkins", "base_url"),
    ("jenkins.username", "jenkins", "username"),
)

LEGACY_MIGRATED_META = "workspaces.legacy_migrated"


def _settings_to_raw(settings: dict[str, str]) -> dict:
    """Плоский KV ('jira.base_url') → вложенный dict, как из tomllib.load(config.toml).

    Так переиспользуется весь разбор из config._apply_raw: дефолты, rstrip('/'), merge views.
    """
    raw: dict = {}
    for key, value in settings.items():
        parts = key.split(".")
        if len(parts) < 2 or parts[0] not in ("jira", "sdesk", "bitbucket", "jenkins"):
            continue  # служебные ключи воркспейса (features.seq и пр.) — не конфиг
        node = raw
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return raw


def config_for_workspace(store: Store, workspace: Workspace) -> Config:
    """Собрать Config воркспейса: настройки и секреты — из БД, путь до БД — глобальный."""
    cfg = _apply_raw(Config(), _settings_to_raw(store.workspace_settings(workspace.id)))
    # storage остаётся глобальным: БД надо найти ДО того, как известен воркспейс
    cfg.storage.db_path = load_config().storage.db_path
    cfg.secrets = secrets_mod.DbSecrets(store, workspace.id)
    return cfg


def save_workspace_config(store: Store, workspace: Workspace, cfg: Config) -> None:
    """Записать несекретные поля конфига в настройки воркспейса."""
    values = {key: str(getattr(getattr(cfg, section), attr) or "")
              for key, section, attr in _SETTING_FIELDS}
    for name, jql in (cfg.jira.views or {}).items():
        values[f"jira.views.{name}"] = jql
    store.set_workspace_settings(workspace.id, values)


def migrate_legacy_config(store: Store, workspace: Workspace | None = None) -> tuple[int, int]:
    """Перенести глобальный config.toml + секреты из keyring в воркспейс.

    Возвращает (перенесено настроек, перенесено секретов). Повторный вызов ничего не
    делает — сторожевой флаг в meta. Keyring НЕ чистим: откат на старую версию jwu
    должен оставаться возможным.
    """
    if store.get_meta(LEGACY_MIGRATED_META) == "1":
        return (0, 0)
    workspace = workspace or store.get_workspace_by_slug(DEFAULT_SLUG)
    if workspace is None:
        return (0, 0)

    cfg = load_config()  # глобальный config.toml + KeyringSecrets
    save_workspace_config(store, workspace, cfg)
    moved = 0
    for slot in secrets_mod.SECRET_SLOTS:
        ref = _keyring_ref(cfg, slot)
        if ref is None:
            continue
        value = secrets_mod.get_secret(*ref)
        if value:
            store.set_workspace_secret(workspace.id, slot, value)
            moved += 1
    store.set_meta(LEGACY_MIGRATED_META, "1")
    return (len(_SETTING_FIELDS), moved)


def _keyring_ref(cfg: Config, slot: str):
    from .config import slot_keyring_ref

    return slot_keyring_ref(cfg, slot)


def create(
    store: Store, slug: str, *, name: str = "", jira: bool = False, bitbucket: bool = False,
    paths: list[str] | None = None,
) -> Workspace:
    """Создать воркспейс и сразу привязать к нему папки."""
    slug = normalize_slug(slug)
    if store.get_workspace_by_slug(slug) is not None:
        raise WorkspaceError(f"Воркспейс «{slug}» уже есть.")
    ws = store.create_workspace(
        slug, name=name or slug, jira_enabled=jira, bitbucket_enabled=bitbucket
    )
    for path in paths or []:
        add_path(store, ws, path)
    refreshed = store.get_workspace(ws.id)
    return refreshed or ws
