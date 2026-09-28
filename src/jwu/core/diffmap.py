"""Карта unified-диффа: какая строка файла в PR добавлена, удалена или контекст.

Нужна, чтобы inline-коммент цеплялся к диффу. Bitbucket привязывает коммент к строке
по тройке (line, lineType, fileType): коммент на добавленную строку с ``lineType=CONTEXT``
создаётся, но в диффе не виден — висит только в общей ленте (у него ``anchor_idx = -1``).
Поэтому тип строки берём из самого диффа PR, а не угадываем.

Стороны: ``TO`` — новая версия файла (строки ``+`` и `` ``), ``FROM`` — старая (строки
``-`` и `` ``). Номер строки — номер в файле на этой стороне, как его показывает
заголовок хунка ``@@ -a,b +c,d @@``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

ADDED, REMOVED, CONTEXT = "ADDED", "REMOVED", "CONTEXT"
LINE_TYPES = (ADDED, REMOVED, CONTEXT)
TO, FROM = "TO", "FROM"


class DiffLineError(ValueError):
    """Строки нет в диффе PR — inline-коммент к ней не привяжется."""


@dataclass
class DiffLine:
    kind: str                 # ADDED | REMOVED | CONTEXT
    text: str
    old: int | None = None    # номер строки в старой версии (FROM)
    new: int | None = None    # номер строки в новой версии (TO)


@dataclass
class FileDiff:
    path: str                 # путь в новой версии (для удалённого файла — в старой)
    old_path: str = ""
    lines: list[DiffLine] = field(default_factory=list)

    def at(self, side: str, line: int) -> DiffLine | None:
        attr = "new" if side == TO else "old"
        for ln in self.lines:
            if getattr(ln, attr) == line:
                return ln
        return None

    def ranges(self, side: str) -> list[tuple[int, int]]:
        """Непрерывные диапазоны строк стороны, которые есть в диффе (для текста ошибки)."""
        attr = "new" if side == TO else "old"
        nums = sorted(n for n in (getattr(ln, attr) for ln in self.lines) if n is not None)
        out: list[tuple[int, int]] = []
        for n in nums:
            if out and n == out[-1][1] + 1:
                out[-1] = (out[-1][0], n)
            else:
                out.append((n, n))
        return out


def _strip_prefix(p: str) -> str:
    return p[2:] if p[:2] in ("a/", "b/") else p


def parse(diff: str) -> dict[str, FileDiff]:
    """Unified diff → {путь: FileDiff}. Файл без хунков (бинарный) остаётся с пустыми lines."""
    files: dict[str, FileDiff] = {}
    cur: FileDiff | None = None
    old_no = new_no = 0
    in_hunk = False
    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", raw)
            old_p, new_p = (m.group(1), m.group(2)) if m else ("", "")
            cur = FileDiff(path=new_p, old_path=old_p)
            files[new_p] = cur
            in_hunk = False
            continue
        if cur is None:
            continue
        m = _HUNK_RE.match(raw)
        if m:
            old_no, new_no = int(m.group(1)), int(m.group(3))
            in_hunk = True
            continue
        if not in_hunk:
            # заголовки файла: уточняем пути (--- /dev/null у нового, +++ /dev/null у удалённого)
            if raw.startswith("--- ") and raw[4:] != "/dev/null":
                cur.old_path = _strip_prefix(raw[4:])
            elif raw.startswith("+++ ") and raw[4:] == "/dev/null":
                files.pop(cur.path, None)
                cur.path = cur.old_path or cur.path
                files[cur.path] = cur
            continue
        if raw.startswith("\\"):
            continue  # «\ No newline at end of file» и пометки об обрезке
        tag, text = raw[:1], raw[1:]
        if tag == "+":
            cur.lines.append(DiffLine(ADDED, text, new=new_no))
            new_no += 1
        elif tag == "-":
            cur.lines.append(DiffLine(REMOVED, text, old=old_no))
            old_no += 1
        else:
            cur.lines.append(DiffLine(CONTEXT, text, old=old_no, new=new_no))
            old_no += 1
            new_no += 1
    return files


def _fmt_ranges(ranges: list[tuple[int, int]]) -> str:
    if not ranges:
        return "—"
    shown = [f"{a}" if a == b else f"{a}–{b}" for a, b in ranges[:12]]
    return ", ".join(shown) + (" …" if len(ranges) > 12 else "")


def locate(diff: str, path: str, line: int, *, side: str | None = None,
           line_type: str | None = None) -> tuple[str, str]:
    """Тип строки для inline-коммента: (lineType, fileType).

    ``side`` — TO (новая версия, по умолчанию) или FROM (удалённая строка). Явный
    ``line_type`` тоже задаёт сторону (REMOVED → FROM) и сверяется с диффом: попросить
    ADDED для строки контекста — ошибка, а не молчаливая отправка «мимо».
    """
    if line_type is not None:
        line_type = line_type.upper()
        if line_type not in LINE_TYPES:
            raise DiffLineError(f"line_type «{line_type}»: одно из {', '.join(LINE_TYPES)}")
    if side is None:
        side = FROM if line_type == REMOVED else TO
    side = side.upper()
    if side not in (TO, FROM):
        raise DiffLineError(f"side «{side}»: TO (новая версия) или FROM (старая)")
    files = parse(diff)
    fd = files.get(path)
    if fd is None:
        names = ", ".join(sorted(files)[:15]) or "—"
        raise DiffLineError(f"Файла {path} нет в диффе PR. Файлы PR: {names}")
    ln = fd.at(side, int(line))
    where = "новой версии" if side == TO else "старой версии"
    if ln is None:
        raise DiffLineError(
            f"Строки {line} ({where}) нет в диффе {path} — коммент к ней не привяжется. "
            f"В диффе есть строки: {_fmt_ranges(fd.ranges(side))}."
        )
    if line_type is not None and line_type != ln.kind:
        raise DiffLineError(
            f"Строка {path}:{line} ({where}) в диффе — {ln.kind}, а не {line_type}."
        )
    return ln.kind, (FROM if ln.kind == REMOVED else TO)


def numbered(diff: str) -> str:
    """Дифф с номерами строк слева: «старая новая │ ±текст» — для ревью и точных file:line.

    Номер справа — строка новой версии файла: ровно его передают в inline-коммент.
    """
    out: list[str] = []
    for fd in parse(diff).values():
        out.append(f"=== {fd.path}" + (f" (было {fd.old_path})" if fd.old_path and fd.old_path != fd.path else ""))
        prev_new = prev_old = None
        for ln in fd.lines:
            # разрыв между хунками — отдельной строкой, чтобы не читать их как соседние
            if (prev_new is not None and ln.new is not None and ln.new != prev_new + 1
                    and ln.kind != REMOVED) or (prev_old is not None and ln.old is not None
                                               and ln.old != prev_old + 1 and ln.kind == REMOVED):
                out.append("        ⋮")
            sign = {ADDED: "+", REMOVED: "-"}.get(ln.kind, " ")
            old = "" if ln.old is None else str(ln.old)
            new = "" if ln.new is None else str(ln.new)
            out.append(f"{old:>5} {new:>5} │{sign}{ln.text}")
            prev_new = ln.new if ln.new is not None else prev_new
            prev_old = ln.old if ln.old is not None else prev_old
    return "\n".join(out) + ("\n" if out else "")
