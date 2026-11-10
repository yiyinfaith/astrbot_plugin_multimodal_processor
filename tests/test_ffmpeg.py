"""视频 / 音频处理的真实 FFmpeg 输出验证（没有 ffmpeg 时整体跳过）。"""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

import pytest
from PIL import Image

from conftest import FakeEvent, comp, needs_ffmpeg, plain, run

from mmp.core.args import SizeSpec
from mmp.core.errors import ProcessingError, ToolMissing
from mmp.core.ffmpeg import FFmpegRunner
from mmp.processors.audio import ops as audio_ops
from mmp.processors.video import ops as video_ops

pytestmark = needs_ffmpeg

RUNNER = FFmpegRunner(threads=2, memory_mb=1536)


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


@pytest.fixture(scope="module")
def samples(tmp_path_factory):
    root = tmp_path_factory.mktemp("samples")
    video = root / "clip.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=4", "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(video))
    silent = root / "silent.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=160x120:rate=10:duration=2", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(silent))
    # 1 秒声音 + 2 秒静音 + 1 秒声音
    audio = root / "tone.wav"
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=330:duration=1", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=2",
           "-f", "lavfi", "-i", "sine=frequency=550:duration=1", "-filter_complex", "[0:a]aresample=44100,aformat=channel_layouts=mono[a0];[2:a]aresample=44100,aformat=channel_layouts=mono[a2];[a0][1:a][a2]concat=n=3:v=0:a=1[o]",
           "-map", "[o]", str(audio))
    gif = root / "anim.gif"
    frames = [Image.new("RGB", (64, 48), c) for c in ((255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0))]
    frames[0].save(gif, save_all=True, append_images=frames[1:], duration=100, loop=0)
    return {"video": video, "silent": silent, "audio": audio, "gif": gif}


def probe(path: Path):
    return run(RUNNER.probe(path))


def vjob(source: Path, tmp_path: Path) -> video_ops.VideoJob:
    return video_ops.VideoJob(RUNNER, source, probe(source), tmp_path, "clip", 120, crf=28, preset="ultrafast", max_output_side=1920)


def ajob(source: Path, tmp_path: Path) -> audio_ops.AudioJob:
    return audio_ops.AudioJob(RUNNER, source, probe(source), tmp_path, "tone", 120)


def test_probe(samples):
    info = probe(samples["video"])
    assert info.video.codec == "h264" and info.display_size == (320, 240)
    assert info.audio.codec == "aac" and abs(info.duration - 4) < 0.2
    assert probe(samples["silent"]).audio is None


def test_video_to_gif_keeps_animation(samples, tmp_path):
    result = run(video_ops.to_gif(vjob(samples["video"], tmp_path), 10, 160, 2, 1))
    with Image.open(result.path) as image:
        assert image.format == "GIF" and image.width == 160 and image.n_frames >= 18
        assert image.info.get("loop") == 0
    # 宽度不会超过原视频
    big = run(video_ops.to_gif(vjob(samples["video"], tmp_path), 5, 2000, 1))
    with Image.open(big.path) as image:
        assert image.width == 320


def test_gif_to_video(samples, tmp_path):
    result = run(video_ops.gif_to_video(vjob(samples["gif"], tmp_path)))
    info = probe(result.path)
    assert info.video.codec == "h264" and info.display_size == (64, 48) and info.duration > 0.3


@pytest.mark.parametrize("fmt, vcodec", [("mp4", "h264"), ("mkv", "h264"), ("webm", "vp9"), ("mov", "h264")])
def test_video_convert(samples, tmp_path, fmt, vcodec):
    result = run(video_ops.convert(vjob(samples["video"], tmp_path), fmt))
    info = probe(result.path)
    assert result.path.suffix == f".{fmt}" and info.video.codec == vcodec and info.audio is not None
    assert abs(info.duration - 4) < 0.3


def test_video_compress_trim_speed_scale_rotate(samples, tmp_path):
    job = lambda: vjob(samples["video"], tmp_path)  # noqa: E731
    compressed = run(video_ops.compress(job(), 35, 120))
    info = probe(compressed.path)
    assert min(info.display_size) == 120 and "CRF 35" in compressed.note

    trimmed = probe(run(video_ops.trim(job(), 1.0, 2.5)).path)
    assert abs(trimmed.duration - 1.5) < 0.15 and trimmed.audio is not None

    fast = run(video_ops.speed(job(), 2.0))
    info = probe(fast.path)
    assert abs(info.duration - 2.0) < 0.2
    assert abs(info.video.fps - 25) < 1, "加速后帧率应保持原值"
    # 音画同步：音轨时长与视频一致
    audio_len = float(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=duration", "-of", "csv=p=0", str(fast.path)], capture_output=True, text=True).stdout.strip())
    assert abs(audio_len - info.duration) < 0.15

    slow = probe(run(video_ops.speed(job(), 0.5)).path)
    assert abs(slow.duration - 8.0) < 0.3

    scaled = probe(run(video_ops.scale(job(), SizeSpec(short=120))).path)
    assert scaled.display_size == (160, 120)
    exact = probe(run(video_ops.scale(job(), SizeSpec(width=101, height=77))).path)
    assert exact.display_size == (100, 76), "H.264 需要偶数宽高"

    rotated = probe(run(video_ops.rotate(job(), 90)).path)
    assert rotated.display_size == (240, 320)


def test_video_audio_extract_mute_frame(samples, tmp_path):
    for fmt, codec in (("mp3", "mp3"), ("m4a", "aac"), ("wav", "pcm_s16le"), ("ogg", "vorbis"), ("opus", "opus"), ("flac", "flac")):
        result = run(video_ops.extract_audio(vjob(samples["video"], tmp_path), fmt))
        info = probe(result.path)
        assert info.video is None and info.audio.codec == codec, fmt
    muted = probe(run(video_ops.mute(vjob(samples["video"], tmp_path))).path)
    assert muted.audio is None and muted.video is not None
    shot = run(video_ops.frame(vjob(samples["video"], tmp_path), 1.5))
    with Image.open(shot.path) as image:
        assert image.format == "PNG" and image.size == (320, 240)
    with pytest.raises(Exception):
        run(video_ops.frame(vjob(samples["video"], tmp_path), 99))
    with pytest.raises(Exception):
        run(video_ops.extract_audio(vjob(samples["silent"], tmp_path), "mp3"))


@pytest.mark.parametrize("fmt, codec", [("mp3", "mp3"), ("wav", "pcm_s16le"), ("ogg", "vorbis"), ("m4a", "aac"), ("flac", "flac"), ("opus", "opus")])
def test_audio_convert(samples, tmp_path, fmt, codec):
    info = probe(run(audio_ops.convert(ajob(samples["audio"], tmp_path), fmt)).path)
    assert info.audio.codec == codec and abs(info.duration - 4) < 0.15


def test_audio_operations(samples, tmp_path):
    job = lambda: ajob(samples["audio"], tmp_path)  # noqa: E731
    assert abs(probe(run(audio_ops.trim(job(), 0.5, 2)).path).duration - 1.5) < 0.1
    assert abs(probe(run(audio_ops.speed(job(), 2.0)).path).duration - 2.0) < 0.1
    assert abs(probe(run(audio_ops.speed(job(), 0.5)).path).duration - 8.0) < 0.2
    assert abs(probe(run(audio_ops.speed(job(), 2.0, keep_pitch=False)).path).duration - 2.0) < 0.1
    assert probe(run(audio_ops.volume(job(), "db", 6)).path).audio is not None
    assert probe(run(audio_ops.volume(job(), "factor", 0.3)).path).audio is not None
    compressed = run(audio_ops.compress(job(), 32))
    assert compressed.path.suffix == ".mp3" and "32 kbps" in compressed.note
    assert probe(run(audio_ops.mute_range(job(), 0, 1)).path).audio is not None
    desilenced = run(audio_ops.remove_silence(job(), -50, 0.5))
    assert probe(desilenced.path).duration < 3.0, "两秒静音应被删除"


def test_timeout_kills_ffmpeg(tmp_path):
    out = tmp_path / "long.mp4"
    started = time.monotonic()
    with pytest.raises(ProcessingError):
        run(RUNNER.run(["-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=600", "-c:v", "libx264", "-preset", "veryslow"], out, 1.5))
    assert time.monotonic() - started < 10
    assert not out.exists()
    leftover = subprocess.run(["pgrep", "-f", "testsrc2=size=1280x720:rate=30:duration=600"], capture_output=True, text=True).stdout.strip()
    assert leftover == "", "超时后 FFmpeg 进程应被杀掉"


def test_missing_binary(tmp_path):
    runner = FFmpegRunner(str(tmp_path / "ffmpeg"), str(tmp_path / "ffprobe"))
    assert not runner.available
    with pytest.raises(ToolMissing):
        run(runner.probe(tmp_path / "x.mp4"))


def test_end_to_end_video_and_audio_commands_cleanup(samples, tmp_path, make_app):
    app = make_app({"general": {"heavy_concurrency": 1, "queue_limit": 5, "per_user_limit": 3}, "video": {"preset": "ultrafast"}})
    import shutil

    video = tmp_path / "in.mp4"
    shutil.copyfile(samples["video"], video)
    audio = tmp_path / "in.wav"
    shutil.copyfile(samples["audio"], audio)

    def ev(text, media, user="1"):
        return FakeEvent([plain("/" + text), media], message_str=text, sender_id=user)

    vcomp = comp("Video", file=str(video), url="", path="")
    acomp = comp("Record", file=str(audio), url="", path="")
    fcomp = comp("File", name="song.wav", file_=str(audio), url="")

    async def go():
        await asyncio.gather(
            app.handle_command(ev("视频转GIF 0s-1s fps8 40%", vcomp, "1"), "视频转GIF"),
            app.handle_command(ev("视频压缩 低", vcomp, "2"), "视频压缩"),
            app.handle_command(ev("提取音频", vcomp, "3"), "提取音频"),
            app.handle_command(ev("音频转MP3", acomp, "4"), "音频转换"),
            app.handle_command(ev("音频裁剪 0 1", fcomp, "5"), "音频裁剪"),
            app.handle_command(ev("视频信息", vcomp, "6"), "视频信息"),
            app.handle_command(ev("音频信息", acomp, "7"), "音频信息"),
        )

    run(go())
    kinds = app.sender.kinds()
    assert kinds.count("image") == 1 and kinds.count("video") == 1 and kinds.count("audio") == 3, app.sender.calls
    assert any("h264" in t for t in app.sender.texts()) and any("pcm_s16le" in t for t in app.sender.texts())
    assert list(app.tasks.root.glob("task-*")) == []

    # 时长超限
    strict = make_app({"video": {"max_duration": 1}})
    run(strict.handle_command(ev("视频静音", vcomp), "视频静音"))
    assert "超过限制" in strict.sender.texts()[0]
    # 把音频当视频
    run(strict.handle_command(ev("视频静音", acomp), "视频静音"))
    assert strict.sender.texts()[1]


def test_mjpeg_video_counts_but_cover_art_does_not():
    from mmp.core.ffmpeg import parse_probe

    video = {"codec_type": "video", "codec_name": "mjpeg", "width": 640, "height": 480}
    cover = {**video, "disposition": {"attached_pic": 1}}
    assert parse_probe({"streams": [video]}).video is not None
    assert parse_probe({"streams": [cover]}).video is None
