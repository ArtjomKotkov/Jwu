"""Check CLI installs never fall through to another client's skill inventory."""

from pathlib import Path
import re

import pytest
from typer.testing import CliRunner

from jwu.cli.main import app
from jwu.skills_install import EXPECTED_SKILLS, install_skills, skill_bundle


HERMES_ONLY = {
    "jwu-hermes-role-samples", "jwu-4test-message", "jwu-duty", "jwu-qa-triage", "jwu-reportbot",
}
ROLE_REFERENCES = {
    "references/review-filter-sample.md", "references/reviewer-jwu-sample.md",
    "references/voice-writer-sample.md",
}
ORCHESTRATOR_SUPPORT = {
    "references/hermes-worker-handoff.md", "references/install.md", "scripts/run_with_gateway.sh",
    "scripts/worker_launch.py", "scripts/worker_relay.py",
}


@pytest.fixture
def client_homes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / "codex-custom"))
    monkeypatch.setenv("HERMES_HOME", str(home / "hermes-custom"))
    return home


def names(root: Path):
    return {p.parent.name for p in root.glob("*/SKILL.md")}


def test_explicit_hermes_install_only_writes_its_bundle(client_homes):
    profile = client_homes / "hermes-custom"
    private = profile / "skills/jwu/jwu-hermes-roles/SKILL.md"
    private.parent.mkdir(parents=True)
    private.write_text("private")
    old = profile / "skills/jwu/jwu-hermes-orchestrator/SKILL.md"
    old.parent.mkdir()
    old.write_text("legacy left for its owner")
    config = profile / "config.yaml"
    config.write_text("unchanged")
    result = CliRunner().invoke(app, ["install", "--for", "jwu-hermes"])
    assert result.exit_code == 0, result.output
    bundle = profile / "skills/jwu-hermes"
    assert names(bundle) == EXPECTED_SKILLS | HERMES_ONLY
    assert private.read_text() == "private"
    assert config.read_text() == "unchanged"
    assert old.read_text() == "legacy left for its owner"
    assert list(client_homes.iterdir()) == [profile]


@pytest.mark.parametrize(("mode", "clients"), [
    ("both", {".claude", ".cursor"}),
    ("all", {".claude", ".cursor", "codex-custom"}),
])
def test_aggregate_targets_preserve_original_clients_without_hermes(client_homes, mode, clients):
    result = CliRunner().invoke(app, ["install", "--for", mode, "--skip-mcp"])
    assert result.exit_code == 0, result.output
    assert {p.name for p in client_homes.iterdir()} == clients
    for client in clients:
        root = client_homes / client / "skills"
        assert names(root) == EXPECTED_SKILLS
        instruction = (root / "jwu-orchestrator/SKILL.md").read_text()
        assert "session_mailbox" not in instruction


@pytest.mark.parametrize("target", ["claude", "cursor", "codex", "jwu-hermes"])
def test_exact_bundle_resources_and_installed_file_contents(tmp_path, target):
    expected_names = EXPECTED_SKILLS | HERMES_ONLY if target == "jwu-hermes" else EXPECTED_SKILLS
    expected_files = {f"{name}/SKILL.md" for name in expected_names}
    expected_files.add("jwu-session-init/references/clarify-menu-russian.md")
    if target == "jwu-hermes":
        expected_files |= {f"jwu-hermes-role-samples/{ref}" for ref in ROLE_REFERENCES}
        expected_files |= {f"jwu-orchestrator/{ref}" for ref in ORCHESTRATOR_SUPPORT}

    bundle = Path(str(skill_bundle(target)))
    assert not any(p.is_symlink() for p in bundle.rglob("*"))
    source_files = {p.relative_to(bundle).as_posix(): p for p in bundle.rglob("*")
                    if p.is_file() and "__pycache__" not in p.parts}
    assert set(source_files) == expected_files
    dest = tmp_path / "skills"
    assert {name for name, _ in install_skills(dest, target=target)} == expected_names
    installed_files = {p.relative_to(dest).as_posix(): p for p in dest.rglob("*") if p.is_file()}
    assert set(installed_files) == expected_files
    for rel, source in source_files.items():
        assert installed_files[rel].read_bytes() == source.read_bytes(), rel
        if source.name == "SKILL.md":
            text = source.read_text(encoding="utf-8")
            assert re.match(rf"\A---\nname: {re.escape(source.parent.name)}\ndescription: \S", text), rel
            for url in re.findall(r"\]\(([^ ):]+\.md)(?:#[^ )]*)?\)", text):
                assert (source.parent / url).is_file(), (rel, url)
    if target == "jwu-hermes":
        assert (dest / "jwu-orchestrator/scripts/run_with_gateway.sh").stat().st_mode & 0o100
    else:
        for name in ("jwu-orchestrator", "jwu-session-init", "jwu-close-job", "jwu-review-queue"):
            body = source_files[f"{name}/SKILL.md"].read_text()
            assert not any(token in body for token in ("session_mailbox", "archive_worker", "worker_relay.py"))


def test_hermes_install_does_not_follow_existing_category_link(client_homes):
    old = client_homes / "hermes-custom/skills/jwu"
    old.mkdir(parents=True)
    (old.parent / "jwu-hermes").symlink_to(old, target_is_directory=True)
    result = CliRunner().invoke(app, ["install", "--for", "jwu-hermes"])
    assert result.exit_code == 1 and "Символическая ссылка" in result.output
    assert list(old.iterdir()) == []
