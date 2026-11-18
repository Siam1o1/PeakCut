"""Pretend videos and a pretend live stream for tests: a test picture and a tone, made with ffmpeg, so nothing needs
the internet. make_buffer writes 6-second pieces exactly as a recording does (p01_00000.ts ... and p01.csv)."""
import subprocess
from pathlib import Path


def _src(seconds, size):
    return ["-f", "lavfi", "-i", f"testsrc=size={size}:rate=15:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]


def make_video(path: Path, seconds=4, size="1280x720"):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", *_src(seconds, size), "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


def make_buffer(buf: Path, seconds=150, size="320x180"):
    buf.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", *_src(seconds, size), "-c:v", "libx264", "-preset", "ultrafast",
                    "-g", "15", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                    "-f", "segment", "-segment_time", "6", "-reset_timestamps", "1", "-segment_format", "mpegts",
                    "-segment_list", "p01.csv", "-segment_list_type", "csv", "p01_%05d.ts"], cwd=buf, check=True)
    return buf
