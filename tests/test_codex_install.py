"""jwu install --for codex: скиллы в ~/.codex/skills и [mcp_servers.jwu] в config.toml без порчи чужого."""

from jwu import skills_install as si

FOREIGN = """# мой конфиг
model = "gpt-5"

[mcp_servers.node_repl]
command = "node"
args = ["repl.js"]

[projects."/Users/me/code"]
trust_level = "trusted"
"""


def test_register_mcp_codex_keeps_foreign_sections(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(FOREIGN)
    assert si.register_mcp_codex(cfg, command="/x/jwu-mcp") == "добавлен"
    text = cfg.read_text()
    assert text.startswith(FOREIGN.rstrip("\n"))                   # чужое — байт в байт
    assert si.codex_mcp_entry(cfg) == {"command": "/x/jwu-mcp", "args": []}
    assert si.register_mcp_codex(cfg, command="/x/jwu-mcp") == "без изменений"
    assert si.register_mcp_codex(cfg, command="/y/jwu-mcp") == "обновлён"
    text = cfg.read_text()
    assert text.count("[mcp_servers.jwu]") == 1 and "/x/jwu-mcp" not in text
    assert '[projects."/Users/me/code"]' in text and "[mcp_servers.node_repl]" in text


def test_register_mcp_codex_middle_section_and_empty_file(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[mcp_servers.jwu]\ncommand = "/old"\nargs = []\n\n[other]\nk = 1\n')
    assert si.register_mcp_codex(cfg, command="/new") == "обновлён"
    assert "[other]\nk = 1" in cfg.read_text() and si.codex_mcp_entry(cfg)["command"] == "/new"
    empty = tmp_path / "new" / "config.toml"
    assert si.register_mcp_codex(empty, command="/z") == "добавлен"
    assert si.codex_mcp_entry(empty)["command"] == "/z"


def test_install_for_codex_cli(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from jwu.cli import main as cli

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setattr(si, "mcp_command", lambda: "/p/jwu-mcp")
    r = CliRunner().invoke(cli.app, ["install", "--for", "codex"])
    assert r.exit_code == 0, r.output
    assert (tmp_path / "codex" / "skills" / "jwu-start-job" / "SKILL.md").is_file()
    assert si.codex_mcp_entry(tmp_path / "codex" / "config.toml")["command"] == "/p/jwu-mcp"
    agents = sorted((tmp_path / "codex" / "agents").glob("*.toml"))
    assert agents, r.output
    import importlib.resources as res
    shipped = {e.name[:-3] for e in (res.files("jwu") / "agents").iterdir() if e.name.endswith(".md")}
    assert {a.stem for a in agents} == shipped
    for a in agents:
        data = si.tomllib.loads(a.read_text(encoding="utf-8"))
        assert data["name"] == a.stem and data["description"] and len(data["developer_instructions"]) > 100
        assert "tools" not in data and "color" not in data


def test_doctor_sees_codex_agents(tmp_path, monkeypatch):
    from jwu.core import doctor

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    (tmp_path / "codex").mkdir()
    status = {c.name: c.status for c in doctor.check_codex()}
    assert status["Субагенты в Codex"] == "warn"
    si.install_agents(si.codex_agents_dest(), codex=True)
    status = {c.name: c.status for c in doctor.check_codex()}
    assert status["Субагенты в Codex"] == "ok"
