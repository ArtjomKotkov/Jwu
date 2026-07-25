"""Индексация git-информации о папках — чтением файлов, без запуска git."""

from pathlib import Path

from jwu.core import gitinfo


def _make_repo(path: Path, *, branch: str = "main", origin: str = "") -> Path:
    """Минимальный «репозиторий»: ровно то, что читает jwu."""
    path.mkdir(parents=True, exist_ok=True)
    git = path / ".git"
    git.mkdir()
    (git / "HEAD").write_text(f"ref: refs/heads/{branch}\n")
    if origin:
        (git / "config").write_text(
            '[core]\n\trepositoryformatversion = 0\n'
            f'[remote "origin"]\n\turl = {origin}\n\tfetch = +refs/heads/*\n'
        )
    return path


def test_repo_name_from_origin_and_branch(tmp_path):
    repo = _make_repo(tmp_path / "checkout", branch="feat/workspaces",
                      origin="git@github.com:acme/jwu.git")
    info = gitinfo.git_info(repo)
    assert info is not None
    assert (info.name, info.branch) == ("jwu", "feat/workspaces")
    assert info.label == "jwu/feat/workspaces"   # имя из origin, а не из папки


def test_repo_name_falls_back_to_folder(tmp_path):
    repo = _make_repo(tmp_path / "my-project")
    assert gitinfo.git_info(repo).label == "my-project/main"


def test_detached_head_is_reported_as_sha(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    (repo / ".git" / "HEAD").write_text("9f1c0de2b4a5c6d7e8f90a1b2c3d4e5f60718293\n")
    info = gitinfo.git_info(repo)
    assert (info.branch, info.detached) == ("", "9f1c0de")
    assert info.label == "repo/@9f1c0de"


def test_git_file_of_worktree_is_followed(tmp_path):
    """У worktree/submodule .git — файл со ссылкой на настоящий каталог."""
    real = tmp_path / "real-git"
    real.mkdir()
    (real / "HEAD").write_text("ref: refs/heads/wt\n")
    work = tmp_path / "worktree"
    work.mkdir()
    (work / ".git").write_text(f"gitdir: {real}\n")

    info = gitinfo.git_info(work)
    assert info is not None and info.branch == "wt"


def test_not_a_repo_and_no_upward_search(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    nested = repo / "src" / "core"
    nested.mkdir(parents=True)
    # вверх намеренно НЕ поднимаемся: маркер ставится ровно у корня репозитория
    assert gitinfo.git_info(nested) is None
    assert gitinfo.git_info(tmp_path / "empty") is None


def test_find_repos_inside_folder(tmp_path):
    _make_repo(tmp_path / "dev" / "backend", origin="https://git.acme.com/scm/x/backend.git")
    _make_repo(tmp_path / "dev" / "frontend")
    (tmp_path / "dev" / "notes").mkdir(parents=True)

    found = gitinfo.find_repos(tmp_path / "dev")
    assert sorted(i.name for i in found) == ["backend", "frontend"]


def test_repo_folder_is_not_scanned_deeper(tmp_path):
    """Если папка сама репозиторий — внутрь не лезем (подмодули тут не нужны)."""
    repo = _make_repo(tmp_path / "repo")
    _make_repo(repo / "vendor" / "lib")

    found = gitinfo.find_repos(repo)
    assert [i.root for i in found] == [str(repo)]


def test_noise_directories_are_skipped(tmp_path):
    _make_repo(tmp_path / "dev" / "node_modules" / "pkg")
    _make_repo(tmp_path / "dev" / "real")

    found = gitinfo.find_repos(tmp_path / "dev")
    assert [i.name for i in found] == ["real"]


def test_depth_is_limited(tmp_path):
    _make_repo(tmp_path / "a" / "b" / "c" / "deep")
    assert gitinfo.find_repos(tmp_path / "a", max_depth=2) == []
    assert gitinfo.find_repos(tmp_path / "a", max_depth=3)[0].name == "deep"


def test_index_maps_paths_to_repos(tmp_path):
    repo = _make_repo(tmp_path / "solo")
    group = tmp_path / "group"
    _make_repo(group / "one")
    _make_repo(group / "two")

    index = gitinfo.index([str(repo), str(group), str(tmp_path / "gone")])
    assert [i.name for i in index[str(repo)]] == ["solo"]
    assert len(index[str(group)]) == 2
    assert index[str(tmp_path / "gone")] == []   # пропавшая папка не роняет индексацию


def test_unreadable_files_do_not_raise(tmp_path):
    repo = tmp_path / "broken"
    (repo / ".git").mkdir(parents=True)
    # ни HEAD, ни config — инфо всё равно возвращается, просто без ветки
    info = gitinfo.git_info(repo)
    assert info is not None and info.branch == "" and info.name == "broken"


def test_hidden_directories_are_skipped(tmp_path):
    """Скрытые каталоги не индексируем — дерево структуры их тоже не показывает."""
    _make_repo(tmp_path / "dev" / ".cache" / "vendored")
    _make_repo(tmp_path / "dev" / "visible")

    found = gitinfo.find_repos(tmp_path / "dev")
    assert [i.name for i in found] == ["visible"]
