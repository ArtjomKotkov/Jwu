"""Планы: шаги, зависимости, ожидания, доказательства, связь с работами, сводки."""

import subprocess

import pytest

from jwu.core import plans
from jwu.core.plans import PlanError
from jwu.core.store import Store


@pytest.fixture
def store(tmp_path):
    st = Store(tmp_path / "s.db")
    st.use_workspace(st.get_workspace_by_slug("work").id)
    yield st
    st.close()


@pytest.fixture
def plan(store):
    p = plans.create_plan(store, "Граф стола", "вся механика — графом", rules=["агенты на opus"])
    plans.add_step(store, p.key, "Сервер книги", key="4а")
    plans.add_step(store, p.key, "Фронт книги", key="4б", after=["4а"])
    plans.add_step(store, p.key, "Перевод пака", key="5", after=["4б"], waits=[{"kind": "question", "text": "записью или настройкой?"}])
    return p


def _ok_evidence(tmp_path):
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"x")
    return [{"kind": "screenshot", "text": "экран книги", "ref": str(shot)}]


def test_keys_order_and_cycle(store, plan):
    st = plans.add_step(store, plan.key, "без ключа")
    assert st.key == "6"                                   # после числового «5»
    with pytest.raises(PlanError, match="круг"):
        plans.update_step(store, f"{plan.key}/4а", add_after=["5"])
    with pytest.raises(PlanError, match="уже есть"):
        plans.add_step(store, plan.key, "дубль", key="4а")


def test_dependencies_block_and_free(store, plan, tmp_path):
    assert plans.effective(store, plans.get_step(store, "PLAN-1/4б")[1]) == "blocked"
    with pytest.raises(PlanError, match="ещё ждёт"):
        plans.update_step(store, "PLAN-1/4б", status="doing")
    plans.update_step(store, "PLAN-1/4а", status="doing")
    res = plans.update_step(store, "PLAN-1/4а", status="done", evidence=_ok_evidence(tmp_path))
    assert res.freed == ["4б"]
    plans.update_step(store, "PLAN-1/4б", status="doing")


def test_done_needs_evidence_refs_and_no_leftovers(store, plan, tmp_path):
    ref = "PLAN-1/4а"
    with pytest.raises(PlanError, match="только с доказательством"):
        plans.update_step(store, ref, status="done")
    with pytest.raises(PlanError, match="нужна ref"):
        plans.update_step(store, ref, evidence=[{"kind": "tests", "text": "ok"}])
    with pytest.raises(PlanError, match="файла .* нет"):
        plans.update_step(store, ref, evidence=[{"kind": "screenshot", "text": "x", "ref": "/nope/x.png"}])
    plans.update_step(store, ref, leftovers=["не сделал экспорт"], evidence=_ok_evidence(tmp_path))
    with pytest.raises(PlanError, match="остаток не разобран"):
        plans.update_step(store, ref, status="done")
    plans.update_step(store, ref, leftovers=[], status="done")


def test_needs_aliases(store, tmp_path):
    p = plans.create_plan(store, "П", needs=["code"])
    plans.add_step(store, p.key, "шаг", key="1", needs=["ui"])
    with pytest.raises(PlanError, match="code"):
        plans.update_step(store, f"{p.key}/1", status="done", evidence=_ok_evidence(tmp_path))
    job = store.create_job("", "работа")
    with pytest.raises(PlanError, match="test-pass"):
        plans.update_step(store, f"{p.key}/1", evidence=[{"kind": "tests", "text": "ok", "ref": f"job:{job.id}"}])
    store.add_job_record(job.id, "pytest 10 passed", kind="test-pass")
    with pytest.raises(PlanError, match="ui"):
        plans.update_step(store, f"{p.key}/1", status="done",
                          evidence=[{"kind": "tests", "text": "ok", "ref": f"job:{job.id}"}])
    assert plans.get_step(store, f"{p.key}/1")[1].evidence == []    # из отказанного вызова не сохранилось
    plans.update_step(store, f"{p.key}/1", status="done",
                      evidence=[{"kind": "tests", "text": "ok", "ref": f"job:{job.id}"}, *_ok_evidence(tmp_path)])


def test_commit_evidence_checked_in_workspace_repos(store, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "a").write_text("1")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"], check=True)
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    store.add_workspace_path(store.workspace_id, str(repo))
    p = plans.create_plan(store, "П")
    plans.add_step(store, p.key, "шаг", key="1")
    plans.update_step(store, f"{p.key}/1", evidence=[{"kind": "build", "text": "собрано", "ref": f"commit:{sha[:10]}"}])
    with pytest.raises(PlanError, match="коммита"):
        plans.update_step(store, f"{p.key}/1", evidence=[{"kind": "build", "text": "x", "ref": "deadbeef1"}])


def test_waits_owner_and_resolve(store, plan, tmp_path):
    st = plans.get_step(store, "PLAN-1/5")[1]
    assert plans.effective(store, st) == "waiting_owner"
    now = plans.now(store)
    assert now["plans"][0]["owner"][0]["step"] == "5"
    plans.update_step(store, "PLAN-1/5", resolve_waits=[0])
    assert plans.effective(store, plans.get_step(store, "PLAN-1/5")[1]) == "blocked"   # ещё ждёт 4б


def test_rollback_of_done_needs_force_and_dropped_hangs(store, plan, tmp_path):
    plans.update_step(store, "PLAN-1/4а", status="done", evidence=_ok_evidence(tmp_path))
    plans.update_step(store, "PLAN-1/4б", status="doing")
    with pytest.raises(PlanError, match="уже строят"):
        plans.update_step(store, "PLAN-1/4а", status="doing")
    plans.update_step(store, "PLAN-1/4а", status="doing", force=True)
    res = plans.update_step(store, "PLAN-1/4б", status="dropped")
    assert res.hanging == ["5"]
    assert "отменённый шаг" in plans.blockers(store, plans.get_step(store, "PLAN-1/5")[1])[0]


def test_children_and_cross_plan(store, plan, tmp_path):
    other = plans.create_plan(store, "Перевод пака")
    plans.add_step(store, other.key, "переводить", key="1", after=["PLAN-1/4б"])
    assert "PLAN-1/4б" in plans.blockers(store, plans.get_step(store, "PLAN-2/1")[1])[0]
    plans.add_step(store, plan.key, "подшаг", key="4а1", parent="4а")
    with pytest.raises(PlanError, match="подшаги"):
        plans.update_step(store, "PLAN-1/4а", status="done", evidence=_ok_evidence(tmp_path))


def test_next_focus_files_and_owner(store, plan, tmp_path):
    p2 = plans.create_plan(store, "Второй")
    plans.add_step(store, p2.key, "a", key="1")
    plans.update_step(store, "PLAN-2/1", links={"files": ["x.py"]})
    plans.add_step(store, p2.key, "b", key="2")
    plans.update_step(store, "PLAN-2/2", links={"files": ["x.py"]})
    plans.add_step(store, p2.key, "владелец", key="3", executor="owner")
    got = [x["step"] for x in plans.next_steps(store, slots=10)]
    assert got == ["PLAN-1/4а", "PLAN-2/1"]                 # 2 — те же файлы, 3 — владельцу, 4б/5 ждут
    plans.edit_plan(store, p2.key, focus=True)
    assert [x["step"] for x in plans.next_steps(store, slots=10)] == ["PLAN-2/1"]


def test_jobs_link_and_handoff(store, plan):
    from jwu.core import handoff

    job = store.create_job("", "сервер книги")
    plans.link_job(store, "PLAN-1/4а", job.id)
    plans.update_step(store, "PLAN-1/4а", links={"job_id": job.id})          # повтор — без дубля
    assert plans.get_step(store, "PLAN-1/4а")[1].links["jobs"] == [job.id]
    assert plans.steps_for_job(store, job.id)[0]["step"] == "PLAN-1/4а"
    md = handoff.render(handoff.collect(store, store.get_job(job.id), offline=True))
    assert "Шаг плана PLAN-1/4а" in md and "агенты на opus" in md
    with pytest.raises(PlanError, match="работы #999"):
        plans.update_step(store, "PLAN-1/4а", links={"jobs": [999]})


def test_view_now_and_events(store, plan, tmp_path):
    plans.update_step(store, "PLAN-1/4а", status="doing")
    v = plans.view(store, plan.key)
    assert "## PLAN-1 · Граф стола — 0/3" in v["markdown"] and "🔄 **4а**" in v["markdown"]
    assert any("статус: todo → doing" in e["text"] for e in v["events"])
    md = plans.now(store, plan.key)["markdown"]
    assert "Идёт:** 4а" in md and "Ждёт тебя" in md and "Стоит" in md


def test_mcp_tools(store, monkeypatch):
    import asyncio

    from jwu import mcp_server as srv

    created = asyncio.run(srv.jwu_plan_create(title="MCP-план", workspace="work"))
    asyncio.run(srv.jwu_plan_step_add(plan=created["key"], title="шаг", workspace="work"))
    got = asyncio.run(srv.jwu_plan_step(step=f"{created['key']}/1", workspace="work"))
    assert got["state"] == "todo"
    with pytest.raises(ValueError, match="доказательством"):
        asyncio.run(srv.jwu_plan_step(step=f"{created['key']}/1", status="done", workspace="work"))
    job = asyncio.run(srv.jwu_job_start(title="по шагу", step=f"{created['key']}/1", workspace="work"))
    assert job["plan_step"] == f"{created['key']}/1"
    assert "MCP-план" in asyncio.run(srv.jwu_plan_now(workspace="work"))["markdown"]
