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
        # CLI проверяет наличие клиентов, чтобы отказать в контуре без провайдера
        self.tasks_client = object()
        self.pr_client = object()
        self.jira = self.tasks_client
        self.bitbucket = self.pr_client
        self.workspace = None
    def __enter__(self): return self
    def __exit__(self, *exc): pass
    def close(self): pass
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


def test_configure_non_interactive_writes_settings_and_secrets(monkeypatch, tmp_path):
    """Несекретные поля и секреты уезжают в воркспейс; в config.toml секретов нет."""
    db, cfg_path, keyring_store = _configure_env(monkeypatch, tmp_path)

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

    store = Store(db)
    ws = store.get_workspace_by_slug("work")
    settings = store.workspace_settings(ws.id)
    assert settings["jira.base_url"] == "https://jira.acme.com"
    assert settings["jira.username"] == "alice"
    assert settings["bitbucket.repo"] == "server"
    assert store.workspace_secrets(ws.id) == {"jira.token": "JTOK", "bitbucket.token": "BTOK"}
    store.close()

    # секретов нет в config.toml — там только путь до БД
    text = cfg_path.read_text()
    assert "JTOK" not in text and "BTOK" not in text
    assert str(tmp_path / "jwu.db") in text


def test_configure_keeps_existing_secret_when_omitted(monkeypatch, tmp_path):
    """Не переданный токен не затирается — остаётся прежний."""
    db, _, _ = _configure_env(monkeypatch, tmp_path)
    assert runner.invoke(cli.app, ["configure", "--non-interactive",
                                   "--jira-token", "OLD"]).exit_code == 0

    res = runner.invoke(cli.app, ["configure", "--non-interactive", "--jira-user", "bob"])
    assert res.exit_code == 0, res.output

    store = Store(db)
    ws = store.get_workspace_by_slug("work")
    assert store.workspace_secrets(ws.id)["jira.token"] == "OLD"
    assert store.workspace_settings(ws.id)["jira.username"] == "bob"
    store.close()


def test_install_claude_skills_to_custom_dest(tmp_path):
    res = runner.invoke(cli.app, ["install-claude-skills", "--dest", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert (tmp_path / "jwu-resume-job" / "SKILL.md").is_file()
    assert (tmp_path / "jwu-start-job" / "SKILL.md").is_file()
    assert "Готово" in res.output


def _fake_authcheck(monkeypatch):
    class _FakeSvc:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def auth_check(self): return {"jira": {"ok": True}, "bitbucket": {"ok": True}}
    # проверка связи после configure идёт через воркспейс — сеть в тестах не трогаем
    monkeypatch.setattr(cli.Service, "for_workspace",
                        classmethod(lambda cls, ws, cfg=None, **kw: _FakeSvc()))


def _configure_env(monkeypatch, tmp_path):
    """Изолировать configure: свой config.toml, своя БД, keyring в памяти."""
    import keyring

    from jwu.core import config as cfgmod

    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr(cfgmod, "config_path", lambda: cfg_path)
    db = tmp_path / "state.db"
    monkeypatch.setattr(cli, "_open_store", lambda: Store(db))
    monkeypatch.setattr(cli, "_WORKSPACE_ARG", None)
    keyring_store = {}
    monkeypatch.setattr(keyring, "set_password",
                        lambda s, a, p: keyring_store.__setitem__((s, a), p))
    monkeypatch.setattr(keyring, "get_password", lambda s, a: keyring_store.get((s, a)))
    _fake_authcheck(monkeypatch)
    return db, cfg_path, keyring_store


def test_configure_interactive_writes_to_workspace(monkeypatch, tmp_path):
    """Интерактивный визард спрашивает гейт и кладёт настройки/секреты в воркспейс."""
    db, cfg_path, keyring_store = _configure_env(monkeypatch, tmp_path)

    # порядок промптов: host, user, project, PAT, session-pw, gate-login, gate-pw,
    # sdesk-host (пусто => SDESK пропускается целиком),
    # bb-host, bb-project, bb-repo, bb-PAT, jenkins-host, jenkins-user, db-path.
    # Jenkins username пустой => токен не спрашивается.
    answers = "\n".join([
        "https://jira.x", "alice", "ACME", "", "",
        "gw", "GPW",
        "",
        "https://git.x", "PROJ", "repo", "",
        "", "",
        str(tmp_path / "x.db"),
    ]) + "\n"
    res = runner.invoke(cli.app, ["configure"], input=answers)
    assert res.exit_code == 0, res.output

    store = Store(db)
    ws = store.get_workspace_by_slug("work")
    settings = store.workspace_settings(ws.id)
    assert settings["jira.base_url"] == "https://jira.x"
    assert settings["jira.proxy_basic_user"] == "gw"
    secrets_in_db = store.workspace_secrets(ws.id)
    assert secrets_in_db["jira.gate_password"] == "GPW"
    assert "jira.password" not in secrets_in_db  # пустой сессионный пароль не пишется
    store.close()

    # в keyring больше не пишем — только читаем как фолбэк
    assert keyring_store == {}


def test_configure_keeps_db_path_in_global_config(monkeypatch, tmp_path):
    """Путь до БД остаётся глобальным: его надо знать ДО того, как известен воркспейс."""
    db, cfg_path, _ = _configure_env(monkeypatch, tmp_path)
    from jwu.core import config as cfgmod

    res = runner.invoke(cli.app, [
        "configure", "--non-interactive", "--db-path", str(tmp_path / "moved.db"),
    ])
    assert res.exit_code == 0, res.output
    assert cfgmod.load_config(cfg_path).storage.db_path == str(tmp_path / "moved.db")


def test_configure_enables_integrations_by_hosts(monkeypatch, tmp_path):
    """Настроенные креды поднимают провайдера ЛОКАЛЬНОГО контура — иначе он бы молчал."""
    db, _, _ = _configure_env(monkeypatch, tmp_path)
    runner.invoke(cli.app, ["workspace", "create", "home"])

    res = runner.invoke(cli.app, [
        "-W", "home", "configure", "--non-interactive",
        "--jira-host", "https://jira.acme.com", "--jira-token", "JTOK",
    ])
    assert res.exit_code == 0, res.output

    store = Store(db)
    ws = store.get_workspace_by_slug("home")
    assert ws.provider == "jira"
    assert ws.bitbucket_enabled is False  # хост Bitbucket не задавали
    store.close()


def test_configure_enables_github_provider(monkeypatch, tmp_path):
    """То же для GitHub: owner + токен делают локальный контур github-контуром."""
    db, _, _ = _configure_env(monkeypatch, tmp_path)
    runner.invoke(cli.app, ["workspace", "create", "dndeck"])

    res = runner.invoke(cli.app, [
        "-W", "dndeck", "configure", "--non-interactive",
        "--github-owner", "akotkov", "--github-repos", "dndeck",
        "--github-token", "GHTOK",
    ])
    assert res.exit_code == 0, res.output

    store = Store(db)
    ws = store.get_workspace_by_slug("dndeck")
    assert ws.provider == "github" and ws.github_enabled is True
    assert ws.prs_enabled is True          # PR у GitHub приходят из того же места
    settings = store.workspace_settings(ws.id)
    assert settings["github.owner"] == "akotkov"
    assert store.workspace_secrets(ws.id)["github.token"] == "GHTOK"
    store.close()


def test_configure_export_then_import_cli(monkeypatch, tmp_path):
    """configure export пишет бандл, configure import восстанавливает воркспейс."""
    db, cfg_path, _ = _configure_env(monkeypatch, tmp_path)

    res = runner.invoke(cli.app, [
        "configure", "--non-interactive",
        "--jira-host", "https://jira.acme.com", "--jira-user", "alice",
        "--jira-project", "ACME", "--jira-password", "JPW",
        "--gate-user", "gw", "--gate-password", "GPW",
        "--bitbucket-token", "BTOK",
    ])
    assert res.exit_code == 0, res.output

    bundle = tmp_path / "b.toml"
    res = runner.invoke(cli.app, ["configure", "export", str(bundle)])
    assert res.exit_code == 0, res.output
    assert bundle.exists()

    # «новая машина»: чистая БД
    db2 = tmp_path / "fresh.db"
    monkeypatch.setattr(cli, "_open_store", lambda: Store(db2))
    res = runner.invoke(cli.app, ["configure", "import", str(bundle)])
    assert res.exit_code == 0, res.output

    store = Store(db2)
    ws = store.get_workspace_by_slug("work")
    assert store.workspace_settings(ws.id)["jira.proxy_basic_user"] == "gw"
    assert store.workspace_secrets(ws.id) == {
        "jira.password": "JPW", "jira.gate_password": "GPW", "bitbucket.token": "BTOK",
    }
    store.close()


def test_import_reads_legacy_bundle_format(tmp_path):
    """Старый бандл (секреты парой service/account) читается и раскладывается по слотам."""
    from jwu.core.config import read_bundle

    bundle = tmp_path / "old.toml"
    bundle.write_text(
        '[jira]\nbase_url = "https://jira.x"\nusername = "alice"\n'
        'proxy_basic_user = "gw"\n\n'
        '[[secrets]]\nservice = "jira-login"\naccount = "alice"\nvalue = "JPW"\n\n'
        '[[secrets]]\nservice = "bitbucket-pat"\naccount = "bitbucket"\nvalue = "BTOK"\n'
    )
    cfg, values = read_bundle(bundle)
    assert cfg.jira.username == "alice"
    assert values == {"jira.password": "JPW", "bitbucket.token": "BTOK"}


# --------------------------------------------------------------------------- #
# Воркспейсы
# --------------------------------------------------------------------------- #


def _patch_open_store(monkeypatch, tmp_path):
    """Подменить обе фабрики Store: команды воркспейсов ходят через _open_store."""
    db = tmp_path / "state.db"
    monkeypatch.setattr(cli, "_open_store", lambda: Store(db))

    def _scoped():
        store = Store(db)
        store.use_workspace(cli._resolve_workspace(store).id)
        return store

    monkeypatch.setattr(cli, "_store", _scoped)
    monkeypatch.setattr(cli, "_WORKSPACE_ARG", None)
    return db


def test_workspace_create_list_and_current(monkeypatch, tmp_path):
    monkeypatch.delenv("JWU_WORKSPACE", raising=False)
    _patch_open_store(monkeypatch, tmp_path)
    folder = tmp_path / "pet"
    folder.mkdir()

    res = runner.invoke(cli.app, ["workspace", "create", "home", "--name", "Личное",
                                  "--path", str(folder), "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    assert payload["slug"] == "home" and payload["provider"] == "local"
    assert payload["jira_enabled"] is False

    res = runner.invoke(cli.app, ["workspace", "list", "--json"])
    slugs = [w["slug"] for w in json.loads(res.stdout)["workspaces"]]
    assert slugs == ["work", "home"]

    # активным стал созданный (--use по умолчанию)
    res = runner.invoke(cli.app, ["workspace", "current", "--json"])
    assert json.loads(res.stdout)["slug"] == "home"


def test_workspace_scopes_jobs(monkeypatch, tmp_path):
    monkeypatch.delenv("JWU_WORKSPACE", raising=False)
    _patch_open_store(monkeypatch, tmp_path)
    assert runner.invoke(cli.app, ["workspace", "create", "home", "--no-use"]).exit_code == 0
    # воркспейсов стало два, текущая папка ни к одному не привязана — нужен явный выбор
    res = runner.invoke(cli.app, ["jobs", "--json"])
    assert res.exit_code == 1 and "в каком воркспейсе" in res.output
    assert runner.invoke(cli.app, ["workspace", "use", "work"]).exit_code == 0

    res = runner.invoke(cli.app, ["job", "start", "PROJ-1", "--title", "рабочая", "--json"])
    assert res.exit_code == 0, res.output

    res = runner.invoke(cli.app, ["-W", "home", "jobs", "--json"])
    assert json.loads(res.stdout) == []

    res = runner.invoke(cli.app, ["-W", "work", "jobs", "--json"])
    assert [j["title"] for j in json.loads(res.stdout)] == ["рабочая"]


def test_unknown_workspace_flag_fails_clearly(monkeypatch, tmp_path):
    monkeypatch.delenv("JWU_WORKSPACE", raising=False)
    _patch_open_store(monkeypatch, tmp_path)
    res = runner.invoke(cli.app, ["-W", "nope", "jobs", "--json"])
    assert res.exit_code == 1
    assert "не найден" in res.output


def test_workspace_add_and_remove_path(monkeypatch, tmp_path):
    monkeypatch.delenv("JWU_WORKSPACE", raising=False)
    _patch_open_store(monkeypatch, tmp_path)
    folder = tmp_path / "repo"
    folder.mkdir()

    assert runner.invoke(cli.app, ["workspace", "add-path", str(folder)]).exit_code == 0
    res = runner.invoke(cli.app, ["workspace", "show", "--json"])
    assert [p["path"] for p in json.loads(res.stdout)["paths"]] == [str(folder.resolve())]

    assert runner.invoke(cli.app, ["workspace", "remove-path", str(folder)]).exit_code == 0
    res = runner.invoke(cli.app, ["workspace", "show", "--json"])
    assert json.loads(res.stdout)["paths"] == []


def test_jira_commands_refuse_in_workspace_without_jira(monkeypatch, tmp_path):
    """В воркспейсе без Jira команды не падают трейсбеком, а объясняют, что делать."""
    monkeypatch.delenv("JWU_WORKSPACE", raising=False)
    db = _patch_open_store(monkeypatch, tmp_path)

    from jwu.core.service import Service

    monkeypatch.setattr(
        cli, "_service",
        lambda: Service.for_workspace(
            cli._resolve_workspace(Store(db)), _cfg_stub(), db_path=str(db)
        ),
    )
    assert runner.invoke(cli.app, ["workspace", "create", "home"]).exit_code == 0

    res = runner.invoke(cli.app, ["-W", "home", "tasks"])
    assert res.exit_code == 1
    assert "нет провайдера задач" in res.output
    assert "jwu feature list" in res.output

    res = runner.invoke(cli.app, ["-W", "home", "prs"])
    assert res.exit_code == 1
    assert "не подключён хостинг репозиториев" in res.output

    res = runner.invoke(cli.app, ["-W", "home", "sync"])
    assert res.exit_code == 1
    assert "синкать нечего" in res.output


def _cfg_stub():
    """Конфиг с дефолтами: воркспейсу без интеграций креды не нужны вовсе."""
    from jwu.core.config import Config

    return Config()


# --------------------------------------------------------------------------- #
# jwu init — подключение проекта
# --------------------------------------------------------------------------- #


def _repo(path):
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir()
    (path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return path


def test_init_creates_workspace_with_repo_tags(monkeypatch, tmp_path):
    monkeypatch.delenv("JWU_WORKSPACE", raising=False)
    db = _patch_open_store(monkeypatch, tmp_path)
    project = _repo(tmp_path / "DnDex")

    res = runner.invoke(cli.app, ["init", str(project), "--yes", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    assert payload["slug"] == "dndex"                      # slug выведен из имени папки
    assert [p["path"] for p in payload["paths"]] == [str(project.resolve())]
    assert payload["paths"][0]["tags"] == ["dndex"]        # тег из имени репозитория
    assert payload["jira_enabled"] is False                # интеграции не навязываем

    store = Store(db)
    assert {w.slug for w in store.list_workspaces()} == {"work", "dndex"}
    store.close()


def test_init_is_idempotent_and_never_duplicates(monkeypatch, tmp_path):
    """Повторный init на привязанной папке НЕ создаёт второй воркспейс."""
    monkeypatch.delenv("JWU_WORKSPACE", raising=False)
    db = _patch_open_store(monkeypatch, tmp_path)
    project = _repo(tmp_path / "DnDex")
    runner.invoke(cli.app, ["init", str(project), "--yes"])

    res = runner.invoke(cli.app, ["init", str(project), "--yes", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    assert payload["already_initialized"] is True
    assert payload["slug"] == "dndex"

    store = Store(db)
    assert len([w for w in store.list_workspaces()]) == 2   # work + dndex, третьего нет
    store.close()


def test_init_recognizes_nested_folder_as_initialized(monkeypatch, tmp_path):
    """Вложенная папка проекта тоже считается подключённой — резолв идёт по дереву."""
    monkeypatch.delenv("JWU_WORKSPACE", raising=False)
    db = _patch_open_store(monkeypatch, tmp_path)
    project = _repo(tmp_path / "DnDex")
    nested = project / "src" / "core"
    nested.mkdir(parents=True)
    runner.invoke(cli.app, ["init", str(project), "--yes"])

    res = runner.invoke(cli.app, ["init", str(nested), "--yes", "--json"])
    assert json.loads(res.stdout)["already_initialized"] is True
    store = Store(db)
    assert len(store.list_workspaces()) == 2
    store.close()


def test_init_json_without_yes_asks_for_confirmation(monkeypatch, tmp_path):
    """Агенту сначала отдаём предложение и список существующих контуров — без записи."""
    monkeypatch.delenv("JWU_WORKSPACE", raising=False)
    db = _patch_open_store(monkeypatch, tmp_path)
    project = _repo(tmp_path / "DnDex")

    res = runner.invoke(cli.app, ["init", str(project), "--json"])
    payload = json.loads(res.stdout)
    assert payload["reason"] == "confirm_required"
    assert payload["suggested"]["slug"] == "dndex"
    assert [w["slug"] for w in payload["existing_workspaces"]] == ["work"]

    store = Store(db)
    assert len(store.list_workspaces()) == 1     # ничего не создано
    store.close()


def test_init_attach_binds_to_existing_workspace(monkeypatch, tmp_path):
    monkeypatch.delenv("JWU_WORKSPACE", raising=False)
    db = _patch_open_store(monkeypatch, tmp_path)
    project = _repo(tmp_path / "DnDex")

    res = runner.invoke(cli.app, ["init", str(project), "--attach", "work", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    assert payload["slug"] == "work"
    assert [p["path"] for p in payload["paths"]] == [str(project.resolve())]

    store = Store(db)
    assert len(store.list_workspaces()) == 1     # новый контур не появился
    store.close()


def test_init_suggests_inner_repos_for_container_folder(monkeypatch, tmp_path):
    """Папка-контейнер: привязываем найденные внутри репозитории, у каждого свой тег."""
    monkeypatch.delenv("JWU_WORKSPACE", raising=False)
    _patch_open_store(monkeypatch, tmp_path)
    container = tmp_path / "dev"
    _repo(container / "backend")
    _repo(container / "frontend")

    res = runner.invoke(cli.app, ["init", str(container), "--yes", "--json"])
    payload = json.loads(res.stdout)
    paths = {p["path"]: p["tags"] for p in payload["paths"]}
    assert paths == {
        str((container / "backend").resolve()): ["backend"],
        str((container / "frontend").resolve()): ["frontend"],
    }


def test_dashboard_opens_last_workspace_and_pins_it(monkeypatch, tmp_path):
    """`jwu dashboard` открывается на последнем выбранном контуре, а не на контуре папки.

    И закрепляет его в `_WORKSPACE_ARG`: иначе TUI показывал бы один воркспейс,
    а колбэки (синк, работы, фичи) молча ходили бы в другой — тот, что дала папка.
    """
    from jwu.core import workspaces

    db = tmp_path / "state.db"
    store = Store(db)
    home = workspaces.create(store, "home")
    workspaces.create(store, "work2")
    folder = tmp_path / "repo"
    folder.mkdir()
    workspaces.add_path(store, home, folder)
    workspaces.set_active(store, store.get_workspace_by_slug("work2"))
    store.close()

    monkeypatch.setattr(cli, "_open_store", lambda: Store(db))
    monkeypatch.setattr(cli, "_store", lambda: Store(db))
    monkeypatch.setattr(cli, "_WORKSPACE_ARG", None)
    monkeypatch.chdir(folder)

    started: dict = {}

    class _FakeApp:
        def __init__(self, data, **kwargs):
            started["data"] = data

        def run(self):
            started["ran"] = True

    import jwu.cli.dashboard as dash
    monkeypatch.setattr(dash, "JwuDashboard", _FakeApp)

    res = runner.invoke(cli.app, ["dashboard"])
    assert res.exit_code == 0, res.output
    assert started["ran"] is True
    assert started["data"].workspace.slug == "work2"
    assert cli._WORKSPACE_ARG == "work2"


def test_dashboard_json_still_resolves_by_folder(monkeypatch, tmp_path):
    """`--json` — это выдача для агентов: они работают в папке проекта, её и слушаем."""
    from jwu.core import workspaces

    db = tmp_path / "state.db"
    store = Store(db)
    home = workspaces.create(store, "home")
    workspaces.create(store, "work2")
    folder = tmp_path / "repo"
    folder.mkdir()
    workspaces.add_path(store, home, folder)
    workspaces.set_active(store, store.get_workspace_by_slug("work2"))
    store.close()

    # реальный _store() — он и делает резолв воркспейса, ради которого тест и написан
    monkeypatch.setattr(cli, "_open_store", lambda: Store(db))
    monkeypatch.setattr(cli, "_WORKSPACE_ARG", None)
    monkeypatch.chdir(folder)

    res = runner.invoke(cli.app, ["dashboard", "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["workspace"]["slug"] == "home"


def test_db_prune_is_dry_by_default(monkeypatch, tmp_path):
    """`jwu db prune` без --apply ничего не удаляет — операция необратима."""
    from datetime import datetime, timedelta, timezone

    from jwu.core.models import Issue

    db = tmp_path / "state.db"
    store = Store(db)
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    for _ in range(5):
        run = store.start_sync_run(["mine"])
        store.save_issue_snapshot(run, Issue(key="PROJ-1", summary="s"), ["mine"])
        store.finish_sync_run(run, {"tasks:mine": 1})
        store.conn.execute("UPDATE sync_runs SET started_at = ? WHERE id = ?", (old, run))
        store.conn.execute(
            "UPDATE issue_snapshots SET fetched_at = ? WHERE sync_run_id = ?", (old, run))
    store.conn.commit()
    store.close()

    monkeypatch.setattr(cli, "_open_store", lambda: Store(db))

    res = runner.invoke(cli.app, ["db", "prune", "--json"])
    assert res.exit_code == 0, res.output
    dry = json.loads(res.stdout)
    assert dry["dry_run"] is True and dry["workspaces"]["work"]["issue_snapshots"] > 0
    with Store(db) as check:
        assert check.conn.execute(
            "SELECT COUNT(*) FROM issue_snapshots").fetchone()[0] == 5

    res = runner.invoke(cli.app, ["db", "prune", "--apply", "--json"])
    assert res.exit_code == 0, res.output
    applied = json.loads(res.stdout)
    assert applied["dry_run"] is False
    assert applied["workspaces"]["work"]["issue_snapshots"] == \
        dry["workspaces"]["work"]["issue_snapshots"]
    with Store(db) as check:
        assert check.conn.execute(
            "SELECT COUNT(*) FROM issue_snapshots").fetchone()[0] == 1


def test_db_stats_reports_size_and_counts(monkeypatch, tmp_path):
    from jwu.core.models import Issue

    db = tmp_path / "state.db"
    store = Store(db)
    run = store.start_sync_run(["mine"])
    store.save_issue_snapshot(run, Issue(key="PROJ-1", summary="s"), ["mine"])
    store.close()

    monkeypatch.setattr(cli, "_open_store", lambda: Store(db))
    res = runner.invoke(cli.app, ["db", "stats", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    assert payload["size"] > 0
    assert payload["workspaces"]["work"]["issue_snapshots"] == 1


def test_rule_add_reads_multiline_from_stdin(monkeypatch, tmp_path):
    """Инструкцию по стенду в аргумент не засунуть — она приходит через --file -."""
    _patch_store(monkeypatch, tmp_path)

    res = runner.invoke(
        cli.app,
        ["rule", "add", "Как поднять стенд", "--kind", "howto",
         "--tag", "legacy-бэкенд", "--file", "-", "--json"],
        input="1. docker compose up\n2. ./manage.py migrate\n",
    )
    assert res.exit_code == 0, res.output
    rule = json.loads(res.stdout)
    assert rule["kind"] == "howto" and rule["tag"] == "legacy-бэкенд"
    assert rule["text"].splitlines() == ["1. docker compose up", "2. ./manage.py migrate"]

    res = runner.invoke(cli.app, ["rule", "show", str(rule["id"])])
    assert "docker compose up" in res.output


def test_rules_list_filters_and_alias(monkeypatch, tmp_path):
    _patch_store(monkeypatch, tmp_path)
    runner.invoke(cli.app, ["rule", "add", "Не пушить в develop", "--kind", "constraint"])
    runner.invoke(cli.app, ["rule", "add", "pnpm, не npm", "--kind", "gotcha",
                            "--tag", "фронт"])

    every = json.loads(runner.invoke(cli.app, ["rules", "--json"]).stdout)
    assert len(every) == 2

    # общие действуют везде, поэтому выдача по тегу включает и их
    scoped = json.loads(runner.invoke(cli.app, ["rules", "--tag", "фронт", "--json"]).stdout)
    assert {r["title"] for r in scoped} == {"Не пушить в develop", "pnpm, не npm"}

    bans = json.loads(runner.invoke(cli.app, ["rule", "list", "--kind", "constraint",
                                              "--json"]).stdout)
    assert [r["title"] for r in bans] == ["Не пушить в develop"]

    bad = runner.invoke(cli.app, ["rules", "--kind", "nope"])
    assert bad.exit_code != 0


def test_rule_edit_and_rm(monkeypatch, tmp_path):
    _patch_store(monkeypatch, tmp_path)
    created = json.loads(runner.invoke(
        cli.app, ["rule", "add", "Ревью до коммита", "--json"]).stdout)

    assert runner.invoke(cli.app, ["rule", "edit", str(created["id"]),
                                   "--kind", "constraint"]).exit_code == 0
    shown = json.loads(runner.invoke(
        cli.app, ["rule", "show", str(created["id"]), "--json"]).stdout)
    assert shown["kind"] == "constraint" and shown["title"] == "Ревью до коммита"

    assert runner.invoke(cli.app, ["rule", "rm", str(created["id"]), "-y"]).exit_code == 0
    assert runner.invoke(cli.app, ["rule", "show", str(created["id"])]).exit_code == 1


def test_workspace_current_json_carries_rules(monkeypatch, tmp_path):
    """Bash-фолбэк должен давать тот же контекст, что MCP: скиллы ссылаются на оба."""
    from jwu.core import workspaces

    db = tmp_path / "state.db"
    store = Store(db)
    ws = workspaces.create(store, "home")
    folder = tmp_path / "repo"
    folder.mkdir()
    workspaces.add_path(store, ws, folder, tags=["фронт"])
    store.use_workspace(ws.id)
    store.add_rule("Не пушить в develop", text="никогда", kind="constraint")
    store.add_rule("Сборка через pnpm", text="длинная инструкция",
                   kind="convention", tag="фронт")
    store.close()

    monkeypatch.setattr(cli, "_open_store", lambda: Store(db))
    monkeypatch.setattr(cli, "_WORKSPACE_ARG", None)
    monkeypatch.chdir(folder)

    payload = json.loads(runner.invoke(
        cli.app, ["workspace", "current", "--json"]).stdout)
    md = payload["rules_md"]
    assert "⛔ ЗАПРЕТ — Не пушить в develop" in md and "никогда" in md
    assert "[#фронт]" in md and "длинная инструкция" not in md
    assert [p["tags"] for p in payload["paths"]] == [["фронт"]]


def test_configure_keeps_settings_it_was_not_asked_to_change(monkeypatch, tmp_path):
    """Запуск с одним флагом не должен обнулять остальной конфиг контура.

    Раньше configure брал за основу глобальный config.toml, а потом писал его поверх
    настроек воркспейса: `--github-token` в одиночку стирал owner и репозитории,
    и поиск задач молча уходил искать по всему GitHub.
    """
    db, _, _ = _configure_env(monkeypatch, tmp_path)
    runner.invoke(cli.app, ["workspace", "create", "dndeck"])
    assert runner.invoke(cli.app, [
        "-W", "dndeck", "configure", "--non-interactive",
        "--github-owner", "DnDeck", "--github-repos", "dndeck",
        "--github-token", "T1",
    ]).exit_code == 0

    # второй запуск — только новый токен, про owner/репозитории речи нет
    assert runner.invoke(cli.app, [
        "-W", "dndeck", "configure", "--non-interactive", "--github-token", "T2",
    ]).exit_code == 0

    store = Store(db)
    ws = store.get_workspace_by_slug("dndeck")
    settings = store.workspace_settings(ws.id)
    assert settings["github.owner"] == "DnDeck"
    assert settings["github.repos"] == "dndeck"
    assert store.workspace_secrets(ws.id)["github.token"] == "T2"
    store.close()


class _CreateSvc:
    """Сервис-заглушка для `jwu issue create`: помнит, дошло ли дело до записи."""

    def __init__(self, problems=None, error=None):
        self.tasks_client = object()
        self.pr_client = None
        self.jira = self.tasks_client
        self.workspace = None
        self.problems = problems or []
        self.similar = []
        self.error = error
        self.created: list[dict] = []
        self.links: list[tuple] = []

    def __enter__(self): return self
    def __exit__(self, *exc): pass
    def close(self): pass

    def create_issue(self, project, summary, *, dry_run=False, from_job=None,
                     check_duplicates=True, **kw):
        from jwu.core.jira import build_create_fields
        if from_job is not None:
            kw["description"] = f"черновик из работы #{from_job}"
        fields = build_create_fields(project, summary, **kw)
        check = {"ok": not self.problems, "problems": list(self.problems),
                 "project": project, "issue_types": ["Task"], "required": ["Summary"]}
        if dry_run or self.problems:
            return {"ok": False, "dry_run": dry_run, "fields": fields, "check": check,
                    "similar": list(self.similar) if check_duplicates else [], "issue": {}}
        if self.error is not None:
            raise self.error
        self.created.append(fields)
        return {"ok": True, "dry_run": False, "fields": fields, "check": check,
                "similar": [], "issue": {"key": "PROJ-777", "id": "1",
                          "url": "https://jira.test/browse/PROJ-777"}}

    def link_types(self, key=""): return ["Relates", "Blocks"]

    def link_issues(self, inward, outward, link_type):
        self.links.append((inward, outward, link_type))
        return {"type": link_type, "inward": inward, "outward": outward}


def _patch_create_svc(monkeypatch, svc):
    monkeypatch.setattr(cli, "_service_with_jira", lambda: svc)
    return svc


def test_issue_create_json_requires_confirmation(monkeypatch):
    """--json без --yes показывает payload и НЕ создаёт: подтверждение — отдельный шаг."""
    svc = _patch_create_svc(monkeypatch, _CreateSvc())
    res = runner.invoke(cli.app, ["issue", "create", "-p", "PROJ", "-s", "Падает экспорт",
                                  "--json"])
    assert res.exit_code == 0
    payload = json.loads(res.stdout)
    assert payload["reason"] == "confirm_required"
    assert payload["fields"]["summary"] == "Падает экспорт"
    assert svc.created == []


def test_issue_create_with_yes_creates_and_returns_key(monkeypatch, tmp_path):
    svc = _patch_create_svc(monkeypatch, _CreateSvc())
    body = tmp_path / "task.md"
    body.write_text("Шаги:\n# открыть\n# нажать", encoding="utf-8")
    res = runner.invoke(cli.app, ["issue", "create", "-p", "PROJ", "-s", "Падает экспорт",
                                  "-F", str(body), "--type", "Bug", "--label", "duty",
                                  "--yes", "--json"])
    assert res.exit_code == 0
    payload = json.loads(res.stdout)
    assert payload == {"ok": True, "key": "PROJ-777",
                       "url": "https://jira.test/browse/PROJ-777",
                       "fields": payload["fields"]}
    assert svc.created[0]["description"].startswith("Шаги:")   # описание взято из файла
    assert svc.created[0]["labels"] == ["duty"]


def test_issue_create_reads_description_from_stdin(monkeypatch):
    svc = _patch_create_svc(monkeypatch, _CreateSvc())
    res = runner.invoke(cli.app, ["issue", "create", "-p", "PROJ", "-s", "тема",
                                  "-F", "-", "--yes", "--json"], input="из stdin\nвторая строка")
    assert res.exit_code == 0
    assert svc.created[0]["description"] == "из stdin\nвторая строка"


def test_issue_create_dry_run_never_creates(monkeypatch):
    svc = _patch_create_svc(monkeypatch, _CreateSvc())
    res = runner.invoke(cli.app, ["issue", "create", "-p", "PROJ", "-s", "тема",
                                  "--dry-run", "--yes", "--json"])
    assert res.exit_code == 0
    payload = json.loads(res.stdout)
    assert payload["dry_run"] is True and svc.created == []


def test_issue_create_refuses_when_required_field_missing(monkeypatch):
    svc = _patch_create_svc(monkeypatch, _CreateSvc(
        problems=["Не заполнено обязательное поле «Отдел» (customfield_10500)"]))
    res = runner.invoke(cli.app, ["issue", "create", "-p", "PROJ", "-s", "тема",
                                  "--yes", "--json"])
    assert res.exit_code == 1
    payload = json.loads(res.stdout)
    assert payload["reason"] == "invalid_fields"
    assert "Отдел" in payload["check"]["problems"][0]
    assert svc.created == []


def test_issue_create_reports_jira_error(monkeypatch):
    from jwu.core.jira import JiraError

    _patch_create_svc(monkeypatch, _CreateSvc(
        error=JiraError("400: summary: Summary слишком длинный", 400)))
    res = runner.invoke(cli.app, ["issue", "create", "-p", "PROJ", "-s", "тема",
                                  "--yes", "--json"])
    assert res.exit_code == 1
    assert "Summary слишком длинный" in json.loads(res.stdout)["error"]


def test_issue_create_rejects_two_description_sources(monkeypatch):
    svc = _patch_create_svc(monkeypatch, _CreateSvc())
    res = runner.invoke(cli.app, ["issue", "create", "-p", "PROJ", "-s", "тема",
                                  "-d", "строкой", "-F", "-", "--yes"])
    assert res.exit_code == 1
    assert svc.created == []


def test_issue_link_requires_confirmation(monkeypatch):
    svc = _patch_create_svc(monkeypatch, _CreateSvc())
    res = runner.invoke(cli.app, ["issue", "link", "PROJ-2", "PROJ-1", "--json"])
    assert json.loads(res.stdout)["reason"] == "confirm_required"
    assert svc.links == []

    res = runner.invoke(cli.app, ["issue", "link", "PROJ-2", "PROJ-1", "-t", "Blocks",
                                  "--yes", "--json"])
    assert res.exit_code == 0
    assert svc.links == [("PROJ-2", "PROJ-1", "Blocks")]


class _CommentSvc:
    """Заглушка для `jwu comment`: помнит, что и куда отправлено."""

    def __init__(self, sdesk_keys=("SDESK",)):
        self.tasks_client = object()
        self.pr_client = None
        self.workspace = None
        self.sdesk_keys = sdesk_keys
        self.sent: list[tuple] = []

    def __enter__(self): return self
    def __exit__(self, *exc): pass
    def close(self): pass

    def key_is_client_facing(self, key):
        return key.split("-", 1)[0] in self.sdesk_keys

    def add_comment(self, key, text, *, client_facing=False):
        if self.key_is_client_facing(key) and not client_facing:
            raise ValueError("КЛИЕНТ")
        self.sent.append((key, text, client_facing))
        return {"id": "11"}


def test_comment_json_requires_confirmation(monkeypatch):
    svc = _CommentSvc()
    monkeypatch.setattr(cli, "_service_with_jira", lambda: svc)
    res = runner.invoke(cli.app, ["comment", "PROJ-1", "-m", "перенёс фикс", "--json"])
    assert res.exit_code == 0
    payload = json.loads(res.stdout)
    assert payload["reason"] == "confirm_required" and payload["client_facing"] is False
    assert svc.sent == []

    res = runner.invoke(cli.app, ["comment", "PROJ-1", "-m", "перенёс фикс", "--yes", "--json"])
    assert res.exit_code == 0
    assert svc.sent == [("PROJ-1", "перенёс фикс", False)]


def test_comment_to_sdesk_requires_to_client_flag(monkeypatch, tmp_path):
    """Клиентский инстанс: без --to-client команда обязана отказать, ничего не отправив."""
    svc = _CommentSvc()
    monkeypatch.setattr(cli, "_service_with_jira", lambda: svc)
    body = tmp_path / "answer.md"
    body.write_text("Ответ клиенту", encoding="utf-8")

    res = runner.invoke(cli.app, ["comment", "SDESK-9", "-F", str(body), "--yes", "--json"])
    assert res.exit_code == 1
    assert json.loads(res.stdout)["reason"] == "client_facing_confirmation_required"
    assert svc.sent == []

    res = runner.invoke(cli.app, ["comment", "SDESK-9", "-F", str(body),
                                  "--to-client", "--yes", "--json"])
    assert res.exit_code == 0
    assert svc.sent == [("SDESK-9", "Ответ клиенту", True)]


def test_comment_dry_run_sends_nothing(monkeypatch):
    svc = _CommentSvc()
    monkeypatch.setattr(cli, "_service_with_jira", lambda: svc)
    res = runner.invoke(cli.app, ["comment", "PROJ-1", "-m", "текст", "--dry-run",
                                  "--yes", "--json"])
    assert res.exit_code == 0
    assert json.loads(res.stdout)["dry_run"] is True
    assert svc.sent == []


def test_comment_refuses_empty_text(monkeypatch):
    svc = _CommentSvc()
    monkeypatch.setattr(cli, "_service_with_jira", lambda: svc)
    res = runner.invoke(cli.app, ["comment", "PROJ-1", "-m", "   ", "--yes"])
    assert res.exit_code == 1
    assert svc.sent == []


def test_issue_create_from_job_builds_description(monkeypatch):
    """--from-job собирает описание из лога работы; со своим текстом не сочетается."""
    svc = _patch_create_svc(monkeypatch, _CreateSvc())
    res = runner.invoke(cli.app, ["issue", "create", "-p", "PROJ", "-s", "тема",
                                  "--from-job", "7", "--yes", "--json"])
    assert res.exit_code == 0
    assert svc.created[0]["description"] == "черновик из работы #7"

    res = runner.invoke(cli.app, ["issue", "create", "-p", "PROJ", "-s", "тема",
                                  "--from-job", "7", "-d", "свой текст", "--yes", "--json"])
    assert res.exit_code == 1
    assert len(svc.created) == 1


class _TransitionSvc:
    def __init__(self):
        self.tasks_client = object()
        self.pr_client = None
        self.workspace = None
        self.done: list[tuple] = []
        self.attached: list[tuple] = []

    def __enter__(self): return self
    def __exit__(self, *exc): pass
    def close(self): pass

    def transitions(self, key):
        return [{"id": "31", "name": "In Progress", "to": "In Progress"}]

    def transition(self, key, target):
        if target.lower() != "in progress":
            raise ValueError("Перехода нет. Доступные: In Progress")
        self.done.append((key, target))
        return {"key": key, "transition": "In Progress", "status": "In Progress"}

    def add_attachment(self, key, paths):
        self.attached.append((key, tuple(paths)))
        return [{"id": "9", "filename": "a.log", "size": 3, "path": paths[0]}]


def test_issue_transition_requires_confirmation_and_lists_available(monkeypatch):
    svc = _TransitionSvc()
    monkeypatch.setattr(cli, "_service_with_jira", lambda: svc)

    res = runner.invoke(cli.app, ["issue", "transition", "PROJ-1", "In Progress", "--json"])
    assert json.loads(res.stdout)["reason"] == "confirm_required"
    assert svc.done == []

    res = runner.invoke(cli.app, ["issue", "transition", "PROJ-1", "In Progress",
                                  "--yes", "--json"])
    assert res.exit_code == 0 and svc.done == [("PROJ-1", "In Progress")]

    res = runner.invoke(cli.app, ["issue", "transition", "PROJ-1", "Closed", "--yes", "--json"])
    assert res.exit_code == 1
    assert "Доступные: In Progress" in json.loads(res.stdout)["error"]


def test_issue_attach_requires_confirmation(monkeypatch, tmp_path):
    svc = _TransitionSvc()
    monkeypatch.setattr(cli, "_service_with_jira", lambda: svc)
    src = tmp_path / "a.log"
    src.write_text("abc", encoding="utf-8")

    res = runner.invoke(cli.app, ["issue", "attach", "PROJ-1", str(src), "--json"])
    assert json.loads(res.stdout)["reason"] == "confirm_required"
    assert svc.attached == []

    res = runner.invoke(cli.app, ["issue", "attach", "PROJ-1", str(src), "--yes", "--json"])
    assert res.exit_code == 0
    assert svc.attached == [("PROJ-1", (str(src),))]


def test_issue_create_preview_shows_similar(monkeypatch):
    svc = _patch_create_svc(monkeypatch, _CreateSvc())
    svc.similar = [{"key": "PROJ-9", "summary": "то же самое", "status": "Closed",
                    "resolution": "Done", "created": "2025-09-01"}]
    res = runner.invoke(cli.app, ["issue", "create", "-p", "PROJ", "-s", "то же самое",
                                  "--dry-run", "--json"])
    assert res.exit_code == 0
    assert json.loads(res.stdout)["similar"][0]["key"] == "PROJ-9"
