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
    assert rh.similarity("кеш утекает между клиентами сервиса", "кеш пустой после рестарта сервиса",
                         "a/x.py", "b/y.py") == 0                                     # разные файлы, половина слов
    assert rh.similarity("кеш утекает между клиентами", "кеш утекает между клиентами", "a/x.py", "b/y.py") > 0.7
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


def test_round_imported_late_joins_chain(store):
    late = rh.add_round(store, PR, side="external", label="bot", ts="2026-01-03T10:00:00+00:00",
                        findings=[{"level": "critical", "text": "галка sendPassword превращается в false при сохранении"}])
    early = rh.add_round(store, PR, side="external", label="bot", ts="2026-01-01T10:00:00+00:00",
                         findings=[{"level": "critical", "text": "отмеченная галка sendPassword при сохранении превращается в false"}])
    [late_id] = _ids(store, late["round_id"])
    [early_id] = _ids(store, early["round_id"])
    assert early["matched"] == []                       # ранний круг — начало цепочки
    row = store.conn.execute("SELECT same_as FROM review_findings WHERE id = ?", (late_id,)).fetchone()
    assert row["same_as"] == early_id
    found, _ = rh.issues(store, PR)
    assert len(found) == 1 and found[0].state == "open"


def test_manual_link_survives_relink(store):
    r = rh.add_round(store, PR, side="local", label="a", ts="2026-01-01T10:00:00+00:00",
                     findings=[{"text": "кеш не учитывает арендатора"}])
    r2 = rh.add_round(store, PR, side="local", label="a", ts="2026-01-02T10:00:00+00:00",
                      findings=[{"text": "совсем другое про логи сервиса"}])
    a, b = _ids(store, r["round_id"])[0], _ids(store, r2["round_id"])[0]
    rh.link(store, b, a)
    rh.add_round(store, PR, side="local", label="a", ts="2026-01-03T10:00:00+00:00", findings=[{"text": "x y z"}])
    assert store.conn.execute("SELECT same_as FROM review_findings WHERE id = ?", (b,)).fetchone()["same_as"] == a


@pytest.mark.parametrize("text, file, expected", [
    ("Названия quick reply обрезаются до 20 символов без проверки уникальности", "gate/api.py", "логика и баги"),
    ("Стандартный postback без `postback.mid` показывается как неподдерживаемое", "gate/hook.py", "логика и баги"),
    ("Две добавленные строки превышают установленную ширину 160 символов.", "README.md", "длина строк и форматирование"),
    ("В новом тесте используется запрещённый реальный домен `example.org`", "app/tests/test_x.py", "правила проекта"),
    ("Чтение внутренних логов открыто без авторизации.", "nginx/conf.d/main.conf", "безопасность"),
    ("docstring обещает устойчивость, а типы не проверяются", "app/serializers.py", "документация"),
    ("ассерт проверяет не то", "app/tests/test_views.py", "тесты"),
    ("Лишний запрос посетителя на каждый вебхук.", "gate/hook.py", "производительность"),
    ("имя `stored` ничего не говорит о содержимом", "gate/hook.py", "нейминг"),
    ("гонка при записи счётчика", "", "конкурентность"),
])
def test_category(text, file, expected):
    assert rh.category(text, file) == expected


def test_same_source_matches_across_files(store):
    rh.add_round(store, PR, side="external", label="bot", ts="2026-01-01T10:00:00+00:00", findings=[
        {"text": "Выключенный отдел остаётся выбранным в селекторах настроек", "file": "app/general.component.ts"}])
    r = rh.add_round(store, PR, side="external", label="bot", ts="2026-01-02T10:00:00+00:00", findings=[
        {"text": "Выключенный отдел по-прежнему остаётся выбранным", "file": "app/departments.service.ts"}])
    assert len(r["matched"]) == 1                       # тот же ревьюер, соседний файл — та же проблема
    other = rh.add_round(store, PR, side="local", label="deep", ts="2026-01-03T10:00:00+00:00", findings=[
        {"text": "Выключенный отдел виден в отчёте статистики за месяц", "file": "app/other.ts"}])
    assert other["matched"] == []                       # другой ревьюер и другой файл — нужно почти всё


def test_job_review_commit_only_from_pr_branch(store):
    job = store.create_job("X-1")
    store.link_job_pr(job.id, 42, "PROJ", "app")
    store.add_job_record(job.id, REVIEW_1, kind="review", branch="", commit="mainhead1")
    store.add_job_record(job.id, REVIEW_2, kind="review", branch="feature/X-1-cache", commit="featcommit")
    rh.import_job_reviews(store, PR, "feature/X-1-cache")
    commits = [r["commit_sha"] for r in store.conn.execute("SELECT commit_sha FROM review_rounds ORDER BY ts, id")]
    assert commits == ["", "featcommit"]


def test_rewritten_comment_adds_findings_to_same_round(store):
    first = rh.add_round(store, PR, side="external", label="bot", source_key="c:1:944", findings=[])
    again = rh.add_round(store, PR, side="external", label="bot", source_key="c:1:944", verdict="FAILED 0/1/0",
                         findings=[{"level": "major", "text": "находка после правки коммента"}])
    assert again["round_id"] == first["round_id"] and again["added"] == 1
    assert rh.add_round(store, PR, side="external", label="bot", source_key="c:1:944",
                        findings=[{"text": "находка после правки коммента"}])["skipped"]


def test_link_jobs_by_branch(store):
    from jwu.core.service import Service

    job = store.create_job("X-1")
    store.add_job_record(job.id, "начал", branch="feature/X-1-cache")
    dev = store.create_job("X-2")
    store.add_job_record(dev.id, "в develop", branch="develop")
    svc = Service.__new__(Service)
    svc.store = store
    assert svc._link_jobs_by_branch([(PR, "feature/X-1-cache"), (rh.PRRef(43, "PROJ", "app"), "develop")]) == 1
    assert svc._link_jobs_by_branch([(PR, "feature/X-1-cache")]) == 0
    assert [j.id for j in store.jobs_for_pr(42, "PROJ", "app")] == [job.id]


def test_reworded_finding_updates_text_keeps_fate(store):
    r = rh.add_round(store, PR, side="external", label="bot", source_key="c:2:10",
                     findings=[{"level": "minor", "text": "`app/x.py:5`", "file": "app/x.py", "line": 5}])
    [fid] = _ids(store, r["round_id"])
    rh.set_fate(store, [fid], "fixed", commit="abc")
    again = rh.add_round(store, PR, side="external", label="bot", source_key="c:2:10", findings=[
        {"level": "minor", "text": "хост вне списка разрешённых — `app/x.py:5`", "file": "app/x.py", "line": 5}])
    assert again["skipped"] and _ids(store, r["round_id"]) == [fid]
    row = store.conn.execute("SELECT text, fate FROM review_findings WHERE id = ?", (fid,)).fetchone()
    assert row["text"].startswith("хост вне списка") and row["fate"] == "fixed"


def test_fate_on_latest_round_wins(store):
    a = rh.add_round(store, PR, side="external", label="bot", ts="2026-01-01T10:00:00+00:00",
                     findings=[{"text": "недостаточный контраст текста в светлой теме"}])
    b = rh.add_round(store, PR, side="external", label="bot", ts="2026-01-02T10:00:00+00:00",
                     findings=[{"text": "контраст текста в светлой теме недостаточный"}])
    rh.set_fate(store, _ids(store, b["round_id"]), "fixed", commit="c2")
    rh.set_fate(store, _ids(store, a["round_id"]), "fixed", commit="c1")    # позже по времени, но на раннем круге
    found, _ = rh.issues(store, PR)
    assert len(found) == 1 and found[0].state == "fixed"
