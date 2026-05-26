import keyring

from jwu.core import config as cfgmod
from jwu.core.config import Config, db_path, load_config, save_config


class _MemKeyring(keyring.backend.KeyringBackend):
    priority = 1  # type: ignore[assignment]

    def __init__(self):
        self._s = {}

    def get_password(self, service, username):
        return self._s.get((service, username))

    def set_password(self, service, username, password):
        self._s[(service, username)] = password

    def delete_password(self, service, username):
        self._s.pop((service, username), None)


def _mem(monkeypatch):
    m = _MemKeyring()
    monkeypatch.setattr(keyring, "get_password", m.get_password)
    monkeypatch.setattr(keyring, "set_password", m.set_password)
    monkeypatch.setattr(keyring, "delete_password", m.delete_password)
    return m


def test_save_then_load_roundtrip(tmp_path):
    p = tmp_path / "config.toml"
    cfg = Config()
    cfg.jira.base_url = "https://jira.acme.com"
    cfg.jira.username = "alice"
    cfg.jira.project = "ACME"
    cfg.bitbucket.base_url = "https://git.acme.com"
    cfg.bitbucket.repo = "server"
    cfg.storage.db_path = "/tmp/jwu.db"
    save_config(cfg, p)

    loaded = load_config(p)
    assert loaded.jira.base_url == "https://jira.acme.com"
    assert loaded.jira.username == "alice"
    assert loaded.jira.project == "ACME"
    assert loaded.bitbucket.repo == "server"
    assert loaded.storage.db_path == "/tmp/jwu.db"


def test_save_preserves_unknown_keys_and_views(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        '[jira]\nbase_url = "https://old"\ntoken_service = "custom-svc"\n'
        '[jira.views]\nmine = "assignee = currentUser()"\n'
    )
    cfg = load_config(p)
    cfg.jira.base_url = "https://new"
    save_config(cfg, p)

    reloaded = load_config(p)
    assert reloaded.jira.base_url == "https://new"
    assert reloaded.jira.token_service == "custom-svc"          # чужой ключ сохранён
    assert reloaded.jira.views["mine"] == "assignee = currentUser()"  # views сохранены


def test_db_path_env_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("JWU_DB_PATH", str(tmp_path / "env.db"))
    assert db_path() == tmp_path / "env.db"


def test_db_path_from_config(tmp_path, monkeypatch):
    monkeypatch.delenv("JWU_DB_PATH", raising=False)
    cfg = Config()
    cfg.storage.db_path = str(tmp_path / "cfg.db")
    assert db_path(cfg) == tmp_path / "cfg.db"


def test_db_path_default(monkeypatch):
    monkeypatch.delenv("JWU_DB_PATH", raising=False)
    assert db_path(Config()).name == "state.db"


def test_jira_token_prefers_env(monkeypatch):
    _mem(monkeypatch)
    monkeypatch.setenv("JIRA_TOKEN", "envtok")
    assert cfgmod.jira_token(Config()) == "envtok"


def test_jira_token_from_keyring(monkeypatch):
    m = _mem(monkeypatch)
    monkeypatch.delenv("JIRA_TOKEN", raising=False)
    m.set_password("jira-pat", "jira", "kr-tok")
    assert cfgmod.jira_token(Config()) == "kr-tok"


def test_jira_login_uses_username_account(monkeypatch):
    m = _mem(monkeypatch)
    cfg = Config()
    cfg.jira.username = "alice"
    m.set_password("jira-login", "alice", "secretpw")
    assert cfgmod.jira_login(cfg) == ("alice", "secretpw")


def test_jira_login_none_without_password(monkeypatch):
    _mem(monkeypatch)
    cfg = Config()
    cfg.jira.username = "alice"
    assert cfgmod.jira_login(cfg) is None
