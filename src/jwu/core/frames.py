"""Видео → кадры для анализа: снимок каждые N секунд + контактный лист.

Тестировщики и поддержка часто прикладывают к задаче запись экрана вместо скриншотов.
Агент видео смотреть не умеет, а кадры — умеет (Read картинки). Здесь видео режется на
кадры через заданный шаг в заданном отрезке, на каждом кадре — отметка времени, а
поверх них собирается **контактный лист**: сетка миниатюр на одной картинке. Порядок
анализа: сначала лист (что вообще происходит и где), потом нужные кадры целиком, при
необходимости — второй проход с мелким шагом вокруг интересного момента.

Работает системный ffmpeg (`brew install ffmpeg`); нет его — внятная ошибка с подсказкой.
Видео и кадры никуда не отправляются: всё локально, в каталоге рядом с видео.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

INSTALL_HINT = "ffmpeg не найден — поставь: brew install ffmpeg (Linux: apt install ffmpeg)"
DEFAULT_MAX_FRAMES = 60
SHEET_COLS = 4
SHEET_ROWS = 4
SHEET_THUMB_WIDTH = 400


class FramesError(RuntimeError):
    pass


def find_tools() -> tuple[str, str]:
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise FramesError(INSTALL_HINT)
    return ffmpeg, ffprobe


def parse_time(value: str | float | int | None) -> float | None:
    """«90», «90s», «1:30», «00:01:30», «1m30s», «2.5» → секунды. None/"" → None."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().lower()
    if re.fullmatch(r"\d+(?:\.\d+)?s?", s):
        return float(s.rstrip("s"))
    if re.fullmatch(r"\d+(?::\d{1,2}){1,2}(?:\.\d+)?", s):
        total = 0.0
        for part in s.split(":"):
            total = total * 60 + float(part)
        return total
    m = re.fullmatch(r"(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+(?:\.\d+)?)s)?", s)
    if m and any(m.groups()):
        h, mi, se = (float(g) if g else 0.0 for g in m.groups())
        return h * 3600 + mi * 60 + se
    raise FramesError(f"Не понял время «{value}» — нужно вида 90, 1:30 или 1m30s")


def fmt_time(seconds: float) -> str:
    """Секунды → «m:ss» (или «h:mm:ss»), с десятыми, если шаг дробный."""
    whole = int(seconds)
    frac = seconds - whole
    h, rest = divmod(whole, 3600)
    m, s = divmod(rest, 60)
    base = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    return base + (f".{int(round(frac * 10))}" if frac >= 0.05 else "")


_DRAWTEXT: dict[str, bool] = {}


def has_drawtext(ffmpeg: str) -> bool:
    """Собран ли ffmpeg с drawtext (подпись времени на кадре). Homebrew-сборка — нет."""
    if ffmpeg not in _DRAWTEXT:
        proc = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True)
        _DRAWTEXT[ffmpeg] = " drawtext " in (proc.stdout or "")
    return _DRAWTEXT[ffmpeg]


@dataclass
class VideoInfo:
    duration: float
    width: int
    height: int


def probe(video: Path) -> VideoInfo:
    _, ffprobe = find_tools()
    proc = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height:format=duration", "-of", "json", str(video)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise FramesError(f"ffprobe не прочитал {video.name}: {proc.stderr.strip()[:200]}")
    data = json.loads(proc.stdout or "{}")
    stream = (data.get("streams") or [{}])[0]
    try:
        duration = float((data.get("format") or {}).get("duration") or 0)
    except ValueError:
        duration = 0.0
    if duration <= 0:
        raise FramesError(f"Не знаю длительность {video.name} — файл битый или это не видео")
    return VideoInfo(duration=duration, width=int(stream.get("width") or 0), height=int(stream.get("height") or 0))


def timestamps(duration: float, every: float, start: float = 0.0, end: float | None = None,
               max_frames: int = DEFAULT_MAX_FRAMES) -> list[float]:
    """Моменты кадров: start, start+every, … < end (не дальше конца видео)."""
    if every <= 0:
        raise FramesError("Шаг должен быть больше нуля")
    stop = min(end if end is not None else duration, duration)
    if start >= stop:
        raise FramesError(f"Пустой отрезок: с {fmt_time(start)} до {fmt_time(stop)} (видео {fmt_time(duration)})")
    n = int((stop - start) / every - 1e-9) + 1
    if n > max_frames:
        raise FramesError(
            f"Вышло бы {n} кадров при лимите {max_frames}: увеличь шаг (сейчас {every:g} с), "
            f"сузь отрезок (--from/--to) или подними --max")
    return [round(start + i * every, 3) for i in range(n)]


@dataclass
class Frame:
    index: int
    time: float
    label: str
    path: str


@dataclass
class FramesResult:
    video: str
    out_dir: str
    duration: float
    every: float
    frames: list[Frame] = field(default_factory=list)
    sheets: list[str] = field(default_factory=list)
    stamped: bool = True

    def as_dict(self) -> dict:
        return asdict(self)


def _drawtext(label: str) -> str:
    text = label.replace(":", r"\:")
    return (f"drawtext=text='{text}':x=12:y=12:fontsize=h/18:fontcolor=white:"
            f"box=1:boxcolor=black@0.6:boxborderw=8")


def _extract_one(ffmpeg: str, video: Path, t: float, out: Path, width: int, label: str,
                 stamp: bool) -> bool:
    """Один кадр. True — с отметкой времени, False — без (ffmpeg собран без drawtext)."""
    scale = f"scale='min({width},iw)':-2"
    for with_text in ((True, False) if stamp else (False,)):
        vf = scale + ("," + _drawtext(label) if with_text else "")
        proc = subprocess.run([ffmpeg, "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", str(video),
                               "-frames:v", "1", "-vf", vf, "-q:v", "3", str(out)],
                              capture_output=True, text=True)
        if proc.returncode == 0 and out.exists():
            return with_text
    raise FramesError(f"ffmpeg не вынул кадр на {label}: {proc.stderr.strip()[:200]}")


def _make_sheets(ffmpeg: str, frame_dir: Path, count: int) -> list[str]:
    per = SHEET_COLS * SHEET_ROWS
    pattern = str(frame_dir / "frame_%04d.jpg")
    out = frame_dir / "sheet_%02d.jpg"
    proc = subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-framerate", "1", "-start_number", "1", "-i", pattern,
         "-vf", f"scale={SHEET_THUMB_WIDTH}:-2,tile={SHEET_COLS}x{SHEET_ROWS}:padding=4:margin=4",
         "-q:v", "3", str(out)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise FramesError(f"ffmpeg не собрал контактный лист: {proc.stderr.strip()[:200]}")
    sheets = sorted(str(p) for p in frame_dir.glob("sheet_*.jpg"))
    return sheets[: (count + per - 1) // per]


def extract(video: Path, *, every: float, start: float | None = None, end: float | None = None,
            width: int = 1280, max_frames: int = DEFAULT_MAX_FRAMES, sheet: bool = True,
            stamp: bool = True, out_dir: Path | None = None) -> FramesResult:
    """Нарезать видео на кадры. Кадры: ``<out>/frame_0001.jpg``…; лист: ``sheet_01.jpg``…;
    манифест с временами кадров — ``frames.json``. По умолчанию out — ``<видео>.frames/``
    рядом с файлом; старые кадры там перезаписываются."""
    video = Path(video).expanduser()
    if not video.is_file():
        raise FramesError(f"Нет файла {video}")
    ffmpeg, _ = find_tools()
    info = probe(video)
    moments = timestamps(info.duration, every, start or 0.0, end, max_frames)
    out = Path(out_dir).expanduser() if out_dir else video.with_name(video.name + ".frames")
    out.mkdir(parents=True, exist_ok=True)
    for old in list(out.glob("frame_*.jpg")) + list(out.glob("sheet_*.jpg")):
        old.unlink()
    result = FramesResult(video=str(video), out_dir=str(out), duration=round(info.duration, 2), every=every)
    stamp = stamp and has_drawtext(ffmpeg)
    result.stamped = stamp
    for i, t in enumerate(moments, start=1):
        label = fmt_time(t)
        path = out / f"frame_{i:04d}.jpg"
        stamped = _extract_one(ffmpeg, video, t, path, width, label, stamp)
        result.stamped = result.stamped and stamped
        result.frames.append(Frame(index=i, time=t, label=label, path=str(path)))
    if sheet and result.frames:
        result.sheets = _make_sheets(ffmpeg, out, len(result.frames))
    (out / "frames.json").write_text(json.dumps(result.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return result
