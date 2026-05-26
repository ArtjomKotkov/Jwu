import keyring

from jwu.core import secrets as sec


class _MemKeyring(keyring.backend.KeyringBackend):
    priority = 1  # type: ignore[assignment]

    def __init__(self):
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self._store.get((service, username))

    def set_password(self, service, username, password):
        self._store[(service, username)] = password

    def delete_password(self, service, username):
        self._store.pop((service, username), None)


def _use_mem(monkeypatch):
    mem = _MemKeyring()
    monkeypatch.setattr(keyring, "get_password", mem.get_password)
    monkeypatch.setattr(keyring, "set_password", mem.set_password)
    monkeypatch.setattr(keyring, "delete_password", mem.delete_password)
    return mem


def test_set_then_get(monkeypatch):
    _use_mem(monkeypatch)
    sec.set_secret("jira-pat", "jira", "tok123")
    assert sec.get_secret("jira-pat", "jira") == "tok123"


def test_missing_returns_none(monkeypatch):
    _use_mem(monkeypatch)
    assert sec.get_secret("nope", "nobody") is None


def test_env_takes_precedence(monkeypatch):
    _use_mem(monkeypatch)
    sec.set_secret("jira-pat", "jira", "from-keyring")
    monkeypatch.setenv("JIRA_TOKEN", "from-env")
    assert sec.get_secret("jira-pat", "jira", env_var="JIRA_TOKEN") == "from-env"


def test_keyring_error_on_read_returns_none(monkeypatch):
    def boom(service, username):
        raise keyring.errors.KeyringError("no backend")
    monkeypatch.setattr(keyring, "get_password", boom)
    assert sec.get_secret("jira-pat", "jira") is None


def test_delete(monkeypatch):
    _use_mem(monkeypatch)
    sec.set_secret("s", "a", "v")
    sec.delete_secret("s", "a")
    assert sec.get_secret("s", "a") is None
