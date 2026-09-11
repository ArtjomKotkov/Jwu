"""Штатный бэкап и восстановление: один архив вместо ручного RESTORE.md.

Переезд между машинами повторяется, и каждый раз собирался руками: снять базу так,
чтобы не поймать полузаписанную страницу, не забыть config.toml, вспомнить, какие
проектные субагенты и скиллы в ``~/.claude`` — свои, а какие едут с пакетом. Здесь это
одна команда в обе стороны.

В архиве:

- ``state.db`` — консистентная копия через ``VACUUM INTO`` (можно при работающем jwu);
  по умолчанию С СЕКРЕТАМИ воркспейсов в открытом виде (``--no-secrets`` их вычищает
  из копии — тогда после восстановления токены задаются заново);
- ``config.toml`` — глобальный конфиг (из него читается только путь до БД);
- ``claude-extras/`` — проектные субагенты и скиллы из ``~/.claude``, которых нет в
  поставке jwu и которые упоминают jwu (свои ревьюверы, дежурство и т.п.);
- ``manifest.json``, ``SHA256SUMS``, ``RESTORE.md`` — что внутри и как ставить руками.

Восстановление не смешивает: существующую БД не перезаписывает без ``--force`` (для
слияния есть ``jwu memory import``), а перед перезаписью снимает копию рядом. Путь до
БД в config.toml переписывается под текущего пользователя, если указывал в чужой дом.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sqlite3
import tarfile
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from .. import __version__
from ..skills_install import EXPECTED_AGENTS, EXPECTED_SKILLS, default_agents_dest, default_dest
from .config import config_path, data_dir, db_path

try:  # 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

import tomli_w


class BackupError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_archive_name() -> str:
    return f"jwu-backup-{date.today().isoformat()}.tar.gz"


# --------------------------------------------------------------------------- #
# Бэкап
# --------------------------------------------------------------------------- #


def snapshot_db(src: Path, dest: Path, *, with_secrets: bool = True) -> Path:
    """Консистентная копия БД (``VACUUM INTO``); без секретов — вычистить их из копии."""
    if not src.exists():
        raise BackupError(f"БД не найдена: {src}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        con.execute("VACUUM INTO ?", (str(dest),))
    finally:
        con.close()
    if not with_secrets:
        copy = sqlite3.connect(str(dest))
        try:
            copy.execute("DELETE FROM workspace_secrets")
            copy.commit()
            copy.execute("VACUUM")
        finally:
            copy.close()
    try:
        dest.chmod(0o600)
    except OSError:
        pass
    return dest


def collect_extras(agents_dir: Path | None = None, skills_dir: Path | None = None) -> list[tuple[Path, str]]:
    """Проектные субагенты и скиллы: не из поставки jwu, но про jwu. (src, arcname)."""
    agents_dir = agents_dir or default_agents_dest()
    skills_dir = skills_dir or default_dest()
    out: list[tuple[Path, str]] = []
    if agents_dir.is_dir():
        for md in sorted(agents_dir.glob("*.md")):
            if md.stem in EXPECTED_AGENTS:
                continue
            if "jwu" in md.read_text(encoding="utf-8", errors="replace").lower():
                out.append((md, f"claude-extras/agents/{md.name}"))
    if skills_dir.is_dir():
        for folder in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
            if folder.name in EXPECTED_SKILLS:
                continue
            skill = folder / "SKILL.md"
            if skill.is_file() and "jwu" in skill.read_text(encoding="utf-8", errors="replace").lower():
                out.append((skill, f"claude-extras/skills/{folder.name}/SKILL.md"))
    return out


def render_restore_md(manifest: dict) -> str:
    extras = manifest.get("extras") or []
    lines = [
        f"# jwu — бэкап от {manifest['created_at'][:10]} (jwu {manifest['jwu_version']}, "
        f"{manifest['platform']}, пользователь `{manifest['user']}`)",
        "",
        "Проще всего: `pipx install git+https://github.com/ArtjomKotkov/jwu.git` и "
        f"`jwu restore {manifest['archive']}`. Ниже — то же руками.",
        "",
        "## Что внутри",
        "",
        "| Файл | Что это | Куда класть |",
        "|------|---------|-------------|",
        f"| `state.db` | SQLite-база jwu: воркспейсы, настройки{' и СЕКРЕТЫ (токены в открытом виде)' if manifest.get('with_secrets') else ' (секреты вычищены)'}, "
        "работы, заметки, правила, фичи, снапшоты | `~/.local/share/jwu/state.db` |",
        "| `config.toml` | Глобальный конфиг; реально читается только `[storage].db_path` | `~/.config/jwu/config.toml` |",
    ]
    for arc in extras:
        target = "~/.claude/agents/" if arc.startswith("claude-extras/agents/") else "~/.claude/skills/"
        lines.append(f"| `{arc}` | Проектный субагент/скилл, которого нет в поставке jwu | `{target}` |")
    lines += [
        "",
        "## Восстановление руками",
        "",
        "```bash",
        "mkdir -p ~/.local/share/jwu ~/.config/jwu",
        "cp state.db ~/.local/share/jwu/state.db && chmod 700 ~/.local/share/jwu && chmod 600 ~/.local/share/jwu/state.db",
        "cp config.toml ~/.config/jwu/config.toml   # поправь db_path, если имя пользователя другое",
        "cp -R claude-extras/agents/. ~/.claude/agents/ 2>/dev/null; cp -R claude-extras/skills/. ~/.claude/skills/ 2>/dev/null",
        "pipx install git+https://github.com/ArtjomKotkov/jwu.git && jwu install-claude-skills",
        "claude mcp add --scope user jwu -- ~/.local/bin/jwu-mcp",
        "jwu doctor",
        "```",
        "",
        "Базу класть только на локальный диск, не в iCloud/Dropbox. Папки воркспейсов привязаны",
        "абсолютными путями — склонируй репозитории туда же или перепривяжи (`jwu workspace add-path`).",
        "Архив с секретами передавать только по защищённому каналу и удалить после переноса.",
    ]
    return "\n".join(lines) + "\n"


@dataclass
class BackupReport:
    archive: Path
    files: list[str] = field(default_factory=list)
    with_secrets: bool = True
    size: int = 0


def create_backup(dest: Path | None = None, *, with_secrets: bool = True, extras: bool = True,
                  user: str = "") -> BackupReport:
    """Собрать tar.gz: БД (VACUUM INTO), config.toml, проектные субагенты/скиллы, манифест."""
    dest = Path(dest or (Path.cwd() / default_archive_name())).expanduser()
    # Путь без .tar.gz — это каталог (существующий или нет), имя архива — по дате.
    if dest.is_dir() or not dest.name.endswith(".tar.gz"):
        dest = dest / default_archive_name()
    stem = dest.name[:-7] if dest.name.endswith(".tar.gz") else dest.stem
    work = Path(tempfile.mkdtemp(prefix="jwu-backup-"))
    root = work / stem
    root.mkdir()
    try:
        files: list[tuple[Path, str]] = []
        files.append((snapshot_db(db_path(), root / "state.db", with_secrets=with_secrets), "state.db"))
        cfg = config_path()
        if cfg.exists():
            shutil.copy2(cfg, root / "config.toml")
            files.append((root / "config.toml", "config.toml"))
        extra_list: list[str] = []
        if extras:
            for src, arc in collect_extras():
                target = root / arc
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
                files.append((target, arc))
                extra_list.append(arc)
        manifest = {
            "format": 1, "jwu_version": __version__, "created_at": _now(),
            "platform": platform.system(), "user": user or os.environ.get("USER", ""),
            "archive": dest.name, "with_secrets": with_secrets,
            "db_path": str(db_path()), "extras": extra_list,
            "files": [arc for _, arc in files],
        }
        (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                            encoding="utf-8")
        (root / "RESTORE.md").write_text(render_restore_md(manifest), encoding="utf-8")
        sums = "".join(f"{_sha256(path)}  {arc}\n" for path, arc in files)
        (root / "SHA256SUMS").write_text(sums, encoding="utf-8")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(dest, "w:gz") as tar:
            tar.add(root, arcname=stem)
        try:
            dest.chmod(0o600)
        except OSError:
            pass
        return BackupReport(archive=dest, files=[arc for _, arc in files] + ["manifest.json", "RESTORE.md", "SHA256SUMS"],
                            with_secrets=with_secrets, size=dest.stat().st_size)
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Восстановление
# --------------------------------------------------------------------------- #


@dataclass
class RestoreReport:
    archive: str
    dry_run: bool = False
    db: str = ""
    db_backup: str = ""
    config: str = ""
    extras: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> Path:
    """Распаковать, не давая архиву писать за пределы dest; вернуть корневой каталог."""
    dest = dest.resolve()
    members = tar.getmembers()
    for m in members:
        target = (dest / m.name).resolve()
        if dest not in target.parents and target != dest:
            raise BackupError(f"Подозрительный путь в архиве: {m.name}")
    tar.extractall(dest)
    roots = {Path(m.name).parts[0] for m in members if m.name and not m.name.startswith(".")}
    if len(roots) != 1:
        raise BackupError("В архиве должен быть ровно один корневой каталог")
    return dest / roots.pop()


def _verify_sums(root: Path) -> list[str]:
    sums = root / "SHA256SUMS"
    if not sums.exists():
        return ["SHA256SUMS нет — контрольные суммы не проверялись"]
    bad: list[str] = []
    for line in sums.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, _, name = line.partition("  ")
        path = root / name.strip()
        if not path.exists() or _sha256(path) != digest.strip():
            bad.append(name.strip())
    if bad:
        raise BackupError(f"Контрольные суммы не сходятся: {', '.join(bad)} — архив повреждён")
    return []


def _rewrite_db_path(config_text: str, target_db: Path) -> str:
    """Переписать [storage].db_path на актуальный путь этой машины."""
    try:
        raw = tomllib.loads(config_text)
    except (ValueError, TypeError):
        return config_text
    raw.setdefault("storage", {})["db_path"] = str(target_db)
    return tomli_w.dumps(raw)


def restore_backup(archive: Path, *, db_only: bool = False, force: bool = False,
                   dry_run: bool = False, target_db: Path | None = None,
                   target_config: Path | None = None, agents_dir: Path | None = None,
                   skills_dir: Path | None = None) -> RestoreReport:
    """Разложить архив по местам. Существующую БД трогаем только с ``force`` (копия рядом)."""
    archive = Path(archive).expanduser()
    if not archive.exists():
        raise BackupError(f"Архив не найден: {archive}")
    report = RestoreReport(archive=str(archive), dry_run=dry_run)
    # Канонический локальный путь: env JWU_DB_PATH, иначе каталог данных. Нарочно НЕ
    # db_path() — старый config.toml мог смотреть в облачную папку, туда класть нельзя.
    env_db = os.environ.get("JWU_DB_PATH")
    target_db = target_db or (Path(env_db).expanduser() if env_db else data_dir() / "state.db")
    target_config = target_config or config_path()
    agents_dir = agents_dir or default_agents_dest()
    skills_dir = skills_dir or default_dest()
    work = Path(tempfile.mkdtemp(prefix="jwu-restore-"))
    try:
        with tarfile.open(archive, "r:*") as tar:
            root = _safe_extract(tar, work)
        report.notes += _verify_sums(root)
        src_db = root / "state.db"
        if not src_db.exists():
            raise BackupError("В архиве нет state.db")
        try:
            con = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
            ok = con.execute("PRAGMA quick_check").fetchone()[0]
            con.close()
        except sqlite3.DatabaseError as exc:
            raise BackupError(f"state.db в архиве повреждена: {exc}") from exc
        if ok != "ok":
            raise BackupError(f"state.db в архиве повреждена: {ok}")

        # БД
        if target_db.exists() and target_db.stat().st_size > 0 and not force:
            raise BackupError(
                f"БД уже есть: {target_db}. Перезаписать — --force (копия останется рядом); "
                f"слить память без потерь — jwu memory import"
            )
        report.db = str(target_db)
        if not dry_run:
            target_db.parent.mkdir(parents=True, exist_ok=True)
            if target_db.exists() and target_db.stat().st_size > 0:
                keep = target_db.with_name(f"{target_db.name}.pre-restore-{date.today().isoformat()}")
                shutil.copy2(target_db, keep)
                report.db_backup = str(keep)
            for suffix in ("-wal", "-shm"):
                stale = Path(f"{target_db}{suffix}")
                if stale.exists():
                    stale.unlink()
            shutil.copy2(src_db, target_db)
            try:
                target_db.parent.chmod(0o700)
                target_db.chmod(0o600)
            except OSError:
                pass
        if db_only:
            return report

        # конфиг
        src_cfg = root / "config.toml"
        if src_cfg.exists():
            text = _rewrite_db_path(src_cfg.read_text(encoding="utf-8"), target_db)
            report.config = str(target_config)
            if not dry_run:
                target_config.parent.mkdir(parents=True, exist_ok=True)
                target_config.write_text(text, encoding="utf-8")
                try:
                    target_config.chmod(0o600)
                except OSError:
                    pass

        # проектные субагенты и скиллы
        extras_root = root / "claude-extras"
        if extras_root.exists():
            for md in sorted((extras_root / "agents").glob("*.md")) if (extras_root / "agents").exists() else []:
                report.extras.append(f"agents/{md.name}")
                if not dry_run:
                    agents_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(md, agents_dir / md.name)
            skills_src = extras_root / "skills"
            if skills_src.exists():
                for folder in sorted(p for p in skills_src.iterdir() if p.is_dir()):
                    if not (folder / "SKILL.md").exists():
                        continue
                    report.extras.append(f"skills/{folder.name}")
                    if not dry_run:
                        (skills_dir / folder.name).mkdir(parents=True, exist_ok=True)
                        shutil.copy2(folder / "SKILL.md", skills_dir / folder.name / "SKILL.md")
        report.notes.append("дальше: jwu install-claude-skills · claude mcp add --scope user jwu -- ~/.local/bin/jwu-mcp · jwu doctor")
        return report
    finally:
        shutil.rmtree(work, ignore_errors=True)
