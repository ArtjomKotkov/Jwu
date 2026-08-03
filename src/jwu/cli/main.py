"""CLI (Typer). Каждая команда — тонкая обёртка над Service. Везде есть --json для Claude."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import click
import typer
from rich.console import Console, Group
from rich.table import Table

from ..core.bitbucket import BitbucketError
from ..core.github import GitHubError
from ..core import secrets
from ..core.config import ConfigError, db_path, load_config, save_config
from ..core.dates import fmt_ago, fmt_dt
from ..core.maintenance import (
    AUTO_PRUNE_DAYS, ensure_db_available, run_daily_maintenance, run_daily_prune,
    warn_if_cloud_path,
)
from ..skills_install import (
    default_agents_dest as _agents_dest,
    default_dest as _skills_dest,
    install_agents,
    install_skills,
)
from ..core.jira import JiraError
from ..core import models
from ..core.models import (
    JOB_RECORD_BADGES, JOB_RECORD_KINDS, LOCAL_FEATURE_BADGES, LOCAL_FEATURE_STATUSES,
    WORKSPACE_RULE_BADGES, WORKSPACE_RULE_KINDS,
    Delta, Issue, Job, LocalFeature, PR, Workspace,
)
from ..core.service import DashboardData, DayContext, Service, dashboard_from_memory
from .dashboard import render_jira_text
from ..core.store import Store
from ..core import workspaces

app = typer.Typer(
    add_completion=False,
    help="Jira + Bitbucket CLI с памятью, для интеграции с Claude Code.",
    no_args_is_help=True,
)
auth_app = typer.Typer(help="Проверка доступа.")
app.add_typer(auth_app, name="auth")
action_app = typer.Typer(help="Действия для Claude Code (контекст + промпт).")
app.add_typer(action_app, name="action")
job_app = typer.Typer(help="Работы (jobs): цикл работы над задачей, прогресс и связи с PR.")
app.add_typer(job_app, name="job")
workspace_app = typer.Typer(
    help="Воркспейсы: контуры работы (папки + свои интеграции и данные)."
)
app.add_typer(workspace_app, name="workspace")
db_app = typer.Typer(help="Файл БД: сколько занимает, чистка старых снапшотов, VACUUM.")
app.add_typer(db_app, name="db")
feature_app = typer.Typer(help="Локальные фичи: мини-трекер воркспейса, когда Jira нет.")
app.add_typer(feature_app, name="feature")
rule_app = typer.Typer(
    help="Правила воркспейса: запреты, инструкции и общая инфа для агента."
)
app.add_typer(rule_app, name="rule")

console = Console()
err = Console(stderr=True)

# Значение глобального флага --workspace/-W: заполняется корневым колбэком ДО команды
# и читается фабриками _store()/_service(). Через параметр не пробросить — команд много.
_WORKSPACE_ARG: Optional[str] = None


@app.callback()
def _root(
    workspace: Optional[str] = typer.Option(
        None, "--workspace", "-W", envvar=workspaces.ENV_VAR,
        help="Воркспейс (slug или id). По умолчанию определяется по текущей папке.",
    ),
) -> None:
    """jwu — Jira + Bitbucket с памятью, разложенной по воркспейсам."""
    global _WORKSPACE_ARG
    _WORKSPACE_ARG = workspace


def _prepare_db() -> None:
    """Защита от iCloud-плейсхолдера + ежедневный локальный бэкап. Сообщения — в stderr
    (чтобы не ломать JSON на stdout). Бросает ConfigError, если БД выгружена из iCloud."""
    path = db_path()
    ensure_db_available(path)
    for msg in run_daily_maintenance(path):
        err.print(f"[dim]{msg}[/dim]")


def _open_store() -> Store:
    """Store без скоупа воркспейса — для команд про сами воркспейсы."""
    try:
        _prepare_db()
    except ConfigError as exc:
        err.print(f"[red]Ошибка БД:[/red] {exc}")
        raise typer.Exit(code=1)
    store = Store(str(db_path()))
    # Чистка снапшотов — строго ПОСЛЕ бэкапа из _prepare_db (она необратима) и только
    # раз в день на разросшейся базе; на обычной молча возвращает пустой список.
    for msg in run_daily_prune(store, db_path()):
        err.print(f"[dim]{msg}[/dim]")
    return store


def _resolve_workspace(store: Store) -> Workspace:
    """Активный воркспейс для текущего вызова (или внятный отказ).

    Здесь же однократно доезжает legacy-конфиг: старый config.toml + секреты из keyring
    переносятся в воркспейс «Работа», чтобы обновление jwu не потребовало ручных действий.
    """
    _migrate_legacy_once(store)
    try:
        return workspaces.resolve_workspace(store, explicit=_WORKSPACE_ARG)
    except workspaces.WorkspaceError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)


def _migrate_legacy_once(store: Store) -> None:
    try:
        fields, moved = workspaces.migrate_legacy_config(store)
    except Exception as exc:  # noqa: BLE001 — недоступный keyring не должен ломать команду
        err.print(f"[yellow]⚠ не удалось перенести старый конфиг в БД: {exc}[/yellow]")
        return
    if fields or moved:
        err.print(f"[dim]Старый конфиг перенесён в воркспейс «work» "
                  f"(секретов: {moved}). Keyring не тронут.[/dim]")


def _service() -> Service:
    """Сервис активного воркспейса. Клиенты создаются только для его интеграций."""
    try:
        _prepare_db()
        with Store(str(db_path())) as probe:
            ws = _resolve_workspace(probe)
        return Service.for_workspace(ws)
    except ConfigError as exc:
        err.print(f"[red]Ошибка конфига:[/red] {exc}")
        raise typer.Exit(code=1)
    except (JiraError, BitbucketError, GitHubError) as exc:
        err.print(f"[red]Ошибка авторизации:[/red] {exc}")
        raise typer.Exit(code=1)


def _builds_service() -> Service:
    """Сервис для команд сборок: хостинг + CI, без зависимости от трекера задач."""
    try:
        _prepare_db()
        with Store(str(db_path())) as probe:
            ws = _resolve_workspace(probe)
        _require_prs(ws)
        return Service.builds_for_workspace(ws)
    except ConfigError as exc:
        err.print(f"[red]Ошибка конфига:[/red] {exc}")
        raise typer.Exit(code=1)
    except (JiraError, BitbucketError, GitHubError) as exc:
        err.print(f"[red]Ошибка авторизации:[/red] {exc}")
        raise typer.Exit(code=1)


def _require_tasks(svc: Service) -> None:
    """Отказать внятно, если команда требует трекер задач, а контур локальный."""
    if svc.tasks_client is not None:
        return
    slug = svc.workspace.slug if svc.workspace else "?"
    err.print(
        f"[red]В воркспейсе «{slug}» нет провайдера задач[/red] (контур локальный).\n"
        f"Подключить: [cyan]jwu workspace provider -W {slug} jira|github[/cyan]\n"
        f"Локальные фичи: [cyan]jwu feature list[/cyan]   ·   "
        f"работа без задачи: [cyan]jwu job start --title \"…\"[/cyan]"
    )
    svc.close()
    raise typer.Exit(code=1)


# Историческое имя: команды «требуют Jira» ровно в том смысле, что им нужен трекер задач.
_require_jira = _require_tasks


def _require_prs(ws: Workspace) -> None:
    """Отказать внятно, если команда требует PR, а брать их в воркспейсе неоткуда."""
    if ws.prs_enabled:
        return
    if ws.provider == "jira":
        hint = f"jwu configure -W {ws.slug} --bitbucket-token …"
    else:
        hint = f"jwu workspace provider -W {ws.slug} github"
    err.print(
        f"[red]В воркспейсе «{ws.slug}» не подключён хостинг репозиториев[/red] "
        f"(провайдер: {ws.provider_label}).\nПодключить: [cyan]{hint}[/cyan]"
    )
    raise typer.Exit(code=1)


def _service_with_jira() -> Service:
    """Сервис для команд, которым обязателен трекер задач."""
    svc = _service()
    _require_tasks(svc)
    return svc


def _service_with_prs() -> Service:
    """Сервис для команд, которым обязателен хостинг репозиториев (PR)."""
    svc = _service()
    if svc.pr_client is None:
        ws = svc.workspace
        svc.close()
        _require_prs(ws or Workspace(slug="?"))
    return svc


def _store() -> Store:
    """Только память — без токенов/сети (для note/notes/changes), в скоупе воркспейса."""
    store = _open_store()
    store.use_workspace(_resolve_workspace(store).id)
    return store


def _emit_json(payload: object) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


_ATTACH_ICON = {"image": "🖼", "log": "📄", "doc": "📕", "archive": "🗜", "video": "🎬", "other": "📎"}
_ATTACH_RU = {"image": "изображения", "log": "логи/текст", "doc": "документы",
              "archive": "архивы", "video": "видео", "other": "прочие"}


def _human_size(n: int) -> str:
    size = float(n or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024 or unit == "ГБ":
            return f"{int(size)} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{int(n)} Б"


def _attach_counts(attachments: list) -> dict[str, int]:
    """Сводка количества вложений по видам (image/log/doc/archive/video/other)."""
    counts: dict[str, int] = {}
    for a in attachments:
        counts[a.kind] = counts.get(a.kind, 0) + 1
    return counts


def _render_attachments(attachments: list) -> None:
    """Секция «Вложения» в выводе jwu task / jwu attachments."""
    if not attachments:
        return
    counts = _attach_counts(attachments)
    summary = ", ".join(f"{_ATTACH_RU.get(k, k)}: {n}" for k, n in sorted(counts.items()))
    console.print(f"\n[bold]Вложения ({len(attachments)})[/bold]  [dim]{summary}[/dim]")
    for a in attachments:
        icon = _ATTACH_ICON.get(a.kind, "📎")
        console.print(f"  {icon} {a.filename} [dim]{_human_size(a.size)} · {a.kind}"
                      f" · {a.author} · {fmt_dt(a.created)}[/dim]")


_BUILD_ICON = {"SUCCESSFUL": "[green]✅[/green]", "FAILED": "[red]❌[/red]",
               "INPROGRESS": "[yellow]🔄[/yellow]"}


def _build_icon(state: str) -> str:
    return _BUILD_ICON.get(state, "[dim]•[/dim]")


def _render_builds(builds: list) -> None:
    """Секция «Сборки» в выводе jwu pr (статусы CI по head-коммиту)."""
    if not builds:
        return
    console.print(f"\n[bold]Сборки ({len(builds)})[/bold]")
    for b in builds:
        desc = f" [dim]{b.description}[/dim]" if b.description else ""
        console.print(f"  {_build_icon(b.state)} {b.name or b.key}{desc}")
        if b.url:
            console.print(f"      [dim]{b.url}[/dim]")


# --------------------------------------------------------------------------- #
# auth
# --------------------------------------------------------------------------- #


@auth_app.command("check")
def auth_check(json_out: bool = typer.Option(False, "--json", help="Вывести JSON.")) -> None:
    """Проверить доступы контура (какие именно — зависит от его провайдера)."""
    with _service() as svc:
        result = svc.auth_check()
    if json_out:
        _emit_json(result)
        # Jenkins опционален и на вердикт не влияет: сборки видны и без него.
        required = [r for name, r in result.items() if name != "jenkins"]
        raise typer.Exit(code=0 if all(r["ok"] for r in required) else 1)
    if not result:
        console.print("[dim]Локальный контур: внешних доступов нет — проверять нечего.[/dim]")
        raise typer.Exit(code=0)
    ok = True
    # Jenkins опционален — печатаем, если настроен, но на код выхода не влияет.
    for name in ("jira", "sdesk", "bitbucket", "github", "jenkins"):
        r = result.get(name)
        if r is None:
            continue
        if r["ok"]:
            extra = f" ({r.get('name')})" if r.get("name") else ""
            console.print(f"[green]✓[/green] {name}{extra}")
        else:
            if name != "jenkins":
                ok = False
            console.print(f"[red]✗[/red] {name}: {r.get('error')}")
    raise typer.Exit(code=0 if ok else 1)


def _prompt_default(label: str, current: str, *, secret: bool = False) -> str:
    """Спросить значение с дефолтом из текущего. Для секретов ввод скрыт.
    Пустой ввод => оставить текущее (для секрета — не менять)."""
    if secret:
        shown = "задан" if current else "не задан"
        return typer.prompt(f"{label} ({shown}, Enter — оставить)",
                            default="", hide_input=True, show_default=False)
    return typer.prompt(label, default=current)


configure_app = typer.Typer(
    invoke_without_command=True,
    help="Настройка jwu: хосты/логины в config.toml, секреты в keyring. "
         "Без подкоманды — интерактивный визард. export/import — перенос между машинами.",
)
app.add_typer(configure_app, name="configure")


def _enable_configured_integrations(store: Store, ws: Workspace, cfg) -> None:
    """Включить то, что пользователь реально настроил, — в рамках провайдера контура.

    Признак «настроил» — непустой секрет плюс хост, отличный от заглушки в дефолтах
    Config (jira.example.com/git.example.com): иначе `jwu configure` без единого флага
    включал бы интеграции всем подряд. Провайдера здесь НЕ переключаем: это осознанный
    выбор (`jwu workspace provider`), а не побочный эффект того, что ввели токен.
    """
    from ..core.config import BitbucketConfig, JiraConfig

    stored = store.workspace_secrets(ws.id)
    if ws.provider != "jira":
        return
    updates: dict[str, bool] = {}
    bb_ready = (cfg.bitbucket.base_url and cfg.bitbucket.base_url != BitbucketConfig().base_url
                and stored.get("bitbucket.token"))
    if bb_ready and not ws.bitbucket_enabled:
        updates["bitbucket_enabled"] = True
    jira_ready = (cfg.jira.base_url and cfg.jira.base_url != JiraConfig().base_url
                  and (stored.get("jira.token") or stored.get("jira.password")))
    if updates:
        store.update_workspace(ws.id, **updates)
        console.print(f"[green]Подключено к воркспейсу «{ws.label}»:[/green] Bitbucket")
    elif jira_ready and not stored.get("bitbucket.token"):
        console.print("[dim]Bitbucket не настроен — вкладки PR останутся пустыми.[/dim]")


def _enable_provider_if_configured(store: Store, ws: Workspace, cfg) -> Workspace:
    """Поднять провайдера у ЛОКАЛЬНОГО контура, если для него завезли креды.

    Локальный контур — это «ещё не выбрали»; если человек в нём настроил GitHub или Jira,
    молчать и оставлять контур локальным было бы издевательством. У контура с уже
    выбранным провайдером ничего не меняем.
    """
    from ..core.config import JiraConfig

    if ws.provider != "local":
        return ws
    stored = store.workspace_secrets(ws.id)
    if cfg.github.owner and stored.get("github.token"):
        provider = "github"
    elif (cfg.jira.base_url and cfg.jira.base_url != JiraConfig().base_url
          and (stored.get("jira.token") or stored.get("jira.password"))):
        provider = "jira"
    else:
        return ws
    updated = workspaces.set_provider(store, ws, provider)
    console.print(f"[green]Провайдер воркспейса «{ws.label}»:[/green] {updated.provider_label}")
    return updated


def _auth_check_report() -> None:
    """Проверить связь по текущему конфигу и напечатать ✓/✗ по системам провайдера."""
    try:
        with _open_store() as store:
            ws = _resolve_workspace(store)
        if ws.provider == "local":
            return  # локальному контуру проверять нечего
        with Service.for_workspace(ws) as svc:
            res = svc.auth_check()
        for name in ("jira", "sdesk", "bitbucket", "github", "jenkins"):
            r = res.get(name)
            if r is None:
                continue
            mark = "[green]✓[/green]" if r["ok"] else "[red]✗[/red]"
            extra = f" {r.get('error')}" if not r["ok"] else (
                f" ({r.get('name')})" if r.get("name") else "")
            console.print(f"{mark} {name}{extra}")
    except Exception as exc:  # noqa: BLE001
        err.print(f"[yellow]Проверка связи не удалась:[/yellow] {exc}")


@configure_app.callback(invoke_without_command=True)
def configure_main(
    ctx: typer.Context,
    non_interactive: bool = typer.Option(False, "--non-interactive",
        help="Не спрашивать; брать значения только из флагов."),
    jira_host: Optional[str] = typer.Option(None, "--jira-host"),
    jira_user: Optional[str] = typer.Option(None, "--jira-user"),
    jira_project: Optional[str] = typer.Option(None, "--jira-project"),
    jira_token_opt: Optional[str] = typer.Option(None, "--jira-token"),
    jira_password: Optional[str] = typer.Option(None, "--jira-password",
        help="Пароль для сессионного логина Jira."),
    gate_user: Optional[str] = typer.Option(None, "--gate-user",
        help="Логин nginx Basic-гейта перед Jira (если есть)."),
    gate_password: Optional[str] = typer.Option(None, "--gate-password"),
    sdesk_host: Optional[str] = typer.Option(None, "--sdesk-host",
        help="Хост второго Jira-инстанса SDESK (Enter/пусто — без SDESK)."),
    sdesk_project: Optional[str] = typer.Option(None, "--sdesk-project",
        help="Префикс ключей SDESK (напр. SDESK)."),
    sdesk_user: Optional[str] = typer.Option(None, "--sdesk-user"),
    sdesk_token_opt: Optional[str] = typer.Option(None, "--sdesk-token"),
    sdesk_password: Optional[str] = typer.Option(None, "--sdesk-password",
        help="Пароль для сессионного логина SDESK."),
    sdesk_gate_password: Optional[str] = typer.Option(None, "--sdesk-gate-password",
        help="Пароль nginx-гейта перед SDESK (логин-гейт берётся из --gate-user, если не задан отдельно)."),
    sdesk_gate_user: Optional[str] = typer.Option(None, "--sdesk-gate-user"),
    bitbucket_host: Optional[str] = typer.Option(None, "--bitbucket-host"),
    bitbucket_project: Optional[str] = typer.Option(None, "--bitbucket-project"),
    bitbucket_repo: Optional[str] = typer.Option(None, "--bitbucket-repo"),
    bitbucket_token_opt: Optional[str] = typer.Option(None, "--bitbucket-token"),
    github_owner: Optional[str] = typer.Option(None, "--github-owner",
        help="Организация или пользователь GitHub, чьи репозитории ведём."),
    github_repos: Optional[str] = typer.Option(None, "--github-repos",
        help="Репозитории через запятую (пусто — все репозитории owner'а)."),
    github_user: Optional[str] = typer.Option(None, "--github-user",
        help="Мой логин на GitHub (пусто — узнаем сами)."),
    github_token_opt: Optional[str] = typer.Option(None, "--github-token",
        help="PAT GitHub (classic со scope repo — покрывает Issues, PR и Actions)."),
    github_api: Optional[str] = typer.Option(None, "--github-api",
        help="API-хост для GitHub Enterprise (напр. https://ghe.example.com/api/v3)."),
    github_web: Optional[str] = typer.Option(None, "--github-web",
        help="Веб-хост GitHub Enterprise (напр. https://ghe.example.com)."),
    jenkins_host: Optional[str] = typer.Option(None, "--jenkins-host"),
    jenkins_user: Optional[str] = typer.Option(None, "--jenkins-user"),
    jenkins_token_opt: Optional[str] = typer.Option(None, "--jenkins-token",
        help="API-токен Jenkins (профиль → Security → API Token)."),
    db_path_opt: Optional[str] = typer.Option(None, "--db-path"),
) -> None:
    """Визард настройки (когда вызвано без подкоманды export/import)."""
    if ctx.invoked_subcommand is not None:
        return  # вызвана подкоманда (export/import) — визард не запускаем

    # За основу берём УЖЕ СОХРАНЁННЫЙ конфиг воркспейса, а не глобальный config.toml:
    # ниже он целиком перезаписывается, поэтому запуск с одним флагом (скажем, только
    # --github-token) иначе обнулял бы все остальные поля контура. Заодно интерактивный
    # визард показывает в дефолтах то, что реально настроено здесь.
    # Что спрашивать, диктует провайдер контура: у GitHub-контура вопросов про Jira,
    # Bitbucket и Jenkins просто не существует.
    with _open_store() as probe:
        ws0 = _resolve_workspace(probe)
        provider = ws0.provider
        cfg = workspaces.config_for_workspace(probe, ws0)
    if not non_interactive and provider == "local":
        provider = _prompt_default(
            "Провайдер задач и PR (local / jira / github)", provider
        ).strip().lower()
        if provider not in ("local", "jira", "github"):
            err.print(f"[red]Неизвестный провайдер: {provider}[/red]")
            raise typer.Exit(code=1)

    if non_interactive:
        if jira_host is not None: cfg.jira.base_url = jira_host.rstrip("/")
        if jira_user is not None: cfg.jira.username = jira_user
        if jira_project is not None: cfg.jira.project = jira_project
        if bitbucket_host is not None: cfg.bitbucket.base_url = bitbucket_host.rstrip("/")
        if bitbucket_project is not None: cfg.bitbucket.project = bitbucket_project
        if bitbucket_repo is not None: cfg.bitbucket.repo = bitbucket_repo
        if jenkins_host is not None: cfg.jenkins.base_url = jenkins_host.rstrip("/")
        if jenkins_user is not None: cfg.jenkins.username = jenkins_user
        if gate_user is not None: cfg.jira.proxy_basic_user = gate_user
        if sdesk_host is not None: cfg.sdesk.base_url = sdesk_host.rstrip("/")
        if sdesk_project is not None: cfg.sdesk.project = sdesk_project
        if sdesk_user is not None: cfg.sdesk.username = sdesk_user
        # логин гейта SDESK: явный --sdesk-gate-user, иначе тот же, что у Jira-гейта
        if sdesk_gate_user is not None: cfg.sdesk.proxy_basic_user = sdesk_gate_user
        elif gate_user is not None and not cfg.sdesk.proxy_basic_user:
            cfg.sdesk.proxy_basic_user = gate_user
        if github_owner is not None: cfg.github.owner = github_owner
        if github_repos is not None: cfg.github.repos = github_repos
        if github_user is not None: cfg.github.username = github_user
        if github_api is not None: cfg.github.api_url = github_api.rstrip("/")
        if github_web is not None: cfg.github.web_url = github_web.rstrip("/")
        if db_path_opt is not None: cfg.storage.db_path = db_path_opt
        new_secrets = {
            "jira.token": jira_token_opt,
            "jira.password": jira_password,
            "jira.gate_password": gate_password,
            "sdesk.token": sdesk_token_opt,
            "sdesk.password": sdesk_password,
            "sdesk.gate_password": sdesk_gate_password,
            "bitbucket.token": bitbucket_token_opt,
            "github.token": github_token_opt,
            "jenkins.token": jenkins_token_opt,
        }
    elif provider == "github":
        cfg.github.owner = github_owner or _prompt_default(
            "GitHub owner (организация или пользователь)", cfg.github.owner)
        cfg.github.repos = github_repos if github_repos is not None else _prompt_default(
            "Репозитории через запятую (Enter — все)", cfg.github.repos)
        cfg.github.username = github_user if github_user is not None else _prompt_default(
            "Мой логин на GitHub (Enter — узнаем сами)", cfg.github.username)
        gtok = github_token_opt if github_token_opt is not None else _prompt_default(
            "GitHub PAT-токен", "", secret=True)
        # GitHub Enterprise: спрашиваем, только если человек сам поменял хост
        cfg.github.api_url = (github_api or _prompt_default(
            "API-хост GitHub", cfg.github.api_url)).rstrip("/")
        cfg.github.web_url = (github_web or _prompt_default(
            "Веб-хост GitHub", cfg.github.web_url)).rstrip("/")
        cur_db = cfg.storage.db_path or str(db_path(cfg))
        cfg.storage.db_path = db_path_opt or _prompt_default("Путь до БД", cur_db)
        new_secrets = {"github.token": gtok}
    elif provider == "local":
        cur_db = cfg.storage.db_path or str(db_path(cfg))
        cfg.storage.db_path = db_path_opt or _prompt_default("Путь до БД", cur_db)
        new_secrets = {}
        console.print("[dim]Локальный контур: внешних доступов не требуется. "
                      "Задачи ведутся фичами (jwu feature add).[/dim]")
    else:
        cfg.jira.base_url = (jira_host or _prompt_default("Jira host", cfg.jira.base_url)).rstrip("/")
        cfg.jira.username = jira_user or _prompt_default("Jira username", cfg.jira.username)
        cfg.jira.project = jira_project or _prompt_default("Jira project", cfg.jira.project)
        jtok = jira_token_opt if jira_token_opt is not None else _prompt_default(
            "Jira PAT-токен", "", secret=True)
        jpw = jira_password if jira_password is not None else _prompt_default(
            "Jira пароль (сессия)", "", secret=True)
        # nginx Basic-гейт перед Jira (опционально): логин в config, пароль в keyring
        cfg.jira.proxy_basic_user = gate_user or _prompt_default(
            "Логин nginx-гейта (Enter — без гейта)", cfg.jira.proxy_basic_user)
        gpw = gate_password if gate_password is not None else (
            _prompt_default("Пароль nginx-гейта", "", secret=True)
            if cfg.jira.proxy_basic_user else "")
        # SDESK — второй Jira-инстанс (опционально). Enter на хосте — пропустить целиком.
        cfg.sdesk.base_url = (sdesk_host or _prompt_default(
            "SDESK host (Enter — без SDESK)", cfg.sdesk.base_url)).rstrip("/")
        if cfg.sdesk.base_url:
            cfg.sdesk.project = sdesk_project or _prompt_default(
                "SDESK project (префикс ключей)", cfg.sdesk.project or "SDESK")
            cfg.sdesk.username = sdesk_user or _prompt_default(
                "SDESK username", cfg.sdesk.username or cfg.jira.username)
            sdtok = sdesk_token_opt if sdesk_token_opt is not None else _prompt_default(
                "SDESK PAT-токен", "", secret=True)
            sdpw = sdesk_password if sdesk_password is not None else _prompt_default(
                "SDESK пароль (сессия)", "", secret=True)
            cfg.sdesk.proxy_basic_user = sdesk_gate_user or _prompt_default(
                "Логин nginx-гейта SDESK (Enter — как у Jira/без гейта)",
                cfg.sdesk.proxy_basic_user or cfg.jira.proxy_basic_user)
            sdgpw = sdesk_gate_password if sdesk_gate_password is not None else (
                _prompt_default("Пароль nginx-гейта SDESK", "", secret=True)
                if cfg.sdesk.proxy_basic_user else "")
        else:
            sdtok = sdpw = sdgpw = ""
        cfg.bitbucket.base_url = (bitbucket_host or _prompt_default(
            "Bitbucket host", cfg.bitbucket.base_url)).rstrip("/")
        cfg.bitbucket.project = bitbucket_project or _prompt_default(
            "Bitbucket project", cfg.bitbucket.project)
        cfg.bitbucket.repo = bitbucket_repo or _prompt_default(
            "Bitbucket repo", cfg.bitbucket.repo)
        btok = bitbucket_token_opt if bitbucket_token_opt is not None else _prompt_default(
            "Bitbucket PAT-токен", "", secret=True)
        # Jenkins опционален (детализация причин падения сборок). Enter — пропустить.
        cfg.jenkins.base_url = (jenkins_host or _prompt_default(
            "Jenkins host (Enter — без Jenkins)", cfg.jenkins.base_url)).rstrip("/")
        cfg.jenkins.username = jenkins_user or _prompt_default(
            "Jenkins username", cfg.jenkins.username)
        ktok = jenkins_token_opt if jenkins_token_opt is not None else (
            _prompt_default("Jenkins API-токен", "", secret=True)
            if cfg.jenkins.username else "")
        cur_db = cfg.storage.db_path or str(db_path(cfg))
        cfg.storage.db_path = db_path_opt or _prompt_default("Путь до БД", cur_db)
        new_secrets = {
            "jira.token": jtok,
            "jira.password": jpw,
            "jira.gate_password": gpw,
            "sdesk.token": sdtok,
            "sdesk.password": sdpw,
            "sdesk.gate_password": sdgpw,
            "bitbucket.token": btok,
            "jenkins.token": ktok,
        }

    # Путь до БД — единственное, что остаётся глобальным: БД надо найти ДО того,
    # как известен воркспейс (в ней же лежат конфиги воркспейсов).
    global_cfg = load_config()
    if cfg.storage.db_path != global_cfg.storage.db_path:
        global_cfg.storage.db_path = cfg.storage.db_path
        save_config(global_cfg)

    with _open_store() as store:
        ws = _resolve_workspace(store)
        # cfg приехал из ПРЕДЫДУЩЕГО соединения (см. начало функции) — источник секретов
        # в нём смотрит в уже закрытый Store; перевешиваем на живое.
        cfg.secrets = secrets.DbSecrets(store, ws.id)
        workspaces.save_workspace_config(store, ws, cfg)
        saved = 0
        for slot, value in new_secrets.items():
            if value:  # пусто/None => не трогаем, старое значение остаётся
                store.set_workspace_secret(ws.id, slot, value)
                saved += 1
        ws = _enable_provider_if_configured(store, ws, cfg)
        _enable_configured_integrations(store, ws, cfg)
        for warn in warn_if_cloud_path(db_path()):
            err.print(f"[yellow]⚠ {warn}[/yellow]")

    console.print(f"[green]Конфиг воркспейса «{ws.label}» сохранён[/green] "
                  f"(секретов записано: {saved})")
    _auth_check_report()


@configure_app.command("export")
def configure_export(
    path: str = typer.Argument(..., help="Куда записать бандл (.toml)."),
) -> None:
    """Выгрузить настройки воркспейса + СЕКРЕТЫ в файл (плайнтекст — храните безопасно)."""
    from ..core.config import export_bundle

    with _open_store() as store:
        ws = _resolve_workspace(store)
        cfg = workspaces.config_for_workspace(store, ws)
        n = export_bundle(cfg, Path(path))
    console.print(f"[green]Бандл записан[/green]: {path}  "
                  f"(воркспейс «{ws.label}», секретов: {n})")
    err.print("[yellow]Внимание:[/yellow] файл содержит пароли в открытом виде — "
              "не коммить и храни безопасно.")


@configure_app.command("import")
def configure_import(
    path: str = typer.Argument(..., help="Файл бандла (.toml) из `configure export`."),
) -> None:
    """Применить бандл к активному воркспейсу: настройки и секреты, затем проверить связь."""
    from ..core.config import read_bundle

    try:
        cfg, values = read_bundle(Path(path))
    except ConfigError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    with _open_store() as store:
        ws = _resolve_workspace(store)
        workspaces.save_workspace_config(store, ws, cfg)
        for slot, value in values.items():
            store.set_workspace_secret(ws.id, slot, value)
        _enable_configured_integrations(store, ws, cfg)

    console.print(f"[green]Импортировано[/green] в воркспейс «{ws.label}»: "
                  f"настройки + секретов {len(values)}")
    _auth_check_report()


@app.command("install-claude-skills")
def install_claude_skills(
    dest: Optional[str] = typer.Option(
        None, "--dest", help="Каталог скиллов (по умолчанию ~/.claude/skills)."),
    agents_dest: Optional[str] = typer.Option(
        None, "--agents-dest", help="Каталог субагентов (по умолчанию ~/.claude/agents)."),
    skip_agents: bool = typer.Option(
        False, "--skip-agents", help="Не ставить забандленных субагентов."),
) -> None:
    """Развернуть jwu-скиллы и субагенты Claude Code из пакета (свежие; существующие заменяются)."""
    target = Path(dest).expanduser() if dest else _skills_dest()
    try:
        results = install_skills(target)
    except Exception as exc:  # noqa: BLE001
        err.print(f"[red]Не удалось установить скиллы:[/red] {exc}")
        raise typer.Exit(code=1)
    for name, action in results:
        color = "yellow" if action == "обновлён" else "green"
        console.print(f"[{color}]{action}[/{color}]: {name}")
    console.print(f"Готово: {len(results)} скиллов → {target}")

    if skip_agents:
        return
    agents_target = Path(agents_dest).expanduser() if agents_dest else _agents_dest()
    try:
        agent_results = install_agents(agents_target)
    except Exception as exc:  # noqa: BLE001
        err.print(f"[red]Не удалось установить субагентов:[/red] {exc}")
        raise typer.Exit(code=1)
    for name, action in agent_results:
        color = "yellow" if action == "обновлён" else "green"
        console.print(f"[{color}]{action}[/{color}]: агент {name}")
    console.print(f"Готово: {len(agent_results)} субагентов → {agents_target}")


# --------------------------------------------------------------------------- #
# tasks / task
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Воркспейсы
# --------------------------------------------------------------------------- #


def _ws_json(store: Store, ws: Workspace) -> dict:
    """Воркспейс для --json. Счётчики считаются в его же скоупе (store переключается)."""
    store.use_workspace(ws.id)
    jobs = store.list_jobs()
    return {
        "id": ws.id,
        "slug": ws.slug,
        "name": ws.name,
        "provider": ws.provider,
        "jira_enabled": ws.jira_enabled,
        "github_enabled": ws.github_enabled,
        "bitbucket_enabled": ws.bitbucket_enabled,
        "prs_enabled": ws.prs_enabled,
        "archived": ws.archived,
        "jobs": len(jobs),
        "jobs_active": len([j for j in jobs if j.status == "active"]),
        # Тот же контекст и в той же форме, что отдаёт MCP: скиллы ходят сюда фолбэком,
        # когда MCP-сервера нет, и видеть они должны ровно то же самое.
        **store.workspace_context(),
        "created_at": ws.created_at,
        "updated_at": ws.updated_at,
    }


def _yes_no(flag: bool) -> str:
    return "[green]да[/green]" if flag else "[dim]нет[/dim]"


def _provider_cell(ws: Workspace) -> str:
    """Провайдер контура в таблицах: у Jira отдельно отмечаем, есть ли Bitbucket."""
    if ws.provider == "local":
        return "[dim]локальный[/dim]"
    if ws.provider == "jira":
        return "[cyan]Jira[/cyan]" + ("" if ws.bitbucket_enabled else " [dim](без Bitbucket)[/dim]")
    return f"[cyan]{ws.provider_label}[/cyan]"


@workspace_app.command("list")
def workspace_list(
    all_ws: bool = typer.Option(False, "--all", help="Показать и архивные."),
    json_out: bool = typer.Option(False, "--json", help="Вывести JSON."),
) -> None:
    """Список воркспейсов."""
    store = _open_store()
    items = store.list_workspaces(include_archived=all_ws)
    active = store.get_meta(workspaces.ACTIVE_META_KEY) or ""
    if json_out:
        _emit_json({"active": active, "workspaces": [_ws_json(store, w) for w in items]})
        return
    if not items:
        console.print("[dim]Воркспейсов нет. Создать: jwu workspace create <slug>[/dim]")
        return
    table = Table(box=None, pad_edge=False)
    for col in ("", "Slug", "Название", "Провайдер", "PR", "Папок", "Работ"):
        table.add_column(col)
    for ws in items:
        store.use_workspace(ws.id)
        table.add_row(
            "→" if ws.slug == active else " ",
            ws.slug, ws.name,
            _provider_cell(ws), _yes_no(ws.prs_enabled),
            str(len(ws.paths)), str(len(store.list_jobs())),
        )
    console.print(table)


@workspace_app.command("create")
def workspace_create(
    slug: str = typer.Argument(..., help="Короткое имя (латиница): work, home-jwu."),
    name: str = typer.Option("", "--name", help="Человекочитаемое название."),
    paths: list[str] = typer.Option([], "--path", help="Папка воркспейса (можно несколько)."),
    tag: list[str] = typer.Option(
        [], "--tag", "-t", help="Теги для указанных папок: legacy-бэкенд, новая-версия…"),
    provider: str = typer.Option(
        "local", "--provider", "-p",
        help="Откуда задачи и PR: local (свои фичи) | jira (+Bitbucket) | github."),
    bitbucket: bool = typer.Option(
        False, "--bitbucket/--no-bitbucket", help="Для jira-контура: подключён ли Bitbucket."
    ),
    use: bool = typer.Option(True, "--use/--no-use", help="Сделать активным."),
    json_out: bool = typer.Option(False, "--json", help="Вывести JSON."),
) -> None:
    """Создать воркспейс. Провайдер объявляется явно — по умолчанию контур локальный."""
    store = _open_store()
    try:
        ws = workspaces.create(
            store, slug, name=name, provider=provider, bitbucket=bitbucket,
            paths=list(paths), tags=list(tag),
        )
    except workspaces.WorkspaceError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    if use:
        workspaces.set_active(store, ws)
    if json_out:
        _emit_json(_ws_json(store, ws))
        return
    console.print(f"[green]Воркспейс «{ws.label}» создан.[/green] "
                  f"Провайдер: {ws.provider_label}")
    for p in ws.paths:
        console.print(f"  📁 {p.path}")
    if ws.provider != "local":
        console.print(f"Настроить доступы: [cyan]jwu configure -W {ws.slug}[/cyan]")


@workspace_app.command("provider")
def workspace_provider(
    provider: Optional[str] = typer.Argument(
        None, help="local | jira | github. Без аргумента — показать текущий."),
    bitbucket: Optional[bool] = typer.Option(
        None, "--bitbucket/--no-bitbucket", help="Для jira-контура: подключён ли Bitbucket."),
    json_out: bool = typer.Option(False, "--json", help="Вывести JSON."),
) -> None:
    """Показать или сменить провайдера контура (задачи и PR у него один источник).

    Локальные данные — работы, фичи, заметки, правила — остаются на месте: провайдер
    отвечает только за то, откуда приезжают задачи и PR.
    """
    store = _open_store()
    ws = _resolve_workspace(store)
    if provider is None:
        if json_out:
            _emit_json(_ws_json(store, ws))
            return
        console.print(f"[b]{ws.label}[/b]   провайдер: {_provider_cell(ws)}")
        for name, (label, what) in models.PROVIDER_LABELS.items():
            mark = "→" if name == ws.provider else " "
            console.print(f" {mark} [cyan]{name}[/cyan] — {label}: [dim]{what}[/dim]")
        console.print("\nСменить: [cyan]jwu workspace provider <local|jira|github>[/cyan]")
        return
    try:
        updated = workspaces.set_provider(store, ws, provider, bitbucket=bitbucket)
    except workspaces.WorkspaceError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    if json_out:
        _emit_json(_ws_json(store, updated))
        return
    console.print(f"[green]Провайдер воркспейса «{updated.label}»:[/green] "
                  f"{updated.provider_label}")
    if updated.provider != "local":
        console.print(f"Доступы: [cyan]jwu configure -W {updated.slug}[/cyan]")


@app.command("init")
def init_workspace(
    path: str = typer.Argument(".", help="Папка проекта (по умолчанию — текущая)."),
    slug: Optional[str] = typer.Option(None, "--slug", help="Имя воркспейса (иначе — по папке)."),
    name: str = typer.Option("", "--name", help="Человекочитаемое название."),
    provider: str = typer.Option(
        "local", "--provider", "-p",
        help="Откуда задачи и PR: local | jira (+Bitbucket) | github."),
    bitbucket: bool = typer.Option(
        False, "--bitbucket/--no-bitbucket", help="Для jira-контура: подключить Bitbucket."),
    attach: Optional[str] = typer.Option(
        None, "--attach", help="Привязать папку к СУЩЕСТВУЮЩЕМУ воркспейсу вместо создания."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Не спрашивать, применить предложение."),
    json_out: bool = typer.Option(False, "--json", help="Вывести JSON (для агентов)."),
) -> None:
    """Подключить проект к jwu: завести воркспейс для папки, привязать репозитории, дать теги.

    Сначала показывает, ЧТО собирается сделать (найденные репозитории и предлагаемые теги),
    и только потом спрашивает подтверждение. Провайдер по умолчанию локальный — для
    личного проекта внешние системы не нужны, а для рабочего есть `--provider` и
    `jwu configure`.
    """
    store = _open_store()
    sg = workspaces.suggest(store, path)

    # ГЛАВНОЕ: если папка уже резолвится в воркспейс — НИЧЕГО не создаём.
    # Повторный init обязан быть безопасным: он подтверждает, что проект уже подключён.
    if sg.already_bound and sg.workspace is not None:
        ws = sg.workspace
        store.use_workspace(ws.id)
        if json_out:
            payload = _ws_json(store, ws)
            payload.update({"ok": True, "already_initialized": True, "path": sg.path})
            _emit_json(payload)
            return
        console.print(f"[green]Проект уже подключён[/green] к воркспейсу «{ws.label}» "
                      f"[dim](папка резолвится сюда)[/dim]")
        for p_ in ws.paths:
            chips = f"   [cyan]{' '.join('#' + t for t in p_.tags)}[/cyan]" if p_.tags else ""
            console.print(f"  📁 {p_.path}{chips}")
        console.print("Добавить репозиторий: [cyan]jwu workspace add-path <DIR> --tag <тег>[/cyan]"
                      "   ·   теги: [cyan]jwu workspace tag <DIR> --add <тег>[/cyan]")
        return

    # Папка не привязана, но воркспейсы уже есть — возможно, её надо привязать к одному
    # из них, а не плодить новый. Молча не решаем.
    existing = [w for w in store.list_workspaces()]
    if attach:
        target = workspaces.find_workspace(store, attach)
        if target is None:
            err.print(f"[red]Воркспейс «{attach}» не найден.[/red] Список: jwu workspace list")
            raise typer.Exit(code=1)
        for folder in sg.suggested_paths:
            workspaces.add_path(store, target, folder, tags=sg.suggested_tags.get(folder, []))
        workspaces.set_active(store, target)
        target = store.get_workspace(target.id) or target
        if json_out:
            _emit_json(_ws_json(store, target))
            return
        console.print(f"[green]Папки привязаны[/green] к воркспейсу «{target.label}» "
                      f"(папок теперь {len(target.paths)})")
        return

    target_slug = slug or sg.slug
    if json_out and not yes:
        _emit_json({
            "ok": False, "reason": "confirm_required", "path": sg.path,
            "suggested": {"slug": target_slug, "name": name or sg.name,
                          "paths": sg.suggested_paths, "tags": sg.suggested_tags,
                          "provider": provider, "bitbucket": bitbucket},
            "repos": [{"root": r.root, "name": r.name, "branch": r.branch} for r in sg.repos],
            # альтернатива созданию: привязать папку к уже существующему контуру
            "existing_workspaces": [{"slug": w.slug, "name": w.name,
                                     "paths": len(w.paths)} for w in existing],
        })
        raise typer.Exit(code=0)

    # Превью — только для человека: при --json на stdout должен быть ТОЛЬКО JSON.
    if not json_out:
        console.print(f"[b]Подключаю проект[/b] {sg.path}")
        console.print(f"  воркспейс: [cyan]{target_slug}[/cyan]"
                      f"   провайдер: [cyan]{provider}[/cyan]"
                      + (f"   Bitbucket: {_yes_no(bitbucket)}" if provider == "jira" else ""))
        if sg.repos:
            console.print(f"  найдено репозиториев: {len(sg.repos)}")
        for folder in sg.suggested_paths:
            tags = sg.suggested_tags.get(folder, [])
            chips = f"   [cyan]{' '.join('#' + t for t in tags)}[/cyan]" if tags else ""
            console.print(f"  📁 {folder}{chips}")
        if not sg.repos:
            console.print("  [dim]git-репозиториев не нашлось — привяжу папку как есть[/dim]")
        if existing:
            console.print(f"[dim]уже есть воркспейсы: {', '.join(w.slug for w in existing)}. "
                          f"Привязать папку к одному из них: jwu init --attach <slug>[/dim]")

    if not yes and not typer.confirm("Всё верно?", default=True):
        console.print("[dim]Отменено. Можно задать своё: jwu init --slug … --name …[/dim]")
        raise typer.Exit(code=1)

    try:
        ws = workspaces.create(store, target_slug, name=name or sg.name,
                               provider=provider, bitbucket=bitbucket)
        for folder in sg.suggested_paths:
            workspaces.add_path(store, ws, folder, tags=sg.suggested_tags.get(folder, []))
    except workspaces.WorkspaceError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    workspaces.set_active(store, ws)
    ws = store.get_workspace(ws.id) or ws

    if json_out:
        _emit_json(_ws_json(store, ws))
        return
    console.print(f"\n[green]Готово:[/green] воркспейс «{ws.label}», папок {len(ws.paths)}")
    console.print("Дальше: [cyan]jwu dashboard[/cyan]   ·   "
                  "фичи: [cyan]jwu feature add \"…\"[/cyan]")
    if ws.provider != "local":
        console.print(f"Доступы: [cyan]jwu configure -W {ws.slug}[/cyan]")


@workspace_app.command("use")
def workspace_use(
    slug: str = typer.Argument(..., help="Slug или id воркспейса."),
) -> None:
    """Сделать воркспейс активным (для команд вне зарегистрированных папок)."""
    store = _open_store()
    ws = workspaces.find_workspace(store, slug)
    if ws is None:
        err.print(f"[red]Воркспейс «{slug}» не найден.[/red] Список: jwu workspace list")
        raise typer.Exit(code=1)
    workspaces.set_active(store, ws)
    console.print(f"[green]Активный воркспейс: {ws.label}[/green]")


@workspace_app.command("current")
def workspace_current(
    json_out: bool = typer.Option(False, "--json", help="Вывести JSON."),
) -> None:
    """Какой воркспейс сейчас активен и почему."""
    store = _open_store()
    try:
        res = workspaces.resolve(store, explicit=_WORKSPACE_ARG)
    except workspaces.WorkspaceError as exc:
        if json_out:
            _emit_json({"workspace": None, "error": str(exc), "cwd": str(Path.cwd())})
            raise typer.Exit(code=1)
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    store.use_workspace(res.workspace.id)
    if json_out:
        payload = _ws_json(store, res.workspace)
        payload.update({"source": res.source, "matched_path": res.matched_path,
                        "cwd": str(Path.cwd())})
        _emit_json(payload)
        return
    ws = res.workspace
    console.print(f"[b]{ws.label}[/b]   [dim]({res.source_human})[/dim]")
    console.print(f"Провайдер: {_provider_cell(ws)}   ·   PR: {_yes_no(ws.prs_enabled)}")


@workspace_app.command("migrate")
def workspace_migrate() -> None:
    """Перенести старый config.toml и секреты из keyring в БД (обычно происходит само)."""
    store = _open_store()
    ws = store.get_workspace_by_slug("work") or _resolve_workspace(store)
    fields, moved = workspaces.migrate_legacy_config(store, ws)
    if not fields and not moved:
        console.print("[dim]Переносить нечего — это уже сделано ранее.[/dim]")
        return
    console.print(f"[green]Перенесено в «{ws.label}»[/green]: настроек {fields}, "
                  f"секретов {moved}. Keyring не тронут (остаётся как фолбэк).")
    for warn in warn_if_cloud_path(db_path()):
        err.print(f"[yellow]⚠ {warn}[/yellow]")


@workspace_app.command("show")
def workspace_show(
    slug: Optional[str] = typer.Argument(None, help="Slug/id; без него — активный."),
    json_out: bool = typer.Option(False, "--json", help="Вывести JSON."),
    show_secrets: bool = typer.Option(
        False, "--show-secrets", help="Показать секреты в открытом виде."
    ),
) -> None:
    """Карточка воркспейса: папки, интеграции, счётчики, настройки."""
    store = _open_store()
    ws = workspaces.find_workspace(store, slug) if slug else _resolve_workspace(store)
    if ws is None:
        err.print(f"[red]Воркспейс «{slug}» не найден.[/red]")
        raise typer.Exit(code=1)
    store.use_workspace(ws.id)
    if json_out:
        payload = _ws_json(store, ws)
        payload["settings"] = store.workspace_settings(ws.id)
        payload["secrets"] = {
            slot: (value if show_secrets else "****")
            for slot, value in store.workspace_secrets(ws.id).items()
        }
        _emit_json(payload)
        return
    console.print(f"[b]{ws.label}[/b]")
    console.print(f"Провайдер: {_provider_cell(ws)}   ·   PR: {_yes_no(ws.prs_enabled)}")
    jobs_all = store.list_jobs()
    active_jobs = [j for j in jobs_all if j.status == "active"]
    console.print(f"Работ: {len(jobs_all)} (активных {len(active_jobs)})")
    if ws.paths:
        console.print("\n[b]Папки[/b]")
        for p in ws.paths:
            mark = "" if Path(p.path).exists() else "  [yellow](нет на диске)[/yellow]"
            label = f"  [dim]{p.label}[/dim]" if p.label else ""
            tags = f"  [cyan]{' '.join('#' + t for t in p.tags)}[/cyan]" if p.tags else ""
            console.print(f"  📁 {p.path}{tags}{label}{mark}")
    else:
        console.print("\n[dim]Папок нет. Привязать текущую: jwu workspace add-path .[/dim]")

    # Показываем настройки ТОЛЬКО текущего провайдера: хост Jira в github-контуре
    # ничего не значит (он мог достаться от глобального конфига) и лишь путает.
    own = {"jira": ("jira.", "sdesk.", "bitbucket.", "jenkins."),
           "github": ("github.",)}.get(ws.provider, ())
    settings = {k: v for k, v in store.workspace_settings(ws.id).items()
                if not k.startswith("features.") and v and k.startswith(own)}
    if settings:
        console.print(f"\n[b]Настройки[/b] [dim](провайдер: {ws.provider})[/dim]")
        for key in sorted(settings):
            if ".views." in key:
                continue  # JQL/поисковый запрос длинный и шумный — виден в --json
            console.print(f"  [dim]{key}[/dim] = {settings[key]}")
    stored = store.workspace_secrets(ws.id)
    if stored:
        console.print("\n[b]Секреты[/b] [dim](в БД, открытым текстом)[/dim]")
        for slot in sorted(stored):
            value = stored[slot] if show_secrets else "****"
            console.print(f"  [dim]{slot}[/dim] = {value}")


@workspace_app.command("add-path")
def workspace_add_path(
    path: str = typer.Argument(".", help="Папка (по умолчанию — текущая)."),
    label: str = typer.Option("", "--label", help="Пометка (например «бэкенд»)."),
    tag: list[str] = typer.Option(
        [], "--tag", "-t",
        help="Тег для поиска (можно несколько): legacy-бэкенд, новая-версия, фронт."),
) -> None:
    """Привязать папку к воркспейсу — по ней он и будет определяться автоматически."""
    store = _open_store()
    ws = _resolve_workspace(store)
    try:
        norm, warn = workspaces.add_path(store, ws, path, label, tags=list(tag))
    except workspaces.WorkspaceError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    tags_note = f"   [cyan]{' '.join('#' + t for t in tag)}[/cyan]" if tag else ""
    console.print(f"[green]{norm}[/green] → воркспейс «{ws.label}»{tags_note}")
    if warn:
        err.print(f"[yellow]⚠ {warn}[/yellow]")


@workspace_app.command("tag")
def workspace_tag(
    path: str = typer.Argument(".", help="Папка (по умолчанию — текущая)."),
    add: list[str] = typer.Option([], "--add", "-a", help="Добавить тег (можно несколько)."),
    remove: list[str] = typer.Option([], "--remove", "-r", help="Убрать тег."),
    set_: list[str] = typer.Option([], "--set", help="Заменить весь набор тегов."),
    json_out: bool = typer.Option(False, "--json", help="Вывести JSON."),
) -> None:
    """Теги папки: по ним потом искать, где что лежит (legacy-бэкенд, новая-версия…)."""
    store = _open_store()
    ws = _resolve_workspace(store)
    norm = workspaces.normalize_path(path)
    row = next((p for p in store.workspace_paths(ws.id) if p.path == norm), None)
    if row is None:
        err.print(f"[red]Папка {norm} не привязана к воркспейсу «{ws.label}».[/red]\n"
                  f"Привязать: [cyan]jwu workspace add-path {path}[/cyan]")
        raise typer.Exit(code=1)
    if set_:
        tags = store.set_path_tags(row.id, list(set_))
    else:
        if add:
            store.add_path_tags(row.id, list(add))
        tags = store.remove_path_tags(row.id, list(remove)) if remove \
            else store._path_tags([row.id]).get(row.id, [])
    if json_out:
        _emit_json({"path": norm, "tags": tags})
        return
    console.print(f"{norm}   [cyan]{' '.join('#' + t for t in tags) or '— без тегов'}[/cyan]")


@workspace_app.command("paths")
def workspace_paths(
    tag: Optional[str] = typer.Option(None, "--tag", "-t", help="Только папки с этим тегом."),
    json_out: bool = typer.Option(False, "--json", help="Вывести JSON."),
) -> None:
    """Папки воркспейса (с тегами). С --tag — поиск: где лежит legacy-бэкенд и т.п."""
    store = _open_store()
    ws = _resolve_workspace(store)
    rows = store.workspace_paths(ws.id, tag=tag)
    if json_out:
        _emit_json({"workspace": ws.slug, "tag": tag,
                    "paths": [p.model_dump() for p in rows],
                    "known_tags": store.all_tags(ws.id)})
        return
    if not rows:
        what = f" с тегом «{tag}»" if tag else ""
        console.print(f"[dim]Папок{what} нет.[/dim]")
        raise typer.Exit(code=1 if tag else 0)
    table = Table(box=None, pad_edge=False)
    for col in ("Папка", "Теги", "Метка"):
        table.add_column(col)
    for row in rows:
        table.add_row(row.path,
                      " ".join(f"#{t}" for t in row.tags) or "—",
                      row.label or "—")
    console.print(table)
    known = store.all_tags(ws.id)
    if known and not tag:
        console.print("[dim]теги: " + "  ".join(f"#{t}({n})" for t, n in known.items()) + "[/dim]")


@workspace_app.command("remove-path")
def workspace_remove_path(
    path: str = typer.Argument(".", help="Папка (по умолчанию — текущая)."),
) -> None:
    """Отвязать папку от воркспейса."""
    store = _open_store()
    norm = workspaces.normalize_path(path)
    if not store.remove_workspace_path(norm):
        err.print(f"[yellow]Папка {norm} ни к одному воркспейсу не привязана.[/yellow]")
        raise typer.Exit(code=1)
    console.print(f"[green]Отвязано:[/green] {norm}")


@workspace_app.command("rename")
def workspace_rename(
    slug: str = typer.Argument(..., help="Текущий slug/id."),
    new_slug: str = typer.Argument(..., help="Новый slug."),
    name: Optional[str] = typer.Option(None, "--name", help="Новое название."),
) -> None:
    """Переименовать воркспейс."""
    store = _open_store()
    ws = workspaces.find_workspace(store, slug)
    if ws is None:
        err.print(f"[red]Воркспейс «{slug}» не найден.[/red]")
        raise typer.Exit(code=1)
    try:
        target = workspaces.normalize_slug(new_slug)
    except workspaces.WorkspaceError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    if target != ws.slug and store.get_workspace_by_slug(target) is not None:
        err.print(f"[red]Воркспейс «{target}» уже есть.[/red]")
        raise typer.Exit(code=1)
    fields = {"slug": target}
    if name is not None:
        fields["name"] = name
    store.update_workspace(ws.id, **fields)
    if (store.get_meta(workspaces.ACTIVE_META_KEY) or "") == ws.slug:
        store.set_meta(workspaces.ACTIVE_META_KEY, target)
    console.print(f"[green]«{ws.slug}» → «{target}»[/green]")


@workspace_app.command("delete")
def workspace_delete(
    slug: str = typer.Argument(..., help="Slug/id воркспейса."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Не спрашивать подтверждение."),
    keep_data: bool = typer.Option(
        False, "--keep-data", help="Оставить работы/заметки/анализы в БД (осиротевшими)."
    ),
) -> None:
    """Удалить воркспейс вместе с его локальными данными."""
    store = _open_store()
    ws = workspaces.find_workspace(store, slug)
    if ws is None:
        err.print(f"[red]Воркспейс «{slug}» не найден.[/red]")
        raise typer.Exit(code=1)
    if len(store.list_workspaces(include_archived=True)) == 1:
        err.print("[red]Это единственный воркспейс — удалять нечего, останется пустая БД.[/red]")
        raise typer.Exit(code=1)
    store.use_workspace(ws.id)
    jobs_count = len(store.list_jobs())
    if not yes:
        what = "вместе со всеми данными" if not keep_data else "оставив данные в БД"
        confirm = typer.confirm(
            f"Удалить воркспейс «{ws.label}» {what}? Работ: {jobs_count}", default=False
        )
        if not confirm:
            console.print("[dim]Отменено.[/dim]")
            raise typer.Exit(code=1)
    store.delete_workspace(ws.id, keep_data=keep_data)
    if (store.get_meta(workspaces.ACTIVE_META_KEY) or "") == ws.slug:
        store.set_meta(workspaces.ACTIVE_META_KEY, "")
    console.print(f"[green]Воркспейс «{ws.label}» удалён.[/green]")


def _render_issues(issues: list[Issue]) -> None:
    table = Table(show_header=True, header_style="bold")
    table.add_column("Key", style="cyan", no_wrap=True)
    table.add_column("Статус")
    table.add_column("Приоритет")
    table.add_column("Summary")
    for it in issues:
        table.add_row(it.key, it.status, it.priority, it.summary)
    console.print(table)
    console.print(f"[dim]Всего: {len(issues)}[/dim]")


@app.command()
def tasks(
    view: str = typer.Option("mine", "--view", "-v", help="mine | review | mentions"),
    jql: Optional[str] = typer.Option(None, "--jql", help="Произвольный JQL (игнорирует --view)."),
    json_out: bool = typer.Option(False, "--json", help="Вывести JSON."),
) -> None:
    """Список задач по вью или произвольному JQL."""
    with _service_with_jira() as svc:
        try:
            issues = svc.tasks(view, jql=jql)
        except (ValueError, JiraError, GitHubError) as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1)
    if json_out:
        _emit_json([i.model_dump() for i in issues])
    else:
        _render_issues(issues)


@app.command()
def task(
    key: str = typer.Argument(..., help="Ключ задачи, напр. PROJ-5525."),
    json_out: bool = typer.Option(False, "--json", help="Вывести JSON."),
) -> None:
    """Полная карточка задачи: описание, все комменты, статус, links, dev-панель."""
    with _service_with_jira() as svc:
        issue = svc.issue(key)
        notes = svc.get_notes(key)
        jobs_list = svc.jobs_for_task(key)
        # Статусы CI-сборок по OPEN-PR из dev-панели (best-effort, не валим карточку).
        pr_builds: dict[str, list] = {}
        for pr in [p for p in issue.pull_requests if p.status == "OPEN"][:5]:
            try:
                pr_builds[pr.id] = svc.build_statuses_for_pr_url(pr.url)
            except Exception:  # noqa: BLE001
                pr_builds[pr.id] = []
    if json_out:
        payload = issue.model_dump()
        payload["notes"] = [n.model_dump() for n in notes]
        payload["jobs"] = [j.model_dump() for j in jobs_list]
        payload["pr_builds"] = {pid: [b.model_dump() for b in bs] for pid, bs in pr_builds.items()}
        _emit_json(payload)
        return
    console.print(f"[bold cyan]{issue.key}[/bold cyan]  [{issue.status}]  {issue.summary}")
    console.print(f"[dim]assignee:[/dim] {issue.assignee or '—'}   [dim]priority:[/dim] {issue.priority or '—'}")
    if issue.description:
        console.print("\n[bold]Описание[/bold]")
        console.print(Group(*render_jira_text(issue.description)))
    if issue.comments:
        console.print(f"\n[bold]Комментарии ({len(issue.comments)})[/bold]")
        for c in issue.comments:
            console.print(f"[dim]{fmt_dt(c.created)} · {c.author}[/dim]")
            console.print(Group(*render_jira_text(c.body or "")))
            console.print()
    _render_attachments(issue.attachments)
    if issue.pull_requests or issue.branches:
        console.print("[bold]Development[/bold]")
        for b in issue.branches:
            console.print(f"  ветка: {b.name} [dim]{b.repository}[/dim]")
        for pr in issue.pull_requests:
            console.print(f"  PR {pr.id} [{pr.status}] {pr.name}")
            for b in pr_builds.get(pr.id, []):
                desc = f" [dim]{b.description}[/dim]" if b.description else ""
                console.print(f"      {_build_icon(b.state)} {b.name or b.key}{desc}")
    if jobs_list:
        console.print(f"\n[bold]Работы ({len(jobs_list)})[/bold]")
        for j in jobs_list:
            prs = ", ".join(f"#{p.pr_id}" for p in j.prs) or "—"
            console.print(f"  #{j.id} [{j.status}] {j.title or '—'} "
                          f"[dim]записей: {len(j.records)}; PR: {prs}[/dim]")
    if notes:
        console.print(f"\n[bold]Заметки[/bold]")
        for n in notes:
            console.print(f"[dim]{n.ts} · {n.author}[/dim] {n.text}")


def _extract_archive(path: Path) -> list[Path]:
    """Распаковать zip/tar* в <path>.extracted/. Вернуть извлечённые файлы (rar/7z — мимо)."""
    import tarfile
    import zipfile

    out = path.with_name(path.name + ".extracted")
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as z:
                z.extractall(out)
        elif tarfile.is_tarfile(path):
            with tarfile.open(path) as t:
                t.extractall(out, filter="data")  # filter='data' — защита от path traversal
        else:
            return []
    except Exception:  # noqa: BLE001 — битый архив не должен ронять команду
        return []
    return sorted(p for p in out.rglob("*") if p.is_file())


@app.command()
def attachments(
    key: str = typer.Argument(..., help="Ключ задачи, напр. PROJ-5525."),
    download: bool = typer.Option(False, "--download", "-d", help="Скачать вложения в tmp."),
    kind: Optional[list[str]] = typer.Option(
        None, "--kind", "-k",
        help="Какие виды качать (повторяй -k): image|log|doc|archive. По умолчанию все, кроме видео."),
    dest: Optional[str] = typer.Option(
        None, "--dest", help="Каталог для скачивания (по умолчанию <tmp>/jwu/<KEY>)."),
    extract: bool = typer.Option(True, "--extract/--no-extract", help="Распаковывать архивы."),
    json_out: bool = typer.Option(False, "--json", help="Вывести JSON."),
) -> None:
    """Вложения задачи: список с видами/счётчиками; с --download — скачать в tmp для анализа.

    Видео всегда только в списке (не качаются). Для Claude: --download качает файлы,
    печатает локальные пути — изображения/логи/pdf затем читаются через Read.
    """
    with _service_with_jira() as svc:
        issue = svc.issue(key)
        dest_dir = Path(dest) if dest else svc.attachments_dir(key)
        downloaded: list[tuple] = []
        if download:
            downloaded = svc.download_attachments(
                key, kinds=kind or None, dest=dest_dir, issue=issue)
    atts = issue.attachments

    extracted_map: dict[str, list[str]] = {}
    if download and extract:
        for att, path in downloaded:
            if att.kind == "archive":
                ex = _extract_archive(path)
                if ex:
                    extracted_map[str(path)] = [str(p) for p in ex]

    if json_out:
        payload: dict = {
            "key": key,
            "counts": _attach_counts(atts),
            "attachments": [a.model_dump() for a in atts],
        }
        if download:
            payload["dest"] = str(dest_dir)
            payload["downloaded"] = [
                {"filename": att.filename, "kind": att.kind, "path": str(path),
                 "extracted": extracted_map.get(str(path), [])}
                for att, path in downloaded
            ]
        _emit_json(payload)
        return

    if not atts:
        console.print(f"[dim]У {key} вложений нет.[/dim]")
        return
    _render_attachments(atts)
    if download:
        console.print(f"\n[bold]Скачано ({len(downloaded)})[/bold] → [cyan]{dest_dir}[/cyan]")
        for att, path in downloaded:
            console.print(f"  {path}")
            for ex in extracted_map.get(str(path), []):
                console.print(f"      [dim]↳ {ex}[/dim]")
        if not downloaded:
            console.print("  [dim]нет вложений выбранных видов[/dim]")


# --------------------------------------------------------------------------- #
# prs / pr
# --------------------------------------------------------------------------- #


def _render_prs(prs: list[PR]) -> None:
    mine = any(p.my_review_status for p in prs)
    table = Table(show_header=True, header_style="bold")
    table.add_column("PR", style="cyan", no_wrap=True)
    table.add_column("Repo")
    if mine:
        table.add_column("Моё ревью")
    else:
        table.add_column("Состояние")
        table.add_column("Конфликт")
    table.add_column("Title")
    for pr in prs:
        if mine:
            mark = "[green]галка[/green]" if pr.my_review_status == "APPROVED" else "[yellow]needs work[/yellow]"
            when = f" {fmt_dt(pr.my_review_at)}" if pr.my_review_at else ""
            table.add_row(str(pr.id), f"{pr.project}/{pr.repository}", f"{mark}{when}", pr.title)
        else:
            conflict = "—" if pr.conflicted is None else ("[red]да[/red]" if pr.conflicted else "нет")
            table.add_row(str(pr.id), f"{pr.project}/{pr.repository}", pr.state, conflict, pr.title)
    console.print(table)
    console.print(f"[dim]Всего: {len(prs)}[/dim]")


@app.command()
def prs(
    view: str = typer.Option("review", "--view", "-v", help="mine | review"),
    no_conflicts: bool = typer.Option(False, "--no-conflicts", help="Не запрашивать статус конфликтов (быстрее)."),
    mine_reviews: bool = typer.Option(False, "--mine-reviews", help="Только PR, где я апрувнул / поставил needs work; добавляет дату моего ревью (из activities, медленнее)."),
    on: Optional[str] = typer.Option(None, "--on", help="Фильтр ревью по дате моего апрува/needs work (YYYY-MM-DD или 'today'). Включает --mine-reviews."),
    json_out: bool = typer.Option(False, "--json", help="Вывести JSON."),
) -> None:
    """PR из Bitbucket по роли (мои / на ревью) со статусом merge-конфликта."""
    with _service_with_prs() as svc:
        if mine_reviews or on is not None:
            day = datetime.now().date().isoformat() if (on or "").lower() == "today" else on
            prs_list = svc.my_reviews(on=day)
        else:
            prs_list = svc.prs(view, with_conflicts=not no_conflicts)
    if json_out:
        _emit_json([p.model_dump() for p in prs_list])
    else:
        _render_prs(prs_list)


@app.command()
def pr(
    pr_id: int = typer.Argument(..., help="Числовой id PR."),
    project: Optional[str] = typer.Option(None, "--project", help="Ключ проекта Bitbucket."),
    repo: Optional[str] = typer.Option(None, "--repo", help="Slug репозитория."),
    json_out: bool = typer.Option(False, "--json", help="Вывести JSON."),
) -> None:
    """Детали одного PR + статус merge-конфликта + комментарии ревью."""
    with _service_with_prs() as svc:
        detail = svc.pr_detail(project, repo, pr_id)
        pull = detail.pr
        jobs_list = svc.jobs_for_pr(pr_id, project or "", repo or "")
    if json_out:
        payload = pull.model_dump()
        payload["comments"] = [c.model_dump() for c in detail.comments]
        payload["commits"] = detail.commits
        payload["jobs"] = [j.model_dump() for j in jobs_list]
        _emit_json(payload)
        return
    console.print(f"[bold cyan]PR {pull.id}[/bold cyan] [{pull.state}] {pull.title}")
    console.print(f"{pull.source_branch} → {pull.target_branch}  [dim]{pull.project}/{pull.repository}[/dim]")
    if pull.conflicted is not None:
        console.print(f"конфликт: {'[red]да[/red]' if pull.conflicted else 'нет'}  can_merge: {pull.can_merge}")
    _render_builds(pull.builds)
    if pull.reviewers:
        console.print("[bold]Ревьюеры[/bold]")
        for r in pull.reviewers:
            mark = "[green]✓[/green]" if r.approved else r.status or "—"
            console.print(f"  {mark} {r.display_name or r.name}")
    console.print(f"\n[bold]Комментарии ({len(detail.comments)})[/bold]")
    if not detail.comments:
        console.print("  [dim]нет[/dim]")
    for c in detail.comments:
        loc = f"[dim]{c.file}:{c.line}[/dim] " if c.file else ""
        indent = "  " + "    " * c.depth
        ts = f"[dim]{fmt_dt(c.created)}[/dim] " if c.created else ""
        console.print(f"{indent}{ts}{loc}[bold]{c.author}[/bold]: {(c.text or '').strip()}")
    if jobs_list:
        console.print(f"[bold]Работы ({len(jobs_list)})[/bold]")
        for j in jobs_list:
            console.print(f"  #{j.id} [{j.status}] {j.title or '—'} [dim]{j.task_key}[/dim]")


@app.command()
def builds(
    pr_id: int = typer.Argument(..., help="Числовой id PR."),
    project: Optional[str] = typer.Option(None, "--project", help="Ключ проекта Bitbucket."),
    repo: Optional[str] = typer.Option(None, "--repo", help="Slug репозитория."),
    json_out: bool = typer.Option(False, "--json", help="Вывести JSON."),
) -> None:
    """Статусы CI-сборок по head-коммиту PR (быстро, из build-status API Bitbucket)."""
    with _builds_service() as svc:
        proj = project or svc.cfg.bitbucket.project
        rp = repo or svc.cfg.bitbucket.repo
        statuses = svc.build_statuses_for_pr(proj, rp, pr_id)
    if json_out:
        _emit_json([b.model_dump() for b in statuses])
        return
    if not statuses:
        console.print("[dim]Сборок по head-коммиту PR нет.[/dim]")
        return
    _render_builds(statuses)


@app.command()
def build(
    pr_id: int = typer.Argument(..., help="Числовой id PR."),
    project: Optional[str] = typer.Option(None, "--project", help="Ключ проекта Bitbucket."),
    repo: Optional[str] = typer.Option(None, "--repo", help="Slug репозитория."),
    url: Optional[str] = typer.Option(None, "--url", help="URL конкретной сборки Jenkins."),
    json_out: bool = typer.Option(False, "--json", help="Вывести JSON (для субагента-аналитика)."),
) -> None:
    """Детальный разбор сборки: статус из Bitbucket + причина падения из Jenkins.

    По умолчанию берётся упавшая сборка по head-коммиту PR. Без Jenkins-токена выводит
    только статус из Bitbucket. ``--json`` отдаёт структурированный отчёт для анализа.
    """
    with _builds_service() as svc:
        report = svc.build_report(project, repo, pr_id, build_url=url)
    if json_out:
        _emit_json(report.model_dump() if report is not None else None)
        return
    if report is None:
        console.print("[dim]Сборок по head-коммиту PR нет.[/dim]")
        return
    console.print(f"{_build_icon(report.state)} [bold]{report.name}[/bold]  "
                  f"[dim]{report.description}[/dim]")
    if report.url:
        console.print(f"[dim]{report.url}[/dim]")
    if report.jenkins_available:
        res = report.result or ("идёт" if report.building else "—")
        line = f"результат: {res}"
        if report.branch:
            line += f"  ветка: {report.branch}"
        if report.sha:
            line += f"  commit: {report.sha[:10]}"
        console.print(line)
        if report.summary is not None:
            s = report.summary
            console.print(f"тесты: [red]fail={s['fail']}[/red] pass={s['passed']} skip={s['skip']}")
        if report.failures:
            console.print(f"\n[bold red]Упавшие тесты ({len(report.failures)})[/bold red]")
            for f in report.failures:
                console.print(f"  [red]✗[/red] {f.class_name}::{f.name} [dim]({f.status})[/dim]")
                if f.error_details:
                    console.print(f"      [yellow]{f.error_details.strip().splitlines()[0][:200]}[/yellow]")
    if report.note:
        console.print(f"[yellow]{report.note}[/yellow]")


# --------------------------------------------------------------------------- #
# sync / changes
# --------------------------------------------------------------------------- #


def _render_deltas(deltas: list[Delta]) -> None:
    if not deltas:
        console.print("[dim]Изменений с прошлого синка нет.[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("Тип", style="yellow", no_wrap=True)
    table.add_column("Ключ", style="cyan", no_wrap=True)
    table.add_column("Детали")
    table.add_column("Summary")
    for d in deltas:
        table.add_row(d.kind, d.key, d.detail, d.summary)
    console.print(table)


@app.command()
def sync(
    json_out: bool = typer.Option(False, "--json", help="Вывести JSON."),
) -> None:
    """Синк по команде: тянет вью + PR, пишет снапшот в память, считает дельты.

    Секции, которых у провайдера нет, пропускаются: локальному контуру синкать
    задачи неоткуда, и это не ошибка.
    """
    with _service() as svc:
        if svc.tasks_client is None and svc.pr_client is None:
            slug = svc.workspace.slug if svc.workspace else "?"
            err.print(f"[yellow]В воркспейсе «{slug}» нет внешнего провайдера — синкать нечего.[/yellow]")
            raise typer.Exit(code=1)
        result = svc.sync()
    if json_out:
        _emit_json({
            "run_id": result.run_id,
            "counts": result.counts,
            "deltas": [d.model_dump() for d in result.deltas],
        })
        return
    console.print(f"[green]Синк #{result.run_id}[/green]  " + "  ".join(
        f"{k}={v}" for k, v in result.counts.items()
    ))
    _render_deltas(result.deltas)


@app.command()
def changes(
    clear: bool = typer.Option(False, "--clear", help="Закрыть (очистить) накопленные изменения."),
    json_out: bool = typer.Option(False, "--json", help="Вывести JSON."),
) -> None:
    """Накопленные изменения (копятся между синками, пока не закрыты). --clear очищает."""
    with _store() as store:
        if clear:
            store.clear_pending_changes()
            console.print("[green]Изменения закрыты.[/green]")
            return
        deltas = store.pending_changes()
    if json_out:
        _emit_json([d.model_dump() for d in deltas])
    else:
        _render_deltas(deltas)


# --------------------------------------------------------------------------- #
# dashboard
# --------------------------------------------------------------------------- #


def _full_sync_dashboard() -> DashboardData:
    """Полный синк всех секций + снимок из памяти (для --sync и авто-синка дашборда).

    В отличие от _service(), ошибки доступа (JiraError/BitbucketError) НЕ гасятся в
    typer.Exit, а пробрасываются: дашборд показывает их в статусе/уведомлении сам,
    а не печатает в stderr поверх TUI (иначе в уведомление прилетала пустая строка).
    """
    _prepare_db()
    with Store(str(db_path())) as probe:
        ws = workspaces.resolve_workspace(probe, explicit=_WORKSPACE_ARG)
    with Service.for_workspace(ws) as svc:
        svc.sync()
        return svc.dashboard()


def _memory_dashboard() -> DashboardData:
    """Снимок из памяти без сети (для быстрого авто-обновления локальных вкладок)."""
    with _store() as store:
        return dashboard_from_memory(store)


def _ack_changes() -> DashboardData:
    """Очистить ВСЕ накопленные изменения и вернуть свежий снимок (клавиша C в TUI)."""
    with _store() as store:
        store.clear_pending_changes()
        return dashboard_from_memory(store)


def _clear_changes(pairs: list[tuple[str, str]]) -> DashboardData:
    """Очистить изменения активной секции (клавиша c / кнопка ✕ очистить)."""
    with _store() as store:
        store.clear_pending_changes(pairs)
        return dashboard_from_memory(store)


def _mentions_seen(mention_ids: Optional[list[int]]) -> DashboardData:
    """Пометить упоминания прочитанными: перечисленные либо все (None)."""
    with _store() as store:
        store.mark_mentions_seen(mention_ids)
        return dashboard_from_memory(store)


@app.command()
def dashboard(
    do_sync: bool = typer.Option(False, "--sync", help="Сначала синхронизировать все секции."),
    auto_update: bool = typer.Option(
        False, "--auto-update", "-a",
        help="Авто-обновление: локальные вкладки (Работы/Фичи) — раз в 5с, "
             "сетевые таблицы — раз в 10 мин, открытая задача/PR — раз в минуту.",
    ),
    fast_interval: float = typer.Option(
        5.0, "--fast-interval", help="Интервал авто-обновления локальных вкладок (Работы/Фичи), сек."),
    slow_interval: float = typer.Option(
        900.0, "--slow-interval", help="Интервал авто-синка сетевых таблиц (задачи/PR), сек. Отсчёт ведётся от ОКОНЧАНИЯ предыдущего синка."),
    detail_interval: float = typer.Option(
        60.0, "--detail-interval", help="Интервал авто-дотягивания открытой задачи/PR из сети, сек."),
    index_interval: float = typer.Option(
        120.0, "--index-interval",
        help="Интервал переиндексации папок воркспейса (git-ветки в «Структуре»), сек. "
             "0 — только при открытии дашборда. Работает и без -a: индексация локальная."),
    json_out: bool = typer.Option(False, "--json", help="Вывести JSON вместо TUI (для Claude)."),
) -> None:
    """Дашборд: задачи на мне, упоминания, PR и изменения. По умолчанию — из памяти."""
    # _full_sync_dashboard пробрасывает ошибки доступа (для авто-синка TUI); в
    # синхронных ветках команды показываем их так же чисто, как остальные команды.
    def _initial_sync() -> DashboardData:
        try:
            return _full_sync_dashboard()
        except (JiraError, BitbucketError, GitHubError) as exc:
            err.print(f"[red]Ошибка авторизации:[/red] {exc}")
            raise typer.Exit(code=1)

    if json_out:
        data = _initial_sync() if do_sync else dashboard_from_memory(_store())
        _emit_json(data.to_json_dict())
        return

    # TUI: начальные данные — из памяти (без токенов); refresh активной вкладки — по сети, лениво.
    # Если воркспейс определить не удалось (папка ни к одному не привязана и активного нет) —
    # не падаем, а стартуем с экрана выбора: дашборд без контура показывать нечего.
    needs_picker = False
    try:
        with _open_store() as store:
            # Дашборд открывается там, где его закрыли (prefer_active): его держат одним
            # окном и переключают воркспейс руками — папка запуска тут не указ. Выбор
            # сразу и закрепляем, чтобы следующий запуск был предсказуем без «W».
            global _WORKSPACE_ARG
            ws = workspaces.resolve_workspace(
                store, explicit=_WORKSPACE_ARG, prefer_active=True)
            workspaces.set_active(store, ws)
            _WORKSPACE_ARG = ws.slug
            store.use_workspace(ws.id)
            data = _initial_sync() if do_sync else dashboard_from_memory(store)
            cfg = workspaces.config_for_workspace(store, ws)
    except workspaces.WorkspaceNotSelected:
        needs_picker = True
        data = DashboardData()
        cfg = load_config()

    from .dashboard import JwuDashboard  # ленивый импорт textual

    env_label = ""
    if cfg.jira.base_url:
        env_label = (f"{cfg.jira.project} @ "
                     f"{urlparse(cfg.jira.base_url).netloc or cfg.jira.base_url}")

    JwuDashboard(
        data,
        memory_fn=_memory_dashboard,
        full_sync_fn=_full_sync_dashboard,
        pr_detail_fn=_pr_detail,
        issue_get_fn=_issue_detail,
        job_get_fn=_job_get,
        job_delete_fn=_job_delete,
        job_status_fn=_job_set_status,
        ack_changes_fn=_ack_changes,
        clear_changes_fn=_clear_changes,
        mentions_seen_fn=_mentions_seen,
        workspaces_fn=_tui_workspaces,
        workspace_switch_fn=_tui_switch_workspace,
        workspace_create_fn=_tui_create_workspace,
        workspace_delete_fn=_tui_delete_workspace,
        path_add_fn=_tui_add_path,
        path_remove_fn=_tui_remove_path,
        feature_get_fn=_feature_get,
        feature_jobs_fn=_feature_jobs,
        feature_create_fn=_feature_create,
        feature_status_fn=_feature_set_status,
        feature_edit_fn=_feature_set_title,
        rule_get_fn=_rule_get,
        rule_create_fn=_rule_create,
        rule_edit_fn=_rule_apply_edit,
        rule_delete_fn=_rule_delete,
        tags_fn=_workspace_tags,
        cwd=str(Path.cwd()),
        start_with_picker=needs_picker,
        jira_base=cfg.jira.base_url,
        env_label=env_label,
        auto_update=auto_update,
        fast_interval=fast_interval,
        slow_interval=slow_interval,
        detail_interval=detail_interval,
        index_interval=index_interval,
    ).run()


def _pr_detail(project: str, repo: str, pr_id: int):
    """Лениво подтянуть детали PR для экрана PR."""
    with _service_with_prs() as svc:
        return svc.pr_detail(project, repo, pr_id)


def _issue_detail(key: str) -> Issue:
    """Дотянуть свежую карточку задачи из сети (для авто-рефреша открытого экрана)."""
    with _service_with_jira() as svc:
        return svc.issue(key)


def _job_get(job_id: int):
    """Прочитать работу из памяти (для экрана работы в TUI)."""
    with _store() as store:
        return store.get_job(job_id)


def _job_delete(job_id: int) -> None:
    """Удалить работу (для кнопки удаления в TUI)."""
    with _store() as store:
        store.delete_job(job_id)


def _job_set_status(job_id: int, status: str) -> None:
    """Сменить статус работы (для кнопки «закрыть» в TUI)."""
    with _store() as store:
        store.set_job_status(job_id, status)


# --- колбэки TUI по воркспейсам и фичам ------------------------------------- #
# Смена воркспейса в TUI = подмена глобального выбора для последующих вызовов:
# все остальные колбэки ходят через _store(), который читает _WORKSPACE_ARG.


def _tui_workspaces() -> list[Workspace]:
    """Список воркспейсов со счётчиком работ (для экрана выбора)."""
    with _open_store() as store:
        items = store.list_workspaces()
        for ws in items:
            store.use_workspace(ws.id)
            ws.jobs_count = len(store.list_jobs())
        return items


def _tui_switch_workspace(workspace_id: int) -> DashboardData:
    global _WORKSPACE_ARG
    with _open_store() as store:
        ws = store.get_workspace(workspace_id)
        if ws is None:
            raise ValueError(f"воркспейс #{workspace_id} не найден")
        _WORKSPACE_ARG = ws.slug
        workspaces.set_active(store, ws)
        store.use_workspace(ws.id)
        return dashboard_from_memory(store)


def _tui_create_workspace(slug: str) -> None:
    with _open_store() as store:
        workspaces.create(store, slug)


def _tui_delete_workspace(workspace_id: int) -> DashboardData:
    """Удалить воркспейс из TUI и вернуть снимок уже другого (активного) контура."""
    global _WORKSPACE_ARG
    with _open_store() as store:
        ws = store.get_workspace(workspace_id)
        if ws is None:
            raise ValueError(f"воркспейс #{workspace_id} не найден")
        if len(store.list_workspaces(include_archived=True)) <= 1:
            raise ValueError("это единственный воркспейс — удалять нечего")
        store.delete_workspace(workspace_id)
        if (store.get_meta(workspaces.ACTIVE_META_KEY) or "") == ws.slug:
            store.set_meta(workspaces.ACTIVE_META_KEY, "")
        if _WORKSPACE_ARG == ws.slug:
            _WORKSPACE_ARG = None
        # переезжаем в первый оставшийся, иначе дашборду нечего показывать
        rest = store.list_workspaces()
        target = rest[0] if rest else None
        if target is not None:
            _WORKSPACE_ARG = target.slug
            workspaces.set_active(store, target)
            store.use_workspace(target.id)
        return dashboard_from_memory(store)


def _tui_add_path(workspace_id: int, path: str) -> DashboardData:
    with _open_store() as store:
        ws = store.get_workspace(workspace_id)
        if ws is None:
            raise ValueError(f"воркспейс #{workspace_id} не найден")
        workspaces.add_path(store, ws, path)
        store.use_workspace(ws.id)
        return dashboard_from_memory(store)


def _tui_remove_path(workspace_id: int, path: str) -> DashboardData:
    with _open_store() as store:
        store.remove_workspace_path(path, workspace_id)
        store.use_workspace(workspace_id)
        return dashboard_from_memory(store)


def _feature_get(feature_id: int):
    with _store() as store:
        return store.get_feature(feature_id)


def _feature_jobs(feature_id: int) -> list[Job]:
    with _store() as store:
        return store.list_jobs(feature_id=feature_id)


def _feature_create(title: str) -> None:
    with _store() as store:
        store.create_feature(title)


def _feature_set_status(feature_id: int, status: str) -> None:
    with _store() as store:
        store.update_feature(feature_id, status=status)


def _feature_set_title(feature_id: int, title: str) -> None:
    with _store() as store:
        store.update_feature(feature_id, title=title)


def _rule_get(rule_id: int):
    with _store() as store:
        return store.get_rule(rule_id)


def _rule_create(values: dict) -> None:
    with _store() as store:
        store.add_rule(values.get("title", ""), text=values.get("text", ""),
                       kind=values.get("kind", "info"), tag=values.get("tag", ""))


def _rule_apply_edit(rule_id: int, values: dict) -> None:
    with _store() as store:
        store.update_rule(rule_id, **values)


def _rule_delete(rule_id: int) -> None:
    with _store() as store:
        store.delete_rule(rule_id)


def _workspace_tags() -> list[str]:
    """Теги папок контура — подсказка при выборе области действия правила."""
    with _open_store() as store:
        ws = _resolve_workspace(store)
        return list(store.all_tags(ws.id))


# --------------------------------------------------------------------------- #
# action: day-analyze (контекст + промпт для Claude Code)
# --------------------------------------------------------------------------- #

_DAY_PROMPT = """## Что нужно сделать
Составь КРАТКУЮ сводку-план рабочего дня по данным ниже (без глубокого погружения — только суть, я разберусь сам).
Сочетай ДВА среза: дельты (что изменилось) И текущее состояние PR/задач (даже без свежей дельты состояние может требовать действия).
Для каждого моего PR/задачи — конкретный следующий шаг. Ориентиры:
- PR `состояние: конфликт` → поправить merge-конфликт (приоритет, если апрувы собраны).
- PR `апрувы собраны`, без NEEDS_WORK/конфликта, а задача не на тестах → перевести задачу на тесты.
- PR `есть NEEDS_WORK` / новые комменты → ответить/поправить по замечаниям.
- PR `нет ревьюверов` → назначить ревьюверов; `ждёт апрувов` давно → пнуть.
- Упоминание с пометкой `· новое` → прочитать, понять, что от меня хотят, и ответить.
- База — дельты: новые комменты, смена статуса, апрувы, новые PR; `resolved` → закрыть работу.
Пиши сжато, маркерами, без воды; группируй по действиям."""


def _pr_state(pr: PR) -> str:
    """Короткая готовность PR для эвристик: что мешает мержу прямо сейчас."""
    if pr.conflicted:
        return "конфликт"
    if any((r.status or "") == "NEEDS_WORK" for r in pr.reviewers):
        return "есть NEEDS_WORK"
    if not pr.reviewers:
        return "нет ревьюверов"
    if all(r.approved for r in pr.reviewers):
        return "апрувы собраны"
    return "ждёт апрувов"


def _pr_line(pr: PR) -> str:
    revs = ", ".join(
        f"{r.display_name or r.name}:{'A' if r.approved else (r.status or 'N')}"
        for r in pr.reviewers
    ) or "—"
    conflict = "КОНФЛИКТ" if pr.conflicted else ("ok" if pr.conflicted is False else "?")
    return (f'- {pr.project}/{pr.repository}#{pr.id} "{pr.title}" — {conflict}; '
            f"состояние: {_pr_state(pr)}; ревью: {revs}; комментов: {pr.comment_count}; "
            f"обновлён: {fmt_ago(pr.updated)}")


def _render_day_context_md(ctx: DayContext) -> str:
    L: list[str] = [
        "# Контекст дневного анализа (jwu)",
        f"Пользователь: {ctx.me_display or '—'} ({ctx.user or '—'}). Синк: {ctx.synced_at or '—'}.",
        "",
        _DAY_PROMPT,
        "",
        f"## Изменения с прошлого синка ({len(ctx.deltas)})",
    ]
    L += [f"- [{d.kind}] {d.key} {d.detail} — {d.summary}" for d in ctx.deltas] or ["- нет"]

    L.append(f"\n## Мои задачи ({len(ctx.mine)})")
    L += [
        f"- {it.key} [{it.status}] ({it.priority}) assignee: {it.assignee or '—'} — {it.summary}"
        for it in ctx.mine
    ] or ["- нет"]

    for header, prs in (("Мои PR", ctx.prs_mine), ("PR на ревью", ctx.prs_review)):
        L.append(f"\n## {header} ({len(prs)})")
        if not prs:
            L.append("- нет")
        for pr in prs:
            L.append(_pr_line(pr))
            for c in ctx.pr_comments.get(pr.id, [])[:8]:
                loc = f"{c.file}:{c.line} " if c.file else ""
                text = " ".join((c.text or "").split())[:200]
                L.append(f"    - {loc}{c.author}: {text}")

    fresh = [m for m in ctx.mentions if not m.seen]
    L.append(f"\n## Упоминания ({len(ctx.mentions)}, новых {len(fresh)})")
    if not ctx.mentions:
        L.append("- нет")
    for m in ctx.mentions:
        mark = " · новое" if not m.seen else ""
        when = fmt_dt(m.created) if m.created else "—"
        L.append(f"- {m.task_key} [{when}] от {m.author or '—'}{mark} — {m.summary}")
        L.append(f"  > {' '.join((m.text or '').split())[:300]}")
    return "\n".join(L)


def _day_context_json(ctx: DayContext) -> dict:
    return {
        "user": ctx.user,
        "me_display": ctx.me_display,
        "synced_at": ctx.synced_at,
        "deltas": [d.model_dump() for d in ctx.deltas],
        "mine": [i.model_dump() for i in ctx.mine],
        "prs_mine": [p.model_dump() for p in ctx.prs_mine],
        "prs_review": [p.model_dump() for p in ctx.prs_review],
        "mentions": [m.model_dump() for m in ctx.mentions],
        "pr_comments": {
            str(pid): [c.model_dump() for c in cs] for pid, cs in ctx.pr_comments.items()
        },
    }


@action_app.command("day-analyze")
def day_analyze(json_out: bool = typer.Option(False, "--json", help="Вывести JSON-контекст.")) -> None:
    """Фулл-синк + расширенный контекст и промпт для дневного анализа (для Claude Code)."""
    with _service_with_jira() as svc:
        ctx = svc.collect_day_context()
    if json_out:
        _emit_json(_day_context_json(ctx))
    else:
        typer.echo(_render_day_context_md(ctx))


# --------------------------------------------------------------------------- #
# db: размер файла, чистка снапшотов, VACUUM
# --------------------------------------------------------------------------- #


def _size_human(n: int) -> str:
    size = float(max(0, n))
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024 or unit == "ГБ":
            return f"{int(size)} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} Б"


@db_app.command("stats")
def db_stats(json_out: bool = typer.Option(False, "--json", help="Вывести JSON.")) -> None:
    """Сколько занимает БД и из чего она состоит (по воркспейсам)."""
    with _open_store() as store:
        current = store.workspace_id
        rows = []
        for ws in store.list_workspaces(include_archived=True):
            store.use_workspace(ws.id)
            rows.append((ws.slug, store.snapshot_counts()))
        store.use_workspace(current)
        payload = {
            "path": str(db_path()),
            "size": store.db_size(),
            "free_ratio": round(store.free_ratio(), 4),
            "workspaces": {slug: counts for slug, counts in rows},
        }
    if json_out:
        _emit_json(payload)
        return
    console.print(f"[b]{payload['path']}[/b]")
    console.print(f"Размер: {_size_human(payload['size'])}   ·   "
                  f"свободно внутри файла: {payload['free_ratio'] * 100:.1f}% "
                  f"[dim](вернёт jwu db vacuum)[/dim]")
    table = Table(show_header=True, header_style="bold")
    table.add_column("Воркспейс", style="cyan")
    table.add_column("Снапшотов задач", justify="right")
    table.add_column("Задач", justify="right")
    table.add_column("Снапшотов PR", justify="right")
    table.add_column("PR", justify="right")
    table.add_column("Прогонов", justify="right")
    for slug, c in rows:
        table.add_row(slug, str(c["issue_snapshots"]), str(c["issue_keys"]),
                      str(c["pr_snapshots"]), str(c["pr_ids"]), str(c["sync_runs"]))
    console.print(table)


@db_app.command("prune")
def db_prune(
    days: int = typer.Option(AUTO_PRUNE_DAYS, "--days",
                             help="Удалять снапшоты старше стольких дней."),
    apply: bool = typer.Option(False, "--apply",
                               help="Выполнить удаление. Без флага — только показать."),
    vacuum: bool = typer.Option(False, "--vacuum",
                                help="После удаления пересобрать файл (долго на большой БД)."),
    json_out: bool = typer.Option(False, "--json", help="Вывести JSON."),
) -> None:
    """Снести старые снапшоты во ВСЕХ воркспейсах. По умолчанию — сухой прогон.

    Сохраняется всё, на чём держатся дельты: свежайший снапшот каждой задачи и PR и
    последние надёжные прогоны каждой вкладки. Остальное старше `--days` — под нож.
    """
    with _open_store() as store:
        before = store.db_size()
        reports = store.prune_all_workspaces(days=days, dry_run=not apply)
        freed = store.vacuum() if (apply and vacuum) else 0
        payload = {
            "dry_run": not apply,
            "days": days,
            "size_before": before,
            "size_after": store.db_size(),
            "freed": freed,
            "workspaces": {
                slug: {"issue_snapshots": r.issue_snapshots, "pr_snapshots": r.pr_snapshots,
                       "sync_runs": r.sync_runs, "protected_runs": r.protected_runs}
                for slug, r in reports.items()
            },
        }
    if json_out:
        _emit_json(payload)
        return
    total = sum(r.total for r in reports.values())
    head = ("[yellow]Сухой прогон[/yellow]" if not apply else "[green]Удалено[/green]")
    console.print(f"{head}: {total} записей старше {days} дн.")
    table = Table(show_header=True, header_style="bold")
    table.add_column("Воркспейс", style="cyan")
    table.add_column("Снапшотов задач", justify="right")
    table.add_column("Снапшотов PR", justify="right")
    table.add_column("Прогонов", justify="right")
    table.add_column("Защищено прогонов", justify="right")
    for slug, r in reports.items():
        table.add_row(slug, str(r.issue_snapshots), str(r.pr_snapshots),
                      str(r.sync_runs), str(r.protected_runs))
    console.print(table)
    if not apply:
        console.print("[dim]Ничего не удалено. Повтори с [/dim][cyan]--apply[/cyan]"
                      "[dim]; место вернёт [/dim][cyan]--vacuum[/cyan][dim] "
                      "(или jwu db vacuum).[/dim]")
    elif freed:
        console.print(f"VACUUM: файл похудел на {_size_human(freed)}")
    elif vacuum:
        console.print("[dim]VACUUM ничего не вернул.[/dim]")


@db_app.command("vacuum")
def db_vacuum() -> None:
    """Пересобрать файл БД, вернув освободившееся место ОС (долго на большой базе)."""
    with _open_store() as store:
        before, ratio = store.db_size(), store.free_ratio()
        console.print(f"Пересобираю {_size_human(before)} "
                      f"[dim](свободно внутри: {ratio * 100:.1f}%)[/dim]…")
        freed = store.vacuum()
        console.print(f"[green]Готово[/green]: {_size_human(store.db_size())} "
                      f"(освободилось {_size_human(freed)})")


# --------------------------------------------------------------------------- #
# notes
# --------------------------------------------------------------------------- #


@app.command()
def note(
    key: str = typer.Argument(..., help="Ключ задачи."),
    text: str = typer.Argument(..., help="Текст заметки."),
    json_out: bool = typer.Option(False, "--json", help="Вывести JSON."),
) -> None:
    """Записать заметку Claude по задаче."""
    with _store() as store:
        saved = store.add_note(key, text)
    if json_out:
        _emit_json(saved.model_dump())
    else:
        console.print(f"[green]Заметка сохранена[/green] для {key}")


@app.command()
def notes(
    key: str = typer.Argument(..., help="Ключ задачи."),
    json_out: bool = typer.Option(False, "--json", help="Вывести JSON."),
) -> None:
    """Показать заметки по задаче."""
    with _store() as store:
        items = store.get_notes(key)
    if json_out:
        _emit_json([n.model_dump() for n in items])
        return
    if not items:
        console.print(f"[dim]Заметок по {key} нет.[/dim]")
        return
    for n in items:
        console.print(f"[dim]{n.ts} · {n.author}[/dim] {n.text}")


@app.command()
def worklog(
    key: str = typer.Argument(..., help="Ключ задачи, напр. PROJ-5525."),
    time: str = typer.Argument(..., help="Время в формате Jira: «2h 30m», «45m», «1d 4h»."),
    comment: Optional[str] = typer.Option(None, "--comment", "-m", help="Описание работы."),
    started: Optional[str] = typer.Option(
        None, "--started", help="Начало работы, ISO 8601 (по умолчанию — текущий момент)."
    ),
    json_out: bool = typer.Option(False, "--json", help="Вывести JSON."),
) -> None:
    """Залогировать время по задаче в таймтрекер Jira (worklog)."""
    try:
        with _service_with_jira() as svc:
            result = svc.add_worklog(key, time, comment=comment, started=started)
    except (JiraError, GitHubError) as exc:
        if json_out:
            _emit_json({"ok": False, "key": key, "error": str(exc)})
        else:
            console.print(f"[red]✗[/red] {key}: не залогировано — {exc}")
        raise typer.Exit(1)
    if json_out:
        _emit_json({"ok": True, "key": key, "timeSpent": time, "worklog": result})
    else:
        console.print(f"[green]✓[/green] {key}: затрекано {time}")


@app.command()
def worklogs(
    keys: list[str] = typer.Argument(..., help="Ключи задач: PROJ-1 PROJ-2 …"),
    on: str = typer.Option("today", "--on", help="Дата (YYYY-MM-DD или 'today')."),
    json_out: bool = typer.Option(False, "--json", help="Вывести JSON."),
) -> None:
    """Мои уже залогированные worklog'и по задачам за день (проверка двойного трека)."""
    day = datetime.now().date().isoformat() if on.lower() == "today" else on
    with _service_with_jira() as svc:
        data = svc.my_worklogs_on(keys, day)
    if json_out:
        _emit_json(data)
        return
    if not data:
        console.print(f"[dim]За {day} по этим задачам ничего не затрекано.[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("Задача", style="cyan", no_wrap=True)
    table.add_column("Время")
    table.add_column("Описание")
    for key, entries in data.items():
        for e in entries:
            table.add_row(key, e["time"], (e["comment"] or "—").splitlines()[0][:60])
    console.print(table)
    total = sum(e["seconds"] for entries in data.values() for e in entries)
    console.print(f"[dim]Итого за {day}: {total // 3600}h {(total % 3600) // 60}m[/dim]")


# --------------------------------------------------------------------------- #
# jobs / job
# --------------------------------------------------------------------------- #


def _render_jobs_table(jobs: list[Job]) -> None:
    if not jobs:
        console.print("[dim]Работ нет.[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Статус")
    table.add_column("Якорь")
    table.add_column("Записей", justify="right")
    table.add_column("PR")
    table.add_column("Title")
    for j in jobs:
        prs = ", ".join(str(p.pr_id) for p in j.prs) or "—"
        table.add_row(str(j.id), j.status, j.anchor, str(len(j.records)), prs, j.title)
    console.print(table)
    console.print(f"[dim]Всего: {len(jobs)}[/dim]")


@job_app.command("start")
def job_start(
    task_key: Optional[str] = typer.Argument(
        None, help="Ключ задачи Jira, напр. PROJ-399. Без него — работа по фиче или без якоря."
    ),
    feature: Optional[str] = typer.Option(
        None, "--feature", "-f", help="Локальная фича как якорь (id или ключ HOMEJWU-1)."
    ),
    title: str = typer.Option("", "--title", "-t", help="Короткий заголовок работы."),
    json_out: bool = typer.Option(False, "--json", help="Вывести JSON."),
) -> None:
    """Начать новую работу: по задаче Jira, по локальной фиче либо вовсе без якоря."""
    if task_key and feature:
        err.print("[red]Нужно что-то одно: ключ задачи Jira ИЛИ --feature.[/red]")
        raise typer.Exit(code=1)
    if not task_key and not feature and not title:
        err.print("[red]Работе без якоря нужен --title, иначе её не опознать.[/red]")
        raise typer.Exit(code=1)
    with _store() as store:
        feature_row = None
        if feature:
            feature_row = store.get_feature(feature)
            if feature_row is None:
                err.print(f"[red]Фича «{feature}» не найдена.[/red] Список: jwu feature list")
                raise typer.Exit(code=1)
        existing = (
            store.jobs_for_task(task_key) if task_key
            else store.list_jobs(feature_id=feature_row.id) if feature_row
            else []
        )
        job = store.create_job(
            task_key or "", title, feature_id=feature_row.id if feature_row else None
        )
    if json_out:
        _emit_json(job.model_dump())
        return
    where = f" по {job.anchor}" if (task_key or feature_row) else ""
    console.print(f"[green]Работа #{job.id}[/green] начата{where}")
    if existing:
        console.print(f"[dim]Уже есть работы: "
                      f"{', '.join(f'#{j.id}[{j.status}]' for j in existing)}[/dim]")


@job_app.command("add")
def job_add(
    job_id: int = typer.Argument(..., help="ID работы."),
    text: str = typer.Argument(..., help="Текст записи."),
    kind: str = typer.Option("note", "--kind", "-k", help=" | ".join(JOB_RECORD_KINDS) + " (decision — решение с обоснованием, constraint — запрет, bug/bug-resolved — баг/исправлен, test-pass/test-fail — прогон тестов, todo — отложенное).", click_type=click.Choice(JOB_RECORD_KINDS)),
    status: Optional[str] = typer.Option(None, "--status", help="Опц. статус записи (напр. done)."),
    json_out: bool = typer.Option(False, "--json", help="Вывести JSON."),
) -> None:
    """Добавить запись в работу (фаза/пункт/замечание)."""
    with _store() as store:
        if store.get_job(job_id) is None:
            err.print(f"[red]Работа #{job_id} не найдена.[/red]")
            raise typer.Exit(code=1)
        rec = store.add_job_record(job_id, text, kind=kind, status=status)
    if json_out:
        _emit_json(rec.model_dump())
    else:
        console.print(f"[green]Запись добавлена[/green] в работу #{job_id} (kind={kind})")


@job_app.command("link")
def job_link(
    job_id: int = typer.Argument(..., help="ID работы."),
    pr: int = typer.Option(..., "--pr", help="Числовой id PR."),
    project: str = typer.Option("", "--project", help="Ключ проекта Bitbucket."),
    repo: str = typer.Option("", "--repo", help="Slug репозитория."),
) -> None:
    """Привязать PR к работе."""
    with _store() as store:
        if store.get_job(job_id) is None:
            err.print(f"[red]Работа #{job_id} не найдена.[/red]")
            raise typer.Exit(code=1)
        store.link_job_pr(job_id, pr, project=project, repo=repo)
    console.print(f"[green]PR {pr} привязан[/green] к работе #{job_id}")


@job_app.command("status")
def job_status(
    job_id: int = typer.Argument(..., help="ID работы."),
    status: str = typer.Argument(..., help="active | done | paused | cancelled.", click_type=click.Choice(["active", "done", "paused", "cancelled"])),
) -> None:
    """Сменить статус работы."""
    with _store() as store:
        if store.get_job(job_id) is None:
            err.print(f"[red]Работа #{job_id} не найдена.[/red]")
            raise typer.Exit(code=1)
        store.set_job_status(job_id, status)
    console.print(f"[green]Работа #{job_id}[/green] → {status}")


@job_app.command("done")
def job_done(job_id: int = typer.Argument(..., help="ID работы.")) -> None:
    """Пометить работу завершённой."""
    with _store() as store:
        if store.get_job(job_id) is None:
            err.print(f"[red]Работа #{job_id} не найдена.[/red]")
            raise typer.Exit(code=1)
        store.set_job_status(job_id, "done")
    console.print(f"[green]Работа #{job_id}[/green] завершена")


@job_app.command("cancel")
def job_cancel(job_id: int = typer.Argument(..., help="ID работы.")) -> None:
    """Закрыть работу как неактуальную (статус cancelled)."""
    with _store() as store:
        if store.get_job(job_id) is None:
            err.print(f"[red]Работа #{job_id} не найдена.[/red]")
            raise typer.Exit(code=1)
        store.set_job_status(job_id, "cancelled")
    console.print(f"[yellow]Работа #{job_id}[/yellow] закрыта (неактуальна)")


@job_app.command("delete")
def job_delete(
    job_id: int = typer.Argument(..., help="ID работы."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Без подтверждения."),
) -> None:
    """Удалить работу полностью (записи и связи с PR тоже)."""
    with _store() as store:
        if store.get_job(job_id) is None:
            err.print(f"[red]Работа #{job_id} не найдена.[/red]")
            raise typer.Exit(code=1)
        if not yes and not typer.confirm(f"Удалить работу #{job_id} безвозвратно?"):
            raise typer.Exit(code=1)
        store.delete_job(job_id)
    console.print(f"[red]Работа #{job_id}[/red] удалена")


@job_app.command("show")
def job_show(
    job_id: int = typer.Argument(..., help="ID работы."),
    json_out: bool = typer.Option(False, "--json", help="Вывести JSON."),
) -> None:
    """Показать работу: задача, статус, привязанные PR, все записи по времени."""
    with _store() as store:
        job = store.get_job(job_id)
    if job is None:
        err.print(f"[red]Работа #{job_id} не найдена.[/red]")
        raise typer.Exit(code=1)
    if json_out:
        _emit_json(job.model_dump())
        return
    # \[ — иначе rich съедает [active] как разметку
    console.print(f"[bold cyan]Работа #{job.id}[/bold cyan] \\[{job.status}]  {job.title or '—'}")
    anchor_label = "задача" if job.task_key else ("фича" if job.feature_key else "якорь")
    anchor = job.task_key or job.feature_key or "— (работа без задачи)"
    console.print(f"[dim]{anchor_label}:[/dim] {anchor}   "
                  f"[dim]обновлена:[/dim] {fmt_dt(job.updated_at)}")
    if job.prs:
        prs = ", ".join(f"{p.project}/{p.repo}#{p.pr_id}" if p.project else f"#{p.pr_id}" for p in job.prs)
        console.print(f"[dim]PR:[/dim] {prs}")
    if job.records:
        console.print("\n[bold]Записи[/bold]")
        for r in job.records:
            st = f" [{r.status}]" if r.status else ""
            badge = JOB_RECORD_BADGES.get((r.kind or "").lower())
            if badge:
                label, color = badge
                console.print(
                    f"[dim]{fmt_dt(r.ts)}[/dim] [bold {color}]{label}[/bold {color}]{st} "
                    f"[{color}]{r.text}[/{color}]")
            else:
                console.print(f"[dim]{fmt_dt(r.ts)} · {r.kind}{st}[/dim] {r.text}")


@app.command()
def jobs(
    task: Optional[str] = typer.Option(None, "--task", help="Фильтр по ключу задачи."),
    feature: Optional[str] = typer.Option(
        None, "--feature", "-f", help="Фильтр по локальной фиче (id или ключ)."
    ),
    pr: Optional[int] = typer.Option(None, "--pr", help="Фильтр по id PR."),
    status: Optional[str] = typer.Option(None, "--status", help="active | done | paused."),
    json_out: bool = typer.Option(False, "--json", help="Вывести JSON."),
) -> None:
    """Список работ текущего воркспейса (по задаче / фиче / PR / статусу)."""
    with _store() as store:
        feature_id = None
        if feature:
            row = store.get_feature(feature)
            if row is None:
                err.print(f"[red]Фича «{feature}» не найдена.[/red]")
                raise typer.Exit(code=1)
            feature_id = row.id
        items = store.list_jobs(task_key=task, pr_id=pr, status=status, feature_id=feature_id)
    if json_out:
        _emit_json([j.model_dump() for j in items])
    else:
        _render_jobs_table(items)


# --------------------------------------------------------------------------- #
# Локальные фичи (мини-трекер воркспейса без Jira)
# --------------------------------------------------------------------------- #


def _render_features(items: list[LocalFeature]) -> None:
    if not items:
        console.print("[dim]Фич нет. Завести: jwu feature add \"название\"[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("Ключ", style="cyan", no_wrap=True)
    table.add_column("Статус")
    table.add_column("Приоритет")
    table.add_column("Название")
    for f in items:
        label, color = LOCAL_FEATURE_BADGES.get(f.status, (f.status, "white"))
        table.add_row(f.key, f"[{color}]{label}[/{color}]", f.priority or "—", f.title)
    console.print(table)
    console.print(f"[dim]Всего: {len(items)}[/dim]")


@feature_app.command("add")
def feature_add(
    title: str = typer.Argument(..., help="Название фичи."),
    desc: str = typer.Option("", "--desc", "-d", help="Описание / что нужно сделать."),
    priority: str = typer.Option("", "--priority", "-p", help="Приоритет (свободный текст)."),
    json_out: bool = typer.Option(False, "--json", help="Вывести JSON."),
) -> None:
    """Завести локальную фичу — якорь для работ там, где нет Jira."""
    with _store() as store:
        feature = store.create_feature(title, description=desc, priority=priority)
    if json_out:
        _emit_json(feature.model_dump())
    else:
        console.print(f"[green]{feature.key}[/green] · {feature.title}")


@feature_app.command("list")
def feature_list(
    status: Optional[str] = typer.Option(
        None, "--status", "-s", help=" | ".join(LOCAL_FEATURE_STATUSES),
        click_type=click.Choice(LOCAL_FEATURE_STATUSES),
    ),
    json_out: bool = typer.Option(False, "--json", help="Вывести JSON."),
) -> None:
    """Список локальных фич воркспейса."""
    with _store() as store:
        items = store.list_features(status=status)
    if json_out:
        _emit_json([f.model_dump() for f in items])
    else:
        _render_features(items)


@feature_app.command("show")
def feature_show(
    ref: str = typer.Argument(..., help="Ключ (HOMEJWU-1) или id."),
    json_out: bool = typer.Option(False, "--json", help="Вывести JSON."),
) -> None:
    """Карточка фичи вместе со связанными работами."""
    with _store() as store:
        feature = store.get_feature(ref)
        if feature is None:
            err.print(f"[red]Фича «{ref}» не найдена.[/red]")
            raise typer.Exit(code=1)
        linked = store.list_jobs(feature_id=feature.id)
    if json_out:
        payload = feature.model_dump()
        payload["jobs"] = [j.model_dump() for j in linked]
        _emit_json(payload)
        return
    label, color = LOCAL_FEATURE_BADGES.get(feature.status, (feature.status, "white"))
    console.print(f"[bold cyan]{feature.key}[/bold cyan] [{color}]{label}[/{color}]  {feature.title}")
    if feature.priority:
        console.print(f"[dim]приоритет:[/dim] {feature.priority}")
    console.print(f"[dim]обновлена:[/dim] {fmt_dt(feature.updated_at)}")
    if feature.description:
        console.print(f"\n{feature.description}")
    if linked:
        console.print("\n[bold]Работы[/bold]")
        for j in linked:
            console.print(f"  #{j.id} \\[{j.status}] {j.title or '—'} "
                          f"[dim]({len(j.records)} записей)[/dim]")


@feature_app.command("status")
def feature_status(
    ref: str = typer.Argument(..., help="Ключ или id фичи."),
    status: str = typer.Argument(
        ..., help=" | ".join(LOCAL_FEATURE_STATUSES),
        click_type=click.Choice(LOCAL_FEATURE_STATUSES),
    ),
) -> None:
    """Сменить статус фичи."""
    with _store() as store:
        feature = store.get_feature(ref)
        if feature is None:
            err.print(f"[red]Фича «{ref}» не найдена.[/red]")
            raise typer.Exit(code=1)
        store.update_feature(feature.id, status=status)
    console.print(f"[green]{feature.key}[/green]: {feature.status} → {status}")


@feature_app.command("edit")
def feature_edit(
    ref: str = typer.Argument(..., help="Ключ или id фичи."),
    title: Optional[str] = typer.Option(None, "--title", "-t", help="Новое название."),
    desc: Optional[str] = typer.Option(None, "--desc", "-d", help="Новое описание."),
    priority: Optional[str] = typer.Option(None, "--priority", "-p", help="Новый приоритет."),
) -> None:
    """Изменить название / описание / приоритет фичи."""
    with _store() as store:
        feature = store.get_feature(ref)
        if feature is None:
            err.print(f"[red]Фича «{ref}» не найдена.[/red]")
            raise typer.Exit(code=1)
        store.update_feature(feature.id, title=title, description=desc, priority=priority)
    console.print(f"[green]{feature.key} обновлена.[/green]")


@feature_app.command("rm")
def feature_rm(
    ref: str = typer.Argument(..., help="Ключ или id фичи."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Без подтверждения."),
) -> None:
    """Удалить фичу. Работы по ней остаются, но теряют якорь."""
    with _store() as store:
        feature = store.get_feature(ref)
        if feature is None:
            err.print(f"[red]Фича «{ref}» не найдена.[/red]")
            raise typer.Exit(code=1)
        linked = store.list_jobs(feature_id=feature.id)
        if not yes and not typer.confirm(
            f"Удалить фичу {feature.key}? Работ по ней: {len(linked)}", default=False
        ):
            raise typer.Exit(code=1)
        store.delete_feature(feature.id)
    console.print(f"[red]{feature.key}[/red] удалена")


@app.command()
def features(
    status: Optional[str] = typer.Option(
        None, "--status", "-s", help=" | ".join(LOCAL_FEATURE_STATUSES),
        click_type=click.Choice(LOCAL_FEATURE_STATUSES),
    ),
    json_out: bool = typer.Option(False, "--json", help="Вывести JSON."),
) -> None:
    """Список локальных фич (алиас `jwu feature list`)."""
    feature_list(status=status, json_out=json_out)


# --------------------------------------------------------------------------- #
# rules: правила воркспейса
# --------------------------------------------------------------------------- #


def _read_rule_text(text: Optional[str], file: Optional[str]) -> Optional[str]:
    """Текст правила из --text либо из --file (`-` — stdin, для многострочного)."""
    if file is None:
        return text
    if file == "-":
        return sys.stdin.read().strip()
    return Path(file).read_text(encoding="utf-8").strip()


def _render_rules(items: list) -> None:
    if not items:
        console.print("[dim]Правил пока нет. Завести: jwu rule add \"…\" --kind constraint[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Тип", no_wrap=True)
    table.add_column("Тег", no_wrap=True)
    table.add_column("Правило")
    for r in items:
        label, color = WORKSPACE_RULE_BADGES.get(r.kind, (r.kind, "white"))
        title = r.title + (" [dim]…[/dim]" if r.text else "")
        table.add_row(str(r.id), f"[{color}]{label}[/{color}]",
                      r.tag or "[dim]общее[/dim]", title)
    console.print(table)


@rule_app.command("add")
def rule_add(
    title: str = typer.Argument(..., help="Короткая суть правила (одной строкой)."),
    kind: str = typer.Option(
        "info", "--kind", "-k", help=" | ".join(WORKSPACE_RULE_KINDS),
        click_type=click.Choice(WORKSPACE_RULE_KINDS),
    ),
    tag: str = typer.Option("", "--tag", "-t",
                            help="Тег папки; без него правило общее для воркспейса."),
    text: Optional[str] = typer.Option(None, "--text", help="Подробности."),
    file: Optional[str] = typer.Option(
        None, "--file", "-f", help="Взять подробности из файла; `-` — из stdin."),
    json_out: bool = typer.Option(False, "--json", help="Вывести JSON."),
) -> None:
    """Завести правило воркспейса — то, что агент обязан знать о проекте."""
    with _store() as store:
        rule = store.add_rule(title, text=_read_rule_text(text, file) or "",
                              kind=kind, tag=tag)
    if json_out:
        _emit_json(rule.model_dump())
        return
    label, color = WORKSPACE_RULE_BADGES.get(rule.kind, (rule.kind, "white"))
    scope = f" [dim]({rule.tag})[/dim]" if rule.tag else ""
    console.print(f"[green]Правило #{rule.id}[/green] [{color}]{label}[/{color}]{scope}  {rule.title}")


@rule_app.command("list")
def rule_list(
    kind: Optional[str] = typer.Option(
        None, "--kind", "-k", help=" | ".join(WORKSPACE_RULE_KINDS),
        click_type=click.Choice(WORKSPACE_RULE_KINDS),
    ),
    tag: Optional[str] = typer.Option(
        None, "--tag", "-t", help="Правила этого тега И общие; пустая строка — только общие."),
    json_out: bool = typer.Option(False, "--json", help="Вывести JSON."),
) -> None:
    """Правила воркспейса."""
    with _store() as store:
        items = store.list_rules(kind=kind, tag=tag)
    if json_out:
        _emit_json([r.model_dump() for r in items])
        return
    _render_rules(items)


@rule_app.command("show")
def rule_show(
    rule_id: int = typer.Argument(..., help="ID правила."),
    json_out: bool = typer.Option(False, "--json", help="Вывести JSON."),
) -> None:
    """Правило целиком, вместе с подробностями."""
    with _store() as store:
        rule = store.get_rule(rule_id)
    if rule is None:
        err.print(f"[red]Правило #{rule_id} не найдено.[/red]")
        raise typer.Exit(code=1)
    if json_out:
        _emit_json(rule.model_dump())
        return
    label, color = WORKSPACE_RULE_BADGES.get(rule.kind, (rule.kind, "white"))
    console.print(f"[bold cyan]#{rule.id}[/bold cyan] [{color}]{label}[/{color}]  {rule.title}")
    console.print(f"[dim]область:[/dim] {rule.tag or 'весь воркспейс'}   "
                  f"[dim]обновлено:[/dim] {fmt_dt(rule.updated_at)}")
    if rule.text:
        console.print(f"\n{rule.text}")


@rule_app.command("edit")
def rule_edit(
    rule_id: int = typer.Argument(..., help="ID правила."),
    title: Optional[str] = typer.Option(None, "--title", help="Новая суть."),
    kind: Optional[str] = typer.Option(
        None, "--kind", "-k", help=" | ".join(WORKSPACE_RULE_KINDS),
        click_type=click.Choice(WORKSPACE_RULE_KINDS),
    ),
    tag: Optional[str] = typer.Option(None, "--tag", "-t", help="Новый тег («» — сделать общим)."),
    text: Optional[str] = typer.Option(None, "--text", help="Новые подробности."),
    file: Optional[str] = typer.Option(
        None, "--file", "-f", help="Взять подробности из файла; `-` — из stdin."),
) -> None:
    """Изменить правило."""
    with _store() as store:
        if store.get_rule(rule_id) is None:
            err.print(f"[red]Правило #{rule_id} не найдено.[/red]")
            raise typer.Exit(code=1)
        store.update_rule(rule_id, title=title, kind=kind, tag=tag,
                          text=_read_rule_text(text, file))
    console.print(f"[green]Правило #{rule_id} обновлено.[/green]")


@rule_app.command("rm")
def rule_rm(
    rule_id: int = typer.Argument(..., help="ID правила."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Без подтверждения."),
) -> None:
    """Удалить правило."""
    with _store() as store:
        rule = store.get_rule(rule_id)
        if rule is None:
            err.print(f"[red]Правило #{rule_id} не найдено.[/red]")
            raise typer.Exit(code=1)
        if not yes and not typer.confirm(f"Удалить правило «{rule.title}»?", default=False):
            raise typer.Exit(code=1)
        store.delete_rule(rule_id)
    console.print(f"[red]Правило #{rule_id}[/red] удалено")


@app.command()
def rules(
    kind: Optional[str] = typer.Option(
        None, "--kind", "-k", help=" | ".join(WORKSPACE_RULE_KINDS),
        click_type=click.Choice(WORKSPACE_RULE_KINDS),
    ),
    tag: Optional[str] = typer.Option(None, "--tag", "-t", help="Правила тега И общие."),
    json_out: bool = typer.Option(False, "--json", help="Вывести JSON."),
) -> None:
    """Правила воркспейса (алиас `jwu rule list`)."""
    rule_list(kind=kind, tag=tag, json_out=json_out)


if __name__ == "__main__":  # pragma: no cover
    app()
