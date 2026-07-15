"""Тесты CI-сборок: build-status (Bitbucket) + детализация из Jenkins."""

import httpx
import respx

from jwu.core.bitbucket import BitbucketClient
from jwu.core.config import Config
from jwu.core.jenkins import JenkinsClient, parse_build_url
from jwu.core.jira import JiraClient
from jwu.core.service import Service
from jwu.core.store import Store

JIRA = "https://jira.test"
BB = "https://git.test"
JEN = "https://jenkins.test"
BUILD_URL = f"{JEN}/job/app/job/tests/159/display/redirect"


def _build_status_raw(url=BUILD_URL):
    """Bitbucket задваивает запись по одной сборке: чистый ключ + ключ с экранированием."""
    return {
        "size": 2,
        "values": [
            {"state": "FAILED", "key": "ci/tests", "name": "tests",
             "url": url, "description": "#159 failed in 3 min", "dateAdded": 1700000000000},
            {"state": "FAILED", "key": "tests-https:\\/\\/jenkins\\/", "name": "tests",
             "url": url, "description": "built by Jenkins", "dateAdded": 1700000000000},
        ],
    }


def _jenkins_build_info_raw():
    return {
        "number": 159, "result": "FAILURE", "building": False,
        "duration": 221000, "estimatedDuration": 4000000, "displayName": "#159",
        "actions": [
            {},
            {"lastBuiltRevision": {"branch": [{"SHA1": "deadbeef00", "name": "origin/x"}]}},
        ],
    }


def _jenkins_testreport_raw():
    # один ответ покрывает и test_summary, и failed_cases (берут разные поля).
    return {
        "failCount": 1, "passCount": 1615, "skipCount": 8,
        "suites": [{"cases": [
            {"className": "common.tests.test_async_options",
             "name": "test_options[AsyncBaseView]", "status": "REGRESSION",
             "errorDetails": "TypeError: Can't instantiate abstract class",
             "errorStackTrace": "Traceback ... abstract method _process_request"},
            {"className": "common.tests.test_ok", "name": "test_passes",
             "status": "PASSED", "errorDetails": None, "errorStackTrace": None},
        ]}],
    }


def test_parse_build_url():
    assert parse_build_url(BUILD_URL) == ("job/app/job/tests", 159)
    assert parse_build_url(f"{JEN}/job/a/job/b/12/console") == ("job/a/job/b", 12)
    assert parse_build_url("https://x/not-a-build") is None
    assert parse_build_url("") is None


@respx.mock
def test_bitbucket_build_statuses_dedup():
    respx.get(f"{BB}/rest/build-status/1.0/commits/abc").mock(
        return_value=httpx.Response(200, json=_build_status_raw()))
    with BitbucketClient(BB, "tok") as bb:
        builds = bb.build_statuses("abc")
    assert len(builds) == 1                 # задвоение схлопнуто по url
    assert builds[0].key == "ci/tests"  # выбран ключ без обратных слэшей
    assert builds[0].state == "FAILED"


@respx.mock
def test_jenkins_failed_cases_and_summary():
    respx.get(f"{JEN}/job/app/job/tests/159/testReport/api/json").mock(
        return_value=httpx.Response(200, json=_jenkins_testreport_raw()))
    with JenkinsClient(JEN, ("u", "tok")) as jk:
        summary = jk.test_summary("job/app/job/tests", 159)
        cases = jk.failed_cases("job/app/job/tests", 159)
    assert summary == {"fail": 1, "passed": 1615, "skip": 8}
    assert len(cases) == 1                   # PASSED отфильтрован
    assert cases[0]["status"] == "REGRESSION"
    assert "abstract method" in cases[0]["stack"]


@respx.mock
def test_jenkins_test_summary_none_when_no_report():
    respx.get(f"{JEN}/job/a/job/b/1/testReport/api/json").mock(
        return_value=httpx.Response(404, text="no tests"))
    with JenkinsClient(JEN, ("u", "tok")) as jk:
        assert jk.test_summary("job/a/job/b", 1) is None
        assert jk.failed_cases("job/a/job/b", 1) == []


def _service(tmp_path, *, with_jenkins=True):
    cfg = Config()
    cfg.jira.base_url = JIRA
    cfg.bitbucket.base_url = BB
    cfg.jenkins.base_url = JEN
    cfg.jenkins.username = "u" if with_jenkins else ""
    jenkins = JenkinsClient(JEN, ("u", "tok") if with_jenkins else None)
    return Service(cfg, JiraClient(JIRA, "t"), BitbucketClient(BB, "t"),
                   Store(tmp_path / "s.db"), jenkins)


def _mock_pr_build_chain():
    respx.get(f"{BB}/rest/api/1.0/projects/PROJ/repos/repo/pull-requests/42/commits").mock(
        return_value=httpx.Response(200, json={"values": [{"id": "abc"}], "isLastPage": True}))
    respx.get(f"{BB}/rest/build-status/1.0/commits/abc").mock(
        return_value=httpx.Response(200, json=_build_status_raw()))


@respx.mock
def test_build_report_collects_failures(tmp_path):
    _mock_pr_build_chain()
    base = f"{JEN}/job/app/job/tests/159"
    respx.get(f"{base}/api/json").mock(
        return_value=httpx.Response(200, json=_jenkins_build_info_raw()))
    respx.get(f"{base}/testReport/api/json").mock(
        return_value=httpx.Response(200, json=_jenkins_testreport_raw()))
    respx.get(f"{base}/consoleText").mock(
        return_value=httpx.Response(200, text="...console tail..."))

    svc = _service(tmp_path)
    report = svc.build_report("PROJ", "repo", 42)
    svc.close()

    assert report is not None
    assert report.state == "FAILED"
    assert report.jenkins_available is True
    assert report.result == "FAILURE"
    assert report.branch == "origin/x" and report.sha == "deadbeef00"
    assert report.summary == {"fail": 1, "passed": 1615, "skip": 8}
    assert len(report.failures) == 1
    assert report.failures[0].class_name == "common.tests.test_async_options"
    assert "abstract method" in report.failures[0].stack
    assert report.console_tail == "...console tail..."


@respx.mock
def test_build_report_degrades_without_jenkins_token(tmp_path):
    _mock_pr_build_chain()
    svc = _service(tmp_path, with_jenkins=False)
    report = svc.build_report("PROJ", "repo", 42)
    svc.close()
    assert report is not None
    assert report.state == "FAILED"          # статус из Bitbucket есть
    assert report.jenkins_available is False
    assert not report.failures
    assert "токен не настроен" in report.note


@respx.mock
def test_build_report_none_when_no_builds(tmp_path):
    respx.get(f"{BB}/rest/api/1.0/projects/PROJ/repos/repo/pull-requests/42/commits").mock(
        return_value=httpx.Response(200, json={"values": [{"id": "abc"}], "isLastPage": True}))
    respx.get(f"{BB}/rest/build-status/1.0/commits/abc").mock(
        return_value=httpx.Response(200, json={"size": 0, "values": []}))
    svc = _service(tmp_path)
    assert svc.build_report("PROJ", "repo", 42) is None
    svc.close()
