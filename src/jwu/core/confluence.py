"""Confluence Server: чтение, создание и правка страниц. Удаления нет — и не будет.

Инструкции и гайды живут в Confluence, и агенту, который их пишет, нужен доступ без
браузера. Сознательно только три глагола: прочитать, создать, поправить. Удаление
страницы в Confluence — это потеря истории для всех, кто на неё ссылается; такого
метода здесь нет ни в клиенте, ни в сервисе, ни в MCP, ни в CLI (это проверяет тест).

Авторизация — как у Jira того же контура: nginx Basic-гейт + сессия, которую Confluence
выдаёт на ``/dologin.action`` (логин и пароль Jira: учётка общая, LDAP). Протухшую
сессию клиент перелогинивает сам и повторяет запрос один раз.

Тело страницы Confluence хранит в формате storage (XHTML). Писать можно им же либо
вики-разметкой Jira/Confluence (``fmt="wiki"``) — тогда перед записью она конвертируется
сервером Confluence (``/rest/api/contentbody/convert/storage``).
"""

from __future__ import annotations

from typing import Optional

import httpx

from .http import new_client

BASE_URL_SETTING = "confluence.base_url"
SPACE_SETTING = "confluence.space"
FORMATS = ("storage", "wiki")


class ConfluenceError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ConfluenceClient:
    def __init__(self, base_url: str, *, proxy_basic: Optional[tuple[str, str]] = None,
                 session_login: Optional[tuple[str, str]] = None,
                 client: Optional[httpx.Client] = None, timeout: float | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._login_creds = session_login
        self._owns_client = client is None
        kw: dict = {"timeout": timeout} if timeout is not None else {}
        self._client = client or new_client(
            base_url=self.base_url,
            headers={"Accept": "application/json", "X-Atlassian-Token": "no-check"},
            auth=httpx.BasicAuth(*proxy_basic) if proxy_basic else None,
            **kw,
        )
        self._logged_in = False

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "ConfluenceClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- низкоуровневое ------------------------------------------------------ #

    def _login(self) -> None:
        if self._login_creds is None:
            return
        user, password = self._login_creds
        try:
            resp = self._client.post("/dologin.action", data={
                "os_username": user, "os_password": password, "login": "Log in"})
        except httpx.HTTPError as exc:
            raise ConfluenceError(f"Сеть/Confluence недоступен при логине: {exc}") from exc
        # неудачный вход Confluence отдаёт 200 со страницей логина либо редирект на неё
        location = resp.headers.get("location", "")
        if resp.status_code == 401:
            raise ConfluenceError("401: гейт Confluence не пустил (логин/пароль гейта)", 401)
        if resp.status_code >= 400 or "login.action" in location or (
                resp.status_code == 200 and "os_password" in resp.text):
            raise ConfluenceError("Не удалось войти в Confluence (логин/пароль Jira не подошли)", 401)
        self._logged_in = True

    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        if not self._logged_in:
            self._login()
        for attempt in (1, 2):
            try:
                resp = self._client.request(method, f"/rest/api{path}", **kw)
            except httpx.HTTPError as exc:
                raise ConfluenceError(f"Сеть/Confluence недоступен: {exc}") from exc
            if resp.status_code == 401 and attempt == 1 and self._login_creds is not None:
                self._login()  # сессия протухла — перелогин и повтор
                continue
            break
        if resp.status_code == 401:
            raise ConfluenceError("401: нет доступа к Confluence", 401)
        if resp.status_code == 403:
            raise ConfluenceError("403: нет прав на это в Confluence", 403)
        if resp.status_code == 404:
            raise ConfluenceError("404: страница не найдена (или нет прав её видеть)", 404)
        if resp.status_code >= 400:
            raise ConfluenceError(f"{resp.status_code}: {resp.text[:300]}", resp.status_code)
        return resp

    def _get(self, path: str, params: dict | None = None) -> dict:
        return self._request("GET", path, params=params).json()

    def _write(self, method: str, path: str, body: dict) -> dict:
        resp = self._request(method, path, json=body)
        return resp.json() if resp.content else {}

    # --- чтение -------------------------------------------------------------- #

    def page_url(self, raw: dict) -> str:
        webui = ((raw.get("_links") or {}).get("webui") or "")
        return f"{self.base_url}{webui}" if webui else f"{self.base_url}/pages/viewpage.action?pageId={raw.get('id')}"

    def _page(self, raw: dict, *, with_body: bool) -> dict:
        ancestors = raw.get("ancestors") or []
        out = {
            "id": str(raw.get("id", "")),
            "title": raw.get("title", ""),
            "space": ((raw.get("space") or {}).get("key", "")),
            "version": int(((raw.get("version") or {}).get("number")) or 0),
            "updated_by": (((raw.get("version") or {}).get("by") or {}).get("displayName", "")),
            "parent_id": str(ancestors[-1]["id"]) if ancestors else "",
            "path": [a.get("title", "") for a in ancestors],
            "url": self.page_url(raw),
        }
        if with_body:
            out["body"] = (((raw.get("body") or {}).get("storage") or {}).get("value", ""))
        return out

    def page(self, page_id: str | int, *, with_body: bool = True) -> dict:
        expand = "space,version,ancestors" + (",body.storage" if with_body else "")
        return self._page(self._get(f"/content/{page_id}", {"expand": expand}), with_body=with_body)

    def children(self, page_id: str | int, limit: int = 100) -> list[dict]:
        data = self._get(f"/content/{page_id}/child/page", {"limit": limit, "expand": "version"})
        return [{"id": str(x.get("id")), "title": x.get("title", ""),
                 "version": int(((x.get("version") or {}).get("number")) or 0),
                 "url": self.page_url(x)} for x in data.get("results", []) or []]

    def search(self, cql: str, limit: int = 25) -> list[dict]:
        data = self._get("/content/search", {"cql": cql, "limit": limit, "expand": "space"})
        return [{"id": str(x.get("id")), "title": x.get("title", ""),
                 "space": ((x.get("space") or {}).get("key", "")), "url": self.page_url(x)}
                for x in data.get("results", []) or []]

    def find_by_title(self, space: str, title: str) -> Optional[dict]:
        data = self._get("/content", {"spaceKey": space, "title": title, "type": "page"})
        results = data.get("results") or []
        return {"id": str(results[0]["id"]), "title": results[0].get("title", "")} if results else None

    # --- запись (без удаления) ------------------------------------------------ #

    def to_storage(self, body: str, fmt: str) -> str:
        if fmt not in FORMATS:
            raise ConfluenceError(f"Формат «{fmt}»: storage | wiki")
        if fmt == "storage":
            return body
        data = self._write("POST", "/contentbody/convert/storage", {"value": body, "representation": "wiki"})
        return data.get("value", "")

    def create_page(self, *, space: str, title: str, body: str, parent_id: str | int | None = None) -> dict:
        payload: dict = {
            "type": "page", "title": title, "space": {"key": space},
            "body": {"storage": {"value": body, "representation": "storage"}},
        }
        if parent_id:
            payload["ancestors"] = [{"id": str(parent_id)}]
        raw = self._write("POST", "/content", payload)
        return {"id": str(raw.get("id", "")), "title": raw.get("title", title),
                "version": int(((raw.get("version") or {}).get("number")) or 1), "url": self.page_url(raw)}

    def update_page(self, page_id: str | int, *, title: str, body: str, version: int,
                    message: str = "") -> dict:
        """Новая версия страницы. ``version`` — текущая: Confluence требует следующую."""
        payload = {
            "id": str(page_id), "type": "page", "title": title,
            "version": {"number": int(version) + 1, **({"message": message} if message else {})},
            "body": {"storage": {"value": body, "representation": "storage"}},
        }
        raw = self._write("PUT", f"/content/{page_id}", payload)
        return {"id": str(raw.get("id", page_id)), "title": raw.get("title", title),
                "version": int(((raw.get("version") or {}).get("number")) or version + 1),
                "url": self.page_url(raw)}
