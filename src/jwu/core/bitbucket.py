"""Клиент Bitbucket Server / Data Center (REST API 1.0).

Авторизация — HTTP access token: ``Authorization: Bearer <PAT>``.
Вью задаём через dashboard-эндпоинт по роли: AUTHOR (мои PR) / REVIEWER (на ревью).
"""

from __future__ import annotations

from typing import Optional

import httpx

from .models import PR, PRComment, _get

# роль в dashboard/pull-requests
ROLE_BY_VIEW = {"mine": "AUTHOR", "review": "REVIEWER"}

_SEG_PREFIX = {"ADDED": "+", "REMOVED": "-", "CONTEXT": " "}


def _diff_lines(diff: dict, max_lines: int = 24) -> list[dict]:
    """Строки диффа с префиксом и номерами (source/destination) для поиска якоря."""
    out: list[dict] = []
    for hunk in diff.get("hunks", []) or []:
        for seg in hunk.get("segments", []) or []:
            prefix = _SEG_PREFIX.get(seg.get("type", "CONTEXT"), " ")
            for ln in seg.get("lines", []) or []:
                out.append({
                    "text": prefix + (ln.get("line", "") or ""),
                    "source": ln.get("source"),
                    "destination": ln.get("destination"),
                })
    return out[:max_lines]


def _anchor_index(lines: list[dict], anchor: dict) -> int:
    """Индекс в lines, соответствующий прокомментированной строке (по line + fileType)."""
    target = anchor.get("line")
    if target is None:
        return -1
    field = "source" if anchor.get("fileType") == "FROM" else "destination"
    for i, ln in enumerate(lines):
        if ln.get(field) == target:
            return i
    return -1


def _diff_context(diff: dict, max_lines: int = 24) -> list[str]:
    return [ln["text"] for ln in _diff_lines(diff, max_lines)]


def _flatten_comment(
    c: dict, comments: list[PRComment], *, file: str, line, context, anchor_idx: int, depth: int
) -> None:
    """Добавить коммент и рекурсивно его ответы (replies лежат в comment.comments)."""
    author = _get_dn(c)
    comments.append(
        PRComment(
            id=str(c.get("id", "")),
            author=author,
            text=c.get("text", "") or "",
            created=int(c.get("createdDate", 0) or 0),
            file=file,
            line=line,
            depth=depth,
            context=context if depth == 0 else [],
            anchor_idx=anchor_idx if depth == 0 else -1,
        )
    )
    for reply in c.get("comments", []) or []:
        _flatten_comment(reply, comments, file=file, line=line, context=[],
                         anchor_idx=-1, depth=depth + 1)


def _get_dn(c: dict) -> str:
    author = c.get("author") or {}
    return author.get("displayName", "") or author.get("name", "")


class BitbucketError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class BitbucketClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        client: Optional[httpx.Client] = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=f"{self.base_url}/rest/api/1.0",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=timeout,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "BitbucketClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get(self, path: str, params: dict | None = None) -> dict:
        try:
            resp = self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise BitbucketError(f"Сеть/Bitbucket недоступен: {exc}") from exc
        if resp.status_code == 401:
            raise BitbucketError("401: токен Bitbucket невалиден", 401)
        if resp.status_code == 403:
            raise BitbucketError("403: нет прав в Bitbucket", 403)
        if resp.status_code >= 400:
            raise BitbucketError(f"{resp.status_code}: {resp.text[:200]}", resp.status_code)
        return resp.json()

    def _paged(self, path: str, params: dict | None = None) -> list[dict]:
        """Собрать все страницы Bitbucket (values / isLastPage / nextPageStart)."""
        params = dict(params or {})
        start = 0
        out: list[dict] = []
        while True:
            params["start"] = start
            params.setdefault("limit", 50)
            data = self._get(path, params=params)
            out.extend(data.get("values", []) or [])
            if data.get("isLastPage", True):
                break
            start = data.get("nextPageStart")
            if start is None:
                break
        return out

    # --- API ------------------------------------------------------------- #

    def ping(self) -> dict:
        """Проверка токена: user-scoped эндпоинт, требует авторизации."""
        return self._get("/dashboard/pull-requests", params={"limit": 1})

    def dashboard_prs(self, view: str, *, state: str = "OPEN") -> list[PR]:
        """Мои PR (AUTHOR) или на моё ревью (REVIEWER) по всем репозиториям."""
        role = ROLE_BY_VIEW.get(view)
        if role is None:
            raise BitbucketError(f"Неизвестный вью PR: {view!r} (mine|review)")
        raw = self._paged(
            "/dashboard/pull-requests", params={"role": role, "state": state}
        )
        return [PR.from_bitbucket(r) for r in raw]

    def pr(self, project: str, repo: str, pr_id: int, *, with_merge: bool = True) -> PR:
        """Детали PR + (опционально) статус merge-конфликта."""
        raw = self._get(
            f"/projects/{project}/repos/{repo}/pull-requests/{pr_id}"
        )
        pull = PR.from_bitbucket(raw)
        if with_merge:
            try:
                pull.apply_merge_status(self.merge_status(project, repo, pr_id))
            except BitbucketError:
                pass
        return pull

    def merge_status(self, project: str, repo: str, pr_id: int) -> dict:
        return self._get(
            f"/projects/{project}/repos/{repo}/pull-requests/{pr_id}/merge"
        )

    def latest_commit(self, project: str, repo: str, pr_id: int) -> str:
        """ID последнего коммита PR (дёшево, для детекта новых коммитов)."""
        data = self._get(
            f"/projects/{project}/repos/{repo}/pull-requests/{pr_id}/commits",
            params={"limit": 1},
        )
        values = data.get("values", []) or []
        return values[0].get("id", "") if values else ""

    def pr_commits(self, project: str, repo: str, pr_id: int, *, limit: int = 25) -> list[dict]:
        """Список коммитов PR (id, displayId, message, author) — для экрана PR."""
        data = self._get(
            f"/projects/{project}/repos/{repo}/pull-requests/{pr_id}/commits",
            params={"limit": limit},
        )
        out = []
        for c in data.get("values", []) or []:
            out.append({
                "id": c.get("displayId", c.get("id", "")),
                "message": (c.get("message", "") or "").strip(),
                "author": _get(c, "author", "name") or _get(c, "author", "displayName") or "",
            })
        return out

    def my_review_at(self, project: str, repo: str, pr_id: int, login: str) -> int | None:
        """Дата (epoch ms) последнего ревью-действия пользователя в PR.

        Берётся из activities: action ``APPROVED`` / ``REVIEWED`` (= needs work) /
        ``UNAPPROVED``. В массиве reviewers даты нет — поэтому тянем ленту активностей.
        Возвращает None, если пользователь не оставлял ревью-действий.
        """
        acts = self._paged(
            f"/projects/{project}/repos/{repo}/pull-requests/{pr_id}/activities"
        )
        best: int | None = None
        for a in acts:
            if (a.get("user") or {}).get("name") != login:
                continue
            if a.get("action") not in ("APPROVED", "REVIEWED", "UNAPPROVED"):
                continue
            ts = int(a.get("createdDate") or 0)
            if best is None or ts > best:
                best = ts
        return best

    def pr_comments(self, project: str, repo: str, pr_id: int) -> list[PRComment]:
        """Комментарии PR из activities: общие + inline (с file:line и куском диффа)."""
        acts = self._paged(
            f"/projects/{project}/repos/{repo}/pull-requests/{pr_id}/activities"
        )
        # каждый тред (коммент + ответы) собираем в группу.
        # activities приходят новыми сверху — сохраняем этот порядок групп
        # (свежие треды первыми), не трогая порядок внутри треда.
        groups: list[list[PRComment]] = []
        for a in acts:
            if a.get("action") != "COMMENTED":
                continue
            c = a.get("comment") or {}
            anchor = a.get("commentAnchor") or {}
            diff = a.get("diff") or {}
            lines = _diff_lines(diff) if diff else []
            group: list[PRComment] = []
            _flatten_comment(
                c,
                group,
                file=anchor.get("path", "") or "",
                line=anchor.get("line"),
                context=[ln["text"] for ln in lines],
                anchor_idx=_anchor_index(lines, anchor) if anchor else -1,
                depth=0,
            )
            groups.append(group)
        return [comment for group in groups for comment in group]
