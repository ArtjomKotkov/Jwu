"""История ревью по PR: разбор записей журнала, сопоставление кругов, судьбы, выжимка."""

import pytest

from jwu.core import reviewhist as rh
from jwu.core.models import PRComment, PRTask
from jwu.core.store import Store

PR = rh.PRRef(42, "PROJ", "app")

REVIEW_1 = """Ревьюер: reviewer-sample; ветка feature/X-1-cache→main; PR #42; итерация 1; вердикт: нужны доработки; блокеры: 1, важные: 1, мелкие: 2.
Замечания:
[B1] cache/store.py:120 — ключ кеша не учитывает арендатора, данные утекают между клиентами
[I1] retry без ограничения числа попыток в worker/loop.py
[M1] опечатка в docstring CacheStore
[M2] лишний импорт os в cache/store.py"""

REVIEW_2 = ("Ревьюер: reviewer-sample; ветка feature/X-1-cache→main; PR #42; итерация 2; вердикт: можно коммитить; "
            "блокеры: 0, важные: 0, мелкие: 1. Замечания: [M1] опечатка в docstring класса CacheStore")


@pytest.fixture
def store(tmp_path):
    st = Store(tmp_path / "s.db")
    st.use_workspace(st.get_workspace_by_slug("work").id)
    yield st
    st.close()


def test_parse_local_review_both_layouts():
    p = rh.parse_local_review(REVIEW_1)
    assert (p["reviewer"], p["pr"], p["iteration"], p["verdict"]) == ("reviewer-sample", 42, 1, "нужны доработки")
    assert [f["anchor"] for f in p["findings"]] == ["B1", "I1", "M1", "M2"]
    assert [f["level"] for f in p["findings"]] == ["blocker", "important", "minor", "minor"]
    assert (p["findings"][0]["file"], p["findings"][0]["line"]) == ("cache/store.py", 120)
    assert p["findings"][1]["file"] == "worker/loop.py"
    one_line = rh.parse_local_review(REVIEW_2)
    assert [(f["anchor"], f["text"]) for f in one_line["findings"]] == [("M1", "опечатка в docstring класса CacheStore")]
    assert rh.parse_local_review("Ревьюер: x; PR #—; итерация 1; вердикт: можно коммитить")["findings"] == []


def test_find_file_ignores_attribute_access():
    assert rh.find_file("exec_driver_sql вместо sa.text") == ("", None)
    assert rh.find_file("строки в `docs/ADDONS.md:121, 128` длиннее 160") == ("docs/ADDONS.md", 121)


def test_rounds_match_and_gone(store):
    r1 = rh.add_round(store, PR, side="local", label="reviewer-sample", ts="2026-01-01T10:00:00+00:00",
                      findings=rh.parse_local_review(REVIEW_1)["findings"])
    assert r1["added"] == 4 and r1["matched"] == []
    r2 = rh.add_round(store, PR, side="local", label="reviewer-sample", ts="2026-01-01T11:00:00+00:00",
                      findings=rh.parse_local_review(REVIEW_2)["findings"])
    assert len(r2["matched"]) == 1                       # опечатка — та же проблема
    found, rounds = rh.issues(store, PR)
    assert len(rounds) == 2
    states = {i.last["anchor"] + ":" + i.last["text"][:8]: i.state for i in found}
    assert list(states.values()).count("open") == 1      # повторилась во 2-м круге
    assert list(states.values()).count("gone") == 3      # в следующем круге того же ревьюера их нет


def test_rejected_repeat_and_fixed_regression(store):
    ext = dict(side="external", label="ci-bot")
    a = rh.add_round(store, PR, **ext, ts="2026-01-01T10:00:00+00:00", findings=[
        {"level": "major", "text": "`api/views.py:30` нет проверки прав на удаление чужого объекта"},
        {"level": "minor", "text": "длинная строка в README.md:10"}])
    rows = store.conn.execute("SELECT id FROM review_findings WHERE round_id = ? ORDER BY id",
                              (a["round_id"],)).fetchall()
    major, minor = rows[0]["id"], rows[1]["id"]
    with pytest.raises(rh.ReviewError, match="довод"):
        rh.set_fate(store, [f"F{major}"], "rejected")
    rh.set_fate(store, [f"F{major}"], "rejected", reason="права проверяет middleware, см. тред")
    rh.set_fate(store, [minor], "fixed", commit="abc1234567")
    rh.add_round(store, PR, **ext, ts="2026-01-02T10:00:00+00:00", findings=[
        {"level": "major", "text": "api/views.py:31: нет проверки прав на удаление чужого объекта"},
        {"level": "minor", "text": "README.md:10 длинная строка"}])
    out = rh.history(store, PR)
    assert out["counts"] == {"rejected": 1, "reopened": 1}
    assert out["rejected_repeats"] == 1
    md = out["markdown"]
    assert "### Отклонено" in md and "права проверяет middleware" in md
    assert "### Было исправлено, но подняли снова" in md and "abc1234567" in md


def test_duplicate_link_and_cycle(store):
    r = rh.add_round(store, PR, side="local", label="a", findings=[{"text": "первое про кеш"},
                                                                    {"text": "совсем другое про логи"}])
    ids = [x["id"] for x in store.conn.execute("SELECT id FROM review_findings WHERE round_id = ?", (r["round_id"],))]
    with pytest.raises(rh.ReviewError, match="same_as"):
        rh.set_fate(store, [ids[1]], "duplicate")
    rh.set_fate(store, [ids[1]], "duplicate", same_as=ids[0])
    with pytest.raises(rh.ReviewError, match="круг"):
        rh.link(store, ids[0], ids[1])
    found, _ = rh.issues(store, PR)
    assert len(found) == 1


def test_import_job_reviews_idempotent(store):
    job = store.create_job("X-1")
    store.link_job_pr(job.id, 42, "PROJ", "app")
    store.add_job_record(job.id, REVIEW_1, kind="review")
    store.add_job_record(job.id, REVIEW_1.replace("PR #42", "PR #7"), kind="review")   # чужой PR — мимо
    store.add_job_record(job.id, "Нейроревью PR #42: [M1] что-то", kind="review")       # без шапки — мимо
    assert rh.import_job_reviews(store, PR) == 1
    assert rh.import_job_reviews(store, PR) == 0
    out = rh.history(store, PR)
    assert out["rounds"] == 1 and out["counts"] == {"open": 4}
    assert "local reviewer-sample ×1" in out["markdown"]


def test_import_comments_tasks_resolved(store):
    comments = [
        PRComment(id="1", author="alice", text="тут гонка при записи", file="db/write.py", line=5, created=1_700_000_000_000),
        PRComment(id="2", author="me", text="мой коммент", file="db/write.py", line=6),
        PRComment(id="3", author="bob", text="поправь имя", created=1_700_000_100_000,
                  tasks=[PRTask(id=1, text="имя", state="RESOLVED")]),
        PRComment(id="4", author="bob", text="общий коммент без задач"),
        PRComment(id="5", author="alice", text="ответ", file="db/write.py", line=5, depth=1),
    ]
    assert rh.import_pr_comments(store, PR, comments, me="ME") == 2
    assert rh.import_pr_comments(store, PR, comments, me="ME") == 0
    out = rh.history(store, PR)
    assert out["counts"] == {"open": 1, "fixed": 1}       # человеческие треды не «снимаются» сами


def test_history_respects_budget(store):
    rh.add_round(store, PR, side="external", label="bot", findings=[
        {"level": "major", "text": f"проблема номер {n} " + "подробности " * 20 + f"слово{n}"} for n in range(40)])
    out = rh.history(store, PR, max_chars=2000)
    assert len(out["markdown"]) <= 2000
    assert "не влезло" in out["markdown"]


def test_empty_history(store):
    out = rh.history(store, PR)
    assert out["rounds"] == 0 and "Кругов ревью нет" in out["markdown"]


def test_similarity_rules():
    assert rh.similarity("send_contact_message дублирует базовый метод", "_find_quoted дублирует базовый метод") == 0
    assert rh.similarity("get_rate дублирует is_enabled", "`api/x.py:3`: `get_rate` дублирует is_enabled целиком") > 0.4
    assert rh.similarity("кеш утекает", "кеш утекает", "a/x.py", "b/y.py") == 0          # разные файлы
    assert rh.similarity("одно слово кеш", "совсем другое про кеш") == 0                    # одно общее слово


def _ids(store, round_id):
    return [r["id"] for r in store.conn.execute("SELECT id FROM review_findings WHERE round_id = ? ORDER BY id",
                                                (round_id,))]


def test_round_diff_and_compare(store):
    loc = rh.add_round(store, PR, side="local", label="deep", commit="aaa1111", ts="2026-01-01T09:00:00+00:00",
                       findings=[{"level": "important", "text": "гонка при записи счётчика в update_counter"},
                                 {"level": "minor", "text": "нет теста на пустой список заказов"}])
    e1 = rh.add_round(store, PR, side="external", label="bot", commit="aaa1111", ts="2026-01-01T10:00:00+00:00",
                      findings=[{"level": "major", "text": "update_counter: гонка при записи счётчика"},
                                {"level": "major", "text": "README.md:5 обещает ретраи, а их нет"},
                                {"level": "minor", "text": "длинная строка таблицы docs/table.md:3"}])
    rejected = _ids(store, e1["round_id"])[1]
    rh.set_fate(store, [rejected], "out_of_pr", reason="README не менялся в PR")
    cmp = rh.compare(store, PR)
    assert cmp["same_commit"] and len(cmp["matched"]) == 1 and len(cmp["only_local"]) == 1
    assert len(cmp["only_external"]) == 1 and len(cmp["only_external_rejected"]) == 1
    rh.add_round(store, PR, side="external", label="bot", commit="bbb2222", ts="2026-01-02T10:00:00+00:00",
                 findings=[{"level": "major", "text": "README.md:5 обещает ретраи, которых нет"},
                           {"level": "minor", "text": "новое: опечатка в сообщении об ошибке"}])
    diff = rh.round_diff(store, PR)
    assert (len(diff["new"]), len(diff["repeat"]), len(diff["repeat_rejected"]), len(diff["gone"])) == (1, 0, 1, 2)
    assert rh.brief(store, PR)["repeat_rejected"] == 1
    out = rh.history(store, PR)
    assert "повтор отклонённого 1" in out["markdown"] and out["last_external"]["summary"]
    assert "→ нет в круге 02.01 на bbb2222" in out["markdown"]          # гонку бот больше не поднимал


def test_misses_by_category(store):
    rh.add_round(store, PR, side="local", label="deep", ts="2026-11-01T09:00:00+00:00",
                 findings=[{"text": "гонка при записи счётчика в update_counter"}])
    rh.add_round(store, PR, side="external", label="bot", ts="2026-11-01T10:00:00+00:00",
                 findings=[{"text": "строки таблицы в docs/guide.md:10 длиннее 160 символов"},
                           {"text": "имя функции load_all вводит в заблуждение"},
                           {"text": "update_counter: гонка при записи счётчика"}])
    rep = rh.misses(store, days=36500)
    assert rep["compared"] == 1 and rep["matched"] == 1 and rep["only_external"] == 2
    assert {c["name"] for c in rep["categories"]} == {"длина строк и форматирование", "нейминг"}
    assert "Чаще всего наше ревью пропускает" in rh.misses_line(store, days=36500)
    assert rh.category("тут гонка при записи") == "конкурентность"
    assert rh.category("что-то сломалось") == "логика и баги"


def test_service_imports_addon_rounds(store, monkeypatch):
    from jwu.core import addons
    from jwu.core.service import Service

    class FakeAddon:
        name = "fake"

        def has(self, attr):
            return attr == "pr_review_rounds"

        def call(self, attr, cfg, pr, comments):
            return [{"label": "ci-review", "findings": [{"level": "major", "text": "бот нашёл проблему"}],
                     "source_key": f"fake:{comments[0].id}", "comment_ids": [comments[0].id], "commit": "c1"}]

    monkeypatch.setattr(addons, "active", lambda: [FakeAddon()])
    svc = Service.__new__(Service)
    svc.store, svc.cfg = store, None
    svc._resolve_username = lambda: "me"
    comments = [PRComment(id="9", author="bot", text="**отчёт бота**", tasks=[PRTask(id=1, text="x")]),
                PRComment(id="10", author="alice", text="поправь тут", file="a.py", line=1)]
    assert svc._import_review_sources(PR, comments) == 2      # круг бота + тред alice; коммент бота не тред
    assert svc._import_review_sources(PR, comments) == 0
    out = rh.history(store, PR)
    assert "external ci-review" in out["markdown"] and "external alice" in out["markdown"]
