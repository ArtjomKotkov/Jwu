"""Keep the Hermes project skill catalog in step with the bundled catalog."""

import json
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


def test_documented_desktop_handoff_checks_worker_before_sending():
    doc = (BUNDLED_SKILLS / "jwu-hermes-orchestrator/references/install.md").read_text()
    assert "--create-only" in doc and "--brief-file" not in doc
    assert doc.index("--create-only") < doc.index("worker_relay.py send")
    before_create = doc.split("--create-only", 1)[0]
    assert "Совместимость шлюза" in before_create and "Совместимость Python payload" in before_create
    assert "Canary" in before_create and "MailboxRepository.worker_parent" in before_create
    assert '--key' not in doc[doc.index('"$S/run_with_gateway.sh" "$S/worker_relay.py" send'):].split('```', 1)[0]


def test_desktop_mail_examples_keep_routing_out_of_text():
    skill = HERMES_SKILLS / "jwu-hermes-orchestrator" / "SKILL.md"
    mail = skill.read_text().split("## Пассивная почта", 1)[1].split("## Архив", 1)[0]
    report, question, reply = [json.loads(line) for line in mail.splitlines() if line.startswith('{"action"')]
    assert [message["kind"] for message in (report, question, reply)] == ["report", "question", "reply"]
    assert {message["action"] for message in (report, question, reply)} == {"send"}
    assert report["recipient"] == question["recipient"] == "MAIN_ID"
    assert reply["recipient"] == "WORKER_ID" and reply["reply_to"] == "QUESTION_ID"
    assert len({message["request_id"] for message in (report, question, reply)}) == 3
    for message in (report, question, reply):
        assert set(message) <= {"action", "kind", "recipient", "request_id", "text", "reply_to"}
        assert message["recipient"] not in message["text"]
        assert message["request_id"] not in message["text"]
    assert "`prompt.submit`" in mail and "без запасного маршрута" in mail


def test_desktop_orchestration_has_no_automatic_archive_route():
    for root in (HERMES_SKILLS, BUNDLED_SKILLS):
        skill = (root / "jwu-orchestrator" / "SKILL.md").read_text()
        assert 'session_mailbox' in skill
        assert 'Автоматического маршрута нет' in skill
        assert 'вручную в Hermes Desktop' in skill
        close = (root / "jwu-close-job" / "SKILL.md").read_text()
        assert 'вручную в Hermes Desktop' in close
        assert 'просьбу архивировать' not in close


def test_install_checks_mailbox_availability_in_each_chat():
    doc = (BUNDLED_SKILLS / "jwu-hermes-orchestrator/references/install.md").read_text()
    assert 'отдельно в Main и отдельно в чате воркера' in doc
    assert 'без запасного маршрута' in doc


def test_native_review_and_voice_callers_do_not_require_a_private_role_catalog():
    for name in ("job-review", "review-pr", "review-queue", "pr-comment", "voice-rewrite"):
        path = HERMES_SKILLS / f"jwu-{name}" / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        assert 'skill_view(name="jwu-hermes-roles")' not in text, path
        dependency = "jwu-voice-rewrite" if name == "pr-comment" else "jwu-hermes-role-samples"
        assert dependency in text, path
