"""Куда доехал коммит: сборка настоящих репозиториев во временном каталоге.

Мокать здесь нечего — проверяется ровно то, как отвечает git, поэтому тесты создают
маленькие репозитории на диске. Если git в системе нет, тесты пропускаются.
"""

import shutil
import subprocess

import pytest

from jwu.core import gitbranches

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git не установлен")


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _repo(root, origin=""):
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "master")
    _git(root, "config", "user.email", "t@test")
    _git(root, "config", "user.name", "T")
    if origin:
        _git(root, "remote", "add", "origin", origin)
    return root


def _commit(root, text, name="f.txt"):
    (root / name).write_text(text, encoding="utf-8")
    _git(root, "add", name)
    _git(root, "commit", "-qm", text)
    return _git(root, "rev-parse", "HEAD")


def test_reach_splits_release_branches_by_presence(tmp_path):
    root = _repo(tmp_path / "app")
    base = _commit(root, "base")
    _git(root, "checkout", "-qb", "release-1.0")
    _git(root, "checkout", "-q", "master")
    fix = _commit(root, "fix")
    _git(root, "checkout", "-qb", "release-1.1")   # ветка с фиксом
    _git(root, "checkout", "-q", "master")

    result = gitbranches.repo_reach(str(root), [fix])
    assert result.error == ""
    assert result.found_shas == [fix]
    assert "release-1.1" in result.reached and "master" in result.reached
    assert result.missing == ["release-1.0"]
    assert base not in result.reached


def test_partial_reach_when_only_part_of_commits_arrived(tmp_path):
    """Ветка с частью коммитов — самый опасный случай: фикс там неполный."""
    root = _repo(tmp_path / "app")
    first = _commit(root, "first")
    _git(root, "checkout", "-qb", "release-2.0")   # сюда доехал только первый коммит
    _git(root, "checkout", "-q", "master")
    second = _commit(root, "second")

    result = gitbranches.repo_reach(str(root), [first, second])
    assert result.reached == ["master"]
    assert result.partial == ["release-2.0"]


def test_unknown_commit_is_reported_as_not_found(tmp_path):
    root = _repo(tmp_path / "app")
    _commit(root, "base")
    result = gitbranches.repo_reach(str(root), ["0" * 40])
    assert result.found_shas == []
    assert result.commits[0].found is False


def test_branch_pattern_limits_candidates(tmp_path):
    root = _repo(tmp_path / "app")
    fix = _commit(root, "fix")
    _git(root, "checkout", "-qb", "feature/x")
    _git(root, "checkout", "-q", "master")
    result = gitbranches.repo_reach(str(root), [fix], patterns=["feature/*"])
    assert result.reached == ["feature/x"]   # master под шаблон не попал


def test_reach_skips_repos_without_the_commit_and_dedupes_clones(tmp_path):
    """Клоны одного репозитория (стенды, worktree) не должны дублироваться в отчёте."""
    origin = "https://git.test/scm/app.git"
    first = _repo(tmp_path / "clone-a", origin=origin)
    fix = _commit(first, "fix")
    second = _repo(tmp_path / "clone-b", origin=origin)
    _git(second, "fetch", "-q", str(first), "master")
    _git(second, "reset", "-q", "--hard", "FETCH_HEAD")
    other = _repo(tmp_path / "other", origin="https://git.test/scm/other.git")
    _commit(other, "unrelated")

    result = gitbranches.reach([str(first), str(second), str(other)], [fix])
    assert [r.root for r in result] == [str(first)]   # чужой репозиторий и клон отсеяны


def test_broken_repo_reports_error_instead_of_raising(tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    result = gitbranches.repo_reach(str(plain), ["deadbeef"])
    assert result.error and result.commits == []
