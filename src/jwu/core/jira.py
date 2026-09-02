"""Клиент Jira Server / Data Center (REST API v2 + dev-status).

Авторизация — Personal Access Token: ``Authorization: Bearer <PAT>``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import httpx

from .models import Issue

DEFAULT_FIELDS = "summary,status,assignee,reporter,priority,created,updated,resolution"
DETAIL_FIELDS = DEFAULT_FIELDS + ",description,comment,issuelinks,attachment"

# Системные поля, которые заполняет сам jwu: их отсутствие в запросе — не ошибка
# пользователя, даже если createmeta пометил их обязательными (project/issuetype
# всегда есть в payload, summary проверяем отдельно, reporter Jira ставит сама).
_META_IGNORED_FIELDS = {"project", "issuetype", "reporter"}


class JiraError(RuntimeError):
    """Ошибка обращения к Jira (с кодом ответа, если есть)."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _describe_error_body(resp: httpx.Response) -> str:
    """Человекочитаемая расшифровка тела ошибки Jira.

    При 400 Jira возвращает ``{"errorMessages": [...], "errors": {"поле": "текст"}}`` —
    именно там лежит причина отказа (какого поля не хватает, чем не устроило значение).
    Без неё диагностировать нечего, поэтому показываем дословно, а не обрезком текста.
    """
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001 — не JSON (HTML гейта, plain text) — отдаём как есть
        return resp.text[:200]
    if not isinstance(data, dict):
        return resp.text[:200]
    parts = [str(m) for m in (data.get("errorMessages") or []) if m]
    errors = data.get("errors")
    if isinstance(errors, dict):
        parts.extend(f"{field}: {text}" for field, text in errors.items())
    return "; ".join(parts) if parts else resp.text[:200]


def build_create_fields(
    project: str,
    summary: str,
    *,
    description: str | None = None,
    issuetype: str = "Task",
    priority: str | None = None,
    assignee: str | None = None,
    labels: list[str] | None = None,
    components: list[str] | None = None,
    fix_versions: list[str] | None = None,
    parent: str | None = None,
) -> dict:
    """Собрать блок ``fields`` для ``POST /issue``.

    Функция чистая: тот же payload показывается в ``--dry-run`` и уходит в Jira, так что
    пользователь видит ровно то, что будет отправлено. Jira здесь серверная (REST v2),
    поэтому ``description`` — обычный wiki-текст, а не ADF; пользователи задаются логином
    (``name``), а не accountId, как в Cloud.
    """
    fields: dict = {
        "project": {"key": project},
        "summary": summary,
        "issuetype": {"name": issuetype},
    }
    if description:
        fields["description"] = description
    if priority:
        fields["priority"] = {"name": priority}
    if assignee:
        fields["assignee"] = {"name": assignee}
    if labels:
        fields["labels"] = list(labels)
    if components:
        fields["components"] = [{"name": c} for c in components]
    if fix_versions:
        fields["fixVersions"] = [{"name": v} for v in fix_versions]
    if parent:
        fields["parent"] = {"key": parent}
    return fields


def _allowed_index(field_meta: dict) -> dict[str, dict]:
    """Допустимые значения поля из createmeta: имя в нижнем регистре → сам элемент.

    Имя значения лежит то в ``name``, то в ``value`` (у разных типов полей по-разному),
    а идентификатор — всегда в ``id``. Индекс нужен и для проверки, и для резолва
    имени в id.
    """
    index: dict[str, dict] = {}
    for item in field_meta.get("allowedValues") or []:
        if isinstance(item, dict):
            name = item.get("name") or item.get("value")
            if name:
                index.setdefault(str(name).lower(), item)
    return index


def _allowed_names(field_meta: dict) -> list[str]:
    """Допустимые значения поля из createmeta, как их показывать пользователю."""
    out = []
    for item in field_meta.get("allowedValues") or []:
        if isinstance(item, dict):
            name = item.get("name") or item.get("value")
            if name:
                out.append(str(name))
    return out


def _by_id(item: dict, fallback_name: str) -> dict:
    """Ссылка на значение так, как её надёжнее всего понимает Jira Server: по ``id``.

    Имя инстанс может не разрезолвить: когда в нём несколько схем типов задач (или
    приоритетов) с одинаковыми именами, ``{"name": "Task"}`` неоднозначно, и Jira
    отвечает «The issue type selected is invalid», хотя тип в проекте есть. Идентификатор
    из ``createmeta`` однозначен всегда. Если id почему-то не пришёл — остаёмся на имени.
    """
    value_id = item.get("id")
    return {"id": str(value_id)} if value_id else {"name": fallback_name}


def check_create_fields(meta: dict, fields: dict) -> dict:
    """Сверить будущий payload с ``createmeta`` проекта ДО отправки и привести его к id.

    У разных проектов свои обязательные поля и свои наборы типов задач, поэтому вслепую
    задача просто не создастся, а Jira ответит сырой 400-й. Здесь же видно заранее, чего
    не хватает и какие значения допустимы.

    Заодно значения, заданные ИМЕНАМИ (тип задачи, приоритет, компоненты, версии),
    заменяются на идентификаторы из ``createmeta``: пользователь по-прежнему пишет
    ``-t Task``, а в Jira уходит ``{"id": "10002"}``. Иначе на инстансе с несколькими
    схемами типов имя не резолвится и создание падает уже после «всё в порядке» в dry-run.

    Возвращает ``{"ok", "problems", "fields", "issue_types", "required", "project"}``:
    ``problems`` — готовые к показу строки, пустой список значит «можно отправлять»,
    ``fields`` — payload, который и надо отправлять (при проблемах — исходный).
    """
    issue_types = [str(t.get("name", "")) for t in (meta.get("issuetypes") or []) if t.get("name")]
    wanted_type = (fields.get("issuetype") or {}).get("name", "")
    result: dict = {
        "ok": False,
        "problems": [],
        "fields": dict(fields),
        "project": meta.get("key", "") or "",
        "issue_types": issue_types,
        "required": [],
    }
    problems: list[str] = result["problems"]
    resolved: dict = result["fields"]

    if not meta:
        project_key = (fields.get("project") or {}).get("key", "?")
        problems.append(
            f"Проект {project_key} недоступен для создания задач: его нет, "
            f"либо у пользователя нет права Create Issue в нём."
        )
        return result

    type_meta = next(
        (t for t in meta.get("issuetypes") or []
         if str(t.get("name", "")).lower() == wanted_type.lower()),
        None,
    )
    if type_meta is None:
        problems.append(
            f"Тип задачи «{wanted_type}» недоступен в проекте {result['project']}. "
            f"Доступные: {', '.join(issue_types) or '—'}"
        )
        return result
    # Имя типа из Jira: регистр приводим к тому, что знает инстанс, а в payload
    # подставляем id — по имени этот инстанс тип может и не найти.
    result["issuetype"] = str(type_meta.get("name", wanted_type))
    resolved["issuetype"] = _by_id(type_meta, result["issuetype"])

    meta_fields: dict = type_meta.get("fields") or {}
    for field_id, field_meta in meta_fields.items():
        if field_id in _META_IGNORED_FIELDS:
            continue
        title = str(field_meta.get("name", field_id))
        allowed = _allowed_names(field_meta)
        if field_meta.get("required"):
            result["required"].append(title)
            if field_id not in fields or fields.get(field_id) in ("", [], None):
                problems.append(
                    f"Не заполнено обязательное поле «{title}» ({field_id})"
                    + (f". Допустимые значения: {', '.join(allowed)}" if allowed else "")
                )
        # Значения, которых поле не принимает, — вторая по частоте причина 400-й.
        if not allowed or field_id not in fields:
            continue
        index = _allowed_index(field_meta)
        value = fields[field_id]
        items = value if isinstance(value, list) else [value]
        converted: list[dict] = []
        for entry in items:
            name = entry.get("name") if isinstance(entry, dict) else None
            if not name:
                converted.append(entry)
                continue
            match = index.get(str(name).lower())
            if match is None:
                problems.append(
                    f"Поле «{title}» ({field_id}) не принимает значение «{name}». "
                    f"Допустимые: {', '.join(allowed)}"
                )
                converted.append(entry)
                continue
            converted.append(_by_id(match, str(match.get("name") or match.get("value"))))
        resolved[field_id] = converted if isinstance(value, list) else converted[0]

    result["ok"] = not problems
    return result


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

    def _send(self, send: "Callable[[], httpx.Response]") -> httpx.Response:
        """Выполнить запрос, при протухшей сессии — перелогиниться и повторить ОДИН раз.

        Сессия Jira (JSESSIONID) живёт ограниченное время, а процесс jwu — долго:
        MCP-сервер поднимается один раз на сессию Claude Code и висит часами. Через
        какое-то время каждый его запрос начинал отвечать 401, хотя логин и пароль
        в порядке — «ключ протух». Здесь это лечится само: 401 при сессионной
        авторизации означает «сессию пора переустановить», а не «креды неверны».

        Повтор ровно один: если и после логина 401, дело в самих кредах, и второй
        круг только зациклит. При PAT-авторизации перелогиниться нечем — 401
        отдаётся сразу, с подсказкой, что токен пора обновить.
        """
        try:
            resp = send()
        except httpx.HTTPError as exc:  # сетевые ошибки
            raise JiraError(f"Сеть/Jira недоступна: {exc}") from exc
        if resp.status_code == 401 and self._session_login is not None:
            self._login(*self._session_login)
            try:
                resp = send()
            except httpx.HTTPError as exc:
                raise JiraError(f"Сеть/Jira недоступна: {exc}") from exc
        return resp

    def _raise_for(self, resp: httpx.Response) -> None:
        """Общая трактовка кодов ответа Jira (401 — уже после попытки перелогина)."""
        if resp.status_code == 401:
            raise JiraError(
                "401: сессия Jira не восстановилась — проверь логин/пароль (jwu configure)"
                if self._session_login is not None else
                "401: токен Jira невалиден или истёк — обнови: jwu configure --jira-token",
                401,
            )
        if resp.status_code == 403:
            raise JiraError("403: нет прав в Jira", 403)
        if resp.status_code >= 400:
            raise JiraError(f"{resp.status_code}: {_describe_error_body(resp)}", resp.status_code)

    def _get(self, path: str, params: dict | None = None) -> dict:
        resp = self._send(lambda: self._client.get(path, params=params))
        self._raise_for(resp)
        return resp.json()

    def _post(self, path: str, json_body: dict, params: dict | None = None) -> dict:
        resp = self._send(lambda: self._client.post(path, json=json_body, params=params))
        self._raise_for(resp)
        return resp.json() if resp.content else {}

    # --- API ------------------------------------------------------------- #

    def add_worklog(
        self,
        key: str,
        time_spent: str,
        *,
        comment: str | None = None,
        started: str | None = None,
    ) -> dict:
        """Залогировать время по задаче (worklog).

        ``time_spent`` — в формате Jira: ``"2h 30m"``, ``"45m"``, ``"1d 4h"``.
        ``started`` — ISO ``"2026-06-15T09:00:00.000+0000"``; по умолчанию Jira ставит
        текущий момент. ``comment`` — описание работы (попадает в карточку worklog).
        """
        body: dict = {"timeSpent": time_spent}
        if comment:
            body["comment"] = comment
        if started:
            body["started"] = started
        return self._post(f"/api/2/issue/{key}/worklog", body)

    def worklogs(self, key: str) -> list[dict]:
        """Все worklog-записи задачи (значения из /issue/{key}/worklog)."""
        data = self._get(f"/api/2/issue/{key}/worklog")
        return data.get("worklogs", []) or []

    def add_comment(self, key: str, body: str) -> dict:
        """Добавить комментарий к задаче (``POST /issue/{key}/comment``).

        Текст — обычный wiki-текст Jira Server. Возвращает созданный комментарий как
        его отдаёт Jira (id, author, created).
        """
        return self._post(f"/api/2/issue/{key}/comment", {"body": body})

    def create_meta(self, project: str) -> dict:
        """Метаданные создания задач проекта: типы задач и их обязательные поля.

        Отдаёт сырой блок ``projects[0]`` из
        ``/issue/createmeta?projectKeys=<KEY>&expand=projects.issuetypes.fields``.
        Пустой словарь — проекта нет или на создание в нём нет прав (Jira в этом случае
        возвращает не 403, а просто пустой список проектов).
        """
        data = self._get(
            "/api/2/issue/createmeta",
            params={"projectKeys": project, "expand": "projects.issuetypes.fields"},
        )
        projects = data.get("projects") or []
        return projects[0] if projects else {}

    def create_issue(self, fields: dict) -> dict:
        """Создать задачу из готового набора полей (``POST /issue``).

        ``fields`` собирается ``build_create_fields`` — сюда приходит уже готовым, чтобы
        один и тот же payload можно было показать в ``--dry-run`` и отправить.
        Возвращает ``{"key", "id", "url"}``: ключ созданной задачи и ссылку на неё.
        """
        created = self._post("/api/2/issue", {"fields": fields})
        key = created.get("key", "") or ""
        return {"key": key, "id": created.get("id", ""), "url": self.browse_url(key)}

    def transitions(self, key: str) -> list[dict]:
        """Доступные СЕЙЧАС переходы задачи по процессу.

        Список зависит от текущего статуса и схемы проекта, поэтому спрашивать его
        нужно каждый раз, а не помнить: «In Progress» из одного статуса есть,
        из другого — нет.
        """
        data = self._get(f"/api/2/issue/{key}/transitions",
                         params={"expand": "transitions.fields"})
        return [
            {
                "id": str(t.get("id", "")),
                "name": str(t.get("name", "")),
                "to": str((t.get("to") or {}).get("name", "")),
            }
            for t in data.get("transitions", []) or []
        ]

    def do_transition(self, key: str, transition_id: str) -> dict:
        """Выполнить переход по его id (``POST /issue/{key}/transitions``)."""
        self._post(f"/api/2/issue/{key}/transitions", {"transition": {"id": transition_id}})
        return {"key": key, "transition": transition_id}

    def add_attachment(self, key: str, path: Path) -> list[dict]:
        """Приложить файл к задаче (``POST /issue/{key}/attachments``).

        Jira требует заголовок ``X-Atlassian-Token: no-check`` (защита от CSRF) и
        multipart-поле именно с именем ``file`` — без этого запрос отвергается
        с невнятной ошибкой. Тело ответа — список созданных вложений.
        """
        def _upload() -> httpx.Response:
            # Файл открывается на каждую попытку: после первого чтения курсор в конце,
            # и повтор запроса отправил бы пустое тело.
            with path.open("rb") as fh:
                return self._client.post(
                    f"/api/2/issue/{key}/attachments",
                    files={"file": (path.name, fh)},
                    headers={"X-Atlassian-Token": "no-check"},
                )

        try:
            resp = self._send(_upload)
        except OSError as exc:
            raise JiraError(f"Не прочитать файл {path}: {exc}") from exc
        self._raise_for(resp)
        data = resp.json() if resp.content else []
        return data if isinstance(data, list) else [data]

    def link_types(self) -> list[dict]:
        """Типы связей между задачами (``Relates``, ``Blocks``, …) — как их знает инстанс."""
        data = self._get("/api/2/issueLinkType")
        return data.get("issueLinkTypes", []) or []

    def link_issues(self, inward: str, outward: str, link_type: str) -> dict:
        """Связать две задачи (``POST /issueLink``).

        Направление как в API Jira: ``inward`` — задача, к которой применяется inward-описание
        типа связи («is blocked by»), ``outward`` — та, к которой outward («blocks»).
        Тело ответа Jira при успехе пустое, поэтому возвращаем описание связи сами.
        """
        self._post(
            "/api/2/issueLink",
            {
                "type": {"name": link_type},
                "inwardIssue": {"key": inward},
                "outwardIssue": {"key": outward},
            },
        )
        return {"type": link_type, "inward": inward, "outward": outward}

    def browse_url(self, key: str) -> str:
        """Ссылка на задачу в вебе: ``<host>/browse/<KEY>``."""
        return f"{self.base_url}/browse/{key}" if key else ""

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

        def _download() -> int:
            """Скачать в dest; вернуть код ответа (файл пишется только при успехе)."""
            with self._client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    resp.read()
                    return resp.status_code
                with dest.open("wb") as fh:
                    for chunk in resp.iter_bytes():
                        fh.write(chunk)
            return resp.status_code

        try:
            status = _download()
            # Сессия могла протухнуть за время жизни процесса — как и в _send.
            if status == 401 and self._session_login is not None:
                self._login(*self._session_login)
                status = _download()
        except httpx.HTTPError as exc:
            raise JiraError(f"Сеть/Jira недоступна при скачивании вложения: {exc}") from exc
        if status >= 400:
            raise JiraError(f"{status}: не скачать вложение {url}", status)
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
