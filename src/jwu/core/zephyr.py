"""Zephyr Scale (Server/DC, плагин test-manager в Jira): тест-кейсы — чтение и запись.

Ходит через клиент Jira контура (та же сессия, гейт и учётка): публичный API
``/rest/atm/1.0`` — кейсы, поиск, связи с задачами, папки; служебный ``/rest/tests/1.0`` —
то, чего в публичном нет: проекты, дерево папок, статусы, приоритеты и свои поля проекта.

Проект кейсов — настройка контура ``zephyr.project`` (кейсы часто живут в отдельном проекте,
а задачи — в другом: связь «кейс ↔ задача» идёт через ``issueLinks``). Удаления кейсов в jwu
нет — намеренно. Запись (создать, поправить, привязать, завести папку) — только с «да».
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any, Iterable, Optional

if TYPE_CHECKING:
    from .jira import JiraClient
    from .store import Store

PROJECT_SETTING = "zephyr.project"
AGENT_SETTING = "testing.agent"
DEFAULT_AGENT = "jwu-testing-agent"
RULES_GUIDE = "zephyr-testcase-rules"
CASE_FIELDS = ("name", "objective", "precondition", "folder", "status", "priority", "labels",
               "issueLinks", "customFields", "owner")


class ZephyrError(RuntimeError):
    pass


def project(store: "Store", workspace_id: int) -> str:
    return (store.workspace_settings(workspace_id).get(PROJECT_SETTING) or "").strip()


def agent(store: "Store", workspace_id: int) -> str:
    return (store.workspace_settings(workspace_id).get(AGENT_SETTING) or "").strip() or DEFAULT_AGENT


def _flatten(node: dict, prefix: str = "") -> list[dict]:
    out = []
    for child in node.get("children") or []:
        path = f"{prefix}/{child.get('name', '')}"
        out.append({"path": path, "count": int(child.get("itemsCount") or 0)})
        out += _flatten(child, path)
    return out


def steps_from(items: Iterable[Any]) -> list[dict]:
    """Шаги в формате Zephyr из [{"step"/"description", "data"/"testData", "expected"/"expectedResult"}]."""
    out = []
    for it in items:
        if isinstance(it, str):
            it = {"description": it}
        step = {"description": it.get("description") or it.get("step") or "",
                "expectedResult": it.get("expectedResult") or it.get("expected") or ""}
        data = it.get("testData") or it.get("data")
        if data:
            step["testData"] = data
        if not step["description"]:
            raise ZephyrError("у шага нет описания")
        out.append(step)
    return out


class ZephyrClient:
    def __init__(self, jira: "JiraClient", project_key: str = "") -> None:
        self.jira = jira
        self.project_key = project_key

    def _key(self, project_key: Optional[str]) -> str:
        key = (project_key or self.project_key or "").strip()
        if not key:
            raise ZephyrError("Не знаю проект кейсов: jwu zephyr setup --project KEY (или передай project)")
        return key

    # --- чтение ------------------------------------------------------------ #

    def ping(self) -> dict:
        return {"projects": len(self.projects()), "project": self.project_key}

    def projects(self) -> list[dict]:
        return [{"id": p.get("id"), "key": p.get("key"), "name": p.get("name")}
                for p in self.jira._get("/tests/1.0/project") or []]

    def project_id(self, project_key: Optional[str] = None) -> int:
        key = self._key(project_key)
        for p in self.projects():
            if p["key"] == key:
                return int(p["id"])
        raise ZephyrError(f"Проекта {key} в Zephyr нет (или у тебя нет к нему доступа)")

    def case(self, key: str) -> dict:
        return self.jira._get(f"/atm/1.0/testcase/{key}")

    def search(self, query: str, max_results: int = 50) -> list[dict]:
        return list(self.jira._get("/atm/1.0/testcase/search",
                                   {"query": query, "maxResults": max_results}) or [])

    def for_issue(self, issue_key: str) -> list[dict]:
        """Кейсы, привязанные к задаче (из любого проекта кейсов)."""
        keys = [t.get("key") for t in self.jira._get(f"/atm/1.0/issuelink/{issue_key}/testcases") or []]
        return [self.case(k) for k in keys if k]

    def folders(self, project_key: Optional[str] = None) -> list[dict]:
        tree = self.jira._get(f"/tests/1.0/project/{self.project_id(project_key)}/foldertree/testcase")
        return _flatten(tree)

    def fields(self, project_key: Optional[str] = None) -> dict:
        """Что можно ставить кейсу в проекте: статусы, приоритеты, свои поля (с вариантами)."""
        pid = self.project_id(project_key)
        statuses = self.jira._get(f"/tests/1.0/project/{pid}/testcasestatus") or []
        priorities = self.jira._get(f"/tests/1.0/project/{pid}/testcasepriority") or []
        custom = self.jira._get(f"/tests/1.0/project/{pid}/customfields/testcase") or []
        return {
            "project": self._key(project_key),
            "statuses": [{"name": s["name"], "default": bool(s.get("isDefault"))} for s in statuses],
            "priorities": [{"name": p["name"], "default": bool(p.get("isDefault")),
                            "description": p.get("description", "")} for p in priorities],
            "custom_fields": [{"name": c["name"], "type": c.get("type", ""), "required": bool(c.get("required")),
                               "options": [o["name"] for o in c.get("options") or [] if not o.get("archived")]}
                              for c in custom if not c.get("archived")],
        }

    def suggest_folder(self, issue_key: str, project_key: Optional[str] = None) -> list[dict]:
        """Куда класть кейс задачи: папки её уже существующих кейсов (чаще — выше)."""
        key = self._key(project_key)
        counter = Counter(c.get("folder") for c in self.for_issue(issue_key)
                          if c.get("folder") and c.get("projectKey") == key)
        return [{"path": path, "cases": n} for path, n in counter.most_common()]

    # --- запись (только после «да» пользователя) ----------------------------- #

    def create(self, *, name: str, steps: Iterable[Any] = (), project_key: Optional[str] = None,
               **fields: Any) -> dict:
        body: dict = {"projectKey": self._key(project_key), "name": name}
        body.update({k: v for k, v in fields.items() if k in CASE_FIELDS and v not in (None, "", [], {})})
        script = steps_from(steps)
        if script:
            body["testScript"] = {"type": "STEP_BY_STEP", "steps": script}
        return self.jira._post("/atm/1.0/testcase", body)

    def update(self, key: str, *, steps: Optional[Iterable[Any]] = None, **fields: Any) -> dict:
        body: dict = {k: v for k, v in fields.items() if k in CASE_FIELDS and v is not None}
        if steps is not None:
            body["testScript"] = {"type": "STEP_BY_STEP", "steps": steps_from(steps)}
        if not body:
            raise ZephyrError("нечего менять")
        self.jira._put(f"/atm/1.0/testcase/{key}", body)
        return {"key": key, "updated": sorted(body)}

    def link(self, key: str, issue_keys: Iterable[str]) -> dict:
        """Привязать кейс к задачам (к уже привязанным добавляются, не заменяя)."""
        current = list(self.case(key).get("issueLinks") or [])
        merged = current + [k for k in issue_keys if k and k not in current]
        if merged == current:
            return {"key": key, "issueLinks": current, "changed": False}
        self.jira._put(f"/atm/1.0/testcase/{key}", {"issueLinks": merged})
        return {"key": key, "issueLinks": merged, "changed": True}

    def create_folder(self, path: str, project_key: Optional[str] = None) -> dict:
        path = "/" + path.strip("/")
        return self.jira._post("/atm/1.0/folder", {"projectKey": self._key(project_key), "name": path,
                                                   "type": "TEST_CASE"})

    def case_url(self, key: str) -> str:
        base = str(self.jira._client.base_url).rstrip("/").removesuffix("/rest")
        return f"{base}/secure/Tests.jspa#/testCase/{key}"
