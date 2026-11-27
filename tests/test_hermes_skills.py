"""Keep the Hermes project skill catalog in step with the bundled catalog."""

import json
import re
from pathlib import Path

from jwu.skills_install import install_skills, skill_bundle


ROOT = Path(__file__).resolve().parents[1]
PROJECT_SKILLS = ROOT / ".hermes" / "skills"
HERMES_SKILLS = skill_bundle("jwu-hermes")
ENTRY = HERMES_SKILLS / "jwu-orchestrator"


def test_project_discovery_exposes_only_the_canonical_hermes_bundle():
    project = PROJECT_SKILLS / "jwu-hermes"
    assert list(PROJECT_SKILLS.iterdir()) == [project]
    assert project.is_symlink()
    assert project.resolve() == (ROOT / "src/jwu/skill_bundles/jwu-hermes").resolve()
    assert {p.resolve() for p in project.glob("*/SKILL.md")} == {
        p.resolve() for p in project.resolve().glob("*/SKILL.md")
    }


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
    assert (skill, "добавлен") in install_skills(tmp_path, target="jwu-hermes")
    project = HERMES_SKILLS / skill
    installed = tmp_path / skill
    assert (installed / "SKILL.md").read_bytes() == (project / "SKILL.md").read_bytes()
    assert {p.stem for p in (installed / "references").glob("*.md")} == roles
    for role in roles:
        ref = f"references/{role}.md"
        assert ref in (installed / "SKILL.md").read_text(encoding="utf-8")
        assert (installed / ref).read_bytes() == (project / ref).read_bytes()


def test_documented_desktop_handoff_checks_worker_before_sending():
    doc = (ENTRY / "references/install.md").read_text()
    assert "--create-only" in doc and "--brief-file" not in doc
    assert doc.index("--create-only") < doc.index("worker_relay.py send")
    before_create = doc.split("--create-only", 1)[0]
    assert "Совместимость шлюза" in before_create and "Совместимость Python payload" in before_create
    assert "Canary" in before_create and "MailboxRepository.worker_parent" in before_create
    assert '--key' not in doc[doc.index('"$S/run_with_gateway.sh" "$S/worker_relay.py" send'):].split('```', 1)[0]


def test_delegated_look_is_tracked_work_even_without_code_changes():
    # These are executable agent instructions: both the entry and the handoff
    # must teach the same interpretation, even when read independently.
    for path in (ENTRY / "SKILL.md", ENTRY / "references/hermes-worker-handoff.md"):
        text = path.read_text(encoding="utf-8")
        assert re.search(r"[«\"]Глянь PROJ-123, отдай воркеру[»\"]", text), path
        assert "Возьми PROJ-123 в работу через Jwu" in text, path
        assert "без подтверждённого `job_id` не приступай к разбору" in text, path
        assert "«Без правок» не означает «без работы Jwu»" in text, path
    start = (HERMES_SKILLS / "jwu-start-job/SKILL.md").read_text(encoding="utf-8")
    assert "прямого запроса без делегирования" in start
    assert "без подтверждённого `job_id` не приступай к разбору" in start
    assert "Просьба только посмотреть или объяснить не требует взятия" not in start


def test_worker_handoff_uses_typed_mailbox_actions_in_order():
    handoff = (ENTRY / "references/hermes-worker-handoff.md").read_text()
    commands = [json.loads(line) for line in handoff.splitlines() if line.startswith('{"action"')]
    report, confirm, request, archive = commands
    assert set(report) == {"action", "kind", "recipient", "request_id", "text"}
    assert report["action"] == "send" and report["kind"] == "report"
    assert report["recipient"] == "ORCHESTRATOR_ID"
    assert not any(value in report["text"] for value in (report["recipient"], report["request_id"]))
    assert confirm == {"action": "confirm_report", "request_id": "confirm-1", "report_id": "REPORT_ID"}
    assert request == {"action": "request_archive", "request_id": "request-archive-1", "confirmation_id": "CONFIRMATION_ID"}
    assert archive == {"action": "archive_worker", "request_id": "archive-1", "archive_request_id": "ARCHIVE_REQUEST_ID"}
    assert len({command["request_id"] for command in commands}) == 4
    for token in ("message.id", "message.kind=report_confirmation", "message.reply_to=REPORT_ID",
                  "message.kind=archive_request", "message.reply_to=CONFIRMATION_ID", "archived: true",
                  "session.info", 'worker_relay.py" check', "не ждёт завершения хода воркера"):
        assert token in handoff


def test_worker_domain_skills_use_handoff_instead_of_requesting_archive():
    handoff = "../jwu-orchestrator/references/hermes-worker-handoff.md"
    for name in ("jwu-close-job", "jwu-review-queue"):
        file = HERMES_SKILLS / name / "SKILL.md"
        text = file.read_text()
        assert handoff in text and (file.parent / handoff).is_file()
        assert not any(token in text for token in ("archive_worker", "request_archive", "session_mailbox"))
    entry = (ENTRY / "SKILL.md").read_text()
    assert "не загружает рабочие скиллы закрытия или ревью" in entry
    assert "не аудирует работу, Jira, код или файлы" in entry
    assert "оставь чат открытым" in entry
    for md in HERMES_SKILLS.rglob("*.md"):
        assert not re.search(r"архив.{0,24}вручную|\bMain\b|archive_self|worker_archive|jwu-hermes-orchestrator",
                             md.read_text()), md


def test_install_checks_mailbox_availability_in_each_chat():
    doc = (ENTRY / "references/install.md").read_text()
    assert 'отдельно в чате оркестратора' in doc and 'в связанном чате воркера' in doc
    assert 'без запасного маршрута' in doc


def test_native_review_and_voice_callers_do_not_require_a_private_role_catalog():
    for name in ("job-review", "review-pr", "review-queue", "pr-comment", "voice-rewrite"):
        path = HERMES_SKILLS / f"jwu-{name}" / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        assert 'skill_view(name="jwu-hermes-roles")' not in text, path
        dependency = "jwu-voice-rewrite" if name == "pr-comment" else "jwu-hermes-role-samples"
        assert f"jwu-hermes/{dependency}" in text, path
