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


def test_export_read_bundle_roundtrip(tmp_path, monkeypatch):
    """export → read переносит config и ВСЕ секреты; секреты адресуются слотами."""
    from jwu.core import secrets
    from jwu.core.config import export_bundle, read_bundle

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

    # «новая машина»: пустой keyring — всё нужное приезжает из бандла
    _mem(monkeypatch)
    cfg2, values = read_bundle(bundle)
    assert cfg2.jira.base_url == "https://jira.acme.com"
    assert cfg2.jira.proxy_basic_user == "gateuser"
    assert cfg2.bitbucket.repo == "server"
    assert values == {
        "jira.token": "JTOK", "jira.password": "JPW",
        "jira.gate_password": "GPW", "bitbucket.token": "BTOK",
    }
    # read_bundle ничего не пишет: config.toml остаётся нетронутым
    assert not cfg_path.exists()


def test_sdesk_disabled_by_default():
    from jwu.core.config import sdesk_enabled

    assert sdesk_enabled(Config()) is False


def test_sdesk_save_load_roundtrip(tmp_path):
    """[sdesk] пишется, только когда инстанс подключён (base_url + project)."""
    from jwu.core.config import sdesk_enabled

    p = tmp_path / "config.toml"
    cfg = Config()
    cfg.jira.base_url = "https://jira.acme.com"
    cfg.sdesk.base_url = "https://sdesk.acme.com"
    cfg.sdesk.project = "SDESK"
    cfg.sdesk.username = "alice"
    cfg.sdesk.proxy_basic_user = "gw"
    save_config(cfg, p)

    loaded = load_config(p)
    assert sdesk_enabled(loaded) is True
    assert loaded.sdesk.base_url == "https://sdesk.acme.com"
    assert loaded.sdesk.project == "SDESK"
    assert loaded.sdesk.username == "alice"
    assert loaded.sdesk.proxy_basic_user == "gw"
    # секреты SDESK лежат под отдельными сервисами
    assert loaded.sdesk.token_service == "sdesk-pat"
    assert loaded.sdesk.login_service == "sdesk-login"
    assert loaded.sdesk.proxy_basic_service == "sdesk-proxy-basic"


def test_sdesk_section_omitted_when_disabled(tmp_path):
    p = tmp_path / "config.toml"
    save_config(Config(), p)  # SDESK не задан
    assert "[sdesk]" not in p.read_text()


def test_sdesk_secrets_use_own_services(monkeypatch):
    m = _mem(monkeypatch)
    monkeypatch.delenv("SDESK_TOKEN", raising=False)
    cfg = Config()
    cfg.sdesk.username = "alice"
    m.set_password("sdesk-pat", "sdesk", "SDTOK")
    m.set_password("sdesk-login", "alice", "SDPW")
    assert cfgmod.sdesk_token(cfg) == "SDTOK"
    assert cfgmod.sdesk_login(cfg) == ("alice", "SDPW")
    # Jira-секреты не задеты — инстансы независимы
    assert cfgmod.jira_login(cfg) is None


def test_export_read_bundle_includes_sdesk(tmp_path, monkeypatch):
    from jwu.core import secrets
    from jwu.core.config import export_bundle, read_bundle, sdesk_enabled

    _mem(monkeypatch)
    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr(cfgmod, "config_path", lambda: cfg_path)

    cfg = Config()
    cfg.jira.base_url = "https://jira.acme.com"
    cfg.jira.username = "alice"
    cfg.sdesk.base_url = "https://sdesk.acme.com"
    cfg.sdesk.project = "SDESK"
    cfg.sdesk.username = "alice"
    cfg.sdesk.proxy_basic_user = "gw"
    secrets.set_secret(cfg.jira.token_service, cfg.jira.token_account, "JTOK")
    secrets.set_secret(cfg.sdesk.token_service, cfg.sdesk.token_account, "SDTOK")
    secrets.set_secret(cfg.sdesk.login_service, "alice", "SDPW")
    secrets.set_secret(cfg.sdesk.proxy_basic_service, "gw", "SDGPW")

    bundle = tmp_path / "bundle.toml"
    n = export_bundle(cfg, bundle)
    assert n == 4  # jira PAT + 3 sdesk-секрета
    assert "SDPW" in bundle.read_text()

    _mem(monkeypatch)
    cfg2, values = read_bundle(bundle)
    assert sdesk_enabled(cfg2) is True
    assert cfg2.sdesk.project == "SDESK"
    assert values["sdesk.password"] == "SDPW"
    assert values["sdesk.gate_password"] == "SDGPW"


def test_read_bundle_missing_file_raises(tmp_path):
    from jwu.core.config import ConfigError, read_bundle

    import pytest
    with pytest.raises(ConfigError):
        read_bundle(tmp_path / "nope.toml")


# --------------------------------------------------------------------------- #
# Секреты воркспейса в БД
# --------------------------------------------------------------------------- #


def test_db_secrets_prefer_env_then_db_then_keyring(tmp_path, monkeypatch):
    """Порядок источников: переменная окружения → БД воркспейса → keyring (фолбэк)."""
    from jwu.core import secrets as secmod
    from jwu.core.store import Store
    from jwu.core.workspaces import config_for_workspace

    m = _mem(monkeypatch)
    store = Store(tmp_path / "state.db")
    ws = store.get_workspace_by_slug("work")
    cfg = config_for_workspace(store, ws)

    # только keyring
    m.set_password("bitbucket-pat", "bitbucket", "FROM_KEYRING")
    assert cfgmod.bitbucket_token(cfg) == "FROM_KEYRING"

    # БД перебивает keyring
    store.set_workspace_secret(ws.id, "bitbucket.token", "FROM_DB")
    assert cfgmod.bitbucket_token(cfg) == "FROM_DB"

    # переменная окружения перебивает всё
    monkeypatch.setenv("BITBUCKET_TOKEN", "FROM_ENV")
    assert cfgmod.bitbucket_token(cfg) == "FROM_ENV"
    store.close()


def test_db_secrets_write_goes_to_db_not_keyring(tmp_path, monkeypatch):
    from jwu.core.store import Store
    from jwu.core.workspaces import config_for_workspace

    m = _mem(monkeypatch)
    store = Store(tmp_path / "state.db")
    ws = store.get_workspace_by_slug("work")
    cfg = config_for_workspace(store, ws)

    cfgmod.set_slot(cfg, "jira.token", "NEW")
    assert store.get_workspace_secret(ws.id, "jira.token") == "NEW"
    assert m.get_password("jira-pat", "jira") is None  # в keyring не пишем
    store.close()


def test_legacy_migration_moves_config_and_secrets_once(tmp_path, monkeypatch):
    from jwu.core import secrets, workspaces
    from jwu.core.store import Store

    m = _mem(monkeypatch)
    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr(cfgmod, "config_path", lambda: cfg_path)
    cfg = Config()
    cfg.jira.base_url = "https://jira.acme.com"
    cfg.jira.username = "alice"
    cfg.bitbucket.repo = "server"
    cfgmod.save_config(cfg, cfg_path)
    secrets.set_secret("jira-pat", "jira", "JTOK")
    secrets.set_secret("jira-login", "alice", "JPW")

    store = Store(tmp_path / "state.db")
    fields, moved = workspaces.migrate_legacy_config(store)
    assert moved == 2 and fields > 0

    ws = store.get_workspace_by_slug("work")
    assert store.workspace_settings(ws.id)["jira.base_url"] == "https://jira.acme.com"
    assert store.workspace_secrets(ws.id) == {"jira.token": "JTOK", "jira.password": "JPW"}
    # keyring не чистим: откат на старую версию jwu должен оставаться возможным
    assert m.get_password("jira-pat", "jira") == "JTOK"

    # повторный вызов ничего не делает
    assert workspaces.migrate_legacy_config(store) == (0, 0)
    store.close()


def test_workspace_config_roundtrip_keeps_views(tmp_path, monkeypatch):
    from jwu.core import workspaces
    from jwu.core.store import Store

    _mem(monkeypatch)
    store = Store(tmp_path / "state.db")
    ws = store.get_workspace_by_slug("work")

    cfg = Config()
    cfg.jira.base_url = "https://jira.acme.com"
    cfg.jira.views = dict(cfg.jira.views, mine="assignee = currentUser() ORDER BY key")
    cfg.sdesk.base_url = "https://sdesk.acme.com"
    cfg.sdesk.project = "SDESK"
    workspaces.save_workspace_config(store, ws, cfg)

    loaded = workspaces.config_for_workspace(store, ws)
    assert loaded.jira.base_url == "https://jira.acme.com"
    assert loaded.jira.views["mine"] == "assignee = currentUser() ORDER BY key"
    assert cfgmod.sdesk_enabled(loaded) is True
    store.close()


def test_db_file_is_chmod_600(tmp_path):
    import stat

    from jwu.core.store import Store

    db = tmp_path / "state.db"
    Store(db).close()
    assert stat.S_IMODE(db.stat().st_mode) == 0o600


def test_cloud_path_warning(tmp_path):
    from jwu.core.maintenance import warn_if_cloud_path

    assert warn_if_cloud_path(tmp_path / "state.db") == []
    warns = warn_if_cloud_path(
        tmp_path / "Library" / "Mobile Documents" / "jwu" / "state.db"
    )
    assert warns and "открытом виде" in warns[0]
