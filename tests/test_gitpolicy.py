"""Политика git (core.gitpolicy): уровни, правила воркспейса и репозитория, допуски, TOML, память."""

from datetime import datetime, timedelta, timezone

import pytest

from jwu.core import gitpolicy as gp
from jwu.core.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "s.db")
    s.use_workspace(s.get_workspace_by_slug("work").id)
    yield s
    s.close()


def test_defaults_written_on_first_access(store):
    assert not gp.has_policy(store, store.workspace_id)
    pol = gp.ensure(store)
    assert gp.has_policy(store, store.workspace_id)
    assert pol.ops["commit"] == "allow" and pol.ops["push"] == "ask" and pol.ops["tag"] == "grant"
    assert [r.name for r in pol.rules] == ["прод-ветки", "общие ветки", "основная папка"]


def test_decide_by_branch_and_where(store):
    pol = gp.ensure(store)
    d = gp.decide(pol, "push", remote="proj/app", branch="master", where="job")
    assert d.level == "deny" and "прод-ветки" in d.rule
    assert gp.decide(pol, "push", branch="release-5.2", where="job").level == "grant"
    assert gp.decide(pol, "force_push", branch="develop").level == "deny"
    assert gp.decide(pol, "push", branch="PROJ-1-fix", where="job").level == "ask"
    assert gp.decide(pol, "commit", branch="PROJ-1-fix", where="job").level == "allow"
    assert gp.decide(pol, "commit", branch="PROJ-1-fix", where="main").level == "ask"


def test_repo_rules_win_over_workspace(store):
    pol = gp.ensure(store)
    pol.repos.append(gp.Repo(remote="proj/app", sync="rebase", target="develop", rules=[
        gp.Rule(name="свои", key=["PROJ"], where="job", ops={"push": "allow"}),
        gp.Rule(name="чужие", key=["*"], ops={"push": "deny"}),
    ]))
    pol.repos.append(gp.Repo(remote="proj/legacy", ops={"push": "deny", "commit": "deny"}))
    pol = gp.save(store, pol)
    assert gp.decide(pol, "push", remote="proj/app", branch="PROJ-1-x", where="job").level == "allow"
    assert gp.decide(pol, "push", remote="proj/app", branch="OTHER-2-x", where="job").level == "deny"
    # правил под ветку без ключа в репо нет → правила воркспейса, потом ops воркспейса
    assert gp.decide(pol, "push", remote="proj/app", branch="develop").level == "grant"
    assert gp.decide(pol, "push", remote="proj/app", branch="hotfix").level == "ask"
    # ops репозитория — после правил воркспейса, до ops воркспейса
    d = gp.decide(pol, "commit", remote="proj/legacy", branch="x", where="job")
    assert d.level == "deny" and "proj/legacy" in d.rule
    assert gp.decide(pol, "commit", remote="proj/legacy", branch="master").rule.startswith("воркспейс")
    assert gp.sync_mode(pol, "proj/app") == "rebase" and gp.sync_mode(pol, "proj/legacy") == "merge"
    assert gp.target_branch(pol, "proj/app") == "develop"


def test_legacy_push_protected_becomes_grant_rule(store):
    store.set_workspace_settings(store.workspace_id, {"push.protected": "master,develop,rc-*"})
    pol = gp.ensure(store)
    shared = next(r for r in pol.rules if r.name == "общие ветки")
    assert shared.branch == ["develop", "rc-*"] and shared.ops["push"] == "grant"


def test_validation_errors():
    with pytest.raises(gp.PolicyError, match="уровня"):
        gp.Rule(branch=["x"], ops={"push": "maybe"}).validate()
    with pytest.raises(gp.PolicyError, match="операции"):
        gp.Rule(branch=["x"], ops={"rm": "deny"}).validate()
    with pytest.raises(gp.PolicyError, match="ничего не решает"):
        gp.Rule(branch=["x"]).validate()
    with pytest.raises(gp.PolicyError, match="where"):
        gp.Rule(where="cloud", ops={"push": "deny"}).validate()
    with pytest.raises(gp.PolicyError, match="дважды"):
        gp.Policy(repos=[gp.Repo(remote="a/b"), gp.Repo(remote="a/b")]).validate()


def test_toml_roundtrip_and_errors(store):
    pol = gp.ensure(store)
    pol.repos.append(gp.Repo(remote="proj/app", target="develop", rules=[gp.Rule(branch=["develop"],
                                                                            ops={"push": "grant"})]))
    text = gp.to_toml(pol)
    back = gp.from_toml(text)
    assert gp.to_toml(back) == text
    with pytest.raises(gp.PolicyError, match="Неизвестные поля"):
        gp.from_toml("[git]\nfoo = 1\n")
    with pytest.raises(gp.PolicyError, match="TOML"):
        gp.from_toml("[git\n")
    with pytest.raises(gp.PolicyError, match="Нет секции"):
        gp.from_toml("x = 1\n")


def test_grant_flow_one_shot_and_head_bound(store):
    pol = gp.ensure(store)
    req = gp.request_grant(store, remote="proj/app", branch="develop", op="push", head="abc", detail="3 коммита")
    assert req["new"] and gp.request_grant(store, remote="proj/app", branch="develop", op="push",
                                           head="abc")["new"] is False
    assert gp.consume_grant(store, remote="proj/app", branch="develop", op="push", head="abc") is None
    gp.answer_grant(store, req["id"], allow=True, via="telegram", policy=pol)
    with pytest.raises(gp.PolicyError, match="уже"):
        gp.answer_grant(store, req["id"], allow=True, via="cli", policy=pol)
    assert gp.consume_grant(store, remote="proj/app", branch="develop", op="push", head="other") is None
    assert gp.consume_grant(store, remote="proj/app", branch="develop", op="push", head="abc") is not None
    assert gp.consume_grant(store, remote="proj/app", branch="develop", op="push", head="abc") is None
    decisions = [a["decision"] for a in gp.audit_log(store)]
    assert "grant:granted" in decisions


def test_grant_expires_and_via_restricted(store):
    pol = gp.ensure(store)
    g = gp.grant_direct(store, remote="proj/app", branch="develop", op="push", policy=pol)
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="seconds")
    store.conn.execute("UPDATE git_grants SET expires_at = ? WHERE id = ?", (past, g["id"]))
    assert gp.consume_grant(store, remote="proj/app", branch="develop", op="push") is None
    pol.grant_via = ["telegram"]
    pol = gp.save(store, pol)
    req = gp.request_grant(store, remote="proj/app", branch="develop", op="push")
    with pytest.raises(gp.PolicyError, match="выключен"):
        gp.answer_grant(store, req["id"], allow=True, via="cli", policy=pol)


@pytest.mark.parametrize("url, slug", [
    ("ssh://git@git.example:7999/proj/app.git", "proj/app"),
    ("https://git.example/scm/PROJ/app.git", "proj/app"),
    ("git@github.com:me/dndeck.git", "me/dndeck"),
    ("https://github.com/me/dndeck", "me/dndeck"),
    ("", ""),
])
def test_remote_slug(url, slug):
    assert gp.remote_slug(url) == slug


def test_branch_key():
    assert gp.branch_key("feature/PROJ-12-fix") == "PROJ"
    assert gp.branch_key("develop") == ""


def test_memory_export_import_keeps_local_policy(store, tmp_path):
    from jwu.core import memory

    pol = gp.ensure(store)
    pol.ops["push"] = "deny"
    gp.save(store, pol)
    memory.export_memory(store, tmp_path / "mem")
    assert (tmp_path / "mem" / "work" / "git-policy.toml").exists()
    other = Store(tmp_path / "o.db")
    try:
        rep = memory.import_memory(other, tmp_path / "mem")
        other.use_workspace(other.get_workspace_by_slug("work").id)
        assert rep.added.get("git_policy") == 1 and gp.load(other).ops["push"] == "deny"
        rep = memory.import_memory(other, tmp_path / "mem")
        assert rep.skipped.get("git_policy") == 1
    finally:
        other.close()


def test_bot_buttons_answer_grant(store):
    from jwu.core import gitgrant

    gp.ensure(store)
    req = gp.request_grant(store, remote="proj/app", branch="develop", op="push", head="h", detail="2 коммита")
    mk = gitgrant.markup(req, 600)
    ok, no = [b["callback_data"] for b in mk["inline_keyboard"][0]]
    assert ok == f"git:ok:{req['id']}" and no == f"git:no:{req['id']}"
    assert "Нужен допуск" in gitgrant._text(req) and "2 коммита" in gitgrant._text(req)
    text, _ = gitgrant.handle_callback(store, ok)
    assert text.startswith("✅ Разрешено")
    text, _ = gitgrant.handle_callback(store, no)            # второй раз — уже отвечен
    assert "уже" in text
    req2 = gp.request_grant(store, remote="proj/app", branch="develop", op="push", head="h2")
    assert gitgrant.handle_callback(store, f"git:no:{req2['id']}")[0].startswith("⛔")
    assert gitgrant.handle_callback(store, "git:zzz")[0] == "Кнопка допуска устарела."


def test_cli_policy_rules_and_check(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from jwu.cli import main as cli

    monkeypatch.setenv("JWU_DB_PATH", str(tmp_path / "c.db"))
    run = lambda *a: CliRunner().invoke(cli.app, ["-W", "work", "git", *a])  # noqa: E731
    r = run("policy", "show")
    assert r.exit_code == 0 and "встроенная" in r.output
    assert run("policy", "init", "--no-discover").exit_code == 0
    assert run("repo", "set", "PROJ/App", "--target", "develop", "--sync", "rebase").exit_code == 0
    r = run("rule", "add", "--repo", "proj/app", "--key", "PROJ", "--where", "job", "--op", "push=allow")
    assert r.exit_code == 0, r.output
    r = run("policy", "check", "push", "--repo", "proj/app", "--branch", "PROJ-1-x", "--where", "job")
    assert "allow" in r.output and "proj/app" in r.output
    assert run("rule", "add", "--op", "push=sometimes").exit_code == 2
    assert run("rule", "rm", "9").exit_code == 2
    r = run("policy", "show", "--toml")
    assert 'remote = "proj/app"' in r.output and 'sync = "rebase"' in r.output
    assert run("grant", "1").exit_code == 2                 # не терминал — допуска нет
