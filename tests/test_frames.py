"""Видео → кадры через N секунд + контактный лист (JWU-57)."""

import json
import shutil
import subprocess

import pytest
from typer.testing import CliRunner

from jwu.cli import main as cli
from jwu.core import frames

runner = CliRunner()
HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


@pytest.mark.parametrize("value, sec", [
    ("90", 90.0), ("90s", 90.0), ("1:30", 90.0), ("00:01:30", 90.0), ("1m30s", 90.0),
    ("2.5", 2.5), ("1h2m", 3720.0), (12, 12.0), (None, None), ("", None),
])
def test_parse_time(value, sec):
    assert frames.parse_time(value) == sec


def test_parse_time_rejects():
    with pytest.raises(frames.FramesError, match="Не понял"):
        frames.parse_time("полторы минуты")


def test_fmt_time():
    assert frames.fmt_time(5) == "0:05" and frames.fmt_time(95) == "1:35"
    assert frames.fmt_time(3725) == "1:02:05" and frames.fmt_time(2.5) == "0:02.5"


def test_timestamps_range_and_limits():
    assert frames.timestamps(20, 5) == [0, 5, 10, 15]
    assert frames.timestamps(20, 5, start=3, end=14) == [3, 8, 13]
    assert frames.timestamps(10, 4, end=99) == [0, 4, 8]             # не дальше конца видео
    with pytest.raises(frames.FramesError, match="лимите 3"):
        frames.timestamps(60, 5, max_frames=3)
    with pytest.raises(frames.FramesError, match="Пустой отрезок"):
        frames.timestamps(10, 1, start=12)
    with pytest.raises(frames.FramesError, match="больше нуля"):
        frames.timestamps(10, 0)


def test_missing_ffmpeg_is_explicit(tmp_path, monkeypatch):
    video = tmp_path / "a.mp4"
    video.write_bytes(b"x")
    monkeypatch.setattr(frames.shutil, "which", lambda name: None)
    with pytest.raises(frames.FramesError, match="brew install ffmpeg"):
        frames.extract(video, every=1)


def test_missing_file(tmp_path):
    with pytest.raises(frames.FramesError, match="Нет файла"):
        frames.extract(tmp_path / "nope.mp4", every=1)


@pytest.fixture
def video(tmp_path):
    if not HAS_FFMPEG:
        pytest.skip("нет ffmpeg")
    path = tmp_path / "screen.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=12:size=640x360:rate=10",
                    "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def test_extract_frames_sheet_and_manifest(video):
    res = frames.extract(video, every=2, start=1, end=9)
    assert [f.label for f in res.frames] == ["0:01", "0:03", "0:05", "0:07"]
    assert all(frames.Path(f.path).is_file() for f in res.frames)
    assert len(res.sheets) == 1 and frames.Path(res.sheets[0]).is_file()
    assert res.out_dir.endswith("screen.mp4.frames") and abs(res.duration - 12) < 0.5
    manifest = json.loads((frames.Path(res.out_dir) / "frames.json").read_text())
    assert [f["time"] for f in manifest["frames"]] == [1, 3, 5, 7]
    # повторный запуск чистит старые кадры
    again = frames.extract(video, every=6)
    assert sorted(p.name for p in frames.Path(again.out_dir).glob("frame_*.jpg")) == ["frame_0001.jpg", "frame_0002.jpg"]


def test_extract_many_frames_make_several_sheets(video):
    res = frames.extract(video, every=0.5, max_frames=30, sheet=True)
    assert len(res.frames) == 24 and len(res.sheets) == 2        # 16 + 8 миниатюр


def test_cli_video_frames(video):
    r = runner.invoke(cli.app, ["video-frames", str(video), "--every", "4", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    assert [f["label"] for f in data["frames"]] == ["0:00", "0:04", "0:08"]


def test_cli_video_frames_error_without_file(tmp_path):
    r = runner.invoke(cli.app, ["video-frames", str(tmp_path / "x.mp4")])
    assert r.exit_code == 1 and "Нет файла" in r.output
