"""Локальные фичи (мини-трекер воркспейса) и работы без задачи Jira."""

import json

import pytest
from typer.testing import CliRunner

from jwu.cli import main as cli
from jwu.core.store import Store

runner = CliRunner()


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "state.db")
    yield s
    s.close()


# --------------------------------------------------------------------------- #
# Ключи фич
# --------------------------------------------------------------------------- #


def test_feature_keys_are_sequential_within_workspace(store):
    home = store.create_workspace("home-jwu", name="Личное")
    store.use_workspace(home.id)

    keys = [store.create_feature(f"фича {i}").key for i in range(1, 4)]
    assert keys == ["HOMEJWU-1", "HOMEJWU-2", "HOMEJWU-3"]


def test_feature_numbering_is_independent_between_workspaces(store):
    home = store.create_workspace("home", name="Личное")
    pet = store.create_workspace("pet", name="Pet")

    store.use_workspace(home.id)
    assert store.create_feature("a").key == "HOME-1"
    store.use_workspace(pet.id)
    assert store.create_feature("b").key == "PET-1"
    store.use_workspace(home.id)
    assert store.create_feature("c").key == "HOME-2"


def test_deleted_feature_does_not_reuse_its_number(store):
    home = store.create_workspace("home", name="Личное")
    store.use_workspace(home.id)
    first = store.create_feature("a")
    second = store.create_feature("b")
    store.delete_feature(second.id)
    # нумерация идёт от максимума, а не от количества — ключи не переиспользуются
    assert store.create_feature("c").key == "HOME-3"
    assert first.key == "HOME-1"


def test_feature_prefix_falls_back_for_unusable_slug(store):
    ws = store.create_workspace("2fa", name="2FA")
    store.use_workspace(ws.id)
    key = store.create_feature("a").key
    # ключ обязан читаться как ключ Jira: буква первой, потом буквы/цифры
    assert key == "F2FA-1"

    import re

    assert re.match(r"^[A-Z][A-Z0-9]+-\d+$", key)


def test_feature_prefix_can_be_overridden_by_setting(store):
    ws = store.create_workspace("home-jwu")
    store.use_workspace(ws.id)
    store.set_workspace_settings(ws.id, {"features.prefix": "JWU"})
    assert store.create_feature("a").key == "JWU-1"


# --------------------------------------------------------------------------- #
# Store: фичи и их связь с работами
# --------------------------------------------------------------------------- #


def test_feature_crud_and_lookup_by_key_or_id(store):
    feature = store.create_feature("Тёмная тема", description="d", priority="high")
    assert store.get_feature(feature.key).id == feature.id
    assert store.get_feature(feature.key.lower()).id == feature.id
    assert store.get_feature(feature.id).title == "Тёмная тема"

    store.update_feature(feature.id, status="in_progress", title="Тёмная тема v2")
    assert store.get_feature(feature.id).status == "in_progress"
    assert store.get_feature(feature.id).title == "Тёмная тема v2"
    assert [f.key for f in store.list_features(status="in_progress")] == [feature.key]
    assert store.list_features(status="done") == []


def test_job_anchored_to_feature_carries_its_key(store):
    feature = store.create_feature("Тёмная тема")
    job = store.create_job("", "тема", feature_id=feature.id)
    assert (job.feature_key, job.anchor) == (feature.key, feature.key)

    loaded = store.get_job(job.id)
    assert loaded.feature_key == feature.key
    assert [j.id for j in store.list_jobs(feature_id=feature.id)] == [job.id]


def test_job_without_anchor_falls_back_to_its_id(store):
    job = store.create_job("", "разобрать бэклог")
    assert store.get_job(job.id).anchor == f"#{job.id}"
    assert store.get_job(job.id).feature_id is None


def test_job_with_jira_key_still_wins_as_anchor(store):
    feature = store.create_feature("Тёмная тема")
    job = store.create_job("PROJ-1", "t", feature_id=feature.id)
    assert store.get_job(job.id).anchor == "PROJ-1"


def test_deleting_feature_keeps_jobs_but_drops_anchor(store):
    feature = store.create_feature("Тёмная тема")
    job = store.create_job("", "тема", feature_id=feature.id)
    store.add_job_record(job.id, "фаза", kind="phase")

    store.delete_feature(feature.id)
    loaded = store.get_job(job.id)
    assert loaded is not None
    assert (loaded.feature_id, loaded.feature_key) == (None, "")
    assert len(loaded.records) == 1  # журнал работы не пострадал


def test_features_are_isolated_between_workspaces(store):
    home = store.create_workspace("home")
    store.create_feature("рабочая фича")
    store.use_workspace(home.id)
    assert store.list_features() == []
    assert store.get_feature("WORK-1") is None


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _patch_cli(monkeypatch, tmp_path):
    db = tmp_path / "state.db"
    monkeypatch.setattr(cli, "_open_store", lambda: Store(db))
    monkeypatch.setattr(cli, "_store", lambda: Store(db))
    monkeypatch.setattr(cli, "_WORKSPACE_ARG", None)


def test_feature_cli_roundtrip(monkeypatch, tmp_path):
    _patch_cli(monkeypatch, tmp_path)

    res = runner.invoke(cli.app, ["feature", "add", "Тёмная тема", "--json"])
    assert res.exit_code == 0, res.output
    key = json.loads(res.stdout)["key"]

    res = runner.invoke(cli.app, ["job", "start", "--feature", key, "--title", "тема", "--json"])
    assert res.exit_code == 0, res.output
    job = json.loads(res.stdout)
    assert job["anchor"] == key

    res = runner.invoke(cli.app, ["feature", "show", key, "--json"])
    payload = json.loads(res.stdout)
    assert [j["id"] for j in payload["jobs"]] == [job["id"]]

    assert runner.invoke(cli.app, ["feature", "status", key, "done"]).exit_code == 0
    res = runner.invoke(cli.app, ["features", "--status", "done", "--json"])
    assert [f["key"] for f in json.loads(res.stdout)] == [key]


def test_job_start_rejects_both_anchors_and_bare_call(monkeypatch, tmp_path):
    _patch_cli(monkeypatch, tmp_path)
    runner.invoke(cli.app, ["feature", "add", "ф"])

    res = runner.invoke(cli.app, ["job", "start", "PROJ-1", "--feature", "WORK-1"])
    assert res.exit_code == 1 and "что-то одно" in res.output

    res = runner.invoke(cli.app, ["job", "start"])
    assert res.exit_code == 1 and "--title" in res.output

    res = runner.invoke(cli.app, ["job", "start", "--feature", "NOPE-9"])
    assert res.exit_code == 1 and "не найдена" in res.output


def test_jobs_filter_by_feature_cli(monkeypatch, tmp_path):
    _patch_cli(monkeypatch, tmp_path)
    key = json.loads(
        runner.invoke(cli.app, ["feature", "add", "ф", "--json"]).stdout
    )["key"]
    runner.invoke(cli.app, ["job", "start", "--feature", key, "--title", "по фиче"])
    runner.invoke(cli.app, ["job", "start", "--title", "без якоря"])

    res = runner.invoke(cli.app, ["jobs", "--feature", key, "--json"])
    assert [j["title"] for j in json.loads(res.stdout)] == ["по фиче"]

    res = runner.invoke(cli.app, ["jobs", "--json"])
    assert len(json.loads(res.stdout)) == 2
