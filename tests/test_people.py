"""Человек по словам: транслит, уменьшительные, опечатки в окончаниях."""

from jwu.core import people

OLEG = {"name": "osmirn", "display_name": "Oleg Smirnovsky"}
SASHA = {"name": "avolk2", "display_name": "Alexander Volkov"}
OTHER = {"name": "ivanov", "display_name": "Ivan Ivanov"}


def test_score_cyrillic_names_and_nicknames():
    assert people.score("Олег Смирновский", OLEG) == 2
    assert people.score("Саша Волков", SASHA) == 2
    assert people.score("Волков", SASHA) == 1
    assert people.score("Саша Волков", OTHER) == 0
    assert people.score("avolk2", SASHA) == 1


def test_search_keys_tolerate_endings():
    keys = people.search_keys("Смирновский")
    assert "smirn" in keys


def test_find_people_prefers_usual_then_searches(monkeypatch):
    from jwu.core.service import Service

    class FakeClient:
        calls = []

        def users(self, q):
            self.calls.append(q)
            return [OTHER, {"name": "nkoz", "display_name": "Nikolay Kozlov"}] if q.startswith("kozl") else []

    svc = Service.__new__(Service)
    fake = FakeClient()
    monkeypatch.setattr(Service, "usual_reviewers", lambda self, limit=6: [{**OLEG, "prs": 3}, {**SASHA, "prs": 2}])
    monkeypatch.setattr(Service, "_require_prs", lambda self: fake)
    assert svc.find_people("Саша Волков") == [{"name": "avolk2", "display_name": "Alexander Volkov"}]
    assert fake.calls == []                                   # нашли среди обычных — без поиска
    assert svc.find_people("Коля Козлов") == [{"name": "nkoz", "display_name": "Nikolay Kozlov"}]
    assert fake.calls
