"""Голос пользователя: профиль, корпус, примеры, журнал правок, сбор (JWU-47)."""

import asyncio
import json
import subprocess
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from jwu import mcp_server as srv
from jwu.cli import main as cli
from jwu.core import voice as voice_mod
from jwu.core.models import Comment, Issue, PR, PRComment
from jwu.core.store import Store
from jwu.skills_install import EXPECTED_AGENTS

runner = CliRunner()


def _s(i, channel, text=None, created="2026-09-01T00:00:00+00:00", audience="colleague"):
    return voice_mod.VoiceSample(id=f"x:{i}", channel=channel, audience=audience,
                                 text=text or f"Текст номер {i}, достаточно длинный для примера.",
                                 created=created)


def test_profile_created_from_neutral_template():
    path = voice_mod.ensure_profile("work")
    text = path.read_text()
    assert path == voice_mod.profile_path("work") and voice_mod.FEEDBACK_HEADER in text
    # в шаблоне из пакета нет ничего персонального — только заготовки разделов
    assert "КХ" not in text and "store()" not in text


def test_agent_setting(tmp_path):
    store = Store(tmp_path / "s.db")
    try:
        wid = store.get_workspace_by_slug("work").id
        assert voice_mod.agent_name(store, wid) == "voice-writer-sample"
        assert voice_mod.set_agent(store, wid, "voice-me") == "voice-me"
        assert voice_mod.agent_name(store, wid) == "voice-me"
        voice_mod.set_agent(store, wid, "")
        assert voice_mod.agent_name(store, wid) == "voice-writer-sample"
    finally:
        store.close()


def test_default_agent_is_shipped():
    assert "voice-writer-sample" in EXPECTED_AGENTS


def test_corpus_merge_dedup_and_stats():
    assert voice_mod.merge_corpus("work", [_s(1, "pr_comment"), _s(2, "commit")]) == (2, 2)
    changed = _s(1, "pr_comment", text="Поправленный текст того же коммента, он длиннее.")
    assert voice_mod.merge_corpus("work", [changed, _s(3, "jira_comment")]) == (1, 3)
    stats = voice_mod.corpus_stats("work")
    assert stats["total"] == 3 and stats["by_channel"] == {"pr_comment": 1, "commit": 1, "jira_comment": 1}
    assert {s.text for s in voice_mod.load_corpus("work")} >= {changed.text}


def test_examples_channel_fallback_audience_and_recency():
    voice_mod.merge_corpus("work", [
        _s(1, "jira_comment", created="2026-01-01T00:00:00+00:00"),
        _s(2, "jira_comment", created="2026-09-01T00:00:00+00:00"),
        _s(3, "jira_comment", created="2026-05-01T00:00:00+00:00", audience="client"),
        _s(4, "sdesk_client", created="2026-02-01T00:00:00+00:00", audience="client"),
        _s(5, "commit", text="коротко"),  # слишком короткий — не пример
    ])
    got = [e["id"] for e in voice_mod.examples("work", "sdesk_client", audience="client", limit=3)]
    assert got == ["x:4", "x:3", "x:2"]  # свой канал → соседний, в нём клиент первым, потом свежее
    assert voice_mod.examples("work", "commit") == []


def test_feedback_appends_to_profile():
    path = voice_mod.add_feedback("work", channel="pr_comment", verdict="edited",
                                  before="проверял по сырым строкам?",
                                  after="Ты смотрел сырые строки на каждом узле?", reason="непонятно, кто проверял")
    text = path.read_text()
    tail = text[text.index(voice_mod.FEEDBACK_HEADER):]
    assert "pr_comment · поправлено" in tail and "> проверял по сырым строкам?" in tail
    assert "Стало:\n> Ты смотрел" in tail and "Почему: непонятно" in tail
    with pytest.raises(ValueError):
        voice_mod.add_feedback("work", channel="x", verdict="meh", before="a")


# --- сбор корпуса ------------------------------------------------------------ #

class _PRClient:
    def dashboard_prs(self, view, *, state="OPEN"):
        pr = PR(id=42 if view == "mine" else 7, project="P", repository="r", title="t",
                description="Описание моего PR: что делает и зачем, для ревьюеров." if view == "mine" else "",
                author="Я", created=1_900_000_000_000, updated=4_000_000_000_000)
        old = PR(id=1, project="P", repository="r", title="old", updated=1)
        return [pr, old]

    def pr_comments(self, project, repo, pr_id):
        return [
            PRComment(id="10", author="Коллега", text="Почему здесь без таймаута?", created=1, depth=0),
            PRComment(id="11", author="Я", text="Поправил, таймаут теперь из настроек.", created=2, depth=1),
            PRComment(id="12", author="Я", text="Если узел упадёт, пачка задублируется.", created=3, depth=0),
        ]


class _Tasks:
    def search(self, jql, max_results=50):
        assert "currentUser()" in jql
        return [Issue(key="PROJ-1")]

    def issue(self, key, with_dev=True):
        return Issue(key=key, comments=[
            Comment(id="1", author="Коллега", author_key="colleague", body="Что со стендом?"),
            Comment(id="2", author="Я", author_key="me", body="Стенд обновил, можно проверять."),
        ])


def _fake_svc(store):
    svc = SimpleNamespace(pr_client=_PRClient(), tasks_client=_Tasks(), provider="jira", store=store)
    svc._resolve_username = lambda: "me"
    svc._myself = lambda: {"name": "me", "displayName": "Я"}
    svc._client_for_key = lambda key: svc.tasks_client
    svc._key_is_sdesk = lambda key: key.startswith("SDESK-")
    return svc


def _git_repo(path):
    path.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True, capture_output=True)
    run("init", "-q")
    run("config", "user.email", "me@example.com")
    run("config", "user.name", "Me")
    (path / "f.txt").write_text("x")
    run("add", ".")
    run("commit", "-q", "-m", "PROJ-1: таймаут из настроек\n\nCo-Authored-By: Bot <b@x>")
    return path


def test_collect_prs_jira_and_git(tmp_path):
    store = Store(tmp_path / "s.db")
    store.use_workspace(store.get_workspace_by_slug("work").id)
    try:
        repo = _git_repo(tmp_path / "repo")
        report = voice_mod.collect(_fake_svc(store), "work", [str(repo)], days=30)
    finally:
        store.close()
    corpus = {s.id: s for s in voice_mod.load_corpus("work")}
    assert report.errors == [] and report.total == len(corpus)
    reply = corpus["bitbucket:P/r#42:11"]
    assert reply.channel == "pr_reply" and reply.context.startswith("Почему здесь")
    assert corpus["bitbucket:P/r#42:12"].channel == "pr_comment"
    assert "bitbucket:P/r#42:10" not in corpus  # чужой коммент
    assert corpus["bitbucket:P/r#42:description"].channel == "pr_description"
    assert not any(k.startswith("bitbucket:P/r#1:") for k in corpus)  # старый PR вне окна
    assert corpus["jira:PROJ-1:2"].text == "Стенд обновил, можно проверять."
    commit = next(s for s in corpus.values() if s.channel == "commit")
    assert commit.text == "PROJ-1: таймаут из настроек"  # трейлер соавторства срезан
    assert report.by_source == {"bitbucket": 5, "jira": 1, "git": 1}  # #42: описание + 2, #7: 2


# --- MCP и CLI --------------------------------------------------------------- #

def test_mcp_voice_profile_and_examples(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    monkeypatch.setenv("JWU_DB_PATH", str(db))
    monkeypatch.setattr(srv, "_base_store", None)
    monkeypatch.setattr(srv, "_stores", {})
    Store(db).close()
    voice_mod.merge_corpus("work", [_s(1, "pr_comment")])
    try:
        prof = asyncio.run(srv.jwu_voice_profile(workspace="work"))
        assert prof["agent"] == "voice-writer-sample" and prof["corpus"]["total"] == 1
        assert voice_mod.FEEDBACK_HEADER in prof["profile_md"]
        ex = asyncio.run(srv.jwu_voice_examples("pr_reply", workspace="work"))
        assert [e["id"] for e in ex["examples"]] == ["x:1"]
        fb = asyncio.run(srv.jwu_voice_feedback("pr_comment", "rejected", "натянуто", reason="вне скоупа",
                                                workspace="work"))
        assert "Почему: вне скоупа" in open(fb["profile_path"]).read()
    finally:
        for s in list(srv._stores.values()):
            s.close()
        if srv._base_store is not None:
            srv._base_store.close()


def test_cli_voice_show_agent_examples():
    r = runner.invoke(cli.app, ["-W", "work", "voice", "agent", "voice-me"])
    assert r.exit_code == 0 and "voice-me" in r.output
    r = runner.invoke(cli.app, ["-W", "work", "voice", "show", "--json"])
    data = json.loads(r.output[r.output.index("{"):])
    assert data["agent"] == "voice-me" and data["corpus"]["total"] == 0
    runner.invoke(cli.app, ["-W", "work", "voice", "agent", "-"])
    voice_mod.merge_corpus("work", [_s(1, "commit")])
    r = runner.invoke(cli.app, ["-W", "work", "voice", "examples", "commit"])
    assert "Текст номер 1" in r.output
