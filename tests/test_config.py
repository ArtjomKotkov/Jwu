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


def test_export_import_bundle_roundtrip(tmp_path, monkeypatch):
    """export → import переносит config + ВСЕ секреты (включая гейт) в чистую среду."""
    from jwu.core import secrets
    from jwu.core.config import export_bundle, import_bundle

    _mem(monkeypatch)
    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr(cfgmod, "config_path", lambda: cfg_path)

    cfg = Config()
    cfg.jira.base_url = "https://jira.acme.com"
    cfg.jira.username = "alice"
    cfg.jira.project = "ACME"
    cfg.jira.proxy_basic_user = "gateuser"
    cfg.bitbucket.base_url = "https://git.acme.com"
    cfg.bitbucket.repo = "server"
    cfg.storage.db_path = str(tmp_path / "jwu.db")
    secrets.set_secret(cfg.jira.token_service, cfg.jira.token_account, "JTOK")
    secrets.set_secret(cfg.jira.login_service, "alice", "JPW")
    secrets.set_secret(cfg.jira.proxy_basic_service, "gateuser", "GPW")
    secrets.set_secret(cfg.bitbucket.token_service, cfg.bitbucket.token_account, "BTOK")

    bundle = tmp_path / "bundle.toml"
    n = export_bundle(cfg, bundle)
    assert n == 4
    text = bundle.read_text()
    assert "JPW" in text and "GPW" in text  # секреты в бандле (плайнтекст)

    # «новая машина»: пустой keyring + нет config.toml
    m2 = _mem(monkeypatch)
    if cfg_path.exists():
        cfg_path.unlink()

    cfg2, written = import_bundle(bundle)
    assert written == 4
    assert cfg2.jira.proxy_basic_user == "gateuser"

    loaded = load_config(cfg_path)
    assert loaded.jira.base_url == "https://jira.acme.com"
    assert loaded.jira.proxy_basic_user == "gateuser"
    assert m2.get_password("jira-login", "alice") == "JPW"
    assert m2.get_password("jira-proxy-basic", "gateuser") == "GPW"
    assert m2.get_password("bitbucket-pat", "bitbucket") == "BTOK"


def test_import_bundle_missing_file_raises(tmp_path):
    from jwu.core.config import ConfigError, import_bundle

    import pytest
    with pytest.raises(ConfigError):
        import_bundle(tmp_path / "nope.toml")
