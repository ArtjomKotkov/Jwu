"""Пороги «давно» — настройка воркспейса, а не константа в промпте скилла.

Сколько дней PR без движения считать застрявшим, сколько ждать апрувов, сколько задача
может висеть на тестах, какой давности упоминания ещё показывать — у каждой команды
своё. Раньше это были числа в тексте jwu-analyze-day; теперь они лежат в настройках
контура (``thresholds.*``), а дневной анализ сам считает и ЯВНО называет застрявшее:
«PR … застрял N дн» вместо «обновлён давно».
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import TYPE_CHECKING, Iterable

from .dates import age_days
from .models import PR, Issue
from .store import is_testing_status

if TYPE_CHECKING:
    from .store import Store

SETTING_PREFIX = "thresholds."


@dataclass
class Thresholds:
    stale_pr_days: int = 14       # мой PR без движения — застрял
    approval_wait_days: int = 7   # мой PR ждёт апрувов дольше — пора пнуть
    review_wait_days: int = 3     # чужой PR ждёт МОЕГО ревью дольше — это на мне
    testing_days: int = 5         # задача на тестах дольше — пнуть тестирование
    mention_days: int = 14        # упоминания старше — не показывать в кратком режиме

    @classmethod
    def names(cls) -> list[str]:
        return [f.name for f in fields(cls)]

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def load(store: "Store", workspace_id: int) -> Thresholds:
    """Пороги контура: настройки ``thresholds.<имя>`` поверх дефолтов; мусор игнорируется."""
    settings = store.workspace_settings(workspace_id)
    values: dict[str, int] = {}
    for name in Thresholds.names():
        raw = settings.get(SETTING_PREFIX + name)
        if raw is None or raw == "":
            continue
        try:
            values[name] = max(0, int(raw))
        except ValueError:
            continue
    return Thresholds(**values)


def save(store: "Store", workspace_id: int, **values: int | None) -> Thresholds:
    """Записать заданные пороги (None — не трогать); вернуть итог."""
    updates = {SETTING_PREFIX + k: str(max(0, int(v))) for k, v in values.items()
               if v is not None and k in Thresholds.names()}
    if updates:
        store.set_workspace_settings(workspace_id, updates)
    return load(store, workspace_id)


def reset(store: "Store", workspace_id: int) -> Thresholds:
    """Вернуть дефолты (записать их явно — так видно, что настройка была)."""
    store.set_workspace_settings(workspace_id, {SETTING_PREFIX + k: str(v) for k, v in Thresholds().as_dict().items()})
    return load(store, workspace_id)


# --------------------------------------------------------------------------- #
# Что застряло
# --------------------------------------------------------------------------- #


def stuck_prs(prs_mine: Iterable[PR], th: Thresholds) -> list[dict]:
    """Мои PR, которые давно не двигались или давно ждут апрувов. Каждый — с причиной."""
    out: list[dict] = []
    for pr in prs_mine:
        days = age_days(pr.updated)
        if days is None:
            continue
        approved = bool(pr.reviewers) and all(r.approved for r in pr.reviewers)
        waiting = pr.reviewers and not approved and not any((r.status or "") == "NEEDS_WORK" for r in pr.reviewers)
        reasons: list[str] = []
        if days >= th.stale_pr_days:
            reasons.append(f"без движения {days} дн (порог {th.stale_pr_days})")
            if approved:
                reasons.append("апрувы собраны — мержить или закрыть")
        elif waiting and days >= th.approval_wait_days:
            reasons.append(f"ждёт апрувов {days} дн (порог {th.approval_wait_days}) — пнуть ревьюверов")
        if reasons:
            out.append({"kind": "pr", "key": f"{pr.project}/{pr.repository}#{pr.id}",
                        "title": pr.title, "days": days, "reason": "; ".join(reasons)})
    return out


def stuck_reviews(prs_review: Iterable[PR], th: Thresholds, login: str) -> list[dict]:
    """Чужие PR, которые ждут именно моего ревью дольше порога."""
    out: list[dict] = []
    me = (login or "").casefold()
    for pr in prs_review:
        mine = next((r for r in pr.reviewers if (r.name or "").casefold() == me), None)
        if mine is not None and (mine.status or "UNAPPROVED") == "APPROVED":
            continue
        days = age_days(pr.updated)
        if days is None or days < th.review_wait_days:
            continue
        out.append({"kind": "review", "key": f"{pr.project}/{pr.repository}#{pr.id}",
                    "title": pr.title, "days": days,
                    "reason": f"ждёт моего ревью {days} дн (порог {th.review_wait_days})"})
    return out


def stuck_tasks(mine: Iterable[Issue], th: Thresholds) -> list[dict]:
    """Мои задачи, которые висят на тестах дольше порога (по дате последнего обновления)."""
    out: list[dict] = []
    for issue in mine:
        if not is_testing_status(issue.status):
            continue
        days = age_days(issue.updated)
        if days is None or days < th.testing_days:
            continue
        out.append({"kind": "task", "key": issue.key, "title": issue.summary, "days": days,
                    "reason": f"на тестах без движения {days} дн (порог {th.testing_days}) — пнуть тестирование"})
    return out


def collect_stuck(*, prs_mine: Iterable[PR], prs_review: Iterable[PR], mine: Iterable[Issue],
                  th: Thresholds, login: str) -> list[dict]:
    items = stuck_prs(prs_mine, th) + stuck_reviews(prs_review, th, login) + stuck_tasks(mine, th)
    return sorted(items, key=lambda x: -x["days"])
