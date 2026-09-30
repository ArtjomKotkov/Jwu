"""Скрины дашборда для README — на выдуманном контуре ACME, без живых данных.

    python scripts/screenshots.py          # → docs/img/*.svg

Все задачи, PR, люди и хосты здесь придуманы: в картинки не должно попасть ничего
из реальной работы. Скрипт не трогает БД и конфиг jwu — дашборд получает готовый снимок.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jwu.cli.dashboard import JwuDashboard  # noqa: E402
from jwu.core.models import (  # noqa: E402
    PR, BuildStatus, Delta, Issue, Job, JobRecord, Reviewer, Workspace, WorkspacePath,
)
from jwu.core.service import DashboardData  # noqa: E402

OUT = ROOT / "docs" / "img"
NOW = datetime.now(timezone.utc)


def _iso(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


def _ms(hours_ago: float) -> int:
    return int((NOW - timedelta(hours=hours_ago)).timestamp() * 1000)


def _issue(key, summary, status, hours, prio="Major"):
    return Issue(key=key, summary=summary, status=status, assignee="Alex Doe", reporter="Sam Lee",
                 priority=prio, created=_iso(hours + 72), updated=_iso(hours))


def _pr(pid, title, *, repo="shop-api", target="develop", reviewers=(), build="SUCCESSFUL",
        conflicted=False, hours=3, comments=0, tasks=0):
    return PR(id=pid, title=title, state="OPEN", author="Alex Doe", project="ACME", repository=repo,
              source_branch=f"feature/{title.split(':')[0]}", target_branch=target,
              url=f"https://git.example.com/acme/{repo}/pull/{pid}", created=_ms(hours + 30),
              updated=_ms(hours), comment_count=comments, conflicted=conflicted, can_merge=not conflicted,
              tasks_open=tasks,
              reviewers=[Reviewer(name=n.lower().replace(" ", "."), display_name=n, approved=s == "APPROVED",
                                  status=s) for n, s in reviewers],
              builds=[BuildStatus(state=build, key="ci", name="tests", url="https://ci.example.com/1")] if build else [])


def demo_data() -> DashboardData:
    ws = Workspace(id=1, slug="acme", name="ACME", provider="jira", bitbucket_enabled=True,
                   paths=[WorkspacePath(path="~/code/shop-api", tags=["backend"]),
                          WorkspacePath(path="~/code/shop-web", tags=["frontend"])])
    mine = [
        _issue("ACME-412", "Checkout: повторная оплата при таймауте платёжного шлюза", "In Progress", 1, "Critical"),
        _issue("ACME-398", "Экспорт заказов в CSV теряет часовой пояс", "Code Review", 5),
        _issue("ACME-377", "Поиск по каталогу: учитывать синонимы брендов", "Testing", 20),
        _issue("ACME-361", "Кэш цен не сбрасывается после импорта прайса", "Reopened", 30, "Critical"),
        _issue("ACME-350", "Уведомления о доставке на двух языках", "To Do", 50, "Minor"),
    ]
    prs_mine = [
        _pr(1204, "ACME-398: часовой пояс в экспорте заказов",
            reviewers=[("Maria Lopez", "APPROVED"), ("Ken Ito", "APPROVED")], hours=2),
        _pr(1198, "ACME-412: идемпотентный ключ для повторной оплаты",
            reviewers=[("Maria Lopez", "NEEDS_WORK"), ("Ken Ito", "UNAPPROVED")], build="FAILED",
            hours=4, comments=6, tasks=2),
        _pr(1187, "ACME-377: синонимы брендов в поиске", repo="shop-web",
            reviewers=[("Ken Ito", "APPROVED"), ("Priya Shah", "UNAPPROVED")], build="INPROGRESS",
            hours=9, comments=2),
        _pr(1175, "ACME-361: сброс кэша цен после импорта", target="release/2.4",
            reviewers=[("Priya Shah", "UNAPPROVED")], conflicted=True, hours=26),
    ]
    prs_review = [
        _pr(1210, "ACME-420: пагинация в истории заказов", reviewers=[("Alex Doe", "UNAPPROVED")], hours=1),
        _pr(1206, "ACME-415: ретраи вебхуков склада", repo="shop-api",
            reviewers=[("Alex Doe", "UNAPPROVED"), ("Ken Ito", "APPROVED")], hours=6, comments=3),
    ]
    jobs = [
        Job(id=31, task_key="ACME-412", title="Повторная оплата при таймауте", status="active",
            created_at=_iso(28), updated_at=_iso(1),
            records=[JobRecord(kind="phase", text="Разбор: шлюз отвечает таймаутом, заказ уже оплачен",
                               status="done", ts=_iso(26)),
                     JobRecord(kind="decision", text="Идемпотентный ключ на стороне заказа", ts=_iso(20)),
                     JobRecord(kind="test-fail", text="Упал интеграционный тест ретраев", ts=_iso(3))]),
        Job(id=30, task_key="ACME-398", title="Часовой пояс в CSV", status="done",
            created_at=_iso(60), updated_at=_iso(5),
            records=[JobRecord(kind="phase", text="Фикс и тесты", status="done", ts=_iso(6))]),
    ]
    deltas = [
        Delta(key="ACME/shop-api#1204", kind="pr_ready_to_merge", summary=prs_mine[0].title,
              detail="апрувов 2/2 · сборка ок · конфликтов нет"),
        Delta(key="ACME/shop-api#1198", kind="reviewer_needs_work", summary=prs_mine[1].title,
              detail="Maria Lopez: needs work"),
        Delta(key="ACME-377", kind="returned_from_testing", summary=mine[2].summary,
              detail="Testing → Reopened"),
    ]
    return DashboardData(
        user="alex.doe", display_name="Alex Doe", email="alex@example.com",
        last_sync={s: _iso(0.2) for s in ("mine", "mentions", "prs_mine", "prs_review")},
        deltas=deltas, mine=mine, prs_mine=prs_mine, prs_review=prs_review, jobs=jobs,
        workspace=ws, workspaces=[ws], paths=ws.paths, provider="jira", bitbucket_enabled=True,
        env_label="ACME @ jira.example.com", web_base="https://jira.example.com",
        task_status={i.key: i.status for i in mine},
        task_assignee={i.key: i.assignee for i in mine},
        status_notes={"ACME/shop-api#1175": "ждём релизную ветку 2.4"},
    )


async def shoot() -> list[Path]:
    OUT.mkdir(parents=True, exist_ok=True)
    # (вкладка, файл, показать панель «Изменения»)
    shots = [("tab-prs-mine", "dashboard-prs.svg", True), ("tab-mine", "dashboard-tasks.svg", False),
             ("tab-jobs", "dashboard-jobs.svg", False)]
    written: list[Path] = []
    for tab, name, changes in shots:
        app = JwuDashboard(demo_data(), jira_base="https://jira.example.com")
        async with app.run_test(size=(176, 24)) as pilot:
            app._tabs.active = tab
            if changes:
                app._show_changes(True)
            await pilot.pause(0.3)
            app.save_screenshot(filename=name, path=str(OUT))
        written.append(OUT / name)
    return written


if __name__ == "__main__":
    for p in asyncio.run(shoot()):
        print(p.relative_to(ROOT))
