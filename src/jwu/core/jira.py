"""Клиент Jira Server / Data Center (REST API v2 + dev-status).

Авторизация — Personal Access Token: ``Authorization: Bearer <PAT>``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import httpx

from .models import Issue

DEFAULT_FIELDS = "summary,status,assignee,reporter,priority,created,updated,resolution"
DETAIL_FIELDS = DEFAULT_FIELDS + ",description,comment,issuelinks,attachment"


class JiraError(RuntimeError):
    """Ошибка обращения к Jira (с кодом ответа, если есть)."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class JiraClient:
    def __init__(
        self,
        base_url: str,
        token: str = "",
        *,
        proxy_basic: Optional[tuple[str, str]] = None,
        session_login: Optional[tuple[str, str]] = None,
        client: Optional[httpx.Client] = None,
        timeout: float = 30.0,
    ) -> None:
        """Три режима авторизации:

        - ``session_login`` задан → за Jira стоит nginx Basic-гейт (``proxy_basic``):
          Basic уходит nginx в заголовке, а сама Jira авторизуется cookie-сессией.
        - иначе ``token`` → обычный PAT через ``Authorization: Bearer`` (гейта нет).
        - ``client`` можно подсунуть в тестах (тогда авторизацию не настраиваем).
        """
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._session_login = session_login
        if client is not None:
            self._client = client
        else:
            headers = {"Accept": "application/json"}
            auth = None
            if session_login is not None:
                if proxy_basic is not None:
                    auth = httpx.BasicAuth(*proxy_basic)
            else:
                headers["Authorization"] = f"Bearer {token}"
            self._client = httpx.Client(
                base_url=f"{self.base_url}/rest",
                headers=headers,
                auth=auth,
                timeout=timeout,
            )
            if session_login is not None:
                self._login(*session_login)

    def _login(self, username: str, password: str) -> None:
        """Создать сессию Jira (cookie JSESSIONID оседает в cookie jar клиента)."""
        try:
            resp = self._client.post(
                "/auth/1/session",
                json={"username": username, "password": password},
            )
        except httpx.HTTPError as exc:
            raise JiraError(f"Сеть/Jira недоступна при логине: {exc}") from exc
        if resp.status_code == 401:
            raise JiraError("401: не удалось залогиниться в Jira (логин/пароль или гейт)", 401)
        if resp.status_code >= 400:
            raise JiraError(f"Логин в Jira не удался: {resp.status_code}: {resp.text[:200]}", resp.status_code)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "JiraClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get(self, path: str, params: dict | None = None) -> dict:
        try:
            resp = self._client.get(path, params=params)
        except httpx.HTTPError as exc:  # сетевые ошибки
            raise JiraError(f"Сеть/Jira недоступна: {exc}") from exc
        if resp.status_code == 401:
            raise JiraError("401: токен Jira невалиден", 401)
        if resp.status_code == 403:
            raise JiraError("403: нет прав в Jira", 403)
        if resp.status_code >= 400:
            raise JiraError(f"{resp.status_code}: {resp.text[:200]}", resp.status_code)
        return resp.json()

    # --- API ------------------------------------------------------------- #

    def myself(self) -> dict:
        """Текущий пользователь — заодно проверка токена."""
        return self._get("/api/2/myself")

    def search(
        self,
        jql: str,
        *,
        fields: str = DEFAULT_FIELDS,
        max_results: int = 50,
    ) -> list[Issue]:
        """Поиск задач по JQL с пагинацией."""
        issues: list[Issue] = []
        start_at = 0
        while True:
            data = self._get(
                "/api/2/search",
                params={
                    "jql": jql,
                    "fields": fields,
                    "startAt": start_at,
                    "maxResults": max_results,
                },
            )
            batch = data.get("issues", []) or []
            issues.extend(Issue.from_jira(raw) for raw in batch)
            total = data.get("total", len(issues))
            start_at += len(batch)
            if not batch or start_at >= total:
                break
        return issues

    def issue(self, key: str, *, with_dev: bool = True) -> Issue:
        """Полная карточка задачи: поля, описание, комментарии, links + dev-панель."""
        raw = self._get(f"/api/2/issue/{key}", params={"fields": DETAIL_FIELDS})
        issue = Issue.from_jira(raw)
        if with_dev:
            issue_id = raw.get("id")
            if issue_id:
                detail, ok = self._dev_status(str(issue_id))
                issue.apply_dev_status(detail)
                issue.dev_ok = ok
            else:
                issue.dev_ok = False
        else:
            issue.dev_ok = False  # dev-панель не запрашивали — pr/branches недостоверны
        return issue

    def download_attachment(self, url: str, dest: Path) -> Path:
        """Скачать файл вложения по абсолютному content-URL в dest (стримингом).

        URL — абсолютный (на хосте Jira), а у клиента base_url указывает на /rest;
        httpx при абсолютном URL игнорирует base_url, но заголовки авторизации/куки
        сессии остаются на клиенте и применяются. Каталог dest.parent создаётся.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    resp.read()
                    raise JiraError(f"{resp.status_code}: не скачать вложение {url}",
                                    resp.status_code)
                with dest.open("wb") as fh:
                    for chunk in resp.iter_bytes():
                        fh.write(chunk)
        except httpx.HTTPError as exc:
            raise JiraError(f"Сеть/Jira недоступна при скачивании вложения: {exc}") from exc
        return dest

    def _dev_status(self, issue_id: str) -> tuple[dict, bool]:
        """Слить ветки (dataType=branch), коммиты (repository) и PR (pullrequest).

        Jira отдаёт ветки отдельным dataType=branch — у repository только коммиты.
        Ошибки dev-status не критичны (плагин может быть недоступен) — глотаем, но
        возвращаем ok=False, если хоть один запрос упал: иначе пустой из-за сбоя
        список PR неотличим от «PR реально нет» и порождает фантомные дельты.
        """
        merged: dict = {"branches": [], "repositories": [], "pullRequests": []}
        ok = True
        for data_type in ("branch", "repository", "pullrequest"):
            try:
                data = self._get(
                    "/dev-status/1.0/issue/detail",
                    params={
                        "issueId": issue_id,
                        "applicationType": "stash",
                        "dataType": data_type,
                    },
                )
            except JiraError:
                ok = False
                continue
            for entry in data.get("detail", []) or []:
                # dataType=branch кладёт ветки прямо в detail[] (repository вложен в ветку),
                # а dataType=repository — в repositories[].branches. Собираем оба варианта.
                merged["branches"].extend(entry.get("branches", []) or [])
                merged["repositories"].extend(entry.get("repositories", []) or [])
                merged["pullRequests"].extend(entry.get("pullRequests", []) or [])
        return merged, ok
