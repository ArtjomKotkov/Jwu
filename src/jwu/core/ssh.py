"""SSH-стенды воркспейса и их выдача Claude Code через ssh-mcp.

Стенд — сервер, куда разработчик ходит смотреть логи и состояние: dev/test-стенд,
препрод. Сам jwu по SSH не ходит. Он хранит описания стендов в настройках контура
(``ssh.server.<имя>`` — JSON без секретов), собирает из них конфиг для
`ssh-mcp <https://github.com/overklassniy/ssh-mcp>`_ и регистрирует этот MCP-сервер
в Claude Code для папок воркспейса — так стенды рабочего контура не видны в личном.

Почему настройки, а не своя таблица: описаний мало, миграция схемы не нужна, а синк
памяти (``memory.export_memory``) уносит настройки контура на другую машину сам.
Пароль и passphrase — секреты воркспейса (``ssh.<имя>.password``), в память не едут.

ssh-mcp в режиме TOML-конфига НЕ берёт пароль из окружения — только из файла. Поэтому
конфиг пишется в каталог данных jwu с правами 600, а в проектные репозитории не попадает
никогда. Лучше вообще обходиться ключом или ssh-agent.

Политика команд по умолчанию — «только чтение» (``readonly``): whitelist просмотра
логов и состояния плюс blacklist цепочек и перенаправлений. Это страховка от случайной
команды модели, а не песочница: права на сервере всё равно решают, что можно.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .config import data_dir

if TYPE_CHECKING:
    from .store import Store

SETTING_PREFIX = "ssh.server."
SECRET_SLOTS = ("password", "passphrase")
POLICIES = ("readonly", "none")
TRANSPORTS = ("exec", "shell")

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")

# Команды просмотра: логи, файлы, процессы, место на диске, сервисы и контейнеры.
READONLY_WHITELIST = [
    r"^(cat|head|tail|less|grep|egrep|fgrep|zgrep|zcat|ls|find|stat|wc|du|df|free|uptime"
    r"|ps|top -b -n 1|whoami|hostname|date|id|uname)( |$)",
    r"^journalctl( |$)",
    r"^systemctl (status|is-active|is-enabled|list-units|show)( |$)",
    r"^docker (ps|logs|inspect|images|stats --no-stream)( |$)",
    r"^kubectl (get|describe|logs|top)( |$)",
]
# Whitelist проверяет только начало строки, поэтому цепочки и подстановки режем отдельно:
# иначе `tail x; rm -rf ~` прошёл бы по `^tail`. Пайп разрешён (grep | tail), но не в
# интерпретатор. find — без -exec/-delete.
READONLY_BLACKLIST = [
    r"[;&`]",
    r"\$\(",
    r">",
    r"\|\s*(sudo|sh|bash|zsh|dash|python\d?|perl|ruby|node|xargs|tee|dd)\b",
    r"\s-(exec|execdir|delete|ok|okdir|fprint\w*)\b",
    r"\bsudo\b",
]


class SshError(ValueError):
    pass


@dataclass
class SshServer:
    name: str
    host: str
    username: str
    port: int = 22
    private_key: str = ""
    agent: str = ""            # "env" — ssh-agent из $SSH_AUTH_SOCK, либо путь к сокету
    policy: str = "readonly"   # readonly | none
    whitelist: list[str] = field(default_factory=list)   # сверх пресета
    blacklist: list[str] = field(default_factory=list)
    allowed_remote_paths: list[str] = field(default_factory=list)
    allowed_local_paths: list[str] = field(default_factory=list)
    transport: str = "exec"
    tag: str = ""              # тег папки воркспейса, к которой относится стенд
    description: str = ""

    def validate(self) -> None:
        if not _NAME_RE.match(self.name):
            raise SshError(f"Имя стенда «{self.name}»: латиница в нижнем регистре, цифры, . _ -")
        if not self.host or not self.username:
            raise SshError("У стенда обязательны host и username.")
        if not 1 <= int(self.port) <= 65535:
            raise SshError(f"Порт {self.port} вне 1–65535.")
        if self.policy not in POLICIES:
            raise SshError(f"Политика «{self.policy}»: одна из {', '.join(POLICIES)}.")
        if self.transport not in TRANSPORTS:
            raise SshError(f"Транспорт «{self.transport}»: один из {', '.join(TRANSPORTS)}.")
        for pattern in self.whitelist + self.blacklist:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise SshError(f"Кривой regex «{pattern}»: {exc}") from exc
        for path in self.allowed_remote_paths:
            if not path.startswith("/"):
                raise SshError(f"Удалённый путь «{path}» должен быть абсолютным.")

    def effective_whitelist(self) -> list[str]:
        base = READONLY_WHITELIST if self.policy == "readonly" else []
        return base + self.whitelist

    def effective_blacklist(self) -> list[str]:
        base = READONLY_BLACKLIST if self.policy == "readonly" else []
        return base + self.blacklist

    def as_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Хранение
# --------------------------------------------------------------------------- #


def _secret_slot(name: str, kind: str) -> str:
    return f"ssh.{name}.{kind}"


def list_servers(store: "Store", workspace_id: int) -> list[SshServer]:
    out: list[SshServer] = []
    known = set(SshServer.__dataclass_fields__)
    for key, raw in store.workspace_settings(workspace_id).items():
        if not key.startswith(SETTING_PREFIX):
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        # Поля, которых эта версия не знает (запись с более новой машины), не роняют чтение.
        out.append(SshServer(**{k: v for k, v in data.items() if k in known}))
    return sorted(out, key=lambda s: s.name)


def get_server(store: "Store", workspace_id: int, name: str) -> SshServer:
    for server in list_servers(store, workspace_id):
        if server.name == name:
            return server
    raise SshError(f"Стенда «{name}» в воркспейсе нет (список — jwu ssh list).")


def save_server(store: "Store", workspace_id: int, server: SshServer, *,
                password: str | None = None, passphrase: str | None = None) -> SshServer:
    """Записать стенд (новый или поверх старого). Секрет None — не трогать, "" — стереть."""
    server.validate()
    for kind, value in (("password", password), ("passphrase", passphrase)):
        if value is None:
            continue
        if value:
            store.set_workspace_secret(workspace_id, _secret_slot(server.name, kind), value)
        else:
            store.delete_workspace_secret(workspace_id, _secret_slot(server.name, kind))
    if not (server.private_key or server.agent
            or store.get_workspace_secret(workspace_id, _secret_slot(server.name, "password"))):
        raise SshError("Нужен способ входа: --key, --agent или --password.")
    store.set_workspace_settings(
        workspace_id, {SETTING_PREFIX + server.name: json.dumps(server.as_dict(), ensure_ascii=False)}
    )
    return server


def remove_server(store: "Store", workspace_id: int, name: str) -> None:
    get_server(store, workspace_id, name)
    store.delete_workspace_settings(workspace_id, [SETTING_PREFIX + name])
    for kind in SECRET_SLOTS:
        store.delete_workspace_secret(workspace_id, _secret_slot(name, kind))


def has_password(store: "Store", workspace_id: int, name: str) -> bool:
    return bool(store.get_workspace_secret(workspace_id, _secret_slot(name, "password")))


# --------------------------------------------------------------------------- #
# Конфиг ssh-mcp
# --------------------------------------------------------------------------- #


def config_path(slug: str) -> Path:
    return data_dir() / "ssh" / f"{slug}.toml"


def _toml_str(value: str) -> str:
    # JSON-строка — валидная базовая строка TOML (те же экранирования, \uXXXX допустим).
    return json.dumps(value, ensure_ascii=False)


def _toml_list(values: list[str]) -> str:
    return "[" + ", ".join(_toml_str(v) for v in values) + "]"


def render_config(servers: list[SshServer], secrets: dict[str, str]) -> str:
    """TOML для ssh-mcp. ``secrets`` — слоты секретов воркспейса (ssh.<имя>.password …)."""
    lines = [
        "# Сгенерировано jwu (jwu ssh config) — руками не править, перезапишется.",
        "",
        "[defaults]",
        'command_timeout = "30s"',
        'connection_timeout = "30s"',
        "max_output_bytes = 1048576",
    ]
    for s in servers:
        lines += ["", "[[server]]",
                  f"name = {_toml_str(s.name)}",
                  f"host = {_toml_str(s.host)}",
                  f"port = {int(s.port)}",
                  f"username = {_toml_str(s.username)}"]
        if s.private_key:
            lines.append(f"private_key = {_toml_str(s.private_key)}")
        if s.agent:
            lines.append(f"agent = {_toml_str(s.agent)}")
        for kind in SECRET_SLOTS:
            value = secrets.get(_secret_slot(s.name, kind))
            if value:
                lines.append(f"{kind} = {_toml_str(value)}")
        if s.transport != "exec":
            lines.append(f"transport = {_toml_str(s.transport)}")
        for key, values in (("whitelist", s.effective_whitelist()),
                            ("blacklist", s.effective_blacklist()),
                            ("allowed_remote_paths", s.allowed_remote_paths),
                            ("allowed_local_paths", s.allowed_local_paths)):
            if values:
                lines.append(f"{key} = {_toml_list(values)}")
    return "\n".join(lines) + "\n"


def write_config(store: "Store", workspace_id: int, slug: str) -> Path:
    """Перезаписать конфиг ssh-mcp контура (права 600: внутри могут быть пароли).

    ssh-mcp сам перечитывает файл при изменении, так что правка стенда подхватывается
    без перезапуска сессии.
    """
    path = config_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = render_config(list_servers(store, workspace_id), store.workspace_secrets(workspace_id))
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return path


# --------------------------------------------------------------------------- #
# Регистрация в Claude Code
# --------------------------------------------------------------------------- #


def mcp_name(slug: str) -> str:
    """Имя MCP-сервера в Claude Code; инструменты придут как mcp__ssh-<slug>__execute-command."""
    return f"ssh-{slug}"


def find_binary() -> str | None:
    found = shutil.which("ssh-mcp")
    if found:
        return found
    gobin = Path(os.environ.get("GOBIN") or Path(os.environ.get("GOPATH") or Path.home() / "go") / "bin")
    candidate = gobin / "ssh-mcp"
    return str(candidate) if candidate.is_file() else None


INSTALL_HINT = ("ssh-mcp не найден. Поставь: go install github.com/overklassniy/ssh-mcp/cmd/ssh-mcp@latest "
                "(нужен Go 1.26+), либо бинарь из Releases на GitHub.")


def mcp_add_args(slug: str, binary: str, config: Path) -> list[str]:
    """`claude mcp add` в скоупе local: сервер виден только в ЭТОЙ папке проекта."""
    return ["claude", "mcp", "add", "--scope", "local", mcp_name(slug), "--",
            binary, "--config", str(config)]


@dataclass
class InstallResult:
    path: str
    ok: bool
    message: str = ""


def install_mcp(slug: str, paths: list[str], config: Path, *, binary: str | None = None,
                dry_run: bool = False) -> list[InstallResult]:
    """Зарегистрировать ssh-mcp контура в каждой папке воркспейса (перезаписав старую запись)."""
    binary = binary or find_binary()
    if not binary:
        raise SshError(INSTALL_HINT)
    if not shutil.which("claude") and not dry_run:
        raise SshError("CLI claude не найден в PATH — добавь сервер вручную:\n  "
                       + " ".join(mcp_add_args(slug, binary, config)))
    results: list[InstallResult] = []
    for path in paths:
        args = mcp_add_args(slug, binary, config)
        if dry_run:
            results.append(InstallResult(path, True, " ".join(args)))
            continue
        if not Path(path).is_dir():
            results.append(InstallResult(path, False, "папки нет на диске"))
            continue
        # Повторная установка: старую запись убрать, иначе `mcp add` откажет «already exists».
        subprocess.run(["claude", "mcp", "remove", "--scope", "local", mcp_name(slug)],
                       cwd=path, capture_output=True, text=True)
        proc = subprocess.run(args, cwd=path, capture_output=True, text=True)
        out = (proc.stdout or proc.stderr or "").strip()
        results.append(InstallResult(path, proc.returncode == 0, out.splitlines()[-1] if out else ""))
    return results


def uninstall_mcp(slug: str, paths: list[str]) -> list[InstallResult]:
    if not shutil.which("claude"):
        raise SshError("CLI claude не найден в PATH.")
    results: list[InstallResult] = []
    for path in paths:
        if not Path(path).is_dir():
            results.append(InstallResult(path, False, "папки нет на диске"))
            continue
        proc = subprocess.run(["claude", "mcp", "remove", "--scope", "local", mcp_name(slug)],
                              cwd=path, capture_output=True, text=True)
        out = (proc.stdout or proc.stderr or "").strip()
        results.append(InstallResult(path, proc.returncode == 0, out.splitlines()[-1] if out else ""))
    return results


def auth_label(server: SshServer, has_pw: bool = False) -> str:
    """Чем входим — для карточек: путь к ключу, агент или пароль."""
    parts = []
    if server.private_key:
        parts.append(f"ключ {server.private_key}")
    if server.agent:
        parts.append("ssh-agent" if server.agent == "env" else f"ssh-agent {server.agent}")
    if has_pw:
        parts.append("пароль")
    return " + ".join(parts) or "—"


def brief(server: SshServer, has_pw: bool = False) -> dict:
    """Короткая карточка стенда для контекста агента (полная — describe / jwu_ssh_servers)."""
    return {"name": server.name, "address": f"{server.username}@{server.host}:{server.port}",
            "auth": auth_label(server, has_pw), "policy": server.policy, "tag": server.tag,
            "description": server.description}


def detail_rows(server: SshServer, has_pw: bool = False, *,
                compact: bool = False) -> list[tuple[str, list[str]]]:
    """Секция стенда для карточки воркспейса: (подпись, значения). Пустое опущено.

    Списки — итоговые, как их увидит ssh-mcp: пресет политики плюс свои правила.
    ``compact`` сворачивает пресет в одну строку (свои правила — полностью): в шапке
    дашборда 11 regex пресета на каждый стенд вытеснили бы дерево папок.
    """
    if compact and server.policy == "readonly":
        whitelist = [f"пресет readonly ({len(READONLY_WHITELIST)})", *server.whitelist]
        blacklist = [f"пресет readonly ({len(READONLY_BLACKLIST)})", *server.blacklist]
    else:
        whitelist = server.effective_whitelist() or ["— (любые команды)"]
        blacklist = server.effective_blacklist()
    rows: list[tuple[str, list[str]]] = [
        ("адрес", [f"{server.username}@{server.host}:{server.port}"]),
        ("вход", [auth_label(server, has_pw)]),
        ("политика", [server.policy + ("" if server.transport == "exec" else f" · транспорт {server.transport}")]),
        ("whitelist", whitelist),
        ("blacklist", blacklist),
        ("SFTP на сервере", server.allowed_remote_paths),
        ("SFTP локально", server.allowed_local_paths),
    ]
    return [(label, values) for label, values in rows if values]


def describe(store: "Store", workspace_id: int, slug: str) -> dict:
    """Что знает jwu о стендах контура — для MCP и скиллов (без секретов)."""
    servers = list_servers(store, workspace_id)
    return {
        "mcp_server": mcp_name(slug),
        "tools_prefix": f"mcp__{mcp_name(slug)}__",
        "config": str(config_path(slug)),
        "config_exists": config_path(slug).exists(),
        "binary": find_binary() or "",
        "servers": [
            {**s.as_dict(),
             "whitelist": s.effective_whitelist(), "blacklist": s.effective_blacklist(),
             "auth": ("key" if s.private_key else "agent" if s.agent else "password"),
             "has_password": has_password(store, workspace_id, s.name)}
            for s in servers
        ],
    }
