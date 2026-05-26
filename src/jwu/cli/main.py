"""CLI (Typer). Каждая команда — тонкая обёртка над Service. Везде есть --json для Claude."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import click
import typer
from rich.console import Console
from rich.table import Table

from ..core.bitbucket import BitbucketError
from ..core import secrets
from ..core.config import ConfigError, db_path, load_config, save_config
from ..core.maintenance import ensure_db_available, run_daily_maintenance
from ..skills_install import default_dest as _skills_dest, install_skills
from ..core.jira import JiraError
from ..core.models import JOB_RECORD_BADGES, JOB_RECORD_KINDS, Delta, Issue, Job, Note, PR
from ..core.service import DashboardData, DayContext, Service, dashboard_from_memory
from ..core.store import Store

app = typer.Typer(
    add_completion=False,
    help="Jira + Bitbucket CLI с памятью, для интеграции с Claude Code.",
    no_args_is_help=True,
)
auth_app = typer.Typer(help="Проверка доступа.")
app.add_typer(auth_app, name="auth")
action_app = typer.Typer(help="Действия для Claude Code (контекст + промпт).")
app.add_typer(action_app, name="action")
analysis_app = typer.Typer(help="Сохранённые анализы/планы.")
app.add_typer(analysis_app, name="analysis")
job_app = typer.Typer(help="Работы (jobs): цикл работы над задачей, прогресс и связи с PR.")
app.add_typer(job_app, name="job")

console = Console()
err = Console(stderr=True)


def _prepare_db() -> None:
    """Защита от iCloud-плейсхолдера + ежедневный локальный бэкап. Сообщения — в stderr
    (чтобы не ломать JSON на stdout). Бросает ConfigError, если БД выгружена из iCloud."""
    path = db_path()
    ensure_db_available(path)
    for msg in run_daily_maintenance(path):
        err.print(f"[dim]{msg}[/dim]")


def _service() -> Service:
    try:
        _prepare_db()
        return Service.from_config(load_config())
    except ConfigError as exc:
        err.print(f"[red]Ошибка конфига:[/red] {exc}")
        raise typer.Exit(code=1)
    except (JiraError, BitbucketError) as exc:
        err.print(f"[red]Ошибка авторизации:[/red] {exc}")
        raise typer.Exit(code=1)


def _store() -> Store:
    """Только память — без токенов/сети (для note/notes/changes)."""
    try:
        _prepare_db()
    except ConfigError as exc:
        err.print(f"[red]Ошибка БД:[/red] {exc}")
        raise typer.Exit(code=1)
    return Store(str(db_path()))


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
                      f" · {a.author} · {a.created[:16]}[/dim]")


# --------------------------------------------------------------------------- #
# auth
# --------------------------------------------------------------------------- #


@auth_app.command("check")
def auth_check(json_out: bool = typer.Option(False, "--json", help="Вывести JSON.")) -> None:
    """Проверить токены Jira и Bitbucket."""
    with _service() as svc:
        result = svc.auth_check()
    if json_out:
        _emit_json(result)
        raise typer.Exit(code=0 if result["jira"]["ok"] and result["bitbucket"]["ok"] else 1)
    ok = True
    for name in ("jira", "bitbucket"):
        r = result[name]
        if r["ok"]:
            extra = f" ({r.get('name')})" if r.get("name") else ""
            console.print(f"[green]✓[/green] {name}{extra}")
        else:
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


def _auth_check_report() -> None:
    """Проверить связь по текущему конфигу и напечатать ✓/✗ по Jira и Bitbucket."""
    try:
        with Service.from_config(load_config()) as svc:
            res = svc.auth_check()
        for name in ("jira", "bitbucket"):
            r = res[name]
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
    bitbucket_host: Optional[str] = typer.Option(None, "--bitbucket-host"),
    bitbucket_project: Optional[str] = typer.Option(None, "--bitbucket-project"),
    bitbucket_repo: Optional[str] = typer.Option(None, "--bitbucket-repo"),
    bitbucket_token_opt: Optional[str] = typer.Option(None, "--bitbucket-token"),
    db_path_opt: Optional[str] = typer.Option(None, "--db-path"),
) -> None:
    """Визард настройки (когда вызвано без подкоманды export/import)."""
    if ctx.invoked_subcommand is not None:
        return  # вызвана подкоманда (export/import) — визард не запускаем
    cfg = load_config()

    if non_interactive:
        if jira_host is not None: cfg.jira.base_url = jira_host.rstrip("/")
        if jira_user is not None: cfg.jira.username = jira_user
        if jira_project is not None: cfg.jira.project = jira_project
        if bitbucket_host is not None: cfg.bitbucket.base_url = bitbucket_host.rstrip("/")
        if bitbucket_project is not None: cfg.bitbucket.project = bitbucket_project
        if bitbucket_repo is not None: cfg.bitbucket.repo = bitbucket_repo
        if gate_user is not None: cfg.jira.proxy_basic_user = gate_user
        if db_path_opt is not None: cfg.storage.db_path = db_path_opt
        new_secrets = {
            (cfg.jira.token_service, cfg.jira.token_account): jira_token_opt,
            (cfg.jira.login_service, cfg.jira.username): jira_password,
            (cfg.jira.proxy_basic_service, cfg.jira.proxy_basic_user): gate_password,
            (cfg.bitbucket.token_service, cfg.bitbucket.token_account): bitbucket_token_opt,
        }
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
        cfg.bitbucket.base_url = (bitbucket_host or _prompt_default(
            "Bitbucket host", cfg.bitbucket.base_url)).rstrip("/")
        cfg.bitbucket.project = bitbucket_project or _prompt_default(
            "Bitbucket project", cfg.bitbucket.project)
        cfg.bitbucket.repo = bitbucket_repo or _prompt_default(
            "Bitbucket repo", cfg.bitbucket.repo)
        btok = bitbucket_token_opt if bitbucket_token_opt is not None else _prompt_default(
            "Bitbucket PAT-токен", "", secret=True)
        cur_db = cfg.storage.db_path or str(db_path(cfg))
        cfg.storage.db_path = db_path_opt or _prompt_default("Путь до БД", cur_db)
        new_secrets = {
            (cfg.jira.token_service, cfg.jira.token_account): jtok,
            (cfg.jira.login_service, cfg.jira.username): jpw,
            (cfg.jira.proxy_basic_service, cfg.jira.proxy_basic_user): gpw,
            (cfg.bitbucket.token_service, cfg.bitbucket.token_account): btok,
        }

    path = save_config(cfg)
    saved = 0
    try:
        for (service, account), value in new_secrets.items():
            if value and account:  # пусто/None или нет account => не трогаем
                secrets.set_secret(service, account, value)
                saved += 1
    except Exception as exc:  # noqa: BLE001 — keyring недоступен
        err.print(f"[red]Не удалось записать секрет в keyring:[/red] {exc}\n"
                  f"Задай токен через переменную окружения (JIRA_TOKEN/BITBUCKET_TOKEN).")
        raise typer.Exit(code=1)

    console.print(f"[green]Конфиг сохранён[/green]: {path}  (секретов записано: {saved})")
    _auth_check_report()


@configure_app.command("export")
def configure_export(
    path: str = typer.Argument(..., help="Куда записать бандл (.toml)."),
) -> None:
    """Выгрузить config + СЕКРЕТЫ в переносимый файл (плайнтекст — храните безопасно)."""
    from ..core.config import export_bundle

    n = export_bundle(load_config(), Path(path))
    console.print(f"[green]Бандл записан[/green]: {path}  (секретов: {n})")
    err.print("[yellow]Внимание:[/yellow] файл содержит пароли в открытом виде — "
              "не коммить и храни безопасно.")


@configure_app.command("import")
def configure_import(
    path: str = typer.Argument(..., help="Файл бандла (.toml) из `configure export`."),
) -> None:
    """Применить бандл: записать config.toml и секреты в keyring, затем проверить связь."""
    from ..core.config import import_bundle

    try:
        _cfg, n = import_bundle(Path(path))
    except ConfigError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    except Exception as exc:  # noqa: BLE001 — keyring недоступен
        err.print(f"[red]Не удалось записать секрет в keyring:[/red] {exc}\n"
                  f"Задай токен через переменную окружения (JIRA_TOKEN/BITBUCKET_TOKEN).")
        raise typer.Exit(code=1)

    console.print(f"[green]Импортировано[/green]: config + секретов {n}")
    _auth_check_report()


@app.command("install-claude-skills")
def install_claude_skills(
    dest: Optional[str] = typer.Option(
        None, "--dest", help="Каталог скиллов (по умолчанию ~/.claude/skills)."),
) -> None:
    """Развернуть jwu-скиллы Claude Code из пакета (свежие; существующие заменяются)."""
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


# --------------------------------------------------------------------------- #
# tasks / task
# --------------------------------------------------------------------------- #


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
    with _service() as svc:
        try:
            issues = svc.tasks(view, jql=jql)
        except (ValueError, JiraError) as exc:
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
    with _service() as svc:
        issue = svc.issue(key)
        notes = svc.get_notes(key)
        jobs_list = svc.jobs_for_task(key)
    if json_out:
        payload = issue.model_dump()
        payload["notes"] = [n.model_dump() for n in notes]
        payload["jobs"] = [j.model_dump() for j in jobs_list]
        _emit_json(payload)
        return
    console.print(f"[bold cyan]{issue.key}[/bold cyan]  [{issue.status}]  {issue.summary}")
    console.print(f"[dim]assignee:[/dim] {issue.assignee or '—'}   [dim]priority:[/dim] {issue.priority or '—'}")
    if issue.description:
        console.print(f"\n[bold]Описание[/bold]\n{issue.description}")
    if issue.comments:
        console.print(f"\n[bold]Комментарии ({len(issue.comments)})[/bold]")
        for c in issue.comments:
            console.print(f"[dim]{c.created} · {c.author}[/dim]\n{c.body}\n")
    _render_attachments(issue.attachments)
    if issue.pull_requests or issue.branches:
        console.print("[bold]Development[/bold]")
        for b in issue.branches:
            console.print(f"  ветка: {b.name} [dim]{b.repository}[/dim]")
        for pr in issue.pull_requests:
            console.print(f"  PR {pr.id} [{pr.status}] {pr.name}")
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
    with _service() as svc:
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
    table = Table(show_header=True, header_style="bold")
    table.add_column("PR", style="cyan", no_wrap=True)
    table.add_column("Repo")
    table.add_column("Состояние")
    table.add_column("Конфликт")
    table.add_column("Title")
    for pr in prs:
        conflict = "—" if pr.conflicted is None else ("[red]да[/red]" if pr.conflicted else "нет")
        table.add_row(str(pr.id), f"{pr.project}/{pr.repository}", pr.state, conflict, pr.title)
    console.print(table)
    console.print(f"[dim]Всего: {len(prs)}[/dim]")


@app.command()
def prs(
    view: str = typer.Option("review", "--view", "-v", help="mine | review"),
    no_conflicts: bool = typer.Option(False, "--no-conflicts", help="Не запрашивать статус конфликтов (быстрее)."),
    json_out: bool = typer.Option(False, "--json", help="Вывести JSON."),
) -> None:
    """PR из Bitbucket по роли (мои / на ревью) со статусом merge-конфликта."""
    with _service() as svc:
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
    with _service() as svc:
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
        console.print(f"{indent}{loc}[bold]{c.author}[/bold]: {(c.text or '').strip()}")
    if jobs_list:
        console.print(f"[bold]Работы ({len(jobs_list)})[/bold]")
        for j in jobs_list:
            console.print(f"  #{j.id} [{j.status}] {j.title or '—'} [dim]{j.task_key}[/dim]")


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
    """Синк по команде: тянет вью + PR, пишет снапшот в память, считает дельты."""
    with _service() as svc:
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
    """Полный синк всех секций + снимок из памяти (для --sync)."""
    with _service() as svc:
        svc.sync()
        return svc.dashboard()


def _section_sync_dashboard(section: str) -> DashboardData:
    """Синк одной секции + снимок (для refresh активной вкладки в TUI)."""
    with _service() as svc:
        svc.sync_section(section)
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


@app.command()
def dashboard(
    do_sync: bool = typer.Option(False, "--sync", help="Сначала синхронизировать все секции."),
    auto_update: bool = typer.Option(
        False, "--auto-update", "-a",
        help="Авто-обновление: локальные вкладки (Работы/Анализ) — раз в 5с, "
             "сетевые таблицы — раз в 10 мин, открытая задача/PR — раз в минуту.",
    ),
    fast_interval: float = typer.Option(
        5.0, "--fast-interval", help="Интервал авто-обновления локальных вкладок (Работы/Анализ), сек."),
    slow_interval: float = typer.Option(
        600.0, "--slow-interval", help="Интервал авто-синка сетевых таблиц (задачи/PR), сек."),
    detail_interval: float = typer.Option(
        60.0, "--detail-interval", help="Интервал авто-дотягивания открытой задачи/PR из сети, сек."),
    json_out: bool = typer.Option(False, "--json", help="Вывести JSON вместо TUI (для Claude)."),
) -> None:
    """Дашборд: задачи на мне, упоминания, PR и изменения. По умолчанию — из памяти."""
    if json_out:
        data = _full_sync_dashboard() if do_sync else dashboard_from_memory(_store())
        _emit_json(data.to_json_dict())
        return

    # TUI: начальные данные — из памяти (без токенов); refresh активной вкладки — по сети, лениво.
    if do_sync:
        data = _full_sync_dashboard()
    else:
        with _store() as store:
            data = dashboard_from_memory(store)
    cfg = load_config()

    from .dashboard import JwuDashboard  # ленивый импорт textual

    JwuDashboard(
        data,
        refresh_section_fn=_section_sync_dashboard,
        memory_fn=_memory_dashboard,
        full_sync_fn=_full_sync_dashboard,
        pr_detail_fn=_pr_detail,
        issue_get_fn=_issue_detail,
        analysis_get_fn=_analysis_get,
        job_get_fn=_job_get,
        job_delete_fn=_job_delete,
        job_status_fn=_job_set_status,
        ack_changes_fn=_ack_changes,
        clear_changes_fn=_clear_changes,
        jira_base=cfg.jira.base_url,
        env_label=f"{cfg.jira.project} @ {urlparse(cfg.jira.base_url).netloc or cfg.jira.base_url}",
        auto_update=auto_update,
        fast_interval=fast_interval,
        slow_interval=slow_interval,
        detail_interval=detail_interval,
    ).run()


def _pr_detail(project: str, repo: str, pr_id: int):
    """Лениво подтянуть детали PR для экрана PR."""
    with _service() as svc:
        return svc.pr_detail(project, repo, pr_id)


def _issue_detail(key: str) -> Issue:
    """Дотянуть свежую карточку задачи из сети (для авто-рефреша открытого экрана)."""
    with _service() as svc:
        return svc.issue(key)


def _analysis_get(analysis_id: int):
    """Прочитать сохранённый анализ из памяти (для экрана анализа в TUI)."""
    with _store() as store:
        return store.get_analysis(analysis_id)


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


# --------------------------------------------------------------------------- #
# action: day-analyze (контекст + промпт для Claude Code)
# --------------------------------------------------------------------------- #

_DAY_PROMPT = """## Что нужно сделать
Составь КРАТКУЮ сводку-план рабочего дня по данным ниже (без глубокого погружения — только суть, я разберусь сам):
1. Что изменилось с прошлого синка.
2. Какие PR посмотреть (на ревью) — по строке, почему.
3. Где у моих PR конфликты или замечания (NEEDS_WORK) — что поправить.
4. Где упоминания требуют ответа/правки.
Пиши сжато, маркерами, без воды. Затем сохрани план:
`jwu analysis save --title "День <дата>"` — передав текст плана в stdin."""


def _pr_line(pr: PR) -> str:
    revs = ", ".join(
        f"{r.display_name or r.name}:{'A' if r.approved else (r.status or 'N')}"
        for r in pr.reviewers
    ) or "—"
    conflict = "КОНФЛИКТ" if pr.conflicted else ("ok" if pr.conflicted is False else "?")
    return (f'- {pr.project}/{pr.repository}#{pr.id} "{pr.title}" — {conflict}; '
            f"ревью: {revs}; комментов: {pr.comment_count}")


def _render_day_context_md(ctx: DayContext) -> str:
    L: list[str] = [
        "# Контекст дневного анализа (jwu)",
        f"Пользователь: {ctx.user or '—'}. Синк: {ctx.synced_at or '—'}.",
        "",
        _DAY_PROMPT,
        "",
        f"## Изменения с прошлого синка ({len(ctx.deltas)})",
    ]
    L += [f"- [{d.kind}] {d.key} {d.detail} — {d.summary}" for d in ctx.deltas] or ["- нет"]

    L.append(f"\n## Мои задачи ({len(ctx.mine)})")
    L += [f"- {it.key} [{it.status}] ({it.priority}) {it.summary}" for it in ctx.mine] or ["- нет"]

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

    L.append(f"\n## Упоминания ({len(ctx.mentions)})")
    if not ctx.mentions:
        L.append("- нет")
    for issue, texts in ctx.mentions:
        L.append(f"- {issue.key} [{issue.status}] {issue.summary}")
        for t in texts:
            L.append(f"  > {' '.join((t or '').split())[:300]}")
    return "\n".join(L)


def _day_context_json(ctx: DayContext) -> dict:
    return {
        "user": ctx.user,
        "synced_at": ctx.synced_at,
        "deltas": [d.model_dump() for d in ctx.deltas],
        "mine": [i.model_dump() for i in ctx.mine],
        "prs_mine": [p.model_dump() for p in ctx.prs_mine],
        "prs_review": [p.model_dump() for p in ctx.prs_review],
        "mentions": [
            {"issue": issue.model_dump(), "texts": texts} for issue, texts in ctx.mentions
        ],
        "pr_comments": {
            str(pid): [c.model_dump() for c in cs] for pid, cs in ctx.pr_comments.items()
        },
    }


@action_app.command("day-analyze")
def day_analyze(json_out: bool = typer.Option(False, "--json", help="Вывести JSON-контекст.")) -> None:
    """Фулл-синк + расширенный контекст и промпт для дневного анализа (для Claude Code)."""
    with _service() as svc:
        ctx = svc.collect_day_context()
    if json_out:
        _emit_json(_day_context_json(ctx))
    else:
        typer.echo(_render_day_context_md(ctx))


# --------------------------------------------------------------------------- #
# analysis: сохранённые планы
# --------------------------------------------------------------------------- #


@analysis_app.command("save")
def analysis_save(
    title: str = typer.Option("", "--title", "-t", help="Заголовок."),
    text: Optional[str] = typer.Option(None, "--text", help="Текст (иначе читается stdin)."),
) -> None:
    """Сохранить план/анализ (текст из --text или stdin)."""
    content = (text if text is not None else sys.stdin.read()).strip()
    if not content:
        err.print("[red]Пустой текст — нечего сохранять.[/red]")
        raise typer.Exit(code=1)
    with _store() as store:
        a = store.save_analysis(content, title)
    console.print(f"[green]Сохранено[/green] #{a.id} {a.title}")


@analysis_app.command("list")
def analysis_list(json_out: bool = typer.Option(False, "--json", help="Вывести JSON.")) -> None:
    """Список сохранённых анализов."""
    with _store() as store:
        items = store.list_analyses()
    if json_out:
        _emit_json([a.model_dump() for a in items])
        return
    if not items:
        console.print("[dim]Анализов пока нет.[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Дата")
    table.add_column("Заголовок")
    for a in items:
        table.add_row(str(a.id), a.created_at[:16], a.title)
    console.print(table)


@analysis_app.command("show")
def analysis_show(
    analysis_id: Optional[int] = typer.Argument(None, help="ID (по умолчанию — последний)."),
    json_out: bool = typer.Option(False, "--json", help="Вывести JSON."),
) -> None:
    """Показать анализ по ID (или последний)."""
    with _store() as store:
        a = store.get_analysis(analysis_id)
    if a is None:
        err.print("[red]Анализ не найден.[/red]")
        raise typer.Exit(code=1)
    if json_out:
        _emit_json(a.model_dump())
        return
    console.print(f"[bold cyan]#{a.id}[/bold cyan] [dim]{a.created_at[:16]}[/dim]  {a.title}\n")
    console.print(a.content)


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
    table.add_column("Задача")
    table.add_column("Записей", justify="right")
    table.add_column("PR")
    table.add_column("Title")
    for j in jobs:
        prs = ", ".join(str(p.pr_id) for p in j.prs) or "—"
        table.add_row(str(j.id), j.status, j.task_key, str(len(j.records)), prs, j.title)
    console.print(table)
    console.print(f"[dim]Всего: {len(jobs)}[/dim]")


@job_app.command("start")
def job_start(
    task_key: str = typer.Argument(..., help="Ключ задачи-якоря, напр. PROJ-399."),
    title: str = typer.Option("", "--title", "-t", help="Короткий заголовок работы."),
    json_out: bool = typer.Option(False, "--json", help="Вывести JSON."),
) -> None:
    """Начать новую работу по задаче (цикл работы). Дубликаты не блокируются."""
    with _store() as store:
        existing = store.jobs_for_task(task_key)
        job = store.create_job(task_key, title)
    if json_out:
        _emit_json(job.model_dump())
        return
    console.print(f"[green]Работа #{job.id}[/green] начата по {task_key}")
    if existing:
        console.print(f"[dim]По задаче уже есть работы: "
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
    console.print(f"[bold cyan]Работа #{job.id}[/bold cyan] [{job.status}]  {job.title or '—'}")
    console.print(f"[dim]задача:[/dim] {job.task_key}   [dim]обновлена:[/dim] {job.updated_at[:16]}")
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
                    f"[dim]{r.ts[:16]}[/dim] [bold {color}]{label}[/bold {color}]{st} "
                    f"[{color}]{r.text}[/{color}]")
            else:
                console.print(f"[dim]{r.ts[:16]} · {r.kind}{st}[/dim] {r.text}")


@app.command()
def jobs(
    task: Optional[str] = typer.Option(None, "--task", help="Фильтр по ключу задачи."),
    pr: Optional[int] = typer.Option(None, "--pr", help="Фильтр по id PR."),
    status: Optional[str] = typer.Option(None, "--status", help="active | done | paused."),
    json_out: bool = typer.Option(False, "--json", help="Вывести JSON."),
) -> None:
    """Список работ (по задаче / PR / статусу)."""
    with _store() as store:
        items = store.list_jobs(task_key=task, pr_id=pr, status=status)
    if json_out:
        _emit_json([j.model_dump() for j in items])
    else:
        _render_jobs_table(items)


if __name__ == "__main__":  # pragma: no cover
    app()
