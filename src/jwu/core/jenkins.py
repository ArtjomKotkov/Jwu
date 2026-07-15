"""Клиент Jenkins (JSON API) — глубокие детали сборок поверх статусов из Bitbucket.

Bitbucket build-status отдаёт состояние сборки и её URL; этот клиент идёт по URL в
Jenkins и вытаскивает причину падения: результат, упавшие тест-кейсы со стектрейсом,
хвост консольного лога. Авторизация — HTTP basic ``username:apiToken``.

Скобки в параметре ``tree`` (напр. ``suites[cases[...]]``) httpx процентно-кодирует,
а Jenkins их декодирует — поэтому, в отличие от curl, никаких спецфлагов не нужно.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlsplit

import httpx

# Кейсы JUnit, которые считаем падением (PASSED/SKIPPED/FIXED — нет).
FAILED_STATUSES = frozenset({"FAILED", "REGRESSION", "ERROR"})

# URL сборки Jenkins: .../job/<A>/job/<B>/<number>[/display/redirect|/console|...]
_BUILD_URL_RE = re.compile(r"/(job/.+?)/(\d+)(?:/|$)")


def parse_build_url(url: str) -> tuple[str, int] | None:
    """Из URL сборки Jenkins вытащить ``(job_path, number)``.

    ``job_path`` — путь джобы для API (напр. ``job/<folder>/job/<job-name>``).
    Возвращает None, если это не похоже на URL конкретной сборки Jenkins.
    """
    if not url:
        return None
    match = _BUILD_URL_RE.search(urlsplit(url).path)
    if not match:
        return None
    return match.group(1), int(match.group(2))


class JenkinsError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class JenkinsClient:
    def __init__(
        self,
        base_url: str,
        auth: tuple[str, str] | None = None,
        *,
        client: Optional[httpx.Client] = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=self.base_url,
            auth=auth,
            headers={"Accept": "application/json"},
            timeout=timeout,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "JenkinsClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- низкоуровневое ---------------------------------------------------- #

    def _request(self, path: str, params: dict | None = None) -> httpx.Response:
        try:
            resp = self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise JenkinsError(f"Сеть/Jenkins недоступен: {exc}") from exc
        if resp.status_code == 401:
            raise JenkinsError("401: токен Jenkins невалиден (нужен username:apiToken)", 401)
        if resp.status_code == 403:
            raise JenkinsError("403: нет прав в Jenkins", 403)
        return resp

    def _get_json(self, path: str, params: dict | None = None) -> dict:
        resp = self._request(path, params)
        if resp.status_code >= 400:
            raise JenkinsError(f"{resp.status_code}: {resp.text[:200]}", resp.status_code)
        return resp.json()

    # --- API --------------------------------------------------------------- #

    def ping(self) -> dict:
        """Проверка доступа/токена: корневой api/json на закрытом Jenkins требует авторизации."""
        return self._get_json("/api/json", params={"tree": "mode"})

    def build_info(self, job_path: str, number: int) -> dict:
        """Сводка о сборке: результат, идёт ли ещё, длительности, собранный commit/ветка."""
        raw = self._get_json(
            f"/{job_path}/{number}/api/json",
            params={
                "tree": "number,result,building,duration,estimatedDuration,timestamp,"
                "displayName,actions[lastBuiltRevision[branch[name,SHA1]]]"
            },
        )
        sha = branch = ""
        for action in raw.get("actions", []) or []:
            rev = action.get("lastBuiltRevision")
            if rev:
                branches = rev.get("branch") or []
                if branches:
                    sha = branches[0].get("SHA1", "") or ""
                    branch = branches[0].get("name", "") or ""
                break
        return {
            "number": raw.get("number"),
            "result": raw.get("result"),
            "building": bool(raw.get("building")),
            "duration_ms": int(raw.get("duration") or 0),
            "estimated_ms": int(raw.get("estimatedDuration") or 0),
            "display_name": raw.get("displayName") or "",
            "sha": sha,
            "branch": branch,
        }

    def test_summary(self, job_path: str, number: int) -> dict | None:
        """Сводка тест-репорта (fail/pass/skip). None, если у сборки нет тест-репорта (404)."""
        resp = self._request(f"/{job_path}/{number}/testReport/api/json",
                             params={"tree": "failCount,passCount,skipCount,duration"})
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise JenkinsError(f"{resp.status_code}: {resp.text[:200]}", resp.status_code)
        data = resp.json()
        return {
            "fail": int(data.get("failCount") or 0),
            "passed": int(data.get("passCount") or 0),
            "skip": int(data.get("skipCount") or 0),
        }

    def failed_cases(self, job_path: str, number: int, *, stack_limit: int = 4000) -> list[dict]:
        """Упавшие тест-кейсы со стектрейсом. Пустой список, если тест-репорта нет."""
        resp = self._request(
            f"/{job_path}/{number}/testReport/api/json",
            params={"tree": "suites[cases[className,name,status,errorDetails,errorStackTrace]]"},
        )
        if resp.status_code == 404:
            return []
        if resp.status_code >= 400:
            raise JenkinsError(f"{resp.status_code}: {resp.text[:200]}", resp.status_code)
        out: list[dict] = []
        for suite in resp.json().get("suites", []) or []:
            for case in suite.get("cases", []) or []:
                if case.get("status") not in FAILED_STATUSES:
                    continue
                stack = case.get("errorStackTrace") or ""
                out.append({
                    "class": case.get("className", "") or "",
                    "name": case.get("name", "") or "",
                    "status": case.get("status", "") or "",
                    "error_details": case.get("errorDetails") or "",
                    "stack": stack[:stack_limit],
                })
        return out

    def console_tail(self, job_path: str, number: int, *, chars: int = 6000) -> str:
        """Хвост консольного лога сборки (последние ``chars`` символов)."""
        resp = self._request(f"/{job_path}/{number}/consoleText")
        if resp.status_code >= 400:
            raise JenkinsError(f"{resp.status_code}: {resp.text[:200]}", resp.status_code)
        text = resp.text
        return text[-chars:] if len(text) > chars else text
