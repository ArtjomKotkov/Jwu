"""Keep the Hermes project skill catalog in step with the bundled catalog."""

import re
from pathlib import Path

from jwu.skills_install import EXPECTED_SKILLS, install_skills


ROOT = Path(__file__).resolve().parents[1]
HERMES_SKILLS = ROOT / ".hermes" / "skills"
BUNDLED_SKILLS = ROOT / "src" / "jwu" / "skills"
ADDON_SKILLS = {
    "jwu-4test-message",
    "jwu-duty",
    "jwu-qa-triage",
    "jwu-reportbot",
}


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


def test_native_role_samples_install_with_all_references(tmp_path):
    skill = "jwu-hermes-role-samples"
    roles = {"reviewer-jwu-sample", "review-filter-sample", "voice-writer-sample"}
    assert (skill, "добавлен") in install_skills(tmp_path)
    assert skill in EXPECTED_SKILLS
    project = HERMES_SKILLS / skill
    installed = tmp_path / skill
    assert (installed / "SKILL.md").read_bytes() == (project / "SKILL.md").read_bytes()
    assert {p.stem for p in (installed / "references").glob("*.md")} == roles
    for role in roles:
        ref = f"references/{role}.md"
        assert ref in (installed / "SKILL.md").read_text(encoding="utf-8")
        assert (installed / ref).read_bytes() == (project / ref).read_bytes()


def test_native_review_and_voice_callers_do_not_require_a_private_role_catalog():
    for name in ("job-review", "review-pr", "review-queue", "pr-comment", "voice-rewrite"):
        path = HERMES_SKILLS / f"jwu-{name}" / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        assert 'skill_view(name="jwu-hermes-roles")' not in text, path
        dependency = "jwu-voice-rewrite" if name == "pr-comment" else "jwu-hermes-role-samples"
        assert dependency in text, path
