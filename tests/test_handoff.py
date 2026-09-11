"""Git-состояние в записях работы (JWU-32) и передача работы другой сессии (JWU-16)."""

import asyncio
import json

from typer.testing import CliRunner

from jwu import mcp_server as srv
from jwu.cli import main as cli
from jwu.core import gitinfo, handoff, workspaces
from jwu.core.models import (
    BuildStatus, Issue, Job, JobPRLink, JobRecord, PR, PRComment, PRTask, Reviewer,
)
from jwu.core.store import Store

runner = CliRunner()


def _fake_repo(root, branch="PROJ-1-fix", sha="abcdef1234567890", packed=False):
    """Репозиторий без git: только файлы, которые читает gitinfo."""
    git = root / ".git"
    git.mkdir(parents=True)
    (git / "HEAD").write_text(f"ref: refs/heads/{branch}\n")
    if packed:
        (git / "packed-refs").write_text(f"# pack-refs\n{sha} refs/heads/{branch}\n")
    else:
        ref = git / "refs" / "heads" / branch
        ref.parent.mkdir(parents=True)
        ref.write_text(sha + "\n")
    return root


# --- gitinfo.head_state ---------------------------------------------------- #

def test_head_state_reads_branch_and_sha_from_subfolder(tmp_path):
    root = _fake_repo(tmp_path / "repo")
    sub = root / "src" / "pkg"
    sub.mkdir(parents=True)
    assert gitinfo.head_state(sub) == ("PROJ-1-fix", "abcdef123456")
    assert gitinfo.head_state(tmp_path) == ("", "")  # не репозиторий


def test_head_state_packed_refs_and_detached(tmp_path):
    root = _fake_repo(tmp_path / "packed", packed=True)
    assert gitinfo.head_state(root) == ("PROJ-1-fix", "abcdef123456")
    detached = tmp_path / "det"
    (detached / ".git").mkdir(parents=True)
    (detached / ".git" / "HEAD").write_text("0123456789abcdef0123\n")
    assert gitinfo.head_state(detached) == ("", "0123456789ab")


# --- записи работы ------------------------------------------------------------ #

def test_job_record_keeps_git_state_and_migrates(tmp_path):
    store = Store(tmp_path / "s.db")
    try:
        job = store.create_job("PROJ-1", "работа")
        store.add_job_record(job.id, "начал", kind="phase", branch="PROJ-1-fix", commit="abc123")
        store.add_job_record(job.id, "без git", kind="note")
        recs = store.get_job(job.id).records
        assert (recs[0].branch, recs[0].commit) == ("PROJ-1-fix", "abc123")
        assert (recs[1].branch, recs[1].commit) == ("", "")
        assert int(store.get_meta("schema_version")) >= 8
    finally:
        store.close()


def test_cli_job_add_records_head_state(monkeypatch, tmp_path):
    root = _fake_repo(tmp_path / "repo")
    monkeypatch.chdir(root)
    db = tmp_path / "state.db"
    monkeypatch.setattr(cli, "_store", lambda: Store(db))
    res = runner.invoke(cli.app, ["job", "start", "PROJ-1", "--title", "t", "--json"])
    job_id = json.loads(res.stdout)["id"]
    res = runner.invoke(cli.app, ["job", "add", str(job_id), "фаза", "--kind", "phase", "--json"])
    assert res.exit_code == 0, res.output
    rec = json.loads(res.stdout)
    assert (rec["branch"], rec["commit"]) == ("PROJ-1-fix", "abcdef123456")
    res = runner.invoke(cli.app, ["job", "add", str(job_id), "без", "--no-git", "--json"])
    assert json.loads(res.stdout)["branch"] == ""
    res = runner.invoke(cli.app, ["job", "show", str(job_id)])
    assert "PROJ-1-fix@abcdef123456" in res.output


# --- handoff ------------------------------------------------------------------ #

def _job_with_log():
    return Job(
        id=5, task_key="PROJ-1", title="Перенос фикса", status="active",
        updated_at="2026-09-11T10:00:00+00:00",
        prs=[JobPRLink(pr_id=42, project="PROJ", repo="repo")],
        records=[
            JobRecord(kind="phase", text="Анализ", status="done", branch="PROJ-1-fix", commit="aaa111"),
            JobRecord(kind="decision", text="Порт делаем в 10.7"),
            JobRecord(kind="constraint", text="Не пушить в develop"),
            JobRecord(kind="bug", text="Падает на пустом фильтре"),
            JobRecord(kind="bug-resolved", text="Починил фильтр"),
            JobRecord(kind="bug", text="NPE в экспорте"),
            JobRecord(kind="phase", text="Тесты", status="pending", branch="PROJ-1-fix", commit="bbb222"),
            JobRecord(kind="todo", text="Обновить README"),
            JobRecord(kind="test-fail", text="3 упало в test_export"),
            JobRecord(kind="review", text="[B1] нет теста на экспорт"),
        ],
    )


class _FakeSvc:
    """Сервис с сетью: карточка задачи и детали PR под контролем теста."""

    tasks_client = object()
    pr_client = object()

    def __init__(self, fail=False):
        self.fail = fail

    def issue(self, key):
        if self.fail:
            raise RuntimeError("сеть")
        return Issue(key=key, summary="Экспорт падает", status="In Progress", priority="High",
                     assignee="Me", description="Длинное описание " * 3)

    def pr_detail(self, project, repo, pr_id):
        if self.fail:
            raise RuntimeError("сеть")
        from jwu.core.service import PRDetail

        pr = PR(id=pr_id, project=project, repository=repo, title="PROJ-1: фикс", author="me",
                source_branch="PROJ-1-fix", target_branch="develop", state="OPEN", conflicted=True,
                builds=[BuildStatus(state="FAILED", key="ci", url="u")], tasks_open=1,
                reviewers=[Reviewer(name="bob", display_name="Bob", status="NEEDS_WORK")])
        comments = [
            PRComment(id="1", author="Bob", text="Лишний запрос", file="a.py", line=3,
                      tasks=[PRTask(id=7, text="Убрать запрос", state="OPEN", comment_id="1")]),
            PRComment(id="2", author="Bob", text="Опечатка", file="b.py", line=9),
            PRComment(id="3", author="me", text="поправил", depth=1),
            PRComment(id="4", author="Bob", text="общий коммент без файла"),
        ]
        return PRDetail(pr=pr, comments=comments, commits=[])


def test_handoff_render_with_network(tmp_path):
    store = Store(tmp_path / "s.db")
    try:
        ws = store.get_workspace_by_slug("work")
        store.use_workspace(ws.id)
        store.add_rule("Стенд поднимается docker compose up", kind="howto")
        data = handoff.collect(store, _job_with_log(), svc=_FakeSvc())
        text = handoff.render(data)
    finally:
        store.close()
    assert "# Передача работы #5: PROJ-1 — Перенос фикса" in text
    assert "## Задача (сеть)" in text and "PROJ-1 [In Progress] (High) assignee: Me — Экспорт падает" in text
    assert "ветка `PROJ-1-fix`, коммит `bbb222`" in text      # последняя запись с git
    assert "- ✅ Анализ" in text
    assert "- ⏳ фаза: Тесты" in text and "- 📌 Обновить README" in text
    assert "- 🐛 не исправлен: NPE в экспорте" in text and "Падает на пустом фильтре" not in text.split("## Что осталось")[1].split("## Запреты")[0]
    assert "КРАСНЫЕ — 3 упало" in text
    assert "⛔ Не пушить в develop" in text and "🧭 Порт делаем в 10.7" in text
    assert "[B1] нет теста на экспорт" in text
    assert "### PR PROJ/repo#42 (сеть)" in text
    assert "блокеры: конфликт, красный билд, открытых задач 1, needs work" in text
    assert "- [ ] #7 Убрать запрос" in text
    assert "b.py:9 Bob: Опечатка" not in text          # на неё ответили
    assert "a.py:3 Bob: Лишний запрос" in text          # без ответа
    assert "Стенд поднимается docker compose up" in text
    assert "`git checkout PROJ-1-fix`" in text
    assert "в git ничего от jwu не оставляй" in text


def test_handoff_offline_falls_back_to_snapshots(tmp_path):
    store = Store(tmp_path / "s.db")
    try:
        ws = store.get_workspace_by_slug("work")
        store.use_workspace(ws.id)
        run = store.start_sync_run(["mine", "prs:mine"])
        store.save_issue_snapshot(run, Issue(key="PROJ-1", summary="Из снапшота", status="Open"), ["mine"])
        store.save_pr_snapshot(run, PR(id=42, project="PROJ", repository="repo", title="снап", tasks_open=2), ["mine"])
        store.finish_sync_run(run, {"tasks:mine": 1, "prs:mine": 1})
        data = handoff.collect(store, _job_with_log(), svc=_FakeSvc(fail=True))
        text = handoff.render(data)
    finally:
        store.close()
    assert "## Задача (снапшот)" in text and "Из снапшота" in text
    assert "### PR PROJ/repo#42 (снапшот)" in text and "открытых задач 2" in text
    # совсем без данных — честно сказано
    store = Store(tmp_path / "empty.db")
    try:
        ws = store.get_workspace_by_slug("work")
        store.use_workspace(ws.id)
        text = handoff.render(handoff.collect(store, _job_with_log(), offline=True))
    finally:
        store.close()
    assert "карточка недоступна" in text and "(нет данных)" in text


def test_cli_job_handoff_offline(monkeypatch, tmp_path):
    db = tmp_path / "state.db"
    monkeypatch.setattr(cli, "_store", lambda: Store(db))
    res = runner.invoke(cli.app, ["job", "start", "PROJ-9", "--title", "hand", "--json"])
    job_id = json.loads(res.stdout)["id"]
    runner.invoke(cli.app, ["job", "add", str(job_id), "фаза один", "--kind", "phase", "--status", "done", "--no-git"])
    res = runner.invoke(cli.app, ["job", "handoff", str(job_id), "--offline"])
    assert res.exit_code == 0, res.output
    assert f"# Передача работы #{job_id}: PROJ-9 — hand" in res.output
    assert "✅ фаза один" in res.output
    res = runner.invoke(cli.app, ["job", "handoff", str(job_id), "--offline", "--json"])
    payload = json.loads(res.stdout)
    assert payload["job"]["id"] == job_id and "markdown" in payload


def test_mcp_job_handoff(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    monkeypatch.setenv("JWU_DB_PATH", str(db))
    monkeypatch.setattr(srv, "_base_store", None)
    monkeypatch.setattr(srv, "_full", {})
    monkeypatch.setattr(srv, "_stores", {})
    store = Store(db)
    home = workspaces.create(store, "home")
    folder = tmp_path / "proj"
    folder.mkdir()
    workspaces.add_path(store, home, folder)
    store.use_workspace(home.id)
    job = store.create_job("", "локальная")
    store.close()
    monkeypatch.chdir(folder)
    try:
        rec = asyncio.run(srv.jwu_job_add(job.id, "шаг", kind="phase"))
        assert rec["workspace"] == "home" and rec["branch"] == ""   # папка не репозиторий
        payload = asyncio.run(srv.jwu_job_handoff(job.id, offline=True))
        assert payload["anchor"] == f"#{job.id}" and "шаг" in payload["markdown"]
    finally:
        for s in list(srv._stores.values()):
            s.close()
        if srv._base_store is not None:
            srv._base_store.close()
