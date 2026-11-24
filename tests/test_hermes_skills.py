"""Keep the Hermes project skill catalog in step with the bundled catalog."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HERMES_SKILLS = ROOT / ".hermes" / "skills"
BUNDLED_SKILLS = ROOT / "src" / "jwu" / "skills"
ADDON_SKILLS = {"jwu-4test-message", "jwu-duty", "jwu-qa-triage", "jwu-reportbot"}


def test_hermes_catalog_covers_bundled_and_supported_addon_skills():
    bundled = {path.parent.name for path in BUNDLED_SKILLS.glob("jwu-*/SKILL.md")}
    available = {path.parent.name for path in HERMES_SKILLS.glob("jwu-*/SKILL.md")}
    assert available == bundled | ADDON_SKILLS


def test_hermes_skill_metadata_matches_directory():
    for path in HERMES_SKILLS.glob("jwu-*/SKILL.md"):
        text = path.read_text(encoding="utf-8")
        metadata = re.match(r"\A---\n(.+?)\n---\n", text, re.DOTALL)
        assert metadata, path
        assert re.search(rf"^name: {re.escape(path.parent.name)}$", metadata[1], re.M), path
        assert re.search(r"^description: \S.+$", metadata[1], re.M), path
        assert text[metadata.end():].strip(), path


def test_draft_skills_do_not_invoke_profile_creation_commands():
    for name in ("4test-message", "qa-triage", "duty", "pr-comment", "voice-rewrite"):
        path = HERMES_SKILLS / f"jwu-{name}" / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"jwu_voice_profile\s*\(|voice show --json", text), path


def test_cleanup_keeps_worktree_removal_groups_report_only():
    text = (HERMES_SKILLS / "jwu-cleanup" / "SKILL.md").read_text(encoding="utf-8")
    for group in ("closed_jobs", "orphan_worktrees"):
        assert re.search(rf"^\s*\| `{group}` \| Только отчёт:", text, re.M), group
