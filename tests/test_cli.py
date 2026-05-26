import json

from typer.testing import CliRunner

from jwu.cli import main as cli
from jwu.core.store import Store

runner = CliRunner()


def _patch_store(monkeypatch, tmp_path):
    db = tmp_path / "state.db"
    monkeypatch.setattr(cli, "_store", lambda: Store(db))


def test_note_and_notes_json(monkeypatch, tmp_path):
    _patch_store(monkeypatch, tmp_path)

    res = runner.invoke(cli.app, ["note", "PROJ-1", "перенёс фикс", "--json"])
    assert res.exit_code == 0

    res = runner.invoke(cli.app, ["notes", "PROJ-1", "--json"])
    assert res.exit_code == 0
    payload = json.loads(res.stdout)
    assert payload[0]["text"] == "перенёс фикс"
    assert payload[0]["author"] == "claude"


def test_changes_empty_json(monkeypatch, tmp_path):
    _patch_store(monkeypatch, tmp_path)
    res = runner.invoke(cli.app, ["changes", "--json"])
    assert res.exit_code == 0
    assert json.loads(res.stdout) == []


def test_job_lifecycle_cli(monkeypatch, tmp_path):
    _patch_store(monkeypatch, tmp_path)

    res = runner.invoke(cli.app, ["job", "start", "PROJ-399", "--title", "dev", "--json"])
    assert res.exit_code == 0
    job_id = json.loads(res.stdout)["id"]

    assert runner.invoke(cli.app, ["job", "add", str(job_id), "мердж", "--kind", "phase"]).exit_code == 0
    assert runner.invoke(cli.app, ["job", "link", str(job_id), "--pr", "334",
                                   "--project", "PROJ", "--repo", "repo"]).exit_code == 0

    res = runner.invoke(cli.app, ["job", "show", str(job_id), "--json"])
    payload = json.loads(res.stdout)
    assert payload["records"][0]["kind"] == "phase"
    assert payload["prs"][0]["pr_id"] == 334

    res = runner.invoke(cli.app, ["jobs", "--task", "PROJ-399", "--json"])
    assert json.loads(res.stdout)[0]["id"] == job_id

    assert runner.invoke(cli.app, ["job", "done", str(job_id)]).exit_code == 0
    res = runner.invoke(cli.app, ["jobs", "--status", "active", "--json"])
    assert json.loads(res.stdout) == []


def test_job_add_bug_kinds(monkeypatch, tmp_path):
    _patch_store(monkeypatch, tmp_path)
    job_id = json.loads(runner.invoke(
        cli.app, ["job", "start", "X-1", "--title", "dev", "--json"]).stdout)["id"]

    for kind in ("warning", "bug", "bug-resolved"):
        res = runner.invoke(cli.app, ["job", "add", str(job_id), f"text-{kind}", "--kind", kind])
        assert res.exit_code == 0, (kind, res.output)

    payload = json.loads(runner.invoke(cli.app, ["job", "show", str(job_id), "--json"]).stdout)
    assert [r["kind"] for r in payload["records"]] == ["warning", "bug", "bug-resolved"]

    # невалидный тип отклоняется выбором click.Choice
    bad = runner.invoke(cli.app, ["job", "add", str(job_id), "x", "--kind", "lol"])
    assert bad.exit_code != 0

    # бейдж исправленного бага виден в человекочитаемом выводе
    shown = runner.invoke(cli.app, ["job", "show", str(job_id)])
    assert "БАГ ИСПРАВЛЕН" in shown.output


def test_job_add_test_kinds(monkeypatch, tmp_path):
    _patch_store(monkeypatch, tmp_path)
    job_id = json.loads(runner.invoke(
        cli.app, ["job", "start", "X-1", "--title", "dev", "--json"]).stdout)["id"]

    for kind in ("test-pass", "test-fail"):
        res = runner.invoke(cli.app, ["job", "add", str(job_id), f"pytest: {kind}", "--kind", kind])
        assert res.exit_code == 0, (kind, res.output)

    payload = json.loads(runner.invoke(cli.app, ["job", "show", str(job_id), "--json"]).stdout)
    assert [r["kind"] for r in payload["records"]] == ["test-pass", "test-fail"]

    shown = runner.invoke(cli.app, ["job", "show", str(job_id)]).output
    assert "ТЕСТЫ OK" in shown and "ТЕСТЫ УПАЛИ" in shown


def test_job_add_decision_and_todo_kinds(monkeypatch, tmp_path):
    _patch_store(monkeypatch, tmp_path)
    job_id = json.loads(runner.invoke(
        cli.app, ["job", "start", "X-1", "--title", "dev", "--json"]).stdout)["id"]

    for kind in ("decision", "todo"):
        res = runner.invoke(cli.app, ["job", "add", str(job_id), f"{kind}-text", "--kind", kind])
        assert res.exit_code == 0, (kind, res.output)

    payload = json.loads(runner.invoke(cli.app, ["job", "show", str(job_id), "--json"]).stdout)
    assert [r["kind"] for r in payload["records"]] == ["decision", "todo"]

    shown = runner.invoke(cli.app, ["job", "show", str(job_id)]).output
    assert "РЕШЕНИЕ" in shown and "TODO" in shown


def test_job_add_constraint_kind(monkeypatch, tmp_path):
    _patch_store(monkeypatch, tmp_path)
    job_id = json.loads(runner.invoke(
        cli.app, ["job", "start", "WM-9", "--title", "dev", "--json"]).stdout)["id"]

    res = runner.invoke(cli.app, ["job", "add", str(job_id),
                                  "не трогать вебхуки каналов", "--kind", "constraint"])
    assert res.exit_code == 0

    res = runner.invoke(cli.app, ["job", "show", str(job_id), "--json"])
    assert json.loads(res.stdout)["records"][0]["kind"] == "constraint"

    # в человекочитаемом выводе запрет помечается явно
    res = runner.invoke(cli.app, ["job", "show", str(job_id)])
    assert "ЗАПРЕТ" in res.stdout


def test_job_show_missing_exits_nonzero(monkeypatch, tmp_path):
    _patch_store(monkeypatch, tmp_path)
    res = runner.invoke(cli.app, ["job", "show", "999", "--json"])
    assert res.exit_code == 1


def test_job_add_missing_exits_nonzero(monkeypatch, tmp_path):
    _patch_store(monkeypatch, tmp_path)
    res = runner.invoke(cli.app, ["job", "add", "999", "x"])
    assert res.exit_code == 1


def test_job_cancel_and_delete_cli(monkeypatch, tmp_path):
    _patch_store(monkeypatch, tmp_path)
    jid = json.loads(runner.invoke(
        cli.app, ["job", "start", "WM-1", "--title", "dev", "--json"]).stdout)["id"]

    # закрыть как неактуальную → выпадает из active
    assert runner.invoke(cli.app, ["job", "cancel", str(jid)]).exit_code == 0
    assert json.loads(runner.invoke(cli.app, ["jobs", "--status", "active", "--json"]).stdout) == []

    # удалить совсем
    assert runner.invoke(cli.app, ["job", "delete", str(jid), "--yes"]).exit_code == 0
    assert runner.invoke(cli.app, ["job", "show", str(jid), "--json"]).exit_code == 1


def test_job_delete_missing_exits_nonzero(monkeypatch, tmp_path):
    _patch_store(monkeypatch, tmp_path)
    assert runner.invoke(cli.app, ["job", "delete", "999", "--yes"]).exit_code == 1


from jwu.core.models import Issue as _Issue


class _FakeSvc:
    def __init__(self, store):
        self._store = store
    def __enter__(self): return self
    def __exit__(self, *exc): pass
    def issue(self, key): return _Issue(key=key, summary="S", status="In Progress")
    def get_notes(self, key): return []
    def jobs_for_task(self, key): return self._store.jobs_for_task(key)
    def pr(self, pr_id, project=None, repo=None):
        from jwu.core.models import PR
        return PR(id=pr_id, title="t", project=project or "PROJ", repository=repo or "repo")
    def pr_detail(self, project, repo, pr_id):
        from jwu.core.models import PR, PRComment
        from jwu.core.service import PRDetail
        pr = PR(id=pr_id, title="t", project=project or "PROJ", repository=repo or "repo")
        return PRDetail(
            pr=pr,
            comments=[PRComment(id="1", author="Dave", text="нужно поправить", file="a.py", line=5)],
            commits=[],
        )
    def jobs_for_pr(self, pr_id, project="", repo=""): return self._store.jobs_for_pr(pr_id)


def test_task_json_includes_jobs(monkeypatch, tmp_path):
    _patch_store(monkeypatch, tmp_path)
    store = Store(tmp_path / "state.db")
    store.create_job("PROJ-399", "dev")
    store.close()
    monkeypatch.setattr(cli, "_service", lambda: _FakeSvc(Store(tmp_path / "state.db")))

    res = runner.invoke(cli.app, ["task", "PROJ-399", "--json"])
    assert res.exit_code == 0
    payload = json.loads(res.stdout)
    assert payload["jobs"][0]["task_key"] == "PROJ-399"


def test_pr_json_includes_jobs(monkeypatch, tmp_path):
    _patch_store(monkeypatch, tmp_path)
    store = Store(tmp_path / "state.db")
    j = store.create_job("PROJ-399", "dev")
    store.link_job_pr(j.id, 334, project="PROJ", repo="repo")
    store.close()
    monkeypatch.setattr(cli, "_service", lambda: _FakeSvc(Store(tmp_path / "state.db")))

    res = runner.invoke(cli.app, ["pr", "334", "--project", "PROJ", "--repo", "repo", "--json"])
    assert res.exit_code == 0
    payload = json.loads(res.stdout)
    assert payload["jobs"][0]["id"] == j.id
    # ревью-комменты должны попадать в JSON (их читает нейронка)
    assert [c["author"] for c in payload["comments"]] == ["Dave"]
    assert payload["comments"][0]["text"] == "нужно поправить"


def test_configure_non_interactive_writes_config_and_secrets(monkeypatch, tmp_path):
    import keyring

    from jwu.core import config as cfgmod
    from jwu.core.config import load_config

    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr(cfgmod, "config_path", lambda: cfg_path)

    store = {}
    monkeypatch.setattr(keyring, "set_password",
                        lambda s, a, p: store.__setitem__((s, a), p))
    monkeypatch.setattr(keyring, "get_password", lambda s, a: store.get((s, a)))

    class _FakeSvc:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def auth_check(self): return {"jira": {"ok": True, "name": "Alice"},
                                      "bitbucket": {"ok": True}}
    monkeypatch.setattr(cli.Service, "from_config", classmethod(lambda cls, c: _FakeSvc()))

    res = runner.invoke(cli.app, [
        "configure", "--non-interactive",
        "--jira-host", "https://jira.acme.com",
        "--jira-user", "alice",
        "--jira-project", "ACME",
        "--jira-token", "JTOK",
        "--bitbucket-host", "https://git.acme.com",
        "--bitbucket-repo", "server",
        "--bitbucket-token", "BTOK",
        "--db-path", str(tmp_path / "jwu.db"),
    ])
    assert res.exit_code == 0, res.output

    loaded = load_config(cfg_path)
    assert loaded.jira.base_url == "https://jira.acme.com"
    assert loaded.jira.username == "alice"
    assert loaded.bitbucket.repo == "server"
    assert loaded.storage.db_path == str(tmp_path / "jwu.db")

    # секреты ушли в keyring, а НЕ в файл
    text = cfg_path.read_text()
    assert "JTOK" not in text and "BTOK" not in text
    assert store[("jira-pat", "jira")] == "JTOK"
    assert store[("bitbucket-pat", "bitbucket")] == "BTOK"


def test_configure_non_interactive_keeps_existing_secret_when_omitted(monkeypatch, tmp_path):
    import keyring

    from jwu.core import config as cfgmod

    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr(cfgmod, "config_path", lambda: cfg_path)
    store = {("jira-pat", "jira"): "OLD"}
    monkeypatch.setattr(keyring, "set_password",
                        lambda s, a, p: store.__setitem__((s, a), p))
    monkeypatch.setattr(keyring, "get_password", lambda s, a: store.get((s, a)))

    class _FakeSvc:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def auth_check(self): return {"jira": {"ok": True}, "bitbucket": {"ok": True}}
    monkeypatch.setattr(cli.Service, "from_config", classmethod(lambda cls, c: _FakeSvc()))

    res = runner.invoke(cli.app, [
        "configure", "--non-interactive", "--jira-user", "bob",
    ])
    assert res.exit_code == 0, res.output
    assert store[("jira-pat", "jira")] == "OLD"  # токен не передан — не затёрт


def test_install_claude_skills_to_custom_dest(tmp_path):
    res = runner.invoke(cli.app, ["install-claude-skills", "--dest", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert (tmp_path / "jwu-resume-job" / "SKILL.md").is_file()
    assert (tmp_path / "jwu-start-job" / "SKILL.md").is_file()
    assert "Готово" in res.output
