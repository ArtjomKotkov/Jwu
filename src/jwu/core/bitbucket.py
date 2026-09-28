"""Клиент Bitbucket Server / Data Center (REST API 1.0).

Авторизация — HTTP access token: ``Authorization: Bearer <PAT>``.
Вью задаём через dashboard-эндпоинт по роли: AUTHOR (мои PR) / REVIEWER (на ревью).
"""

from __future__ import annotations

from typing import Optional

import httpx

from .http import new_client
from pathlib import Path

from .models import PR, BuildStatus, PRAttachment, PRComment, PRTask, _get, pr_attachment_refs

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
            tasks=[PRTask.from_bitbucket(t) for t in c.get("tasks", []) or []],
            attachments=pr_attachment_refs(c.get("text", "") or "", comment_id=str(c.get("id", ""))),
        )
    )
    for reply in c.get("comments", []) or []:
        _flatten_comment(reply, comments, file=file, line=line, context=[],
                         anchor_idx=-1, depth=depth + 1)


def render_unified_diff(data: dict) -> str:
    """JSON-дифф Bitbucket → unified diff (то, что читают люди и `git apply --check`)."""
    out: list[str] = []
    for diff in data.get("diffs", []) or []:
        src = (diff.get("source") or {}).get("toString") or ""
        dst = (diff.get("destination") or {}).get("toString") or ""
        out.append(f"diff --git a/{src or dst} b/{dst or src}")
        if not src:
            out.append("new file")
        elif not dst:
            out.append("deleted file")
        if diff.get("binary"):
            out.append("Binary files differ")
            continue
        out.append(f"--- {'a/' + src if src else '/dev/null'}")
        out.append(f"+++ {'b/' + dst if dst else '/dev/null'}")
        for hunk in diff.get("hunks", []) or []:
            out.append(
                f"@@ -{hunk.get('sourceLine', 0)},{hunk.get('sourceSpan', 0)} "
                f"+{hunk.get('destinationLine', 0)},{hunk.get('destinationSpan', 0)} @@"
            )
            for seg in hunk.get("segments", []) or []:
                prefix = {"ADDED": "+", "REMOVED": "-"}.get(seg.get("type"), " ")
                for ln in seg.get("lines", []) or []:
                    out.append(prefix + (ln.get("line", "") or ""))
                if seg.get("truncated"):
                    out.append("\\ ... (сегмент обрезан Bitbucket)")
            if hunk.get("truncated"):
                out.append("\\ ... (хунк обрезан Bitbucket)")
        if diff.get("truncated"):
            out.append("\\ ... (файл обрезан Bitbucket)")
    if data.get("truncated"):
        out.append("\\ ... (дифф обрезан Bitbucket: слишком большой)")
    return "\n".join(out) + ("\n" if out else "")


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
        timeout: float | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or new_client(
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

    def _send(self, method: str, path: str, payload: dict | None = None) -> dict:
        """Запись (POST/PUT/DELETE) с той же обработкой ошибок, что у ``_get``."""
        try:
            resp = self._client.request(method, path, json=payload)
        except httpx.HTTPError as exc:
            raise BitbucketError(f"Сеть/Bitbucket недоступен: {exc}") from exc
        if resp.status_code == 401:
            raise BitbucketError("401: токен Bitbucket невалиден", 401)
        if resp.status_code == 403:
            raise BitbucketError("403: нет прав в Bitbucket", 403)
        if resp.status_code >= 400:
            raise BitbucketError(f"{resp.status_code}: {resp.text[:200]}", resp.status_code)
        return resp.json() if resp.content else {}

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
        prs = [PR.from_bitbucket(r) for r in raw]
        for pr in prs:
            self._fill_attachment_urls(pr.project, pr.repository, pr.attachments)
        return prs

    def attachment_url(self, project: str, repo: str, path: str) -> str:
        """Прямой URL вложения PR: веб-роут ``/projects/K/repos/S/attachments/<hash>/<имя>``.

        REST-роут на этом инстансе (6.1) отдаёт 400 на закодированный слэш и 404 без
        него; веб-роут с обычным слэшем отвечает файлом — но только без follow-redirect.
        """
        return f"{self.base_url}/projects/{project}/repos/{repo}/attachments/{path}"

    def _fill_attachment_urls(self, project: str, repo: str, items: list[PRAttachment]) -> None:
        for a in items:
            if not a.url:
                a.url = self.attachment_url(project, repo, a.path)

    def download_attachment(self, url: str, dest: Path) -> Path:
        """Скачать вложение PR в dest (стримингом; файл пишется только при успехе)."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._client.stream("GET", url, headers={"Accept": "*/*"},
                                     follow_redirects=False) as resp:
                if resp.status_code >= 400:
                    resp.read()
                    raise BitbucketError(f"{resp.status_code}: не скачать {url}", resp.status_code)
                with dest.open("wb") as fh:
                    for chunk in resp.iter_bytes():
                        fh.write(chunk)
        except httpx.HTTPError as exc:
            raise BitbucketError(f"Сеть/Bitbucket недоступен: {exc}") from exc
        return dest

    def pr(self, project: str, repo: str, pr_id: int, *, with_merge: bool = True) -> PR:
        """Детали PR + (опционально) статус merge-конфликта."""
        raw = self._get(
            f"/projects/{project}/repos/{repo}/pull-requests/{pr_id}"
        )
        pull = PR.from_bitbucket(raw)
        self._fill_attachment_urls(project, repo, pull.attachments)
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

    def build_statuses(
        self, commit_sha: str, *, project: str = "", repo: str = ""
    ) -> list[BuildStatus]:
        """Статусы CI-сборок по коммиту (build-status API — то, что рисуется на странице PR).

        Эндпоинт живёт под ``/rest/build-status/1.0`` (вне базового ``/rest/api/1.0``),
        поэтому дёргаем абсолютным URL. Bitbucket иногда задваивает запись по одной сборке
        (чистый ключ + ключ с экранированными слэшами) — дедуплицируем по URL, предпочитая
        запись без ``\\`` в ключе.

        ``project``/``repo`` здесь не нужны (поиск идёт по всему инстансу) и приняты
        только ради общего контракта с GitHub, где сборки ищутся внутри репозитория.
        """
        url = f"{self.base_url}/rest/build-status/1.0/commits/{commit_sha}"
        try:
            resp = self._client.get(url)
        except httpx.HTTPError as exc:
            raise BitbucketError(f"Сеть/Bitbucket недоступен: {exc}") from exc
        if resp.status_code == 401:
            raise BitbucketError("401: токен Bitbucket невалиден", 401)
        if resp.status_code == 403:
            raise BitbucketError("403: нет прав в Bitbucket", 403)
        if resp.status_code >= 400:
            raise BitbucketError(f"{resp.status_code}: {resp.text[:200]}", resp.status_code)
        by_url: dict[str, BuildStatus] = {}
        for raw in resp.json().get("values", []) or []:
            bs = BuildStatus.from_bitbucket(raw)
            existing = by_url.get(bs.url)
            if existing is None or ("\\" in existing.key and "\\" not in bs.key):
                by_url[bs.url] = bs
        return list(by_url.values())

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

    # --- дифф и ревью --------------------------------------------------------- #

    def pr_diff(self, project: str, repo: str, pr_id: int, *, path: str | None = None,
                context: int = 3) -> str:
        """Дифф PR в unified-виде, собранный из JSON-ответа ``/diff``.

        Текстовый вариант (``.diff``, ``Accept: text/plain``) на 6.1 отдаёт 406, поэтому
        берём JSON с хунками и рендерим сами: ``--- a/…``, ``+++ b/…``, ``@@ … @@`` и
        строки с префиксами. ``path`` — только один файл.
        """
        url = f"/projects/{project}/repos/{repo}/pull-requests/{pr_id}/diff"
        if path:
            url += "/" + path.lstrip("/")
        data = self._get(url, params={"contextLines": context})
        return render_unified_diff(data)

    def pr_review(self, project: str, repo: str, pr_id: int, user_slug: str, status: str) -> dict:
        """Поставить свой статус ревью: APPROVED | NEEDS_WORK | UNAPPROVED (снять)."""
        status = status.upper()
        if status not in ("APPROVED", "NEEDS_WORK", "UNAPPROVED"):
            raise BitbucketError(f"Неизвестный статус ревью {status!r}")
        return self._send(
            "PUT",
            f"/projects/{project}/repos/{repo}/pull-requests/{pr_id}/participants/{user_slug}",
            {"user": {"name": user_slug}, "approved": status == "APPROVED", "status": status},
        )

    def pr_create(self, project: str, repo: str, *, source: str, target: str, title: str,
                  description: str = "", reviewers: list[str] | None = None) -> PR:
        """Создать PR из ветки ``source`` в ``target`` того же репозитория."""
        ref = {"repository": {"slug": repo, "project": {"key": project}}}
        payload = {
            "title": title, "description": description or "",
            "fromRef": {"id": f"refs/heads/{source}", **ref},
            "toRef": {"id": f"refs/heads/{target}", **ref},
            "reviewers": [{"user": {"name": r}} for r in (reviewers or []) if r],
        }
        raw = self._send("POST", f"/projects/{project}/repos/{repo}/pull-requests", payload)
        pull = PR.from_bitbucket(raw)
        self._fill_attachment_urls(project, repo, pull.attachments)
        return pull

    # --- задачи на комментах (Bitbucket Server tasks API) ------------------ #

    def pr_tasks(self, project: str, repo: str, pr_id: int) -> list[PRTask]:
        """Все задачи PR (открытые и закрытые), с привязкой к комменту-якорю."""
        raw = self._paged(f"/projects/{project}/repos/{repo}/pull-requests/{pr_id}/tasks")
        return [PRTask.from_bitbucket(t) for t in raw]

    def pr_task_count(self, project: str, repo: str, pr_id: int) -> tuple[int, int]:
        """(открытых, закрытых) — один дешёвый запрос, для синка."""
        data = self._get(f"/projects/{project}/repos/{repo}/pull-requests/{pr_id}/tasks/count")
        return int(data.get("open", 0) or 0), int(data.get("resolved", 0) or 0)

    def task_create(self, comment_id: int | str, text: str) -> PRTask:
        """Повесить задачу на коммент. Текст — как есть: проверка длины лежит выше."""
        raw = self._send("POST", "/tasks", {
            "anchor": {"id": int(comment_id), "type": "COMMENT"},
            "text": text,
        })
        return PRTask.from_bitbucket(raw)

    def task_set_state(self, task_id: int, state: str) -> PRTask:
        """Закрыть (RESOLVED) или снова открыть (OPEN) задачу."""
        raw = self._send("PUT", f"/tasks/{int(task_id)}", {"id": int(task_id), "state": state})
        return PRTask.from_bitbucket(raw)

    def pr_comment_add(
        self, project: str, repo: str, pr_id: int, text: str, *,
        parent_id: int | str | None = None, path: str | None = None,
        line: int | None = None, line_type: str = "CONTEXT", file_type: str = "TO",
    ) -> dict:
        """Оставить коммент: общий, ответ в тред (``parent_id``) или на строку файла.

        У inline-коммента ``line_type`` — ADDED | REMOVED | CONTEXT, ``file_type`` — TO
        (правая сторона диффа, новая версия) | FROM. Возвращает сырой коммент Bitbucket
        (нужен его ``id`` — на него вешаются задачи).
        """
        payload: dict = {"text": text}
        if parent_id is not None:
            payload["parent"] = {"id": int(parent_id)}
        elif path:
            payload["anchor"] = {"path": path, "lineType": line_type, "fileType": file_type}
            if line is not None:
                payload["anchor"]["line"] = int(line)
        return self._send(
            "POST", f"/projects/{project}/repos/{repo}/pull-requests/{pr_id}/comments", payload
        )

    def pr_comment_get(self, project: str, repo: str, pr_id: int, comment_id: int | str) -> dict:
        """Один коммент (сырой): нужен ``version`` для удаления и ``author`` для проверки «мой»."""
        return self._get(
            f"/projects/{project}/repos/{repo}/pull-requests/{pr_id}/comments/{int(comment_id)}"
        )

    def pr_comment_delete(self, project: str, repo: str, pr_id: int, comment_id: int | str,
                          version: int) -> None:
        """Удалить коммент. Bitbucket требует его текущую ``version`` (защита от гонки правок)."""
        self._send(
            "DELETE",
            f"/projects/{project}/repos/{repo}/pull-requests/{pr_id}/comments/{int(comment_id)}"
            f"?version={int(version)}",
        )

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
        comments = [comment for group in groups for comment in group]
        for c in comments:
            self._fill_attachment_urls(project, repo, c.attachments)
        return comments
